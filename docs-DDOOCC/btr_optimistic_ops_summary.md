摘要：
本文件总结 `btr_cur_optimistic_insert`、`btr_cur_optimistic_update`、`btr_cur_optimistic_delete_func` 三个函数（来源：`storage/innobase/btr/btr0cur.cc`）。按以下结构说明：

1) 传入参数（作用与必要性）
2) 局部参数/变量初始化的目的
3) 主要逻辑流程（带 ASCII 流程图）
4) 前/后维护工作与必要性（系统级、位图、页内/页间、压缩相关）

-- 说明约定 --

- “cursor” 通常为 `btr_cur_t*`，表示 B-tree 游标和当前 page_cur/record。
- 忽略 `ut_ad`/`DBUG` 等调试语句的解释，强调逻辑行为与副作用。

---

一、`btr_cur_optimistic_insert(flags, cursor, offsets, heap, entry, rec, big_rec, thr, mtr)`

1. 传入参数（作用与必要性）

- `flags` (ulint)
  - 描述锁/undo/创建/保留系统字段等行为（比如 `btr_create_flag`、`btr_no_undo_log_flag` 等）。必需，用于控制是否记录 undo/redo、是否需要锁等。
- `cursor` (btr_cur_t*)
  - 定位到目标 leaf/page 及位置信息（page_cur、slot、index 等）。必需，函数在 cursor 指定位置做插入尝试。
- `offsets` (ulint**)
  - 输出/输入的字段偏移向量；供重构新记录或为嵌入字段计算偏移。必要：插入后 caller 可能需要偏移信息以供后续索引更新或 undo。
- `heap` (mem_heap_t**)
  - 临时内存池（分配记录/offsets/临时结构）。可选但通常需要：减少分配开销并方便回收。
- `entry` (dtuple_t*)
  - 要插入的记录字段向量（未序列化的 tuple）。必需：记录的原始数据来源。
- `rec` (rec_t**)
  - 输出：指向插入后的 rec（page 上的地址）。caller 需要以便建立外层结构（例如 big_rec）。
- `big_rec` (big_rec_t**)
  - 输出/传入：当记录包含外部 (LOB) 字段或大字段时，记录其外部存放信息。必要时用于大字段处理。
- `thr` (que_thr_t*)
  - 当前查询线程上下文（可为 NULL）：用于锁/等待/统计。必要性取决于 flags（无锁或无 undo 时可为 NULL）。
- `mtr` (mtr_t*)
  - mini-transaction，用于在页上获得 latch 并保证原子性（延迟提交或回滚）。必需，用于页级并发控制及日志一致性。

2. 局部参数/变量初始化的作用（常见局部变量）

- `page`, `block`：从 cursor 得到当前页数据，page 指针用于检查空闲空间、记录计数等。
- `rec_size` / `entry_size`：计算新记录的物理占用（含系统字段与压缩后的长度），用于判断页空间是否足够。
- `has_extern` / `needs_big_rec`：判断是否有外部字段，决定是否需要预先处理 LOB（分配外部页、big_rec 构造等）。
- `offsets_local` / `heap_local`：函数本地分配的 offsets/heap，当调用者未提供时创建并在返回时传出或释放。
- `page_zip` / `is_comp`：判断页是否为压缩页并准备相应的页面压缩描述符。
- `reorganize_limit` / `reorg_bytes`：估计通过页内重排可释放的空间，用于乐观插入的重组阈值决策。

3. 主要逻辑流程及伪代码
   3.1 流程图：

