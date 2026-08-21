import json
import hashlib
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from ppt_agent.agent_runtime import AgentRuntime
from ppt_agent.errors import GatewayError
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


class P0GenerationRefactorTests(unittest.TestCase):
    def test_sample_exposes_only_read_only_skill_tools_and_server_assembles_public_shell(self):
        fragment='<section class="slide" id="slide-1" data-slide-id="slide-1"><h1>样品</h1></section>'
        client=RecordingClient([
            ModelTurn(None,"skill",(ModelToolCall("read_skill_file",'{"path":"references/design-pack-v1.md"}',"skill-call"),)),
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
        self.assertEqual(path_schema["enum"],["references/design-pack-v1.md"])
        self.assertNotIn("<!doctype html>",client.inputs[0]["input"][1]["content"])
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn(fragment,html)
        self.assertIn('name="ppt-template"',html)
        self.assertIn('assets/template.html#',html)
        self.assertNotIn('linear-gradient(135deg,#172033,#253858)',html)
        self.assertEqual(gateway.runtime.last_audit[-1]["applied_skill_files"],["references/design-pack-v1.md"])
        tool_output=next(item for item in client.inputs[1]["input"] if item.get("type")=="function_call_output")
        contract=json.loads(tool_output["output"])
        self.assertGreaterEqual(contract["bytes"],10000)
        for required_rule in (
            "不可违反的事实边界",
            "每批次先冻结微型 DesignContract",
            "允许的布局 archetype",
            "动效 contract",
            "返回前的确定性自检",
        ):
            self.assertIn(required_rule,contract["content"])
        self.assertIn("最小但完整的 Generation Contract",client.inputs[0]["input"][0]["content"])

    def test_sample_cannot_finish_without_applying_required_design_pack(self):
        fragment='<section class="slide" id="slide-1" data-slide-id="slide-1"><h1>样品</h1></section>'
        client=RecordingClient([ModelTurn(json.dumps({"slides":[{"slide_id":"slide-1","html":fragment}]}),"r1")])

        with self.assertRaises(GatewayError) as caught:
            AgentRuntime(client,SkillRuntime.builtin()).run("sample",{"slide_ids":["slide-1"]})

        self.assertEqual(caught.exception.code,"agent_required_skill_missing")
        self.assertEqual(caught.exception.audit[-1]["unique_skill_files"],0)

    def test_sample_reads_complete_contract_once_and_deduplicates_same_round(self):
        fragment='<section class="slide" id="slide-1" data-slide-id="slide-1"><h1>样品</h1></section>'
        client=RecordingClient([
            ModelTurn(None,"r1",(
                ModelToolCall("read_skill_file",'{"path":"references/design-pack-v1.md"}',"c1"),
                ModelToolCall("read_skill_file",'{"path":"references/design-pack-v1.md"}',"c2"),
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
            ModelTurn(None,"r1",(ModelToolCall("read_skill_file",'{"path":"references/design-pack-v1.md"}',"c1"),)),
            ModelTurn(None,"r2",(ModelToolCall("read_skill_file",'{"path":"references/design-pack-v1.md"}',"c2"),)),
            ModelTurn(json.dumps({"slides":[{"slide_id":"slide-1","html":fragment}]}),"r3"),
        ])
        gateway=AgentGateway(client,skill=SkillRuntime.builtin(),max_steps=30,max_tool_calls=40,max_provider_calls=8)

        html=gateway.build("## [slide-1] 样品",action="sample",slide_ids=["slide-1"])

        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertEqual(client.inputs[1]["tool_choice"],"none")
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
            return AgentRuntime(client,SkillRuntime.builtin(),max_provider_calls=2).run("narrative",{"task":task})
        with ThreadPoolExecutor(max_workers=2) as pool:
            future_a=pool.submit(run,"task-a")
            self.assertTrue(sdk.a_empty.wait(2))
            future_b=pool.submit(run,"task-b")
            self.assertEqual(future_b.result(timeout=3).value["markdown"],"ok")
            with self.assertRaisesRegex(Exception,"真实请求次数"):
                future_a.result(timeout=3)
        self.assertEqual(sdk.calls,{"task-a":2,"task-b":1})


if __name__ == "__main__": unittest.main()
