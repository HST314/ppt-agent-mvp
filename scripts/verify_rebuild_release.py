#!/usr/bin/env python3
"""Run isolated contract-first golden paths and emit secret-free evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.env_loader import load_dotenv  # noqa: E402
from ppt_agent.config import load_config  # noqa: E402
from ppt_agent.gateways import agent_gateways_from_config  # noqa: E402
from ppt_agent.generation.bootstrap import build_generation_pipeline  # noqa: E402
from ppt_agent.generation.contracts import TaskBrief, canonical_json  # noqa: E402
from ppt_agent.generation.errors import GenerationCoreError  # noqa: E402


SENSITIVE_PATTERNS = ("authorization:", "bearer ", "api_key", "api-key", "provider_response_id")


def git_value(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def require_clean_checkout() -> None:
    if git_value("status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("release verification requires a clean tracked checkout")


def default_brief() -> TaskBrief:
    return TaskBrief.parse({
        "schema_version": "1.0",
        "goal": "向评审委员会说明自动化运营方案并申请进入发布阶段",
        "audience": "产品、工程与运维评审委员会",
        "topic": "演示文稿自动化发布链路",
        "slide_count": 6,
        "language": "zh-CN",
        "style_preferences": {"tone": "专业、清晰", "density": "concise"},
        "resource_manifest": [],
        "confirmed_facts": [],
    })


def scan_tree(root: Path) -> list[dict[str, str]]:
    findings = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        try:
            text = path.read_text(encoding="utf-8").lower()
        except (UnicodeDecodeError, OSError):
            continue
        for pattern in SENSITIVE_PATTERNS:
            if pattern in text:
                findings.append({"path": str(path.relative_to(root)), "pattern": pattern})
    return findings


def run_once(pipeline, brief: TaskBrief, artifact_root: Path, commit: str, tree: str, config_hash: str, index: int) -> dict:
    task_id = f"release-{index:02d}-{uuid.uuid4().hex[:12]}"
    run_id = f"run-{uuid.uuid4().hex}"
    run_root = artifact_root / run_id
    run_root.mkdir(parents=True)
    started = time.monotonic()
    stages = []

    def stage(name, function):
        stage_started = time.monotonic()
        result = function()
        stages.append({
            "stage": name,
            "checkpoint_id": result.checkpoint.checkpoint_id,
            "output_sha256": result.checkpoint.output_sha256,
            "duration_ms": round((time.monotonic() - stage_started) * 1000, 3),
            "provider_calls": result.checkpoint.metadata.get("provider_calls", 0),
            "recovery_count": result.checkpoint.metadata.get("recovery_count", 0),
        })
        return result

    narrative = stage("narrative", lambda: pipeline.generate_narrative(task_id, brief))
    outline = stage("outline", lambda: pipeline.generate_outline(task_id, brief, narrative.checkpoint.checkpoint_id))
    sample = stage("sample", lambda: pipeline.generate_sample(task_id, brief, outline.checkpoint.checkpoint_id))
    confirmation = stage("sample_confirmed", lambda: pipeline.confirm_sample(task_id, sample.checkpoint.checkpoint_id))
    deck = stage("deck", lambda: pipeline.generate_deck(task_id, brief, outline.checkpoint.checkpoint_id, confirmation.checkpoint.checkpoint_id))
    review = stage("review_input", lambda: pipeline.create_review_input(task_id, deck.checkpoint.checkpoint_id))
    delivery = stage("delivery", lambda: pipeline.publish_offline(task_id, deck.checkpoint.checkpoint_id, run_root / "delivery"))
    findings = scan_tree(run_root)
    if findings:
        raise RuntimeError(f"sensitive output scan failed: {findings}")
    evidence = {
        "schema_version": "1.0",
        "run_id": run_id,
        "task_id": task_id,
        "commit": commit,
        "tree": tree,
        "config_sha256": config_hash,
        "contract_version": "1.0",
        "renderer_version": deck.artifact.renderer_version,
        "brief_sha256": brief.sha256,
        "stages": stages,
        "checkpoint_chain": [item.checkpoint_id for item in pipeline.checkpoints.chain(delivery.checkpoint.checkpoint_id)],
        "deck": {
            "checkpoint_id": deck.checkpoint.checkpoint_id,
            "sha256": deck.artifact.sha256,
            "page_count": len(deck.value.slides),
            "page_order": [slide.slide_id for slide in deck.value.slides],
            "validation_hash": deck.validation.evidence_hash,
            "blocker_count": len(deck.validation.issues),
        },
        "review_checkpoint_id": review.checkpoint.checkpoint_id,
        "delivery": delivery.value,
        "sensitive_scan_findings": findings,
        "duration_ms": round((time.monotonic() - started) * 1000, 3),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    (run_root / "evidence.json").write_text(canonical_json(evidence) + "\n", encoding="utf-8")
    post_write_findings = scan_tree(run_root)
    if post_write_findings:
        raise RuntimeError(f"evidence scan failed: {post_write_findings}")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "ppt-agent.yaml")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--runs", type=int, choices=(1, 5, 20), default=1)
    parser.add_argument("--artifact-root", type=Path, default=ROOT / ".release-artifacts" / "rebuild")
    parser.add_argument("--brief", type=Path)
    args = parser.parse_args()
    load_dotenv(args.env_file)
    config = load_config(args.config, env_file=args.env_file)
    if config.mode != "agent":
        raise RuntimeError("release verification requires gateway.mode=agent")
    require_clean_checkout()
    brief = TaskBrief.parse(json.loads(args.brief.read_text(encoding="utf-8"))) if args.brief else default_brief()
    commit = git_value("rev-parse", "HEAD")
    tree = git_value("rev-parse", "HEAD^{tree}")
    config_hash = hashlib.sha256(canonical_json(config.public()).encode()).hexdigest()
    batch_root = args.artifact_root.resolve() / f"{commit[:12]}-{args.runs}x-{uuid.uuid4().hex[:8]}"
    batch_root.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="ppt-rebuild-release-") as data_root:
        ports = agent_gateways_from_config(config)
        pipeline = build_generation_pipeline(config, data_root=data_root, generation_client=ports["generator"].client, repository_root=ROOT)
        preflight = pipeline.preflight()
        if not preflight["ready"]:
            raise RuntimeError(f"generation preflight failed: {canonical_json(preflight)}")
        results = []
        for index in range(1, args.runs + 1):
            try:
                results.append(run_once(pipeline, brief, batch_root, commit, tree, config_hash, index))
            except Exception as exc:
                error = exc.public() if isinstance(exc, GenerationCoreError) else {"code": "release_run_failed", "details": {"error_type": type(exc).__name__}}
                report = {"schema_version": "1.0", "status": "failed", "commit": commit, "tree": tree, "requested_runs": args.runs, "completed_runs": len(results), "failed_run": index, "error": error}
                (batch_root / "report.json").write_text(canonical_json(report) + "\n", encoding="utf-8")
                raise
    report = {
        "schema_version": "1.0",
        "status": "passed",
        "commit": commit,
        "tree": tree,
        "runs": args.runs,
        "passed": len(results),
        "run_ids": [item["run_id"] for item in results],
        "task_ids": [item["task_id"] for item in results],
        "artifact_root": batch_root.name,
        "preflight": preflight,
    }
    (batch_root / "report.json").write_text(canonical_json(report) + "\n", encoding="utf-8")
    print(canonical_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
