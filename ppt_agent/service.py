from __future__ import annotations

import hashlib, inspect, json, logging, re, threading, time
from pathlib import Path
from datetime import datetime, timezone

from .config import ClarificationConfig, FeatureFlagsConfig
from .audit import current_agent_audit_context
from .claim_ledger import assert_claims_bound, audit_claims, audit_html_claims, audit_html_claims_by_slide, build_claim_ledger, validate_claim_ledger
from .content_inspection import inspect_content_quality
from .design_contract import (
    build_presentation_technical_contract,
    default_design_intent,
    scope_presentation_technical_contract,
    validate_design_intent,
    validate_presentation_technical_contract,
    validate_shared_design_assets,
)
from .errors import ConflictError, GatewayError, GatewayUnknownResult, NotFoundError, RuntimeUnavailableError, ValidationError
from .fsm import TaskState, transition
from .gateways import FakeGenerationGateway, FakeHtmlBuilder, FakeInspectionGateway, FakeSkillLoader
from .generation.contracts import HtmlDeckSpec, HtmlSampleSpec, TaskBrief
from .generation.context import ContextTextSource, GenerationContextV2, build_stage_payload, stage_payload_metadata
from .schema import DeliveryManifest, InspectionReport, IssueDisposition
from .p2 import canonical, digest, now, parse_task_card, questions_for, scan_resources, validate_answer
from .schema import ClarificationSet, ResourceManifest, TaskCard, TaskInputSnapshot
from .schema import NarrativeDocument, SlideOutline, SampleSelection, DeckArtifact
from .p3 import assert_narrative_quality, changed_slide_ids, narrative_markdown, narrative_quality_evidence, normalize_outline_markdown, outline_markdown, parse_outline, requested_slide_count, structured_outline_markdown
from .p4 import apply_presentation_technical_contract, assemble_presentation, controlled_assets, infer_scope, materialize_required_claim_slots, recommend, render, required_sample_targets
from .render_gate import TechnicalGate, canonical_post_render_evidence, post_render_evidence_hash
from .offline import localize_delivery_html, offline_assets, offline_performance, offline_player
from .overflow_autofit import GEOMETRIC_CODES, MAX_CASCADE_ROUNDS, fit_deck_html

def utcnow(): return datetime.now(timezone.utc).isoformat()
def fingerprint(value): return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":")).encode()).hexdigest()

def narrative_numeric_policy(ledger):
    quantified={"date","quarter","metric","metric_transition","frequency"}
    return {
        "mode":"claim_ledger_exact_allowlist",
        "allowed_claims":[
            {"claim_id":claim["claim_id"],"kind":claim["kind"],"value":claim["value"]}
            for claim in ledger["claims"] if claim["kind"] in quantified
        ],
        "forbidden_transformations":[
            "不得把总周期拆成新的阶段周数或累计周数",
            "不得把已有百分比拆分、重配或补造成新的阶段占比",
            "不得用假设、建议、示例或待确认包装未绑定量化值",
        ],
        "unnumbered_stage_labels":["启动阶段","扩展阶段","稳态阶段","复盘阶段"],
    }

def narrative_structure_policy(task_card):
    return {
        "mode":"minimum_semantic_structure_exact_context",
        "minimum_h1_count":1,
        "minimum_section_count":2,
        "minimum_body_characters":60,
        "required_context":[
            {"field":field,"value":task_card[field]}
            for field in ("topic","goal","audience")
            if isinstance(task_card.get(field),str) and len(re.sub(r"\s+","",task_card[field]))>=2
        ],
        "rule":"输出完整 Markdown 叙事，不得返回分析请求、待办或元说明；将 required_context 每项 value 逐字写入正文，不得缩写、改写或省略，并用至少两个有实质正文的二级章节表达核心论点与页面推进逻辑。",
    }

def required_context_markdown(required_context):
    """Build the server-owned verbatim context block used on correction.

    The provider still writes the narrative.  The service owns this small
    identity block so a long topic cannot be shortened by the second model
    turn before the artifact is validated and committed.
    """
    if not required_context: return ""
    labels={"topic":"冻结主题","goal":"冻结目标","audience":"冻结受众"}
    lines=["## 冻结任务上下文（逐字）"]
    for item in required_context:
        lines.extend((f"**{labels.get(item['field'],item['field'])}**",item["value"],""))
    return "\n".join(lines).rstrip()

def materialize_required_context(markdown,required_context):
    block=required_context_markdown(required_context)
    if not block: return markdown
    return f"{markdown.rstrip()}\n\n{block}\n"

GENERATION_ATTEMPTS=2

INSPECTION_SOURCES=frozenset({"semantic_model","semantic_deterministic","technical_browser"})
INSPECTION_SOURCE_PRIORITY={"semantic_model":0,"semantic_deterministic":1,"technical_browser":2}

def inspection_hard_gate_passed(issues):
    """Only TechnicalGate-classified browser failures may fail inspection."""
    return not any(TechnicalGate.is_inspection_blocker(item) for item in issues)

def inspection_semantic_identity(issue):
    """Return the server-owned identity for a finding, independent of source IDs."""
    level=issue.get("level") or ("element" if issue.get("element_id") else "slide" if issue.get("slide_id") else "deck")
    if level not in {"element","slide","deck"}: level="deck"
    slide_id="" if level=="deck" else str(issue.get("slide_id") or "").strip()
    element_id=str(issue.get("element_id") or "").strip() if level=="element" else ""
    return {
        "code":re.sub(r"\s+"," ",str(issue.get("code") or "quality_issue").strip()).casefold(),
        "level":level,
        "slide_id":slide_id,
        "element_id":element_id,
    }

def inspection_semantic_issue_id(issue):
    return f"inspection-{digest(canonical(inspection_semantic_identity(issue)))[:24]}"

def inspection_source_issue_id(source,issue):
    value=str(issue.get("issue_id") or "").strip()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}",value): return value
    return f"{source}-{digest(canonical(issue))[:24]}"

