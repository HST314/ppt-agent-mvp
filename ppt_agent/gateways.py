from __future__ import annotations

import hashlib, json, os, re, socket, urllib.error, urllib.request, uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from .errors import GatewayError, GatewayUnknownResult, ValidationError
from .agent_runtime import AgentRuntime, STAGE_OUTPUT_SCHEMAS, STAGE_PROVIDER_SCHEMAS, _extract_json_object, normalize_rendering_output
from .audit import current_agent_audit_context
from .claim_ledger import audit_html_claims_by_slide
from .design_contract import validate_design_intent, validate_shared_design_assets
from .p4 import assemble_presentation
from .skill_runtime import ActiveSkillResolver, SkillRuntime

class GenerationGateway(Protocol):
    def generate(self, action:str, payload:dict, *, skill:str)->dict: ...
class InspectionGateway(Protocol):
    def inspect(self, original_outline:str, html:str, *, browser_evidence:dict|None=None)->dict: ...
class SkillLoader(Protocol):
    def load(self, action:str)->dict: ...
class HtmlBuilder(Protocol):
    def build(self, outline:str, **context)->str: ...

class BoundaryCheckedHtml(str):
    """String-compatible builder result carrying the server-consumed self-check."""
    def __new__(cls,value,boundary=None,*,design_intent=None,shared_assets=None):
        result=super().__new__(cls,value)
        result.builder_boundary=boundary
        result.design_intent=validate_design_intent(design_intent)
        result.shared_assets=validate_shared_design_assets(shared_assets)
        return result

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
    # The deterministic demo adapter stands in for an Agent and therefore
    # owns its visual direction; the generic assembler remains style-free.
    design_intent={
        "style_summary":"清晰的本地演示基线",
        "color_strategy":"深色画布上的白色页面",
        "typography_strategy":"系统无衬线字体与大号标题",
        "layout_principles":["统一安全边距","清晰内容层级"],
        "rationale":"为无模型测试和本地演示提供可读输出",
    }
    shared_assets={"css":"body{background:#111827;font-family:Arial,sans-serif;padding:24px 0}.slide{margin-bottom:24px;background:#fff;color:#111827;padding:56px 64px}.slide h1,.slide h2,.slide [data-element-id=\"title\"]{font-size:52px;line-height:1.12}.slide p,.slide li{font-size:24px;line-height:1.45}"}
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
        self.requires_browser_evidence=purpose=="independent_inspection"
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
    def inspect(self,original_outline,html,*,browser_evidence=None):
        value=self._call({"original_outline":original_outline,"html":html,"browser_evidence":browser_evidence})
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
    def __init__(self, client, *, skill=None, skill_resolver=None, max_steps=12, max_tool_calls=24, max_provider_calls=8, run_timeout_seconds=300, job_timeout_seconds=330, model="agent", stage_budgets=None):
        self.requires_browser_evidence = True
        self.client, self.model = client, model
        self.max_steps, self.max_tool_calls, self.max_provider_calls = max_steps, max_tool_calls, max_provider_calls
        self.run_timeout_seconds, self.job_timeout_seconds = run_timeout_seconds, job_timeout_seconds
        self.stage_budgets = stage_budgets or {}
        if skill is not None and skill_resolver is not None:
            raise ValidationError("AgentGateway 不能同时指定 Skill 与 ActiveSkillResolver")
        if skill is not None:
            if not isinstance(skill, SkillRuntime):
                raise ValidationError("AgentGateway Skill 无效")
            self.skill_resolver = None
            self.skill_factory = skill.clone
        else:
            self.skill_resolver = skill_resolver or ActiveSkillResolver.builtin()
            if not isinstance(self.skill_resolver, ActiveSkillResolver):
                raise ValidationError("AgentGateway ActiveSkillResolver 无效")
            self.skill_factory = self.skill_resolver.runtime
        self.runtime = None
        self.audit_sink = None
        self.last_probe_audit = None

    def set_audit_sink(self, sink): self.audit_sink = sink

    def capability_probe_key(self):
        key = getattr(self.client, "capability_probe_key", None)
        return key() if callable(key) else None

    def _run(self, stage, payload):
        # Read quotas and audit are scoped to one stage invocation.  Reusing a
        # mutable SkillRuntime would let earlier stages consume later budgets.
        stage_budget = self.stage_budgets.get(stage)
        runtime_limits = {
            "max_steps": getattr(stage_budget, "max_steps", self.max_steps),
            "max_tool_calls": getattr(stage_budget, "max_tool_calls", self.max_tool_calls),
            "max_provider_calls": getattr(stage_budget, "max_provider_calls", self.max_provider_calls),
        }
        for name in ("max_exploration_rounds", "max_unique_files", "max_skill_bytes", "reserved_final_calls"):
            if stage_budget is not None:
                runtime_limits[name] = getattr(stage_budget, name)
        self.runtime = AgentRuntime(
            self.client,
            self.skill_factory(),
            timeout_seconds=self.run_timeout_seconds,
            **runtime_limits,
        )
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
            original_code = error.code
            if error.code == "gateway_error":
                error.code={
                    "basic_response":"probe_basic_response_failed",
                    "strict_json_schema":"probe_invalid_output",
                    "tool_round_trip":"probe_tool_round_failed",
                }[check]
            terminal = next((item for item in reversed(getattr(error, "audit", ())) if item.get("event") == "terminal"), {})
            probe_phase = getattr(error, "probe_phase", None) or terminal.get("probe_phase") or {
                "basic_response": "basic_response",
                "strict_json_schema": "strict_json_schema",
                "tool_round_trip": "tool_request",
            }[check]
            terminal_reason = getattr(error, "terminal_reason", None) or terminal.get("reason") or original_code
            tool_calls = getattr(error, "tool_calls", None)
            if tool_calls is None:
                tool_calls = terminal.get("tool_calls", 0)
            underlying_code = getattr(error, "underlying_code", None)
            if underlying_code is None and original_code != error.code:
                underlying_code = original_code
            details = {
                "probe_phase": probe_phase,
                "terminal_reason": terminal_reason,
                "tool_calls": tool_calls,
                **({"underlying_code": underlying_code} if underlying_code else {}),
            }
            error.probe_phase = probe_phase
            error.terminal_reason = terminal_reason
            error.tool_calls = tool_calls
            error.underlying_code = underlying_code
            record(check,"failed",error_code=error.code,diagnostic_id=error.diagnostic_id,**details,**error.safe_audit_details())
            self.last_probe_audit={**self.last_probe_audit,"status":"failed","failed_check":check}
            error.probe_id=probe_id
            error.failed_check=check
            if error is exc:
                raise error
            raise error from exc

        check="basic_response"
        record(check,"started")
        max_attempts=2
        for attempt in range(1,max_attempts+1):
            try:
                basic=self.client.create(
                    input=[{"role":"system","content":"运行时连接探测。只返回 OK。"},{"role":"user","content":"OK"}],
                    tools=[],
                    response_schema=None,
                )
                if not isinstance(basic.text,str) or not basic.text.strip():
                    raise GatewayError("模型基础响应缺少文本结果")
                record(check,"succeeded",attempt=attempt,response_id_sha256=hashlib.sha256((basic.response_id or "").encode()).hexdigest())
                break
            except Exception as exc:
                details=exc.safe_audit_details() if isinstance(exc,GatewayError) else {}
                sdk_parse_failure=details.get("category")=="sdk_error" or isinstance(exc,AttributeError)
                if sdk_parse_failure and attempt < max_attempts:
                    record(
                        check,
                        "retrying",
                        attempt=attempt,
                        max_attempts=max_attempts,
                        error_code=getattr(exc,"code","capability_probe_failed"),
                        diagnostic_id=getattr(exc,"diagnostic_id",None),
                        **details,
                    )
                    continue
                failed(check,exc)

        check="strict_json_schema"
        record(check,"started")
        try:
            # Probe the production outline schema itself.  It is the most
            # demanding strict schema in the planning path and previously was
            # only exercised by the first real outline request after readiness
            # had already gone green.
            schema_runtime = AgentRuntime(
                self.client,
                self.skill_factory(),
                max_steps=self.max_steps,
                max_tool_calls=self.max_tool_calls,
                max_provider_calls=self.max_provider_calls,
                timeout_seconds=self.run_timeout_seconds,
            )
            outline = self.client.create(
                input=[
                    {"role":"system","content":"运行时严格结构化输出探测。只返回符合指定 Schema 的 JSON，不得调用工具。"},
                    {"role":"user","content":'返回一页大纲：{"slides":[{"title":"探测","purpose":"验证结构化输出","content_markdown":"- 探测内容","resource_uris":[]}]}。'},
                ],
                tools=[],
                response_schema=STAGE_PROVIDER_SCHEMAS["outline"],
            )
            if outline.tool_calls:
                raise GatewayError("模型在 Schema 探测中返回了未授权工具调用",code="probe_invalid_output")
            try:
                outline_value = _extract_json_object(outline.text or "")
                schema_runtime._validate_schema(outline_value, STAGE_OUTPUT_SCHEMAS["outline"]["schema"], "output")
            except (json.JSONDecodeError, GatewayError) as exc:
                if isinstance(exc, GatewayError) and exc.code == "probe_invalid_output":
                    raise
                raise GatewayError("模型未按 outline 探测契约返回有效结构",code="probe_invalid_output") from exc
            record(check,"succeeded",response_id_sha256=hashlib.sha256((outline.response_id or "").encode()).hexdigest())
        except Exception as exc:
            failed(check,exc)

        check="tool_round_trip"
        record(check,"started")
        try:
            tools = AgentRuntime(
                self.client,
                self.skill_factory(),
                max_steps=self.max_steps,
                max_tool_calls=self.max_tool_calls,
                max_provider_calls=self.max_provider_calls,
                timeout_seconds=self.run_timeout_seconds,
            ).run("narrative", {"capability_probe": "read_skill_entry_then_return_markdown"}, capability_probe=True)
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
        value = self._run(action, payload)
        if action == "outline":
            return {"slides": value["slides"], "model": self.model}
        return {"text": value["markdown"], "model": self.model}

    def clarify(self, payload):
        return {**self._run("clarification", payload), "model": self.model}

    def build(self, outline, **context):
        action = context.pop("action", "deck")
        if action not in {"sample", "deck", "inspection"}:
            raise ValidationError("Agent HTML 阶段无效")
        stage = "deck" if action == "inspection" else action
        expected = list(context.get("slide_ids") or [])
        technical_contract = context.get("presentation_technical_contract") or context.get("design_contract")
        technical_contract_hash = context.get("presentation_technical_contract_hash") or context.get("design_contract_hash")
        agent_context = {
            key: item
            for key, item in context.items()
            if key not in {"design_contract", "design_contract_hash"}
        }
        agent_context["presentation_technical_contract"] = technical_contract
        agent_context["presentation_technical_contract_hash"] = technical_contract_hash
        value = self._run(stage, {"outline": outline, **agent_context})
        rendering = normalize_rendering_output(value, expected)
        slides = rendering["slides"]
        design_intent = rendering["design_intent"]
        shared_assets = rendering["shared_assets"]
        confirmed_intent = context.get("confirmed_design_intent")
        confirmed_assets = context.get("confirmed_shared_assets")
        if confirmed_intent is None and "design_intent" not in value and context.get("design_intent") is not None:
            design_intent = validate_design_intent(context["design_intent"])
        if confirmed_assets is None and "shared_assets" not in value and context.get("shared_assets") is not None:
            shared_assets = validate_shared_design_assets(context["shared_assets"])
        if confirmed_intent is not None:
            confirmed_intent = validate_design_intent(confirmed_intent)
            if "design_intent" in value and design_intent != confirmed_intent:
                raise GatewayError("全稿输出未复用已确认 DesignIntent")
            design_intent = confirmed_intent
        if confirmed_assets is not None:
            confirmed_assets = validate_shared_design_assets(confirmed_assets)
            if "shared_assets" in value and shared_assets != confirmed_assets:
                raise GatewayError("全稿输出未复用已确认共享设计资产")
            shared_assets = confirmed_assets
        boundary=None
        required_by_slide=context.get("required_claims_by_slide")
        if required_by_slide is not None:
            ledger=context.get("claim_ledger")
            if not isinstance(ledger,dict) or list(required_by_slide) != expected:
                raise ValidationError("Builder 逐页 required claim 边界输入无效")
            aggregate_ids={item.get("claim_id") for item in context.get("required_claims_verbatim",[]) if isinstance(item,dict)}
            mapped_ids={item.get("claim_id") for items in required_by_slide.values() for item in items if isinstance(item,dict)}
            if aggregate_ids != mapped_ids:
                raise ValidationError("Builder required claim 汇总与逐页映射不一致")
            ids_by_slide={slide_id:[item["claim_id"] for item in required_by_slide[slide_id]] for slide_id in expected}
            boundary=audit_html_claims_by_slide(
                {item["slide_id"]:item["html"] for item in slides},
                ledger,
                ids_by_slide,
            )

        fragments = []
        for item in slides:
            fragments.append(item["html"])

        assembled=assemble_presentation(
            fragments,
            context.get("rules", []),
            technical_contract,
            technical_contract_hash,
            design_intent=design_intent,
            shared_assets=shared_assets,
        )
        return BoundaryCheckedHtml(
            assembled,
            boundary,
            design_intent=design_intent,
            shared_assets=shared_assets,
        )
    
    def inspect(self, original_outline, html, *, browser_evidence=None):
        value = self._run("inspection", {
            "original_outline": original_outline,
            "html": html,
            "browser_evidence": browser_evidence or {
                "available": False,
                "passed": False,
                "issues": [],
                "reason": "browser_evidence_not_supplied",
            },
        })
        return {**value, "model": self.model}

