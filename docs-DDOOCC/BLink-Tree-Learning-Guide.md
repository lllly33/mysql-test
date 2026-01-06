# B+Tree SMO 并发优化学习路线（候选方案：BLink-Tree 等）- 为初学者设计

> 目标：围绕“高并发 + 高 I/O 延迟（云盘）下的 B+Tree SMO（分裂/合并）锁竞争”梳理可行方案，并能落到 MySQL 8.0.34 InnoDB 源码上。
>
> 说明：文档里提到的 BLink-Tree **不是最终方案**，而是一个你能想到、也确实常见的候选方案；本指南会把“方案空间”和“MySQL 8.0.34 现状（已有努力）”补齐，便于做方案对比与答题。

---

## 第一部分：概念基础（必读）

## 0. 方案空间速览（不止 BLink-Tree）

把问题拆成两类成本：
- **锁成本**：SMO 时为了结构一致性，需要更强的 latch/lock，导致并发退化。
- **I/O 成本**：云盘延迟高，SMO 中的页分配/写入/刷脏会把持锁时间放大。

下面是常见方案光谱（从低侵入到高侵入）：

### 0.1 低侵入（优先评估：不改代码也可能见效）

- **减少分裂频率**：让页更不容易满（例如：更合理的填充率/批量导入/避免高度随机的热点 key）。
- **把热点留在内存**：提高 buffer pool 命中率可以直接减少“持锁等待 I/O”的时间（这个在云盘更明显）。
- **让写更像顺序写**：如果业务允许，避免“随机插入 + 单点热点”，对 B+Tree 极其关键。
- **针对二级索引写**：InnoDB 已有 change buffer（插入缓冲）可在某些场景显著减少随机 I/O。

这些属于“工程手段”，不改变算法，但常常能把最坏情况压下来。

### 0.2 中侵入（仍使用 B+Tree，但更激进地缩小 SMO 影响面）

- **更强的乐观路径**：尽可能让操作停留在 `BTR_MODIFY_LEAF`（只锁叶子）而不是升级到 `BTR_MODIFY_TREE`（树级修改）。
- **更短的树级锁持有时间**：把“必须持有树级锁”的代码段缩到最小，其他工作（例如部分拷贝/准备工作）在更弱锁下完成。
- **分裂/合并的批处理或后台化（有限）**：将“父节点更新”从关键路径挪开（注意：这类做法很容易变成 BLink 思路的一部分）。
- **热点拆分（逻辑层）**：例如把热点 key 分散到多个索引/分区，降低同一棵树上的 SMO 冲突概率。

这类方案通常改动比 BLink 小，但收益也更不确定；优势是对存储格式/恢复逻辑冲击较小。

### 0.3 高侵入（更换树/索引结构）

- **BLink-Tree（Lehman–Yao）**：通过 right-link 允许并发查询绕过正在分裂的节点，典型目标就是“SMO 不阻塞读/少阻塞写”。
- **Bw-Tree（delta chain / mapping table）**：把 SMO 变成逻辑追加（delta），后台 consolidate；对 latch/IO 模型友好，但改动非常大。
- **Masstree / latch-free 变种**：依赖更复杂的并发控制与内存模型，同样属于“研究型/大工程”。

如果是 KSC 问题回答：可以把 BLink 当作“最贴近传统 B+Tree 的并发优化”来讲，同时补充 Bw-Tree/LSM 思路作为备选。

### 1.1 当前InnoDB B+树的问题

#### 现状：SMO操作的全局锁定
```
插入数据时的流程：
1. 从root往下查找合适的leaf
   └─ 获取 S-Latch(读锁) 沿途经过的页

2. 在leaf插入记录
   └─ 升级为 X-Latch(写锁)

3. 🔴 如果leaf满了，触发分裂(Split)
  └─ 进入“树级修改”路径（BTR_MODIFY_TREE）
  └─ 获取该索引的 tree latch（dict_index_get_lock(index) 的 SX/X），结构修改并发被强限制
  └─ 对当前 leaf 及左右兄弟页做 X-latch（避免链表/结构更新时的并发问题）
  └─ 父层 node_ptr 更新可能递归向上，导致持锁时间被 I/O 延迟放大（云盘更明显）

4. 分裂传播到parent、grandparent...甚至root
   └─ 锁持有时间很长
   └─ 特别是在云盘(延迟200us)上时间更长
```

