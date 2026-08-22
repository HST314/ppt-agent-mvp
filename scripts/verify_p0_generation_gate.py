#!/usr/bin/env python3
"""Reproduce the P0 generation acceptance gate with real Chromium."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ppt_agent.browser_inspection import ChromiumDeckInspector  # noqa: E402
from ppt_agent.render_gate import canonical_post_render_evidence  # noqa: E402
from ppt_agent.offline import verify_delivery  # noqa: E402
from ppt_agent.service import TaskService  # noqa: E402
from ppt_agent.store import WorkspaceStore  # noqa: E402


class PassingInspector:
    def inspect(self, _outline, _html, *, browser_evidence=None):
        return {"passed": True, "issues": [], "model": "p0-verifier"}


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ppt-p0-gate-") as data_root:
        service = TaskService(
            WorkspaceStore(data_root),
            inspector=PassingInspector(),
            browser_inspector=ChromiumDeckInspector(),
        )
        task_id = "p0-generation-gate"
        service.create(task_id, "manual")
        service.import_input(task_id, {
            "goal": "批准 AI 客服扩容预算",
            "audience": "CEO 与 CFO",
            "topic": "AI 客服试点复盘",
            "页数": 4,
            "风格": "由当前 Skill 自主定义完整视觉语言",
            "known_facts": {"budget": "80 万元", "saving": "季度节省 36 万元"},
        })
        service.generate_narrative(task_id)
        service.confirm_narrative(task_id)
        service.generate_outline(task_id)
        service.confirm_outline(task_id)
        service.select_samples(task_id, ["slide-1", "slide-3"])
        sample = service.generate_sample(task_id)["sample"]
        service.confirm_sample(task_id)
        deck = service.generate_deck(task_id)["deck"]
        contract = service.design_contract_view(task_id)
        gate = deck["metadata"]["post_render_gate"]
        evidence = service.version(task_id, gate["evidence_hash"])
        service.run_inspection(task_id, 0)
        finalization = service.finalize_deck(task_id, deck["hash"], "review")["finalization"]
        delivery = service.publish_delivery(task_id)["delivery"]
        delivery_root = service.store.delivery_root(task_id, delivery["delivery_id"])
        packaged_evidence = (delivery_root / "post-render-gate-evidence.json").read_bytes()
        offline_files = verify_delivery(delivery_root)
        result = {
            "sample_gate_passed": sample["metadata"]["post_render_gate"]["passed"],
            "deck_gate_passed": gate["passed"],
            "blocker_count": gate["blocker_count"],
            "overflow_count": gate["geometry"]["overflow_count"],
            "technical_binding_percent": gate["layout"]["technical_binding_percent"],
            "unbound_count": gate["claims"]["unbound_count"],
            "missing_required_count": gate["claims"]["missing_required_count"],
            "required_claim_coverage_percent": round(
                gate["claims"]["covered_required_count"] * 100 / gate["claims"]["required_count"], 2
            ) if gate["claims"]["required_count"] else 100,
            "browser_available": gate["geometry"]["available"],
            "browser_engine": gate["geometry"]["engine"],
            "browser_engine_version": gate["geometry"]["engine_version"],
            "contract_type": contract["contract_type"],
            "canvas": contract["canvas"],
            "contract_has_design_fields": bool({"style_id", "template_id", "theme_id", "slide_contracts"}.intersection(contract)),
            "sample_deck_intent_reused": sample["metadata"]["design_intent"] == deck["metadata"]["design_intent"],
            "sample_deck_assets_reused": sample["metadata"]["shared_design_assets"] == deck["metadata"]["shared_design_assets"],
            "presentation_technical_contract_hash": gate["presentation_technical_contract_hash"],
            "design_contract_hash": gate["design_contract_hash"],
            "claim_ledger_hash": gate["claim_ledger_hash"],
            "evidence_hash": gate["evidence_hash"],
            "evidence_recomputed": hashlib.sha256(canonical_post_render_evidence(gate)).hexdigest() == gate["evidence_hash"],
            "evidence_artifact_persisted": evidence == canonical_post_render_evidence(gate),
            "autofit_evidence_present": "overflow_autofit" in json.loads(evidence),
            "finalization_evidence_hash_verified": finalization["post_render_gate_hash"] == gate["evidence_hash"],
            "delivery_evidence_included": packaged_evidence == evidence,
            "delivery_evidence_hash_verified": hashlib.sha256(packaged_evidence).hexdigest() == gate["evidence_hash"],
            "offline_delivery_verified": bool(offline_files),
        }
        expected = {
            "sample_gate_passed": True,
            "deck_gate_passed": True,
            "blocker_count": 0,
            "overflow_count": 0,
            "technical_binding_percent": 100,
            "unbound_count": 0,
            "missing_required_count": 0,
            "required_claim_coverage_percent": 100,
            "browser_available": True,
            "browser_engine": "chromium",
            "contract_type": "presentation_technical_contract",
            "canvas": {"width": 1280, "height": 720, "aspect_ratio": "16:9"},
            "contract_has_design_fields": False,
            "sample_deck_intent_reused": True,
            "sample_deck_assets_reused": True,
            "evidence_recomputed": True,
            "evidence_artifact_persisted": True,
            "autofit_evidence_present": True,
            "finalization_evidence_hash_verified": True,
            "delivery_evidence_included": True,
            "delivery_evidence_hash_verified": True,
            "offline_delivery_verified": True,
        }
        failures = {name: {"expected": value, "actual": result.get(name)} for name, value in expected.items() if result.get(name) != value}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        if failures:
            print(json.dumps({"failures": failures}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
