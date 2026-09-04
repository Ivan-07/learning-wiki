"""lw learning *：知识快照再验证、Views 重建（§6.5/§8.2）。"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from learning_wiki.cli.runtime import get_context

console = Console()
app = typer.Typer(no_args_is_help=True)


@app.command("revalidate")
def revalidate(vault: Path | None = None) -> None:
    """比对知识快照与当前笔记哈希：过期对象标 needs_review、目标进 needs_revalidation。"""
    ctx = get_context(vault)
    try:
        report = ctx.learning_service.snapshots.revalidate()
        console.print(f"检查对象: {report['reviewed']}  过期: {len(report['stale_objects'])}")
        for oid in report["stale_objects"]:
            console.print(f"  [yellow]needs_review[/yellow] {oid}")
            console.print("    复核后: lw learning mark-reviewed <object_id>")
        if not report["stale_objects"]:
            console.print("[green]全部知识快照与当前版本一致[/green]")
        ctx.learning_service.render_views()
    finally:
        ctx.close()


@app.command("mark-reviewed")
def mark_reviewed(
    learning_object_id: str = typer.Argument(...),
    vault: Path | None = None,
) -> None:
    """用户重新验证当前知识版本后，恢复对象为 active。"""
    ctx = get_context(vault)
    try:
        ctx.learning_service.snapshots.mark_reviewed(learning_object_id)
        console.print(f"[green]已复核[/green] {learning_object_id} → active")
    except Exception as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    finally:
        ctx.close()


@app.command("views")
def views(vault: Path | None = None) -> None:
    """重建 40 Learning/Views/（非事实源）。"""
    ctx = get_context(vault)
    try:
        written = ctx.learning_service.render_views()
        for path in written:
            console.print(f"[green]已重建[/green] {path}")
    finally:
        ctx.close()


@app.command("objects")
def objects(
    goal_id: str | None = typer.Option(None, "--goal"),
    vault: Path | None = None,
) -> None:
    """列出学习对象（含 needs_review 状态）。"""
    ctx = get_context(vault)
    try:
        objs = ctx.learning_service.object_repo.list(goal_id)
        table = Table(title=f"学习对象（{len(objs)} 个）")
        for col in ("learning_object_id", "类型", "状态", "能力", "目标", "标题"):
            table.add_column(col)
        for o in objs:
            table.add_row(
                o.learning_object_id,
                o.activity_type,
                o.status,
                o.capability_id or "—",
                o.goal_id or "—",
                o.title[:30],
            )
        console.print(table)
    finally:
        ctx.close()


@app.command("revalidate-tasks")
def revalidate_tasks(vault: Path | None = None) -> None:
    """列出需要重新验证的目标（needs_revalidation）。"""
    ctx = get_context(vault)
    try:
        goals = ctx.learning_service.goals.list("needs_revalidation")
        if not goals:
            console.print("（无待重新验证目标）")
        for g in goals:
            console.print(f"{g.goal_id}  {g.title}")
    finally:
        ctx.close()
