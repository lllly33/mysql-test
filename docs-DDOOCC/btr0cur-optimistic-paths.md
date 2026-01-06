# InnoDB B+Tree：乐观写路径源码解读（MySQL 8.0.34）

解读 InnoDB 在 `btr0cur.cc` 中三条**乐观修改**路径：

- `btr_cur_optimistic_insert()`
- `btr_cur_optimistic_update()`
- `btr_cur_optimistic_delete_func()`（注意：它返回 `bool`，不是 `dberr_t`）

范围约定：

- 只分析这三个函数自身的控制流与关键副作用；**不深入**其子调用（例如 `btr_cur_ins_lock_and_undo()`、`btr_cur_can_delete_without_compress()` 等仅解释“在这里被用来做什么”。）
- 只讨论“optimistic 成功/失败”的判定逻辑、返回码语义、以及它们如何把控制权交给悲观路径。

源码位置：

- `btr_cur_optimistic_insert()`：[storage/innobase/btr/btr0cur.cc](storage/innobase/btr/btr0cur.cc#L2659)
- `btr_cur_optimistic_update()`：[storage/innobase/btr/btr0cur.cc](storage/innobase/btr/btr0cur.cc#L3491)
- `btr_cur_optimistic_delete_func()`：[storage/innobase/btr/btr0cur.cc](storage/innobase/btr/btr0cur.cc#L4530)

---

## 1. `btr_cur_optimistic_insert()`：页内尝试插入（不做 split）

入口：`dberr_t btr_cur_optimistic_insert(...)`，[btr0cur.cc#L2659](storage/innobase/btr/btr0cur.cc#L2659)

### 1.1 这条路径“乐观”的含义

- 只在**当前 cursor 所在页**尝试插入，最多做“页内重组 (reorganize)”来清理垃圾；
- 一旦判断“可能需要 split / 可能压缩失败 / 空间不值得折腾”，直接返回 `DB_FAIL` 让上层切到 `btr_cur_pessimistic_insert()`（后者才会 split/raise root/更新父层）。

### 1.2 关键前置条件（调用者需要满足）

在函数开头通过 `ut_ad()` 约束：

- `mtr` 必须持有 cursor 所在 block 的 **X latch**（`MTR_MEMO_PAGE_X_FIX`）。
- Online DDL 场景下：非聚簇索引只能在 `flags & BTR_CREATE_FLAG` 的条件下走这条路径。
- `entry` 必须是 typed tuple（`dtuple_check_typed(entry)`）。

额外重要约束（写在函数注释里）：

- 如果在“**二级索引的 leaf** + **压缩表空间**”上返回 `DB_SUCCESS`，调用者必须 `mtr_commit(mtr)` 之后再去 latch 其他页（避免锁顺序/位图更新相关的潜在死锁与一致性问题）。

### 1.3 主流程分解（按源码顺序）

#### A) 先把“记录是否太大”处理掉（big record / 压缩页约束）

核心意图：不要在后面的空间判断里混入“LOB 外置”与“压缩页极限”问题。

- 先用 `rec_get_converted_size(index, entry)` 估算 record 落盘大小。
- 如果 `page_zip_rec_needs_ext(...)` 判定需要外置字段，则调用 `dtuple_convert_big_rec()` 构建 `big_rec_vec`，并把 `entry` 改写成“含外置指针”的形式。
  - 若转换失败：直接 `return DB_TOO_BIG_RECORD`。
- 对压缩页额外检查 `page_zip_is_too_big(index, entry)`：如果压缩格式根本容不下该记录，回滚 `big_rec_vec` 改动并 `return DB_TOO_BIG_RECORD`。

#### B) 对压缩页做“padding 风险”快速失败：避免压缩失败带来的抖动

这段是非常典型的“乐观提前失败”：

- 条件：`leaf && page_size.is_compressed()` 且 `page_get_data_size(page) + rec_size >= dict_index_zip_pad_optimal_page_size(index)`
- 处理：跳到 `fail:`，令 `err = DB_FAIL`，并且对 leaf 预取左右兄弟页（为即将进入悲观路径做 IO 预热）。

含义：即使“理论上还塞得下”，但根据 padding 策略判断“塞进去后页会过满，压缩很可能失败”，因此不在乐观路径里冒险。

#### C) 计算“重组后可插入空间”，决定要不要尝试页内重组

关键量：

- `max_size = page_get_max_insert_size_after_reorganize(page, 1)`

逻辑大意：

- 如果页有垃圾（`page_has_garbage(page)`），只有在“就算重组后也不够/或者重组不划算”时才 `goto fail`。
- 如果页没垃圾，`max_size < rec_size` 直接 `goto fail`。

注意：这里的 `fail` 语义不是“错误”，而是“乐观失败，转悲观”。

#### D) 聚簇索引 leaf 的“预留空间”策略：为后续 update 留余量

这段是你在云盘高延迟/SMO 场景里经常会观察到的一个“主动转悲观”的原因：

- 条件大意：
  - leaf + 非压缩页 + 聚簇索引
  - 页上已有至少 2 条记录
  - `dict_index_get_space_reserve() + rec_size > max_size`
  - 并且根据 `btr_page_get_split_rec_to_right/left()` 看起来“分裂是合理的”
- 处理：直接 `goto fail`

含义：即使现在能插入成功，也可能让页变得过满，后续更新记录（尤其变长列变大）更容易触发页分裂；因此提前交给悲观路径做结构调整。

#### E) 真正执行插入（以及在插入前做锁/undo）

- 如果是 intrinsic table 的特例：走 `page_cur_tuple_direct_insert()`（这块属于 intrinsic 的特殊并发/日志策略）。
- 否则：
  1) 先调用 `btr_cur_ins_lock_and_undo(...)` 处理“需要的话就加锁 + 需要的话就写 undo”。
     - 这里可能返回 `DB_WAIT_LOCK` 等，需要上层处理 lock wait。
  2) 然后 `page_cur_tuple_insert(...)` 真正插入。

插入失败的处理分两类：

- **压缩页**：插入失败后会考虑 `ibuf_reset_free_bits(block)`（二级 leaf）并 `goto fail`。
- **非压缩页**：如果不是 intrinsic，会尝试 `btr_page_reorganize()` 后再插一次；如果重组后依然插不进去，会 `ib::fatal(...)`（认为这是“理论上不该发生”的一致性问题）。

#### F) 成功后必须做的“副作用维护”

1) AHI（Adaptive Hash Index）维护

- 若 `!index->disable_ahi`，会按“是否重组、cursor 是否来自 HASH”等条件调用 `btr_search_update_hash_*_on_insert()`。

2) 行锁继承（insert lock inheritance）

