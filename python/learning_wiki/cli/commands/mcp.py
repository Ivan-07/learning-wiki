"""lw mcp：启动 MCP server（stdio），供 Claudian / Claude Code 等接入。"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

console = Console()


def mcp(
    vault: Path | None = None,
) -> None:
    """启动 stdio MCP server（Agent 工具集：检索/证据/提案，不含直接应用）。"""
    from learning_wiki.cli.runtime import resolve_vault
    from learning_wiki.mcp.server import main as mcp_main

    console.print(f"[dim]lw MCP server 启动（vault: {resolve_vault(vault)}）…[/dim]")
    mcp_main(resolve_vault(vault))
