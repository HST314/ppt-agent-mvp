#!/usr/bin/env python3
"""Run the secret-backed P0-A/P0-B/P1 journey against the real provider."""

from __future__ import annotations

import argparse
import hashlib
import tempfile
import uuid
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))

from ppt_agent.config import ClarificationConfig, load_config  # noqa: E402
from ppt_agent.errors import ConflictError, RuntimeUnavailableError, ValidationError  # noqa: E402
from ppt_agent.gateways import agent_gateways_from_config  # noqa: E402
from ppt_agent.generation.bootstrap import build_generation_pipeline  # noqa: E402
from ppt_agent.generation.context import CONTEXT_SECTION_NAMES  # noqa: E402
from ppt_agent.generation.contracts import (  # noqa: E402
    HtmlDeckSpec,
    HtmlSampleSpec,
    canonical_json,
)
from ppt_agent.generation.pipeline import AGENT_HTML_RENDERER_VERSION  # noqa: E402
from ppt_agent.model_clients import ModelTurn  # noqa: E402
from ppt_agent.offline import verify_delivery  # noqa: E402
from ppt_agent.p2 import canonical  # noqa: E402
from ppt_agent.service import TaskService  # noqa: E402
from ppt_agent.store import WorkspaceStore  # noqa: E402


CONTEXT_SENTINEL = "REAL_PROVIDER_CONTEXT_SENTINEL_7F31E2"
ROUND_ANSWER_PREFIX = "REAL_PROVIDER_ROUND"
SHA256_LENGTH = 64


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


