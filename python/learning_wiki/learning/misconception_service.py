"""误解模型（规格《学习机制升级思路》§4）。

- 从错误结构（原信念 + 错误类型 + 触发情境 + 干预历史）而非单次错题建模；
- 候选 vs 确认规则（§4.4）：同一错误结构两次、一次高置信度（≥80）错误、
  用户明确确认、真实应用错误决策 → 确认（open）；否则只建候选；
- resolved 条件（§4.7）：≥2 不同题面正确、≥1 无提示、不同日期；
- 边界（§4.8）：Evidence 冲突等情形创建知识更新候选，不修改 Wiki。
"""

from __future__ import annotations

from learning_wiki.domain import ids
from learning_wiki.domain.contracts import (
    ErrorType,
    KnowledgeUpdateCandidate,
    KnowledgeUpdateCandidateReason,
    LearningEvent,
    Misconception,
)
from learning_wiki.learning.store import (
    EventLog,
    KnowledgeUpdateCandidateStore,
    LearningObjectRepository,
    MisconceptionRepository,
)

HIGH_CONFIDENCE_THRESHOLD = 80
HIGH_PRIORITY_REVIEW_MAX_DAYS = 3


class MisconceptionServiceError(Exception):
    """误解模型操作错误。"""


def is_high_confidence_error(confidence: int | None, result: str | None) -> bool:
    """高置信度错误：confidence >= 80 且答案错误（§4.6）。"""
    return result == "failed" and confidence is not None and confidence >= HIGH_CONFIDENCE_THRESHOLD


