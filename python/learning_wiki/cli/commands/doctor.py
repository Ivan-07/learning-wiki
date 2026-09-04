"""lw doctor"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from learning_wiki.cli.runtime import get_context

console = Console()


def doctor(
    vault: Path | None = None,
    json_output: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    """健康检查：配置、派生库、重建需要、staging 残留与告警。"""
    ctx = get_context(vault)
    try:
        report = ctx.integrity_service.doctor()
        if json_output:
            import json

            console.print_json(json.dumps(report, ensure_ascii=False))
            return
        table = Table(title="lw doctor")
        table.add_column("项目", style="bold")
        table.add_column("值")
        for key, value in report.items():
            style = "red" if key == "needs_rebuild" and value else None
            table.add_row(key, str(value), style=style)
        console.print(table)
        if report["needs_rebuild"]:
            console.print("[yellow]建议运行 `lw rebuild`[/yellow]")
    finally:
        ctx.close()
