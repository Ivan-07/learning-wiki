"""lw proposal *（规格 10.1）。

- ``proposal apply`` 必须显示完整 Diff 并要求 TTY 确认；
- 非 TTY 环境拒绝应用；第一版不提供绕过确认的 ``--yes``。
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from learning_wiki.cli.runtime import get_context
from learning_wiki.proposals.applier import ProposalApplyError
from learning_wiki.proposals.unified_diff import make_diff

console = Console()
app = typer.Typer(no_args_is_help=True)


@app.command("list")
def list_proposals(
    state: str = typer.Option(None, "--state", help="pending | applied | rejected"),
    vault: Path | None = None,
) -> None:
    ctx = get_context(vault)
    try:
        proposals = ctx.proposal_repository.list(state)
        table = Table(title=f"提案（{len(proposals)} 个）")
        table.add_column("proposal_id", no_wrap=True, overflow="crop")
        for col in ("状态", "创建者", "风险", "原因", "操作数"):
            table.add_column(col)
        for p in proposals:
            table.add_row(
                p.proposal_id,
                ctx.proposal_repository.state_of(p.proposal_id),
                p.created_by,
                p.risk,
                p.reason[:40],
                str(len(p.operations)),
            )
        console.print(table)
    finally:
        ctx.close()


@app.command("show")
def show(
    proposal_id: str = typer.Argument(...),
    vault: Path | None = None,
) -> None:
    ctx = get_context(vault)
    try:
        p = ctx.proposal_repository.load(proposal_id)
        triggers = ", ".join(
            f"{t['source_id']}/{t['version_id']}" for t in p.trigger_source_versions
        )
        console.print(
            Panel(
                "\n".join(
                    [
                        f"提案: {p.proposal_id}",
                        f"状态: {ctx.proposal_repository.state_of(proposal_id)}",
                        f"创建者: {p.created_by}    风险: {p.risk}",
                        f"理由: {p.reason}",
                        f"触发来源: {triggers or '（无）'}",
                        f"引用证据: {p.cited_evidence_ids or '（无）'}",
                    ]
                ),
                title="ChangeProposal",
            )
        )
        for op in p.operations:
            console.print(
                f"[bold]{op.operation}[/bold] {op.path}\n"
                f"  base_hash: {(op.base_hash or '—')[:27]}…\n"
                f"  result_hash: {op.result_hash[:27]}…"
            )
        if p.validation:
            v = p.validation
            console.print(
                f"校验: schema={v.schema_valid} paths={v.paths_valid} "
                f"citations={v.citations_valid} base={v.base_hashes_valid} "
                f"result={v.result_hashes_valid}"
            )
            for err in v.errors:
                console.print(f"  [red]-[/red] {err}")
    finally:
        ctx.close()


@app.command("diff")
def diff(
    proposal_id: str = typer.Argument(...),
    vault: Path | None = None,
) -> None:
    """显示提案的完整 Diff（unified diff 仅为审阅表示；权威载荷是后像 + result_hash）。"""
    ctx = get_context(vault)
    try:
        p = ctx.proposal_repository.load(proposal_id)
        for op in p.operations:
            if op.operation == "create":
                console.print(
                    Panel(
                        Syntax(op.content or "", "markdown", line_numbers=True),
                        title=f"create {op.path}",
                    )
                )
                continue
            target = ctx.paths.resolve(op.path)
            base = target.read_text(encoding="utf-8") if target.exists() else ""
            after = ctx.proposal_validator.staged_content(op, [])
            if after is None:
                console.print(f"[red]无法计算后像: {op.path}")
                continue
            text = make_diff(base, after, op.path)
            console.print(
                Panel(Syntax(text, "diff", line_numbers=False), title=f"{op.operation} {op.path}")
            )
    finally:
        ctx.close()


@app.command("validate")
def validate(
    proposal_id: str = typer.Argument(...),
    vault: Path | None = None,
) -> None:
    ctx = get_context(vault)
    try:
        p = ctx.proposal_repository.load(proposal_id)
        v = ctx.proposal_validator.validate(p)
        p.validation = v
        ctx.proposal_repository.save(p)
        if v.errors:
            for err in v.errors:
                console.print(f"[red]✗[/red] {err}")
            raise typer.Exit(code=1)
        console.print("[green]✓ 校验通过[/green]（schema/paths/citations/base_hash/result_hash）")
    finally:
        ctx.close()


@app.command("reject")
def reject(
    proposal_id: str = typer.Argument(...),
    vault: Path | None = None,
) -> None:
    ctx = get_context(vault)
    try:
        if ctx.proposal_repository.state_of(proposal_id) != "pending":
            console.print("[red]只能拒绝 pending 提案[/red]")
            raise typer.Exit(code=1)
        ctx.proposal_repository.move_to(proposal_id, "rejected")
        console.print(f"[green]已拒绝:[/green] {proposal_id}（目标文件未做任何修改）")
    finally:
        ctx.close()


@app.command("apply")
def apply(
    proposal_id: str = typer.Argument(...),
    dry_run: bool = typer.Option(False, "--dry-run"),
    vault: Path | None = None,
) -> None:
    """应用提案：先校验、显示完整 Diff、TTY 确认后安全写入。"""
    if not sys.stdin.isatty() and not dry_run:
        console.print("[red]非 TTY 环境拒绝应用提案（无 --yes 旁路；请用 --dry-run 查看）[/red]")
        raise typer.Exit(code=1)
    ctx = get_context(vault)
    try:
        # 先校验
        p = ctx.proposal_repository.load(proposal_id)
        v = ctx.proposal_validator.validate(p)
        p.validation = v
        ctx.proposal_repository.save(p)
        if v.errors:
            for err in v.errors:
                console.print(f"[red]✗[/red] {err}")
            console.print("[red]校验失败，拒绝应用[/red]")
            raise typer.Exit(code=1)

        # 显示完整 Diff
        _print_diffs(ctx, p)

        if dry_run:
            console.print("[yellow]dry-run: 校验通过，未写入任何文件[/yellow]")
            return

        typer.confirm("应用以上修改？", abort=True)
        outcome = ctx.proposal_applier.apply(proposal_id)
        console.print(
            f"[green]已应用[/green] {outcome.proposal_id}（operation {outcome.operation_id}）"
        )
        for path in outcome.written_paths:
            console.print(f"  写入: {path}")
        if ctx.config.git.enabled:
            git = ctx.git_adapter.status()
            console.print(f"Git: {'仓库' if git.repo else '非仓库（内容已应用，无版本历史）'}")
    except ProposalApplyError as exc:
        console.print(f"[red]应用失败:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        ctx.close()


def _print_diffs(ctx, p) -> None:
    for op in p.operations:
        if op.operation == "create":
            console.print(
                Panel(
                    Syntax(op.content or "", "markdown", line_numbers=True),
                    title=f"create {op.path}",
                )
            )
            continue
        target = ctx.paths.resolve(op.path)
        base = target.read_text(encoding="utf-8") if target.exists() else ""
        after = ctx.proposal_validator.staged_content(op, [])
        if after is None:
            console.print(f"[red]无法计算后像: {op.path}")
            continue
        text = make_diff(base, after, op.path)
        console.print(Panel(Syntax(text, "diff"), title=f"{op.operation} {op.path}"))
