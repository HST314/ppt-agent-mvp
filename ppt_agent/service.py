from __future__ import annotations

import hashlib, inspect, json, logging, re, threading, time, uuid
from pathlib import Path
from datetime import datetime, timezone

from .config import ClarificationConfig
from .content_inspection import inspect_content_quality
from .diagnostics import log_exception_chain
from .errors import ConflictError, GatewayError, NotFoundError, RuntimeUnavailableError, ValidationError
from .fsm import TaskState, transition
from .gateways import FakeGenerationGateway, FakeHtmlBuilder, FakeInspectionGateway, FakeSkillLoader
from .schema import DeliveryManifest, InspectionReport, IssueDisposition
from .p2 import canonical, digest, now, parse_task_card, questions_for, scan_resources, validate_answer
from .schema import ClarificationSet, ResourceManifest, TaskCard, TaskInputSnapshot
from .schema import NarrativeDocument, SlideOutline, SampleSelection, DeckArtifact
from .p3 import changed_slide_ids, narrative_markdown, normalize_outline_markdown, outline_markdown, parse_outline, requested_slide_count, structured_outline_markdown
from .p4 import controlled_assets, infer_scope, recommend, render, validate_html
from .offline import localize_delivery_html, offline_assets, offline_player, verify_delivery

def utcnow(): return datetime.now(timezone.utc).isoformat()
def fingerprint(value): return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":")).encode()).hexdigest()

# 只有传输/认证/5xx 类失败才把全局运行时置为不可用；空响应、工具错误、
# 内容校验等模型行为类失败只记录在所属任务，避免单任务的单次模型抖动
# 劫持全局徽章并波及其他任务。
RUNTIME_DEGRADING_CODES = frozenset({
    "model_timeout",
    "model_connection_error",
    "model_authentication_failed",
    "model_permission_denied",
    "model_upstream_unavailable",
})

def _clarification_directive(config: ClarificationConfig, round_number: int) -> str:
    budget = f"本轮为第 {round_number}/{config.max_rounds} 轮澄清，最多提出 {config.max_questions_per_round} 个问题。"
    if config.style == "minimal":
        return budget + "仅提出真正阻碍交付的关键问题，blocking 置 true；若没有值得追问的问题，返回空 questions 数组提前结束澄清。"
    return (
        budget
        + "积极全面地追问：除缺失的目标/受众/主题外，还可围绕使用场景、内容范围、风格偏好、页数、语言、素材约束等提出高价值问题；"
        "阻断交付的问题 blocking 置 true，偏好类问题 blocking 置 false；"
        "除非确无更多有价值的问题，本轮应尽量提出接近上限数量的问题；确无更多问题时返回空 questions 数组提前结束澄清。"
    )

