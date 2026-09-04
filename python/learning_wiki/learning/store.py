"""学习机制升级：事实源存储层（YAML 对象 + JSONL 事件）。

- Goal / Misconception / ApplicationChallenge / LearningObject 存为
  ``40 Learning/{Goals,Misconceptions,Challenges,Objects}/{id}.yaml``；
- 事件追加到 ``40 Learning/Events/YYYY-MM/{session_id}.jsonl``（无会话事件
  归入 ``sys.jsonl``），按 event_id 幂等；
- 派生索引（SQLite）随事实源写入同步更新，删除后可 rebuild。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from learning_wiki.adapters.executor.local_fsync import SafeFileWriter
from learning_wiki.domain import hashing
from learning_wiki.domain.contracts import (
    ApplicationChallenge,
    Contract,
    Goal,
    KnowledgeUpdateCandidate,
    LearningEvent,
    LearningObject,
    Misconception,
)
from learning_wiki.storage.db.indexer import DbIndexer
from learning_wiki.storage.vault_paths import VaultPaths
from learning_wiki.storage.yaml_io import dump_yaml, load_yaml

_SYS_JOURNAL = "sys"


class LearningStoreError(Exception):
    """学习存储层错误（对象不存在、非法操作等）。"""


class _YamlObjectRepository:
    """按 id 读写单一类型 YAML 对象的基类。"""

    subdir: str = ""

    def __init__(self, paths: VaultPaths, writer: SafeFileWriter) -> None:
        self.paths = paths
        self.writer = writer

    def dir(self) -> Path:
        d = self.paths.folder("learning") / self.subdir
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _path_for(self, object_id: str) -> Path:
        return self.dir() / f"{object_id}.yaml"

    def _relpath_for(self, object_id: str) -> str:
        return self.paths.relpath(self._path_for(object_id))

    def exists(self, object_id: str) -> bool:
        return self._path_for(object_id).exists()

    def _save(self, obj: Any, object_id: str) -> str:
        target = self._path_for(object_id)
        data = json.loads(obj.model_dump_json())
        dump_yaml(data, target, self.writer)
        return self.paths.relpath(target)

    def _load(self, object_id: str, model: type[Contract]) -> Any:
        p = self._path_for(object_id)
        if not p.exists():
            raise LearningStoreError(f"{model.__name__} 不存在: {object_id}")
        return model.model_validate(dict(load_yaml(p)))

    def _list(self, model: type[Contract]) -> list[Any]:
        result = []
        for p in sorted(self.dir().glob("*.yaml")):
            try:
                result.append(model.model_validate(dict(load_yaml(p))))
            except Exception as exc:
                raise LearningStoreError(f"解析失败 {p.name}: {exc}") from exc
        return result


class GoalRepository(_YamlObjectRepository):
    subdir = "Goals"

    def __init__(self, paths: VaultPaths, writer: SafeFileWriter, indexer: DbIndexer) -> None:
        super().__init__(paths, writer)
        self.indexer = indexer

    def save(self, goal: Goal) -> None:
        rel = self._save(goal, goal.goal_id)
        self.indexer.upsert_goal(goal, rel)

    def load(self, goal_id: str) -> Goal:
        return self._load(goal_id, Goal)

    def list(self) -> list[Goal]:
        return self._list(Goal)


class MisconceptionRepository(_YamlObjectRepository):
    subdir = "Misconceptions"

    def __init__(self, paths: VaultPaths, writer: SafeFileWriter, indexer: DbIndexer) -> None:
        super().__init__(paths, writer)
        self.indexer = indexer

    def save(self, mis: Misconception) -> None:
        rel = self._save(mis, mis.misconception_id)
        priority = (
            "high"
            if mis.observations.max_confidence_when_wrong is not None
            and (mis.observations.max_confidence_when_wrong >= 80)
            else "normal"
        )
        self.indexer.upsert_misconception(mis, rel, priority)

    def load(self, misconception_id: str) -> Misconception:
        return self._load(misconception_id, Misconception)

    def list(self) -> list[Misconception]:
        return self._list(Misconception)


class ChallengeRepository(_YamlObjectRepository):
    subdir = "Challenges"

    def __init__(self, paths: VaultPaths, writer: SafeFileWriter, indexer: DbIndexer) -> None:
        super().__init__(paths, writer)
        self.indexer = indexer

    def save(self, challenge: ApplicationChallenge) -> None:
        rel = self._save(challenge, challenge.challenge_id)
        self.indexer.upsert_challenge(challenge, rel)

    def load(self, challenge_id: str) -> ApplicationChallenge:
        return self._load(challenge_id, ApplicationChallenge)

    def list(self) -> list[ApplicationChallenge]:
        return self._list(ApplicationChallenge)


class LearningObjectRepository(_YamlObjectRepository):
    subdir = "Objects"

    def __init__(self, paths: VaultPaths, writer: SafeFileWriter, indexer: DbIndexer) -> None:
        super().__init__(paths, writer)
        self.indexer = indexer

    def save(self, obj: LearningObject) -> None:
        rel = self._save(obj, obj.learning_object_id)
        self.indexer.upsert_learning_object(obj, rel)
        snap = obj.knowledge_snapshot
        if snap is not None:
            self.indexer.insert_snapshot_ref(obj.learning_object_id, snap.note_id, snap.note_hash)
            for ev in snap.evidence:
                self.indexer.insert_snapshot_ref(
                    obj.learning_object_id,
                    snap.note_id,
                    snap.note_hash,
                    ev.evidence_id,
                    ev.source_id,
                    ev.version_id,
                    ev.span_hash,
                )

    def load(self, learning_object_id: str) -> LearningObject:
        return self._load(learning_object_id, LearningObject)

    def list(self, goal_id: str | None = None) -> list[LearningObject]:
        objects = self._list(LearningObject)
        if goal_id is not None:
            objects = [o for o in objects if o.goal_id == goal_id]
        return objects

    def set_status(self, learning_object_id: str, status: str) -> LearningObject:
        obj = self.load(learning_object_id)
        obj.status = status  # type: ignore[assignment]
        self.save(obj)
        return obj


class EventLog:
    """追加型学习事件日志（JSONL 事实源）。"""

    def __init__(
        self,
        paths: VaultPaths,
        writer: SafeFileWriter,
        indexer: DbIndexer,
    ) -> None:
        self.paths = paths
        self.writer = writer
        self.indexer = indexer

    def events_dir(self) -> Path:
        return self.paths.folder("learning") / "Events"

    def journal_path(self, event: LearningEvent) -> Path:
        month = event.occurred_at[:7]  # YYYY-MM
        sid = event.session_id or _SYS_JOURNAL
        return self.events_dir() / month / f"{sid}.jsonl"

    def append(self, event: LearningEvent) -> None:
        """追加事件（按 event_id 幂等：已存在则跳过）。"""
        path = self.journal_path(event)
        line = event.model_dump_json()
        if self._contains(path, event.event_id):
            return
        self.writer.append_line(path, line)
        self.indexer.insert_learning_event(
            event.event_id,
            event.event_type,
            event.session_id,
            event.learning_object_id,
            event.occurred_at,
            self.paths.relpath(path),
            hashing.hash_text(line),
        )

    @staticmethod
    def _contains(path: Path, event_id: str) -> bool:
        if not path.exists():
            return False
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            try:
                if json.loads(raw).get("event_id") == event_id:
                    return True
            except json.JSONDecodeError:
                continue
        return False

    def load_session(self, session_id: str) -> list[LearningEvent]:
        """重放一个会话的全部事件（会话状态由此推导，无独立会话文件）。"""
        events: list[LearningEvent] = []
        if not self.events_dir().exists():
            return events
        for path in sorted(self.events_dir().rglob(f"{session_id}.jsonl")):
            events.extend(self._read_file(path))
        return events

    def all_events(self) -> list[LearningEvent]:
        events: list[LearningEvent] = []
        if not self.events_dir().exists():
            return events
        for path in sorted(self.events_dir().rglob("*.jsonl")):
            events.extend(self._read_file(path))
        return events

    def events_for_goal(self, goal_id: str) -> list[LearningEvent]:
        return [e for e in self.all_events() if e.goal_id == goal_id]

    @staticmethod
    def _read_file(path: Path) -> list[LearningEvent]:
        events: list[LearningEvent] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            try:
                events.append(LearningEvent.model_validate_json(raw))
            except Exception:
                continue  # 未来 schema 事件只读跳过（与 lint 策略一致）
        return events


class KnowledgeUpdateCandidateStore(_YamlObjectRepository):
    """知识更新候选：学习结果只能创建核验候选，不能直接修改 Wiki（§4.8）。"""

    subdir = "UpdateCandidates"

    def __init__(self, paths: VaultPaths, writer: SafeFileWriter) -> None:
        super().__init__(paths, writer)
        # UpdateCandidates 位于 _System/Learning Wiki/ 下，重写目录
        self._dir = paths.system_dir() / "UpdateCandidates"

    def dir(self) -> Path:
        self._dir.mkdir(parents=True, exist_ok=True)
        return self._dir

    def _relpath_for(self, object_id: str) -> str:
        return self.paths.relpath(self._path_for(object_id))

    def save(self, candidate: KnowledgeUpdateCandidate) -> str:
        return self._save(candidate, candidate.candidate_id)

    def load(self, candidate_id: str) -> KnowledgeUpdateCandidate:
        return self._load(candidate_id, KnowledgeUpdateCandidate)

    def list(self) -> list[KnowledgeUpdateCandidate]:
        return self._list(KnowledgeUpdateCandidate)
