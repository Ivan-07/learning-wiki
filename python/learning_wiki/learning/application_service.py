"""迁移与真实应用（规格《学习机制升级思路》§5）。

- ApplicationChallenge：来自真实项目/写作/决策的应用挑战；
- 应用前预测必须先于应用后结果保存（避免事后合理化，§5.5）；
- 应用结果由用户确认，系统不能仅根据文件引用自动认定成功（§5.6）；
- 结果分类（§5.7）：knowledge_conflict → 知识更新候选，不修改 Wiki。
"""

from __future__ import annotations

import sqlite3

from learning_wiki.domain import ids
from learning_wiki.domain.contracts import (
    ApplicationChallenge,
    ApplicationOutcomeLiteral,
    AssessorInfo,
    ChallengeContext,
    ChallengeKnowledgeRefs,
    LearningEvent,
)
from learning_wiki.learning.store import (
    ChallengeRepository,
    EventLog,
    KnowledgeUpdateCandidateStore,
    LearningStoreError,
)


class ApplicationServiceError(Exception):
    """应用挑战 / 应用事件操作错误。"""


class ApplicationService:
    def __init__(
        self,
        challenges: ChallengeRepository,
        events: EventLog,
        clock,
        conn: sqlite3.Connection,
        candidates: KnowledgeUpdateCandidateStore | None = None,
    ) -> None:
        self.challenges = challenges
        self.events = events
        self.clock = clock
        self.conn = conn
        self.candidates = candidates

    # -- 挑战 ----------------------------------------------------------------

    def create_challenge(
        self,
        goal_id: str,
        capability_id: str,
        *,
        problem: str,
        challenge_type: str = "real_project",
        project_id: str | None = None,
        constraints: list[str] | None = None,
        required_reasoning: list[str] | None = None,
        note_ids: list[str] | None = None,
        evidence_ids: list[str] | None = None,
        success_criteria: list[str] | None = None,
    ) -> ApplicationChallenge:
        """创建应用挑战（§5.3）。近迁移/远迁移任务也经此入口登记。"""
        challenge = ApplicationChallenge(
            challenge_id=ids.new_challenge_id(),
            goal_id=goal_id,
            capability_id=capability_id,
            type=challenge_type,  # type: ignore[arg-type]
            status="active",
            created_at=self.clock.now_iso(),
            context=ChallengeContext(
                project_id=project_id,
                problem=problem,
                constraints=constraints or [],
            ),
            required_reasoning=required_reasoning or [],
            knowledge_refs=ChallengeKnowledgeRefs(
                note_ids=note_ids or [], evidence_ids=evidence_ids or []
            ),
            success_criteria=success_criteria or [],
        )
        self.challenges.save(challenge)
        self.events.append(
            LearningEvent(
                event_id=ids.new_event_id(),
                event_type="challenge_created",
                goal_id=goal_id,
                challenge_id=challenge.challenge_id,
                capability_id=capability_id,
                occurred_at=self.clock.now_iso(),
            )
        )
        return challenge

    def load(self, challenge_id: str) -> ApplicationChallenge:
        return self.challenges.load(challenge_id)

    def list_challenges(self, goal_id: str | None = None) -> list[ApplicationChallenge]:
        challenges = self.challenges.list()
        if goal_id:
            challenges = [c for c in challenges if c.goal_id == goal_id]
        return challenges

    # -- 项目集成：候选机会只提建议（§5.6） ------------------------------------

    def suggest_opportunities(self, goal_id: str) -> list[dict[str, str]]:
        """从 50 Projects 提取引用了目标知识依赖笔记的候选应用机会（仅建议）。

        示例输出：
        你已多次解释「X」，但尚无应用证据。当前项目包含引用该知识的决策。
        是否创建一次应用挑战。
        """
        try:
            projects_dir = self.events.paths.folder("projects")
        except KeyError:
            return []
        if not projects_dir.exists():
            return []
        goal_notes = self._goal_note_ids(goal_id)
        suggestions: list[dict[str, str]] = []
        for md in sorted(projects_dir.rglob("*.md")):
            text = md.read_text(encoding="utf-8")
            cited = [n for n in goal_notes if n in text]
            if not cited:
                continue
            rel = self.events.paths.relpath(md)
            suggestions.append(
                {
                    "project_path": rel,
                    "cited_note_ids": ", ".join(cited),
                    "suggestion": (
                        "当前项目页面引用了目标依赖的知识概念，且尚无用户确认的应用证据。"
                        "是否创建一次应用挑战？（系统不会自动认定应用成功）"
                    ),
                }
            )
        return suggestions

    def _goal_note_ids(self, goal_id: str) -> list[str]:
        goals_dir = self.events.paths.folder("learning") / "Goals"
        from learning_wiki.storage.yaml_io import load_yaml

        for p in sorted(goals_dir.glob(f"{goal_id}.yaml")):
            data = dict(load_yaml(p))
            deps = data.get("knowledge_dependencies") or {}
            return list(deps.get("note_ids") or [])
        return []

    # -- 应用前 / 应用后 --------------------------------------------------------

    def record_prediction(
        self,
        challenge_id: str,
        *,
        context: str,
        selected_knowledge: list[str],
        reasoning_before_result: str,
        predicted_outcome: str,
        session_id: str | None = None,
    ) -> LearningEvent:
        """应用前判断（§5.5）：必须先于结果保存，避免事后合理化。"""
        self._require_active_challenge(challenge_id)
        if self._has_outcome(challenge_id):
            raise ApplicationServiceError("应用结果已记录，预测不能再追加（先预测后结果）")
        event = LearningEvent(
            event_id=ids.new_event_id(),
            event_type="application_prediction_recorded",
            session_id=session_id,
            goal_id=self.challenges.load(challenge_id).goal_id,
            challenge_id=challenge_id,
            occurred_at=self.clock.now_iso(),
            context=context,
            selected_knowledge=selected_knowledge,
            reasoning_before_result=reasoning_before_result,
            predicted_outcome=predicted_outcome,
        )
        self.events.append(event)
        return event

    def record_outcome(
        self,
        challenge_id: str,
        *,
        action_taken: str,
        outcome: str,
        user_assessment: ApplicationOutcomeLiteral,
        lessons: str | None = None,
        assessor: AssessorInfo | None = None,
        session_id: str | None = None,
    ) -> LearningEvent:
        """应用后结果（§5.4）：必须由用户确认（assessor.type == user）。"""
        self._require_active_challenge(challenge_id)
        if not self._has_prediction(challenge_id):
            raise ApplicationServiceError(
                "缺少应用前预测：必须先保存应用前判断，再记录结果（避免事后合理化）"
            )
        if assessor is None or assessor.type != "user":
            raise ApplicationServiceError(
                "应用结果必须由用户确认（assessor.type == 'user'），系统不能自动认定"
            )
        challenge = self.challenges.load(challenge_id)
        event = LearningEvent(
            event_id=ids.new_event_id(),
            event_type="application_outcome_recorded",
            session_id=session_id,
            goal_id=challenge.goal_id,
            challenge_id=challenge_id,
            occurred_at=self.clock.now_iso(),
            action_taken=action_taken,
            outcome=outcome,
            user_assessment=user_assessment,
            lessons=lessons,
            assessor=assessor,
        )
        self.events.append(event)
        # 成功 → 挑战完成
        if user_assessment == "successful":
            challenge.status = "completed"
            self.challenges.save(challenge)
        elif user_assessment == "knowledge_conflict" and self.candidates is not None:
            # §5.7/§4.8：结果可能挑战现有结论 → 知识更新候选（人工核验，不改 Wiki）
            from learning_wiki.domain.contracts import KnowledgeUpdateCandidate

            self.candidates.save(
                KnowledgeUpdateCandidate(
                    candidate_id=ids.new_update_candidate_id(),
                    created_at=self.clock.now_iso(),
                    reason="application_evidence_conflicts_with_current_claim",
                    related_note_ids=list(challenge.knowledge_refs.note_ids),
                    learning_event_ids=[event.event_id],
                    detail=f"{outcome[:500]}",
                )
            )
        return event

    # -- 结果路由（§5.7） ------------------------------------------------------

    def route_outcome(self, user_assessment: str) -> dict[str, str]:
        """应用结果分类 → 下一步（确定性映射，供 CLI/MCP 展示）。"""
        routing = {
            "successful": "增加真实应用证据（capability_evidence real_application）",
            "partial_success": "针对缺口生成练习",
            "failed_execution": "程序性练习或项目复盘",
            "wrong_model": "场景辨别练习",
            "invalid_assumption": "假设检查练习",
            "knowledge_conflict": "创建知识更新候选（需人工核验，不直接修改 Wiki）",
            "inconclusive": "保留记录，不升级能力",
        }
        return {"next_step": routing[user_assessment]}

    # -- 内部 ------------------------------------------------------------------

    def _require_active_challenge(self, challenge_id: str) -> ApplicationChallenge:
        try:
            challenge = self.challenges.load(challenge_id)
        except LearningStoreError as exc:
            raise ApplicationServiceError(str(exc)) from exc
        return challenge

    def _has_prediction(self, challenge_id: str) -> bool:
        return any(
            e.event_type == "application_prediction_recorded" and e.challenge_id == challenge_id
            for e in self.events.all_events()
        )

    def _has_outcome(self, challenge_id: str) -> bool:
        return any(
            e.event_type == "application_outcome_recorded" and e.challenge_id == challenge_id
            for e in self.events.all_events()
        )
