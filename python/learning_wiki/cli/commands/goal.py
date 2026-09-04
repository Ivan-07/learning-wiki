"""lw goal *：能力目标的创建与管理（规格《学习机制升级思路》§3/§8.2）。"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from learning_wiki.cli.runtime import get_context
from learning_wiki.domain.contracts import GoalCapability
from learning_wiki.learning.goal_service import GoalStateError

console = Console()
app = typer.Typer(no_args_is_help=True)


def _parse_capability(spec: str) -> GoalCapability:
    """--capability 'cap_id|行为描述|要求层级'。"""
    parts = spec.split("|")
    if len(parts) < 2:
        raise typer.BadParameter(
            "格式: 'capability_id|行为描述|required_level'（层级可省略，默认 explain）"
        )
    capability_id = parts[0].strip()
    behavior = parts[1].strip()
    required = parts[2].strip() if len(parts) > 2 and parts[2].strip() else "explain"
    return GoalCapability(capability_id=capability_id, behavior=behavior, required_level=required)  # type: ignore[arg-type]


@app.command("create")
def create(
    title: str = typer.Option(..., "--title", help="目标标题"),
    capability: list[str] = typer.Option(
        ..., "--capability", help="能力：'cap_id|行为|required_level'，可重复"
    ),
    target_date: str | None = typer.Option(None, "--target-date"),
    importance: str = typer.Option("medium", "--importance"),
    project_id: list[str] = typer.Option([], "--project-id"),
    note_id: list[str] = typer.Option([], "--note-id", help="知识依赖 Wiki 笔记"),
    evidence_id: list[str] = typer.Option([], "--evidence-id"),
    motivation: str | None = typer.Option(None, "--motivation"),
    activate: bool = typer.Option(False, "--activate", help="创建后直接激活"),
    vault: Path | None = None,
) -> None:
    """创建能力目标（>5 项能力时返回拆分建议）。"""
    ctx = get_context(vault)
    try:
        capabilities = [_parse_capability(c) for c in capability]
        goal, advice = ctx.learning_service.goals.create(
            title,
            capabilities,
            target_date=target_date,
            importance=importance,
            project_ids=list(project_id) or None,
            motivation=motivation,
            note_ids=list(note_id) or None,
            evidence_ids=list(evidence_id) or None,
        )
        if activate:
            ctx.learning_service.goals.activate(goal.goal_id)
        ctx.learning_service.render_views()
        console.print(f"[green]已创建目标:[/green] {goal.goal_id}（状态 {goal.status}）")
        for line in advice:
            console.print(f"[yellow]{line}[/yellow]")
    finally:
        ctx.close()


@app.command("list")
def list_goals(
    status: str | None = typer.Option(None, "--status"),
    vault: Path | None = None,
) -> None:
    ctx = get_context(vault)
    try:
        goals = ctx.learning_service.goals.list(status)
        table = Table(title=f"能力目标（{len(goals)} 个）")
        for col in ("goal_id", "状态", "标题", "能力数", "目标日期"):
            table.add_column(col)
        for g in goals:
            table.add_row(
                g.goal_id, g.status, g.title, str(len(g.capabilities)), g.target_date or "—"
            )
        console.print(table)
    finally:
        ctx.close()


@app.command("show")
def show(goal_id: str = typer.Argument(...), vault: Path | None = None) -> None:
    ctx = get_context(vault)
    try:
        info = ctx.learning_service.goals.assess(goal_id)
        console.print(f"[bold]{goal_id}[/bold]  状态: {info['status']}")
        table = Table(title="能力证据（当前最高证据，非永久掌握）")
        for col in ("capability_id", "行为", "要求层级", "当前证据"):
            table.add_column(col)
        for gap in info["gaps"]:
            table.add_row(
                gap["capability_id"],
                gap["behavior"],
                gap["required_level"],
                gap["current_level"],
            )
        console.print(table)
        if info["achieved"]:
            console.print("[green]满足完成策略[/green]")
        else:
            for r in info["reasons"]:
                console.print(f"[yellow]-[/yellow] {r}")
    finally:
        ctx.close()


@app.command("activate")
def activate(goal_id: str = typer.Argument(...), vault: Path | None = None) -> None:
    ctx = get_context(vault)
    try:
        ctx.learning_service.goals.activate(goal_id)
        ctx.learning_service.render_views()
        console.print(f"[green]已激活[/green] {goal_id}")
    except GoalStateError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    finally:
        ctx.close()


@app.command("pause")
def pause(goal_id: str = typer.Argument(...), vault: Path | None = None) -> None:
    """暂停目标：不再产生复习债务（§10.3-10）。"""
    ctx = get_context(vault)
    try:
        ctx.learning_service.goals.pause(goal_id)
        ctx.learning_service.render_views()
        console.print(f"[green]已暂停[/green] {goal_id}（不再安排新活动）")
    except GoalStateError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    finally:
        ctx.close()


@app.command("abandon")
def abandon(goal_id: str = typer.Argument(...), vault: Path | None = None) -> None:
    ctx = get_context(vault)
    try:
        ctx.learning_service.goals.abandon(goal_id)
        ctx.learning_service.render_views()
        console.print(f"[green]已放弃[/green] {goal_id}")
    except GoalStateError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    finally:
        ctx.close()
