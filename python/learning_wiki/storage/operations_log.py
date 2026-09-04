"""操作记录（Operations/YYYY-MM.jsonl）：追加型审计日志。

- 事实源（Git 默认包含）；恢复决策以 staging manifest 为准，本日志用于审计与查询；
- 每行一个 OperationRecord。
"""

from __future__ import annotations

from pathlib import Path

from learning_wiki.domain import ids
from learning_wiki.domain.contracts import OperationRecord
from learning_wiki.storage.vault_paths import VaultPaths


class OperationsLog:
    def __init__(self, paths: VaultPaths, writer: object, clock: object) -> None:
        self.paths = paths
        self.writer = writer
        self.clock = clock

    def _path_for(self, when_iso: str) -> Path:
        month = when_iso[:7]  # YYYY-MM
        return self.paths.system_dir() / "Operations" / f"{month}.jsonl"

    def append(
        self,
        kind: str,
        state: str,
        *,
        proposal_id: str | None = None,
        staging_dir: str | None = None,
        detail: dict | None = None,
        operation_id: str | None = None,
        started_at: str | None = None,
    ) -> str:
        now = self.clock.now_iso()  # type: ignore[attr-defined]
        record = OperationRecord(
            operation_id=operation_id or ids.new_operation_id(),
            kind=kind,  # type: ignore[arg-type]
            proposal_id=proposal_id,
            state=state,
            started_at=started_at or now,
            finished_at=now,
            detail=detail or {},
        )
        self.writer.append_line(  # type: ignore[attr-defined]
            self._path_for(now), record.model_dump_json()
        )
        return record.operation_id
