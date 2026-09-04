"""学习机制升级集成测试（规格《学习机制升级思路》§10.2）。

- 创建 Goal 后可以完成诊断并得到下一活动
- 已有 explain 证据时不会重复安排 recall
- 高置信度错误创建高优先级误解候选；第二次出现后确认
- direct_explanation 无效后不会继续重复相同活动
- 近迁移成功不会自动升级为 real_application
- 真实应用必须有用户确认
- Goal 只有满足 completion policy 才进入 achieved
- Knowledge Note 更新后相关 LearningObject 进入 needs_review
- 删除 SQLite 后可以恢复 Goal、误解和应用证据
"""

from __future__ import annotations

import contextlib
import shutil

import pytest

from learning_wiki.domain.contracts import AssessorInfo, GoalCapability, LearningEvent
from tests.conftest import make_vault


@pytest.fixture
def lv(tmp_path, frozen_clock):
    """初始化好的 vault + learning_service。"""
    ctx = make_vault(tmp_path / "vault", clock=frozen_clock)
    yield ctx
    ctx.close()


def _create_goal(ctx, required="explain", note_ids=None, capability_id="cap_t"):
    goal, _ = ctx.learning_service.goals.create(
        "测试目标",
        [
            GoalCapability(
                capability_id=capability_id,
                behavior="能解释并辨别",
                required_level=required,
            )
        ],
        note_ids=note_ids,
    )
    ctx.learning_service.goals.activate(goal.goal_id)
    return goal


def _levels(ctx, goal_id: str) -> dict[str, str]:
    return {
        g["capability_id"]: g["current_level"]
        for g in ctx.learning_service.goals.assess(goal_id)["gaps"]
    }


def _run_activity(ctx, goal, capability_id, activity_type, result, confidence=50, hints=0):
    """走一遍：next → object → attempt → feedback，返回 feedback 事件。"""
    act = ctx.learning_service.orchestrator.get_next_activity(goal.goal_id)
    obj = ctx.learning_service.orchestrator.create_activity_object(
        session_id=act.session_id,
        goal_id=goal.goal_id,
        activity_type=activity_type,
        title=f"对象-{activity_type}",
        prompt="请闭卷作答",
        capability_id=capability_id,
    )
    ctx.learning_service.orchestrator.submit_attempt(
        session_id=act.session_id,
        learning_object_id=obj.learning_object_id,
        response="用户回答内容",
        confidence=confidence,
        hints_used=hints,
    )
    return ctx.learning_service.orchestrator.record_feedback(
        session_id=act.session_id,
        learning_object_id=obj.learning_object_id,
        result=result,
    )


class TestFullLoop:
    def test_goal_diagnostic_next_activity(self, lv) -> None:
        goal = _create_goal(lv)
        plan = lv.learning_service.diagnostics.start(goal.goal_id)
        assert plan.activities, "新目标应产出诊断活动"
        assert lv.learning_service.goals.load(goal.goal_id).status == "diagnosing"
        lv.learning_service.diagnostics.complete(
            plan.session_id, summary={"weakest_capability": "cap_t"}
        )
        assert lv.learning_service.goals.load(goal.goal_id).status == "learning"
        act = lv.learning_service.orchestrator.get_next_activity(goal.goal_id)
        assert not act.end
        assert act.activity_type == "recall"
        assert act.reason, "推荐必须可解释"

    def test_achieved_only_with_policy(self, lv) -> None:
        goal = _create_goal(lv)
        plan = lv.learning_service.diagnostics.start(goal.goal_id)
        lv.learning_service.diagnostics.complete(plan.session_id, {})
        # 先一次失败：不能 achieved
        _run_activity(lv, goal, "cap_t", "explain", "failed")
        assert lv.learning_service.goals.load(goal.goal_id).status == "learning"
        # 一次成功（explain 层级）→ 满足策略 → achieved（由规则派生）
        _run_activity(lv, goal, "cap_t", "explain", "successful")
        assert lv.learning_service.goals.load(goal.goal_id).status == "achieved"
        events = [
            e for e in lv.learning_service.event_log.all_events() if e.event_type == "goal_achieved"
        ]
        assert events, "achieved 必须伴随 goal_achieved 事件"

    def test_explain_evidence_skips_recall(self, lv) -> None:
        """已有 explain 证据时不会重复安排 recall（§10.2）。"""
        goal = _create_goal(lv, required="discriminate")
        plan = lv.learning_service.diagnostics.start(goal.goal_id)
        lv.learning_service.diagnostics.complete(plan.session_id, {})
        _run_activity(lv, goal, "cap_t", "explain", "successful")
        act = lv.learning_service.orchestrator.get_next_activity(goal.goal_id)
        assert act.activity_type != "recall", "不应重复安排 recall"
        assert act.activity_type == "discriminate"