> 更精确的结论：这里通常不是“把整棵树每个节点页都逐个 X-latch 锁死”，而是
> 1) 进入 BTR_MODIFY_TREE 后拿到索引级 tree latch（SX 或 X），让结构修改在索引维度上高度串行化/强限制；
> 2) 叶子层还会 X-latch 当前页和兄弟页；
> 3) 父层更新会触发从 root 重新定位目标层（逻辑上重新走一遍路径），在高延迟存储下把持锁窗口放大。

#### 这会“挡住谁”？（更贴近你直觉的并发影响）

- **最容易被挡住的**：同一索引上、也需要进入 `BTR_MODIFY_TREE` 的线程（它们要拿 `dict_index_get_lock(index)` 的 SX/X）。
- **经常会被挡住的**：碰巧落在同一 leaf/兄弟页上的写线程（因为 SMO 会对“左/当前/右”页做 `RW_X_LATCH`）。
- **不等价于“所有读写都停摆”**：
  - 纯读一般走 `mtr_s_lock(dict_index_get_lock(index))` + 页级 `RW_S_LATCH`，它是否会被阻塞取决于 tree latch 当前是 SX 还是 X、以及读是否碰巧需要读到被 X-latch 的页。
  - 8.0.34 在树级修改场景里常用 `SX` 而不是无条件 `X`（见下方证据 1），就是为了降低“非必要的读阻塞”。

#### 代码证据（MySQL 8.0.34）

1) 进入树级修改时会对索引加 tree latch（SX 或 X），而不是“静默继续”：

- 代码位置：[storage/innobase/btr/btr0cur.cc](storage/innobase/btr/btr0cur.cc#L800-L846)

```cpp
switch (latch_mode) {
  case BTR_MODIFY_TREE:
    if (/* 一些条件 */) {
      mtr_x_lock(dict_index_get_lock(index), mtr, UT_LOCATION_HERE);
    } else {
      mtr_sx_lock(dict_index_get_lock(index), mtr, UT_LOCATION_HERE);
    }
    upper_rw_latch = RW_X_LATCH;
    break;
  // ...
}
```

2) 在 BTR_MODIFY_TREE 下，叶子层会按“左→当前→右”的顺序 X-latch 同层兄弟页（并且断言你已经持有 index 的 SX/X tree latch）：

- 代码位置：[storage/innobase/btr/btr0cur.cc](storage/innobase/btr/btr0cur.cc#L200-L276)

```cpp
case BTR_MODIFY_TREE:
  /* It is exclusive for other operations which calls
  btr_page_set_prev() */
  ut_ad(mtr_memo_contains_flagged(mtr, dict_index_get_lock(cursor->index),
                                  MTR_MEMO_X_LOCK | MTR_MEMO_SX_LOCK) ||
        cursor->index->table->is_intrinsic());
  /* x-latch also siblings from left to right */

  left_page_no = btr_page_get_prev(page, mtr);
  if (left_page_no != FIL_NULL) {
    get_block = btr_block_get(..., RW_X_LATCH, ...);
  }

  get_block = btr_block_get(page_id, page_size, RW_X_LATCH, ...);

  right_page_no = btr_page_get_next(page, mtr);
  if (right_page_no != FIL_NULL) {
    get_block = btr_block_get(..., RW_X_LATCH, ...);
  }

  return (latch_leaves);
```

3) 分裂触发链路：悲观插入明确要求 mtr 已持有 tree latch（X/SX），随后调用 split：

- 代码位置（悲观插入注释与断言）：[storage/innobase/btr/btr0cur.cc](storage/innobase/btr/btr0cur.cc#L2878-L2912)

```cpp
/** Performs an insert on a page of an index tree. It is assumed that mtr
 holds an x-latch on the tree and on the cursor page. If the insert is
 made on the leaf level, to avoid deadlocks, mtr must also own x-latches
 to brothers of page, if those brothers exist.
 @return DB_SUCCESS or error number */

ut_ad(mtr_memo_contains_flagged(mtr, dict_index_get_lock(cursor->index),
                                MTR_MEMO_X_LOCK | MTR_MEMO_SX_LOCK) ||
      cursor->index->table->is_intrinsic());

// ...
*rec = btr_page_split_and_insert(flags, cursor, offsets, heap, entry, mtr);
```

4) 父层更新会“从 root 重新定位目标层”（逻辑上重新走路径），并且可能递归触发更高层 split：

