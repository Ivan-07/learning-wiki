"""能力目标服务（规格《学习机制升级思路》§3）。

- Goal 状态机（§3.6）合法转换校验；
- achieved 只能由确定性规则从事件证据派生（completion policy），任何入口
  不允许直接写 achieved 状态；
- 一次目标包含超过五个独立能力时返回拆分建议（§3.7）。
"""

from __future__ import annotations

import sqlite3

from learning_wiki.domain import ids
from learning_wiki.domain.contracts import (
    Goal,
    GoalCapability,
    LearningEvent,
)
from learning_wiki.learning.capability import CapabilityEvidenceService
from learning_wiki.learning.store import EventLog, GoalRepository

# 状态机（§3.6）：主链 + 旁路
_LEGAL_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"active", "abandoned"},
    "active": {"diagnosing", "paused", "abandoned", "achieved", "needs_revalidation"},
    "diagnosing": {"learning", "paused", "abandoned", "achieved", "needs_revalidation"},
    "learning": {
        "transfer_testing",
        "application_pending",
        "achieved",
        "paused",
        "abandoned",
        "needs_revalidation",
    },
    "transfer_testing": {
        "learning",
        "application_pending",
        "achieved",
        "paused",
        "abandoned",
        "needs_revalidation",
    },
    "application_pending": {
        "learning",
        "achieved",
        "paused",
        "abandoned",
        "needs_revalidation",
    },
    "achieved": {"needs_revalidation"},
    "paused": {"active", "abandoned"},
    "abandoned": set(),
    "needs_revalidation": {"active", "achieved"},
}

MAX_CAPABILITIES_PER_GOAL = 5


class GoalStateError(Exception):
    """非法状态转换或非法操作。"""