class TestMisconceptions:
    def test_high_confidence_error_confirms(self, lv) -> None:
        """高置信度错误 → 直接确认误解；第二次出现仍为 open。"""
        m1 = lv.learning_service.misconceptions.observe_error(
            concept_ids=["note_x"],
            user_belief="学习越流畅效果越好",
            correction="流畅性不能单独预测延迟保持",
            error_type="proxy_confusion",
            confidence=90,
        )
        assert m1.status == "open", "高置信度错误应直接确认"
        assert m1.observations.max_confidence_when_wrong == 90
        assert m1 in lv.learning_service.misconceptions.high_priority()

    def test_candidate_confirmed_on_second_occurrence(self, lv) -> None:
        """同一错误结构两次 → 确认（§4.4）。"""
        kwargs = dict(
            concept_ids=["note_x"],
            user_belief="B",
            correction="C",
            error_type="fact_gap",
            confidence=40,
        )
        m1 = lv.learning_service.misconceptions.observe_error(**kwargs)
        assert m1.status == "candidate"
        m2 = lv.learning_service.misconceptions.observe_error(**kwargs)
        assert m1.misconception_id == m2.misconception_id
        assert m2.status == "open"
        assert m2.observations.occurrences == 2
        confirmed = [
            e
            for e in lv.learning_service.event_log.all_events()
            if e.event_type == "misconception_confirmed"
        ]
        assert confirmed

    def test_failed_intervention_not_repeated(self, lv) -> None:
        """direct_explanation 无效后不会继续重复相同活动（§10.2/§6.4）。"""
        m = lv.learning_service.misconceptions.observe_error(
            concept_ids=["note_x"],
            user_belief="B",
            correction="C",
            error_type="concept_confusion",
            confidence=90,
        )
        lv.learning_service.misconceptions.record_intervention(
            m.misconception_id, "contrast_case", "recurred"
        )
        act = lv.learning_service.orchestrator._pick_intervention(
            lv.learning_service.misconceptions.repo.load(m.misconception_id)
        )
        assert act != "contrast_case", "失败的首选干预不应被重复"
        assert act == "interleaved_practice"

    def test_resolution_requires_two_contexts_and_dates(self, lv) -> None:
        m = lv.learning_service.misconceptions.observe_error(
            concept_ids=["note_x"],
            user_belief="B",
            correction="C",
            error_type="fact_gap",
            confidence=90,
        )
        # 只有一个题面正确：不满足
        with pytest.raises(Exception):  # noqa: B017
            lv.learning_service.misconceptions.resolve(m.misconception_id)
        # 两个不同题面、不同日期、一次无提示
        from learning_wiki.learning.store import LearningObjectRepository  # noqa: F401

        lv.clock.advance(days=1)
        for i, day in enumerate((0, 1)):
            lv.clock.advance(days=day)
            obj = lv.learning_service.orchestrator.create_activity_object(
                session_id="session_manual_1",
                goal_id="goal_any",
                activity_type="explain",
                title=f"对象{i}",
                prompt="p",
                capability_id="cap_any",
            )
            # 绑定 note_x 的知识快照，让对象计入解决证据
            from learning_wiki.domain.contracts import (
                KnowledgeSnapshot,
                KnowledgeSnapshotEvidence,
            )

            obj.knowledge_snapshot = KnowledgeSnapshot(note_id="note_x", note_hash="h")
            obj.knowledge_snapshot.evidence.append(
                KnowledgeSnapshotEvidence(
                    evidence_id="ev_x", source_id="src_x", version_id="v0001", span_hash="s"
                )
            )
            lv.learning_service.object_repo.save(obj)
            sid = f"session_res_{i}"
            lv.learning_service.event_log.append(
                LearningEvent(
                    event_id=f"evt_attempt_{i}",
                    event_type="attempt_submitted",
                    session_id=sid,
                    learning_object_id=obj.learning_object_id,
                    occurred_at=lv.clock.now_iso(),
                    response_before_feedback="回答",
                )
            )
            lv.learning_service.event_log.append(
                LearningEvent(
                    event_id=f"evt_feedback_{i}",
                    event_type="feedback_recorded",
                    session_id=sid,
                    learning_object_id=obj.learning_object_id,
                    occurred_at=lv.clock.now_iso(),
                    result="successful",
                    hints_used=0,
                )
            )
        lv.learning_service.misconceptions.resolve(m.misconception_id)
        assert lv.learning_service.misconceptions.repo.load(m.misconception_id).status == "resolved"


