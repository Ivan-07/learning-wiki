"""lw diagnostic / lw learn：诊断、下一活动、作答与反馈（§3.4/§6/§8.2）。"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

from learning_wiki.cli.runtime import get_context
from learning_wiki.learning.goal_service import GoalStateError
from learning_wiki.learning.orchestrator import OrchestratorError

console = Console()
app = typer.Typer(no_args_is_help=True)
diagnostic = typer.Typer(no_args_is_help=True)
app.add_typer(diagnostic, name="diagnostic")


@diagnostic.command("start")
def diagnostic_start(goal_id: str = typer.Argument(...), vault: Path | None = None) -> None:
    """起点诊断：定位最小知识缺口；已有充分证据的活动会被跳过。"""
    ctx = get_context(vault)
    try:
        plan = ctx.learning_service.diagnostics.start(goal_id)
        console.print(f"诊断会话: [bold]{plan.session_id}[/bold]")
        if not plan.activities:
            console.print("所有低层活动已有充分证据，诊断已直接完成。")
        for act in plan.activities:
            console.print(f"  - [cyan]{act['activity_type']}[/cyan]: {act['description']}")
        console.print("Agent 按以上框架生成题面（lw learn object-create），用户闭卷作答。")
    except GoalStateError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    finally:
        ctx.close()


@diagnostic.command("complete")
def diagnostic_complete(
    session_id: str = typer.Argument(...),
    summary: str = typer.Option("{}", "--summary", help="诊断摘要 JSON"),
    vault: Path | None = None,
) -> None:
    """结束诊断（diagnosing → learning）。"""
    ctx = get_context(vault)
    try:
        try:
            data = json.loads(summary)
        except json.JSONDecodeError as exc:
            raise typer.BadParameter(f"--summary 不是合法 JSON: {exc}") from exc
        event = ctx.learning_service.diagnostics.complete(session_id, data)
        console.print(f"[green]诊断完成[/green]（事件 {event.event_id}，目标进入 learning）")
        console.print(
            json.dumps(
                ctx.learning_service.diagnostics.summarize(session_id), ensure_ascii=False, indent=2
            )
        )
    finally:
        ctx.close()


@diagnostic.command("summary")
def diagnostic_summary(session_id: str = typer.Argument(...), vault: Path | None = None) -> None:
    """查看诊断摘要（无通过/失败总结果，只用于选择下一步）。"""
    ctx = get_context(vault)
    try:
        console.print_json(
            json.dumps(ctx.learning_service.diagnostics.summarize(session_id), ensure_ascii=False)
        )
    finally:
        ctx.close()


@app.command("next")
def learn_next(
    goal_id: str = typer.Argument(...),
    session_id: str | None = typer.Option(None, "--session"),
    vault: Path | None = None,
) -> None:
    """下一活动（确定性路由，§6.2 固定顺序；reason 说明为何推荐）。"""
    ctx = get_context(vault)
    try:
        act = ctx.learning_service.orchestrator.get_next_activity(goal_id, session_id)
        if act.end:
            console.print(
                Panel(
                    f"结束原因: [bold]{act.end_reason}[/bold]\n{act.reason}",
                    title="本次会话结束",
                )
            )
            return
        console.print(
            Panel(
                "\n".join(
                    [
                        f"会话: {act.session_id}",
                        f"活动类型: [cyan]{act.activity_type}[/cyan]",
                        f"能力: {act.capability_id or '—'}"
                        + (f"（误解干预: {act.intervention}）" if act.intervention else ""),
                        f"为何推荐: {act.reason}",
                        f"剩余活动预算: {act.activities_left}",
                        f"生成约束: {act.guidance}",
                    ]
                ),
                title="下一活动",
            )
        )
    finally:
        ctx.close()


@app.command("object-create")
def object_create(
    session_id: str = typer.Option(..., "--session"),
    goal_id: str = typer.Option(..., "--goal"),
    title: str = typer.Option(..., "--title"),
    prompt: str = typer.Option(..., "--prompt", help="题面（闭卷作答用）"),
    activity_type: str = typer.Option("explain", "--type"),
    capability_id: str | None = typer.Option(None, "--capability"),
    rubric: list[str] = typer.Option([], "--rubric"),
    note_id: str | None = typer.Option(None, "--note", help="绑定 Wiki 笔记（知识快照）"),
    vault: Path | None = None,
) -> None:
    """在 Orchestrator 指定的活动类型上创建具体学习对象（含知识快照）。"""
    ctx = get_context(vault)
    try:
        snapshot = None
        if note_id:
            from learning_wiki.learning.snapshots import build_snapshot

            snapshot = build_snapshot(ctx.conn, note_id)
        obj = ctx.learning_service.orchestrator.create_activity_object(
            session_id=session_id,
            goal_id=goal_id,
            activity_type=activity_type,
            title=title,
            prompt=prompt,
            rubric=list(rubric) or None,
            capability_id=capability_id,
            knowledge_snapshot=snapshot,
        )
        console.print(f"[green]已创建学习对象:[/green] {obj.learning_object_id}")
        console.print(f"  活动: {obj.activity_type}  能力: {obj.capability_id or '—'}")
        if snapshot:
            console.print(f"  知识快照: {snapshot.note_id} @ {snapshot.note_hash[:16]}…")
    finally:
        ctx.close()


@app.command("attempt")
def learn_attempt(
    session_id: str = typer.Argument(...),
    learning_object_id: str = typer.Argument(...),
    response: str = typer.Option(..., "--response", help="闭卷回答内容"),
    confidence: int | None = typer.Option(None, "--confidence", help="0–100"),
    hints: int = typer.Option(0, "--hints"),
    source_opened: bool = typer.Option(False, "--source-opened"),
    vault: Path | None = None,
) -> None:
    """提交作答（必须先于反馈；回答内容为空即拒绝）。"""
    ctx = get_context(vault)
    try:
        event = ctx.learning_service.orchestrator.submit_attempt(
            session_id=session_id,
            learning_object_id=learning_object_id,
            response=response,
            confidence=confidence,
            hints_used=hints,
            source_opened=source_opened,
        )
        console.print(f"[green]作答已记录[/green]（事件 {event.event_id}）")
    except OrchestratorError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    finally:
        ctx.close()


@app.command("feedback")
def learn_feedback(
    session_id: str = typer.Argument(...),
    learning_object_id: str = typer.Argument(...),
    result: str = typer.Option(..., "--result", help="successful | partial | failed"),
    rubric: str = typer.Option("[]", "--rubric", help='JSON: [{"item":"…","met":true}]'),
    feedback: str | None = typer.Option(None, "--feedback"),
    vault: Path | None = None,
) -> None:
    """记录反馈（必须在作答之后；派生能力证据与复习排程）。"""
    from learning_wiki.domain.contracts import RubricItem

    ctx = get_context(vault)
    try:
        try:
            rubric_items = [RubricItem.model_validate(r) for r in json.loads(rubric)]
        except (json.JSONDecodeError, Exception) as exc:
            raise typer.BadParameter(f"--rubric 解析失败: {exc}") from exc
        try:
            event = ctx.learning_service.orchestrator.record_feedback(
                session_id=session_id,
                learning_object_id=learning_object_id,
                result=result,
                rubric_results=rubric_items,
                feedback=feedback,
            )
        except OrchestratorError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        console.print(f"[green]反馈已记录[/green]（事件 {event.event_id}）")
        if event.next_review_at:
            console.print(f"  下次复习: {event.next_review_at}")
        if learning_object_id:
            row = ctx.conn.execute(
                "SELECT goal_id FROM learning_objects WHERE learning_object_id = ?",
                (learning_object_id,),
            ).fetchone()
            if row and row["goal_id"]:
                assessment = ctx.learning_service.goals.refresh_derived(row["goal_id"])
                if assessment.get("achieved_now"):
                    console.print("[bold green]目标已达成（achieved，由完成策略派生）[/bold green]")
                ctx.learning_service.render_views()
    finally:
        ctx.close()