def merge_inspection_source_issues(source_items):
    """Merge source findings by server semantics while retaining raw identities."""
    combined=[]; by_identity={}
    for source,items in source_items:
        if source not in INSPECTION_SOURCES or not isinstance(items,list):
            raise ValidationError("检查报告来源或 issues 无效")
        for original in items:
            if not isinstance(original,dict): raise ValidationError("检查报告 issue 必须是对象")
            identity=inspection_semantic_identity(original)
            key=canonical(identity)
            raw_id=inspection_source_issue_id(source,original)
            candidate={
                **original,
                "issue_id":inspection_semantic_issue_id(original),
                "severity":TechnicalGate.normalize_inspection_severity({**original,"source":source,"sources":[source]}),
                "level":identity["level"],
                "code":original.get("code","quality_issue"),
                "message":original.get("message","发现质量问题"),
                "slide_id":identity["slide_id"],
                "element_id":identity["element_id"],
                "evidence":original.get("evidence",original.get("message","发现质量问题")),
                "suggestion":original.get("suggestion","请人工检查并修复"),
                "source":source,
                "sources":[source],
                "source_issue_ids":[{"source":source,"issue_id":raw_id}],
            }
            existing=by_identity.get(key)
            if existing is None:
                by_identity[key]=candidate; combined.append(candidate); continue
            origin={"source":source,"issue_id":raw_id}
            if origin not in existing["source_issue_ids"]: existing["source_issue_ids"].append(origin)
            existing["sources"]=list(dict.fromkeys([*existing["sources"],source]))
            existing["severity"]=TechnicalGate.normalize_inspection_severity(existing)
            if INSPECTION_SOURCE_PRIORITY[source]>=INSPECTION_SOURCE_PRIORITY[existing["source"]]:
                for field in ("code","message","evidence","suggestion"):
                    existing[field]=candidate[field]
                existing["source"]=source
    return combined

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
    def __init__(self,store,generator=None,inspector=None,skills=None,builder=None,clarifier=None,clarification_config=None,settings_store=None,browser_inspector=None,feature_flags=None,generation_pipeline=None):
        self.store=store; self.generator=generator or FakeGenerationGateway(); self.inspector=inspector or FakeInspectionGateway(); self.skills=skills or FakeSkillLoader(); self.builder=builder or FakeHtmlBuilder(); self.clarifier=clarifier
        self.generation_pipeline=generation_pipeline
        self._generation_core_health={"ready":True,"status":"not_required"} if generation_pipeline is None else {"ready":False,"status":"not_checked"}
        if feature_flags is None:
            self._feature_flags=FeatureFlagsConfig()
        elif isinstance(feature_flags,FeatureFlagsConfig):
            self._feature_flags=feature_flags
        elif isinstance(feature_flags,dict) and set(feature_flags)=={"skill_runtime_v2","technical_gate_v2"} and all(isinstance(value,bool) for value in feature_flags.values()):
            self._feature_flags=FeatureFlagsConfig(**feature_flags)
        else:
            raise ValidationError("feature_flags 无效")
        self.technical_gate=TechnicalGate()
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
        self._runtime_capabilities={"checked":False,"ready":True,"status":"on_demand","models":[]}
        self._clarification_capabilities={"checked":False,"ready":True,"status":"on_demand" if self.clarifier is not None else "not_required","models":[]}
        self._runtime_guard=threading.RLock()
        for gateway in {id(x):x for x in (self.generator,self.inspector,self.builder,self.clarifier) if x is not None}.values():
            if hasattr(gateway,"set_audit_sink"): gateway.set_audit_sink(self.store.append_agent_audit)
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
    def initialize_generation_core(self):
        if self.generation_pipeline is None:
            self._generation_core_health={"ready":True,"status":"not_required"}
        else:
            result=self.generation_pipeline.preflight()
            self._generation_core_health={**result,"status":"ready" if result.get("ready") else "unavailable"}
        return self.generation_core_health()
    def generation_core_health(self):
        return json.loads(json.dumps(self._generation_core_health))
    @staticmethod
    def _clarification_transcript(clarification):
        """Normalize persisted rounds without dropping question wording or raw answers."""
        rounds=[]
        history=list(clarification.get("rounds_history",[]))
        current_round=clarification.get("round",1)
        current_questions=list(clarification.get("details",clarification.get("questions",[])) or [])
        current_answers=dict(clarification.get("answers",{}) or {})
        if current_questions and current_answers and not any(item.get("round")==current_round for item in history if isinstance(item,dict)):
            history.append({
                "round":current_round,
                "questions":current_questions,
                "answers":current_answers,
                "raw_answers":dict(clarification.get("raw_answers",{}) or {}),
            })
        for item in history:
            if not isinstance(item,dict): continue
            answers=dict(item.get("answers",{}) or {})
            raw_answers=dict(item.get("raw_answers",{}) or {})
            exchanges=[]
            for question in item.get("questions",[]) or []:
                if not isinstance(question,dict): continue
                question_id=question.get("question_id")
                if not isinstance(question_id,str) or question_id not in answers: continue
                exchanges.append({
                    "question_id":question_id,
                    "field_path":question.get("field_path",question.get("field","")),
                    "prompt":question.get("prompt",""),
                    "helper_text":question.get("helper_text",""),
                    "options":question.get("options",[]),
                    "allow_other":question.get("allow_other",False),
                    "blocking":question.get("blocking",False),
                    "raw_answer":raw_answers.get(question_id,answers[question_id]),
                    "adopted_value":answers[question_id],
                })
            if exchanges: rounds.append({"round":item.get("round",len(rounds)+1),"exchanges":exchanges})
        return rounds
    @staticmethod
    def _generation_source_texts(view):
        materials=[]; seen=set()
        def add(source_ref,value):
            if not isinstance(value,str) or not value.strip(): return
            key=(source_ref,value)
            if key in seen: return
            seen.add(key); materials.append(ContextTextSource.create(f"source-material-{len(materials)+1:03d}",source_ref,value))
        def walk(prefix,value):
            if isinstance(value,str): add(prefix,value)
            elif isinstance(value,dict):
                for key,item in value.items(): walk(f"{prefix}.{key}",item)
            elif isinstance(value,(list,tuple)):
                for index,item in enumerate(value): walk(f"{prefix}[{index}]",item)
        card=view.get("task_card",{})
        for key,value in card.items():
            if key not in {"goal","audience","topic","source_format","format_detection","missing","assumptions","defaults"}:
                walk(f"normalized_task_card.{key}",value)
        for index,item in enumerate(view.get("manifest",{}).get("resources",[])):
            add(f"resource_manifest[{index}].description",item.get("description"))
        return tuple(materials)
    def _generation_context(self,task_id,view=None):
        view=view or self.input_view(task_id)
        if not view.get("snapshot"): raise ConflictError("须先冻结输入再建立 GenerationContextV2")
        snapshot_record=next(item for item in self.versions(task_id,"input-snapshot") if item["hash"]==view["snapshot_hash"])
        raw_source_hash=snapshot_record["metadata"]["raw_source_hash"]
        original_content=self.version(task_id,raw_source_hash).decode("utf-8")
        original=ContextTextSource.create("original-prompt","input-source",original_content,"application/json; charset=utf-8" if view.get("source_format")=="json" else "text/markdown; charset=utf-8")
        clarification_hash=view.get("clarification_hash") or snapshot_record["metadata"]["clarification_hash"]
        card=view["task_card"]
        constraints=card.get("constraints") if isinstance(card.get("constraints"),dict) else {}
        count=requested_slide_count(card) or self.get(task_id).get("target_slide_count") or 6
        confirmed_constraints={
            "goal":card.get("goal"),"audience":card.get("audience"),"topic":card.get("topic"),
            "slide_count":count,"language":constraints.get("language","zh-CN"),
            "style_preferences":constraints.get("style_preferences",{}),"constraints":constraints,
        }
        resources=[]
        for item in view.get("manifest",{}).get("resources",[]):
            resources.append({key:item[key] for key in ("resource_id","uri","media_type","content_hash")})
        context=GenerationContextV2.create(
            original_prompt=original,
            clarification_transcript=self._clarification_transcript(view.get("clarification",{})),
            normalized_task_card=card,
            source_texts=self._generation_source_texts(view),
            resource_manifest=resources,
            confirmed_constraints=confirmed_constraints,
            lineage={
                "input_snapshot_hash":view["snapshot_hash"],
                "original_input_hash":raw_source_hash,
                "clarification_hash":clarification_hash,
                "task_card_hash":digest(canonical(card)),
                "resource_manifest_hash":view["snapshot"]["resource_manifest_hash"],
            },
        )
        context_hash=self.store.put_version(task_id,"generation-context",canonical(context.to_dict()),{
            "context_snapshot_id":context.context_snapshot_id,
            "input_snapshot_hash":view["snapshot_hash"],
            "clarification_hash":clarification_hash,
            "immutable":True,
        })
        if context_hash!=context.context_hash: raise ConflictError("GenerationContextV2 内容寻址持久化失败")
        return context
    def generation_context_view(self,task_id):
        context=self._generation_context(task_id)
        return {**context.to_dict(),"context_snapshot_hash":context.context_hash}
    def _generation_core_brief(self,task_id,view=None,context=None):
        """Adapt the frozen task input to the model-owned content boundary."""
        if self.generation_pipeline is None: raise ConflictError("生成内核未配置")
        view=view or self._p3_input(task_id)
        context=context or self._generation_context(task_id,view)
        card=view["task_card"]
        count=requested_slide_count(card) or self.get(task_id).get("target_slide_count") or 6
        resources=[]
        for item in view.get("manifest",{}).get("resources",[]):
            uri=item["uri"]
            if uri.startswith("resources://"):
                uri=f"{task_id}/resources/{uri[len('resources://') :]}"
            resources.append({key:item[key] for key in ("resource_id","media_type","content_hash")} | {"uri":uri})
        ledger=view.get("claim_ledger") or self._ensure_claim_ledger(task_id,view)
        transcript_text=json.dumps(context.to_dict()["clarification_transcript"],ensure_ascii=False,sort_keys=True,separators=(",",":"))
        card_text=json.dumps(context.to_dict()["normalized_task_card"],ensure_ascii=False,sort_keys=True,separators=(",",":"))
        text_resources=[
            {"resource_id":"original-prompt","source_ref":context.original_prompt.source_ref,"media_type":context.original_prompt.media_type,"content_hash":context.original_prompt.content_hash,"content":context.original_prompt.content},
            {"resource_id":"clarification-transcript","source_ref":"generation_context.clarification_transcript","media_type":"application/json; charset=utf-8","content_hash":digest(transcript_text.encode("utf-8")),"content":transcript_text},
            {"resource_id":"confirmed-task-card","source_ref":"generation_context.normalized_task_card","media_type":"application/json; charset=utf-8","content_hash":digest(card_text.encode("utf-8")),"content":card_text},
        ]
        for index,item in enumerate(context.source_texts[:29],1):
            text_resources.append({"resource_id":f"source-material-{index:03d}","source_ref":item.source_ref,"media_type":item.media_type,"content_hash":item.content_hash,"content":item.content})
        type_map={
            "date":"date","quarter":"date","ordinal":"ordinal","metric":"count",
            "metric_transition":"metric","frequency":"frequency",
            "derived_temporal_range":"temporal_range",
        }
        facts=[]
        for claim in ledger.get("claims",[]):
            value=str(claim["value"])
            evidence_resource=next((item for item in text_resources if value in item["content"]),text_resources[2])
            source_text=evidence_resource["content"]
            text_resource_id=evidence_resource["resource_id"]
            start=source_text.find(value)
            span=None
            statement=value
            if start>=0:
                end=start+len(value)
                left=max(source_text.rfind(marker,0,start) for marker in ("。","；",";","\n"))+1
                right_candidates=[source_text.find(marker,end) for marker in ("。","；",";","\n")]
                right=min((item for item in right_candidates if item>=0),default=len(source_text))
                left=max(left,start-1_500)
                right=min(right,end+1_500)
                statement=source_text[left:right+1].strip() or value
                span={"start":start,"end":end}
            facts.append({
                "fact_id":f"fact-{claim['claim_id'].removeprefix('claim-')}",
                "text":statement,
                "source_refs":[text_resource_id],
                "fact_type":type_map.get(claim.get("kind"),"statement"),
                "statement":statement,
                "source_ref":evidence_resource["source_ref"],
                "source_span":span,
                "subject":str(card.get("topic") or ""),
                "predicate":"confirmed_source_statement",
                "value":value,
            })
        constraints=card.get("constraints") if isinstance(card.get("constraints"),dict) else {}
        style_preferences=constraints.get("style_preferences")
        if not isinstance(style_preferences,dict):
            style_preferences={"description":str(style_preferences)} if style_preferences else {}
        return TaskBrief.parse({
            "schema_version":"1.0","goal":card["goal"],"audience":card["audience"],"topic":card["topic"],
            "slide_count":count,"language":str(constraints.get("language") or "zh-CN"),
            "style_preferences":style_preferences,"resource_manifest":resources,"confirmed_facts":facts,
            "text_resources":text_resources,
        })
    @staticmethod
    def _generation_core_evidence(result):
        evidence={
            "checkpoint_id":result.checkpoint.checkpoint_id,
            "output_sha256":result.checkpoint.output_sha256,
            "contract_name":result.checkpoint.contract_name,
            "model":result.checkpoint.model,
            "provider_calls":result.checkpoint.metadata.get("provider_calls",0),
            "recovery_count":result.checkpoint.metadata.get("recovery_count",0),
            "pipeline_version":result.checkpoint.metadata.get("pipeline_version"),
            "reused":result.reused,
        }
        for key in ("generation_mode","skill_digest","skill_entry_read","applied_skill_file_hashes","provider_input_sha256","schema_correction_count","validator_version","evidence_version","context_snapshot_id","context_snapshot_hash","stage_payload_hash","context_sections_read","operation","artifact_kind","modification_scope","change_type","requested_slide_ids","modified_slide_ids","preserved_slide_ids","design_system_changed","page_contract_hashes"):
            if key in result.checkpoint.metadata: evidence[key]=result.checkpoint.metadata[key]
        return evidence
    def _version_metadata(self,task_id,kind,artifact_hash):
        return next((item["metadata"] for item in self.versions(task_id,kind) if item["hash"]==artifact_hash),{})
    @staticmethod
    def _narrative_markdown_from_core(value,brief):
        beats="\n".join(f"{index}. **{beat.purpose}**：{beat.message}" for index,beat in enumerate(value.story_arc,1))
        return (f"# {value.thesis}\n\n## 受众结论\n{value.audience_takeaway}。本叙事围绕{brief.topic}组织已确认内容，"
                f"帮助{brief.audience}理解关键判断、形成共同认知，并据此推进{brief.goal}。\n\n"
                f"## 冻结任务上下文\n主题：{brief.topic}\n\n目标：{brief.goal}\n\n受众：{brief.audience}\n\n"
                f"## 叙事路径\n{beats}\n\n## 表达语气\n{value.tone}\n")
    def _generate_narrative_with_core(self,task_id,view,state,scope):
        context=self._generation_context(task_id,view)
        brief=self._generation_core_brief(task_id,view,context)
        ledger=view["claim_ledger"]; ledger_value={key:value for key,value in ledger.items() if key!="hash"}
        def validate_candidate(candidate):
            candidate_text=self._narrative_markdown_from_core(candidate,brief)
            claim_result=assert_claims_bound(candidate_text,ledger_value,"叙事",allow_disclosed_assumptions=False)
            quality_result=assert_narrative_quality(candidate_text,view["task_card"])
            return {"claim_bindings":claim_result["bindings"],"narrative_quality":quality_result}
        result=self.generation_pipeline.generate_narrative(
            task_id,
            brief,
            input_version=view["snapshot_hash"],
            candidate_validator=validate_candidate,
            context=context,
        )
        value=result.value
        text=self._narrative_markdown_from_core(value,brief)
        claim_bindings=assert_claims_bound(text,ledger_value,"叙事")
        quality_evidence=assert_narrative_quality(text,view["task_card"])
        prior=self._current_version(task_id,"narrative"); version=len(self.versions(task_id,"narrative"))+1; content_hash=digest(text.encode())
        model=NarrativeDocument.parse({"document_id":f"narrative-{content_hash[:16]}","task_id":task_id,"version":version,"markdown":text,"content_hash":content_hash,"created_at":now(),"schema_version":"1.0"})
        metadata={"parent":prior,"action":"generate" if not prior else "regenerate","scope":scope,"summary":"生成整稿叙事结构","model":result.checkpoint.model,"input_snapshot_hash":view["snapshot_hash"],"claim_ledger_hash":ledger["hash"],"claim_bindings":claim_bindings["bindings"],"unbound_claim_count":0,"narrative_quality":quality_evidence,"generation_core":self._generation_core_evidence(result)}
        h=self._record_p3(task_id,"narrative",model,metadata,"narrative_generate")
        if state.stage in {state.stage.CLARIFICATION,state.stage.CREATED}: self.command(task_id,f"narrative-stage-{h[:12]}","advance")
        else: self._reset_narrative_gate(task_id,h)
        return self.planning_view(task_id)
    def _outline_markdown_from_core(self,outline,brief):
        facts={fact.fact_id:fact.text for fact in brief.confirmed_facts}; used=set(); blocks=["# 逐页大纲",""]
        for slide in outline.slides:
            blocks.extend((f"## [{slide.slide_id}] {slide.title}",f"- 页面角色：{slide.role}",f"- 页面目的：{slide.message}",f"- 视觉意图：{slide.visual_intent}"))
            for fact_id in slide.evidence_refs:
                if fact_id in facts:
                    blocks.append(f"- 确认事实：{facts[fact_id]}"); used.add(fact_id)
                else:
                    blocks.append(f"- 证据资源：{fact_id}")
            blocks.append("")
        remaining=[text for fact_id,text in facts.items() if fact_id not in used]
        if remaining:
            insert_at=blocks.index("",2)+1 if len(blocks)>2 else len(blocks)
            blocks[insert_at:insert_at]=[f"- 确认事实：{text}" for text in remaining]
        return "\n".join(blocks).rstrip()+"\n"
    @staticmethod
    def _design_intent_from_core(theme,layouts):
        return validate_design_intent({
            "style_summary":"确定性契约驱动的演示设计",
            "color_strategy":f"背景 {theme.background}、正文 {theme.text}、主色 {theme.primary}、强调色 {theme.accent}",
            "typography_strategy":f"标题使用 {theme.font_heading}，正文使用 {theme.font_body}",
            "layout_principles":["固定 1280×720 画布与安全边距",f"仅使用已确认版式族：{', '.join(sorted(layouts))}"],
            "rationale":"样品确认后冻结主题、字体、配色与版式族并复用于全稿",
        })
    @staticmethod
    def _generation_core_preview_html(artifact,brief,controlled):
        """Adapt verified offline asset paths to the existing inert preview boundary."""
        source=artifact.html
        suffixes={"image/png":".png","image/jpeg":".jpg","image/webp":".webp","image/gif":".gif"}
        for record in brief.resource_manifest:
            marker="/resources/"
            if marker not in record.uri: continue
            legacy_uri="resources://"+record.uri.split(marker,1)[1]
            data_url=controlled.get(legacy_uri)
            suffix=suffixes.get(record.media_type)
            if data_url and suffix:
                source=source.replace(f"assets/{record.content_hash}{suffix}",data_url)
        return source
    def _runtime_gateways(self):
        local_types=(FakeGenerationGateway,FakeInspectionGateway,FakeHtmlBuilder)
        return list({
            id(item):item for item in (self.generator,self.inspector,self.builder,self.clarifier)
            if item is not None and not isinstance(item,local_types)
        }.values())
    def _disabled_release_features(self):
        return [name for name,value in self._feature_flags.public().items() if not value]
    def _require_feature(self,name):
        if getattr(self._feature_flags,name): return
        raise RuntimeUnavailableError(
            f"{name} 未对本实例开放，已停止接收新的写任务",
            runtime_error_code="release_feature_disabled",
            failed_check=name,
        )
    def require_release_write_enabled(self):
        disabled=self._disabled_release_features()
        if disabled: self._require_feature(disabled[0])
    def release_status(self):
        flags=self._feature_flags.public()
        return {
            "flags":flags,
            "write_enabled":all(flags.values()),
            "rollback_mode":"traffic_to_previous_release",
            "legacy_implementation_present":False,
        }
    def initialize_clarification_runtime(self):
        """Return the on-demand execution policy without contacting a model."""
        return self.clarification_runtime_health()
    def clarification_runtime_health(self):
        with self._runtime_guard:
            return json.loads(json.dumps(self._clarification_capabilities))
    def initialize_runtime(self):
        """Return the on-demand execution policy without contacting a model."""
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
            "release":self.release_status(),
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
        self.require_release_write_enabled()
    def require_clarification_runtime_ready(self):
        self.require_release_write_enabled()
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
    def get(self,task_id):
        state=self.store.checkpoint(task_id)
        # ``blockers_resolved`` is a compatibility field, not an independent
        # mutable truth.  Once inspection exists it is projected exclusively
        # from the current deck/report/disposition lineage, so every API view
        # agrees even after a deck edit makes the prior report stale.
        projection=self._inspection_projection(task_id)
        if projection["has_reports"]:
            state={**state,"blockers_resolved":projection["blockers_resolved"]}
        return state
    def _require_actionable(self,task_id):
        status=self.get(task_id)["status"]
        if status in {"paused","cancelled","failed","completed"}: raise ConflictError(f"任务状态 {status} 不允许启动新动作")
    def _require_candidate_mutable(self,task_id):
        self._require_actionable(task_id)
        state=TaskState.parse(self.get(task_id))
        if state.stage not in {state.stage.DECK,state.stage.REVIEW}:
            raise ConflictError("当前阶段的候选全稿不可修改；如需调整，请从已交付版本显式派生新候选")
        return state

    def _inspection_projection(self,task_id):
        deck_hash=self._current_version(task_id,"deck")
        reports=self.versions(task_id,"inspection")
        current=None
        if reports:
            by_hash={item["hash"]:item for item in reports}
            current_hash=next((
                event.get("result",{}).get("report_hash") for event in reversed(self.events(task_id))
                if event.get("action")=="inspection_complete" and event.get("result",{}).get("report_hash") in by_hash
            ),reports[-1]["hash"])
            record=by_hash[current_hash]
            try:
                value=json.loads(self.version(task_id,current_hash))
                if not isinstance(value,dict): raise ValueError("inspection report is not an object")
                report_deck_hash=value.get("deck_hash")
                current={**value,"hash":current_hash,"metadata":record["metadata"],"stale":not deck_hash or report_deck_hash!=deck_hash}
            except (NotFoundError,json.JSONDecodeError,UnicodeDecodeError,ValueError):
                # Keep a fail-closed placeholder instead of treating a missing
                # current report as "unchecked", which would allow finalize.
                report_deck_hash=record["metadata"].get("deck_hash")
                current={"hash":current_hash,"metadata":record["metadata"],"deck_hash":report_deck_hash,"issues":[],"passed":False,"stale":not deck_hash or report_deck_hash!=deck_hash,"integrity_error":"检查报告工件缺失或无效"}
        dispositions=[]
        for record in self.versions(task_id,"issue-disposition"):
            value=json.loads(self.version(task_id,record["hash"]))
            dispositions.append({**value,"hash":record["hash"],"metadata":record["metadata"],"stale":not deck_hash or value["target_deck_hash"]!=deck_hash})
        active={}
        for disposition in sorted((item for item in dispositions if not item["stale"]),key=lambda item:(item["metadata"].get("sequence",0),item["created_at"],item["hash"])):
            active[disposition["issue_id"]]=disposition
        unresolved=[] if not current or current["stale"] else [
            issue for issue in current["issues"]
            if active.get(issue["issue_id"],{}).get("action") not in {"resolve","manual","waive"}
        ]
        blockers=[issue for issue in unresolved if issue["severity"]=="blocker"]
        integrity_valid=bool(current and not current["stale"] and self._assert_inspection_evidence(task_id,current,fail_closed=False)["valid"])
        return {
            "has_reports":bool(reports),"report":current,"reports":reports,
            "dispositions":dispositions,"unresolved":unresolved,"blocking_issues":blockers,
            "blockers_resolved":bool(integrity_valid and not blockers),
        }
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
            return {"hash":value,"version":artifact["version"],"outline_hash":artifact["outline_hash"],"source":meta.get("source","unknown"),"operator":meta.get("operator","system"),"summary":meta.get("summary",""),"html":meta["html"],"fragments":self._slide_fragments(meta["html"]),"contract_hashes":meta.get("page_contract_hashes",{})}
        lhs,rhs=describe(left),describe(right); page_ids=list(dict.fromkeys([*lhs["fragments"],*rhs["fragments"]])); pages=[]
        for slide_id in page_ids:
            lhtml=lhs["fragments"].get(slide_id); rhtml=rhs["fragments"].get(slide_id)
            contract_changed=(slide_id in lhs["contract_hashes"] and slide_id in rhs["contract_hashes"] and lhs["contract_hashes"][slide_id]!=rhs["contract_hashes"][slide_id])
            status="added" if lhtml is None else "removed" if rhtml is None else "unchanged" if lhtml==rhtml and not contract_changed else "modified"
            pages.append({"slide_id":slide_id,"status":status,"left_html":lhtml,"right_html":rhtml,"left_contract_hash":lhs["contract_hashes"].get(slide_id),"right_contract_hash":rhs["contract_hashes"].get(slide_id)})
        for side in (lhs,rhs): side.pop("fragments"); side.pop("contract_hashes")
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
            clarification_meta={"questions":questions,"details":questions,"answers":{},"raw_answers":{},"invalidated":[],"status":"generating" if self.clarifier is not None else "ready","question_source":None if self.clarifier is not None else "fallback","question_model":None,"diagnostic_id":diagnostic_id,"question_schema_version":"1.0","input_hash":raw_source_hash,"round":1,"rounds_history":[],"max_rounds":clarification_config.max_rounds,"max_questions_per_round":clarification_config.max_questions_per_round,"style":clarification_config.style}
            clarification_hash=self.store.put_version(task_id,"clarification",canonical(clarification.to_dict()),clarification_meta)
            snapshot=TaskInputSnapshot.parse({"snapshot_id":f"snapshot-{digest((raw_source_hash+card_hash+manifest_hash).encode())[:16]}","task_id":task_id,"task_card_hash":card_hash,"resource_manifest_hash":manifest_hash,"created_at":now(),"schema_version":"1.0"})
            snapshot_hash=self.store.put_version(task_id,"input-snapshot",canonical(snapshot.to_dict()),{"clarification_hash":clarification_hash,"raw_source_hash":raw_source_hash,"rebuild_of":existing[-1]["hash"] if existing else None})
            waiting=self.clarifier is not None or bool(questions)
            new=TaskState.parse(state.to_dict()); new=TaskState(**{**new.__dict__,"stage":new.stage.CLARIFICATION,"status":new.status.WAITING_FOR_USER if waiting else new.status.READY,"waiting_reason":"clarification_generating" if self.clarifier is not None else "missing_required_input" if questions else None,"required_action":"wait_for_clarification" if self.clarifier is not None else "answer_clarifications" if questions else None,"revision":new.revision+1})
            event={"event_id":hashlib.sha256(f"{task_id}:input:{snapshot_hash}".encode()).hexdigest()[:24],"command_id":f"input-{snapshot_hash[:16]}","action":"rebuild_input" if existing else "import_input","actor":"user","request_hash":snapshot_hash,"at":utcnow(),"from":state.to_dict(),"to":new.to_dict(),"result":{"snapshot_hash":snapshot_hash}}
            self.store.commit(task_id,new.to_dict(),event)
            result={"state":new.to_dict(),"snapshot":snapshot.to_dict(),"snapshot_hash":snapshot_hash,"task_card":card,"manifest":{**manifest.to_dict(),"resources":resources,"warnings":warnings},"clarification":{**clarification.to_dict(),"details":questions,**clarification_meta},"clarification_hash":clarification_hash}
            if not waiting: self._generation_context(task_id,self.input_view(task_id))
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
            if event["action"] not in {"answer_clarification","clarification_generate","clarification_failed","clarification_wait_runtime","clarification_fallback"}: continue
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
        return {"state":self.get(task_id),"snapshot":snapshot,"snapshot_hash":item["hash"],"source":source,"source_format":source_format,"task_card":task_card,"manifest":{**json.loads(self.version(task_id,snapshot["resource_manifest_hash"])),**manifest["metadata"]},"clarification":clarification,"clarification_hash":ch}
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
        if result["confirmed"]: self._generation_context(task_id,self.input_view(task_id))
        if result["confirmed"] and self.get(task_id).get("mode") in {"auto","quick"}:
            self._drive_auto_to_sample(task_id)
        return result

    def recover_clarification_failure(self,task_id,error):
        """Normalize deadline/persistence/provider failures to one retry state."""
        view=self.input_view(task_id)
        if (view.get("state",{}).get("waiting_reason") in {"clarification_failed","waiting_for_runtime"}
            and view.get("clarification",{}).get("status") in {"failed","waiting_for_runtime"}):
            return view["clarification"]
        if isinstance(error,RuntimeUnavailableError):
            public=error.public()["error"]
        else:
            code = "clarification_infrastructure_failure"
            if error.__class__.__name__ == "ExecutionDeadlineExceeded": code = "stage_deadline_exceeded"
            elif isinstance(error,OSError): code = "job_persistence_error"
            public={"code":code,"message":"澄清服务暂时不可用，请重试或使用兜底问题"}
        return self._record_clarification(task_id,view,[],"failed",None,public,"clarification_failed")
    def use_fallback_clarification(self,task_id):
        view=self.input_view(task_id)
        return self._record_clarification(task_id,view,questions_for(view["task_card"]),"ready",None,None,"clarification_fallback",source="fallback")
    def fail_clarification_for_runtime(self,task_id,error):
        view=self.input_view(task_id)
        public=error.public()["error"] if hasattr(error,"public") else {"code":"runtime_unavailable","message":"模型运行时尚未就绪","diagnostic_id":hashlib.sha256(f"{task_id}:runtime".encode()).hexdigest()[:24]}
        return self._record_clarification(task_id,view,[],"failed",None,public,"clarification_failed")
    def wait_clarification_for_runtime(self,task_id,error):
        """Keep the frozen input resumable when no model request was admitted.

        This state is deliberately different from a failed/unknown provider
        request: no model boundary was crossed, so recovery may safely create a
        fresh clarification Job after readiness succeeds.
        """
        view=self.input_view(task_id)
        public=error.public()["error"] if hasattr(error,"public") else {"code":"runtime_unavailable","message":"模型运行时尚未就绪","diagnostic_id":hashlib.sha256(f"{task_id}:runtime".encode()).hexdigest()[:24]}
        return self._record_clarification(task_id,view,[],"waiting_for_runtime",None,public,"clarification_wait_runtime")
    def _validate_model_questions(self,questions,card,*,max_questions=5,asked_field_paths=()):
        """校验模型问题，返回 (有效问题列表, 被过滤的重复字段数)。

        跨轮撞库（询问任务卡已知字段或历轮已问字段）采用过滤而非拒绝：
        剔除撞库问题，剩余问题照常展示；全部被剔除时返回空列表，由上层
        视为"模型没有更多问题"提前确认，流程不被打断。
        """
        if not isinstance(questions,list) or len(questions)>max_questions: raise ValidationError(f"澄清模型 questions 必须为 0 到 {max_questions} 项")
        required={"question_id","field_path","prompt","helper_text","options","allow_other","blocking"}; optional={"recommended"}; seen_ids=set(); seen_paths=set(); known={k for k in ("goal","audience","topic") if k not in card.get("missing",[])}; asked={path for path in asked_field_paths if isinstance(path,str)}; result=[]; filtered=0
        for q in questions:
            if not isinstance(q,dict) or not required.issubset(q) or set(q)-required-optional: raise ValidationError("澄清问题 Schema 无效")
            if q["question_id"] in seen_ids or q["field_path"] in seen_paths: raise ValidationError("澄清问题存在重复 ID 或字段")
            if q["field_path"] in known or q["field_path"] in asked:
                filtered+=1; continue
            if not all(isinstance(q[k],str) and q[k].strip() for k in ("question_id","field_path","prompt","helper_text")) or not isinstance(q["allow_other"],bool) or not isinstance(q["blocking"],bool): raise ValidationError("澄清问题字段无效")
            if not isinstance(q["options"],list) or any(not isinstance(o,dict) or set(o)!={"value","label","description"} or not all(isinstance(o[k],str) for k in o) or not o["value"].strip() or not o["label"].strip() for o in q["options"]): raise ValidationError("澄清选项 Schema 无效")
            if len({o["value"] for o in q["options"]})!=len(q["options"]): raise ValidationError("澄清选项重复")
            recommended=q.get("recommended") or (q["options"][0]["value"] if q["options"] else "")
            if recommended and recommended not in {item["value"] for item in q["options"]}: raise ValidationError("澄清推荐项不在选项中")
            seen_ids.add(q["question_id"]); seen_paths.add(q["field_path"]); result.append({**q,"recommended":recommended,"field":q["field_path"]})
        return result, filtered
    def _record_clarification(self,task_id,view,questions,status,model,error,action,source="model",extra=None):
        config=self._task_clarification_config(task_id)
        meta={"questions":questions,"details":questions,"answers":{},"raw_answers":{},"invalidated":[],"status":status,"question_source":source if status=="ready" else None,"question_model":model,"diagnostic_id":view["clarification"]["diagnostic_id"],"question_schema_version":"1.0","input_hash":view["clarification"]["input_hash"],"normalized_task_card":view["task_card"],"round":config.max_rounds if source=="fallback" else view["clarification"].get("round",1),"rounds_history":list(view["clarification"].get("rounds_history",[])),"max_rounds":config.max_rounds,"max_questions_per_round":config.max_questions_per_round,"style":config.style}
        if error: meta["error"]=error
        if extra: meta.update(extra)
        artifact=ClarificationSet.parse({"clarification_id":f"clarification-{digest(canonical(meta))[:16]}","task_id":task_id,"questions":tuple(q["prompt"] for q in questions),"assumptions":tuple(),"confirmed":status=="ready" and not questions,"schema_version":"1.0"}); ch=self.store.put_version(task_id,"clarification",canonical(artifact.to_dict()),meta)
        state=TaskState.parse(self.get(task_id)); failed=status=="failed"; waiting_runtime=status=="waiting_for_runtime"
        new=TaskState(**{
            **state.__dict__,
            "status":state.status.WAITING_FOR_USER if failed or waiting_runtime or questions else state.status.READY,
            "waiting_reason":"clarification_failed" if failed else "waiting_for_runtime" if waiting_runtime else "missing_required_input" if questions else None,
            "required_action":"retry_clarification" if failed else "continue_clarification" if waiting_runtime else "answer_clarifications" if questions else None,
            "revision":state.revision+1,
        })
        event={"event_id":hashlib.sha256(f"{task_id}:{action}:{ch}".encode()).hexdigest()[:24],"command_id":f"{action}-{ch[:16]}","action":action,"actor":"system" if action!="clarification_fallback" else "user","request_hash":meta["input_hash"],"at":utcnow(),"from":state.to_dict(),"to":new.to_dict(),"result":{"clarification_hash":ch,"snapshot_hash":view["snapshot_hash"],"input_hash":meta["input_hash"]}}
        self.store.commit(task_id,new.to_dict(),event); return {"clarification_hash":ch,**meta,"confirmed":artifact.confirmed}
    def _freeze_task_card(self,task_id,merged,clarification_hash):
        raw=canonical(TaskCard.parse({"task_id":task_id,"goal":merged.get("goal","待澄清"),"audience":merged.get("audience","待澄清"),"topic":merged.get("topic","待澄清"),"source_format":merged.get("source_format","json"),"schema_version":"1.0"}).to_dict())
        # 规范卡内容未变化时（回答只涉及 goal/audience/topic 以外的字段）跳过重写：
        # 合入结果已由澄清元数据携带，重写只会与既有版本产生元数据冲突。
        cards=self.versions(task_id,"task-card")
        if cards and self.version(task_id,cards[-1]["hash"])==raw: return
        self.store.put_version(task_id,"task-card",raw,{"normalized":merged,"clarification_hash":clarification_hash})
    @staticmethod
    def _archive_clarification_round(history,round_number,questions,answers,raw_answers):
        archived={
            "round":round_number,
            "questions":json.loads(json.dumps(questions,ensure_ascii=False)),
            "answers":dict(answers),
            "raw_answers":json.loads(json.dumps(raw_answers,ensure_ascii=False)),
        }
        values=[item for item in history if not isinstance(item,dict) or item.get("round")!=round_number]
        values.append(archived)
        return sorted(values,key=lambda item:item.get("round",0) if isinstance(item,dict) else 0)
    def answer_clarification(self,task_id,question_id,answer):
        return self.answer_clarifications(task_id,{question_id:answer})
    def answer_clarifications(self,task_id,submitted,require_complete=False):
        self._require_actionable(task_id)
        if not isinstance(submitted,dict) or not submitted: raise ValidationError("本轮回答不得为空")
        view=self.input_view(task_id); clarification=view.get("clarification")
        if not clarification: raise ConflictError("尚未生成澄清问题")
        if clarification.get("status") != "ready": raise ConflictError("澄清问题尚未生成完成")
        details=list(clarification.get("details",clarification.get("questions",[])) or [])
        history=json.loads(json.dumps(clarification.get("rounds_history",[]) or [],ensure_ascii=False))
        answers=dict(clarification.get("answers",{}) or {})
        raw_answers=json.loads(json.dumps(clarification.get("raw_answers",{}) or {},ensure_ascii=False))
        current_by_id={q["question_id"]:q for q in details}
        history_locations={}
        for entry in history:
            if not isinstance(entry,dict): continue
            entry.setdefault("answers",{}); entry.setdefault("raw_answers",{})
            for question in entry.get("questions",[]) or []:
                if isinstance(question,dict) and isinstance(question.get("question_id"),str):
                    history_locations[question["question_id"]]=(entry,question)
        by_id={question_id:question for question_id,(_,question) in history_locations.items()} | current_by_id
        changed=False
        if require_complete:
            required={q["question_id"] for q in details if q["blocking"]}
            missing=sorted(required-set(submitted))
            if missing: raise ValidationError("必须一次提交本轮全部阻断题："+",".join(missing))
        for question_id,answer in submitted.items():
            if question_id not in by_id: raise ValidationError("澄清问题不存在")
            value=validate_answer(by_id[question_id],answer)
            raw_value=json.loads(json.dumps(answer,ensure_ascii=False))
            if question_id in current_by_id:
                changed |= question_id in answers and (answers[question_id]!=value or raw_answers.get(question_id)!=raw_value)
                answers[question_id]=value
                raw_answers[question_id]=raw_value
            else:
                entry,_=history_locations[question_id]
                changed |= question_id in entry["answers"] and (entry["answers"][question_id]!=value or entry["raw_answers"].get(question_id)!=raw_value)
                entry["answers"][question_id]=value
                entry["raw_answers"][question_id]=raw_value
        pending=[q for q in details if q["blocking"] and q["question_id"] not in answers]
        merged=dict(view["task_card"])
        for entry in history:
            if not isinstance(entry,dict): continue
            for question in entry.get("questions",[]) or []:
                question_id=question.get("question_id") if isinstance(question,dict) else None
                field=question.get("field",question.get("field_path")) if isinstance(question,dict) else None
                if isinstance(field,str) and question_id in entry.get("answers",{}): merged[field]=entry["answers"][question_id]
        merged.update({q.get("field",q.get("field_path")):answers[q["question_id"]] for q in details if q["question_id"] in answers})
        merged["missing"]=[key for key in merged.get("missing",[]) if not merged.get(key)]
        invalidated=["narrative","outline","sample","deck","inspection","delivery"] if changed else clarification.get("invalidated",[])
        payload={"questions":details,"details":details,"answers":answers,"raw_answers":raw_answers,"status":"ready","normalized_task_card":merged,"invalidated":invalidated,"round":clarification.get("round",1),"rounds_history":history,"max_rounds":clarification.get("max_rounds",1),"max_questions_per_round":clarification.get("max_questions_per_round",6),"style":clarification.get("style","minimal"),**{k:clarification.get(k) for k in ("question_source","question_model","diagnostic_id","question_schema_version","input_hash")}}
        if not pending:
            config=self._task_clarification_config(task_id); current_round=clarification.get("round",1)
            if details and self.clarifier is not None and clarification.get("question_source")=="model" and current_round < config.max_rounds:
                # 阻断题已答完且轮次预算未用尽：归档本轮问答，自动生成下一轮。
                current_adopted={q["question_id"]:answers[q["question_id"]] for q in details if q["question_id"] in answers}
                current_raw={q["question_id"]:raw_answers[q["question_id"]] for q in details if q["question_id"] in raw_answers}
                history=self._archive_clarification_round(history,current_round,details,current_adopted,current_raw)
                next_meta={"questions":[],"details":[],"answers":{},"raw_answers":{},"invalidated":payload["invalidated"],"status":"generating","question_source":None,"question_model":None,"diagnostic_id":clarification["diagnostic_id"],"question_schema_version":"1.0","input_hash":clarification["input_hash"],"normalized_task_card":merged,"round":current_round+1,"rounds_history":history,"max_rounds":config.max_rounds,"max_questions_per_round":config.max_questions_per_round,"style":config.style}
                artifact=ClarificationSet.parse({"clarification_id":f"clarification-{digest(canonical(next_meta))[:16]}","task_id":task_id,"questions":tuple(),"assumptions":tuple(),"confirmed":False,"schema_version":"1.0"})
                ch=self.store.put_version(task_id,"clarification",canonical(artifact.to_dict()),next_meta)
                state=TaskState.parse(self.get(task_id)); new=TaskState(**{**state.__dict__,"status":state.status.WAITING_FOR_USER,"waiting_reason":"clarification_generating","required_action":"wait_for_clarification","revision":state.revision+1})
                event={"event_id":hashlib.sha256(f"{task_id}:answer:{ch}".encode()).hexdigest()[:24],"command_id":f"answer-{ch[:16]}","action":"answer_clarification","actor":"user","request_hash":fingerprint(submitted),"at":utcnow(),"from":state.to_dict(),"to":new.to_dict(),"result":{"clarification_hash":ch,"invalidated":payload["invalidated"],"next_round":current_round+1}}
                self.store.commit(task_id,new.to_dict(),event)
                return {"state":self.get(task_id),"clarification_hash":ch,**next_meta,"confirmed":False}
            if details:
                current_adopted={q["question_id"]:answers[q["question_id"]] for q in details if q["question_id"] in answers}
                current_raw={q["question_id"]:raw_answers[q["question_id"]] for q in details if q["question_id"] in raw_answers}
                payload["rounds_history"]=self._archive_clarification_round(history,current_round,details,current_adopted,current_raw)
        model=ClarificationSet.parse({"clarification_id":f"clarification-{digest(canonical(payload))[:16]}","task_id":task_id,"questions":tuple(q["prompt"] for q in details),"assumptions":tuple(),"confirmed":not pending,"schema_version":"1.0"})
        ch=self.store.put_version(task_id,"clarification",canonical(model.to_dict()),payload)
        state=TaskState.parse(self.get(task_id))
        reset={"stage":state.stage.CLARIFICATION,"sample_confirmed":False,"blockers_resolved":False,"delivery_confirmed":False} if changed and not pending else {}
        new=TaskState(**{**state.__dict__,**reset,"status":state.status.WAITING_FOR_USER if pending else state.status.READY,"waiting_reason":"missing_required_input" if pending else None,"required_action":"answer_clarifications" if pending else None,"revision":state.revision+1})
        event={"event_id":hashlib.sha256(f"{task_id}:answer:{ch}".encode()).hexdigest()[:24],"command_id":f"answer-{ch[:16]}","action":"answer_clarification","actor":"user","request_hash":fingerprint(submitted),"at":utcnow(),"from":state.to_dict(),"to":new.to_dict(),"result":{"clarification_hash":ch,"invalidated":payload["invalidated"]}}
        self.store.commit(task_id,new.to_dict(),event)
        if not pending:
            self._freeze_task_card(task_id,merged,ch)
            self._generation_context(task_id,self.input_view(task_id))
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
            if event["action"]=="answer_clarification" and kind in event.get("result",{}).get("invalidated",[]):
                return None
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
        view["claim_ledger"]=self._ensure_claim_ledger(task_id,view)
        return view
    def _ensure_claim_ledger(self,task_id,input_view=None):
        view=input_view or self.input_view(task_id)
        snapshot_hash=view.get("snapshot_hash")
        if not snapshot_hash: raise ConflictError("须先冻结输入再建立 Claim Ledger")
        context=self._generation_context(task_id,view)
        context_hash=context.context_hash
        for record in reversed(self.versions(task_id,"claim-ledger")):
            if record["metadata"].get("generation_context_hash")==context_hash:
                ledger=validate_claim_ledger(json.loads(self.version(task_id,record["hash"])))
                return {**ledger,"hash":record["hash"]}
        ledger=build_claim_ledger(
            task_id=task_id,
            input_snapshot_hash=context_hash,
            source_binding={"source":view.get("source"),"task_card":view.get("task_card")},
            created_at=utcnow(),
        )
        ledger_hash=self.store.put_version(task_id,"claim-ledger",canonical(ledger),{
            "input_snapshot_hash":snapshot_hash,
            "generation_context_hash":context_hash,
            "claim_count":len(ledger["claims"]),
            "immutable":True,
        })
        return {**ledger,"hash":ledger_hash}
    def claim_ledger_view(self,task_id):
        return self._ensure_claim_ledger(task_id)
    def _ensure_design_contract(self,task_id,outline_hash=None):
        outline_hash=outline_hash or self._current_version(task_id,"outline")
        if not outline_hash: raise ConflictError("须先生成逐页大纲再建立 PresentationTechnicalContract")
        view=self._p3_input(task_id)
        records=self.versions(task_id,"presentation-technical-contract") or self.versions(task_id,"design-contract")
        for record in reversed(records):
            if record["metadata"].get("outline_hash")==outline_hash and record["metadata"].get("input_snapshot_hash")==view["snapshot_hash"]:
                contract=validate_presentation_technical_contract(json.loads(self.version(task_id,record["hash"])))
                return {**contract,"hash":record["hash"]}
        outline=json.loads(self.version(task_id,outline_hash))
        contract=build_presentation_technical_contract(
            task_id=task_id,
            input_snapshot_hash=view["snapshot_hash"],
            outline_hash=outline_hash,
            slide_ids=list(outline["slide_ids"]),
            created_at=utcnow(),
        )
        contract_hash=self.store.put_version(task_id,"presentation-technical-contract",canonical(contract),{
            "input_snapshot_hash":view["snapshot_hash"],
            "outline_hash":outline_hash,
            "contract_type":contract["contract_type"],
            "immutable":True,
        })
        return {**contract,"hash":contract_hash}
    def design_contract_view(self,task_id):
        return self._ensure_design_contract(task_id)
    def presentation_technical_contract_view(self,task_id):
        return self._ensure_design_contract(task_id)
    def _generation_contracts(self,task_id,outline_hash=None):
        contract=self._ensure_design_contract(task_id,outline_hash)
        ledger=self._ensure_claim_ledger(task_id)
        return contract,ledger
    def _bound_deck_contracts(self,task_id,deck):
        """Load the immutable contracts already bound to a candidate deck.

        Content-only outline revisions deliberately retain the visual contract
        selected when the candidate was designed.  Loading by the hashes on
        the deck prevents a later registry default from silently changing that
        contract while still requiring the Claim Ledger to match frozen input.
        """
        metadata=deck.get("metadata",{})
        contract_hash=metadata.get("design_contract_hash")
        ledger_hash=metadata.get("claim_ledger_hash")
        if not contract_hash or not ledger_hash:
            raise ConflictError("候选全稿未绑定 PresentationTechnicalContract 或 Claim Ledger")
        try:
            contract=validate_presentation_technical_contract(json.loads(self.version(task_id,contract_hash)))
            ledger=validate_claim_ledger(json.loads(self.version(task_id,ledger_hash)))
        except NotFoundError as exc:
            raise ConflictError("候选全稿绑定的 PresentationTechnicalContract 或 Claim Ledger 不存在") from exc
        current_ledger=self._ensure_claim_ledger(task_id)
        if current_ledger["hash"]!=ledger_hash:
            raise ConflictError("候选全稿的 Claim Ledger 与当前冻结输入不一致")
        outline=json.loads(self.version(task_id,deck["outline_hash"]))
        expected_ids=list(contract["slide_ids"])
        if list(outline["slide_ids"])!=expected_ids:
            raise ConflictError("候选全稿页面范围与 PresentationTechnicalContract 不一致")
        return {**contract,"hash":contract_hash},{**ledger,"hash":ledger_hash}
    def _generation_browser_gate(self):
        inspector=self.browser_inspector
        if inspector is None: return None
        enabled=getattr(inspector,"enforce_on_generation",type(inspector).__name__=="ChromiumDeckInspector")
        return inspector if enabled else None
    def _required_claim_ids(self,task_id,slide_ids,contract,ledger):
        all_slide_ids=list(contract["slide_ids"])
        all_claim_ids=[item["claim_id"] for item in ledger["claims"]]
        if list(slide_ids)==all_slide_ids:
            return all_claim_ids
        mapping=self._required_claim_ids_by_slide(task_id,slide_ids,ledger)
        return sorted({claim_id for ids in mapping.values() for claim_id in ids})
    def _required_claim_ids_by_slide(self,task_id,slide_ids,ledger):
        outline_hash=self._current_version(task_id,"outline")
        if not outline_hash:
            raise ConflictError("须先冻结逐页大纲再计算必需事实覆盖")
        outline=json.loads(self.version(task_id,outline_hash))
        _,blocks=parse_outline(outline["markdown"],self.input_view(task_id)["manifest"].get("resources",[]),None)
        if any(slide_id not in blocks for slide_id in slide_ids):
            raise ConflictError("样品页面不属于当前逐页大纲")
        record=next((item for item in self.versions(task_id,"outline") if item["hash"]==outline_hash),None)
        persisted_mapping=(record or {}).get("metadata",{}).get("required_claim_ids_by_slide")
        if isinstance(persisted_mapping,dict) and all(isinstance(persisted_mapping.get(slide_id),list) for slide_id in slide_ids):
            known={claim["claim_id"] for claim in ledger["claims"]}
            scoped={claim_id for slide_id in slide_ids for claim_id in persisted_mapping[slide_id]}
            if not scoped.issubset(known):
                raise ConflictError("逐页大纲的 required claim 映射与当前 Claim Ledger 不一致")
            return {slide_id:list(persisted_mapping[slide_id]) for slide_id in slide_ids}
        ledger_value={key:value for key,value in ledger.items() if key!="hash"}
        return {
            slide_id:sorted({
                claim_id
                for binding in audit_claims(blocks[slide_id],ledger_value)["bindings"]
                for claim_id in binding.get("source_claim_ids",[])
            })
            for slide_id in slide_ids
        }
    def _required_claims_by_slide(self,task_id,slide_ids,ledger):
        ids_by_slide=self._required_claim_ids_by_slide(task_id,slide_ids,ledger)
        claims={claim["claim_id"]:{"claim_id":claim["claim_id"],"kind":claim["kind"],"value":claim["value"]} for claim in ledger["claims"]}
        return {slide_id:[claims[claim_id] for claim_id in ids] for slide_id,ids in ids_by_slide.items()}
    def _required_claims_verbatim(self,task_id,slide_ids,contract,ledger):
        required_by_slide=self._required_claims_by_slide(task_id,slide_ids,ledger)
        required_ids={claim["claim_id"] for claims in required_by_slide.values() for claim in claims}
        return [
            {"claim_id":claim["claim_id"],"kind":claim["kind"],"value":claim["value"]}
            for claim in ledger["claims"] if claim["claim_id"] in required_ids
        ]
    def _post_render_gate(self,task_id,html_text,slide_ids,contract,ledger,assets,generation_attempt_evidence_hashes=None):
        self.require_release_write_enabled()
        contract_hash=contract["hash"]; ledger_hash=ledger["hash"]
        contract_value={key:value for key,value in contract.items() if key!="hash"}
        ledger_value={key:value for key,value in ledger.items() if key!="hash"}
        required_claim_ids=self._required_claim_ids(task_id,slide_ids,contract,ledger)
        required_claim_ids_by_slide=self._required_claim_ids_by_slide(task_id,slide_ids,ledger)
        html_text=apply_presentation_technical_contract(html_text,contract_value,contract_hash)
        html_text=self.technical_gate.validate_candidate_html(html_text,slide_ids,assets)
        html_by_slide=self._slide_fragments(html_text)
        browser=self._generation_browser_gate()
        first=self.technical_gate.evaluate(
            html_text,
            expected_slide_ids=slide_ids,
            contract=contract_value,
            contract_hash=contract_hash,
            claim_ledger=ledger_value,
            claim_ledger_hash=ledger_hash,
            required_claim_ids=required_claim_ids,
            required_claim_ids_by_slide=required_claim_ids_by_slide,
            html_by_slide=html_by_slide,
            browser_inspector=browser,
            generation_attempt_evidence_hashes=generation_attempt_evidence_hashes,
        )
        autofit=None
        if browser is not None and first["geometry"]["overflow_count"]:
            fitted=fit_deck_html(html_text,max_rounds=MAX_CASCADE_ROUNDS)
            if fitted.get("available") and fitted.get("rules"):
                html_text=self.technical_gate.validate_candidate_html(fitted["html"],slide_ids,assets)
                html_by_slide=self._slide_fragments(html_text)
                autofit={
                    "rules":fitted["rules"],"rounds":fitted["rounds"],
                    "converged":fitted["converged"],"remaining":fitted["remaining"],
                }
        evidence=self.technical_gate.evaluate(
            html_text,
            expected_slide_ids=slide_ids,
            contract=contract_value,
            contract_hash=contract_hash,
            claim_ledger=ledger_value,
            claim_ledger_hash=ledger_hash,
            required_claim_ids=required_claim_ids,
            required_claim_ids_by_slide=required_claim_ids_by_slide,
            html_by_slide=html_by_slide,
            browser_inspector=browser,
            overflow_autofit=autofit,
            generation_attempt_evidence_hashes=generation_attempt_evidence_hashes,
        )
        # Persist pass AND failure evidence.  Failed diagnostics carry the
        # slide / selector / scroll-client geometry of every blocker and must
        # stay auditable after the run aborts, not vanish with the exception.
        evidence_hash=self.store.put_version(task_id,"post-render-gate-evidence",canonical_post_render_evidence(evidence),{
            "presentation_technical_contract_hash":contract_hash,
            "design_contract_hash":contract_hash,
            "claim_ledger_hash":ledger_hash,
            "rendered_html_hash":evidence["rendered_html_hash"],
            "passed":evidence["passed"],
            "immutable":True,
        })
        if evidence_hash!=evidence["evidence_hash"]:
            raise ConflictError("渲染后门禁 evidence 内容寻址持久化失败")
        if evidence["blockers"]:
            summary="；".join(f"{item.get('code')}:{item.get('evidence','')}" for item in evidence["blockers"][:5])
            raise ValidationError(f"TechnicalGate 渲染后硬门禁未通过（evidence {evidence_hash[:12]}）：{summary}")
        return html_text,evidence
    def _prepare_sample_preview(self,html_text,contract):
        self.require_release_write_enabled()
        contract_value={key:value for key,value in contract.items() if key!="hash"}
        return apply_presentation_technical_contract(html_text,contract_value,contract["hash"])
    def _latest_generation_audit_id(self,task_id,action):
        if not hasattr(self.store,"agent_audits"): return ""
        context=current_agent_audit_context()
        try:
            audits=self.store.agent_audits(task_id=task_id,job_id=context.get("job_id"))
        except Exception:
            return ""
        stage="deck" if action=="deck" else "sample"
        return next((str(item.get("audit_id") or "") for item in reversed(audits) if item.get("stage")==stage),"")
    def _persist_generation_attempt(self,task_id,body):
        raw=canonical(body)
        return self.store.put_version(task_id,"generation-attempt-evidence",raw,{
            "action":body["action"],"attempt":body["attempt"],"status":body["status"],
            "candidate_hash":body["candidate_hash"],"immutable":True,
        })
    def _persist_generation_correction(self,task_id,body):
        return self.store.put_version(task_id,"generation-correction-evidence",canonical(body),{
            "action":body["action"],"next_attempt":body["next_attempt"],
            "parent_attempt_id":body["parent_attempt_id"],"immutable":True,
        })
    def _build_with_technical_retry(self,task_id,outline,*,action,slide_ids,assets,context):
        """Run model output, assembly and every objective preflight as one attempt.

        A real ``AgentGateway`` validates DesignIntent and assembles shared CSS
        before it returns.  Those model-output failures therefore have to be
        caught around ``builder.build`` itself, not only around the HTML that a
        successful builder returns.  Two model attempts are followed by one
        deterministic, network-free fallback inside the same user Job.
        """
        correction=None
        generation_context=self._generation_context(task_id)
        payload_stage="modify" if context.get("prompt") else "sample" if action=="sample" else "deck_batch"
        base_payload=build_stage_payload(generation_context,payload_stage,{
            "task_id":task_id,"requested_action":action,"upstream_outline":outline,"requested_slide_ids":list(slide_ids),"stage_context":context,
        })
        context_evidence=stage_payload_metadata(generation_context,base_payload)
        ledger=context.get("claim_ledger")
        required_claims=list(context.get("required_claims_verbatim") or [])
        required_ids=[claim["claim_id"] for claim in required_claims]
        required_claims_by_slide=dict(context.get("required_claims_by_slide") or {})
        layout_by_slide={slide_id:"" for slide_id in slide_ids}
        required_ids_by_slide={
            slide_id:[claim["claim_id"] for claim in claims]
            for slide_id,claims in required_claims_by_slide.items()
        }
        attempt_hashes=[]; correction_hashes=[]; parent_attempt_id=""

        def correctable(error):
            if isinstance(error,ValidationError): return True
            if not isinstance(error,GatewayError) or isinstance(error,GatewayUnknownResult): return False
            # Transport/auth/provider failures have safe adapter details and
            # must still degrade runtime readiness instead of being disguised
            # as a successful model run.  Output-contract failures do not.
            return not error.retryable and not error.safe_audit_details() and not error.code.startswith("probe_")

        def prepare(source,*,forced_intent=None,forced_assets=None):
            candidate=str(source)
            page_coverage=getattr(source,"builder_boundary",None)
            design_intent=validate_design_intent(
                forced_intent if forced_intent is not None else (
                    getattr(source,"design_intent",None)
                    or context.get("confirmed_design_intent")
                    or context.get("design_intent")
                    or default_design_intent()
                )
            )
            shared_assets=validate_shared_design_assets(
                forced_assets if forced_assets is not None else (
                    getattr(source,"shared_assets",None)
                    or context.get("confirmed_shared_assets")
                    or context.get("shared_assets")
                )
            )
            candidate=self.technical_gate.validate_candidate_html(candidate,slide_ids,assets)
            fragments=self._slide_fragments(candidate)
            candidate=assemble_presentation(
                [fragments[slide_id] for slide_id in slide_ids],
                context.get("rules",[]),context["design_contract"],context["design_contract_hash"],
                design_intent=design_intent,
                shared_assets=shared_assets,
            )
            candidate=self.technical_gate.validate_candidate_html(candidate,slide_ids,assets)
            return candidate,page_coverage,design_intent,shared_assets

        def preflight(candidate,page_coverage):
            aggregate_coverage=None; browser_evidence=None; browser_blockers=[]; autofit=None
            if ledger:
                aggregate_coverage=audit_html_claims(candidate,ledger,required_claim_ids=required_ids)
                if not isinstance(page_coverage,dict):
                    page_coverage=audit_html_claims_by_slide(
                        self._slide_fragments(candidate),ledger,required_ids_by_slide,
                    )
            browser=self._generation_browser_gate()
            if browser is not None and not (aggregate_coverage or {}).get("unbound_count") and not (page_coverage or {}).get("missing_required_count"):
                browser_evidence=browser.inspect(candidate,slide_ids)
                browser_blockers=TechnicalGate.browser_blockers(browser_evidence)
                geometric=[item for item in browser_blockers if item.get("code") in GEOMETRIC_CODES]
                if geometric:
                    fitted=fit_deck_html(candidate,max_rounds=MAX_CASCADE_ROUNDS)
                    autofit={key:fitted.get(key) for key in ("available","rules","rounds","converged","remaining")}
                    if fitted.get("available") and fitted.get("rules"):
                        fitted_candidate=self.technical_gate.validate_candidate_html(fitted["html"],slide_ids,assets)
                        fitted_browser=browser.inspect(fitted_candidate,slide_ids)
                        fitted_blockers=TechnicalGate.browser_blockers(fitted_browser)
                        if not fitted_blockers:
                            candidate=fitted_candidate; browser_evidence=fitted_browser; browser_blockers=[]
            return candidate,page_coverage,aggregate_coverage,browser_evidence,browser_blockers,autofit,browser

        def correction_for(attempt,error,aggregate_coverage,page_coverage,browser_blockers,autofit):
            if error is not None:
                return {
                    "attempt":attempt,
                    "reason":"gateway_output_failed" if isinstance(error,GatewayError) else "technical_validation_failed",
                    "error_code":error.code,
                    "error":error.message,
                    "rule":"重新提交完整 slides、非空 DesignIntent 与不含 at-rule/任务外资源的 shared_assets；修复页面集合、资源引用和 HTML 安全错误。",
                }
            if (aggregate_coverage or {}).get("unbound_count"):
                return {
                    "attempt":attempt,"reason":"unbound_claims","error":"本批页面包含 Claim Ledger 之外的量化事实",
                    "unbound_claims":list(aggregate_coverage.get("unbound") or []),
                    "rule":"删除所有 unbound_claims；只使用冻结大纲与 Claim Ledger 中的事实，不得用假设、示例或占位文本新增数字。",
                }
            if (page_coverage or {}).get("missing_required_count"):
                missing_ids={claim["claim_id"] for claim in page_coverage["missing_required"]}
                missing_by_slide={
                    slide_id:[
                        claim for claim in required_claims_by_slide[slide_id]
                        if any(item["slide_id"]==slide_id and item["claim_id"]==claim["claim_id"] for item in page_coverage["missing_required"])
                    ] for slide_id in slide_ids
                }
                return {
                    "attempt":attempt,"reason":"missing_required_claims","error":"本批页面遗漏或错放必需冻结事实",
                    "required_claims_verbatim":required_claims,"required_claims_by_slide":required_claims_by_slide,
                    "missing_required_claims_verbatim":[claim for claim in required_claims if claim["claim_id"] in missing_ids],
                    "missing_required_claims_by_slide":missing_by_slide,
                    "rule":"重新提交本批完整 slides；逐页把 required_claims_by_slide 对应 value 逐字写入该 slide 的可见正文，尤其补齐 missing_required_claims_by_slide；出现在其他页不算覆盖。不得放入隐藏节点、样式、脚本或元数据，不得新增 Claim Ledger 之外的量化事实。",
                }
            if browser_blockers:
                return {
                    "attempt":attempt,"reason":"browser_render_blockers","error":"Chromium builder 边界预检未通过",
                    "browser_blockers":browser_blockers,"overflow_autofit":autofit,
                    "rule":"根据 selector 与 geometry 修复溢出、裁切、坏资源或渲染错误；不得隐藏内容或放宽技术门禁。",
                }
            return None

        for attempt in range(1,GENERATION_ATTEMPTS+1):
            request={
                **base_payload,
                **context,
                "presentation_technical_contract":context["design_contract"],
                "presentation_technical_contract_hash":context["design_contract_hash"],
                "generation_attempt":attempt,
            }
            if correction is not None: request["technical_correction"]=correction
            source=None; candidate=""; generation_error=None; page_coverage=None
            design_intent=default_design_intent(); shared_assets={"css":""}
            aggregate_coverage=None; browser_evidence=None; browser_blockers=[]; autofit=None; browser=None
            try:
                source=self.builder.build(outline,action=action,slide_ids=slide_ids,assets=assets,**request)
                candidate,page_coverage,design_intent,shared_assets=prepare(source)
                candidate,page_coverage,aggregate_coverage,browser_evidence,browser_blockers,autofit,browser=preflight(candidate,page_coverage)
            except (GatewayError,ValidationError) as exc:
                if not correctable(exc): raise
                generation_error=exc
                if source is not None: candidate=str(source)

            correction=correction_for(
                attempt,generation_error,aggregate_coverage,page_coverage,browser_blockers,autofit,
            )
            materialization=None
            fallback=None

            # The model receives one structured correction.  If the second
            # candidate still omits a registered frozen fact, bind it into a
            # visible, page-scoped server slot and rerun every preflight on
            # the exact candidate that will be evidenced and returned.
            if (
                attempt==GENERATION_ATTEMPTS
                and correction is not None
                and correction.get("reason")=="missing_required_claims"
            ):
                materialized_claims_by_slide=correction["missing_required_claims_by_slide"]
                candidate,materialization=materialize_required_claim_slots(
                    candidate,materialized_claims_by_slide,layout_by_slide,
                )
                aggregate_coverage=None; page_coverage=None
                browser_evidence=None; browser_blockers=[]; autofit=None
                try:
                    candidate=self.technical_gate.validate_candidate_html(candidate,slide_ids,assets)
                except ValidationError as exc:
                    generation_error=exc
                if generation_error is None and ledger:
                    aggregate_coverage=audit_html_claims(candidate,ledger,required_claim_ids=required_ids)
                    page_coverage=audit_html_claims_by_slide(
                        self._slide_fragments(candidate),ledger,required_ids_by_slide,
                    )
                if (
                    browser is not None
                    and not (aggregate_coverage or {}).get("unbound_count")
                    and not (page_coverage or {}).get("missing_required_count")
                ):
                    browser_evidence=browser.inspect(candidate,slide_ids)
                    browser_blockers=TechnicalGate.browser_blockers(browser_evidence)
                    geometric=[item for item in browser_blockers if item.get("code") in GEOMETRIC_CODES]
                    if geometric:
                        fitted=fit_deck_html(candidate,max_rounds=MAX_CASCADE_ROUNDS)
                        autofit={key:fitted.get(key) for key in ("available","rules","rounds","converged","remaining")}
                        if fitted.get("available") and fitted.get("rules"):
                            fitted_candidate=self.technical_gate.validate_candidate_html(fitted["html"],slide_ids,assets)
                            fitted_browser=browser.inspect(fitted_candidate,slide_ids)
                            fitted_blockers=TechnicalGate.browser_blockers(fitted_browser)
                            if not fitted_blockers:
                                candidate=fitted_candidate; browser_evidence=fitted_browser; browser_blockers=[]
                correction=correction_for(
                    attempt,generation_error,aggregate_coverage,page_coverage,browser_blockers,autofit,
                )

            # The second invalid model result must not escape to the UI as a
            # second user-visible failure.  Rebuild from the frozen outline
            # with the generic, network-free shell and rerun every preflight.
            if attempt==GENERATION_ATTEMPTS and correction is not None:
                from .execution import progress
                progress("safe_fallback","模型输出仍不合规，正在生成安全样品")
                fallback={
                    "degraded_fallback":True,
                    "reason":correction["reason"],
                    "error_code":correction.get("error_code"),
                    "strategy":"frozen_outline_generic_shell",
                }
                fallback_intent=default_design_intent()
                fallback_assets={"css":""}
                try:
                    fallback_source=render(
                        outline,slide_ids,context.get("rules"),context.get("exceptions"),assets,
                        context["design_contract"],context["design_contract_hash"],
                        design_intent=fallback_intent,shared_assets=fallback_assets,
                    )
                    candidate,page_coverage,design_intent,shared_assets=prepare(
                        fallback_source,forced_intent=fallback_intent,forced_assets=fallback_assets,
                    )
                    candidate,page_coverage,aggregate_coverage,browser_evidence,browser_blockers,autofit,browser=preflight(candidate,None)
                    if (page_coverage or {}).get("missing_required_count"):
                        candidate,materialization=materialize_required_claim_slots(
                            candidate,{
                                slide_id:[
                                    claim for claim in required_claims_by_slide[slide_id]
                                    if any(item["slide_id"]==slide_id and item["claim_id"]==claim["claim_id"] for item in page_coverage["missing_required"])
                                ] for slide_id in slide_ids
                            },layout_by_slide,
                        )
                        candidate=self.technical_gate.validate_candidate_html(candidate,slide_ids,assets)
                        candidate,page_coverage,aggregate_coverage,browser_evidence,browser_blockers,autofit,browser=preflight(candidate,None)
                    generation_error=None
                except (GatewayError,ValidationError) as exc:
                    generation_error=exc
                correction=correction_for(
                    attempt,generation_error,aggregate_coverage,page_coverage,browser_blockers,autofit,
                )
                if correction is not None:
                    fallback["failed_reason"]=correction["reason"]

            accepted=(
                correction is None
                and generation_error is None
                and not (aggregate_coverage or {}).get("unbound_count")
                and not (page_coverage or {}).get("missing_required_count")
                and not browser_blockers
            )
            status="accepted" if accepted else ("correction_required" if correction is not None and attempt<GENERATION_ATTEMPTS else "failed")
            page_hashes={slide_id:page.get("text_hash") for slide_id,page in (page_coverage or {}).get("pages",{}).items()}
            attempt_body={
                "schema_version":"1.0","task_id":task_id,
                "action":action,"attempt":attempt,"parent_attempt_id":parent_attempt_id,
                "input_hash":digest(canonical(request)),"candidate_hash":digest(candidate.encode()),"candidate_html":candidate,
                "required_claims_by_slide":required_claims_by_slide,"builder_boundary":page_coverage,
                "visible_text_hash":digest(canonical(page_hashes)),"aggregate_claims":aggregate_coverage,
                "browser_preflight":browser_evidence,"overflow_autofit":autofit,
                "server_claim_materialization":materialization,
                "generation_error":None if generation_error is None else {"code":generation_error.code,"message":generation_error.message},
                "fallback":fallback,
                "status":status,"provider_audit_id":self._latest_generation_audit_id(task_id,action),
                "result_certainty":"known","created_at":utcnow(),
            }
            attempt_hash=self._persist_generation_attempt(task_id,attempt_body)
            attempt_hashes.append(attempt_hash); parent_attempt_id=attempt_hash
            if status=="accepted":
                generation_meta={"attempts":attempt,"retry_count":attempt-1,"max_attempts":GENERATION_ATTEMPTS,"attempt_evidence_hashes":attempt_hashes,"correction_evidence_hashes":correction_hashes,"design_intent":design_intent,"shared_assets":shared_assets,**context_evidence}
                if materialization is not None: generation_meta["server_claim_materialization"]=materialization
                if fallback is not None: generation_meta.update(degraded_fallback=True,fallback=fallback)
                return candidate,generation_meta
            if correction is not None and attempt<GENERATION_ATTEMPTS:
                from .execution import progress
                progress("technical_correction","模型输出未通过技术校验，正在自动修正")
                correction_body={
                    "schema_version":"1.0","task_id":attempt_body["task_id"],"action":action,
                    "parent_attempt_id":attempt_hash,"next_attempt":attempt+1,"correction":correction,
                    "correction_hash":digest(canonical(correction)),"provider_audit_id":attempt_body["provider_audit_id"],
                    "result_certainty":"known","created_at":utcnow(),
                }
                correction_hashes.append(self._persist_generation_correction(task_id,correction_body))
                continue
            if generation_error is not None: raise generation_error
            raise ValidationError(f"确定性安全 fallback 未通过技术门禁：{(correction or {}).get('reason','unknown')}")
        raise AssertionError("技术输出有界重取未终止")
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
        current=self._current_version(task_id,"narrative")
        if self.generation_pipeline is not None and prompt is None and scope=="all":
            if current and self._version_metadata(task_id,"narrative",current).get("generation_core"):
                return self.planning_view(task_id)
            if not current:
                return self._generate_narrative_with_core(task_id,view,state,scope)
        skill=self.skills.load("narrative"); prior=self._current_version(task_id,"narrative"); context_evidence={}
        ledger=view["claim_ledger"]; ledger_value={key:value for key,value in ledger.items() if key!="hash"}
        if isinstance(self.generator,FakeGenerationGateway):
            text=narrative_markdown(view["task_card"])
            if prompt: text += f"\n## 修改要求\n{prompt.strip()}\n"
            model_name=self.generator.model
            claim_bindings=assert_claims_bound(text,ledger_value,"叙事")
            quality_evidence=assert_narrative_quality(text,view["task_card"])
        else:
            numeric_policy=narrative_numeric_policy(ledger_value)
            structure_policy=narrative_structure_policy(view["task_card"])
            generation_context=self._generation_context(task_id,view)
            payload=build_stage_payload(generation_context,"narrative",{"task_id":task_id,"task_card":view["task_card"],"prompt":prompt,"scope":scope,"claim_ledger":ledger_value,"narrative_numeric_policy":numeric_policy,"narrative_structure_policy":structure_policy})
            context_evidence=stage_payload_metadata(generation_context,payload)
            for attempt in range(1,3):
                generated=self.generator.generate("narrative",payload,skill=skill["content"])
                text=generated["text"]; model_name=generated.get("model","unknown")
                if attempt==2:
                    # The correction turn may still paraphrase or truncate a
                    # long identity field.  Assemble the exact frozen values
                    # at the trusted boundary before validating the candidate.
                    text=materialize_required_context(text,structure_policy["required_context"])
                claim_error=quality_error=None
                try:
                    claim_bindings=assert_claims_bound(text,ledger_value,"叙事",allow_disclosed_assumptions=False)
                except ValidationError as exc:
                    claim_error=exc
                try:
                    quality_evidence=assert_narrative_quality(text,view["task_card"])
                except ValidationError as exc:
                    quality_error=exc
                    quality_evidence=narrative_quality_evidence(text,view["task_card"])
                if claim_error is None and quality_error is None:
                    break
                if attempt==2:
                    raise claim_error or quality_error
                evidence=audit_claims(text,ledger_value,allow_disclosed_assumptions=False)
                errors=[exc.message for exc in (claim_error,quality_error) if exc is not None]
                payload={**payload,"semantic_correction":{
                    "attempt":attempt,
                    "error":"；".join(errors),
                    "forbidden_values":[item["value"] for item in evidence["unbound"]],
                    "missing_context_fields":quality_evidence["missing_context_fields"],
                    "required_context_verbatim":structure_policy["required_context"],
                    "required_context_markdown_block":required_context_markdown(structure_policy["required_context"]),
                    "rule":"删除全部未绑定量化值；不得改标为假设、建议、示例或待确认。阶段改用无数字名称，仅保留 narrative_numeric_policy.allowed_claims 中的原始量化事实。并按 narrative_structure_policy 返回完整叙事，将 required_context_verbatim 逐字写入正文；服务端也会将 semantic_correction.required_context_markdown_block 原样组装入纠错产物，正文不得出现与其冲突的主题、目标或受众；补齐核心论点与页面推进逻辑，不得返回分析请求或元说明。",
                }}
        version=len(self.versions(task_id,"narrative"))+1; content_hash=digest(text.encode())
        model=NarrativeDocument.parse({"document_id":f"narrative-{content_hash[:16]}","task_id":task_id,"version":version,"markdown":text,"content_hash":content_hash,"created_at":now(),"schema_version":"1.0"})
        metadata={"parent":prior,"action":"generate" if not prior else "regenerate","scope":scope,"summary":"生成整稿叙事结构","model":model_name,"skill":{"action":"narrative","version":skill["version"],"hash":digest(skill["content"].encode()),"included":["narrative"],"trimmed":["outline","html","inspection"]},"input_snapshot_hash":view["snapshot_hash"],"claim_ledger_hash":ledger["hash"],"claim_bindings":claim_bindings["bindings"],"unbound_claim_count":0,"narrative_quality":quality_evidence,**context_evidence}
        h=self._record_p3(task_id,"narrative",model,metadata,"narrative_generate")
        if state.stage in {state.stage.CLARIFICATION,state.stage.CREATED}:
            self.command(task_id,f"narrative-stage-{h[:12]}","advance")
        else:
            self._reset_narrative_gate(task_id,h)
        return self.planning_view(task_id)
    def edit_narrative(self,task_id,markdown,summary="直接编辑"):
        self._require_actionable(task_id)
        view=self._p3_input(task_id)
        if not isinstance(markdown,str) or not markdown.strip(): raise ValidationError("叙事 Markdown 不得为空")
        ledger=view["claim_ledger"]; claim_bindings=assert_claims_bound(markdown,{key:value for key,value in ledger.items() if key!="hash"},"叙事")
        quality_evidence=assert_narrative_quality(markdown,view["task_card"])
        prior=self._current_version(task_id,"narrative"); version=len(self.versions(task_id,"narrative"))+1; content_hash=digest(markdown.encode())
        model=NarrativeDocument.parse({"document_id":f"narrative-{content_hash[:16]}","task_id":task_id,"version":version,"markdown":markdown,"content_hash":content_hash,"created_at":now(),"schema_version":"1.0"})
        h=self._record_p3(task_id,"narrative",model,{"parent":prior,"action":"direct_edit","summary":summary,"authoritative":True,"invalidated":["outline","sample","deck"],"claim_ledger_hash":ledger["hash"],"claim_bindings":claim_bindings["bindings"],"unbound_claim_count":0,"narrative_quality":quality_evidence},"narrative_edit","user")
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
        current=self._current_version(task_id,"outline"); context_evidence={}
        narrative_meta=self._version_metadata(task_id,"narrative",narrative)
        if self.generation_pipeline is not None and prompt is None and not slide_ids and narrative_meta.get("generation_core"):
            if current and self._version_metadata(task_id,"outline",current).get("generation_core"):
                return self.planning_view(task_id)
            if not current:
                context=self._generation_context(task_id,view)
                brief=self._generation_core_brief(task_id,view,context)
                result=self.generation_pipeline.generate_outline(task_id,brief,narrative_meta["generation_core"]["checkpoint_id"],context=context)
                text=self._outline_markdown_from_core(result.value,brief)
                return self.edit_outline(task_id,text,"生成逐页大纲",actor="system",extra_metadata={"generation_core":self._generation_core_evidence(result)})
        skill=self.skills.load("outline"); count=requested_slide_count(view["task_card"]); resources=view["manifest"].get("resources",[])
        ledger=view["claim_ledger"]; ledger_value={key:value for key,value in ledger.items() if key!="hash"}
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
                required_claims=[
                    {"claim_id":claim["claim_id"],"kind":claim["kind"],"value":claim["value"]}
                    for claim in ledger_value["claims"]
                ]
                required_claim_ids=[claim["claim_id"] for claim in required_claims]
                generation_context=self._generation_context(task_id,view)
                payload=build_stage_payload(generation_context,"outline",{"task_id":task_id,"task_card":view["task_card"],"narrative":json.loads(self.version(task_id,narrative))["markdown"],"resources":resources,"slide_count":count,"prompt":prompt,"claim_ledger":ledger_value,"outline_required_claims_verbatim":required_claims})
                context_evidence=stage_payload_metadata(generation_context,payload)
                for attempt in range(1,3):
                    generated=self.generator.generate("outline",payload,skill=skill["content"])
                    text=None
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
                        assert_claims_bound(text,ledger_value,"大纲",require_all=True)
                        break
                    except ValidationError as exc:
                        self.store.put_version(task_id,"outline-diagnostic",canonical({"attempt":attempt,"candidate":generated}),{
                            "v":len(self.versions(task_id,"outline-diagnostic"))+1,
                            "stage":"outline","attempt":attempt,"error_code":exc.code,"error_message":exc.message,
                            "model":generated.get("model","unknown") if isinstance(generated,dict) else "unknown","public_error_exposes_candidate":False,
                        })
                        if attempt == 2: raise
                        missing_claims=required_claims
                        if isinstance(text,str):
                            coverage=audit_claims(text,ledger_value,required_claim_ids=required_claim_ids)
                            missing_ids={claim["claim_id"] for claim in coverage["missing_required"]}
                            missing_claims=[claim for claim in required_claims if claim["claim_id"] in missing_ids]
                        payload={**payload,"semantic_correction":{
                            "attempt":1,
                            "error":exc.message,
                            "previous_candidate":generated,
                            "required_claims_verbatim":required_claims,
                            "missing_required_claims_verbatim":missing_claims,
                            "rule":"重新提交完整 slides；逐字覆盖 required_claims_verbatim 的每个 value，尤其不得合并、概括或遗漏预算拆分。missing_required_claims_verbatim 是上一候选明确缺失的子集。不得新增 Claim Ledger 之外的量化事实。",
                        }}
        return self.edit_outline(task_id,text,"生成逐页大纲",actor="system",skill=skill,extra_metadata=context_evidence)
    def edit_outline(self,task_id,markdown,summary="直接编辑",actor="user",skill=None,extra_metadata=None):
        self._require_actionable(task_id)
        view=self._p3_input(task_id); expected=requested_slide_count(view["task_card"])
        markdown=normalize_outline_markdown(markdown,view["manifest"].get("resources",[]),expected)
        ledger=view["claim_ledger"]; ledger_value={key:value for key,value in ledger.items() if key!="hash"}; claim_bindings=assert_claims_bound(markdown,ledger_value,"大纲",require_all=True)
        slide_ids,blocks=parse_outline(markdown,view["manifest"].get("resources",[]),expected)
        required_claim_ids_by_slide={
            slide_id:sorted({
                claim_id
                for binding in audit_claims(block,ledger_value)["bindings"]
                for claim_id in binding.get("source_claim_ids",[])
            })
            for slide_id,block in blocks.items()
        }
        prior=self._current_version(task_id,"outline"); before={}
        if prior: _,before=parse_outline(json.loads(self.version(task_id,prior))["markdown"],view["manifest"].get("resources",[]),None)
        affected=changed_slide_ids(before,blocks); version=len(self.versions(task_id,"outline"))+1; content_hash=digest(markdown.encode())
        model=SlideOutline.parse({"outline_id":f"outline-{content_hash[:16]}","task_id":task_id,"version":version,"markdown":markdown,"slide_ids":slide_ids,"content_hash":content_hash,"created_at":now(),"schema_version":"1.0"})
        meta={"parent":prior,"action":"generate" if not prior else "edit","summary":summary,"affected":affected,"unchanged":[sid for sid in blocks if sid in before and blocks[sid]==before[sid]],"authoritative":True,"invalidated":{"sample":affected,"deck":affected},"claim_ledger_hash":ledger["hash"],"claim_bindings":claim_bindings["bindings"],"unbound_claim_count":0,"required_claim_ids":claim_bindings["required_claim_ids"],"required_claim_ids_by_slide":required_claim_ids_by_slide}
        if skill: meta["skill"]={"action":"outline","version":skill["version"],"hash":digest(skill["content"].encode()),"included":["outline"],"trimmed":["narrative","html","inspection"]}
        if extra_metadata: meta.update(extra_metadata)
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
        result={"state":self.get(task_id),"outline_hash":outline,"selection":None,"sample":None,"confirmation":None,"design_contract":None,"presentation_technical_contract":None,"claim_ledger":None,"versions":self.versions(task_id,"sample")}
        if outline:
            contract_records=self.versions(task_id,"presentation-technical-contract") or self.versions(task_id,"design-contract")
            contract_record=next((record for record in reversed(contract_records) if record["metadata"].get("outline_hash")==outline),None)
            if contract_record:
                result["design_contract"]={**json.loads(self.version(task_id,contract_record["hash"])),"hash":contract_record["hash"]}
                result["presentation_technical_contract"]=result["design_contract"]
        ledger_record=next(iter(reversed(self.versions(task_id,"claim-ledger"))),None)
        if ledger_record:
            result["claim_ledger"]={**json.loads(self.version(task_id,ledger_record["hash"])),"hash":ledger_record["hash"]}
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
                    and event["result"].get("design_contract_hash")==sample["metadata"].get("design_contract_hash")
                    and event["result"].get("claim_ledger_hash")==sample["metadata"].get("claim_ledger_hash")
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
        contract,ledger=self._generation_contracts(task_id,outline)
        automatic_selection=slide_ids is None
        if automatic_selection:
            sample_targets=required_sample_targets(data["markdown"],count)
            slide_ids,reasons=recommend(
                data["markdown"],count,required_targets=sample_targets,
            )
        else:
            if not isinstance(slide_ids,list) or not slide_ids or len(slide_ids)>len(valid) or len(set(slide_ids))!=len(slide_ids) or any(x not in valid for x in slide_ids): raise ValidationError("样品页面选择无效或重复")
            reasons={sid:"用户选择" for sid in slide_ids}
            sample_targets=[{"slide_id":sid,"role":"user-selected","basis":"explicit user selection"} for sid in slide_ids]
        target_ids={item["slide_id"] for item in sample_targets}
        if not target_ids.issubset(set(slide_ids)):
            raise ValidationError("样品页面选择未覆盖 required sample targets")
        seed=canonical({"outline_hash":outline,"slide_ids":slide_ids,"required_sample_targets":sample_targets}); model=SampleSelection.parse({"selection_id":f"selection-{digest(seed)[:16]}","task_id":task_id,"outline_hash":outline,"slide_ids":slide_ids,"confirmed":False,"required_sample_targets":sample_targets,"schema_version":"1.0"})
        selection_metadata={"reasons":reasons,"strategy":"representative-diversity-v1" if automatic_selection else "user-selected","required_sample_targets":sample_targets,"presentation_technical_contract_hash":contract["hash"],"design_contract_hash":contract["hash"],"claim_ledger_hash":ledger["hash"]}
        h=self.store.put_version(task_id,"sample-selection",canonical(model.to_dict()),selection_metadata)
        state=TaskState.parse(self.get(task_id))
        new=(TaskState(**{**state.__dict__,"sample_confirmed":False,"status":state.status.WAITING_FOR_USER,"waiting_reason":"manual_gate","required_action":"confirm_sample","revision":state.revision+1})
             if state.sample_confirmed else TaskState(**{**state.__dict__,"revision":state.revision+1}))
        event={"event_id":hashlib.sha256(f"{task_id}:select-samples:{h}:{state.revision}".encode()).hexdigest()[:24],"command_id":f"select-samples-{h[:16]}-{state.revision}","action":"select_samples","actor":"system" if automatic_selection else "user","request_hash":h,"at":utcnow(),"from":state.to_dict(),"to":new.to_dict(),"result":{"hash":h,"invalidated":["sample_confirmation","deck"] if state.sample_confirmed else []}}
        self.store.commit(task_id,new.to_dict(),event)
        return {**self.sample_view(task_id),"selection":{**model.to_dict(),"hash":h,"metadata":selection_metadata}}
    def generate_sample(self,task_id,prompt=None):
        from .execution import progress
        self._require_actionable(task_id)
        view=self.sample_view(task_id)
        if view["state"]["stage"]!="sample": raise ConflictError("当前样品阶段已完成；历史样品只读")
        selection=view["selection"]
        if not selection:
            view=self.select_samples(task_id); selection=view["selection"]
        outline=self._current_version(task_id,"outline")
        if selection["outline_hash"] != outline:
            if selection.get("metadata",{}).get("strategy")=="representative-diversity-v1":
                # An automatic selection has no user-authored intent to
                # preserve.  Refresh it against the newly confirmed outline in
                # the same command so the UI cannot enqueue a guaranteed 0 ms
                # conflict after an outline edit.  Explicit user selections
                # remain fail-closed and require reconfirmation.
                view=self.select_samples(task_id,count=len(selection["slide_ids"]))
                selection=view["selection"]
            else:
                raise ConflictError("用户样品选择已因大纲变化而失效，请重新选择页面")
        data=json.loads(self.version(task_id,outline)); rules=[]; assets=controlled_assets(self.input_view(task_id)["manifest"],self.store.resource_root(task_id))
        contract,ledger=self._generation_contracts(task_id,outline)
        contract_value={key:value for key,value in contract.items() if key!="hash"}
        ledger_value={key:value for key,value in ledger.items() if key!="hash"}
        required_claims=self._required_claims_verbatim(task_id,list(selection["slide_ids"]),contract,ledger)
        required_claims_by_slide=self._required_claims_by_slide(task_id,list(selection["slide_ids"]),ledger)
        if prompt: rules.append(prompt.strip())
        outline_meta=self._version_metadata(task_id,"outline",outline)
        current_sample=view.get("sample")
        if (self.generation_pipeline is not None and prompt is None and outline_meta.get("generation_core")
            and current_sample and current_sample.get("metadata",{}).get("generation_core")
            and current_sample["metadata"].get("selection_hash")==selection["hash"]):
            return view
        if self.generation_pipeline is not None and prompt is None and outline_meta.get("generation_core"):
            context=self._generation_context(task_id)
            brief=self._generation_core_brief(task_id,context=context)
            result=self.generation_pipeline.generate_sample(task_id,brief,outline_meta["generation_core"]["checkpoint_id"],selected_slide_ids=selection["slide_ids"],context=context)
            source=self._generation_core_preview_html(result.artifact,brief,assets)
            if isinstance(result.value,HtmlSampleSpec):
                design_intent=validate_design_intent(result.value.design_intent)
                shared_assets=validate_shared_design_assets({"css":result.value.shared_css})
            else:
                design_intent=self._design_intent_from_core(result.value.theme_tokens,{slide.layout_family for slide in result.value.slides})
                shared_assets={"css":""}
            generation={**self._generation_core_evidence(result),"attempts":1,"retry_count":0,"max_attempts":1,"design_intent":design_intent,"shared_assets":shared_assets,"renderer_version":result.artifact.renderer_version}
            if result.validation is not None: generation["validation_hash"]=result.validation.evidence_hash
        elif isinstance(self.builder,FakeHtmlBuilder):
            design_intent=validate_design_intent(self.builder.design_intent)
            shared_assets=validate_shared_design_assets(self.builder.shared_assets)
            source=render(data["markdown"],selection["slide_ids"],rules,assets=assets,design_contract=contract_value,contract_hash=contract["hash"],design_intent=design_intent,shared_assets=shared_assets)
            generation={"attempts":1,"retry_count":0,"max_attempts":1,"design_intent":design_intent,"shared_assets":shared_assets}
        else:
            source,generation=self._build_with_technical_retry(
                task_id,data["markdown"],action="sample",slide_ids=list(selection["slide_ids"]),assets=assets,
                context={"rules":rules,"design_contract":contract_value,"design_contract_hash":contract["hash"],"claim_ledger":ledger_value,"claim_ledger_hash":ledger["hash"],"required_claims_verbatim":required_claims,"required_claims_by_slide":required_claims_by_slide},
            )
        progress("saving_preview", "保存样品预览")
        html_text=self._prepare_sample_preview(source,contract)
        version=len(self.versions(task_id,"sample"))+1; content_hash=digest(html_text.encode()); model=DeckArtifact.parse({"artifact_id":f"sample-{content_hash[:16]}","task_id":task_id,"version":version,"kind":"sample","outline_hash":outline,"content_hash":content_hash,"created_at":now(),"schema_version":"1.0"})
        prior=self._current_version(task_id,"sample"); meta={"html":html_text,"selection_hash":selection["hash"],"parent":prior,"summary":"生成真实 HTML 样品","scope":"global","global_rules":rules,"local_exceptions":{},"build":"success","generation":generation,"design_intent":generation.get("design_intent",default_design_intent()),"shared_design_assets":generation.get("shared_assets",{"css":""}),"presentation_technical_contract_hash":contract["hash"],"design_contract_hash":contract["hash"],"claim_ledger_hash":ledger["hash"],"preview":{"ready":True,"rendered_html_hash":content_hash,"slide_ids":list(selection["slide_ids"])}}
        if generation.get("page_contract_hashes"): meta["page_contract_hashes"]=generation["page_contract_hashes"]
        if "checkpoint_id" in generation:
            keys=("checkpoint_id","output_sha256","contract_name","model","provider_calls","recovery_count","pipeline_version","generation_mode","reused","context_snapshot_id","context_snapshot_hash","stage_payload_hash","context_sections_read")
            meta["generation_core"]={key:generation[key] for key in keys if key in generation}
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
        contract,ledger=self._generation_contracts(task_id,outline)
        contract_value={key:value for key,value in contract.items() if key!="hash"}
        ledger_value={key:value for key,value in ledger.items() if key!="hash"}
        required_claims=self._required_claims_verbatim(task_id,ids,contract,ledger)
        required_claims_by_slide=self._required_claims_by_slide(task_id,ids,ledger)
        previous_slides="".join(self._slide_fragments(sample["html"]).get(sid,"") for sid in ids)
        prior_intent=validate_design_intent(sample["metadata"].get("design_intent"))
        prior_assets=validate_shared_design_assets(sample["metadata"].get("shared_design_assets"))
        core_evidence=None
        core_sample=meta.get("generation_core")
        if (self.generation_pipeline is not None and core_sample
            and core_sample.get("contract_name")==HtmlSampleSpec.TITLE
            and core_sample.get("generation_mode")=="agent_html"):
            context=self._generation_context(task_id)
            brief=self._generation_core_brief(task_id,context=context)
            result=self.generation_pipeline.modify_sample(
                task_id,brief,core_sample["checkpoint_id"],prompt,
                scope=scope,slide_id=slide_id,element_id=element_id,
                current_fragments=self._slide_fragments(sample["html"]),context=context,
            )
            source=self._generation_core_preview_html(result.artifact,brief,assets)
            prior_intent=validate_design_intent(result.value.design_intent)
            prior_assets=validate_shared_design_assets({"css":result.value.shared_css})
            core_evidence=self._generation_core_evidence(result)
            generation={**core_evidence,"attempts":1,"retry_count":result.checkpoint.metadata.get("recovery_count",0),"max_attempts":1,"design_intent":prior_intent,"shared_assets":prior_assets,"renderer_version":result.artifact.renderer_version}
            if result.validation is not None: generation["validation_hash"]=result.validation.evidence_hash
        elif isinstance(self.builder,FakeHtmlBuilder):
            source=render(data["markdown"],ids,rules,exceptions,assets,contract_value,contract["hash"],design_intent=prior_intent,shared_assets=prior_assets)
            generation={"attempts":1,"retry_count":0,"max_attempts":1,"design_intent":prior_intent,"shared_assets":prior_assets}
        else:
            source,generation=self._build_with_technical_retry(
                task_id,data["markdown"],action="sample",slide_ids=ids,assets=assets,
                context={"rules":rules,"exceptions":exceptions,"previous_slides":previous_slides,"prompt":prompt,"scope":scope,"slide_id":slide_id,"element_id":element_id,"design_contract":contract_value,"design_contract_hash":contract["hash"],"claim_ledger":ledger_value,"claim_ledger_hash":ledger["hash"],"required_claims_verbatim":required_claims,"required_claims_by_slide":required_claims_by_slide,"design_intent":prior_intent,"shared_assets":prior_assets},
            )
        html_text=self._prepare_sample_preview(source,contract)
        version=len(self.versions(task_id,"sample"))+1; ch=digest(html_text.encode()); model=DeckArtifact.parse({"artifact_id":f"sample-{ch[:16]}","task_id":task_id,"version":version,"kind":"sample","outline_hash":outline,"content_hash":ch,"created_at":now(),"schema_version":"1.0"})
        modification_meta={"html":html_text,"selection_hash":view["selection"]["hash"],"parent":sample["hash"],"summary":prompt.strip(),"scope":scope,"scope_understanding":understanding,"slide_id":slide_id,"element_id":element_id,"global_rules":rules,"local_exceptions":exceptions,"build":"success","generation":generation,"design_intent":generation.get("design_intent",sample["metadata"].get("design_intent",default_design_intent())),"shared_design_assets":generation.get("shared_assets",sample["metadata"].get("shared_design_assets",{"css":""})),"presentation_technical_contract_hash":contract["hash"],"design_contract_hash":contract["hash"],"claim_ledger_hash":ledger["hash"],"preview":{"ready":True,"rendered_html_hash":ch,"slide_ids":ids}}
        if core_evidence:
            modification_meta["generation_core"]=core_evidence
            modification_meta["page_contract_hashes"]=core_evidence.get("page_contract_hashes",{})
        h=self._record_p3(task_id,"sample",model,modification_meta,"sample_modify","user")
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
            contract,ledger=self._generation_contracts(task_id,outline)
            if (sample["metadata"].get("design_contract_hash")!=contract["hash"]
                or sample["metadata"].get("claim_ledger_hash")!=ledger["hash"]):
                raise ConflictError("样品未绑定当前 PresentationTechnicalContract 或 Claim Ledger")
            state=TaskState.parse(self.get(task_id))
            if state.stage==state.stage.DECK and state.sample_confirmed and view["confirmation"]:
                return view
            if state.stage!=state.stage.SAMPLE:
                raise ConflictError("当前阶段不能确认样品")
            pages=self._slide_fragments(sample["html"])
            if set(pages) != set(selection["slide_ids"]): raise ConflictError("样品页面边界无效，无法确认")
            core_confirmation=None
            core_sample=sample["metadata"].get("generation_core")
            if self.generation_pipeline is not None and core_sample:
                core_result=self.generation_pipeline.confirm_sample(task_id,core_sample["checkpoint_id"])
                core_confirmation=self._generation_core_evidence(core_result)
            new=transition(state,"confirm_sample",actor="user")
            confirmed_pages={sid:{"html":fragment,"sha256":digest(fragment.encode())} for sid,fragment in pages.items()}
            design_intent=validate_design_intent(sample["metadata"].get("design_intent"))
            shared_assets=validate_shared_design_assets(sample["metadata"].get("shared_design_assets"))
            result={"confirmed_outline_hash":outline,"confirmed_sample_hash":sample["hash"],"confirmed_content_hash":sample["content_hash"],"selection_hash":view["selection"]["hash"],"presentation_technical_contract_hash":contract["hash"],"design_contract_hash":contract["hash"],"claim_ledger_hash":ledger["hash"],"confirmed_pages":confirmed_pages,"design_intent":design_intent,"design_intent_hash":digest(canonical(design_intent)),"shared_design_assets":shared_assets,"shared_design_assets_hash":digest(canonical(shared_assets))}
            if core_confirmation: result["generation_core_confirmation"]=core_confirmation
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
        contract,ledger=self._generation_contracts(task_id,outline)
        contract_value={key:value for key,value in contract.items() if key!="hash"}
        ledger_value={key:value for key,value in ledger.items() if key!="hash"}
        if meta.get("design_contract_hash")!=contract["hash"] or meta.get("claim_ledger_hash")!=ledger["hash"]:
            raise ConflictError("确认样品未绑定当前 PresentationTechnicalContract 或 Claim Ledger")
        confirmation=sample_view["confirmation"] or {}
        design_intent=validate_design_intent(confirmation.get("design_intent") or meta.get("design_intent"))
        shared_assets=validate_shared_design_assets(confirmation.get("shared_design_assets") or meta.get("shared_design_assets"))
        confirmed_pages=confirmation.get("confirmed_pages") or {}
        sample_fragments={sid:item["html"] for sid,item in confirmed_pages.items()}
        if not sample_fragments or any(digest(fragment.encode()) != confirmed_pages[sid].get("sha256") for sid,fragment in sample_fragments.items()):
            raise ConflictError("确认样品原始页面或 SHA-256 无效")
        outline_meta=self._version_metadata(task_id,"outline",outline)
        core_confirmation=confirmation.get("generation_core_confirmation")
        if self.generation_pipeline is not None and outline_meta.get("generation_core") and core_confirmation:
            current=self.deck_view(task_id).get("deck")
            if current and current.get("metadata",{}).get("generation_core") and current["metadata"].get("sample_hash")==sample["hash"]:
                return self.deck_view(task_id)
            context=self._generation_context(task_id)
            brief=self._generation_core_brief(task_id,context=context)
            core_result=self.generation_pipeline.generate_deck(task_id,brief,outline_meta["generation_core"]["checkpoint_id"],core_confirmation["checkpoint_id"],context=context)
            review_result=self.generation_pipeline.create_review_input(task_id,core_result.checkpoint.checkpoint_id)
            html_text=self._generation_core_preview_html(core_result.artifact,brief,assets)
            html_text=self._replace_slide_fragments(html_text,sample_fragments)
            html_text,gate=self._post_render_gate(task_id,html_text,ids,contract,ledger,assets)
            deck_fragments=self._slide_fragments(html_text)
            preserved={sid:digest(deck_fragments[sid].encode())==digest(fragment.encode()) for sid,fragment in sample_fragments.items()}
            if not all(preserved.values()): raise ConflictError("确认样品发生未提示变化")
            core_evidence=self._generation_core_evidence(core_result)
            core_evidence["review_checkpoint_id"]=review_result.checkpoint.checkpoint_id
            return self._commit_candidate_deck(task_id,html_text,outline,{"parent":token["parent_deck_hash"],"summary":"生成未检查候选稿","scope":"global","affected":ids,"sample_hash":sample["hash"],"sample_pages_preserved":preserved,"outline_consistent":True,"inspection_status":"pending","global_rules":meta.get("global_rules",[]),"local_exceptions":meta.get("local_exceptions",{}),"generation_batches":core_result.checkpoint.metadata.get("batches",[]),"design_intent":design_intent,"shared_design_assets":shared_assets,"presentation_technical_contract_hash":contract["hash"],"design_contract_hash":contract["hash"],"claim_ledger_hash":ledger["hash"],"post_render_gate":gate,"generation_core":core_evidence,"page_contract_hashes":core_result.checkpoint.metadata.get("page_contract_hashes",{})},"deck_generate",token)
        generation_batches=[]
        if isinstance(self.builder,FakeHtmlBuilder):
            html_text=render(data["markdown"],ids,meta.get("global_rules",[]),meta.get("local_exceptions",{}),assets,contract_value,contract["hash"],design_intent=design_intent,shared_assets=shared_assets)
        else:
            from .execution import checkpoint, progress
            unconfirmed=[sid for sid in ids if sid not in sample_fragments]
            generated={}
            for index in range(0,len(unconfirmed),3):
                checkpoint(); batch=unconfirmed[index:index+3]
                progress("generating_batch",f"生成未确认页面 {index+1}-{index+len(batch)} / {len(unconfirmed)}")
                batch_contract=scope_presentation_technical_contract(contract_value,batch)
                required_claims=self._required_claims_verbatim(task_id,batch,contract,ledger)
                required_claims_by_slide=self._required_claims_by_slide(task_id,batch,ledger)
                partial,batch_generation=self._build_with_technical_retry(
                    task_id,data["markdown"],action="deck",slide_ids=batch,assets=assets,
                    context={"rules":meta.get("global_rules",[]),"exceptions":meta.get("local_exceptions",{}),"design_contract":batch_contract,"design_contract_hash":contract["hash"],"claim_ledger":ledger_value,"claim_ledger_hash":ledger["hash"],"required_claims_verbatim":required_claims,"required_claims_by_slide":required_claims_by_slide,"confirmed_design_intent":design_intent,"confirmed_shared_assets":shared_assets},
                )
                generation_batches.append({"slide_ids":list(batch),**batch_generation})
                generated.update(self._slide_fragments(self.technical_gate.validate_candidate_html(partial,batch,assets)))
            ordered={**generated,**sample_fragments}
            shell=render(data["markdown"],ids,meta.get("global_rules",[]),meta.get("local_exceptions",{}),assets,contract_value,contract["hash"],design_intent=design_intent,shared_assets=shared_assets)
            html_text=self._replace_slide_fragments(shell,ordered)
        html_text=self.technical_gate.validate_candidate_html(html_text,ids,assets); deck_fragments=self._slide_fragments(html_text)
        # Confirmed fragments are immutable and are merged by the server for
        # every builder implementation, including deterministic test/fallback
        # builders.
        html_text=self._replace_slide_fragments(html_text,sample_fragments)
        generation_attempt_hashes=[item for batch in generation_batches for item in batch.get("attempt_evidence_hashes",[])]
        html_text,gate=self._post_render_gate(task_id,html_text,ids,contract,ledger,assets,generation_attempt_hashes); deck_fragments=self._slide_fragments(html_text)
        preserved={sid:digest(deck_fragments[sid].encode())==digest(fragment.encode()) for sid,fragment in sample_fragments.items()}
        if not all(preserved.values()): raise ConflictError("确认样品发生未提示变化")
        return self._commit_candidate_deck(task_id,html_text,outline,{"parent":token["parent_deck_hash"],"summary":"生成未检查候选稿","scope":"global","affected":ids,"sample_hash":sample["hash"],"sample_pages_preserved":preserved,"outline_consistent":True,"inspection_status":"pending","global_rules":meta.get("global_rules",[]),"local_exceptions":meta.get("local_exceptions",{}),"generation_batches":generation_batches,"design_intent":design_intent,"shared_design_assets":shared_assets,"presentation_technical_contract_hash":contract["hash"],"design_contract_hash":contract["hash"],"claim_ledger_hash":ledger["hash"],"post_render_gate":gate},"deck_generate",token)
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
        contract,ledger=self._bound_deck_contracts(task_id,deck)
        # Content edits prepare a new outline version but retain the frozen visual
        # contract and Claim Ledger until that cross-artifact transaction commits.
        if deck["metadata"].get("design_contract_hash")!=contract["hash"] or deck["metadata"].get("claim_ledger_hash")!=ledger["hash"]:
            raise ConflictError("候选全稿未绑定当前 PresentationTechnicalContract 或 Claim Ledger")
        contract_value={key:value for key,value in contract.items() if key!="hash"}
        ledger_value={key:value for key,value in ledger.items() if key!="hash"}
        required_claims=self._required_claims_verbatim(task_id,all_ids,contract,ledger)
        required_claims_by_slide=self._required_claims_by_slide(task_id,all_ids,ledger)
        deck_slides=self._slide_fragments(deck["html"])
        previous_slides="".join(deck_slides.get(sid,"") for sid in all_ids)
        prior_intent=validate_design_intent(deck["metadata"].get("design_intent"))
        prior_assets=validate_shared_design_assets(deck["metadata"].get("shared_design_assets"))
        core_evidence=None; core_modified=set(); core_contract_hashes={}
        core_deck=deck["metadata"].get("generation_core")
        if (self.generation_pipeline is not None and core_deck
            and core_deck.get("contract_name")==HtmlDeckSpec.TITLE
            and core_deck.get("generation_mode")=="agent_html"):
            context=self._generation_context(task_id)
            brief=self._generation_core_brief(task_id,context=context)
            core_result=self.generation_pipeline.modify_deck(
                task_id,brief,core_deck["checkpoint_id"],prompt,
                scope=inferred,slide_ids=affected,element_id=element_id,change_type=change_type,
                current_fragments=deck_slides,
                authoritative_outline={"outline_hash":outline_hash,"markdown":markdown},
                context=context,
            )
            source=self._generation_core_preview_html(core_result.artifact,brief,assets)
            prior_intent=validate_design_intent(core_result.value.design_intent)
            prior_assets=validate_shared_design_assets({"css":core_result.value.shared_css})
            core_evidence=self._generation_core_evidence(core_result)
            core_modified=set(core_result.checkpoint.metadata.get("modified_slide_ids",[]))
            core_contract_hashes=core_result.checkpoint.metadata.get("page_contract_hashes",{})
            prior_contract_hashes=deck["metadata"].get("page_contract_hashes",{})
            core_contract_hashes={
                slide_id:(core_contract_hashes.get(slide_id) if slide_id in core_modified else prior_contract_hashes.get(slide_id,core_contract_hashes.get(slide_id)))
                for slide_id in all_ids
                if core_contract_hashes.get(slide_id) or prior_contract_hashes.get(slide_id)
            }
            generation={**core_evidence,"attempts":1,"retry_count":core_result.checkpoint.metadata.get("recovery_count",0),"max_attempts":2,"design_intent":prior_intent,"shared_assets":prior_assets,"validation_hash":core_result.validation.evidence_hash,"renderer_version":core_result.artifact.renderer_version}
        elif isinstance(self.builder,FakeHtmlBuilder):
            source=render(markdown,all_ids,rules,exceptions,assets,contract_value,contract["hash"],design_intent=prior_intent,shared_assets=prior_assets)
            generation={"attempts":1,"retry_count":0,"max_attempts":1,"design_intent":prior_intent,"shared_assets":prior_assets}
        else:
            source,generation=self._build_with_technical_retry(
                task_id,markdown,action="deck",slide_ids=all_ids,assets=assets,
                context={"rules":rules,"exceptions":exceptions,"previous_slides":previous_slides,"prompt":prompt,"scope":inferred,"affected_slide_ids":affected,"element_id":element_id,"design_contract":contract_value,"design_contract_hash":contract["hash"],"claim_ledger":ledger_value,"claim_ledger_hash":ledger["hash"],"required_claims_verbatim":required_claims,"required_claims_by_slide":required_claims_by_slide,"design_intent":prior_intent,"shared_assets":prior_assets},
            )
        html_text,gate=self._post_render_gate(task_id,source,all_ids,contract,ledger,assets,generation.get("attempt_evidence_hashes"))
        before=deck["metadata"]["page_hashes"]; after={sid:digest(fragment.encode()) for sid,fragment in self._slide_fragments(html_text).items()}; actual=[sid for sid in all_ids if before[sid]!=after[sid] or sid in core_modified]
        if any(s not in affected for s in actual): raise ConflictError("修改超出声明影响范围")
        # Cross-artifact edits are prepared and validated above. Only successful
        # render/validation may publish the outline and deck versions.
        publish_outline=(lambda:self._record_p3(task_id,"outline",pending_outline[0],pending_outline[1],"outline_edit","user")) if pending_outline else None
        modification_meta={"parent":deck["hash"],"summary":prompt.strip(),"scope":inferred,"change_type":change_type,"affected":actual,"requested_affected":affected,"unchanged":[s for s in all_ids if s not in actual],"scope_understanding":understanding,"element_id":element_id,"outline_consistent":True,"global_rules":rules,"local_exceptions":exceptions,"generation":generation,"design_intent":generation.get("design_intent",prior_intent),"shared_design_assets":generation.get("shared_assets",prior_assets),"presentation_technical_contract_hash":contract["hash"],"design_contract_hash":contract["hash"],"claim_ledger_hash":ledger["hash"],"post_render_gate":gate}
        if core_evidence:
            modification_meta["generation_core"]=core_evidence
            modification_meta["page_contract_hashes"]=core_contract_hashes
        return self._commit_candidate_deck(task_id,html_text,outline_hash,modification_meta,"deck_modify",token,"user",publish_outline)
    def rollback_deck(self,task_id,target_hash):
        with self.store.lock(task_id):
            token=self._candidate_write_token(task_id)
            known={v["hash"] for v in self.versions(task_id,"deck")}
            if target_hash not in known: raise ValidationError("目标全稿版本不存在")
            target=json.loads(self.version(task_id,target_hash)); meta=next(v["metadata"] for v in self.versions(task_id,"deck") if v["hash"]==target_hash)
            current_outline=self._current_version(task_id,"outline"); inconsistent=target["outline_hash"]!=current_outline
        return self._commit_candidate_deck(task_id,meta["html"],target["outline_hash"],{"parent":token["parent_deck_hash"],"rollback_from":target_hash,"summary":f"回退自 {target_hash[:12]}","scope":"global","affected":list(meta["page_hashes"]),"outline_consistent":not inconsistent,"regenerate_required":list(meta["page_hashes"]) if inconsistent else [],"global_rules":meta.get("global_rules",[]),"local_exceptions":meta.get("local_exceptions",{}),"design_contract_hash":meta.get("design_contract_hash"),"claim_ledger_hash":meta.get("claim_ledger_hash"),"post_render_gate":meta.get("post_render_gate"),"page_contract_hashes":meta.get("page_contract_hashes",{})},"deck_rollback",token,"user")

    def inspection_view(self,task_id):
        deck=self.deck_view(task_id)["deck"]
        projection=self._inspection_projection(task_id); current=projection["report"]
        evidence_trace={"valid":False,"reference_count":0,"artifact_hashes":[],"screenshot_hashes":[],"errors":["尚无当前检查报告"]}
        if current and not current["stale"]:
            evidence_trace=self._assert_inspection_evidence(task_id,current,fail_closed=False)
        state=self.get(task_id)
        return {
            "state":state,"deck":deck,"report":current,"reports":projection["reports"],
            "dispositions":projection["dispositions"],"unresolved":projection["unresolved"],
            "blocking_issues":projection["blocking_issues"],
            "delivery_allowed":bool(current and not current["stale"] and not projection["blocking_issues"] and evidence_trace["valid"]),
            "evidence_trace":evidence_trace,"waiting_reason":state.get("waiting_reason"),
        }

    @staticmethod
    def _normalize_inspection_issues(items):
        normalized=[]
        for index,item in enumerate(items):
            if not isinstance(item,dict): raise ValidationError("检查报告 issue 必须是对象")
            identity=inspection_semantic_identity(item); level=identity["level"]
            sources=list(dict.fromkeys(item.get("sources") or [item.get("source","semantic_model")]))
            evidence_refs=list(dict.fromkeys(item.get("evidence_refs") or []))
            if any(source not in INSPECTION_SOURCES for source in sources): raise ValidationError("检查问题来源无效")
            source=max(sources,key=lambda value:INSPECTION_SOURCE_PRIORITY[value])
            source_issues=list(item.get("source_issues") or [])
            normalized_issue={"issue_id":inspection_semantic_issue_id(item),"level":level,"code":item.get("code","quality_issue"),"message":item.get("message","发现质量问题"),"slide_id":identity["slide_id"],"element_id":identity["element_id"],"evidence":item.get("evidence",item.get("message","发现质量问题")),"suggestion":item.get("suggestion","请人工检查并修复"),"source":source,"sources":sources,"evidence_refs":evidence_refs,"source_issues":source_issues}
            normalized.append({**normalized_issue,"severity":TechnicalGate.normalize_inspection_severity(normalized_issue)})
        return normalized

    def _assert_inspection_evidence(self,task_id,report,fail_closed=True):
        errors=[]; artifact_hashes=[]; screenshot_hashes=[]; reference_count=0; cache={}; report_value=None
        report_hash=report.get("hash") if isinstance(report,dict) else None
        try:
            report_records={item["hash"]:item for item in self.versions(task_id,"inspection")}
        except NotFoundError:
            report_records={}
        report_record=report_records.get(report_hash)
        try:
            report_raw=self.version(task_id,report_hash)
            candidate=json.loads(report_raw)
            if not isinstance(candidate,dict): raise ValueError("inspection report is not an object")
            report_value=candidate
        except (NotFoundError,ValidationError,json.JSONDecodeError,UnicodeDecodeError,ValueError):
            errors.append("检查报告内容寻址工件缺失或无效")
            report_raw=None
        if report_value is not None:
            try: InspectionReport.parse(report_value)
            except ValidationError: errors.append("检查报告 schema 或字段语义无效")
            projected={key:report.get(key) for key in report_value} if isinstance(report,dict) else {}
            if (report_record is None or not isinstance(report_hash,str) or digest(report_raw)!=report_hash
                or report_raw!=canonical(report_value) or projected!=report_value):
                errors.append("检查报告哈希重算或投影不一致")
            metadata=report.get("metadata",{}) if isinstance(report,dict) else {}
            if (report_record is None or metadata!=report_record.get("metadata")
                or metadata.get("deck_hash")!=report_value.get("deck_hash")
                or report_value.get("task_id")!=task_id):
                errors.append("检查报告任务、deck 或版本元数据绑定不一致")
        else:
            metadata=report.get("metadata",{}) if isinstance(report,dict) else {}
        try:
            records={item["hash"]:item for item in self.versions(task_id,"inspection-evidence")}
        except NotFoundError:
            records={}
        def resolve(evidence_hash,label):
            if evidence_hash in cache: return cache[evidence_hash]
            record=records.get(evidence_hash)
            try:
                raw=self.version(task_id,evidence_hash); value=json.loads(raw)
            except (NotFoundError,ValidationError,json.JSONDecodeError,UnicodeDecodeError):
                errors.append(f"{label}:evidence 工件缺失或无效"); cache[evidence_hash]=None; return None
            source=value.get("source")
            if (record is None or digest(raw)!=evidence_hash or raw!=canonical(value)
                or set(value)!={"schema_version","source","deck_hash","payload"}
                or value.get("schema_version")!="1.0" or value.get("deck_hash")!=(report_value or report).get("deck_hash")
                or source not in INSPECTION_SOURCES or not isinstance(value.get("payload"),dict)
                or not isinstance(value["payload"].get("issues",[]),list)
                or record["metadata"].get("source")!=source or record["metadata"].get("deck_hash")!=(report_value or report).get("deck_hash")
                or record["metadata"].get("immutable") is not True):
                errors.append(f"{label}:evidence 哈希或绑定不一致"); cache[evidence_hash]=None; return None
            cache[evidence_hash]=value; return value

        declared=(report_value or {}).get("evidence_artifacts") if report_value is not None else metadata.get("evidence_artifacts")
        if not isinstance(declared,dict) or not declared:
            errors.append("报告缺少 evidence_artifacts 清单")
            declared={}
        if metadata.get("evidence_artifacts")!=declared:
            errors.append("检查报告 evidence 清单与版本元数据不一致")
        documents={}
        for source,evidence_hash in declared.items():
            if source not in INSPECTION_SOURCES or not re.fullmatch(r"[0-9a-f]{64}",str(evidence_hash)):
                errors.append("报告 evidence_artifacts 清单格式无效")
                continue
            artifact_hashes.append(evidence_hash)
            document=resolve(evidence_hash,"报告")
            if not document or document.get("source")!=source:
                errors.append(f"报告:{source} evidence 绑定不一致")
            else:
                documents[source]=document

        expected_issues=[]
        try:
            source_order=("semantic_deterministic","semantic_model","technical_browser")
            merged=merge_inspection_source_issues([
                (source,documents[source]["payload"].get("issues",[])) for source in source_order if source in documents
            ])
            for item in merged:
                origins=item.pop("source_issue_ids")
                sources=list(dict.fromkeys(origin["source"] for origin in origins))
                source_issues=[]
                for origin in origins:
                    evidence_hash=declared.get(origin["source"])
                    ref=f"inspection-evidence://{evidence_hash}"
                    reference_count+=1; artifact_hashes.append(evidence_hash)
                    source_issues.append({**origin,"evidence_ref":ref})
                expected_issues.append({**item,"sources":sources,"evidence_refs":list(dict.fromkeys(origin["evidence_ref"] for origin in source_issues)),"source_issues":source_issues})
            expected_issues=self._normalize_inspection_issues(expected_issues)
        except (ValidationError,TypeError):
            errors.append("evidence payload issue 集结构无效")
            expected_issues=[]
        if report_value is not None:
            if report_value.get("issues")!=expected_issues:
                errors.append("检查报告 issue 集与 evidence payload 双向绑定不一致")
            # The quality report may remain red and drive bounded Agent
            # self-correction even when its findings are advisory to delivery.
            expected_passed=inspection_hard_gate_passed(expected_issues) and all(bool(document["payload"].get("passed",inspection_hard_gate_passed(document["payload"].get("issues",[])))) for document in documents.values())
            if report_value.get("passed") is not expected_passed:
                errors.append("检查报告 passed 与 evidence payload 不一致")
            expected_sources={item["issue_id"]:list(item["sources"]) for item in expected_issues}
            expected_origins={item["issue_id"]:list(item["source_issues"]) for item in expected_issues}
            if metadata.get("issue_sources")!=expected_sources or metadata.get("issue_origins")!=expected_origins:
                errors.append("检查报告 issue 来源元数据绑定不一致")
        visual_quality=None
        browser_document=documents.get("technical_browser")
        if browser_document is not None:
            candidate=browser_document["payload"].get("visual_quality")
            if candidate is not None:
                visual_quality=candidate
                screenshots=candidate.get("screenshots") if isinstance(candidate,dict) else None
                if not isinstance(screenshots,list):
                    errors.append("视觉质量截图清单无效")
                    screenshots=[]
                deck_hash=(report_value or report).get("deck_hash")
                try:
                    deck_record=next(item for item in self.versions(task_id,"deck") if item["hash"]==deck_hash)
                    page_hashes=deck_record["metadata"].get("page_hashes")
                    if not isinstance(page_hashes,dict) or not page_hashes: raise ValueError("deck page order is unavailable")
                    expected_slides=list(page_hashes)
                except (NotFoundError,StopIteration,TypeError,ValueError):
                    errors.append("视觉质量截图缺少可验证的 deck 页面顺序")
                    expected_slides=[]
                try:
                    screenshot_records={item["hash"]:item for item in self.versions(task_id,"inspection-screenshot")}
                except NotFoundError:
                    screenshot_records={}
                seen_slides=[]
                for slide_index,item in enumerate(screenshots):
                    if not isinstance(item,dict):
                        errors.append("视觉质量截图引用无效")
                        continue
                    screenshot_hash=str(item.get("sha256") or "")
                    evidence_ref=str(item.get("evidence_ref") or "")
                    slide_id=str(item.get("slide_id") or "")
                    record=screenshot_records.get(screenshot_hash)
                    try:
                        raw=self.version(task_id,screenshot_hash)
                    except (NotFoundError,ValidationError):
                        raw=None
                    if (not re.fullmatch(r"[0-9a-f]{64}",screenshot_hash)
                        or evidence_ref!=f"inspection-screenshot://{screenshot_hash}"
                        or item.get("deck_hash")!=deck_hash or item.get("slide_index")!=slide_index
                        or item.get("media_type")!="image/webp" or item.get("width")!=1280 or item.get("height")!=720
                        or raw is None or digest(raw)!=screenshot_hash or len(raw)!=item.get("byte_size")
                        or not raw.startswith(b"RIFF") or raw[8:12]!=b"WEBP"
                        or record is None or record["metadata"]!={"media_type":"image/webp","immutable":True}):
                        errors.append(f"视觉质量截图哈希或绑定不一致:{slide_id or 'unknown'}")
                        continue
                    screenshot_hashes.append(screenshot_hash); seen_slides.append(slide_id); reference_count+=1
                measured_slides=[str(item.get("slide_id") or "") for item in browser_document["payload"].get("slides",[]) if isinstance(item,dict)]
                scored_slides=[str(item.get("slide_id") or "") for item in candidate.get("slides",[]) if isinstance(item,dict)] if isinstance(candidate,dict) else []
                if seen_slides!=expected_slides or measured_slides!=expected_slides or scored_slides!=expected_slides:
                    errors.append("视觉质量截图没有完整覆盖 deck 页面顺序")
        result={"valid":not errors,"reference_count":reference_count,"artifact_hashes":sorted(set(artifact_hashes)),"screenshot_hashes":sorted(set(screenshot_hashes)),"visual_quality":visual_quality,"errors":list(dict.fromkeys(errors))}
        if errors and fail_closed:
            raise ConflictError("检查报告与 evidence 溯源失败："+"；".join(errors[:3]))
        return result

    def _call_inspector(self,outline,html_text,browser_evidence):
        method=self.inspector.inspect
        parameters=inspect.signature(method).parameters
        accepts_evidence="browser_evidence" in parameters or any(
            item.kind==inspect.Parameter.VAR_KEYWORD for item in parameters.values()
        )
        return method(outline,html_text,browser_evidence=browser_evidence) if accepts_evidence else method(outline,html_text)

    def _call_browser_inspector(self,html_text,slide_ids,*,visual_quality=False):
        method=self.browser_inspector.inspect
        parameters=inspect.signature(method).parameters
        supports_visual="visual_quality" in parameters or any(
            item.kind==inspect.Parameter.VAR_KEYWORD for item in parameters.values()
        )
        return method(html_text,slide_ids,visual_quality=True) if visual_quality and supports_visual else method(html_text,slide_ids)

    def _prepare_inspection_result(self,task_id,deck):
        outline=json.loads(self.version(task_id,deck["outline_hash"]))["markdown"]
        input_view=self.input_view(task_id)
        ledger=self._ensure_claim_ledger(task_id,input_view)
        ledger_value={key:value for key,value in ledger.items() if key!="hash"}
        content_evidence=inspect_content_quality(deck["html"],{
            "source":input_view.get("source"),
            "task_card":input_view.get("task_card"),
        })
        ledger_evidence=audit_html_claims(deck["html"],ledger_value)
        bound_values={item["value"] for item in ledger_evidence["bindings"] if item["status"] in {"bound","derived"}}
        content_evidence["issues"]=[
            item for item in content_evidence.get("issues",[])
            if item.get("code") not in {"unverified_critical_fact","unverified_fact"}
            or not any(value in item.get("evidence","") for value in bound_values)
        ]
        for item in ledger_evidence["unbound"]:
            identity=f"{item['kind']}\0{item['normalized_value']}"
            content_evidence["issues"].append({
                "issue_id":f"claim-unbound-{hashlib.sha256(identity.encode()).hexdigest()[:16]}",
                "severity":"blocker","level":"deck","code":"unbound_claim",
                "message":"演示稿包含未绑定事实","slide_id":"","element_id":"",
                "evidence":f"Claim Ledger 未绑定：{item['value']}",
                "suggestion":"绑定来源 claim、记录可审计公式，或删除该事实",
                "source":"semantic_deterministic",
            })
        content_evidence.update({
            "passed":not content_evidence["issues"],
            "claim_ledger_hash":ledger["hash"],
            "claim_binding_count":ledger_evidence["binding_count"],
            "unbound_claim_count":ledger_evidence["unbound_count"],
        })
        browser_evidence=None
        visual_screenshots=[]
        if self.browser_inspector is not None:
            browser_evidence=self._call_browser_inspector(deck["html"],list(deck["metadata"]["page_hashes"]),visual_quality=True)
            if isinstance(browser_evidence,dict):
                visual_screenshots=browser_evidence.pop("_visual_screenshots",[])
        raw=self._call_inspector(outline,deck["html"],browser_evidence)
        if not isinstance(raw,dict): raise ValidationError("检查响应契约无效")
        model_issues=raw.get("issues",[])
        if not isinstance(model_issues,list): raise ValidationError("检查响应 issues 必须是数组")
        browser_issues=(browser_evidence or {}).get("issues",[])
        content_issues=content_evidence.get("issues",[])
        sources=(
            ("semantic_deterministic",content_issues),
            ("semantic_model",model_issues),
            ("technical_browser",browser_issues),
        )
        combined=merge_inspection_source_issues(sources)
        merged_counts={source:sum(source in item["sources"] for item in combined) for source,_ in sources}
        browser_passed=True if browser_evidence is None else not TechnicalGate.browser_blockers(browser_evidence)
        if isinstance(browser_evidence,dict):
            # Reassert the server-owned hard-gate meaning even when a browser
            # adapter reports ``passed=false`` solely because it emitted an
            # advisory visual warning.
            browser_evidence["passed"]=browser_passed
        checks={
            "semantic_deterministic":{"available":True,"passed":bool(content_evidence.get("passed")),"issue_count":merged_counts["semantic_deterministic"],"raw_issue_count":len(content_issues),"evidence_hash":fingerprint(content_evidence)},
            "semantic_model":{"available":True,"passed":bool(raw.get("passed",not model_issues)),"issue_count":merged_counts["semantic_model"],"raw_issue_count":len(model_issues),"model":raw.get("model","unknown")},
            "technical_browser":{"available":browser_evidence is not None and bool(browser_evidence.get("available")),"passed":browser_passed,"issue_count":merged_counts["technical_browser"],"raw_issue_count":len(browser_issues),**({"visual_quality":{"score":browser_evidence["visual_quality"].get("score"),"grade":browser_evidence["visual_quality"].get("grade"),"screenshot_count":len(browser_evidence["visual_quality"].get("screenshots",[]))}} if isinstance(browser_evidence,dict) and isinstance(browser_evidence.get("visual_quality"),dict) else {})},
        }
        evidence_documents=[
            {"schema_version":"1.0","source":"semantic_deterministic","deck_hash":deck["hash"],"payload":content_evidence},
            {"schema_version":"1.0","source":"semantic_model","deck_hash":deck["hash"],"payload":{"model":raw.get("model","unknown"),"passed":bool(raw.get("passed",not model_issues)),"issues":model_issues}},
        ]
        if browser_evidence is not None:
            evidence_documents.append({"schema_version":"1.0","source":"technical_browser","deck_hash":deck["hash"],"payload":browser_evidence})
        return {
            **raw,
            "issues":combined,
            "passed":bool(raw.get("passed",not model_issues)) and bool(content_evidence.get("passed")) and browser_passed and inspection_hard_gate_passed(combined),
            "content_evidence":content_evidence,
            "quality_checks":checks,
            "evidence_documents":evidence_documents,
            "_visual_screenshots":visual_screenshots,
            **({"browser_evidence":browser_evidence} if browser_evidence is not None else {}),
        }

    def _inspect_once(self,task_id,scope,affected,round_number,prepared_raw=None):
        deck=self.deck_view(task_id)["deck"]
        if not deck: raise ConflictError("尚未生成全稿")
        # Deliberately pass only the original outline, review HTML and local
        # Chromium measurements. Generation dialogue, model self-description,
        # resources and screenshot pixels never cross this boundary.
        skill=self.skills.load("inspection")
        raw=prepared_raw if prepared_raw is not None else self._prepare_inspection_result(task_id,deck)
        visual_screenshots=raw.pop("_visual_screenshots",[])
        browser_payload=raw.get("browser_evidence")
        visual_quality=browser_payload.get("visual_quality") if isinstance(browser_payload,dict) else None
        if visual_quality is not None:
            if not isinstance(visual_quality,dict) or not isinstance(visual_screenshots,list):
                raise ValidationError("视觉质量截图缺少 Chromium 评分绑定")
            declared=visual_quality.get("screenshots")
            page_order=list(deck["metadata"]["page_hashes"])
            if (not isinstance(declared,list) or len(declared)!=len(page_order)
                or len(visual_screenshots)!=len(page_order)):
                raise ValidationError("视觉质量截图没有完整覆盖候选页面顺序")
            candidates=[]
            for slide_index,(expected,item) in enumerate(zip(declared,visual_screenshots)):
                slide_id=page_order[slide_index]
                if (not isinstance(expected,dict) or not isinstance(item,dict)
                    or not isinstance(item.get("content"),(bytes,bytearray))):
                    raise ValidationError("视觉质量截图内容无效")
                content=bytes(item["content"])
                if (str(expected.get("slide_id") or "")!=slide_id or str(item.get("slide_id") or "")!=slide_id
                    or expected.get("media_type")!="image/webp" or expected.get("width")!=1280 or expected.get("height")!=720
                    or expected.get("sha256")!=digest(content) or expected.get("byte_size")!=len(content)
                    or not content.startswith(b"RIFF") or content[8:12]!=b"WEBP"):
                    raise ValidationError("视觉质量截图哈希、格式或候选绑定无效")
                candidates.append((slide_index,slide_id,expected,content))
            references=[]
            for slide_index,slide_id,expected,content in candidates:
                # The artifact metadata describes the bytes only.  Deck/page
                # identity belongs to the immutable reference in the evidence
                # document so identical screenshots can be shared by pages.
                screenshot_hash=self.store.put_version(task_id,"inspection-screenshot",content,{"media_type":"image/webp","immutable":True})
                references.append({**expected,"deck_hash":deck["hash"],"slide_index":slide_index,"evidence_ref":f"inspection-screenshot://{screenshot_hash}"})
            visual_quality["screenshots"]=references
        elif visual_screenshots:
            raise ValidationError("视觉质量截图缺少 Chromium 评分绑定")
        documents=raw.get("evidence_documents")
        if not isinstance(documents,list):
            model_issues=raw.get("issues",[])
            documents=[{"schema_version":"1.0","source":"semantic_model","deck_hash":deck["hash"],"payload":{"model":raw.get("model","unknown"),"passed":bool(raw.get("passed",not model_issues)),"issues":model_issues}}]
        evidence_refs={}; source_payloads=[]
        for document in documents:
            if (not isinstance(document,dict) or set(document)!={"schema_version","source","deck_hash","payload"}
                or document.get("schema_version")!="1.0" or document.get("deck_hash")!=deck["hash"]
                or document.get("source") not in INSPECTION_SOURCES
                or not isinstance(document.get("payload"),dict)):
                raise ValidationError("检查 evidence 工件结构或候选绑定无效")
            source=document["source"]
            if source in evidence_refs: raise ValidationError("检查 evidence 来源重复")
            payload_issues=document["payload"].get("issues",[])
            if not isinstance(payload_issues,list): raise ValidationError("检查 evidence payload issues 必须是数组")
            raw_evidence=canonical(document)
            evidence_hash=self.store.put_version(task_id,"inspection-evidence",raw_evidence,{"source":source,"deck_hash":deck["hash"],"immutable":True})
            if evidence_hash!=digest(raw_evidence): raise ConflictError("检查 evidence 内容寻址持久化失败")
            evidence_refs[source]=f"inspection-evidence://{evidence_hash}"
            source_payloads.append((source,payload_issues))
        enriched=[]
        for item in merge_inspection_source_issues(source_payloads):
            origins=item.pop("source_issue_ids")
            sources=list(dict.fromkeys(origin["source"] for origin in origins))
            source_issues=[{**origin,"evidence_ref":evidence_refs[origin["source"]]} for origin in origins]
            enriched.append({**item,"sources":sources,"evidence_refs":list(dict.fromkeys(origin["evidence_ref"] for origin in source_issues)),"source_issues":source_issues})
        issues=self._normalize_inspection_issues(enriched)
        passed=inspection_hard_gate_passed(issues) and all(bool(document["payload"].get("passed",inspection_hard_gate_passed(document["payload"].get("issues",[])))) for document in documents)
        evidence_artifacts={source:ref.removeprefix("inspection-evidence://") for source,ref in evidence_refs.items()}
        created=utcnow(); seed=canonical({"deck_hash":deck["hash"],"issues":issues,"evidence_artifacts":evidence_artifacts,"created_at":created})
        report=InspectionReport.parse({"report_id":f"report-{digest(seed)[:16]}","task_id":task_id,"deck_hash":deck["hash"],"issues":issues,"passed":passed,"created_at":created,"evidence_artifacts":evidence_artifacts,"schema_version":"1.0"})
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
            **({"visual_quality":{"score":browser["visual_quality"].get("score"),"grade":browser["visual_quality"].get("grade"),"composition_score":browser["visual_quality"].get("composition_score"),"layout_diversity_score":browser["visual_quality"].get("layout_diversity_score"),"theme_rhythm_score":browser["visual_quality"].get("theme_rhythm_score"),"screenshot_count":len(browser["visual_quality"].get("screenshots",[])),"screenshot_hashes":[item.get("sha256") for item in browser["visual_quality"].get("screenshots",[])]}} if isinstance(browser.get("visual_quality"),dict) else {}),
        }
        content_meta=None if not isinstance(content,dict) else {
            "available":bool(content.get("available")),
            "passed":bool(content.get("passed")),
            "issue_count":len(content.get("issues",[])),
            "visible_text_hash":content.get("visible_text_hash"),
            "source_binding_hash":content.get("source_binding_hash"),
            "claim_ledger_hash":content.get("claim_ledger_hash"),
            "claim_binding_count":content.get("claim_binding_count",0),
            "unbound_claim_count":content.get("unbound_claim_count",0),
            "evidence_hash":fingerprint(content),
        }
        metadata={"deck_hash":deck["hash"],"scope":scope,"affected_slide_ids":affected,"includes_deck_consistency":True,"includes_content_quality":content_meta is not None,"includes_browser_render":browser_meta is not None,"content_evidence":content_meta,"browser_evidence":browser_meta,"quality_checks":raw.get("quality_checks",{}),"issue_sources":{item["issue_id"]:list(item["sources"]) for item in issues},"issue_origins":{item["issue_id"]:list(item["source_issues"]) for item in issues},"evidence_artifacts":evidence_artifacts,"round":round_number,"model":raw.get("model","unknown"),"skill":{"action":"inspection","version":skill["version"],"hash":digest(skill["content"].encode()),"files":skill.get("files",[])},"input_fields":["original_outline","html","frozen_source_binding","design_contract","claim_ledger",*(["browser_evidence"] if browser_meta is not None else [])],"excluded_fields":["generation_context","self_description","source_images","screenshot_pixels"],"design_contract_hash":deck["metadata"].get("design_contract_hash"),"claim_ledger_hash":deck["metadata"].get("claim_ledger_hash"),"post_render_gate":deck["metadata"].get("post_render_gate")}
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
        contract,ledger=self._bound_deck_contracts(task_id,deck)
        contract_value={key:value for key,value in contract.items() if key!="hash"}
        ledger_value={key:value for key,value in ledger.items() if key!="hash"}
        suggestions=[{"issue_id":i["issue_id"],"slide_id":i["slide_id"],"element_id":i["element_id"],"code":i["code"],"suggestion":i["suggestion"]} for i in selected]
        if isinstance(self.builder,FakeHtmlBuilder):
            rules=list(deck["metadata"].get("global_rules",[])); exceptions={k:list(v) for k,v in deck["metadata"].get("local_exceptions",{}).items()}
            for slide_id in affected: exceptions.setdefault(slide_id,[]).append(f"检查修复第 {round_number} 轮："+"；".join(s["suggestion"] for s in suggestions if s["slide_id"]==slide_id))
            html_text=render(outline,list(deck["metadata"]["page_hashes"]),rules,exceptions,assets,contract_value,contract["hash"])
        else:
            html_text=self.builder.build(outline,action="inspection",slide_ids=list(deck["metadata"]["page_hashes"]),assets=assets,previous_html=deck["html"],inspection_report=report,suggestions=suggestions,affected_slide_ids=affected,design_contract=contract_value,design_contract_hash=contract["hash"],claim_ledger=ledger_value,claim_ledger_hash=ledger["hash"])
        html_text=self.technical_gate.validate_candidate_html(html_text,list(deck["metadata"]["page_hashes"]),assets)
        candidate_slides=self._slide_fragments(html_text)
        html_text=self._replace_slide_fragments(deck["html"],{slide_id:candidate_slides[slide_id] for slide_id in affected})
        html_text,gate=self._post_render_gate(task_id,html_text,list(deck["metadata"]["page_hashes"]),contract,ledger,assets)
        metadata={"parent":deck["hash"],"summary":f"自动修复第 {round_number} 轮","scope":"page","affected":affected,"outline_consistent":True,"global_rules":deck["metadata"].get("global_rules",[]),"local_exceptions":deck["metadata"].get("local_exceptions",{}),"inspection_report_hash":report["hash"],"inspection_issue_ids":[item["issue_id"] for item in selected],"auto_fix_round":round_number,"design_contract_hash":contract["hash"],"claim_ledger_hash":ledger["hash"],"post_render_gate":gate}
        return html_text,deck["outline_hash"],metadata,token

    def _auto_fix(self,task_id,report,round_number,prepared=None):
        html_text,outline_hash,metadata,token=prepared or self._prepare_auto_fix(task_id,report,round_number)
        return self._commit_candidate_deck(task_id,html_text,outline_hash,metadata,"deck_auto_fix",token,"system")["deck"]

    def _try_overflow_autofit(self,task_id,report):
        """Deterministic geometric repair ahead of any LLM fix round.

        Returns the fresh post-repair inspection report when geometric
        blockers existed and at least one rule was committed; ``None`` when
        there was nothing for the deterministic path to do.
        """
        if self.browser_inspector is None: return None
        geometric=[item for item in report["issues"] if item["severity"]=="blocker" and item["code"] in GEOMETRIC_CODES]
        if not geometric: return None
        with self.store.lock(task_id):
            token=self._candidate_write_token(task_id)
            deck=self.deck_view(task_id)["deck"]
        result=fit_deck_html(deck["html"],max_rounds=MAX_CASCADE_ROUNDS)
        if not result["available"] or not result["rules"]: return None
        contract,ledger=self._bound_deck_contracts(task_id,deck)
        assets=controlled_assets(self.input_view(task_id)["manifest"],self.store.resource_root(task_id))
        html_text,gate=self._post_render_gate(task_id,result["html"],list(deck["metadata"]["page_hashes"]),contract,ledger,assets)
        metadata={"parent":deck["hash"],"summary":f"确定性溢出修复 {len(result['rules'])} 处（{result['rounds']} 轮）","scope":"global","affected":list(deck["metadata"]["page_hashes"]),"outline_consistent":True,"overflow_autofit":{"rules":result["rules"],"rounds":result["rounds"],"converged":result["converged"],"remaining":result["remaining"]},"global_rules":deck["metadata"].get("global_rules",[]),"local_exceptions":deck["metadata"].get("local_exceptions",{}),"design_contract_hash":contract["hash"],"claim_ledger_hash":ledger["hash"],"post_render_gate":gate}
        self._commit_candidate_deck(task_id,html_text,deck["outline_hash"],metadata,"deck_overflow_autofit",token,"system")
        return self._inspect_once(task_id,"full",list(deck["metadata"]["page_hashes"]),0)

    def autofit_overflow(self,task_id,max_rounds=2):
        """User-triggered deterministic overflow repair from the review page."""
        state=self._require_candidate_mutable(task_id)
        if isinstance(max_rounds,bool) or not isinstance(max_rounds,int) or max_rounds<1 or max_rounds>5: raise ValidationError("max_rounds 必须为 1 到 5 的整数")
        deck=self.deck_view(task_id)["deck"]
        if not deck: raise ConflictError("尚未生成全稿")
        if state.stage==state.stage.DECK: self.command(task_id,f"to-review-{self._current_version(task_id,'deck')[:12]}","advance","system")
        with self.store.lock(task_id):
            token=self._candidate_write_token(task_id)
            deck=self.deck_view(task_id)["deck"]
        result=fit_deck_html(deck["html"],max_rounds=max_rounds)
        if not result["available"]: raise ConflictError("Chromium 渲染不可用，无法执行确定性溢出修复")
        if not result["rules"]:
            return {**self.inspection_view(task_id),"autofit":{"applied":0,"rounds":0,"converged":result["converged"],"remaining":result["remaining"]}}
        contract,ledger=self._bound_deck_contracts(task_id,deck)
        assets=controlled_assets(self.input_view(task_id)["manifest"],self.store.resource_root(task_id))
        html_text,gate=self._post_render_gate(task_id,result["html"],list(deck["metadata"]["page_hashes"]),contract,ledger,assets)
        metadata={"parent":deck["hash"],"summary":f"确定性溢出修复 {len(result['rules'])} 处（{result['rounds']} 轮）","scope":"global","affected":list(deck["metadata"]["page_hashes"]),"outline_consistent":True,"overflow_autofit":{"rules":result["rules"],"rounds":result["rounds"],"converged":result["converged"],"remaining":result["remaining"]},"global_rules":deck["metadata"].get("global_rules",[]),"local_exceptions":deck["metadata"].get("local_exceptions",{}),"design_contract_hash":contract["hash"],"claim_ledger_hash":ledger["hash"],"post_render_gate":gate}
        self._commit_candidate_deck(task_id,html_text,deck["outline_hash"],metadata,"deck_overflow_autofit",token,"system")
        report=self._inspect_once(task_id,"full",list(deck["metadata"]["page_hashes"]),0)
        return {**self.inspection_view(task_id),"autofit":{"applied":len(result["rules"]),"rounds":result["rounds"],"converged":result["converged"],"remaining":result["remaining"],"report_hash":report["hash"]}}

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
            if state.mode in {"auto","quick"} and max_rounds>=1 and not report["passed"]:
                # 纯几何溢出先做确定性自适应，不消耗模型修复轮次；修复成功
                # 则直接进入人工确认，残留问题再走既有 LLM 有界修复。
                fitted=self._try_overflow_autofit(task_id,report)
                if fitted is not None: report=fitted
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

    @staticmethod
    def _inspection_issue_by_id(report,issue_id):
        matches=[item for item in report.get("issues",[]) if item.get("issue_id")==issue_id or any(origin.get("issue_id")==issue_id for origin in item.get("source_issues",[]))]
        if len(matches)>1: raise ValidationError("来源原始问题 ID 对应多个语义问题，请使用服务端 issue_id")
        return matches[0] if matches else None

    def dispose_issue(self,task_id,issue_id,action,rationale,actor="user"):
        self._require_candidate_mutable(task_id)
        view=self.inspection_view(task_id); report=view["report"]
        if not report or report["stale"]: raise ConflictError("当前 HTML 版本没有有效检查报告")
        self._assert_inspection_evidence(task_id,report)
        issue=self._inspection_issue_by_id(report,issue_id)
        if not issue: raise ValidationError("检查问题不存在")
        issue_id=issue["issue_id"]
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
            return self.store.put_version(task_id,"issue-disposition",canonical(model.to_dict()),{"report_hash":report["hash"],"severity":issue["severity"],"code":issue["code"],"sources":list(issue.get("sources") or [issue.get("source","semantic_model")]),"evidence_refs":list(issue.get("evidence_refs") or []),"sequence":len(self.versions(task_id,"issue-disposition"))+1})
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
        report=view.get("report") or {}
        resolved=[self._inspection_issue_by_id(report,issue_id) for issue_id in issue_ids]
        if any(item is None for item in resolved): raise ValidationError("批量范围包含未知问题")
        stable_ids=[item["issue_id"] for item in resolved]
        if len(set(stable_ids))!=len(stable_ids): raise ValidationError("批量范围包含同一语义问题的重复 ID")
        if len({item["code"] for item in resolved})!=1: raise ValidationError("批量处置仅支持同 code 的同类问题")
        hashes=[]
        for issue_id in stable_ids:
            result=self.dispose_issue(task_id,issue_id,action,rationale); hashes.append(result["disposition_hash"])
        return {**self.inspection_view(task_id),"disposition_hashes":hashes,"batch_scope":stable_ids}

    def _assert_current_post_render_gate(self,task_id):
        self.require_release_write_enabled()
        deck=self.deck_view(task_id)["deck"]
        if not deck: raise ConflictError("尚未生成全稿")
        contract,ledger=self._bound_deck_contracts(task_id,deck)
        gate=deck["metadata"].get("post_render_gate") or {}
        evidence_hash=gate.get("evidence_hash")
        try:
            evidence_records=self.versions(task_id,"post-render-gate-evidence")
        except NotFoundError as exc:
            raise ConflictError("当前全稿的渲染后门禁 evidence 工件缺失") from exc
        record=next((item for item in evidence_records if item["hash"]==evidence_hash),None)
        if not evidence_hash or record is None:
            raise ConflictError("当前全稿的渲染后门禁 evidence 工件缺失")
        try:
            persisted=self.version(task_id,evidence_hash)
            persisted_value=json.loads(persisted)
        except (NotFoundError,json.JSONDecodeError,UnicodeDecodeError) as exc:
            raise ConflictError("当前全稿的渲染后门禁 evidence 工件缺失或无效") from exc
        if (not isinstance(persisted_value,dict)
            or digest(persisted)!=evidence_hash
            or persisted!=canonical_post_render_evidence(persisted_value)
            or canonical_post_render_evidence(gate)!=persisted
            or post_render_evidence_hash(gate)!=evidence_hash):
            raise ConflictError("当前全稿的渲染后门禁 evidence 哈希重算不一致")
        if (gate.get("rendered_html_hash")!=digest(deck["html"].encode())
            or record["metadata"].get("rendered_html_hash")!=gate.get("rendered_html_hash")
            or record["metadata"].get("design_contract_hash")!=contract["hash"]
            or record["metadata"].get("claim_ledger_hash")!=ledger["hash"]):
            raise ConflictError("当前全稿与渲染后门禁 evidence 绑定不一致")
        valid=(
            deck["metadata"].get("design_contract_hash")==contract["hash"]
            and deck["metadata"].get("claim_ledger_hash")==ledger["hash"]
            and gate.get("gate_type") in {None,"technical_gate"}
            and gate.get("schema_version") in {None,TechnicalGate.schema_version}
            and gate.get("passed") is True
            and gate.get("blocker_count")==0
            and not gate.get("blockers")
            and (gate.get("hard_blocker_codes") is None or gate.get("hard_blocker_codes")==sorted(TechnicalGate.blocker_codes))
            and (gate.get("advisory_count") is None or gate.get("advisory_count")==len(gate.get("advisories",[])))
            and gate.get("layout",{}).get("layout_registration_percent")==100
            and gate.get("geometry",{}).get("overflow_count")==0
        )
        if not valid: raise ConflictError("当前全稿未通过 PresentationTechnicalContract 与 TechnicalGate")
        return {"deck":deck,"design_contract":contract,"claim_ledger":ledger,"post_render_gate":gate}

    def assert_delivery_gate(self,task_id):
        generation=self._assert_current_post_render_gate(task_id)
        view=self.inspection_view(task_id)
        if view["blocking_issues"]: raise ConflictError("仍有未解决且未豁免的阻断问题，禁止交付")
        if not view["report"] or view["report"]["stale"]: raise ConflictError("当前 HTML 版本须先完成检查")
        evidence_trace=self._assert_inspection_evidence(task_id,view["report"])
        return {"delivery_allowed":True,"warnings":[x for x in view["unresolved"] if x["severity"]=="warning"],"inspection_report":view["report"],"inspection_evidence":evidence_trace,**generation}

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

    def finalize_deck(self,task_id,deck_hash,source="deck",actor="user",allow_risk=False,risk_rationale=""):
        if actor!="user": raise ValidationError("终稿必须由用户明确确认")
        if not isinstance(allow_risk,bool): raise ValidationError("allow_risk 必须是布尔值")
        if not isinstance(risk_rationale,str): raise ValidationError("带风险定稿依据必须是字符串")
        with self.store.transaction(task_id):
            self._require_actionable(task_id)
            state=TaskState.parse(self.get(task_id)); deck=self.deck_view(task_id)["deck"]
            if state.stage not in {state.stage.DECK,state.stage.REVIEW}: raise ConflictError("只能在全稿或自检与修改阶段确定终稿")
            if not deck or deck["hash"]!=deck_hash: raise ConflictError("终稿必须绑定当前候选 HTML 版本")
            generation=self._assert_current_post_render_gate(task_id)
            inspection=self.inspection_view(task_id); report=inspection["report"]
            if report and not report["stale"]:
                self._assert_inspection_evidence(task_id,report)
            if not report or report["stale"]:
                inspection_status="unchecked" if not report else "stale"
            elif report["passed"]:
                inspection_status="passed"
            elif not inspection["unresolved"]:
                inspection_status="issues_disposed"
            else:
                inspection_status="issues_remaining"
            blockers=list(inspection["blocking_issues"])
            # 阻断问题未清零时禁止默认定稿；用户仍可显式选择带风险定稿，
            # 但必须留下可追溯依据，且终稿事实与交付元数据明确标注“带风险终稿”。
            if blockers and not allow_risk:
                raise ConflictError(f"仍有 {len(blockers)} 项未处置的阻断问题，禁止默认定稿；请先修复、处置，或显式选择带风险定稿并填写依据")
            finalization_mode="standard"
            if blockers and allow_risk:
                if not risk_rationale.strip(): raise ValidationError("带风险定稿必须填写可追溯的风险依据")
                finalization_mode="risk_accepted"
            existing=self.finalization_view(task_id)["current"]
            if existing and existing["inspection_status"]==inspection_status and existing["unresolved_issue_count"]==len(inspection["unresolved"]) and existing["blocking_issue_count"]==len(inspection["blocking_issues"]) and existing.get("finalization_mode","standard")==finalization_mode:
                return {"state":self.get(task_id),"finalization":existing}
            finalized_at=utcnow()
            payload={"finalization_id":f"final-{deck_hash[:16]}","task_id":task_id,"deck_hash":deck_hash,"finalized_by":"user","finalized_at":finalized_at,"source":source if source in {"deck","review"} else "deck","inspection_status":inspection_status,"inspection_report_hash":None if not report or report["stale"] else report["hash"],"unresolved_issue_count":len(inspection["unresolved"]),"blocking_issue_count":len(inspection["blocking_issues"]),"finalization_mode":finalization_mode,"risk_rationale":risk_rationale.strip() if finalization_mode=="risk_accepted" else "","design_contract_hash":generation["design_contract"]["hash"],"claim_ledger_hash":generation["claim_ledger"]["hash"],"post_render_gate_hash":generation["post_render_gate"]["evidence_hash"],"schema_version":"1.0"}
            final_hash=self.store.put_version(task_id,"final-deck",canonical(payload),{"deck_hash":deck_hash,"source":payload["source"],"inspection_status":inspection_status,"finalization_mode":finalization_mode,"design_contract_hash":payload["design_contract_hash"],"claim_ledger_hash":payload["claim_ledger_hash"],"post_render_gate_hash":payload["post_render_gate_hash"]})
            new=TaskState(**{**state.__dict__,"stage":state.stage.DELIVERY,"status":state.status.READY,"waiting_reason":"final_ready","required_action":"publish_delivery","revision":state.revision+1})
            event={"event_id":digest(f"{task_id}:finalize:{deck_hash}".encode())[:24],"command_id":f"finalize-{deck_hash[:16]}","action":"finalize_deck","actor":"user","request_hash":deck_hash,"at":finalized_at,"from":state.to_dict(),"to":new.to_dict(),"result":{"finalization_hash":final_hash,"deck_hash":deck_hash,"inspection_status":inspection_status,"finalization_mode":finalization_mode}}
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
        for kind in ("input-snapshot","claim-ledger","narrative","outline","presentation-technical-contract","sample","deck","inspection","final-deck","delivery"):
            records=self.versions(task_id,kind)
            if not records: continue
            if kind in {"narrative","outline","sample","deck"}: current=self._current_version(task_id,kind)
            elif kind=="input-snapshot": current=next((e["result"].get("snapshot_hash") for e in reversed(self.events(task_id)) if e["action"] in {"import_input","rebuild_input"}),None)
            elif kind=="inspection": current=next((e["result"].get("report_hash") for e in reversed(self.events(task_id)) if e["action"]=="inspection_complete"),None)
            elif kind=="presentation-technical-contract": current=self.deck_view(task_id)["deck"]["metadata"].get("presentation_technical_contract_hash") if self.deck_view(task_id)["deck"] else records[-1]["hash"]
            elif kind=="claim-ledger": current=records[-1]["hash"]
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
        generation=self._assert_current_post_render_gate(task_id)
        design_contract_hash=finalization.get("design_contract_hash")
        claim_ledger_hash=finalization.get("claim_ledger_hash")
        post_render_gate_hash=finalization.get("post_render_gate_hash")
        if (design_contract_hash!=current["metadata"].get("design_contract_hash")
            or claim_ledger_hash!=current["metadata"].get("claim_ledger_hash")
            or post_render_gate_hash!=generation["post_render_gate"].get("evidence_hash")):
            raise ConflictError("终稿与当前 PresentationTechnicalContract、Claim Ledger 或渲染后门禁 evidence 不一致")
        post_render_gate_evidence=self.version(task_id,post_render_gate_hash)
        if state.stage!=state.stage.DELIVERY: raise ConflictError("当前阶段不能交付")
        narrative_hash=self._current_version(task_id,"narrative"); outline_hash=current["outline_hash"]
        snapshot=self.input_view(task_id)
        existing_delivery=next((r for r in self.versions(task_id,"delivery") if json.loads(self.version(task_id,r["hash"]))["deck_hash"]==deck_hash),None)
        delivery_id=(json.loads(self.version(task_id,existing_delivery["hash"]))["delivery_id"] if existing_delivery else f"delivery-{len(self.versions(task_id,'delivery'))+1}-{deck_hash[:12]}")

        # First-time publishes must pass the Chromium-backed delivery gate:
        # a fresh inspection report bound to the current deck with zero
        # unresolved blockers.  Replays of an already-recorded delivery fact
        # stay idempotent and never re-evaluate the gate.
        delivery_gate=None
        if not existing_delivery:
            delivery_gate=self.assert_delivery_gate(task_id)
        inspection_report=None if delivery_gate is None else delivery_gate["inspection_report"]
        inspection_evidence_hashes=[] if delivery_gate is None else delivery_gate["inspection_evidence"]["artifact_hashes"]
        visual_screenshot_hashes=[] if delivery_gate is None else delivery_gate["inspection_evidence"].get("screenshot_hashes",[])
        visual_quality=None if delivery_gate is None else delivery_gate["inspection_evidence"].get("visual_quality")

        # A delivery fact is the domain idempotency boundary.  Replays for the
        # same finalized deck return that immutable fact even when the caller
        # lost the first response or uses a different transport idempotency key.
        if existing_delivery:
            model=DeliveryManifest.parse(json.loads(self.version(task_id,existing_delivery["hash"])))
            published_root=self.store.delivery_root(task_id,delivery_id)
            verified=self.technical_gate.verify_delivery_package(published_root)
            final=self.get(task_id)
            if final["status"]!="completed":
                final=self.command(task_id,f"confirm-delivery-{existing_delivery['hash'][:16]}","confirm_delivery","user",{"deck_hash":deck_hash,"delivery_hash":existing_delivery["hash"]})
            self.store.clear_delivery_intent(task_id,delivery_id)
            hashes={**verified,"manifest.json":digest((published_root/"manifest.json").read_bytes())}
            return {"state":final,"delivery":{**model.to_dict(),"hash":existing_delivery["hash"],"file_hashes":hashes},"result":self.status_summary(task_id)}

        intent=self.store.delivery_intent(task_id,delivery_id,{"task_id":task_id,"deck_hash":deck_hash})
        confirmed_at=intent["confirmed_at"]
        finalization_mode=finalization.get("finalization_mode","standard")
        warnings=[]
        if finalization_mode=="risk_accepted":
            warnings.append({"code":"risk_accepted_finalization","label":"带风险终稿","blocking_issue_count":finalization["blocking_issue_count"],"rationale":finalization.get("risk_rationale","")})
        if finalization["unresolved_issue_count"]:
            warnings.append({"code":"finalized_with_issues","count":finalization["unresolved_issue_count"],"inspection_status":finalization["inspection_status"]})

        def complete(model,hashes,localized_count,delivery_hash=None):
            delivery_hash=delivery_hash or self.store.put_version(task_id,"delivery",canonical(model.to_dict()),{"file_hashes":hashes,"warnings":warnings,"issue_summary":{"unresolved":finalization["unresolved_issue_count"],"blockers":finalization["blocking_issue_count"]},"finalization_hash":finalization["hash"],"presentation_technical_contract_hash":design_contract_hash,"design_contract_hash":design_contract_hash,"claim_ledger_hash":claim_ledger_hash,"post_render_gate_hash":finalization.get("post_render_gate_hash"),"localized_resources":localized_count,"package":delivery_id})
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
            self.technical_gate.verify_delivery_package(published_root)
            package=json.loads((published_root/"manifest.json").read_text(encoding="utf-8"))
            if package.get("delivery_id")!=delivery_id or package.get("task_id")!=task_id or package.get("deck_hash")!=deck_hash or package.get("confirmed_at")!=confirmed_at:
                raise ConflictError("已发布离线包与当前交付事务不一致")
            hashes={name:digest((published_root/name).read_bytes()) for name in package.get("files",{})}
            if hashes!=package.get("files"):
                raise ConflictError("已发布离线包 manifest 不一致")
            packaged_evidence=published_root/"post-render-gate-evidence.json"
            if not packaged_evidence.is_file() or packaged_evidence.read_bytes()!=post_render_gate_evidence:
                raise ConflictError("已发布离线包的渲染后门禁 evidence 缺失或不一致")
            packaged_report=published_root/"inspection-report.json"
            if not packaged_report.is_file() or packaged_report.read_bytes()!=self.version(task_id,inspection_report["hash"]):
                raise ConflictError("已发布离线包的检查报告缺失或不一致")
            packaged_result=json.loads((published_root/"result.json").read_text(encoding="utf-8"))
            if (packaged_result.get("inspection_report_hash")!=inspection_report["hash"]
                or packaged_result.get("inspection_evidence_hashes")!=inspection_evidence_hashes
                or packaged_result.get("visual_screenshot_hashes",[])!=visual_screenshot_hashes):
                raise ConflictError("已发布离线包的检查报告绑定不一致")
            for evidence_hash in inspection_evidence_hashes:
                packaged_inspection_evidence=published_root/"inspection-evidence"/f"{evidence_hash}.json"
                if not packaged_inspection_evidence.is_file() or packaged_inspection_evidence.read_bytes()!=self.version(task_id,evidence_hash):
                    raise ConflictError("已发布离线包的检查 evidence 缺失或不一致")
            for screenshot_hash in visual_screenshot_hashes:
                packaged_screenshot=published_root/"visual-screenshots"/f"{screenshot_hash}.webp"
                if not packaged_screenshot.is_file() or packaged_screenshot.read_bytes()!=self.version(task_id,screenshot_hash):
                    raise ConflictError("已发布离线包的视觉质量截图缺失或不一致")
            packaged_visual=json.loads((published_root/"visual-quality.json").read_text(encoding="utf-8"))
            if packaged_visual!=visual_quality:
                raise ConflictError("已发布离线包的视觉质量评分绑定不一致")
            packaged_performance=json.loads((published_root/"offline-performance.json").read_text(encoding="utf-8"))
            if not packaged_performance.get("passed") or packaged_result.get("offline_performance")!=packaged_performance:
                raise ConflictError("已发布离线包的性能预算证据缺失或不一致")
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
        index_html=offline_player(localized_html); runtime_assets=offline_assets(); performance=offline_performance(index_html,runtime_assets)
        if not performance["passed"]:
            raise ConflictError("离线播放器性能预算未通过")
        files={
            "deck.html":localized_html.encode(),
            "index.html":index_html.encode(),
            "narrative.md":json.loads(self.version(task_id,narrative_hash))["markdown"].encode(),
            "outline.md":json.loads(self.version(task_id,outline_hash))["markdown"].encode(),
            "presentation-technical-contract.json":self.version(task_id,design_contract_hash),
            "claim-ledger.json":self.version(task_id,claim_ledger_hash),
            "post-render-gate-evidence.json":post_render_gate_evidence,
            "inspection-report.json":self.version(task_id,inspection_report["hash"]),
            **{f"inspection-evidence/{evidence_hash}.json":self.version(task_id,evidence_hash) for evidence_hash in inspection_evidence_hashes},
            **{f"visual-screenshots/{screenshot_hash}.webp":self.version(task_id,screenshot_hash) for screenshot_hash in visual_screenshot_hashes},
            "visual-quality.json":canonical(visual_quality),
            "offline-performance.json":canonical(performance),
            "resource-manifest.json":canonical(snapshot["manifest"]),
            "localized-resources.json":canonical({"resources":localization_records}),
            "result.json":canonical({
                "task_id":task_id,
                "deck_hash":deck_hash,
                "finalization_hash":finalization["hash"],
                "presentation_technical_contract_hash":design_contract_hash,
                "design_contract_hash":design_contract_hash,
                "claim_ledger_hash":claim_ledger_hash,
                "post_render_gate_hash":post_render_gate_hash,
                "inspection_report_hash":inspection_report["hash"],
                "inspection_evidence_hashes":inspection_evidence_hashes,
                "visual_screenshot_hashes":visual_screenshot_hashes,
                "visual_quality":None if visual_quality is None else {key:visual_quality.get(key) for key in ("score","grade","composition_score","layout_diversity_score","theme_rhythm_score")},
                "offline_performance":performance,
                "inspection_status":finalization["inspection_status"],
                "finalization_mode":finalization_mode,
                "warnings":warnings,
                "confirmed_at":confirmed_at,
                **result_summary,
            }),
            **runtime_assets,
            **localized_resources,
        }
        for item in snapshot["manifest"].get("resources",[]):
            relative=Path(item["uri"].removeprefix("resources://"))
            source=resource_root/relative
            if source.is_file() and digest(source.read_bytes())==item["content_hash"]: files[f"resources/{relative.as_posix()}"]=source.read_bytes()
        hashes={name:digest(content) for name,content in files.items()}
        package_manifest={"delivery_id":delivery_id,"task_id":task_id,"deck_hash":deck_hash,"confirmed_by":"user","confirmed_at":confirmed_at,"files":hashes}
        files["manifest.json"]=canonical(package_manifest); hashes["manifest.json"]=digest(files["manifest.json"])
        self.store.publish_delivery(task_id,delivery_id,files,verifier=self.technical_gate.verify_delivery_package)
        model=DeliveryManifest.parse({"delivery_id":delivery_id,"task_id":task_id,"deck_hash":deck_hash,"files":tuple(files),"confirmed_by":"user","confirmed_at":confirmed_at,"schema_version":"1.0"})
        return complete(model,hashes,len(localization_records))

    def confirm_delivery(self,task_id,deck_hash,actor="user"):
        """Compatibility wrapper for clients which previously combined both actions."""
        if not self.finalization_view(task_id)["current"]:
            self.finalize_deck(task_id,deck_hash,"review" if self.get(task_id)["stage"]=="review" else "deck",actor)
        return self.publish_delivery(task_id,actor)

    def reopen_review(self,task_id):
        """Return a finalized-but-unpublished task to the review stage.

        The publish gate blocks first-time writes when the inspection report is
        missing/stale or unresolved blockers remain; without a way back the task
        would be wedged, since inspection and disposition require a mutable
        candidate.  Completed deliveries stay immutable (derive instead)."""
        state=TaskState.parse(self.get(task_id))
        if state.stage!=state.stage.DELIVERY: raise ConflictError("只有交付阶段可以返回自检与修改")
        if not self.finalization_view(task_id)["current"]: raise ConflictError("当前没有已冻结的终稿")
        self.command(task_id,f"reopen-review-{state.revision}","reopen_review","user")
        return self.inspection_view(task_id)

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
            self._record_deck(task_id,meta["html"],target["outline_hash"],{"parent":delivered["deck_hash"],"derived_from_delivery":delivery_hash,"summary":"从已交付版本派生候选","scope":"global","affected":[],"outline_consistent":True,"global_rules":meta.get("global_rules",[]),"local_exceptions":meta.get("local_exceptions",{}),"design_contract_hash":meta.get("design_contract_hash"),"claim_ledger_hash":meta.get("claim_ledger_hash"),"post_render_gate":meta.get("post_render_gate")},"delivery_derive","user")
        return self.modify_deck(task_id,prompt,scope="page" if slide_ids else "global",slide_ids=slide_ids or [])
