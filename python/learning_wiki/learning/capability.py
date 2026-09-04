"""能力层级与证据派生（规格《学习机制升级思路》§3.3/§3.6）。

确定性规则（可解释、无推荐模型）：
- feedback successful 且无提示且未开卷 → 活动对应层级；
- successful 但依赖提示 → 降一级（最低 recognize）；
- partial 且无提示 → 降两级（最低 recognize）；
- failed → 不产生能力证据。

能力状态只表示「当前存在的最高证据」，不表示永久掌握。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from learning_wiki.domain.contracts import (
    ACTIVITY_TO_LEVEL,
    CAPABILITY_LEVELS,
    CapabilityLevel,
    Goal,
    LearningEvent,
)

_RECOGNIZE = CAPABILITY_LEVELS.index("recognize")


def level_index(level: str) -> int:
    try:
        return CAPABILITY_LEVELS.index(level)  # type: ignore[arg-type]
    except ValueError as exc:
        raise ValueError(f"未知能力层级: {level}") from exc


def level_at_least(a: str, b: str) -> bool:
    return level_index(a) >= level_index(b)


def level_below(level: str, steps: int, floor: str = "recognize") -> CapabilityLevel:
    idx = max(level_index(level) - steps, level_index(floor))
    return CAPABILITY_LEVELS[idx]


def evidence_level_for(
    activity_type: str,
    result: str,
    hints_used: int | None,
    source_opened: bool | None,
) -> CapabilityLevel | None:
    """一次反馈可产生的最高证据层级；无证据返回 None。"""
    if result != "successful" and result != "partial":
        return None
    base: CapabilityLevel | None = ACTIVITY_TO_LEVEL.get(activity_type)
    if base is None:
        return None
    hinted = (hints_used or 0) > 0 or bool(source_opened)
    if result == "successful":
        return base if not hinted else level_below(base, 1)
    # partial：方向对但不足；无提示降两级，带提示降三级（最低 recognize）
    return level_below(base, 2) if not hinted else level_below(base, 3)


@dataclass
class EvidenceRecord:
    goal_id: str
    capability_id: str
    level: str
    event_id: str
    occurred_at: str
    activity_type: str | None = None
    hint_free: bool = True
    closed_book: bool = True

    @property
    def quality_ok(self) -> bool:
        return self.hint_free and self.closed_book


@dataclass
class GoalAssessment:
    """Goal 完成策略评估结果（§3.6 必要条件的逐条可解释输出）。"""

    achieved: bool = False
    reasons: list[str] = field(default_factory=list)
    capability_levels: dict[str, str] = field(default_factory=dict)


class CapabilityEvidenceService:
    """从事件流派生能力证据与 Goal 完成判定（纯函数式，无写操作）。"""

    def derive_evidence(self, events: list[LearningEvent]) -> list[EvidenceRecord]:
        records: list[EvidenceRecord] = []
        for e in events:
            if e.event_type != "capability_evidence_added":
                continue
            if not (e.goal_id and e.capability_id and e.level):
                continue
            records.append(
                EvidenceRecord(
                    goal_id=e.goal_id,
                    capability_id=e.capability_id,
                    level=e.level,
                    event_id=e.event_id,
                    occurred_at=e.occurred_at,
                    activity_type=None,
                    hint_free=(e.hints_used or 0) == 0,
                    closed_book=not e.source_opened_before_answer,
                )
            )
        return records

    def current_levels(self, events: list[LearningEvent]) -> dict[str, str]:
        """每项能力的当前最高证据层级。"""
        levels: dict[str, str] = {}
        for r in self.derive_evidence(events):
            cur = levels.get(r.capability_id)
            if cur is None or level_index(r.level) > level_index(cur):
                levels[r.capability_id] = r.level
        return levels

    def assess_goal(
        self,
        goal: Goal,
        events: list[LearningEvent],
        now: str,
        note_statuses: dict[str, str] | None = None,
    ) -> GoalAssessment:
        """评估 Goal 是否满足 completion policy（不修改任何状态）。

        - 每个必需能力达到目标层级（证据未超龄、闭卷、不依赖提示）；
        - 真实应用类能力要求用户确认的 successful 应用事件；
        - 相关知识版本未被标记为过期或争议升级。
        """
        note_statuses = note_statuses or {}
        policy = goal.completion_policy
        evidence = [r for r in self.derive_evidence(events) if r.goal_id == goal.goal_id]
        levels = self.current_levels(events)
        reasons: list[str] = []

        def _fresh(r: EvidenceRecord) -> bool:
            if policy.max_evidence_age_days <= 0:
                return True
            return _days_between(r.occurred_at, now) <= policy.max_evidence_age_days

        def _has_sufficient(cap_level_req: str, capability_id: str) -> bool:
            for r in evidence:
                if r.capability_id != capability_id:
                    continue
                if not level_at_least(r.level, cap_level_req):
                    continue
                if not _fresh(r):
                    continue
                if policy.require_closed_book and (not r.closed_book or not r.hint_free):
                    continue  # 高层能力不能完全依赖提示（§3.6）
                if cap_level_req == "real_application" and not r.closed_book:
                    continue  # 真实应用必须用户确认，见下方单独检查
                return True
            return False

        # 真实应用必须有用户确认的 successful 应用事件
        app_events = [
            e
            for e in events
            if e.event_type == "application_outcome_recorded"
            and e.user_assessment == "successful"
            and (e.assessor is not None and e.assessor.type == "user")
        ]

        needs = goal.capabilities if policy.require_all_capabilities else goal.capabilities[:1]
        for cap in needs:
            cur = levels.get(cap.capability_id, "unseen")
            if not level_at_least(cur, cap.required_level):
                reasons.append(
                    f"能力 {cap.capability_id} 当前证据 {cur}，未达要求 {cap.required_level}"
                )
                continue
            if not _has_sufficient(cap.required_level, cap.capability_id):
                reasons.append(
                    f"能力 {cap.capability_id} 的 {cap.required_level} 证据不满足"
                    "（超龄 / 依赖提示 / 非闭卷）"
                )
                continue
            if (
                cap.required_level == "real_application"
                and policy.require_user_confirmed_application
                and not any(e.goal_id == goal.goal_id for e in app_events)
            ):
                reasons.append(
                    f"能力 {cap.capability_id} 要求真实应用，但缺少用户确认的 successful 应用事件"
                )

        # 知识依赖未被标记过期或争议
        for note_id in goal.knowledge_dependencies.note_ids:
            status = note_statuses.get(note_id)
            if status in ("deprecated", "disputed"):
                reasons.append(f"知识依赖 {note_id} 状态为 {status}，需重新验证")

        return GoalAssessment(
            achieved=not reasons,
            reasons=reasons,
            capability_levels=levels,
        )


def _days_between(earlier: str, later: str) -> int:
    """两个 ISO 时间戳之间的天数（证据年龄，粗粒度即可）。"""
    import datetime as dt

    def _parse(value: str) -> dt.datetime:
        try:
            return dt.datetime.fromisoformat(value)
        except ValueError:
            return dt.datetime.max.replace(tzinfo=dt.UTC)

    a, b = _parse(earlier), _parse(later)
    if a > b:
        a, b = b, a
    return (b - a).days
