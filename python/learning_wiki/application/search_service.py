"""SearchService：统一检索 Wiki、来源与（配置允许的）个人笔记。

结果四要素（规格 6.4）：命中对象、Evidence ID + 来源版本、匹配原因、
时间与内容类型。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from learning_wiki.domain.contracts import SearchHit, SearchResponse, VaultConfig
from learning_wiki.retrieval import segmenter
from learning_wiki.retrieval.fts import FtsIndex
from learning_wiki.retrieval.query_router import classify_query
from learning_wiki.storage.source_repository import SourceRepository
from learning_wiki.storage.vault_paths import VaultPaths


@dataclass
class EvidenceLocation:
    evidence_id: str
    source_id: str
    version_id: str
    path: str  # vault 相对路径
    block_id: str
    text: str
    newer_version_available: bool = False


class SearchService:
    def __init__(
        self,
        paths: VaultPaths,
        config: VaultConfig,
        conn: sqlite3.Connection,
        repo: SourceRepository,
        fts: FtsIndex,
    ) -> None:
        self.paths = paths
        self.config = config
        self.conn = conn
        self.repo = repo
        self.fts = fts

    def search(
        self,
        query: str,
        *,
        scope: str = "all",
        source_id: str | None = None,
        trace: bool = False,
        limit: int = 20,
    ) -> SearchResponse:
        query_type = classify_query(query, source_id=source_id)
        trace_lines: list[str] = []
        hits: list[SearchHit] = []

        if query_type == "exact":
            hits = self._exact_lookup(query, trace_lines)
        else:
            if scope in ("all", "sources"):
                hits += self._search_sources(
                    query,
                    source_id=source_id,
                    limit=limit,
                    trace_lines=trace_lines,
                )
            if scope in ("all", "wiki") and self.config.retrieval.index_wiki:
                hits += self._search_notes(query, limit=limit, trace_lines=trace_lines)

        return SearchResponse(
            query=query,
            query_type=query_type,  # type: ignore[arg-type]  # classify_query 返回受控子集
            trace=trace_lines,
            hits=hits,
        )

    # -- 各策略 ---------------------------------------------------------------

    def _exact_lookup(self, query: str, trace: list[str]) -> list[SearchHit]:
        trace.append(f"exact: 识别为 ID（{query[:2]}…）")
        q = query.strip()
        if q.startswith("ev_"):
            row = self.conn.execute("SELECT * FROM evidence WHERE evidence_id = ?", (q,)).fetchone()
            if row is None:
                return []
            return [self._evidence_hit(row, "精确 Evidence ID 匹配")]
        # source id → 列出全部版本，最新在前
        rows = self.conn.execute(
            "SELECT v.*, s.title FROM source_versions v"
            " JOIN sources s ON s.source_id = v.source_id"
            " WHERE v.source_id = ? ORDER BY v.version_seq DESC",
            (q,),
        ).fetchall()
        return [
            SearchHit(
                kind="source_version",
                source_id=r["source_id"],
                version_id=r["version_id"],
                title=r["title"],
                path=self._version_path(r["source_id"], r["version_id"]),
                match_reason="精确 Source ID 匹配",
                content_type="source",
                occurred_at=r["created_at"],
            )
            for r in rows
        ]

    def _search_sources(
        self,
        query: str,
        *,
        source_id: str | None,
        limit: int,
        trace_lines: list[str],
    ) -> list[SearchHit]:
        rows = self.fts.query_sources(query, limit=limit * 2)
        trace_lines.append(f"fts_sources: {len(rows)} 个版本命中（jieba + unicode61）")
        hits: list[SearchHit] = []
        for row in rows:
            v = self.conn.execute(
                "SELECT v.*, s.title FROM source_versions v"
                " JOIN sources s ON s.source_id = v.source_id"
                " WHERE v.id = ?",
                (row["rowid"],),
            ).fetchone()
            if v is None:
                continue
            if source_id is not None and v["source_id"] != source_id:
                continue
            hit = SearchHit(
                kind="source_version",
                source_id=v["source_id"],
                version_id=v["version_id"],
                title=v["title"],
                path=self._version_path(v["source_id"], v["version_id"]),
                match_reason="全文命中" + ("（限定来源内）" if source_id else ""),
                content_type="source",
                occurred_at=v["created_at"],
            )
            # 证据装配：该版本中包含任一查询 token 的证据块
            ev = self._match_evidence(v["source_id"], v["version_id"], query)
            if ev is not None:
                hit.matched_evidence_id = ev["evidence_id"]
                hit.matched_text = ev["text"][:200]
            hits.append(hit)
            if len(hits) >= limit:
                break
        return hits

    def _search_notes(self, query: str, *, limit: int, trace_lines: list[str]) -> list[SearchHit]:
        rows = self.fts.query_notes(query, limit=limit)
        trace_lines.append(f"fts_notes: {len(rows)} 个 Wiki 页面命中")
        hits = []
        for row in rows:
            note = self.conn.execute(
                "SELECT * FROM wiki_notes WHERE note_id = ?", (row["note_id"],)
            ).fetchone()
            if note is None:
                continue
            hits.append(
                SearchHit(
                    kind="wiki_note",
                    note_id=note["note_id"],
                    title=note["title"],
                    path=note["path"],
                    match_reason="Wiki 全文命中",
                    content_type="wiki",
                    occurred_at=note["updated_at"],
                )
            )
        return hits

    # -- 证据定位 -------------------------------------------------------------

    def open_evidence(self, evidence_id: str) -> EvidenceLocation:
        row = self.conn.execute(
            "SELECT * FROM evidence WHERE evidence_id = ?", (evidence_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Evidence 不存在: {evidence_id}")
        return self._evidence_location(row)

    def _match_evidence(self, source_id: str, version_id: str, query: str) -> sqlite3.Row | None:
        """返回该版本中包含任一查询 token 的第一条证据。"""
        rows = self.conn.execute(
            "SELECT * FROM evidence WHERE source_id = ? AND version_id = ? ORDER BY block_id",
            (source_id, version_id),
        ).fetchall()
        tokens = [t for t in segmenter.tokens(query) if len(t) >= 2]
        for row in rows:
            if any(t in row["text"] for t in tokens):
                return row  # type: ignore[no-any-return]
        # token 未命中（分词噪声）→ 退化为原始查询子串
        q = query.strip()
        for row in rows:
            if q and q in row["text"]:
                return row  # type: ignore[no-any-return]
        return None

    def _evidence_hit(self, row: sqlite3.Row, reason: str) -> SearchHit:
        loc = self._evidence_location(row)
        return SearchHit(
            kind="source_version",
            source_id=loc.source_id,
            version_id=loc.version_id,
            matched_evidence_id=loc.evidence_id,
            matched_text=loc.text[:200],
            path=loc.path,
            match_reason=reason,
            content_type="source",
        )

    def _evidence_location(self, row: sqlite3.Row) -> EvidenceLocation:
        v = self.conn.execute(
            "SELECT v.*, s.active_version_id, s.title FROM source_versions v"
            " JOIN sources s ON s.source_id = v.source_id"
            " WHERE v.source_id = ? AND v.version_id = ?",
            (row["source_id"], row["version_id"]),
        ).fetchone()
        newer = bool(v and v["version_id"] != v["active_version_id"])
        return EvidenceLocation(
            evidence_id=row["evidence_id"],
            source_id=row["source_id"],
            version_id=row["version_id"],
            path=self._version_path(row["source_id"], row["version_id"]),
            block_id=row["block_id"],
            text=row["text"],
            newer_version_available=newer,
        )

    def _version_path(self, source_id: str, version_id: str) -> str:
        row = self.conn.execute(
            "SELECT content_path FROM source_versions WHERE source_id = ? AND version_id = ?",
            (source_id, version_id),
        ).fetchone()
        if row is None:
            return ""
        return f"{self.paths.folders.sources}/{source_id}/{row['content_path']}"
