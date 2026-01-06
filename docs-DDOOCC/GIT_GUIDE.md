# Git 版本控制使用指南

## 📌 重要说明

**Git 本地版本控制完全独立运行**：无需远程仓库即可实现完整的版本控制功能，包括提交、分支、回滚、对比等所有核心功能。远程仓库只是用于团队协作和备份。

---

## 📋 当前 Git 配置

### 本地仓库配置
```
仓库位置：/usr/local/mysql-8.0.34/.git/
用户名：  MySQL Development
邮箱：    dev@mysql-btree-test.local
远程仓库：ssh://ezone.ksyun.com/WUT/MySQL-8.0.34
```

### 配置文件位置
- **仓库配置**: `/usr/local/mysql-8.0.34/.git/config`
- **忽略规则**: `/usr/local/mysql-8.0.34/.gitignore`

### 当前状态
- **基线标签**: `v0.1-baseline` （原始 MySQL 8.0.34 源码 + 测试框架）
- **当前分支**: `master`

---

## 🚀 常用 Git 命令

### 1️⃣ 查看状态与历史

```bash
# 查看当前工作区状态（修改了哪些文件）
git status

# 查看提交历史（简洁版）
git log --oneline

# 查看提交历史（详细版）
git log

# 查看最近 N 条提交
git log -n 5 --oneline

# 查看某个文件的修改历史
git log --oneline -- storage/innobase/btr/btr0btr.cc

# 查看所有标签
git tag -l
```

### 2️⃣ 查看代码差异

```bash
# 查看工作区所有未暂存的修改
git diff

# 查看某个文件的修改
git diff storage/innobase/btr/btr0btr.cc

# 查看已暂存（staged）但未提交的修改
git diff --staged

# 对比两个提交
git diff v0.1-baseline HEAD

# 对比当前代码与某个标签
git diff v0.1-baseline

# 查看某次提交的改动
git show <commit-hash>
git show 8e07be6
```

### 3️⃣ 暂存与提交

```bash
# 添加单个文件到暂存区
git add storage/innobase/btr/btr0btr.cc

# 添加所有修改的文件
git add .

# 添加所有 .cc 文件
git add **/*.cc

# 提交（需先 git add）
git commit -m "优化 B+Tree split 逻辑，减少页分裂次数"

# 一步提交所有已跟踪文件的修改（不包括新文件）
git commit -am "Fix B+Tree merge success rate"

# 修改最后一次提交信息
git commit --amend -m "新的提交信息"
```

### 4️⃣ 分支管理

```bash
# 查看所有分支
git branch

# 创建新分支
git branch feature/btree-split-optimization

# 切换到分支
git checkout feature/btree-split-optimization

# 创建并切换到新分支（一步完成）
git checkout -b feature/btree-merge-improvement

# 删除分支
git branch -d feature/old-experiment

# 查看分支图
git log --graph --oneline --all
```

### 5️⃣ 回滚与撤销

```bash
# 丢弃工作区某个文件的修改（危险！不可恢复）
git checkout -- storage/innobase/btr/btr0btr.cc

# 丢弃工作区所有修改（危险！）
git checkout -- .

# 取消暂存某个文件（保留修改）
git reset HEAD storage/innobase/btr/btr0btr.cc

# 回退到上一次提交（保留工作区修改）
git reset --soft HEAD~1

# 回退到上一次提交（丢弃所有修改，危险！）
git reset --hard HEAD~1

# 回退到指定提交或标签
git reset --hard v0.1-baseline
git reset --hard 8e07be6

# 创建一个"反向提交"来撤销某次提交（推荐方式）
git revert <commit-hash>
```

### 6️⃣ 标签管理

```bash
# 创建轻量标签
git tag v0.2-split-optimized

# 创建带注释的标签（推荐）
git tag -a v0.2-split-optimized -m "优化了 B+Tree split 逻辑"

# 为历史提交打标签
git tag -a v0.1.1-bugfix -m "修复了..." <commit-hash>

# 查看标签信息
git show v0.2-split-optimized

# 删除标签
git tag -d v0.2-split-optimized

# 推送标签到远程（如果需要）
git push origin v0.2-split-optimized
```

### 7️⃣ 暂存工作进度

```bash
# 临时保存当前修改（切换分支前使用）
git stash

# 查看所有暂存的进度
git stash list

# 恢复最近的暂存
git stash pop

# 恢复指定的暂存
git stash apply stash@{1}

# 删除暂存
git stash drop stash@{0}
```

---

## 📖 典型工作流程

### 场景 1：修改源码并提交

```bash
# 1. 创建开发分支
git checkout -b feature/btree-split-optimization

# 2. 修改代码
vim storage/innobase/btr/btr0btr.cc

# 3. 查看修改
git diff

# 4. 提交修改
git add storage/innobase/btr/btr0btr.cc
git commit -m "优化 B+Tree split 阈值，从 50% 调整为 40%"

# 5. 继续修改并提交
# ... 重复步骤 2-4

# 6. 打标签标记里程碑
git tag -a v0.2-split-optimized -m "B+Tree split 优化完成"
```

### 场景 2：对比优化前后的代码

```bash
# 对比当前代码与基线
git diff v0.1-baseline storage/innobase/btr/btr0btr.cc

# 查看所有修改的文件
git diff v0.1-baseline --name-only

# 生成完整的 diff 文件
git diff v0.1-baseline > optimization.patch
```

### 场景 3：实验性修改（可能需要回滚）

