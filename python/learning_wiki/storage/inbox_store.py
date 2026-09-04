"""Inbox 持久化：设备级瞬时状态（.learning-wiki/inbox.jsonl）。

按规格 4.2，任务进度无长期事实源；删除 .learning-wiki/ 允许丢失
未处理的 Inbox 项，不参与重建。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from learning_wiki.domain.contracts import InboxItem

if TYPE_CHECKING:
    from learning_wiki.adapters.executor.local_fsync import SafeFileWriter


class InboxStore:
    def __init__(self, jsonl_path: Path, writer: SafeFileWriter) -> None:
        self.path = jsonl_path
        self.writer = writer

    def load_all(self) -> list[InboxItem]:
        if not self.path.exists():
            return []
        items = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                items.append(InboxItem.model_validate_json(line))
        return items

    def get(self, item_id: str) -> InboxItem:
        for item in self.load_all():
            if item.item_id == item_id:
                return item
        raise KeyError(f"Inbox 项不存在: {item_id}")

    def upsert(self, item: InboxItem) -> None:
        items = [i for i in self.load_all() if i.item_id != item.item_id]
        items.append(item)
        items.sort(key=lambda i: i.created_at)
        payload = "\n".join(i.model_dump_json() for i in items) + "\n"
        self.writer.write_text(self.path, payload)
