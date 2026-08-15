from __future__ import annotations

import hashlib, json, os, socket, urllib.error, urllib.request, uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from .errors import GatewayError, GatewayUnknownResult, ValidationError
from .agent_runtime import AgentRuntime
from .audit import current_agent_audit_context
from .skill_runtime import SkillRuntime

class GenerationGateway(Protocol):
    def generate(self, action:str, payload:dict, *, skill:str)->dict: ...
class InspectionGateway(Protocol):
    def inspect(self, original_outline:str, html:str)->dict: ...
class SkillLoader(Protocol):
    def load(self, action:str)->dict: ...
class HtmlBuilder(Protocol):
    def build(self, outline:str, **context)->str: ...

@dataclass
class FakeSkillLoader:
    version:str="fake-skill-1"
    def load(self,action): return {"action":action,"version":self.version,"content":f"rules:{action}"}
@dataclass
class FakeGenerationGateway:
    model:str="fake-generator-1"
    def generate(self,action,payload,*,skill): return {"text":f"{action}:{payload.get('task_id','task')}","model":self.model,"skill":skill}
@dataclass
class FakeInspectionGateway:
    model:str="fake-inspector-1"
    calls:list|None=None
    def inspect(self,original_outline,html):
        if self.calls is not None:self.calls.append({"outline":original_outline,"html":html})
        return {"issues":[{"issue_id":"fake-overflow","severity":"blocker","level":"element","code":"text_overflow","message":"标题文本溢出","slide_id":"slide-1","element_id":"title","evidence":"标题超出安全框","suggestion":"缩短标题或减小字号"}],"passed":False,"model":self.model}
@dataclass
class FakeHtmlBuilder:
    version:str="fake-builder-1"
    def build(self,outline,**context): return f"<!doctype html><html><body>{outline}</body></html>"

class DirectorySkillLoader:
    ACTIONS={"narrative","outline","sample","deck","inspection"}
    def __init__(self,root): self.root=Path(root).resolve()
    def load(self,action):
        if action not in self.ACTIONS: raise ValidationError("Skill action 不在允许列表")
        path=(self.root/f"{action}.md").resolve()
        if self.root not in path.parents or not path.is_file(): raise ValidationError(f"缺少 Skill：{action}")
        content=path.read_text(encoding="utf-8")
        if not content.strip() or len(content.encode())>256*1024: raise ValidationError("Skill 内容为空或超过 256 KiB")
        return {"action":action,"version":hashlib.sha256(content.encode()).hexdigest()[:16],"content":content}

class JsonHttpModelGateway:
    """Vendor-neutral adapter. It never blindly retries an unknown result."""
    def __init__(self,endpoint,model,api_key="",timeout=30.0,purpose="generation"):
        if not endpoint.startswith("https://") and not endpoint.startswith("http://127.0.0.1:") and not endpoint.startswith("http://localhost:"):
            raise ValidationError("模型端点必须使用 HTTPS（本机回环地址除外）")
        self.endpoint,self.model,self.api_key=endpoint,model,api_key
        self.timeout,self.purpose=float(timeout),purpose
    def _call(self,payload):
        body=json.dumps({"model":self.model,"purpose":self.purpose,**payload},ensure_ascii=False).encode()
        headers={"Content-Type":"application/json","Accept":"application/json"}
        if self.api_key: headers["Authorization"]=f"Bearer {self.api_key}"
        request=urllib.request.Request(self.endpoint,data=body,headers=headers,method="POST")
        try:
            with urllib.request.urlopen(request,timeout=self.timeout) as response: raw=response.read(4*1024*1024+1)
        except urllib.error.HTTPError as exc: raise GatewayError(f"模型服务返回 HTTP {exc.code}") from exc
        except (TimeoutError,socket.timeout) as exc: raise GatewayError("模型调用超时") from exc
        except (urllib.error.URLError,ConnectionError,OSError) as exc: raise GatewayUnknownResult("模型调用结果未知，请人工确认后再重试") from exc
        if len(raw)>4*1024*1024: raise GatewayError("模型响应超过 4 MiB")
        try: value=json.loads(raw)
        except (UnicodeDecodeError,json.JSONDecodeError) as exc: raise GatewayError("模型响应不是有效 JSON") from exc
        if not isinstance(value,dict): raise GatewayError("模型响应必须为 JSON object")
        return value
    def generate(self,action,payload,*,skill):
        value=self._call({"action":action,"input":payload,"skill":skill})
        if not isinstance(value.get("text"),str) or not value["text"].strip(): raise GatewayError("生成响应缺少 text")
        return {**value,"model":self.model}
    def inspect(self,original_outline,html):
        value=self._call({"original_outline":original_outline,"html":html})
        if not isinstance(value.get("passed"),bool) or not isinstance(value.get("issues"),list): raise GatewayError("检查响应契约无效")
        return {**value,"model":self.model}