- 在满足 `!(flags & BTR_NO_LOCKING_FLAG) && inherit` 时调用 `lock_update_insert(block, *rec)`。

3) Change Buffer / IBUF 位图（只对二级索引 leaf 且非 temporary）

- 压缩页：`ibuf_update_free_bits_zip(block, mtr)`（同一 mtr 内更新）
- 非压缩页：`ibuf_update_free_bits_if_full(...)`（可能在独立 mtr 中减少位图 free bits）

### 1.4 返回码与“转悲观”的信号

- `DB_SUCCESS`：乐观插入完成；若 `*big_rec != nullptr`，调用者还需按 big-rec 协议把外置字段写到 LOB 页。
- `DB_FAIL`：**核心的“乐观失败”**信号——表示这里不做 split/raise-root，让上层走 `btr_cur_pessimistic_insert()`。
- `DB_TOO_BIG_RECORD`：记录在页格式/压缩限制下无法容纳（即使外置也不行）。
- `DB_WAIT_LOCK` / 其他错误码：来自锁/undo 子流程（例如 `btr_cur_ins_lock_and_undo()`）。

---

## 2. `btr_cur_optimistic_update()`：leaf 上更新（能就地就就地，否则 delete+insert 同页）

入口：`dberr_t btr_cur_optimistic_update(...)`，[btr0cur.cc#L3491](storage/innobase/btr/btr0cur.cc#L3491)

### 2.1 前置条件与范围

这条函数**只用于 leaf page update**（源码中直接 `ut_ad(page_is_leaf(page))`）。

同时要求：

- `mtr` 持有 leaf 页的 X latch（`MTR_MEMO_PAGE_X_FIX`）。
- 不能是 insert buffer tree（`ut_ad(!dict_index_is_ibuf(index))`）。

### 2.2 “最常见快路径”：字段大小不变且不涉及外置字段

函数开头有一个很强的分流：

- 条件：`!row_upd_changes_field_size_or_external(index, *offsets, update)`
- 行为：直接调用 `btr_cur_update_in_place(...)` 并返回其返回码。

这条快路径的含义：

- 不需要“删旧插新”
- 不需要重新计算页空间容纳能力
- 典型场景：定长列更新、变长列等长替换、以及不会触发 extern 的更新。

### 2.3 任何 extern（LOB 外置）都直接判为“乐观不做”

两处检查都会把你送去悲观 update：

- 旧记录本身有 extern：`rec_offs_any_extern(*offsets)`
- 更新向量中出现 extern：遍历 `update` 的每个 field，检查 `dfield_is_ext(&...->new_val)`

命中后：

- 预取兄弟页 `btr_cur_prefetch_siblings(block)`
- `return DB_OVERFLOW`

注意：这里用 `DB_OVERFLOW` 作为“需要悲观处理”的信号（不是字面意义的溢出）。

### 2.4 构造 new_entry 并评估“是否会导致页过满/过空/压缩溢出”

当必须改变记录大小（或需要重建 record）时，流程是：

1) 用 `row_rec_to_index_entry()` 复制出 `new_entry`
2) 用 `row_upd_index_replace_new_col_vals_index_pos()` 把 update 应用到 `new_entry`
3) 处理 INSTANT 列默认值是否要 materialize（`materialize_instant_default()`）
4) 计算：

