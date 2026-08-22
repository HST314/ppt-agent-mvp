#!/usr/bin/env python3
"""Fail the release when retired Skill/design gate implementations return."""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "ppt_agent"
RETIRED_FILES = (
    "ppt_agent/canonical_validator.py",
    "ppt_agent/layout_structure.py",
    "ppt_agent/generation_preflight.py",
)
RETIRED_SYMBOLS = (
    "DirectorySkillLoader",
    "JsonHttpModelGateway",
    "ModelHtmlBuilder",
    "gateways_from_env",
    "structured_canonical_blockers",
    "layout_capacity_policy",
    "inspect_layout_capacity",
    "run_canonical_validator",
    "run_layout_structure_validator",
    "canonical_validation",
    "template-registry.json",
    "validate-swiss-deck",
    "planning-summary.md",
    "design-pack-v1.md",
)
BUSINESS_PATHS = (
    "ppt_agent/agent_runtime.py",
    "ppt_agent/gateways.py",
    "ppt_agent/service.py",
    "ppt_agent/design_contract.py",
    "ppt_agent/p4.py",
    "ppt_agent/render_gate.py",
)


def verify() -> dict:
    failures = []
    for relative in RETIRED_FILES:
        if (ROOT / relative).exists():
            failures.append(f"retired file exists: {relative}")

    sources = {
        str(path.relative_to(ROOT)): path.read_text(encoding="utf-8")
        for path in PACKAGE.rglob("*.py")
        if "builtin_skills" not in path.parts
    }
    for relative, source in sources.items():
        for symbol in RETIRED_SYMBOLS:
            if symbol in source:
                failures.append(f"retired symbol {symbol!r} in {relative}")
        if "guizang-ppt" in source.casefold():
            failures.append(f"active Skill name hard-coded in {relative}")

    runtime_assets = {
        str(path.relative_to(ROOT)): path.read_text(encoding="utf-8")
        for suffix in ("*.js", "*.html", "*.css")
        for path in PACKAGE.rglob(suffix)
        if "builtin_skills" not in path.parts
    }
    for relative, source in runtime_assets.items():
        if "guizang-ppt" in source.casefold():
            failures.append(f"active Skill name hard-coded in {relative}")

    business_source = "\n".join(sources[path] for path in BUSINESS_PATHS)
    if re.search(r"\bS(?:0[1-9]|1[0-9]|2[0-2])\b", business_source):
        failures.append("fixed layout identifier found in business runtime")
    for call in ("SkillRuntime.builtin(", "ActiveSkillResolver.builtin("):
        if call in business_source:
            failures.append(f"business runtime constructs an implicit Skill via {call}")

    config_source = (ROOT / "ppt_agent/config.py").read_text(encoding="utf-8")
    for flag in ("skill_runtime_v2", "technical_gate_v2"):
        if flag not in config_source:
            failures.append(f"missing rollout flag: {flag}")

    result = {
        "status": "failed" if failures else "ok",
        "scanned_python_files": len(sources),
        "scanned_runtime_assets": len(runtime_assets),
        "retired_files_absent": not any((ROOT / item).exists() for item in RETIRED_FILES),
        "feature_flags": ["skill_runtime_v2", "technical_gate_v2"],
        "failures": failures,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return result


if __name__ == "__main__":
    raise SystemExit(1 if verify()["failures"] else 0)
