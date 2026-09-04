"""学习机制升级单元测试（规格《学习机制升级思路》§10.1）。

- 能力层级比较与证据派生规则
- Goal 状态机与完成策略
- 诊断路由
- 错误类型到活动类型映射
- 高置信度错误规则
- 误解确认与解决条件
- 应用结果分类
- 事件重放幂等
"""

from __future__ import annotations

import itertools

from learning_wiki.domain.contracts import (
    CompletionPolicy,
    Goal,
    GoalCapability,
    LearningEvent,
)
from learning_wiki.learning.capability import (
    CapabilityEvidenceService,
    evidence_level_for,
    level_at_least,
    level_index,
)
from learning_wiki.learning.goal_service import _LEGAL_TRANSITIONS
from learning_wiki.learning.misconception_service import is_high_confidence_error
from learning_wiki.learning.orchestrator import _GAP_ROUTING, ERROR_ACTIVITY_MAP

NOW = "2026-09-04T10:00:00+08:00"


def _ev(
    goal_id="goal_t",
    cap="cap_t",
    level="explain",
    event_id="evt_t_1",
    at=NOW,
    hints=0,
    opened=False,
):
    return LearningEvent(
        event_id=event_id,
        event_type="capability_evidence_added",
        goal_id=goal_id,
        capability_id=cap,
        occurred_at=at,
        level=level,
        hints_used=hints,
        source_opened_before_answer=opened,
    )


# ---------------------------------------------------------------- 层级比较


class TestCapabilityLevels:
    def test_ordering(self) -> None:
        assert level_index("unseen") < level_index("recognize")
        assert level_index("recognize") < level_index("recall")
        assert level_index("recall") < level_index("explain")
        assert level_index("explain") < level_index("discriminate")
        assert level_index("discriminate") < level_index("near_transfer")
        assert level_index("near_transfer") < level_index("far_transfer")
        assert level_index("far_transfer") < level_index("real_application")
        assert level_at_least("far_transfer", "explain")
        assert not level_at_least("explain", "far_transfer")

    def test_evidence_level_rules(self) -> None:
        # successful 无提示闭卷 → 活动层级
        assert evidence_level_for("explain", "successful", 0, False) == "explain"
        assert evidence_level_for("near_transfer", "successful", 0, False) == "near_transfer"
        # successful 带提示 → 降一级（最低 recognize）
        assert evidence_level_for("explain", "successful", 1, False) == "recall"
        assert evidence_level_for("recall", "successful", 1, False) == "recognize"
        # 开卷同带提示处理
        assert evidence_level_for("explain", "successful", 0, True) == "recall"
        # partial 无提示 → 降两级
        assert evidence_level_for("discriminate", "partial", 0, False) == "recall"
        # failed → 无证据
        assert evidence_level_for("explain", "failed", 0, False) is None

    def test_repeated_same_object_never_exceeds_its_level(self) -> None:
        """同一题重复三次答对，不得自动算远迁移（§10.3-3）。"""
        for _ in range(3):
            assert evidence_level_for("discriminate", "successful", 0, False) == "discriminate"
            assert evidence_level_for("recall", "successful", 0, False) == "recall"


# ---------------------------------------------------------------- 完成策略


class TestCompletionPolicy:
    def _goal(self, policy: CompletionPolicy | None = None, required="explain"):
        return Goal(
            goal_id="goal_t",
            title="t",
            status="learning",
            created_at=NOW,
            capabilities=[
                GoalCapability(capability_id="cap_t", behavior="b", required_level=required)
            ],
            completion_policy=policy or CompletionPolicy(),
        )

    def test_achieved_when_sufficient(self) -> None:
        svc = CapabilityEvidenceService()
        a = svc.assess_goal(self._goal(), [_ev()], NOW)
        assert a.achieved, a.reasons

    def test_not_achieved_low_level(self) -> None:
        svc = CapabilityEvidenceService()
        a = svc.assess_goal(self._goal(), [_ev(level="recall")], NOW)
        assert not a.achieved

    def test_not_achieved_when_hint_dependent(self) -> None:
        svc = CapabilityEvidenceService()
        a = svc.assess_goal(self._goal(), [_ev(hints=1)], NOW)
        assert not a.achieved

    def test_not_achieved_when_stale(self) -> None:
        svc = CapabilityEvidenceService()
        old = "2026-01-01T10:00:00+08:00"  # 远超 90 天
        a = svc.assess_goal(self._goal(), [_ev(at=old)], NOW)
        assert not a.achieved
        assert any("超龄" in r for r in a.reasons)

    def test_real_application_requires_user_confirmed_event(self) -> None:
        svc = CapabilityEvidenceService()
        goal = self._goal(required="real_application")
        # 有 real_application 层级证据但没有用户确认应用事件
        a = svc.assess_goal(goal, [_ev(level="real_application")], NOW)
        assert not a.achieved
        assert any("用户确认" in r for r in a.reasons)
        # 加上用户确认的 successful 应用事件
        app = LearningEvent(
            event_id="evt_app",
            event_type="application_outcome_recorded",
            goal_id="goal_t",
            occurred_at=NOW,
            user_assessment="successful",
            assessor={"type": "user"},
        )
        a = svc.assess_goal(goal, [_ev(level="real_application"), app], NOW)
        assert a.achieved, a.reasons

    def test_deprecated_note_blocks_achieved(self) -> None:
        svc = CapabilityEvidenceService()
        goal = self._goal()
        goal.knowledge_dependencies.note_ids.append("note_x")
        a = svc.assess_goal(self._goal(), [_ev()], NOW, note_statuses={"note_x": "deprecated"})
        # goal 的依赖是 note_x，但评估用的 goal 对象没带依赖——重新构造
        goal.knowledge_dependencies.note_ids = ["note_x"]
        a = svc.assess_goal(goal, [_ev()], NOW, note_statuses={"note_x": "deprecated"})
        assert not a.achieved
        assert any("deprecated" in r for r in a.reasons)


