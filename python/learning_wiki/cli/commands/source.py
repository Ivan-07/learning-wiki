"""lw source * / lw evidence show"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from learning_wiki.cli.runtime import get_context
from learning_wiki.domain import hashing
from learning_wiki.storage import evidence_store

console = Console()
app = typer.Typer(no_args_is_help=True)


@app.command("show")
def show(
    source_id: str = typer.Argument(...),
    vault: Path | None = None,
) -> None:
    ctx = get_context(vault)
    try:
        manifest = ctx.source_repository.load(source_id)
        table = Table(title=manifest.title)
        table.add_column("字段", style="bold")
        table.add_column("值")
        for key in (
            "source_id",
            "source_type",
            "author",
            "canonical_url",
            "captured_at",
            "value_state",
            "active_version",
        ):
            value = getattr(manifest, key)
            if value is not None:
                table.add_row(key, str(value))
        console.print(table)
        versions = Table(title="版本")
        for col in ("version_id", "created_at", "extraction_method", "content_hash", "quality"):
            versions.add_column(col)
        for v in manifest.versions:
            versions.add_row(
                v.version_id,
                v.created_at,
                v.extraction_method,
                v.content_hash[:19] + "…",
                f"{v.extraction_quality or '—'}",
            )
        console.print(versions)
    finally:
        ctx.close()


@app.command("versions")
def versions(
    source_id: str = typer.Argument(...),
    vault: Path | None = None,
) -> None:
    ctx = get_context(vault)
    try:
        manifest = ctx.source_repository.load(source_id)
        for v in manifest.versions:
            marker = " [active]" if v.version_id == manifest.active_version else ""
            console.print(
                f"{v.version_id}{marker}  {v.created_at}  "
                f"{v.extraction_method}  {v.content_hash[:19]}…"
            )
    finally:
        ctx.close()


@app.command("reextract")
def reextract(
    source_id: str = typer.Argument(...),
    dry_run: bool = typer.Option(False, "--dry-run"),
    vault: Path | None = None,
) -> None:
    """重新提取 → 创建新版本（旧版本永不修改）。"""
    ctx = get_context(vault)
    try:
        manifest = ctx.source_repository.load(source_id)
        if dry_run:
            console.print(
                f"[yellow]dry-run:[/yellow] 将为 {source_id} 创建新版本"
                f"（当前 {len(manifest.versions)} 个版本）"
            )
            return
        item = ctx.capture_service.add_to_inbox(
            "url" if manifest.source_type == "web" else "text",
            payload=manifest.canonical_url or "",
        )
        result = ctx.capture_service.process(item.item_id)
        console.print(f"[green]新版本:[/green] {result.source_id} / {result.version_id}")
    except Exception as exc:
        console.print(f"[red]重提取失败:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        ctx.close()


def evidence_show(
    evidence_id: str = typer.Argument(...),
    vault: Path | None = None,
) -> None:
    """显示证据：原文、span_hash 校验、可点击的 vault 路径。"""
    ctx = get_context(vault)
    try:
        loc = ctx.search_service.open_evidence(evidence_id)
        manifest = ctx.source_repository.load(loc.source_id)
        rec = next(v for v in manifest.versions if v.version_id == loc.version_id)
        content = (ctx.source_repository.source_dir(loc.source_id) / rec.content_path).read_text(
            encoding="utf-8"
        )
        ev = next(
            e
            for e in evidence_store.load_evidence_file(
                ctx.source_repository.source_dir(loc.source_id) / rec.evidence_path
            )
            if e.evidence_id == evidence_id
        )
        span_ok = evidence_store.verify_evidence(ev, content)
        newer = (
            f"  [yellow]（存在更新版本 {manifest.active_version}）[/yellow]"
            if loc.newer_version_available
            else ""
        )
        console.print(
            Panel(
                loc.text,
                title=f"{evidence_id}{newer}",
            )
        )
        console.print(f"路径: {loc.path}#^{loc.block_id}")
        console.print(
            "span_hash: [green]一致[/green]" if span_ok else "[red]不一致（锚点漂移或篡改）[/red]"
        )
        console.print(f"span_hash 值: {hashing.span_hash(loc.text)[:19]}…")
        if not span_ok:
            raise typer.Exit(code=1)
    except KeyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    finally:
        ctx.close()
