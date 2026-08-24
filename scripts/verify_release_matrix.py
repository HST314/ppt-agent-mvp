#!/usr/bin/env python3
"""Run the named release lanes locally and in CI."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
LANES = {
    "standard": [PYTHON, "scripts/verify_standard_tests.py"],
    "architecture": [PYTHON, "scripts/verify_release_architecture.py"],
    "contracts": [PYTHON, "scripts/verify_p0.py"],
    "frontend": [PYTHON, "scripts/update_frontend_build.py", "--check"],
    "browser": [PYTHON, "scripts/verify_browser_gate.py"],
    "generation": [PYTHON, "scripts/verify_p0_generation_gate.py"],
    "offline": [
        PYTHON,
        "-m",
        "unittest",
        "tests.test_stage_d_offline",
        "tests.browser.test_ac_16_offline_delivery",
        "-v",
    ],
    "real_model": [PYTHON, "scripts/verify_real_model_release.py"],
}
PROFILES = {
    "standard": ("architecture", "contracts", "frontend", "standard"),
    "chromium": ("browser", "generation", "offline"),
    "full": ("architecture", "contracts", "frontend", "standard", "browser", "generation", "offline"),
    "real-model": ("real_model",),
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the SkillRuntime v2 / TechnicalGate v2 release matrix")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="standard")
    parser.add_argument("--config", type=Path, help="Runtime YAML for the real-model lane")
    parser.add_argument("--env-file", type=Path, help="dotenv file for the real-model lane")
    parser.add_argument("--evidence-file", type=Path, help="Secret-free JSON evidence output for the real-model lane")
    parser.add_argument("--list", action="store_true", help="Print the matrix without running it")
    args = parser.parse_args()
    selected = PROFILES[args.profile]
    if args.list:
        print(json.dumps({"profile": args.profile, "lanes": list(selected)}, ensure_ascii=False, sort_keys=True))
        return 0

    results = []
    for lane in selected:
        command = list(LANES[lane])
        if lane == "real_model":
            if args.config:
                command.extend(("--config", str(args.config)))
            if args.env_file:
                command.extend(("--env-file", str(args.env_file)))
            if args.evidence_file:
                command.extend(("--evidence-file", str(args.evidence_file)))
        started = time.monotonic()
        completed = subprocess.run(command, cwd=ROOT, check=False)
        results.append({
            "lane": lane,
            "status": "passed" if completed.returncode == 0 else "failed",
            "returncode": completed.returncode,
            "elapsed_seconds": round(time.monotonic() - started, 2),
        })
        if completed.returncode:
            print(json.dumps({"profile": args.profile, "status": "failed", "results": results}, ensure_ascii=False, sort_keys=True))
            return completed.returncode
    print(json.dumps({"profile": args.profile, "status": "passed", "results": results}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
