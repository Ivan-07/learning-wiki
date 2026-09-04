"""学习机制升级对抗测试（规格《学习机制升级思路》§10.3）。

1. Agent 试图直接把 Goal 标记 achieved → 没有任何入口；不满足策略时无法达成
2. Agent 在用户未回答时提交伪造 Attempt → 服务拒绝
3. 同一题重复三次答对，不得自动算远迁移
4. 一个真实项目文件引用 Wiki，不得自动算应用成功
5. 来源/笔记版本更新后，旧 Attempt 不被改写
6. 达到时间预算后，系统停止继续生成活动
7. 低价值目标被用户暂停后，不继续产生复习债务
"""

from __future__ import annotations

import asyncio

import pytest

from learning_wiki.domain.contracts import AssessorInfo, GoalCapability
from learning_wiki.learning.orchestrator import OrchestratorError
from tests.conftest import make_vault


@pytest.fixture
def lv(tmp_path, frozen_clock):
    ctx = make_vault(tmp_path / "vault", clock=frozen_clock)
    yield ctx
    ctx.close()


def _create_goal(ctx, required="explain"):
    goal, _ = ctx.learning_service.goals.create(
        "对抗目标",
        [GoalCapability(capability_id="cap_t", behavior="b", required_level=required)],
    )
    ctx.learning_service.goals.activate(goal.goal_id)
    return goal


class TestAgentCannotForgeAchieved:
    def test_no_direct_achieved_api(self, lv) -> None:
        """GoalService 不提供 set_achieved；唯一入口是 refresh_derived 的规则派生。"""
        assert not hasattr(lv.learning_service.goals, "set_achieved")
        assert not hasattr(lv.learning_service.goals, "mark_achieved")

    def test_feedback_cannot_achieve_without_policy(self, lv) -> None:
        """Agent 反馈 successful 也不足以 achieved：real_application 需用户确认。"""
        goal = _create_goal(lv, required="real_application")
        act = lv.learning_service.orchestrator.get_next_activity(goal.goal_id)
        obj = lv.learning_service.orchestrator.create_activity_object(
            session_id=act.session_id,
            goal_id=goal.goal_id,
            activity_type="explain",
            title="t",
            prompt="p",
            capability_id="cap_t",
        )
        lv.learning_service.orchestrator.submit_attempt(
            session_id=act.session_id,
            learning_object_id=obj.learning_object_id,
            response="回答",
        )
        lv.learning_service.orchestrator.record_feedback(
            session_id=act.session_id,
            learning_object_id=obj.learning_object_id,
            result="successful",
            assessor=AssessorInfo(type="agent"),
        )
        assert lv.learning_service.goals.load(goal.goal_id).status != "achieved"

    def test_mcp_has_no_achieve_tool(self, lv) -> None:
        from learning_wiki.mcp.server import create_server

        server = create_server(lv.root)
        tools = asyncio.run(server.list_tools())
        names = {t.name for t in tools}
        assert not any("achieve" in n for n in names)


class TestForgedAttemptRejected:
    def test_unknown_session_rejected(self, lv) -> None:
        goal = _create_goal(lv)
        obj = lv.learning_service.orchestrator.create_activity_object(
            session_id="session_not_exists",
            goal_id=goal.goal_id,
            activity_type="explain",
            title="t",
            prompt="p",
            capability_id="cap_t",
        )
        with pytest.raises(OrchestratorError):
            lv.learning_service.orchestrator.submit_attempt(
                session_id="session_not_exists",
                learning_object_id=obj.learning_object_id,
                response="伪造的回答",
            )

    def test_empty_response_rejected(self, lv) -> None:
        goal = _create_goal(lv)
        act = lv.learning_service.orchestrator.get_next_activity(goal.goal_id)
        obj = lv.learning_service.orchestrator.create_activity_object(
            session_id=act.session_id,
            goal_id=goal.goal_id,
            activity_type="explain",
            title="t",
            prompt="p",
            capability_id="cap_t",
        )
        with pytest.raises(OrchestratorError):
            lv.learning_service.orchestrator.submit_attempt(
                session_id=act.session_id,
                learning_object_id=obj.learning_object_id,
                response="   ",
            )

    def test_feedback_before_attempt_rejected(self, lv) -> None:
        goal = _create_goal(lv)
        act = lv.learning_service.orchestrator.get_next_activity(goal.goal_id)
        obj = lv.learning_service.orchestrator.create_activity_object(
            session_id=act.session_id,
            goal_id=goal.goal_id,
            activity_type="explain",
            title="t",
            prompt="p",
            capability_id="cap_t",
        )
        with pytest.raises(OrchestratorError):
            lv.learning_service.orchestrator.record_feedback(
                session_id=act.session_id,
                learning_object_id=obj.learning_object_id,
                result="successful",
            )

    def test_mcp_submit_attempt_requires_session(self, lv) -> None:
        from learning_wiki.mcp.server import create_server

        goal = _create_goal(lv)
        obj = lv.learning_service.orchestrator.create_activity_object(
            session_id="session_none",
            goal_id=goal.goal_id,
            activity_type="explain",
            title="t",
            prompt="p",
            capability_id="cap_t",
        )
        server = create_server(lv.root)
        result = asyncio.run(
            server.call_tool(
                "lw_submit_attempt",
                {
                    "session_id": "session_none",
                    "learning_object_id": obj.learning_object_id,
                    "response": "伪造回答",
                },
            )
        )
        assert "error" in str(result)


