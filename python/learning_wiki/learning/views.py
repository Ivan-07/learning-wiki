"""Views：从事实源重建的 Markdown 视图（非事实源，可随时重建）。

规格《学习机制升级思路》§7.1：Views/ 中的 Markdown 不是事实源，
由每次状态变更后与本模块重建。
"""

from __future__ import annotations

from learning_wiki.learning.capability import level_index
from learning_wiki.learning.store import (
    ChallengeRepository,
    EventLog,
    GoalRepository,
    MisconceptionRepository,
)

_HEADER = """---
lw_view: true
regeneratable: true
---

"""


class ViewsRenderer:
    def __init__(
        self,
        goals: GoalRepository,
        misconceptions: MisconceptionRepository,
        challenges: ChallengeRepository,
        events: EventLog,
    ) -> None:
        self.goals = goals
        self.misconceptions = misconceptions
        self.challenges = challenges
        self.events = events

    def render_all(self) -> list[str]:
        written = []
        written.append(self._write("Active Goals.md", self._active_goals()))
        written.append(self._write("Open Misconceptions.md", self._open_misconceptions()))
        written.append(
            self._write("Application Opportunities.md", self._application_opportunities())
        )
        return written

    def _views_dir(self):
        return self.goals.paths.folder("learning") / "Views"

    def _write(self, name: str, content: str) -> str:
        target = self._views_dir() / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_HEADER + content, encoding="utf-8")
        return self.goals.paths.relpath(target)

    def _levels(self, goal) -> dict[str, str]:
        events = self.events.events_for_goal(goal.goal_id)
        levels: dict[str, str] = {}
        for e in events:
            if e.event_type == "capability_evidence_added" and e.capability_id and e.level:
                cur = levels.get(e.capability_id)
                if cur is None or level_index(e.level) > level_index(cur):
                    levels[e.capability_id] = e.level
        return levels

    def _active_goals(self) -> str:
        lines = ["# 进行中的能力目标", ""]
        actives = [g for g in self.goals.list() if g.status not in ("achieved", "abandoned")]
        if not actives:
            lines.append("（无）")
            return "\n".join(lines) + "\n"
        for goal in actives:
            lines.append(f"## {goal.title}")
            lines.append(f"- 状态：`{goal.status}`  目标 ID：`{goal.goal_id}`")
            if goal.target_date:
                lines.append(f"- 目标日期：{goal.target_date}")
            levels = self._levels(goal)
            lines.append("- 能力证据：")
            for cap in goal.capabilities:
                cur = levels.get(cap.capability_id, "unseen")
                mark = "✅" if level_index(cur) >= level_index(cap.required_level) else "⬜"
                lines.append(
                    f"  - {mark} `{cap.capability_id}`（{cur} / 要求 {cap.required_level}）"
                    f" — {cap.behavior}"
                )
            lines.append("")
        return "\n".join(lines) + "\n"

    def _open_misconceptions(self) -> str:
        lines = ["# 开放误解", ""]
        opens = [
            m
            for m in self.misconceptions.list()
            if m.status in ("candidate", "open", "improving", "recurring")
        ]
        if not opens:
            lines.append("（无）")
            return "\n".join(lines) + "\n"
        for mis in opens:
            lines.append(f"## {mis.misconception_id}（{mis.status}）")
            lines.append(f"- 原本信念：{mis.statement.user_belief}")
            lines.append(f"- 正确表述：{mis.statement.correction}")
            lines.append(f"- 错误类型：`{mis.statement.error_type}`")
            obs = mis.observations
            max_conf = (
                str(obs.max_confidence_when_wrong)
                if obs.max_confidence_when_wrong is not None
                else "—"
            )
            lines.append(f"- 出现 {obs.occurrences} 次；错误时最大置信度 {max_conf}")
            if obs.contexts:
                lines.append(f"- 触发情境：{', '.join(obs.contexts)}")
            if mis.intervention_history:
                tried = "、".join(f"{i.activity}({i.result})" for i in mis.intervention_history)
                lines.append(f"- 干预历史：{tried}")
            lines.append("")
        return "\n".join(lines) + "\n"

    def _application_opportunities(self) -> str:
        lines = ["# 应用挑战与机会", ""]
        challenges = self.challenges.list()
        if not challenges:
            lines.append("（无）")
            return "\n".join(lines) + "\n"
        for ch in challenges:
            lines.append(f"## {ch.challenge_id}（{ch.type}，{ch.status}）")
            lines.append(f"- 问题：{ch.context.problem}")
            if ch.context.constraints:
                lines.append(f"- 约束：{'；'.join(ch.context.constraints)}")
            if ch.success_criteria:
                lines.append("- 成功标准：")
                for s in ch.success_criteria:
                    lines.append(f"  - {s}")
            lines.append("")
        return "\n".join(lines) + "\n"
