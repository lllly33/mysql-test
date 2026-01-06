-- B+树分裂复现工作负载（单实例、单会话即可触发）
-- 目标：在叶子页装满后进行分裂，便于在 gdb 观察 btr_page_split_and_insert

-- 清理并准备
DROP DATABASE IF EXISTS blink_dbg;
CREATE DATABASE blink_dbg;
USE blink_dbg;

-- 表设计：主键顺序插入，行稍大一些，便于快速填满页
-- 注：页大小由实例初始化决定，这里不修改
CREATE TABLE t (
  k BIGINT PRIMARY KEY,
  pad VARBINARY(200) -- 二百字节的 payload，便于快速占满页
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC;

-- 生成 50k 行顺序主键，强制右侧分裂路径（使用 information_schema 派生序列，避免 CTE 兼容性问题）
SET @n := 0;
INSERT INTO t(k, pad)
SELECT @n := @n + 1 AS n, REPEAT(0x78, 200)
FROM information_schema.COLUMNS a
JOIN information_schema.COLUMNS b
LIMIT 50000;

-- 再插入一批非顺序主键，加速产生跨页分裂（打乱主键顺序，用 IGNORE 避免冲突）
SET @m := 50000;
INSERT IGNORE INTO t(k, pad)
SELECT (n * 7919) % 9000000 + 1, REPEAT(0x79, 200)
FROM (
  SELECT @m := @m + 1 AS n
  FROM information_schema.COLUMNS a
  JOIN information_schema.COLUMNS b
  LIMIT 30000
) AS seq2;

-- 小查询以避免优化器延迟执行
SELECT COUNT(*) AS rows_after_load FROM t;
