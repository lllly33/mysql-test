# B+树分裂调试指南

## 正确启动调试流程（必须按顺序）

### 1. 启动带 gdb 的 mysqld（VS Code 调试面板）

**重要**：必须通过 VS Code 调试面板启动，才会附加 gdb 并自动设置断点。

1. 打开 VS Code 左侧"运行和调试"面板（或按 `Ctrl+Shift+D`）
2. 在顶部下拉框选择：`(Launch) mysqld (insert datadir)`
3. 点击绿色三角或按 `F5` 启动

**你会看到**：
- 调试控制台出现 gdb 输出
- 自动执行断点设置命令：
  ```
  Breakpoint 1 at ... btr_page_split_and_insert
  Breakpoint 2 at ... btr_cur_optimistic_update
  Breakpoint 3 at ... btr_cur_pessimistic_update
  ...
  ```
- mysqld 启动日志显示 "ready for connections"

### 2. 运行工作负载（触发分裂）

**方式 1（推荐）**：VS Code 任务
- `Ctrl+Shift+P` → 输入 "Tasks: Run Task"
- 选择：`Run: B+tree split workload (insert datadir)`

**方式 2**：命令行
```bash
/usr/local/mysql-8.0.34/build_debug/runtime_output_directory/mysql \
  --no-defaults --protocol=socket \
  --socket=/usr/local/mysql-8.0.34/build_debug/mysql_insert.sock \
  -uroot < /usr/local/mysql-8.0.34/scripts/btr_split_repro.sql
```

### 3. 观察断点命中

**预置的断点**（见 `.vscode/launch.json`）：
- `btr_page_split_and_insert`：B+树页分裂入口
- `btr_cur_optimistic_update`：乐观更新（同页内修改）
- `btr_cur_pessimistic_update`：悲观更新（可能分裂）
- `row_ins_clust_index_entry_low`：聚簇索引插入（低层）
- `row_ins_clust_index_entry_by_modify`：聚簇索引插入（修改路径）
- `rbreak ^rw_lock_(s|sx|x)_(lock|unlock)$`：捕获所有读写锁的加锁/解锁

**停在断点后可查看**：
```gdb
# 查看当前页信息
p *cursor
p *cursor->index
p page_get_n_recs(page)

# 查看锁状态
p block->lock
p block->lock.writer
p block->lock.reader_count

# 查看调用栈
bt

# 继续执行
c
```

## 常见问题

### Q: 没有命中断点
**A**: 确认是否通过 VS Code 调试面板启动（不是终端命令行）。查看调试控制台是否有 gdb 输出。

### Q: 断点设置失败
**A**: 在 gdb 控制台手动补设：
```gdb
break btr_page_split_and_insert
break btr_cur_pessimistic_update
```

### Q: 想禁用锁的 rbreak（太频繁）
**A**: 启动后在 gdb 控制台：
```gdb
info break
delete <锁断点编号范围>
```

## 文件位置

- 调试配置：`.vscode/launch.json`
- 任务配置：`.vscode/tasks.json`
- 工作负载 SQL：`scripts/btr_split_repro.sql`
- 本指南：`DEBUG_GUIDE.md`
