"""NoteIndexer：Wiki 页面 → notes / claims / claim_evidence / FTS。

RebuildService 与 ProposalApplier 共用（应用提案后增量重索引）。
"""

from __future__ import annotations

from pathlib import Path

from learning_wiki.domain import hashing
from learning_wiki.retrieval.fts import FtsIndex
from learning_wiki.storage import note_parser
from learning_wiki.storage.db.indexer import DbIndexer
from learning_wiki.storage.vault_paths import VaultPaths


class NoteIndexer:
    def __init__(self, paths: VaultPaths, indexer: DbIndexer, fts: FtsIndex) -> None:
        self.paths = paths
        self.indexer = indexer
        self.fts = fts

    def index_note(self, rel: str, text: str, now: str) -> str:
        """索引一个 Wiki 页面，返回 note_id。"""
        path = self.paths.resolve(rel)
        fm = note_parser.parse_frontmatter(text)
        note_id = str(fm.get("note_id") or Path(rel).stem)
        title = str(fm.get("title") or Path(rel).stem)
        self.indexer.upsert_note(
            note_id=note_id,
            path=rel,
            title=title,
            note_type=str(fm.get("type", "concept")),
            status=str(fm.get("status", "draft")),
            file_hash=hashing.hash_text(text),
            mtime=path.stat().st_mtime if path.exists() else 0.0,
            updated_at=str(fm.get("updated_at") or now),
        )
        self.fts.remove_note(note_id)
        self.fts.index_note(note_id, title, text)
        # Claim Block → claims / claim_evidence（反向引用）
        for claim in note_parser.parse_claims(text, note_id):
            self.indexer.upsert_claim(claim)
            for evidence_id in claim.citations:
                row = self.indexer.conn.execute(
                    "SELECT source_id, version_id FROM evidence WHERE evidence_id = ?",
                    (evidence_id,),
                ).fetchone()
                if row is not None:
                    self.indexer.insert_claim_evidence(
                        claim.claim_block_id,
                        evidence_id,
                        row["source_id"],
                        row["version_id"],
                    )
        self.indexer.record_index_state(
            rel,
            hashing.hash_text(text),
            path.stat().st_mtime if path.exists() else 0.0,
            len(text.encode("utf-8")),
            now,
        )
        return note_id