- 代码位置（在非叶子层插入 node_ptr 时调用 search）：[storage/innobase/btr/btr0btr.cc](storage/innobase/btr/btr0btr.cc#L1925-L2006)

```cpp
btr_cur_search_to_nth_level(index, level, tuple, PAGE_CUR_LE,
                            BTR_CONT_MODIFY_TREE, &cursor, ... , mtr);
```

- 代码位置（注释明确“可能递归导致更高层 split”）：[storage/innobase/btr/btr0btr.cc](storage/innobase/btr/btr0btr.cc#L2068-L2112)

```cpp
/* Insert it next to the pointer to the lower half. Note that this
may generate recursion leading to a split on the higher level. */
btr_insert_on_non_leaf_level(flags, index, level + 1, node_ptr_upper,
                             UT_LOCATION_HERE, mtr);
```

**问题量化**：
- 本地盘NVMe：10us延迟 → 分裂耗时~100us
- 云盘ESSD：200us延迟 → 分裂耗时~2ms (20倍!)
- 高并发场景：分裂等待队列堆积

---

### 1.2 BLink-Tree的解决方案

#### 核心改进：右兄弟链指针

```
传统B+树（分裂前）：
    [Root]
      |
   [Parent]
      |
   [Leaf: 1 2 3 4 | 5 6 7 8]
    ^
    查询路径


传统B+树（分裂后）：
    [Root]              <- 需要修改
      |
   [Parent]            <- 需要修改
      |
   [Leaf: 1 2 3 4] [新Leaf: 5 6 7 8]
    ^                  ^
    查询可能过来！      新兄弟


问题：查询线程需要等待root和parent的更新完成！
时间长 → 并发下锁冲突严重
```

#### BLink-Tree解决方案（加右指针）：

```
BLink-Tree（分裂前）：
    [Root]
      |
   [Parent]
      |
   [Leaf: 1 2 3 4] → [空指针]
    ^
    查询路径


BLink-Tree（分裂后）：
    [Root]              <- ✅ 不需要立即修改
      |
   [Parent]            <- ✅ 不需要立即修改
      |
   [Leaf A: 1 2 3 4] → [Leaf B: 5 6 7 8] → [原来的右兄弟]
    ^
    查询线程：
    - 如果要找5：发现5不在Leaf A
    - 沿着右指针 → 继续在Leaf B中查找 ✅
    - 不用回到root重新查询！

实际修改parent/root：延迟进行，异步完成
```

**关键优势**：
- ✅ 分裂无需立即更新整个路径
- ✅ 查询线程可沿右指针继续
- ✅ 写线程间锁冲突大幅降低
- ✅ 并发度从序列化 → 真正并行

---

### 1.3 分裂/合并操作对比

| 操作 | 当前B+树 | BLink-Tree | 改进 |
|-----|---------|-----------|------|
| 分裂触发 | Leaf满 | Leaf满 | 相同 |
| 分裂过程 | 从root锁定整路径 | 只锁当前leaf | **并发度↑** |
| 路径更新 | 立即完成 | 延迟/异步 | **延迟↓** |
| 分裂期间查询 | 完全阻塞 | 可沿右指针继续 | **吞吐↑** |
| 合并（删除） | 同上 | 同上 | **相同改进** |
| 实现复杂度 | 简单 | 中等偏高 | **代价：代码增加** |

---

## 第二部分：代码结构导航

## 2.0 MySQL 8.0.34 在这方面已经做了什么（源码证据）

这一节非常重要：很多“看起来要做 BLink 才能解决”的问题，其实 8.0.34 已经通过工程手段做过一部分缓解。你在设计方案时，最好把这些现有机制当作“基线”，评估你的方案到底还能再减少多少锁竞争。

- **区分“只改叶子” vs “改树结构”**：搜索/修改路径里明确存在 `BTR_MODIFY_LEAF` 与 `BTR_MODIFY_TREE` 两套策略。
  - 在 `BTR_MODIFY_LEAF` 下，通常只需要对叶子页做 `RW_X_LATCH`（或搜索时 `RW_S_LATCH`）。
  - 一旦进入 `BTR_MODIFY_TREE`，会牵涉到 sibling（左右兄弟）以及父节点更新，因此会更强地加锁。
  - 参考：`btr_cur_latch_leaves()` 的 `switch (latch_mode)` 分支（../storage/innobase/btr/btr0cur.cc）。

- **树级锁不总是全 X，而是尽量用 SX 降低冲突**：在 `BTR_MODIFY_TREE` 分支里，代码会根据场景选择 `mtr_x_lock()` 或 `mtr_sx_lock()`：
  - purge 压力大、或空间/IO 特殊场景可能升级为 X；否则倾向 SX。
  - 参考：`btr_cur_search_to_nth_level()` 进入树级修改前对 `dict_index_get_lock(index)` 的加锁逻辑（../storage/innobase/btr/btr0cur.cc）。

- **严格的 sibling 加锁顺序，避免死锁**：树级修改时会按“左→当前→右”顺序 X-latch 同层兄弟页。
  - 这正是 SMO 场景（更新 `FIL_PAGE_PREV/NEXT` 等链表关系）需要的。
  - 参考：`BTR_MODIFY_TREE` 下对 `btr_page_get_prev()`/`btr_page_get_next()` 的处理（../storage/innobase/btr/btr0cur.cc）。

- **AHI（自适应哈希索引）减少走树与持锁时间**：在满足条件时，搜索会走 `btr_search_guess_on_hash()`，减少 B+Tree 下降次数。
  - 这不直接改变 SMO，但能降低大量读/点查对 B+Tree latch 的压力，从而让 SMO 冲突概率变小。
  - 参考：../storage/innobase/btr/btr0cur.cc 与 ../storage/innobase/btr/btr0sea.cc。

- **Change Buffer（插入缓冲）降低二级索引随机 I/O**：当叶子页不在 buffer pool 且满足条件时，会尝试把二级索引变更写入 ibuf（之后后台合并），减少“持锁等待读入页”的概率。
  - 参考：`ibuf_should_try()` 与 `ibuf_insert()` 调用路径（../storage/innobase/btr/btr0cur.cc）。

- **页级链指针本来就存在（`FIL_PAGE_PREV/NEXT`）**：同一 `PAGE_LEVEL` 的 B-tree index pages 会通过 `FIL_PAGE_PREV` / `FIL_PAGE_NEXT` 维护双向链表。
  - 这与 BLink 的 right-link 在“形式”上相似，但语义不同：BLink 还需要处理“分裂进行中”的一致性（通常要配合 high-key/side-link 规则）。
  - 参考：`FIL_PAGE_PREV`/`FIL_PAGE_NEXT` 常量定义（../storage/innobase/include/fil0types.h）。

### 2.1 关键文件及其职责

```
storage/innobase/btr/
├── btr0btr.cc              [关键]
│   ├── btr_page_split_and_insert()  LINE 2278
│   │   └─ 当前的分裂实现
│   │   └─ 需要修改：添加右指针逻辑
│   │
│   ├── btr_root_raise_and_insert()  LINE 1455
│   │   └─ root分裂的特殊情况
│   │   └─ 需要修改：处理新增的链指针
│   │
│   └── btr_compress()               LINE 2970
│       └─ 节点合并/压缩操作
│       └─ 需要修改：处理右指针删除
│
├── btr0cur.cc              [关键]
│   ├── btr_cur_search_to_nth_level()  LINE 618
│   │   └─ 查询路径搜索
│   │   └─ 需要修改：失败时沿右指针续搜
│   │
│   └── btr_cur_optimistic_insert()    LINE 2659
│       └─ 乐观插入
│       └─ 需要修改：分裂时的新逻辑
│
├── btr0pcur.cc             [辅助]
│   └─ 持久化游标，记录路径
│   └─ 可能需要修改以支持右指针跟踪
│
└── include/btr0cur.h & btr0btr.h
    └─ 数据结构定义
    └─ 需要修改：添加right_link字段

storage/innobase/include/
├── page0types.h            [重要]
│   └─ 页面结构定义
│   └─ 需要修改：页头添加右指针存储

lock/
└── lock0lock.cc            [理解即可]
    └─ 锁管理
    └─ 当前：不需要修改（BLink改进就是减少锁需求）
```

### 2.2 数据结构：搜索路径

现在看看MySQL中的关键结构：

```cpp
// 当前游标结构 (storage/innobase/include/btr0cur.h)
struct btr_cur_t {
  dict_index_t *index;       // 所在索引
  rec_t *rec;                // 当前记录指针
  buf_block_t *left_block;   // 左兄弟页（用于范围扫描）
  // ... 其他字段
};

// 需要添加的字段（用于BLink-Tree）：
struct btr_cur_t {
  // ... 原有字段
  buf_block_t *right_block;  // ✅ 新增：右兄弟页
  ulint right_link_offset;   // ✅ 新增：右指针在页中的位置
};
```

```cpp
// 页面结构 (storage/innobase/include/page0types.h)
struct page_t {
  // 页头信息...
  byte FIL_PAGE_TYPE;        // 页类型
  byte FIL_PAGE_PREV;        // 前驱页号
  byte FIL_PAGE_NEXT;        // 后继页号（当前）
  // ... 其他字段
};

// 需要添加的字段：
struct page_t {
  // ... 原有字段
  byte FIL_PAGE_RIGHT_LINK;  // ✅ 新增：右链指针（用于BLink）
  // 这与FIL_PAGE_NEXT不同：
  // - FIL_PAGE_NEXT：逻辑兄弟（父节点管理）
  // - FIL_PAGE_RIGHT_LINK：物理链接（分裂时动态更新）
};
```

---

## 第三部分：具体代码修改步骤

### 3.1 第一阶段：理解现有代码（1小时）

#### Step 1: 找到分裂的触发点

```bash
# 在terminal中搜索
cd /usr/local/mysql-8.0.34
grep -n "btr_page_split_and_insert" storage/innobase/btr/btr0cur.cc

# 输出可能是：
# 3084:        *rec = btr_page_split_and_insert(flags, cursor, offsets,
# heap, entry, mtr);
```

在 `btr0cur.cc` 第3084行附近，会看到：

```cpp
// 当插入失败时的回退逻辑
if (!*rec) {
    // 页面空间不足
    *rec = btr_page_split_and_insert(...);  // ← 调用分裂
}
```

**你需要理解的**：
- 什么时候触发分裂？
- 分裂后如何处理cursor？
- 分裂的返回值是什么？

#### Step 2: 阅读分裂实现

打开 `btr0btr.cc`，找到 `btr_page_split_and_insert()` (第2278行):

```cpp
rec_t *btr_page_split_and_insert(
    ulint flags,           // 操作标志
    btr_cur_t *cursor,     // 游标（输入：在待分裂叶）
    ulint **offsets,       // 记录偏移
    mem_heap_t **heap,
    const dtuple_t *tuple, // 要插入的新记录
    mtr_t *mtr)            // 事务日志
{
  // 第2278行开始
  // 这个函数约700行，做的事情：

  // 1. 确定分裂点
  split_rec = btr_page_get_split_rec(...);

  // 2. 创建新页
  new_block = btr_page_allocate(...);

  // 3. 复制记录到新页（数据复制）
  btr_page_copy_rec_list_end(new_page, page, split_rec, ...);

  // 4. 删除旧页中已复制的记录
  page_delete_rec_list_start(...);

  // 5. 向parent插入指向新页的指针
  btr_insert_on_non_leaf_level(...);  // ← 这导致树修改传播！

  // 6. 返回新插入位置
  return (rec);
}
```

**关键观察点**：
- 第5步 `btr_insert_on_non_leaf_level()` 是性能瓶颈
- 它会递归向上修改整个路径
- BLink改进就是延迟这一步

#### Step 3: 理解锁的获取时点

在 `btr_cur_search_to_nth_level()` (第618行):

```cpp
void btr_cur_search_to_nth_level(
    dict_index_t *index,
    ulint level,
    const dtuple_t *tuple,
    page_cur_mode_t mode,
    ulint latch_mode,      // ← 关键：指定锁定模式
    btr_cur_t *cursor,
    mtr_t *mtr)
{
  // 第822行：如果要修改树，获取X-Latch
  if (latch_mode == BTR_MODIFY_TREE) {
    mtr_x_lock(dict_index_get_lock(index), mtr, UT_LOCATION_HERE);
    // ↑ 这里！整个树被锁定！
  }

  // 然后搜索...
  while (height > level) {
    // 从root往下遍历
    block = buf_page_get_gen(page_id, ...);

    if (/* 不需要分裂 */) {
      // 可以立即释放上层页的锁
      for (n_releases < n_blocks; n_releases++) {
        mtr_release_block_at_savepoint(...);
      }
    }
  }
}
```

**关键理解**：
- `mtr_x_lock()` 是全局锁（针对整个索引）
- BLink改进就是改为逐页锁
- 分裂时只锁当前leaf和parent，不锁整个树

---

### 3.2 第二阶段：实现BLink结构（3小时）

> 注意：下面的“实现 BLink”更像是**原型推演/学习用路线**。
> 真实落地时，最难的往往不是“加一个 right pointer”，而是：
> - 需要保证 crash-recovery/redo/压缩页/页格式兼容；
> - 需要定义并验证并发语义（读在分裂中如何安全前进）；
> - 需要处理所有树层级（不只 leaf）。

#### 修改步骤 1：添加数据结构字段

**文件**：`storage/innobase/include/btr0cur.h`

添加右链指针到游标结构：

```cpp
struct btr_cur_t {
  // ... 已有字段
  buf_block_t *left_block;

  // ✅ 新增字段
  buf_block_t *right_block;    // 右兄弟页面块
  page_no_t right_link_page_no; // 右链指向的页号
};
```

#### 修改步骤 2：关于“右链指针存在哪里”（先做正确的工程判断）

如果你真的要做 BLink，需要先回答：right-link 要不要“占用页头字段”。这里有两条路线：

1) **尽量复用现有链指针（优先考虑）**
- InnoDB 已经在页头维护 `FIL_PAGE_PREV`/`FIL_PAGE_NEXT`。
- 对于 index pages，同一 `PAGE_LEVEL` 会按记录顺序维护链表。
- 参考：`FIL_PAGE_PREV/FIL_PAGE_NEXT` 定义在 `storage/innobase/include/fil0types.h`，并且有对应的读写封装（例如 `storage/innobase/include/btr0btr.ic`）。

2) **新增专用 right-link 字段（高风险/高成本）**
- 这会触及：页格式、redo logging、压缩页（page_zip_*）、工具链（备份/校验/崩溃恢复）等。
- 因此不建议把它当成“3 小时就能做完”的工作量；除非你只做概念验证（PoC）且接受格式不兼容。