```
┌─────────────────────────────────────────────────────────────────────┐
│  btr_cur_optimistic_insert(flags, cursor, entry, rec, big_rec, ...)  │
└─────────────────────────────────────────────────────────────────────┘
                            │
                            ▼
         ┌──────────────────────────────────────────┐
         │ 1. 计算 rec_size（entry → physical size） │
         │    - 系统字段 (trx_id, roll_ptr)          │
         │    - null 位图、变长字段头                  │
         │    - 外部字段标志位（has_extern）           │
         └──────────────────────────────────────────┘
                            │
                            ▼
         ┌──────────────────────────────────────────┐
         │ 2. 检查页状态                              │
         │    - page = cursor->page_cur.block→frame │
         │    - 页是否压缩 (page_is_comp)             │
         │    - 页是否满足 IBUF 位图约束（压缩页）       │
         └──────────────────────────────────────────┘
                            │
                            ▼
              ┌──────────────────────────────┐
              │ 有外部字段或 rec_size 过大？    │
              └───────────┬──────────────────┘
                   是 /   \  否
                    /      \
                   ▼        ▼
        ┌─────────────────┐  │
        │ 处理 big_rec     │  │
        │ 分配外部存储      │  │
        └────────┬────────┘  │
                 └─────┬─────┘
                       ▼
         ┌──────────────────────────────────────────┐
         │ 3. 尝试页内直接插入                      │
         │    - page_cur_insert_rec() 或类似        │
         │    - 检查页剩余空间 >= rec_size          │
         └──────────────────────────────────────────┘
                       │
                ┌──────┴──────┐
             成功│             │失败
                ▼             ▼
        ┌───────────────┐  ┌─────────────────────────┐
        │ 写 undo/redo  │  │ 页空间不足              │
        │ 设 *rec       │  │ 尝试页重组？             │
        │ 更新 page_hdr │  └──────────┬──────────────┘
        │ 返回 SUCCESS  │             │
        └───────────────┘    ┌────────┴─────────┐
                            是│                  │否
                            ▼                  ▼
                    ┌──────────────┐  ┌───────────────────┐
                    │ 执行重组      │  │ 返回错误           │
                    │ 释放空洞      │  │ DB_OVERFLOW/FAIL  │
                    │ 重试插入      │  │ 调用方触发悲观路径  │
                    └────────┬─────┘  └───────────────────┘
                             │
                             └──────────┐
                                        ▼
                            ┌───────────────────────┐
                            │ 返回 SUCCESS 或 再失败  │
                            │ → 悲观处理             │
                            └───────────────────────┘
```

3.2 伪代码：

```c
dberr_t btr_cur_optimistic_insert(...) {
    page_t* page = buf_block_get_frame(cursor->block);
    ulint   rec_size = 0;
    rec_t*  rec = nullptr;
    dberr_t err = DB_SUCCESS;

    // Step 1: 计算新记录大小
    rec_size = dtuple_get_data_size(entry);
    if (page_is_comp(page)) {
        rec_size += page_new_record_header_size();  // 紧凑页额外头
    } else {
        rec_size += page_record_header_size();
    }

    // Step 2: 检查是否有外部字段
    bool has_extern = entry_has_extern_fields(entry);
    if (has_extern) {
        // 预先分配/检查外部存储
        if (LOB_alloc_failed(...)) {
            return DB_OUT_OF_FILE_SPACE;
        }
    }

    // Step 3: 尝试直接插入
    page_cur_t page_cur = cursor->page_cur;
    rec = page_cur_insert_rec(
        page_cur,
        entry,
        offsets,
        heap
    );

    if (rec != nullptr) {
        // 成功：写 undo/redo
        if (!(flags & btr_no_undo_log_flag)) {
            trx_undo_report_row_operation(
                flags, undo_node, thr, cursor, entry, nullptr
            );
        }
        mtr_memo_push(mtr, page, MTR_MEMO_PAGE_X_FIX);  // 标记脏
        *rec_out = rec;
        return DB_SUCCESS;
    }

    // Step 4: 若失败，尝试页重组
    ulint free_before = page_get_max_insert_size(page);
    if (free_before < rec_size + BTR_CUR_PAGE_REORGANIZE_LIMIT) {
        // 尝试重组
        btr_page_reorganize(cursor, mtr);
        rec = page_cur_insert_rec(page_cur, entry, offsets, heap);
        if (rec != nullptr) {
            // 重组后成功，返回成功
            return DB_SUCCESS;
        }
    }

    // Step 5: 都失败了，返回错误
    return DB_FAIL;  // 调用方会改走 btr_cur_pessimistic_insert
}
```

