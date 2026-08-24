from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import portalocker

from .contracts import (
    CONTRACT_VERSION,
    DeckSpec,
    NarrativeSpec,
    OutlineDraft,
    OutlineSpec,
    SampleSpec,
    SlideBatchSpec,
    SlideSpec,
    TaskBrief,
    ThemeTokens,
    canonical_json,
    content_sha256,
    narrative_contract_for_evidence,
    outline_contract_for_evidence,
    sample_contract_for_assets,
    slide_batch_contract_for_assets,
    validate_slide_outline_alignment,
    verify_evidence_refs,
)
from .errors import CheckpointConflict, ContractValidationError, ErrorContext
from .model_gateway import GatewayResult, ModelGateway
from .prompts import PROMPT_VERSION, narrative_prompt, outline_prompt, sample_prompt, slide_batch_prompt
from ..rendering.assets import AssetResolver, ResolvedAsset
from ..rendering.renderer import DeterministicRenderer, RenderedDeck
from ..rendering.validator import TechnicalValidator, ValidationReport


CHECKPOINT_VERSION = "1.0"
PIPELINE_VERSION = "1.0.0"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_task_id(task_id: str) -> str:
    if not task_id or len(task_id) > 128 or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in task_id):
        raise ValueError("task_id format invalid")
    return task_id


@dataclass(frozen=True)
class Checkpoint:
    checkpoint_id: str
    task_id: str
    stage: str
    input_version: str
    contract_name: str
    contract_version: str
    model: str
    output_sha256: str
    parent_checkpoint_id: str | None
    idempotency_key_sha256: str
    created_at: str
    output: dict[str, Any]
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CHECKPOINT_VERSION,
            "checkpoint_id": self.checkpoint_id,
            "task_id": self.task_id,
            "stage": self.stage,
            "input_version": self.input_version,
            "contract_name": self.contract_name,
            "contract_version": self.contract_version,
            "model": self.model,
            "output_sha256": self.output_sha256,
            "parent_checkpoint_id": self.parent_checkpoint_id,
            "idempotency_key_sha256": self.idempotency_key_sha256,
            "created_at": self.created_at,
            "output": self.output,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class StageResult:
    checkpoint: Checkpoint
    value: Any
    artifact: RenderedDeck | None = None
    validation: ValidationReport | None = None
    reused: bool = False


