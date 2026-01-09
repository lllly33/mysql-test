# B+Tree 延迟合并 + 锁粒度优化计划

**目标**：通过延迟合并和右链乐观重试，降低高并发删除场景下的锁等待与 P99 延迟
**现状基线**（mixed_with_delete，10 线程，30 万操作）：
- 锁等待总耗时：8.59 秒
- 锁等待次数：1968 万
- 合并尝试：3457 次，成功率 21%（730 次成功）
- P99 延迟：6.469 ms

**目标收益**：
- 🎯 锁等待时间降低 30-50%
- 🎯 合并尝试次数降低 50%+ （异步化）
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
| **A: 轻量版 - 跳过低收益合并** | 小 | 低 | 中 | ⭐⭐⭐⭐⭐ 先做 |
| **B: 中量版 - 内存标记+延迟触发** | 中 | 中 | 中高 | ⭐⭐⭐⭐ 次选 |
| **C: 完整版 - 异步队列+后台线程** | 大 | 高 | 高 | ⭐⭐⭐ 最后 |

### 推荐路线：A → B → C 渐进式

**先做方案 A**：不改磁盘格式，只在合并决策点加条件判断，跳过"低收益合并"
**验证有效后做方案 B**：在内存中标记待合并页，延迟到下次访问时合并
**最后考虑方案 C**：如果 A+B 收益不够，再引入后台线程

---

## 📋 分阶段计划

---

## Phase 0: 轻量版 - 跳过低收益合并（推荐先做）

**目标**：在合并决策点增加更严格的条件，跳过"即使合并也很快会再分裂"的情况

**原理**：当前合并成功率只有 21%（730/3457），说明大量合并尝试是无效的。通过更智能的决策，直接跳过这些无效尝试。

### Step 0.1：分析现有合并触发条件
- **文件**：`storage/innobase/btr/btr0btr.cc`
- **目标**：找到 `btr_compress()` 和相关合并函数，理解触发条件
- **产出**：记录当前合并阈值逻辑

### Step 0.2：增加合并跳过条件
- **文件**：`storage/innobase/btr/btr0btr.cc`
- **改动思路**：
  ```cpp
  // 在 btr_compress() 或合并决策点
  // 原逻辑：页利用率 < merge_threshold 就尝试合并

  // 新增跳过条件：
  // 1. 如果兄弟页利用率也较低（合并后很快又会分裂），跳过
  // 2. 如果该页最近刚分裂过（时间窗口内），跳过
  // 3. 如果合并后的页利用率 > 85%（太满，插入会立即分裂），跳过

  bool should_skip_merge = false;

  // 条件1：兄弟页太空，合并收益低
  if (sibling_fill_rate < 30%) {
    should_skip_merge = true;
  }

  // 条件2：合并后太满
  ulint merged_size = current_used + sibling_used;
  if (merged_size > page_size * 0.85) {
    should_skip_merge = true;
  }

  if (should_skip_merge) {
    // 记录统计：跳过的合并次数
    srv_stats.btr_merge_skipped.inc();
    return;  // 不执行合并
  }
  ```
- **验证**：
  - 编译通过
  - 跑 mixed_with_delete，观察合并尝试次数是否下降
  - 确认无数据损坏

### Step 0.3：添加统计计数器
- **文件**：`storage/innobase/include/srv0srv.h` 或 `srv0mon.h`
- **改动**：增加 `btr_merge_skipped` 计数器，便于观测跳过了多少合并
- **验证**：统计数据可通过 `SHOW STATUS` 或日志输出

### Phase 0 验收标准
- ✅ 编译通过，无新警告
- ✅ mixed_with_delete 测试：合并尝试次数下降 30%+
- ✅ 无数据损坏（校验测试数据完整性）
- ✅ 其他工作负载无性能回退

---

## Phase 1: 中量版 - 内存标记 + 延迟触发

**前置条件**：Phase 0 完成并验证有效

**目标**：对于仍需要合并的页，不立即执行，而是在内存中标记，等下次访问时再触发

