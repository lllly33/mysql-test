降低 merge_threshold# InnoDB B+Tree 锁与性能测试完整指南

## 概要

本文档描述一套完整的性能测试框架，用来衡量 InnoDB B+树 SMO（Structure Modification Operations，如分裂/合并）操作中的**锁持有时间**对并发性能的影响，特别是在**高延迟存储**（ESSD 200us vs NVMe 10us，**20 倍延迟**）场景下。

### 核心问题

- **NVMe 场景**：分裂持有锁 100us，其他线程等待 100us，影响有限
- **ESSD 场景**：同一分裂操作持有锁 2000us（20 倍）， **其他 99 个并发线程都被堵在这 2000us**
  - 单个线程的延迟 × 线程数 = 全局吞吐量下降
  - 假设 100 个并发线程，有 1 个线程触发分裂，锁 2000us，则这 2ms 内其他 99 个线程都在等锁
  - 相当于 99 个线程的 2ms 全部浪费了，这就是**吞吐量崩溃**的根源

### 改进目标

- 缩短分裂时 **X-Latch 的持有时间**（或用 SX-Latch、则Latch 等弱锁）
- 减少分裂中的 **磁盘 I/O 调用次数**（每次多 200us，无法避免）
- **提前释放不必要的锁**（如根提升完成后立即释放树闩）

---

## 测试流程（四层体系）

### 第一层：工作负载设计（What to test）

#### 目标：构造一个容易触发 SMO（分裂/合并）的场景

**场景 A：高并发单值插入（最容易触发分裂）**

```
100 个线程
├─ 线程 1：插入 10000 条 (id=1, id=1, id=1...)
├─ 线程 2：插入 10000 条 (id=2, id=2, id=2...)
└─ 线程 100：插入 10000 条 (id=100, id=100, id=100...)

结果：同一个索引节点被 100 个线程并发写入 → 快速填满 → 分裂
     分裂时整个树被 X-Latch 锁住，其他 99 个线程都等待
```

**场景 B：范围插入（渐进式填满页面）**

```
100 个线程
├─ 线程 1：插入 id=1~10000
├─ 线程 2：插入 id=10001~20000
└─ 线程 100：插入 id=990001~1000000

结果：页面填充更均匀，分裂频率相对较低
     但当最右叶节点分裂时，影响范围最大
```

**场景 C：范围读 + 插入混合（观察行锁与闩的交互）**

```
50 个插入线程 + 50 个读取线程
├─ 插入线程：INSERT 新数据
└─ 读线程：SELECT ... WHERE id BETWEEN x AND y

结果：读线程的下一键锁（Next-Key Lock）与插入的间隙锁（Gap Lock）竞争
     在 ESSD 延迟下，等待时间会很长
```

**选择建议：** 先从 **场景 A（单值并发插入）** 开始，最容易看到 SMO 的影响。

---

### 第二层：监测与延迟注入（How to observe）

#### 2.1 性能监测的三个维度

**维度 1：吞吐量和延迟**
- 指标：TPS（每秒事务数）、P50/P95/P99 响应时间
- 来源：Sysbench 输出 或 custom Python 脚本计时
- 含义：能直观看到"改代码后吞吐快了多少、延迟降了多少"

**维度 2：锁等待时间**
- 指标：事务被锁阻挡的时长、等待队列长度、死锁次数
- 来源：`performance_schema.metadata_locks`、`INNODB_LOCK_WAITS`
- 含义：定位"到底是哪个锁在卡"

**维度 3：SMO 发生频率和耗时**
- 指标：分裂/合并次数、单次 SMO 耗时、持有树闩的时长
- 来源：InnoDB Status、自定义计数器或日志
- 含义：定量看"代码改动有没有真的减少 SMO 时间"

#### 2.2 启用 Performance Schema（MySQL 监测工具）

**编辑 my.cnf**
```ini
[mysqld]
# 启用 Performance Schema（对性能有 3~5% 开销，但能看到锁细节）
performance_schema=ON

# 只打开需要的监听项，减少开销
performance-schema-instrument='memory/%=ON'
performance-schema-instrument='metadata_locks=ON'
performance-schema-instrument='table_io_waits=ON'
performance-schema-instrument='events_waits=ON'

# 事件历史大小（增大才能汇总更多数据）
performance-schema-max-table-instances=12500
performance-schema-events-waits-history-long-size=10000
```

**重启 MySQL 使配置生效**

#### 2.3 关键查询：监测锁等待