- `old_rec_size = rec_offs_size(*offsets)`
- `new_rec_size = rec_get_converted_size(index, new_entry)`

接着做一组“乐观决策型”失败判断：

- 压缩页空间：`btr_cur_update_alloc_zip(...)` 失败则 `return DB_ZIP_OVERFLOW`
- 记录上限：`new_rec_size >= REC_MAX_DATA_SIZE` => `DB_OVERFLOW`
- 过大记录导致风险：`new_rec_size >= free_space_of_empty/2` => `DB_OVERFLOW`
- 页会变得“过空”（触发 merge/压缩更划算）：
  - 条件：`page_get_data_size(page) - old_rec_size + new_rec_size < BTR_CUR_PAGE_COMPRESS_LIMIT(index)`
  - 返回：`DB_UNDERFLOW`
- 页空间不足（就算重组也不值得/不够）：返回 `DB_OVERFLOW`

这里的关键点：

- **DB_UNDERFLOW** 是“更新后页太空”，倾向于交给悲观路径做结构性动作（如压缩/合并）
- **DB_OVERFLOW** 是“更新后页太满/空间不足”，倾向于交给悲观路径做 split 或更复杂的调整

### 2.5 真正执行“删旧插新（同页）”

一旦通过了上面的空间/阈值检查，才会：

1) `btr_cur_upd_lock_and_undo(...)`：锁检查 + undo 记录（可能返回 lock wait/其他错误）
2) 显式锁迁移：

- `lock_rec_store_on_page_infimum(block, rec)` 把锁暂存在 infimum（避免 delete 时锁结构被释放/丢失）

3) AHI 维护：`btr_search_update_hash_on_delete(cursor)`
4) 删除旧 record：`page_cur_delete_rec(...)`
5) 把 trx_id/roll_ptr 写入 `new_entry`（非 `BTR_KEEP_SYS_FLAG` 且非 intrinsic）
6) 插入新 record（必须成功）：`btr_cur_insert_if_possible(...)`，源码里有 `ut_a(rec)` 断言
7) 恢复锁：`lock_rec_restore_from_page_infimum(...)`
8) 更新 IBUF 位图（对二级索引、非临时表）：zip/非 zip 两条分支

如果在中途返回了非 `DB_SUCCESS`，函数末尾会：

- 再次 `btr_cur_prefetch_siblings(block)` 给悲观路径做 IO 预热。

### 2.6 返回码语义（上层如何用它决定回退）

- `DB_SUCCESS`：完成更新（就地或同页删插）。
- `DB_ZIP_OVERFLOW`：压缩页空间不足（需要悲观路径处理，可能触发 split/重压缩/重组）。
- `DB_OVERFLOW`：空间不足或涉及 extern（需要悲观路径）。
- `DB_UNDERFLOW`：更新后页过空（悲观路径可能会做压缩/merge）。
- 其他错误码：来自锁/undo（例如 lock wait）。

补充：上层 `btr_cur_pessimistic_update()` 会先调用一次该函数，捕获 `DB_ZIP_OVERFLOW/DB_OVERFLOW/DB_UNDERFLOW` 后再进入更重的流程（含空间预留、LOB 页处理、必要时 split）。

