# 快速参考卡

## 最简单的使用方式

```bash
cd /usr/local/mysql-8.0.34/tests

# 第一次使用（准备配置）
cp config.yaml.example config.yaml
vim config.yaml  # 改一下配置

# 测试基准版本
python3 test_framework.py --variant=baseline --config config.yaml

# 测试优化版本（参数相同）
python3 test_framework.py --variant=optimized --config config.yaml

# 生成对比报告 --- 取最近两次的
python3 compare.py \
  --baseline "$(ls -dt out/runs/* | head -n 2 | tail -n 1)/result.csv" \
  --optimized "$(ls -dt out/runs/* | head -n 1)/result.csv" \
  --output out/reports
```

## 一键测试（复制粘贴）

### 测试 Split（页分裂）优化
```bash
python3 test_framework.py --variant=baseline --config config.yaml --workload=insert --threads=10 --operations=10000
python3 test_framework.py --variant=optimized --config config.yaml --workload=insert --threads=10 --operations=10000
```

### 测试 Merge（页合并）优化
```bash
python3 test_framework.py --variant=baseline --config config.yaml --workload=delete_heavy --threads=5 --operations=2000
python3 test_framework.py --variant=optimized --config config.yaml --workload=delete_heavy --threads=5 --operations=2000
```

### 测试混合场景
```bash
python3 test_framework.py --variant=baseline --config config.yaml --workload=mixed_with_delete --threads=5 --operations=2000
python3 test_framework.py --variant=optimized --config config.yaml --workload=mixed_with_delete --threads=5 --operations=2000
```

## Workload 速查表

| 名称 | 场景 | Splits | Merges |
|------|------|--------|--------|
| `insert` | 纯插入 | ✓✓✓ | ✗ |
| `range_insert` | 范围插入 | ✓✓ | ✗ |
| `mixed` | 混合读写 | ✓ | ✗ |
| `delete_heavy` | **大量删除** | ✓ | ✓✓✓ |
| `mixed_with_delete` | 混合含删除 | ✓✓ | ✓ |
| `churn` | 数据流失 | ✓✓ | ✓ |

## 关键指标速查

运行完成后看这些指标：

```
吞吐量 (TPS): 3932.08           ← 越高越好
P50: 1.071 ms                   ← 中位数，越低越好
P95: 2.476 ms                   ← 尾延迟，越低越好
P99: 3.218 ms                   ← 尾延迟，越低越好

页分裂 (Splits): 22 次           ← Split 场景看这个
页合并尝试 (Merge Attempts): 11 次  ← Merge 场景看这个
页合并成功 (Merge Successful): 3 次 ← Merge 场景看这个
```

## 参数覆盖速查

```bash
# 改 workload
--workload=delete_heavy

# 改并发度
--threads=20

# 改数据量
--operations=5000

# 改数据库连接（不推荐，建议改 config.yaml）
--host=127.0.0.1 --port=3306 --user=root --password=0333
```

## 报告查看

```bash
# 查看最新报告列表
ls -lt out/reports/*.html | head -5

# 用浏览器打开最新报告
open $(ls -t out/reports/*.html | head -1)
```

## 故障排查

```bash
# MySQL 没启动？
ps aux | grep mysqld

# 连接失败？
mysql -h127.0.0.1 -P3306 -uroot -p0333 -e "SELECT 1"

# 配置文件格式错误？
python3 -c "import yaml; yaml.safe_load(open('config.yaml'))"

# 查看输出目录
ls -dt out/runs/* | head -3

# 查看详细结果（JSON）
cat $(ls -dt out/runs/* | head -1)/result.json | python3 -m json.tool | head -50
```

## 环境变量快速设置

```bash
# 使用环境变量临时改参数（只在这次命令生效）
MYSQL_HOST=192.168.1.100 TEST_WORKLOAD=delete_heavy \
  python3 test_framework.py --variant=baseline --config config.yaml

# 导出环境变量（对后续所有命令生效）
export MYSQL_HOST=192.168.1.100
export TEST_WORKLOAD=delete_heavy
python3 test_framework.py --variant=baseline --config config.yaml
```

---

更多详情：
- [测试说明.md](../docs-DDOOCC/测试说明.md) - 新手入门
- [CONFIG_GUIDE.md](CONFIG_GUIDE.md) - 配置文件详解
- [tests/README.md](README.md) - 技术深度文档