# ---------------------------------------------------------------- 状态机


class TestGoalStateMachine:
    def test_main_chain(self) -> None:
        chain = [
            "draft",
            "active",
            "diagnosing",
            "learning",
            "transfer_testing",
            "application_pending",
            "achieved",
        ]
        for cur, nxt in itertools.pairwise(chain):
            assert nxt in _LEGAL_TRANSITIONS[cur], f"{cur} → {nxt}"

    def test_illegal_transitions(self) -> None:
        assert "achieved" not in _LEGAL_TRANSITIONS["draft"]
        assert "active" not in _LEGAL_TRANSITIONS["abandoned"]  # 终态
        assert "draft" not in _LEGAL_TRANSITIONS["achieved"]

    def test_bypass_states(self) -> None:
        for state in ("paused", "abandoned", "needs_revalidation"):
            assert state in _LEGAL_TRANSITIONS["learning"]


# ---------------------------------------------------------------- 诊断路由


class TestDiagnosticRouting:
    def test_gap_routing_table(self) -> None:
        assert _GAP_ROUTING["unseen"] == "recall"
        assert _GAP_ROUTING["recall"] == "explain"
        assert _GAP_ROUTING["explain"] == "discriminate"
        assert _GAP_ROUTING["discriminate"] == "near_transfer"
        assert _GAP_ROUTING["near_transfer"] == "far_transfer"
        assert _GAP_ROUTING["far_transfer"] == "real_application"


# ---------------------------------------------------------------- 错误映射


class TestErrorActivityMap:
    def test_closed_set_of_ten(self) -> None:
        assert len(ERROR_ACTIVITY_MAP) == 10
        expected = {
            "fact_gap",
            "causal_gap",
            "boundary_error",
            "concept_confusion",
            "proxy_confusion",
            "overgeneralization",
            "unsupported_inference",
            "procedure_error",
            "application_mismatch",
            "outdated_knowledge",
        }
        assert set(ERROR_ACTIVITY_MAP) == expected

    def test_each_error_has_two_choices(self) -> None:
        for activities in ERROR_ACTIVITY_MAP.values():
            assert len(activities) == 2


# ---------------------------------------------------------------- 高置信度


class TestHighConfidenceError:
    def test_rule(self) -> None:
        assert is_high_confidence_error(90, "failed")
        assert is_high_confidence_error(80, "failed")
        assert not is_high_confidence_error(79, "failed")
        assert not is_high_confidence_error(90, "successful")  # 答对不算
        assert not is_high_confidence_error(None, "failed")


# ---------------------------------------------------------------- 事件重放


class TestEventReplay:
    def test_idempotent_append(self, tmp_path) -> None:
        from datetime import datetime

        from learning_wiki.domain.clock import FrozenClock
        from tests.conftest import make_vault

        ctx = make_vault(tmp_path / "v", clock=FrozenClock(datetime(2026, 9, 4)))
        try:
            ev = LearningEvent(
                event_id="evt_dup_1",
                event_type="goal_created",
                goal_id="goal_t",
                occurred_at=NOW,
            )
            ctx.learning_service.event_log.append(ev)
            ctx.learning_service.event_log.append(ev)  # 幂等
            count = ctx.conn.execute(
                "SELECT COUNT(*) FROM learning_events WHERE event_id='evt_dup_1'"
            ).fetchone()[0]
            assert count == 1
        finally:
            ctx.close()


class TestGoalServiceUnit:
    def test_split_advice_over_five_capabilities(self, tmp_path) -> None:
        from datetime import datetime

        from learning_wiki.domain.clock import FrozenClock
        from tests.conftest import make_vault

        ctx = make_vault(tmp_path / "v", clock=FrozenClock(datetime(2026, 9, 4)))
        try:
            caps = [
                GoalCapability(
                    capability_id=f"cap_{i}", behavior=f"行为{i}", required_level="explain"
                )
                for i in range(6)
            ]
            goal, advice = ctx.learning_service.goals.create("过大的目标", caps)
            assert goal.status == "draft"
            assert advice, "超过 5 项能力应返回拆分建议"
            assert goal.goal_id.startswith("goal_")
        finally:
            ctx.close()
