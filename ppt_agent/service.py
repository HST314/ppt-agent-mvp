from __future__ import annotations

import hashlib, json
from datetime import datetime, timezone

from .errors import ConflictError, ValidationError
from .fsm import TaskState, transition
from .gateways import FakeGenerationGateway, FakeHtmlBuilder, FakeInspectionGateway, FakeSkillLoader
from .schema import DeliveryManifest, InspectionReport, IssueDisposition
from .p2 import canonical, digest, now, parse_task_card, questions_for, scan_resources, validate_answer
from .schema import ClarificationSet, ResourceManifest, TaskCard, TaskInputSnapshot

def utcnow(): return datetime.now(timezone.utc).isoformat()
def fingerprint(value): return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":")).encode()).hexdigest()

class TaskService:
    def __init__(self,store,generator=None,inspector=None,skills=None,builder=None):
        self.store=store; self.generator=generator or FakeGenerationGateway(); self.inspector=inspector or FakeInspectionGateway(); self.skills=skills or FakeSkillLoader(); self.builder=builder or FakeHtmlBuilder()
    def create(self,task_id,mode="manual"):
        if mode not in {"manual","auto"}: raise ValidationError("mode 只能是 manual 或 auto")
        s=TaskState(task_id=task_id,mode=mode); self.store.create(task_id,s.to_dict()); return s.to_dict()
    def get(self,task_id): return self.store.checkpoint(task_id)
    def command(self,task_id,command_id,action,actor="system",payload=None):
        request={"action":action,"actor":actor,"payload":payload or {}}
        with self.store.lock(task_id):
            prior=[e for e in self.store.events(task_id) if e["command_id"]==command_id]
            if prior:
                if prior[0]["request_hash"] != fingerprint(request): raise ConflictError("command_id 请求内容冲突")
                return prior[0]["result"]
            old=TaskState.parse(self.get(task_id)); new=transition(old,action,actor=actor)
            result=new.to_dict()
            event={"event_id":hashlib.sha256(f"{task_id}:{command_id}".encode()).hexdigest()[:24],"command_id":command_id,"action":action,"actor":actor,"request_hash":fingerprint(request),"at":utcnow(),"from":old.to_dict(),"to":result,"result":result}
            self.store.commit(task_id,result,event); return result
    def versions(self,task_id,kind=None): return self.store.versions(task_id,kind)
    def version(self,task_id,digest): return self.store.artifact(task_id,digest)
    def compare(self,task_id,left,right): return {"left":left,"right":right,"equal":self.version(task_id,left)==self.version(task_id,right)}
    def events(self,task_id): return self.store.events(task_id)
    def import_input(self,task_id,source,source_format="json",rebuild=False):
        with self.store.lock(task_id):
            state=TaskState.parse(self.get(task_id))
            existing=self.versions(task_id,"input-snapshot")
            if existing and not rebuild: raise ConflictError("输入已冻结；采用新资料须显式重建快照")
            if rebuild and state.stage not in {state.stage.CREATED,state.stage.CLARIFICATION}: raise ConflictError("大纲阶段后不可重建输入快照")
            card=parse_task_card(source,source_format)
            card_json={"task_id":task_id,"goal":card.get("goal","待澄清"),"audience":card.get("audience","待澄清"),"topic":card.get("topic","待澄清"),"source_format":source_format,"schema_version":"1.0"}
            parsed_card=TaskCard.parse(card_json); card_hash=self.store.put_version(task_id,"task-card",canonical(parsed_card.to_dict()),{"normalized":card})
            resources,warnings=scan_resources(self.store.resource_root(task_id))
            schema_resources=[{k:r[k] for k in ("resource_id","uri","media_type","content_hash")} for r in resources]
            manifest_seed={"task_id":task_id,"resources":schema_resources,"warnings":warnings}
            manifest=ResourceManifest.parse({"manifest_id":f"manifest-{digest(canonical(manifest_seed))[:16]}","task_id":task_id,"resources":schema_resources,"content_hash":digest(canonical(manifest_seed)),"created_at":now(),"schema_version":"1.0"})
            manifest_hash=self.store.put_version(task_id,"resource-manifest",canonical(manifest.to_dict()),{"resources":resources,"warnings":warnings})
            questions=questions_for(card)
            clarification=ClarificationSet.parse({"clarification_id":f"clarification-{digest(canonical(questions))[:16]}","task_id":task_id,"questions":tuple(q["prompt"] for q in questions),"assumptions":tuple(card["assumptions"]),"confirmed":not questions,"schema_version":"1.0"})
            clarification_hash=self.store.put_version(task_id,"clarification",canonical(clarification.to_dict()),{"questions":questions,"answers":{},"invalidated":[]})
            snapshot=TaskInputSnapshot.parse({"snapshot_id":f"snapshot-{digest((card_hash+manifest_hash).encode())[:16]}","task_id":task_id,"task_card_hash":card_hash,"resource_manifest_hash":manifest_hash,"created_at":now(),"schema_version":"1.0"})
            snapshot_hash=self.store.put_version(task_id,"input-snapshot",canonical(snapshot.to_dict()),{"clarification_hash":clarification_hash,"rebuild_of":existing[-1]["hash"] if existing else None})
            new=TaskState.parse(state.to_dict()); new=TaskState(**{**new.__dict__,"stage":new.stage.CLARIFICATION,"status":new.status.WAITING_FOR_USER if questions else new.status.READY,"waiting_reason":"missing_required_input" if questions else None,"required_action":"answer_clarifications" if questions else None,"revision":new.revision+1})
            event={"event_id":hashlib.sha256(f"{task_id}:input:{snapshot_hash}".encode()).hexdigest()[:24],"command_id":f"input-{snapshot_hash[:16]}","action":"rebuild_input" if existing else "import_input","actor":"user","request_hash":snapshot_hash,"at":utcnow(),"from":state.to_dict(),"to":new.to_dict(),"result":{"snapshot_hash":snapshot_hash}}
            self.store.commit(task_id,new.to_dict(),event)
            return {"state":new.to_dict(),"snapshot":snapshot.to_dict(),"snapshot_hash":snapshot_hash,"task_card":card,"manifest":{**manifest.to_dict(),"resources":resources,"warnings":warnings},"clarification":{**clarification.to_dict(),"details":questions,"answers":{}},"clarification_hash":clarification_hash}
    def input_view(self,task_id):
        snapshots=self.versions(task_id,"input-snapshot")
        if not snapshots: return {"state":self.get(task_id),"snapshot":None}
        item=snapshots[-1]; snapshot=json.loads(self.version(task_id,item["hash"])); meta=item["metadata"]
        ch=meta["clarification_hash"]
        # The frozen input points at the initial set; answers are append-only
        # clarification versions, so select the newest committed answer event.
        for event in reversed(self.events(task_id)):
            if event["action"] == "answer_clarification": ch=event["result"]["clarification_hash"]; break
        cv=next(v for v in self.versions(task_id,"clarification") if v["hash"]==ch)
        return {"state":self.get(task_id),"snapshot":snapshot,"snapshot_hash":item["hash"],"clarification":{**json.loads(self.version(task_id,ch)),**cv["metadata"]}}
    def answer_clarification(self,task_id,question_id,answer):
        view=self.input_view(task_id); clarification=view.get("clarification")
        if not clarification: raise ConflictError("尚未生成澄清问题")
        question=next((q for q in clarification["questions"] if isinstance(q,dict) and q["question_id"]==question_id),None)
        # Metadata uses details to avoid changing the stable P1 ClarificationSet wire schema.
        if question is None: question=next((q for q in clarification.get("details",[]) if q["question_id"]==question_id),None)
        if question is None: raise ValidationError("澄清问题不存在")
        value=validate_answer(question,answer); answers=dict(clarification.get("answers",{})); changed=question_id in answers and answers[question_id]!=value; answers[question_id]=value
        details=clarification.get("details",clarification.get("questions",[])); pending=[q for q in details if q["blocking"] and q["question_id"] not in answers]
        payload={"questions":details,"answers":answers,"invalidated":(["narrative","outline","sample","deck","inspection","delivery"] if changed else clarification.get("invalidated",[]))}
        model=ClarificationSet.parse({"clarification_id":f"clarification-{digest(canonical(payload))[:16]}","task_id":task_id,"questions":tuple(q["prompt"] for q in details),"assumptions":tuple(),"confirmed":not pending,"schema_version":"1.0"})
        ch=self.store.put_version(task_id,"clarification",canonical(model.to_dict()),payload)
        state=TaskState.parse(self.get(task_id)); new=TaskState(**{**state.__dict__,"status":state.status.WAITING_FOR_USER if pending else state.status.READY,"waiting_reason":"missing_required_input" if pending else None,"required_action":"answer_clarifications" if pending else None,"revision":state.revision+1})
        event={"event_id":hashlib.sha256(f"{task_id}:answer:{ch}".encode()).hexdigest()[:24],"command_id":f"answer-{ch[:16]}","action":"answer_clarification","actor":"user","request_hash":fingerprint(answer),"at":utcnow(),"from":state.to_dict(),"to":new.to_dict(),"result":{"clarification_hash":ch,"invalidated":payload["invalidated"]}}
        self.store.commit(task_id,new.to_dict(),event); return {"state":new.to_dict(),"clarification_hash":ch,**payload,"confirmed":not pending}
    def run_fake_pipeline(self,task_id):
        state=self.get(task_id)
        if state["stage"] != "created": raise ConflictError("fake 全链路只能从空任务启动")
        # The fake is an executable acceptance path, not a preview-only shortcut.
        for number in range(1,5): self.command(task_id,f"fake-{number}","advance")
        self.command(task_id,"fake-sample-confirm","confirm_sample","user")
        self.command(task_id,"fake-to-deck","advance")
        outline=self.generator.generate("outline",{"task_id":task_id},skill=self.skills.load("outline")["version"])["text"]
        outline_hash=self.store.put_version(task_id,"outline",outline.encode(),{"generator":"fake"})
        html=self.builder.build(outline); deck_hash=self.store.put_version(task_id,"deck",html.encode(),{"outline_hash":outline_hash})
        inspection=self.inspector.inspect(outline,html)
        report=InspectionReport.parse({"report_id":"fake-report","task_id":task_id,"deck_hash":deck_hash,"issues":inspection["issues"],"passed":inspection["passed"],"created_at":utcnow(),"schema_version":"1.0"})
        report_hash=self.store.put_version(task_id,"inspection",json.dumps(report.to_dict(),sort_keys=True).encode(),{"deck_hash":deck_hash})
        self.command(task_id,"fake-to-review","advance")
        dispositions=[]
        for issue in report.issues:
            disposition=IssueDisposition.parse({"disposition_id":f"disposition-{issue.issue_id}","task_id":task_id,"issue_id":issue.issue_id,"action":"resolve","actor":"user","created_at":utcnow(),"schema_version":"1.0"})
            raw=json.dumps(disposition.to_dict(),sort_keys=True).encode()
            dispositions.append(self.store.put_version(task_id,"issue-disposition",raw,{"issue_id":issue.issue_id}))
        self.command(task_id,"fake-blockers","resolve_blockers","user",{"disposition_hashes":dispositions})
        self.command(task_id,"fake-to-delivery","advance")
        manifest=DeliveryManifest.parse({"delivery_id":"fake-delivery","task_id":task_id,"deck_hash":deck_hash,"files":["deck.html"],"confirmed_by":"user","confirmed_at":utcnow(),"schema_version":"1.0"})
        delivery=json.dumps(manifest.to_dict(),sort_keys=True).encode()
        delivery_hash=self.store.put_version(task_id,"delivery",delivery,{"deck_hash":deck_hash})
        final=self.command(task_id,"fake-delivery-confirm","confirm_delivery","user")
        return {"outline_hash":outline_hash,"deck_hash":deck_hash,"report_hash":report_hash,"disposition_hashes":dispositions,"delivery_hash":delivery_hash,"passed":inspection["passed"],"preview":html,"state":final}
