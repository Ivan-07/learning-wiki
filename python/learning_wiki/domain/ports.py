"""领域端口（Protocol 接口）。

依赖方向（mypy 强制）：domain 不依赖任何其他包；application 只依赖 domain；
storage/retrieval/proposals/adapters 实现 ports，被 application 组合。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from learning_wiki.domain.contracts import (
    EvidenceRef,
    ExtractionResult,
    InboxItem,
    RawBlock,
    SourceManifest,
    SourceVersionRecord,
)

# 进度回调：stage 名 + 0..1 进度
ProgressFn = Callable[[str, float], None]


class Clock(Protocol):
    def now_iso(self) -> str: ...


@runtime_checkable
class CaptureAdapter(Protocol):
    """输入适配器：纯文本 / Markdown/TXT 文件 / 公开网页。"""

    input_type: str

    def can_handle(self, item: InboxItem) -> bool: ...

    def extract(self, item: InboxItem, progress: ProgressFn | None = None) -> ExtractionResult: ...


@runtime_checkable
class SourceRepository(Protocol):
    def load(self, source_id: str) -> SourceManifest: ...

    def load_all(self) -> list[SourceManifest]: ...

    def create_source(
        self,
        manifest: SourceManifest,
        content: str,
        evidence: list[EvidenceRef],
        original_bytes: bytes | None,
        original_filename: str | None,
    ) -> None: ...

    def append_version(
        self,
        source_id: str,
        version: SourceVersionRecord,
        manifest: SourceManifest,
        content: str,
        evidence: list[EvidenceRef],
        original_bytes: bytes | None,
        original_filename: str | None,
    ) -> None: ...

    def find_by_content_hash(
        self, content_hash: str, source_type: str | None = None
    ) -> list[tuple[str, str]]: ...


@runtime_checkable
class EvidenceStore(Protocol):
    def load_version_evidence(self, source_id: str, version_id: str) -> list[EvidenceRef]: ...

    def build_blocks(self, blocks: list[RawBlock]) -> list[tuple[str, str]]:
        """(块文本, 追加的块 ID 行) 列表 —— 由具体实现提供。"""
        ...