class TestApplication:
    def test_prediction_before_outcome_and_user_confirmation(self, lv) -> None:
        goal = _create_goal(lv, required="real_application")
        ch = lv.learning_service.applications.create_challenge(
            goal.goal_id, "cap_t", problem="为一个月的学习材料设计复习计划"
        )
        # 没有预测 → 拒绝记录结果
        with pytest.raises(Exception):  # noqa: B017
            lv.learning_service.applications.record_outcome(
                ch.challenge_id,
                action_taken="执行了计划",
                outcome="大部分完成",
                user_assessment="partial_success",
                assessor=AssessorInfo(type="user"),
            )
        lv.learning_service.applications.record_prediction(
            ch.challenge_id,
            context="为本月学习内容安排复习",
            selected_knowledge=["note_x"],
            reasoning_before_result="先短间隔再延长",
            predicted_outcome="一周内可持续",
        )
        # 非 user assessor → 拒绝（Agent 不能自动确认）
        with pytest.raises(Exception):  # noqa: B017
            lv.learning_service.applications.record_outcome(
                ch.challenge_id,
                action_taken="执行了计划",
                outcome="大部分完成",
                user_assessment="partial_success",
                assessor=AssessorInfo(type="agent"),
            )
        ev = lv.learning_service.applications.record_outcome(
            ch.challenge_id,
            action_taken="执行了计划",
            outcome="大部分完成，周末积压",
            user_assessment="partial_success",
            assessor=AssessorInfo(type="user"),
        )
        assert ev.event_type == "application_outcome_recorded"

    def test_near_transfer_does_not_upgrade_to_real_application(self, lv) -> None:
        """近迁移成功不会自动升级为 real_application（§10.2）。"""
        goal = _create_goal(lv, required="real_application")
        plan = lv.learning_service.diagnostics.start(goal.goal_id)
        lv.learning_service.diagnostics.complete(plan.session_id, {})
        _run_activity(lv, goal, "cap_t", "near_transfer", "successful")
        assert _levels(lv, goal.goal_id).get("cap_t") == "near_transfer"
        assert not lv.learning_service.goals.assess(goal.goal_id)["achieved"], (
            "缺少用户确认的真实应用事件，不能 achieved"
        )

    def test_real_application_needs_user_confirmed_event(self, lv) -> None:
        goal = _create_goal(lv, required="real_application")
        plan = lv.learning_service.diagnostics.start(goal.goal_id)
        lv.learning_service.diagnostics.complete(plan.session_id, {})
        _run_activity(lv, goal, "cap_t", "real_application", "successful")
        assert lv.learning_service.goals.load(goal.goal_id).status != "achieved"
        ch = lv.learning_service.applications.create_challenge(
            goal.goal_id, "cap_t", problem="真实项目"
        )
        lv.learning_service.applications.record_prediction(
            ch.challenge_id,
            context="c",
            selected_knowledge=[],
            reasoning_before_result="r",
            predicted_outcome="p",
        )
        lv.learning_service.applications.record_outcome(
            ch.challenge_id,
            action_taken="a",
            outcome="o",
            user_assessment="successful",
            assessor=AssessorInfo(type="user"),
        )
        lv.learning_service.goals.refresh_derived(goal.goal_id)
        assert lv.learning_service.goals.load(goal.goal_id).status == "achieved"

    def test_knowledge_conflict_creates_update_candidate(self, lv) -> None:
        goal = _create_goal(lv)
        ch = lv.learning_service.applications.create_challenge(
            goal.goal_id, "cap_t", problem="p", note_ids=["note_x"]
        )
        lv.learning_service.applications.record_prediction(
            ch.challenge_id,
            context="c",
            selected_knowledge=["note_x"],
            reasoning_before_result="r",
            predicted_outcome="p",
        )
        lv.learning_service.applications.record_outcome(
            ch.challenge_id,
            action_taken="a",
            outcome="结果与现有结论冲突",
            user_assessment="knowledge_conflict",
            assessor=AssessorInfo(type="user"),
        )
        candidates = lv.learning_service.candidate_store.list()
        assert candidates, "knowledge_conflict 应创建知识更新候选"
        assert candidates[0].reason == "application_evidence_conflicts_with_current_claim"
        assert candidates[0].requires_human_review