4. 前/后维护工作与必要性（系统级、位图、页内/页间、压缩）

- Undo/Redo 日志
  - 插入成功后需要写 undo 以便回滚（如果 flags 表示需要 undo），并写相应的 redo（事务提交时用于恢复）。undo 记录中需包含旧 `trx_id`/`roll_ptr` 的维护。
- 锁/意向锁
  - 根据 flags 决定是否申请锁（行锁/意向锁），保证并发安全；悲观路径会申请更严的锁并预留页面以避免空间分配失败导致回滚复杂度。
- IBUF / Insert Buffer（缓冲插入）位图（仅二级索引压缩页相关）
  - 对于压缩二级索引叶页，需要更新 `IBUF_BITMAP_FREE` 以反映可用压缩槽；此更新必须在同一 mini-transaction 中或在 commit 之前通过 `ibuf_reset_free_bits()` 进行，以避免压缩页的不一致。
- 页内维护（within-page）
  - 更新 slot 指针、记录偏移向量、紧缩空洞；更新 page header（例如 `n_recs`、空闲碎片信息）并将页标记为脏（mtr 标记）。
  - 对压缩页需维护 `page_zip_des_t` 的元信息（压缩页目录、slots）并处理 `DB_ZIP_OVERFLOW` 或 `DB_UNDERFLOW` 错误。
- 页间维护（between-pages / split/merge）
  - 若插入触发分裂，需分配新页、更新父节点、可能继承/修正 supremum 的锁（见 pess_upd_restore_supremum 场景）。
  - 分裂时要保证锁顺序和 undo 可回滚，可能需要提前在 tablespace 中预留页面（尤其是悲观路径）。
- LOB / 外部字段
  - 若记录包含外部字段（长字段），必须先在 LOB 存储区分配/更新外部页，并在主记录内放置指针（`big_rec` 管理）。外部字段处理可能导致乐观路径失败（需改走悲观）。
- 索引层次维护
  - 若插入发生在聚簇索引，则还需调整二级索引的条目（可能触发二级索引 rebuild 或更新），并注意不可在未 commit mtr 前访问尚未稳定的索引条目。

4. 前/后维护工作详细说明

**4.1 维护工作总结表**

| 维护工作项                      | Insert | Update | Delete | 必要性说明                                    |
| ------------------------------- | ------ | ------ | ------ | --------------------------------------------- |
| **Undo 日志**             | ✓     | ✓     | ✓     | 事务回滚/崩溃恢复的关键；记录原值/操作类型    |
| **Redo 日志**             | ✓     | ✓     | ✓     | 崩溃后恢复数据一致性；由 mtr 自动处理         |
| **行锁 (Lock)**           | ✓     | ✓     | ✓     | 并发事务隔离；防止脏读/幻读                   |
| **IBUF 位图** (二级压缩)  | ✓     | ✓     | ✓     | 二级索引压缩页的可用空间标记；必须同 mtr      |
| **页内 slot/偏移**        | ✓     | ✓     | ✓     | 记录寻址的基础；插入/删除后需更新             |
| **页 header**             | ✓     | ✓     | ✓     | n_recs / n_dels / max_trx_id 等               |
| **压缩页目录** (page_zip) | ✓     | ✓     | ✓     | 紧凑页的子槽位及再压缩处理                    |
| **页分裂与链接**          | ▲     | ▲     | ▲     | 仅在页空间不足时触发；涉及父节点更新          |
| **外部字段管理** (LOB)    | ✓     | ✓     | ✓     | 大字段的单独分配与回收                        |
| **二级索引条目**          | ✓     | ▲     | ▲     | Insert 后需插入二级条目；Update/Delete 需同步 |

说明：✓ 必须处理，▲ 可能需要（取决于数据大小和页状态）

**4.2 Undo 日志维护伪代码**