class ForbiddenDeterministicRenderer:
    """Fail immediately if the real Agent HTML journey enters the fallback renderer."""

    version = "forbidden-real-model-release-gate"

    def __init__(self):
        self.calls = 0

    def render(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("real-model agent_html journey invoked DeterministicRenderer.render")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConflictError(message)


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == SHA256_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _answers(questions: list[dict], round_number: int) -> dict:
    result = {}
    for index, question in enumerate(questions, 1):
        marker = f"{ROUND_ANSWER_PREFIX}_{round_number}_ANSWER_{index}"
        if question.get("allow_other"):
            result[question["question_id"]] = {
                "option": "Other",
                "other": f"{marker}：采用面向跨职能评审的专业、清晰、可执行表达。",
            }
            continue
        options = question.get("options") or []
        result[question["question_id"]] = {
            "option": options[0]["value"] if options else "稍后补充",
        }
    return result


def _provider_boundary_evidence(metadata: Mapping[str, Any], expected_context_hash: str, label: str) -> dict:
    _require(metadata.get("context_snapshot_hash") == expected_context_hash, f"{label} 未绑定统一上下文")
    _require(
        metadata.get("context_sections_read") == list(CONTEXT_SECTION_NAMES),
        f"{label} 未声明读取完整 GenerationContextV2",
    )
    payload_hash = metadata.get("stage_payload_hash")
    provider_input_hash = metadata.get("provider_input_sha256")
    _require(_is_sha256(payload_hash), f"{label} 缺少 stage payload hash")
    _require(provider_input_hash == payload_hash, f"{label} provider 输入与 stage payload 不一致")
    _require(int(metadata.get("provider_calls", 0)) > 0, f"{label} 没有真实 provider 调用")
    _require(metadata.get("skill_entry_read") is True, f"{label} 未读取 Skill 入口")
    _require("SKILL.md" in metadata.get("applied_skill_file_hashes", {}), f"{label} 缺少 Skill 文件证据")
    return {
        "label": label,
        "stage_payload_hash": payload_hash,
        "provider_input_sha256": provider_input_hash,
        "provider_calls": metadata["provider_calls"],
        "skill_entry_read": True,
    }


def _checkpoint_evidence(
    pipeline,
    checkpoint_id: str,
    expected_context_hash: str,
    expected_contract: str,
    label: str,
    *,
    direct_provider_boundary: bool,
) -> tuple[Any, dict]:
    checkpoint = pipeline.checkpoints.load(checkpoint_id)
    _require(checkpoint.contract_name == expected_contract, f"{label} 输出契约不正确")
    _require(checkpoint.metadata.get("context_snapshot_hash") == expected_context_hash, f"{label} checkpoint 上下文不一致")
    evidence = {
        "label": label,
        "checkpoint_id": checkpoint.checkpoint_id,
        "contract_name": checkpoint.contract_name,
        "output_sha256": checkpoint.output_sha256,
        "context_snapshot_hash": checkpoint.metadata["context_snapshot_hash"],
    }
    if direct_provider_boundary:
        evidence["provider_boundaries"] = [
            _provider_boundary_evidence(checkpoint.metadata, expected_context_hash, label)
        ]
    else:
        batches = checkpoint.metadata.get("batches") or []
        _require(bool(batches), f"{label} 缺少 provider batch 证据")
        evidence["provider_boundaries"] = [
            _provider_boundary_evidence(batch, expected_context_hash, f"{label}/batch-{index}")
            for index, batch in enumerate(batches)
        ]
    return checkpoint, evidence


def _html_authority_evidence(checkpoint: Any, label: str) -> dict:
    output = checkpoint.output
    shared_css = output.get("shared_css")
    slides = output.get("slides") or []
    rendered_html = output.get("rendered_html")
    _require(isinstance(shared_css, str) and bool(shared_css.strip()), f"{label} 未落盘 shared_css")
    _require(isinstance(rendered_html, str) and shared_css in rendered_html, f"{label} shared_css 未进入最终 DOM")
    _require(bool(slides), f"{label} 未落盘 HTML 页面")
    for index, slide in enumerate(slides):
        fragment = slide.get("html_fragment")
        _require(isinstance(fragment, str) and bool(fragment.strip()), f"{label} 第 {index + 1} 页缺少 html_fragment")
        _require(fragment in rendered_html, f"{label} 第 {index + 1} 页 fragment 未进入最终 DOM")
    _require("content_blocks" not in canonical_json(output), f"{label} 仍依赖 content_blocks")
    _require(output.get("renderer_version") == AGENT_HTML_RENDERER_VERSION, f"{label} 未走 Agent HTML renderer")
    return {
        "renderer_version": output["renderer_version"],
        "shared_css_sha256": hashlib.sha256(shared_css.encode()).hexdigest(),
        "slide_contract_hashes": {
            slide["slide_id"]: hashlib.sha256(canonical_json(slide).encode()).hexdigest()
            for slide in slides
        },
        "rendered_sha256": output["rendered_sha256"],
    }


def _modification_evidence(checkpoint: Any, before: Any, expected_requested: list[str], label: str) -> dict:
    metadata = checkpoint.metadata
    before_by_id = {slide["slide_id"]: canonical_json(slide) for slide in before.output["slides"]}
    after_by_id = {slide["slide_id"]: canonical_json(slide) for slide in checkpoint.output["slides"]}
    _require(metadata.get("operation") == "modify", f"{label} 未记录 modify operation")
    _require(metadata.get("requested_slide_ids") == expected_requested, f"{label} 请求页证据不正确")
    _require(set(metadata.get("modified_slide_ids") or []) == set(expected_requested), f"{label} 请求页未实际修改")
    preserved = [slide_id for slide_id in before_by_id if slide_id not in expected_requested]
    _require(metadata.get("preserved_slide_ids") == preserved, f"{label} 未记录完整保留页")
    _require(all(before_by_id[slide_id] == after_by_id[slide_id] for slide_id in preserved), f"{label} 改写了未请求页面")
    _require(all(before_by_id[slide_id] != after_by_id[slide_id] for slide_id in expected_requested), f"{label} 请求页内容未变化")
    return {
        "requested_slide_ids": expected_requested,
        "modified_slide_ids": metadata["modified_slide_ids"],
        "preserved_slide_ids": metadata["preserved_slide_ids"],
        "design_system_changed": metadata["design_system_changed"],
        "page_contract_hashes": metadata["page_contract_hashes"],
    }


def _long_release_input() -> dict:
    return {
        "goal": "向发布委员会说明演示文稿自动化链路并申请进入受控发布",
        "topic": "基于 Agent HTML 的演示文稿自动化发布链路",
        "页数": 6,
        "风格": "专业、清晰、信息层级明确",
        "data_source": (
            f"{CONTEXT_SENTINEL}。本材料要求从用户原始需求开始保留全链路语义，不得由阶段摘要替代。"
            "内容主线包括：输入材料进入不可变上下文；多轮澄清保留问题原文、选项、用户原始回答与采用值；"
            "叙事和逐页大纲引用同一上下文；样品阶段由真实模型直接生成逐页 HTML fragment 与共享 CSS；"
            "样品确认冻结页面和设计系统；全稿批次继续使用相同上下文和已确认设计语言；"
            "局部修改只重写被请求页面，其他页面逐字节保留；最终经过浏览器技术门禁、检查、定稿与离线交付。"
            "发布证据必须能回溯上下文 hash、每个模型输入 payload hash、HTML 契约、Skill 入口读取以及修改页集合。"
            "这是一条面向跨职能评审的发布链路，重点是可追溯、可回滚、无隐式降级，并且不能用模拟模型结果代替真实 provider 证据。"
        ),
    }


def _write_evidence(path: Path | None, result: dict) -> None:
    if path is None:
        return
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(canonical_json(result) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the P0-A/P0-B/P1 release against the configured real model")
    parser.add_argument("--config", type=Path, default=ROOT / "config/ppt-agent.yaml")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--evidence-file", type=Path)
    args = parser.parse_args()
    config = load_config(args.config, env_file=args.env_file)
    if config.mode != "agent":
        raise ValidationError("真实模型发布门禁要求 gateway.mode=agent")
    if config.generation_mode != "agent_html":
        raise ValidationError("真实模型发布门禁要求 gateway.generation_mode=agent_html")
    if not all(config.feature_flags.public().values()):
        raise RuntimeUnavailableError("真实模型发布门禁要求两个 v2 开关均已启用")

    with tempfile.TemporaryDirectory(prefix="ppt-real-release-") as data_root:
        ports = agent_gateways_from_config(config)
        fault = InjectOneInvalidJson(ports["generator"].client)
        ports["generator"].client = fault
        pipeline = build_generation_pipeline(
            config,
            data_root=data_root,
            generation_client=fault,
            repository_root=ROOT,
        )
        _require(pipeline is not None, "真实模型发布门禁未创建 GenerationPipeline")
        service = TaskService(
            WorkspaceStore(data_root),
            clarification_config=ClarificationConfig(
                max_questions_per_round=3,
                max_rounds=2,
                style="comprehensive",
            ),
            feature_flags=config.feature_flags,
            generation_pipeline=pipeline,
            **ports,
        )
        core_health = service.initialize_generation_core()
        if not core_health.get("ready"):
            raise RuntimeUnavailableError(
                "本地生成依赖未就绪",
                runtime_error_code="generation_core_unavailable",
                failed_check="generation_core",
            )
        _require(core_health.get("generation_mode") == "agent_html", "生成内核未启用 agent_html")
        _require(core_health.get("renderer_version") == AGENT_HTML_RENDERER_VERSION, "生成内核 renderer 版本不正确")
        renderer_guard = ForbiddenDeterministicRenderer()
        pipeline.renderer = renderer_guard

        task_id = f"release-{uuid.uuid4().hex[:12]}"
        release_input = _long_release_input()
        service.create(task_id, "manual")
        service.import_input(task_id, release_input)

        answered_rounds: dict[int, dict] = {}
        for _ in range(8):
            clarification = service.input_view(task_id)["clarification"]
            if clarification.get("status") == "generating":
                service.generate_clarification(task_id)
                continue
            questions = clarification.get("details") or []
            if questions:
                round_number = int(clarification.get("round", 1))
                _require(round_number in {1, 2}, "真实模型澄清超出两轮发布契约")
                answers = _answers(questions, round_number)
                answered_rounds[round_number] = answers
                answered = service.answer_clarifications(task_id, answers, require_complete=True)
                if answered.get("confirmed"):
                    break
                continue
            if clarification.get("confirmed"):
                break
            raise ConflictError("真实模型澄清链路未收敛")
        else:
            raise ConflictError("真实模型澄清轮次超出发布门禁上限")

        context_view = service.generation_context_view(task_id)
        context_hash = context_view["context_snapshot_hash"]
        transcript = context_view["clarification_transcript"]
        _require([item.get("round") for item in transcript] == [1, 2], "真实 provider 未完成两轮带回答的澄清")
        _require(set(answered_rounds) == {1, 2}, "两轮澄清回答证据不完整")
        _require(context_view["original_prompt"]["content"] == canonical(release_input).decode("utf-8"), "原始 Prompt 未逐字保留")
        _require(CONTEXT_SENTINEL in context_view["original_prompt"]["content"], "长 Prompt 哨兵未进入上下文")
        for round_number, item in enumerate(transcript, 1):
            exchanges = item.get("exchanges") or []
            _require(bool(exchanges), f"第 {round_number} 轮澄清没有问答")
            _require(all(exchange.get("prompt") for exchange in exchanges), f"第 {round_number} 轮问题原文丢失")
            _require(all("raw_answer" in exchange for exchange in exchanges), f"第 {round_number} 轮原始回答丢失")
            expected_answers = answered_rounds[round_number]
            _require(
                all(exchange["raw_answer"] == expected_answers[exchange["question_id"]] for exchange in exchanges),
                f"第 {round_number} 轮原始回答与提交值不一致",
            )

        fault.arm()
        narrative = service.generate_narrative(task_id)["narrative"]
        service.confirm_narrative(task_id)
        outline = service.generate_outline(task_id)["outline"]
        service.confirm_outline(task_id)
        service.select_samples(task_id)
        sample_before_view = service.generate_sample(task_id)["sample"]

        narrative_checkpoint, narrative_evidence = _checkpoint_evidence(
            pipeline,
            narrative["metadata"]["generation_core"]["checkpoint_id"],
            context_hash,
            "narrative_spec_v1",
            "narrative",
            direct_provider_boundary=True,
        )
        _require(fault.injected, "未向真实 provider 纠错链注入格式故障")
        _require(narrative_checkpoint.metadata.get("schema_correction_count", 0) > 0, "真实 provider 格式纠错证据未记录")
        _, outline_evidence = _checkpoint_evidence(
            pipeline,
            outline["metadata"]["generation_core"]["checkpoint_id"],
            context_hash,
            "outline_spec_v1",
            "outline",
            direct_provider_boundary=True,
        )
        sample_before_checkpoint, sample_before_evidence = _checkpoint_evidence(
            pipeline,
            sample_before_view["metadata"]["generation_core"]["checkpoint_id"],
            context_hash,
            HtmlSampleSpec.TITLE,
            "sample",
            direct_provider_boundary=True,
        )
        sample_before_authority = _html_authority_evidence(sample_before_checkpoint, "sample")

        sample_ids = [slide["slide_id"] for slide in sample_before_checkpoint.output["slides"]]
        sample_target = sample_ids[0]
        sample_after_view = service.modify_sample(
            task_id,
            "仅修改本页 HTML，在根 section 增加 data-real-provider-sample-modified 属性；共享设计系统与其余页面保持不变。",
            scope="page",
            slide_id=sample_target,
        )["sample"]
        sample_after_checkpoint, sample_modify_evidence = _checkpoint_evidence(
            pipeline,
            sample_after_view["metadata"]["generation_core"]["checkpoint_id"],
            context_hash,
            HtmlSampleSpec.TITLE,
            "sample-modify",
            direct_provider_boundary=False,
        )
        sample_modify_assertions = _modification_evidence(
            sample_after_checkpoint,
            sample_before_checkpoint,
            [sample_target],
            "sample-modify",
        )
        sample_after_authority = _html_authority_evidence(sample_after_checkpoint, "sample-modify")

        confirmation = service.confirm_sample(task_id)["confirmation"]
        confirmation_checkpoint = pipeline.checkpoints.load(
            confirmation["generation_core_confirmation"]["checkpoint_id"]
        )
        _require(confirmation_checkpoint.contract_name == "frozen_html_sample_v1", "样品确认未冻结 HTML 契约")
        _require(confirmation_checkpoint.output["slides"] == sample_after_checkpoint.output["slides"], "样品确认未逐字冻结修改后页面")
        _require(confirmation_checkpoint.output["shared_css"] == sample_after_checkpoint.output["shared_css"], "样品确认未冻结 shared_css")
        _require(confirmation_checkpoint.metadata.get("context_snapshot_hash") == context_hash, "样品确认上下文不一致")

        deck_before_view = service.generate_deck(task_id)["deck"]
        deck_before_checkpoint, deck_before_evidence = _checkpoint_evidence(
            pipeline,
            deck_before_view["metadata"]["generation_core"]["checkpoint_id"],
            context_hash,
            HtmlDeckSpec.TITLE,
            "deck",
            direct_provider_boundary=False,
        )
        deck_before_authority = _html_authority_evidence(deck_before_checkpoint, "deck")
        frozen_sample_by_id = {slide["slide_id"]: canonical_json(slide) for slide in confirmation_checkpoint.output["slides"]}
        deck_before_by_id = {slide["slide_id"]: canonical_json(slide) for slide in deck_before_checkpoint.output["slides"]}
        _require(
            all(deck_before_by_id[slide_id] == slide for slide_id, slide in frozen_sample_by_id.items()),
            "全稿未逐字复用已确认样品页",
        )

        deck_ids = [slide["slide_id"] for slide in deck_before_checkpoint.output["slides"]]
        deck_target = next((slide_id for slide_id in deck_ids if slide_id not in frozen_sample_by_id), deck_ids[-1])
        deck_after_view = service.modify_deck(
            task_id,
            "仅修改指定页 HTML，在根 section 增加 data-real-provider-deck-modified 属性；共享设计系统与未请求页面保持不变。",
            scope="page",
            slide_ids=[deck_target],
        )["deck"]
        deck_after_checkpoint, deck_modify_evidence = _checkpoint_evidence(
            pipeline,
            deck_after_view["metadata"]["generation_core"]["checkpoint_id"],
            context_hash,
            HtmlDeckSpec.TITLE,
            "deck-modify",
            direct_provider_boundary=False,
        )
        deck_modify_assertions = _modification_evidence(
            deck_after_checkpoint,
            deck_before_checkpoint,
            [deck_target],
            "deck-modify",
        )
        deck_after_authority = _html_authority_evidence(deck_after_checkpoint, "deck-modify")
        _require(renderer_guard.calls == 0, "真实 Agent HTML 链路调用了确定性 renderer")

        inspection = service.run_inspection(task_id, 0)
        if inspection["blocking_issues"]:
            raise ConflictError("真实模型全链路仍有 TechnicalGate 阻断")
        finalization = service.finalize_deck(task_id, deck_after_view["hash"], "review")["finalization"]
        delivery = service.publish_delivery(task_id)["delivery"]
        delivery_root = service.store.delivery_root(task_id, delivery["delivery_id"])
        verified_files = verify_delivery(delivery_root)

        result = {
            "status": "passed",
            "task_id": task_id,
            "feature_flags": config.feature_flags.public(),
            "generation_mode": config.generation_mode,
            "renderer_version": AGENT_HTML_RENDERER_VERSION,
            "deterministic_renderer_calls": renderer_guard.calls,
            "format_retry_injected": fault.injected,
            "format_retry_observed": narrative_checkpoint.metadata["schema_correction_count"] > 0,
            "context": {
                "context_snapshot_hash": context_hash,
                "original_prompt_sha256": context_view["original_prompt"]["content_hash"],
                "sentinel_present": True,
                "clarification_rounds": len(transcript),
                "round_exchange_counts": [len(item["exchanges"]) for item in transcript],
                "sections": list(CONTEXT_SECTION_NAMES),
            },
            "checkpoints": [
                narrative_evidence,
                outline_evidence,
                sample_before_evidence,
                sample_modify_evidence,
                deck_before_evidence,
                deck_modify_evidence,
            ],
            "html_authority": {
                "sample": sample_before_authority,
                "sample_modify": sample_after_authority,
                "deck": deck_before_authority,
                "deck_modify": deck_after_authority,
            },
            "modifications": {
                "sample": sample_modify_assertions,
                "deck": deck_modify_assertions,
            },
            "sample_confirmation_checkpoint_id": confirmation_checkpoint.checkpoint_id,
            "sample_pages_preserved_in_deck": sorted(frozen_sample_by_id),
            "sample_gate_passed": sample_after_view["metadata"]["post_render_gate"]["passed"],
            "deck_gate_passed": deck_after_view["metadata"]["post_render_gate"]["passed"],
            "finalization_hash": finalization["hash"],
            "delivery_hash": delivery["hash"],
            "offline_file_count": len(verified_files),
        }
        _write_evidence(args.evidence_file, result)
        print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
