"""ProposalApplier：prepare → execute → finalize 状态机（方案 §6）。

安全写入顺序不变量：
1. prepare：校验 → staging 前后像落盘 → manifest(state=prepared)；
2. execute：逐文件原子替换，**文件持久化先于 manifest 的 done 标记**，
   任意崩溃点重跑幂等（当前哈希 == result_hash 视为已完成）；
3. finalize：复验 + post-check（frontmatter）→ applied → 提案移目录 →
   索引更新 → 可选 Git pathspec 提交 → 清 staging。

move_to_archive 展开为两步：写归档副本 → 删原文件；恢复逻辑与后端无关。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from learning_wiki.git.git_adapter import GitAdapter

from learning_wiki.adapters.executor.local_fsync import SafeFileWriter, crash_point
from learning_wiki.domain import hashing, ids
from learning_wiki.domain.contracts import (
    FilePlan,
    FilePlanStep,
    NoteFrontmatter,
)
from learning_wiki.proposals.unified_diff import _ensure_trailing_newline
from learning_wiki.proposals.validator import ProposalValidator
from learning_wiki.storage.db.indexer import DbIndexer
from learning_wiki.storage.locks import vault_write_lock
from learning_wiki.storage.note_indexer import NoteIndexer
from learning_wiki.storage.note_parser import parse_frontmatter
from learning_wiki.storage.operations_log import OperationsLog
from learning_wiki.storage.proposal_repository import ProposalRepository
from learning_wiki.storage.vault_paths import VaultPaths


class ProposalApplyError(Exception):
    pass


class BaseHashConflict(ProposalApplyError):
    """目标文件已被外部修改，提案基于过期内容。"""


@dataclass
class ApplyOutcome:
    proposal_id: str
    operation_id: str
    applied: bool
    reason: str = ""
    written_paths: list[str] = field(default_factory=list)
    git_committed: bool = False


class ProposalApplier:
    def __init__(
        self,
        paths: VaultPaths,
        writer: SafeFileWriter,
        clock: object,
        repo: ProposalRepository,
        validator: ProposalValidator,
        indexer: DbIndexer,
        note_indexer: NoteIndexer,
        operations: OperationsLog,
        git_adapter: GitAdapter | None = None,
    ) -> None:
        self.paths = paths
        self.writer = writer
        self.clock = clock
        self.repo = repo
        self.validator = validator
        self.indexer = indexer
        self.note_indexer = note_indexer
        self.operations = operations
        self.git_adapter = git_adapter

    # -- staging -------------------------------------------------------------

    def staging_dir(self, operation_id: str) -> Path:
        return self.paths.dot_dir() / "staging" / operation_id

    def load_plan(self, operation_id: str) -> FilePlan:
        manifest = self.staging_dir(operation_id) / "manifest.json"
        return FilePlan.model_validate(json.loads(manifest.read_text(encoding="utf-8")))

    def save_plan(self, plan: FilePlan) -> None:
        plan.updated_at = self.clock.now_iso()  # type: ignore[attr-defined]
        self.writer.write_text(
            self.staging_dir(plan.operation_id) / "manifest.json",
            json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        )

    def _archive_target(self, path: str) -> Path:
        return self.paths.folder("archive") / Path(path).name

    # -- 单入口（CLI 使用） ----------------------------------------------------

    def apply(self, proposal_id: str) -> ApplyOutcome:
        """prepare + execute + finalize，全程持 Vault 写锁。"""
        with vault_write_lock(self.paths.dot_dir()):
            plan = self.prepare(proposal_id)
            self.execute(plan)
            return self.finalize(plan)

    # -- 阶段一：prepare ------------------------------------------------------

    def prepare(self, proposal_id: str) -> FilePlan:
        proposal = self.repo.load(proposal_id)
        if self.repo.state_of(proposal_id) != "pending":
            raise ProposalApplyError(
                f"提案状态为 {self.repo.state_of(proposal_id)}，只能应用 pending 提案"
            )
        validation = self.validator.validate(proposal)
        if validation.errors:
            raise ProposalApplyError("提案校验失败: " + "; ".join(validation.errors))
        proposal.validation = validation
        self.repo.save(proposal)

        operation_id = ids.new_operation_id()
        sdir = self.staging_dir(operation_id)
        steps: list[FilePlanStep] = []
        for op in proposal.operations:
            base_text = None
            target = self.paths.resolve(op.path)
            if target.exists():
                base_text = target.read_text(encoding="utf-8")
            after = self.validator.staged_content(op, [])
            assert after is not None  # 校验已通过
            after = _ensure_trailing_newline(after)
            # staging 前后像（after 是权威载荷；patch 只是审阅表示）
            if base_text is not None:
                self.writer.write_text(sdir / "before" / op.path, base_text)
            self.writer.write_text(sdir / "after" / op.path, after)
            steps.append(
                FilePlanStep(
                    path=op.path,
                    action=op.operation,
                    base_hash=op.base_hash,
                    result_hash=hashing.hash_bytes(after.encode("utf-8")),
                    before_ref=f"before/{op.path}" if base_text is not None else None,
                    after_ref=f"after/{op.path}",
                )
            )
        plan = FilePlan(
            operation_id=operation_id,
            proposal_id=proposal_id,
            state="prepared",
            steps=steps,
            created_at=self.clock.now_iso(),  # type: ignore[attr-defined]
            updated_at=self.clock.now_iso(),  # type: ignore[attr-defined]
        )
        self.save_plan(plan)
        self.operations.append(
            "apply",
            "prepared",
            proposal_id=proposal_id,
            staging_dir=str(sdir),
            operation_id=operation_id,
        )
        return plan

    # -- 阶段二：execute ------------------------------------------------------

    def execute(self, plan: FilePlan) -> FilePlan:
        plan.state = "applying"
        self.save_plan(plan)
        for i, step in enumerate(plan.steps):
            crash_point(f"apply:before_step_{i}")
            self._execute_step(plan, step)
            step.done = True
            self.save_plan(plan)  # 文件持久化先于 done 标记 → 崩溃可判定
            crash_point(f"apply:after_step_{i}")
        return plan

    def _execute_step(self, plan: FilePlan, step: FilePlanStep) -> None:
        sdir = self.staging_dir(plan.operation_id)
        target = self.paths.resolve(step.path)
        after_bytes = (sdir / step.after_ref).read_bytes()

        if step.action == "move_to_archive":
            archive = self._archive_target(step.path)
            current = hashing.hash_bytes(archive.read_bytes()) if archive.exists() else None
            if current != step.result_hash:
                self.writer.write_bytes(archive, after_bytes)
            if target.exists():
                self.writer.delete(target)
            return

        current = hashing.hash_bytes(target.read_bytes()) if target.exists() else None
        if current == step.result_hash:
            return  # 幂等续跑：已完成
        if step.action == "create":
            if current is not None:
                raise BaseHashConflict(f"create 目标已存在: {step.path}")
        elif current != step.base_hash:
            raise BaseHashConflict(f"目标文件已被外部修改（base_hash 过期）: {step.path}")
        self.writer.write_bytes(target, after_bytes)

    # -- 阶段三：finalize -----------------------------------------------------

    def finalize(self, plan: FilePlan) -> ApplyOutcome:
        # 复验全部目标
        for step in plan.steps:
            target = (
                self._archive_target(step.path)
                if step.action == "move_to_archive"
                else self.paths.resolve(step.path)
            )
            current = hashing.hash_bytes(target.read_bytes()) if target.exists() else None
            if current != step.result_hash:
                raise ProposalApplyError(f"finalize 复验失败: {step.path}")

        # post-check：Wiki 页面 frontmatter（失败 → 回滚）
        for step in plan.steps:
            if step.action == "move_to_archive":
                continue
            target = self.paths.resolve(step.path)
            if target.exists() and str(target).startswith(str(self.paths.folder("wiki"))):
                fm = parse_frontmatter(target.read_text(encoding="utf-8"))
                if fm:
                    try:
                        NoteFrontmatter.model_validate(fm)
                    except Exception as exc:
                        self.rollback(plan)
                        raise ProposalApplyError(
                            f"post-check 失败（frontmatter 无效），已回滚: {step.path}: {exc}"
                        ) from exc

        assert plan.proposal_id is not None
        plan.state = "applied"
        self.save_plan(plan)
        self.repo.move_to(plan.proposal_id, "applied")
        self.operations.append(
            "apply",
            "applied",
            proposal_id=plan.proposal_id,
            operation_id=plan.operation_id,
            detail={"paths": [s.path for s in plan.steps]},
        )

        # 索引更新
        written: list[str] = []
        for step in plan.steps:
            if step.action == "move_to_archive":
                self.indexer.conn.execute("DELETE FROM wiki_notes WHERE path = ?", (step.path,))
                self.indexer.conn.execute(
                    "DELETE FROM claims WHERE note_id IN "
                    "(SELECT note_id FROM wiki_notes WHERE path = ?)",
                    (step.path,),
                )
                archive_rel = self.paths.relpath(self._archive_target(step.path))
                text = self._archive_target(step.path).read_text(encoding="utf-8")
                self.note_indexer.index_note(
                    archive_rel,
                    text,
                    self.clock.now_iso(),  # type: ignore[attr-defined]
                )
                continue
            target = self.paths.resolve(step.path)
            if target.exists():
                rel = self.paths.relpath(target)
                self.note_indexer.index_note(
                    rel,
                    target.read_text(encoding="utf-8"),
                    self.clock.now_iso(),  # type: ignore[attr-defined]
                )
                written.append(rel)

        # 可选 Git 提交（失败不回滚内容，仅标记未提交）
        git_committed = False
        if self.git_adapter is not None:
            try:
                self.git_adapter.commit_proposal(plan.proposal_id, [s.path for s in plan.steps])
                git_committed = True
            except Exception as exc:
                self.operations.append(
                    "apply",
                    "applied_uncommitted",
                    proposal_id=plan.proposal_id,
                    detail={"git_error": str(exc)},
                )

        # 清理 staging
        import shutil

        shutil.rmtree(self.staging_dir(plan.operation_id), ignore_errors=True)
        return ApplyOutcome(
            proposal_id=plan.proposal_id,
            operation_id=plan.operation_id,
            applied=True,
            written_paths=written,
            git_committed=git_committed,
        )

    # -- 回滚 ---------------------------------------------------------------

    def rollback(self, plan: FilePlan) -> None:
        """用 staging 前像恢复；create 步骤删除目标。"""
        plan.state = "rolling_back"
        self.save_plan(plan)
        sdir = self.staging_dir(plan.operation_id)
        for step in plan.steps:
            target = self.paths.resolve(step.path)
            if step.action == "create":
                if target.exists() and hashing.hash_bytes(target.read_bytes()) == step.result_hash:
                    self.writer.delete(target)
            elif step.before_ref is not None:
                self.writer.write_bytes(target, (sdir / step.before_ref).read_bytes())
            if step.action == "move_to_archive":
                archive = self._archive_target(step.path)
                if archive.exists():
                    self.writer.delete(archive)
        plan.state = "rolled_back"
        self.save_plan(plan)
        assert plan.proposal_id is not None
        self.operations.append(
            "apply",
            "rolled_back",
            proposal_id=plan.proposal_id,
            operation_id=plan.operation_id,
        )