**实时观察谁在等锁**
```sql
-- 打开新的 MySQL 连接，执行这个查询，能实时看到堵塞情况
SELECT
    w.REQUESTING_TRX_ID,
    w.BLOCKING_TRX_ID,
    r.OBJECT_SCHEMA,
    r.OBJECT_NAME,
    r.LOCK_TYPE,
    r.LOCK_MODE
FROM information_schema.INNODB_LOCK_WAITS w
JOIN information_schema.INNODB_LOCKS r
  ON w.BLOCKING_LOCK_ID = r.LOCK_ID;

-- 输出示例：
-- REQUESTING_TRX_ID | BLOCKING_TRX_ID | OBJECT_SCHEMA | OBJECT_NAME | LOCK_TYPE | LOCK_MODE
-- 12345             | 12344           | test          | test_table  | RECORD    | X
-- 12346             | 12344           | test          | test_table  | RECORD    | X
-- （说明：事务 12344 持有排他锁，事务 12345、12346 都在等）
```

**汇总锁等待统计（测试后分析）**
```sql
-- 查看各个表的 I/O 等待时间（含锁等待）
SELECT
    OBJECT_SCHEMA,
    OBJECT_NAME,
    COUNT_STAR as total_ops,
    SUM_TIMER_WAIT / 1e12 as total_wait_sec,
    AVG_TIMER_WAIT / 1e9 as avg_wait_ms
FROM performance_schema.table_io_waits_summary_by_table
WHERE OBJECT_SCHEMA != 'mysql'
ORDER BY SUM_TIMER_WAIT DESC;

-- 输出示例：
-- OBJECT_SCHEMA | OBJECT_NAME | total_ops | total_wait_sec | avg_wait_ms
-- test          | test_table  | 1000000   | 85.5           | 0.0855
-- （说明：100 万次操作，总等待 85.5 秒，平均每次 0.0855ms）
```

#### 2.4 延迟注入：模拟 ESSD（关键步骤）

这是最重要的一步，能把"延迟 20 倍"的痛点放大。

**方法 1：Linux tc（最简单，但只能注入网络延迟）**

如果用远程存储或网络文件系统：
```bash
# 注入 200us 延迟（目标 ESSD 延迟）
sudo tc qdisc add dev eth0 root netem delay 200us

# 验证
ping 127.0.0.1  # 往返延迟应该会变成 ~400us

# 恢复
sudo tc qdisc del dev eth0 root
```

**说明：关于“延迟注入”**

当前仓库的测试框架不提供（也不依赖）代码/SQL 级别的延迟注入能力，因此不建议在本文档中按“注入 200us/10us”来复现。

如需模拟 I/O 延迟，请优先使用系统级手段（例如本节的 `tc netem`），并以“同一数据集 + 同一 workload + 同一参数”进行对比。

**方法 3：用容器/虚拟化隔离（最复杂但最真实）**

用 fio 创建虚拟高延迟块设备，让 MySQL 数据文件存储其上：

```bash
# 创建虚拟块设备（会有 ~200us 延迟）
fio --name=create_slowdev \
    --filename=/dev/mapper/slow-mysql-data \
    --size=100G \
    --ioengine=libaio \
    --rw=write \
    --bs=4k \
    --latency_target=200000 \    # 目标延迟 200us
    --latency_window=1000000

# MySQL 的 datadir 改指向这个虚拟设备
# 在 my.cnf：datadir=/mnt/slow-mysql-data/
```

**推荐：** 用**方法 2（代码注入）**，因为最快、最可控、能精确控制延迟。

---

### 第三层：对比与分析（How to measure improvement）

#### 3.1 测试流程（改代码前后对比）

**步骤 1：建立基准线（原始代码）**

```bash
# 编译原始 MySQL（保留当前状态作为基准）
cd /usr/local/mysql-8.0.34
mkdir -p build_baseline
cd build_baseline
cmake .. -DBUILD_CONFIG=mysql_release -DCMAKE_BUILD_TYPE=Release
make -j$(nproc)

# 初始化数据目录
rm -rf data_baseline
./runtime_output_directory/mysqld \
  --initialize \
  --basedir=. \
  --datadir=./data_baseline \
  --user=mysql \
  --explicit_defaults_for_timestamp

# 启动 MySQL（基准版本）
./runtime_output_directory/mysqld \
  --basedir=. \
  --datadir=./data_baseline \
  --port=3306 \
  --performance_schema=ON &

sleep 5

# 运行测试脚本，收集数据
python3 test_framework.py \
  --variant=baseline \
  --threads=100 \
  --operations=100000 \
  --output=baseline_results.csv

# 停止 MySQL
mysqladmin -u root shutdown
```

**步骤 2：改代码，编译新版本**

