"""LearningService：学习机制升级的门面（组合根）。

CLI / MCP / 测试经此使用；状态变更后自动重建 Views（非事实源）。
"""

from __future__ import annotations

import sqlite3

from learning_wiki.adapters.executor.local_fsync import SafeFileWriter
from learning_wiki.learning.application_service import ApplicationService
from learning_wiki.learning.capability import CapabilityEvidenceService
from learning_wiki.learning.diagnostic import DiagnosticService
from learning_wiki.learning.goal_service import GoalService
from learning_wiki.learning.misconception_service import MisconceptionService
from learning_wiki.learning.orchestrator import Orchestrator
from learning_wiki.learning.snapshots import SnapshotService
from learning_wiki.learning.store import (
    ChallengeRepository,
    EventLog,
    GoalRepository,
    KnowledgeUpdateCandidateStore,
    LearningObjectRepository,
    MisconceptionRepository,
)
from learning_wiki.learning.views import ViewsRenderer
from learning_wiki.storage.db.indexer import DbIndexer
from learning_wiki.storage.vault_paths import VaultPaths


class LearningService:
    def __init__(
        self,
        paths: VaultPaths,
        writer: SafeFileWriter,
        indexer: DbIndexer,
        conn: sqlite3.Connection,
        clock,
    ) -> None:
        self.paths = paths
        self.writer = writer
        self.conn = conn
        self.clock = clock

        self.goal_repo = GoalRepository(paths, writer, indexer)
        self.misconception_repo = MisconceptionRepository(paths, writer, indexer)
        self.challenge_repo = ChallengeRepository(paths, writer, indexer)
        self.object_repo = LearningObjectRepository(paths, writer, indexer)
        self.event_log = EventLog(paths, writer, indexer)
        self.candidate_store = KnowledgeUpdateCandidateStore(paths, writer)

        self.capability_service = CapabilityEvidenceService()
        self.goals = GoalService(
            self.goal_repo, self.event_log, clock, conn, self.capability_service
        )
        self.diagnostics = DiagnosticService(self.goals, self.event_log)
        self.misconceptions = MisconceptionService(
            self.misconception_repo,
            self.object_repo,
            self.event_log,
            clock,
            self.candidate_store,
        )
        self.applications = ApplicationService(
            self.challenge_repo, self.event_log, clock, conn, self.candidate_store
        )
        self.orchestrator = Orchestrator(
            self.goals, self.misconceptions, self.object_repo, self.event_log, clock, conn
        )
        self.snapshots = SnapshotService(
            self.object_repo, self.event_log, clock, conn, goals=self.goals
        )
        self.views = ViewsRenderer(
            self.goal_repo,
            self.misconception_repo,
            self.challenge_repo,
            self.event_log,
        )

    def render_views(self) -> list[str]:
        return self.views.render_all()
