from __future__ import annotations

import hashlib, json
from datetime import datetime, timezone

from .errors import ConflictError, ValidationError
from .fsm import TaskState, transition
from .gateways import FakeGenerationGateway, FakeHtmlBuilder, FakeInspectionGateway, FakeSkillLoader
from .schema import DeliveryManifest, InspectionReport, IssueDisposition

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
