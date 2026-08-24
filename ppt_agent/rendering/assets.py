from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping
from urllib.parse import unquote, urlparse

from ..generation.contracts import ResourceRecord
from ..generation.errors import AssetResolutionError, ErrorContext


MEDIA_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "font/woff2": ".woff2",
    "font/woff": ".woff",
}


@dataclass(frozen=True)
class ResolvedAsset:
    resource_id: str
    media_type: str
    content_hash: str
    offline_path: str
    content: bytes


class AssetResolver:
    """Resolve a closed, hash-verified local resource manifest."""

    def __init__(self, manifest: Iterable[ResourceRecord], root: str | Path):
        self.root = Path(root).resolve()
        records = tuple(manifest)
        self.manifest = {item.resource_id: item for item in records}
        if len(self.manifest) != len(records):
            raise AssetResolutionError("资源清单包含重复 ID")

    def resolve(self, resource_ids: Iterable[str]) -> dict[str, ResolvedAsset]:
        ordered = tuple(dict.fromkeys(resource_ids))
        unknown = sorted(set(ordered) - set(self.manifest))
        if unknown:
            raise AssetResolutionError("资源引用不存在", context=ErrorContext(field_path="asset_refs"))
        return {resource_id: self._resolve_one(self.manifest[resource_id]) for resource_id in ordered}

    def _resolve_one(self, record: ResourceRecord) -> ResolvedAsset:
        content = self._read(record)
        actual = hashlib.sha256(content).hexdigest()
        if actual != record.content_hash:
            raise AssetResolutionError("资源内容哈希不一致", context=ErrorContext(field_path=f"resource_manifest.{record.resource_id}"))
        suffix = MEDIA_SUFFIXES.get(record.media_type) or mimetypes.guess_extension(record.media_type) or ".bin"
        offline_path = f"assets/{record.content_hash}{suffix}"
        return ResolvedAsset(record.resource_id, record.media_type, record.content_hash, offline_path, content)

    def _read(self, record: ResourceRecord) -> bytes:
        uri = record.uri
        if uri.startswith("data:"):
            header, separator, payload = uri.partition(",")
            if not separator or ";base64" not in header:
                raise AssetResolutionError("data 资源必须使用 base64", context=ErrorContext(field_path=f"resource_manifest.{record.resource_id}.uri"))
            try:
                return base64.b64decode(payload, validate=True)
            except ValueError as exc:
                raise AssetResolutionError("data 资源编码无效", context=ErrorContext(field_path=f"resource_manifest.{record.resource_id}.uri")) from exc
        parsed = urlparse(uri)
        if parsed.scheme not in {"", "file"} or parsed.netloc not in {"", "localhost"}:
            raise AssetResolutionError("资源必须来自本地或内嵌清单", context=ErrorContext(field_path=f"resource_manifest.{record.resource_id}.uri"))
        raw = Path(unquote(parsed.path if parsed.scheme == "file" else uri))
        path = raw.resolve() if raw.is_absolute() else (self.root / raw).resolve()
        if path != self.root and self.root not in path.parents:
            raise AssetResolutionError("资源路径越界", context=ErrorContext(field_path=f"resource_manifest.{record.resource_id}.uri"))
        if not path.is_file() or path.is_symlink():
            raise AssetResolutionError("资源文件不存在或类型不安全", context=ErrorContext(field_path=f"resource_manifest.{record.resource_id}.uri"))
        return path.read_bytes()

    @staticmethod
    def publish(assets: Mapping[str, ResolvedAsset], target_root: str | Path) -> list[str]:
        """Atomically materialize a verified offline asset tree."""
        target = Path(target_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
        try:
            for asset in assets.values():
                path = staging / asset.offline_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(asset.content)
                if hashlib.sha256(path.read_bytes()).hexdigest() != asset.content_hash:
                    raise AssetResolutionError("离线资源写入校验失败")
            if target.exists():
                existing = {str(path.relative_to(target)): hashlib.sha256(path.read_bytes()).hexdigest() for path in target.rglob("*") if path.is_file()}
                expected = {asset.offline_path: asset.content_hash for asset in assets.values()}
                if existing != expected:
                    raise AssetResolutionError("离线资源目标不可覆盖")
                return sorted(existing)
            os.replace(staging, target)
            return sorted(asset.offline_path for asset in assets.values())
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