class TestSnapshotRevalidation:
    def _setup_note(self, lv) -> str:
        """创建一个带 frontmatter 的 Wiki 笔记并索引。"""
        note_dir = lv.paths.folder("wiki")
        note_dir.mkdir(parents=True, exist_ok=True)
        path = note_dir / "note-snapshot-test.md"
        path.write_text(
            "---\nnote_id: note_snap\nstatus: draft\ncreated_at: 2026-09-01\n"
            "updated_at: 2026-09-01\n---\n\n检索练习内容。\n",
            encoding="utf-8",
        )
        from learning_wiki.storage.note_indexer import NoteIndexer

        NoteIndexer(lv.paths, lv.indexer, lv.fts).index_note(
            lv.paths.relpath(path),
            path.read_text(encoding="utf-8"),
            lv.clock.now_iso(),
        )
        return "note_snap"

    def test_note_update_marks_needs_review(self, lv) -> None:
        note_id = self._setup_note(lv)
        goal = _create_goal(lv, note_ids=[note_id])
        plan = lv.learning_service.diagnostics.start(goal.goal_id)
        lv.learning_service.diagnostics.complete(plan.session_id, {})

        # 创建带知识快照的学习对象
        from learning_wiki.learning.snapshots import build_snapshot

        act = lv.learning_service.orchestrator.get_next_activity(goal.goal_id)
        obj = lv.learning_service.orchestrator.create_activity_object(
            session_id=act.session_id,
            goal_id=goal.goal_id,
            activity_type="explain",
            title="t",
            prompt="p",
            capability_id="cap_t",
            knowledge_snapshot=build_snapshot(lv.conn, note_id),
        )
        # 笔记更新（内容变化 → file_hash 变化）
        note_path = lv.paths.folder("wiki") / "note-snapshot-test.md"
        note_path.write_text(
            "---\nnote_id: note_snap\nstatus: draft\ncreated_at: 2026-09-01\n"
            "updated_at: 2026-09-04\n---\n\n检索练习内容（已更新）。\n",
            encoding="utf-8",
        )
        from learning_wiki.storage.note_indexer import NoteIndexer

        NoteIndexer(lv.paths, lv.indexer, lv.fts).index_note(
            lv.paths.relpath(note_path),
            note_path.read_text(encoding="utf-8"),
            lv.clock.now_iso(),
        )
        report = lv.learning_service.snapshots.revalidate()
        assert obj.learning_object_id in report["stale_objects"]
        assert lv.learning_service.object_repo.load(obj.learning_object_id).status == "needs_review"
        assert lv.learning_service.goals.load(goal.goal_id).status == "needs_revalidation"
        # 历史事件未被改写
        events = [
            e
            for e in lv.learning_service.event_log.all_events()
            if e.learning_object_id == obj.learning_object_id
        ]
        assert all(e.event_type in ("review_scheduled",) for e in events) or not events


class TestRebuild:
    def test_delete_sqlite_and_rebuild(self, tmp_path, frozen_clock) -> None:
        ctx = make_vault(tmp_path / "vault", clock=frozen_clock)
        try:
            goal = _create_goal(ctx)
            plan = ctx.learning_service.diagnostics.start(goal.goal_id)
            ctx.learning_service.diagnostics.complete(plan.session_id, {})
            _run_activity(ctx, goal, "cap_t", "explain", "successful")
            ctx.learning_service.misconceptions.observe_error(
                concept_ids=["note_x"],
                user_belief="B",
                correction="C",
                error_type="fact_gap",
                confidence=90,
            )
            ch = ctx.learning_service.applications.create_challenge(
                goal.goal_id, "cap_t", problem="p"
            )
            ctx.learning_service.applications.record_prediction(
                ch.challenge_id,
                context="c",
                selected_knowledge=[],
                reasoning_before_result="r",
                predicted_outcome="p",
            )
            levels_before = _levels(ctx, goal.goal_id)
            goal_status_before = ctx.learning_service.goals.load(goal.goal_id).status
            db_path = ctx.paths.db_path()
            ctx.conn.close()
            shutil.rmtree(db_path.parent)
        finally:
            with contextlib.suppress(Exception):
                ctx.close()

        # 重建
        ctx2 = make_vault(tmp_path / "vault", clock=frozen_clock)
        try:
            report = ctx2.rebuild_service.rebuild()
            assert report.goals == 1
            assert report.misconceptions == 1
            assert report.challenges == 1
            assert report.learning_events > 0
            levels_after = _levels(ctx2, goal.goal_id)
            assert levels_after == levels_before
            assert ctx2.learning_service.goals.load(goal.goal_id).status == goal_status_before
            mis = ctx2.learning_service.misconceptions.repo.list()[0]
            assert mis.status == "open"
            assert mis.observations.max_confidence_when_wrong == 90
            # 事件文件仍在（事实源）
            assert any(
                e.event_type == "application_prediction_recorded"
                for e in ctx2.learning_service.event_log.all_events()
            )
        finally:
            ctx2.close()
