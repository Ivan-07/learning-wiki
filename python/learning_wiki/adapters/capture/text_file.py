"""TextFileAdapter：Markdown / TXT 文件导入。"""

from __future__ import annotations

import re
from pathlib import Path

from learning_wiki.domain.contracts import ExtractionResult, InboxItem
from learning_wiki.storage import evidence_store

_ALLOWED_SUFFIXES = {".md", ".markdown", ".txt"}
_HEADING = re.compile(r"^#\s+(.+)$", re.MULTILINE)


class UnsupportedFileTypeError(ValueError):
    pass


class TextFileAdapter:
    input_type = "file"

    def can_handle(self, item: InboxItem) -> bool:
        return item.input_type == "file"

    def extract(self, item: InboxItem, progress=None) -> ExtractionResult:
        path = Path(item.payload_path or "")
        if not path.exists():
            raise FileNotFoundError(f"文件不存在: {path}")
        if path.suffix.lower() not in _ALLOWED_SUFFIXES:
            raise UnsupportedFileTypeError(f"不支持的文件类型: {path.suffix}（支持 Markdown/TXT）")
        if progress:
            progress("extract", 0.5)
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="replace")
        m = _HEADING.search(text)
        title = (m.group(1).strip() if m else path.stem)[:120]
        return ExtractionResult(
            original_bytes=raw,
            original_filename=f"original{path.suffix.lower()}",
            content_markdown=text,
            title=title,
            extraction_method="text_file",
            extraction_quality=1.0,
            blocks=evidence_store.split_markdown_blocks(text),
        )