#### 修改步骤 3：修改分裂逻辑

**文件**：`storage/innobase/btr/btr0btr.cc` 中的 `btr_page_split_and_insert()`

伪代码改进：

```cpp
rec_t *btr_page_split_and_insert(...) {
  // 原有逻辑...

  // 1. 创建新页
  new_block = btr_page_allocate(...);
  new_page = buf_block_get_frame(new_block);

  // 2. 复制记录到新页
  btr_page_copy_rec_list_end(...);

  // ✅ BLink改进：建立链指针
  {
    // 3a. 获取当前页的原右兄弟
    page_no_t old_right = mach_read_from_4(page + FIL_PAGE_NEXT);

    // 3b. 当前页的右链指向新页
    mach_write_to_4(page + FIL_PAGE_RIGHT_LINK, new_block->page.id.page_no());

    // 3c. 新页的右链指向原来的右兄弟
    mach_write_to_4(new_page + FIL_PAGE_RIGHT_LINK, old_right);

    // 日志记录这些写操作
    mlog_write_ulint(page + FIL_PAGE_RIGHT_LINK, ..., MLOG_4BYTES, mtr);
  }

  // ✅ BLink改进：延迟parent更新
  {
    // 原代码立即调用：
    // btr_insert_on_non_leaf_level(level + 1, node_ptr, mtr);

    // 改为：将更新操作加入异步队列
    // 或者标记page为"待同步"状态
    // 先继续，不阻塞其他查询

    // 暂存parent指针信息，供后台线程处理
    btr_insert_on_non_leaf_level_deferred(level + 1, node_ptr, mtr);
  }

  return (rec);
}
```