class MisconceptionService:
    def __init__(
        self,
        repo: MisconceptionRepository,
        objects: LearningObjectRepository,
        events: EventLog,
        clock,
        candidates: KnowledgeUpdateCandidateStore,
    ) -> None:
        self.repo = repo
        self.objects = objects
        self.events = events
        self.clock = clock
        self.candidates = candidates

    # -- 观察错误 -----------------------------------------------------------

    def observe_error(
        self,
        *,
        concept_ids: list[str],
        user_belief: str,
        correction: str,
        error_type: ErrorType,
        evidence_ids: list[str] | None = None,
        source_event_id: str | None = None,
        context: str | None = None,
        confidence: int | None = None,
        user_confirmed: bool = False,
        from_application: bool = False,
    ) -> Misconception:
        """记录一次错误观察，应用候选/确认规则（§4.4）。

        AI 提议错误类型，确定性服务保存结构；用户可修改或拒绝。
        """
        existing = self._match(concept_ids, error_type)
        now = self.clock.now_iso()
        if existing is None:
            mis = Misconception(
                misconception_id=ids.new_misconception_id(),
                status="candidate",
                created_at=now,
                concept_ids=concept_ids,
                statement={
                    "user_belief": user_belief,
                    "correction": correction,
                    "error_type": error_type,
                },  # type: ignore[arg-type]
                evidence_ids=evidence_ids or [],
            )
            confirmed_now = (
                is_high_confidence_error(confidence, "failed") or user_confirmed or from_application
            )
            if confirmed_now:
                mis.status = "open"
            self._apply_observation(mis, source_event_id, context, confidence)
            self.repo.save(mis)
            self.events.append(
                LearningEvent(
                    event_id=ids.new_event_id(),
                    event_type="misconception_proposed",
                    misconception_id=mis.misconception_id,
                    goal_id=None,
                    occurred_at=now,
                    error_type=error_type,
                    context=context,
                    confidence=confidence,
                )
            )
            if confirmed_now:
                self._emit_confirmed(mis, now)
            return mis

        # 已有同结构误解：计一次复发
        mis = existing
        was_resolved = mis.status == "resolved"
        was_candidate = mis.status == "candidate"
        self._apply_observation(mis, source_event_id, context, confidence)
        if was_resolved:
            mis.status = "recurring"
            self.repo.save(mis)
            self._emit_state_changed(mis, "recurring", now)
            mis.status = "open"
            self.repo.save(mis)
        elif was_candidate and mis.observations.occurrences >= 2:
            mis.status = "open"
            self.repo.save(mis)
            self._emit_confirmed(mis, now)
        else:
            self.repo.save(mis)
        return mis

    def _match(self, concept_ids: list[str], error_type: str) -> Misconception | None:
        """同一错误结构 = 相同 error_type 且 concept_ids 有交集（第一版确定性匹配）。

        resolved 的误解也会被匹配：再次出现即按复发（§4.7 recurring）处理。
        """
        for mis in self.repo.list():
            if mis.statement.error_type != error_type:
                continue
            if set(mis.concept_ids) & set(concept_ids):
                return mis
        return None

    def _apply_observation(
        self,
        mis: Misconception,
        source_event_id: str | None,
        context: str | None,
        confidence: int | None,
    ) -> None:
        obs = mis.observations
        obs.occurrences += 1
        obs.last_event_id = source_event_id or obs.last_event_id
        if obs.first_event_id is None:
            obs.first_event_id = source_event_id
        if confidence is not None:
            obs.max_confidence_when_wrong = max(obs.max_confidence_when_wrong or 0, confidence)
        if context and context not in obs.contexts:
            obs.contexts.append(context)

    def _emit_confirmed(self, mis: Misconception, now: str) -> None:
        self.events.append(
            LearningEvent(
                event_id=ids.new_event_id(),
                event_type="misconception_confirmed",
                misconception_id=mis.misconception_id,
                occurred_at=now,
                error_type=mis.statement.error_type,
            )
        )

    def _emit_state_changed(self, mis: Misconception, new_status: str, now: str) -> None:
        self.events.append(
            LearningEvent(
                event_id=ids.new_event_id(),
                event_type="misconception_state_changed",
                misconception_id=mis.misconception_id,
                occurred_at=now,
                end_reason=new_status,
            )
        )

    # -- 干预与解决 -----------------------------------------------------------

    def record_intervention(
        self, misconception_id: str, activity: str, result: str
    ) -> Misconception:
        """记录一次干预结果；失败的干预不会在路由中被无限重复（见 orchestrator）。"""
        from learning_wiki.domain.contracts import MisconceptionIntervention

        mis = self.repo.load(misconception_id)
        mis.intervention_history.append(
            MisconceptionIntervention(activity=activity, result=result)  # type: ignore[arg-type]
        )
        if result == "improving":
            mis.status = "improving"
        elif result == "recurred" and mis.status == "improving":
            mis.status = "open"
        self.repo.save(mis)
        return mis

    def resolution_evidence(self, misconception_id: str) -> dict:
        """从事件流统计解决证据（§4.7）：不同题面正确 / 无提示 / 不同日期。"""
        mis = self.repo.load(misconception_id)
        concepts = set(mis.concept_ids)
        correct_objects: dict[str, dict] = {}
        for e in self.events.all_events():
            if e.event_type != "feedback_recorded" or not e.learning_object_id:
                continue
            if e.result != "successful":
                continue
            try:
                obj = self.objects.load(e.learning_object_id)
            except Exception:
                continue
            note = obj.knowledge_snapshot.note_id if obj.knowledge_snapshot else None
            if not (concepts & ({note} if note else set())):
                continue
            correct_objects.setdefault(
                e.learning_object_id,
                {"dates": set(), "hint_free": False},
            )
            correct_objects[e.learning_object_id]["dates"].add(e.occurred_at[:10])
            if (e.hints_used or 0) == 0 and not e.source_opened_before_answer:
                correct_objects[e.learning_object_id]["hint_free"] = True
        distinct_dates = set()
        for info in correct_objects.values():
            distinct_dates |= info["dates"]
        return {
            "distinct_correct_objects": len(correct_objects),
            "distinct_dates": len(distinct_dates),
            "any_hint_free": any(i["hint_free"] for i in correct_objects.values()),
        }

    def resolve(
        self, misconception_id: str, *, forced: bool = False, reason: str | None = None
    ) -> Misconception:
        """解决误解：满足 §4.7 条件，或用户强制（必须给理由，落事件）。"""
        mis = self.repo.load(misconception_id)
        if not forced:
            ev = self.resolution_evidence(misconception_id)
            if ev["distinct_correct_objects"] < 2:
                raise MisconceptionServiceError(
                    "解决条件不满足：需要至少两个不同题面正确（当前 "
                    f"{ev['distinct_correct_objects']}）"
                )
            if not ev["any_hint_free"]:
                raise MisconceptionServiceError("解决条件不满足：至少一次无提示正确")
            if ev["distinct_dates"] < 2:
                raise MisconceptionServiceError("解决条件不满足：证据需发生在不同日期")
        mis.status = "resolved"
        self.repo.save(mis)
        self._emit_state_changed(mis, "resolved", self.clock.now_iso())
        if forced and reason:
            # 用户强制解决的理由随事件留痕
            self.events.append(
                LearningEvent(
                    event_id=ids.new_event_id(),
                    event_type="misconception_state_changed",
                    misconception_id=misconception_id,
                    occurred_at=self.clock.now_iso(),
                    end_reason=f"resolved_by_user: {reason}",
                )
            )
        return mis

    def propose_correction_conflict(
        self,
        reason: KnowledgeUpdateCandidateReason,
        *,
        related_claim_ids: list[str] | None = None,
        related_note_ids: list[str] | None = None,
        learning_event_ids: list[str] | None = None,
        detail: str | None = None,
    ) -> KnowledgeUpdateCandidate:
        """创建知识更新候选（§4.8）：学习系统不能直接修改 Wiki。"""
        candidate = KnowledgeUpdateCandidate(
            candidate_id=ids.new_update_candidate_id(),
            created_at=self.clock.now_iso(),
            reason=reason,
            related_claim_ids=related_claim_ids or [],
            related_note_ids=related_note_ids or [],
            learning_event_ids=learning_event_ids or [],
            detail=detail,
        )
        self.candidates.save(candidate)
        return candidate

    def open_misconceptions(self) -> list[Misconception]:
        return [
            m
            for m in self.repo.list()
            if m.status in ("candidate", "open", "improving", "recurring")
        ]

    def high_priority(self) -> list[Misconception]:
        """高置信度错误 → 高优先级（§4.6）。"""
        return [
            m
            for m in self.open_misconceptions()
            if (m.observations.max_confidence_when_wrong or 0) >= HIGH_CONFIDENCE_THRESHOLD
        ]
