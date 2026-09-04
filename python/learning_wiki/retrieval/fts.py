"""FTS5 索引读写。

- fts_sources / fts_exact：rowid == source_versions.id（变更时 delete+insert）
- fts_notes：经 fts_notes_map 映射到 note_id
- 全部为派生状态，可由 rebuild 重建
"""

from __future__ import annotations

import sqlite3

from learning_wiki.retrieval import segmenter


class FtsIndex:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -- 写入 ---------------------------------------------------------------

    def index_source_version(self, version_row_id: int, title: str, body: str) -> None:
        self.remove_source_version(version_row_id)
        self.conn.execute(
            "INSERT INTO fts_sources(rowid, title_seg, body_seg) VALUES (?,?,?)",
            (version_row_id, segmenter.segment(title), segmenter.segment(body)),
        )
        self.conn.execute(
            "INSERT INTO fts_exact(rowid, body) VALUES (?,?)",
            (version_row_id, body),
        )

    def remove_source_version(self, version_row_id: int) -> None:
        self.conn.execute("DELETE FROM fts_sources WHERE rowid = ?", (version_row_id,))
        self.conn.execute("DELETE FROM fts_exact WHERE rowid = ?", (version_row_id,))

    def index_note(self, note_id: str, title: str, body: str) -> None:
        self.remove_note(note_id)
        cur = self.conn.execute(
            "INSERT INTO fts_notes(title_seg, body_seg) VALUES (?,?)",
            (segmenter.segment(title), segmenter.segment(body)),
        )
        self.conn.execute(
            "INSERT INTO fts_notes_map(fts_rowid, note_id) VALUES (?,?)",
            (cur.lastrowid, note_id),
        )

    def remove_note(self, note_id: str) -> None:
        row = self.conn.execute(
            "SELECT fts_rowid FROM fts_notes_map WHERE note_id = ?", (note_id,)
        ).fetchone()
        if row:
            self.conn.execute("DELETE FROM fts_notes WHERE rowid = ?", (row[0],))
            self.conn.execute("DELETE FROM fts_notes_map WHERE note_id = ?", (note_id,))

    # -- 查询 ---------------------------------------------------------------

    def query_sources(self, query: str, limit: int = 20) -> list[sqlite3.Row]:
        """按 bm25 排序返回 (rowid, rank)；query 为原始文本（内部分词）。"""
        match = segmenter.fts_query(query)
        if not match:
            return []
        return self.conn.execute(
            "SELECT rowid, rank, bm25(fts_sources) AS score "
            "FROM fts_sources WHERE fts_sources MATCH ? "
            "ORDER BY rank LIMIT ?",
            (match, limit),
        ).fetchall()

    def query_notes(self, query: str, limit: int = 20) -> list[sqlite3.Row]:
        match = segmenter.fts_query(query)
        if not match:
            return []
        return self.conn.execute(
            "SELECT m.note_id, f.rank FROM fts_notes f "
            "JOIN fts_notes_map m ON m.fts_rowid = f.rowid "
            "WHERE fts_notes MATCH ? ORDER BY f.rank LIMIT ?",
            (match, limit),
        ).fetchall()

    def query_exact_substring(self, text: str, limit: int = 20) -> list[int]:
        """逐字精确子串匹配（≥3 字符走 trigram；否则退化全表 LIKE 不可行，
        返回空由调用方回退证据文本扫描）。"""
        if len(text) < 3:
            return []
        rows = self.conn.execute(
            "SELECT rowid FROM fts_exact WHERE fts_exact MATCH ? LIMIT ?",
            (f'"{text}"', limit),
        ).fetchall()
        return [r[0] for r in rows]