---

### 3.3 第三阶段：修改查询逻辑（2小时）

#### 查询时的右链跟踪

**文件**：`storage/innobase/btr/btr0cur.cc` 中的 `btr_cur_search_to_nth_level()`

伪代码改进：

```cpp
void btr_cur_search_to_nth_level(...) {
  // ... 原有初始化

  while (height > level) {
    block = buf_page_get_gen(page_id, page_size, RW_S_LATCH, ...);
    page = buf_block_get_frame(block);

    // 在页中查找记录
    page_cur_search_with_match(block, index, tuple, mode, ...);

    // ✅ BLink改进：如果记录不在此页
    if (page_cur_get_rec(page_cursor) == page_get_supremum_rec(page)) {
      // 超过当前页最大值

      // 检查右链
      page_no_t right_link =
          mach_read_from_4(page + FIL_PAGE_RIGHT_LINK);

      if (right_link != FIL_NULL) {
        // ✅ 沿右链继续查询，不回到parent！
        block = buf_page_get_gen(
            page_id_t(page_id.space(), right_link),
            page_size, RW_S_LATCH, ...);
        page = buf_block_get_frame(block);

        // 继续在新页中搜索
        page_cur_search_with_match(block, index, tuple, mode, ...);
      } else {
        // 没有右链，进入parent搜索（正常流程）
        height--;
        node_ptr = page_cur_get_rec(page_cursor);
        // ...
      }
    } else {
      // 找到了，继续下行
      height--;
      node_ptr = page_cur_get_rec(page_cursor);
    }
  }
}
```

