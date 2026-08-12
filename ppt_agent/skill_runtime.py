from __future__ import annotations

import hashlib
import json
import mimetypes
from pathlib import Path, PurePosixPath

from .errors import ValidationError


class SkillRuntime:
    """Read-only, quota-bound view of a locked standard Skill directory."""

    TEXT_SUFFIXES = {".md", ".html", ".js", ".css", ".json", ".txt"}

    def __init__(self, root: str | Path, *, max_file_bytes: int = 256 * 1024, max_total_bytes: int = 512 * 1024):
        self.root = Path(root).resolve()
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.total_bytes = 0
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
        for name, expected in self._manifest().items():
            path = self._resolve(name)
            try:
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as exc:
                raise ValidationError(f"Skill 固定文件缺失：{name}") from exc
            if actual != expected:
                raise ValidationError(f"Skill 固定文件校验失败：{name}")

    def _resolve(self, name: str) -> Path:
        if not isinstance(name, str) or not name or "\\" in name:
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
        return name == "SKILL.md" or name.startswith("references/") or name.startswith("assets/")

    def list_skill_files(self) -> dict:
        files = sorted(name for name in self._manifest() if self._allowed(name))
        return {"skill": "guizang-ppt", "files": files}

    def read_skill_file(self, name: str) -> dict:
        path = self._resolve(name)
        if not self._allowed(name) or path.suffix.lower() not in self.TEXT_SUFFIXES or not path.is_file():
            raise ValidationError("Skill 文件不在只读白名单")
        size = path.stat().st_size
        if size > self.max_file_bytes:
            raise ValidationError("Skill 单文件超过读取上限")
        if self.total_bytes + size > self.max_total_bytes:
            raise ValidationError("Skill 累计读取超过上限")
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValidationError("Skill 文件无法按 UTF-8 读取") from exc
        self.total_bytes += size
        return {"path": name, "content": content, "bytes": size, "sha256": hashlib.sha256(content.encode()).hexdigest()}

    def get_asset_info(self, name: str) -> dict:
        path = self._resolve(name)
        if not name.startswith("assets/") or not path.is_file():
            raise ValidationError("Asset 不在只读白名单")
        size = path.stat().st_size
        if size > self.max_file_bytes:
            raise ValidationError("Skill 单文件超过读取上限")
        if self.total_bytes + size > self.max_total_bytes:
            raise ValidationError("Skill 累计读取超过上限")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.total_bytes += size
        return {"path": name, "bytes": size, "media_type": mimetypes.guess_type(name)[0] or "application/octet-stream", "sha256": digest}

    def dispatch(self, name: str, arguments: dict) -> dict:
        if name == "list_skill_files":
            return self.list_skill_files()
        if name == "read_skill_file":
            return self.read_skill_file(arguments.get("path"))
        if name == "get_asset_info":
            return self.get_asset_info(arguments.get("path"))
        raise ValidationError("Agent 请求了未授权工具")
