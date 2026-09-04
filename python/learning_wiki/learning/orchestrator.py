"""Learning Orchestrator（规格《学习机制升级思路》§6）。

确定性职责：下一活动路由（固定顺序，§6.2）、最小知识缺口路由（§3.5）、
错误类型→活动映射（§6.4）、会话预算与结束原因（§6.3）、先作答后反馈的
顺序保证。Agent 只负责在指定的活动类型与 Evidence 上生成题面、提示和反馈。

会话状态由重放该 session 的 JSONL 事件推导（无独立会话文件）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from learning_wiki.domain import ids
from learning_wiki.domain.contracts import (
    AssessorInfo,
    LearningEvent,
    LearningObject,
    RubricItem,
)
from learning_wiki.learning.capability import (
    evidence_level_for,
    level_at_least,
    level_index,
)
from learning_wiki.learning.goal_service import GoalService
from learning_wiki.learning.misconception_service import (
    MisconceptionService,
    is_high_confidence_error,
)
from learning_wiki.learning.store import (
    EventLog,
    LearningObjectRepository,
    LearningStoreError,
)

# 会话预算（§6.3）
MAX_ACTIVITIES_PER_SESSION = 3
MAX_HINT_LEVELS = 2
HIGH_PRIORITY_REVIEW_DAYS = 3

# 错误类型 → 干预活动（§6.4：首选 / 第二选择；失败的首选不重复）
ERROR_ACTIVITY_MAP: dict[str, list[str]] = {
    "fact_gap": ["free_recall", "hint_fading"],
    "causal_gap": ["causal_chain_explain", "teach_back"],
    "boundary_error": ["counterexample", "condition_variation"],
    "concept_confusion": ["contrast_case", "interleaved_practice"],
    "proxy_confusion": ["metric_counterexample", "predict_outcome"],
    "overgeneralization": ["scope_judgment", "failure_case"],
    "unsupported_inference": ["evidence_classification", "source_verification"],
    "procedure_error": ["actual_execution", "step_ordering"],
    "application_mismatch": ["scenario_selection", "decision_tree"],
    "outdated_knowledge": ["version_diff", "current_version_apply"],
}

# 干预活动 → 基础活动类型（LearningObject.activity_type）
_INTERVENTION_BASE_TYPE = {
    "free_recall": "recall",
    "hint_fading": "recall",
    "causal_chain_explain": "explain",
    "teach_back": "explain",
    "counterexample": "explain",
    "condition_variation": "explain",
    "contrast_case": "discriminate",
    "interleaved_practice": "discriminate",
    "metric_counterexample": "discriminate",
    "predict_outcome": "discriminate",
    "scope_judgment": "discriminate",
    "failure_case": "explain",
    "evidence_classification": "discriminate",
    "source_verification": "discriminate",
    "actual_execution": "apply",
    "step_ordering": "apply",
    "scenario_selection": "discriminate",
    "decision_tree": "discriminate",
    "version_diff": "discriminate",
    "current_version_apply": "apply",
}

# 最小知识缺口路由（§3.5）：当前证据层级 → 下一活动类型
_GAP_ROUTING = {
    "unseen": "recall",
    "recognize": "recall",
    "recall": "explain",
    "explain": "discriminate",
    "discriminate": "near_transfer",
    "near_transfer": "far_transfer",
    "far_transfer": "real_application",
}

END_REASONS = (
    "evidence_reached",
    "time_budget_reached",
    "needs_source_review",
    "needs_human_judgment",
    "user_stopped",
    "knowledge_conflict",
)


class OrchestratorError(Exception):
    """非法会话操作（伪造 attempt、反馈先于作答、预算超限等）。"""


@dataclass
class NextActivity:
    """下一活动决策（可解释：reason 说明为何推荐）。"""

    session_id: str
    goal_id: str
    activity_type: str
    capability_id: str | None = None
    misconception_id: str | None = None
    intervention: str | None = None
    reason: str = ""
    guidance: str = ""
    end: bool = False
    end_reason: str | None = None
    learning_object_id: str | None = None
    challenge_id: str | None = None
    hint_budget_left: int = MAX_HINT_LEVELS
    activities_left: int = MAX_ACTIVITIES_PER_SESSION


class Orchestrator:
    def __init__(
        self,
        goals: GoalService,
        misconceptions: MisconceptionService,
        objects: LearningObjectRepository,
        events: EventLog,
        clock,
        conn: sqlite3.Connection,
    ) -> None:
        self.goals = goals
        self.misconceptions = misconceptions
        self.objects = objects
        self.events = events
        self.clock = clock
        self.conn = conn

    # ------------------------------------------------------------------
    # 下一活动选择（§6.2 固定顺序，第一版不引入推荐模型）
    # ------------------------------------------------------------------

    def get_next_activity(self, goal_id: str, session_id: str | None = None) -> NextActivity:
        goal = self.goals.load(goal_id)

        def _end(reason: str, why: str, sid: str | None = None) -> NextActivity:
            if sid:
                self.events.append(
                    LearningEvent(
                        event_id=ids.new_event_id(),
                        event_type="session_ended",
                        session_id=sid,
                        goal_id=goal_id,
                        occurred_at=self.clock.now_iso(),
                        end_reason=reason,
                        schedule_reason=why,
                    )
                )
            return NextActivity(
                session_id=sid or "",
                goal_id=goal_id,
                activity_type="none",
                reason=why,
                end=True,
                end_reason=reason,
            )

        # 8. 是否应该暂停或结束（先排除不可学习状态；不产生新会话/复习债务）
        if goal.status in ("paused", "abandoned"):
            return _end("user_stopped", f"目标状态为 {goal.status}，不产生新的学习活动")
        if goal.status == "achieved":
            return _end("evidence_reached", "目标已达成")
        if goal.status == "needs_revalidation":
            return _end(
                "needs_source_review",
                "关联知识版本已更新，请先完成变化辨别任务（lw learning revalidate 查看受影响对象）",
            )

        if session_id is None:
            session_id = ids.new_session_id()

        session_events = self.events.load_session(session_id)
        if not session_events:
            # 新会话落 session_started：attempt 护栏据此校验会话真实性
            self.events.append(
                LearningEvent(
                    event_id=ids.new_event_id(),
                    event_type="session_started",
                    session_id=session_id,
                    goal_id=goal_id,
                    occurred_at=self.clock.now_iso(),
                )
            )
            session_events = self.events.load_session(session_id)

        # 会话预算：本会话已完成的主要活动数
        attempted_objects = {
            e.learning_object_id
            for e in session_events
            if e.event_type == "attempt_submitted" and e.learning_object_id
        }
        activities_left = MAX_ACTIVITIES_PER_SESSION - len(attempted_objects)
        if activities_left <= 0:
            return _end(
                "time_budget_reached",
                "本会话活动预算（3 个）已用完",
                sid=session_id,
            )

        # 2. 是否存在高置信度开放误解
        goal_notes = set(goal.knowledge_dependencies.note_ids)
        for mis in self.misconceptions.high_priority():
            if mis.status != "open":
                continue
            if not (set(mis.concept_ids) & goal_notes):
                continue
            intervention = self._pick_intervention(mis)
            return NextActivity(
                session_id=session_id,
                goal_id=goal_id,
                activity_type=_INTERVENTION_BASE_TYPE[intervention],
                misconception_id=mis.misconception_id,
                intervention=intervention,
                reason=(
                    f"存在高置信度开放误解（{mis.statement.error_type}，"
                    f"最大错误置信度 {mis.observations.max_confidence_when_wrong}），"
                    f"优先安排干预活动 {intervention}"
                ),
                guidance=(
                    "高置信度错误流程（§4.6）：先让用户指出自己可能依赖的假设，"
                    "展示最小反证或边界证据，再进行一次结构相同、表面不同的辨别题。"
                    "不要立即给出完整长答案。"
                ),
                activities_left=activities_left,
            )

        # 3–5. 能力缺口路由（含从解释升级到辨别、近迁移）
        levels = self.goals.capability_service.current_levels(self.events.events_for_goal(goal_id))
        weakest: tuple[str, str, str] | None = None  # (capability_id, current, required)
        for cap in goal.capabilities:
            current = levels.get(cap.capability_id, "unseen")
            if level_at_least(current, cap.required_level):
                continue
            if weakest is None or level_index(current) < level_index(weakest[1]):
                weakest = (cap.capability_id, current, cap.required_level)
        if weakest is not None:
            capability_id, current, required = weakest
            activity = _GAP_ROUTING.get(current, "recall")
            return NextActivity(
                session_id=session_id,
                goal_id=goal_id,
                activity_type=activity,
                capability_id=capability_id,
                reason=(
                    f"能力 {capability_id} 当前证据 {current}，要求 {required}；"
                    f"按最小知识缺口路由安排 {activity}"
                ),
                guidance=self._guidance_for(activity),
                activities_left=activities_left,
            )

        # 6. 是否存在真实项目应用机会
        challenge_rows = self.conn.execute(
            "SELECT challenge_id FROM application_challenges"
            " WHERE goal_id = ? AND status = 'active'",
            (goal_id,),
        ).fetchall()
        if challenge_rows:
            challenge_id = challenge_rows[0]["challenge_id"]
            has_prediction = any(
                e.event_type == "application_prediction_recorded" and e.challenge_id == challenge_id
                for e in self.events.all_events()
            )
            has_outcome = any(
                e.event_type == "application_outcome_recorded" and e.challenge_id == challenge_id
                for e in self.events.all_events()
            )
            if has_prediction and not has_outcome:
                return _end(
                    "needs_human_judgment",
                    f"应用挑战 {challenge_id} 已有应用前预测，等待用户确认应用结果",
                    sid=session_id,
                )
            return NextActivity(
                session_id=session_id,
                goal_id=goal_id,
                activity_type="real_application",
                challenge_id=challenge_id,
                reason="所有能力证据已达标，存在进行中的应用挑战，验证真实应用",
                guidance=(
                    "先记录应用前判断（预测），再执行并等用户确认结果；文件引用不等于应用成功。"
                ),
                activities_left=activities_left,
            )

        # 7. 是否到达延迟复习时间
        due = self.conn.execute(
            "SELECT learning_object_id FROM review_queue WHERE next_review_at <= ?",
            (self.clock.now_iso(),),
        ).fetchone()
        if due is not None:
            return NextActivity(
                session_id=session_id,
                goal_id=goal_id,
                activity_type="recall",
                learning_object_id=due["learning_object_id"],
                reason=f"学习对象 {due['learning_object_id']} 到达延迟复习时间",
                activities_left=activities_left,
            )

        # 证据已足够但目标未转 achieved（例如缺用户确认应用）
        return _end(
            "evidence_reached",
            "当前没有需要安排的活动；能力证据已达标",
            sid=session_id,
        )

    @staticmethod
    def _pick_intervention(mis) -> str:
        """按 §6.4 选择干预活动；已失败（recurred）的首选不重复（§10.2）。"""
        failed = {i.activity for i in mis.intervention_history if i.result == "recurred"}
        for activity in ERROR_ACTIVITY_MAP.get(mis.statement.error_type, ["contrast_case"]):
            if activity not in failed:
                return activity
        # 全部失败：换第二选择仍失败时，返回首选并要求改变方式（不再无限重复同类型）
        return ERROR_ACTIVITY_MAP.get(mis.statement.error_type, ["contrast_case"])[-1]

    @staticmethod
    def _guidance_for(activity: str) -> str:
        guidance = {
            "recall": "闭卷自由回忆；不显示标准答案。",
            "explain": "因果链 / 自我解释 / 为什么问题；rubric 覆盖解释。",
            "discriminate": "对比案例、交错练习、强制选择理由。",
            "near_transfer": "保持结构相近，改变数字/表述/部分条件；验证没有只记住原题答案。",
            "far_transfer": (
                "必须：指定复用的底层结构；改变至少两个表面维度；不在题面中直接提示"
                "目标概念；提供可验证 rubric；引用底层知识 Evidence；说明是否存在多个"
                "合理答案；不把单次开放式回答自动判定为完成（§5.8）。"
            ),
            "real_application": "真实项目应用；必须先记录应用前预测，结果由用户确认。",
            "apply": "情境应用判断。",
        }
        return guidance.get(activity, "")

    # ------------------------------------------------------------------
    # 作答与反馈（先作答后反馈的确定性保证）
    # ------------------------------------------------------------------

    def create_activity_object(
        self,
        *,
        session_id: str,
        goal_id: str,
        activity_type: str,
        title: str,
        prompt: str,
        rubric: list[str] | None = None,
        capability_id: str | None = None,
        evidence_ids: list[str] | None = None,
        knowledge_snapshot=None,
        created_by: str = "user_and_ai",
    ) -> LearningObject:
        """Agent/用户在 Orchestrator 指定的活动类型上生成具体学习对象。"""
        obj = LearningObject(
            learning_object_id=ids.new_learning_object_id(),
            title=title,
            activity_type=activity_type,  # type: ignore[arg-type]
            prompt=prompt,
            rubric=rubric or [],
            evidence_ids=evidence_ids or [],
            goal_id=goal_id,
            capability_id=capability_id,
            knowledge_snapshot=knowledge_snapshot,
            created_by=created_by,  # type: ignore[arg-type]
            created_at=self.clock.now_iso(),
        )
        self.objects.save(obj)
        return obj

    def submit_attempt(
        self,
        *,
        session_id: str,
        learning_object_id: str,
        response: str,
        confidence: int | None = None,
        hints_used: int = 0,
        source_opened: bool = False,
    ) -> LearningEvent:
        """提交作答。护栏：必须有真实会话与回答内容（拒绝伪造 Attempt，§10.3）。"""
        session_events = self.events.load_session(session_id)
        if not session_events:
            raise OrchestratorError(
                f"会话 {session_id} 不存在：作答必须来自经 get_next_activity 建立的会话"
            )
        if not response or not response.strip():
            raise OrchestratorError("回答内容为空，拒绝记录（缺少用户交互上下文）")
        if hints_used > MAX_HINT_LEVELS:
            raise OrchestratorError(f"提示层级超过会话预算（最多 {MAX_HINT_LEVELS} 级）")
        try:
            obj = self.objects.load(learning_object_id)
        except LearningStoreError as exc:
            raise OrchestratorError(str(exc)) from exc
        event = LearningEvent(
            event_id=ids.new_event_id(),
            event_type="attempt_submitted",
            session_id=session_id,
            learning_object_id=learning_object_id,
            goal_id=obj.goal_id,
            capability_id=obj.capability_id,
            occurred_at=self.clock.now_iso(),
            response_before_feedback=response,
            confidence=confidence,
            hints_used=hints_used,
            source_opened_before_answer=source_opened,
        )
        self.events.append(event)
        return event

    def record_feedback(
        self,
        *,
        session_id: str,
        learning_object_id: str,
        result: str,
        rubric_results: list[RubricItem] | None = None,
        feedback: str | None = None,
        assessor: AssessorInfo | None = None,
    ) -> LearningEvent:
        """记录反馈（必须在 attempt 之后）并派生能力证据与复习排程。"""
        session_events = self.events.load_session(session_id)
        attempt = next(
            (
                e
                for e in session_events
                if e.event_type == "attempt_submitted"
                and e.learning_object_id == learning_object_id
            ),
            None,
        )
        if attempt is None:
            raise OrchestratorError(
                f"对象 {learning_object_id} 在会话 {session_id} 中没有作答记录："
                "反馈必须晚于作答落盘（先作答后反馈）"
            )
        try:
            obj = self.objects.load(learning_object_id)
        except LearningStoreError as exc:
            raise OrchestratorError(str(exc)) from exc

        event = LearningEvent(
            event_id=ids.new_event_id(),
            event_type="feedback_recorded",
            session_id=session_id,
            learning_object_id=learning_object_id,
            goal_id=obj.goal_id,
            capability_id=obj.capability_id,
            occurred_at=self.clock.now_iso(),
            result=result,  # type: ignore[arg-type]
            rubric_results=rubric_results or [],
            feedback=feedback,
            assessor=assessor,
            confidence=attempt.confidence,
            hints_used=attempt.hints_used,
            source_opened_before_answer=attempt.source_opened_before_answer,
        )
        self.events.append(event)

        # 能力证据派生（§3.3 确定性规则）
        self._derive_evidence(obj, event, attempt)
        # 复习排程（M3 基线间隔）
        self._schedule_review(obj, event)
        return event

    def _derive_evidence(
        self, obj: LearningObject, feedback: LearningEvent, attempt: LearningEvent
    ) -> None:
        if not (obj.goal_id and obj.capability_id):
            return
        level = evidence_level_for(
            obj.activity_type,
            feedback.result or "failed",
            attempt.hints_used,
            attempt.source_opened_before_answer,
        )
        if level is None:
            return
        self.events.append(
            LearningEvent(
                event_id=ids.new_event_id(),
                event_type="capability_evidence_added",
                session_id=feedback.session_id,
                goal_id=obj.goal_id,
                capability_id=obj.capability_id,
                learning_object_id=obj.learning_object_id,
                occurred_at=self.clock.now_iso(),
                level=level,
                result=feedback.result,
                hints_used=attempt.hints_used,
                source_opened_before_answer=attempt.source_opened_before_answer,
            )
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO capability_evidence(goal_id, capability_id, level,"
            " event_id, occurred_at, activity_type, hint_free, closed_book)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                obj.goal_id,
                obj.capability_id,
                level,
                feedback.event_id,
                feedback.occurred_at,
                obj.activity_type,
                int((attempt.hints_used or 0) == 0),
                int(not attempt.source_opened_before_answer),
            ),
        )
        # 派生目标状态（achieved 只能经确定性规则进入，§3.6）
        self.goals.refresh_derived(obj.goal_id)

    def _schedule_review(self, obj: LearningObject, feedback: LearningEvent) -> None:
        from learning_wiki.domain.contracts import LearningEvent as LE

        success = feedback.result == "successful"
        consecutive = 0
        for e in reversed(self.events.all_events()):
            if (
                e.event_type == "feedback_recorded"
                and e.learning_object_id == obj.learning_object_id
            ):
                if e.result == "successful":
                    consecutive += 1
                else:
                    break
        intervals = [1, 3, 7, 21, 45, 90]
        if is_high_confidence_error(feedback.confidence, feedback.result):
            days = min(HIGH_PRIORITY_REVIEW_DAYS, intervals[0])
            reason = "high_confidence_error: 复查间隔不超过三天（§4.6）"
        elif success:
            idx = min(consecutive - 1, len(intervals) - 1)
            days = intervals[max(idx, 0)]
            reason = "successful: 按间隔递增"
        else:
            days = intervals[0]
            reason = "failed: 明日复查"
        import datetime as dt

        next_at = (
            dt.datetime.fromisoformat(feedback.occurred_at) + dt.timedelta(days=days)
        ).isoformat()
        self.conn.execute(
            "INSERT OR REPLACE INTO review_queue(learning_object_id, next_review_at,"
            " reason, derived_from_event_id) VALUES (?,?,?,?)",
            (obj.learning_object_id, next_at, reason, feedback.event_id),
        )
        self.events.append(
            LE(
                event_id=ids.new_event_id(),
                event_type="review_scheduled",
                session_id=feedback.session_id,
                learning_object_id=obj.learning_object_id,
                goal_id=obj.goal_id,
                occurred_at=self.clock.now_iso(),
                next_review_at=next_at,
                schedule_reason=reason,
            )
        )
