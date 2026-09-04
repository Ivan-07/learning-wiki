"""lw search"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

from learning_wiki.cli.runtime import get_context

console = Console()


def search(
    query: str = typer.Argument(..., help="查询文本 / Source ID / Evidence ID"),
    scope: str = typer.Option("all", "--scope", help="all | sources | wiki"),
    source: str = typer.Option(None, "--source", help="限定来源内检索"),
    trace: bool = typer.Option(False, "--trace", help="显示检索路径"),
    vault: Path | None = None,
) -> None:
    """统一检索 Wiki、来源与允许的个人笔记。"""
    if scope not in ("all", "sources", "wiki"):
        console.print("[red]--scope 必须是 all/sources/wiki[/red]")
        raise typer.Exit(code=1)
    ctx = get_context(vault)
    try:
        response = ctx.search_service.search(query, scope=scope, source_id=source, trace=trace)
        if trace:
            for line in response.trace:
                console.print(f"[dim]· {line}[/dim]")
        if not response.hits:
            console.print(f"[yellow]无结果[/yellow]（query_type={response.query_type}）")
            return
        for hit in response.hits:
            title = hit.title or hit.note_id or ""
            header = f"{hit.kind}: {hit.source_id or hit.note_id}"
            body_lines = [f"标题: {title}"]
            if hit.path:
                body_lines.append(f"路径: {hit.path}")
            if hit.matched_evidence_id:
                body_lines.append(f"证据: {hit.matched_evidence_id}")
                body_lines.append(f"原文: {hit.matched_text}")
            if hit.occurred_at:
                body_lines.append(f"时间: {hit.occurred_at}")
            body_lines.append(f"原因: {hit.match_reason} | 类型: {hit.content_type}")
            console.print(
                Panel(
                    "\n".join(body_lines),
                    title=header,
                    title_align="left",
                )
            )
    finally:
        ctx.close()