class TestNoFreeTransfer:
    def test_same_object_repeats_never_far_transfer(self, lv) -> None:
        """同一题重复三次答对，不得自动算远迁移。"""
        from learning_wiki.learning.capability import evidence_level_for

        goal = _create_goal(lv, required="far_transfer")
        act = lv.learning_service.orchestrator.get_next_activity(goal.goal_id)
        obj = lv.learning_service.orchestrator.create_activity_object(
            session_id=act.session_id,
            goal_id=goal.goal_id,
            activity_type="discriminate",
            title="t",
            prompt="p",
            capability_id="cap_t",
        )
        for i in range(3):
            lv.learning_service.orchestrator.submit_attempt(
                session_id=act.session_id,
                learning_object_id=obj.learning_object_id,
                response=f"回答{i}",
            )
            lv.learning_service.orchestrator.record_feedback(
                session_id=act.session_id,
                learning_object_id=obj.learning_object_id,
                result="successful",
            )
        levels = {
            g["capability_id"]: g["current_level"]
            for g in lv.learning_service.goals.assess(goal.goal_id)["gaps"]
        }
        assert levels["cap_t"] == "discriminate", "重复同一题不能产生远迁移证据"
        # 证据规则本身也不允许
        assert evidence_level_for("discriminate", "successful", 0, False) != "far_transfer"


class TestFileReferenceIsNotApplication:
    def test_project_reference_does_not_apply(self, lv) -> None:
        """真实项目文件引用 Wiki，不得自动算应用成功。"""
        goal = _create_goal(lv, required="real_application")
        # 项目文件引用目标知识
        projects = lv.paths.folder("projects")
        projects.mkdir(parents=True, exist_ok=True)
        (projects / "proj.md").write_text(
            "使用 [[concept_retrieval_practice]] 安排复习。\n", encoding="utf-8"
        )
        goal.knowledge_dependencies.note_ids.append("concept_retrieval_practice")
        lv.learning_service.goal_repo.save(goal)
        # 系统只给建议
        suggestions = lv.learning_service.applications.suggest_opportunities(goal.goal_id)
        assert suggestions, "应产生建议"
        assert "不会自动认定" in suggestions[0]["suggestion"]
        # 没有应用事件 → 不 achieved
        assert lv.learning_service.goals.load(goal.goal_id).status != "achieved"
        events = [
            e
            for e in lv.learning_service.event_log.all_events()
            if e.event_type == "application_outcome_recorded"
        ]
        assert not events

    def test_mcp_outcome_requires_user_confirmation(self, lv) -> None:
        from learning_wiki.mcp.server import create_server

        goal = _create_goal(lv)
        ch = lv.learning_service.applications.create_challenge(goal.goal_id, "cap_t", problem="p")
        lv.learning_service.applications.record_prediction(
            ch.challenge_id,
            context="c",
            selected_knowledge=[],
            reasoning_before_result="r",
            predicted_outcome="p",
        )
        server = create_server(lv.root)
        result = asyncio.run(
            server.call_tool(
                "lw_record_application_outcome",
                {
                    "challenge_id": ch.challenge_id,
                    "action_taken": "a",
                    "outcome": "o",
                    "user_assessment": "successful",
                    "user_confirmed": False,  # Agent 未获用户确认
                },
            )
        )
        assert "error" in str(result)


