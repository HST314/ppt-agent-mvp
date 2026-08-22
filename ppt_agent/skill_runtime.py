from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import stat
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

from .errors import ValidationError


_STANDARD_DIRECTORIES = frozenset({"references", "assets", "scripts"})
_SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|\Z)")


def _validate_relative_path(name: str, *, label: str = "Skill 路径") -> PurePosixPath:
    if not isinstance(name, str) or not name or "\\" in name or "\0" in name:
        raise ValidationError(f"{label}无效")
    relative = PurePosixPath(name)
    if not relative.parts or relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValidationError(f"{label}越界")
    return relative


def _read_regular_file(
    path: Path,
    *,
    max_bytes: int | None = None,
    limit_message: str = "Skill 文件超过快照上限",
) -> bytes:
    """Read a regular file without following a final-component symlink."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValidationError("Skill 文件读取失败") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ValidationError("Skill 仅允许普通文件")
        if max_bytes is not None and details.st_size > max_bytes:
            raise ValidationError(limit_message)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise ValidationError(limit_message)
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _metadata(raw: bytes) -> tuple[str, str]:
    if len(raw) > 256 * 1024:
        raise ValidationError("SKILL.md 超过 256 KiB 元数据读取上限")
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise ValidationError("SKILL.md 必须是 UTF-8 文本") from exc
    match = _FRONTMATTER.match(text.lstrip("\ufeff"))
    if not match:
        raise ValidationError("SKILL.md 缺少 YAML frontmatter")
    try:
        value = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise ValidationError("SKILL.md frontmatter 无效") from exc
    if not isinstance(value, dict):
        raise ValidationError("SKILL.md frontmatter 必须为 object")
    name = value.get("name")
    description = value.get("description")
    if not isinstance(name, str) or not _SKILL_NAME.fullmatch(name) or len(name) > 128:
        raise ValidationError("SKILL.md frontmatter.name 无效")
    if (
        not isinstance(description, str)
        or not description.strip()
        or "\0" in description
        or len(description.encode("utf-8")) > 8 * 1024
    ):
        raise ValidationError("SKILL.md frontmatter.description 无效")
    return name, description.strip()


@dataclass(frozen=True)
class SkillSnapshot:
    """Immutable identity and content hashes for one resolved Skill directory."""

    root: Path
    directory_name: str
    name: str
    description: str
    digest: str
    file_hashes: tuple[tuple[str, str], ...]
    total_bytes: int

    @property
    def manifest(self) -> dict[str, str]:
        return dict(self.file_hashes)


class ActiveSkillResolver:
    """Resolve the single administrator-configured Skill below a trusted root.

    A resolver caches the validated snapshot. Calling :meth:`reload` validates a
    replacement before atomically publishing it, so runtimes already created
    for an in-flight Job keep their original digest and file hashes.
    """

    def __init__(
        self,
        root: str | Path,
        active: str,
        *,
        expected_digest: str | None = None,
        max_files: int = 4096,
        max_snapshot_bytes: int = 64 * 1024 * 1024,
        max_snapshot_file_bytes: int = 16 * 1024 * 1024,
    ):
        raw_root = Path(root)
        if raw_root.is_symlink():
            raise ValidationError("skills.root 不允许软链接")
        try:
            resolved_root = raw_root.resolve(strict=True)
        except OSError as exc:
            raise ValidationError("skills.root 不存在") from exc
        if not resolved_root.is_dir():
            raise ValidationError("skills.root 必须是目录")
        if (
            isinstance(max_files, bool)
            or not isinstance(max_files, int)
            or not 1 <= max_files <= 10000
            or isinstance(max_snapshot_bytes, bool)
            or not isinstance(max_snapshot_bytes, int)
            or not 1024 <= max_snapshot_bytes <= 1024 * 1024 * 1024
            or isinstance(max_snapshot_file_bytes, bool)
            or not isinstance(max_snapshot_file_bytes, int)
            or not 1024 <= max_snapshot_file_bytes <= max_snapshot_bytes
        ):
            raise ValidationError("Skill 快照限制无效")
        if expected_digest is not None and (
            not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest)
        ):
            raise ValidationError("Skill 完整性摘要无效")
        self.root = resolved_root
        self.active = self._active_name(active)
        self.expected_digest = expected_digest
        self.max_files = max_files
        self.max_snapshot_bytes = max_snapshot_bytes
        self.max_snapshot_file_bytes = max_snapshot_file_bytes
        self._lock = threading.RLock()
        self._snapshot: SkillSnapshot | None = None

    @staticmethod
    def _active_name(active: str) -> str:
        relative = _validate_relative_path(active, label="skills.active")
        return relative.as_posix()

    @classmethod
    def builtin(cls) -> "ActiveSkillResolver":
        return cls(Path(__file__).parent / "builtin_skills", "guizang-ppt")

    def _active_root(self, active: str) -> Path:
        relative = _validate_relative_path(active, label="skills.active")
        candidate = self.root.joinpath(*relative.parts)
        current = self.root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValidationError("skills.active 不允许软链接")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise ValidationError("skills.active 目录不存在") from exc
        if self.root not in resolved.parents or not resolved.is_dir():
            raise ValidationError("skills.active 必须是 skills.root 下的目录")
        return resolved

    @staticmethod
    def _standard_files(skill_root: Path):
        entry = skill_root / "SKILL.md"
        if entry.is_symlink() or not entry.is_file():
            raise ValidationError("当前 Skill 缺少普通文件 SKILL.md")
        yield entry
        for directory_name in sorted(_STANDARD_DIRECTORIES):
            directory = skill_root / directory_name
            if not directory.exists():
                continue
            if directory.is_symlink() or not directory.is_dir():
                raise ValidationError(f"Skill 标准目录无效：{directory_name}")
            pending = [directory]
            while pending:
                current = pending.pop()
                try:
                    with os.scandir(current) as iterator:
                        entries = sorted(iterator, key=lambda item: item.name)
                except OSError as exc:
                    raise ValidationError("Skill 目录扫描失败") from exc
                for item in entries:
                    path = Path(item.path)
                    if item.is_symlink():
                        raise ValidationError("Skill 不允许软链接")
                    if item.is_dir(follow_symlinks=False):
                        pending.append(path)
                    elif item.is_file(follow_symlinks=False):
                        yield path
                    else:
                        raise ValidationError("Skill 标准目录仅允许普通文件和目录")

    def _build_snapshot(self, active: str) -> SkillSnapshot:
        root = self._active_root(active)
        hashes: dict[str, str] = {}
        total_bytes = 0
        skill_entry: bytes | None = None
        for path in self._standard_files(root):
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError as exc:
                raise ValidationError("Skill 路径越界") from exc
            raw = _read_regular_file(path, max_bytes=self.max_snapshot_file_bytes)
            total_bytes += len(raw)
            if len(hashes) >= self.max_files or total_bytes > self.max_snapshot_bytes:
                raise ValidationError("Skill 快照超过文件数或总大小上限")
            hashes[relative] = hashlib.sha256(raw).hexdigest()
            if relative == "SKILL.md":
                skill_entry = raw
        if skill_entry is None:
            raise ValidationError("当前 Skill 缺少 SKILL.md")
        name, description = _metadata(skill_entry)
        digest_source = bytearray(b"skill-snapshot-v1\0")
        for path, file_hash in sorted(hashes.items()):
            digest_source.extend(path.encode("utf-8"))
            digest_source.extend(b"\0")
            digest_source.extend(file_hash.encode("ascii"))
            digest_source.extend(b"\0")
        digest = hashlib.sha256(digest_source).hexdigest()
        if self.expected_digest is not None and digest != self.expected_digest:
            raise ValidationError("Skill 完整性摘要校验失败")
        return SkillSnapshot(
            root=root,
            directory_name=active,
            name=name,
            description=description,
            digest=digest,
            file_hashes=tuple(sorted(hashes.items())),
            total_bytes=total_bytes,
        )

    def resolve(self) -> SkillSnapshot:
        with self._lock:
            if self._snapshot is None:
                self._snapshot = self._build_snapshot(self.active)
            return self._snapshot

    snapshot = resolve

    def runtime(self, *, max_file_bytes: int = 256 * 1024, max_total_bytes: int = 512 * 1024) -> "SkillRuntime":
        return SkillRuntime(self.resolve(), max_file_bytes=max_file_bytes, max_total_bytes=max_total_bytes)

    def reload(self, active: str | None = None) -> SkillSnapshot:
        replacement = self._active_name(active) if active is not None else self.active
        candidate = self._build_snapshot(replacement)
        with self._lock:
            self.active = replacement
            self._snapshot = candidate
            return candidate


class SkillRuntime:
    """Generic, read-only and quota-bound view of one immutable Skill snapshot."""

    TEXT_SUFFIXES = {
        ".md", ".html", ".htm", ".js", ".mjs", ".css", ".json", ".txt",
        ".yaml", ".yml", ".xml", ".csv", ".ts", ".tsx", ".jsx", ".svg",
    }

    def __init__(
        self,
        source: str | Path | SkillSnapshot,
        *,
        max_file_bytes: int = 256 * 1024,
        max_total_bytes: int = 512 * 1024,
    ):
        if isinstance(max_file_bytes, bool) or not isinstance(max_file_bytes, int) or max_file_bytes < 1:
            raise ValidationError("Skill 单文件读取上限无效")
        if isinstance(max_total_bytes, bool) or not isinstance(max_total_bytes, int) or max_total_bytes < 1:
            raise ValidationError("Skill 累计读取上限无效")
        snapshot = source if isinstance(source, SkillSnapshot) else ActiveSkillResolver(Path(source).parent, Path(source).name).resolve()
        self.snapshot = snapshot
        self.root = snapshot.root
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.total_bytes = 0
        self.manifest = snapshot.manifest
        self.skill_name = snapshot.name
        self.skill_description = snapshot.description
        self.skill_version = snapshot.digest

    @classmethod
    def builtin(cls) -> "SkillRuntime":
        """Compatibility helper for tests and non-configured library callers."""
        return ActiveSkillResolver.builtin().runtime()

    def clone(self) -> "SkillRuntime":
        return SkillRuntime(
            self.snapshot,
            max_file_bytes=self.max_file_bytes,
            max_total_bytes=self.max_total_bytes,
        )

    def _resolve(self, name: str) -> Path:
        relative = _validate_relative_path(name)
        path = self.root.joinpath(*relative.parts)
        current = self.root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValidationError("Skill 不允许软链接")
        try:
            resolved = path.resolve(strict=False)
        except OSError as exc:
            raise ValidationError("Skill 路径无效") from exc
        if resolved != self.root and self.root not in resolved.parents:
            raise ValidationError("Skill 路径越界")
        return path

    @staticmethod
    def _allowed(name: str) -> bool:
        return name == "SKILL.md" or any(name.startswith(f"{directory}/") for directory in _STANDARD_DIRECTORIES)

    def list_skill_files(self, *, allowed_files: frozenset[str] | None = None) -> dict:
        files = sorted(
            name for name in self.manifest
            if self._allowed(name) and (allowed_files is None or name in allowed_files)
        )
        return {
            "skill": self.skill_name,
            "description": self.skill_description,
            "version": self.skill_version,
            "digest": self.snapshot.digest,
            "files": files,
        }

    def _normalize_tool_path(self, name: str) -> str:
        if not isinstance(name, str):
            return name
        normalized = name.strip()
        while normalized.startswith("./"):
            normalized = normalized[2:]
        normalized = normalized.lstrip("/")
        for prefix in (self.skill_name, self.snapshot.directory_name):
            skill_prefix = f"{prefix}/"
            if normalized.startswith(skill_prefix):
                normalized = normalized[len(skill_prefix):]
                break
        return normalized

    def normalize_tool_path(self, name: str) -> str:
        return self._normalize_tool_path(name)

    def _whitelist_error(self, asset: bool, allowed_files: frozenset[str] | None = None) -> ValidationError:
        files = self.list_skill_files(allowed_files=allowed_files)["files"]
        if asset:
            allowed = "、".join(name for name in files if name.startswith("assets/")) or "（无）"
            return ValidationError(f"Asset 不在只读快照；合法路径：{allowed}")
        return ValidationError("Skill 文件不在只读快照；合法路径：" + "、".join(files))

    def _locked_bytes(
        self,
        name: str,
        *,
        asset: bool = False,
        allowed_files: frozenset[str] | None = None,
    ) -> tuple[Path, bytes]:
        name = self._normalize_tool_path(name)
        path = self._resolve(name)
        if (
            name not in self.manifest
            or not self._allowed(name)
            or allowed_files is not None and name not in allowed_files
        ):
            raise self._whitelist_error(asset, allowed_files)
        if asset and not name.startswith("assets/"):
            raise self._whitelist_error(asset=True, allowed_files=allowed_files)
        raw = _read_regular_file(
            path,
            max_bytes=self.max_file_bytes,
            limit_message="Skill 单文件超过读取上限",
        )
        if hashlib.sha256(raw).hexdigest() != self.manifest[name]:
            raise ValidationError(f"Skill 快照文件校验失败：{name}")
        return path, raw

    def _charge(self, size: int) -> None:
        if size > self.max_file_bytes:
            raise ValidationError("Skill 单文件超过读取上限")
        if self.total_bytes + size > self.max_total_bytes:
            raise ValidationError("Skill 累计读取超过上限")
        self.total_bytes += size

    def read_skill_file(self, name: str, *, allowed_files: frozenset[str] | None = None) -> dict:
        name = self._normalize_tool_path(name)
        path, raw = self._locked_bytes(name, allowed_files=allowed_files)
        if path.suffix.lower() not in self.TEXT_SUFFIXES:
            raise ValidationError("Skill 文件不是允许的 UTF-8 文本；二进制 Asset 请用 get_asset_info")
        self._charge(len(raw))
        try:
            content = raw.decode("utf-8")
        except UnicodeError as exc:
            self.total_bytes -= len(raw)
            raise ValidationError("Skill 文件无法按 UTF-8 读取") from exc
        return {
            "path": name,
            "content": content,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    def get_asset_info(self, name: str, *, allowed_files: frozenset[str] | None = None) -> dict:
        name = self._normalize_tool_path(name)
        _, raw = self._locked_bytes(name, asset=True, allowed_files=allowed_files)
        self._charge(len(raw))
        return {
            "path": name,
            "bytes": len(raw),
            "media_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    def read_locked_text(self, name: str) -> str:
        """Read a snapshot-verified server-owned text file without Agent quota."""
        path, raw = self._locked_bytes(name)
        if path.suffix.lower() not in self.TEXT_SUFFIXES:
            raise ValidationError("Skill 模板不是允许的文本文件")
        try:
            return raw.decode("utf-8")
        except UnicodeError as exc:
            raise ValidationError("Skill 模板无法按 UTF-8 读取") from exc

    def dispatch(self, name: str, arguments: dict, *, allowed_files: frozenset[str] | None = None) -> dict:
        if name == "list_skill_files":
            return self.list_skill_files(allowed_files=allowed_files)
        if name == "read_skill_file":
            return self.read_skill_file(arguments.get("path"), allowed_files=allowed_files)
        if name == "get_asset_info":
            return self.get_asset_info(arguments.get("path"), allowed_files=allowed_files)
        raise ValidationError("Agent 请求了未授权工具")
