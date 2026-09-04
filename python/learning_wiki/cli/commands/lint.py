"""lw lint"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from learning_wiki.cli.runtime import get_context

console = Console()


def lint(
    vault: Path | None = None,
) -> None:
    """完整性检查：Schema、哈希、证据锚点、块 ID 冲突。"""
    ctx = get_context(vault)
    try:
        report = ctx.integrity_service.lint()
        for issue in report.issues:
            color = "red" if issue.severity == "error" else "yellow"
            console.print(
                f"[{color}]{issue.severity.upper()}[/{color}] "
                f"[bold]{issue.kind}[/bold] {issue.subject}: {issue.message}"
            )
        if report.ok:
            console.print(f"[green]lint 通过[/green]（{len(report.issues)} 条警告）")
        else:
            console.print(f"[red]lint 失败：{len(report.errors)} 个错误[/red]")
            raise typer.Exit(code=1)
    finally:
        ctx.close()