class GoalService:
    def __init__(
        self,
        repo: GoalRepository,
        events: EventLog,
        clock,
        conn: sqlite3.Connection,
        capability_service: CapabilityEvidenceService,
    ) -> None:
        self.repo = repo
        self.events = events
        self.clock = clock
        self.conn = conn
        self.capability_service = capability_service

    # -- 创建 ---------------------------------------------------------------

    def create(
        self,
        title: str,
        capabilities: list[GoalCapability],
        *,
        target_date: str | None = None,
        importance: str = "medium",
        project_ids: list[str] | None = None,
        motivation: str | None = None,
        note_ids: list[str] | None = None,
        evidence_ids: list[str] | None = None,
        completion_policy=None,
    ) -> tuple[Goal, list[str]]:
        """创建 draft 目标。返回 (goal, 拆分建议)；>5 项能力时给出建议（§3.7）。"""
        if not capabilities:
            raise GoalStateError("Goal 至少包含一项能力")
        advice: list[str] = []
        if len(capabilities) > MAX_CAPABILITIES_PER_GOAL:
            advice = [
                f"目标包含 {len(capabilities)} 项独立能力，"
                f"超过 {MAX_CAPABILITIES_PER_GOAL} 项，建议拆分：",
                "- 每个目标应能在一至四周内验证；",
                "- 每项能力应可被一个明确活动检验；",
                "- 先形成轻量依赖，不为每个知识点创建目标。",
            ]
        from learning_wiki.domain.contracts import (
            CompletionPolicy,
            GoalContext,
            GoalKnowledgeDependencies,
        )

        goal = Goal(
            goal_id=ids.new_goal_id(),
            title=title,
            status="draft",
            created_at=self.clock.now_iso(),
            target_date=target_date,
            importance=importance,  # type: ignore[arg-type]
            context=GoalContext(project_ids=project_ids or [], motivation=motivation),
            capabilities=capabilities,
            knowledge_dependencies=GoalKnowledgeDependencies(
                note_ids=note_ids or [], evidence_ids=evidence_ids or []
            ),
            completion_policy=completion_policy or CompletionPolicy(),
        )
        self.repo.save(goal)
        self.events.append(
            LearningEvent(
                event_id=ids.new_event_id(),
                event_type="goal_created",
                goal_id=goal.goal_id,
                occurred_at=self.clock.now_iso(),
            )
        )
        return goal, advice

    def load(self, goal_id: str) -> Goal:
        return self.repo.load(goal_id)

    def list(self, status: str | None = None) -> list[Goal]:
        goals = self.repo.list()
        if status:
            goals = [g for g in goals if g.status == status]
        return goals

    # -- 状态转换 -----------------------------------------------------------

    def _transition(self, goal_id: str, new_status: str) -> tuple[Goal, bool]:
        goal = self.repo.load(goal_id)
        if new_status == goal.status:
            return goal, False
        allowed = _LEGAL_TRANSITIONS.get(goal.status, set())
        if new_status not in allowed:
            raise GoalStateError(f"非法状态转换: {goal.status} → {new_status}（goal {goal_id}）")
        goal.status = new_status  # type: ignore[assignment]
        self.repo.save(goal)
        return goal, True

    def _emit(self, event_type: str, goal: Goal, **kwargs) -> None:
        self.events.append(
            LearningEvent(
                event_id=ids.new_event_id(),
                event_type=event_type,  # type: ignore[arg-type]
                goal_id=goal.goal_id,
                occurred_at=self.clock.now_iso(),
                **kwargs,
            )
        )

    def activate(self, goal_id: str) -> Goal:
        goal, changed = self._transition(goal_id, "active")
        if changed:
            self._emit("goal_activated", goal)
        return goal

    def pause(self, goal_id: str) -> Goal:
        goal = self.repo.load(goal_id)
        if goal.status in ("achieved", "abandoned"):
            raise GoalStateError(f"目标已 {goal.status}，不能暂停")
        goal, _ = self._transition(goal_id, "paused")
        self._emit("goal_paused", goal)
        return goal

    def abandon(self, goal_id: str) -> Goal:
        goal, _ = self._transition(goal_id, "abandoned")
        self._emit("goal_abandoned", goal)
        return goal

    def mark_needs_revalidation(self, goal_id: str, reason: str) -> Goal:
        goal = self.repo.load(goal_id)
        if goal.status in ("draft", "abandoned"):
            raise GoalStateError(f"目标 {goal.status} 无需重新验证")
        if goal.status != "needs_revalidation":
            goal, _ = self._transition(goal_id, "needs_revalidation")
        self._emit("knowledge_revalidation_required", goal, schedule_reason=reason)
        return goal

    def resume_after_revalidation(self, goal_id: str) -> Goal:
        goal, _ = self._transition(goal_id, "active")
        return goal

    # -- 派生状态（achieved 只能从这里进入） ----------------------------------

    def note_statuses(self, goal: Goal) -> dict[str, str]:
        """从派生库读取知识依赖笔记的当前状态（deprecated/disputed 触发再验证）。"""
        statuses: dict[str, str] = {}
        for note_id in goal.knowledge_dependencies.note_ids:
            row = self.conn.execute(
                "SELECT status FROM wiki_notes WHERE note_id = ?", (note_id,)
            ).fetchone()
            statuses[note_id] = row["status"] if row else "missing"
        return statuses

    def refresh_derived(self, goal_id: str) -> dict:
        """重算能力当前层级与完成判定；满足策略时进入 achieved 并落 goal_achieved。

        返回评估结果（含 capability_levels / reasons）。
        """
        goal = self.repo.load(goal_id)
        goal_events = self.events.events_for_goal(goal_id)
        assessment = self.capability_service.assess_goal(
            goal,
            [e for e in goal_events if e.goal_id == goal_id],
            self.clock.now_iso(),
            note_statuses=self.note_statuses(goal),
        )
        # 同步派生表中的当前层级
        for cap in goal.capabilities:
            level = assessment.capability_levels.get(cap.capability_id, "unseen")
            self.conn.execute(
                "UPDATE goal_capabilities SET current_level=? WHERE goal_id=? AND capability_id=?",
                (level, goal_id, cap.capability_id),
            )
        result = {
            "achieved": assessment.achieved,
            "reasons": assessment.reasons,
            "capability_levels": assessment.capability_levels,
        }
        if assessment.achieved and goal.status in (
            "active",
            "diagnosing",
            "learning",
            "transfer_testing",
            "application_pending",
        ):
            self._transition(goal_id, "achieved")
            self._emit("goal_achieved", goal)
            result["achieved_now"] = True
        return result

    def assess(self, goal_id: str) -> dict:
        """只读评估（不改状态）：当前能力层级与缺口。"""
        goal = self.repo.load(goal_id)
        goal_events = [e for e in self.events.events_for_goal(goal_id)]
        assessment = self.capability_service.assess_goal(
            goal,
            goal_events,
            self.clock.now_iso(),
            note_statuses=self.note_statuses(goal),
        )
        gaps = []
        for cap in goal.capabilities:
            cur = assessment.capability_levels.get(cap.capability_id, "unseen")
            gaps.append(
                {
                    "capability_id": cap.capability_id,
                    "behavior": cap.behavior,
                    "required_level": cap.required_level,
                    "current_level": cur,
                }
            )
        return {
            "goal_id": goal_id,
            "status": goal.status,
            "gaps": gaps,
            "achieved": assessment.achieved,
            "reasons": assessment.reasons,
        }