---

## 3. `btr_cur_optimistic_delete_func()`：能“直接删记录”就删，否则转悲观删除

入口：`bool btr_cur_optimistic_delete_func(...)`，[btr0cur.cc#L4530](storage/innobase/btr/btr0cur.cc#L4530)

### 3.1 这条路径的核心语义

它是一个“能不能在当前 leaf 页上完成物理删除”的快速判定与执行器：

- 返回 `true`：已经在当前页完成了 `page_cur_delete_rec(...)`，并完成必要的 AHI/锁/IBUF 维护。
- 返回 `false`：不做删除，提前为悲观路径预取兄弟页，然后让上层去调用 `btr_cur_pessimistic_delete()`。

### 3.2 前置条件

- 只允许 leaf page：`ut_ad(page_is_leaf(...))`
- `mtr` 持有该页 X latch。
- Online DDL：同 insert/update 的限制（要么 clustered，要么 `flags & BTR_CREATE_FLAG`）。

### 3.3 成功条件：`no_compress_needed`

源码里把“能否乐观物理删除”浓缩成一个布尔表达式：

- `!rec_offs_any_extern(offsets)`：记录没有 extern（LOB 外置）字段
- `btr_cur_can_delete_without_compress(cursor, rec_offs_size(offsets), mtr)`：删除后不需要触发页压缩/合并等“结构性动作”

两者同时满足才进入真正 delete。

### 3.4 真正 delete 时做了什么（按源码顺序）

在 `no_compress_needed == true` 分支：

1) `lock_update_delete(block, rec)`：更新行锁结构（删除对应的 lock 状态调整）
2) `btr_search_update_hash_on_delete(cursor)`：AHI 清理
3) `page_cur_delete_rec(...)`：物理删除 record

随后分 zip/non-zip：

- **压缩页（`page_zip != nullptr`）**：只做 delete，不更新 IBUF 位图 free（注释说明：对压缩页，IBUF_BITMAP_FREE 定义方式导致“purge 删除记录”不会影响其值）。
- **非压缩页**：
  - 计算 `max_ins = page_get_max_insert_size_after_reorganize(page, 1)`
  - delete 后，如果是二级索引 leaf、非临时表、且不是 change buffer 自己：`ibuf_update_free_bits_low(block, max_ins, mtr)`

### 3.5 为什么会返回 false（转悲观）

只要满足任一：

- 记录有 extern 字段（LOB）
- 删除会导致页过空，需要压缩/merge（`btr_cur_can_delete_without_compress()` 返回 false）

则：

- `btr_cur_prefetch_siblings(block)` 预读左右兄弟页
- 返回 `false`，让上层进入 `btr_cur_pessimistic_delete()`（后者会处理：释放外部字段、父节点指针更新、可能的 merge/重平衡等）。

---

## 4. 调试对照建议（只针对这三个函数本身）

你在 VS Code/gdb 里观察“乐观→悲观回退”时，最省力的断点组合通常是：

- 插入：在 [btr_cur_optimistic_insert()](storage/innobase/btr/btr0cur.cc#L2659) 下断点，重点看最终是 `DB_SUCCESS` 还是 `DB_FAIL`
  - 关注变量：`rec_size`、`max_size`、`leaf`、`page_size.is_compressed()`、`big_rec_vec != nullptr`
- 更新：在 [btr_cur_optimistic_update()](storage/innobase/btr/btr0cur.cc#L3491) 下断点，重点看返回码是否为 `DB_OVERFLOW/DB_UNDERFLOW/DB_ZIP_OVERFLOW`
  - 关注变量：`old_rec_size`、`new_rec_size`、`page_get_data_size(page)`
- 删除：在 [btr_cur_optimistic_delete_func()](storage/innobase/btr/btr0cur.cc#L4530) 下断点，重点看 `no_compress_needed` 是否为 true
  - 关注变量：`rec_offs_any_extern(offsets)`、`rec_offs_size(offsets)`

如果你希望把“回退到悲观路径”的现场卡住（比如要看 split/merge 前的锁状态），我建议下一步把断点再加到：

- `btr_cur_pessimistic_insert()` / `btr_cur_pessimistic_update()` / `btr_cur_pessimistic_delete()` 的入口，观察它们各自接收到的 “optimistic 返回码/布尔值”。
