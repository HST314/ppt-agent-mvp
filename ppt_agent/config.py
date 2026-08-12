from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv

from .errors import ValidationError


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    model: str
    api_key_env: str
    base_url_env: str
    timeout_seconds: float
    max_steps: int
    api_key: str
    base_url: str

    def public(self) -> dict:
        value = asdict(self)
        value.pop("api_key")
        return value


@dataclass(frozen=True)
class RuntimeConfig:
    mode: str
    generation: ModelConfig | None = None
    inspection: ModelConfig | None = None
    inspection_fallback: bool = False

    def public(self) -> dict:
        return {
            "gateway": {"mode": self.mode},
            "models": {
                "generation": self.generation.public() if self.generation else None,
                "inspection": self.inspection.public() if self.inspection else None,
                "inspection_fallback": self.inspection_fallback,
            },
        }


_ROOT_KEYS = {"gateway", "models", "skills", "capabilities"}
_MODEL_KEYS = {"provider", "model", "api_key_env", "base_url_env", "timeout_seconds", "max_steps", "fallback_to_generation"}


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
    parsed = urlparse(base_url)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}):
        raise ValidationError(f"models.{name} Base URL 必须使用 HTTPS（本机回环地址除外）")
    try:
        timeout = float(value.get("timeout_seconds", 60))
        max_steps = int(value.get("max_steps", 12))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"models.{name} 的超时或步数无效") from exc
    if not 1 <= timeout <= 600 or not 1 <= max_steps <= 100:
        raise ValidationError(f"models.{name} 的超时或步数超出范围")
    return ModelConfig(provider, model, api_key_env, base_url_env, timeout, max_steps, api_key, base_url.rstrip("/"))


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
    if mode == "fake":
        return RuntimeConfig(mode="fake")
    models = _mapping(raw.get("models"), "models")
    if set(models) - {"generation", "inspection"}:
        raise ValidationError("models 包含未知字段")
    generation = _model(models.get("generation"), "generation", required=True)
    inspection_raw = models.get("inspection")
    fallback = bool(inspection_raw and inspection_raw.get("fallback_to_generation", False))
    fallback_only = isinstance(inspection_raw, dict) and set(inspection_raw) == {"fallback_to_generation"}
    inspection = None if fallback_only else _model(inspection_raw, "inspection", required=False)
    if inspection is None:
        if not fallback:
            raise ValidationError("缺少检查模型且未启用生成模型回退")
        inspection = generation
    return RuntimeConfig("agent", generation, inspection, fallback)