---

## 第四部分：测试和验证

### 4.1 基本测试用例

```sql
-- Test 1: 基础插入（触发分裂）
INSERT INTO test_table VALUES (1, 'data1');
INSERT INTO test_table VALUES (2, 'data2');
-- ... 插入到页满

-- Test 2: 查询（验证右链）
SELECT * FROM test_table WHERE id = 500;

-- Test 3: 并发写入（性能对比）
-- 使用sysbench进行并发测试
sysbench oltp_prepare --tables=1 mysql-bench
sysbench oltp_read_write --threads=16 mysql-bench

-- Test 4: 监控锁冲突
SHOW ENGINE INNODB STATUS;
-- 查看 "Lock structs", "Transactions" 部分
```

### 4.2 性能指标

```
测试前（当前B+树）：
├─ 并发度：8 connections
├─ 吞吐量：1000 ops/sec
├─ 锁冲突等待：50%
└─ 平均响应时间：10ms

测试后（示例，非承诺）：
├─ 并发度：32 connections
├─ 吞吐量：上升（幅度取决于分裂频率与热点程度）
├─ 锁冲突等待：下降（目标是显著减少树级修改冲突）
└─ 平均响应时间：下降（尤其在云盘 I/O 放大场景）
```

---

## 第五部分：调试技巧

