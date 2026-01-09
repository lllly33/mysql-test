#!/usr/bin/env python3
# ============================================================================
# test_framework.py - InnoDB B+Tree SMO Performance Test Framework
# ============================================================================
"""
主测试驱动程序，用于生成高并发工作负载并收集性能数据。

功能：
  1. 构造高并发 INSERT/UPDATE/SELECT 工作负载
  2. 实时监控锁等待与 I/O 性能
  3. 收集延迟分布（P50, P95, P99）
  4. 输出结果 CSV/JSON，便于对比与回归

用法：
    # 使用配置文件（推荐）
    python3 test_framework.py --variant=baseline --config config.yaml

    # 覆盖配置文件中的部分参数
    python3 test_framework.py --variant=baseline --config config.yaml --workload=delete_heavy --threads=20
"""

import argparse
import mysql.connector
import threading
import time
import json
import sys
import os
from collections import defaultdict
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


# ============================================================================
# 配置文件加载
# ============================================================================

def load_config_file(config_path):
    """从 YAML 配置文件加载配置"""
    try:
        import yaml
    except ImportError:
        print("[ERROR] 需要安装 PyYAML: pip3 install PyYAML")
        sys.exit(1)

    try:
        with open(config_path, 'r') as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        print(f"[ERROR] 配置文件不存在: {config_path}")
        sys.exit(1)
    except yaml.YAMLError as e:
        print(f"[ERROR] 配置文件格式错误: {e}")
        sys.exit(1)


# =========================================================================
# 数据库侧观测：B+Tree split/merge 与等待事件
# =========================================================================

class DBMetrics:
    """在单次测试区间内采集 DB 侧指标（通过跑前/跑后快照计算增量）。"""

    INNODB_METRIC_NAMES = (
        'index_page_splits',
        'index_page_merge_attempts',
        'index_page_merge_successful',
    )

    @staticmethod
    def _exec(conn, sql, params=None, dict_rows=False):
        cursor = conn.cursor(dictionary=dict_rows)
        try:
            cursor.execute(sql, params or ())
            if getattr(cursor, 'with_rows', False):
                return cursor.fetchall()
            return []
        finally:
            cursor.close()

    @staticmethod
    def best_effort_prepare(conn):
        """尽量开启/计时相关 instrument，并启用 InnoDB metrics 计数器。"""
        # 1) 启用 InnoDB 内置 metrics（默认可能是 disabled）
        for name in DBMetrics.INNODB_METRIC_NAMES:
            try:
                DBMetrics._exec(conn, "SET GLOBAL innodb_monitor_enable = %s", (name,))
            except Exception:
                pass

        # 2) 确保等待事件能计时（如果 performance_schema 允许）
        try:
            DBMetrics._exec(
                conn,
                """
                UPDATE performance_schema.setup_instruments
                SET enabled='YES', timed='YES'
                WHERE name LIKE 'wait/synch/%/innodb/%'
                   OR name = 'wait/lock/metadata/sql/mdl'
                """.strip(),
            )
        except Exception:
            pass

    @staticmethod
    def snapshot_innodb_metrics(conn):
        rows = DBMetrics._exec(
            conn,
            """
            SELECT name, count
            FROM information_schema.innodb_metrics
            WHERE name IN ('index_page_splits','index_page_merge_attempts','index_page_merge_successful')
            """.strip(),
            dict_rows=True,
        )
        snap = {r['name']: int(r.get('count') or 0) for r in rows}
        for name in DBMetrics.INNODB_METRIC_NAMES:
            snap.setdefault(name, 0)
        return snap

    @staticmethod
    def snapshot_waits_summary(conn):
        rows = DBMetrics._exec(
            conn,
            """
            SELECT EVENT_NAME,
                   COUNT_STAR,
                   SUM_TIMER_WAIT,
                   MAX_TIMER_WAIT,
                   AVG_TIMER_WAIT
            FROM performance_schema.events_waits_summary_global_by_event_name
            WHERE EVENT_NAME LIKE 'wait/synch/%/innodb/%'
               OR EVENT_NAME = 'wait/lock/metadata/sql/mdl'
            """.strip(),
            dict_rows=True,
        )
        snap = {}
        for r in rows:
            ev = r['EVENT_NAME']
            snap[ev] = {
                'count_star': int(r.get('COUNT_STAR') or 0),
                'sum_timer_wait': int(r.get('SUM_TIMER_WAIT') or 0),
                'max_timer_wait': int(r.get('MAX_TIMER_WAIT') or 0),
                'avg_timer_wait': int(r.get('AVG_TIMER_WAIT') or 0),
            }
        return snap

    @staticmethod
    def diff_innodb_metrics(before, after):
        delta = {}
        for name in DBMetrics.INNODB_METRIC_NAMES:
            delta[name] = max(0, int(after.get(name, 0)) - int(before.get(name, 0)))
        return delta

    @staticmethod
    def diff_waits(before, after):
        delta = {}
        for ev, a in after.items():
            b = before.get(ev, {})
            delta_count = int(a.get('count_star', 0)) - int(b.get('count_star', 0))
            delta_sum = int(a.get('sum_timer_wait', 0)) - int(b.get('sum_timer_wait', 0))
            if delta_count <= 0 and delta_sum <= 0:
                continue
            # max_timer_wait 是“历史最大值”，严格的区间增量无法直接得出；这里保留 end 值用于参考。
            delta[ev] = {
                'count_star_delta': max(0, delta_count),
                'sum_timer_wait_delta': max(0, delta_sum),
                'max_timer_wait_end': int(a.get('max_timer_wait', 0)),
            }
        return delta

    @staticmethod
    def waits_topn(delta_waits, topn=15):
        items = [
            {
                'event_name': ev,
                'count': v['count_star_delta'],
                'sum_s': v['sum_timer_wait_delta'] / 1e12,
                'max_ms_end': v['max_timer_wait_end'] / 1e9,
            }
            for ev, v in delta_waits.items()
        ]
        items.sort(key=lambda x: x['sum_s'], reverse=True)
        return items[:topn]

