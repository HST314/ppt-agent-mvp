from __future__ import annotations

import hashlib
import os
import threading
import uuid
from pathlib import Path

import yaml

from .config import RuntimeConfig, load_config
from .errors import ValidationError


class GlobalSettingsStore:
    """Atomic application settings stored in the active runtime YAML."""

    _locks_guard = threading.Lock()
    _locks: dict[str, threading.RLock] = {}

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        key = str(self.path)
        with self._locks_guard:
            self._lock = self._locks.setdefault(key, threading.RLock())

    @staticmethod
    def _values(config: RuntimeConfig) -> dict:
        return {
            "workflow": config.clarification.public(),
            "jobs": config.jobs.public(),
            "review": config.review.public(),
        }

    @staticmethod
    def _snapshot(config: RuntimeConfig, raw: bytes) -> dict:
        return {
            "values": GlobalSettingsStore._values(config),
            "config_revision": hashlib.sha256(raw).hexdigest(),
            "scope": "global",
        }

    def read(self) -> dict:
        with self._lock:
            raw = self.path.read_bytes()
            return self._snapshot(load_config(self.path), raw)

    def update(self, patch: dict) -> dict:
        if not isinstance(patch, dict) or set(patch) - {"workflow", "jobs", "review"}:
            raise ValidationError("设置分组无效")
        with self._lock:
            current_config = load_config(self.path)
            merged = self._values(current_config)
            for group, group_patch in patch.items():
                if not isinstance(group_patch, dict) or set(group_patch) - set(merged[group]):
                    raise ValidationError(f"{group} 设置字段无效")
                merged[group].update(group_patch)

            try:
                payload = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError) as exc:
                raise ValidationError(f"无法读取配置文件：{self.path}") from exc
            if not isinstance(payload, dict):
                raise ValidationError("配置 root 必须为 object")
            payload["clarification"] = merged["workflow"]
            payload["jobs"] = merged["jobs"]
            payload["review"] = merged["review"]
            rendered = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).encode("utf-8")
            temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
            try:
                with open(temporary, "xb") as stream:
                    stream.write(rendered)
                    stream.flush()
                    os.fsync(stream.fileno())
                validated = load_config(temporary)
                os.replace(temporary, self.path)
                try:
                    directory = os.open(self.path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                except OSError:
                    pass
            finally:
                temporary.unlink(missing_ok=True)
            return self._snapshot(validated, rendered)