### 5.1 打印日志定位问题

```cpp
// 在btr0btr.cc分裂函数中添加
fprintf(stderr, "[BLINK] Split page %u into %u and %u, right_link=%u\n",
        old_page_no, left_page_no, right_page_no,
        mach_read_from_4(new_page + FIL_PAGE_RIGHT_LINK));

// 在btr0cur.cc查询函数中添加
if (right_link != FIL_NULL) {
    fprintf(stderr, "[BLINK] Following right link from %u to %u\n",
            page_id.page_no(), right_link);
}
```

### 5.2 GDB调试

```bash
# 启动MySQL server in debug mode
gdb mysql_server

# 设置断点在分裂函数
(gdb) break btr_page_split_and_insert

# 运行
(gdb) run

# 执行会触发分裂的SQL
mysql> INSERT INTO table VALUES (...);

# 检查变量
(gdb) print right_link
(gdb) print new_block->page.id
```

---

## 第六部分：常见问题Q&A

### Q1: 右链指针会不会浪费存储空间？
**A**: 只增加4字节/页（页号大小），相对于16KB页面可忽略（0.025%）

### Q2: 如果分裂后没有及时更新parent怎么办？
**A**: 不影响查询（沿右链继续），但要有background thread定期同步parent

### Q3: 合并(merge)如何处理右链？
**A**: 合并时删除被合并页的右链，在原页的右链指向被合并页原来的右兄弟

### Q4: 范围扫描会变慢吗？
**A**: 不会！反而可能快些：可能遇到已分裂的新页，本地缓存命中率更高

---

## 学习路线时间估计

```
总耗时：约 12-16 小时

├─ 第一部分（概念）：2小时
├─ 第二部分（结构）：1小时
├─ 第三阶段（理解代码）：3小时
├─ 第三阶段（修改代码）：3小时
├─ 第四部分（测试）：2小时
├─ 第五部分（调试）：1小时
└─ 总体理解和文档：2小时
```

---

## 下一步行动

1. **今天**：读完第一、二部分，建立概念框架
2. **明天**：按照第三部分的Step 1-3理解现有代码
3. **后天**：开始小改动（添加日志打印）测试编译
4. **持续**：逐步增加修改，每次编译测试

**不要急！** 一个完整的企业级改造可能需要：
- 初版实现：2-3周
- 性能优化：1-2周
- 大规模测试：2-3周
- 生产部署：1周

祝你成功！
