-- 学习机制三项升级（能力目标 / 误解模型 / 迁移与应用）派生表。
-- 全部为派生状态：可删除，rebuild 从 YAML 与 JSONL 事实源重建（规格 §7.2）。

CREATE TABLE learning_goals(
  goal_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  status TEXT NOT NULL,
  importance TEXT,
  created_at TEXT,
  target_date TEXT,
  path TEXT NOT NULL UNIQUE,           -- 指向 YAML 事实源
  capability_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE goal_capabilities(
  goal_id TEXT NOT NULL REFERENCES learning_goals(goal_id) ON DELETE CASCADE,
  capability_id TEXT NOT NULL,
  behavior TEXT,
  required_level TEXT NOT NULL,
  current_level TEXT NOT NULL DEFAULT 'unseen',  -- 当前最高证据（派生）
  PRIMARY KEY (goal_id, capability_id)
);

CREATE TABLE capability_evidence(
  goal_id TEXT NOT NULL,
  capability_id TEXT NOT NULL,
  level TEXT NOT NULL,
  event_id TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  activity_type TEXT,
  hint_free INTEGER NOT NULL DEFAULT 0,
  closed_book INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (goal_id, capability_id, event_id)
);
CREATE INDEX idx_cap_evidence_level ON capability_evidence(goal_id, capability_id, level);

CREATE TABLE misconceptions(
  misconception_id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  error_type TEXT,
  user_belief TEXT,
  correction TEXT,
  created_at TEXT,
  occurrences INTEGER NOT NULL DEFAULT 0,
  max_confidence_when_wrong INTEGER,
  priority TEXT NOT NULL DEFAULT 'normal',  -- high | normal（高置信度错误）
  path TEXT NOT NULL UNIQUE
);

CREATE TABLE misconception_occurrences(
  misconception_id TEXT NOT NULL REFERENCES misconceptions(misconception_id) ON DELETE CASCADE,
  event_id TEXT NOT NULL,
  context TEXT,
  occurred_at TEXT NOT NULL,
  confidence INTEGER,
  PRIMARY KEY (misconception_id, event_id)
);

CREATE TABLE application_challenges(
  challenge_id TEXT PRIMARY KEY,
  goal_id TEXT NOT NULL,
  capability_id TEXT,
  type TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT,
  path TEXT NOT NULL UNIQUE
);
CREATE INDEX idx_challenges_goal ON application_challenges(goal_id);

CREATE TABLE knowledge_snapshot_refs(
  learning_object_id TEXT NOT NULL,
  note_id TEXT NOT NULL,
  note_hash TEXT NOT NULL,
  evidence_id TEXT NOT NULL DEFAULT '',
  source_id TEXT,
  version_id TEXT,
  span_hash TEXT,
  PRIMARY KEY (learning_object_id, note_id, evidence_id)
);
