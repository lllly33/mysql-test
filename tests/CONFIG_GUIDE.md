# 配置文件使用指南

## 概述

为了避免在命令行中暴露敏感信息（如数据库密码），框架支持通过 YAML 配置文件管理所有参数。

## 快速开始

### 1. 复制配置文件模板

```bash
cd /usr/local/mysql-8.0.34/tests
cp config.yaml.example config.yaml
```

### 2. 编辑配置文件

编辑 `config.yaml`，修改数据库连接参数为你的实际环境：

```yaml
database:
  host: 127.0.0.1
  port: 3306
  user: root
  password: "你的密码"  # 改为实际密码
  name: test_innodb

output:
  output_dir: out/runs

test:
  workload: insert
  threads: 10
  operations: 10000
  duration: 60
```

### 3. 使用配置文件运行测试

```bash
# 使用配置文件的所有默认参数
python3 test_framework.py --variant=baseline --config config.yaml

# 覆盖配置文件中的部分参数（命令行优先级更高）
python3 test_framework.py --variant=baseline --config config.yaml \
  --workload=delete_heavy --threads=5
```

## 参数优先级

参数会按以下优先级加载（从高到低）：

1. **命令行参数**（最高优先级）
   ```bash
   --host=192.168.1.100 --port=3307 --workload=delete_heavy
   ```

2. **环境变量**
   ```bash
   export MYSQL_HOST=192.168.1.100
   export TEST_WORKLOAD=delete_heavy
   python3 test_framework.py --variant=baseline --config config.yaml
   ```

3. **配置文件**
   ```yaml
   database:
     host: 127.0.0.1
   test:
     workload: insert
   ```

4. **代码默认值**（最低优先级）
   ```python
   threads: 10
   operations: 10000
   ```

## 支持的参数

### 数据库连接参数（database.*）

| 参数 | 环境变量 | 默认值 | 说明 |
|------|---------|--------|------|
| `database.host` | `MYSQL_HOST` | 127.0.0.1 | MySQL 主机地址 |
| `database.port` | `MYSQL_PORT` | 3306 | MySQL 端口 |
| `database.user` | `MYSQL_USER` | root | MySQL 用户名 |
| `database.password` | `MYSQL_PASSWORD` | (空) | MySQL 密码 |
| `database.name` | `MYSQL_DATABASE` | test_innodb | 测试数据库名 |

### 输出参数（output.*）

| 参数 | 环境变量 | 说明 |
|------|---------|------|
| `output.output_dir` | `OUTPUT_DIR` | 输出目录，会自动生成时间戳+参数的子目录 |
| `output.output_file` | `OUTPUT_FILE` | 输出文件路径（通常不需要，使用 output_dir 更好） |

### 测试参数（test.*）

| 参数 | 环境变量 | 默认值 | 说明 |
|------|---------|--------|------|
| `test.workload` | `TEST_WORKLOAD` | insert | 工作负载类型（insert/range_insert/mixed/delete_heavy/churn/mixed_with_delete） |
| `test.threads` | `TEST_THREADS` | 10 | 并发线程数 |
| `test.operations` | `TEST_OPERATIONS` | 10000 | 每个线程的操作数 |
| `test.duration` | `TEST_DURATION` | 60 | 测试持续时间（秒） |

### 必需参数

- `--variant`：必须通过命令行指定（baseline 或 optimized），因为这是最关键的区分标识

## 实际使用示例

### 示例 1：使用配置文件的所有默认参数

```bash
cp config.yaml.example config.yaml
vim config.yaml  # 修改密码

# 基准版本
python3 test_framework.py --variant=baseline --config config.yaml

# 优化版本（使用相同参数）
python3 test_framework.py --variant=optimized --config config.yaml

# 生成对比报告
python3 compare.py \
  --baseline "$(ls -dt out/runs/* | head -n 2 | tail -n 1)/result.csv" \
  --optimized "$(ls -dt out/runs/* | head -n 1)/result.csv" \
  --output out/reports
```

### 示例 2：覆盖工作负载类型

配置文件中的默认值是 `insert`，但想测试 Merge：

```bash
python3 test_framework.py --variant=baseline --config config.yaml \
  --workload=delete_heavy --threads=5 --operations=2000
```

### 示例 3：使用环境变量

```bash
export MYSQL_HOST=192.168.1.100
export MYSQL_PORT=3307
export TEST_WORKLOAD=mixed_with_delete

python3 test_framework.py --variant=baseline --config config.yaml
# 连接到 192.168.1.100:3307，使用 mixed_with_delete workload，其他参数从配置文件读取
```

### 示例 4：多个环境配置

如果需要在不同的 MySQL 实例上测试：

```bash
# 创建多个配置文件
cp config.yaml.example config.dev.yaml  # 开发环境
cp config.yaml.example config.prod.yaml # 生产环境

# 编辑各自的连接参数
vim config.dev.yaml   # 修改为开发环境的 MySQL 连接信息
vim config.prod.yaml  # 修改为生产环境的 MySQL 连接信息

# 在开发环境测试
python3 test_framework.py --variant=baseline --config config.dev.yaml

# 在生产环境测试
python3 test_framework.py --variant=baseline --config config.prod.yaml
```

## 安全建议

1. **不要提交 config.yaml 到版本控制**（存放敏感密码）
   ```bash
   echo "config.yaml" >> .gitignore
   ```

2. **使用 config.yaml.example 作为模板**
   ```bash
   git add config.yaml.example  # 模板可以提交
   git add .gitignore
   ```

3. **权限管理**
   ```bash
   # 限制 config.yaml 的读权限
   chmod 600 config.yaml
   ```

## 常见问题

**Q: 我忘记了密码，怎么重置？**
A: 编辑 `config.yaml`，修改 `password` 字段，然后重新运行测试。

**Q: 能否使用不同的配置文件？**
A: 可以，使用 `--config` 参数指定任何配置文件路径：
```bash
python3 test_framework.py --variant=baseline --config /path/to/my/config.yaml
```

**Q: 环境变量和配置文件冲突时，哪个优先？**
A: 环境变量优先级更高。设置的环境变量会覆盖配置文件中的值。

**Q: 能否只用命令行参数，不用配置文件？**
A: 可以，但需要显式指定所有参数（包括密码），不推荐：
```bash
python3 test_framework.py --variant=baseline \
  --host=127.0.0.1 --port=3306 --user=root --password=0333 \
  --workload=insert --threads=10 --operations=10000 \
  --output_dir=out/runs
```

## 后续步骤

- 参考 [测试说明.md](../docs-DDOOCC/测试说明.md) 了解如何运行和对比测试
- 参考 [tests/README.md](README.md) 了解详细的技术细节
