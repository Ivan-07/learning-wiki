"""RebuildService：删除派生库后从文件事实源完整重建（规格 4.3）。

恢复范围：来源与版本、Evidence、Wiki 页面、学习对象/事件、提案状态、
操作记录、FTS5。不恢复：瞬时任务、UI 状态、缓存。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from learning_wiki.domain import hashing
from learning_wiki.domain.contracts import (
    ApplicationChallenge,
    Goal,
    LearningEvent,
    LearningObject,
    Misconception,
)
from learning_wiki.retrieval.fts import FtsIndex
from learning_wiki.storage import evidence_store, yaml_io
from learning_wiki.storage.db.indexer import DbIndexer
from learning_wiki.storage.note_indexer import NoteIndexer
from learning_wiki.storage.source_repository import SourceRepository
from learning_wiki.storage.vault_paths import VaultPaths


@dataclass
class RebuildReport:
    sources: int = 0
    versions: int = 0
    evidence: int = 0
    notes: int = 0
    learning_objects: int = 0
    learning_events: int = 0
    goals: int = 0
    misconceptions: int = 0
    challenges: int = 0
    proposals: int = 0
    operations: int = 0
    errors: list[str] = field(default_factory=list)


class RebuildService:
    def __init__(
        self,
        paths: VaultPaths,
        conn: sqlite3.Connection,
        indexer: DbIndexer,
        fts: FtsIndex,
        repo: SourceRepository,
        clock: object,
    ) -> None:
        self.paths = paths
        self.conn = conn
        self.indexer = indexer
        self.fts = fts
        self.repo = repo
        self.clock = clock
        self.note_indexer = NoteIndexer(paths, indexer, fts)

    def rebuild(self, progress=None) -> RebuildReport:
        report = RebuildReport()
        now = str(getattr(self.clock, "now_iso", lambda: "")())
        self.indexer.wipe_all()
        self._rebuild_sources(report, progress)
        self._rebuild_wiki(report, now, progress)
        self._rebuild_learning(report, progress)
        self._rebuild_proposals(report)
        self._rebuild_operations(report)
        return report

    # -- 各段 ---------------------------------------------------------------

    def _rebuild_sources(self, report: RebuildReport, progress=None) -> None:
        manifests = self.repo.load_all()
        for i, manifest in enumerate(manifests):
            if progress:
                progress("sources", (i + 1) / max(len(manifests), 1))
            sdir = self.repo.source_dir(manifest.source_id)
            self.indexer.upsert_source(manifest)
            self.conn.execute(
                "UPDATE sources SET manifest_path = ? WHERE source_id = ?",
                (self.paths.relpath(sdir / "manifest.yaml"), manifest.source_id),
            )
            for rec in manifest.versions:
                row_id = self.indexer.upsert_version(manifest.source_id, rec)
                content_file = sdir / rec.content_path
                if content_file.exists():
                    body = content_file.read_text(encoding="utf-8")
                    self.fts.index_source_version(row_id, manifest.title, body)
                    self.indexer.record_index_state(
                        self.paths.relpath(content_file),
                        rec.content_hash,
                        content_file.stat().st_mtime,
                        content_file.stat().st_size,
                        rec.created_at,
                    )
                report.versions += 1
                evs = evidence_store.load_evidence_file(sdir / rec.evidence_path)
                for ev in evs:
                    self.indexer.insert_evidence(ev)
                    report.evidence += 1
            report.sources += 1

    def _rebuild_wiki(self, report: RebuildReport, now: str, progress=None) -> None:
        wiki_dir = self.paths.folder("wiki")
        if not wiki_dir.exists():
            return
        files = sorted(p for p in wiki_dir.rglob("*.md") if p.is_file())
        for i, path in enumerate(files):
            if progress:
                progress("wiki", (i + 1) / max(len(files), 1))
            rel = self.paths.relpath(path)
            self.note_indexer.index_note(rel, path.read_text(encoding="utf-8"), now)
            report.notes += 1

    def _rebuild_learning(self, report: RebuildReport, progress=None) -> None:
        learning_dir = self.paths.folder("learning")
        if not learning_dir.exists():
            return
        # Goals
        for path in sorted((learning_dir / "Goals").glob("*.yaml")):
            try:
                goal = Goal.model_validate(dict(yaml_io.load_yaml(path)))
                self.indexer.upsert_goal(goal, self.paths.relpath(path))
                report.goals += 1
            except Exception as exc:
                report.errors.append(f"goal {path.name}: {exc}")
        # LearningObjects（含知识快照引用）
        for path in sorted((learning_dir / "Objects").glob("*.yaml")):
            try:
                lo = LearningObject.model_validate(dict(yaml_io.load_yaml(path)))
                self.indexer.upsert_learning_object(lo, self.paths.relpath(path))
                snap = lo.knowledge_snapshot
                if snap is not None:
                    self.indexer.insert_snapshot_ref(
                        lo.learning_object_id, snap.note_id, snap.note_hash
                    )
                    for ev in snap.evidence:
                        self.indexer.insert_snapshot_ref(
                            lo.learning_object_id,
                            snap.note_id,
                            snap.note_hash,
                            ev.evidence_id,
                            ev.source_id,
                            ev.version_id,
                            ev.span_hash,
                        )
                report.learning_objects += 1
            except Exception as exc:
                report.errors.append(f"learning_object {path.name}: {exc}")
        # Misconceptions
        for path in sorted((learning_dir / "Misconceptions").glob("*.yaml")):
            try:
                mis = Misconception.model_validate(dict(yaml_io.load_yaml(path)))
                priority = (
                    "high" if (mis.observations.max_confidence_when_wrong or 0) >= 80 else "normal"
                )
                self.indexer.upsert_misconception(mis, self.paths.relpath(path), priority)
                report.misconceptions += 1
            except Exception as exc:
                report.errors.append(f"misconception {path.name}: {exc}")
        # ApplicationChallenges
        for path in sorted((learning_dir / "Challenges").glob("*.yaml")):
            try:
                ch = ApplicationChallenge.model_validate(dict(yaml_io.load_yaml(path)))
                self.indexer.upsert_challenge(ch, self.paths.relpath(path))
                report.challenges += 1
            except Exception as exc:
                report.errors.append(f"challenge {path.name}: {exc}")
        # Events（重放：learning_events + capability_evidence + review_queue）
        levels: dict[tuple[str, str], str] = {}
        for path in sorted((learning_dir / "Events").rglob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    evt = LearningEvent.model_validate_json(line)
                except Exception as exc:
                    report.errors.append(f"learning_event {path.name}: {exc}")
                    continue
                self._replay_learning_event(
                    evt, self.paths.relpath(path), hashing.hash_text(line), levels
                )
                report.learning_events += 1
        # 回填当前最高证据层级
        for (goal_id, capability_id), level in levels.items():
            self.indexer.update_goal_capability_level(goal_id, capability_id, level)

    def _replay_learning_event(
        self,
        evt: LearningEvent,
        record_path: str,
        payload_hash: str,
        levels: dict[tuple[str, str], str],
    ) -> None:
        self.indexer.insert_learning_event(
            evt.event_id,
            evt.event_type,
            evt.session_id,
            evt.learning_object_id,
            evt.occurred_at,
            record_path,
            payload_hash,
        )
        if evt.event_type == "capability_evidence_added" and evt.goal_id and evt.capability_id:
            self.indexer.insert_capability_evidence(
                evt.goal_id,
                evt.capability_id,
                evt.level or "recognize",
                evt.event_id,
                evt.occurred_at,
                None,
                (evt.hints_used or 0) == 0,
                not evt.source_opened_before_answer,
            )
            from learning_wiki.learning.capability import level_index

            key = (evt.goal_id, evt.capability_id)
            cur = levels.get(key)
            if cur is None or level_index(evt.level or "recognize") > level_index(cur):
                levels[key] = evt.level or "recognize"
        elif evt.event_type == "review_scheduled" and evt.learning_object_id:
            if evt.next_review_at:
                self.conn.execute(
                    "INSERT OR REPLACE INTO review_queue(learning_object_id,"
                    " next_review_at, reason, derived_from_event_id) VALUES (?,?,?,?)",
                    (evt.learning_object_id, evt.next_review_at, evt.schedule_reason, evt.event_id),
                )

    def _rebuild_proposals(self, report: RebuildReport) -> None:
        proposals_dir = self.paths.system_dir() / "Proposals"
        if not proposals_dir.exists():
            return
        for state_dir in ("pending", "applied", "rejected"):
            folder = proposals_dir / state_dir
            if not folder.exists():
                continue
            for path in sorted(folder.glob("*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    self.indexer.upsert_proposal(
                        proposal_id=data["proposal_id"],
                        status=data.get("status", state_dir),
                        path=self.paths.relpath(path),
                        risk=data.get("risk", "medium"),
                        created_by=data.get("created_by", "agent"),
                        created_at=data.get("created_at", ""),
                        operation_count=len(data.get("operations", [])),
                        validation=data.get("validation"),
                    )
                    report.proposals += 1
                except Exception as exc:
                    report.errors.append(f"proposal {path.name}: {exc}")

    def _rebuild_operations(self, report: RebuildReport) -> None:
        ops_dir = self.paths.system_dir() / "Operations"
        if not ops_dir.exists():
            return
        for path in sorted(ops_dir.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    self.indexer.insert_operation(
                        operation_id=data["operation_id"],
                        kind=data.get("kind", "apply"),
                        state=data.get("state", ""),
                        started_at=data.get("started_at", ""),
                        proposal_id=data.get("proposal_id"),
                        staging_dir=data.get("staging_dir"),
                        detail=data.get("detail"),
                    )
                    report.operations += 1
                except Exception as exc:
                    report.errors.append(f"operation {path.name}: {exc}")