class LockedSkillMetadataLoader:
    """Metadata-only compatibility port; Skill text is read by Agent tools."""
    def __init__(self, skill=None, *, skill_resolver=None):
        if skill is not None and skill_resolver is not None:
            raise ValidationError("Skill metadata loader 不能同时指定 Skill 与 ActiveSkillResolver")
        if skill is not None:
            if not isinstance(skill, SkillRuntime):
                raise ValidationError("Skill metadata loader 的 Skill 无效")
            self.skill_factory = skill.clone
        else:
            resolver = skill_resolver or ActiveSkillResolver.builtin()
            if not isinstance(resolver, ActiveSkillResolver):
                raise ValidationError("Skill metadata loader 的 ActiveSkillResolver 无效")
            self.skill_factory = resolver.runtime
    def load(self, action):
        if action not in {"narrative", "outline", "sample", "deck", "inspection"}: raise ValidationError("Skill action 不在允许列表")
        skill=self.skill_factory()
        files=["SKILL.md"]
        file_hashes={name:skill.manifest[name] for name in files}
        return {
            "action":action,
            "name":skill.skill_name,
            "description":skill.skill_description,
            "version":skill.skill_version,
            "content":json.dumps(file_hashes,sort_keys=True,separators=(",",":")),
            "files":files,
            "file_hashes":file_hashes,
        }

