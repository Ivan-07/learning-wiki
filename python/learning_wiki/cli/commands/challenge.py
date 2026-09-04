"""lw challenge / lw application：应用挑战与应用前后记录（§5/§8.2）。"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

from learning_wiki.cli.runtime import get_context
from learning_wiki.domain.contracts import AssessorInfo
from learning_wiki.learning.application_service import ApplicationServiceError

console = Console()
challenge_app = typer.Typer(no_args_is_help=True)
application_app = typer.Typer(no_args_is_help=True)


@challenge_app.command("create")
def create(
    goal_id: str = typer.Argument(...),
    capability_id: str = typer.Option(..., "--capability"),
    problem: str = typer.Option(..., "--problem"),
    type: str = typer.Option(
        "real_project", "--type", help="real_project | near_transfer_task | far_transfer_task"
    ),
    project_id: str | None = typer.Option(None, "--project"),
    constraint: list[str] = typer.Option([], "--constraint"),
    reasoning: list[str] = typer.Option([], "--reasoning"),
    note_id: list[str] = typer.Option([], "--note"),
    evidence_id: list[str] = typer.Option([], "--evidence"),
    criterion: list[str] = typer.Option([], "--criterion", help="成功标准，可重复"),
    vault: Path | None = None,
) -> None:
    """创建应用挑战（§5.3）。"""
    ctx = get_context(vault)
    try:
        ch = ctx.learning_service.applications.create_challenge(
            goal_id,
            capability_id,
            problem=problem,
            challenge_type=type,
            project_id=project_id,
            constraints=list(constraint) or None,
            required_reasoning=list(reasoning) or None,
            note_ids=list(note_id) or None,
            evidence_ids=list(evidence_id) or None,
            success_criteria=list(criterion) or None,
        )
        ctx.learning_service.render_views()
        console.print(f"[green]已创建挑战:[/green] {ch.challenge_id}")
        console.print(f"  类型 {ch.type}  问题: {problem}")
        console.print("  下一步：lw application predict（必须先于结果记录）")
    finally:
        ctx.close()


@challenge_app.command("show")
def show(challenge_id: str = typer.Argument(...), vault: Path | None = None) -> None:
    ctx = get_context(vault)
    try:
        ch = ctx.learning_service.applications.load(challenge_id)
        console.print(
            Panel(
                "\n".join(
                    [
                        f"目标: {ch.goal_id}  能力: {ch.capability_id}",
                        f"类型: {ch.type}  状态: {ch.status}",
                        f"问题: {ch.context.problem}",
                        f"约束: {'；'.join(ch.context.constraints) or '—'}",
                        f"成功标准: {'；'.join(ch.success_criteria) or '—'}",
                    ]
                ),
                title=challenge_id,
            )
        )
    finally:
        ctx.close()


@challenge_app.command("list")
def list_challenges(
    goal_id: str | None = typer.Option(None, "--goal"),
    vault: Path | None = None,
) -> None:
    ctx = get_context(vault)
    try:
        challenges = ctx.learning_service.applications.list_challenges(goal_id)
        for ch in challenges:
            console.print(
                f"{ch.challenge_id}  {ch.type:20}  {ch.status:10}  {ch.context.problem[:50]}"
            )
        if not challenges:
            console.print("（无挑战）")
    finally:
        ctx.close()


@challenge_app.command("opportunities")
def opportunities(goal_id: str = typer.Argument(...), vault: Path | None = None) -> None:
    """从 50 Projects 提取候选应用机会（仅建议，不自动认定应用成功）。"""
    ctx = get_context(vault)
    try:
        suggestions = ctx.learning_service.applications.suggest_opportunities(goal_id)
        if not suggestions:
            console.print("（暂无候选应用机会）")
        for s in suggestions:
            body = (
                f"项目: {s['project_path']}\n引用知识: {s['cited_note_ids']}\n\n{s['suggestion']}"
            )
            console.print(Panel(body, title="应用机会"))
    finally:
        ctx.close()


@application_app.command("predict")
def predict(
    challenge_id: str = typer.Argument(...),
    context: str = typer.Option(..., "--context", help="当前问题情境"),
    knowledge: list[str] = typer.Option(..., "--knowledge", help="准备使用的知识，可重复"),
    reasoning: str = typer.Option(..., "--reasoning", help="为什么适用"),
    prediction: str = typer.Option(..., "--prediction", help="对结果的预测"),
    vault: Path | None = None,
) -> None:
    """应用前判断（§5.5：必须先于结果保存，避免事后合理化）。"""
    ctx = get_context(vault)
    try:
        event = ctx.learning_service.applications.record_prediction(
            challenge_id,
            context=context,
            selected_knowledge=list(knowledge),
            reasoning_before_result=reasoning,
            predicted_outcome=prediction,
        )
        console.print(f"[green]应用前预测已记录[/green]（事件 {event.event_id}）")
        console.print("执行应用后，用 lw application record 记录结果（需用户确认）。")
    except ApplicationServiceError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    finally:
        ctx.close()


@application_app.command("record")
def record(
    challenge_id: str = typer.Argument(...),
    action: str = typer.Option(..., "--action", help="实际采取的行动"),
    outcome: str = typer.Option(..., "--outcome", help="结果描述"),
    assessment: str = typer.Option(
        ...,
        "--assessment",
        help="successful | partial_success | failed_execution | wrong_model |"
        " invalid_assumption | knowledge_conflict | inconclusive",
    ),
    lessons: str | None = typer.Option(None, "--lessons"),
    vault: Path | None = None,
) -> None:
    """应用后结果（用户在 TTY 确认；先预测后结果）。"""
    import sys

    if not sys.stdin.isatty():
        console.print("[red]应用结果必须由用户在终端确认（非 TTY 拒绝）[/red]")
        raise typer.Exit(code=1)
    ctx = get_context(vault)
    try:
        typer.confirm("确认以上应用结果由你本人评定？", abort=True)
        event = ctx.learning_service.applications.record_outcome(
            challenge_id,
            action_taken=action,
            outcome=outcome,
            user_assessment=assessment,  # type: ignore[arg-type]
            lessons=lessons,
            assessor=AssessorInfo(type="user", model="cli"),
        )
        route = ctx.learning_service.applications.route_outcome(assessment)
        console.print(f"[green]应用结果已记录[/green]（事件 {event.event_id}）")
        console.print(f"下一步: {route['next_step']}")
        ctx.learning_service.render_views()
    except ApplicationServiceError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    finally:
        ctx.close()


@application_app.command("show")
def show_event(challenge_id: str = typer.Argument(...), vault: Path | None = None) -> None:
    """查看挑战的应用前/后事件。"""
    ctx = get_context(vault)
    try:
        events = [
            e for e in ctx.learning_service.event_log.all_events() if e.challenge_id == challenge_id
        ]
        for e in events:
            data = json.loads(e.model_dump_json())
            console.print_json(json.dumps(data, ensure_ascii=False))
        if not events:
            console.print("（无应用事件）")
    finally:
        ctx.close()
