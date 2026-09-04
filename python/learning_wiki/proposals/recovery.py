"""ProposalRecovery：启动时扫描非终态 staging manifest 并恢复（规格 7.2）。

判定规则（staging manifest 是恢复权威，Operations JSONL 只做审计）：

| 发现                          | 动作                                   |
| ----------------------------- | -------------------------------------- |
| prepared 且无任何 done        | 清 staging，提案回 pending             |
| applying 部分完成              | 幂等续跑（execute + finalize）         |
| 全部文件哈希 == result_hash    | 补 finalize                            |
| 哈希既非 base 也非 result      | needs_manual + integrity_alert         |
| rolling_back                  | 继续回滚                               |
| applied（未清 staging）        | 清 staging                             |
"""

from __future__ import annotations

from dataclasses import dataclass, field

from learning_wiki.domain import hashing, ids
from learning_wiki.domain.contracts import FilePlan
from learning_wiki.proposals.applier import BaseHashConflict, ProposalApplier
from learning_wiki.storage.db.indexer import DbIndexer


@dataclass
class RecoveryReport:
    operation_id: str
    proposal_id: str | None
    outcome: str  # resumed_applied | reset_pending | needs_manual | cleaned
    detail: str = ""
    written_paths: list[str] = field(default_factory=list)


class ProposalRecovery:
    def __init__(
        self,
        applier: ProposalApplier,
        indexer: DbIndexer,
        clock: object,
    ) -> None:
        self.applier = applier
        self.indexer = indexer
        self.clock = clock

    def scan_and_recover(self) -> list[RecoveryReport]:
        staging_root = self.applier.paths.dot_dir() / "staging"
        if not staging_root.exists():
            return []
        reports: list[RecoveryReport] = []
        for entry in sorted(staging_root.iterdir()):
            manifest = entry / "manifest.json"
            if not manifest.exists():
                continue  # 非 staging 内容（如临时目录），留给 doctor 报告
            try:
                plan = self.applier.load_plan(entry.name)
            except Exception as exc:
                reports.append(
                    RecoveryReport(entry.name, None, "needs_manual", f"manifest 无法解析: {exc}")
                )
                continue
            reports.append(self._recover_one(plan))
        return reports

    def _recover_one(self, plan: FilePlan) -> RecoveryReport:
        proposal_id = plan.proposal_id
        base = RecoveryReport(plan.operation_id, proposal_id, "needs_manual")

        if plan.state == "applied":
            import shutil

            shutil.rmtree(self.applier.staging_dir(plan.operation_id), ignore_errors=True)
            base.outcome = "cleaned"
            return base

        if plan.state == "rolling_back":
            self.applier.rollback(plan)
            base.outcome = "reset_pending"
            base.detail = "已回滚"
            return base

        if plan.state == "prepared" and not any(s.done for s in plan.steps):
            import shutil

            shutil.rmtree(self.applier.staging_dir(plan.operation_id), ignore_errors=True)
            base.outcome = "reset_pending"
            base.detail = "未开始：清 staging 回 pending"
            return base

        if plan.state in ("prepared", "applying", "needs_manual"):
            # 没有任何目标被替换（无 done 且无文件写入）→ 恢复为 pending（规格 7.2）
            if not any(s.done for s in plan.steps) and not self._any_step_written(plan):
                import shutil

                shutil.rmtree(self.applier.staging_dir(plan.operation_id), ignore_errors=True)
                base.outcome = "reset_pending"
                base.detail = "未替换任何目标：清 staging 回 pending"
                return base
            # 先判断是否全部已写入（可能 execute 完成、finalize 前崩溃）
            if self._all_steps_written(plan):
                outcome = self.applier.finalize(plan)
                base.outcome = "resumed_applied"
                base.detail = "补 finalize"
                base.written_paths = outcome.written_paths
                return base
            try:
                self.applier.execute(plan)
                outcome = self.applier.finalize(plan)
                base.outcome = "resumed_applied"
                base.detail = "幂等续跑"
                base.written_paths = outcome.written_paths
                return base
            except BaseHashConflict as exc:
                self._needs_manual(plan, str(exc))
                base.detail = str(exc)
                return base

        base.detail = f"未知状态: {plan.state}"
        self._needs_manual(plan, base.detail)
        return base

    def _all_steps_written(self, plan: FilePlan) -> bool:
        for step in plan.steps:
            target = (
                self.applier._archive_target(step.path)
                if step.action == "move_to_archive"
                else self.applier.paths.resolve(step.path)
            )
            if not target.exists():
                return False
            if hashing.hash_bytes(target.read_bytes()) != step.result_hash:
                return False
        return True

    def _any_step_written(self, plan: FilePlan) -> bool:
        for step in plan.steps:
            target = (
                self.applier._archive_target(step.path)
                if step.action == "move_to_archive"
                else self.applier.paths.resolve(step.path)
            )
            if target.exists() and hashing.hash_bytes(target.read_bytes()) == step.result_hash:
                return True
        return False

    def _needs_manual(self, plan: FilePlan, detail: str) -> None:
        plan.state = "needs_manual"
        self.applier.save_plan(plan)
        assert plan.proposal_id is not None
        self.indexer.insert_alert(
            ids.new_alert_id(),
            "needs_manual_recovery",
            plan.proposal_id,
            {"operation_id": plan.operation_id, "detail": detail},
            self.clock.now_iso(),  # type: ignore[attr-defined]
        )
