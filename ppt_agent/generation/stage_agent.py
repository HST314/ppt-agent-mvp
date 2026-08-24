from __future__ import annotations

import hashlib
import threading
import time
from typing import Any, Callable

from ..agent_runtime import AgentRuntime
from ..errors import ValidationError
from ..skill_runtime import ActiveSkillResolver, SkillRuntime, SkillSnapshot
from .contracts import Contract
from .model_gateway import GatewayResult


STAGE_AGENT_VERSION = "2.0"


class StageAgentExecutor:
    """Run generation contracts through the progressive Skill Agent protocol.

    The pipeline still owns checkpoints and workflow-independent validation.
    This executor owns only one model/tool conversation and therefore cannot
    publish an artifact before its optional semantic validator has accepted the
    candidate.
    """

    def __init__(
        self,
        client,
        skill_resolver: ActiveSkillResolver,
        *,
        model: str,
        timeout_seconds: float = 120,
        max_steps: int = 30,
        max_tool_calls: int = 40,
        max_provider_calls: int = 8,
        max_skill_bytes: int = 512 * 1024,
        stage_budgets: dict[str, Any] | None = None,
    ):
        if not isinstance(skill_resolver, ActiveSkillResolver):
            raise ValidationError("StageAgentExecutor 必须使用 ActiveSkillResolver")
        self.client = client
        self.skill_resolver = skill_resolver
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_steps = max_steps
        self.max_tool_calls = max_tool_calls
        self.max_provider_calls = max_provider_calls
        self.max_skill_bytes = max_skill_bytes
        self.stage_budgets = stage_budgets or {}
        self._guard = threading.RLock()
        self._locks: dict[str, threading.Lock] = {}

    @property
    def snapshot(self) -> SkillSnapshot:
        return self.skill_resolver.resolve()

    def identity(self, stage: str, snapshot: SkillSnapshot | None = None) -> str:
        snapshot = snapshot or self.snapshot
        return ":".join((
            STAGE_AGENT_VERSION,
            stage,
            snapshot.digest,
        ))

    def _lock(self, key: str) -> threading.Lock:
        digest = hashlib.sha256(key.encode()).hexdigest()
        with self._guard:
            return self._locks.setdefault(digest, threading.Lock())

    def _runtime(self, stage: str, snapshot: SkillSnapshot) -> AgentRuntime:
        budget = self.stage_budgets.get(stage)
        max_steps = getattr(budget, "max_steps", self.max_steps)
        max_tool_calls = getattr(budget, "max_tool_calls", self.max_tool_calls)
        max_provider_calls = getattr(budget, "max_provider_calls", self.max_provider_calls)
        max_skill_bytes = getattr(budget, "max_skill_bytes", self.max_skill_bytes)
        reserved_final_calls = getattr(budget, "reserved_final_calls", 1)
        max_exploration_rounds = getattr(budget, "max_exploration_rounds", None)
        # max_unique_files is deliberately not forwarded. V2 treats file count
        # as audit information; bytes, time, steps and provider calls remain the
        # engineering protection boundaries.
        return AgentRuntime(
            self.client,
            SkillRuntime(
                snapshot,
                max_file_bytes=min(max_skill_bytes, 256 * 1024),
                max_total_bytes=max_skill_bytes,
            ),
            max_steps=max_steps,
            timeout_seconds=self.timeout_seconds,
            max_tool_calls=max_tool_calls,
            max_provider_calls=max_provider_calls,
            max_exploration_rounds=max_exploration_rounds,
            max_skill_bytes=max_skill_bytes,
            reserved_final_calls=reserved_final_calls,
            allow_schema_override=True,
        )

    def execute(
        self,
        stage: str,
        contract_type: type[Contract],
        *,
        payload: dict[str, Any],
        idempotency_key: str,
        instruction: str,
        validator: Callable[[Contract], dict[str, Any] | None] | None = None,
        snapshot: SkillSnapshot | None = None,
    ) -> GatewayResult:
        runtime_stage = "deck" if stage == "deck_batch" else stage
        if runtime_stage not in {"narrative", "outline", "sample", "deck"}:
            raise ValidationError("StageAgentExecutor 阶段无效")
        last_candidate: dict[str, Any] | None = None

        def validate_candidate(value: dict[str, Any]):
            nonlocal last_candidate
            last_candidate = value
            if runtime_stage == "narrative" and any(
                not isinstance(item, dict) or "evidence_refs" not in item
                for item in value.get("story_arc", [])
            ):
                raise ValidationError("Narrative story_arc 每个节点必须显式声明 evidence_refs")
            contract = contract_type.parse(value)
            return validator(contract) if validator is not None else None

        with self._lock(idempotency_key):
            # The caller may pin the snapshot while constructing its checkpoint
            # key. Reuse those exact bytes for the entire stage, even if an
            # administrator reloads the active Skill concurrently.
            snapshot = snapshot or self.snapshot
            runtime = self._runtime(runtime_stage, snapshot)
            started = time.monotonic()
            try:
                result = runtime.run(
                    runtime_stage,
                    payload,
                    response_schema=contract_type.provider_schema(),
                    system_instruction=(
                        "StageAgentExecutor 的当前严格契约优先于通用阶段示例。"
                        + instruction
                    ),
                    result_validator=validate_candidate,
                    max_semantic_corrections=1,
                )
            except Exception as exc:
                if last_candidate is not None and not hasattr(exc, "rejected_output"):
                    exc.rejected_output = last_candidate
                raise
        contract = contract_type.parse(result.value)
        run = next(item for item in result.audit if item.get("event") == "run")
        terminal = next((item for item in reversed(result.audit) if item.get("event") == "terminal"), {})
        applied = terminal.get("applied_skill_files", [])
        file_hashes = snapshot.manifest
        response_id = result.response_id or ""
        return GatewayResult(
            contract=contract,
            response_id_sha256=hashlib.sha256(response_id.encode()).hexdigest(),
            provider_calls=int(terminal.get("provider_calls", 0)),
            recovery_count=sum(item.get("event") == "semantic_correction" for item in result.audit),
            model=self.model,
            elapsed_ms=round((time.monotonic() - started) * 1000, 3),
            metadata={
                "stage_agent_version": STAGE_AGENT_VERSION,
                "provider_input_sha256": run.get("input_sha256"),
                "schema_correction_count": sum(
                    item.get("event") in {"schema_correction", "technical_correction"}
                    for item in result.audit
                ),
                "skill_digest": snapshot.digest,
                "skill_entry_read": bool(terminal.get("skill_entry_read")),
                "applied_skill_file_hashes": {
                    path: file_hashes[path]
                    for path in applied
                    if path in file_hashes
                },
                "semantic_validation": result.validation or {},
            },
        )
