"""Vault 路径解析与防护。

规则：
- 相对路径一律在 Vault root 内 canonicalize；
- 拒绝 ``..``、绝对路径逃逸、符号链接逃逸（resolved 后必须仍在 root 内）；
- 写入目标必须位于允许目录（allowed roots）之一；
- 目录名映射来自 Config，不假定默认目录名（Agent 规则 1）。
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from learning_wiki.domain.contracts import VaultConfig


class PathForbiddenError(Exception):
    """路径越界、逃逸或不在允许目录内。"""


def _to_posix(rel: str | PurePosixPath) -> PurePosixPath:
    p = PurePosixPath(rel)
    if p.is_absolute():
        raise PathForbiddenError(f"绝对路径不允许: {rel}")
    parts = []
    for part in p.parts:
        if part in ("..",):
            raise PathForbiddenError(f"路径包含 '..': {rel}")
        if part == ".":
            continue
        parts.append(part)
    return PurePosixPath(*parts)


class VaultPaths:
    def __init__(self, root: Path, config: VaultConfig) -> None:
        self.root = root.resolve()
        self.config = config
        self.folders = config.library.folders

    # -- 目录 ---------------------------------------------------------------

    def folder(self, name: str) -> Path:
        rel = getattr(self.folders, name, None)
        if rel is None:
            raise KeyError(f"未知目录名: {name}")
        return self.root / rel

    def system_dir(self) -> Path:
        return self.folder("system")

    def dot_dir(self) -> Path:
        """设备级派生状态目录 .learning-wiki/（不是事实源）。"""
        return self.root / ".learning-wiki"

    def db_path(self) -> Path:
        return self.dot_dir() / "knowledge.db"

    # -- 解析与防护 ---------------------------------------------------------

    def resolve(self, rel: str | PurePosixPath) -> Path:
        """canonicalize 到 root 内的绝对路径；逃逸即抛 PathForbiddenError。"""
        posix = _to_posix(rel)
        candidate = self.root / posix
        resolved = candidate.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise PathForbiddenError(f"路径逃逸出 Vault: {rel}")
        return resolved

    def ensure_inside(self, rel: str | PurePosixPath, allowed: list[Path]) -> Path:
        """解析并校验位于允许目录之一内（写入防护）。"""
        resolved = self.resolve(rel)
        for base in allowed:
            base_resolved = base.resolve()
            if resolved == base_resolved or base_resolved in resolved.parents:
                return resolved
        raise PathForbiddenError(f"路径不在允许目录内: {rel}（允许: {[str(a) for a in allowed]}）")

    def relpath(self, absolute: Path) -> str:
        absolute = Path(absolute).resolve()
        if absolute == self.root:
            return "."
        if self.root not in absolute.parents:
            raise PathForbiddenError(f"路径不在 Vault 内: {absolute}")
        return absolute.relative_to(self.root).as_posix()
