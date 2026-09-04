"""lw CLI 入口（规格 10.1）。

- 修改型命令支持 ``--dry-run``；
- ``proposal apply``（M2）必须 TTY 确认，非 TTY 拒绝；
- 学习机制升级（M3+）：goal / diagnostic / learn / misconception /
  challenge / application / learning 命令组（学习机制升级思路 §8.2）。
"""

from __future__ import annotations

import typer

from learning_wiki.cli.commands import (
    challenge,
    doctor,
    goal,
    inbox,
    init_vault,
    learn,
    learning,
    lint,
    mcp,
    misconception,
    proposal,
    rebuild,
    search,
    source,
)

app = typer.Typer(
    name="lw",
    help="学习型 LLM Wiki 个人知识库系统",
    no_args_is_help=True,
    add_completion=False,
)


def _evidence_app() -> typer.Typer:
    ev_app = typer.Typer(no_args_is_help=True)
    ev_app.command("show")(source.evidence_show)
    return ev_app


app.command(name="init-vault")(init_vault.init_vault)
app.command(name="doctor")(doctor.doctor)
app.command(name="lint")(lint.lint)
app.command(name="rebuild")(rebuild.rebuild)
app.command(name="search")(search.search)
app.command(name="mcp")(mcp.mcp)
app.add_typer(inbox.app, name="inbox")
app.add_typer(source.app, name="source")
app.add_typer(proposal.app, name="proposal")
app.add_typer(goal.app, name="goal")
app.add_typer(learn.app, name="learn")
app.add_typer(learn.diagnostic, name="diagnostic")
app.add_typer(misconception.app, name="misconception")
app.add_typer(challenge.challenge_app, name="challenge")
app.add_typer(challenge.application_app, name="application")
app.add_typer(learning.app, name="learning")
app.add_typer(_evidence_app(), name="evidence")


if __name__ == "__main__":
    app()
