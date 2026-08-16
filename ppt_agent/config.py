from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import yaml
from dotenv import load_dotenv

from .errors import ValidationError


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    model: str
    api_key_env: str
    base_url_env: str
    request_timeout_seconds: float
    run_timeout_seconds: float
    max_steps: int
    api_key: str
    base_url: str
    structured_output: str = "auto"

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
class RuntimeConfig:
    mode: str
    generation: ModelConfig | None = None
    inspection: ModelConfig | None = None
    inspection_fallback: bool = False
    clarification: ClarificationConfig = ClarificationConfig()

    def public(self) -> dict:
        return {
            "gateway": {"mode": self.mode},
            "models": {
                "generation": self.generation.public() if self.generation else None,
                "inspection": self.inspection.public() if self.inspection else None,
                "inspection_fallback": self.inspection_fallback,
            },
            "clarification": self.clarification.public(),
        }


_ROOT_KEYS = {"gateway", "models", "skills", "capabilities", "clarification"}
_MODEL_KEYS = {"provider", "model", "api_key_env", "base_url_env", "timeout_seconds", "request_timeout_seconds", "run_timeout_seconds", "max_steps", "fallback_to_generation", "structured_output"}
_STRUCTURED_OUTPUT_MODES = {"auto", "json_schema", "prompt"}
_CLARIFICATION_KEYS = {"max_questions_per_round", "max_rounds", "style"}
_CLARIFICATION_STYLES = {"minimal", "comprehensive"}


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
    api_key, base_url = os.getenv(api_key_env, ""), os.getenv(base_url_env, "")
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
    max_steps = value.get("max_steps", 12)
    for label, timeout in (("request_timeout_seconds", request_timeout), ("run_timeout_seconds", run_timeout)):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValidationError(f"models.{name}.{label} 必须为 number")
    if isinstance(max_steps, bool) or not isinstance(max_steps, int):
        raise ValidationError(f"models.{name}.max_steps 必须为 integer")
    if not 1 <= request_timeout <= 600 or not 10 <= run_timeout <= 3600 or not 1 <= max_steps <= 100:
        raise ValidationError(f"models.{name} 的超时或步数超出范围")
    structured_output = value.get("structured_output", "auto")
    if structured_output not in _STRUCTURED_OUTPUT_MODES:
        raise ValidationError(f"models.{name}.structured_output 只能是 auto、json_schema 或 prompt")
    return ModelConfig(provider, model, api_key_env, base_url_env, request_timeout, run_timeout, max_steps, api_key, base_url.rstrip("/"), structured_output)


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
    if mode == "fake":
        return RuntimeConfig(mode="fake", clarification=clarification)
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
    return RuntimeConfig("agent", generation, inspection, fallback, clarification)