```bash
# 1. 创建实验分支
git checkout -b experiment/aggressive-merge

# 2. 做激进的修改
# ...

# 3. 测试失败，放弃修改
git checkout master
git branch -d experiment/aggressive-merge

# 或者：保留实验但回到主分支
git checkout master
# 实验分支仍在，以后可以再看
```

### 场景 4：发现问题需要回滚

```bash
# 查看提交历史找到问题提交
git log --oneline

# 方法 1：回到某个历史版本（硬回滚）
git reset --hard v0.1-baseline

# 方法 2：创建反向提交（推荐，保留历史）
git revert <problem-commit-hash>
```

---

## 🔍 源码修改追踪建议

### 推荐的分支策略

```
master                 ← 稳定版本（每次测试通过后合并）
├── feature/split-opt  ← B+Tree Split 优化
├── feature/merge-opt  ← B+Tree Merge 优化
└── experiment/*       ← 实验性修改
```

### 推荐的标签命名

```
v0.1-baseline          ← 原始版本
v0.2-split-optimized   ← Split 优化完成
v0.3-merge-optimized   ← Merge 优化完成
v1.0-production        ← 生产就绪版本
```

### 提交信息规范

```bash
# 推荐格式：<类型>: <简短描述>
git commit -m "feat: 优化 B+Tree split 阈值"
git commit -m "fix: 修复 merge 并发冲突问题"
git commit -m "perf: 减少页分裂时的锁等待"
git commit -m "test: 添加 split 性能测试用例"
git commit -m "docs: 更新 B+Tree 优化文档"

# 类型说明：
# feat   - 新功能
# fix    - 修复 bug
# perf   - 性能优化
# refactor - 重构
# test   - 测试相关
# docs   - 文档更新
```

---

## 📊 与测试框架结合使用

### 测试前后对比流程

```bash
# 1. 在基线版本测试（记录性能基准）
git checkout v0.1-baseline
cd tests
python3 test_framework.py --variant=baseline --config config.yaml
# 结果保存在 tests/out/

# 2. 切换到优化版本
git checkout feature/btree-split-optimization

# 3. 重新编译（如果修改了源码）
cd /usr/local/mysql-8.0.34/build_debug
make -j$(nproc)

# 4. 测试优化版本
cd /usr/local/mysql-8.0.34/tests
python3 test_framework.py --variant=optimized --config config.yaml

# 5. 对比测试结果
python3 compare.py

# 6. 如果性能提升，提交并打标签
git add .
git commit -m "perf: B+Tree split 优化，split 次数减少 30%"
git tag -a v0.2-split-optimized -m "Split 优化版本"
```

---

## ⚠️ 注意事项

### 1. `.gitignore` 已配置忽略

- `build*/` - 编译产物
- `data*/` - 数据目录
- `tests/out/` - 测试结果
- `tests/config.yaml` - 敏感配置

这些文件不会被提交，修改后不会影响版本控制。

### 2. 危险命令（慎用）

```bash
# 这些命令会永久丢失数据，使用前三思！
git reset --hard <commit>   # 丢弃所有未提交的修改
git checkout -- <file>      # 丢弃文件的修改
git clean -fd               # 删除所有未跟踪的文件
git push -f                 # 强制推送（覆盖远程）
```

### 3. 推荐的安全做法

```bash
# 修改前先查看影响
git status
git diff

# 重要修改前创建备份标签
git tag backup-before-major-change

# 实验性修改使用分支
git checkout -b experiment/new-idea
```

---

## 🛠️ 快速参考

```bash
# 最常用的 5 个命令
git status                    # 查看状态
git diff                      # 查看修改
git add <file>                # 暂存文件
git commit -m "message"       # 提交
git log --oneline             # 查看历史

# 源码修改工作流
git checkout -b feature/xxx   # 创建分支
# ... 修改代码 ...
git add .                     # 暂存
git commit -m "perf: xxx"     # 提交
git tag -a v0.x -m "xxx"      # 打标签

# 性能测试对比流程
git checkout v0.1-baseline    # 切换到基线
# ... 测试 ...
git checkout feature/xxx      # 切换到优化版本
# ... 测试 ...
python3 compare.py            # 对比结果
```

---

## 📚 相关文档

- **测试框架**: `/usr/local/mysql-8.0.34/tests/README.md`
- **配置指南**: `/usr/local/mysql-8.0.34/tests/CONFIG_GUIDE.md`
- **快速参考**: `/usr/local/mysql-8.0.34/tests/QUICK_REFERENCE.md`
- **Git 配置**: `/usr/local/mysql-8.0.34/.git/config`
- **忽略规则**: `/usr/local/mysql-8.0.34/.gitignore`

---

## 💡 常见问题

**Q: 本地版本控制需要联网吗？**
A: 不需要。Git 是分布式版本控制系统，所有历史记录都在本地 `.git` 目录，完全离线工作。

**Q: 如何查看某次修改的详细内容？**
A: `git show <commit-hash>` 或 `git log -p`

**Q: 如何找回误删的提交？**
A: `git reflog` 可以查看所有操作历史，然后 `git reset --hard <commit>`

**Q: 修改源码后需要先提交再测试吗？**
A: 不需要。Git 不影响编译和测试，可以随时修改、测试，确认后再提交。

**Q: 远程仓库是必需的吗？**
A: 不是。远程仓库只用于备份和协作，本地 Git 功能完全独立。当前配置已有远程仓库 `ssh://ezone.ksyun.com/WUT/MySQL-8.0.34`，需要时可以推送：`git push origin master`

---

**最后更新**: 2026-01-06
**文档位置**: `/usr/local/mysql-8.0.34/docs-DDOOCC/GIT_GUIDE.md`
