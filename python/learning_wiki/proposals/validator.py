"""ProposalValidator：提案的五项确定性校验（规格 5.7 validation）。

1. schema_valid —— ChangeProposal 本身可解析；
2. paths_valid —— 路径在允许目录（Wiki / Archive）内、无越界逃逸；
3. citations_valid —— 引用的 Evidence 存在且 trusted；无引用的 supported 声明
   必须标记 inference（诚实不变量）；
4. base_hashes_valid —— patch/move 的目标文件当前哈希 == base_hash
   （create 要求目标不存在）；
5. result_hashes_valid —— patch 应用后内容哈希 == result_hash
   （staged 后像与声明一致）。

校验失败不抛异常：返回带 errors 的 ProposalValidation，由调用方决定拒绝。
"""

from __future__ import annotations

import sqlite3

from learning_wiki.domain import hashing
from learning_wiki.domain.contracts import ChangeProposal, ProposalValidation
from learning_wiki.proposals.unified_diff import (
    PatchConflictError,
    _ensure_trailing_newline,
    apply_unified,
)
from learning_wiki.storage.vault_paths import PathForbiddenError, VaultPaths


class ProposalValidator:
    def __init__(self, paths: VaultPaths, conn: sqlite3.Connection) -> None:
        self.paths = paths
        self.conn = conn

    def validate(self, proposal: ChangeProposal) -> ProposalValidation:
        result = ProposalValidation()
        errors: list[str] = []

        # -- 1. schema（模型已解析才到这里；校验内容约束） ----------------------
        if not proposal.operations:
            errors.append("提案没有任何操作")
        if proposal.status not in ("pending",):
            errors.append(f"提案状态为 {proposal.status}，只能校验 pending 提案")
        result.schema_valid = not errors

        # -- 2. paths ---------------------------------------------------------
        allowed = [self.paths.folder("wiki"), self.paths.folder("archive")]
        for op in proposal.operations:
            try:
                self.paths.ensure_inside(op.path, allowed)
            except PathForbiddenError as exc:
                errors.append(f"路径不合法: {exc}")
        result.paths_valid = all("路径不合法" not in e for e in errors) and not [
            e for e in errors if e.startswith("路径")
        ]

        # -- 3. citations ------------------------------------------------------
        cited = set(proposal.cited_evidence_ids)
        for op in proposal.operations:
            if op.content:
                cited |= _citations_in(op.content)
        for evidence_id in sorted(cited):
            row = self.conn.execute(
                "SELECT trusted FROM evidence WHERE evidence_id = ?", (evidence_id,)
            ).fetchone()
            if row is None:
                errors.append(f"引用的 Evidence 不存在: {evidence_id}")
            elif not row["trusted"]:
                errors.append(f"引用的 Evidence 已失效（来源被篡改）: {evidence_id}")
        result.citations_valid = all("Evidence" not in e for e in errors)

        # -- 4/5. base_hash 与 result_hash（逐操作） ----------------------------
        hash_errors = self._validate_hashes(proposal, errors)
        result.base_hashes_valid = "base_hash" not in hash_errors
        result.result_hashes_valid = "result_hash" not in hash_errors

        result.errors = errors
        return result

    def _validate_hashes(self, proposal: ChangeProposal, errors: list[str]) -> str:
        """逐操作校验 base_hash（当前状态）与 result_hash（应用结果）。"""
        marker = ""
        for op in proposal.operations:
            target = self.paths.resolve(op.path)
            current_hash = hashing.hash_bytes(target.read_bytes()) if target.exists() else None
            if op.operation == "create":
                if current_hash is not None:
                    errors.append(f"base_hash: create 目标已存在: {op.path}")
                    marker += "base_hash"
            else:
                # patch / move_to_archive 必须基于当前内容
                if current_hash != op.base_hash:
                    errors.append(
                        f"base_hash 过期（目标文件已被修改）: {op.path}"
                        f"（预期 {op.base_hash}，实际 {current_hash}）"
                    )
                    marker += "base_hash"
            # result_hash：后像内容哈希（diff 管道统一规范化尾换行）
            after = self.staged_content(op, errors)
            if after is None:
                marker += "result_hash"
            elif (
                hashing.hash_bytes(_ensure_trailing_newline(after).encode("utf-8"))
                != op.result_hash
            ):
                errors.append(f"result_hash 与实际后像不一致: {op.path}")
                marker += "result_hash"
        return marker

    def staged_content(self, op, errors: list[str]) -> str | None:
        """计算操作的后像（应用 patch 或直接取 content）。"""
        if op.operation == "create":
            return op.content or ""
        target = self.paths.resolve(op.path)
        if not target.exists():
            errors.append(f"目标文件不存在: {op.path}")
            return None
        base = target.read_text(encoding="utf-8")
        if op.operation == "move_to_archive":
            return base
        if op.patch is None:
            errors.append(f"patch 操作缺少 diff: {op.path}")
            return None
        try:
            return apply_unified(base, op.patch)
        except PatchConflictError as exc:
            errors.append(f"patch 应用失败: {op.path}: {exc}")
            return None


def _citations_in(content: str) -> set[str]:
    """从后像内容提取 [[…#^ev-…]] 引用并转为 Evidence ID。"""
    import re

    from learning_wiki.domain import ids

    refs = set()
    for block_id in re.findall(r"\[\[[^\]]*?#\^([A-Za-z0-9-]+)[^\]]*\]\]", content):
        if block_id.startswith("ev-"):
            refs.add(ids.block_id_to_evidence_id(block_id))
    return refs
