"""全部持久对象契约（单一事实源）。

- 每个模型导出为 schemas/*.schema.json（见 scripts/export_schemas.py）。
- 所有模型 ``extra="allow"``：未知字段在内存中保留，读写回盘不得丢失（规格 §5）。
- 时间戳一律为 ISO 8601 带偏移量的字符串，由 domain.clock 提供者生成。
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Contract(BaseModel):
    """全部契约模型的公共基类：保留未知字段。"""

    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# 捕获 / Inbox
# ---------------------------------------------------------------------------

ValueState = Literal["reference", "learn", "apply", "discard"]
InboxState = Literal["new", "processing", "captured", "failed", "discarded"]
InputKind = Literal["text", "file", "url"]


class InboxItem(Contract):
    schema_version: int = 1
    item_id: str
    input_type: InputKind
    state: InboxState = "new"
    payload: str | None = None
    payload_path: str | None = None
    why_saved: str | None = None
    value_state: ValueState | None = None
    source_id: str | None = None
    version_id: str | None = None
    error: str | None = None
    created_at: str
    processed_at: str | None = None


class RawBlock(Contract):
    """提取阶段产出的一个可引用内容块（尚未分配 Evidence ID）。"""

    anchor_type: Literal["heading", "paragraph", "list_item", "quote", "code"]
    text: str
    extraction_confidence: float = 1.0


class ExtractionResult(Contract):
    """CaptureAdapter.extract 的返回值。"""

    original_bytes: bytes | None = None
    original_filename: str | None = None
    content_markdown: str
    title: str | None = None
    author: str | None = None
    canonical_url: str | None = None
    published_at: str | None = None
    extraction_method: str
    extraction_quality: float = 1.0
    blocks: list[RawBlock] = Field(default_factory=list)
    # 受限来源（登录墙/反爬/验证码）时置 True，content_markdown 为空
    blocked_reason: str | None = None


# ---------------------------------------------------------------------------
# Source / 来源版本
# ---------------------------------------------------------------------------

SourceKind = Literal["text", "file", "web"]


class SourceVersionRecord(Contract):
    """manifest.yaml 中 versions[] 的一项。"""

    schema_version: int = 1
    version_id: str
    created_at: str
    extraction_method: str
    original_hash: str | None = None
    content_hash: str
    extraction_quality: float | None = None
    original_paths: list[str] = Field(default_factory=list)
    # 路径由 SourceRepository 写入时填充（versions/{vid}/...）；manifest 落盘必为真实值
    content_path: str = ""
    evidence_path: str = ""
    note: str | None = None


class SourceManifest(Contract):
    """10 Sources/{source_id}/manifest.yaml 的模型。"""

    schema_version: int = 1
    source_id: str
    source_type: SourceKind
    title: str
    author: str | None = None
    canonical_url: str | None = None
    published_at: str | None = None
    captured_at: str
    rights_status: str = "user_accessible"
    value_state: Literal["reference", "learn", "apply", "archived"] = "reference"
    active_version: str
    versions: list[SourceVersionRecord]


# ---------------------------------------------------------------------------
# Evidence / 证据
# ---------------------------------------------------------------------------


class EvidenceRef(Contract):
    """evidence.jsonl 的一行。绑定 source_id + version_id + content_hash + span_hash。"""

    schema_version: int = 1
    evidence_id: str
    source_id: str
    version_id: str
    content_hash: str
    span_hash: str
    anchor_type: str = "paragraph"
    anchor_start: str
    anchor_end: str | None = None
    text: str
    extraction_confidence: float = 1.0


# ---------------------------------------------------------------------------
# Wiki / KnowledgeNote
# ---------------------------------------------------------------------------

NoteStatus = Literal["draft", "reviewed", "disputed", "deprecated"]


class NoteFrontmatter(Contract):
    """Wiki 页面 YAML frontmatter。"""

    schema_version: int = 1
    note_id: str
    type: Literal["concept", "comparison", "synthesis", "open_question"] = "concept"
    title: str
    aliases: list[str] = Field(default_factory=list)
    status: NoteStatus = "draft"
    created_at: str
    updated_at: str
    last_verified_at: str | None = None

    @field_validator("created_at", "updated_at", "last_verified_at", mode="before")
    @classmethod
    def _date_to_iso(cls, value: object) -> object:
        """YAML 会把 2026-09-03 解析为 date；统一转 ISO 字符串。"""
        if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
            return value.isoformat()
        if isinstance(value, dt.datetime):
            return value.isoformat()
        return value


class ClaimInfo(Contract):
    """从 Wiki Markdown 解析出的 Claim Block（lint/索引用）。"""

    claim_block_id: str
    note_id: str
    claim_status: Literal["supported", "inference", "disputed", "superseded"] = "supported"
    valid_at: str | None = None
    review_after: str | None = None
    citations: list[str] = Field(default_factory=list)  # Evidence ID 列表
    text: str


# ---------------------------------------------------------------------------
# Learning / 学习
# ---------------------------------------------------------------------------

# 能力层级（规格《学习机制升级思路》§3.3）：可解释的离散层级，不是总分。
# unseen 仅表示尚无学习记录，不是可授予的证据层级。
CapabilityLevel = Literal[
    "unseen",
    "recognize",
    "recall",
    "explain",
    "discriminate",
    "near_transfer",
    "far_transfer",
    "real_application",
]

CAPABILITY_LEVELS: list[CapabilityLevel] = [
    "unseen",
    "recognize",
    "recall",
    "explain",
    "discriminate",
    "near_transfer",
    "far_transfer",
    "real_application",
]

# 误解错误类型（§4.2）：第一版封闭集合，新增类型必须有重复真实样本支持。
ErrorType = Literal[
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
]

ERROR_TYPES: list[ErrorType] = [
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
]

# 学习活动类型：低层（recall/explain/discriminate/apply）为既有四类，
# 高层（near_transfer/far_transfer/real_application）为迁移与应用证据（§5.2）。
ActivityType = Literal[
    "recall",
    "explain",
    "discriminate",
    "apply",
    "near_transfer",
    "far_transfer",
    "real_application",
]

ACTIVITY_TO_LEVEL: dict[str, CapabilityLevel] = {
    "recall": "recall",
    "explain": "explain",
    "discriminate": "discriminate",
    "apply": "discriminate",  # apply 证据按辨别层级记，真实应用需 real_application 活动与用户确认
    "near_transfer": "near_transfer",
    "far_transfer": "far_transfer",
    "real_application": "real_application",
}


class RubricItem(Contract):
    item: str
    met: bool


class AssessorInfo(Contract):
    type: Literal["agent", "user"] = "agent"
    model: str = "unknown"
    prompt_version: str | None = None
    self_reported: bool = True


# 应用结果分类（§5.7）
ApplicationOutcomeLiteral = Literal[
    "successful",
    "partial_success",
    "failed_execution",
    "wrong_model",
    "invalid_assumption",
    "knowledge_conflict",
    "inconclusive",
]


LearningEventType = Literal[
    # 既有（M3 基础闭环）
    "attempt_submitted",
    "confidence_recorded",
    "hint_used",
    "feedback_recorded",
    "review_scheduled",
    "application_recorded",
    # 学习机制升级 §7.3 第一版新增
    "goal_created",
    "goal_activated",
    "goal_paused",
    "goal_abandoned",
    "diagnostic_started",
    "diagnostic_completed",
    "misconception_proposed",
    "misconception_confirmed",
    "misconception_state_changed",
    "capability_evidence_added",
    "challenge_created",
    "application_prediction_recorded",
    "application_outcome_recorded",
    "knowledge_revalidation_required",
    "goal_achieved",
    # 扩展：会话开始/结束必须可见（§6.3），且 attempt 护栏依赖会话已建立
    "session_started",
    "session_ended",
]


class LearningEvent(Contract):
    """学习事件（追加型，不修改历史）。

    attempt_submitted 与 feedback_recorded 共用本模型：
    反馈前回答字段只在 attempt 事件中填，result/rubric 只在 feedback 事件中填。
    学习机制升级后的事件（goal/misconception/challenge/application 等）同样
    共用本模型，按 event_type 填写对应字段组；历史事件不可原地修改。
    """

    schema_version: int = 1
    event_id: str
    event_type: LearningEventType
    session_id: str | None = None
    learning_object_id: str | None = None
    occurred_at: str
    # attempt_submitted
    response_before_feedback: str | None = None
    confidence: int | None = Field(default=None, ge=0, le=100)
    hints_used: int | None = Field(default=None, ge=0)
    source_opened_before_answer: bool | None = None
    # feedback_recorded
    result: Literal["successful", "partial", "failed"] | None = None
    rubric_results: list[RubricItem] = Field(default_factory=list)
    feedback: str | None = None
    assessor: AssessorInfo | None = None
    # 复习排程
    next_review_at: str | None = None
    schedule_reason: str | None = None
    # 学习机制升级：目标 / 能力 / 误解 / 应用
    goal_id: str | None = None
    capability_id: str | None = None
    misconception_id: str | None = None
    challenge_id: str | None = None
    error_type: ErrorType | None = None
    level: CapabilityLevel | None = None
    diagnostic_summary: dict[str, Any] | None = None
    end_reason: str | None = None
    # application_prediction_recorded / application_outcome_recorded（§5.4/§5.5）
    context: str | None = None
    selected_knowledge: list[str] = Field(default_factory=list)
    reasoning_before_result: str | None = None
    predicted_outcome: str | None = None
    action_taken: str | None = None
    outcome: str | None = None
    user_assessment: ApplicationOutcomeLiteral | None = None
    lessons: str | None = None
    knowledge_update_candidate: bool | None = None


class KnowledgeSnapshotEvidence(Contract):
    """LearningObject 绑定的证据引用（§6.5）。"""

    evidence_id: str
    source_id: str
    version_id: str
    span_hash: str


class KnowledgeSnapshot(Contract):
    """学习对象创建时的知识版本快照：知识更新后据此检测过期。"""

    note_id: str
    note_hash: str
    evidence: list[KnowledgeSnapshotEvidence] = Field(default_factory=list)


class LearningObject(Contract):
    """40 Learning/Objects/{id}.yaml。"""

    schema_version: int = 1
    learning_object_id: str
    title: str
    knowledge_type: Literal["fact", "conceptual", "procedural"] = "conceptual"
    activity_type: ActivityType = "explain"
    prompt: str
    rubric: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    importance: Literal["low", "medium", "high"] = "medium"
    status: Literal["active", "paused", "retired", "needs_review"] = "active"
    created_by: Literal["user", "agent", "user_and_ai"] = "user_and_ai"
    created_at: str | None = None
    # 学习机制升级：目标、能力与知识版本绑定
    goal_id: str | None = None
    capability_id: str | None = None
    knowledge_snapshot: KnowledgeSnapshot | None = None


class MasterySummary(Contract):
    """可解释掌握摘要（规格 5.6）——没有单一 mastery 百分比。"""

    concept_id: str
    last_closed_book_attempt: str | None = None
    recall_result: str | None = None
    explain_result: str | None = None
    discriminate_result: str | None = None
    apply_result: str | None = None
    hints_used_last_time: int | None = None
    high_confidence_error: bool = False
    consecutive_successes: int = 0
    next_review_at: str | None = None
    evidence_event_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 学习机制升级：能力目标（§3）
# ---------------------------------------------------------------------------

GoalStatus = Literal[
    "draft",
    "active",
    "diagnosing",
    "learning",
    "transfer_testing",
    "application_pending",
    "achieved",
    "paused",
    "abandoned",
    "needs_revalidation",
]

GOAL_STATUSES: list[GoalStatus] = [
    "draft",
    "active",
    "diagnosing",
    "learning",
    "transfer_testing",
    "application_pending",
    "achieved",
    "paused",
    "abandoned",
    "needs_revalidation",
]


class GoalCapability(Contract):
    """目标内的一项能力：以可观察行为定义（§3.1）。"""

    capability_id: str
    behavior: str
    required_level: CapabilityLevel = "explain"

    @field_validator("required_level")
    @classmethod
    def _not_unseen(cls, value: str) -> str:
        if value == "unseen":
            raise ValueError("required_level 不能是 unseen（unseen 表示尚无学习记录）")
        return value


class GoalContext(Contract):
    project_ids: list[str] = Field(default_factory=list)
    motivation: str | None = None


class GoalKnowledgeDependencies(Contract):
    note_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class CompletionPolicy(Contract):
    """完成策略：进入 achieved 的确定性条件（§3.6）。"""

    require_all_capabilities: bool = True
    max_evidence_age_days: int = 90
    require_closed_book: bool = True
    require_user_confirmed_application: bool = True


class Goal(Contract):
    """40 Learning/Goals/{goal_id}.yaml。"""

    schema_version: int = 1
    goal_id: str
    title: str
    status: GoalStatus = "draft"
    created_at: str
    target_date: str | None = None
    importance: Literal["low", "medium", "high"] = "medium"
    context: GoalContext = Field(default_factory=GoalContext)
    capabilities: list[GoalCapability] = Field(default_factory=list)
    knowledge_dependencies: GoalKnowledgeDependencies = Field(
        default_factory=GoalKnowledgeDependencies
    )
    completion_policy: CompletionPolicy = Field(default_factory=CompletionPolicy)

    @field_validator("capabilities")
    @classmethod
    def _non_empty(cls, value: list[GoalCapability]) -> list[GoalCapability]:
        if not value:
            raise ValueError("Goal 至少包含一项能力")
        return value


# ---------------------------------------------------------------------------
# 学习机制升级：误解模型（§4）
# ---------------------------------------------------------------------------

MisconceptionStatus = Literal["candidate", "open", "improving", "resolved", "recurring"]

MISCONCEPTION_STATUSES: list[MisconceptionStatus] = [
    "candidate",
    "open",
    "improving",
    "resolved",
    "recurring",
]


class MisconceptionStatement(Contract):
    """用户原本相信什么，以及正确表述（correction 必须引用 Evidence）。"""

    user_belief: str
    correction: str
    error_type: ErrorType


class MisconceptionObservations(Contract):
    first_event_id: str | None = None
    last_event_id: str | None = None
    occurrences: int = 0
    max_confidence_when_wrong: int | None = Field(default=None, ge=0, le=100)
    contexts: list[str] = Field(default_factory=list)


class MisconceptionIntervention(Contract):
    activity: str
    result: Literal["recurred", "improving", "resolved"] = "recurred"


class MisconceptionResolutionPolicy(Contract):
    require_two_non_identical_contexts: bool = True
    require_closed_book: bool = True


class Misconception(Contract):
    """40 Learning/Misconceptions/{misconception_id}.yaml（§4.3）。"""

    schema_version: int = 1
    misconception_id: str
    status: MisconceptionStatus = "candidate"
    created_at: str
    concept_ids: list[str] = Field(default_factory=list)
    statement: MisconceptionStatement
    evidence_ids: list[str] = Field(default_factory=list)
    observations: MisconceptionObservations = Field(default_factory=MisconceptionObservations)
    intervention_history: list[MisconceptionIntervention] = Field(default_factory=list)
    resolution_policy: MisconceptionResolutionPolicy = Field(
        default_factory=MisconceptionResolutionPolicy
    )


# ---------------------------------------------------------------------------
# 学习机制升级：迁移与真实应用（§5）
# ---------------------------------------------------------------------------

ChallengeType = Literal[
    "real_project",
    "near_transfer_task",
    "far_transfer_task",
]

ChallengeStatus = Literal["active", "completed", "abandoned"]


class ChallengeContext(Contract):
    project_id: str | None = None
    problem: str
    constraints: list[str] = Field(default_factory=list)


class ChallengeKnowledgeRefs(Contract):
    note_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class ApplicationChallenge(Contract):
    """40 Learning/Challenges/{challenge_id}.yaml（§5.3）。"""

    schema_version: int = 1
    challenge_id: str
    goal_id: str
    capability_id: str
    type: ChallengeType = "real_project"
    status: ChallengeStatus = "active"
    created_at: str
    context: ChallengeContext
    required_reasoning: list[str] = Field(default_factory=list)
    knowledge_refs: ChallengeKnowledgeRefs = Field(default_factory=ChallengeKnowledgeRefs)
    success_criteria: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 学习机制升级：知识更新候选（§4.8，学习系统不能直接修改 Wiki）
# ---------------------------------------------------------------------------

KnowledgeUpdateCandidateReason = Literal[
    "evidence_self_contradictory",
    "rubric_conflicts_with_wiki",
    "source_version_changed_conclusion",
    "repeated_correct_answers_judged_wrong",
    "application_evidence_conflicts_with_current_claim",
]


class KnowledgeUpdateCandidate(Contract):
    """_System/Learning Wiki/UpdateCandidates/{candidate_id}.yaml。

    学习结果只能创建核验候选，由人工审阅后经 ChangeProposal 修改 Wiki。
    """

    schema_version: int = 1
    candidate_id: str
    created_at: str
    reason: KnowledgeUpdateCandidateReason
    related_claim_ids: list[str] = Field(default_factory=list)
    related_note_ids: list[str] = Field(default_factory=list)
    learning_event_ids: list[str] = Field(default_factory=list)
    detail: str | None = None
    requires_human_review: bool = True


# ---------------------------------------------------------------------------
# Proposal / 提案与安全写入
# ---------------------------------------------------------------------------

ProposalOpKind = Literal["create", "patch", "move_to_archive"]
ProposalState = Literal[
    "pending",
    "applying",
    "applied",
    "rejected",
    "expired_conflict",
    "interrupted",
    "needs_manual",
]


class ProposalOperation(Contract):
    operation: ProposalOpKind
    path: str
    base_hash: str | None = None  # create 时为 None
    result_hash: str
    patch: str | None = None  # unified diff，仅为审阅表示；权威载荷是 staged 后像
    content: str | None = None  # create / move_to_archive 操作的完整后像


class ProposalValidation(Contract):
    schema_valid: bool = False
    paths_valid: bool = False
    citations_valid: bool = False
    base_hashes_valid: bool = False
    result_hashes_valid: bool = False
    errors: list[str] = Field(default_factory=list)


class ChangeProposal(Contract):
    """_System/Learning Wiki/Proposals/pending/{proposal_id}.json。"""

    schema_version: int = 1
    proposal_id: str
    created_at: str
    created_by: Literal["agent", "user"] = "agent"
    reason: str
    trigger_source_versions: list[dict[str, str]] = Field(default_factory=list)
    cited_evidence_ids: list[str] = Field(default_factory=list)
    risk: Literal["low", "medium", "high"] = "medium"
    status: ProposalState = "pending"
    operations: list[ProposalOperation]
    validation: ProposalValidation | None = None


class FilePlanStep(Contract):
    path: str
    action: ProposalOpKind
    base_hash: str | None = None
    result_hash: str
    before_ref: str | None = None  # staging 内前像相对路径
    after_ref: str
    done: bool = False


class FilePlan(Contract):
    """已验证的文件计划：staging manifest 的模型（状态机权威文件）。"""

    schema_version: int = 1
    operation_id: str
    proposal_id: str | None = None
    state: Literal[
        "prepared",
        "applying",
        "applied",
        "rolling_back",
        "rolled_back",
        "needs_manual",
    ]
    backend: Literal["local_fsync"] = "local_fsync"
    steps: list[FilePlanStep]
    created_at: str
    updated_at: str


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------


class SearchHit(Contract):
    kind: Literal["source_version", "wiki_note"]
    source_id: str | None = None
    version_id: str | None = None
    note_id: str | None = None
    path: str | None = None
    title: str | None = None
    matched_evidence_id: str | None = None
    matched_text: str | None = None
    match_reason: str = ""
    content_type: Literal["source", "wiki", "user_note", "ai_inference"] = "source"
    occurred_at: str | None = None


class SearchResponse(Contract):
    query: str
    query_type: Literal["exact", "source_local", "conceptual", "temporal", "personal", "action"]
    trace: list[str] = Field(default_factory=list)
    hits: list[SearchHit] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


class FolderConfig(Contract):
    inbox: str = "00 Inbox"
    sources: str = "10 Sources"
    thoughts: str = "20 Thoughts"
    wiki: str = "30 Wiki"
    learning: str = "40 Learning"
    projects: str = "50 Projects"
    archive: str = "90 Archive"
    system: str = "_System/Learning Wiki"


class LibraryConfig(Contract):
    language: str = "zh-CN"
    timezone: str = "Asia/Shanghai"
    folders: FolderConfig = Field(default_factory=FolderConfig)


class CaptureConfig(Contract):
    store_original_web_html: bool = True
    default_value_state: Literal["reference", "learn", "apply"] = "reference"
    max_file_size_mb: int = 50


class RetrievalConfig(Contract):
    index_sources: bool = True
    index_wiki: bool = True
    index_thoughts: bool = False
    index_projects: bool = True
    embeddings_enabled: bool = False


class WikiConfig(Contract):
    require_citations_for_factual_ai_content: bool = True
    auto_apply: bool = False
    max_proposed_claims_per_run: int = 3


class LearningConfig(Contract):
    daily_review_limit: int = 10
    dashboard_priority_limit: int = 3
    require_closed_book_attempt: bool = True
    default_intervals_days: list[int] = Field(default_factory=lambda: [1, 3, 7, 21, 45, 90])


class GitConfig(Contract):
    enabled: bool = True
    auto_commit_applied_proposals: bool = False
    include_source_originals: bool = False


class PrivacyConfig(Contract):
    cloud_context_requires_preview: bool = True
    excluded_from_agent: list[str] = Field(default_factory=list)


class VaultConfig(Contract):
    """_System/Learning Wiki/Config.yaml。"""

    schema_version: int = 1
    library: LibraryConfig = Field(default_factory=LibraryConfig)
    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    wiki: WikiConfig = Field(default_factory=WikiConfig)
    learning: LearningConfig = Field(default_factory=LearningConfig)
    git: GitConfig = Field(default_factory=GitConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)


# ---------------------------------------------------------------------------
# 完整性告警 / 操作记录
# ---------------------------------------------------------------------------


class IntegrityAlert(Contract):
    schema_version: int = 1
    alert_id: str
    kind: str  # e.g. source_tampered, broken_citation, schema_version_unsupported
    subject: str
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    resolved_at: str | None = None


class OperationRecord(Contract):
    """Operations/*.jsonl 的一行（审计日志；恢复以 staging manifest 为准）。"""

    schema_version: int = 1
    operation_id: str
    kind: Literal["capture", "apply", "rebuild", "migrate", "recovery"]
    proposal_id: str | None = None
    state: str
    started_at: str
    finished_at: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


# 导出给 scripts/export_schemas.py 的注册表
EXPORTED_MODELS: dict[str, type[Contract]] = {
    "inbox-item": InboxItem,
    "extraction-result": ExtractionResult,
    "source-manifest": SourceManifest,
    "source-version-record": SourceVersionRecord,
    "evidence": EvidenceRef,
    "note-frontmatter": NoteFrontmatter,
    "claim-info": ClaimInfo,
    "learning-object": LearningObject,
    "learning-event": LearningEvent,
    "mastery-summary": MasterySummary,
    "goal": Goal,
    "misconception": Misconception,
    "application-challenge": ApplicationChallenge,
    "knowledge-update-candidate": KnowledgeUpdateCandidate,
    "change-proposal": ChangeProposal,
    "file-plan": FilePlan,
    "search-response": SearchResponse,
    "vault-config": VaultConfig,
    "integrity-alert": IntegrityAlert,
    "operation-record": OperationRecord,
}
