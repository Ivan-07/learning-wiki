"""lw inbox *"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from learning_wiki.cli.runtime import get_context

console = Console()
app = typer.Typer(no_args_is_help=True)


@app.command("add")
def add(
    text: str = typer.Option(None, "--text", help="粘贴文字"),
    file: Path = typer.Option(None, "--file", help="Markdown/TXT 文件路径"),
    url: str = typer.Option(None, "--url", help="公开网页 URL"),
    why: str = typer.Option(None, "--why", help="为什么保存（可选）"),
    vault: Path | None = None,
) -> None:
    """加入 Inbox（不触发网络抓取，立即返回）。"""
    provided = sum(x is not None for x in (text, file, url))
    if provided != 1:
        console.print("[red]--text / --file / --url 必须且只能提供一个[/red]")
        raise typer.Exit(code=1)
    ctx = get_context(vault)
    try:
        if text is not None:
            item = ctx.capture_service.add_to_inbox("text", payload=text, why=why)
        elif file is not None:
            item = ctx.capture_service.add_to_inbox(
                "file",
                payload_path=str(file.expanduser().resolve()),
                why=why,
            )
        else:
            item = ctx.capture_service.add_to_inbox("url", payload=url, why=why)
        console.print(f"[green]已加入 Inbox:[/green] {item.item_id}")
        console.print("处理: " + typer.style(f"lw inbox process {item.item_id}", bold=True))
    finally:
        ctx.close()


@app.command("list")
def list_items(
    state: str = typer.Option(None, "--state", help="按状态过滤"),
    vault: Path | None = None,
) -> None:
    ctx = get_context(vault)
    try:
        items = ctx.capture_service.list_inbox(state)
        table = Table(title=f"Inbox（{len(items)} 项）")
        # item_id 不换行不截断（要能直接复制去 process）
        table.add_column("item_id", no_wrap=True, overflow="crop")
        for col in ("类型", "状态", "标题/内容", "错误"):
            table.add_column(col)
        for i in items:
            preview = (i.payload or i.payload_path or "")[:60]
            table.add_row(i.item_id, i.input_type, i.state, preview, i.error or "")
        console.print(table)
    finally:
        ctx.close()


@app.command("process")
def process(
    item_id: str = typer.Argument(...),
    vault: Path | None = None,
) -> None:
    """处理 Inbox 项：提取、建版本、建证据、更新索引。"""
    ctx = get_context(vault)
    try:
        result = ctx.capture_service.process(item_id)
        dup = "（重复：复用已有版本）" if result.duplicate else ""
        console.print(
            f"[green]已捕获{dup}[/green] {result.source_id} / {result.version_id} "
            f"—— {result.title}（{result.evidence_count} 证据块）"
        )
    except Exception as exc:
        console.print(f"[red]处理失败:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        ctx.close()


@app.command("classify")
def classify(
    item_id: str = typer.Argument(...),
    value_state: str = typer.Argument(..., help="reference | learn | apply | discard"),
    vault: Path | None = None,
) -> None:
    """价值分类（Reference / Learn / Apply / Discard）。"""
    if value_state not in ("reference", "learn", "apply", "discard"):
        console.print("[red]value_state 必须是 reference/learn/apply/discard[/red]")
        raise typer.Exit(code=1)
    ctx = get_context(vault)
    try:
        item = ctx.capture_service.classify(item_id, value_state)
        console.print(f"[green]已分类:[/green] {item.item_id} → {value_state}")
    finally:
        ctx.close()