```c
if (!(flags & btr_no_undo_log_flag)) {
    // Insert：记录新记录全部字段
    undo_entry = undo_build_insert_entry(entry, cursor->index);
    trx_undo_insert_undo_rec(undo_entry, mtr);

    // Update：记录被修改字段的旧值
    undo_entry = undo_build_update_entry(
        old_rec,
        update,    // 哪些列被改
        offsets
    );
    trx_undo_update_undo_rec(undo_entry, mtr);

    // Delete：记录待删除记录的全部内容（以便恢复）
    undo_entry = undo_build_delete_entry(rec, offsets);
    trx_undo_delete_undo_rec(undo_entry, mtr);
}
```

**4.3 IBUF / 压缩页位图维护**

- 对于二级索引的压缩页，每次操作（Insert/Delete）都会改变页可用空间
- 必须更新 `IBUF_BITMAP_FREE` 来反映新的可用槽位数
- **关键约束**：IBUF 位图更新必须在同一 mini-transaction 内完成，或在 commit 前通过 `ibuf_reset_free_bits()` 进行

```c
/* 关键代码模式 */
mtr_start(&mtr);
  // ... 执行 insert/update/delete ...
  // IBUF 位图更新在 mtr 内
  if (is_secondary_index && page_is_comp(page)) {
      ibuf_update_free_bits_low(page, free_space_after, mtr);
  }
mtr_commit(&mtr);  // 原子性保证
```

**4.4 页内维护（slot/记录链）**

```c
/* 页内维护示例 */
page_cur_t* page_cur = &cursor->page_cur;

// Insert 时：分配新 slot，更新相邻记录的链
page_rec_insert(page_cur, rec, offsets);
page_header_set_field(page, PAGE_N_RECS, page_get_n_recs(page) + 1);

// Delete 时：标记或物理删除
rec_set_deleted_flag(rec, is_comp, true);
page_header_set_field(page, PAGE_N_DELS, page_get_n_dels(page) + 1);
```

**4.5 压缩页（page_zip）特殊处理**

- 每次修改前需检查压缩页是否会溢出（`DB_ZIP_OVERFLOW`）或欠溢（`DB_UNDERFLOW`）
- 若超过压缩能力，需要转移到非压缩或直接返回失败让上层走悲观路径

```c
/* 压缩页检查示例 */
if (page_is_comp(page)) {
    page_zip_des_t *page_zip = page_get_zip_des(page);

    // 检查新数据是否能压缩
    if (compressed_size_after > page_zip->size) {
        return DB_ZIP_OVERFLOW;  // 不能乐观处理
    }

    // 执行压缩重排
    page_zip_reorganize(page_zip, page, index, offsets, mtr);
}
```

**4.6 外部字段（LOB）处理**

```c
/* LOB 管理伪代码 */
if (entry_has_extern_fields(entry)) {
    big_rec_t *big_rec = big_rec_create(entry);

    // 为外部字段分配 LOB 页
    for (ulint i = 0; i < big_rec->n_fields; i++) {
        ulint lob_page = lob_allocate_page(heap);
        lob_write_field(lob_page, big_rec->fields[i]);
        big_rec->fields[i]->ref = (lob_page, offset);  // 存储指针
    }

    // 在主记录中存放 LOB 指针
    rec_set_field_extern_flag(rec, field_no, true);
    rec_set_field_data(rec, field_no, big_rec->fields[i]->ref);
}
```

**4.7 二级索引同步**

```c
/* 二级索引同步示例 */
// Insert 后更新二级索引
dict_index_t *index = cursor->index;
dict_index_t *sec_index = dict_table_get_next_index(index);
while (sec_index != nullptr) {
    if (!dict_index_is_clust(sec_index)) {
        row_ins_sec_index_entry(
            sec_index,
            sec_entry,
            thr,
            mtr
        );
    }
    sec_index = dict_table_get_next_index(sec_index);
}
```

总结：维护工作贯穿整个操作过程，不仅保证单条记录的正确性，更维系整个 B-tree、页级压缩、事务隔离的一致性。乐观路径通过快速决策和最小化锁持有时间提升性能，而每一项维护都在确保崩溃后恢复和并发控制的可靠性。

---

二、`btr_cur_optimistic_update(flags, cursor, offsets, heap, update, cmpl_info, thr, trx_id, mtr)`

