"""VaultContext：组合根。

CLI / 测试 /（未来的 MCP）都从这里获取装配好的服务。
构造即校验：Vault 未初始化（无 Config.yaml）时抛 ConfigNotFoundError。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from learning_wiki.adapters.executor.local_fsync import SafeFileWriter
from learning_wiki.application.capture_service import CaptureService
from learning_wiki.application.integrity_service import IntegrityService
from learning_wiki.application.search_service import SearchService
from learning_wiki.domain.clock import SystemClock
from learning_wiki.domain.contracts import VaultConfig
from learning_wiki.git.git_adapter import GitAdapter
from learning_wiki.learning.service import LearningService
from learning_wiki.proposals.applier import ProposalApplier
from learning_wiki.proposals.recovery import ProposalRecovery
from learning_wiki.proposals.validator import ProposalValidator
from learning_wiki.retrieval.fts import FtsIndex
from learning_wiki.storage import config_store
from learning_wiki.storage.db import connection as db_connection
from learning_wiki.storage.db.indexer import DbIndexer
from learning_wiki.storage.evidence_store import EvidenceService
from learning_wiki.storage.note_indexer import NoteIndexer
from learning_wiki.storage.operations_log import OperationsLog
from learning_wiki.storage.proposal_repository import ProposalRepository
from learning_wiki.storage.rebuild_service import RebuildService
from learning_wiki.storage.source_repository import SourceRepository
from learning_wiki.storage.vault_paths import VaultPaths

DEFAULT_SYSTEM_DIR = "_System/Learning Wiki"


class VaultContext:
    def __init__(
        self,
        vault_root: Path,
        *,
        clock: SystemClock | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        self.root = vault_root.expanduser().resolve()
        if not self.root.is_dir():
            raise FileNotFoundError(f"Vault 目录不存在: {self.root}")
        # Config 的引导位置固定；目录映射以 Config 内容为准
        bootstrap_system = self.root / DEFAULT_SYSTEM_DIR
        self.config: VaultConfig = config_store.load_config(bootstrap_system)
        self.paths = VaultPaths(self.root, self.config)
        self.clock = clock or SystemClock(self.config.library.timezone)
        self.conn = conn or db_connection.connect(self.paths.db_path())
        db_connection.migrate(self.conn)
        self.indexer = DbIndexer(self.conn)
        self.fts = FtsIndex(self.conn)
        self.writer = SafeFileWriter(self.root)
        self.source_repository = SourceRepository(self.paths, self.writer)
        self.evidence_service = EvidenceService()
        self.note_indexer = NoteIndexer(self.paths, self.indexer, self.fts)
        self.proposal_repository = ProposalRepository(self.paths, self.writer)
        self.operations_log = OperationsLog(self.paths, self.writer, self.clock)
        self._learning_service: LearningService | None = None

    @property
    def capture_service(self) -> CaptureService:
        return CaptureService(
            paths=self.paths,
            config=self.config,
            repo=self.source_repository,
            evidence=self.evidence_service,
            indexer=self.indexer,
            fts=self.fts,
            conn=self.conn,
            clock=self.clock,
            writer=self.writer,
        )

    @property
    def search_service(self) -> SearchService:
        return SearchService(
            paths=self.paths,
            config=self.config,
            conn=self.conn,
            repo=self.source_repository,
            fts=self.fts,
        )

    @property
    def rebuild_service(self) -> RebuildService:
        return RebuildService(
            paths=self.paths,
            conn=self.conn,
            indexer=self.indexer,
            fts=self.fts,
            repo=self.source_repository,
            clock=self.clock,
        )

    @property
    def proposal_validator(self) -> ProposalValidator:
        return ProposalValidator(self.paths, self.conn)

    @property
    def git_adapter(self) -> GitAdapter:
        return GitAdapter(self.root, enabled=self.config.git.enabled)

    @property
    def proposal_applier(self) -> ProposalApplier:
        # Git 未启用时 GitAdapter 抛 GitNotEnabledError，applier 标记「未提交」
        return ProposalApplier(
            paths=self.paths,
            writer=self.writer,
            clock=self.clock,
            repo=self.proposal_repository,
            validator=self.proposal_validator,
            indexer=self.indexer,
            note_indexer=self.note_indexer,
            operations=self.operations_log,
            git_adapter=self.git_adapter if self.config.git.auto_commit_applied_proposals else None,
        )

    @property
    def proposal_recovery(self) -> ProposalRecovery:
        return ProposalRecovery(
            applier=self.proposal_applier, indexer=self.indexer, clock=self.clock
        )

    @property
    def integrity_service(self) -> IntegrityService:
        return IntegrityService(
            paths=self.paths,
            repo=self.source_repository,
            conn=self.conn,
        )

    @property
    def learning_service(self) -> LearningService:
        """学习机制升级门面：能力目标 / 误解模型 / 迁移与应用 / 快照联动。"""
        if self._learning_service is None:
            self._learning_service = LearningService(
                self.paths, self.writer, self.indexer, self.conn, self.clock
            )
        return self._learning_service

    def close(self) -> None:
        self.conn.close()
