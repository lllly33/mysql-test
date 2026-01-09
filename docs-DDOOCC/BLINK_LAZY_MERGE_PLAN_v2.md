# B+Tree 延迟合并 + 锁粒度优化计划 v2

**目标**：通过延迟合并和右链乐观重试，降低高并发删除场景下的锁等待与 P99 延迟
**现状基线**（mixed_with_delete，10 线程，30 万操作）：
- 锁等待总耗时：8.59 秒
- 锁等待次数：1968 万
- 合并尝试：3457 次，成功率 21%（730 次成功）
- P99 延迟：6.469 ms

**目标收益**：
- 🎯 锁等待时间降低 30-50%
- 🎯 无效合并尝试减少 50%+
- 🎯 P99 延迟改善 15-25%

---

## ⚠️ 风险评估与方案选择

### 原方案风险点

| 风险 | 严重程度 | 说明 |
|------|---------|------|
| **Page Header 改动** | 🔴 高 | 修改磁盘格式会破坏向后兼容，旧数据文件无法读取 |
| **崩溃恢复** | 🔴 高 | 内存队列丢失后，待合并页状态不一致，需要 redo log 支持 |
| **一致性风险** | 🟡 中 | 待合并页被其他事务访问时，可能看到中间状态 |
| **复杂度** | 🟡 中 | 同时引入队列+后台线程+标记位，调试困难 |
| **死锁风险** | 🟡 中 | 后台线程与前台事务可能争抢同一页面锁 |

### 方案对比

| 方案 | 改动量 | 风险 | 预期收益 | 推荐度 |
|------|-------|------|---------|-------|
| **A: 轻量版 - 跳过低收益合并** | 小 | 低 | 中 | ⭐⭐⭐⭐⭐ **先做** |
| **B: 中量版 - 内存标记+延迟触发** | 中 | 中 | 中高 | ⭐⭐⭐⭐ 次选 |
| **C: 完整版 - 异步队列+后台线程** | 大 | 高 | 高 | ⭐⭐⭐ 最后 |

### 推荐路线：A → B → C 渐进式

**先做方案 A**：不改磁盘格式，只在合并决策点加条件判断，跳过"低收益合并"
**验证有效后做方案 B**：在内存中标记待合并页，延迟到下次访问时合并
**最后考虑方案 C**：如果 A+B 收益不够，再引入后台线程

---

## 📋 分阶段计划

---

## Phase 0: 轻量版 - 跳过低收益合并 ⭐推荐先做

**目标**：在合并决策点增加更严格的条件，跳过"即使合并也很快会再分裂"的情况

**原理**：当前合并成功率只有 21%（730/3457），说明大量合并尝试是无效的。通过更智能的决策，直接跳过这些无效尝试。

**改动量**：约 20-50 行代码
**风险**：低（纯逻辑判断，无结构改动）

### Step 0.1：分析现有合并触发条件
- **文件**：`storage/innobase/btr/btr0btr.cc`
- **目标**：找到 `btr_compress()` 函数，理解当前合并触发逻辑
- **产出**：记录当前判断条件，理解 `merge_threshold` 的使用方式

### Step 0.2：增加合并跳过条件
- **文件**：`storage/innobase/btr/btr0btr.cc`
- **改动思路**：在 `btr_compress()` 入口或合并决策点增加以下条件

```cpp
// 新增跳过条件（伪代码）

// 条件1：兄弟页也很空，合并后仍很空，收益低
ulint sibling_data_size = page_get_data_size(sibling_page);
if (sibling_data_size < page_size * 0.3) {
  // 兄弟页利用率 < 30%，合并收益低
  MONITOR_INC(MONITOR_INDEX_MERGE_SKIPPED);
  return;
}

// 条件2：合并后太满，很快会再分裂
ulint current_data_size = page_get_data_size(page);
ulint merged_size = current_data_size + sibling_data_size;
if (merged_size > page_size * 0.85) {
  // 合并后利用率 > 85%，下次插入就会分裂
  MONITOR_INC(MONITOR_INDEX_MERGE_SKIPPED);
  return;
}

// 条件3（可选）：如果页面最近刚分裂过，跳过合并
// 避免 split-merge 循环
```

- **验证**：
  - 编译通过
  - 跑 mixed_with_delete，观察 `btree_merge_attempts` 是否下降
  - 确认无数据损坏

