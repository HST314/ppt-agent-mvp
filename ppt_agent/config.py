from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import yaml
from dotenv import load_dotenv

from .errors import ValidationError


@dataclass(frozen=True)
class StageBudgetConfig:
    max_steps: int
    max_tool_calls: int
    max_provider_calls: int
    max_exploration_rounds: int
    max_unique_files: int
    max_skill_bytes: int
    reserved_final_calls: int


DEFAULT_STAGE_BUDGETS = {
    "sample": StageBudgetConfig(8, 4, 6, 2, 4, 128 * 1024, 2),
    "deck": StageBudgetConfig(12, 8, 10, 3, 4, 128 * 1024, 2),
}


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    model: str
    api_key_env: str
    base_url_env: str
    request_timeout_seconds: float
    run_timeout_seconds: float
    job_timeout_seconds: float
    max_steps: int
    max_tool_calls: int
    max_provider_calls: int
    api_key: str
    base_url: str
    structured_output: str = "auto"
    stage_budgets: dict[str, StageBudgetConfig] | None = None

    def public(self) -> dict:
        value = asdict(self)
        value.pop("api_key")
        # Keep snapshots safe even if a ModelConfig is constructed outside the
        # validated YAML loader. Credentials and URL metadata are never public.
        parsed = urlparse(self.base_url)
        hostname = parsed.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        try:
            port = f":{parsed.port}" if parsed.port is not None else ""
        except ValueError:
            port = ""
        value["base_url"] = urlunparse((parsed.scheme, f"{hostname}{port}", parsed.path, "", "", ""))
        return value