### Step 1.1：在 Buffer Pool 页控制块中加标记（不改磁盘格式）
- **文件**：`storage/innobase/include/buf0buf.h`
- **改动**：在 `buf_block_t` 或 `buf_page_t` 中加内存标记
  ```cpp
  struct buf_page_t {
    // ... 现有字段 ...

    /** 标记该页是否待合并（仅内存状态，不持久化） */
    bool merge_pending;

    /** 标记时间戳，用于超时清理 */
    uint64_t merge_pending_time;
  };
  ```
- **优点**：不改磁盘格式，崩溃后标记自动丢失（安全）
- **验证**：编译通过

### Step 1.2：合并决策点改为"标记"而非"执行"
- **文件**：`storage/innobase/btr/btr0btr.cc`
- **改动**：
  ```cpp
  // 原逻辑：立即执行合并
  // btr_compress(cursor, ...);

  // 新逻辑：标记待合并
  if (should_merge && !should_skip_merge) {
    buf_block_t *block = btr_cur_get_block(cursor);
    block->page.merge_pending = true;
    block->page.merge_pending_time = ut_time_monotonic_us();
    // 不立即合并，释放锁返回
  }
  ```
- **验证**：编译通过，insert 测试无错误

### Step 1.3：在页访问时检查并触发延迟合并
- **文件**：`storage/innobase/btr/btr0cur.cc`
- **改动**：在 `btr_cur_search_to_nth_level()` 或页访问入口处
  ```cpp
  // 获取页后，检查是否有待合并标记
  if (block->page.merge_pending) {
    // 检查是否超时（如 100ms 内不合并，清除标记）
    if (ut_time_monotonic_us() - block->page.merge_pending_time > 100000) {
      block->page.merge_pending = false;  // 超时清除
    } else {
      // 尝试合并（如果当前持有合适的锁）
      if (latch_mode == BTR_MODIFY_TREE) {
        btr_compress_lazy(cursor, mtr);
        block->page.merge_pending = false;
      }
    }
  }
  ```
- **验证**：
  - 编译通过
  - mixed_with_delete 测试，观察锁等待是否下降

### Phase 1 验收标准
- ✅ 编译通过
- ✅ 不修改磁盘格式（兼容性保持）
- ✅ 崩溃恢复安全（内存标记丢失不影响一致性）
- ✅ 锁等待时间下降 20%+

---

## Phase 2: 完整版 - 后台合并线程（可选）

**前置条件**：Phase 0 + Phase 1 完成，且收益仍不够

**目标**：专用后台线程批量执行延迟合并，进一步减少前台锁持有

### Step 2.1：创建合并队列数据结构（内存队列，非持久化）
- **文件**：新建 `storage/innobase/include/btr0merge.h`
- **改动**：
  ```cpp
  /** 合并任务（仅内存，不持久化） */
  struct btr_merge_task_t {
    space_id_t space_id;
    page_no_t page_no;
    index_id_t index_id;
    uint64_t enqueue_time;
  };

  /** 全局合并队列（不是每个索引一个，简化管理） */
  class btr_merge_queue_t {
  public:
    void enqueue(const btr_merge_task_t &task);
    bool try_dequeue(btr_merge_task_t &task);
    size_t size() const;
    void clear();  // 崩溃恢复时清空
  private:
    std::deque<btr_merge_task_t> m_queue;
    mutable ib_mutex_t m_mutex;
  };

  extern btr_merge_queue_t *srv_merge_queue;
  ```
- **关键设计**：
  - 全局单一队列，避免索引生命周期管理复杂性
  - 非持久化，崩溃后自动清空（安全）
  - 有大小限制，防止内存膨胀

### Step 2.2：后台合并线程
- **文件**：新建 `storage/innobase/srv/srv0merge.cc`
- **改动**：
  ```cpp
  /** 后台合并线程入口 */
  void srv_merge_thread() {
    while (srv_shutdown_state == SRV_SHUTDOWN_NONE) {
      btr_merge_task_t task;
      if (srv_merge_queue->try_dequeue(task)) {
        // 执行合并
        btr_merge_execute(task);
      } else {
        // 队列空，睡眠等待
        os_event_wait_time(srv_merge_event, 10000);  // 10ms
      }
    }
  }
  ```
- **启停钩子**：在 `srv_start()` 和 `srv_shutdown()` 中管理线程

