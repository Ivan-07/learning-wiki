"""GitAdapter：提案提交的严格边界（规格 7.3）。

- 未经用户同意不初始化仓库（Git 未启用时静默跳过）；
- 不修改 remote / branch / hooks / 用户配置；
- 自动 commit 前，用户已有暂存内容 → 拒绝（GitDirtyError）；
- 只提交提案自己修改的明确路径（pathspec 限定，绝不 git add -A）；
- 提交后核对本次 commit 只包含提案路径。

Git 是可选内容历史层，不是数据库。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitNotEnabledError(Exception):
    pass


class GitDirtyError(Exception):
    """用户已有暂存内容，拒绝自动 commit（规格 13.3.8）。"""


class GitCommitError(Exception):
    pass


@dataclass
class GitStatus:
    enabled: bool
    repo: bool = False
    staged_files: list[str] = None  # type: ignore[assignment]
    dirty_files: list[str] = None  # type: ignore[assignment]


class GitAdapter:
    def __init__(self, root: Path, *, enabled: bool = True) -> None:
        self.root = root.resolve()
        self.enabled = enabled

    # -- 基础 ---------------------------------------------------------------

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        # core.quotepath=false：中文路径不转义，保证 pathspec 比对可靠
        return subprocess.run(
            ["git", "-C", str(self.root), "-c", "core.quotepath=false", *args],
            capture_output=True,
            text=True,
            check=check,
        )

    def is_repo(self) -> bool:
        if not self.enabled:
            return False
        proc = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0 and proc.stdout.strip() == "true"

    def status(self) -> GitStatus:
        if not self.is_repo():
            return GitStatus(enabled=False)
        staged = [
            line[3:]
            for line in self._run("diff", "--cached", "--name-only").stdout.splitlines()
            if line.strip()
        ]
        dirty = [
            line[3:]
            for line in self._run("status", "--porcelain").stdout.splitlines()
            if line.strip() and not line.startswith("??")
        ]
        return GitStatus(enabled=True, repo=True, staged_files=staged, dirty_files=dirty)

    # -- 提交 ---------------------------------------------------------------

    def commit_proposal(self, proposal_id: str, paths: list[str]) -> str:
        """只提交提案路径；返回 commit hash。

        - 仓库不存在 / Git 未启用 → GitNotEnabledError（调用方决定是否忽略）；
        - 用户已有 staged 内容 → GitDirtyError（拒绝，绝不混入）。
        """
        if not self.enabled:
            raise GitNotEnabledError("Git 未启用")
        if not self.is_repo():
            raise GitNotEnabledError(f"不是 Git 仓库: {self.root}")
        status = self.status()
        if status.staged_files:
            raise GitDirtyError(
                "用户已有暂存内容，拒绝自动 commit: " + ", ".join(status.staged_files)
            )
        # pathspec 限定 add（提案明确路径）
        self._run("add", "--", *paths)
        proc = self._run(
            "commit",
            "-m",
            f"lw: apply proposal {proposal_id}",
            "--only",
            "--",
            *paths,
            check=False,
        )
        if proc.returncode != 0:
            raise GitCommitError(f"git commit 失败: {proc.stderr.strip()}")
        commit = self._run("rev-parse", "HEAD").stdout.strip()
        # 核对：本次提交只包含提案路径
        committed = [
            line
            for line in self._run(
                "show", "--name-only", "--pretty=format:", "HEAD"
            ).stdout.splitlines()
            if line.strip()
        ]
        unexpected = [p for p in committed if p not in paths]
        if unexpected:
            raise GitCommitError(f"提交包含了非提案路径（已产生 {commit[:12]}）: {unexpected}")
        return commit
