"""CLI 运行时工具：Vault 根目录解析与 VaultContext 构建。"""

from __future__ import annotations

import os
from pathlib import Path

from learning_wiki.application.context import VaultContext


def resolve_vault(vault: Path | None) -> Path:
    """优先级：显式参数 > LW_VAULT 环境变量 > 当前目录。"""
    if vault is not None:
        return vault
    return Path(os.environ.get("LW_VAULT") or os.getcwd())


def get_context(vault: Path | None) -> VaultContext:
    return VaultContext(resolve_vault(vault))
