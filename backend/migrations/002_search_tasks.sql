-- 受限 3NF 分表求解：搜索任务、可续跑工作单元与幂等事件流。
-- 权威模型见 src/models/entities.py；本文件供 MySQL 手工建表/评审参考，
-- 运行时仍由 SQLAlchemy metadata.create_all 幂等初始化。

CREATE TABLE IF NOT EXISTS search_tasks (
  id INTEGER PRIMARY KEY AUTO_INCREMENT,
  name VARCHAR(120) NOT NULL,
  relation_id INTEGER NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'queued',
  spec JSON NOT NULL,
  precheck JSON NULL,
  best_solution JSON NULL,
  report JSON NULL,
  error TEXT NULL,
  stats JSON NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX ix_search_tasks_status (status),
  CONSTRAINT fk_search_tasks_relation FOREIGN KEY (relation_id) REFERENCES relations (id)
);

CREATE TABLE IF NOT EXISTS search_units (
  id INTEGER PRIMARY KEY AUTO_INCREMENT,
  task_id INTEGER NOT NULL,
  unit_key VARCHAR(120) NOT NULL,
  level_limit INTEGER NOT NULL,
  stack JSON NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'pending',
  lease_owner VARCHAR(80) NULL,
  lease_expires DOUBLE NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_search_unit_key (task_id, unit_key),
  INDEX ix_search_unit_claim (task_id, status, lease_expires),
  CONSTRAINT fk_search_units_task FOREIGN KEY (task_id) REFERENCES search_tasks (id)
);

CREATE TABLE IF NOT EXISTS search_events (
  id INTEGER PRIMARY KEY AUTO_INCREMENT,
  task_id INTEGER NOT NULL,
  event_key VARCHAR(160) NOT NULL,
  message VARCHAR(400) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_search_event_key (task_id, event_key),
  INDEX ix_search_event_task (task_id, id),
  CONSTRAINT fk_search_events_task FOREIGN KEY (task_id) REFERENCES search_tasks (id)
);