class FileCheckpointStore:
    """Content-addressed immutable checkpoints with task-local authority keys."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._guard = threading.RLock()

    def commit(
        self,
        *,
        task_id: str,
        stage: str,
        input_version: str,
        contract_name: str,
        output: dict[str, Any],
        model: str,
        parent_checkpoint_id: str | None,
        idempotency_key: str,
        metadata: dict[str, Any] | None = None,
    ) -> Checkpoint:
        task_id = _safe_task_id(task_id)
        if not stage or not input_version or not contract_name or not idempotency_key:
            raise ValueError("checkpoint identity fields must be non-empty")
        canonical_output = json.loads(canonical_json(output))
        canonical_metadata = json.loads(canonical_json(metadata or {}))
        output_hash = content_sha256(canonical_output)
        key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
        identity = {
            "task_id": task_id,
            "stage": stage,
            "input_version": input_version,
            "contract_name": contract_name,
            "contract_version": CONTRACT_VERSION,
            "model": model,
            "output_sha256": output_hash,
            "parent_checkpoint_id": parent_checkpoint_id,
            "idempotency_key_sha256": key_hash,
            "metadata": canonical_metadata,
        }
        checkpoint_id = f"cp-{content_sha256(identity)}"
        task_root = self._task_root(task_id)
        task_root.mkdir(parents=True, exist_ok=True)
        lock_path = task_root / ".checkpoint.lock"
        with self._guard, portalocker.Lock(lock_path, mode="a", timeout=10):
            authority = task_root / "authority" / f"{key_hash}.json"
            if authority.exists():
                existing_id = json.loads(authority.read_text(encoding="utf-8"))["checkpoint_id"]
                existing = self.load(existing_id)
                if existing.checkpoint_id != checkpoint_id:
                    raise CheckpointConflict("同一幂等键已提交不同权威结果", context=ErrorContext(stage=stage))
                return existing
            if parent_checkpoint_id is not None:
                parent = self.load(parent_checkpoint_id)
                if parent.task_id != task_id:
                    raise CheckpointConflict("父 checkpoint 不属于当前任务", context=ErrorContext(stage=stage))
            checkpoint = Checkpoint(
                checkpoint_id=checkpoint_id,
                task_id=task_id,
                stage=stage,
                input_version=input_version,
                contract_name=contract_name,
                contract_version=CONTRACT_VERSION,
                model=model,
                output_sha256=output_hash,
                parent_checkpoint_id=parent_checkpoint_id,
                idempotency_key_sha256=key_hash,
                created_at=_now(),
                output=canonical_output,
                metadata=canonical_metadata,
            )
            checkpoint_path = task_root / "checkpoints" / f"{checkpoint_id}.json"
            self._write_once(checkpoint_path, checkpoint.to_dict())
            self._write_once(authority, {"checkpoint_id": checkpoint_id})
            return checkpoint

    def find(self, task_id: str, idempotency_key: str) -> Checkpoint | None:
        task_root = self._task_root(_safe_task_id(task_id))
        key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
        authority = task_root / "authority" / f"{key_hash}.json"
        if not authority.is_file():
            return None
        return self.load(json.loads(authority.read_text(encoding="utf-8"))["checkpoint_id"])

    def load(self, checkpoint_id: str) -> Checkpoint:
        if not isinstance(checkpoint_id, str) or not checkpoint_id.startswith("cp-") or len(checkpoint_id) != 67:
            raise CheckpointConflict("checkpoint ID 格式无效")
        matches = list(self.root.glob(f"*/checkpoints/{checkpoint_id}.json"))
        if len(matches) != 1:
            raise CheckpointConflict("checkpoint 不存在或不唯一")
        value = json.loads(matches[0].read_text(encoding="utf-8"))
        if value.get("schema_version") != CHECKPOINT_VERSION:
            raise CheckpointConflict("checkpoint 版本不受支持")
        if content_sha256(value.get("output")) != value.get("output_sha256"):
            raise CheckpointConflict("checkpoint 输出哈希无效")
        identity = {key: value.get(key) for key in ("task_id", "stage", "input_version", "contract_name", "contract_version", "model", "output_sha256", "parent_checkpoint_id", "idempotency_key_sha256", "metadata")}
        if f"cp-{content_sha256(identity)}" != checkpoint_id:
            raise CheckpointConflict("checkpoint 身份哈希无效")
        return Checkpoint(
            checkpoint_id=value["checkpoint_id"],
            task_id=value["task_id"],
            stage=value["stage"],
            input_version=value["input_version"],
            contract_name=value["contract_name"],
            contract_version=value["contract_version"],
            model=value["model"],
            output_sha256=value["output_sha256"],
            parent_checkpoint_id=value.get("parent_checkpoint_id"),
            idempotency_key_sha256=value["idempotency_key_sha256"],
            created_at=value["created_at"],
            output=value["output"],
            metadata=value["metadata"],
        )

    def chain(self, checkpoint_id: str) -> tuple[Checkpoint, ...]:
        chain: list[Checkpoint] = []
        seen: set[str] = set()
        current: str | None = checkpoint_id
        while current is not None:
            if current in seen:
                raise CheckpointConflict("checkpoint 链包含循环")
            seen.add(current)
            item = self.load(current)
            chain.append(item)
            current = item.parent_checkpoint_id
        return tuple(chain)

    def _task_root(self, task_id: str) -> Path:
        return self.root / task_id

    @staticmethod
    def _write_once(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (canonical_json(value) + "\n").encode()
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            existing = path.read_bytes()
            if existing != payload:
                raise CheckpointConflict("不可变 checkpoint 文件发生冲突")
            return
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise


class GenerationPipeline:
    """Stage orchestration without workflow-state or user-gate authority."""

    def __init__(
        self,
        gateway: ModelGateway,
        checkpoints: FileCheckpointStore,
        renderer: DeterministicRenderer,
        validator: TechnicalValidator,
        *,
        asset_root: str | Path,
        batch_size: int = 3,
        max_batch_workers: int = 1,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ):
        if not 1 <= batch_size <= 8:
            raise ValueError("batch_size must be between 1 and 8")
        if not 1 <= max_batch_workers <= 8:
            raise ValueError("max_batch_workers must be between 1 and 8")
        self.gateway = gateway
        self.checkpoints = checkpoints
        self.renderer = renderer
        self.validator = validator
        self.asset_root = Path(asset_root).resolve()
        self.batch_size = batch_size
        self.max_batch_workers = max_batch_workers
        self.event_sink = event_sink

    def generate_narrative(self, task_id: str, brief: TaskBrief, *, input_version: str | None = None) -> StageResult:
        input_version = input_version or brief.sha256
        brief_checkpoint = self._brief_checkpoint(task_id, brief, input_version)
        key = self._key(task_id, "narrative", brief_checkpoint.checkpoint_id)
        existing = self.checkpoints.find(task_id, key)
        if existing:
            return StageResult(existing, NarrativeSpec.parse(existing.output), reused=True)
        self._event(task_id, "narrative", "started")
        evidence_ids = tuple(item.resource_id for item in brief.resource_manifest) + tuple(item.fact_id for item in brief.confirmed_facts)
        gateway_result = self.gateway.generate(narrative_contract_for_evidence(evidence_ids), input=narrative_prompt({"task_brief": brief.to_dict()}), idempotency_key=key, stage="narrative")
        narrative = gateway_result.contract
        verify_evidence_refs(narrative, brief)
        checkpoint = self._commit_contract(task_id, "narrative", brief_checkpoint.checkpoint_id, narrative, gateway_result, key)
        self._event(task_id, "narrative", "succeeded", checkpoint=checkpoint)
        return StageResult(checkpoint, narrative)

    def generate_outline(self, task_id: str, brief: TaskBrief, narrative_checkpoint_id: str) -> StageResult:
        narrative_checkpoint = self._require_checkpoint(task_id, narrative_checkpoint_id, "narrative")
        narrative = NarrativeSpec.parse(narrative_checkpoint.output)
        key = self._key(task_id, "outline", narrative_checkpoint_id, brief.sha256)
        existing = self.checkpoints.find(task_id, key)
        if existing:
            return StageResult(existing, OutlineSpec.parse(existing.output, expected_slide_count=brief.slide_count), reused=True)
        self._event(task_id, "outline", "started")
        evidence_ids = tuple(item.resource_id for item in brief.resource_manifest) + tuple(item.fact_id for item in brief.confirmed_facts)
        gateway_result = self.gateway.generate(outline_contract_for_evidence(evidence_ids, brief.slide_count), input=outline_prompt({"task_brief": brief.to_dict(), "narrative": narrative.to_dict(), "slide_count": brief.slide_count}), idempotency_key=key, stage="outline")
        outline = OutlineSpec.from_draft(gateway_result.contract, expected_slide_count=brief.slide_count)
        verify_evidence_refs(outline, brief)
        checkpoint = self._commit_contract(task_id, "outline", narrative_checkpoint_id, outline, gateway_result, key)
        self._event(task_id, "outline", "succeeded", checkpoint=checkpoint)
        return StageResult(checkpoint, outline)

    def generate_sample(self, task_id: str, brief: TaskBrief, outline_checkpoint_id: str, *, selected_slide_ids: Sequence[str] | None = None) -> StageResult:
        outline_checkpoint = self._require_checkpoint(task_id, outline_checkpoint_id, "outline")
        outline = OutlineSpec.parse(outline_checkpoint.output, expected_slide_count=brief.slide_count)
        selected = tuple(selected_slide_ids or self.select_representative_slides(outline))
        if not 2 <= len(selected) <= 3 or len(set(selected)) != len(selected):
            raise ContractValidationError("样品页必须是 2 到 3 个唯一页面", context=ErrorContext(stage="sample", field_path="selected_slide_ids"))
        by_id = {slide.slide_id: slide for slide in outline.slides}
        if any(slide_id not in by_id for slide_id in selected):
            raise ContractValidationError("样品页不在大纲中", context=ErrorContext(stage="sample", field_path="selected_slide_ids"))
        key = self._key(task_id, "sample", outline_checkpoint_id, content_sha256(selected))
        existing = self.checkpoints.find(task_id, key)
        if existing:
            sample, artifact, validation = self._read_rendered_sample(existing)
            return StageResult(existing, sample, artifact, validation, reused=True)
        self._event(task_id, "sample", "started")
        allowed_assets = tuple(item.resource_id for item in brief.resource_manifest)
        gateway_result = self.gateway.generate(
            sample_contract_for_assets(allowed_assets, len(selected)),
            input=sample_prompt({
                "task_brief": brief.to_dict(),
                "outline_checkpoint_id": outline_checkpoint_id,
                "selected_slides": [by_id[slide_id].to_dict() for slide_id in selected],
            }),
            idempotency_key=key,
            stage="sample",
        )
        sample = gateway_result.contract
        sample = SampleSpec(tuple(replace(slide, slide_id=selected[index], role=by_id[selected[index]].role) for index, slide in enumerate(sample.slides)), sample.theme_tokens, sample.shared_assets, outline_checkpoint_id)
        validate_slide_outline_alignment(sample.slides, outline)
        if not set(sample.shared_assets).issubset(set(allowed_assets)):
            raise ContractValidationError("样品引用了任务清单之外的资源", context=ErrorContext(stage="sample", field_path="shared_assets"))
        assets = self._assets(brief, sample.shared_assets)
        sample_deck = DeckSpec(sample.slides, sample.theme_tokens, sample.shared_assets, outline_checkpoint_id, "sample-pending")
        artifact = self.renderer.render(sample_deck, assets, language=brief.language)
        validation = self.validator.validate(artifact.html, selected, assets)
        output = {**sample.to_dict(), "rendered_html": artifact.html, "rendered_sha256": artifact.sha256, "renderer_version": artifact.renderer_version, "validation": validation.to_dict()}
        checkpoint = self.checkpoints.commit(
            task_id=task_id,
            stage="sample",
            input_version=outline_checkpoint_id,
            contract_name=SampleSpec.TITLE,
            output=output,
            model=gateway_result.model,
            parent_checkpoint_id=outline_checkpoint_id,
            idempotency_key=key,
            metadata=self._gateway_metadata(gateway_result) | {"pipeline_version": PIPELINE_VERSION, "selected_slide_ids": list(selected)},
        )
        self._event(task_id, "sample", "succeeded", checkpoint=checkpoint)
        return StageResult(checkpoint, sample, artifact, validation)

    def confirm_sample(self, task_id: str, sample_checkpoint_id: str) -> StageResult:
        sample_checkpoint = self._require_checkpoint(task_id, sample_checkpoint_id, "sample")
        sample = SampleSpec.parse({key: value for key, value in sample_checkpoint.output.items() if key in {"schema_version", "slides", "theme_tokens", "shared_assets", "outline_checkpoint_id"}})
        key = self._key(task_id, "sample_confirmed", sample_checkpoint_id)
        existing = self.checkpoints.find(task_id, key)
        if existing:
            return StageResult(existing, existing.output, reused=True)
        frozen = {
            "schema_version": CONTRACT_VERSION,
            "sample_checkpoint_id": sample_checkpoint_id,
            "outline_checkpoint_id": sample.outline_checkpoint_id,
            "theme_tokens": sample.theme_tokens.to_dict(),
            "layout_families": sorted({slide.layout_family for slide in sample.slides}),
            "shared_assets": list(sample.shared_assets),
            "slides": [slide.to_dict() for slide in sample.slides],
            "rendered_sha256": sample_checkpoint.output["rendered_sha256"],
        }
        checkpoint = self.checkpoints.commit(
            task_id=task_id,
            stage="sample_confirmed",
            input_version=sample_checkpoint_id,
            contract_name="frozen_sample_v1",
            output=frozen,
            model="service",
            parent_checkpoint_id=sample_checkpoint_id,
            idempotency_key=key,
            metadata={"pipeline_version": PIPELINE_VERSION, "confirmed_by": "user"},
        )
        self._event(task_id, "sample_confirmed", "succeeded", checkpoint=checkpoint)
        return StageResult(checkpoint, frozen)

    def generate_deck(self, task_id: str, brief: TaskBrief, outline_checkpoint_id: str, sample_confirmation_checkpoint_id: str) -> StageResult:
        outline_checkpoint = self._require_checkpoint(task_id, outline_checkpoint_id, "outline")
        confirmation = self._require_checkpoint(task_id, sample_confirmation_checkpoint_id, "sample_confirmed")
        if confirmation.output.get("outline_checkpoint_id") != outline_checkpoint_id:
            raise CheckpointConflict("样品确认与当前大纲 checkpoint 不一致", context=ErrorContext(stage="deck"))
        outline = OutlineSpec.parse(outline_checkpoint.output, expected_slide_count=brief.slide_count)
        frozen_theme = ThemeTokens.parse(confirmation.output["theme_tokens"])
        frozen_layouts = set(confirmation.output["layout_families"])
        frozen_assets = tuple(confirmation.output["shared_assets"])
        sample_slides = tuple(SlideSpec.parse(value, f"sample_confirmation.slides[{index}]") for index, value in enumerate(confirmation.output["slides"]))
        sample_by_id = {slide.slide_id: slide for slide in sample_slides}
        remaining = [slide.slide_id for slide in outline.slides if slide.slide_id not in sample_by_id]
        key = self._key(task_id, "deck", outline_checkpoint_id, sample_confirmation_checkpoint_id)
        existing = self.checkpoints.find(task_id, key)
        if existing:
            deck, artifact, validation = self._read_rendered_deck(existing, outline)
            return StageResult(existing, deck, artifact, validation, reused=True)
        self._event(task_id, "deck", "started")
        batches = [remaining[index:index + self.batch_size] for index in range(0, len(remaining), self.batch_size)]
        generated: dict[str, SlideSpec] = {}
        batch_evidence: list[dict[str, Any]] = []

        def produce(item: tuple[int, list[str]]) -> tuple[int, list[str], GatewayResult]:
            index, ids = item
            batch_key = self._key(task_id, "deck_batch", outline_checkpoint_id, sample_confirmation_checkpoint_id, str(index), CONTRACT_VERSION)
            slides_by_id = {slide.slide_id: slide for slide in outline.slides}
            result = self.gateway.generate(
                slide_batch_contract_for_assets(frozen_assets, len(ids), tuple(frozen_layouts)),
                input=slide_batch_prompt({
                    "task_brief": brief.to_dict(),
                    "outline_checkpoint_id": outline_checkpoint_id,
                    "sample_checkpoint_id": sample_confirmation_checkpoint_id,
                    "batch_index": index,
                    "requested_slides": [slides_by_id[slide_id].to_dict() for slide_id in ids],
                    "frozen_theme_tokens": frozen_theme.to_dict(),
                    "allowed_layout_families": sorted(frozen_layouts),
                    "allowed_asset_refs": list(frozen_assets),
                }),
                idempotency_key=batch_key,
                stage="deck_batch",
            )
            return index, ids, result

        with ThreadPoolExecutor(max_workers=self.max_batch_workers, thread_name_prefix="deck-batch") as executor:
            results = list(executor.map(produce, enumerate(batches)))
        for batch_index, requested_ids, gateway_result in sorted(results):
            batch = gateway_result.contract
            outline_by_id = {slide.slide_id: slide for slide in outline.slides}
            batch = SlideBatchSpec(tuple(replace(slide, slide_id=requested_ids[index], role=outline_by_id[requested_ids[index]].role) for index, slide in enumerate(batch.slides)))
            validate_slide_outline_alignment(batch.slides, outline)
            if any(slide.layout_family not in frozen_layouts for slide in batch.slides):
                raise ContractValidationError("模型批次使用了未冻结版式族", context=ErrorContext(stage="deck", field_path=f"batches[{batch_index}].layout_family"))
            if any(not set(slide.asset_refs).issubset(set(frozen_assets)) for slide in batch.slides):
                raise ContractValidationError("模型批次使用了未冻结资源", context=ErrorContext(stage="deck", field_path=f"batches[{batch_index}].asset_refs"))
            generated.update({slide.slide_id: slide for slide in batch.slides})
            batch_evidence.append({"batch_index": batch_index, "slide_ids": requested_ids, **self._gateway_metadata(gateway_result)})
        ordered = tuple(sample_by_id.get(slide.slide_id) or generated[slide.slide_id] for slide in outline.slides)
        shared_assets = tuple(sorted({resource_id for slide in ordered for resource_id in slide.asset_refs}))
        # Frozen shared assets are a permission ceiling. DeckSpec carries only
        # the resources actually referenced by the final ordered page set.
        deck = DeckSpec(ordered, frozen_theme, shared_assets, outline_checkpoint_id, sample_confirmation_checkpoint_id)
        deck = DeckSpec.parse(deck.to_dict(), expected_slide_ids=[slide.slide_id for slide in outline.slides], frozen_theme=frozen_theme, allowed_layouts=frozen_layouts)
        assets = self._assets(brief, shared_assets)
        artifact = self.renderer.render(deck, assets, language=brief.language)
        validation = self.validator.validate(artifact.html, [slide.slide_id for slide in outline.slides], assets)
        output = {**deck.to_dict(), "rendered_html": artifact.html, "rendered_sha256": artifact.sha256, "renderer_version": artifact.renderer_version, "validation": validation.to_dict()}
        checkpoint = self.checkpoints.commit(
            task_id=task_id,
            stage="deck",
            input_version=f"{outline_checkpoint_id}:{sample_confirmation_checkpoint_id}",
            contract_name=DeckSpec.TITLE,
            output=output,
            model=self.gateway.model,
            parent_checkpoint_id=sample_confirmation_checkpoint_id,
            idempotency_key=key,
            metadata={"pipeline_version": PIPELINE_VERSION, "batches": batch_evidence, "sample_slide_hashes": {slide.slide_id: slide.sha256 for slide in sample_slides}},
        )
        self._event(task_id, "deck", "succeeded", checkpoint=checkpoint)
        return StageResult(checkpoint, deck, artifact, validation)

    def create_review_input(self, task_id: str, deck_checkpoint_id: str) -> StageResult:
        deck_checkpoint = self._require_checkpoint(task_id, deck_checkpoint_id, "deck")
        key = self._key(task_id, "review_input", deck_checkpoint_id)
        existing = self.checkpoints.find(task_id, key)
        if existing:
            return StageResult(existing, existing.output, reused=True)
        deck = DeckSpec.parse({key: value for key, value in deck_checkpoint.output.items() if key in {"schema_version", "slides", "theme_tokens", "shared_assets", "outline_checkpoint_id", "sample_checkpoint_id"}})
        output = {
            "schema_version": CONTRACT_VERSION,
            "deck_checkpoint_id": deck_checkpoint_id,
            "deck_sha256": deck_checkpoint.output["rendered_sha256"],
            "slides": [{"slide_id": slide.slide_id, "role": slide.role, "title": slide.title, "speaker_notes": slide.speaker_notes, "content_sha256": slide.sha256} for slide in deck.slides],
            "technical_validation_hash": deck_checkpoint.output["validation"]["evidence_hash"],
        }
        checkpoint = self.checkpoints.commit(task_id=task_id, stage="review_input", input_version=deck_checkpoint_id, contract_name="review_input_v1", output=output, model="service", parent_checkpoint_id=deck_checkpoint_id, idempotency_key=key, metadata={"pipeline_version": PIPELINE_VERSION})
        return StageResult(checkpoint, output)

    def publish_offline(self, task_id: str, deck_checkpoint_id: str, target_root: str | Path) -> StageResult:
        deck_checkpoint = self._require_checkpoint(task_id, deck_checkpoint_id, "deck")
        deck = DeckSpec.parse({key: value for key, value in deck_checkpoint.output.items() if key in {"schema_version", "slides", "theme_tokens", "shared_assets", "outline_checkpoint_id", "sample_checkpoint_id"}})
        brief_checkpoint = next((item for item in reversed(self.checkpoints.chain(deck_checkpoint_id)) if item.stage == "brief"), None)
        if brief_checkpoint is None:
            raise CheckpointConflict("deck checkpoint 链缺少任务简报")
        brief = TaskBrief.parse(brief_checkpoint.output)
        assets = self._assets(brief, deck.shared_assets)
        target = Path(target_root).resolve()
        key = self._key(task_id, "delivery", deck_checkpoint_id, str(target))
        existing = self.checkpoints.find(task_id, key)
        if existing:
            return StageResult(existing, existing.output, reused=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
        try:
            (staging / "index.html").write_text(deck_checkpoint.output["rendered_html"], encoding="utf-8")
            for asset in assets.values():
                path = staging / asset.offline_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(asset.content)
            files = {str(path.relative_to(staging)): hashlib.sha256(path.read_bytes()).hexdigest() for path in staging.rglob("*") if path.is_file()}
            manifest = {
                "schema_version": CONTRACT_VERSION,
                "task_id": task_id,
                "deck_checkpoint_id": deck_checkpoint_id,
                "deck_sha256": deck_checkpoint.output["rendered_sha256"],
                "contract_version": CONTRACT_VERSION,
                "renderer_version": deck_checkpoint.output["renderer_version"],
                "files": files,
            }
            (staging / "manifest.json").write_text(canonical_json(manifest) + "\n", encoding="utf-8")
            manifest_hash = hashlib.sha256((staging / "manifest.json").read_bytes()).hexdigest()
            if target.exists():
                current = {str(path.relative_to(target)): hashlib.sha256(path.read_bytes()).hexdigest() for path in target.rglob("*") if path.is_file()}
                proposed = {str(path.relative_to(staging)): hashlib.sha256(path.read_bytes()).hexdigest() for path in staging.rglob("*") if path.is_file()}
                if current != proposed:
                    raise CheckpointConflict("离线交付目录不可覆盖")
                shutil.rmtree(staging)
            else:
                os.replace(staging, target)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        output = {"schema_version": CONTRACT_VERSION, "deck_checkpoint_id": deck_checkpoint_id, "manifest_sha256": manifest_hash, "files": sorted([*files, "manifest.json"]), "target_name": target.name}
        checkpoint = self.checkpoints.commit(task_id=task_id, stage="delivery", input_version=deck_checkpoint_id, contract_name="delivery_manifest_v1", output=output, model="service", parent_checkpoint_id=deck_checkpoint_id, idempotency_key=key, metadata={"pipeline_version": PIPELINE_VERSION})
        self._event(task_id, "delivery", "succeeded", checkpoint=checkpoint)
        return StageResult(checkpoint, output)

    def preflight(self) -> dict[str, Any]:
        checks: dict[str, Any] = {}
        self.checkpoints.root.mkdir(parents=True, exist_ok=True)
        probe = Path(tempfile.mkdtemp(prefix="ppt-preflight-", dir=self.checkpoints.root))
        try:
            (probe / "write-check").write_text("ready", encoding="utf-8")
            checks["data_directory"] = True
            checks["temporary_directory"] = True
        finally:
            shutil.rmtree(probe, ignore_errors=True)
        checks["asset_root"] = self.asset_root.is_dir() and os.access(self.asset_root, os.R_OK)
        checks["chromium"] = self.validator.readiness()
        capability = getattr(self.gateway.provider, "probe_capabilities", None)
        if callable(capability):
            try:
                values = capability()
                checks["provider"] = {"ready": bool(values and all(values.values())), "checks": values}
            except Exception as exc:
                checks["provider"] = {"ready": False, "error_type": type(exc).__name__}
        else:
            checks["provider"] = {"ready": True, "checks": "deferred"}
        ready = bool(checks["data_directory"] and checks["temporary_directory"] and checks["asset_root"] and checks["chromium"].get("ready") and checks["provider"].get("ready"))
        return {"ready": ready, "pipeline_version": PIPELINE_VERSION, "contract_version": CONTRACT_VERSION, "renderer_version": self.renderer.version, "checks": checks}

    @staticmethod
    def select_representative_slides(outline: OutlineSpec) -> tuple[str, ...]:
        if len(outline.slides) < 2:
            raise ContractValidationError("样品链路至少需要两页大纲", context=ErrorContext(stage="sample", field_path="outline.slides"))
        selected = [outline.slides[0].slide_id]
        priority_roles = ("data", "metric", "analysis", "image", "evidence")
        for role in priority_roles:
            candidate = next((slide.slide_id for slide in outline.slides[1:] if role in slide.role.lower() and slide.slide_id not in selected), None)
            if candidate:
                selected.append(candidate)
                break
        if len(selected) < 2:
            selected.append(outline.slides[min(1, len(outline.slides) - 1)].slide_id)
        if len(outline.slides) >= 6:
            candidate = outline.slides[-1].slide_id
            if candidate not in selected:
                selected.append(candidate)
        return tuple(selected[:3])

    def _brief_checkpoint(self, task_id: str, brief: TaskBrief, input_version: str) -> Checkpoint:
        key = self._key(task_id, "brief", input_version, brief.sha256)
        existing = self.checkpoints.find(task_id, key)
        if existing:
            if TaskBrief.parse(existing.output) != brief:
                raise CheckpointConflict("任务简报幂等输入冲突")
            return existing
        return self.checkpoints.commit(task_id=task_id, stage="brief", input_version=input_version, contract_name=TaskBrief.TITLE, output=brief.to_dict(), model="service", parent_checkpoint_id=None, idempotency_key=key, metadata={"pipeline_version": PIPELINE_VERSION})

    def _commit_contract(self, task_id: str, stage: str, parent_checkpoint_id: str, contract, result: GatewayResult, key: str) -> Checkpoint:
        return self.checkpoints.commit(task_id=task_id, stage=stage, input_version=parent_checkpoint_id, contract_name=contract.TITLE, output=contract.to_dict(), model=result.model, parent_checkpoint_id=parent_checkpoint_id, idempotency_key=key, metadata=self._gateway_metadata(result) | {"pipeline_version": PIPELINE_VERSION})

    @staticmethod
    def _gateway_metadata(result: GatewayResult) -> dict[str, Any]:
        return {"provider_calls": result.provider_calls, "recovery_count": result.recovery_count, "response_id_sha256": result.response_id_sha256, "model": result.model, "elapsed_ms": result.elapsed_ms, "prompt_version": PROMPT_VERSION}

    def _assets(self, brief: TaskBrief, resource_ids: Iterable[str]) -> dict[str, ResolvedAsset]:
        return AssetResolver(brief.resource_manifest, self.asset_root).resolve(resource_ids)

    def _require_checkpoint(self, task_id: str, checkpoint_id: str, stage: str) -> Checkpoint:
        value = self.checkpoints.load(checkpoint_id)
        if value.task_id != task_id or value.stage != stage:
            raise CheckpointConflict("checkpoint 与任务阶段不匹配", context=ErrorContext(stage=stage))
        return value

    def _read_rendered_sample(self, checkpoint: Checkpoint) -> tuple[SampleSpec, RenderedDeck, ValidationReport]:
        sample = SampleSpec.parse({key: value for key, value in checkpoint.output.items() if key in {"schema_version", "slides", "theme_tokens", "shared_assets", "outline_checkpoint_id"}})
        return sample, self._artifact(checkpoint), self._validation(checkpoint)

    def _read_rendered_deck(self, checkpoint: Checkpoint, outline: OutlineSpec) -> tuple[DeckSpec, RenderedDeck, ValidationReport]:
        deck = DeckSpec.parse({key: value for key, value in checkpoint.output.items() if key in {"schema_version", "slides", "theme_tokens", "shared_assets", "outline_checkpoint_id", "sample_checkpoint_id"}}, expected_slide_ids=[slide.slide_id for slide in outline.slides])
        return deck, self._artifact(checkpoint), self._validation(checkpoint)

    @staticmethod
    def _artifact(checkpoint: Checkpoint) -> RenderedDeck:
        html_text = checkpoint.output["rendered_html"]
        if hashlib.sha256(html_text.encode()).hexdigest() != checkpoint.output["rendered_sha256"]:
            raise CheckpointConflict("renderer artifact 哈希无效")
        slides = checkpoint.output["slides"]
        return RenderedDeck(html_text, checkpoint.output["rendered_sha256"], checkpoint.output["renderer_version"], {value["slide_id"]: content_sha256(value) for value in slides})

    @staticmethod
    def _validation(checkpoint: Checkpoint) -> ValidationReport:
        value = checkpoint.output["validation"]
        return ValidationReport(value["passed"], tuple(value["issues"]), tuple(value["expected_slide_ids"]), tuple(value["observed_slide_ids"]), tuple(value["asset_paths"]), value.get("browser"), value["evidence_hash"])

    @staticmethod
    def _key(*parts: str) -> str:
        return ":".join(parts)

    def _event(self, task_id: str, stage: str, status: str, *, checkpoint: Checkpoint | None = None) -> None:
        if self.event_sink is None:
            return
        record = {"event": "generation_pipeline", "task_id": task_id, "stage": stage, "status": status, "contract_version": CONTRACT_VERSION, "pipeline_version": PIPELINE_VERSION, "at": _now()}
        if checkpoint is not None:
            record.update({"checkpoint_id": checkpoint.checkpoint_id, "output_sha256": checkpoint.output_sha256})
        self.event_sink(record)
