-- 001_initial.sql —— M0 全量派生库 schema
-- 本库为设备级派生状态，可删除后由 `lw rebuild` 从文件事实源重建。

-- ============ 来源 ============
CREATE TABLE sources(
  source_id TEXT PRIMARY KEY,
  source_type TEXT NOT NULL,
  title TEXT NOT NULL,
  author TEXT,
  canonical_url TEXT,
  published_at TEXT,
  value_state TEXT NOT NULL DEFAULT 'reference',
  active_version_id TEXT,
  manifest_path TEXT NOT NULL,
  first_captured_at TEXT,
  updated_at TEXT
);

CREATE TABLE source_versions(
  id INTEGER PRIMARY KEY,                -- FTS rowid 别名
  source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
  version_id TEXT NOT NULL,
  version_seq INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  extraction_method TEXT,
  original_hash TEXT,
  content_hash TEXT NOT NULL,
  content_path TEXT NOT NULL,
  evidence_path TEXT,
  extraction_quality REAL,
  UNIQUE(source_id, version_id)
);
CREATE INDEX idx_versions_content_hash ON source_versions(content_hash);

CREATE TABLE evidence(
  evidence_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  version_id TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  span_hash TEXT NOT NULL,
  anchor_type TEXT,
  block_id TEXT NOT NULL,
  text TEXT NOT NULL,
  extraction_confidence REAL,
  trusted INTEGER NOT NULL DEFAULT 1,
  UNIQUE(source_id, version_id, block_id)
);
CREATE INDEX idx_evidence_by_version ON evidence(source_id, version_id);

-- ============ Wiki 与反向引用 ============
CREATE TABLE wiki_notes(
  note_id TEXT PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  title TEXT,
  note_type TEXT,
  status TEXT,
  file_hash TEXT,
  mtime REAL,
  updated_at TEXT
);

CREATE TABLE claims(
  claim_block_id TEXT PRIMARY KEY,
  note_id TEXT NOT NULL REFERENCES wiki_notes(note_id) ON DELETE CASCADE,
  claim_status TEXT,
  valid_at TEXT,
  review_after TEXT,
  raw_text TEXT
);
CREATE INDEX idx_claims_review ON claims(review_after) WHERE review_after IS NOT NULL;

CREATE TABLE claim_evidence(
  claim_block_id TEXT NOT NULL REFERENCES claims(claim_block_id) ON DELETE CASCADE,
  evidence_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  version_id TEXT NOT NULL,
  PRIMARY KEY(claim_block_id, evidence_id)
);
CREATE INDEX idx_ce_by_evidence ON claim_evidence(evidence_id);

CREATE TABLE note_links(
  from_note TEXT NOT NULL,
  to_note TEXT,
  to_path TEXT,
  link_text TEXT,
  PRIMARY KEY(from_note, to_path, link_text)
);

-- ============ Inbox / 学习 ============
CREATE TABLE inbox_items(
  item_id TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  input_type TEXT,
  payload_path TEXT,
  value_state TEXT,
  source_id TEXT,
  version_id TEXT,
  error TEXT,
  created_at TEXT,
  processed_at TEXT
);

CREATE TABLE learning_objects(
  learning_object_id TEXT PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  title TEXT,
  knowledge_type TEXT,
  activity_type TEXT,
  status TEXT,
  importance TEXT,
  concept_note_id TEXT,
  next_review_at TEXT,
  last_result TEXT,
  consecutive_successes INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE learning_events(
  event_id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  session_id TEXT,
  learning_object_id TEXT,
  occurred_at TEXT NOT NULL,
  record_path TEXT NOT NULL,             -- 指向 JSONL 事实源
  payload_hash TEXT NOT NULL             -- 事件重放幂等校验
);
CREATE INDEX idx_events_object_time ON learning_events(learning_object_id, occurred_at);

CREATE TABLE review_queue(
  learning_object_id TEXT PRIMARY KEY,
  next_review_at TEXT NOT NULL,
  reason TEXT,
  derived_from_event_id TEXT
);

-- ============ 提案与操作 ============
CREATE TABLE proposals(
  proposal_id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  path TEXT NOT NULL,
  risk TEXT,
  created_by TEXT,
  created_at TEXT,
  operation_count INTEGER,
  validation_json TEXT
);

CREATE TABLE operations(
  operation_id TEXT PRIMARY KEY,
  proposal_id TEXT,
  kind TEXT NOT NULL,
  state TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  staging_dir TEXT,
  detail_json TEXT
);

CREATE TABLE integrity_alerts(
  alert_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  subject TEXT NOT NULL,
  detail_json TEXT,
  created_at TEXT NOT NULL,
  resolved_at TEXT
);

CREATE TABLE index_state(
  path TEXT PRIMARY KEY,
  file_hash TEXT,
  mtime REAL,
  size INTEGER,
  indexed_at TEXT
);

-- ============ FTS5 ============
-- 主索引：jieba 预分词列 + unicode61（写入与查询都先分词再入索引）
CREATE VIRTUAL TABLE fts_sources USING fts5(
  title_seg, body_seg,
  tokenize='unicode61 remove_diacritics 2'
);  -- rowid == source_versions.id；变更时 delete+insert

CREATE VIRTUAL TABLE fts_notes USING fts5(
  title_seg, body_seg,
  tokenize='unicode61 remove_diacritics 2'
);
CREATE TABLE fts_notes_map(
  fts_rowid INTEGER PRIMARY KEY,
  note_id TEXT NOT NULL UNIQUE
);

-- 辅助：trigram 虚表，仅服务逐字精确子串检索（"找到原话"场景）
CREATE VIRTUAL TABLE fts_exact USING fts5(
  body,
  tokenize='trigram'
);  -- rowid == source_versions.id
