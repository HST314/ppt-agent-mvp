from e2e.support import SampleJourney
from ppt_agent.errors import ConflictError, GatewayUnknownResult
from ppt_agent.fsm import TaskState
from ppt_agent.p4 import render
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore

import tempfile


class ProbeBuilder:
    def __init__(self): self.calls=[]
    def build(self, outline, **context):
        self.calls.append(context)
        return render(outline, context["slide_ids"], context.get("rules", []), context.get("exceptions", {}), context.get("assets", {}))

class UnknownOnRecheck:
    def __init__(self): self.calls=0
    def inspect(self,outline,html):
        self.calls+=1
        if self.calls==1:
            return {"passed":False,"issues":[{"issue_id":"x","severity":"warning","slide_id":"slide-1","suggestion":"fix"}]}
        raise GatewayUnknownResult("repair recheck unknown")


class AuditRegressionTests(SampleJourney):
    def test_latest_disposition_overrides_older_waiver(self):
        self.app.service.generate_sample("journey"); self.app.service.confirm_sample("journey")
        self.app.service.generate_deck("journey"); self.app.service.run_inspection("journey",0)
        waived=self.app.service.dispose_issue("journey","fake-overflow","waive","accepted")
        self.assertTrue(waived["delivery_allowed"])
        deferred=self.app.service.dispose_issue("journey","fake-overflow","defer","")
        self.assertFalse(deferred["delivery_allowed"])

    def test_completed_state_has_no_waiting_action(self):
        self.app.service.generate_sample("journey"); self.app.service.confirm_sample("journey")
        deck=self.app.service.generate_deck("journey")["deck"]
        self.app.service.run_inspection("journey",0)
        self.app.service.dispose_issue("journey","fake-overflow","waive","accepted")
        state=self.app.service.confirm_delivery("journey",deck["hash"])["state"]
        self.assertEqual(state["status"],"completed")
        self.assertIsNone(state["waiting_reason"]); self.assertIsNone(state["required_action"])
        summary=self.app.service.status_summary("journey")
        self.assertIsNone(summary["current_action"]); self.assertEqual(summary["human_actions"],[])

    def test_rebuilt_input_and_summary_use_explicit_current_versions(self):
        with tempfile.TemporaryDirectory() as root:
            service=TaskService(WorkspaceStore(root)); service.create("current")
            first=service.import_input("current",{"goal":"old","audience":"a","topic":"t"})
            second=service.import_input("current",{"goal":"new","audience":"a","topic":"t"},rebuild=True)
            self.assertEqual(service.input_view("current")["snapshot_hash"],second["snapshot_hash"])
            self.assertEqual(service.input_view("current")["task_card"]["goal"],"new")
            self.assertEqual(service.status_summary("current")["latest_artifacts"]["input-snapshot"],second["snapshot_hash"])
            self.assertNotEqual(first["snapshot_hash"],second["snapshot_hash"])

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

    def test_auto_recheck_unknown_rolls_back_deck_report_state_and_events(self):
        self.app.service.generate_sample("journey"); self.app.service.confirm_sample("journey")
        self.app.service.generate_deck("journey")
        self.app.service.switch_inspection_mode("journey","auto")
        self.app.service.inspector=UnknownOnRecheck()
        before={
            "state":self.app.service.get("journey"),
            "deck":[v["hash"] for v in self.app.service.versions("journey","deck")],
            "reports":[v["hash"] for v in self.app.service.versions("journey","inspection")],
            "events":self.app.service.events("journey"),
        }
        with self.assertRaises(GatewayUnknownResult): self.app.service.run_inspection("journey",2)
        self.assertEqual(self.app.service.get("journey"),before["state"])
        self.assertEqual([v["hash"] for v in self.app.service.versions("journey","deck")],before["deck"])
        self.assertEqual([v["hash"] for v in self.app.service.versions("journey","inspection")],before["reports"])
        self.assertEqual(self.app.service.events("journey"),before["events"])