# ============================================================================
# 配置
# ============================================================================

class Config:
    """测试配置"""
    def __init__(self, args):
        self.host = args.host
        self.port = args.port
        self.user = args.user
        self.password = args.password
        self.database = args.database

        self.variant = args.variant  # 'baseline' 或 'optimized'
        self.threads = args.threads  # 并发线程数
        self.duration = args.duration  # 测试持续时间（秒）
        self.operations_per_thread = args.operations

        self.output_file = args.output

        # 工作负载类型
        self.workload = args.workload  # 'insert' / 'range_insert' / 'mixed'


# ============================================================================
# 数据库连接与初始化
# ============================================================================

class DatabaseConnection:
    """数据库连接池"""
    def __init__(self, config):
        self.config = config
        self.pool = []

    def get_connection(self):
        """获取一个数据库连接"""
        try:
            conn = mysql.connector.connect(
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                password=self.config.password,
                database=self.config.database,
                autocommit=True,
                use_unicode=True,
                charset='utf8mb4'
            )
            return conn
        except mysql.connector.Error as e:
            print(f"[ERROR] 无法连接数据库: {e}")
            sys.exit(1)

    def close_all(self):
        """关闭所有连接"""
        for conn in self.pool:
            try:
                conn.close()
            except:
                pass


# ============================================================================
# 工作负载生成器
# ============================================================================