### Step 0.3：添加统计计数器
- **文件**：`storage/innobase/srv/srv0mon.cc` + `storage/innobase/include/srv0mon.h`
- **改动**：增加 `MONITOR_INDEX_MERGE_SKIPPED` 计数器
- **验证**：可通过 `SHOW STATUS LIKE 'Innodb_btree_merge_skipped'` 查看

### Phase 0 验收标准
- ✅ 编译通过，无新警告
- ✅ mixed_with_delete 测试：合并尝试次数下降 30%+
- ✅ 无数据损坏（校验测试数据完整性）
- ✅ 其他工作负载（insert）无性能回退

### Phase 0 测试命令
```bash
# 编译
cd /usr/local/mysql-8.0.34/build_debug
make -j8

# 运行测试
cd /usr/local/mysql-8.0.34/tests
python3 test_framework.py \
  --variant=optimized \
  --workload=mixed_with_delete \
  --threads=10 \
  --operations=300000

# 对比基线
python3 compare.py \
  --baseline out/runs/<baseline>/result.csv \
  --optimized out/runs/<phase0>/result.csv
```

---

## Phase 1: 中量版 - 内存标记 + 延迟触发

**前置条件**：Phase 0 完成并验证有效

**目标**：对于仍需要合并的页，不立即执行，而是在内存中标记，等下次访问时再触发

**改动量**：约 100-200 行代码
**风险**：中（需要处理并发访问）

### Step 1.1：在 Buffer Pool 页控制块中加标记
- **文件**：`storage/innobase/include/buf0buf.h`
- **改动**：在 `buf_page_t` 中加内存标记（**不改磁盘格式**）
```cpp
struct buf_page_t {
  // ... 现有字段 ...

  /** 标记该页是否待合并（仅内存状态，不持久化）
      崩溃后自动丢失，安全 */
  bool merge_pending{false};

  /** 标记时间戳，用于超时清理（微秒） */
  uint64_t merge_pending_time{0};
};
```

**为什么安全**：
- 内存标记，不写入磁盘，不改变数据文件格式
- 崩溃后标记自动丢失，页面保持原状态
- 最坏情况：该合并的页没合并，只影响空间利用率，不影响正确性

### Step 1.2：合并决策点改为"标记"而非"执行"
- **文件**：`storage/innobase/btr/btr0btr.cc`
- **改动**：在 Phase 0 的跳过逻辑之后
```cpp
// 如果没有被 Phase 0 跳过，但仍可以延迟
if (should_merge && !should_skip) {
  buf_block_t *block = btr_cur_get_block(cursor);

  // 标记待合并，而非立即执行
  block->page.merge_pending = true;
  block->page.merge_pending_time = ut_time_monotonic_us();

  MONITOR_INC(MONITOR_INDEX_MERGE_DEFERRED);
  // 释放锁，返回
  return;
}
```

### Step 1.3：在页访问时检查并触发延迟合并
- **文件**：`storage/innobase/btr/btr0cur.cc`
- **改动**：在 `btr_cur_search_to_nth_level()` 获取页后
```cpp
// 检查是否有待合并标记
if (block->page.merge_pending) {
  uint64_t now = ut_time_monotonic_us();
  uint64_t elapsed = now - block->page.merge_pending_time;

  // 超时清除（如 500ms）
  if (elapsed > 500000) {
    block->page.merge_pending = false;
  }
  // 如果当前持有足够的锁，尝试合并
  else if (latch_mode == BTR_MODIFY_TREE) {
    btr_compress(cursor, true, mtr);  // 执行合并
    block->page.merge_pending = false;
    MONITOR_INC(MONITOR_INDEX_MERGE_LAZY_DONE);
  }
}
```

### Phase 1 验收标准
- ✅ 编译通过
- ✅ 不修改磁盘格式（兼容性保持）
- ✅ 崩溃恢复安全（内存标记丢失不影响一致性）
- ✅ 锁等待时间下降 20%+

---

## Phase 2: 完整版 - 后台合并线程（可选）

**前置条件**：Phase 0 + Phase 1 完成，且收益仍不够

**目标**：专用后台线程批量执行延迟合并

**改动量**：约 300-500 行代码
**风险**：高（新增线程、锁交互复杂）

### Step 2.1：创建全局合并队列
- **文件**：新建 `storage/innobase/include/btr0merge.h`
```cpp
/** 合并任务（仅内存，不持久化） */
struct btr_merge_task_t {
  space_id_t space_id;
  page_no_t page_no;
  index_id_t index_id;
  uint64_t enqueue_time;
};

/** 全局合并队列 */
class btr_merge_queue_t {
public:
  void enqueue(const btr_merge_task_t &task);
  bool try_dequeue(btr_merge_task_t &task);
  size_t size() const;
  void clear();

  static constexpr size_t MAX_SIZE = 10000;  // 队列上限
private:
  std::deque<btr_merge_task_t> m_queue;
  mutable ib_mutex_t m_mutex;
};

extern btr_merge_queue_t *srv_merge_queue;
```