class TestHistoryImmutable:
    def test_note_update_does_not_rewrite_attempts(self, lv, tmp_path) -> None:
        """来源/笔记更新后，旧 Attempt 不被改写（§10.3-7）。"""
        from learning_wiki.learning.snapshots import build_snapshot

        wiki = lv.paths.folder("wiki")
        wiki.mkdir(parents=True, exist_ok=True)
        path = wiki / "note-adv.md"
        path.write_text(
            "---\nnote_id: note_adv\nstatus: draft\ncreated_at: 2026-09-01\n"
            "updated_at: 2026-09-01\n---\n\n内容 A。\n",
            encoding="utf-8",
        )
        from learning_wiki.storage.note_indexer import NoteIndexer

        NoteIndexer(lv.paths, lv.indexer, lv.fts).index_note(
            lv.paths.relpath(path), path.read_text(encoding="utf-8"), lv.clock.now_iso()
        )
        goal = _create_goal(lv)
        act = lv.learning_service.orchestrator.get_next_activity(goal.goal_id)
        obj = lv.learning_service.orchestrator.create_activity_object(
            session_id=act.session_id,
            goal_id=goal.goal_id,
            activity_type="explain",
            title="t",
            prompt="p",
            capability_id="cap_t",
            knowledge_snapshot=build_snapshot(lv.conn, "note_adv"),
        )
        lv.learning_service.orchestrator.submit_attempt(
            session_id=act.session_id,
            learning_object_id=obj.learning_object_id,
            response="原始回答",
        )
        attempts_before = [
            e.model_dump_json()
            for e in lv.learning_service.event_log.all_events()
            if e.event_type == "attempt_submitted"
        ]
        # 笔记更新
        path.write_text(
            "---\nnote_id: note_adv\nstatus: draft\ncreated_at: 2026-09-01\n"
            "updated_at: 2026-09-04\n---\n\n内容 A（更新）。\n",
            encoding="utf-8",
        )
        NoteIndexer(lv.paths, lv.indexer, lv.fts).index_note(
            lv.paths.relpath(path), path.read_text(encoding="utf-8"), lv.clock.now_iso()
        )
        lv.learning_service.snapshots.revalidate()
        attempts_after = [
            e.model_dump_json()
            for e in lv.learning_service.event_log.all_events()
            if e.event_type == "attempt_submitted"
        ]
        assert attempts_before == attempts_after, "历史 Attempt 不可改写"


class TestBudgetStops:
    def test_time_budget_reached(self, lv) -> None:
        """达到活动预算后，系统停止继续生成活动。"""
        goal = _create_goal(lv, required="far_transfer")
        session_id = None
        for _ in range(3):
            act = lv.learning_service.orchestrator.get_next_activity(goal.goal_id, session_id)
            session_id = act.session_id
            obj = lv.learning_service.orchestrator.create_activity_object(
                session_id=session_id,
                goal_id=goal.goal_id,
                activity_type=act.activity_type,
                title="t",
                prompt="p",
                capability_id="cap_t",
            )
            lv.learning_service.orchestrator.submit_attempt(
                session_id=session_id,
                learning_object_id=obj.learning_object_id,
                response="回答",
            )
            lv.learning_service.orchestrator.record_feedback(
                session_id=session_id,
                learning_object_id=obj.learning_object_id,
                result="partial",  # partial 保持目标未完成
            )
        act = lv.learning_service.orchestrator.get_next_activity(goal.goal_id, session_id)
        assert act.end
        assert act.end_reason == "time_budget_reached"
        ended = [
            e
            for e in lv.learning_service.event_log.load_session(session_id)
            if e.event_type == "session_ended"
        ]
        assert ended and ended[-1].end_reason == "time_budget_reached"


class TestPausedGoalNoDebt:
    def test_paused_goal_generates_nothing(self, lv) -> None:
        """低价值目标被用户暂停后，不继续产生复习债务（§10.3-10）。"""
        goal = _create_goal(lv)
        act = lv.learning_service.orchestrator.get_next_activity(goal.goal_id)
        obj = lv.learning_service.orchestrator.create_activity_object(
            session_id=act.session_id,
            goal_id=goal.goal_id,
            activity_type="explain",
            title="t",
            prompt="p",
            capability_id="cap_t",
        )
        lv.learning_service.orchestrator.submit_attempt(
            session_id=act.session_id,
            learning_object_id=obj.learning_object_id,
            response="回答",
        )
        lv.learning_service.orchestrator.record_feedback(
            session_id=act.session_id,
            learning_object_id=obj.learning_object_id,
            result="failed",
        )
        lv.learning_service.goals.pause(goal.goal_id)
        act = lv.learning_service.orchestrator.get_next_activity(goal.goal_id)
        assert act.end
        assert act.end_reason == "user_stopped"
        # 不新增任何事件（无复习债务）
        events_before = len(lv.learning_service.event_log.all_events())
        for _ in range(3):
            lv.learning_service.orchestrator.get_next_activity(goal.goal_id)
        assert len(lv.learning_service.event_log.all_events()) == events_before