class WorkloadGenerator:
    """生成不同类型的数据库工作负载"""

    def __init__(self, config, db_conn):
        self.config = config
        self.db_conn = db_conn
        self.stats = {
            'operations': 0,
            'errors': 0,
            'latencies': [],  # 记录每个操作的延迟
            'lock_waits': 0,
        }
        self.lock = threading.Lock()

    def reset_table(self):
        """重置测试表"""
        conn = self.db_conn.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("CALL reset_test_table()")
            # mysql-connector 要求消费完所有结果集，否则关闭 cursor 时会报 Unread result found。
            while True:
                if getattr(cursor, "with_rows", False):
                    cursor.fetchall()
                if not cursor.nextset():
                    break
            print("[INFO] 测试表已重置")
        except Exception as e:
            print(f"[ERROR] 重置表失败: {e}")
        finally:
            cursor.close()
            conn.close()

    def insert_batch(self, thread_id, data_key_base, batch_size, key_stride=None, key_offset=0):
        """
        单个线程执行批量 INSERT

        参数：
          thread_id: 线程编号
          data_key_base: 数据键基值（用于避免主键冲突）
                    batch_size: 本线程要插入的记录数
                    key_stride: data_key 的步长（用于避免多线程主键冲突）
                    key_offset: data_key 的偏移（配合 key_stride 使用）
        """
        conn = self.db_conn.get_connection()
        cursor = conn.cursor()

        for i in range(batch_size):
            try:
                t0 = time.time()

                # 构造数据（填充数据以加速页面填满）
                if key_stride is None:
                    data_key = data_key_base + i
                else:
                    # 让多个线程写入同一递增 key 空间，但彼此不冲突：
                    # 线程 tid 写入: base + (i*stride + offset)
                    data_key = data_key_base + (i * int(key_stride) + int(key_offset))
                payload = 'x' * 800  # 800 字节数据

                # INSERT 语句
                cursor.execute(
                    "INSERT INTO test_table (data_key, payload) VALUES (%s, %s)",
                    (data_key, payload)
                )
                conn.commit()  # 每次立即提交，确保事务独立

                elapsed_ms = (time.time() - t0) * 1000

                # 记录延迟
                with self.lock:
                    self.stats['latencies'].append(elapsed_ms)
                    self.stats['operations'] += 1

                if i % 100 == 0:
                    print(f"[THREAD {thread_id}] 已插入 {i} / {batch_size} 条")

            except Exception as e:
                with self.lock:
                    self.stats['errors'] += 1
                print(f"[ERROR] 线程 {thread_id}: {e}")

        cursor.close()
        conn.close()

    def range_insert(self, thread_id, id_range_start, id_range_end):
        """
        范围插入：多个线程分别在不同的键值范围插入数据
        这种方式会让分裂分散到多个节点，而不是集中在同一个
        """
        batch_size = id_range_end - id_range_start
        # range_insert 需要严格落在各自的范围内，使用连续 key
        self.insert_batch(thread_id, id_range_start, batch_size, key_stride=None)

    def mixed_workload(self, thread_id, operations):
        """
        混合工作负载：50% INSERT、30% SELECT、20% UPDATE
        用于观察读写竞争与锁交互
        """
        conn = self.db_conn.get_connection()
        cursor = conn.cursor()

        for i in range(operations):
            try:
                op_type = i % 10
                t0 = time.time()

                # 每个线程使用独立 key 空间，避免冲突
                data_key = thread_id * operations + i

                if op_type < 5:  # 50% INSERT
                    cursor.execute(
                        "INSERT INTO test_table (data_key, payload) VALUES (%s, %s)",
                        (data_key, 'x' * 800)
                    )
                elif op_type < 8:  # 30% SELECT
                    cursor.execute(
                        "SELECT COUNT(*) FROM test_table WHERE data_key BETWEEN %s AND %s",
                        (data_key - 100, data_key + 100)
                    )
                    cursor.fetchall()
                else:  # 20% UPDATE
                    cursor.execute(
                        "UPDATE test_table SET payload = %s WHERE data_key = %s LIMIT 1",
                        ('x' * 900, data_key)
                    )

                conn.commit()
                elapsed_ms = (time.time() - t0) * 1000

                with self.lock:
                    self.stats['latencies'].append(elapsed_ms)
                    self.stats['operations'] += 1

            except Exception as e:
                with self.lock:
                    self.stats['errors'] += 1

        cursor.close()
        conn.close()

    def delete_heavy_workload(self, thread_id, operations):
        """
        DELETE_HEAVY 工作负载：
        1. 先插入大量数据填充 B+Tree
        2. 随机删除 50-70% 的记录
        预期：触发大量 Merge 操作
        """
        conn = self.db_conn.get_connection()
        cursor = conn.cursor()

        # 每个线程使用独立 key 空间
        key_base = thread_id * operations * 2

        # 阶段 1: 先插入 operations * 1.5 条数据
        insert_count = int(operations * 1.5)
        inserted_keys = []

        for i in range(insert_count):
            try:
                t0 = time.time()
                data_key = key_base + i
                inserted_keys.append(data_key)

                cursor.execute(
                    "INSERT INTO test_table (data_key, payload) VALUES (%s, %s)",
                    (data_key, 'x' * 800)
                )
                conn.commit()

                elapsed_ms = (time.time() - t0) * 1000
                with self.lock:
                    self.stats['latencies'].append(elapsed_ms)
                    self.stats['operations'] += 1
            except Exception as e:
                with self.lock:
                    self.stats['errors'] += 1

        # 阶段 2: 随机删除 60% 的记录
        import random
        delete_count = int(len(inserted_keys) * 0.6)
        keys_to_delete = random.sample(inserted_keys, delete_count)

        for data_key in keys_to_delete:
            try:
                t0 = time.time()
                cursor.execute(
                    "DELETE FROM test_table WHERE data_key = %s LIMIT 1",
                    (data_key,)
                )
                conn.commit()

                elapsed_ms = (time.time() - t0) * 1000
                with self.lock:
                    self.stats['latencies'].append(elapsed_ms)
                    self.stats['operations'] += 1
            except Exception as e:
                with self.lock:
                    self.stats['errors'] += 1

        cursor.close()
        conn.close()

    def churn_workload(self, thread_id, operations):
        """
        CHURN 工作负载（流失型）：
        持续插入新记录，同时删除旧记录
        模拟真实业务的数据流失场景
        预期：同时触发 Split 和 Merge
        """
        conn = self.db_conn.get_connection()
        cursor = conn.cursor()

        key_base = thread_id * operations * 10
        window_size = 500  # 保持活跃数据窗口大小
        inserted_keys = []

        for i in range(operations):
            try:
                t0 = time.time()
                data_key = key_base + i

                # 插入新记录
                cursor.execute(
                    "INSERT INTO test_table (data_key, payload) VALUES (%s, %s)",
                    (data_key, 'x' * 800)
                )
                inserted_keys.append(data_key)

                # 如果窗口满了，删除最旧的记录
                if len(inserted_keys) > window_size:
                    old_key = inserted_keys.pop(0)
                    cursor.execute(
                        "DELETE FROM test_table WHERE data_key = %s LIMIT 1",
                        (old_key,)
                    )

                conn.commit()
                elapsed_ms = (time.time() - t0) * 1000

                with self.lock:
                    self.stats['latencies'].append(elapsed_ms)
                    self.stats['operations'] += 1

            except Exception as e:
                with self.lock:
                    self.stats['errors'] += 1

        cursor.close()
        conn.close()

    def mixed_with_delete_workload(self, thread_id, operations):
        """
        MIXED_WITH_DELETE 工作负载：
        40% INSERT + 30% SELECT + 20% UPDATE + 10% DELETE
        预期：触发少量 Merge
        """
        conn = self.db_conn.get_connection()
        cursor = conn.cursor()

        key_base = thread_id * operations * 2
        inserted_keys = []

        for i in range(operations):
            try:
                op_type = i % 10
                t0 = time.time()
                data_key = key_base + i

                if op_type < 4:  # 40% INSERT
                    cursor.execute(
                        "INSERT INTO test_table (data_key, payload) VALUES (%s, %s)",
                        (data_key, 'x' * 800)
                    )
                    inserted_keys.append(data_key)

                elif op_type < 7:  # 30% SELECT
                    cursor.execute(
                        "SELECT COUNT(*) FROM test_table WHERE data_key BETWEEN %s AND %s",
                        (data_key - 100, data_key + 100)
                    )
                    cursor.fetchall()

                elif op_type < 9:  # 20% UPDATE
                    cursor.execute(
                        "UPDATE test_table SET payload = %s WHERE data_key = %s LIMIT 1",
                        ('x' * 900, data_key)
                    )

                else:  # 10% DELETE
                    # 从已插入的 key 中随机删除
                    if inserted_keys:
                        import random
                        delete_key = random.choice(inserted_keys)
                        inserted_keys.remove(delete_key)
                        cursor.execute(
                            "DELETE FROM test_table WHERE data_key = %s LIMIT 1",
                            (delete_key,)
                        )

                conn.commit()
                elapsed_ms = (time.time() - t0) * 1000

                with self.lock:
                    self.stats['latencies'].append(elapsed_ms)
                    self.stats['operations'] += 1

            except Exception as e:
                with self.lock:
                    self.stats['errors'] += 1

        cursor.close()
        conn.close()

    def run_workload(self):
        """
        并发执行工作负载
        """
        print(
            f"\n[INFO] 开始测试 - 并发线程: {self.config.threads}, 工作负载: {self.config.workload}"
        )

        # 重置表（不计入本轮 workload 的 DB 侧指标）
        self.reset_table()

        # DB 侧指标：跑前快照（尽量只覆盖本轮 workload 区间）
        metrics_conn = None
        before_innodb = None
        before_waits = None
        try:
            metrics_conn = self.db_conn.get_connection()
            DBMetrics.best_effort_prepare(metrics_conn)
            before_innodb = DBMetrics.snapshot_innodb_metrics(metrics_conn)
            before_waits = DBMetrics.snapshot_waits_summary(metrics_conn)
        except Exception as e:
            print(f"[WARNING] DB 指标跑前快照失败，将跳过该部分: {e}")

        # 启动测试线程
        start_time = time.time()

        with ThreadPoolExecutor(max_workers=self.config.threads) as executor:
            futures = []

            if self.config.workload == 'insert':
                # 场景 A：100 个线程都往同一个 data_key 范围插入
                # 这会快速填满节点，触发分裂
                ops_per_thread = self.config.operations_per_thread
                for tid in range(self.config.threads):
                    future = executor.submit(
                        self.insert_batch,
                        tid,
                        data_key_base=0,  # 所有线程都用同一个键值
                        batch_size=ops_per_thread,
                        # 关键：避免主键冲突，但仍然写入同一递增 key 空间
                        key_stride=self.config.threads,
                        key_offset=tid,
                    )
                    futures.append(future)

            elif self.config.workload == 'range_insert':
                # 场景 B：线程 i 插入 id=i*10000 到 (i+1)*10000
                ops_per_thread = self.config.operations_per_thread
                for tid in range(self.config.threads):
                    range_start = tid * ops_per_thread
                    range_end = range_start + ops_per_thread
                    future = executor.submit(
                        self.range_insert,
                        tid,
                        range_start,
                        range_end
                    )
                    futures.append(future)

            elif self.config.workload == 'mixed':
                # 场景 C：混合读写
                for tid in range(self.config.threads):
                    future = executor.submit(
                        self.mixed_workload,
                        tid,
                        self.config.operations_per_thread
                    )
                    futures.append(future)

            elif self.config.workload == 'delete_heavy':
                # 场景 D：大量删除（触发 Merge）
                for tid in range(self.config.threads):
                    future = executor.submit(
                        self.delete_heavy_workload,
                        tid,
                        self.config.operations_per_thread
                    )
                    futures.append(future)

            elif self.config.workload == 'churn':
                # 场景 E：数据流失（同时触发 Split 和 Merge）
                for tid in range(self.config.threads):
                    future = executor.submit(
                        self.churn_workload,
                        tid,
                        self.config.operations_per_thread
                    )
                    futures.append(future)

            elif self.config.workload == 'mixed_with_delete':
                # 场景 F：混合负载含删除
                for tid in range(self.config.threads):
                    future = executor.submit(
                        self.mixed_with_delete_workload,
                        tid,
                        self.config.operations_per_thread
                    )
                    futures.append(future)

            # 等待所有线程完成
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    print(f"[ERROR] 线程执行失败: {e}")

        elapsed_time = time.time() - start_time

        # DB 侧指标：跑后快照 & 增量
        try:
            if metrics_conn and before_innodb is not None and before_waits is not None:
                after_innodb = DBMetrics.snapshot_innodb_metrics(metrics_conn)
                after_waits = DBMetrics.snapshot_waits_summary(metrics_conn)

                innodb_delta = DBMetrics.diff_innodb_metrics(before_innodb, after_innodb)
                waits_delta = DBMetrics.diff_waits(before_waits, after_waits)
                waits_top = DBMetrics.waits_topn(waits_delta, topn=15)

                waits_sum_s = sum(x['sum_s'] for x in waits_top)  # TopN 的 sum，用于粗略对比
                waits_count = sum(int(x['count']) for x in waits_top)

                self.stats.update({
                    'btree_splits': innodb_delta.get('index_page_splits', 0),
                    'btree_merge_attempts': innodb_delta.get('index_page_merge_attempts', 0),
                    'btree_merge_successful': innodb_delta.get('index_page_merge_successful', 0),
                    'waits_top_sum_s': waits_sum_s,
                    'waits_top_count': waits_count,
                    'db_metrics': {
                        'innodb_metrics_delta': innodb_delta,
                        'waits_top': waits_top,
                    },
                })
        except Exception as e:
            print(f"[WARNING] DB 指标跑后快照/计算失败，将跳过该部分: {e}")
        finally:
            try:
                if metrics_conn:
                    metrics_conn.close()
            except Exception:
                pass

        # 计算统计数据
        self.compute_statistics(elapsed_time)

        return self.stats

    def compute_statistics(self, elapsed_time):
        """计算性能统计"""
        if not self.stats['latencies']:
            print("[ERROR] 无操作完成，无法计算统计")
            return

        latencies = sorted(self.stats['latencies'])
        n = len(latencies)

        # 百分位数
        p50 = latencies[int(n * 0.50)]
        p95 = latencies[int(n * 0.95)]
        p99 = latencies[int(n * 0.99)]

        # 基本统计
        avg = sum(latencies) / n
        min_lat = min(latencies)
        max_lat = max(latencies)

        tps = self.stats['operations'] / elapsed_time

        # 更新统计数据
        self.stats.update({
            'elapsed_time_sec': elapsed_time,
            'tps': tps,
            'avg_latency_ms': avg,
            'p50_latency_ms': p50,
            'p95_latency_ms': p95,
            'p99_latency_ms': p99,
            'min_latency_ms': min_lat,
            'max_latency_ms': max_lat,
        })

        # 打印统计信息
        print(f"\n{'='*70}")
        print(f"测试完成：{self.config.variant} - {self.config.workload}")
        print(f"{'='*70}")
        print(f"总耗时: {elapsed_time:.2f} 秒")
        print(f"总操作数: {self.stats['operations']}")
        print(f"失败数: {self.stats['errors']}")
        print(f"吞吐量 (TPS): {tps:.2f}")
        print(f"\n延迟分布 (ms):")
        print(f"  最小: {min_lat:.3f}")
        print(f"  P50: {p50:.3f}")
        print(f"  P95: {p95:.3f}")
        print(f"  P99: {p99:.3f}")
        print(f"  最大: {max_lat:.3f}")
        print(f"  平均: {avg:.3f}")

        # 打印 B+Tree 结构修改统计
        if 'btree_splits' in self.stats:
            print(f"\nB+Tree 结构修改统计:")
            print(f"  页分裂 (Splits): {self.stats.get('btree_splits', 0)} 次")
            print(f"  页合并尝试 (Merge Attempts): {self.stats.get('btree_merge_attempts', 0)} 次")
            print(f"  页合并成功 (Merge Successful): {self.stats.get('btree_merge_successful', 0)} 次")

            # 计算成功率
            merge_attempts = self.stats.get('btree_merge_attempts', 0)
            merge_successful = self.stats.get('btree_merge_successful', 0)
            if merge_attempts > 0:
                success_rate = (merge_successful / merge_attempts) * 100
                print(f"  合并成功率: {success_rate:.1f}%")

        print(f"{'='*70}\n")