```bash
# 修改 B+树锁逻辑（例如改变分裂时的 X-Latch 策略）
vim storage/innobase/btr/btr0btr.cc
# 或改变其他锁相关的代码...

# 重新编译（保留改动版本）
mkdir -p build_optimized
cd build_optimized
cmake .. -DBUILD_CONFIG=mysql_release -DCMAKE_BUILD_TYPE=Release
make -j$(nproc)

# 初始化并启动
rm -rf data_optimized
./runtime_output_directory/mysqld --initialize ...
./runtime_output_directory/mysqld --datadir=./data_optimized ... &

# 运行相同的测试
python3 test_framework.py \
  --variant=optimized \
  --threads=100 \
  --operations=100000 \
  --output=optimized_results.csv
```

**步骤 3：对比与可视化**

```bash
# 使用提供的对比脚本
python3 compare.py \
  --baseline=baseline_results.csv \
  --optimized=optimized_results.csv \
  --output=comparison_report.html

# 生成图表：
# - 吞吐量对比（TPS）
# - 响应时间分布（P50/P95/P99）
# - 锁等待时间对比
# - 分裂频率与耗时
```

#### 3.2 数据分析维度

**维度 1：吞吐量（最直观的指标）**

```
基准版本（原始 X-Latch 分裂）：
  - 平均 TPS: 1000
  - 稳定性: ±5%

优化版本（改进的锁策略）：
  - 平均 TPS: 1500
  - 稳定性: ±3%

改进：+50% 吞吐量，且更稳定

为什么？因为锁持有时间从 2ms 降到 1.5ms，
其他线程的等待时间相应减少，更多线程能并行执行。
```

**维度 2：延迟分布（P99 很重要，代表最坏情况）**

```
基准版本：
  - P50: 50ms
  - P95: 150ms
  - P99: 500ms  ← 有些请求慢到 500ms，说明经常被堵

优化版本：
  - P50: 35ms
  - P95: 100ms
  - P99: 250ms  ← 大幅改善，只有极少数请求超过 250ms
```

**维度 3：锁等待时间（定位真正的瓶颈）**

```sql
-- 从 performance_schema 提取
SELECT AVG(TIMER_WAIT)/1e9 as avg_wait_ms
FROM performance_schema.table_io_waits_summary_by_table
WHERE OBJECT_NAME='test_table';

基准版本：15ms 平均锁等待
优化版本：8ms 平均锁等待
改进：-47% 锁等待时间
```

**维度 4：SMO（分裂/合并）频率与耗时**

```
基准版本：
  - 分裂次数: 5000
  - 平均单次耗时: 2.0ms
  - 总耗时: 10000ms (分裂占总时间 10%)

优化版本：
  - 分裂次数: 5000 (同样频率，无法避免)
  - 平均单次耗时: 1.2ms
  - 总耗时: 6000ms (分裂占总时间 6%)

改进：单次分裂快了 40%，总耗时少了 40%
```

---

### 第四层：对比分析报告（How to present findings）

#### 报告模板

生成的对比报告应包含以下内容：

**1. 执行摘要**
```
测试日期：2025-12-29
测试环境：MySQL 8.0.34，100 个并发线程，总操作 100000
模拟延迟：200us（ESSD）
改动内容：将 btr_page_split_and_insert() 中的 X-Latch 替换为 SX-Latch

关键数据：
┌─────────────────┬──────────┬──────────┬─────────┐
│ 指标            │ 基准     │ 优化     │ 改进    │
├─────────────────┼──────────┼──────────┼─────────┤
│ 吞吐量 (TPS)    │ 1000     │ 1500     │ +50%    │
│ P95 延迟 (ms)   │ 150      │ 100      │ -33%    │
│ 锁等待 (ms)     │ 15       │ 8        │ -47%    │
│ 分裂耗时 (ms)   │ 2000     │ 1200     │ -40%    │
└─────────────────┴──────────┴──────────┴─────────┘

结论：优化有效。在 ESSD 高延迟场景下，改进的锁策略使吞吐量提升 50%。
```

**2. 详细对比图表**
- 吞吐量随时间的变化曲线（基准 vs 优化）
- 响应时间的 CDF（累积分布函数）
- 锁等待时间的热力分布
- 分裂频率与耗时的时序图

**3. 原因分析**
```
为什么优化有效？

原理：
- 基准版本：分裂时持有 X-Latch（独占），其他所有线程都被锁住 2ms
  100 个线程，每 2ms 一次分裂 → 平均每个线程被堵 20% 的时间

- 优化版本：分裂时持有 SX-Latch（共享排他），允许 S 读并发
  其他 99 个线程中，只有 50 个在做写操作被锁，50 个在读，可以继续
  相当于只有 50% 的线程被堵，吞吐量自然提升
```

**4. 风险与限制**
```
- 分裂频率无法避免（页面大小固定），只能优化单次耗时
- ESSD 延迟是硬件限制，代码优化有上限
- SX-Latch 在某些场景下可能引入新的竞争（需要更多验证）
```

