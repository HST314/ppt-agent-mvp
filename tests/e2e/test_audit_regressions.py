from e2e.support import SampleJourney
from ppt_agent.errors import ConflictError
from ppt_agent.fsm import TaskState
from ppt_agent.p4 import render
from ppt_agent.service import TaskService


class ProbeBuilder:
    def __init__(self): self.calls=[]
    def build(self, outline, **context):
        self.calls.append(context)
        return render(outline, context["slide_ids"], context.get("rules", []), context.get("exceptions", {}), context.get("assets", {}))


class AuditRegressionTests(SampleJourney):
    def test_cancelled_rejects_every_planning_write(self):
        self.app.service.command("journey", "audit-cancel", "cancel", "user")
        for action in (
            lambda:self.app.service.edit_narrative("journey", "# forbidden"),
            lambda:self.app.service.edit_outline("journey", self.app.service.planning_view("journey")["outline"]["markdown"]),
            lambda:self.app.service.rollback_planning("journey", "outline", self.app.service._current_version("journey", "outline")),
        ):
            with self.assertRaises(ConflictError): action()

    def test_outline_edit_requires_confirmation_of_exact_hash(self):
        outline=self.app.service.planning_view("journey")["outline"]["markdown"]
        self.app.service.edit_outline("journey", outline + "\n<!-- changed -->\n")
        with self.assertRaises(ConflictError): self.app.service.select_samples("journey")
        self.app.service.confirm_outline("journey")
        self.app.service.select_samples("journey")

    def test_real_builder_receives_stage_and_confirmed_sample(self):
        probe=ProbeBuilder(); self.app.service.builder=probe
        self.app.service.generate_sample("journey")
        self.app.service.modify_sample("journey", "标题更醒目")
        self.app.service.confirm_sample("journey")
        self.app.service.generate_deck("journey")
        self.app.service.modify_deck("journey", "统一留白")
        self.assertEqual([x["action"] for x in probe.calls], ["sample", "sample", "deck", "deck"])
        self.assertIn("confirmed_sample_html", probe.calls[2])

    def test_auto_fix_publishes_changed_current_html_and_rechecks_it(self):
        self.app.service.inspector.calls=[]
        self.app.service.generate_sample("journey"); self.app.service.confirm_sample("journey")
        self.app.service.generate_deck("journey")
        before=self.app.service.deck_view("journey")["deck"]
        report=self.app.service.inspection_view("journey")["report"]
        fixed=self.app.service._auto_fix("journey", report, 1)
        self.assertNotEqual(before["hash"], fixed["hash"])
        self.assertNotEqual(before["html"], fixed["html"])
        self.app.service._inspect_once("journey", "incremental", list(fixed["metadata"]["page_hashes"]), 1)
        self.assertEqual(self.app.service.inspector.calls[-1]["html"], fixed["html"])