@dataclass(frozen=True)
class ClarificationConfig:
    max_questions_per_round: int = 3
    max_rounds: int = 3
    style: str = "comprehensive"

    def public(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class JobSettingsConfig:
    generation_timeout_seconds: int = 630
    inspection_timeout_seconds: int = 630
    delivery_timeout_seconds: int = 180

    def public(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ReviewSettingsConfig:
    default_max_rounds: int = 2

    def public(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeConfig:
    mode: str
    generation: ModelConfig | None = None
    inspection: ModelConfig | None = None
    inspection_fallback: bool = False
    clarification: ClarificationConfig = ClarificationConfig()
    jobs: JobSettingsConfig = JobSettingsConfig()
    review: ReviewSettingsConfig = ReviewSettingsConfig()

    def public(self) -> dict:
        return {
            "gateway": {"mode": self.mode},
            "models": {
                "generation": self.generation.public() if self.generation else None,
                "inspection": self.inspection.public() if self.inspection else None,
                "inspection_fallback": self.inspection_fallback,
            },
            "clarification": self.clarification.public(),
            "jobs": self.jobs.public(),
            "review": self.review.public(),
        }


_ROOT_KEYS = {"gateway", "models", "skills", "capabilities", "clarification", "jobs", "review"}
_MODEL_KEYS = {
    "provider", "model", "api_key_env", "base_url_env", "timeout_seconds",
    "request_timeout_seconds", "run_timeout_seconds", "job_timeout_seconds",
    "max_steps", "max_tool_calls", "max_provider_calls",
    "fallback_to_generation", "structured_output", "stage_budgets",
}
_STAGE_BUDGET_KEYS = {
    "max_steps", "max_tool_calls", "max_provider_calls", "max_exploration_rounds",
    "max_unique_files", "max_skill_bytes", "reserved_final_calls",
}
_STRUCTURED_OUTPUT_MODES = {"auto", "json_schema", "prompt"}
_CLARIFICATION_KEYS = {"max_questions_per_round", "max_rounds", "style"}
_CLARIFICATION_STYLES = {"minimal", "comprehensive"}
_JOB_KEYS = {"generation_timeout_seconds", "inspection_timeout_seconds", "delivery_timeout_seconds"}
_REVIEW_KEYS = {"default_max_rounds"}


def _mapping(value, name: str) -> dict:
    if not isinstance(value, dict):
        raise ValidationError(f"配置 {name} 必须为 object")
    return value


def _model(value: dict, name: str, *, required: bool) -> ModelConfig | None:
    if value is None and not required:
        return None
    value = _mapping(value, f"models.{name}")
    unknown = set(value) - _MODEL_KEYS
    if unknown:
        raise ValidationError(f"models.{name} 包含未知字段：{', '.join(sorted(unknown))}")
    provider = value.get("provider")
    if provider != "openai_responses":
        raise ValidationError(f"models.{name}.provider 只能是 openai_responses")
    model = value.get("model")
    api_key_env = value.get("api_key_env")
    base_url_env = value.get("base_url_env")
    if not all(isinstance(item, str) and item.strip() for item in (model, api_key_env, base_url_env)):
        raise ValidationError(f"models.{name} 缺少模型或环境变量名称")
    # Values inherited from a CRLF dotenv file sourced by a shell can retain a
    # trailing carriage return. Normalize their surrounding whitespace before
    # URL parsing and SDK construction so a valid endpoint is not rejected.
    api_key = os.getenv(api_key_env, "").strip()
    base_url = os.getenv(base_url_env, "").strip()
    if not api_key or not base_url:
        raise ValidationError(f"models.{name} 引用的环境变量未配置")
    try:
        parsed = urlparse(base_url)
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise ValidationError(f"models.{name} Base URL 无效") from exc
    if not hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValidationError(f"models.{name} Base URL 不得缺少主机或包含凭证、查询参数、片段")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and hostname in {"127.0.0.1", "localhost"}):
        raise ValidationError(f"models.{name} Base URL 必须使用 HTTPS（本机回环地址除外）")
    legacy_timeout = value.get("timeout_seconds")
    request_timeout = value.get("request_timeout_seconds")
    if legacy_timeout is not None and request_timeout is not None:
        raise ValidationError(f"models.{name} 不能同时配置 timeout_seconds 与 request_timeout_seconds")
    if request_timeout is None:
        # timeout_seconds 是 request_timeout_seconds 的兼容别名；整轮运行预算
        # 由 run_timeout_seconds 独立控制。
        request_timeout = legacy_timeout if legacy_timeout is not None else 60
    run_timeout = value.get("run_timeout_seconds", 300)
    job_timeout = value.get("job_timeout_seconds")
    if job_timeout is None:
        job_timeout = run_timeout + 30 if isinstance(run_timeout, (int, float)) and not isinstance(run_timeout, bool) else 330
    max_steps = value.get("max_steps", 12)
    max_tool_calls = value.get("max_tool_calls", 24)
    max_provider_calls = value.get("max_provider_calls", 8)
    for label, timeout in (
        ("request_timeout_seconds", request_timeout),
        ("run_timeout_seconds", run_timeout),
        ("job_timeout_seconds", job_timeout),
    ):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValidationError(f"models.{name}.{label} 必须为 number")
    for label, limit in (
        ("max_steps", max_steps),
        ("max_tool_calls", max_tool_calls),
        ("max_provider_calls", max_provider_calls),
    ):
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValidationError(f"models.{name}.{label} 必须为 integer")
    if (
        not 1 <= request_timeout < run_timeout <= 3600
        or not run_timeout < job_timeout <= 3660
        or not 1 <= max_steps <= 100
        or not 1 <= max_tool_calls <= 200
        or not 1 <= max_provider_calls <= 20
    ):
        raise ValidationError(f"models.{name} 的超时或步数超出范围")
    structured_output = value.get("structured_output", "auto")
    if structured_output not in _STRUCTURED_OUTPUT_MODES:
        raise ValidationError(f"models.{name}.structured_output 只能是 auto、json_schema 或 prompt")
    raw_stage_budgets = value.get("stage_budgets", {})
    raw_stage_budgets = _mapping(raw_stage_budgets, f"models.{name}.stage_budgets")
    if set(raw_stage_budgets) - {"sample", "deck"}:
        raise ValidationError(f"models.{name}.stage_budgets 只能配置 sample 或 deck")
    stage_budgets: dict[str, StageBudgetConfig] = {}
    for stage, defaults in DEFAULT_STAGE_BUDGETS.items():
        raw_budget = _mapping(raw_stage_budgets.get(stage, {}), f"models.{name}.stage_budgets.{stage}")
        unknown_budget = set(raw_budget) - _STAGE_BUDGET_KEYS
        if unknown_budget:
            raise ValidationError(
                f"models.{name}.stage_budgets.{stage} 包含未知字段：{', '.join(sorted(unknown_budget))}"
            )
        budget = StageBudgetConfig(**{
            field: raw_budget.get(field, getattr(defaults, field))
            for field in _STAGE_BUDGET_KEYS
        })
        values = asdict(budget)
        if any(isinstance(item, bool) or not isinstance(item, int) for item in values.values()):
            raise ValidationError(f"models.{name}.stage_budgets.{stage} 所有字段必须为 integer")
        if (
            not 2 <= budget.max_steps <= 100
            or not 1 <= budget.max_tool_calls <= 200
            or not 2 <= budget.max_provider_calls <= 20
            or not 0 <= budget.max_exploration_rounds <= 10
            or not 1 <= budget.max_unique_files <= 20
            or not 1024 <= budget.max_skill_bytes <= 512 * 1024
            or not 1 <= budget.reserved_final_calls < budget.max_provider_calls
            or budget.max_exploration_rounds > budget.max_provider_calls - budget.reserved_final_calls
        ):
            raise ValidationError(f"models.{name}.stage_budgets.{stage} 预算超出范围或无法预留最终输出请求")
        stage_budgets[stage] = budget
    return ModelConfig(
        provider, model, api_key_env, base_url_env,
        request_timeout, run_timeout, job_timeout,
        max_steps, max_tool_calls, max_provider_calls,
        api_key, base_url.rstrip("/"), structured_output, stage_budgets,
    )


def _clarification(value) -> ClarificationConfig:
    value = _mapping(value, "clarification")
    unknown = set(value) - _CLARIFICATION_KEYS
    if unknown:
        raise ValidationError(f"clarification 包含未知字段：{', '.join(sorted(unknown))}")
    max_questions = value.get("max_questions_per_round", 3)
    max_rounds = value.get("max_rounds", 3)
    for label, item in (("max_questions_per_round", max_questions), ("max_rounds", max_rounds)):
        if isinstance(item, bool) or not isinstance(item, int):
            raise ValidationError(f"clarification.{label} 必须为 integer")
    if not 1 <= max_questions <= 10 or not 1 <= max_rounds <= 5:
        raise ValidationError("clarification 的题数或轮次超出范围")
    style = value.get("style", "comprehensive")
    if style not in _CLARIFICATION_STYLES:
        raise ValidationError("clarification.style 只能是 minimal 或 comprehensive")
    return ClarificationConfig(max_questions, max_rounds, style)


def _jobs(value, *, generation_default: int, inspection_default: int) -> JobSettingsConfig:
    value = _mapping(value, "jobs")
    unknown = set(value) - _JOB_KEYS
    if unknown:
        raise ValidationError(f"jobs 包含未知字段：{', '.join(sorted(unknown))}")
    settings = JobSettingsConfig(
        generation_timeout_seconds=value.get("generation_timeout_seconds", generation_default),
        inspection_timeout_seconds=value.get("inspection_timeout_seconds", inspection_default),
        delivery_timeout_seconds=value.get("delivery_timeout_seconds", 180),
    )
    for label, item in settings.public().items():
        if isinstance(item, bool) or not isinstance(item, int) or not 30 <= item <= 3660:
            raise ValidationError(f"jobs.{label} 超出范围")
    return settings


def _review(value) -> ReviewSettingsConfig:
    value = _mapping(value, "review")
    unknown = set(value) - _REVIEW_KEYS
    if unknown:
        raise ValidationError(f"review 包含未知字段：{', '.join(sorted(unknown))}")
    rounds = value.get("default_max_rounds", 2)
    if isinstance(rounds, bool) or not isinstance(rounds, int) or not 0 <= rounds <= 10:
        raise ValidationError("review.default_max_rounds 超出范围")
    return ReviewSettingsConfig(rounds)


def load_config(path: str | Path | None = None, *, env_file: str | Path | None = None) -> RuntimeConfig:
    load_dotenv(env_file, override=False) if env_file is not None else load_dotenv(override=False)
    config_path = Path(path or os.getenv("PPT_AGENT_CONFIG", "config/ppt-agent.yaml"))
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValidationError(f"无法读取配置文件：{config_path}") from exc
    raw = _mapping(raw, "root")
    unknown = set(raw) - _ROOT_KEYS
    if unknown:
        raise ValidationError(f"配置包含未知字段：{', '.join(sorted(unknown))}")
    gateway = _mapping(raw.get("gateway", {}), "gateway")
    if set(gateway) - {"mode"}:
        raise ValidationError("gateway 包含未知字段")
    mode = gateway.get("mode", "fake")
    if mode not in {"fake", "agent"}:
        raise ValidationError("gateway.mode 只能是 fake 或 agent")
    skills = _mapping(raw.get("skills", {}), "skills")
    if set(skills) - {"active"} or ("active" in skills and not isinstance(skills["active"], str)):
        raise ValidationError("skills 配置无效")
    capabilities = _mapping(raw.get("capabilities", {}), "capabilities")
    allowed_capabilities = {"network_tools", "image_input", "image_generation"}
    if set(capabilities) - allowed_capabilities or any(not isinstance(value, bool) for value in capabilities.values()):
        raise ValidationError("capabilities 配置无效")
    if any(capabilities.values()):
        raise ValidationError("阶段 A 不允许启用网络或图片能力")
    clarification = _clarification(raw.get("clarification", {}))
    review = _review(raw.get("review", {}))
    if mode == "fake":
        jobs = _jobs(raw.get("jobs", {}), generation_default=630, inspection_default=630)
        return RuntimeConfig(mode="fake", clarification=clarification, jobs=jobs, review=review)
    models = _mapping(raw.get("models"), "models")
    if set(models) - {"generation", "inspection"}:
        raise ValidationError("models 包含未知字段")
    generation = _model(models.get("generation"), "generation", required=True)
    inspection_raw = models.get("inspection")
    if inspection_raw is not None:
        inspection_raw = _mapping(inspection_raw, "models.inspection")
    fallback = inspection_raw.get("fallback_to_generation", False) if inspection_raw is not None else False
    if not isinstance(fallback, bool):
        raise ValidationError("models.inspection.fallback_to_generation 必须为 boolean")
    fallback_only = inspection_raw is not None and set(inspection_raw) == {"fallback_to_generation"}
    inspection = None if fallback_only else _model(inspection_raw, "inspection", required=False)
    if inspection is None:
        if not fallback:
            raise ValidationError("缺少检查模型且未启用生成模型回退")
        inspection = generation
    jobs = _jobs(
        raw.get("jobs", {}),
        generation_default=int(generation.job_timeout_seconds),
        inspection_default=int(inspection.job_timeout_seconds),
    )
    return RuntimeConfig(
        mode="agent",
        generation=generation,
        inspection=inspection,
        inspection_fallback=fallback,
        clarification=clarification,
        jobs=jobs,
        review=review,
    )
