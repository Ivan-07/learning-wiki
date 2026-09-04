"""CaptureService：捕获用例编排（规格 6.1）。

规则：
- ``add_to_inbox`` 不触网、不读文件正文（<300ms）；
- 重复检测：同来源（web 按 canonical_url）同 content_hash 复用现有版本；
  相同内容不同上下文不合并（规格 5.1）；
- 版本不可变：正文变化 / 重提取 / 人工纠正都创建新版本；
- 写入经 SourceRepository（manifest 为提交点）+ 派生索引。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from learning_wiki.adapters.capture.plain_text import PlainTextAdapter
from learning_wiki.adapters.capture.text_file import TextFileAdapter
from learning_wiki.adapters.capture.web import GenericWebAdapter
from learning_wiki.adapters.executor.local_fsync import SafeFileWriter
from learning_wiki.domain import hashing, ids
from learning_wiki.domain.clock import SystemClock
from learning_wiki.domain.contracts import (
    ExtractionResult,
    InboxItem,
    SourceManifest,
    SourceVersionRecord,
    VaultConfig,
)
from learning_wiki.retrieval.fts import FtsIndex
from learning_wiki.storage.db.indexer import DbIndexer
from learning_wiki.storage.evidence_store import EvidenceService
from learning_wiki.storage.inbox_store import InboxStore
from learning_wiki.storage.locks import vault_write_lock
from learning_wiki.storage.operations_log import OperationsLog
from learning_wiki.storage.source_repository import SourceRepository
from learning_wiki.storage.vault_paths import VaultPaths

ProgressFn = Callable[[str, float], None]


class InboxStateError(Exception):
    pass


@dataclass
class CaptureResult:
    item_id: str
    source_id: str
    version_id: str
    title: str
    duplicate: bool = False
    evidence_count: int = 0
    created_new_source: bool = True


class CaptureService:
    def __init__(
        self,
        paths: VaultPaths,
        config: VaultConfig,
        repo: SourceRepository,
        evidence: EvidenceService,
        indexer: DbIndexer,
        fts: FtsIndex,
        conn: sqlite3.Connection,
        clock: SystemClock,
        writer: SafeFileWriter,
        adapters: dict[str, PlainTextAdapter | TextFileAdapter | GenericWebAdapter] | None = None,
    ) -> None:
        self.paths = paths
        self.config = config
        self.repo = repo
        self.evidence = evidence
        self.indexer = indexer
        self.fts = fts
        self.conn = conn
        self.clock = clock
        self.inbox = InboxStore(paths.dot_dir() / "inbox.jsonl", writer)
        self.operations = OperationsLog(paths, writer, clock)
        self.adapters = adapters or {
            "text": PlainTextAdapter(),
            "file": TextFileAdapter(),
            "url": GenericWebAdapter(store_html=config.capture.store_original_web_html),
        }

    # -- Inbox --------------------------------------------------------------

    def add_to_inbox(
        self,
        kind: str,
        *,
        payload: str | None = None,
        payload_path: str | None = None,
        why: str | None = None,
    ) -> InboxItem:
        item = InboxItem(
            item_id=ids.new_inbox_id(),
            input_type=kind,  # type: ignore[arg-type]
            payload=payload,
            payload_path=payload_path,
            why_saved=why,
            created_at=self.clock.now_iso(),
        )
        self.inbox.upsert(item)
        self.indexer.upsert_inbox_item(item)
        return item

    def list_inbox(self, state: str | None = None) -> list[InboxItem]:
        items = self.inbox.load_all()
        return [i for i in items if state is None or i.state == state]

    def classify(self, item_id: str, value_state: str) -> InboxItem:
        item = self.inbox.get(item_id)
        item.value_state = value_state  # type: ignore[assignment]  # CLI 已校验取值
        if value_state == "discard":
            item.state = "discarded"
        self.inbox.upsert(item)
        self.indexer.upsert_inbox_item(item)
        if item.source_id and value_state in ("reference", "learn", "apply", "discard"):
            state_map = {"discard": "archived"}
            self.repo.update_value_state(item.source_id, state_map.get(value_state, value_state))
            manifest = self.repo.load(item.source_id)
            self.indexer.upsert_source(manifest)
        return item

    # -- 处理 -----------------------------------------------------------------

    def process(self, item_id: str, progress: ProgressFn | None = None) -> CaptureResult:
        with vault_write_lock(self.paths.dot_dir()):
            return self._process_locked(item_id, progress)

    def _process_locked(self, item_id: str, progress: ProgressFn | None) -> CaptureResult:
        item = self.inbox.get(item_id)
        if item.state not in ("new", "failed"):
            raise InboxStateError(f"Inbox 项状态为 {item.state}，不能处理")

        adapter = self.adapters.get(item.input_type)
        if adapter is None:
            raise InboxStateError(f"无适配器: {item.input_type}")

        try:
            extraction = adapter.extract(item, progress)
        except Exception as exc:  # 失败原因记入 Inbox 后原样上抛
            item.state = "failed"
            item.error = str(exc)
            self.inbox.upsert(item)
            self.indexer.upsert_inbox_item(item)
            raise

        content_hash = hashing.hash_text(extraction.content_markdown)
        existing_source = self._find_existing_source(extraction, content_hash)

        # —— 重复：同来源同内容哈希 → 复用现有版本，不写任何文件 ——
        manifest: SourceManifest | None = None
        if existing_source is not None:
            source_id, manifest, dup_rec = existing_source
            if dup_rec is not None:
                item.state = "captured"
                item.source_id = source_id
                item.version_id = dup_rec.version_id
                item.error = None
                item.processed_at = self.clock.now_iso()
                self.inbox.upsert(item)
                self.indexer.upsert_inbox_item(item)
                return CaptureResult(
                    item_id=item_id,
                    source_id=source_id,
                    version_id=dup_rec.version_id,
                    title=manifest.title,
                    duplicate=True,
                    created_new_source=False,
                )

        # —— 新版本或新来源 ——
        if existing_source is not None:
            source_id = existing_source[0]
            version_id = ids.next_version_id(
                [v.version_id for v in manifest.versions]  # type: ignore[union-attr]
            )
            created_new_source = False
        else:
            source_id = ids.new_source_id()
            version_id = "v0001"
            created_new_source = True

        content, evidence = self.evidence.build(
            source_id, version_id, content_hash, extraction.content_markdown
        )
        rec = SourceVersionRecord(
            version_id=version_id,
            created_at=self.clock.now_iso(),
            extraction_method=extraction.extraction_method,
            original_hash=(
                hashing.hash_bytes(extraction.original_bytes)
                if extraction.original_bytes is not None
                else None
            ),
            content_hash=content_hash,
            extraction_quality=extraction.extraction_quality,
        )

        if progress:
            progress("write", 0.7)
        if manifest is None:
            manifest = SourceManifest(
                source_id=source_id,
                source_type=self._source_type(item.input_type),
                title=extraction.title or "未命名来源",
                author=extraction.author,
                canonical_url=extraction.canonical_url,
                published_at=extraction.published_at,
                captured_at=self.clock.now_iso(),
                value_state=self.config.capture.default_value_state,
                active_version=version_id,
                versions=[rec],
            )
            self.repo.create_source(
                manifest,
                content,
                evidence,
                extraction.original_bytes,
                extraction.original_filename,
            )
        else:
            manifest = self.repo.append_version(
                source_id,
                rec,
                content,
                evidence,
                extraction.original_bytes,
                extraction.original_filename,
            )

        if progress:
            progress("index", 0.85)
        self._index_version(manifest, rec, content)

        item.state = "captured"
        item.source_id = source_id
        item.version_id = version_id
        item.error = None
        item.processed_at = self.clock.now_iso()
        self.inbox.upsert(item)
        self.indexer.upsert_inbox_item(item)

        self.operations.append(
            "capture",
            "applied",
            detail={
                "source_id": source_id,
                "version_id": version_id,
                "extraction_method": extraction.extraction_method,
                "duplicate": False,
            },
        )
        return CaptureResult(
            item_id=item_id,
            source_id=source_id,
            version_id=version_id,
            title=manifest.title,
            evidence_count=len(evidence),
            created_new_source=created_new_source,
        )

    # -- 内部 ---------------------------------------------------------------

    @staticmethod
    def _source_type(input_type: str) -> Literal["text", "file", "web"]:
        return {"text": "text", "file": "file", "url": "web"}[input_type]  # type: ignore[return-value]

    def _find_existing_source(
        self, extraction: ExtractionResult, content_hash: str
    ) -> tuple[str, SourceManifest, SourceVersionRecord | None] | None:
        """返回 (source_id, manifest, 同内容哈希的已有版本或 None)。"""
        if extraction.canonical_url:
            row = self.conn.execute(
                "SELECT source_id FROM sources WHERE canonical_url = ? AND source_type = 'web'",
                (extraction.canonical_url,),
            ).fetchone()
            if row is not None:
                manifest = self.repo.load(row[0])
                dup = next(
                    (v for v in manifest.versions if v.content_hash == content_hash),
                    None,
                )
                return row[0], manifest, dup
            return None
        # 纯文本/文件：同类型来源 + 相同内容哈希 → 复用（重复导入不产生新版本）
        row = self.conn.execute(
            "SELECT sv.source_id FROM source_versions sv"
            " JOIN sources s ON s.source_id = sv.source_id"
            " WHERE sv.content_hash = ? AND s.source_type = ? LIMIT 1",
            (content_hash, self._source_type_by_extraction(extraction)),
        ).fetchone()
        if row is None:
            return None
        manifest = self.repo.load(row[0])
        dup = next((v for v in manifest.versions if v.content_hash == content_hash), None)
        return row[0], manifest, dup

    @staticmethod
    def _source_type_by_extraction(extraction: ExtractionResult) -> str:
        return {
            "plain_text": "text",
            "text_file": "file",
            "trafilatura": "web",
        }.get(extraction.extraction_method, "text")

    def _index_version(
        self, manifest: SourceManifest, rec: SourceVersionRecord, content: str
    ) -> None:
        self.indexer.upsert_source(manifest)
        self.conn.execute(
            "UPDATE sources SET manifest_path = ? WHERE source_id = ?",
            (self.paths.relpath(self.repo.manifest_path(manifest.source_id)), manifest.source_id),
        )
        row_id = self.indexer.upsert_version(manifest.source_id, rec)
        self.fts.index_source_version(row_id, manifest.title, content)
        for ev in self.evidence.load(self.repo.source_dir(manifest.source_id) / rec.evidence_path):
            self.indexer.insert_evidence(ev)
