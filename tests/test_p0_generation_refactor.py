import json
import hashlib
import tempfile
import unittest

from ppt_agent.gateways import AgentGateway
from ppt_agent.model_clients import ModelTurn
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
    def test_sample_is_tool_free_and_server_assembles_public_shell(self):
        fragment='<section class="slide" id="slide-1" data-slide-id="slide-1"><h1>样品</h1></section>'
        client=RecordingClient([ModelTurn(json.dumps({"slides":[{"slide_id":"slide-1","html":fragment}]}),"r1")])
        gateway=AgentGateway(client,skill=SkillRuntime.builtin(),max_steps=100)
        html=gateway.build("## [slide-1] 样品",action="sample",slide_ids=["slide-1"])
        self.assertEqual(len(client.inputs),1)
        self.assertEqual(client.inputs[0]["tools"],[])
        self.assertNotIn("<!doctype html>",client.inputs[0]["input"][1]["content"])
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn(fragment,html)

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

    def test_latency_stub_deadlines_are_90_and_180_seconds(self):
        self.assertEqual(OPERATION_BUDGET_SECONDS["samples.generate"],90)
        self.assertEqual(OPERATION_BUDGET_SECONDS["samples.modify"],90)
        self.assertEqual(OPERATION_BUDGET_SECONDS["deck.generate"],180)
        self.assertEqual(OPERATION_BUDGET_SECONDS["deck.modify"],180)
        self.assertEqual(OPERATION_BUDGET_SECONDS["inspection.run"],90)

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
            self.assertIn('<section class="slide"',call["previous_slides"])
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


if __name__ == "__main__": unittest.main()
