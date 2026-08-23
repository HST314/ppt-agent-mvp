import json
import hashlib
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from ppt_agent.agent_runtime import AgentRuntime
from ppt_agent.claim_ledger import build_claim_ledger
from ppt_agent.design_contract import validate_presentation_technical_contract
from ppt_agent.errors import GatewayError, ValidationError
from ppt_agent.gateways import AgentGateway
from ppt_agent.model_clients import ModelToolCall, ModelTurn
from ppt_agent.model_clients import OpenAIResponsesClient
from ppt_agent.p4 import render
from ppt_agent.service import TaskService
from ppt_agent.skill_runtime import SkillRuntime
from ppt_agent.store import WorkspaceStore
from ppt_agent.web.jobs import OPERATION_BUDGET_SECONDS


class RecordingClient:
    def __init__(self, turns):
        self.turns=list(turns); self.inputs=[]
    def create(self, **kwargs):
        self.inputs.append(kwargs)
        return self.turns.pop(0)


class BatchBuilder:
    def __init__(self): self.calls=[]
    def build(self, outline, **context):
        self.calls.append(dict(context))
        return render(outline,context["slide_ids"],context.get("rules"),context.get("exceptions"),context.get("assets"))


class LockedThemeRetryBuilder:
    def __init__(self, *, always_bad=False):
        self.always_bad=always_bad; self.calls=[]

    def build(self, outline, **context):
        self.calls.append(dict(context))
        html=render(
            outline,
            context["slide_ids"],
            context.get("rules"),
            context.get("exceptions"),
            context.get("assets"),
            context.get("design_contract"),
            context.get("design_contract_hash"),
        )
        if self.always_bad or len(self.calls)==1:
            return html.replace("<section ",'<section style="--ink:#fff" ',1)
        return html


class RequiredClaimRetryBuilder:
    def __init__(self, *, always_bad=False):
        self.always_bad=always_bad; self.calls=[]

    def build(self, outline, **context):
        self.calls.append(dict(context))
        html=render(
            outline,
            context["slide_ids"],
            context.get("rules"),
            context.get("exceptions"),
            context.get("assets"),
            context.get("design_contract"),
            context.get("design_contract_hash"),
        )
        required=context["required_claims_verbatim"]
        if required and (self.always_bad or len(self.calls)==1):
            html=html.replace(required[0]["value"],"冻结事实遗漏")
        return html


class MisplacedRequiredClaimBuilder:
    def __init__(self, *, always_bad=False):
        self.always_bad=always_bad; self.calls=[]

    def build(self, outline, **context):
        self.calls.append(dict(context))
        html=render(
            outline,context["slide_ids"],context.get("rules"),context.get("exceptions"),
            context.get("assets"),context.get("design_contract"),context.get("design_contract_hash"),
        )
        if not (self.always_bad or len(self.calls)==1):
            return html
        mapped=context["required_claims_by_slide"]
        target=next(slide_id for slide_id,claims in mapped.items() if claims)
        other=next(slide_id for slide_id in context["slide_ids"] if slide_id!=target)
        value=mapped[target][0]["value"]
        fragments=TaskService._slide_fragments(html)
        fragments[target]=fragments[target].replace(value,"冻结事实错页")
        fragments[other]=fragments[other].replace("</section>",f"<p>{value}</p></section>",1)
        return TaskService._replace_slide_fragments(html,fragments)


class ReportedClaimRetryBuilder:
    def __init__(self):
        self.calls=[]

    def build(self,outline,**context):
        self.calls.append(dict(context))
        html=render(
            outline,context["slide_ids"],context.get("rules"),context.get("exceptions"),
            context.get("assets"),context.get("design_contract"),context.get("design_contract_hash"),
        )
        if len(self.calls)==1:
            for value in ("12 周","80 万元"):
                html=html.replace(value,"冻结事实遗漏")
        return html


class GeometryCorrectionBuilder:
    def __init__(self): self.calls=[]
    def build(self,outline,**context):
        self.calls.append(dict(context))
        html=render(
            outline,context["slide_ids"],context.get("rules"),context.get("exceptions"),
            context.get("assets"),context.get("design_contract"),context.get("design_contract_hash"),
        )
        if context.get("technical_correction",{}).get("reason")!="browser_render_blockers":
            return html.replace("<section ",'<section data-needs-layout-correction="true" ',1)
        return html


class GeometryCorrectionBrowser:
    enforce_on_generation=True
    def inspect(self,html,expected_slide_ids):
        issues=[]
        if "data-needs-layout-correction" in html:
            issues=[{
                "issue_id":"capacity-overflow","severity":"blocker","level":"element",
                "code":"element_scroll_overflow","message":"高密度正文滚动溢出",
                "slide_id":expected_slide_ids[0],"element_id":"body",
                "selector":f'.slide[data-slide-id="{expected_slide_ids[0]}"] [data-element-id="body"]',
                "geometry":{"client_width":1126,"client_height":468,"scroll_width":1126,"scroll_height":613,"delta_height":145},
                "evidence":"client=1126x468; scroll=1126x613","suggestion":"压缩内容或切换布局",
            }]
        return {
            "available":True,"passed":not issues,"engine":"chromium","engine_version":"preflight-test",
            "viewport":{"width":1280,"height":720},"issues":issues,
            "slides":[{"slide_id":slide_id} for slide_id in expected_slide_ids],
        }


VALID_DESIGN_INTENT = {
    "style_summary":"清晰的业务汇报",
    "color_strategy":"深色标题配高对比正文",
    "typography_strategy":"系统无衬线字体与明确字号层级",
    "layout_principles":["统一安全边距","同类信息保持一致结构"],
    "rationale":"保证样品在固定画布内稳定可读",
}


