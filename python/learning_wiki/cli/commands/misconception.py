"""lw misconception *：误解模型管理（§4/§8.2）。"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from learning_wiki.cli.runtime import get_context
from learning_wiki.learning.misconception_service import MisconceptionServiceError

console = Console()
app = typer.Typer(no_args_is_help=True)


@app.command("list")
def list_misconceptions(
    status: str | None = typer.Option(None, "--status"),
    vault: Path | None = None,
) -> None:
    ctx = get_context(vault)
    try:
        misconceptions = ctx.learning_service.misconceptions.repo.list()
        if status:
            misconceptions = [m for m in misconceptions if m.status == status]
        table = Table(title=f"误解（{len(misconceptions)} 个）")
        for col in ("misconception_id", "状态", "错误类型", "出现", "最大错误置信度", "原本信念"):
            table.add_column(col)
        for m in misconceptions:
            table.add_row(
                m.misconception_id,
                m.status,
                m.statement.error_type,
                str(m.observations.occurrences),
                str(m.observations.max_confidence_when_wrong or "—"),
                m.statement.user_belief[:40],
            )
        console.print(table)
    finally:
        ctx.close()


@app.command("show")
def show(misconception_id: str = typer.Argument(...), vault: Path | None = None) -> None:
    ctx = get_context(vault)
    try:
        m = ctx.learning_service.misconceptions.repo.load(misconception_id)
        ev = ctx.learning_service.misconceptions.resolution_evidence(misconception_id)
        console.print(
            Panel(
                "\n".join(
                    [
                        f"状态: {m.status}    错误类型: {m.statement.error_type}",
                        f"原本信念: {m.statement.user_belief}",
                        f"正确表述: {m.statement.correction}",
                        f"相关概念: {', '.join(m.concept_ids) or '—'}",
                        f"纠正 Evidence: {', '.join(m.evidence_ids) or '—'}",
                        f"出现 {m.observations.occurrences} 次；"
                        f"触发情境: {', '.join(m.observations.contexts) or '—'}",
                        f"解决证据: 不同题面正确 {ev['distinct_correct_objects']}/2，"
                        f"不同日期 {ev['distinct_dates']}/2，"
                        f"无提示 {'是' if ev['any_hint_free'] else '否'}",
                    ]
                ),
                title=misconception_id,
            )
        )
        if m.intervention_history:
            console.print("干预历史:")
            for i in m.intervention_history:
                console.print(f"  - {i.activity}: {i.result}")
    finally:
        ctx.close()


@app.command("propose")
def propose(
    concept_id: list[str] = typer.Option(..., "--concept", help="相关概念，可重复"),
    user_belief: str = typer.Option(..., "--belief"),
    correction: str = typer.Option(..., "--correction"),
    error_type: str = typer.Option(..., "--type"),
    evidence_id: list[str] = typer.Option([], "--evidence"),
    context: str | None = typer.Option(None, "--context"),
    confidence: int | None = typer.Option(None, "--confidence"),
    vault: Path | None = None,
) -> None:
    """记录一次错误观察（候选/确认规则见 §4.4；高置信度错误自动确认）。"""
    ctx = get_context(vault)
    try:
        m = ctx.learning_service.misconceptions.observe_error(
            concept_ids=list(concept_id),
            user_belief=user_belief,
            correction=correction,
            error_type=error_type,  # type: ignore[arg-type]
            evidence_ids=list(evidence_id) or None,
            context=context,
            confidence=confidence,
        )
        ctx.learning_service.render_views()
        console.print(f"[green]已记录:[/green] {m.misconception_id}（状态 {m.status}）")
    finally:
        ctx.close()


@app.command("resolve")
def resolve(
    misconception_id: str = typer.Argument(...),
    force: bool = typer.Option(False, "--force", help="用户强制解决"),
    reason: str | None = typer.Option(None, "--reason", help="强制解决必须给理由"),
    vault: Path | None = None,
) -> None:
    """解决误解：默认需满足 §4.7（两个不同题面、无提示、不同日期）。"""
    ctx = get_context(vault)
    try:
        if force and not reason:
            console.print("[red]--force 必须配合 --reason（留痕）[/red]")
            raise typer.Exit(code=1)
        m = ctx.learning_service.misconceptions.resolve(
            misconception_id, forced=force, reason=reason
        )
        ctx.learning_service.render_views()
        console.print(f"[green]已解决[/green] {m.misconception_id}")
    except MisconceptionServiceError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    finally:
        ctx.close()


@app.command("intervene")
def intervene(
    misconception_id: str = typer.Argument(...),
    activity: str = typer.Option(..., "--activity"),
    result: str = typer.Option(..., "--result", help="recurred | improving | resolved"),
    vault: Path | None = None,
) -> None:
    """记录一次干预结果（失败的首选干预不会被路由重复，§10.2）。"""
    ctx = get_context(vault)
    try:
        m = ctx.learning_service.misconceptions.record_intervention(
            misconception_id, activity, result
        )
        ctx.learning_service.render_views()
        console.print(f"[green]已记录干预[/green] {activity}: {result}（状态 {m.status}）")
    finally:
        ctx.close()


@app.command("evidence")
def evidence(misconception_id: str = typer.Argument(...), vault: Path | None = None) -> None:
    """查看解决证据统计（JSON）。"""
    ctx = get_context(vault)
    try:
        console.print_json(
            json.dumps(
                ctx.learning_service.misconceptions.resolution_evidence(misconception_id),
                ensure_ascii=False,
            )
        )
    finally:
        ctx.close()
