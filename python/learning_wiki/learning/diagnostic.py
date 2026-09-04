"""起点诊断（规格《学习机制升级思路》§3.4）。

最小诊断 = 一个开放式自由回忆 + 一个概念辨别 + 一个应用判断，各带
0–100 置信度与是否开卷记录。诊断不生成「通过/失败」总结果，只用于
选择下一步；已有充分证据的低层活动会被跳过（§3.5）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from learning_wiki.domain import ids
from learning_wiki.domain.contracts import Goal, LearningEvent
from learning_wiki.learning.capability import level_index
from learning_wiki.learning.goal_service import GoalService, GoalStateError
from learning_wiki.learning.store import EventLog

DIAGNOSTIC_ACTIVITIES: list[tuple[str, str]] = [
    ("recall", "自由回忆：不看任何材料，说出该能力的核心内容"),
    ("discriminate", "概念辨别：在相似案例中区分相关概念"),
    ("apply", "应用判断：给一个简短情境，判断方法是否适用并说明理由"),
]

# 诊断活动可检验的层级（apply 判断按辨别层级记证据）
_ACTIVITY_TEST_LEVEL = {
    "recall": "recall",
    "discriminate": "discriminate",
    "apply": "discriminate",
}


@dataclass
class DiagnosticPlan:
    goal_id: str
    session_id: str
    activities: list[dict[str, str]] = field(default_factory=list)


class DiagnosticService:
    def __init__(self, goals: GoalService, events: EventLog) -> None:
        self.goals = goals
        self.events = events

    def start(self, goal_id: str) -> DiagnosticPlan:
        """开始诊断：active 目标进入 diagnosing，产出三类活动的确定性框架。

        Agent 负责在此框架上生成具体题面（prompt/rubric）。
        """
        goal = self.goals.load(goal_id)
        if goal.status != "active":
            raise GoalStateError(f"诊断只能从 active 状态开始（当前 {goal.status}）")
        session_id = ids.new_session_id()
        now = self.goals.clock.now_iso()
        self.goals._transition(goal_id, "diagnosing")
        self.events.append(
            LearningEvent(
                event_id=ids.new_event_id(),
                event_type="diagnostic_started",
                session_id=session_id,
                goal_id=goal_id,
                occurred_at=now,
            )
        )
        # 跳过已有充分证据的低层活动（§3.5）
        levels = self._current_levels(goal)
        activities = [
            {"activity_type": t, "description": d}
            for t, d in DIAGNOSTIC_ACTIVITIES
            if self._needs_activity(goal, levels, t)
        ]
        if not activities:
            # 全部跳过：诊断直接完成，进入 learning
            self.complete(
                session_id,
                summary={
                    "skipped_all": True,
                    "reason": "所有低层活动已有充分证据",
                    "recommended_next_activity": "near_transfer",
                },
            )
        return DiagnosticPlan(goal_id=goal_id, session_id=session_id, activities=activities)

    @staticmethod
    def _needs_activity(goal: Goal, levels: dict[str, str], activity_type: str) -> bool:
        """该活动是否仍需练习：存在能力 (当前证据 < 活动可检验层级 ≤ 要求层级)。"""
        test_level = _ACTIVITY_TEST_LEVEL[activity_type]
        for cap in goal.capabilities:
            current = levels.get(cap.capability_id, "unseen")
            if level_index(current) < level_index(test_level) <= level_index(cap.required_level):
                return True
        return False

    def _current_levels(self, goal: Goal) -> dict[str, str]:
        events = self.events.events_for_goal(goal.goal_id)
        return self.goals.capability_service.current_levels(events)

    def complete(self, session_id: str, summary: dict) -> LearningEvent:
        """诊断结束：diagnosing → learning，summary 为可解释的结构化结论（§3.4）。"""
        started = self._find_session_event(session_id, "diagnostic_started")
        goal_id = started.goal_id
        assert goal_id is not None, "diagnostic_started 事件必须携带 goal_id"
        event = LearningEvent(
            event_id=ids.new_event_id(),
            event_type="diagnostic_completed",
            session_id=session_id,
            goal_id=goal_id,
            occurred_at=self.goals.clock.now_iso(),
            diagnostic_summary=summary,
        )
        self.events.append(event)
        goal = self.goals.load(goal_id)
        if goal.status == "diagnosing":
            self.goals._transition(goal_id, "learning")
        return event

    def summarize(self, session_id: str) -> dict:
        """从会话事件推导诊断摘要（确定性，无通过/失败总结果）。"""
        events = self.events.load_session(session_id)
        started = next((e for e in events if e.event_type == "diagnostic_started"), None)
        goal_id = started.goal_id if started else None
        levels: dict[str, str] = {}
        weakest, weakest_level = None, None
        if goal_id:
            goal = self.goals.load(goal_id)
            levels = self._current_levels(goal)
            for cap in goal.capabilities:
                cur = levels.get(cap.capability_id, "unseen")
                if weakest_level is None or level_index(cur) < level_index(weakest_level):
                    weakest, weakest_level = cap.capability_id, cur
        strongest = max(levels.items(), key=lambda kv: level_index(kv[1]), default=None)
        return {
            "goal_id": goal_id,
            "session_id": session_id,
            "strongest_capability": strongest[1] if strongest else None,
            "weakest_capability": weakest,
            "recommended_next_activity": self._recommend(weakest_level),
            "attempt_count": sum(1 for e in events if e.event_type == "attempt_submitted"),
        }

    @staticmethod
    def _recommend(weakest_level: str | None) -> str:
        """最小知识缺口路由（§3.5）：按最弱能力的当前层级推荐下一步。"""
        routing = {
            None: "recall",
            "unseen": "recall",
            "recognize": "recall",
            "recall": "explain",
            "explain": "discriminate",
            "discriminate": "near_transfer",
            "near_transfer": "far_transfer",
            "far_transfer": "real_application",
            "real_application": "delayed_revalidation",
        }
        return routing.get(weakest_level, "recall")

    def _find_session_event(self, session_id: str, event_type: str) -> LearningEvent:
        for e in self.events.load_session(session_id):
            if e.event_type == event_type:
                return e
        raise ValueError(f"会话 {session_id} 中找不到 {event_type} 事件")