class ModelHtmlBuilder:
    version="model-html-v1"
    def __init__(self,gateway,skill_loader): self.gateway,self.skills=gateway,skill_loader
    def build(self,outline,**context):
        action=context.pop("action", "deck")
        if action not in {"sample", "deck", "inspection"}: raise ValidationError("HTML Builder action 不在允许列表")
        skill=self.skills.load(action)
        return self.gateway.generate(action + "_html",{"outline":outline,**context},skill=skill["content"])["text"]

class AgentGateway:
    """Adapts the constrained stage runtime to the existing FSM ports.

    It deliberately exposes no workflow operation to the model.  The service
    remains the only owner of stages, versions, approvals and commits.
    """
    def __init__(self, client, *, skill=None, max_steps=12, timeout_seconds=60, model="agent"):
        self.client, self.model = client, model
        self.max_steps, self.timeout_seconds = max_steps, timeout_seconds
        self.skill_factory = SkillRuntime.builtin if skill is None else lambda: SkillRuntime(skill.root, max_file_bytes=skill.max_file_bytes, max_total_bytes=skill.max_total_bytes)
        self.runtime = None
        self.audit_sink = None
        self.last_probe_audit = None

    def set_audit_sink(self, sink): self.audit_sink = sink

    def _run(self, stage, payload):
        # Read quotas and audit are scoped to one stage invocation.  Reusing a
        # mutable SkillRuntime would let earlier stages consume later budgets.
        self.runtime = AgentRuntime(self.client, self.skill_factory(), max_steps=self.max_steps, timeout_seconds=self.timeout_seconds)
        failure = None
        try:
            result = self.runtime.run(stage, payload)
            return result.value
        except Exception as exc:
            failure = exc
            raise
        finally:
            if self.audit_sink and self.runtime.last_audit:
                audit_id = f"agent-audit-{uuid.uuid4().hex}"
                context = current_agent_audit_context()
                if "task_id" not in context and isinstance(payload.get("task_id"), str):
                    context["task_id"] = payload["task_id"]
                failure_context = {}
                if failure is not None:
                    failure_context = {
                        "diagnostic_id": getattr(failure, "diagnostic_id", None),
                        "error_code": getattr(failure, "code", "internal_error"),
                    }
                self.audit_sink({"audit_id":audit_id,"stage":stage,"model":self.model,**context,**failure_context,"events":list(self.runtime.last_audit)})
                if failure is not None:
                    failure.agent_audit_id = audit_id

    def probe_capabilities(self, *, probe_id=None):
        """Run three isolated provider checks and retain a secret-free trace."""
        probe_id = probe_id or f"runtime-probe-{uuid.uuid4().hex}"
        events = []

        def record(check, status, **details):
            events.append({"event":"probe_check","check":check,"status":status,**details})
            self.last_probe_audit={"probe_id":probe_id,"model":self.model,"status":"failed" if status=="failed" else "checking","events":list(events)}

        def failed(check, exc):
            error = exc if isinstance(exc, GatewayError) else GatewayError(
                "模型能力探测发生无法分类的 SDK 故障",
                code="capability_probe_failed",
                audit_details={"category":"sdk_error","sdk_exception_type":type(exc).__name__,"retryable":False},
            )
            # `OpenAIResponsesClient` deliberately preserves secret-free SDK
            # metadata on an otherwise unclassified `gateway_error`.  Audit
            # detail presence does not make that public code actionable: a
            # probe must still identify which capability check failed.  Codes
            # already classified by the adapter (auth, rate limit, transport,
            # etc.) remain untouched.
            if error.code == "gateway_error":
                error.code={
                    "basic_response":"probe_basic_response_failed",
                    "strict_json_schema":"probe_invalid_output",
                    "tool_round_trip":"probe_tool_round_failed",
                }[check]
            record(check,"failed",error_code=error.code,diagnostic_id=error.diagnostic_id,**error.safe_audit_details())
            self.last_probe_audit={**self.last_probe_audit,"status":"failed","failed_check":check}
            error.probe_id=probe_id
            error.failed_check=check
            if error is exc:
                raise error
            raise error from exc

        check="basic_response"
        record(check,"started")
        try:
            basic=self.client.create(
                input=[{"role":"system","content":"运行时连接探测。只返回 OK。"},{"role":"user","content":"OK"}],
                tools=[],
                response_schema=None,
            )
            if not isinstance(basic.text,str) or not basic.text.strip():
                raise GatewayError("模型基础响应缺少文本结果")
            record(check,"succeeded",response_id_sha256=hashlib.sha256((basic.response_id or "").encode()).hexdigest())
        except Exception as exc:
            failed(check,exc)

        check="strict_json_schema"
        record(check,"started")
        try:
            clarification = AgentRuntime(
                self.client,
                self.skill_factory(),
                max_steps=self.max_steps,
                timeout_seconds=self.timeout_seconds,
            ).run("clarification", {"capability_probe": "return_empty_questions"}, capability_probe=True)
            if clarification.value != {"questions": []}:
                raise GatewayError("模型未按探测契约返回空问题集",code="probe_invalid_output")
            record(check,"succeeded",response_id_sha256=hashlib.sha256((clarification.response_id or "").encode()).hexdigest())
        except Exception as exc:
            failed(check,exc)

        check="tool_round_trip"
        record(check,"started")
        try:
            tools = AgentRuntime(
                self.client,
                self.skill_factory(),
                max_steps=self.max_steps,
                timeout_seconds=self.timeout_seconds,
            ).run("narrative", {"capability_probe": "list_skill_files_then_return_markdown"}, capability_probe=True)
            if not any(event.get("event") == "tool" for event in tools.audit):
                raise GatewayError("模型未完成强制工具调用",code="probe_tool_call_missing")
            record(check,"succeeded",response_id_sha256=hashlib.sha256((tools.response_id or "").encode()).hexdigest())
        except Exception as exc:
            failed(check,exc)

        checks={"basic_response":True,"strict_json_schema":True,"tool_round_trip":True}
        self.last_probe_audit={"probe_id":probe_id,"model":self.model,"status":"succeeded","checks":checks,"events":list(events)}
        return checks

    def generate(self, action, payload, *, skill=""):
        if action not in {"narrative", "outline"}:
            raise ValidationError("Agent 生成阶段无效")
        return {"text": self._run(action, payload)["markdown"], "model": self.model}

    def clarify(self, payload):
        return {**self._run("clarification", payload), "model": self.model}

    def build(self, outline, **context):
        action = context.pop("action", "deck")
        if action not in {"sample", "deck", "inspection"}:
            raise ValidationError("Agent HTML 阶段无效")
        # Inspection repair still generates deck HTML; inspection itself uses
        # inspect() and can never return a modified artifact.
        stage = "deck" if action == "inspection" else action
        return self._run(stage, {"outline": outline, **context})["html"]

    def inspect(self, original_outline, html):
        value = self._run("inspection", {"original_outline": original_outline, "html": html})
        return {**value, "model": self.model}

