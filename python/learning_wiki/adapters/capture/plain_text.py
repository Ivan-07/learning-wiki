"""PlainTextAdapter：粘贴文字 → 段落级证据锚点。"""

from __future__ import annotations

from learning_wiki.domain.contracts import ExtractionResult, InboxItem
from learning_wiki.storage import evidence_store


class PlainTextAdapter:
    input_type = "text"

    def can_handle(self, item: InboxItem) -> bool:
        return item.input_type == "text"

    def extract(self, item: InboxItem, progress=None) -> ExtractionResult:
        if progress:
            progress("extract", 0.5)
        text = (item.payload or "").strip()
        if not text:
            raise ValueError("粘贴内容为空")
        title = text.split("\n", 1)[0][:80]
        return ExtractionResult(
            original_bytes=text.encode("utf-8"),
            original_filename="original.txt",
            content_markdown=text,
            title=title,
            extraction_method="plain_text",
            extraction_quality=1.0,
            blocks=evidence_store.split_markdown_blocks(text),
        )
