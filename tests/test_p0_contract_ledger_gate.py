import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ppt_agent.claim_ledger import assert_claims_bound, audit_claims, build_claim_ledger
from ppt_agent.design_contract import TemplateRegistry
from ppt_agent.errors import ConflictError, ValidationError
from ppt_agent.p2 import canonical, digest, now
from ppt_agent.render_gate import canonical_post_render_evidence
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


class AutofitGenerationBrowser(OverflowGenerationBrowser):
    def __init__(self):
        self.calls = 0

    def inspect(self, _html, expected_slide_ids):
        self.calls += 1
        if self.calls == 1:
            return super().inspect(_html, expected_slide_ids)
        return PassingGenerationBrowser.inspect(self, _html, expected_slide_ids)


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

    def _to_deck(self, service, task_id="task"):
        self._to_outline(service, task_id)
        service.select_samples(task_id, ["slide-1", "slide-3"])
        service.generate_sample(task_id)
        service.confirm_sample(task_id)
        return service.generate_deck(task_id)["deck"]

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
            self.assertEqual(canonical_post_render_evidence(gate), service.version("task", gate["evidence_hash"]))
            self.assertIn(gate["evidence_hash"], {item["hash"] for item in service.versions("task", "post-render-gate-evidence")})

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
            self.assertIn("post-render-gate-evidence.json", delivery["files"])
            delivery_root = service.store.delivery_root("task", delivery["delivery_id"])
            packaged_evidence = (delivery_root / "post-render-gate-evidence.json").read_bytes()
            self.assertEqual(packaged_evidence, canonical_post_render_evidence(gate))
            self.assertEqual(digest(packaged_evidence), gate["evidence_hash"])

    def test_autofit_is_part_of_the_hashed_persisted_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            browser = AutofitGenerationBrowser()
            service = self._service(root, browser)
            self._to_outline(service)
            service.select_samples("task", ["slide-1"])
            fitted = {
                "available": True,
                "html": None,
                "rules": [{"slide_id": "slide-1", "element_id": "body", "font_size": 18}],
                "rounds": 1,
                "converged": True,
                "remaining": [],
            }

            def fit(html_text, max_rounds):
                self.assertEqual(max_rounds, 2)
                return {**fitted, "html": html_text}

            with patch("ppt_agent.service.fit_deck_html", side_effect=fit):
                sample = service.generate_sample("task")["sample"]

            gate = sample["metadata"]["post_render_gate"]
            self.assertEqual(gate["overflow_autofit"], {key: value for key, value in fitted.items() if key not in {"available", "html"}})
            raw = canonical_post_render_evidence(gate)
            self.assertEqual(digest(raw), gate["evidence_hash"])
            self.assertEqual(service.version("task", gate["evidence_hash"]), raw)

    def test_finalize_rejects_tampered_gate_evidence_without_saving_a_fact(self):
        with tempfile.TemporaryDirectory() as root:
            service = self._service(root)
            deck = self._to_deck(service)
            service.run_inspection("task", 0)
            evidence_hash = deck["metadata"]["post_render_gate"]["evidence_hash"]
            evidence_path = Path(root) / "task" / "artifacts" / evidence_hash
            tampered = json.loads(evidence_path.read_bytes())
            tampered["blocker_count"] = 1
            evidence_path.write_bytes(canonical(tampered))

            with self.assertRaisesRegex(ConflictError, "evidence 哈希重算不一致"):
                service.finalize_deck("task", deck["hash"], "review")

            self.assertFalse(service.versions("task", "final-deck"))

    def test_delivery_rejects_missing_gate_evidence_without_writing_a_package(self):
        with tempfile.TemporaryDirectory() as root:
            service = self._service(root)
            deck = self._to_deck(service)
            service.run_inspection("task", 0)
            service.finalize_deck("task", deck["hash"], "review")
            evidence_hash = deck["metadata"]["post_render_gate"]["evidence_hash"]
            (Path(root) / "task" / "artifacts" / evidence_hash).unlink()

            with self.assertRaisesRegex(ConflictError, "evidence 工件缺失"):
                service.publish_delivery("task")

            self.assertFalse(service.versions("task", "delivery"))

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