def agent_gateways_from_config(config):
    if config.mode == "fake": return {}
    from .model_clients import model_clients_from_config
    clients = model_clients_from_config(config)
    resolver = ActiveSkillResolver(config.skills.root, config.skills.active)
    generation = AgentGateway(
        clients["generation"], skill_resolver=resolver,
        max_steps=config.generation.max_steps,
        max_tool_calls=config.generation.max_tool_calls,
        max_provider_calls=config.generation.max_provider_calls,
        run_timeout_seconds=config.generation.run_timeout_seconds,
        job_timeout_seconds=config.generation.job_timeout_seconds,
        model=config.generation.model,
        stage_budgets=config.generation.stage_budgets,
    )
    inspection = AgentGateway(
        clients["inspection"], skill_resolver=resolver,
        max_steps=config.inspection.max_steps,
        max_tool_calls=config.inspection.max_tool_calls,
        max_provider_calls=config.inspection.max_provider_calls,
        run_timeout_seconds=config.inspection.run_timeout_seconds,
        job_timeout_seconds=config.inspection.job_timeout_seconds,
        model=config.inspection.model,
    )
    return {"generator": generation, "clarifier": generation, "builder": generation, "inspector": inspection, "skills": LockedSkillMetadataLoader(skill_resolver=resolver)}

def gateways_from_env():
    mode=os.environ.get("PPT_AGENT_GATEWAY_MODE","fake")
    if mode=="fake": return {}
    if mode!="http": raise ValidationError("PPT_AGENT_GATEWAY_MODE 只能是 fake 或 http")
    endpoint=os.environ.get("PPT_AGENT_MODEL_ENDPOINT",""); model=os.environ.get("PPT_AGENT_MODEL",""); skill_root=os.environ.get("PPT_AGENT_SKILL_DIR","")
    if not endpoint or not model or not skill_root: raise ValidationError("http 模式缺少模型端点、模型名或 Skill 目录")
    timeout=float(os.environ.get("PPT_AGENT_MODEL_TIMEOUT","30")); key=os.environ.get("PPT_AGENT_API_KEY","")
    generator=JsonHttpModelGateway(endpoint,model,key,timeout,"generation"); skills=DirectorySkillLoader(skill_root)
    return {"generator":generator,"inspector":JsonHttpModelGateway(endpoint,model,key,timeout,"independent_inspection"),"skills":skills,"builder":ModelHtmlBuilder(generator,skills)}