1. 传入参数（作用与必要性）

- `flags`：同 insert，控制锁/undo/系统字段保留行为等。
- `cursor`：定位到要更新的记录；必需。
- `offsets` (ulint**): 当前 record 的字段偏移信息；必需以便按字段定位并应用更新向量。
- `heap` (mem_heap_t**): 临时内存池，用于解析 `upd_t`、构造新记录或 offsets；通常需要。
- `update` (upd_t const*): 更新向量（要修改的列和新值或表达式）。必需。
- `cmpl_info` (ulint): 用于次级索引更新的编译信息/标志；决定是否也需更新二级索引条目。
- `thr`、`trx_id`、`mtr`：线程/事务 ID 与 mini-transaction，用于锁/undo/logging，与 insert 相同的必要性。

2. 局部参数/变量初始化目的（常见）

- `rec` / `page` / `page_zip`: 当前 record 与 page 指针，用于读取原始值和写回。
- `new_size`, `old_size`, `offsets_new`：计算更新后记录是否大小不变，若 size 相同，能走 in-place 快路径。
- `has_extern` / `needs_big_rec`：若更新涉及 LOB 或外部字段，可能需要迁移到外部并创建 big_rec。
- `update_in_place_ok`（布尔）：是否允许就地更新（通常在 new_size == old_size 且压缩/LOB 约束允许时）。
- `undo_ptr` / `roll_ptr`：用于 undo 日志的指针记录，必要于回滚。

3. 主要逻辑流程及伪代码
   3.1 流程图：

```
┌─────────────────────────────────────────────────────────────────────┐
│  btr_cur_optimistic_update(flags, cursor, offsets, update, ...)      │
└─────────────────────────────────────────────────────────────────────┘
                            │
                            ▼
         ┌──────────────────────────────────────────┐
         │ 1. 解析 update 向量                      │
         │    - 遍历 update→fields[]               │
         │    - 计算 old_size（current record）     │
         │    - 计算 new_size（after apply）        │
         └──────────────────────────────────────────┘
                            │
                            ▼
         ┌──────────────────────────────────────────┐
         │ 2. 检查是否能就地更新（in-place）        │
         │    条件：                                │
         │    - new_size == old_size               │
         │    - 无外部字段或外部字段不变             │
         │    - 压缩页不会 overflow                │
         └──────────────────────────────────────────┘
                            │
                ┌───────────┴───────────┐
             条件满足│                   │条件不满足
                    ▼                   ▼
        ┌─────────────────────┐  ┌─────────────────────┐
        │ A. 就地更新快路径   │  │ B. 需要重新安排      │
        │                    │  │                    │
        │ btr_cur_update_    │  │ 构造新 record       │
        │ in_place():        │  │ 计算压缩/LOB 影响   │
        │ - 设置字段值        │  │                    │
        │ - 写 undo/redo     │  │ 尝试在页内插入      │
        │ - 标记页脏         │  │ 新 record          │
        │ - 返回 SUCCESS     │  │                    │
        └─────────────────────┘  └──────────┬────────┘
                                            │
                                    ┌───────┴──────────┐
                                 成功│                  │失败
                                    ▼                  ▼
                        ┌────────────────────┐  ┌─────────────────┐
                        │ 删除旧 record      │  │ 页空间不足      │
                        │ 写完整 undo        │  │                │
                        │ 更新二级索引信息   │  │ 返回 DB_FAIL   │
                        │ 返回 SUCCESS       │  │ 走悲观更新      │
                        └────────────────────┘  └─────────────────┘
```

3.2 伪代码：

