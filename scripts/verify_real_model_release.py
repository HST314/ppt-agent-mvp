#!/usr/bin/env python3
"""Run a real Responses-model journey from imported material to delivery."""

from __future__ import annotations

import argparse
import json
import tempfile
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))

from ppt_agent.config import load_config  # noqa: E402
from ppt_agent.errors import ConflictError, RuntimeUnavailableError, ValidationError  # noqa: E402
from ppt_agent.gateways import agent_gateways_from_config  # noqa: E402
from ppt_agent.model_clients import ModelTurn  # noqa: E402
from ppt_agent.offline import verify_delivery  # noqa: E402
from ppt_agent.service import TaskService  # noqa: E402
from ppt_agent.store import WorkspaceStore  # noqa: E402


class InjectOneInvalidJson:
    """Corrupt one real final turn so the real model must answer correction."""

    def __init__(self, client):
        self.client = client
        self.armed = False
        self.injected = False

    def __getattr__(self, name):
        return getattr(self.client, name)

    def arm(self):
        self.armed = True

    def create(self, **kwargs):
        turn = self.client.create(**kwargs)
        if self.armed and not self.injected and turn.text and not turn.tool_calls:
            self.injected = True
            return ModelTurn("{", turn.response_id, ())
        return turn


def _answers(questions: list[dict]) -> dict:
    result = {}
    for question in questions:
        options = question.get("options") or []
        if options:
            result[question["question_id"]] = {"option": options[0]["value"]}
        elif question.get("allow_other"):
            result[question["question_id"]] = {"option": "Other", "other": "按任务卡既定约束执行"}
        else:
            result[question["question_id"]] = {"option": "稍后补充"}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the release against the configured real model")
    parser.add_argument("--config", type=Path, default=ROOT / "config/ppt-agent.yaml")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    config = load_config(args.config, env_file=args.env_file)
    if config.mode != "agent":
        raise ValidationError("真实模型发布门禁要求 gateway.mode=agent")
    if not all(config.feature_flags.public().values()):
        raise RuntimeUnavailableError("真实模型发布门禁要求两个 v2 开关均已启用")

    with tempfile.TemporaryDirectory(prefix="ppt-real-release-") as data_root:
        ports = agent_gateways_from_config(config)
        service = TaskService(
            WorkspaceStore(data_root),
            clarification_config=config.clarification,
            feature_flags=config.feature_flags,
            **ports,
        )
        health = service.initialize_runtime()
        if not health.get("ready"):
            raise RuntimeUnavailableError(
                "真实模型能力探测未通过",
                runtime_error_code=(health.get("error") or {}).get("code"),
                failed_check=health.get("failed_check"),
            )

        task_id = f"release-{uuid.uuid4().hex[:12]}"
        service.create(task_id, "manual")
        service.import_input(task_id, {
            "goal": "向评审委员会说明自动化运营方案并申请进入发布灰度",
            "audience": "产品、工程与运维评审委员会",
            "topic": "演示文稿自动化发布链路",
            "页数": 4,
            "风格": "专业、清晰、信息层级明确",
            "known_facts": {"rollout": "5%→25%→100%", "rollback": "切回上一个不可变版本"},
        })

        for _ in range(config.clarification.max_rounds + 2):
            clarification = service.input_view(task_id)["clarification"]
            if clarification.get("status") == "generating":
                service.generate_clarification(task_id)
                continue
            questions = clarification.get("details") or []
            if questions:
                service.answer_clarifications(task_id, _answers(questions), require_complete=True)
                continue
            if clarification.get("confirmed") or service.get(task_id).get("status") == "ready":
                break
            raise ConflictError("真实模型澄清链路未收敛")
        else:
            raise ConflictError("真实模型澄清轮次超出配置上限")

        generation = ports["generator"]
        fault = InjectOneInvalidJson(generation.client)
        generation.client = fault
        fault.arm()
        service.generate_narrative(task_id)
        service.confirm_narrative(task_id)
        service.generate_outline(task_id)
        service.confirm_outline(task_id)
        service.select_samples(task_id)
        sample = service.generate_sample(task_id)["sample"]
        service.confirm_sample(task_id)
        deck = service.generate_deck(task_id)["deck"]
        inspection = service.run_inspection(task_id, 0)
        if inspection["blocking_issues"]:
            raise ConflictError("真实模型全链路仍有 TechnicalGate 阻断")
        finalization = service.finalize_deck(task_id, deck["hash"], "review")["finalization"]
        delivery = service.publish_delivery(task_id)["delivery"]
        delivery_root = service.store.delivery_root(task_id, delivery["delivery_id"])
        verified_files = verify_delivery(delivery_root)
        audits = service.agent_audits(task_id)
        events = [event for audit in audits for event in audit.get("events", [])]
        skill_entry_read = any(
            event.get("event") == "tool"
            and event.get("tool") == "read_skill_file"
            and event.get("path") == "SKILL.md"
            for event in events
        )
        schema_retry = any(event.get("event") == "schema_correction" for event in events)
        result = {
            "status": "passed",
            "task_id": task_id,
            "feature_flags": config.feature_flags.public(),
            "format_retry_injected": fault.injected,
            "format_retry_observed": schema_retry,
            "skill_entry_read": skill_entry_read,
            "sample_gate_passed": sample["metadata"]["post_render_gate"]["passed"],
            "deck_gate_passed": deck["metadata"]["post_render_gate"]["passed"],
            "finalization_hash": finalization["hash"],
            "delivery_hash": delivery["hash"],
            "offline_file_count": len(verified_files),
        }
        if not all((fault.injected, schema_retry, skill_entry_read, result["sample_gate_passed"], result["deck_gate_passed"])):
            raise ConflictError("真实模型发布证据不完整")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
