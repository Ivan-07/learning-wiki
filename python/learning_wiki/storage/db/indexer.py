"""派生库行写入层：CaptureService 与 RebuildService 共用的索引入口。

SQLite 是派生状态：这里只做 upsert/insert，不做任何业务判断。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from learning_wiki.domain.contracts import (
    ApplicationChallenge,
    ClaimInfo,
    EvidenceRef,
    Goal,
    InboxItem,
    LearningObject,
    Misconception,
    SourceManifest,
    SourceVersionRecord,
)


class DbIndexer:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def wipe_all(self) -> None:
        """rebuild 前清空全部派生数据。"""
        tables = [
            "fts_sources",
            "fts_notes",
            "fts_notes_map",
            "fts_exact",
            "claim_evidence",
            "claims",
            "note_links",
            "wiki_notes",
            "evidence",
            "source_versions",
            "sources",
            "learning_events",
            "learning_objects",
            "review_queue",
            "learning_goals",
            "goal_capabilities",
            "capability_evidence",
            "misconceptions",
            "misconception_occurrences",
            "application_challenges",
            "knowledge_snapshot_refs",
            "proposals",
            "operations",
            "integrity_alerts",
            "index_state",
            "inbox_items",
        ]
        for t in tables:
            self.conn.execute(f"DELETE FROM {t}")

    # -- 来源 ---------------------------------------------------------------

    def upsert_source(self, manifest: SourceManifest) -> None:
        self.conn.execute(
            "INSERT INTO sources(source_id, source_type, title, author, canonical_url,"
            " published_at, value_state, active_version_id, manifest_path,"
            " first_captured_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(source_id) DO UPDATE SET title=excluded.title,"
            " author=excluded.author, canonical_url=excluded.canonical_url,"
            " value_state=excluded.value_state,"
            " active_version_id=excluded.active_version_id,"
            " updated_at=excluded.updated_at",
            (
                manifest.source_id,
                manifest.source_type,
                manifest.title,
                manifest.author,
                manifest.canonical_url,
                manifest.published_at,
                manifest.value_state,
                manifest.active_version,
                manifest.source_id,  # manifest_path 占位，由调用方以 relpath 更新
                manifest.captured_at,
                manifest.versions[-1].created_at if manifest.versions else manifest.captured_at,
            ),
        )

    def upsert_version(self, source_id: str, rec: SourceVersionRecord) -> int:
        cur = self.conn.execute(
            "INSERT INTO source_versions(source_id, version_id, version_seq,"
            " created_at, extraction_method, original_hash, content_hash,"
            " content_path, evidence_path, extraction_quality)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(source_id, version_id) DO UPDATE SET"
            " content_hash=excluded.content_hash, extraction_quality=excluded.extraction_quality"
            " RETURNING id",
            (
                source_id,
                rec.version_id,
                int(rec.version_id[1:]),
                rec.created_at,
                rec.extraction_method,
                rec.original_hash,
                rec.content_hash,
                rec.content_path,
                rec.evidence_path,
                rec.extraction_quality,
            ),
        )
        return int(cur.fetchone()[0])

    def insert_evidence(self, ev: EvidenceRef) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO evidence(evidence_id, source_id, version_id,"
            " content_hash, span_hash, anchor_type, block_id, text,"
            " extraction_confidence, trusted)"
            " VALUES (?,?,?,?,?,?,?,?,?,1)",
            (
                ev.evidence_id,
                ev.source_id,
                ev.version_id,
                ev.content_hash,
                ev.span_hash,
                ev.anchor_type,
                ev.anchor_start,
                ev.text,
                ev.extraction_confidence,
            ),
        )

    # -- Wiki ---------------------------------------------------------------

    def upsert_note(
        self,
        note_id: str,
        path: str,
        title: str,
        note_type: str,
        status: str,
        file_hash: str,
        mtime: float,
        updated_at: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO wiki_notes(note_id, path, title, note_type, status,"
            " file_hash, mtime, updated_at) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(note_id) DO UPDATE SET path=excluded.path,"
            " title=excluded.title, note_type=excluded.note_type,"
            " status=excluded.status, file_hash=excluded.file_hash,"
            " mtime=excluded.mtime, updated_at=excluded.updated_at",
            (note_id, path, title, note_type, status, file_hash, mtime, updated_at),
        )

    # -- Claim（反向引用索引的数据源） ----------------------------------------

    def upsert_claim(self, claim: ClaimInfo) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO claims(claim_block_id, note_id, claim_status,"
            " valid_at, review_after, raw_text) VALUES (?,?,?,?,?,?)",
            (
                claim.claim_block_id,
                claim.note_id,
                claim.claim_status,
                claim.valid_at,
                claim.review_after,
                claim.text,
            ),
        )

    def insert_claim_evidence(
        self, claim_block_id: str, evidence_id: str, source_id: str, version_id: str
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO claim_evidence(claim_block_id, evidence_id,"
            " source_id, version_id) VALUES (?,?,?,?)",
            (claim_block_id, evidence_id, source_id, version_id),
        )

    # -- Inbox --------------------------------------------------------------

    def upsert_inbox_item(self, item: InboxItem) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO inbox_items(item_id, state, input_type,"
            " payload_path, value_state, source_id, version_id, error,"
            " created_at, processed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                item.item_id,
                item.state,
                item.input_type,
                item.payload_path,
                item.value_state,
                item.source_id,
                item.version_id,
                item.error,
                item.created_at,
                item.processed_at,
            ),
        )

    # -- 学习 ---------------------------------------------------------------

    def upsert_learning_object(self, lo: LearningObject, path: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO learning_objects(learning_object_id, path,"
            " title, knowledge_type, activity_type, status, importance,"
            " goal_id, capability_id, knowledge_snapshot_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                lo.learning_object_id,
                path,
                lo.title,
                lo.knowledge_type,
                lo.activity_type,
                lo.status,
                lo.importance,
                lo.goal_id,
                lo.capability_id,
                json.dumps(json.loads(lo.knowledge_snapshot.model_dump_json()), ensure_ascii=False)
                if lo.knowledge_snapshot
                else None,
            ),
        )

    def insert_learning_event(
        self,
        event_id: str,
        event_type: str,
        session_id: str | None,
        learning_object_id: str | None,
        occurred_at: str,
        record_path: str,
        payload_hash: str,
    ) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO learning_events(event_id, event_type, session_id,"
            " learning_object_id, occurred_at, record_path, payload_hash)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                event_id,
                event_type,
                session_id,
                learning_object_id,
                occurred_at,
                record_path,
                payload_hash,
            ),
        )

    # -- 学习机制升级：目标 / 能力证据 / 误解 / 应用挑战 ----------------------

    def upsert_goal(self, goal: Goal, path: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO learning_goals(goal_id, title, status, importance,"
            " created_at, target_date, path, capability_count)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                goal.goal_id,
                goal.title,
                goal.status,
                goal.importance,
                goal.created_at,
                goal.target_date,
                path,
                len(goal.capabilities),
            ),
        )
        for cap in goal.capabilities:
            # 先 UPDATE 保留派生的 current_level，未命中（新能力）再 INSERT
            cur = self.conn.execute(
                "UPDATE goal_capabilities SET behavior=?, required_level=?"
                " WHERE goal_id=? AND capability_id=?",
                (cap.behavior, cap.required_level, goal.goal_id, cap.capability_id),
            )
            if cur.rowcount == 0:
                self.conn.execute(
                    "INSERT INTO goal_capabilities(goal_id, capability_id, behavior,"
                    " required_level, current_level) VALUES (?,?,?,?,'unseen')",
                    (goal.goal_id, cap.capability_id, cap.behavior, cap.required_level),
                )

    def update_goal_capability_level(self, goal_id: str, capability_id: str, level: str) -> None:
        self.conn.execute(
            "UPDATE goal_capabilities SET current_level=? WHERE goal_id=? AND capability_id=?",
            (level, goal_id, capability_id),
        )

    def insert_capability_evidence(
        self,
        goal_id: str,
        capability_id: str,
        level: str,
        event_id: str,
        occurred_at: str,
        activity_type: str | None,
        hint_free: bool,
        closed_book: bool,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO capability_evidence(goal_id, capability_id, level,"
            " event_id, occurred_at, activity_type, hint_free, closed_book)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                goal_id,
                capability_id,
                level,
                event_id,
                occurred_at,
                activity_type,
                int(hint_free),
                int(closed_book),
            ),
        )

    def upsert_misconception(self, mis: Misconception, path: str, priority: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO misconceptions(misconception_id, status, error_type,"
            " user_belief, correction, created_at, occurrences, max_confidence_when_wrong,"
            " priority, path)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                mis.misconception_id,
                mis.status,
                mis.statement.error_type,
                mis.statement.user_belief,
                mis.statement.correction,
                mis.created_at,
                mis.observations.occurrences,
                mis.observations.max_confidence_when_wrong,
                priority,
                path,
            ),
        )

    def insert_misconception_occurrence(
        self,
        misconception_id: str,
        event_id: str,
        context: str | None,
        occurred_at: str,
        confidence: int | None,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO misconception_occurrences(misconception_id, event_id,"
            " context, occurred_at, confidence) VALUES (?,?,?,?,?)",
            (misconception_id, event_id, context, occurred_at, confidence),
        )

    def upsert_challenge(self, ch: ApplicationChallenge, path: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO application_challenges(challenge_id, goal_id,"
            " capability_id, type, status, created_at, path)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                ch.challenge_id,
                ch.goal_id,
                ch.capability_id,
                ch.type,
                ch.status,
                ch.created_at,
                path,
            ),
        )

    def insert_snapshot_ref(
        self,
        learning_object_id: str,
        note_id: str,
        note_hash: str,
        evidence_id: str | None = None,
        source_id: str | None = None,
        version_id: str | None = None,
        span_hash: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO knowledge_snapshot_refs(learning_object_id, note_id,"
            " note_hash, evidence_id, source_id, version_id, span_hash)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                learning_object_id,
                note_id,
                note_hash,
                evidence_id or "",
                source_id,
                version_id,
                span_hash,
            ),
        )

    def update_learning_object_status(self, learning_object_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE learning_objects SET status=? WHERE learning_object_id=?",
            (status, learning_object_id),
        )

    # -- 提案 / 操作 / 告警 ---------------------------------------------------

    def upsert_proposal(
        self,
        proposal_id: str,
        status: str,
        path: str,
        risk: str,
        created_by: str,
        created_at: str,
        operation_count: int,
        validation: dict[str, Any] | None,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO proposals(proposal_id, status, path, risk,"
            " created_by, created_at, operation_count, validation_json)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                proposal_id,
                status,
                path,
                risk,
                created_by,
                created_at,
                operation_count,
                json.dumps(validation, ensure_ascii=False) if validation else None,
            ),
        )

    def insert_operation(
        self,
        operation_id: str,
        kind: str,
        state: str,
        started_at: str,
        proposal_id: str | None = None,
        staging_dir: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO operations(operation_id, proposal_id, kind,"
            " state, started_at, staging_dir, detail_json)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                operation_id,
                proposal_id,
                kind,
                state,
                started_at,
                staging_dir,
                json.dumps(detail, ensure_ascii=False) if detail else None,
            ),
        )

    def finish_operation(self, operation_id: str, state: str, finished_at: str) -> None:
        self.conn.execute(
            "UPDATE operations SET state=?, finished_at=? WHERE operation_id=?",
            (state, finished_at, operation_id),
        )

    def insert_alert(
        self,
        alert_id: str,
        kind: str,
        subject: str,
        detail: dict[str, Any],
        created_at: str,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO integrity_alerts(alert_id, kind, subject,"
            " detail_json, created_at) VALUES (?,?,?,?,?)",
            (alert_id, kind, subject, json.dumps(detail, ensure_ascii=False), created_at),
        )

    # -- 索引状态 -----------------------------------------------------------

    def record_index_state(
        self, path: str, file_hash: str, mtime: float, size: int, indexed_at: str
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO index_state(path, file_hash, mtime, size, indexed_at)"
            " VALUES (?,?,?,?,?)",
            (path, file_hash, mtime, size, indexed_at),
        )

    def get_index_state(self, path: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM index_state WHERE path = ?", (path,)).fetchone()