class LockedSkillMetadataLoader:
    """Metadata-only compatibility port; Skill text is read by Agent tools."""
    def __init__(self, skill=None): self.skill = skill or SkillRuntime.builtin()
    def load(self, action):
        if action not in {"narrative", "outline", "sample", "deck", "inspection"}: raise ValidationError("Skill action 不在允许列表")
        return {"action": action, "version": self.skill.skill_version, "content": ""}

def agent_gateways_from_config(config):
    if config.mode == "fake": return {}
    from .model_clients import model_clients_from_config
    clients = model_clients_from_config(config); skill = SkillRuntime.builtin()
    generation = AgentGateway(clients["generation"], skill=skill, max_steps=config.generation.max_steps, timeout_seconds=config.generation.timeout_seconds, model=config.generation.model)
    inspection = AgentGateway(clients["inspection"], skill=skill, max_steps=config.inspection.max_steps, timeout_seconds=config.inspection.timeout_seconds, model=config.inspection.model)
    return {"generator": generation, "clarifier": generation, "builder": generation, "inspector": inspection, "skills": LockedSkillMetadataLoader(skill)}

def gateways_from_env():
    mode=os.environ.get("PPT_AGENT_GATEWAY_MODE","fake")
    if mode=="fake": return {}
    if mode!="http": raise ValidationError("PPT_AGENT_GATEWAY_MODE 只能是 fake 或 http")
    endpoint=os.environ.get("PPT_AGENT_MODEL_ENDPOINT",""); model=os.environ.get("PPT_AGENT_MODEL",""); skill_root=os.environ.get("PPT_AGENT_SKILL_DIR","")
    if not endpoint or not model or not skill_root: raise ValidationError("http 模式缺少模型端点、模型名或 Skill 目录")
    timeout=float(os.environ.get("PPT_AGENT_MODEL_TIMEOUT","30")); key=os.environ.get("PPT_AGENT_API_KEY","")
    generator=JsonHttpModelGateway(endpoint,model,key,timeout,"generation"); skills=DirectorySkillLoader(skill_root)
    return {"generator":generator,"inspector":JsonHttpModelGateway(endpoint,model,key,timeout,"independent_inspection"),"skills":skills,"builder":ModelHtmlBuilder(generator,skills)}
