# InnoDB B+Tree 性能测试框架 - 开发者详细文档

> 本文档面向开发者和深度使用者，包含完整的技术细节、实现原理和高级用法。  
> 快速上手请参考：`docs-DDOOCC/测试说明.md`

---

## 目录

1. [框架概览](#框架概览)
2. [文件清单](#文件清单)
3. [完整测试流程](#完整测试流程)
4. [test_framework.py 详解](#test_frameworkpy-详解)
5. [compare.py 详解](#comparepy-详解)
6. [metrics_collector.py 详解](#metrics_collectorpy-详解)
7. [产物管理最佳实践](#产物管理最佳实践)
8. [Workload 设计原理](#workload-设计原理)
9. [性能指标详解](#性能指标详解)
10. [高级用法与场景](#高级用法与场景)
11. [故障排查](#故障排查)
12. [扩展开发指南](#扩展开发指南)

---

## 框架概览

### 设计目标

本测试框架专为 InnoDB B+Tree 性能测试设计，重点关注：

1. **B+Tree 结构修改操作（SMO）**：
   - Split（页分裂）：INSERT 操作触发，页面写满时发生
   - Merge（页合并）：DELETE 操作触发，页面利用率低时发生

2. **锁和闩（Latch）竞争**：
   - index_tree_rw_lock：B+Tree 结构修改的关键锁
   - 行锁（row lock）：DML 操作的记录锁
   - 等待事件热点：通过 performance_schema 采集

3. **性能指标**：
   - 吞吐量（TPS/QPS）
   - 延迟分布（P50/P95/P99）
   - 资源利用率

### 架构设计

```
┌─────────────────────────────────────────┐
│  test_framework.py（主测试驱动）        │
│  - 多线程并发工作负载                  │
│  - 6 种 workload 类型                   │
│  - 实时采集 InnoDB metrics             │
│  - 输出 result.csv + result.json       │
└─────────────────────────────────────────┘
              ↓
┌─────────────────────────────────────────┐
│  MySQL 8.0.34                            │
│  - InnoDB 存储引擎                       │
│  - performance_schema 启用              │
│  - innodb_metrics 启用                  │
└─────────────────────────────────────────┘
              ↓
┌─────────────────────────────────────────┐
│  compare.py（对比报告生成）             │
│  - 读取两个 result.csv                  │
│  - 计算改进百分比                       │
│  - 生成 HTML 可视化报告                 │
└─────────────────────────────────────────┘
```

---

## 文件清单

| 文件 | 位置 | 功能 | 依赖 |
|------|------|------|------|
| `delay_injector.sql` | `tests/` | 初始化测试库表视图 | MySQL 客户端 |
| `test_framework.py` | `tests/` | 主测试驱动程序 | mysql-connector-python |
| `compare.py` | `tests/` | 对比报告生成器 | matplotlib (可选) |
| `metrics_collector.py` | `tests/` | 额外指标收集器 | mysql-connector-python |
| `run_compare_binaries.sh` | `tests/` | 一键对比脚本 | bash |

---

## 完整测试流程

### 步骤 1：环境准备

#### 1.1 安装 Python 依赖

```bash
# 必需依赖
pip3 install mysql-connector-python

# 可选依赖（用于生成图表）
pip3 install matplotlib numpy
```

#### 1.2 启动 MySQL（Debug 版本示例）

```bash
cd /usr/local/mysql-8.0.34/build_debug

# 初始化数据目录
rm -rf data_insert
./runtime_output_directory/mysqld \
  --initialize-insecure \
  --basedir=$PWD \
  --datadir=$PWD/data_insert \
  --user=$USER

# 启动 mysqld（启用 performance_schema）
./runtime_output_directory/mysqld \
  --basedir=$PWD \
  --datadir=$PWD/data_insert \
  --port=3306 \
  --socket=mysql_insert.sock \
  --performance_schema=ON \
  --log-error=mysql_insert.err \
  --pid-file=mysql_insert.pid &

# 等待启动
sleep 3

# 验证连接
./runtime_output_directory/mysql -S mysql_insert.sock -uroot -e "SELECT VERSION();"
```

#### 1.3 初始化测试表

```bash
cd /usr/local/mysql-8.0.34
./build_debug/runtime_output_directory/mysql -S ./build_debug/mysql_insert.sock -uroot < tests/delay_injector.sql
```

---

### 步骤 2：运行基准测试

```bash
cd /usr/local/mysql-8.0.34/tests

python3 test_framework.py \
  --variant=baseline \
  --workload=insert \
  --threads=100 \
  --operations=10000 \
  --host=127.0.0.1 \
  --port=3306 \
  --user=root \
  --password=密码 \
  --output_dir=out/runs
```

**输出示例：**
```
[INFO] 测试配置:
[INFO]   variant: baseline
[INFO]   workload: insert
[INFO]   threads: 100
[INFO]   operations: 10000
[INFO] 连接到 MySQL: 127.0.0.1:3306
[INFO] 重置表 test_table
[INFO] 采集初始 DB metrics
[INFO] 启动 100 个线程...
[THREAD 0] 开始 insert workload
[THREAD 1] 开始 insert workload
...
[INFO] 所有线程已启动
[INFO] 等待所有线程完成...
[INFO] 所有线程已完成
[INFO] 采集结束 DB metrics
[INFO] 
[INFO] ========== 测试结果 ==========
[INFO] 总耗时: 123.45 秒
[INFO] 总操作数: 1000000
[INFO] 成功操作数: 999950
[INFO] 失败操作数: 50
[INFO] TPS: 8100.12
[INFO] 平均延迟: 12.34 ms
[INFO] P50 延迟: 8.50 ms
[INFO] P95 延迟: 35.20 ms
[INFO] P99 延迟: 58.90 ms
[INFO] 
[INFO] ===== B+Tree 结构修改统计 =====
[INFO] index_page_splits: 5234 次
[INFO] index_page_merge_attempts: 0 次
[INFO] index_page_merge_successful: 0 次
[INFO] 
[INFO] ===== 等待事件热点 Top 15 =====
[INFO] 1. wait/synch/mutex/innodb/index_tree_rw_lock
[INFO]    count: 15234, sum: 12.34s
[INFO] ...
[INFO] 
[INFO] 结果已保存到: out/runs/20260105_143025_baseline_insert_t100_ops10000/
```

---

### 步骤 3：修改代码并重新编译（可选）

```bash
# 假设你要优化 B+Tree split 逻辑
vim /usr/local/mysql-8.0.34/storage/innobase/btr/btr0btr.cc

# 重新编译
cd /usr/local/mysql-8.0.34/build_debug
make -j$(nproc)

# 重启 MySQL
pkill -f "mysqld.*data_insert"
sleep 2
./runtime_output_directory/mysqld \
  --basedir=$PWD \
  --datadir=$PWD/data_insert \
  --port=3306 \
  --socket=mysql_insert.sock \
  --performance_schema=ON &
sleep 3
```

---

### 步骤 4：运行优化版本测试

```bash
cd /usr/local/mysql-8.0.34/tests

python3 test_framework.py \
  --variant=optimized \
  --workload=insert \
  --threads=100 \
  --operations=10000 \
  --host=127.0.0.1 \
  --port=3306 \
  --user=root \
  --password=密码 \
  --output_dir=out/runs
```

---

### 步骤 5：生成对比报告

```bash
python3 compare.py \
  --baseline "$(ls -dt out/runs/*baseline* | head -n 1)/result.csv" \
  --optimized "$(ls -dt out/runs/*optimized* | head -n 1)/result.csv" \
  --output out/reports
```

**报告内容：**
- 关键指标对比表（TPS、延迟分位数、B+Tree splits/merges）
- 改进百分比与方向指示（↑/↓）
- 等待事件热点对比
- 内嵌的性能对比图表（条形图、雷达图）

---

## test_framework.py 详解

### 核心参数说明

```bash
python3 test_framework.py [OPTIONS]
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--variant` | {baseline\|optimized} | **必需** | 测试版本标识 |
| `--workload` | {insert\|range_insert\|mixed\|delete_heavy\|churn\|mixed_with_delete} | insert | 工作负载类型 |
| `--threads` | int | 100 | 并发线程数 |
| `--operations` | int | 10000 | 每个线程的操作数 |
| `--host` | str | 127.0.0.1 | MySQL 主机 |
| `--port` | int | 3306 | MySQL 端口 |
| `--user` | str | root | MySQL 用户名 |
| `--password` | str | '' | MySQL 密码 |
| `--database` | str | test_innodb | 测试数据库名 |
| `--output_dir` | str | . | 输出目录（会自动创建子目录） |

### Workload 类型详解

#### 1. insert（纯插入）

**特性：**
- 所有线程竞争同一个键值范围（从 1 开始）
- 使用 `thread_id * 1000000 + local_counter` 作为主键，避免冲突
- **高度触发页分裂**：所有线程都在写入相邻的页

**触发的 B+Tree 操作：**
- ✅ Split（页分裂）：大量触发
- ❌ Merge（页合并）：不触发

**适用场景：**
- 测试 index_tree_rw_lock 竞争
- 测试页分裂性能优化
- 压力测试 B+Tree 写入路径

**实现代码片段：**
```python
def insert_workload(self, thread_id, operations):
    for i in range(operations):
        key = thread_id * 1000000 + i  # 避免主键冲突
        value = f"value_{random.randint(1, 1000000)}"
        sql = f"INSERT INTO test_table (id, data) VALUES ({key}, '{value}')"
        self.execute_with_timing(sql)
```

**推荐参数：**
```bash
--workload=insert --threads=100 --operations=10000
```

---

#### 2. range_insert（范围插入）

**特性：**
- 每个线程插入独立的键值范围
- 线程 i 插入范围：`[i * 10000, (i+1) * 10000)`
- **分散页分裂压力**：不同线程写入不同的页

**触发的 B+Tree 操作：**
- ✅ Split：适量触发，分散在不同页
- ❌ Merge：不触发

**适用场景：**
- 更接近真实业务（不同用户/分区的插入）
- 降低锁竞争，测试整体吞吐
- 对比集中式 vs 分散式插入的性能差异

**实现代码片段：**
```python
def range_insert_workload(self, thread_id, operations):
    base = thread_id * 10000
    for i in range(operations):
        key = base + i
        value = f"value_{random.randint(1, 1000000)}"
        sql = f"INSERT INTO test_table (id, data) VALUES ({key}, '{value}')"
        self.execute_with_timing(sql)
```

**推荐参数：**
```bash
--workload=range_insert --threads=100 --operations=10000
```

---

#### 3. mixed（混合读写）

**特性：**
- 50% INSERT + 30% SELECT + 20% UPDATE
- 不包含 DELETE 操作
- 观察行锁与闩的交互

**触发的 B+Tree 操作：**
- ✅ Split：由 INSERT 触发
- ❌ Merge：不触发（没有 DELETE）

**适用场景：**
- OLTP 混合负载模拟
- 观察读写并发时的锁竞争
- UPDATE 触发的行锁等待

**实现代码片段：**
```python
def mixed_workload(self, thread_id, operations):
    inserted_keys = []
    for i in range(operations):
        rand = random.random()
        if rand < 0.5:  # 50% INSERT
            key = thread_id * 1000000 + i
            sql = f"INSERT INTO test_table (id, data) VALUES ({key}, 'value')"
            inserted_keys.append(key)
        elif rand < 0.8:  # 30% SELECT
            if inserted_keys:
                key = random.choice(inserted_keys)
                sql = f"SELECT * FROM test_table WHERE id = {key}"
        else:  # 20% UPDATE
            if inserted_keys:
                key = random.choice(inserted_keys)
                sql = f"UPDATE test_table SET data = 'updated' WHERE id = {key}"
        self.execute_with_timing(sql)
```

**推荐参数：**
```bash
--workload=mixed --threads=50 --operations=5000
```

---

#### 4. delete_heavy（大量删除）

**特性：**
- 两阶段执行：
  1. 插入阶段：插入 1.5x 数据量（operations * 1.5）
  2. 删除阶段：随机删除 60% 的数据
- **最能触发页合并**

**触发的 B+Tree 操作：**
- ✅ Split：插入阶段触发
- ✅ **Merge：删除阶段大量触发**（推荐用于 Merge 优化测试）

**适用场景：**
- 测试 B+Tree Merge 性能优化
- 测试页回收和空间重用
- 模拟数据清理场景

**实现代码片段：**
```python
def delete_heavy_workload(self, thread_id, operations):
    # Phase 1: 插入 1.5x 数据
    insert_count = int(operations * 1.5)
    inserted_keys = []
    for i in range(insert_count):
        key = thread_id * 1000000 + i
        sql = f"INSERT INTO test_table (id, data) VALUES ({key}, 'value')"
        self.execute_with_timing(sql)
        inserted_keys.append(key)
    
    # Phase 2: 随机删除 60%
    delete_count = int(len(inserted_keys) * 0.6)
    keys_to_delete = random.sample(inserted_keys, delete_count)
    for key in keys_to_delete:
        sql = f"DELETE FROM test_table WHERE id = {key}"
        self.execute_with_timing(sql)
```

**推荐参数：**
```bash
--workload=delete_heavy --threads=5 --operations=2000
# 注意：operations 表示"目标数据量"，实际会插入 1.5x 再删除 60%
```

**验证 Merge 触发：**
```bash
# 查看输出中的指标
index_page_merge_attempts: 11    # 尝试合并次数
index_page_merge_successful: 4   # 成功合并次数
```

---

#### 5. churn（数据流失）

**特性：**
- 滑动窗口模型（window_size = 500）
- 持续插入新记录，窗口满时删除最旧的记录
- 模拟"数据流失"场景（如时序数据保留策略）

**触发的 B+Tree 操作：**
- ✅ Split：插入触发
- ✅ Merge：删除触发（需要较大 operations）

**适用场景：**
- 时序数据库场景（FIFO）
- 缓存淘汰策略测试
- 稳态负载下的性能测试

**实现代码片段：**
```python
def churn_workload(self, thread_id, operations):
    window_size = 500
    inserted_keys = []
    
    for i in range(operations):
        # 插入新记录
        key = thread_id * 1000000 + i
        sql = f"INSERT INTO test_table (id, data) VALUES ({key}, 'value')"
        self.execute_with_timing(sql)
        inserted_keys.append(key)
        
        # 如果窗口满了，删除最旧的记录
        if len(inserted_keys) > window_size:
            old_key = inserted_keys.pop(0)
            sql = f"DELETE FROM test_table WHERE id = {old_key}"
            self.execute_with_timing(sql)
```

**推荐参数：**
```bash
--workload=churn --threads=10 --operations=5000
# 需要较大的 operations 才能触发有效的 Merge
```

---

#### 6. mixed_with_delete（含删除的混合负载）

**特性：**
- 40% INSERT + 30% SELECT + 20% UPDATE + 10% DELETE
- 更接近真实 OLTP 业务
- 少量但持续的 DELETE 操作

**触发的 B+Tree 操作：**
- ✅ Split：INSERT 触发
- ✅ Merge：DELETE 触发（少量）

**适用场景：**
- 真实业务场景模拟
- 综合性能测试
- 观察 Split/Merge 混合场景下的性能

**实现代码片段：**
```python
def mixed_with_delete_workload(self, thread_id, operations):
    inserted_keys = []
    for i in range(operations):
        rand = random.random()
        if rand < 0.4:  # 40% INSERT
            key = thread_id * 1000000 + len(inserted_keys)
            sql = f"INSERT INTO test_table (id, data) VALUES ({key}, 'value')"
            inserted_keys.append(key)
        elif rand < 0.7:  # 30% SELECT
            if inserted_keys:
                key = random.choice(inserted_keys)
                sql = f"SELECT * FROM test_table WHERE id = {key}"
        elif rand < 0.9:  # 20% UPDATE
            if inserted_keys:
                key = random.choice(inserted_keys)
                sql = f"UPDATE test_table SET data = 'updated' WHERE id = {key}"
        else:  # 10% DELETE
            if inserted_keys:
                key = random.choice(inserted_keys)
                sql = f"DELETE FROM test_table WHERE id = {key}"
                inserted_keys.remove(key)
        self.execute_with_timing(sql)
```

**推荐参数：**
```bash
--workload=mixed_with_delete --threads=10 --operations=5000
```

---

### 性能采集机制

#### 延迟采集

每个操作都会记录执行时间：
```python
start = time.time()
cursor.execute(sql)
cursor.fetchall()  # 确保结果完全返回
end = time.time()
latency_ms = (end - start) * 1000
self.latencies.append(latency_ms)
```

#### 分位数计算

使用 `numpy.percentile()` 或自实现排序：
```python
import numpy as np
p50 = np.percentile(latencies, 50)
p95 = np.percentile(latencies, 95)
p99 = np.percentile(latencies, 99)
```

#### B+Tree 指标采集

测试前后各采集一次 InnoDB metrics，计算增量：
```python
# 测试前
cursor.execute("SELECT NAME, COUNT FROM information_schema.innodb_metrics WHERE NAME LIKE 'index_page%'")
metrics_start = {row[0]: row[1] for row in cursor.fetchall()}

# ... 运行测试 ...

# 测试后
cursor.execute("SELECT NAME, COUNT FROM information_schema.innodb_metrics WHERE NAME LIKE 'index_page%'")
metrics_end = {row[0]: row[1] for row in cursor.fetchall()}

# 计算增量
splits_delta = metrics_end['index_page_splits'] - metrics_start['index_page_splits']
merge_attempts_delta = metrics_end['index_page_merge_attempts'] - metrics_start['index_page_merge_attempts']
merge_successful_delta = metrics_end['index_page_merge_successful'] - metrics_start['index_page_merge_successful']
```

#### 等待事件热点采集

采集 Top 15 等待事件：
```python
sql = """
SELECT 
    event_name,
    count_star,
    sum_timer_wait / 1000000000000 AS sum_seconds,
    max_timer_wait / 1000000000 AS max_ms
FROM performance_schema.events_waits_summary_global_by_event_name
WHERE event_name LIKE 'wait/synch/%'
ORDER BY sum_timer_wait DESC
LIMIT 15
"""
```

计算测试区间的增量：
```python
waits_delta = []
for event in waits_end:
    event_name = event['event_name']
    count_delta = event['count'] - waits_start[event_name]['count']
    sum_delta = event['sum_s'] - waits_start[event_name]['sum_s']
    waits_delta.append({
        'event_name': event_name,
        'count': count_delta,
        'sum_s': sum_delta
    })
```

---

### 输出文件格式

#### result.csv（摘要指标）

```csv
variant,workload,threads,operations,total_time_s,total_ops,successful_ops,failed_ops,tps,avg_latency_ms,p50_ms,p95_ms,p99_ms,index_page_splits,index_page_merge_attempts,index_page_merge_successful,waits_top_sum_s,waits_top_count
baseline,insert,100,10000,123.45,1000000,999950,50,8100.12,12.34,8.50,35.20,58.90,5234,0,0,12.34,15234
```

#### result.json（详细信息）

```json
{
  "config": {
    "variant": "baseline",
    "workload": "insert",
    "threads": 100,
    "operations": 10000,
    "host": "127.0.0.1",
    "port": 3306,
    "database": "test_innodb"
  },
  "stats": {
    "total_time_s": 123.45,
    "total_ops": 1000000,
    "successful_ops": 999950,
    "failed_ops": 50,
    "tps": 8100.12,
    "avg_latency_ms": 12.34,
    "p50_ms": 8.50,
    "p95_ms": 35.20,
    "p99_ms": 58.90,
    "db_metrics": {
      "index_page_splits": 5234,
      "index_page_merge_attempts": 0,
      "index_page_merge_successful": 0,
      "waits_top": [
        {
          "event_name": "wait/synch/mutex/innodb/index_tree_rw_lock",
          "count": 15234,
          "sum_s": 12.34,
          "max_ms_end": 150.5
        },
        ...
      ]
    }
  }
}
```

---

## compare.py 详解

### 核心功能

1. 读取两个 `result.csv` 文件
2. 计算关键指标的改进百分比
3. 生成 HTML 可视化报告（内嵌图表）

### 参数说明

```bash
python3 compare.py --baseline <path> --optimized <path> --output <dir>
```

| 参数 | 说明 |
|------|------|
| `--baseline` | 基准版本的 result.csv 路径 |
| `--optimized` | 优化版本的 result.csv 路径 |
| `--output` | 输出目录（会生成带时间戳的 HTML 文件） |

### 改进百分比计算

```python
def calculate_improvement(baseline_value, optimized_value, higher_is_better=True):
    if baseline_value == 0:
        return 0.0
    
    improvement = (optimized_value - baseline_value) / baseline_value * 100
    
    if not higher_is_better:
        improvement = -improvement
    
    return improvement
```

**示例：**
- TPS（越高越好）：baseline=1000, optimized=1200 → 改进 +20%
- P99 延迟（越低越好）：baseline=100ms, optimized=80ms → 改进 +20%

### HTML 报告结构

```html
<!DOCTYPE html>
<html>
<head>
    <title>性能对比报告</title>
    <style>/* 美化样式 */</style>
</head>
<body>
    <h1>性能对比报告</h1>
    
    <!-- 关键指标对比表 -->
    <table>
        <tr>
            <th>指标</th>
            <th>基准版本</th>
            <th>优化版本</th>
            <th>改进百分比</th>
        </tr>
        <tr>
            <td>TPS</td>
            <td>1000.00</td>
            <td>1200.00</td>
            <td class="improved">+20.00% ↑</td>
        </tr>
        ...
    </table>
    
    <!-- 内嵌图表（base64 编码的 PNG） -->
    <img src="data:image/png;base64,...">
    
    <!-- 等待事件热点对比 -->
    <h2>等待事件热点（Top 15）</h2>
    <table>...</table>
</body>
</html>
```

### 图表生成（可选，需要 matplotlib）

```python
import matplotlib.pyplot as plt
import base64
from io import BytesIO

# 生成条形图
fig, ax = plt.subplots(figsize=(10, 6))
metrics = ['TPS', 'P50', 'P95', 'P99']
baseline_values = [1000, 10, 35, 60]
optimized_values = [1200, 8, 28, 48]

x = range(len(metrics))
width = 0.35
ax.bar([i - width/2 for i in x], baseline_values, width, label='Baseline')
ax.bar([i + width/2 for i in x], optimized_values, width, label='Optimized')
ax.set_xticks(x)
ax.set_xticklabels(metrics)
ax.legend()

# 转换为 base64
buffer = BytesIO()
plt.savefig(buffer, format='png')
buffer.seek(0)
image_base64 = base64.b64encode(buffer.read()).decode()
html_img_tag = f'<img src="data:image/png;base64,{image_base64}">'
```

---

## metrics_collector.py 详解

### 功能

额外的性能指标收集工具，独立于 test_framework.py。

### 使用场景

**单次采集（测试后）：**
```bash
python3 metrics_collector.py \
  --host=127.0.0.1 \
  --port=3306 \
  --user=root \
  --password=密码 \
  --output=metrics.csv
```

**连续监控（测试进行时）：**
```bash
python3 metrics_collector.py \
  --interval=2 \
  --duration=120 \
  --output=metrics_timeline.csv
```

### 采集的指标

- 锁等待统计（mutex, rw_lock）
- I/O 统计（file_summary_by_instance）
- 语句执行时间（events_statements_summary）
- InnoDB 缓冲池状态
- 线程状态

---

## 产物管理最佳实践

### 推荐目录结构

```
tests/
├── out/
│   ├── runs/                          # 运行产物
│   │   ├── 20260105_143025_baseline_insert_t100_ops10000/
│   │   │   ├── result.csv
│   │   │   └── result.json
│   │   ├── 20260105_143156_optimized_insert_t100_ops10000/
│   │   │   ├── result.csv
│   │   │   └── result.json
│   │   └── ...
│   └── reports/                       # 对比报告
│       ├── insert_20260105_143230.html
│       ├── delete_heavy_20260105_144530.html
│       └── ...
├── test_framework.py
├── compare.py
└── README.md
```

### 文件命名规范

**运行目录：**
```
<timestamp>_<variant>_<workload>_t<threads>_ops<operations>
例如：20260105_143025_baseline_insert_t100_ops10000
```

**报告文件：**
```
<workload>_<timestamp>.html
例如：insert_20260105_143230.html
```

### 清理策略

```bash
# 只保留最近 10 次运行
cd /usr/local/mysql-8.0.34/tests/out/runs
ls -dt * | tail -n +11 | xargs rm -rf

# 只保留最近 5 份报告
cd /usr/local/mysql-8.0.34/tests/out/reports
ls -t *.html | tail -n +6 | xargs rm -f
```

---

## Workload 设计原理

### 为什么需要多种 Workload？

不同的优化场景需要不同的负载模式：

| 优化目标 | 推荐 Workload | 原因 |
|----------|--------------|------|
| B+Tree Split 优化 | insert | 集中式插入触发大量 Split |
| B+Tree Merge 优化 | delete_heavy | 删除触发页合并 |
| 锁竞争优化 | insert | 高并发写入同一区域 |
| 混合负载优化 | mixed_with_delete | 接近真实业务 |
| 分散压力测试 | range_insert | 降低单点竞争 |

### Split vs Merge 触发条件

**Split（页分裂）触发条件：**
1. INSERT 导致页面写满（通常 > 90% full）
2. 需要分配新页面
3. 获取 index_tree_rw_lock（X 模式）

**Merge（页合并）触发条件：**
1. DELETE 导致页面利用率低（通常 < 50%）
2. 相邻页面也利用率低
3. 尝试合并两个页面（可能失败）
4. 获取 index_tree_rw_lock（X 模式）

### 关键锁：index_tree_rw_lock

```
index_tree_rw_lock（InnoDB B+Tree 结构锁）
├─ S 模式（Shared）：读取 B+Tree 结构
├─ X 模式（Exclusive）：修改 B+Tree 结构（Split/Merge）
└─ SX 模式（Shared-Exclusive）：允许并发读，但独占写

竞争场景：
- 多个线程同时触发 Split → 竞争 X 锁
- 优化思路：减少 Split 次数，或缩短持锁时间
```

---

## 性能指标详解

### TPS（Transactions Per Second）

实际上是 **Operations Per Second**（OPS），每秒完成的操作数。

**计算公式：**
```
TPS = 总成功操作数 / 测试总耗时（秒）
```

**影响因素：**
- CPU 性能
- 锁竞争程度
- I/O 性能
- B+Tree 结构复杂度

**对比分析：**
- TPS 提升 20% → 吞吐量显著改进
- TPS 下降 → 可能引入了额外开销或锁竞争

---

### 延迟（Latency）

单次操作从开始到完成的耗时。

**关键分位数：**
- **P50（中位数）**：典型用户体验
- **P95**：95% 的用户体验
- **P99**：尾延迟，反映系统稳定性

**分析思路：**
```
P50 很低但 P99 很高 → 尾延迟问题（锁竞争/GC/I/O 抖动）
P50/P95/P99 都降低 → 整体性能改进
P99 改进但 P50 不变 → 解决了尾延迟问题
```

**示例：**
```
Baseline: P50=10ms, P95=35ms, P99=60ms
Optimized: P50=10ms, P95=30ms, P99=45ms
→ 解决了尾延迟问题（P95/P99 改进 14%~25%）
```

---

### B+Tree 结构修改指标

#### index_page_splits

页分裂次数。

**触发条件：**
- INSERT 导致叶子节点满
- 需要拆分为两个页面

**性能影响：**
- 获取 index_tree_rw_lock（X 模式）
- 阻塞其他并发 Split/Merge
- 增加 I/O 写入

**优化方向：**
- 减少 Split 次数（例如增大页面填充因子）
- 缩短 Split 持锁时间（例如提前分配页面）
- 改进锁策略（例如 X → SX）

---

#### index_page_merge_attempts / successful

页合并尝试/成功次数。

**触发条件：**
- DELETE 导致页面利用率低（< 50%）
- 相邻页面也利用率低

**成功率分析：**
```
merge_successful / merge_attempts → 合并成功率

成功率低 → 相邻页面利用率不够低，或有并发操作
成功率高 → 确实触发了有效的页合并
```

**示例：**
```
delete_heavy: attempts=11, successful=4 → 成功率 36%
mixed_with_delete: attempts=11, successful=3 → 成功率 27%
```

---

### 等待事件（Waits）

#### 关键等待事件

| 事件名 | 含义 | 影响 |
|--------|------|------|
| `wait/synch/mutex/innodb/index_tree_rw_lock` | B+Tree 结构锁等待 | Split/Merge 竞争 |
| `wait/synch/mutex/innodb/trx_mutex` | 事务锁等待 | 高并发事务冲突 |
| `wait/io/file/innodb/innodb_data_file` | 数据文件 I/O 等待 | 磁盘性能瓶颈 |
| `wait/synch/rwlock/innodb/btr_search_latch` | 自适应哈希索引等待 | AHI 竞争 |

#### 分析方法

**查看等待事件 Top 15：**
```json
"waits_top": [
  {
    "event_name": "wait/synch/mutex/innodb/index_tree_rw_lock",
    "count": 15234,        // 等待次数
    "sum_s": 12.34,        // 累计等待时间（秒）
    "max_ms_end": 150.5    // 历史最大等待时间（毫秒）
  },
  ...
]
```

**分析思路：**
1. **sum_s 高 → 该事件是性能瓶颈**
2. **count 高 → 频繁发生**
3. **max_ms 高 → 存在极端长等待**

**对比分析：**
```
Baseline index_tree_rw_lock: sum_s=12.34, count=15234
Optimized index_tree_rw_lock: sum_s=8.50, count=12000
→ 锁等待时间降低 31%，次数降低 21%
→ 优化有效！
```

---

## 高级用法与场景

### 场景 1：对比不同锁策略

**背景：**
你修改了 B+Tree split 的锁逻辑（从 X-Latch 改为 SX-Latch）。

**测试步骤：**
```bash
# 1. 测试原始版本（X-Latch）
python3 test_framework.py --variant=baseline --workload=insert --threads=100 --operations=10000 --output_dir=out/runs

# 2. 修改代码，重新编译
vim storage/innobase/btr/btr0btr.cc
make -j$(nproc)

# 3. 重启 MySQL，测试优化版本（SX-Latch）
python3 test_framework.py --variant=optimized --workload=insert --threads=100 --operations=10000 --output_dir=out/runs

# 4. 生成对比报告
python3 compare.py --baseline out/runs/*baseline*/result.csv --optimized out/runs/*optimized*/result.csv --output out/reports
```

**期望结果：**
- TPS 提升（并发度提高）
- index_tree_rw_lock 等待时间降低
- P95/P99 延迟降低

---

### 场景 2：测试不同并发度

**目标：**
找到最优的并发线程数。

**测试脚本：**
```bash
#!/bin/bash
for threads in 10 50 100 200 500; do
    python3 test_framework.py \
        --variant=baseline \
        --workload=insert \
        --threads=$threads \
        --operations=10000 \
        --output_dir=out/runs
done

# 对比不同并发度的 TPS
grep "^baseline,insert" out/runs/*/result.csv | awk -F, '{print $3, $9}' | sort -n
```

**分析：**
```
10  threads → TPS=5000
50  threads → TPS=18000
100 threads → TPS=22000
200 threads → TPS=21000  ← 性能下降（锁竞争加剧）
500 threads → TPS=15000  ← 显著下降
```

**结论：**
最优并发度约为 100 线程。

---

### 场景 3：对比不同存储设备

**背景：**
测试 NVMe SSD vs SATA SSD。

**测试步骤：**
```bash
# 1. 在 NVMe SSD 上运行
python3 test_framework.py --variant=nvme --workload=insert --threads=100 --operations=10000 --output=nvme.csv

# 2. 在 SATA SSD 上运行（修改 datadir 到 SATA 磁盘）
# 修改 MySQL 配置，重启
python3 test_framework.py --variant=sata --workload=insert --threads=100 --operations=10000 --output=sata.csv

# 3. 对比
python3 compare.py --baseline=sata.csv --optimized=nvme.csv
```

**期望结果：**
- NVMe TPS 显著高于 SATA
- I/O 等待事件降低

---

### 场景 4：验证 Merge 优化

**背景：**
你优化了 B+Tree merge 逻辑。

**测试步骤：**
```bash
# 1. 基准版本（delete_heavy workload）
python3 test_framework.py --variant=baseline --workload=delete_heavy --threads=10 --operations=5000 --output_dir=out/runs

# 2. 优化版本
python3 test_framework.py --variant=optimized --workload=delete_heavy --threads=10 --operations=5000 --output_dir=out/runs

# 3. 对比
python3 compare.py --baseline out/runs/*baseline*/result.csv --optimized out/runs/*optimized*/result.csv --output out/reports
```

**关注指标：**
- `index_page_merge_successful` 提升 → Merge 成功率提高
- TPS 提升 → Merge 性能改进
- index_tree_rw_lock 等待降低 → 锁竞争减少

---

## 故障排查

### 问题 1：测试运行报错 "Connection refused"

**原因：**
MySQL 未启动或端口配置错误。

**解决：**
```bash
# 检查 MySQL 是否运行
ps aux | grep mysqld

# 检查端口
netstat -tlnp | grep 3306

# 测试连接
mysql -h127.0.0.1 -P3306 -uroot -p密码 -e "SELECT 1"
```

---

### 问题 2：TPS 极低（< 100）

**可能原因：**
1. 磁盘性能差（机械硬盘）
2. MySQL 配置不当（未启用 performance_schema）
3. 锁竞争严重

**排查步骤：**
```bash
# 1. 检查 performance_schema
mysql -e "SHOW VARIABLES LIKE 'performance_schema'"

# 2. 检查磁盘 I/O
iostat -x 1

# 3. 查看等待事件
python3 metrics_collector.py --output=debug_metrics.csv
```

---

### 问题 3：主键冲突

**原因：**
老版本的 insert workload 可能有主键冲突。

**解决：**
确保使用最新版本的 test_framework.py，已修复为：
```python
key = thread_id * 1000000 + local_counter
```

---

### 问题 4：对比报告显示 "No improvement"

**原因：**
1. 代码改动未生效（未重新编译/重启）
2. 测试参数不同（线程数、操作数）
3. 环境差异（缓存状态、系统负载）

**解决：**
```bash
# 1. 确认编译
make -j$(nproc)
pkill mysqld
sleep 2
./runtime_output_directory/mysqld ... &

# 2. 确保参数相同
diff <(grep "config" out/runs/baseline*/result.json) \
     <(grep "config" out/runs/optimized*/result.json)

# 3. 多次运行取平均
for i in {1..5}; do
    python3 test_framework.py --variant=baseline ... --output=baseline_$i.csv
done
```

---

## 扩展开发指南

### 添加新的 Workload 类型

**步骤 1：实现 workload 方法**

在 `test_framework.py` 中添加：
```python
def custom_workload(self, thread_id, operations):
    """
    自定义工作负载：描述你的负载逻辑
    """
    for i in range(operations):
        # 你的 SQL 逻辑
        sql = "..."
        self.execute_with_timing(sql)
```

**步骤 2：注册到 run_workload()**

```python
def run_workload(self, thread_id):
    if self.config.workload == 'custom':
        self.custom_workload(thread_id, self.config.operations)
    # ...
```

**步骤 3：更新命令行参数**

```python
parser.add_argument(
    '--workload',
    choices=['insert', 'range_insert', 'mixed', 'delete_heavy', 'churn', 'mixed_with_delete', 'custom'],
    default='insert'
)
```

---

### 添加新的性能指标

**步骤 1：采集指标**

在 `collect_db_metrics()` 中添加：
```python
cursor.execute("SELECT ... FROM information_schema.innodb_metrics WHERE NAME='your_metric'")
row = cursor.fetchone()
metrics['your_metric'] = row[1]
```

**步骤 2：计算增量**

```python
your_metric_delta = metrics_end['your_metric'] - metrics_start['your_metric']
```

**步骤 3：输出到 CSV/JSON**

```python
csv_row.append(str(your_metric_delta))
json_data['stats']['db_metrics']['your_metric'] = your_metric_delta
```

---

### 自定义图表

修改 `compare.py` 中的 `generate_chart()` 方法：
```python
def generate_custom_chart(baseline_data, optimized_data):
    fig, ax = plt.subplots()
    # 你的图表逻辑
    # ...
    return fig
```

---

## 常见问题（FAQ）

### Q1: 如何验证代码改动生效？

**A:** 
1. 重新编译：`make -j$(nproc)`
2. 重启 MySQL：`pkill mysqld; sleep 2; ./runtime_output_directory/mysqld ... &`
3. 确认版本：`mysql -e "SELECT VERSION()"`
4. 查看日志：`tail -f mysql.err`（观察是否有新逻辑的日志输出）

---

### Q2: 如何减少测试时间？

**A:** 
```bash
# 快速测试（10 秒内完成）
python3 test_framework.py \
    --threads=10 \
    --operations=1000 \
    --workload=insert
```

---

### Q3: 如何在远程机器上运行？

**A:** 
```bash
# 指定远程 MySQL
python3 test_framework.py \
    --host=192.168.1.100 \
    --port=3306 \
    --user=remote_user \
    --password=remote_pass
```

---

### Q4: 如何批量测试多个场景？

**A:** 
```bash
#!/bin/bash
workloads=("insert" "delete_heavy" "mixed_with_delete")
for wl in "${workloads[@]}"; do
    python3 test_framework.py \
        --variant=baseline \
        --workload=$wl \
        --threads=10 \
        --operations=5000 \
        --output_dir=out/runs
done
```

---

## 参考文档

- `docs-DDOOCC/测试说明.md`（快速上手指南）
- `docs-DDOOCC/InnoDB_Performance_Testing_Guide.md`（理论文档）
- MySQL 8.0 官方文档：https://dev.mysql.com/doc/refman/8.0/en/
- InnoDB 存储引擎内幕：https://www.amazon.com/InnoDB-Storage-Engine-Jeremy-Cole/dp/1449332514

---

## 更新日志

- 2026-01-05：添加 delete_heavy/churn/mixed_with_delete workload，支持 Merge 场景测试
- 2026-01-05：移除 latency_us 延迟注入功能
- 2026-01-05：优化报告生成（内嵌图表、简化文件名）
- 2026-01-05：添加产物目录自动分类

---

**本文档持续更新中。如有问题或建议，请联系开发者。**
