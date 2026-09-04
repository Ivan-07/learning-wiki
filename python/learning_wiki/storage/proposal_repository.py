"""ProposalRepository：提案的事实源读写。

- 位置：_System/Learning Wiki/Proposals/{pending,applied,rejected}/{proposal_id}.json
- 状态与目录一致；移动文件 = 状态迁移（原子写 + 删除）；
- rebuild 只认目录归属。
"""

from __future__ import annotations

import json
from pathlib import Path

from learning_wiki.domain.contracts import ChangeProposal
from learning_wiki.storage.vault_paths import VaultPaths

STATES = ("pending", "applied", "rejected")


class ProposalNotFoundError(FileNotFoundError):
    pass


class ProposalRepository:
    def __init__(self, paths: VaultPaths, writer: object) -> None:
        self.paths = paths
        self.writer = writer
        self._dir = paths.system_dir() / "Proposals"

    # -- 读 -----------------------------------------------------------------

    def dir_for(self, state: str) -> Path:
        return self._dir / state

    def path_for(self, proposal_id: str, state: str) -> Path:
        return self.dir_for(state) / f"{proposal_id}.json"

    def load(self, proposal_id: str) -> ChangeProposal:
        for state in STATES:
            path = self.path_for(proposal_id, state)
            if path.exists():
                return ChangeProposal.model_validate(json.loads(path.read_text(encoding="utf-8")))
        raise ProposalNotFoundError(f"提案不存在: {proposal_id}")

    def state_of(self, proposal_id: str) -> str:
        for state in STATES:
            if self.path_for(proposal_id, state).exists():
                return state
        raise ProposalNotFoundError(f"提案不存在: {proposal_id}")

    def list(self, state: str | None = None) -> list[ChangeProposal]:
        result = []
        for s in [state] if state else STATES:
            folder = self.dir_for(s)
            if not folder.exists():
                continue
            for path in sorted(folder.glob("*.json")):
                result.append(
                    ChangeProposal.model_validate(json.loads(path.read_text(encoding="utf-8")))
                )
        return result

    # -- 写 -----------------------------------------------------------------

    def save(self, proposal: ChangeProposal) -> None:
        target = self.path_for(proposal.proposal_id, "pending")
        target.parent.mkdir(parents=True, exist_ok=True)
        self.writer.write_text(  # type: ignore[attr-defined]
            target,
            json.dumps(proposal.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        )

    def move_to(self, proposal_id: str, state: str) -> ChangeProposal:
        """pending → applied / rejected（或任意迁移）。"""
        if state not in STATES:
            raise ValueError(f"未知提案状态: {state}")
        source = None
        for s in STATES:
            if s == state:
                continue
            candidate = self.path_for(proposal_id, s)
            if candidate.exists():
                source = candidate
                break
        if source is None:
            raise ProposalNotFoundError(f"提案不存在: {proposal_id}")
        proposal = self.load(proposal_id)
        proposal.status = state  # type: ignore[assignment]
        target = self.path_for(proposal_id, state)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.writer.write_text(  # type: ignore[attr-defined]
            target,
            json.dumps(proposal.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        )
        source.unlink()
        return proposal