def rendering_payload(slide_ids, *, css="", design_intent=None):
    return json.dumps({
        "slides":[{
            "slide_id":slide_id,
            "html":f'<section class="slide" id="{slide_id}" data-slide-id="{slide_id}"><h1>{slide_id}</h1><p>已按冻结大纲生成</p></section>',
        } for slide_id in slide_ids],
        "design_intent":design_intent or VALID_DESIGN_INTENT,
        "shared_assets":{"css":css},
    },ensure_ascii=False)


class UnboundClaimRetryBuilder:
    def __init__(self): self.calls=[]
    def build(self,outline,**context):
        self.calls.append(dict(context))
        html=render(
            outline,context["slide_ids"],context.get("rules"),context.get("exceptions"),
            context.get("assets"),context.get("design_contract"),context.get("design_contract_hash"),
        )
        if len(self.calls)==1:
            html=html.replace("</section>","<p>未经绑定的预测增长 999%</p></section>",1)
        return html


class AlwaysInvalidBuilder:
    def __init__(self): self.calls=[]
    def build(self,_outline,**context):
        self.calls.append(dict(context))
        raise ValidationError("CSS 包含规则或任务外资源")


class AlwaysBlockedBrowser:
    enforce_on_generation=True
    def inspect(self,_html,expected_slide_ids):
        slide_id=expected_slide_ids[0]
        issue={
            "issue_id":"persistent-overflow","severity":"blocker","level":"element",
            "code":"element_scroll_overflow","message":"安全 fallback 仍溢出",
            "slide_id":slide_id,"element_id":"body","selector":f"#{slide_id}",
            "geometry":{"client_width":100,"client_height":100,"scroll_width":100,"scroll_height":200,"delta_height":100},
            "evidence":"client=100x100; scroll=100x200","suggestion":"拒绝落盘",
        }
        return {
            "available":True,"passed":False,"engine":"chromium","engine_version":"fallback-test",
            "viewport":{"width":1280,"height":720},"issues":[issue],
            "slides":[{"slide_id":item} for item in expected_slide_ids],
        }


