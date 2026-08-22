from __future__ import annotations

import hashlib
import json
import mimetypes
from pathlib import Path, PurePosixPath

from .errors import ValidationError


class SkillRuntime:
    """Read-only, quota-bound view of a locked standard Skill directory."""

    TEXT_SUFFIXES = {".md", ".html", ".js", ".mjs", ".css", ".json", ".txt"}
    STAGE_FILES = {
        "narrative": frozenset({"references/planning-summary.md"}),
        "outline": frozenset({"references/planning-summary.md"}),
        "sample": frozenset({"references/design-pack-v1.md"}),
        "deck": frozenset({"references/design-pack-v1.md"}),
        "inspection": frozenset({"SKILL.md", "references/checklist.md"}),
    }

    def __init__(self, root: str | Path, *, max_file_bytes: int = 256 * 1024, max_total_bytes: int = 512 * 1024):
        self.root = Path(root).resolve()
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.total_bytes = 0
        self.manifest = self._manifest()
        self.skill_name = "guizang-ppt"
        self.skill_version = "unknown"
        try:
            lock = json.loads((self.root / "SKILL_LOCK.json").read_text(encoding="utf-8"))
            self.skill_name = str(lock.get("skill") or self.skill_name)
            self.skill_version = str(lock.get("version") or self.skill_version)
        except (OSError, json.JSONDecodeError):
            pass
        self._verify_lock()

    @classmethod
    def builtin(cls) -> "SkillRuntime":
        return cls(Path(__file__).parent / "builtin_skills" / "guizang-ppt")

    def _manifest(self) -> dict[str, str]:
        try:
            value = json.loads((self.root / "SKILL_LOCK.json").read_text(encoding="utf-8"))
            files = value["files"]
        except (OSError, KeyError, json.JSONDecodeError, TypeError) as exc:
            raise ValidationError("Skill lock 文件无效") from exc
        if not isinstance(files, dict) or not files:
            raise ValidationError("Skill lock 文件无效")
        return files

    def _verify_lock(self) -> None:
        for name, expected in self.manifest.items():
            path = self._resolve(name)
            try:
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as exc:
                raise ValidationError(f"Skill 固定文件缺失：{name}") from exc
            if actual != expected:
                raise ValidationError(f"Skill 固定文件校验失败：{name}")

    def _resolve(self, name: str) -> Path:
        if not isinstance(name, str) or not name or "\\" in name or "\0" in name:
            raise ValidationError("Skill 路径无效")
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValidationError("Skill 路径越界")
        path = self.root.joinpath(*relative.parts)
        if path.is_symlink():
            raise ValidationError("Skill 不允许软链接")
        resolved = path.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise ValidationError("Skill 路径越界")
        return resolved

    def _allowed(self, name: str) -> bool:
        return name == "SKILL.md" or name.startswith("references/") or name.startswith("assets/") or name.startswith("scripts/")

    def files_for_stage(self, stage: str) -> frozenset[str] | None:
        """Return a least-privilege file view for planning/checking stages.

        HTML-producing stages retain the complete locked Skill because they need
        templates, layouts and themes. Planning stages only need the workflow and
        quality checklist; hiding rendering references keeps weak models from
        spending an entire step budget reading irrelevant files.
        """
        return self.STAGE_FILES.get(stage)

    def list_skill_files(self, *, allowed_files: frozenset[str] | None = None) -> dict:
        files = sorted(
            name for name in self.manifest
            if self._allowed(name) and (allowed_files is None or name in allowed_files)
        )
        return {"skill": self.skill_name, "version": self.skill_version, "files": files}

    def _normalize_tool_path(self, name: str) -> str:
        """容忍模型常见的路径前缀写法（``./``、``/``、``<skill名>/``）。

        归一化只剥离无害前缀；随后的越界检查、白名单与哈希校验保持严格，
        安全语义不降级。
        """
        if not isinstance(name, str):
            return name
        normalized = name.strip()
        while normalized.startswith("./"):
            normalized = normalized[2:]
        normalized = normalized.lstrip("/")
        skill_prefix = f"{self.skill_name}/"
        if normalized.startswith(skill_prefix):
            normalized = normalized[len(skill_prefix):]
        return normalized

    def normalize_tool_path(self, name: str) -> str:
        """Expose the canonical locked path for runtime de-duplication."""
        return self._normalize_tool_path(name)

    def _whitelist_error(self, asset: bool, allowed_files: frozenset[str] | None = None) -> ValidationError:
        # 报错回传模型时附带合法路径列表，模型下一轮即可自我修正。
        files = self.list_skill_files(allowed_files=allowed_files)["files"]
        if asset:
            allowed = "、".join(name for name in files if name.startswith("assets/")) or "（无）"
            return ValidationError(f"Asset 不在只读白名单；合法路径：{allowed}")
        return ValidationError("Skill 文件不在锁定只读白名单；合法路径：" + "、".join(files))

    def _locked_bytes(self, name: str, *, asset: bool = False, allowed_files: frozenset[str] | None = None) -> tuple[Path, bytes]:
        name = self._normalize_tool_path(name)
        path = self._resolve(name)
        if name not in self.manifest or not self._allowed(name) or not path.is_file() or (allowed_files is not None and name not in allowed_files):
            raise self._whitelist_error(asset, allowed_files)
        if asset and not name.startswith("assets/"):
            raise self._whitelist_error(asset=True, allowed_files=allowed_files)
        try:
            content = path.read_bytes()
        except (OSError, ValueError) as exc:
            raise ValidationError("Skill 文件读取失败") from exc
        if hashlib.sha256(content).hexdigest() != self.manifest[name]:
            raise ValidationError(f"Skill 固定文件校验失败：{name}")
        return path, content

    def read_skill_file(self, name: str, *, allowed_files: frozenset[str] | None = None) -> dict:
        name = self._normalize_tool_path(name)
        path, raw = self._locked_bytes(name, allowed_files=allowed_files)
        if path.suffix.lower() not in self.TEXT_SUFFIXES:
            raise ValidationError("Skill 文件不在只读白名单（仅可读取文本文件；二进制 Asset 请用 get_asset_info）")
        size = len(raw)
        if size > self.max_file_bytes:
            raise ValidationError("Skill 单文件超过读取上限")
        if self.total_bytes + size > self.max_total_bytes:
            raise ValidationError("Skill 累计读取超过上限")
        try:
            content = raw.decode("utf-8")
        except UnicodeError as exc:
            raise ValidationError("Skill 文件无法按 UTF-8 读取") from exc
        self.total_bytes += size
        return {"path": name, "content": content, "bytes": size, "sha256": hashlib.sha256(content.encode()).hexdigest()}

    def get_asset_info(self, name: str, *, allowed_files: frozenset[str] | None = None) -> dict:
        name = self._normalize_tool_path(name)
        path, raw = self._locked_bytes(name, asset=True, allowed_files=allowed_files)
        size = len(raw)
        if size > self.max_file_bytes:
            raise ValidationError("Skill 单文件超过读取上限")
        if self.total_bytes + size > self.max_total_bytes:
            raise ValidationError("Skill 累计读取超过上限")
        digest = hashlib.sha256(raw).hexdigest()
        self.total_bytes += size
        return {"path": name, "bytes": size, "media_type": mimetypes.guess_type(name)[0] or "application/octet-stream", "sha256": digest}

    def read_locked_text(self, name: str) -> str:
        """Read a verified server-owned text asset without charging Agent quota."""
        path, raw = self._locked_bytes(name)
        if path.suffix.lower() not in self.TEXT_SUFFIXES:
            raise ValidationError("锁定模板不是文本文件")
        try:
            return raw.decode("utf-8")
        except UnicodeError as exc:
            raise ValidationError("锁定模板无法按 UTF-8 读取") from exc

    def dispatch(self, name: str, arguments: dict, *, allowed_files: frozenset[str] | None = None) -> dict:
        if name == "list_skill_files":
            return self.list_skill_files(allowed_files=allowed_files)
        if name == "read_skill_file":
            return self.read_skill_file(arguments.get("path"), allowed_files=allowed_files)
        if name == "get_asset_info":
            return self.get_asset_info(arguments.get("path"), allowed_files=allowed_files)
        raise ValidationError("Agent 请求了未授权工具")