### Step 2.3：前台入队逻辑
- **文件**：`storage/innobase/btr/btr0btr.cc`
- **改动**：Phase 1 的标记改为入队
  ```cpp
  if (should_merge && !should_skip_merge) {
    btr_merge_task_t task = {space_id, page_no, index_id, now()};
    if (srv_merge_queue->size() < MAX_MERGE_QUEUE_SIZE) {
      srv_merge_queue->enqueue(task);
    }
    // 立即返回，不等待合并完成
  }
  ```

### Phase 2 验收标准
- ✅ 后台线程稳定运行
- ✅ 队列有大小限制，不会 OOM
- ✅ 崩溃恢复安全
- ✅ mixed_with_delete 锁等待下降 40%+

---

## Phase 3: 右链乐观重试（独立优化，可并行）

**目标**：读写路径遇到正在 SMO 的页时，尝试跳右兄弟而非等待
  ```cpp
  // 原逻辑：同步合并，长闩持有
  // if (should_merge) {
  //   btr_merge_pages(...);  // 同步，阻塞其他线程
  // }

  // 新逻辑：标记待合并，异步处理
  if (should_merge) {
    page_set_merge_pending(page);  // 标记页面
    btr_merge_task task = {space_id, page_no, nullptr, now()};
    index->merge_queue->enqueue(task);  // 入队
    // 立即返回，前台闩释放
  }
  ```
- **验证**：
  - 编译通过
  - 跑 insert 工作负载，确认没有新错误（页标记机制正常工作）

**Step 2.3：添加编译开关与日志**
- **文件**：`storage/innobase/include/univ.i` 或 `config.h.cmake`
- **改动**：加编译宏 `#define BTR_LAZY_MERGE 1`，用于灰度控制
- **验证**：可通过宏开关快速回退

---

### **Phase 3: 后台线程 - 异步合并执行**

**目标**：专用后台线程批量执行入队的合并任务

**Step 3.1：创建后台合并线程框架**
- **文件**：`storage/innobase/srv/srv0srv.cc` 或新文件 `storage/innobase/btr/btr0merge_worker.cc`
- **改动**：
  ```cpp
  class btr_merge_worker {
  public:
    btr_merge_worker();
    void start();  // 启动后台线程
    void stop();
  private:
    void worker_thread_func();  // 线程入口
    std::thread worker;
  };
  ```
- **验证**：编译通过，线程正常启停

**Step 3.2：后台线程实现合并执行**
- **文件**：`storage/innobase/btr/btr0merge_worker.cc`
- **改动**：
  ```cpp
  void btr_merge_worker::worker_thread_func() {
    while (running) {
      // 从所有索引的合并队列中获取待合并页
      for (auto index : all_indexes) {
        if (!index->merge_queue->is_empty()) {
          btr_merge_task task = index->merge_queue->dequeue();

          // 执行实际合并（原来的同步逻辑）
          mtr_t mtr;
          mtr.start();
          btr_merge_pages(task.space_id, task.page_no, &mtr);
          mtr.commit();
        }
      }
      // 定期检查，避免忙轮询
      std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
  }
  ```
- **验证**：
  - 编译通过
  - 跑 mixed_with_delete 工作负载
  - 观察：合并尝试次数是否下降（预期由后台异步处理）
  - 检查：后台线程是否启动，合并队列是否有任务出入

**Step 3.3：添加启停钩子**
- **文件**：`storage/innobase/srv/srv0srv.cc`
- **改动**：在 MySQL 服务启动/停止时初始化/销毁合并工作线程
- **验证**：启动日志中能看到"Merge worker thread started"

---

### **Phase 4: 右链乐观重试 - 遍历层范围校验**

**目标**：读写路径遇到"待合并"页时，直接跳右兄弟而非等待，减少锁竞争

**Step 4.1：在页遍历时加范围校验**
- **文件**：`storage/innobase/btr/btr0cur.cc`
- **改动**：在 `btr_cur_search_to_level()` 中，加页范围检查：
  ```cpp
  // 遍历找到目标页后，校验 key 是否在页的 [min_key, max_key] 范围
  if (!page_contains_key(page, search_key)) {
    // key 不在该页，说明页可能正在分裂或标记待合并
    // 跳到右兄弟重试
    page_id_t right_sibling = page_get_next_page_id(page);
    if (right_sibling != FIL_NULL) {
      mtr_release_page(page);  // 释放当前页闩
      page = buf_page_get(right_sibling, ...);  // 获取右兄弟
      goto retry;  // 重试查找
    }
  }
  ```