### Step 2.2：后台合并线程
- **文件**：新建 `storage/innobase/srv/srv0merge.cc`
```cpp
void srv_merge_thread() {
  while (srv_shutdown_state.load() == SRV_SHUTDOWN_NONE) {
    btr_merge_task_t task;

    if (srv_merge_queue->try_dequeue(task)) {
      btr_merge_execute_async(task);
    } else {
      os_event_wait_time(srv_merge_event, 10000);  // 10ms
    }
  }
}
```

### Step 2.3：启停钩子
- **文件**：`storage/innobase/srv/srv0start.cc`
- **改动**：在 `srv_start()` 和 `srv_shutdown()` 中管理线程

### Phase 2 验收标准
- ✅ 后台线程稳定运行，正常启停
- ✅ 队列有大小限制，不会 OOM
- ✅ 崩溃恢复安全
- ✅ mixed_with_delete 锁等待下降 40%+
- ✅ 无死锁

---

## Phase 3: 右链乐观重试（独立优化，可与上述并行）

**目标**：读写路径遇到 SMO 中的页时，尝试跳右兄弟而非等待

**改动量**：约 50-100 行代码
**风险**：中

### Step 3.1：在页遍历时加范围校验
- **文件**：`storage/innobase/btr/btr0cur.cc`
- **改动**：在 `btr_cur_search_to_nth_level()` 中
```cpp
// 校验 key 是否在当前页范围内
// 如果不在（可能正在 SMO），跳右兄弟重试
page_no_t right_page_no = btr_page_get_next(page, mtr);
if (right_page_no != FIL_NULL) {
  rec_t *supremum = page_get_supremum_rec(page);
  if (cmp_dtuple_rec(tuple, supremum, ...) > 0) {
    // key 超出当前页范围，跳右兄弟
    MONITOR_INC(MONITOR_INDEX_RIGHT_HOP);
    // 释放当前页，获取右兄弟，重试
  }
}
```

### Step 3.2：添加统计计数
```cpp
MONITOR_INDEX_RIGHT_HOP  // 跳右兄弟次数
```

### Phase 3 验收标准
- ✅ 乐观重试次数合理
- ✅ P99 延迟改善 15%+
- ✅ 无正确性问题

---

## 🎯 总体验收标准

| 指标 | 基线 | Phase 0 目标 | Phase 1 目标 | Phase 2 目标 |
|------|------|-------------|-------------|-------------|
| 合并尝试次数 | 3457 | ↓30% (~2400) | ↓50% (~1700) | ↓70% (~1000) |
| 锁等待时间 | 8.59s | ↓10% (~7.7s) | ↓25% (~6.4s) | ↓40% (~5.1s) |
| P99 延迟 | 6.47ms | ≈持平 | ↓15% (~5.5ms) | ↓25% (~4.9ms) |

---

## 📅 执行时间线

| Phase | 预计耗时 | 关键检查点 | 状态 |
|-------|---------|----------|------|
| 0 | 2-3 小时 | 编译通过，合并尝试下降 | ⬜ 待开始 |
| 1 | 3-4 小时 | 延迟触发正常工作 | ⬜ 待开始 |
| 2 | 4-6 小时 | 后台线程稳定 | ⬜ 可选 |
| 3 | 2-3 小时 | 右链跳转正常 | ⬜ 可并行 |

---

## 🔄 回退策略

**Phase 0**：删除新增的 if 判断即可
**Phase 1**：编译宏 `#define BTR_LAZY_MERGE 0` 禁用
**Phase 2**：不启动后台线程即可
**Phase 3**：编译宏 `#define BTR_RIGHT_HOP 0` 禁用

---

## 📝 Git 提交规范

每完成一个 Step，单独提交：
```bash
git add -A
git commit -m "phase0-step1: 分析合并触发条件"
git commit -m "phase0-step2: 增加合并跳过条件"
git commit -m "phase0-step3: 添加 MONITOR_INDEX_MERGE_SKIPPED 计数器"
```

每完成一个 Phase，打 tag：
```bash
git tag -a phase0-done -m "Phase 0 完成：跳过低收益合并"
```

---

**开始日期**：2026-01-09
**当前阶段**：Phase 0 待开始