class TaskService:
    def __init__(self,store,generator=None,inspector=None,skills=None,builder=None,clarifier=None,clarification_config=None,settings_store=None,browser_inspector=None):
        self.store=store; self.generator=generator or FakeGenerationGateway(); self.inspector=inspector or FakeInspectionGateway(); self.skills=skills or FakeSkillLoader(); self.builder=builder or FakeHtmlBuilder(); self.clarifier=clarifier
        if browser_inspector is False:
            self.browser_inspector=None
        elif browser_inspector is not None:
            self.browser_inspector=browser_inspector
        elif getattr(self.inspector,"requires_browser_evidence",False):
            from .browser_inspection import ChromiumDeckInspector
            self.browser_inspector=ChromiumDeckInspector()
        else:
            self.browser_inspector=None
        self._settings_store=settings_store
        self._clarification_config=clarification_config or ClarificationConfig()
        self._runtime_capabilities={"checked":False,"ready":True,"status":"not_required","models":[]}
        self._runtime_guard=threading.RLock()
        self._runtime_probe_guard=threading.Lock()
        for gateway in {id(x):x for x in (self.generator,self.inspector,self.builder,self.clarifier) if x is not None}.values():
            if hasattr(gateway,"set_audit_sink"): gateway.set_audit_sink(self.store.append_agent_audit)
        if self._runtime_gateways():
            self._runtime_capabilities={"checked":False,"ready":False,"status":"not_checked","models":[]}
        generation_timeout=max((getattr(gateway,"job_timeout_seconds",0) for gateway in (self.clarifier,self.generator,self.builder) if gateway is not None),default=0) or 630
        inspection_timeout=getattr(self.inspector,"job_timeout_seconds",0) or 630
        snapshot=self._settings_store.read() if self._settings_store is not None else {"values":{},"config_revision":None,"scope":"memory"}
        saved=snapshot["values"]
        workflow=saved.get("workflow",{}) if isinstance(saved,dict) else {}
        self._settings_revision=snapshot.get("config_revision")
        self._settings_scope=snapshot.get("scope","global" if self._settings_store is not None else "memory")
        self._clarification_config=ClarificationConfig(
            workflow.get("max_questions_per_round",self._clarification_config.max_questions_per_round),
            workflow.get("max_rounds",self._clarification_config.max_rounds),
            workflow.get("style",self._clarification_config.style),
        )
        self._runtime_settings={
            "workflow":self._clarification_config.public(),
            "jobs":{
                "generation_timeout_seconds":saved.get("jobs",{}).get("generation_timeout_seconds",generation_timeout),
                "inspection_timeout_seconds":saved.get("jobs",{}).get("inspection_timeout_seconds",inspection_timeout),
                "delivery_timeout_seconds":saved.get("jobs",{}).get("delivery_timeout_seconds",180),
            },
            "review":{"default_max_rounds":saved.get("review",{}).get("default_max_rounds",2)},
        }
    def _runtime_gateways(self):
        return list({id(x):x for x in (self.generator,self.inspector,self.builder,self.clarifier) if hasattr(x,"probe_capabilities")}.values())
    def initialize_runtime(self):
        with self._runtime_probe_guard:
            gateways=self._runtime_gateways()
            if not gateways:
                with self._runtime_guard:
                    self._runtime_capabilities={"checked":False,"ready":True,"status":"not_required","models":[],"checked_at":utcnow()}
                return self.runtime_health()
            probe_id=f"runtime-probe-{uuid.uuid4().hex}"
            started_at=utcnow()
            with self._runtime_guard:
                self._runtime_capabilities={"checked":False,"ready":False,"status":"checking","models":[],"probe_id":probe_id,"checked_at":started_at}
            models=[]; probe_events=[]; probe_results={}
            try:
                for gateway_index,gateway in enumerate(gateways):
                    identity_method=getattr(gateway,"capability_probe_key",None)
                    probe_key=identity_method() if callable(identity_method) else None
                    if probe_key is not None and probe_key in probe_results:
                        source_index,checks=probe_results[probe_key]
                        models.append({"model":gateway.model,"checks":checks,"probe_reused":True,"probe_source_gateway_index":source_index})
                        probe_events.append({
                            "event":"probe_check",
                            "gateway_index":gateway_index,
                            "model":str(getattr(gateway,"model","unknown")),
                            "check":"capability_contract",
                            "status":"reused",
                            "probe_source_gateway_index":source_index,
                        })
                        continue
                    method=gateway.probe_capabilities
                    parameters=inspect.signature(method).parameters
                    accepts_probe_id="probe_id" in parameters or any(item.kind==inspect.Parameter.VAR_KEYWORD for item in parameters.values())
                    checks=method(probe_id=probe_id) if accepts_probe_id else method()
                    audit=getattr(gateway,"last_probe_audit",None)
                    if isinstance(audit,dict):
                        probe_events.extend({"gateway_index":gateway_index,"model":str(getattr(gateway,"model","unknown")),**event} for event in audit.get("events",[]) if isinstance(event,dict))
                    else:
                        probe_events.append({"event":"probe_check","gateway_index":gateway_index,"model":str(getattr(gateway,"model","unknown")),"check":"capability_contract","status":"succeeded"})
                    if not checks or not all(checks.values()):
                        failed_check=next((key for key,value in (checks or {}).items() if not value),"capability_contract")
                        error=GatewayError("模型能力探测未满足运行契约",code="capability_probe_failed")
                        error.failed_check=failed_check
                        error.probe_id=probe_id
                        raise error
                    models.append({"model":gateway.model,"checks":checks})
                    if probe_key is not None:
                        probe_results[probe_key]=(gateway_index,dict(checks))
            except Exception as exc:
                audit=getattr(gateway,"last_probe_audit",None) if "gateway" in locals() else None
                if isinstance(audit,dict):
                    for event in audit.get("events",[]):
                        enriched={"gateway_index":gateway_index,"model":str(getattr(gateway,"model","unknown")),**event}
                        if enriched not in probe_events: probe_events.append(enriched)
                failed_check=getattr(exc,"failed_check",None) or "capability_contract"
                if not isinstance(exc,GatewayError):
                    wrapped=GatewayError(
                        "模型能力探测发生无法分类的 SDK 故障",
                        code="capability_probe_failed",
                        audit_details={"category":"sdk_error","sdk_exception_type":type(exc).__name__,"retryable":False},
                    )
                    wrapped.__cause__=exc
                    wrapped.__suppress_context__=True
                    exc=wrapped
                public=exc.public()["error"]
                error={key:public[key] for key in ("code","message","diagnostic_id","retryable","retry_after_seconds","agent_audit_id","probe_phase","terminal_reason","tool_calls","underlying_code") if key in public}
                error.update({"probe_id":probe_id,"failed_check":failed_check})
                client=getattr(gateway,"client",None) if "gateway" in locals() else None
                config=getattr(client,"config",None)
                secrets=[str(getattr(config,"api_key",""))] if config is not None else []
                diagnostic_context_method=getattr(client,"probe_diagnostic_context",None)
                try:
                    client_context=diagnostic_context_method() if callable(diagnostic_context_method) else {}
                except Exception as context_error:
                    client_context={"diagnostic_context_error":type(context_error).__name__}
                log_exception_chain(
                    exc,
                    diagnostic_id=error["diagnostic_id"],
                    probe_id=probe_id,
                    context={
                        "gateway_index":gateway_index if "gateway_index" in locals() else None,
                        "gateway_type":type(gateway).__name__ if "gateway" in locals() else None,
                        "client_type":type(client).__name__ if client is not None else None,
                        "model":str(getattr(gateway,"model","unknown")) if "gateway" in locals() else "unknown",
                        **client_context,
                    },
                    secrets=secrets,
                )
                if not any(event.get("status")=="failed" for event in probe_events):
                    probe_events.append({"event":"probe_check","gateway_index":gateway_index,"model":str(getattr(gateway,"model","unknown")),"check":failed_check,"status":"failed","error_code":error["code"],"diagnostic_id":error["diagnostic_id"],**exc.safe_audit_details()})
                completed_at=utcnow()
                self.store.append_runtime_probe({"probe_id":probe_id,"status":"failed","started_at":started_at,"completed_at":completed_at,"models":models,"failed_check":failed_check,"error":error,"events":probe_events})
                with self._runtime_guard:
                    self._runtime_capabilities={"checked":True,"ready":False,"status":"unavailable","models":models,"probe_id":probe_id,"failed_check":failed_check,"error":error,"checked_at":completed_at}
                return self.runtime_health()
            completed_at=utcnow()
            self.store.append_runtime_probe({"probe_id":probe_id,"status":"succeeded","started_at":started_at,"completed_at":completed_at,"models":models,"events":probe_events})
            with self._runtime_guard:
                self._runtime_capabilities={"checked":True,"ready":True,"status":"ready","models":models,"probe_id":probe_id,"checked_at":completed_at}
            return self.runtime_health()
    def runtime_health(self):
        with self._runtime_guard:
            return json.loads(json.dumps(self._runtime_capabilities))
    def runtime_config_summary(self):
        gateways=self._runtime_gateways()
        gateway_summaries=[]
        for gateway in gateways:
            summary={"model":str(getattr(gateway,"model","unknown")),"type":type(gateway).__name__}
            config=getattr(getattr(gateway,"client",None),"config",None)
            if config is not None and hasattr(config,"public"):
                summary["config"]=config.public()
            gateway_summaries.append(summary)
        return {
            "mode":"agent" if gateways else "fake",
            "gateways":sorted(gateway_summaries,key=lambda item:(item["model"],item["type"])),
        }
    def settings_view(self):
        with self._runtime_guard:
            values=json.loads(json.dumps(self._runtime_settings))
        return {
            "values":values,
            "scope":self._settings_scope,
            "config_revision":self._settings_revision,
            "models":self.runtime_config_summary(),
            "schema":{
                "workflow":{
                    "max_questions_per_round":{"type":"integer","minimum":1,"maximum":10,"label":"每轮最多澄清问题"},
                    "max_rounds":{"type":"integer","minimum":1,"maximum":5,"label":"最多澄清轮次"},
                    "style":{"type":"select","options":["minimal","comprehensive"],"label":"澄清风格"},
                },
                "jobs":{
                    "generation_timeout_seconds":{"type":"integer","minimum":30,"maximum":3660,"label":"生成 Job 时限（秒）"},
                    "inspection_timeout_seconds":{"type":"integer","minimum":30,"maximum":3660,"label":"检查 Job 时限（秒）"},
                    "delivery_timeout_seconds":{"type":"integer","minimum":30,"maximum":3660,"label":"交付 Job 时限（秒）"},
                },
                "review":{"default_max_rounds":{"type":"integer","minimum":0,"maximum":10,"label":"默认自动修复轮数"}},
            },
        }
    def update_settings(self,value):
        if not isinstance(value,dict) or set(value)-{"workflow","jobs","review"}: raise ValidationError("设置分组无效")
        with self._runtime_guard:
            if self._settings_store is not None:
                snapshot=self._settings_store.update(value)
                merged=snapshot["values"]
                self._settings_revision=snapshot["config_revision"]
                self._settings_scope=snapshot["scope"]
            else:
                merged=json.loads(json.dumps(self._runtime_settings))
                for group,patch in value.items():
                    if not isinstance(patch,dict) or set(patch)-set(merged[group]): raise ValidationError(f"{group} 设置字段无效")
                    merged[group].update(patch)
                workflow=merged["workflow"]
                for key,minimum,maximum in (("max_questions_per_round",1,10),("max_rounds",1,5)):
                    item=workflow[key]
                    if isinstance(item,bool) or not isinstance(item,int) or not minimum<=item<=maximum: raise ValidationError(f"workflow.{key} 超出范围")
                if workflow["style"] not in {"minimal","comprehensive"}: raise ValidationError("workflow.style 无效")
                for key,item in merged["jobs"].items():
                    if isinstance(item,bool) or not isinstance(item,int) or not 30<=item<=3660: raise ValidationError(f"jobs.{key} 超出范围")
                rounds=merged["review"]["default_max_rounds"]
                if isinstance(rounds,bool) or not isinstance(rounds,int) or not 0<=rounds<=10: raise ValidationError("review.default_max_rounds 超出范围")
            workflow=merged["workflow"]
            self._clarification_config=ClarificationConfig(workflow["max_questions_per_round"],workflow["max_rounds"],workflow["style"])
            self._runtime_settings=merged
        return self.settings_view()
    def job_timeouts(self):
        with self._runtime_guard: return dict(self._runtime_settings["jobs"])
    def default_inspection_rounds(self):
        with self._runtime_guard: return self._runtime_settings["review"]["default_max_rounds"]
    def require_runtime_ready(self):
        capabilities=self.runtime_health()
        if capabilities.get("ready"):
            return
        error=capabilities.get("error",{})
        failed_check=error.get("failed_check") or capabilities.get("failed_check")
        raise RuntimeUnavailableError(
            runtime_error_code=error.get("code"),
            retryable=error.get("retryable") is True,
            retry_after_seconds=error.get("retry_after_seconds"),
            # 全局运行时错误可能源自其他任务的执行：抛给当前任务时换发全新的
            # 诊断 ID，且不再引用其他任务的 Agent 审计；探测产生的审计不绑定
            # 任何任务，可以保留引用。
            agent_audit_id=error.get("agent_audit_id") if error.get("probe_id") else None,
            diagnostic_id=uuid.uuid4().hex,
            probe_id=error.get("probe_id") or (capabilities.get("probe_id") if failed_check else None),
            failed_check=failed_check,
            probe_phase=error.get("probe_phase"),
            terminal_reason=error.get("terminal_reason"),
            tool_calls=error.get("tool_calls"),
            underlying_code=error.get("underlying_code"),
        )
    def runtime_probes(self,limit=20): return self.store.runtime_probes(limit)
    def record_runtime_failure(self,error):
        if not isinstance(error,GatewayError): return
        if error.code not in RUNTIME_DEGRADING_CODES: return
        public=error.public()["error"]
        safe={key:public[key] for key in ("code","diagnostic_id","retryable","retry_after_seconds","agent_audit_id") if key in public}
        with self._runtime_guard:
            current=self._runtime_capabilities
            self._runtime_capabilities={**current,"checked":True,"ready":False,"status":"unavailable","error":safe,"checked_at":utcnow()}
    def record_runtime_success(self):
        with self._runtime_guard:
            if self._runtime_capabilities.get("ready"):
                self._runtime_capabilities.pop("last_failure",None)
    def agent_audits(self,task_id,job_id=None):
        self.store.checkpoint(task_id)
        return self.store.agent_audits(task_id=task_id,job_id=job_id)
    def create(self,task_id,mode="manual",target_slide_count=None):
        if mode not in {"manual","auto","quick"}: raise ValidationError("mode 只能是 manual、auto 或 quick")
        if mode == "quick":
            if isinstance(target_slide_count,bool) or not isinstance(target_slide_count,int) or not 1 <= target_slide_count <= 200:
                raise ValidationError("快速生成必须明确 1 到 200 页的最终页数")
        elif target_slide_count is not None:
            raise ValidationError("target_slide_count 仅用于快速生成模式")
        s=TaskState(task_id=task_id,mode=mode,target_slide_count=target_slide_count)
        self.store.create(task_id,s.to_dict()); return s.to_dict()
    def _task_clarification_config(self,task_id):
        if self.get(task_id).get("mode") == "quick":
            return ClarificationConfig(
                max_questions_per_round=self._clarification_config.max_questions_per_round,
                max_rounds=1,
                style="minimal",
            )
        return self._clarification_config
    def get(self,task_id): return self.store.checkpoint(task_id)
    def _require_actionable(self,task_id):
        status=self.get(task_id)["status"]
        if status in {"paused","cancelled","failed","completed"}: raise ConflictError(f"任务状态 {status} 不允许启动新动作")
    def _require_candidate_mutable(self,task_id):
        self._require_actionable(task_id)
        state=TaskState.parse(self.get(task_id))
        if state.stage not in {state.stage.DECK,state.stage.REVIEW}:
            raise ConflictError("当前阶段的候选全稿不可修改；如需调整，请从已交付版本显式派生新候选")
        return state
    def _candidate_write_token(self,task_id,allowed_stages=None):
        """Capture the state which a prepared deck candidate is allowed to replace."""
        with self.store.lock(task_id):
            self._require_actionable(task_id)
            state=TaskState.parse(self.get(task_id))
            allowed=set(allowed_stages or {"deck","review"})
            serialized=state.to_dict()
            if serialized["stage"] not in allowed:
                raise ConflictError("当前阶段不允许写入候选全稿")
            return {
                "stage":serialized["stage"],
                "revision":serialized["revision"],
                "parent_deck_hash":self._current_version(task_id,"deck"),
            }
    def _assert_candidate_write_token(self,task_id,token):
        state=TaskState.parse(self.get(task_id)).to_dict()
        current={
            "stage":state["stage"],
            "revision":state["revision"],
            "parent_deck_hash":self._current_version(task_id,"deck"),
        }
        if current != token:
            raise ConflictError("候选全稿准备期间任务状态或父版本已变更，未提交过期结果")
    def command(self,task_id,command_id,action,actor="system",payload=None):
        request={"action":action,"actor":actor,"payload":payload or {}}
        with self.store.lock(task_id):
            prior=[e for e in self.store.events(task_id) if e["command_id"]==command_id]
            if prior:
                if prior[0]["request_hash"] != fingerprint(request): raise ConflictError("command_id 请求内容冲突")
                return prior[0]["result"]
            old=TaskState.parse(self.get(task_id))
            if action == "advance" and old.stage == old.stage.OUTLINE:
                if self._confirmed_outline_hash(task_id) != self._current_version(task_id,"outline"): raise ConflictError("当前版本逐页大纲尚未确认")
            if action == "advance" and old.stage == old.stage.SAMPLE and self.versions(task_id,"sample"):
                self._require_current_sample_confirmation(task_id)
            new=transition(old,action,actor=actor)
            result=new.to_dict()
            event={"event_id":hashlib.sha256(f"{task_id}:{command_id}".encode()).hexdigest()[:24],"command_id":command_id,"action":action,"actor":actor,"request_hash":fingerprint(request),"at":utcnow(),"from":old.to_dict(),"to":result,"result":result}
            self.store.commit(task_id,result,event); return result
    def versions(self,task_id,kind=None): return self.store.versions(task_id,kind)
    def version(self,task_id,digest): return self.store.artifact(task_id,digest)
    def compare(self,task_id,left,right): return {"left":left,"right":right,"equal":self.version(task_id,left)==self.version(task_id,right)}
    def compare_decks(self,task_id,left,right):
        records={v["hash"]:v for v in self.versions(task_id,"deck")}
        if left not in records or right not in records: raise ValidationError("只能对比全稿版本")
        def describe(value):
            artifact=json.loads(self.version(task_id,value)); meta=records[value]["metadata"]
            return {"hash":value,"version":artifact["version"],"outline_hash":artifact["outline_hash"],"source":meta.get("source","unknown"),"operator":meta.get("operator","system"),"summary":meta.get("summary",""),"html":meta["html"],"fragments":self._slide_fragments(meta["html"])}
        lhs,rhs=describe(left),describe(right); page_ids=list(dict.fromkeys([*lhs["fragments"],*rhs["fragments"]])); pages=[]
        for slide_id in page_ids:
            lhtml=lhs["fragments"].get(slide_id); rhtml=rhs["fragments"].get(slide_id)
            status="added" if lhtml is None else "removed" if rhtml is None else "unchanged" if lhtml==rhtml else "modified"
            pages.append({"slide_id":slide_id,"status":status,"left_html":lhtml,"right_html":rhtml})
        for side in (lhs,rhs): side.pop("fragments")
        return {"left":lhs,"right":rhs,"equal":all(p["status"]=="unchanged" for p in pages),"pages":pages,"changed_slide_ids":[p["slide_id"] for p in pages if p["status"]!="unchanged"]}
    def events(self,task_id): return self.store.events(task_id)
    def import_input(self,task_id,source,source_format="auto",rebuild=False):
        self._require_actionable(task_id)
        with self.store.transaction(task_id):
            state=TaskState.parse(self.get(task_id))
            existing=self.versions(task_id,"input-snapshot")
            if existing and not rebuild: raise ConflictError("输入已冻结；采用新资料须显式重建快照")
            if rebuild and state.stage not in {state.stage.CREATED,state.stage.CLARIFICATION}: raise ConflictError("大纲阶段后不可重建输入快照")
            raw_source=canonical(source) if isinstance(source,dict) else str(source).encode("utf-8")
            raw_source_hash=self.store.put_version(task_id,"input-source",raw_source,{"content_type":"application/json" if isinstance(source,dict) else "text/plain"})
            card=parse_task_card(source,source_format)
            if state.mode == "quick":
                requested=requested_slide_count(card)
                if requested is not None and requested != state.target_slide_count:
                    raise ValidationError(f"任务卡页数与快速生成目标 {state.target_slide_count} 页不一致")
                card={**card,"constraints":{**card.get("constraints",{}),"page_count":state.target_slide_count}}
            source_format=card["source_format"]
            card_json={"task_id":task_id,"goal":card.get("goal","待澄清"),"audience":card.get("audience","待澄清"),"topic":card.get("topic","待澄清"),"source_format":source_format,"schema_version":"1.0"}
            parsed_card=TaskCard.parse(card_json); card_hash=self.store.put_version(task_id,"task-card",canonical(parsed_card.to_dict()),{"normalized":card})
            resources,warnings=scan_resources(self.store.resource_root(task_id))
            schema_resources=[{k:r[k] for k in ("resource_id","uri","media_type","content_hash")} for r in resources]
            manifest_seed={"task_id":task_id,"resources":schema_resources,"warnings":warnings}
            manifest=ResourceManifest.parse({"manifest_id":f"manifest-{digest(canonical(manifest_seed))[:16]}","task_id":task_id,"resources":schema_resources,"content_hash":digest(canonical(manifest_seed)),"created_at":now(),"schema_version":"1.0"})
            manifest_hash=self.store.put_version(task_id,"resource-manifest",canonical(manifest.to_dict()),{"resources":resources,"warnings":warnings})
            questions=[] if self.clarifier is not None else questions_for(card)
            diagnostic_id=f"clarification-{digest(canonical({'task_id':task_id,'card':card,'raw_source_hash':raw_source_hash}))[:16]}"
            clarification=ClarificationSet.parse({"clarification_id":f"clarification-{digest(canonical({'questions':questions,'diagnostic_id':diagnostic_id,'raw_source_hash':raw_source_hash}))[:16]}","task_id":task_id,"questions":tuple(q["prompt"] for q in questions),"assumptions":tuple(card["assumptions"]),"confirmed":not questions,"schema_version":"1.0"})
            clarification_config=self._task_clarification_config(task_id)
            clarification_meta={"questions":questions,"details":questions,"answers":{},"invalidated":[],"status":"generating" if self.clarifier is not None else "ready","question_source":None if self.clarifier is not None else "fallback","question_model":None,"diagnostic_id":diagnostic_id,"question_schema_version":"1.0","input_hash":raw_source_hash,"round":1,"rounds_history":[],"max_rounds":clarification_config.max_rounds,"max_questions_per_round":clarification_config.max_questions_per_round,"style":clarification_config.style}
            clarification_hash=self.store.put_version(task_id,"clarification",canonical(clarification.to_dict()),clarification_meta)
            snapshot=TaskInputSnapshot.parse({"snapshot_id":f"snapshot-{digest((raw_source_hash+card_hash+manifest_hash).encode())[:16]}","task_id":task_id,"task_card_hash":card_hash,"resource_manifest_hash":manifest_hash,"created_at":now(),"schema_version":"1.0"})
            snapshot_hash=self.store.put_version(task_id,"input-snapshot",canonical(snapshot.to_dict()),{"clarification_hash":clarification_hash,"raw_source_hash":raw_source_hash,"rebuild_of":existing[-1]["hash"] if existing else None})
            waiting=self.clarifier is not None or bool(questions)
            new=TaskState.parse(state.to_dict()); new=TaskState(**{**new.__dict__,"stage":new.stage.CLARIFICATION,"status":new.status.WAITING_FOR_USER if waiting else new.status.READY,"waiting_reason":"clarification_generating" if self.clarifier is not None else "missing_required_input" if questions else None,"required_action":"wait_for_clarification" if self.clarifier is not None else "answer_clarifications" if questions else None,"revision":new.revision+1})
            event={"event_id":hashlib.sha256(f"{task_id}:input:{snapshot_hash}".encode()).hexdigest()[:24],"command_id":f"input-{snapshot_hash[:16]}","action":"rebuild_input" if existing else "import_input","actor":"user","request_hash":snapshot_hash,"at":utcnow(),"from":state.to_dict(),"to":new.to_dict(),"result":{"snapshot_hash":snapshot_hash}}
            self.store.commit(task_id,new.to_dict(),event)
            result={"state":new.to_dict(),"snapshot":snapshot.to_dict(),"snapshot_hash":snapshot_hash,"task_card":card,"manifest":{**manifest.to_dict(),"resources":resources,"warnings":warnings},"clarification":{**clarification.to_dict(),"details":questions,**clarification_meta},"clarification_hash":clarification_hash}
            # Fake mode can advance immediately. In real mode the empty
            # placeholder means the clarification model has not run yet; the
            # route will enqueue that persisted Job first.
            if new.mode in {"auto","quick"} and self.clarifier is None and not questions:
                self._drive_auto_to_sample(task_id); result["state"]=self.get(task_id)
            return result
    def input_view(self,task_id):
        snapshots=self.versions(task_id,"input-snapshot")
        if not snapshots: return {"state":self.get(task_id),"snapshot":None}
        events=self.events(task_id)
        input_event_index=next(i for i in range(len(events)-1,-1,-1) if events[i]["action"] in {"import_input","rebuild_input"})
        current=events[input_event_index]["result"]["snapshot_hash"]
        item=next(v for v in snapshots if v["hash"]==current); snapshot=json.loads(self.version(task_id,item["hash"])); meta=item["metadata"]
        ch=meta["clarification_hash"]
        # Clarification events are append-only, but a rebuild starts a new
        # lineage.  Only accept events after the current input event whose
        # artifact is explicitly bound to this snapshot's raw input.
        clarification_versions={v["hash"]:v for v in self.versions(task_id,"clarification")}
        for event in reversed(events[input_event_index+1:]):
            if event["action"] not in {"answer_clarification","clarification_generate","clarification_failed","clarification_fallback"}: continue
            candidate=event["result"]["clarification_hash"]
            record=clarification_versions.get(candidate)
            if record and record["metadata"].get("input_hash")==meta["raw_source_hash"]:
                ch=candidate; break
        cv=clarification_versions[ch]
        card=next(v for v in self.versions(task_id,"task-card") if v["hash"]==snapshot["task_card_hash"])
        manifest=next(v for v in self.versions(task_id,"resource-manifest") if v["hash"]==snapshot["resource_manifest_hash"])
        clarification={**json.loads(self.version(task_id,ch)),**cv["metadata"]}
        task_card=clarification.get("normalized_task_card",card["metadata"]["normalized"])
        source_format=card["metadata"]["normalized"]["source_format"]
        raw_source=self.version(task_id,meta["raw_source_hash"]).decode("utf-8")
        source=json.loads(raw_source) if source_format=="json" else raw_source
        return {"state":self.get(task_id),"snapshot":snapshot,"snapshot_hash":item["hash"],"source":source,"source_format":source_format,"task_card":task_card,"manifest":{**json.loads(self.version(task_id,snapshot["resource_manifest_hash"])),**manifest["metadata"]},"clarification":clarification}
    def generate_clarification(self,task_id):
        if self.clarifier is None: raise ConflictError("当前为 fake 模式，不能调用澄清模型")
        view=self.input_view(task_id); snapshot_record=next(v for v in self.versions(task_id,"input-snapshot") if v["hash"]==view["snapshot_hash"]); raw_hash=snapshot_record["metadata"]["raw_source_hash"]
        prior=view["clarification"]; config=self._task_clarification_config(task_id)
        round_number=prior.get("round",1); history=list(prior.get("rounds_history",[]))
        payload={"task_id":task_id,"original_input":self.version(task_id,raw_hash).decode("utf-8"),"original_input_sha256":raw_hash,"normalized_task_card":view["task_card"],"candidate_missing_fields":view["task_card"].get("missing",[]),"resource_summary":view["manifest"],"clarification_context":{"round":round_number,"max_rounds":config.max_rounds,"max_questions_per_round":config.max_questions_per_round,"style":config.style,"directive":_clarification_directive(config,round_number),"previous_qa":history}}
        try:
            value=self.clarifier.clarify(payload)
            asked=[q.get("field_path") for entry in history for q in entry.get("questions",[]) if isinstance(q,dict)]
            questions,filtered=self._validate_model_questions(value.get("questions"),view["task_card"],max_questions=config.max_questions_per_round,asked_field_paths=asked)
            if self.get(task_id).get("mode") == "quick":
                non_blocking=sum(1 for question in questions if not question["blocking"])
                questions=[question for question in questions if question["blocking"]]
                filtered += non_blocking
        except Exception as exc:
            error=exc.public()["error"] if hasattr(exc,"public") else {"code":"clarification_generation_failed","message":"澄清问题生成失败","diagnostic_id":hashlib.sha256(f"{task_id}:{type(exc).__name__}".encode()).hexdigest()[:24]}
            self._record_clarification(task_id,view,[],"failed",None,error,"clarification_failed"); raise
        extra={"filtered_duplicate_questions":filtered} if filtered else None
        result=self._record_clarification(task_id,view,questions,"ready",value.get("model"),None,"clarification_generate",extra=extra)
        if result["confirmed"] and history:
            # 模型在后续轮次返回 0 题 = 提前确认；此前轮次的答案已合并进任务卡，同步冻结。
            self._freeze_task_card(task_id,view["task_card"],result["clarification_hash"])
        if result["confirmed"] and self.get(task_id).get("mode") in {"auto","quick"}:
            self._drive_auto_to_sample(task_id)
        return result

    def recover_clarification_failure(self,task_id,error):
        """Normalize deadline/persistence/provider failures to one retry state."""
        view=self.input_view(task_id)
        if (view.get("state",{}).get("waiting_reason")=="clarification_failed"
            and view.get("clarification",{}).get("status")=="failed"):
            return view["clarification"]
        code = "clarification_infrastructure_failure"
        if error.__class__.__name__ == "ExecutionDeadlineExceeded": code = "stage_deadline_exceeded"
        elif isinstance(error,OSError): code = "job_persistence_error"
        return self._record_clarification(task_id,view,[],"failed",None,{"code":code,"message":"澄清服务暂时不可用，请重试或使用兜底问题"},"clarification_failed")
    def use_fallback_clarification(self,task_id):
        view=self.input_view(task_id)
        return self._record_clarification(task_id,view,questions_for(view["task_card"]),"ready",None,None,"clarification_fallback",source="fallback")
    def fail_clarification_for_runtime(self,task_id,error):
        view=self.input_view(task_id)
        public=error.public()["error"] if hasattr(error,"public") else {"code":"runtime_unavailable","message":"模型运行时尚未就绪","diagnostic_id":hashlib.sha256(f"{task_id}:runtime".encode()).hexdigest()[:24]}
        return self._record_clarification(task_id,view,[],"failed",None,public,"clarification_failed")
    def _validate_model_questions(self,questions,card,*,max_questions=5,asked_field_paths=()):
        """校验模型问题，返回 (有效问题列表, 被过滤的重复字段数)。

        跨轮撞库（询问任务卡已知字段或历轮已问字段）采用过滤而非拒绝：
        剔除撞库问题，剩余问题照常展示；全部被剔除时返回空列表，由上层
        视为"模型没有更多问题"提前确认，流程不被打断。
        """
        if not isinstance(questions,list) or len(questions)>max_questions: raise ValidationError(f"澄清模型 questions 必须为 0 到 {max_questions} 项")
        required={"question_id","field_path","prompt","helper_text","options","allow_other","blocking"}; seen_ids=set(); seen_paths=set(); known={k for k in ("goal","audience","topic") if k not in card.get("missing",[])}; asked={path for path in asked_field_paths if isinstance(path,str)}; result=[]; filtered=0
        for q in questions:
            if not isinstance(q,dict) or set(q)!=required: raise ValidationError("澄清问题 Schema 无效")
            if q["question_id"] in seen_ids or q["field_path"] in seen_paths: raise ValidationError("澄清问题存在重复 ID 或字段")
            if q["field_path"] in known or q["field_path"] in asked:
                filtered+=1; continue
            if not all(isinstance(q[k],str) and q[k].strip() for k in ("question_id","field_path","prompt","helper_text")) or not isinstance(q["allow_other"],bool) or not isinstance(q["blocking"],bool): raise ValidationError("澄清问题字段无效")
            if not isinstance(q["options"],list) or any(not isinstance(o,dict) or set(o)!={"value","label","description"} or not all(isinstance(o[k],str) for k in o) or not o["value"].strip() or not o["label"].strip() for o in q["options"]): raise ValidationError("澄清选项 Schema 无效")
            if len({o["value"] for o in q["options"]})!=len(q["options"]): raise ValidationError("澄清选项重复")
            seen_ids.add(q["question_id"]); seen_paths.add(q["field_path"]); result.append({**q,"field":q["field_path"]})
        return result, filtered
    def _record_clarification(self,task_id,view,questions,status,model,error,action,source="model",extra=None):
        config=self._task_clarification_config(task_id)
        meta={"questions":questions,"details":questions,"answers":{},"invalidated":[],"status":status,"question_source":source if status=="ready" else None,"question_model":model,"diagnostic_id":view["clarification"]["diagnostic_id"],"question_schema_version":"1.0","input_hash":view["clarification"]["input_hash"],"normalized_task_card":view["task_card"],"round":config.max_rounds if source=="fallback" else view["clarification"].get("round",1),"rounds_history":list(view["clarification"].get("rounds_history",[])),"max_rounds":config.max_rounds,"max_questions_per_round":config.max_questions_per_round,"style":config.style}
        if error: meta["error"]=error
        if extra: meta.update(extra)
        artifact=ClarificationSet.parse({"clarification_id":f"clarification-{digest(canonical(meta))[:16]}","task_id":task_id,"questions":tuple(q["prompt"] for q in questions),"assumptions":tuple(),"confirmed":status=="ready" and not questions,"schema_version":"1.0"}); ch=self.store.put_version(task_id,"clarification",canonical(artifact.to_dict()),meta)
        state=TaskState.parse(self.get(task_id)); failed=status=="failed"; new=TaskState(**{**state.__dict__,"status":state.status.WAITING_FOR_USER if failed or questions else state.status.READY,"waiting_reason":"clarification_failed" if failed else "missing_required_input" if questions else None,"required_action":"retry_clarification" if failed else "answer_clarifications" if questions else None,"revision":state.revision+1})
        event={"event_id":hashlib.sha256(f"{task_id}:{action}:{ch}".encode()).hexdigest()[:24],"command_id":f"{action}-{ch[:16]}","action":action,"actor":"system" if action!="clarification_fallback" else "user","request_hash":meta["input_hash"],"at":utcnow(),"from":state.to_dict(),"to":new.to_dict(),"result":{"clarification_hash":ch,"snapshot_hash":view["snapshot_hash"],"input_hash":meta["input_hash"]}}
        self.store.commit(task_id,new.to_dict(),event); return {"clarification_hash":ch,**meta,"confirmed":artifact.confirmed}
    def _freeze_task_card(self,task_id,merged,clarification_hash):
        raw=canonical(TaskCard.parse({"task_id":task_id,"goal":merged.get("goal","待澄清"),"audience":merged.get("audience","待澄清"),"topic":merged.get("topic","待澄清"),"source_format":merged.get("source_format","json"),"schema_version":"1.0"}).to_dict())
        # 规范卡内容未变化时（回答只涉及 goal/audience/topic 以外的字段）跳过重写：
        # 合入结果已由澄清元数据携带，重写只会与既有版本产生元数据冲突。
        cards=self.versions(task_id,"task-card")
        if cards and self.version(task_id,cards[-1]["hash"])==raw: return
        self.store.put_version(task_id,"task-card",raw,{"normalized":merged,"clarification_hash":clarification_hash})
    def answer_clarification(self,task_id,question_id,answer):
        return self.answer_clarifications(task_id,{question_id:answer})
    def answer_clarifications(self,task_id,submitted,require_complete=False):
        self._require_actionable(task_id)
        if not isinstance(submitted,dict) or not submitted: raise ValidationError("本轮回答不得为空")
        view=self.input_view(task_id); clarification=view.get("clarification")
        if not clarification: raise ConflictError("尚未生成澄清问题")
        if clarification.get("status") != "ready": raise ConflictError("澄清问题尚未生成完成")
        details=clarification.get("details",clarification.get("questions",[]))
        by_id={q["question_id"]:q for q in details}; answers=dict(clarification.get("answers",{})); changed=False
        if require_complete:
            required={q["question_id"] for q in details if q["blocking"]}
            missing=sorted(required-set(submitted))
            if missing: raise ValidationError("必须一次提交本轮全部阻断题："+",".join(missing))
        for question_id,answer in submitted.items():
            if question_id not in by_id: raise ValidationError("澄清问题不存在")
            value=validate_answer(by_id[question_id],answer); changed |= question_id in answers and answers[question_id]!=value; answers[question_id]=value
        pending=[q for q in details if q["blocking"] and q["question_id"] not in answers]
        merged=dict(view["task_card"]); merged.update({q["field"]:answers[q["question_id"]] for q in details if q["question_id"] in answers})
        merged["missing"]=[key for key in merged.get("missing",[]) if not merged.get(key)]
        payload={"questions":details,"details":details,"answers":answers,"status":"ready","normalized_task_card":merged,"invalidated":(["narrative","outline","sample","deck","inspection","delivery"] if changed else clarification.get("invalidated",[])),**{k:clarification.get(k) for k in ("question_source","question_model","diagnostic_id","question_schema_version","input_hash")}}
        if not pending:
            config=self._task_clarification_config(task_id); current_round=clarification.get("round",1)
            if self.clarifier is not None and clarification.get("question_source")=="model" and current_round < config.max_rounds:
                # 阻断题已答完且轮次预算未用尽：归档本轮问答，自动生成下一轮。
                history=list(clarification.get("rounds_history",[]))
                history.append({"round":current_round,"questions":details,"answers":{q["question_id"]:answers[q["question_id"]] for q in details if q["question_id"] in answers}})
                next_meta={"questions":[],"details":[],"answers":{},"invalidated":payload["invalidated"],"status":"generating","question_source":None,"question_model":None,"diagnostic_id":clarification["diagnostic_id"],"question_schema_version":"1.0","input_hash":clarification["input_hash"],"normalized_task_card":merged,"round":current_round+1,"rounds_history":history,"max_rounds":config.max_rounds,"max_questions_per_round":config.max_questions_per_round,"style":config.style}
                artifact=ClarificationSet.parse({"clarification_id":f"clarification-{digest(canonical(next_meta))[:16]}","task_id":task_id,"questions":tuple(),"assumptions":tuple(),"confirmed":False,"schema_version":"1.0"})
                ch=self.store.put_version(task_id,"clarification",canonical(artifact.to_dict()),next_meta)
                state=TaskState.parse(self.get(task_id)); new=TaskState(**{**state.__dict__,"status":state.status.WAITING_FOR_USER,"waiting_reason":"clarification_generating","required_action":"wait_for_clarification","revision":state.revision+1})
                event={"event_id":hashlib.sha256(f"{task_id}:answer:{ch}".encode()).hexdigest()[:24],"command_id":f"answer-{ch[:16]}","action":"answer_clarification","actor":"user","request_hash":fingerprint(submitted),"at":utcnow(),"from":state.to_dict(),"to":new.to_dict(),"result":{"clarification_hash":ch,"invalidated":payload["invalidated"],"next_round":current_round+1}}
                self.store.commit(task_id,new.to_dict(),event)
                return {"state":self.get(task_id),"clarification_hash":ch,**next_meta,"confirmed":False}
        model=ClarificationSet.parse({"clarification_id":f"clarification-{digest(canonical(payload))[:16]}","task_id":task_id,"questions":tuple(q["prompt"] for q in details),"assumptions":tuple(),"confirmed":not pending,"schema_version":"1.0"})
        ch=self.store.put_version(task_id,"clarification",canonical(model.to_dict()),payload)
        state=TaskState.parse(self.get(task_id)); new=TaskState(**{**state.__dict__,"status":state.status.WAITING_FOR_USER if pending else state.status.READY,"waiting_reason":"missing_required_input" if pending else None,"required_action":"answer_clarifications" if pending else None,"revision":state.revision+1})
        event={"event_id":hashlib.sha256(f"{task_id}:answer:{ch}".encode()).hexdigest()[:24],"command_id":f"answer-{ch[:16]}","action":"answer_clarification","actor":"user","request_hash":fingerprint(submitted),"at":utcnow(),"from":state.to_dict(),"to":new.to_dict(),"result":{"clarification_hash":ch,"invalidated":payload["invalidated"]}}
        self.store.commit(task_id,new.to_dict(),event)
        if not pending:
            self._freeze_task_card(task_id,merged,ch)
        if new.mode in {"auto","quick"} and not pending: self._drive_auto_to_sample(task_id)
        return {"state":self.get(task_id),"clarification_hash":ch,**payload,"confirmed":not pending}

    def _drive_auto_to_sample(self,task_id):
        state=TaskState.parse(self.get(task_id))
        if state.mode not in {"auto","quick"}: return
        if not self._current_version(task_id,"narrative"): self.generate_narrative(task_id)
        if not self._current_version(task_id,"outline"): self.generate_outline(task_id)
        if not self._current_version(task_id,"sample"): self.generate_sample(task_id)
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
        self._require_actionable(task_id)
        view=self._p3_input(task_id); state=TaskState.parse(view["state"])
        skill=self.skills.load("narrative"); prior=self._current_version(task_id,"narrative")
        if isinstance(self.generator,FakeGenerationGateway):
            text=narrative_markdown(view["task_card"])
            if prompt: text += f"\n## 修改要求\n{prompt.strip()}\n"
            model_name=self.generator.model
        else:
            generated=self.generator.generate("narrative",{"task_id":task_id,"task_card":view["task_card"],"prompt":prompt,"scope":scope},skill=skill["content"])
            text=generated["text"]; model_name=generated.get("model","unknown")
        version=len(self.versions(task_id,"narrative"))+1; content_hash=digest(text.encode())
        model=NarrativeDocument.parse({"document_id":f"narrative-{content_hash[:16]}","task_id":task_id,"version":version,"markdown":text,"content_hash":content_hash,"created_at":now(),"schema_version":"1.0"})
        metadata={"parent":prior,"action":"generate" if not prior else "regenerate","scope":scope,"summary":"生成整稿叙事结构","model":model_name,"skill":{"action":"narrative","version":skill["version"],"hash":digest(skill["content"].encode()),"included":["narrative"],"trimmed":["outline","html","inspection"]},"input_snapshot_hash":view["snapshot_hash"]}
        h=self._record_p3(task_id,"narrative",model,metadata,"narrative_generate")
        if state.stage in {state.stage.CLARIFICATION,state.stage.CREATED}:
            self.command(task_id,f"narrative-stage-{h[:12]}","advance")
        else:
            self._reset_narrative_gate(task_id,h)
        return self.planning_view(task_id)
    def edit_narrative(self,task_id,markdown,summary="直接编辑"):
        self._require_actionable(task_id)
        self._p3_input(task_id)
        if not isinstance(markdown,str) or not markdown.strip(): raise ValidationError("叙事 Markdown 不得为空")
        prior=self._current_version(task_id,"narrative"); version=len(self.versions(task_id,"narrative"))+1; content_hash=digest(markdown.encode())
        model=NarrativeDocument.parse({"document_id":f"narrative-{content_hash[:16]}","task_id":task_id,"version":version,"markdown":markdown,"content_hash":content_hash,"created_at":now(),"schema_version":"1.0"})
        h=self._record_p3(task_id,"narrative",model,{"parent":prior,"action":"direct_edit","summary":summary,"authoritative":True,"invalidated":["outline","sample","deck"]},"narrative_edit","user")
        self._reset_narrative_gate(task_id,h)
        return self.planning_view(task_id)
    def confirm_narrative(self,task_id):
        self._require_actionable(task_id)
        current=self._current_version(task_id,"narrative")
        if not current: raise ConflictError("尚未生成叙事结构")
        state=TaskState.parse(self.get(task_id))
        if state.stage==state.stage.NARRATIVE:
            new=transition(state,"advance",actor="user")
            event={"event_id":hashlib.sha256(f"{task_id}:confirm-narrative:{current}".encode()).hexdigest()[:24],"command_id":f"confirm-narrative-{current[:16]}","action":"confirm_narrative","actor":"user","request_hash":current,"at":utcnow(),"from":state.to_dict(),"to":new.to_dict(),"result":{"confirmed_narrative_hash":current}}
            self.store.commit(task_id,new.to_dict(),event)
        return self.planning_view(task_id)
    def generate_outline(self,task_id,prompt=None,slide_ids=None):
        self._require_actionable(task_id)
        view=self._p3_input(task_id); narrative=self._current_version(task_id,"narrative")
        if not narrative: raise ConflictError("须先生成叙事结构")
        state=TaskState.parse(self.get(task_id))
        if state.mode=="manual" and self._confirmed_narrative_hash(task_id) != narrative: raise ConflictError("manual 模式须先确认当前版本叙事结构")
        skill=self.skills.load("outline"); count=requested_slide_count(view["task_card"]); resources=view["manifest"].get("resources",[])
        current=self._current_version(task_id,"outline")
        if slide_ids:
            if not current or not prompt: raise ValidationError("指定页修改需要现有大纲和修改 Prompt")
            old=normalize_outline_markdown(json.loads(self.version(task_id,current))["markdown"],resources,None); order,blocks=parse_outline(old,resources,None)
            unknown=set(slide_ids)-set(order)
            if unknown: raise ValidationError("指定页面不存在")
            for sid in slide_ids: blocks[sid] += f"\n- 修改要求：{prompt.strip()}"
            prefix=old[:old.index("## [")].rstrip(); text=prefix+"\n\n"+"\n\n".join(blocks[sid] for sid in order)+"\n"
        else:
            if isinstance(self.generator,FakeGenerationGateway):
                text=outline_markdown(view["task_card"],resources,count)
                if prompt: text += f"\n<!-- 修改要求：{prompt.strip()} -->\n"
            else:
                payload={"task_id":task_id,"task_card":view["task_card"],"narrative":json.loads(self.version(task_id,narrative))["markdown"],"resources":resources,"slide_count":count,"prompt":prompt}
                for attempt in range(1,3):
                    generated=self.generator.generate("outline",payload,skill=skill["content"])
                    try:
                        if not isinstance(generated,dict):
                            raise ValidationError("大纲生成响应必须为对象")
                        if "slides" in generated:
                            text=structured_outline_markdown(generated["slides"],resources,count)
                        elif isinstance(generated.get("text"),str):
                            # Compatibility boundary for legacy HTTP gateways.
                            text=normalize_outline_markdown(generated["text"],resources,count)
                        else:
                            raise ValidationError("大纲生成响应必须包含 slides 或兼容 text")
                        break
                    except ValidationError as exc:
                        self.store.put_version(task_id,"outline-diagnostic",canonical({"attempt":attempt,"candidate":generated}),{
                            "v":len(self.versions(task_id,"outline-diagnostic"))+1,
                            "stage":"outline","attempt":attempt,"error_code":exc.code,"error_message":exc.message,
                            "model":generated.get("model","unknown") if isinstance(generated,dict) else "unknown","public_error_exposes_candidate":False,
                        })
                        if attempt == 2: raise
                        payload={**payload,"semantic_correction":{"attempt":1,"error":exc.message,"previous_candidate":generated}}
        return self.edit_outline(task_id,text,"生成逐页大纲",actor="system",skill=skill)
    def edit_outline(self,task_id,markdown,summary="直接编辑",actor="user",skill=None):
        self._require_actionable(task_id)
        view=self._p3_input(task_id); expected=requested_slide_count(view["task_card"])
        markdown=normalize_outline_markdown(markdown,view["manifest"].get("resources",[]),expected)
        slide_ids,blocks=parse_outline(markdown,view["manifest"].get("resources",[]),expected)
        prior=self._current_version(task_id,"outline"); before={}
        if prior: _,before=parse_outline(json.loads(self.version(task_id,prior))["markdown"],view["manifest"].get("resources",[]),None)
        affected=changed_slide_ids(before,blocks); version=len(self.versions(task_id,"outline"))+1; content_hash=digest(markdown.encode())
        model=SlideOutline.parse({"outline_id":f"outline-{content_hash[:16]}","task_id":task_id,"version":version,"markdown":markdown,"slide_ids":slide_ids,"content_hash":content_hash,"created_at":now(),"schema_version":"1.0"})
        meta={"parent":prior,"action":"generate" if not prior else "edit","summary":summary,"affected":affected,"unchanged":[sid for sid in blocks if sid in before and blocks[sid]==before[sid]],"authoritative":True,"invalidated":{"sample":affected,"deck":affected}}
        if skill: meta["skill"]={"action":"outline","version":skill["version"],"hash":digest(skill["content"].encode()),"included":["outline"],"trimmed":["narrative","html","inspection"]}
        h=self._record_p3(task_id,"outline",model,meta,"outline_generate" if not prior else "outline_edit",actor)
        self._invalidate_outline_confirmation(task_id,h)
        self._invalidate_sample_gate(task_id,h)
        state=TaskState.parse(self.get(task_id))
        if state.stage==state.stage.NARRATIVE and state.mode in {"auto","quick"}:
            self.command(task_id,f"auto-outline-stage-{h[:12]}","advance")
            self.confirm_outline(task_id)
        return self.planning_view(task_id)
    def rollback_planning(self,task_id,kind,target_hash):
        self._require_actionable(task_id)
        if kind not in {"narrative","outline"}: raise ValidationError("版本类型无效")
        target=json.loads(self.version(task_id,target_hash)); known={v["hash"] for v in self.versions(task_id,kind)}
        if target_hash not in known: raise ValidationError("目标版本类型不匹配")
        return self.edit_narrative(task_id,target["markdown"],f"回退自 {target_hash[:12]}") if kind=="narrative" else self.edit_outline(task_id,target["markdown"],f"回退自 {target_hash[:12]}")
    def confirm_outline(self,task_id):
        self._require_actionable(task_id)
        current=self._current_version(task_id,"outline")
        if not current: raise ConflictError("尚未生成逐页大纲")
        state=TaskState.parse(self.get(task_id))
        if state.stage==state.stage.OUTLINE:
            new=transition(state,"advance",actor="user" if state.mode=="manual" else "system")
            result={"confirmed_outline_hash":current}
            event={"event_id":hashlib.sha256(f"{task_id}:confirm-outline:{current}".encode()).hexdigest()[:24],"command_id":f"confirm-outline-{current[:16]}","action":"confirm_outline","actor":"user" if state.mode=="manual" else "system","request_hash":current,"at":utcnow(),"from":state.to_dict(),"to":new.to_dict(),"result":result}
            self.store.commit(task_id,new.to_dict(),event)
        return self.planning_view(task_id)
    def _confirmed_outline_hash(self,task_id):
        for event in reversed(self.events(task_id)):
            if event["action"] == "confirm_outline": return event["result"].get("confirmed_outline_hash")
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

    def _invalidate_outline_confirmation(self,task_id,artifact_hash):
        state=TaskState.parse(self.get(task_id))
        if state.stage in {state.stage.CREATED,state.stage.CLARIFICATION,state.stage.NARRATIVE,state.stage.OUTLINE}: return
        new=TaskState(**{**state.__dict__,"stage":state.stage.OUTLINE,"sample_confirmed":False,"status":state.status.WAITING_FOR_USER,"waiting_reason":"manual_gate","required_action":"approve_outline","revision":state.revision+1})
        event={"event_id":hashlib.sha256(f"{task_id}:invalidate-outline:{artifact_hash}".encode()).hexdigest()[:24],"command_id":f"invalidate-outline-{artifact_hash[:16]}","action":"invalidate_outline_confirmation","actor":"system","request_hash":artifact_hash,"at":utcnow(),"from":state.to_dict(),"to":new.to_dict(),"result":{"hash":artifact_hash,"confirmed_outline_hash":None,"invalidated":["sample","deck","inspection"]}}
        self.store.commit(task_id,new.to_dict(),event)
    def select_samples(self,task_id,slide_ids=None,count=2):
        self._require_actionable(task_id)
        outline=self._current_version(task_id,"outline"); state=TaskState.parse(self.get(task_id))
        if not outline or state.stage != state.stage.SAMPLE: raise ConflictError("须先完成并确认逐页大纲")
        if self._confirmed_outline_hash(task_id) != outline: raise ConflictError("须先确认当前版本逐页大纲")
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
        from .execution import progress
        self._require_actionable(task_id)
        view=self.sample_view(task_id)
        if view["state"]["stage"]!="sample": raise ConflictError("当前样品阶段已完成；历史样品只读")
        selection=view["selection"]
        if not selection: view=self.select_samples(task_id); selection=view["selection"]
        outline=self._current_version(task_id,"outline")
        if selection["outline_hash"] != outline: raise ConflictError("样品选择已因大纲变化而失效")
        data=json.loads(self.version(task_id,outline)); rules=[]; assets=controlled_assets(self.input_view(task_id)["manifest"],self.store.resource_root(task_id))
        if prompt: rules.append(prompt.strip())
        source=render(data["markdown"],selection["slide_ids"],rules,assets=assets) if isinstance(self.builder,FakeHtmlBuilder) else self.builder.build(data["markdown"],action="sample",slide_ids=selection["slide_ids"],rules=rules,assets=assets)
        progress("validating_html", "校验 HTML")
        html_text=validate_html(source,selection["slide_ids"],assets)
        version=len(self.versions(task_id,"sample"))+1; content_hash=digest(html_text.encode()); model=DeckArtifact.parse({"artifact_id":f"sample-{content_hash[:16]}","task_id":task_id,"version":version,"kind":"sample","outline_hash":outline,"content_hash":content_hash,"created_at":now(),"schema_version":"1.0"})
        prior=self._current_version(task_id,"sample"); meta={"html":html_text,"selection_hash":selection["hash"],"parent":prior,"summary":"生成真实 HTML 样品","scope":"global","global_rules":rules,"local_exceptions":{},"build":"success"}
        h=self._record_p3(task_id,"sample",model,meta,"sample_generate")
        self._invalidate_sample_gate(task_id,h)
        return self.sample_view(task_id)
    def modify_sample(self,task_id,prompt,scope=None,slide_id=None,element_id=None):
        self._require_actionable(task_id)
        view=self.sample_view(task_id); sample=view["sample"]
        if view["state"]["stage"]!="sample": raise ConflictError("当前样品阶段已完成；历史样品只读")
        if not sample: raise ConflictError("尚未生成样品")
        if not isinstance(prompt,str) or not prompt.strip(): raise ValidationError("修改 Prompt 不得为空")
        ids=list(view["selection"]["slide_ids"])
        scope,understanding=infer_scope(prompt,slide_id,element_id,scope)
        if scope in {"element","page"} and slide_id not in ids: raise ValidationError("修改页面不在样品中")
        if scope=="element" and not element_id: raise ValidationError("元素级修改必须指定 element_id")
        meta=sample["metadata"]; rules=list(meta.get("global_rules",[])); exceptions={k:list(v) for k,v in meta.get("local_exceptions",{}).items()}
        if scope=="global": rules.append(prompt.strip())
        else: exceptions.setdefault(slide_id,[]).append((f"元素 {element_id}: " if scope=="element" else "")+prompt.strip())
        outline=self._current_version(task_id,"outline"); data=json.loads(self.version(task_id,outline)); assets=controlled_assets(self.input_view(task_id)["manifest"],self.store.resource_root(task_id))
        previous_slides="".join(self._slide_fragments(sample["html"]).get(sid,"") for sid in ids)
        source=render(data["markdown"],ids,rules,exceptions,assets) if isinstance(self.builder,FakeHtmlBuilder) else self.builder.build(data["markdown"],action="sample",slide_ids=ids,rules=rules,exceptions=exceptions,assets=assets,previous_slides=previous_slides,prompt=prompt,scope=scope,slide_id=slide_id,element_id=element_id)
        html_text=validate_html(source,ids,assets)
        version=len(self.versions(task_id,"sample"))+1; ch=digest(html_text.encode()); model=DeckArtifact.parse({"artifact_id":f"sample-{ch[:16]}","task_id":task_id,"version":version,"kind":"sample","outline_hash":outline,"content_hash":ch,"created_at":now(),"schema_version":"1.0"})
        h=self._record_p3(task_id,"sample",model,{"html":html_text,"selection_hash":view["selection"]["hash"],"parent":sample["hash"],"summary":prompt.strip(),"scope":scope,"scope_understanding":understanding,"slide_id":slide_id,"element_id":element_id,"global_rules":rules,"local_exceptions":exceptions,"build":"success"},"sample_modify","user")
        self._invalidate_sample_gate(task_id,h)
        return self.sample_view(task_id)
    def confirm_sample(self,task_id):
        # Validation, the immutable confirmation fact and stage advancement are
        # one task-local transaction.  A repeated request after a lost response
        # returns the same fact without advancing revision a second time.
        with self.store.lock(task_id):
            self._require_actionable(task_id)
            view=self.sample_view(task_id); sample=view["sample"]; outline=self._current_version(task_id,"outline")
            selection=view["selection"]
            if (not sample or not selection or sample["outline_hash"] != outline
                or selection["outline_hash"] != outline
                or sample["metadata"].get("selection_hash") != selection["hash"]):
                raise ConflictError("须先基于当前大纲和页面选择重新生成样品")
            state=TaskState.parse(self.get(task_id))
            if state.stage==state.stage.DECK and state.sample_confirmed and view["confirmation"]:
                return view
            if state.stage!=state.stage.SAMPLE:
                raise ConflictError("当前阶段不能确认样品")
            pages=self._slide_fragments(sample["html"])
            if set(pages) != set(selection["slide_ids"]): raise ConflictError("样品页面边界无效，无法确认")
            new=transition(state,"confirm_sample",actor="user")
            confirmed_pages={sid:{"html":fragment,"sha256":digest(fragment.encode())} for sid,fragment in pages.items()}
            result={"confirmed_outline_hash":outline,"confirmed_sample_hash":sample["hash"],"confirmed_content_hash":sample["content_hash"],"selection_hash":view["selection"]["hash"],"confirmed_pages":confirmed_pages}
            event={"event_id":hashlib.sha256(f"{task_id}:confirm-sample:{sample['hash']}".encode()).hexdigest()[:24],"command_id":f"confirm-sample-{sample['hash'][:16]}","action":"confirm_sample_version","actor":"user","request_hash":sample["hash"],"at":utcnow(),"from":state.to_dict(),"to":new.to_dict(),"result":result}
            self.store.commit(task_id,new.to_dict(),event)
            return self.sample_view(task_id)

    @staticmethod
    def _slide_fragments(html_text):
        tag_re=re.compile(r'<section\b[^>]*>|</section\s*>',re.I)
        id_re=re.compile(r'\bid=["\']([A-Za-z0-9_-]+)["\']',re.I)
        class_re=re.compile(r'\bclass=["\']([^"\']*)["\']',re.I)
        fragments={}; stack=[]
        for match in tag_re.finditer(html_text):
            tag=match.group(0)
            if tag.lower().startswith('</'):
                if not stack: continue
                start,sid=stack.pop()
                if sid is not None:
                    if sid in fragments: raise ValidationError("HTML 包含重复页面 ID")
                    fragments[sid]=html_text[start:match.end()]
                continue
            identifier=id_re.search(tag); classes=class_re.search(tag)
            is_slide=bool(classes and "slide" in classes.group(1).split())
            sid=identifier.group(1) if is_slide and identifier else None
            # Only top-level slide sections define page boundaries. Nested
            # sections remain byte-for-byte inside their owning page.
            stack.append((match.start(),sid if not stack else None))
        if stack: raise ValidationError("HTML section 标签未闭合")
        return fragments

    @classmethod
    def _replace_slide_fragments(cls,html_text,replacements):
        fragments=cls._slide_fragments(html_text)
        spans=[]
        for sid,old in fragments.items():
            start=html_text.find(old)
            spans.append((start,start+len(old),replacements.get(sid,old)))
        for start,end,value in sorted(spans,reverse=True): html_text=html_text[:start]+value+html_text[end:]
        return html_text
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
        metadata={"source":action,"operator":actor,**metadata,"html":html_text,"page_hashes":{sid:digest(fragment.encode()) for sid,fragment in fragments.items()}}
        self._record_p3(task_id,"deck",model,metadata,action,actor)
        return self.deck_view(task_id)
    def _commit_candidate_deck(self,task_id,html_text,outline_hash,metadata,action,token,actor="system",before_commit=None):
        """Publish a prepared candidate only if its complete parent snapshot still wins."""
        with self.store.transaction(task_id):
            self._assert_candidate_write_token(task_id,token)
            if metadata.get("parent") != token["parent_deck_hash"]:
                raise ConflictError("候选全稿父版本与提交令牌不一致")
            if before_commit is not None:
                outline_hash=before_commit()
            return self._record_deck(task_id,html_text,outline_hash,metadata,action,actor)
    def generate_deck(self,task_id):
        with self.store.lock(task_id):
            sample_view=self.sample_view(task_id); self._require_current_sample_confirmation(task_id)
            token=self._candidate_write_token(task_id,{"deck"})
        outline=self._current_version(task_id,"outline"); data=json.loads(self.version(task_id,outline)); ids=list(data["slide_ids"])
        sample=sample_view["sample"]; meta=sample["metadata"]
        assets=controlled_assets(self.input_view(task_id)["manifest"],self.store.resource_root(task_id))
        confirmation=sample_view["confirmation"] or {}
        confirmed_pages=confirmation.get("confirmed_pages") or {}
        sample_fragments={sid:item["html"] for sid,item in confirmed_pages.items()}
        if not sample_fragments or any(digest(fragment.encode()) != confirmed_pages[sid].get("sha256") for sid,fragment in sample_fragments.items()):
            raise ConflictError("确认样品原始页面或 SHA-256 无效")
        if isinstance(self.builder,FakeHtmlBuilder):
            html_text=render(data["markdown"],ids,meta.get("global_rules",[]),meta.get("local_exceptions",{}),assets)
        else:
            from .execution import checkpoint, progress
            unconfirmed=[sid for sid in ids if sid not in sample_fragments]
            generated={}
            for index in range(0,len(unconfirmed),3):
                checkpoint(); batch=unconfirmed[index:index+3]
                progress("generating_batch",f"生成未确认页面 {index+1}-{index+len(batch)} / {len(unconfirmed)}")
                partial=self.builder.build(data["markdown"],action="deck",slide_ids=batch,rules=meta.get("global_rules",[]),exceptions=meta.get("local_exceptions",{}),assets=assets)
                generated.update(self._slide_fragments(validate_html(partial,batch,assets)))
            ordered={**generated,**sample_fragments}
            shell=render(data["markdown"],ids,meta.get("global_rules",[]),meta.get("local_exceptions",{}),assets)
            html_text=self._replace_slide_fragments(shell,ordered)
        html_text=validate_html(html_text,ids,assets); deck_fragments=self._slide_fragments(html_text)
        # Confirmed fragments are immutable and are merged by the server for
        # every builder implementation, including deterministic test/fallback
        # builders.
        html_text=self._replace_slide_fragments(html_text,sample_fragments)
        html_text=validate_html(html_text,ids,assets); deck_fragments=self._slide_fragments(html_text)
        preserved={sid:digest(deck_fragments[sid].encode())==digest(fragment.encode()) for sid,fragment in sample_fragments.items()}
        if not all(preserved.values()): raise ConflictError("确认样品发生未提示变化")
        return self._commit_candidate_deck(task_id,html_text,outline,{"parent":token["parent_deck_hash"],"summary":"生成未检查候选稿","scope":"global","affected":ids,"sample_hash":sample["hash"],"sample_pages_preserved":preserved,"outline_consistent":True,"inspection_status":"pending","global_rules":meta.get("global_rules",[]),"local_exceptions":meta.get("local_exceptions",{})},"deck_generate",token)
    def modify_deck(self,task_id,prompt,change_type="visual",scope=None,slide_ids=None,element_id=None):
        with self.store.lock(task_id):
            token=self._candidate_write_token(task_id)
            view=self.deck_view(task_id); deck=view["deck"]
        if not deck: raise ConflictError("尚未生成全稿")
        if deck["hash"]!=token["parent_deck_hash"]: raise ConflictError("当前候选全稿版本已变更")
        if not isinstance(prompt,str) or not prompt.strip(): raise ValidationError("修改 Prompt 不得为空")
        if change_type not in {"visual","content"}: raise ValidationError("修改类型只能是 visual 或 content")
        all_ids=list(deck["metadata"]["page_hashes"]); slide_ids=slide_ids or []
        chosen=slide_ids[0] if slide_ids else None
        inferred,understanding=infer_scope(prompt,chosen,element_id,scope)
        if inferred in {"page","element"} and (not slide_ids or any(s not in all_ids for s in slide_ids)): raise ValidationError("修改页面不存在")
        affected=all_ids if inferred=="global" else list(slide_ids)
        outline_hash=self._current_version(task_id,"outline"); outline=json.loads(self.version(task_id,outline_hash)); markdown=outline["markdown"]
        rules=list(deck["metadata"].get("global_rules",[])); exceptions={k:list(v) for k,v in deck["metadata"].get("local_exceptions",{}).items()}
        pending_outline=None
        if change_type=="content":
            order,blocks=parse_outline(markdown,self.input_view(task_id)["manifest"].get("resources",[]),None)
            for sid in affected: blocks[sid] += f"\n- 内容修改：{prompt.strip()}"
            markdown=markdown[:markdown.index("## [")].rstrip()+"\n\n"+"\n\n".join(blocks[s] for s in order)+"\n"
            content_hash=digest(markdown.encode()); model=SlideOutline.parse({"outline_id":f"outline-{content_hash[:16]}","task_id":task_id,"version":len(self.versions(task_id,"outline"))+1,"markdown":markdown,"slide_ids":order,"content_hash":content_hash,"created_at":now(),"schema_version":"1.0"})
            pending_outline=(model,{"parent":outline_hash,"action":"deck_content_edit","summary":prompt.strip(),"affected":affected,"authoritative":True,"invalidated":{"deck":affected}})
            outline_hash=digest(canonical(model.to_dict()))
        elif inferred=="global": rules.append(prompt.strip())
        else:
            for sid in affected: exceptions.setdefault(sid,[]).append((f"元素 {element_id}: " if inferred=="element" else "")+prompt.strip())
        assets=controlled_assets(self.input_view(task_id)["manifest"],self.store.resource_root(task_id))
        deck_slides=self._slide_fragments(deck["html"])
        previous_slides="".join(deck_slides.get(sid,"") for sid in all_ids)
        source=render(markdown,all_ids,rules,exceptions,assets) if isinstance(self.builder,FakeHtmlBuilder) else self.builder.build(markdown,action="deck",slide_ids=all_ids,rules=rules,exceptions=exceptions,assets=assets,previous_slides=previous_slides,prompt=prompt,scope=inferred,affected_slide_ids=affected,element_id=element_id)
        html_text=validate_html(source,all_ids,assets)
        before=deck["metadata"]["page_hashes"]; after={sid:digest(fragment.encode()) for sid,fragment in self._slide_fragments(html_text).items()}; actual=[sid for sid in all_ids if before[sid]!=after[sid]]
        if any(s not in affected for s in actual): raise ConflictError("修改超出声明影响范围")
        # Cross-artifact edits are prepared and validated above. Only successful
        # render/validation may publish the outline and deck versions.
        publish_outline=(lambda:self._record_p3(task_id,"outline",pending_outline[0],pending_outline[1],"outline_edit","user")) if pending_outline else None
        return self._commit_candidate_deck(task_id,html_text,outline_hash,{"parent":deck["hash"],"summary":prompt.strip(),"scope":inferred,"change_type":change_type,"affected":actual,"requested_affected":affected,"unchanged":[s for s in all_ids if s not in actual],"scope_understanding":understanding,"element_id":element_id,"outline_consistent":True,"global_rules":rules,"local_exceptions":exceptions},"deck_modify",token,"user",publish_outline)
    def rollback_deck(self,task_id,target_hash):
        with self.store.lock(task_id):
            token=self._candidate_write_token(task_id)
            known={v["hash"] for v in self.versions(task_id,"deck")}
            if target_hash not in known: raise ValidationError("目标全稿版本不存在")
            target=json.loads(self.version(task_id,target_hash)); meta=next(v["metadata"] for v in self.versions(task_id,"deck") if v["hash"]==target_hash)
            current_outline=self._current_version(task_id,"outline"); inconsistent=target["outline_hash"]!=current_outline
        return self._commit_candidate_deck(task_id,meta["html"],target["outline_hash"],{"parent":token["parent_deck_hash"],"rollback_from":target_hash,"summary":f"回退自 {target_hash[:12]}","scope":"global","affected":list(meta["page_hashes"]),"outline_consistent":not inconsistent,"regenerate_required":list(meta["page_hashes"]) if inconsistent else [],"global_rules":meta.get("global_rules",[]),"local_exceptions":meta.get("local_exceptions",{})},"deck_rollback",token,"user")

    def inspection_view(self,task_id):
        deck=self.deck_view(task_id)["deck"]; reports=self.versions(task_id,"inspection")
        current=None
        if reports:
            record=max(reports,key=lambda r:json.loads(self.version(task_id,r["hash"]))["created_at"]); model=json.loads(self.version(task_id,record["hash"]))
            current={**model,"hash":record["hash"],"metadata":record["metadata"],"stale":not deck or model["deck_hash"]!=deck["hash"]}
        dispositions=[]
        for record in self.versions(task_id,"issue-disposition"):
            value=json.loads(self.version(task_id,record["hash"]))
            dispositions.append({**value,"hash":record["hash"],"metadata":record["metadata"],"stale":not deck or value["target_deck_hash"]!=deck["hash"]})
        active={}
        for disposition in sorted((d for d in dispositions if not d["stale"]),key=lambda d:(d["metadata"].get("sequence",0),d["created_at"],d["hash"])):
            active[disposition["issue_id"]]=disposition
        unresolved=[] if not current or current["stale"] else [i for i in current["issues"] if active.get(i["issue_id"],{}).get("action") not in {"resolve","manual","waive"}]
        blockers=[i for i in unresolved if i["severity"]=="blocker"]
        return {"state":self.get(task_id),"deck":deck,"report":current,"reports":reports,"dispositions":dispositions,"unresolved":unresolved,"blocking_issues":blockers,"delivery_allowed":bool(current and not current["stale"] and not blockers),"waiting_reason":self.get(task_id).get("waiting_reason")}

    @staticmethod
    def _normalize_inspection_issues(items):
        normalized=[]
        for index,item in enumerate(items):
            if not isinstance(item,dict): raise ValidationError("检查报告 issue 必须是对象")
            level=item.get("level") or ("element" if item.get("element_id") else "slide" if item.get("slide_id") else "deck")
            normalized.append({"issue_id":item.get("issue_id") or f"issue-{index+1}","severity":item.get("severity","warning"),"level":level,"code":item.get("code","quality_issue"),"message":item.get("message","发现质量问题"),"slide_id":item.get("slide_id","") or "","element_id":item.get("element_id","") or "","evidence":item.get("evidence",item.get("message","发现质量问题")),"suggestion":item.get("suggestion","请人工检查并修复"),"source":item.get("source","semantic_model")})
        return normalized

    def _call_inspector(self,outline,html_text,browser_evidence):
        method=self.inspector.inspect
        parameters=inspect.signature(method).parameters
        accepts_evidence="browser_evidence" in parameters or any(
            item.kind==inspect.Parameter.VAR_KEYWORD for item in parameters.values()
        )
        return method(outline,html_text,browser_evidence=browser_evidence) if accepts_evidence else method(outline,html_text)

    def _prepare_inspection_result(self,task_id,deck):
        outline=json.loads(self.version(task_id,deck["outline_hash"]))["markdown"]
        input_view=self.input_view(task_id)
        content_evidence=inspect_content_quality(deck["html"],{
            "source":input_view.get("source"),
            "task_card":input_view.get("task_card"),
        })
        browser_evidence=None
        if self.browser_inspector is not None:
            browser_evidence=self.browser_inspector.inspect(deck["html"],list(deck["metadata"]["page_hashes"]))
        raw=self._call_inspector(outline,deck["html"],browser_evidence)
        if not isinstance(raw,dict): raise ValidationError("检查响应契约无效")
        model_issues=raw.get("issues",[])
        if not isinstance(model_issues,list): raise ValidationError("检查响应 issues 必须是数组")
        browser_issues=(browser_evidence or {}).get("issues",[])
        content_issues=content_evidence.get("issues",[])
        combined=[]; seen=set()
        sources=(
            ("semantic_deterministic",content_issues),
            ("semantic_model",model_issues),
            ("technical_browser",browser_issues),
        )
        for source,items in sources:
            if not isinstance(items,list): raise ValidationError("检查报告 issues 必须是数组")
            for original in items:
                if not isinstance(original,dict): raise ValidationError("检查报告 issue 必须是对象")
                item={**original,"source":source}
                identity=item.get("issue_id") or fingerprint(item)
                if identity in seen: continue
                seen.add(identity); combined.append(item)
        browser_passed=True if browser_evidence is None else bool(browser_evidence.get("available")) and bool(browser_evidence.get("passed"))
        checks={
            "semantic_deterministic":{"available":True,"passed":bool(content_evidence.get("passed")),"issue_count":len(content_issues),"evidence_hash":fingerprint(content_evidence)},
            "semantic_model":{"available":True,"passed":bool(raw.get("passed",not model_issues)),"issue_count":len(model_issues),"model":raw.get("model","unknown")},
            "technical_browser":{"available":browser_evidence is not None and bool(browser_evidence.get("available")),"passed":browser_passed,"issue_count":len(browser_issues)},
        }
        return {
            **raw,
            "issues":combined,
            "passed":bool(raw.get("passed",not model_issues)) and bool(content_evidence.get("passed")) and browser_passed and not combined,
            "content_evidence":content_evidence,
            "quality_checks":checks,
            **({"browser_evidence":browser_evidence} if browser_evidence is not None else {}),
        }

    def _inspect_once(self,task_id,scope,affected,round_number,prepared_raw=None):
        deck=self.deck_view(task_id)["deck"]
        if not deck: raise ConflictError("尚未生成全稿")
        # Deliberately pass only the original outline, review HTML and local
        # Chromium measurements. Generation dialogue, model self-description,
        # resources and screenshots never cross this boundary.
        skill=self.skills.load("inspection")
        raw=prepared_raw if prepared_raw is not None else self._prepare_inspection_result(task_id,deck)
        issues=self._normalize_inspection_issues(raw.get("issues",[])); passed=bool(raw.get("passed",not issues)) and not issues
        created=utcnow(); seed=canonical({"deck_hash":deck["hash"],"issues":issues,"created_at":created})
        report=InspectionReport.parse({"report_id":f"report-{digest(seed)[:16]}","task_id":task_id,"deck_hash":deck["hash"],"issues":issues,"passed":passed,"created_at":created,"schema_version":"1.0"})
        browser=raw.get("browser_evidence")
        content=raw.get("content_evidence")
        browser_meta=None if not isinstance(browser,dict) else {
            "available":bool(browser.get("available")),
            "passed":bool(browser.get("passed")),
            "engine":browser.get("engine"),
            "engine_version":browser.get("engine_version"),
            "viewport":browser.get("viewport"),
            "issue_count":len(browser.get("issues",[])),
            "evidence_hash":fingerprint(browser),
        }
        content_meta=None if not isinstance(content,dict) else {
            "available":bool(content.get("available")),
            "passed":bool(content.get("passed")),
            "issue_count":len(content.get("issues",[])),
            "visible_text_hash":content.get("visible_text_hash"),
            "source_binding_hash":content.get("source_binding_hash"),
            "evidence_hash":fingerprint(content),
        }
        metadata={"deck_hash":deck["hash"],"scope":scope,"affected_slide_ids":affected,"includes_deck_consistency":True,"includes_content_quality":content_meta is not None,"includes_browser_render":browser_meta is not None,"content_evidence":content_meta,"browser_evidence":browser_meta,"quality_checks":raw.get("quality_checks",{}),"issue_sources":{item["issue_id"]:item["source"] for item in issues},"round":round_number,"model":raw.get("model","unknown"),"skill":{"action":"inspection","version":skill["version"],"hash":digest(skill["content"].encode()),"files":skill.get("files",[])},"input_fields":["original_outline","html","frozen_source_binding",*(["browser_evidence"] if browser_meta is not None else [])],"excluded_fields":["generation_context","self_description","images","screenshots"]}
        h=self.store.put_version(task_id,"inspection",canonical(report.to_dict()),metadata)
        return {**report.to_dict(),"hash":h,"metadata":metadata}

    def _prepare_auto_fix(self,task_id,report,round_number,issue_ids=None):
        with self.store.lock(task_id):
            token=self._candidate_write_token(task_id)
            deck=self.deck_view(task_id)["deck"]
        if not deck or deck["hash"]!=token["parent_deck_hash"]: raise ConflictError("当前候选全稿版本已变更")
        selected=[item for item in report["issues"] if issue_ids is None or item["issue_id"] in set(issue_ids)]
        if not selected: raise ValidationError("检查修复范围为空")
        affected=list(dict.fromkeys(i["slide_id"] for i in selected if i["slide_id"]))
        if not affected: affected=list(deck["metadata"]["page_hashes"])
        outline=json.loads(self.version(task_id,deck["outline_hash"]))["markdown"]
        assets=controlled_assets(self.input_view(task_id)["manifest"],self.store.resource_root(task_id))
        suggestions=[{"issue_id":i["issue_id"],"slide_id":i["slide_id"],"element_id":i["element_id"],"code":i["code"],"suggestion":i["suggestion"]} for i in selected]
        if isinstance(self.builder,FakeHtmlBuilder):
            rules=list(deck["metadata"].get("global_rules",[])); exceptions={k:list(v) for k,v in deck["metadata"].get("local_exceptions",{}).items()}
            for slide_id in affected: exceptions.setdefault(slide_id,[]).append(f"检查修复第 {round_number} 轮："+"；".join(s["suggestion"] for s in suggestions if s["slide_id"]==slide_id))
            html_text=render(outline,list(deck["metadata"]["page_hashes"]),rules,exceptions,assets)
        else:
            html_text=self.builder.build(outline,action="inspection",slide_ids=list(deck["metadata"]["page_hashes"]),assets=assets,previous_html=deck["html"],inspection_report=report,suggestions=suggestions,affected_slide_ids=affected)
        html_text=validate_html(html_text,list(deck["metadata"]["page_hashes"]),assets)
        candidate_slides=self._slide_fragments(html_text)
        html_text=self._replace_slide_fragments(deck["html"],{slide_id:candidate_slides[slide_id] for slide_id in affected})
        html_text=validate_html(html_text,list(deck["metadata"]["page_hashes"]),assets)
        metadata={"parent":deck["hash"],"summary":f"自动修复第 {round_number} 轮","scope":"page","affected":affected,"outline_consistent":True,"global_rules":deck["metadata"].get("global_rules",[]),"local_exceptions":deck["metadata"].get("local_exceptions",{}),"inspection_report_hash":report["hash"],"inspection_issue_ids":[item["issue_id"] for item in selected],"auto_fix_round":round_number}
        return html_text,deck["outline_hash"],metadata,token

    def _auto_fix(self,task_id,report,round_number,prepared=None):
        html_text,outline_hash,metadata,token=prepared or self._prepare_auto_fix(task_id,report,round_number)
        return self._commit_candidate_deck(task_id,html_text,outline_hash,metadata,"deck_auto_fix",token,"system")["deck"]

    def run_inspection(self,task_id,max_rounds=2,affected_slide_ids=None,_prepared_raw=None):
        metric_started=time.monotonic()
        state=self._require_candidate_mutable(task_id)
        if not isinstance(max_rounds,int) or isinstance(max_rounds,bool) or max_rounds<0 or max_rounds>10: raise ValidationError("max_rounds 必须为 0 到 10 的整数")
        # Obtain the Gateway result before advancing deck -> review.  Public
        # callers therefore observe the exact pre-call snapshot on ambiguity.
        deck=self.deck_view(task_id)["deck"]
        if not deck: raise ConflictError("尚未生成全稿")
        all_ids=list(deck["metadata"]["page_hashes"]); affected_slide_ids=affected_slide_ids or []
        if any(x not in all_ids for x in affected_slide_ids): raise ValidationError("增量检查页面不存在")
        scope="incremental" if affected_slide_ids else "full"; affected=affected_slide_ids or all_ids
        if _prepared_raw is None:
            _prepared_raw=self._prepare_inspection_result(task_id,deck)
        with self.store.transaction(task_id):
            if state.stage==state.stage.DECK: self.command(task_id,f"to-review-{self._current_version(task_id,'deck')[:12]}","advance","system"); state=TaskState.parse(self.get(task_id))
            if state.stage!=state.stage.REVIEW: raise ConflictError("当前阶段不能执行检查")
            report=self._inspect_once(task_id,scope,affected,0,_prepared_raw); rounds=0
            if state.mode in {"auto","quick"}:
                while not report["passed"] and rounds<max_rounds:
                    rounds+=1; self._auto_fix(task_id,report,rounds); report=self._inspect_once(task_id,"incremental",affected,rounds)
            waiting=not report["passed"]
            current=TaskState.parse(self.get(task_id))
            new=current.__class__(**{**current.__dict__,"status":current.status.WAITING_FOR_USER if waiting or state.mode=="manual" else current.status.READY,"waiting_reason":"inspection_round_limit" if waiting and state.mode in {"auto","quick"} else "manual_review" if state.mode=="manual" else None,"required_action":"review_issues" if waiting or state.mode=="manual" else None,"revision":current.revision+1})
            event={"event_id":digest(f"{task_id}:{report['hash']}:inspection".encode())[:24],"command_id":f"inspection-{report['hash'][:16]}","action":"inspection_complete","actor":"system","request_hash":report["hash"],"at":utcnow(),"from":current.to_dict(),"to":new.to_dict(),"result":{"report_hash":report["hash"],"rounds":rounds,"passed":report["passed"],"mode":state.mode}}
            self.store.commit(task_id,new.to_dict(),event)
        logging.info(json.dumps({"event":"action_metric","action":"inspection_run","task_id":task_id,"duration_ms":round((time.monotonic()-metric_started)*1000,2),"failed":False,"repair_rounds":rounds,"passed":report["passed"]}))
        return {**self.inspection_view(task_id),"rounds":rounds}

    def switch_inspection_mode(self,task_id,mode):
        self._require_candidate_mutable(task_id)
        if mode not in {"manual","auto"}: raise ValidationError("mode 只能是 manual 或 auto")
        self.command(task_id,f"inspection-mode-{mode}-{self.get(task_id)['revision']}",f"switch_{mode}","user")
        return self.inspection_view(task_id)

    def dispose_issue(self,task_id,issue_id,action,rationale,actor="user"):
        self._require_candidate_mutable(task_id)
        view=self.inspection_view(task_id); report=view["report"]
        if not report or report["stale"]: raise ConflictError("当前 HTML 版本没有有效检查报告")
        issue=next((x for x in report["issues"] if x["issue_id"]==issue_id),None)
        if not issue: raise ValidationError("检查问题不存在")
        if action not in {"agent_fix","manual","waive","defer"}: raise ValidationError("问题处置动作无效")
        if action in {"manual","waive"} and actor!="user": raise ValidationError("手工处理和豁免必须由用户执行")
        if action=="waive" and (not isinstance(rationale,str) or not rationale.strip()): raise ValidationError("豁免必须填写依据")
        # Build and validate the repair candidate before publishing the audit
        # disposition.  GatewayUnknownResult must not assert that a fix was
        # performed when no repaired deck exists.
        prepared_fix=self._prepare_auto_fix(task_id,report,1,[issue_id]) if action=="agent_fix" else None
        created=utcnow(); payload={"task_id":task_id,"issue_id":issue_id,"action":action,"actor":actor,"target_deck_hash":report["deck_hash"],"rationale":(rationale or "按当前处置执行").strip(),"created_at":created,"schema_version":"1.0"}
        model=IssueDisposition.parse({"disposition_id":f"disposition-{digest(canonical(payload))[:16]}",**payload})
        def record_disposition():
            return self.store.put_version(task_id,"issue-disposition",canonical(model.to_dict()),{"report_hash":report["hash"],"severity":issue["severity"],"code":issue["code"],"sequence":len(self.versions(task_id,"issue-disposition"))+1})
        if action=="agent_fix":
            html_text,outline_hash,metadata,token=prepared_fix
            with self.store.transaction(task_id):
                self._assert_candidate_write_token(task_id,token)
                if metadata.get("parent")!=token["parent_deck_hash"]: raise ConflictError("候选全稿父版本与提交令牌不一致")
                h=record_disposition()
                self._record_deck(task_id,html_text,outline_hash,metadata,"deck_auto_fix","system")
        else:
            with self.store.transaction(task_id):
                self._require_candidate_mutable(task_id)
                h=record_disposition()
        return {**self.inspection_view(task_id),"disposition_hash":h}

    def dispose_issues(self,task_id,issue_ids,action,rationale):
        if not isinstance(issue_ids,list) or not issue_ids or len(set(issue_ids))!=len(issue_ids): raise ValidationError("批量范围必须是非空且不重复的问题 ID")
        if action=="agent_fix" and len(issue_ids)>1: raise ValidationError("Agent 修复会生成新 HTML，请逐项执行并复检")
        view=self.inspection_view(task_id)
        issues={x["issue_id"]:x for x in (view.get("report") or {}).get("issues",[])}
        if any(x not in issues for x in issue_ids): raise ValidationError("批量范围包含未知问题")
        if len({issues[x]["code"] for x in issue_ids})!=1: raise ValidationError("批量处置仅支持同 code 的同类问题")
        hashes=[]
        for issue_id in issue_ids:
            result=self.dispose_issue(task_id,issue_id,action,rationale); hashes.append(result["disposition_hash"])
        return {**self.inspection_view(task_id),"disposition_hashes":hashes,"batch_scope":issue_ids}

    def assert_delivery_gate(self,task_id):
        view=self.inspection_view(task_id)
        if view["blocking_issues"]: raise ConflictError("仍有未解决且未豁免的阻断问题，禁止交付")
        if not view["report"] or view["report"]["stale"]: raise ConflictError("当前 HTML 版本须先完成检查")
        return {"delivery_allowed":True,"warnings":[x for x in view["unresolved"] if x["severity"]=="warning"]}

    def finalization_view(self,task_id):
        deck=self.deck_view(task_id)["deck"]
        records=self.versions(task_id,"final-deck")
        finalizations=[]
        for record in records:
            value=json.loads(self.version(task_id,record["hash"]))
            finalizations.append({**value,"hash":record["hash"],"metadata":record["metadata"],"stale":not deck or value["deck_hash"]!=deck["hash"]})
        finalizations.sort(key=lambda item:(item["finalized_at"],item["hash"]))
        current=next((item for item in reversed(finalizations) if not item["stale"]),None)
        return {"current":current,"history":finalizations}

    def finalize_deck(self,task_id,deck_hash,source="deck",actor="user"):
        if actor!="user": raise ValidationError("终稿必须由用户明确确认")
        with self.store.transaction(task_id):
            self._require_actionable(task_id)
            state=TaskState.parse(self.get(task_id)); deck=self.deck_view(task_id)["deck"]
            if state.stage not in {state.stage.DECK,state.stage.REVIEW}: raise ConflictError("只能在全稿或自检与修改阶段确定终稿")
            if not deck or deck["hash"]!=deck_hash: raise ConflictError("终稿必须绑定当前候选 HTML 版本")
            existing=self.finalization_view(task_id)["current"]
            if existing:
                return {"state":self.get(task_id),"finalization":existing}
            inspection=self.inspection_view(task_id); report=inspection["report"]
            if not report or report["stale"]:
                inspection_status="unchecked" if not report else "stale"
            elif report["passed"]:
                inspection_status="passed"
            elif not inspection["unresolved"]:
                inspection_status="issues_disposed"
            else:
                inspection_status="issues_remaining"
            finalized_at=utcnow()
            payload={"finalization_id":f"final-{deck_hash[:16]}","task_id":task_id,"deck_hash":deck_hash,"finalized_by":"user","finalized_at":finalized_at,"source":source if source in {"deck","review"} else "deck","inspection_status":inspection_status,"inspection_report_hash":None if not report or report["stale"] else report["hash"],"unresolved_issue_count":len(inspection["unresolved"]),"blocking_issue_count":len(inspection["blocking_issues"]),"schema_version":"1.0"}
            final_hash=self.store.put_version(task_id,"final-deck",canonical(payload),{"deck_hash":deck_hash,"source":payload["source"],"inspection_status":inspection_status})
            new=TaskState(**{**state.__dict__,"stage":state.stage.DELIVERY,"status":state.status.READY,"waiting_reason":"final_ready","required_action":"publish_delivery","revision":state.revision+1})
            event={"event_id":digest(f"{task_id}:finalize:{deck_hash}".encode())[:24],"command_id":f"finalize-{deck_hash[:16]}","action":"finalize_deck","actor":"user","request_hash":deck_hash,"at":finalized_at,"from":state.to_dict(),"to":new.to_dict(),"result":{"finalization_hash":final_hash,"deck_hash":deck_hash,"inspection_status":inspection_status}}
            self.store.commit(task_id,new.to_dict(),event)
        return {"state":new.to_dict(),"finalization":{**payload,"hash":final_hash,"metadata":{"deck_hash":deck_hash,"source":payload["source"],"inspection_status":inspection_status},"stale":False}}

    def delivery_view(self,task_id):
        deliveries=[]
        for record in self.versions(task_id,"delivery"):
            model=json.loads(self.version(task_id,record["hash"]))
            deliveries.append({**model,"hash":record["hash"],"metadata":record["metadata"]})
        deliveries.sort(key=lambda item:item["confirmed_at"])
        return {"state":self.get(task_id),"finalization":self.finalization_view(task_id)["current"],"deliveries":deliveries,"latest":deliveries[-1] if deliveries else None,"summary":self.status_summary(task_id)}

    def status_summary(self,task_id):
        state=self.get(task_id); latest={}
        for kind in ("input-snapshot","narrative","outline","sample","deck","inspection","final-deck","delivery"):
            records=self.versions(task_id,kind)
            if not records: continue
            if kind in {"narrative","outline","sample","deck"}: current=self._current_version(task_id,kind)
            elif kind=="input-snapshot": current=next((e["result"].get("snapshot_hash") for e in reversed(self.events(task_id)) if e["action"] in {"import_input","rebuild_input"}),None)
            elif kind=="inspection": current=next((e["result"].get("report_hash") for e in reversed(self.events(task_id)) if e["action"]=="inspection_complete"),None)
            else: current=records[-1]["hash"]
            if current: latest[kind]=current
        progress={"created":5,"clarification":15,"narrative":30,"outline":45,"sample":60,"deck":72,"review":85,"delivery":95}.get(state["stage"],0)
        if state["status"]=="completed": progress=100
        return {"task_id":task_id,"stage":state["stage"],"progress":progress,"status":state["status"],"current_action":state.get("required_action"),"waiting_reason":state.get("waiting_reason"),"human_actions":[state["required_action"]] if state.get("required_action") else [],"latest_artifacts":latest,"error_summary":"task_failed" if state["status"]=="failed" else None}

    def publish_delivery(self,task_id,actor="user"):
        if actor!="user": raise ValidationError("写入工程文件夹必须由用户明确确认")
        state=TaskState.parse(self.get(task_id)); current=self.deck_view(task_id)["deck"]
        if state.status!=state.status.COMPLETED: self._require_actionable(task_id)
        finalization=self.finalization_view(task_id)["current"]
        if not current or not finalization or finalization["deck_hash"]!=current["hash"]: raise ConflictError("请先将当前候选确定为终稿")
        deck_hash=current["hash"]
        if state.stage!=state.stage.DELIVERY: raise ConflictError("当前阶段不能交付")
        narrative_hash=self._current_version(task_id,"narrative"); outline_hash=current["outline_hash"]
        snapshot=self.input_view(task_id)
        existing_delivery=next((r for r in self.versions(task_id,"delivery") if json.loads(self.version(task_id,r["hash"]))["deck_hash"]==deck_hash),None)
        delivery_id=(json.loads(self.version(task_id,existing_delivery["hash"]))["delivery_id"] if existing_delivery else f"delivery-{len(self.versions(task_id,'delivery'))+1}-{deck_hash[:12]}")

        # A delivery fact is the domain idempotency boundary.  Replays for the
        # same finalized deck return that immutable fact even when the caller
        # lost the first response or uses a different transport idempotency key.
        if existing_delivery:
            model=DeliveryManifest.parse(json.loads(self.version(task_id,existing_delivery["hash"])))
            published_root=self.store.delivery_root(task_id,delivery_id)
            verified=verify_delivery(published_root)
            final=self.get(task_id)
            if final["status"]!="completed":
                final=self.command(task_id,f"confirm-delivery-{existing_delivery['hash'][:16]}","confirm_delivery","user",{"deck_hash":deck_hash,"delivery_hash":existing_delivery["hash"]})
            self.store.clear_delivery_intent(task_id,delivery_id)
            hashes={**verified,"manifest.json":digest((published_root/"manifest.json").read_bytes())}
            return {"state":final,"delivery":{**model.to_dict(),"hash":existing_delivery["hash"],"file_hashes":hashes},"result":self.status_summary(task_id)}

        intent=self.store.delivery_intent(task_id,delivery_id,{"task_id":task_id,"deck_hash":deck_hash})
        confirmed_at=intent["confirmed_at"]
        warnings=[]
        if finalization["unresolved_issue_count"]:
            warnings=[{"code":"finalized_with_issues","count":finalization["unresolved_issue_count"],"inspection_status":finalization["inspection_status"]}]

        def complete(model,hashes,localized_count,delivery_hash=None):
            delivery_hash=delivery_hash or self.store.put_version(task_id,"delivery",canonical(model.to_dict()),{"file_hashes":hashes,"warnings":warnings,"issue_summary":{"unresolved":finalization["unresolved_issue_count"],"blockers":finalization["blocking_issue_count"]},"finalization_hash":finalization["hash"],"localized_resources":localized_count,"package":delivery_id})
            if self.store.fault: self.store.fault("after_delivery_fact")
            final=self.command(task_id,f"confirm-delivery-{delivery_hash[:16]}","confirm_delivery","user",{"deck_hash":deck_hash,"delivery_hash":delivery_hash})
            if self.store.fault: self.store.fault("after_delivery_completed")
            self.store.clear_delivery_intent(task_id,delivery_id)
            return {"state":final,"delivery":{**model.to_dict(),"hash":delivery_hash,"file_hashes":hashes},"result":self.status_summary(task_id)}

        # A crash can happen after the directory was atomically published but
        # before the delivery fact/state commit.  Reuse the verified package;
        # never re-download a remote image whose bytes may have changed.
        try:
            published_root=self.store.delivery_root(task_id,delivery_id)
        except NotFoundError:
            published_root=None
        if published_root is not None:
            verify_delivery(published_root)
            package=json.loads((published_root/"manifest.json").read_text(encoding="utf-8"))
            if package.get("delivery_id")!=delivery_id or package.get("task_id")!=task_id or package.get("deck_hash")!=deck_hash or package.get("confirmed_at")!=confirmed_at:
                raise ConflictError("已发布离线包与当前交付事务不一致")
            hashes={name:digest((published_root/name).read_bytes()) for name in package.get("files",{})}
            if hashes!=package.get("files"):
                raise ConflictError("已发布离线包 manifest 不一致")
            hashes["manifest.json"]=digest((published_root/"manifest.json").read_bytes())
            localized=json.loads((published_root/"localized-resources.json").read_text(encoding="utf-8")).get("resources",[])
            if existing_delivery:
                model=DeliveryManifest.parse(json.loads(self.version(task_id,existing_delivery["hash"])))
                delivery_hash=existing_delivery["hash"]
            else:
                model=DeliveryManifest.parse({"delivery_id":delivery_id,"task_id":task_id,"deck_hash":deck_hash,"files":tuple([*package["files"],"manifest.json"]),"confirmed_by":"user","confirmed_at":confirmed_at,"schema_version":"1.0"})
                delivery_hash=None
            return complete(model,hashes,len(localized),delivery_hash)

        resource_root=self.store.resource_root(task_id)
        localized_html,localized_resources,localization_records=localize_delivery_html(current["html"],snapshot["manifest"],resource_root)
        result_summary={"version":delivery_id,"status":{"stage":"delivery","status":"completed"},"description":"用户确定的终稿已写入工程文件夹并通过离线校验"}
        files={"deck.html":localized_html.encode(),"index.html":offline_player(localized_html).encode(),"narrative.md":json.loads(self.version(task_id,narrative_hash))["markdown"].encode(),"outline.md":json.loads(self.version(task_id,outline_hash))["markdown"].encode(),"resource-manifest.json":canonical(snapshot["manifest"]),"localized-resources.json":canonical({"resources":localization_records}),"result.json":canonical({"task_id":task_id,"deck_hash":deck_hash,"finalization_hash":finalization["hash"],"inspection_status":finalization["inspection_status"],"warnings":warnings,"confirmed_at":confirmed_at,**result_summary}),**offline_assets(),**localized_resources}
        for item in snapshot["manifest"].get("resources",[]):
            relative=Path(item["uri"].removeprefix("resources://"))
            source=resource_root/relative
            if source.is_file() and digest(source.read_bytes())==item["content_hash"]: files[f"resources/{relative.as_posix()}"]=source.read_bytes()
        hashes={name:digest(content) for name,content in files.items()}
        package_manifest={"delivery_id":delivery_id,"task_id":task_id,"deck_hash":deck_hash,"confirmed_by":"user","confirmed_at":confirmed_at,"files":hashes}
        files["manifest.json"]=canonical(package_manifest); hashes["manifest.json"]=digest(files["manifest.json"])
        self.store.publish_delivery(task_id,delivery_id,files,verifier=verify_delivery)
        model=DeliveryManifest.parse({"delivery_id":delivery_id,"task_id":task_id,"deck_hash":deck_hash,"files":tuple(files),"confirmed_by":"user","confirmed_at":confirmed_at,"schema_version":"1.0"})
        return complete(model,hashes,len(localization_records))

    def confirm_delivery(self,task_id,deck_hash,actor="user"):
        """Compatibility wrapper for clients which previously combined both actions."""
        if not self.finalization_view(task_id)["current"]:
            self.finalize_deck(task_id,deck_hash,"review" if self.get(task_id)["stage"]=="review" else "deck",actor)
        return self.publish_delivery(task_id,actor)

    def derive_from_delivery(self,task_id,delivery_hash,prompt,slide_ids=None):
        with self.store.transaction(task_id):
            record=next((x for x in self.versions(task_id,"delivery") if x["hash"]==delivery_hash),None)
            if not record: raise ValidationError("交付版本不存在")
            delivered=json.loads(self.version(task_id,delivery_hash)); current=TaskState.parse(self.get(task_id))
            if current.status!=current.status.COMPLETED: raise ConflictError("只有已完成任务可从交付派生")
            deck_record=next(x for x in self.versions(task_id,"deck") if x["hash"]==delivered["deck_hash"])
            target=json.loads(self.version(task_id,delivered["deck_hash"])); meta=deck_record["metadata"]
            reopened=TaskState(**{**current.__dict__,"stage":current.stage.DECK,"status":current.status.READY,"delivery_confirmed":False,"blockers_resolved":False,"revision":current.revision+1})
            event={"event_id":digest(f"{task_id}:derive:{delivery_hash}:{current.revision}".encode())[:24],"command_id":f"derive-{delivery_hash[:16]}-{current.revision}","action":"derive_delivery","actor":"user","request_hash":fingerprint({"prompt":prompt,"slide_ids":slide_ids}),"at":utcnow(),"from":current.to_dict(),"to":reopened.to_dict(),"result":{"delivery_hash":delivery_hash,"deck_hash":delivered["deck_hash"]}}
            self.store.commit(task_id,reopened.to_dict(),event)
            self._record_deck(task_id,meta["html"],target["outline_hash"],{"parent":delivered["deck_hash"],"derived_from_delivery":delivery_hash,"summary":"从已交付版本派生候选","scope":"global","affected":[],"outline_consistent":True,"global_rules":meta.get("global_rules",[]),"local_exceptions":meta.get("local_exceptions",{})},"delivery_derive","user")
        return self.modify_deck(task_id,prompt,scope="page" if slide_ids else "global",slide_ids=slide_ids or [])
