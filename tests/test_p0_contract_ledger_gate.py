import json
import tempfile
import unittest

from ppt_agent.claim_ledger import assert_claims_bound, audit_claims, build_claim_ledger
from ppt_agent.design_contract import TemplateRegistry
from ppt_agent.errors import ValidationError
from ppt_agent.p2 import now
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore


class PassingInspector:
    requires_browser_evidence = True

    def inspect(self, _outline, _html, *, browser_evidence=None):
        return {"passed": True, "issues": [], "model": "passing", "browser_received": browser_evidence is not None}


class PassingGenerationBrowser:
    enforce_on_generation = True

    def inspect(self, _html, expected_slide_ids):
        return {
            "available": True,
            "passed": True,
            "engine": "chromium",
            "engine_version": "contract-test",
            "viewport": {"width": 1280, "height": 720},
            "issues": [],
            "slides": [{"slide_id": slide_id} for slide_id in expected_slide_ids],
        }


class OverflowGenerationBrowser(PassingGenerationBrowser):
    def inspect(self, _html, expected_slide_ids):
        result = super().inspect(_html, expected_slide_ids)
        result.update({
            "passed": False,
            "issues": [{
                "issue_id": "overflow",
                "severity": "blocker",
                "level": "element",
                "code": "content_out_of_bounds",
                "message": "overflow",
                "slide_id": expected_slide_ids[0],
                "element_id": "body",
                "evidence": "35px",
                "suggestion": "fit",
            }],
        })
        return result


class ContractLedgerGateTests(unittest.TestCase):
    def _service(self, root, browser=None):
        return TaskService(
            WorkspaceStore(root),
            inspector=PassingInspector(),
            browser_inspector=browser or PassingGenerationBrowser(),
        )

    def _to_outline(self, service, task_id="task"):
        service.create(task_id, "manual")
        service.import_input(task_id, {
            "goal": "批准扩容预算",
            "audience": "CEO 与 CFO",
            "topic": "AI 客服试点复盘",
            "页数": 4,
            "风格": "严格使用风格 B · 瑞士国际主义",
            "known_facts": {"budget": "80 万元", "saving": "季度节省 36 万元"},
        })
        service.generate_narrative(task_id)
        service.confirm_narrative(task_id)
        service.generate_outline(task_id)
        service.confirm_outline(task_id)

    def test_real_registry_selects_hash_locked_swiss_template(self):
        registry = TemplateRegistry()
        selected = registry.select({"constraints": {"风格": "Style B 瑞士国际主义"}})

        self.assertEqual(selected.style_id, "swiss")
        self.assertEqual(selected.asset_path, "assets/template-swiss.html")
        self.assertEqual(len(selected.allowed_layouts), 22)
        self.assertEqual(selected.template_hash, registry.skill.manifest[selected.asset_path])

    def test_claim_ledger_binds_source_and_accepts_only_auditable_derivation(self):
        ledger = build_claim_ledger(
            task_id="task",
            input_snapshot_hash="a" * 64,
            source_binding={"known_facts": ["软件 12 万元", "服务 8 万元"]},
            created_at=now(),
        )
        bound = audit_claims("预算为 12 万元，服务为 8 万元，合计 12万元+8万元=20万元。", ledger)

        self.assertTrue(bound["passed"])
        self.assertTrue(any(item["status"] == "derived" and item["value"].replace(" ", "") == "20万元" for item in bound["bindings"]))
        with self.assertRaises(ValidationError):
            assert_claims_bound("预计 7 个月回本。", ledger, "叙事")

    def test_contract_and_ledger_hashes_survive_every_delivery_stage(self):
        with tempfile.TemporaryDirectory() as root:
            service = self._service(root)
            self._to_outline(service)
            service.select_samples("task", ["slide-1", "slide-3"])
            sample = service.generate_sample("task")["sample"]
            contract = service.design_contract_view("task")
            ledger = service.claim_ledger_view("task")

            self.assertEqual(contract["style_id"], "swiss")
            self.assertEqual(sample["metadata"]["design_contract_hash"], contract["hash"])
            self.assertEqual(sample["metadata"]["claim_ledger_hash"], ledger["hash"])
            service.confirm_sample("task")
            deck = service.generate_deck("task")["deck"]
            gate = deck["metadata"]["post_render_gate"]
            self.assertEqual(gate["blocker_count"], 0)
            self.assertEqual(gate["geometry"]["overflow_count"], 0)
            self.assertEqual(gate["layout"]["layout_registration_percent"], 100)
            self.assertEqual(gate["claims"]["unbound_count"], 0)

            inspection = service.run_inspection("task", 0)["report"]
            self.assertEqual(inspection["metadata"]["design_contract_hash"], contract["hash"])
            self.assertEqual(inspection["metadata"]["claim_ledger_hash"], ledger["hash"])
            finalization = service.finalize_deck("task", deck["hash"], "review")["finalization"]
            self.assertEqual(finalization["design_contract_hash"], contract["hash"])
            self.assertEqual(finalization["claim_ledger_hash"], ledger["hash"])
            delivery = service.publish_delivery("task")["delivery"]
            delivery_record = service.versions("task", "delivery")[-1]
            self.assertEqual(delivery_record["metadata"]["design_contract_hash"], contract["hash"])
            self.assertEqual(delivery_record["metadata"]["claim_ledger_hash"], ledger["hash"])
            self.assertIn("design-contract.json", delivery["files"])
            self.assertIn("claim-ledger.json", delivery["files"])

    def test_unbound_narrative_is_rejected_before_a_new_artifact_is_saved(self):
        with tempfile.TemporaryDirectory() as root:
            service = self._service(root)
            service.create("task", "manual")
            service.import_input("task", {"goal": "汇报", "audience": "管理层", "topic": "试点"})
            service.generate_narrative("task")
            before = len(service.versions("task", "narrative"))

            with self.assertRaisesRegex(ValidationError, "未绑定事实"):
                service.edit_narrative("task", "# 叙事\n\n预计 7 个月回本。")

            self.assertEqual(len(service.versions("task", "narrative")), before)

    def test_generation_overflow_is_rejected_before_sample_artifact_is_saved(self):
        with tempfile.TemporaryDirectory() as root:
            service = self._service(root, OverflowGenerationBrowser())
            self._to_outline(service)
            service.select_samples("task", ["slide-1"])

            with self.assertRaisesRegex(ValidationError, "渲染后硬门禁未通过"):
                service.generate_sample("task")

            self.assertFalse(service.versions("task", "sample"))


if __name__ == "__main__":
    unittest.main()