class P0GenerationRefactorTests(unittest.TestCase):
    @staticmethod
    def prepared_sample_service(root, *, pages=8):
        svc=TaskService(WorkspaceStore(root)); svc.create("task","manual")
        svc.import_input("task",{"goal":"发布方案","audience":"管理层","topic":"一次生成验收","页数":pages})
        svc.generate_narrative("task"); svc.confirm_narrative("task")
        svc.generate_outline("task"); svc.confirm_outline("task")
        selected=["slide-1","slide-7"] if pages>=7 else ["slide-1"]
        svc.select_samples("task",selected)
        return svc,selected

    def test_empty_design_intent_is_corrected_by_outer_attempt_controller(self):
        with tempfile.TemporaryDirectory() as root:
            svc,slide_ids=self.prepared_sample_service(root,pages=3)
            invalid_intent={**VALID_DESIGN_INTENT,"color_strategy":""}
            client=RecordingClient([
                ModelTurn(None,"skill",(ModelToolCall("read_skill_file",'{"path":"SKILL.md"}',"skill-1"),)),
                ModelTurn(rendering_payload(slide_ids,design_intent=invalid_intent),"invalid-intent"),
                ModelTurn(None,"skill",(ModelToolCall("read_skill_file",'{"path":"SKILL.md"}',"skill-2"),)),
                ModelTurn(rendering_payload(slide_ids),"corrected-intent"),
            ])
            gateway=AgentGateway(client,skill=SkillRuntime.builtin(),max_steps=5,max_tool_calls=4,max_provider_calls=4)
            svc.builder=gateway

            sample=svc.generate_sample("task")["sample"]

            generation=sample["metadata"]["generation"]
            self.assertEqual({key:generation[key] for key in ("attempts","retry_count")},{"attempts":2,"retry_count":1})
            self.assertEqual(sample["metadata"]["design_intent"],VALID_DESIGN_INTENT)
            correction=json.loads(svc.version("task",generation["correction_evidence_hashes"][0]))["correction"]
            self.assertEqual(correction["reason"],"technical_validation_failed")
            self.assertIn("DesignIntent",correction["error"])
            self.assertEqual(len(client.inputs),4)

    def test_forbidden_shared_css_is_corrected_by_outer_attempt_controller(self):
        with tempfile.TemporaryDirectory() as root:
            svc,slide_ids=self.prepared_sample_service(root,pages=8)
            client=RecordingClient([
                ModelTurn(None,"skill-1",(ModelToolCall("read_skill_file",'{"path":"SKILL.md"}',"skill-1"),)),
                ModelTurn(rendering_payload(slide_ids,css='@font-face{font-family:x;src:url("https://example.com/x.woff2")}'),"bad-css"),
                ModelTurn(None,"skill-2",(ModelToolCall("read_skill_file",'{"path":"SKILL.md"}',"skill-2"),)),
                ModelTurn(rendering_payload(slide_ids,css=".slide{font-family:Arial,sans-serif}"),"safe-css"),
            ])
            gateway=AgentGateway(client,skill=SkillRuntime.builtin(),max_steps=4,max_tool_calls=4,max_provider_calls=4)
            svc.builder=gateway

            sample=svc.generate_sample("task")["sample"]

            generation=sample["metadata"]["generation"]
            self.assertEqual({key:generation[key] for key in ("attempts","retry_count")},{"attempts":2,"retry_count":1})
            correction=json.loads(svc.version("task",generation["correction_evidence_hashes"][0]))["correction"]
            self.assertEqual(correction["reason"],"technical_validation_failed")
            self.assertIn("CSS",correction["error"])
            second_payload=json.loads(client.inputs[2]["input"][1]["content"])
            self.assertEqual(second_payload["technical_correction"]["reason"],"technical_validation_failed")
            self.assertNotIn("@font-face",sample["html"])
            self.assertFalse(generation.get("degraded_fallback",False))

    def test_two_invalid_shared_css_outputs_use_audited_safe_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            svc,slide_ids=self.prepared_sample_service(root,pages=8)
            turns=[]
            for attempt in (1,2):
                turns.extend((
                    ModelTurn(None,f"skill-{attempt}",(ModelToolCall("read_skill_file",'{"path":"SKILL.md"}',f"skill-{attempt}"),)),
                    ModelTurn(rendering_payload(slide_ids,css="@import url('https://example.com/theme.css');"),f"bad-css-{attempt}"),
                ))
            svc.builder=AgentGateway(RecordingClient(turns),skill=SkillRuntime.builtin(),max_steps=4,max_tool_calls=4,max_provider_calls=4)

            sample=svc.generate_sample("task")["sample"]

            generation=sample["metadata"]["generation"]
            self.assertTrue(generation["degraded_fallback"])
            self.assertEqual(generation["fallback"]["strategy"],"frozen_outline_generic_shell")
            self.assertEqual(generation["fallback"]["reason"],"technical_validation_failed")
            self.assertNotIn("@import",sample["html"])
            self.assertTrue(sample["metadata"]["post_render_gate"]["passed"])
            attempts=[json.loads(svc.version("task",item)) for item in generation["attempt_evidence_hashes"]]
            self.assertEqual([item["status"] for item in attempts],["correction_required","accepted"])
            self.assertIsNone(attempts[1]["generation_error"])
            self.assertTrue(attempts[1]["fallback"]["degraded_fallback"])

    def test_unbound_claim_is_regenerated_before_final_gate(self):
        with tempfile.TemporaryDirectory() as root:
            builder=UnboundClaimRetryBuilder()
            svc,slide_ids=self.prepared_sample_service(root,pages=3)
            svc.builder=builder

            sample=svc.generate_sample("task")["sample"]

            self.assertEqual(len(builder.calls),2)
            self.assertEqual(builder.calls[1]["technical_correction"]["reason"],"unbound_claims")
            self.assertEqual(sample["metadata"]["post_render_gate"]["claims"]["unbound_count"],0)
            self.assertNotIn("999%",sample["html"])

    def test_safe_fallback_still_fails_closed_when_browser_gate_blocks(self):
        with tempfile.TemporaryDirectory() as root:
            svc,_=self.prepared_sample_service(root,pages=3)
            builder=AlwaysInvalidBuilder()
            svc.builder=builder
            svc.browser_inspector=AlwaysBlockedBrowser()

            with self.assertRaisesRegex(ValidationError,"fallback 未通过技术门禁"):
                svc.generate_sample("task")

            self.assertEqual(len(builder.calls),2)
            self.assertEqual(svc.versions("task","sample"),[])
            attempts=svc.versions("task","generation-attempt-evidence")
            corrections=svc.versions("task","generation-correction-evidence")
            self.assertEqual(len(attempts),2)
            self.assertEqual(len(corrections),1)
            final=json.loads(svc.version("task",attempts[-1]["hash"]))
            self.assertEqual(final["status"],"failed")
            self.assertEqual(final["fallback"]["failed_reason"],"browser_render_blockers")

    def test_unfixable_geometry_gets_one_structured_regeneration_and_evidence_chain(self):
        with tempfile.TemporaryDirectory() as root:
            builder=GeometryCorrectionBuilder()
            svc=TaskService(WorkspaceStore(root),builder=builder,browser_inspector=GeometryCorrectionBrowser())
            svc.create("geometry","manual")
            svc.import_input("geometry",{"goal":"批准投资","audience":"管理层","topic":"投资方案","页数":3})
            svc.generate_narrative("geometry"); svc.confirm_narrative("geometry")
            svc.generate_outline("geometry"); svc.confirm_outline("geometry")
            svc.select_samples("geometry",["slide-1"])

            sample=svc.generate_sample("geometry")["sample"]

            self.assertEqual(len(builder.calls),2)
            correction=builder.calls[1]["technical_correction"]
            self.assertEqual(correction["reason"],"browser_render_blockers")
            blocker=correction["browser_blockers"][0]
            self.assertEqual(blocker["geometry"]["scroll_height"],613)
            generation=sample["metadata"]["generation"]
            attempts=[json.loads(svc.version("geometry",item)) for item in generation["attempt_evidence_hashes"]]
            self.assertEqual([item["status"] for item in attempts],["correction_required","accepted"])
            self.assertEqual(attempts[1]["parent_attempt_id"],generation["attempt_evidence_hashes"][0])
            self.assertEqual(sample["metadata"]["post_render_gate"]["generation_attempt_evidence_hashes"],generation["attempt_evidence_hashes"])

    def test_sample_exposes_only_read_only_skill_tools_and_server_assembles_public_shell(self):
        fragment='<section class="slide" id="slide-1" data-slide-id="slide-1"><h1>样品</h1></section>'
        client=RecordingClient([
            ModelTurn(None,"skill",(ModelToolCall("read_skill_file",'{"path":"SKILL.md"}',"skill-call"),)),
            ModelTurn(json.dumps({"slides":[{"slide_id":"slide-1","html":fragment}]}),"r1"),
        ])
        gateway=AgentGateway(client,skill=SkillRuntime.builtin(),max_steps=100)
        html=gateway.build("## [slide-1] 样品",action="sample",slide_ids=["slide-1"])
        self.assertEqual(len(client.inputs),2)
        self.assertEqual(
            {tool["name"] for tool in client.inputs[0]["tools"]},
            {"read_skill_file"},
        )
        self.assertEqual(client.inputs[0]["tool_choice"],{"type":"function","name":"read_skill_file"})
        path_schema=client.inputs[0]["tools"][0]["parameters"]["properties"]["path"]
        self.assertEqual(path_schema["enum"],["SKILL.md"])
        self.assertNotIn("<!doctype html>",client.inputs[0]["input"][1]["content"])
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn(fragment,html)
        self.assertIn('name="design-intent"',html)
        self.assertIn('--presentation-width:1280px',html)
        self.assertNotIn('data-layout=',html)
        self.assertNotIn('linear-gradient(135deg,#172033,#253858)',html)
        self.assertEqual(gateway.runtime.last_audit[-1]["applied_skill_files"],["SKILL.md"])
        tool_output=next(item for item in client.inputs[1]["input"] if item.get("type")=="function_call_output")
        contract=json.loads(tool_output["output"])
        self.assertGreaterEqual(contract["bytes"],10000)
        for required_rule in (
            "references/layouts.md",
            "references/themes.md",
            "references/checklist.md",
            "scripts/validate-swiss-deck.mjs",
        ):
            self.assertIn(required_rule,contract["content"])
        self.assertIn("必须首先调用 read_skill_file 完整读取 SKILL.md",client.inputs[0]["input"][0]["content"])

    def test_sample_cannot_finish_without_applying_required_design_pack(self):
        fragment='<section class="slide" id="slide-1" data-slide-id="slide-1"><h1>样品</h1></section>'
        early=ModelTurn(json.dumps({"slides":[{"slide_id":"slide-1","html":fragment}]}),"r1")
        client=RecordingClient([early,early])

        with self.assertRaises(GatewayError) as caught:
            AgentRuntime(client,SkillRuntime.builtin()).run("sample",{"slide_ids":["slide-1"]})

        self.assertEqual(caught.exception.code,"agent_skill_entry_missing")
        self.assertEqual(caught.exception.audit[-1]["unique_skill_files"],0)

    def test_sample_reads_complete_contract_once_and_deduplicates_same_round(self):
        fragment='<section class="slide" id="slide-1" data-slide-id="slide-1"><h1>样品</h1></section>'
        client=RecordingClient([
            ModelTurn(None,"r1",(
                ModelToolCall("read_skill_file",'{"path":"SKILL.md"}',"c1"),
                ModelToolCall("read_skill_file",'{"path":"SKILL.md"}',"c2"),
            )),
            ModelTurn(json.dumps({"slides":[{"slide_id":"slide-1","html":fragment}]}),"r2"),
        ])
        gateway=AgentGateway(
            client,
            skill=SkillRuntime.builtin(),
            max_steps=30,
            max_tool_calls=40,
            max_provider_calls=8,
        )

        html=gateway.build("## [slide-1] 样品",action="sample",slide_ids=["slide-1"])

        self.assertEqual(len(client.inputs),2)
        self.assertEqual(client.inputs[1]["tools"],[])
        outputs=[item for item in client.inputs[1]["input"] if item.get("type")=="function_call_output"]
        self.assertIn('"already_read": true',outputs[-1]["output"])
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn(fragment,html)

    def test_sample_cross_round_duplicate_is_cached_without_error_recovery(self):
        fragment='<section class="slide" id="slide-1" data-slide-id="slide-1"><h1>样品</h1></section>'
        client=RecordingClient([
            ModelTurn(None,"r1",(ModelToolCall("read_skill_file",'{"path":"SKILL.md"}',"c1"),)),
            ModelTurn(None,"r2",(ModelToolCall("read_skill_file",'{"path":"SKILL.md"}',"c2"),)),
            ModelTurn(json.dumps({"slides":[{"slide_id":"slide-1","html":fragment}]}),"r3"),
        ])
        gateway=AgentGateway(client,skill=SkillRuntime.builtin(),max_steps=30,max_tool_calls=40,max_provider_calls=8)

        html=gateway.build("## [slide-1] 样品",action="sample",slide_ids=["slide-1"])

        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIsNone(client.inputs[1]["tool_choice"])
        self.assertEqual(client.inputs[2]["tool_choice"],"none")
        cached_outputs=[
            item for item in client.inputs[2]["input"]
            if item.get("type")=="function_call_output" and '"cached": true' in item.get("output","")
        ]
        self.assertEqual(len(cached_outputs),1)
        self.assertFalse(any(item.get("event")=="tool_error" for item in gateway.runtime.last_audit))
        self.assertEqual(gateway.runtime.last_audit[-1]["repeated_skill_reads"],1)

    def test_deck_generates_only_unconfirmed_pages_in_bounded_batches(self):
        with tempfile.TemporaryDirectory() as root:
            svc=TaskService(WorkspaceStore(root)); svc.create("task","manual")
            svc.import_input("task",{"goal":"发布","audience":"客户","topic":"方案","页数":8})
            svc.generate_narrative("task"); svc.confirm_narrative("task")
            svc.generate_outline("task"); svc.confirm_outline("task")
            svc.select_samples("task",["slide-2","slide-7"]); svc.generate_sample("task"); svc.confirm_sample("task")
            builder=BatchBuilder(); svc.builder=builder
            candidate=svc.generate_deck("task")["deck"]
            requested=[sid for call in builder.calls for sid in call["slide_ids"]]
            self.assertEqual(requested,["slide-1","slide-3","slide-4","slide-5","slide-6","slide-8"])
            self.assertTrue(all(len(call["slide_ids"]) <= 3 for call in builder.calls))
            self.assertEqual(candidate["metadata"]["inspection_status"],"pending")
            self.assertFalse(svc.versions("task","inspection"))

    def test_sample_accepts_agent_owned_custom_css_variables_without_retry(self):
        with tempfile.TemporaryDirectory() as root:
            builder=LockedThemeRetryBuilder()
            svc=TaskService(WorkspaceStore(root),builder=builder); svc.create("task","manual")
            svc.import_input("task",{"goal":"发布","audience":"客户","topic":"方案","页数":3})
            svc.generate_narrative("task"); svc.confirm_narrative("task")
            svc.generate_outline("task"); svc.confirm_outline("task")
            svc.select_samples("task",["slide-1"])

            sample=svc.generate_sample("task")["sample"]

            self.assertEqual(len(builder.calls),1)
            self.assertEqual(builder.calls[0]["generation_attempt"],1)
            self.assertNotIn("locked_theme_policy",builder.calls[0])
            generation=sample["metadata"]["generation"]
            self.assertEqual({key:generation[key] for key in ("attempts","retry_count","max_attempts")},{"attempts":1,"retry_count":0,"max_attempts":2})
            self.assertEqual(len(generation["attempt_evidence_hashes"]),1)
            self.assertEqual(len(generation["correction_evidence_hashes"]),0)
            self.assertIn("--ink:#fff",sample["html"])
            self.assertEqual(len(svc.versions("task","sample")),1)

    def test_sample_does_not_treat_skill_design_tokens_as_framework_errors(self):
        with tempfile.TemporaryDirectory() as root:
            builder=LockedThemeRetryBuilder(always_bad=True)
            svc=TaskService(WorkspaceStore(root),builder=builder); svc.create("task","manual")
            svc.import_input("task",{"goal":"发布","audience":"客户","topic":"方案","页数":3})
            svc.generate_narrative("task"); svc.confirm_narrative("task")
            svc.generate_outline("task"); svc.confirm_outline("task")
            svc.select_samples("task",["slide-1"])

            sample=svc.generate_sample("task")["sample"]

            self.assertEqual(len(builder.calls),1)
            self.assertIn("--ink:#fff",sample["html"])

    def test_sample_missing_required_claim_is_corrected_once_in_same_job(self):
        with tempfile.TemporaryDirectory() as root:
            builder=RequiredClaimRetryBuilder()
            svc=TaskService(WorkspaceStore(root),builder=builder); svc.create("task","manual")
            svc.import_input("task",{"goal":"批准预算","audience":"管理层","topic":"扩容方案","页数":3,"总预算":"80 万元"})
            svc.generate_narrative("task"); svc.confirm_narrative("task")
            svc.generate_outline("task"); svc.confirm_outline("task")
            svc.select_samples("task",["slide-1","slide-2","slide-3"])

            sample=svc.generate_sample("task")["sample"]

            self.assertEqual(len(builder.calls),2)
            required=builder.calls[0]["required_claims_verbatim"]
            self.assertEqual({item["value"] for item in required},{"80 万元"})
            correction=builder.calls[1]["technical_correction"]
            self.assertEqual(correction["reason"],"missing_required_claims")
            self.assertEqual(correction["required_claims_verbatim"],required)
            self.assertEqual(correction["missing_required_claims_verbatim"],required)
            self.assertEqual(correction["required_claims_by_slide"],builder.calls[0]["required_claims_by_slide"])
            generation=sample["metadata"]["generation"]
            self.assertEqual({key:generation[key] for key in ("attempts","retry_count","max_attempts")},{"attempts":2,"retry_count":1,"max_attempts":2})
            attempts=[json.loads(svc.version("task",item)) for item in generation["attempt_evidence_hashes"]]
            correction_evidence=json.loads(svc.version("task",generation["correction_evidence_hashes"][0]))
            self.assertEqual(attempts[0]["status"],"correction_required")
            self.assertEqual(attempts[1]["parent_attempt_id"],generation["attempt_evidence_hashes"][0])
            self.assertEqual(correction_evidence["parent_attempt_id"],generation["attempt_evidence_hashes"][0])
            self.assertEqual(correction_evidence["correction"]["missing_required_claims_by_slide"],correction["missing_required_claims_by_slide"])
            self.assertEqual(sample["metadata"]["post_render_gate"]["generation_attempt_evidence_hashes"],generation["attempt_evidence_hashes"])

    def test_sample_claim_on_wrong_page_is_corrected_from_page_mapping(self):
        with tempfile.TemporaryDirectory() as root:
            builder=MisplacedRequiredClaimBuilder()
            svc=TaskService(WorkspaceStore(root),builder=builder); svc.create("task","manual")
            svc.import_input("task",{"goal":"批准预算","audience":"管理层","topic":"扩容方案","页数":3,"总预算":"80 万元"})
            svc.generate_narrative("task"); svc.confirm_narrative("task")
            svc.generate_outline("task"); svc.confirm_outline("task")
            svc.select_samples("task",["slide-1","slide-2","slide-3"])

            sample=svc.generate_sample("task")["sample"]

            self.assertEqual(len(builder.calls),2)
            correction=builder.calls[1]["technical_correction"]
            missing_pages={slide_id for slide_id,claims in correction["missing_required_claims_by_slide"].items() if claims}
            self.assertEqual(len(missing_pages),1)
            self.assertEqual(sample["metadata"]["post_render_gate"]["claims"]["missing_required_count"],0)
            self.assertIsNotNone(sample["metadata"]["post_render_gate"]["claims"]["page_coverage"])

    def test_reported_duration_and_budget_omissions_keep_their_target_pages_in_correction(self):
        with tempfile.TemporaryDirectory() as root:
            builder=ReportedClaimRetryBuilder()
            svc=TaskService(WorkspaceStore(root),builder=builder); svc.create("task","manual")
            svc.import_input("task",{
                "goal":"批准预算","audience":"管理层","topic":"扩容方案","页数":3,
                "实施周期":"12 周","总预算":"80 万元",
            })
            svc.generate_narrative("task"); svc.confirm_narrative("task")
            svc.generate_outline("task"); svc.confirm_outline("task")
            svc.select_samples("task",["slide-1","slide-2","slide-3"])

            sample=svc.generate_sample("task")["sample"]

            correction=builder.calls[1]["technical_correction"]
            self.assertEqual({item["value"] for item in correction["missing_required_claims_verbatim"]},{"12 周","80 万元"})
            mapped_missing={
                item["value"]:slide_id
                for slide_id,items in correction["missing_required_claims_by_slide"].items()
                for item in items
            }
            self.assertEqual(set(mapped_missing),{"12 周","80 万元"})
            for value,slide_id in mapped_missing.items():
                self.assertIn(value,{item["value"] for item in correction["required_claims_by_slide"][slide_id]})
            self.assertEqual(sample["metadata"]["post_render_gate"]["claims"]["missing_required_count"],0)

    def test_persistent_wrong_page_claim_is_bound_into_target_page_server_slot(self):
        with tempfile.TemporaryDirectory() as root:
            builder=MisplacedRequiredClaimBuilder(always_bad=True)
            svc=TaskService(WorkspaceStore(root),builder=builder); svc.create("task","manual")
            svc.import_input("task",{"goal":"批准预算","audience":"管理层","topic":"扩容方案","页数":3,"总预算":"80 万元"})
            svc.generate_narrative("task"); svc.confirm_narrative("task")
            svc.generate_outline("task"); svc.confirm_outline("task")
            svc.select_samples("task",["slide-1","slide-2","slide-3"])

            sample=svc.generate_sample("task")["sample"]

            materialization=sample["metadata"]["generation"]["server_claim_materialization"]
            self.assertEqual(materialization["materialized_count"],1)
            self.assertEqual(materialization["placements"][0]["slide_id"],next(
                slide_id for slide_id,claims in builder.calls[0]["required_claims_by_slide"].items() if claims
            ))
            self.assertEqual(sample["metadata"]["post_render_gate"]["claims"]["missing_required_count"],0)
            self.assertIn('data-server-materialized="true"',sample["html"])

    def test_agent_builder_exposes_page_visible_claim_boundary_self_check(self):
        ledger=build_claim_ledger(
            task_id="boundary",input_snapshot_hash="a"*64,
            source_binding={"总预算":"80 万元"},created_at="2026-08-22T00:00:00+00:00",
        )
        claim={key:ledger["claims"][0][key] for key in ("claim_id","kind","value")}
        slides=[
            {"slide_id":"slide-1","html":'<section class="slide" id="slide-1" data-slide-id="slide-1"><p>预算待补</p></section>'},
            {"slide_id":"slide-2","html":'<section class="slide" id="slide-2" data-slide-id="slide-2"><p>80 万元</p></section>'},
        ]
        client=RecordingClient([
            ModelTurn(None,"skill",(ModelToolCall("read_skill_file",'{"path":"SKILL.md"}',"c1"),)),
            ModelTurn(json.dumps({"slides":slides},ensure_ascii=False),"render"),
        ])
        gateway=AgentGateway(client,skill=SkillRuntime.builtin(),max_steps=4,max_tool_calls=4,max_provider_calls=4)

        html=gateway.build(
            "## [slide-1] 预算\n## [slide-2] 总结",action="sample",slide_ids=["slide-1","slide-2"],
            claim_ledger=ledger,required_claims_verbatim=[claim],
            required_claims_by_slide={"slide-1":[claim],"slide-2":[]},
        )

        self.assertEqual(html.builder_boundary["missing_required_count"],1)
        self.assertEqual(html.builder_boundary["missing_required"][0]["slide_id"],"slide-1")

    def test_persistent_sample_required_claim_omission_uses_deterministic_visible_slot(self):
        with tempfile.TemporaryDirectory() as root:
            builder=RequiredClaimRetryBuilder(always_bad=True)
            svc=TaskService(WorkspaceStore(root),builder=builder); svc.create("task","manual")
            svc.import_input("task",{"goal":"批准预算","audience":"管理层","topic":"扩容方案","页数":3,"总预算":"80 万元"})
            svc.generate_narrative("task"); svc.confirm_narrative("task")
            svc.generate_outline("task"); svc.confirm_outline("task")
            svc.select_samples("task",["slide-1","slide-2","slide-3"])

            sample=svc.generate_sample("task")["sample"]

            self.assertEqual(len(builder.calls),2)
            slots=builder.calls[0]["required_claims_by_slide"]
            self.assertEqual(
                {claim["value"] for claims in slots.values() for claim in claims},
                {"80 万元"},
            )
            materialization=sample["metadata"]["generation"]["server_claim_materialization"]
            self.assertEqual(materialization["materialized_count"],1)
            self.assertEqual(sample["metadata"]["post_render_gate"]["claims"]["missing_required_count"],0)
            self.assertIn('data-server-materialized="true">80 万元</span>',sample["html"])

    def test_deck_batch_accepts_confirmed_design_tokens_without_retry(self):
        with tempfile.TemporaryDirectory() as root:
            svc=TaskService(WorkspaceStore(root)); svc.create("task","manual")
            svc.import_input("task",{"goal":"发布","audience":"客户","topic":"方案","页数":3})
            svc.generate_narrative("task"); svc.confirm_narrative("task")
            svc.generate_outline("task"); svc.confirm_outline("task")
            svc.select_samples("task",["slide-1"]); svc.generate_sample("task"); svc.confirm_sample("task")
            builder=LockedThemeRetryBuilder(); svc.builder=builder

            deck=svc.generate_deck("task")["deck"]

            self.assertEqual(len(builder.calls),1)
            batch=deck["metadata"]["generation_batches"][0]
            self.assertEqual({key:batch[key] for key in ("slide_ids","attempts","retry_count","max_attempts")},{
                "slide_ids":["slide-2","slide-3"],"attempts":1,"retry_count":0,"max_attempts":2,
            })
            self.assertEqual(len(batch["attempt_evidence_hashes"]),1)
            self.assertEqual(len(batch["correction_evidence_hashes"]),0)

    def test_deck_has_no_framework_owned_style_token_rejection(self):
        with tempfile.TemporaryDirectory() as root:
            svc=TaskService(WorkspaceStore(root)); svc.create("task","manual")
            svc.import_input("task",{"goal":"发布","audience":"客户","topic":"方案","页数":3})
            svc.generate_narrative("task"); svc.confirm_narrative("task")
            svc.generate_outline("task"); svc.confirm_outline("task")
            svc.select_samples("task",["slide-1"]); svc.generate_sample("task"); svc.confirm_sample("task")
            builder=LockedThemeRetryBuilder(always_bad=True); svc.builder=builder

            deck=svc.generate_deck("task")["deck"]

            self.assertEqual(len(builder.calls),1)
            self.assertIn("--ink:#fff",deck["html"])

    def test_production_agent_builder_receives_valid_scoped_contract_per_batch(self):
        class ContractAwareClient:
            def __init__(self):
                self.contracts=[]
                self.contract_hashes=[]

            def create(self, **kwargs):
                payload=json.loads(kwargs["input"][1]["content"])
                contract=validate_presentation_technical_contract(payload["presentation_technical_contract"])
                self.assert_batch(payload["slide_ids"],contract)
                if kwargs.get("tool_choice") != "none":
                    return ModelTurn(None,"skill",(
                        ModelToolCall("read_skill_file",'{"path":"SKILL.md"}',f"skill-{len(self.contracts)}"),
                    ))
                self.contracts.append(contract)
                self.contract_hashes.append(payload["presentation_technical_contract_hash"])
                slides=[{
                    "slide_id":slide_id,
                    "html":f'<section class="slide" id="{slide_id}" data-slide-id="{slide_id}"><h1>{slide_id}</h1><p>已生成内容</p></section>',
                } for slide_id in payload["slide_ids"]]
                return ModelTurn(json.dumps({"slides":slides},ensure_ascii=False),f"deck-{len(self.contracts)}")

            @staticmethod
            def assert_batch(slide_ids, contract):
                if contract["slide_ids"] != slide_ids:
                    raise AssertionError("batch PresentationTechnicalContract did not match requested slides")

        with tempfile.TemporaryDirectory() as root:
            svc=TaskService(WorkspaceStore(root)); svc.create("task","manual")
            svc.import_input("task",{"goal":"发布","audience":"客户","topic":"方案","页数":8})
            svc.generate_narrative("task"); svc.confirm_narrative("task")
            svc.generate_outline("task"); svc.confirm_outline("task")
            svc.select_samples("task",["slide-2","slide-7"]); svc.generate_sample("task"); svc.confirm_sample("task")
            full_contract=svc.design_contract_view("task")
            client=ContractAwareClient()
            svc.builder=AgentGateway(client,skill=SkillRuntime.builtin(),max_steps=4,max_tool_calls=4,max_provider_calls=4)

            deck=svc.generate_deck("task")["deck"]

            self.assertEqual(
                [contract["slide_ids"] for contract in client.contracts],
                [["slide-1","slide-3","slide-4"],["slide-5","slide-6","slide-8"]],
            )
            self.assertTrue(all(contract["contract_id"] != full_contract["contract_id"] for contract in client.contracts))
            self.assertEqual(client.contract_hashes,[full_contract["hash"],full_contract["hash"]])
            self.assertEqual(list(deck["metadata"]["page_hashes"]),[f"slide-{index}" for index in range(1,9)])

    def test_html_generation_jobs_have_ten_minute_budget_plus_save_tail(self):
        self.assertEqual(OPERATION_BUDGET_SECONDS["samples.generate"],630)
        self.assertEqual(OPERATION_BUDGET_SECONDS["samples.modify"],630)
        self.assertEqual(OPERATION_BUDGET_SECONDS["deck.generate"],630)
        self.assertEqual(OPERATION_BUDGET_SECONDS["deck.modify"],630)
        self.assertEqual(OPERATION_BUDGET_SECONDS["inspection.run"],630)

    def test_confirmed_nested_section_is_preserved_by_original_bytes_and_sha256(self):
        class NestedBuilder:
            def build(self,outline,**context):
                source=render(outline,context["slide_ids"],context.get("rules"),context.get("exceptions"),context.get("assets"))
                if context.get("action") != "sample": return source
                fragments=TaskService._slide_fragments(source)
                sid=context["slide_ids"][0]; original=fragments[sid]
                nested=original.replace("</section>",'<section class="detail"><span>tail-marker</span></section></section>',1)
                return TaskService._replace_slide_fragments(source,{sid:nested})
        with tempfile.TemporaryDirectory() as root:
            svc=TaskService(WorkspaceStore(root),builder=NestedBuilder()); svc.create("nested","manual")
            svc.import_input("nested",{"goal":"发布","audience":"客户","topic":"方案","页数":3})
            svc.generate_narrative("nested"); svc.confirm_narrative("nested")
            svc.generate_outline("nested"); svc.confirm_outline("nested")
            svc.select_samples("nested",["slide-1"]); svc.generate_sample("nested")
            sample=svc.sample_view("nested")["sample"]
            nested=svc._slide_fragments(sample["html"])["slide-1"]
            svc.confirm_sample("nested")
            confirmation=svc.sample_view("nested")["confirmation"]
            page=confirmation["confirmed_pages"]["slide-1"]
            self.assertEqual(page["html"],nested)
            self.assertEqual(page["sha256"],hashlib.sha256(nested.encode()).hexdigest())
            deck=svc.generate_deck("nested")["deck"]
            self.assertEqual(svc._slide_fragments(deck["html"])["slide-1"],nested)
            self.assertIn("tail-marker",deck["html"])

    def test_modify_inputs_contain_fragments_but_not_public_shell(self):
        with tempfile.TemporaryDirectory() as root:
            svc=TaskService(WorkspaceStore(root)); svc.create("modify","manual")
            svc.import_input("modify",{"goal":"发布","audience":"客户","topic":"方案","页数":3})
            svc.generate_narrative("modify"); svc.confirm_narrative("modify")
            svc.generate_outline("modify"); svc.confirm_outline("modify")
            svc.select_samples("modify",["slide-1"]); svc.generate_sample("modify")
            builder=BatchBuilder(); svc.builder=builder
            svc.modify_sample("modify","标题更醒目")
            call=builder.calls[-1]
            self.assertNotIn("previous_html",call)
            self.assertIn('<section class="slide',call["previous_slides"])
            self.assertNotIn("<!doctype html>",call["previous_slides"].lower())

    def test_provider_request_budget_caps_empty_response_retries_at_two(self):
        class SDK:
            def __init__(self): self.responses=self; self.calls=0
            def create(self,**_kwargs):
                self.calls+=1
                return type("Response",(),{"output_text":"","output":[],"id":f"r{self.calls}"})()
        config=type("Config",(),{"model":"m","api_key":"k","base_url":"https://example.com","timeout_seconds":1,"structured_output":"prompt"})()
        sdk=SDK(); client=OpenAIResponsesClient(config,sdk_client=sdk)
        with self.assertRaisesRegex(Exception,"真实请求次数"):
            client.create(input=[],provider_call_limit=2)
        self.assertEqual(sdk.calls,2)

    def test_provider_request_timeout_is_never_widened_to_the_stage_deadline(self):
        class SDK:
            def __init__(self): self.responses=self; self.timeout=None
            def create(self,**kwargs):
                self.timeout=kwargs["timeout"]
                return type("Response",(),{"output_text":"ok","output":[],"id":"r"})()
        config=type("Config",(),{"model":"m","api_key":"k","base_url":"https://example.com","request_timeout_seconds":180,"structured_output":"prompt"})()
        sdk=SDK(); client=OpenAIResponsesClient(config,sdk_client=sdk)

        client.create(input=[],timeout_seconds=600)

        self.assertEqual(sdk.timeout,180)

    def test_shared_client_keeps_concurrent_stage_budgets_private(self):
        class SDK:
            def __init__(self):
                self.responses=self; self.calls={"task-a":0,"task-b":0}
                self.a_empty=threading.Event(); self.b_done=threading.Event()
                self.lock=threading.Lock()
            def create(self,**kwargs):
                serialized=json.dumps(kwargs["input"])
                task="task-a" if "task-a" in serialized else "task-b"
                if kwargs.get("tool_choice") == {"type":"function", "name":"read_skill_file"}:
                    call=type("Call",(),{"type":"function_call","name":"read_skill_file","arguments":'{"path":"SKILL.md"}',"call_id":f"{task}-skill"})()
                    return type("Response",(),{"output_text":"","output":[call],"id":f"{task}-skill"})()
                with self.lock:
                    self.calls[task]+=1; attempt=self.calls[task]
                if task == "task-a" and attempt == 1:
                    self.a_empty.set()
                    return type("Response",(),{"output_text":"","output":[],"id":"a-empty"})()
                if task == "task-a":
                    self.assert_interleaving()
                    return type("Response",(),{"output_text":"{}","output":[],"id":"a-invalid"})()
                self.b_done.set()
                text='{"markdown":"ok"}'
                return type("Response",(),{"output_text":text,"output":[],"id":"b-valid"})()
            def assert_interleaving(self):
                if not self.b_done.wait(2):
                    raise AssertionError("task B did not interleave before task A schema correction")

        config=type("Config",(),{"model":"m","api_key":"k","base_url":"https://example.com","timeout_seconds":3,"structured_output":"prompt"})()
        sdk=SDK(); client=OpenAIResponsesClient(config,sdk_client=sdk)
        def run(task):
            return AgentRuntime(client,SkillRuntime.builtin(),max_provider_calls=3).run("narrative",{"task":task})
        with ThreadPoolExecutor(max_workers=2) as pool:
            future_a=pool.submit(run,"task-a")
            self.assertTrue(sdk.a_empty.wait(2))
            future_b=pool.submit(run,"task-b")
            self.assertEqual(future_b.result(timeout=3).value["markdown"],"ok")
            with self.assertRaisesRegex(Exception,"真实请求次数"):
                future_a.result(timeout=3)
        self.assertEqual(sdk.calls,{"task-a":2,"task-b":1})


if __name__ == "__main__": unittest.main()