```c
dberr_t btr_cur_optimistic_update(
    ulint flags,
    btr_cur_t *cursor,
    ulint *offsets,
    mem_heap_t **heap,
    const upd_t *update,
    ulint cmpl_info,
    que_thr_t *thr,
    trx_id_t trx_id,
    mtr_t *mtr
) {
    rec_t* rec = btr_cur_get_rec(cursor);
    page_t* page = page_align(rec);
    ulint old_size = rec_size_calc(offsets);

    // Step 1: 计算更新后的大小
    ulint new_size = old_size;
    bool size_changed = false;

    upd_field_t* field = update->fields;
    while (field) {
        if (field->needs_allocation) {
            new_size += field->new_size - field->old_size;
            size_changed = true;
        }
        field = field->next;
    }

    // Step 2: 检查是否能就地更新
    if (new_size == old_size && !has_extern_in_update(update)) {
        // 快路径：就地更新，无需移动记录
        dberr_t err = btr_cur_update_in_place(
            flags,
            cursor,
            offsets,
            update,
            cmpl_info,
            thr,
            trx_id,
            mtr
        );

        if (err == DB_SUCCESS) {
            // 成功：写入新值到各字段，undo/redo 已由 update_in_place 处理
            return DB_SUCCESS;
        }
        if (err == DB_ZIP_OVERFLOW) {
            // 压缩页溢出，退出
            return err;
        }
    }

    // Step 3: 需要移动 record 或更改大小
    // 构造新 record
    dtuple_t *new_entry = row_upd_build_new_entry(
        rec,
        index,
        update,
        heap
    );

    // 尝试在页内插入新 record
    page_cur_t page_cur = cursor->page_cur;
    rec_t *new_rec = page_cur_insert_rec(
        page_cur,
        new_entry,
        offsets,
        heap
    );

    if (new_rec != nullptr) {
        // 插入成功，删除旧 record，写完整 undo
        page_cur_delete_rec(page_cur, index, offsets, mtr);

        // 更新游标与二级索引追踪
        btr_cur_update_set_rec_fields(cursor, new_rec, offsets);

        return DB_SUCCESS;
    }

    // Step 4: 插入失败，返回失败让上层走悲观
    return DB_FAIL;  // 调用方会改走 btr_cur_pessimistic_update
}
```

关键变量监控（gdb/breakpoint 时）：

```
print old_size           # 原记录大小
print new_size           # 更新后预计大小
print size_changed       # 大小是否改变
print page_get_free_space(page)  # 页剩余空间
print has_extern         # 是否有 LOB 字段
```

- 就地更新：
  - 写入 undo/redo（记录列 Old/New），必要以便事务回滚/崩溃恢复。
  - 若更新影响索引列（primary key 不允许直接改变聚簇主键位置），可能需要 delete+insert（移动行），并更新相应二级索引条目。
  - 在压缩页上，要检查是否会引起 `DB_ZIP_OVERFLOW`；若发生则需要转换到非就地路径或悲观路径。
- delete+insert 路径：
  - 先申请足够锁并写 undo（保证原子性），然后插入新行并删除旧行（或标记为删除），并处理页分裂与父节点更新。
- LOB 处理：
  - 若字段转为/来自外部存储，需要申请/释放外部页并在 undo 中记录这些操作以便回滚。
- 锁迁移与 suprema 修复：
  - 分裂时，可能产生新的 supremum，需要修复其继承锁（见 `btr_cur_pess_upd_restore_supremum`）。

---

三、`btr_cur_optimistic_delete_func(cursor, offsets, heap, rec, has_extern, mtr)`（函数名在源码中可能不同，但逻辑为乐观删除）

1. 传入参数（作用与必要性）

- `cursor`：定位要删除的记录。必需。
- `offsets` / `heap`：用于定位和解析 record 内字段与系统字段；通常需要以便在删除后写 undo 信息。
- `rec`：指向待删除的 rec（或者用于输出已删除 rec 指针）。
- `has_extern`（布尔或通过偏移判断）：是否存在外部字段需要在删除时处理 LOB 的释放。
- `mtr`：mini-transaction，用于页级并发控制以及在删除/标记删除期间保持原子性。

2. 局部变量初始化目的

- `page` / `block`：获得页上下文以便检查记录邻居/页内空间/页类型。
- `can_delete_in_place`：检查是否能在当前页直接删除（比如页不受压缩约束或没有外部字段）。
- `undo_ptr` / `trx_id`：为 undo 日志准备信息，记录删除者事务 id 与回滚指针。

3. 主要逻辑流程及伪代码
   3.1 流程图：

