from __future__ import annotations

import hashlib, json, re
from datetime import datetime, timezone

from .errors import ConflictError, ValidationError
from .fsm import TaskState, transition
from .gateways import FakeGenerationGateway, FakeHtmlBuilder, FakeInspectionGateway, FakeSkillLoader
from .schema import DeliveryManifest, InspectionReport, IssueDisposition
from .p2 import canonical, digest, now, parse_task_card, questions_for, scan_resources, validate_answer
from .schema import ClarificationSet, ResourceManifest, TaskCard, TaskInputSnapshot
from .schema import NarrativeDocument, SlideOutline, SampleSelection, DeckArtifact
from .p3 import changed_slide_ids, narrative_markdown, outline_markdown, parse_outline, requested_slide_count
from .p4 import controlled_assets, infer_scope, recommend, render, validate_html

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
            old=TaskState.parse(self.get(task_id))
            if action == "advance" and old.stage == old.stage.SAMPLE and self.versions(task_id,"sample"):
                self._require_current_sample_confirmation(task_id)
            new=transition(old,action,actor=actor)
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
        card=next(v for v in self.versions(task_id,"task-card") if v["hash"]==snapshot["task_card_hash"])
        manifest=next(v for v in self.versions(task_id,"resource-manifest") if v["hash"]==snapshot["resource_manifest_hash"])
        return {"state":self.get(task_id),"snapshot":snapshot,"snapshot_hash":item["hash"],"task_card":card["metadata"]["normalized"],"manifest":{**json.loads(self.version(task_id,snapshot["resource_manifest_hash"])),**manifest["metadata"]},"clarification":{**json.loads(self.version(task_id,ch)),**cv["metadata"]}}
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
    def _current_version(self,task_id,kind):
        for event in reversed(self.events(task_id)):
            # A narrative revision invalidates the previously generated outline.
            if kind == "outline" and event["action"] in {"narrative_generate","narrative_edit"}:
                return None
            if event["action"].startswith(kind + "_") and event["result"].get("hash"):
                return event["result"]["hash"]
        return None
    def _confirmed_narrative_hash(self,task_id):
        for event in reversed(self.events(task_id)):
            if event["action"] == "confirm_narrative": return event["result"]["confirmed_narrative_hash"]
        return None
    def _reset_narrative_gate(self,task_id,artifact_hash):
        state=TaskState.parse(self.get(task_id))
        if state.mode != "manual": return
        new=TaskState(**{**state.__dict__,"stage":state.stage.NARRATIVE,"status":state.status.WAITING_FOR_USER,"waiting_reason":"manual_gate","required_action":"approve_narrative","revision":state.revision+1})
        event={"event_id":hashlib.sha256(f"{task_id}:invalidate-narrative:{artifact_hash}".encode()).hexdigest()[:24],"command_id":f"invalidate-narrative-{artifact_hash[:16]}","action":"invalidate_narrative_confirmation","actor":"system","request_hash":artifact_hash,"at":utcnow(),"from":state.to_dict(),"to":new.to_dict(),"result":{"hash":artifact_hash,"confirmed_narrative_hash":None,"invalidated":["outline","sample","deck"]}}
        self.store.commit(task_id,new.to_dict(),event)
    def _record_p3(self,task_id,kind,model,metadata,action,actor="system"):
        raw=canonical(model.to_dict()); artifact_hash=self.store.put_version(task_id,kind,raw,metadata)
        state=TaskState.parse(self.get(task_id)); new=TaskState(**{**state.__dict__,"revision":state.revision+1})
        event={"event_id":hashlib.sha256(f"{task_id}:{action}:{artifact_hash}".encode()).hexdigest()[:24],"command_id":f"{action}-{artifact_hash[:16]}","action":action,"actor":actor,"request_hash":artifact_hash,"at":utcnow(),"from":state.to_dict(),"to":new.to_dict(),"result":{"hash":artifact_hash,"version":model.version,"affected":metadata.get("affected",[])}}
        self.store.commit(task_id,new.to_dict(),event); return artifact_hash
    def _p3_input(self,task_id):
        view=self.input_view(task_id)
        if not view.get("snapshot") or not view.get("clarification",{}).get("confirmed"):
            raise ConflictError("须先冻结输入并完成阻断澄清")
        return view
    def planning_view(self,task_id):
        state=self.get(task_id); result={"state":state,"narrative":None,"outline":None,"versions":self.versions(task_id)}
        for kind in ("narrative","outline"):
            current=self._current_version(task_id,kind)
            if current:
                item=json.loads(self.version(task_id,current)); meta=next(v["metadata"] for v in self.versions(task_id,kind) if v["hash"]==current)
                result[kind]={**item,"hash":current,"metadata":meta}
        return result
    def generate_narrative(self,task_id,prompt=None,scope="all"):
        view=self._p3_input(task_id); state=TaskState.parse(view["state"])
        skill=self.skills.load("narrative"); prior=self._current_version(task_id,"narrative")
        text=narrative_markdown(view["task_card"])
        if prompt: text += f"\n## 修改要求\n{prompt.strip()}\n"
        version=len(self.versions(task_id,"narrative"))+1; content_hash=digest(text.encode())
        model=NarrativeDocument.parse({"document_id":f"narrative-{content_hash[:16]}","task_id":task_id,"version":version,"markdown":text,"content_hash":content_hash,"created_at":now(),"schema_version":"1.0"})
        metadata={"parent":prior,"action":"generate" if not prior else "regenerate","scope":scope,"summary":"生成整稿叙事结构","skill":{"action":"narrative","version":skill["version"],"hash":digest(skill["content"].encode()),"included":["narrative"],"trimmed":["outline","html","inspection"]},"input_snapshot_hash":view["snapshot_hash"]}
        h=self._record_p3(task_id,"narrative",model,metadata,"narrative_generate")
        if state.stage in {state.stage.CLARIFICATION,state.stage.CREATED}:
            self.command(task_id,f"narrative-stage-{h[:12]}","advance")
        else:
            self._reset_narrative_gate(task_id,h)
        return self.planning_view(task_id)
    def edit_narrative(self,task_id,markdown,summary="直接编辑"):
        self._p3_input(task_id)
        if not isinstance(markdown,str) or not markdown.strip(): raise ValidationError("叙事 Markdown 不得为空")
        prior=self._current_version(task_id,"narrative"); version=len(self.versions(task_id,"narrative"))+1; content_hash=digest(markdown.encode())
        model=NarrativeDocument.parse({"document_id":f"narrative-{content_hash[:16]}","task_id":task_id,"version":version,"markdown":markdown,"content_hash":content_hash,"created_at":now(),"schema_version":"1.0"})
        h=self._record_p3(task_id,"narrative",model,{"parent":prior,"action":"direct_edit","summary":summary,"authoritative":True,"invalidated":["outline","sample","deck"]},"narrative_edit","user")
        self._reset_narrative_gate(task_id,h)
        return self.planning_view(task_id)
    def confirm_narrative(self,task_id):
        current=self._current_version(task_id,"narrative")
        if not current: raise ConflictError("尚未生成叙事结构")
        state=TaskState.parse(self.get(task_id))
        if state.stage==state.stage.NARRATIVE:
            new=transition(state,"advance",actor="user")
            event={"event_id":hashlib.sha256(f"{task_id}:confirm-narrative:{current}".encode()).hexdigest()[:24],"command_id":f"confirm-narrative-{current[:16]}","action":"confirm_narrative","actor":"user","request_hash":current,"at":utcnow(),"from":state.to_dict(),"to":new.to_dict(),"result":{"confirmed_narrative_hash":current}}
            self.store.commit(task_id,new.to_dict(),event)
        return self.planning_view(task_id)
    def generate_outline(self,task_id,prompt=None,slide_ids=None):
        view=self._p3_input(task_id); narrative=self._current_version(task_id,"narrative")
        if not narrative: raise ConflictError("须先生成叙事结构")
        state=TaskState.parse(self.get(task_id))
        if state.mode=="manual" and self._confirmed_narrative_hash(task_id) != narrative: raise ConflictError("manual 模式须先确认当前版本叙事结构")
        skill=self.skills.load("outline"); count=requested_slide_count(view["task_card"]); resources=view["manifest"].get("resources",[])
        current=self._current_version(task_id,"outline")
        if slide_ids:
            if not current or not prompt: raise ValidationError("指定页修改需要现有大纲和修改 Prompt")
            old=json.loads(self.version(task_id,current))["markdown"]; order,blocks=parse_outline(old,resources,None)
            unknown=set(slide_ids)-set(order)
            if unknown: raise ValidationError("指定页面不存在")
            for sid in slide_ids: blocks[sid] += f"\n- 修改要求：{prompt.strip()}"
            prefix=old[:old.index("## [")].rstrip(); text=prefix+"\n\n"+"\n\n".join(blocks[sid] for sid in order)+"\n"
        else:
            text=outline_markdown(view["task_card"],resources,count)
            if prompt: text += f"\n<!-- 修改要求：{prompt.strip()} -->\n"
        return self.edit_outline(task_id,text,"生成逐页大纲",actor="system",skill=skill)
    def edit_outline(self,task_id,markdown,summary="直接编辑",actor="user",skill=None):
        view=self._p3_input(task_id); expected=requested_slide_count(view["task_card"])
        slide_ids,blocks=parse_outline(markdown,view["manifest"].get("resources",[]),expected)
        prior=self._current_version(task_id,"outline"); before={}
        if prior: _,before=parse_outline(json.loads(self.version(task_id,prior))["markdown"],view["manifest"].get("resources",[]),None)
        affected=changed_slide_ids(before,blocks); version=len(self.versions(task_id,"outline"))+1; content_hash=digest(markdown.encode())
        model=SlideOutline.parse({"outline_id":f"outline-{content_hash[:16]}","task_id":task_id,"version":version,"markdown":markdown,"slide_ids":slide_ids,"content_hash":content_hash,"created_at":now(),"schema_version":"1.0"})
        meta={"parent":prior,"action":"generate" if not prior else "edit","summary":summary,"affected":affected,"unchanged":[sid for sid in blocks if sid in before and blocks[sid]==before[sid]],"authoritative":True,"invalidated":{"sample":affected,"deck":affected}}
        if skill: meta["skill"]={"action":"outline","version":skill["version"],"hash":digest(skill["content"].encode()),"included":["outline"],"trimmed":["narrative","html","inspection"]}
        h=self._record_p3(task_id,"outline",model,meta,"outline_generate" if not prior else "outline_edit",actor)
        self._invalidate_sample_gate(task_id,h)
        state=TaskState.parse(self.get(task_id))
        if state.stage==state.stage.NARRATIVE and state.mode=="auto": self.command(task_id,f"auto-outline-stage-{h[:12]}","advance")
        return self.planning_view(task_id)
    def rollback_planning(self,task_id,kind,target_hash):
        if kind not in {"narrative","outline"}: raise ValidationError("版本类型无效")
        target=json.loads(self.version(task_id,target_hash)); known={v["hash"] for v in self.versions(task_id,kind)}
        if target_hash not in known: raise ValidationError("目标版本类型不匹配")
        return self.edit_narrative(task_id,target["markdown"],f"回退自 {target_hash[:12]}") if kind=="narrative" else self.edit_outline(task_id,target["markdown"],f"回退自 {target_hash[:12]}")
    def confirm_outline(self,task_id):
        current=self._current_version(task_id,"outline")
        if not current: raise ConflictError("尚未生成逐页大纲")
        state=TaskState.parse(self.get(task_id))
        if state.stage==state.stage.OUTLINE: self.command(task_id,f"confirm-outline-{current[:12]}","advance","user" if state.mode=="manual" else "system")
        return self.planning_view(task_id)
    def _confirmed_outline_hash(self,task_id):
        for event in reversed(self.events(task_id)):
            if event["action"] == "confirm_outline": return event["result"].get("confirmed_outline_hash")
            if event["action"] == "advance" and event["from"]["stage"] == "outline": return self._current_version(task_id,"outline")
        return None
    def sample_view(self,task_id):
        outline=self._current_version(task_id,"outline"); current=self._current_version(task_id,"sample")
        result={"state":self.get(task_id),"outline_hash":outline,"selection":None,"sample":None,"confirmation":None,"versions":self.versions(task_id,"sample")}
        sels=self.versions(task_id,"sample-selection")
        if sels:
            selected=None
            for event in reversed(self.events(task_id)):
                if event["action"] == "select_samples": selected=event["result"].get("hash"); break
            last=next((item for item in sels if item["hash"]==selected),sels[-1])
            result["selection"]={**json.loads(self.version(task_id,last["hash"])),"hash":last["hash"],"metadata":last["metadata"]}
        if current:
            item=json.loads(self.version(task_id,current)); meta=next(v["metadata"] for v in self.versions(task_id,"sample") if v["hash"]==current)
            result["sample"]={**item,"hash":current,"html":meta["html"],"metadata":meta}
        for event in reversed(self.events(task_id)):
            if event["action"]=="confirm_sample_version":
                sample=result["sample"]; selection=result["selection"]
                if (current and sample and selection
                    and event["result"].get("confirmed_sample_hash")==current
                    and event["result"].get("confirmed_content_hash")==sample["content_hash"]
                    and event["result"].get("confirmed_outline_hash")==outline
                    and event["result"].get("selection_hash")==selection["hash"]
                    and sample["metadata"].get("selection_hash")==selection["hash"]):
                    result["confirmation"]=event["result"]
                break
        return result
    def _require_current_sample_confirmation(self,task_id):
        view=self.sample_view(task_id)
        if not view["confirmation"] or not view["state"]["sample_confirmed"]:
            raise ConflictError("当前大纲、样品内容或页面选择尚未由用户确认")
    def _invalidate_sample_gate(self,task_id,artifact_hash):
        state=TaskState.parse(self.get(task_id))
        if not state.sample_confirmed: return
        new=TaskState(**{**state.__dict__,"sample_confirmed":False,"status":state.status.WAITING_FOR_USER,"waiting_reason":"manual_gate","required_action":"confirm_sample","revision":state.revision+1})
        event={"event_id":hashlib.sha256(f"{task_id}:invalidate-sample:{artifact_hash}".encode()).hexdigest()[:24],"command_id":f"invalidate-sample-{artifact_hash[:16]}","action":"invalidate_sample_confirmation","actor":"system","request_hash":artifact_hash,"at":utcnow(),"from":state.to_dict(),"to":new.to_dict(),"result":{"hash":artifact_hash,"invalidated":["sample_confirmation","deck"]}}
        self.store.commit(task_id,new.to_dict(),event)
    def select_samples(self,task_id,slide_ids=None,count=2):
        outline=self._current_version(task_id,"outline"); state=TaskState.parse(self.get(task_id))
        if not outline or state.stage != state.stage.SAMPLE: raise ConflictError("须先完成并确认逐页大纲")
        data=json.loads(self.version(task_id,outline)); valid=list(data["slide_ids"])
        if slide_ids is None: slide_ids,reasons=recommend(data["markdown"],count)
        else:
            if not isinstance(slide_ids,list) or not slide_ids or len(slide_ids)>len(valid) or len(set(slide_ids))!=len(slide_ids) or any(x not in valid for x in slide_ids): raise ValidationError("样品页面选择无效或重复")
            reasons={sid:"用户选择" for sid in slide_ids}
        seed=canonical({"outline_hash":outline,"slide_ids":slide_ids}); model=SampleSelection.parse({"selection_id":f"selection-{digest(seed)[:16]}","task_id":task_id,"outline_hash":outline,"slide_ids":slide_ids,"confirmed":False,"schema_version":"1.0"})
        h=self.store.put_version(task_id,"sample-selection",canonical(model.to_dict()),{"reasons":reasons})
        state=TaskState.parse(self.get(task_id))
        new=(TaskState(**{**state.__dict__,"sample_confirmed":False,"status":state.status.WAITING_FOR_USER,"waiting_reason":"manual_gate","required_action":"confirm_sample","revision":state.revision+1})
             if state.sample_confirmed else TaskState(**{**state.__dict__,"revision":state.revision+1}))
        event={"event_id":hashlib.sha256(f"{task_id}:select-samples:{h}:{state.revision}".encode()).hexdigest()[:24],"command_id":f"select-samples-{h[:16]}-{state.revision}","action":"select_samples","actor":"user" if slide_ids is not None else "system","request_hash":h,"at":utcnow(),"from":state.to_dict(),"to":new.to_dict(),"result":{"hash":h,"invalidated":["sample_confirmation","deck"] if state.sample_confirmed else []}}
        self.store.commit(task_id,new.to_dict(),event)
        return {**self.sample_view(task_id),"selection":{**model.to_dict(),"hash":h,"metadata":{"reasons":reasons}}}
    def generate_sample(self,task_id,prompt=None):
        view=self.sample_view(task_id); selection=view["selection"]
        if not selection: view=self.select_samples(task_id); selection=view["selection"]
        outline=self._current_version(task_id,"outline")
        if selection["outline_hash"] != outline: raise ConflictError("样品选择已因大纲变化而失效")
        data=json.loads(self.version(task_id,outline)); rules=[]; assets=controlled_assets(self.input_view(task_id)["manifest"],self.store.resource_root(task_id))
        if prompt: rules.append(prompt.strip())
        html_text=validate_html(render(data["markdown"],selection["slide_ids"],rules,assets=assets),selection["slide_ids"],assets.values())
        version=len(self.versions(task_id,"sample"))+1; content_hash=digest(html_text.encode()); model=DeckArtifact.parse({"artifact_id":f"sample-{content_hash[:16]}","task_id":task_id,"version":version,"kind":"sample","outline_hash":outline,"content_hash":content_hash,"created_at":now(),"schema_version":"1.0"})
        prior=self._current_version(task_id,"sample"); meta={"html":html_text,"selection_hash":selection["hash"],"parent":prior,"summary":"生成真实 HTML 样品","scope":"global","global_rules":rules,"local_exceptions":{},"build":"success"}
        h=self._record_p3(task_id,"sample",model,meta,"sample_generate")
        self._invalidate_sample_gate(task_id,h)
        return self.sample_view(task_id)
    def modify_sample(self,task_id,prompt,scope=None,slide_id=None,element_id=None):
        view=self.sample_view(task_id); sample=view["sample"]
        if not sample: raise ConflictError("尚未生成样品")
        if not isinstance(prompt,str) or not prompt.strip(): raise ValidationError("修改 Prompt 不得为空")
        ids=list(view["selection"]["slide_ids"])
        scope,understanding=infer_scope(prompt,slide_id,element_id,scope)
        if scope in {"element","page"} and slide_id not in ids: raise ValidationError("修改页面不在样品中")
        if scope=="element" and not element_id: raise ValidationError("元素级修改必须指定 element_id")
        meta=sample["metadata"]; rules=list(meta.get("global_rules",[])); exceptions={k:list(v) for k,v in meta.get("local_exceptions",{}).items()}
        if scope=="global": rules.append(prompt.strip())
        else: exceptions.setdefault(slide_id,[]).append((f"元素 {element_id}: " if scope=="element" else "")+prompt.strip())
        outline=self._current_version(task_id,"outline"); data=json.loads(self.version(task_id,outline)); assets=controlled_assets(self.input_view(task_id)["manifest"],self.store.resource_root(task_id)); html_text=validate_html(render(data["markdown"],ids,rules,exceptions,assets),ids,assets.values())
        version=len(self.versions(task_id,"sample"))+1; ch=digest(html_text.encode()); model=DeckArtifact.parse({"artifact_id":f"sample-{ch[:16]}","task_id":task_id,"version":version,"kind":"sample","outline_hash":outline,"content_hash":ch,"created_at":now(),"schema_version":"1.0"})
        h=self._record_p3(task_id,"sample",model,{"html":html_text,"selection_hash":view["selection"]["hash"],"parent":sample["hash"],"summary":prompt.strip(),"scope":scope,"scope_understanding":understanding,"slide_id":slide_id,"element_id":element_id,"global_rules":rules,"local_exceptions":exceptions,"build":"success"},"sample_modify","user")
        self._invalidate_sample_gate(task_id,h)
        return self.sample_view(task_id)
    def confirm_sample(self,task_id):
        view=self.sample_view(task_id); sample=view["sample"]; outline=self._current_version(task_id,"outline")
        selection=view["selection"]
        if (not sample or not selection or sample["outline_hash"] != outline
            or selection["outline_hash"] != outline
            or sample["metadata"].get("selection_hash") != selection["hash"]):
            raise ConflictError("须先基于当前大纲和页面选择重新生成样品")
        state=TaskState.parse(self.get(task_id)); new=transition(state,"confirm_sample",actor="user")
        result={"confirmed_outline_hash":outline,"confirmed_sample_hash":sample["hash"],"confirmed_content_hash":sample["content_hash"],"selection_hash":view["selection"]["hash"]}
        event={"event_id":hashlib.sha256(f"{task_id}:confirm-sample:{sample['hash']}".encode()).hexdigest()[:24],"command_id":f"confirm-sample-{sample['hash'][:16]}","action":"confirm_sample_version","actor":"user","request_hash":sample["hash"],"at":utcnow(),"from":state.to_dict(),"to":new.to_dict(),"result":result}
        self.store.commit(task_id,new.to_dict(),event); return self.sample_view(task_id)

    @staticmethod
    def _slide_fragments(html_text):
        return {m.group(1):m.group(0) for m in re.finditer(r'<section class="slide" id="([A-Za-z0-9_-]+)"[\s\S]*?</section>',html_text)}
    def deck_view(self,task_id):
        current=self._current_version(task_id,"deck")
        result={"state":self.get(task_id),"deck":None,"versions":self.versions(task_id,"deck")}
        if current:
            item=json.loads(self.version(task_id,current)); record=next(v for v in result["versions"] if v["hash"]==current)
            result["deck"]={**item,"hash":current,"html":record["metadata"]["html"],"metadata":record["metadata"]}
        return result
    def _record_deck(self,task_id,html_text,outline_hash,metadata,action,actor="system"):
        version=len(self.versions(task_id,"deck"))+1; content_hash=digest(html_text.encode())
        model=DeckArtifact.parse({"artifact_id":f"deck-{content_hash[:16]}","task_id":task_id,"version":version,"kind":"deck","outline_hash":outline_hash,"content_hash":content_hash,"created_at":now(),"schema_version":"1.0"})
        fragments=self._slide_fragments(html_text)
        metadata={**metadata,"html":html_text,"page_hashes":{sid:digest(fragment.encode()) for sid,fragment in fragments.items()}}
        self._record_p3(task_id,"deck",model,metadata,action,actor)
        return self.deck_view(task_id)
    def generate_deck(self,task_id):
        sample_view=self.sample_view(task_id); self._require_current_sample_confirmation(task_id)
        state=TaskState.parse(self.get(task_id))
        if state.stage==state.stage.SAMPLE: self.command(task_id,f"to-deck-{sample_view['sample']['hash'][:12]}","advance","system")
        elif state.stage!=state.stage.DECK: raise ConflictError("当前阶段不能生成全稿")
        outline=self._current_version(task_id,"outline"); data=json.loads(self.version(task_id,outline)); ids=list(data["slide_ids"])
        sample=sample_view["sample"]; meta=sample["metadata"]
        assets=controlled_assets(self.input_view(task_id)["manifest"],self.store.resource_root(task_id))
        html_text=validate_html(render(data["markdown"],ids,meta.get("global_rules",[]),meta.get("local_exceptions",{}),assets),ids,assets.values())
        sample_fragments=self._slide_fragments(sample["html"]); deck_fragments=self._slide_fragments(html_text)
        preserved={sid:digest(deck_fragments[sid].encode())==digest(fragment.encode()) for sid,fragment in sample_fragments.items()}
        if not all(preserved.values()): raise ConflictError("确认样品发生未提示变化")
        return self._record_deck(task_id,html_text,outline,{"parent":self._current_version(task_id,"deck"),"summary":"生成完整 HTML 演示稿","scope":"global","affected":ids,"sample_hash":sample["hash"],"sample_pages_preserved":preserved,"outline_consistent":True,"global_rules":meta.get("global_rules",[]),"local_exceptions":meta.get("local_exceptions",{})},"deck_generate")
    def modify_deck(self,task_id,prompt,change_type="visual",scope=None,slide_ids=None,element_id=None):
        view=self.deck_view(task_id); deck=view["deck"]
        if not deck: raise ConflictError("尚未生成全稿")
        if not isinstance(prompt,str) or not prompt.strip(): raise ValidationError("修改 Prompt 不得为空")
        if change_type not in {"visual","content"}: raise ValidationError("修改类型只能是 visual 或 content")
        all_ids=list(deck["metadata"]["page_hashes"]); slide_ids=slide_ids or []
        chosen=slide_ids[0] if slide_ids else None
        inferred,understanding=infer_scope(prompt,chosen,element_id,scope)
        if inferred in {"page","element"} and (not slide_ids or any(s not in all_ids for s in slide_ids)): raise ValidationError("修改页面不存在")
        affected=all_ids if inferred=="global" else list(slide_ids)
        outline_hash=self._current_version(task_id,"outline"); outline=json.loads(self.version(task_id,outline_hash)); markdown=outline["markdown"]
        rules=list(deck["metadata"].get("global_rules",[])); exceptions={k:list(v) for k,v in deck["metadata"].get("local_exceptions",{}).items()}
        if change_type=="content":
            order,blocks=parse_outline(markdown,self.input_view(task_id)["manifest"].get("resources",[]),None)
            for sid in affected: blocks[sid] += f"\n- 内容修改：{prompt.strip()}"
            markdown=markdown[:markdown.index("## [")].rstrip()+"\n\n"+"\n\n".join(blocks[s] for s in order)+"\n"
            content_hash=digest(markdown.encode()); model=SlideOutline.parse({"outline_id":f"outline-{content_hash[:16]}","task_id":task_id,"version":len(self.versions(task_id,"outline"))+1,"markdown":markdown,"slide_ids":order,"content_hash":content_hash,"created_at":now(),"schema_version":"1.0"})
            outline_hash=self._record_p3(task_id,"outline",model,{"parent":outline_hash,"action":"deck_content_edit","summary":prompt.strip(),"affected":affected,"authoritative":True,"invalidated":{"deck":affected}},"outline_edit","user")
        elif inferred=="global": rules.append(prompt.strip())
        else:
            for sid in affected: exceptions.setdefault(sid,[]).append((f"元素 {element_id}: " if inferred=="element" else "")+prompt.strip())
        assets=controlled_assets(self.input_view(task_id)["manifest"],self.store.resource_root(task_id)); html_text=validate_html(render(markdown,all_ids,rules,exceptions,assets),all_ids,assets.values())
        before=deck["metadata"]["page_hashes"]; after={sid:digest(fragment.encode()) for sid,fragment in self._slide_fragments(html_text).items()}; actual=[sid for sid in all_ids if before[sid]!=after[sid]]
        if any(s not in affected for s in actual): raise ConflictError("修改超出声明影响范围")
        return self._record_deck(task_id,html_text,outline_hash,{"parent":deck["hash"],"summary":prompt.strip(),"scope":inferred,"change_type":change_type,"affected":actual,"requested_affected":affected,"unchanged":[s for s in all_ids if s not in actual],"scope_understanding":understanding,"element_id":element_id,"outline_consistent":True,"global_rules":rules,"local_exceptions":exceptions},"deck_modify","user")
    def rollback_deck(self,task_id,target_hash):
        known={v["hash"] for v in self.versions(task_id,"deck")}
        if target_hash not in known: raise ValidationError("目标全稿版本不存在")
        target=json.loads(self.version(task_id,target_hash)); meta=next(v["metadata"] for v in self.versions(task_id,"deck") if v["hash"]==target_hash)
        current_outline=self._current_version(task_id,"outline"); inconsistent=target["outline_hash"]!=current_outline
        return self._record_deck(task_id,meta["html"],target["outline_hash"],{"parent":self._current_version(task_id,"deck"),"rollback_from":target_hash,"summary":f"回退自 {target_hash[:12]}","scope":"global","affected":list(meta["page_hashes"]),"outline_consistent":not inconsistent,"regenerate_required":list(meta["page_hashes"]) if inconsistent else [],"global_rules":meta.get("global_rules",[]),"local_exceptions":meta.get("local_exceptions",{})},"deck_rollback","user")
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
