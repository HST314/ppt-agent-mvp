#!/usr/bin/env python3
"""Reproduce the P0 generation acceptance gate with real Chromium."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ppt_agent.browser_inspection import ChromiumDeckInspector  # noqa: E402
from ppt_agent.service import TaskService  # noqa: E402
from ppt_agent.store import WorkspaceStore  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ppt-p0-gate-") as data_root:
        service = TaskService(
            WorkspaceStore(data_root),
            browser_inspector=ChromiumDeckInspector(),
        )
        task_id = "p0-generation-gate"
        service.create(task_id, "manual")
        service.import_input(task_id, {
            "goal": "批准 AI 客服扩容预算",
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
        service.select_samples(task_id, ["slide-1", "slide-3"])
        sample = service.generate_sample(task_id)["sample"]
        service.confirm_sample(task_id)
        deck = service.generate_deck(task_id)["deck"]
        contract = service.design_contract_view(task_id)
        gate = deck["metadata"]["post_render_gate"]
        result = {
            "sample_gate_passed": sample["metadata"]["post_render_gate"]["passed"],
            "deck_gate_passed": gate["passed"],
            "blocker_count": gate["blocker_count"],
            "overflow_count": gate["geometry"]["overflow_count"],
            "layout_registration_percent": gate["layout"]["layout_registration_percent"],
            "unbound_count": gate["claims"]["unbound_count"],
            "browser_available": gate["geometry"]["available"],
            "browser_engine": gate["geometry"]["engine"],
            "browser_engine_version": gate["geometry"]["engine_version"],
            "style_id": contract["style_id"],
            "template_id": contract["template_id"],
            "design_contract_hash": gate["design_contract_hash"],
            "claim_ledger_hash": gate["claim_ledger_hash"],
            "evidence_hash": gate["evidence_hash"],
        }
        expected = {
            "sample_gate_passed": True,
            "deck_gate_passed": True,
            "blocker_count": 0,
            "overflow_count": 0,
            "layout_registration_percent": 100,
            "unbound_count": 0,
            "browser_available": True,
            "browser_engine": "chromium",
            "style_id": "swiss",
        }
        failures = {name: {"expected": value, "actual": result.get(name)} for name, value in expected.items() if result.get(name) != value}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        if failures:
            print(json.dumps({"failures": failures}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