```
┌──────────────────────────────────────────────────────────────────────┐
│  btr_cur_optimistic_delete_func(cursor, offsets, rec, has_extern, ...) │
└──────────────────────────────────────────────────────────────────────┘
                            │
                            ▼
         ┌──────────────────────────────────────┐
         │ 1. 检查记录属性                      │
         │    - 获取 record 的系统字段          │
         │    - 是否有外部字段 (LOB)           │
         │    - 是否为压缩页                    │
         └──────────────────────────────────────┘
                            │
                            ▼
         ┌──────────────────────────────────────┐
         │ 2. 决策：能否直接删除 / 需要悲观     │
         │    - 有外部字段？                    │
         │      → 需要释放 LOB，处理复杂        │
         │    - 压缩页且位图不一致？            │
         │      → 需要修复约束                  │
         │    - 页空间其他限制？                │
         └──────────────────────────────────────┘
                            │
                ┌───────────┴───────────┐
             能乐观删除│                 │需要悲观处理
                    ▼                   ▼
        ┌──────────────────────┐  ┌─────────────────┐
        │ A. 标记为删除         │  │ B. 悲观删除     │
        │    (mark delete)     │  │                │
        │                     │  │ 预取兄弟页      │
        │ 写 delete mark      │  │ 申请更强的锁    │
        │ 写 undo/redo        │  │ 可能触发 merge  │
        │ 更新 page header    │  │ 处理外部字段    │
        │ 标记页脏             │  │                │
        │ 返回 SUCCESS        │  │ 返回 SUCCESS   │
        └──────────────────────┘  └─────────────────┘
                   │                      │
                   └──────────┬───────────┘
                              ▼
                    ┌──────────────────────┐
                    │ Purge 线程稍后       │
                    │ 物理回收页空间       │
                    │ 释放外部 LOB         │
                    └──────────────────────┘
```

3.2 伪代码：

```c
dberr_t btr_cur_optimistic_delete_func(
    btr_cur_t *cursor,
    const ulint *offsets,
    mem_heap_t *heap,
    rec_t *rec,
    bool has_extern,
    mtr_t *mtr
) {
    page_t* page = page_align(rec);
    bool is_comp = page_is_comp(page);
    bool can_delete_in_place = true;

    // Step 1: 检查外部字段
    if (has_extern) {
        // 外部字段（LOB）需要在 purge 时释放
        // 但标记删除仍可快速进行
        // 在某些情况下（例如约束冲突），可能还是需要悲观处理
        if (lob_cleanup_would_conflict(...)) {
            can_delete_in_place = false;
        }
    }

    // Step 2: 检查压缩页约束
    if (is_comp) {
        page_zip_des_t* page_zip = page_get_zip_des(page);

        // 压缩页需要检查 IBUF 位图是否有冲突
        if (!page_zip_is_valid_for_delete(...)) {
            can_delete_in_place = false;
        }
    }

    // Step 3: 尝试标记删除或直接删除
    if (can_delete_in_place) {
        // 快路径：标记为删除
        rec_set_deleted_flag(rec, is_comp, true);

        // 写 undo 记录（用于回滚此删除）
        if (!(flags & btr_no_undo_log_flag)) {
            trx_undo_report_row_operation(
                /* delete type */,
                undo_node,
                thr,
                cursor,
                rec,
                offsets
            );
        }

        // 更新页头（n_dels, 页脏标记）
        page_header_set_field(
            page,
            PAGE_N_DELS,
            page_header_get_field(page, PAGE_N_DELS) + 1
        );

        mtr_memo_push(mtr, page, MTR_MEMO_PAGE_X_FIX);  // 标记脏

        return DB_SUCCESS;
    }

    // Step 4: 需要悲观删除（可能涉及页合并）
    // 返回错误，上层改走悲观路径
    return DB_FAIL;  // 调用方会改走 btr_cur_pessimistic_delete
}
```

关键变量监控（调试时）：

