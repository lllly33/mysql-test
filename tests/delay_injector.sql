-- ============================================================================
-- InnoDB B+Tree Performance Test: Delay Injector & Setup
-- ============================================================================
-- 本脚本用于：
-- 1. 创建测试表与索引
-- 2. 初始化性能监测
-- 3. 设置延迟注入参数（模拟 ESSD）
-- ============================================================================

-- 创建测试数据库（如果不存在）
CREATE DATABASE IF NOT EXISTS test_innodb;
USE test_innodb;

-- ============================================================================
-- 1. 创建主测试表（聚集索引将触发 B+树分裂）
-- ============================================================================

DROP TABLE IF EXISTS test_table;

CREATE TABLE test_table (
    id BIGINT NOT NULL AUTO_INCREMENT,
    data_key BIGINT NOT NULL COMMENT '用于范围查询的键',
    payload VARCHAR(1000) NOT NULL COMMENT '填充数据，增加页面压力',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (id),
    KEY idx_data_key (data_key)
) ENGINE=InnoDB
DEFAULT CHARSET=utf8mb4
COLLATE=utf8mb4_general_ci
COMMENT='高并发插入测试表，触发 B+树 SMO';

-- ============================================================================
-- 2. 创建监测视图（用于收集性能数据）
-- ============================================================================

-- 锁等待统计视图
CREATE OR REPLACE VIEW v_lock_waits AS
SELECT
    waiting_trx_id AS REQUESTING_TRX_ID,
    blocking_trx_id AS BLOCKING_TRX_ID,
    SUBSTRING_INDEX(REPLACE(locked_table, '`', ''), '.', 1) AS OBJECT_SCHEMA,
    SUBSTRING_INDEX(REPLACE(locked_table, '`', ''), '.', -1) AS OBJECT_NAME,
    locked_type AS LOCK_TYPE,
    waiting_lock_mode AS LOCK_MODE,
    COUNT(*) AS wait_count
FROM sys.innodb_lock_waits
GROUP BY waiting_trx_id, blocking_trx_id, OBJECT_SCHEMA,
         OBJECT_NAME, locked_type, waiting_lock_mode;

-- 表 I/O 等待统计视图
CREATE OR REPLACE VIEW v_table_io_waits AS
SELECT
    OBJECT_SCHEMA,
    OBJECT_NAME,
    COUNT_STAR as total_ops,
    SUM_TIMER_WAIT / 1e12 as total_wait_sec,
    AVG_TIMER_WAIT / 1e9 as avg_wait_ms,
    MAX_TIMER_WAIT / 1e9 as max_wait_ms
FROM performance_schema.table_io_waits_summary_by_table
WHERE OBJECT_SCHEMA NOT IN ('mysql', 'performance_schema', 'information_schema')
ORDER BY SUM_TIMER_WAIT DESC;

-- ============================================================================
-- 3. 初始化性能监测（清空历史数据）
-- ============================================================================

-- 截断历史表，确保测试从干净状态开始
TRUNCATE TABLE performance_schema.events_statements_summary_global_by_event_name;
TRUNCATE TABLE performance_schema.table_io_waits_summary_by_table;

-- 注：performance_schema.metadata_locks 是动态表，不能通过 TRUNCATE 重置

-- ============================================================================
-- 4. 设置延迟注入参数（如果 MySQL 支持）
-- ============================================================================

-- 注：当前测试框架不依赖/不提供“延迟注入”。
-- 以下内容仅作为占位说明：如果你未来在 MySQL 代码中实现了相关变量，如何在 SQL 层控制。

-- 方法 1：如果已经在代码中实现了 innodb_page_latency_us 变量
-- SET GLOBAL innodb_page_latency_us = 0;      -- NVMe 基准（10us 由硬件决定）
-- SET SESSION innodb_page_latency_us = 200;   -- ESSD 延迟（200us）

-- 方法 2：通过事件追踪（可以在会话中使用）
-- 这需要 MySQL 的自定义变量支持

-- ============================================================================
-- 5. 辅助存储过程：快速清理与重置
-- ============================================================================

DROP PROCEDURE IF EXISTS reset_test_table;

DELIMITER //
CREATE PROCEDURE reset_test_table()
BEGIN
    -- 清空测试表数据
    DELETE FROM test_table;

    -- 重置 AUTO_INCREMENT（强制重新从头分配 ID，重现分裂）
    ALTER TABLE test_table AUTO_INCREMENT = 1;

    -- 强制重建索引（清理分裂碎片）
    OPTIMIZE TABLE test_table;

    SELECT 'Test table reset successfully' as message;
END //
DELIMITER ;

-- ============================================================================
-- 6. 辅助存储过程：收集当前性能指标快照
-- ============================================================================

DROP PROCEDURE IF EXISTS snapshot_metrics;

DELIMITER //
CREATE PROCEDURE snapshot_metrics(
    IN p_snapshot_name VARCHAR(100)
)
BEGIN
    INSERT INTO performance_snapshot (snapshot_name, snapshot_time, metric_data)
    SELECT
        p_snapshot_name,
        NOW(),
        JSON_OBJECT(
            'total_queries', SUM(COUNT_STAR),
            'total_wait_time_sec', SUM(SUM_TIMER_WAIT) / 1e12,
            'avg_latency_ms', AVG(AVG_TIMER_WAIT) / 1e9
        )
    FROM performance_schema.table_io_waits_summary_by_table
    WHERE OBJECT_SCHEMA = 'test_innodb';

    SELECT 'Metrics snapshot saved' as message;
END //
DELIMITER ;

-- ============================================================================
-- 7. 创建性能快照表（用于对比多次运行）
-- ============================================================================

DROP TABLE IF EXISTS performance_snapshot;

CREATE TABLE performance_snapshot (
    snapshot_id INT AUTO_INCREMENT PRIMARY KEY,
    snapshot_name VARCHAR(100) NOT NULL,
    snapshot_time TIMESTAMP,
    metric_data JSON NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

-- ============================================================================
-- 8. 验证设置
-- ============================================================================

SELECT 'InnoDB Performance Test Setup Complete' as setup_status;

SELECT
    TABLE_NAME,
    ENGINE,
    TABLE_ROWS,
    DATA_LENGTH,
    INDEX_LENGTH
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = 'test_innodb'
ORDER BY TABLE_NAME;

-- ============================================================================
-- 9. 快速测试：验证 INSERT 功能是否正常
-- ============================================================================

-- 插入 100 条测试记录
INSERT INTO test_table (data_key, payload)
SELECT
    FLOOR(RAND() * 10000) as data_key,
    REPEAT('x', FLOOR(RAND() * 900) + 100) as payload
FROM (
    SELECT @row := @row + 1
    FROM information_schema.TABLES, (SELECT @row := 0) r
    LIMIT 100
) t;

SELECT COUNT(*) as inserted_rows FROM test_table;

-- ============================================================================
-- 完成
-- ============================================================================
-- 现在你可以：
-- 1. 在 Python 脚本中连接这个数据库
-- 2. 调用 reset_test_table() 重置状态
-- 3. 运行大量并发 INSERT
-- 4. 查询性能视图获取监测数据
-- ============================================================================
