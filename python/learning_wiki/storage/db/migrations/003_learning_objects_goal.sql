-- 学习机制升级：learning_objects 增加目标 / 能力 / 知识快照绑定列。

ALTER TABLE learning_objects ADD COLUMN goal_id TEXT;
ALTER TABLE learning_objects ADD COLUMN capability_id TEXT;
ALTER TABLE learning_objects ADD COLUMN knowledge_snapshot_json TEXT;