- **验证**：
  - 编译通过
  - insert 工作负载，观察是否有乐观重试成功的统计数据

**Step 4.2：为待合并页加跳转标记**
- **文件**：`storage/innobase/btr/btr0btr.cc`
- **改动**：在标记待合并时，同时记录右兄弟信息，便于读路径快速跳转
- **验证**：编译通过

**Step 4.3：添加统计计数**
- **文件**：新增 `storage/innobase/include/btr0stats.h` 或在既有地方加计数器
- **改动**：
  ```cpp
  struct btr_page_merge_stats {
    uint64_t merge_deferred;       // 延迟合并次数
    uint64_t merge_async_done;     // 后台完成合并次数
    uint64_t skip_right_on_merge;  // 因待合并跳右兄弟次数
    uint64_t right_hop_retries;    // 乐观重试次数
  };
  ```
- **验证**：统计数据正确输出

---

### **Phase 5: 测试与验证**

**每个 Phase 完成后的测试流程**：

```bash
# 1. 编译
cd /usr/local/mysql-8.0.34/build_debug
cmake .. -DCMAKE_BUILD_TYPE=Debug
make -j8

# 2. 启动数据库
mysqld --datadir=./data --socket=./mysql.sock --pid-file=./mysql.pid &

# 3. 跑测试工作负载
cd /usr/local/mysql-8.0.34/tests
python3 test_framework.py \
  --variant=current \
  --workload=mixed_with_delete \
  --threads=10 \
  --operations=300000 \
  --config config.yaml \
  --output out/runs/phase_N_test

# 4. 采集对比结果
python3 compare.py \
  --baseline <上一阶段结果> \
  --optimized out/runs/phase_N_test/result.csv \
  --output out/reports

# 5. 验证指标
# 关键看：
# - 锁等待时间（waits_top_sum_s）是否下降
# - 合并尝试次数（btree_merge_attempts）是否下降
# - P99 延迟（p99_ms）是否改善
```

---

## 🎯 验收标准

### Phase 1 验收
- ✅ 编译无新警告/错误
- ✅ page header 大小不变
- ✅ 合并队列数据结构正确定义

### Phase 2 验收
- ✅ 编译通过，insert 工作负载无新错误
- ✅ 可通过宏快速回退

### Phase 3 验收
- ✅ 后台线程正常启停
- ✅ mixed_with_delete 测试中，合并尝试次数 ≥ 30% 下降
- ✅ 锁等待时间 ≥ 20% 下降
- ✅ 无内存泄漏（valgrind/asan）

### Phase 4 验收
- ✅ 乐观重试次数统计合理（高并发下应有显著重试）
- ✅ P99 延迟 ≥ 15% 改善
- ✅ 总体吞吐不下降

### Phase 5 验收
- ✅ mixed_with_delete 工作负载对比基线，目标指标达成
- ✅ 其他工作负载（insert, range_insert）无性能回退

---

## 📅 执行时间线

| Phase | 预计耗时 | 关键检查点 |
|-------|---------|----------|
| 1 | 1-2 小时 | 编译通过，page header 完整性 |
| 2 | 2-3 小时 | insert 测试无错，日志清晰 |
| 3 | 3-4 小时 | 后台线程稳定，队列出入正常 |
| 4 | 2-3 小时 | 乐观重试有效工作，统计合理 |
| 5 | 1-2 小时 | 完整对比测试，指标验证 |

---

## 🔄 回退策略

每个 Phase 都可通过编译宏独立控制：
- `BTR_LAZY_MERGE=0` 禁用延迟合并
- `BTR_BLINK_OPTIMIZATION=0` 禁用右链优化

---

## 📝 后续改进（可选）

- 合并队列优先级：高热度页优先合并
- 合并批处理：批量合并相邻页面
- 自适应后台线程数：根据队列深度自动扩缩容
- 预测性合并：根据访问模式提前预合并

---

**开始日期**：2026-01-09
**预计完成**：2026-01-13

