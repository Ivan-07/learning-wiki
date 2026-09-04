"""MCP 学习类工具（规格《学习机制升级思路》§8.1）。

权限约束（护栏）：
- Agent 可以提议目标拆分和误解；
- Agent 不能伪造用户回答（attempt 必须来自真实会话且带非空回答）；
- Agent 不能自动确认真实应用成功（outcome 的 assessor 必须是 user）；
- Agent 不能直接把 Goal 标记为 achieved——achieved 由确定性规则派生；
- 学习结果只能创建知识更新候选，不能直接修改 Wiki。
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import MCPServer

from learning_wiki.domain.contracts import (
    AssessorInfo,
    GoalCapability,
    RubricItem,
)
from learning_wiki.learning.application_service import ApplicationServiceError
from learning_wiki.learning.goal_service import GoalStateError
from learning_wiki.learning.misconception_service import MisconceptionServiceError
from learning_wiki.learning.orchestrator import OrchestratorError


def register_learning_tools(server: MCPServer, ctx) -> None:
    """在 MCP server 上注册 15 个学习类工具。"""

    def _cap(spec: dict[str, Any]) -> GoalCapability:
        return GoalCapability(
            capability_id=spec["capability_id"],
            behavior=spec.get("behavior", ""),
            required_level=spec.get("required_level", "explain"),
        )

    def _err(exc: Exception) -> dict[str, Any]:
        return {"error": str(exc)}

    # ---------------------------------------------------------------- 目标 --

    @server.tool(name="lw_create_goal")
    def lw_create_goal(
        title: str,
        capabilities: list[dict[str, Any]],
        target_date: str | None = None,
        importance: str = "medium",
        project_ids: list[str] | None = None,
        motivation: str | None = None,
        note_ids: list[str] | None = None,
        evidence_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """创建能力目标（draft 状态）。超过 5 项能力时返回拆分建议。

        capabilities 每项：{capability_id, behavior, required_level}。
        required_level ∈ recognize/recall/explain/discriminate/near_transfer/
        far_transfer/real_application。
        """
        try:
            goal, advice = ctx.learning_service.goals.create(
                title,
                [_cap(c) for c in capabilities],
                target_date=target_date,
                importance=importance,
                project_ids=project_ids,
                motivation=motivation,
                note_ids=note_ids,
                evidence_ids=evidence_ids,
            )
            ctx.learning_service.render_views()
            return {
                "goal_id": goal.goal_id,
                "status": goal.status,
                "split_advice": advice,
                "next": "用户确认后激活：lw goal activate（CLI）",
            }
        except Exception as exc:
            return _err(exc)

    @server.tool(name="lw_get_goal")
    def lw_get_goal(goal_id: str) -> dict[str, Any]:
        """查看目标：状态、每项能力要求层级与当前最高证据、缺口、完成策略。"""
        try:
            return json.loads(
                json.dumps(ctx.learning_service.goals.assess(goal_id), ensure_ascii=False)
            )
        except Exception as exc:
            return _err(exc)

    @server.tool(name="lw_start_diagnostic")
    def lw_start_diagnostic(goal_id: str) -> dict[str, Any]:
        """起点诊断（active 目标）：三类活动框架；已有充分证据的活动被跳过。"""
        try:
            plan = ctx.learning_service.diagnostics.start(goal_id)
            return {
                "session_id": plan.session_id,
                "activities": plan.activities,
                "note": "Agent 按框架生成题面；用户闭卷作答后用 lw_submit_attempt 提交",
            }
        except Exception as exc:
            return _err(exc)

    @server.tool(name="lw_get_next_activity")
    def lw_get_next_activity(goal_id: str, session_id: str | None = None) -> dict[str, Any]:
        """下一活动（确定性路由）。reason 字段说明为何推荐当前活动。"""
        try:
            act = ctx.learning_service.orchestrator.get_next_activity(goal_id, session_id)
            return {
                "session_id": act.session_id,
                "activity_type": act.activity_type,
                "capability_id": act.capability_id,
                "misconception_id": act.misconception_id,
                "intervention": act.intervention,
                "reason": act.reason,
                "guidance": act.guidance,
                "end": act.end,
                "end_reason": act.end_reason,
                "activities_left": act.activities_left,
            }
        except Exception as exc:
            return _err(exc)

    # ------------------------------------------------------------ 作答反馈 --

    @server.tool(name="lw_submit_attempt")
    def lw_submit_attempt(
        session_id: str,
        learning_object_id: str,
        response: str,
        confidence: int | None = None,
        hints_used: int = 0,
        source_opened: bool = False,
    ) -> dict[str, Any]:
        """提交用户闭卷回答。response 必须是用户真实回答（护栏：空回答拒绝）。

        必须先经 lw_get_next_activity / lw_start_diagnostic 建立会话。
        """
        try:
            event = ctx.learning_service.orchestrator.submit_attempt(
                session_id=session_id,
                learning_object_id=learning_object_id,
                response=response,
                confidence=confidence,
                hints_used=hints_used,
                source_opened=source_opened,
            )
            return {"event_id": event.event_id, "next": "评估后用 lw_submit_feedback 记录反馈"}
        except OrchestratorError as exc:
            return _err(exc)

    @server.tool(name="lw_submit_confidence")
    def lw_submit_confidence(
        session_id: str, learning_object_id: str, confidence: int
    ) -> dict[str, Any]:
        """为一次作答补记 0–100 置信度（元认知校准）。"""
        try:
            events = ctx.learning_service.event_log.load_session(session_id)
            target = next(
                (
                    e
                    for e in events
                    if e.event_type == "attempt_submitted"
                    and e.learning_object_id == learning_object_id
                    and e.confidence is None
                ),
                None,
            )
            if target is None:
                return {"error": "找不到待补记置信度的作答（可能已记录过）"}
            from learning_wiki.domain import ids
            from learning_wiki.domain.contracts import LearningEvent

            ctx.learning_service.event_log.append(
                LearningEvent(
                    event_id=ids.new_event_id(),
                    event_type="confidence_recorded",
                    session_id=session_id,
                    learning_object_id=learning_object_id,
                    goal_id=target.goal_id,
                    occurred_at=ctx.clock.now_iso(),
                    confidence=confidence,
                )
            )
            return {"ok": True}
        except Exception as exc:
            return _err(exc)

    @server.tool(name="lw_submit_feedback")
    def lw_submit_feedback(
        session_id: str,
        learning_object_id: str,
        result: str,
        rubric_results: list[dict[str, Any]] | None = None,
        feedback: str | None = None,
    ) -> dict[str, Any]:
        """记录评估反馈（必须在 attempt 之后；派生能力证据与复习排程）。

        result ∈ successful | partial | failed。rubric_results 每项 {item, met}。
        """
        try:
            rubric = [RubricItem.model_validate(r) for r in (rubric_results or [])]
            event = ctx.learning_service.orchestrator.record_feedback(
                session_id=session_id,
                learning_object_id=learning_object_id,
                result=result,
                rubric_results=rubric,
                feedback=feedback,
                assessor=AssessorInfo(type="agent"),
            )
            row = ctx.conn.execute(
                "SELECT goal_id FROM learning_objects WHERE learning_object_id = ?",
                (learning_object_id,),
            ).fetchone()
            achieved_now = False
            if row and row["goal_id"]:
                assessment = ctx.learning_service.goals.refresh_derived(row["goal_id"])
                achieved_now = bool(assessment.get("achieved_now"))
                ctx.learning_service.render_views()
            return {
                "event_id": event.event_id,
                "goal_achieved_now": achieved_now,
                "next_review_at": event.next_review_at,
            }
        except OrchestratorError as exc:
            return _err(exc)

    # ---------------------------------------------------------------- 误解 --

    @server.tool(name="lw_propose_misconception")
    def lw_propose_misconception(
        concept_ids: list[str],
        user_belief: str,
        correction: str,
        error_type: str,
        evidence_ids: list[str] | None = None,
        context: str | None = None,
        confidence: int | None = None,
    ) -> dict[str, Any]:
        """提议误解（AI 提议错误类型，确定性服务保存结构；用户可修改或拒绝）。

        error_type ∈ fact_gap/causal_gap/boundary_error/concept_confusion/
        proxy_confusion/overgeneralization/unsupported_inference/procedure_error/
        application_mismatch/outdated_knowledge。
        confidence ≥ 80 的错误自动确认（open），否则为候选（candidate）。
        """
        try:
            mis = ctx.learning_service.misconceptions.observe_error(
                concept_ids=concept_ids,
                user_belief=user_belief,
                correction=correction,
                error_type=error_type,
                evidence_ids=evidence_ids,
                context=context,
                confidence=confidence,
            )
            ctx.learning_service.render_views()
            return {
                "misconception_id": mis.misconception_id,
                "status": mis.status,
                "occurrences": mis.observations.occurrences,
            }
        except Exception as exc:
            return _err(exc)

    @server.tool(name="lw_update_misconception")
    def lw_update_misconception(
        misconception_id: str,
        action: str,
        activity: str | None = None,
        result: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """更新误解：action ∈ record_intervention（记录干预结果）。

        result ∈ recurred | improving | resolved。目标状态修改（确认/解决/复发）
        由确定性规则或用户经 CLI 完成，Agent 不能直接改状态。
        """
        try:
            if action != "record_intervention":
                return {"error": f"未知 action: {action}（仅 record_intervention）"}
            if not (activity and result):
                return {"error": "record_intervention 需要 activity 与 result"}
            mis = ctx.learning_service.misconceptions.record_intervention(
                misconception_id,
                activity,
                result,
            )
            return {"misconception_id": mis.misconception_id, "status": mis.status}
        except Exception as exc:
            return _err(exc)

    # ------------------------------------------------------- 应用与挑战 --

    @server.tool(name="lw_create_application_challenge")
    def lw_create_application_challenge(
        goal_id: str,
        capability_id: str,
        problem: str,
        challenge_type: str = "real_project",
        project_id: str | None = None,
        constraints: list[str] | None = None,
        required_reasoning: list[str] | None = None,
        note_ids: list[str] | None = None,
        evidence_ids: list[str] | None = None,
        success_criteria: list[str] | None = None,
    ) -> dict[str, Any]:
        """创建应用挑战（§5.3）：来自真实项目/写作/决策。"""
        try:
            ch = ctx.learning_service.applications.create_challenge(
                goal_id,
                capability_id,
                problem=problem,
                challenge_type=challenge_type,
                project_id=project_id,
                constraints=constraints,
                required_reasoning=required_reasoning,
                note_ids=note_ids,
                evidence_ids=evidence_ids,
                success_criteria=success_criteria,
            )
            ctx.learning_service.render_views()
            return {
                "challenge_id": ch.challenge_id,
                "next": "用户执行前先记录应用前预测（lw_record_application_prediction）",
            }
        except Exception as exc:
            return _err(exc)

    @server.tool(name="lw_record_application_prediction")
    def lw_record_application_prediction(
        challenge_id: str,
        context: str,
        selected_knowledge: list[str],
        reasoning_before_result: str,
        predicted_outcome: str,
    ) -> dict[str, Any]:
        """记录应用前判断（§5.5：必须先于结果，避免事后合理化）。"""
        try:
            event = ctx.learning_service.applications.record_prediction(
                challenge_id,
                context=context,
                selected_knowledge=selected_knowledge,
                reasoning_before_result=reasoning_before_result,
                predicted_outcome=predicted_outcome,
            )
            return {"event_id": event.event_id}
        except ApplicationServiceError as exc:
            return _err(exc)

    @server.tool(name="lw_record_application_outcome")
    def lw_record_application_outcome(
        challenge_id: str,
        action_taken: str,
        outcome: str,
        user_assessment: str,
        lessons: str | None = None,
        user_confirmed: bool = False,
    ) -> dict[str, Any]:
        """记录应用结果。护栏：必须 user_confirmed=True（用户确认），否则拒绝。

        user_assessment ∈ successful/partial_success/failed_execution/wrong_model/
        invalid_assumption/knowledge_conflict/inconclusive。
        """
        try:
            if not user_confirmed:
                return {
                    "error": "应用结果必须由用户确认（user_confirmed=True 且来自用户交互）；"
                    "系统不能自动认定应用成功"
                }
            event = ctx.learning_service.applications.record_outcome(
                challenge_id,
                action_taken=action_taken,
                outcome=outcome,
                user_assessment=user_assessment,
                lessons=lessons,
                assessor=AssessorInfo(type="user", model="mcp_user_confirmed"),
            )
            route = ctx.learning_service.applications.route_outcome(user_assessment)
            ctx.learning_service.render_views()
            return {"event_id": event.event_id, "next_step": route["next_step"]}
        except ApplicationServiceError as exc:
            return _err(exc)

    @server.tool(name="lw_get_capability_evidence")
    def lw_get_capability_evidence(
        goal_id: str, capability_id: str | None = None
    ) -> dict[str, Any]:
        """查看能力证据（事件级，可回溯到具体用户行为记录）。"""
        try:
            evidence = [
                r
                for r in ctx.learning_service.capability_service.derive_evidence(
                    ctx.learning_service.event_log.all_events()
                )
                if r.goal_id == goal_id
            ]
            if capability_id:
                evidence = [r for r in evidence if r.capability_id == capability_id]
            return {
                "evidence": [
                    {
                        "capability_id": r.capability_id,
                        "level": r.level,
                        "event_id": r.event_id,
                        "occurred_at": r.occurred_at,
                        "hint_free": r.hint_free,
                        "closed_book": r.closed_book,
                    }
                    for r in evidence
                ],
                "levels": ctx.learning_service.capability_service.current_levels(
                    ctx.learning_service.event_log.events_for_goal(goal_id)
                ),
            }
        except Exception as exc:
            return _err(exc)

    @server.tool(name="lw_list_revalidation_tasks")
    def lw_list_revalidation_tasks() -> list[dict[str, Any]]:
        """列出需要重新验证的目标（知识版本更新联动，§6.5）。"""
        try:
            return [
                {"goal_id": g.goal_id, "title": g.title, "status": g.status}
                for g in ctx.learning_service.goals.list("needs_revalidation")
            ]
        except Exception as exc:
            return [{"error": str(exc)}]

    # -- 扩展：目标激活（Agent 可激活 draft 目标，但不能标记 achieved——由规则派生）
    #    与误解解决（须用户确认）。§8.1 权限约束保持不变。

    @server.tool(name="lw_activate_goal")
    def lw_activate_goal(goal_id: str) -> dict[str, Any]:
        """激活目标（draft → active）。不能直接标记 achieved——由规则派生。"""
        try:
            ctx.learning_service.goals.activate(goal_id)
            ctx.learning_service.render_views()
            return {"goal_id": goal_id, "status": "active"}
        except GoalStateError as exc:
            return _err(exc)

    @server.tool(name="lw_resolve_misconception")
    def lw_resolve_misconception(
        misconception_id: str, user_confirmed: bool = False, reason: str | None = None
    ) -> dict[str, Any]:
        """解决误解：需满足 §4.7 条件（两个不同题面、无提示、不同日期）。"""
        try:
            if not user_confirmed:
                return {"error": "误解解决需用户确认（user_confirmed=True）或满足规则条件"}
            mis = ctx.learning_service.misconceptions.resolve(
                misconception_id, forced=user_confirmed, reason=reason
            )
            ctx.learning_service.render_views()
            return {"misconception_id": mis.misconception_id, "status": mis.status}
        except MisconceptionServiceError as exc:
            return _err(exc)
