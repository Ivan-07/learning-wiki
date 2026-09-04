"""ID 生成规则。

- source_id: ``src_`` + ULID（时间可排序）
- version_id: ``v0001`` 递增
- evidence_id: ``ev_{src短}_{version_id}_{块序号:04d}``（下划线）
- block_id:   与 evidence_id 一一对应，但用连字符（Obsidian 块 ID 只允许
  字母数字与连字符）
"""

from __future__ import annotations

import re

import ulid as _ulid

SOURCE_PREFIX = "src_"
INBOX_PREFIX = "inbox_"
PROPOSAL_PREFIX = "prop_"
EVENT_PREFIX = "evt_"
OPERATION_PREFIX = "op_"
ALERT_PREFIX = "alert_"
SESSION_PREFIX = "session_"
GOAL_PREFIX = "goal_"
MISCONCEPTION_PREFIX = "mis_"
CHALLENGE_PREFIX = "challenge_"
UPDATE_CANDIDATE_PREFIX = "kuc_"
OBJECT_PREFIX = "lo_"

_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def new_ulid() -> str:
    return str(_ulid.ULID())


def new_source_id() -> str:
    return SOURCE_PREFIX + new_ulid()


def new_inbox_id() -> str:
    return INBOX_PREFIX + new_ulid()


def new_proposal_id() -> str:
    return PROPOSAL_PREFIX + new_ulid()


def new_event_id() -> str:
    return EVENT_PREFIX + new_ulid()


def new_operation_id() -> str:
    return OPERATION_PREFIX + new_ulid()


def new_alert_id() -> str:
    return ALERT_PREFIX + new_ulid()


def new_session_id() -> str:
    return SESSION_PREFIX + new_ulid()


def new_goal_id() -> str:
    return GOAL_PREFIX + new_ulid()


def new_misconception_id() -> str:
    return MISCONCEPTION_PREFIX + new_ulid()


def new_challenge_id() -> str:
    return CHALLENGE_PREFIX + new_ulid()


def new_update_candidate_id() -> str:
    return UPDATE_CANDIDATE_PREFIX + new_ulid()


def new_learning_object_id() -> str:
    return OBJECT_PREFIX + new_ulid()


def next_version_id(existing: list[str]) -> str:
    """给定已有 version_id 列表，返回下一个（v0001 起，四位递增）。"""
    seq = 0
    for vid in existing:
        m = re.fullmatch(r"v(\d+)", vid)
        if m:
            seq = max(seq, int(m.group(1)))
    return f"v{seq + 1:04d}"


def source_short_id(source_id: str) -> str:
    """source_id 去前缀后的 ULID 主体，用于 evidence/block ID。

    ULID 前 10 位是毫秒时间戳、后 16 位是随机数；默认生成器为单调策略，
    同毫秒内只在随机部分末尾递增。**任何截断**都可能碰撞（实测前 6 位和
    前 14 位都会在连续捕获时冲突，导致 evidence_id 主键互相覆盖），
    因此使用完整 26 位——生成器保证全局唯一。
    """
    return source_id.removeprefix(SOURCE_PREFIX)


def evidence_id_for(source_id: str, version_id: str, seq: int) -> str:
    return f"ev_{source_short_id(source_id)}_{version_id}_{seq:04d}"


def block_id_for(source_id: str, version_id: str, seq: int) -> str:
    """Obsidian 块 ID：字母数字与连字符。"""
    return f"ev-{source_short_id(source_id)}-{version_id}-{seq:04d}"


def evidence_id_to_block_id(evidence_id: str) -> str:
    return evidence_id.replace("_", "-")


def block_id_to_evidence_id(block_id: str) -> str:
    return block_id.replace("-", "_")


def is_valid_source_id(value: str) -> bool:
    body = value.removeprefix(SOURCE_PREFIX)
    return value.startswith(SOURCE_PREFIX) and bool(_ULID_RE.fullmatch(body))


def is_valid_evidence_id(value: str) -> bool:
    return bool(re.fullmatch(r"ev_[0-9A-Za-z]+_v\d{4}_\d{4}", value))
