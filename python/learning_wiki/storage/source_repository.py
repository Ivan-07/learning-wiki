"""SourceRepository：唯一允许写 manifest.yaml 的组件。

写入顺序（版本存在性以 manifest 为提交点）：
1. versions/vNNNN/original.*（若有原件）
2. versions/vNNNN/content.md（已含证据块 ID）
3. versions/vNNNN/evidence.jsonl
4. manifest.yaml（原子写 —— rebuild 只认 manifest）

不变量：
- 已存在的版本条目永不修改；新版本只追加；
- 未来 schema_version（> 当前支持）的来源只读打开，拒绝写入（规格 12.3/13.3.10）；
- manifest 重写一律 round-trip，保留注释与未知字段。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from learning_wiki.adapters.executor.local_fsync import SafeFileWriter

from learning_wiki.domain.contracts import (
    EvidenceRef,
    SourceManifest,
    SourceVersionRecord,
)
from learning_wiki.storage import yaml_io
from learning_wiki.storage.vault_paths import VaultPaths

SUPPORTED_SCHEMA_VERSION = 1
VERSIONS_SUBDIR = "versions"


class SourceNotFoundError(FileNotFoundError):
    pass


class UnsupportedSchemaError(Exception):
    """来源 manifest 的 schema_version 高于当前程序支持，只读打开。"""


def _version_paths(rec: SourceVersionRecord) -> set[str]:
    known = {rec.content_path, rec.evidence_path, *rec.original_paths}
    return known


class SourceRepository:
    def __init__(self, paths: VaultPaths, writer: SafeFileWriter) -> None:
        self.paths = paths
        self.writer = writer  # SafeFileWriter

    # -- 读 -----------------------------------------------------------------

    def source_dir(self, source_id: str) -> Path:
        return self.paths.folder("sources") / source_id

    def manifest_path(self, source_id: str) -> Path:
        return self.source_dir(source_id) / "manifest.yaml"

    def load(self, source_id: str) -> SourceManifest:
        path = self.manifest_path(source_id)
        if not path.exists():
            raise SourceNotFoundError(f"来源不存在: {source_id}")
        return self._load_manifest(path)

    def load_all(self) -> list[SourceManifest]:
        sources_dir = self.paths.folder("sources")
        if not sources_dir.exists():
            return []
        result = []
        for path in sorted(sources_dir.glob("*/manifest.yaml")):
            result.append(self._load_manifest(path))
        return result

    def _load_manifest(self, path: Path) -> SourceManifest:
        data = yaml_io.load_yaml(path)
        manifest = SourceManifest.model_validate(dict(data))
        if manifest.schema_version > SUPPORTED_SCHEMA_VERSION:
            raise UnsupportedSchemaError(
                f"{path}: schema_version {manifest.schema_version} 高于当前支持的 "
                f"{SUPPORTED_SCHEMA_VERSION}，来源以只读方式打开"
            )
        return manifest

    def has_version_with_content_hash(
        self, source_id: str, content_hash: str
    ) -> SourceVersionRecord | None:
        try:
            manifest = self.load(source_id)
        except (SourceNotFoundError, UnsupportedSchemaError):
            return None
        for rec in manifest.versions:
            if rec.content_hash == content_hash:
                return rec
        return None

    # -- 写 -----------------------------------------------------------------

    def create_source(
        self,
        manifest: SourceManifest,
        content: str,
        evidence: list[EvidenceRef],
        original_bytes: bytes | None,
        original_filename: str | None,
    ) -> None:
        sdir = self.source_dir(manifest.source_id)
        if self.manifest_path(manifest.source_id).exists():
            raise FileExistsError(f"来源已存在: {manifest.source_id}")
        self._write_version_files(
            sdir, manifest.versions[-1], content, evidence, original_bytes, original_filename
        )
        yaml_io.dump_yaml(
            manifest.model_dump(mode="json"), self.manifest_path(manifest.source_id), self.writer
        )

    def append_version(
        self,
        source_id: str,
        new_rec: SourceVersionRecord,
        content: str,
        evidence: list[EvidenceRef],
        original_bytes: bytes | None,
        original_filename: str | None,
    ) -> SourceManifest:
        """追加新版本：round-trip manifest，只在 versions 列表末尾追加并更新 active。"""
        mpath = self.manifest_path(source_id)
        if not mpath.exists():
            raise SourceNotFoundError(f"来源不存在: {source_id}")
        data = yaml_io.load_yaml(mpath)
        existing_ids = [v["version_id"] for v in data.get("versions", [])]
        if new_rec.version_id in existing_ids:
            raise ValueError(f"版本已存在: {source_id}/{new_rec.version_id}")
        # 未支持的未来 schema 只读（写路径防御性再查一次）
        if int(data.get("schema_version", 1)) > SUPPORTED_SCHEMA_VERSION:
            raise UnsupportedSchemaError(f"{source_id}: 未来 schema_version，只读")
        self._write_version_files(
            self.source_dir(source_id),
            new_rec,
            content,
            evidence,
            original_bytes,
            original_filename,
        )
        # 修改 round-trip 数据（保留注释/未知键），最后原子写 manifest = 提交点
        data["versions"] = [*data.get("versions", []), new_rec.model_dump(mode="json")]
        data["active_version"] = new_rec.version_id
        yaml_io.dump_yaml(data, mpath, self.writer)
        return self.load(source_id)

    def update_value_state(self, source_id: str, state: str) -> SourceManifest:
        mpath = self.manifest_path(source_id)
        data = yaml_io.load_yaml(mpath)
        if int(data.get("schema_version", 1)) > SUPPORTED_SCHEMA_VERSION:
            raise UnsupportedSchemaError(f"{source_id}: 未来 schema_version，只读")
        data["value_state"] = state
        yaml_io.dump_yaml(data, mpath, self.writer)
        return self.load(source_id)

    # -- 内部 ---------------------------------------------------------------

    def _write_version_files(
        self,
        sdir: Path,
        rec: SourceVersionRecord,
        content: str,
        evidence: list[EvidenceRef],
        original_bytes: bytes | None,
        original_filename: str | None,
    ) -> None:
        vdir = sdir / "versions" / rec.version_id
        if original_bytes is not None:
            self.writer.write_bytes(vdir / (original_filename or "original.bin"), original_bytes)
        self.writer.write_text(vdir / "content.md", content)
        lines = "\n".join(e.model_dump_json() for e in evidence)
        self.writer.write_text(vdir / "evidence.jsonl", lines + "\n" if lines else "")
        # manifest 中记录的相对路径以 source 目录为根（规格 4.1/5.1 布局）
        rec.content_path = f"versions/{rec.version_id}/content.md"
        rec.evidence_path = f"versions/{rec.version_id}/evidence.jsonl"
        rec.original_paths = (
            [f"versions/{rec.version_id}/{original_filename or 'original.bin'}"]
            if original_bytes is not None
            else []
        )