```
print has_extern                                # 是否有 LOB 字段
print page_is_comp(page)                        # 是否压缩页
print rec_get_deleted_flag(rec, page_is_comp(page))  # 删除标记
print page_get_n_dels(page)                     # 页上已删除记录数
print page_get_max_trx_id(page)                 # 最大事务 ID（用于 MVCC）
```

4. 前/后维护工作与必要性（系统级、位图、页内/页间、压缩）

**4.8 删除标记与实际回收**

- InnoDB 可能使用 delete-mark（先标记为删除），后由 purge 线程或后续事务物理回收并释放空间
- 这允许 MVCC 读到旧版本直到 rollback/commit 期间，实现多版本并发控制

**4.9 外部字段回收**

- 删除要同时回收外部 LOB 页／块，且这些动作需记录在 undo，以便回滚时重建 LOB 内容
- Purge 操作在清理删除标记时同时执行 LOB 页面的物理释放

**4.10 页合并/兄弟页维护**

- 删除后如果页空闲很多，可能触发页合并/重组
- 这会牵涉父节点更新、锁的迁移与 undo/redo 的额外复杂性

---

五、补充要点与调试建议

**何时乐观失败？**
典型原因：

- 记录大小变更（需要移动记录位置）
- 压缩页上的 zip overflow/underflow
- 存在外部 LOB 操作
- 页空间不足且重组无法释放足够空间
- 存在并发冲突导致无法取得必要锁

**乐观 vs 悲观的核心区别**

- **乐观**：只在目标 leaf 上短暂拿 latch，假设不会触发分裂或外部分配；若失败则回滚并走更慢但安全的悲观路径
- **悲观**：在开始前预留更多资源（预分配页、持有更多锁），能保证操作顺利完成但开销更大

**调试时关注的关键变量**

```
(gdb) print rec_size
(gdb) print new_size
(gdb) print has_extern
(gdb) print page_is_comp(page)
(gdb) print page_get_free_space(page)
(gdb) print IBUF_BITMAP_FREE
(gdb) print mtr->state
(gdb) info locals  # 查看所有局部变量
```

**聚簇索引 vs 二级索引差异**

- 聚簇索引的 insert/update 会影响实际数据行的物理位置（移动会连带更新二级索引）
- 二级索引通常只含索引字段，且压缩页/IBUF 对二级索引影响更明显
- 二级索引的乐观操作更容易成功（无需处理物理移动），但需要同步主索引的变化

**页压缩与大字段处理建议**

- 若页频繁出现 ZIP_OVERFLOW，考虑调整表的压缩级别或使用非压缩页
- 若 LOB 字段较多，考虑分离表结构或使用专用 LOB 存储策略

---

六、快速参考：函数返回值含义

| 返回值                   | 含义                   | 处理方式                      |
| ------------------------ | ---------------------- | ----------------------------- |
| `DB_SUCCESS`           | 操作成功               | 继续执行，事务提交            |
| `DB_FAIL`              | 乐观路径失败，空间不足 | 改走悲观路径                  |
| `DB_ZIP_OVERFLOW`      | 压缩页数据溢出         | 返回上层，可能转非压缩或等待  |
| `DB_UNDERFLOW`         | 压缩页数据过少         | 触发页合并或压缩策略调整      |
| `DB_LOCK_WAIT`         | 等待锁释放             | 事务等待或超时回滚            |
| `DB_DUPLICATE_KEY`     | 唯一键冲突             | 根据 flags 决定是否忽略或报错 |
| `DB_OUT_OF_FILE_SPACE` | 表空间不足             | 扩展表空间或报错              |

---

七、总结

本文档以面向调试与维护者的角度总结了 InnoDB B-tree 三类乐观操作的输入参数、局部变量初始化、主要逻辑流程、以及前后维护工作。

**关键要点**：

1. 乐观操作通过快速决策和最小化锁持有时间提升性能
2. 每一项维护（Undo/Redo/IBUF/页内/页间/LOB/压缩）都在确保崩溃恢复和并发控制的可靠性
3. 失败路径（返回 DB_FAIL 等）会自动回退到悲观操作，保证最终成功
4. 正确理解参数、变量和流程对于调试和优化至关重要