---

## 关键概念再强调

### 延迟倍数 = 并发影响倍数

这是 ESSD 场景最重要的认知：

```
NVMe 环境（10us 延迟）：
  分裂耗时：100us
  被堵线程：99 个
  单线程损失：100us
  全局损失：100us × 99 / 100 = 99% → 影响不大

ESSD 环境（200us 延迟，等于 20 倍）：
  分裂耗时：2000us（基础耗时 × 20）
  被堵线程：99 个
  单线程损失：2000us
  全局损失：2000us × 99 / 100 = 1980us
           = 总时间的 20%

  → 延迟 20 倍，吞吐量下降 ~20%

优化目标：将分裂从 2000us 降到 1200us
  新的全局损失：1200us × 99 / 100 = 1188us = 总时间的 12%
  改进：20% → 12%，等于吞吐量恢复 8%，总吞吐量提升 50%
```

**结论：在 ESSD 场景，缩短锁持有时间的收益被 20 倍放大。这是优化的机会所在。**

---

## 使用的工具与文件

测试框架包括四个核心文件：

1. **test_framework.py** — 主测试驱动程序
   - 构造工作负载（并发插入、范围读等）
  - 控制测试参数（线程数、操作数等）
   - 实时收集性能指标

2. **delay_injector.sql** — 测试对象初始化脚本
  - 创建测试库/表/索引/存储过程等
  - （不提供延迟注入，仅保留为历史占位说明）

3. **metrics_collector.py** — 自动汇总性能数据
   - 从 performance_schema 查询锁等待、I/O 统计
   - 聚合分析 TPS、延迟分布、P95/P99
   - 生成 CSV 报告

4. **compare.py** — 生成对比图表和报告
   - 对比两组测试结果
  - 生成 HTML 报告（图表内嵌，不再单独输出 PNG）
   - 计算改进百分比和统计显著性

---

## 快速开始

```bash
# 1. 编译 MySQL（可选修改锁代码）
cd /usr/local/mysql-8.0.34/build_baseline
make -j$(nproc)

# 2. 启动 MySQL
./runtime_output_directory/mysqld \
  --basedir=. \
  --datadir=./data \
  --port=3306 \
  --performance_schema=ON &

# 3. 初始化（一次性）
mysql -u root < delay_injector.sql

# 4. 运行基准测试
python3 test_framework.py \
  --variant=baseline \
  --threads=100 \
  --duration=60 \
  --operations=10000

# 5. 收集指标
python3 metrics_collector.py \
  --host=127.0.0.1 \
  --port=3306 \
  --output=baseline_metrics.csv

# 6. （修改代码后）运行对比版本
# ... 同 4-5 ...

# 7. 生成对比报告
python3 compare.py \
  --baseline=baseline_metrics.csv \
  --optimized=optimized_metrics.csv
```

---

## 总结

本测试框架的价值：

1. **量化性能改进** — 让"锁优化"可以用数字说话
2. **稳定复现热点** — 用固定 workload/参数复现分裂与等待热点，更容易看出改动效果
3. **可重复性** — 改代码后快速验证，对比结果一目了然
4. **根因分析** — 通过三个维度（吞吐、延迟、锁等待）定位真正的瓶颈

**下一步：** 使用提供的四个脚本文件执行完整的测试流程。



# 关键信息
✅ 完成！MySQL 8.0.34 Release 版已就绪

关键信息：

版本：8.0.34 (Release，非 debug)
端口：3306
数据目录：/rds/mysql/data/
Socket：/rds/mysql/tmp/mysql.sock
root 密码：0333
状态：✅ 正在运行，可连通

```sql
MYSQL_PWD='0333' /usr/local/mysql/bin/mysql --protocol=socket --socket=/rds/mysql/tmp/mysql.sock -uroot
```

# 运行一次测试生成可视化
cd /usr/local/mysql-8.0.34/tests && mkdir -p out/runs out/reports

 python3 test_framework.py --variant=baseline --workload=insert --threads=10 --operations=1000 --host=127.0.0.1 --port=3306 --user=root --password=0333 --output_dir=out/runs


python3 test_framework.py --variant=optimized --workload=insert --threads=10 --operations=1000 --host=127.0.0.1 --port=3306 --user=root --password=0333 --output_dir=out/runs

python3 compare.py --baseline "$(ls -dt out/runs/* | head -n 2 | tail -n 1)/result.csv" --optimized "$(ls -dt out/runs/* | head -n 1)/result.csv" --output out/reports && echo '[INFO] Latest report:' && ls -dt out/reports/*.html | head -n 1 && ls -dt out/reports/*_charts.png | head -n 1