"""lw init-vault"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from learning_wiki.application.vault_setup import VaultSetup

console = Console()


def init_vault(
    vault: Path = typer.Argument(..., help="Vault 根目录"),
) -> None:
    """初始化 Vault：目录、Config、Agent 规则、派生库（幂等，不动既有笔记）。"""
    result = VaultSetup().init(vault)
    console.print(f"[green]Vault 已就绪:[/green] {result.root}")
    if result.created_dirs:
        console.print(f"  新建目录: {', '.join(result.created_dirs)}")
    if result.wrote_files:
        console.print(f"  写入文件: {', '.join(result.wrote_files)}")
    if not result.created_dirs and not result.wrote_files:
        console.print("  （已是初始化状态，未做任何修改）")
    if result.migrations_applied:
        table = Table(title="SQLite migrations")
        table.add_column("version")
        for v in result.migrations_applied:
            table.add_row(str(v))
        console.print(table)
