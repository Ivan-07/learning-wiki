"""lw rebuild"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from learning_wiki.cli.runtime import get_context

console = Console()


def rebuild(
    vault: Path | None = None,
) -> None:
    """删除派生状态并从文件事实源重建全部索引。"""
    ctx = get_context(vault)
    try:
        with console.status("重建中…"):

            def progress(stage: str, fraction: float) -> None:
                console.status(f"重建中… {stage} {fraction:.0%}")

            report = ctx.rebuild_service.rebuild(progress=progress)
        console.print(
            f"[green]重建完成:[/green] "
            f"{report.sources} 来源 / {report.versions} 版本 / "
            f"{report.evidence} 证据 / {report.notes} Wiki 页 / "
            f"{report.learning_objects} 学习对象 / {report.learning_events} 学习事件 / "
            f"{report.goals} 目标 / {report.misconceptions} 误解 / "
            f"{report.challenges} 挑战 / "
            f"{report.proposals} 提案 / {report.operations} 操作记录"
        )
        for err in report.errors:
            console.print(f"[yellow]跳过[/yellow] {err}")
        # Views 是派生物，rebuild 后重建（非事实源）
        for path in ctx.learning_service.render_views():
            console.print(f"[green]视图重建[/green] {path}")
    finally:
        ctx.close()