# ============================================================================
# 主函数
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='InnoDB B+Tree SMO Performance Test Framework',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例用法：
    # 1. 使用配置文件（推荐，隐藏敏感信息）
    python3 test_framework.py --variant=baseline --config config.yaml

    # 2. 覆盖配置文件中的部分参数
    python3 test_framework.py --variant=baseline --config config.yaml --workload=delete_heavy --threads=20

    # 3. 完全通过命令行（不推荐，暴露密码）
    python3 test_framework.py --variant=baseline --workload=insert --threads=100 --operations=10000 \\
      --host=127.0.0.1 --port=3306 --user=root --password=0333
        """
    )

    # 配置文件
    parser.add_argument('--config', default=None, help='配置文件路径（YAML 格式），用于隐藏数据库连接信息')

    # 数据库连接参数（可通过命令行覆盖配置文件）
    parser.add_argument('--host', default=None, help='MySQL 主机')
    parser.add_argument('--port', type=int, default=None, help='MySQL 端口')
    parser.add_argument('--user', default=None, help='MySQL 用户')
    parser.add_argument('--password', default=None, help='MySQL 密码')
    parser.add_argument('--database', default=None, help='测试数据库')

    # 测试参数
    parser.add_argument('--variant', required=True, choices=['baseline', 'optimized'],
                        help='测试版本（基准或优化）')
    parser.add_argument('--workload', default=None,
                        choices=['insert', 'range_insert', 'mixed', 'delete_heavy', 'churn', 'mixed_with_delete'],
                        help='工作负载类型')
    parser.add_argument('--threads', type=int, default=None, help='并发线程数')
    parser.add_argument('--operations', type=int, default=None, help='每个线程的操作数')
    parser.add_argument('--duration', type=int, default=None, help='测试持续时间（秒）')

    # 输出
    parser.add_argument('--output', default=None, help='输出 CSV 文件路径')
    parser.add_argument(
        '--output_dir',
        default=None,
        help='输出目录（自动创建按时间戳+参数命名的子目录，里面包含 result.csv/result.json）'
    )

    args = parser.parse_args()

    # ========================================================================
    # 合并配置：配置文件 + 命令行参数 + 环境变量 + 默认值
    # 优先级：命令行 > 环境变量 > 配置文件 > 默认值
    # ========================================================================

    config_from_file = {}
    if args.config:
        config_from_file = load_config_file(args.config)
        print(f"[INFO] 已加载配置文件: {args.config}")

    # 数据库连接参数，优先使用命令行 → 环境变量 → 配置文件 → 默认值
    def _get_param(cli_value, env_var, config_key, default_value, type_fn=None):
        if cli_value is not None:
            return cli_value

        env_value = os.environ.get(env_var)
        if env_value is not None:
            return type_fn(env_value) if type_fn else env_value

        config_keys = config_key.split('.')
        val = config_from_file
        for key in config_keys:
            if not isinstance(val, dict) or key not in val:
                val = None
                break
            val = val[key]

        if val is not None:
            if type_fn:
                try:
                    return type_fn(val)
                except Exception:
                    print(
                        f"[WARN] 配置项 {config_key} 值类型异常: {val!r}，使用默认值 {default_value!r}",
                        file=sys.stderr,
                    )
                    return default_value
            return val

        return default_value

    host = _get_param(args.host, 'MYSQL_HOST', 'database.host', '127.0.0.1')
    port = _get_param(args.port, 'MYSQL_PORT', 'database.port', 3306, int)
    user = _get_param(args.user, 'MYSQL_USER', 'database.user', 'root')
    password = _get_param(args.password, 'MYSQL_PASSWORD', 'database.password', '')
    database = _get_param(args.database, 'MYSQL_DATABASE', 'database.name', 'test_innodb')

    # 测试参数
    workload = _get_param(args.workload, 'TEST_WORKLOAD', 'test.workload', 'insert')
    threads = _get_param(args.threads, 'TEST_THREADS', 'test.threads', 10, int)
    operations = _get_param(args.operations, 'TEST_OPERATIONS', 'test.operations', 10000, int)
    duration = _get_param(args.duration, 'TEST_DURATION', 'test.duration', 60, int)

    # 输出目录
    output_dir = _get_param(args.output_dir, 'OUTPUT_DIR', 'output.output_dir', None)
    output_file = _get_param(args.output, 'OUTPUT_FILE', 'output.output_file', None)

    print(f"\n[INFO] 测试配置已加载:")
    print(f"  数据库: {user}@{host}:{port}/{database}")
    print(f"  工作负载: {workload}")
    print(f"  并发线程: {threads}")
    print(f"  每线程操作数: {operations}")
    if output_dir:
        print(f"  输出目录: {output_dir}")

    def _make_run_dir(root_dir: Path, cfg: Config) -> Path:
        ts = datetime.now().strftime('%Y%m%dT%H%M%S%f')
        run_name = (
            f"{ts}_variant={cfg.variant}_workload={cfg.workload}_"
            f"t={cfg.threads}_ops={cfg.operations_per_thread}"
        )
        run_dir = root_dir / run_name
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    # 创建配置和数据库连接
    # 临时构造 args 对象以适配 Config 类
    class TempArgs:
        pass

    temp_args = TempArgs()
    temp_args.host = host
    temp_args.port = port
    temp_args.user = user
    temp_args.password = password
    temp_args.database = database
    temp_args.variant = args.variant
    temp_args.workload = workload
    temp_args.threads = threads
    temp_args.operations = operations
    temp_args.duration = duration
    temp_args.output = output_file
    temp_args.output_dir = output_dir

    config = Config(temp_args)
    db_conn = DatabaseConnection(config)

    # 运行工作负载
    generator = WorkloadGenerator(config, db_conn)
    stats = generator.run_workload()

    # 输出结果
    output_csv = None
    output_json = None

    # 规则：优先使用 --output_dir；其次如果 --output 指向目录/以 / 结尾，也视为目录模式
    if output_dir:
        run_dir = _make_run_dir(Path(output_dir), config)
        output_csv = str(run_dir / 'result.csv')
        output_json = str(run_dir / 'result.json')
    elif output_file:
        out_path = Path(output_file)
        if str(output_file).endswith('/') or (out_path.exists() and out_path.is_dir()):
            run_dir = _make_run_dir(out_path, config)
            output_csv = str(run_dir / 'result.csv')
            output_json = str(run_dir / 'result.json')
        else:
            output_csv = str(out_path)
            output_json = str(out_path.with_suffix('.json'))

    if output_csv:
        # 保存为 CSV（与 metrics_collector.py 兼容）
        with open(output_csv, 'w') as f:
            f.write(
                "variant,workload,threads,operations,"
                "tps,avg_latency_ms,p50_ms,p95_ms,p99_ms,"
                "btree_splits,btree_merge_attempts,btree_merge_successful,"
                "waits_top_sum_s,waits_top_count\n"
            )
            f.write(
                f"{config.variant},{config.workload},{config.threads},"
                f"{stats['operations']:.0f},"
                f"{stats['tps']:.2f},"
                f"{stats['avg_latency_ms']:.3f},{stats['p50_latency_ms']:.3f},"
                f"{stats['p95_latency_ms']:.3f},{stats['p99_latency_ms']:.3f},"
                f"{stats.get('btree_splits', 0)},{stats.get('btree_merge_attempts', 0)},"
                f"{stats.get('btree_merge_successful', 0)},"
                f"{stats.get('waits_top_sum_s', 0.0):.6f},{stats.get('waits_top_count', 0)}\n"
            )
        print(f"[INFO] 结果已保存到 {output_csv}")

    # 输出为 JSON（便于进一步处理）
    if output_json:
        with open(output_json, 'w') as f:
            json.dump({
                'config': {
                    'variant': config.variant,
                    'workload': config.workload,
                    'threads': config.threads,
                },
                'stats': stats,
                'timestamp': datetime.now().isoformat(),
            }, f, indent=2, default=str)
        print(f"[INFO] 详细结果已保存到 {output_json}")

    db_conn.close_all()


if __name__ == '__main__':
    main()
