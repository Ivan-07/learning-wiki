"""知识快照与再验证（规格《学习机制升级思路》§6.5）。

- LearningObject 绑定创建时的知识版本（note_hash + Evidence 四元组）；
- 知识更新后：历史 Attempt 仍引用旧快照（不改写），LearningObject 标记
  needs_review，受影响目标进入 needs_revalidation；
- 用户重新验证当前版本后恢复。
"""

from __future__ import annotations

import sqlite3

from learning_wiki.domain import hashing, ids
from learning_wiki.domain.contracts import (
    KnowledgeSnapshot,
    KnowledgeSnapshotEvidence,
    LearningEvent,
    LearningObject,
)
from learning_wiki.learning.store import EventLog, LearningObjectRepository


class SnapshotError(Exception):
    """快照构建 / 再验证错误。"""


def build_snapshot(
    conn: sqlite3.Connection,
    note_id: str,
    evidence_ids: list[str] | None = None,
) -> KnowledgeSnapshot:
    """从派生库读取笔记当前哈希与证据四元组，构建知识快照。"""
    row = conn.execute(
        "SELECT file_hash, path FROM wiki_notes WHERE note_id = ?", (note_id,)
    ).fetchone()
    if row is None:
        raise SnapshotError(f"Wiki 笔记不存在: {note_id}（先创建/索引笔记再绑定）")
    snap = KnowledgeSnapshot(note_id=note_id, note_hash=row["file_hash"])
    for eid in evidence_ids or []:
        ev = conn.execute(
            "SELECT evidence_id, source_id, version_id, span_hash FROM evidence"
            " WHERE evidence_id = ?",
            (eid,),
        ).fetchone()
        if ev is None:
            raise SnapshotError(f"Evidence 不存在: {eid}")
        snap.evidence.append(
            KnowledgeSnapshotEvidence(
                evidence_id=ev["evidence_id"],
                source_id=ev["source_id"],
                version_id=ev["version_id"],
                span_hash=ev["span_hash"],
            )
        )
    return snap


class SnapshotService:
    def __init__(
        self,
        objects: LearningObjectRepository,
        events: EventLog,
        clock,
        conn: sqlite3.Connection,
        goals=None,
    ) -> None:
        self.objects = objects
        self.events = events
        self.clock = clock
        self.conn = conn
        self.goals = goals  # GoalService，惰性注入避免环

    def revalidate(self) -> dict:
        """比对当前笔记哈希与快照，标记 needs_review / needs_revalidation。

        历史回答不可修改：只追加 knowledge_revalidation_required 事件。
        """
        reviewed = 0
        stale: list[str] = []
        for obj in self.objects.list():
            snap = obj.knowledge_snapshot
            if snap is None:
                continue
            reviewed += 1
            current = self._current_hash(snap.note_id)
            if current is None or current != snap.note_hash:
                stale.append(obj.learning_object_id)
                if obj.status != "needs_review":
                    self.objects.set_status(obj.learning_object_id, "needs_review")
                if obj.goal_id and self.goals is not None:
                    goal = self.goals.load(obj.goal_id)
                    if goal.status not in ("draft", "abandoned", "needs_revalidation"):
                        self.goals.mark_needs_revalidation(
                            obj.goal_id,
                            reason=f"知识快照过期: {snap.note_id}",
                        )
                    else:
                        self.events.append(
                            LearningEvent(
                                event_id=ids.new_event_id(),
                                event_type="knowledge_revalidation_required",
                                goal_id=obj.goal_id,
                                learning_object_id=obj.learning_object_id,
                                occurred_at=self.clock.now_iso(),
                                schedule_reason=f"知识快照过期: {snap.note_id}",
                            )
                        )
        return {"reviewed": reviewed, "stale_objects": stale}

    def mark_reviewed(self, learning_object_id: str) -> LearningObject:
        """用户重新验证当前版本后，恢复对象为 active。"""
        obj = self.objects.load(learning_object_id)
        if obj.status != "needs_review":
            raise SnapshotError(f"对象 {learning_object_id} 不在 needs_review 状态")
        snap = obj.knowledge_snapshot
        if snap is not None:
            current = self._current_hash(snap.note_id)
            if current is not None:
                snap.note_hash = current
        self.objects.set_status(learning_object_id, "active")
        return self.objects.load(learning_object_id)

    def _current_hash(self, note_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT file_hash FROM wiki_notes WHERE note_id = ?", (note_id,)
        ).fetchone()
        return row["file_hash"] if row else None


def hash_note_content(content: str) -> str:
    """对笔记内容计算哈希（无派生库时由调用方使用）。"""
    return hashing.hash_text(content)
