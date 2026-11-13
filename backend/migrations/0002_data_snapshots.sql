-- sampleaqp 数据快照（不可变版本）迁移：MySQL 8.0 / utf8mb4
--
-- 背景：线路数据每半小时重跑，层容量/均值/波动会变。刷新不再原地改写总额，
-- 而是把整版层口径冻结成一条不可变 data_snapshots 行，并在单事务内切换
-- datasets.current_snapshot_id 指针。方案/查询都绑定自己那一版快照。
--
-- 幂等：全部用 IF NOT EXISTS，可重复执行；空库也可直接由 0001 + 本脚本建表。
-- 注意：datasets 与 data_snapshots 之间是循环外键，指针外键在快照表建好后补。

-- 1) 不可变快照表
CREATE TABLE IF NOT EXISTS data_snapshots (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  dataset_id BIGINT NOT NULL,
  code VARCHAR(64) NOT NULL UNIQUE,
  version INT NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'published',
  content_fingerprint VARCHAR(64) NOT NULL,
  row_count BIGINT NOT NULL DEFAULT 0,
  truth_total_minor BIGINT NOT NULL DEFAULT 0,
  measured_full_scan_ms INT NULL,
  precision_target_pct DECIMAL(5,2) NOT NULL,
  confidence_level DECIMAL(4,3) NOT NULL,
  strata_json JSON NOT NULL,
  note VARCHAR(400) NULL,
  created_by VARCHAR(160) NOT NULL DEFAULT 'system',
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  published_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  UNIQUE KEY uq_snapshot_dataset_version (dataset_id, version),
  KEY ix_snapshot_dataset (dataset_id),
  CONSTRAINT fk_snapshot_dataset FOREIGN KEY (dataset_id) REFERENCES datasets(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 2) 数据集当前快照指针（可空，循环外键在快照表存在后补）
SET @col_exists := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'datasets' AND COLUMN_NAME = 'current_snapshot_id'
);
SET @ddl := IF(@col_exists = 0,
  'ALTER TABLE datasets ADD COLUMN current_snapshot_id BIGINT NULL',
  'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @fk_exists := (
  SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'datasets' AND CONSTRAINT_NAME = 'fk_dataset_current_snapshot'
);
SET @ddl := IF(@fk_exists = 0,
  'ALTER TABLE datasets ADD CONSTRAINT fk_dataset_current_snapshot FOREIGN KEY (current_snapshot_id) REFERENCES data_snapshots(id)',
  'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 3) 方案绑定快照（旧数据先置 NULL，由应用启动时的基线快照补齐并回填）
SET @col_exists := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'sample_plans' AND COLUMN_NAME = 'snapshot_id'
);
SET @ddl := IF(@col_exists = 0,
  'ALTER TABLE sample_plans ADD COLUMN snapshot_id BIGINT NULL',
  'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @fk_exists := (
  SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'sample_plans' AND CONSTRAINT_NAME = 'fk_plan_snapshot'
);
SET @ddl := IF(@fk_exists = 0,
  'ALTER TABLE sample_plans ADD CONSTRAINT fk_plan_snapshot FOREIGN KEY (snapshot_id) REFERENCES data_snapshots(id) ON DELETE RESTRICT',
  'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 4) 查询结果固化快照版本
SET @col_exists := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'approx_queries' AND COLUMN_NAME = 'snapshot_id'
);
SET @ddl := IF(@col_exists = 0,
  'ALTER TABLE approx_queries ADD COLUMN snapshot_id BIGINT NULL',
  'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @col_exists := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'approx_queries' AND COLUMN_NAME = 'snapshot_code'
);
SET @ddl := IF(@col_exists = 0,
  'ALTER TABLE approx_queries ADD COLUMN snapshot_code VARCHAR(64) NULL',
  'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @fk_exists := (
  SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'approx_queries' AND CONSTRAINT_NAME = 'fk_query_snapshot'
);
SET @ddl := IF(@fk_exists = 0,
  'ALTER TABLE approx_queries ADD CONSTRAINT fk_query_snapshot FOREIGN KEY (snapshot_id) REFERENCES data_snapshots(id) ON DELETE RESTRICT',
  'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 5) 存量数据回填：每个数据集补一条 v1 基线快照，冻结当前活动分层表口径与真值
INSERT INTO data_snapshots (
  dataset_id, code, version, status, content_fingerprint,
  row_count, truth_total_minor, measured_full_scan_ms,
  precision_target_pct, confidence_level, strata_json, note, created_by, created_at, published_at
)
SELECT
  d.id,
  CONCAT('SNAP-', d.code, '-v001') AS code,
  1 AS version,
  'published' AS status,
  SHA2(CONCAT('baseline:', d.id, ':', COALESCE(d.truth_total_minor, 0)), 256) AS content_fingerprint,
  d.row_count,
  COALESCE(d.truth_total_minor, 0),
  d.measured_full_scan_ms,
  d.precision_target_pct,
  d.confidence_level,
  COALESCE((
    SELECT JSON_ARRAYAGG(
      JSON_OBJECT(
        'code', s.code,
        'label', s.label,
        'population', s.population,
        'mean_minor', s.mean_minor,
        'mean_value', s.mean_minor,
        'prior_std', s.prior_std_minor,
        'min_sample', s.min_sample,
        'sort_order', s.sort_order
      )
    )
    FROM (SELECT * FROM dataset_strata WHERE dataset_id = d.id ORDER BY sort_order) s
  ), JSON_ARRAY()) AS strata_json,
  'v1 基线快照：冻结升级前的分层口径与全量真值' AS note,
  'migration' AS created_by,
  CURRENT_TIMESTAMP(3),
  CURRENT_TIMESTAMP(3)
FROM datasets d
WHERE d.current_snapshot_id IS NULL
  AND NOT EXISTS (SELECT 1 FROM data_snapshots ds WHERE ds.dataset_id = d.id AND ds.version = 1);

-- 6) 回填数据集指针与方案/查询的快照绑定
UPDATE datasets d
SET d.current_snapshot_id = (
  SELECT ds.id FROM data_snapshots ds WHERE ds.dataset_id = d.id ORDER BY ds.version LIMIT 1
)
WHERE d.current_snapshot_id IS NULL;

UPDATE sample_plans p
SET p.snapshot_id = (
  SELECT d.current_snapshot_id FROM datasets d WHERE d.id = p.dataset_id
)
WHERE p.snapshot_id IS NULL;

UPDATE approx_queries q
SET q.snapshot_id = (
  SELECT d.current_snapshot_id FROM datasets d WHERE d.id = q.dataset_id
),
q.snapshot_code = (
  SELECT ds.code
  FROM data_snapshots ds
  WHERE ds.id = (SELECT d2.current_snapshot_id FROM datasets d2 WHERE d2.id = q.dataset_id)
)
WHERE q.snapshot_id IS NULL;
