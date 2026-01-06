#!/usr/bin/env python3
# ============================================================================
# metrics_collector.py - InnoDB Performance Metrics Collector
# ============================================================================
"""
自动从 MySQL performance_schema 收集性能指标。

功能：
  1. 查询 performance_schema 表获取锁等待、I/O 统计
  2. 聚合关键指标（TPS、延迟、锁等待时间）
  3. 生成 CSV 报告，供对比分析

用法：
  python3 metrics_collector.py --host=127.0.0.1 --port=3306 --output=metrics.csv
"""

import mysql.connector
import argparse
import sys
import time
from datetime import datetime
import csv

# ============================================================================
# 数据库连接
# ============================================================================

class MetricsCollector:
    def __init__(self, host, port, user, password):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.conn = None

    def connect(self):
        """连接到 MySQL"""
        try:
            self.conn = mysql.connector.connect(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                database='information_schema',
                autocommit=True
            )
            print(f"[OK] 已连接到 {self.host}:{self.port}")
        except mysql.connector.Error as e:
            print(f"[ERROR] 连接失败: {e}")
            sys.exit(1)

    def disconnect(self):
        """断开连接"""
        if self.conn:
            self.conn.close()

    def query(self, sql):
        """执行查询"""
        cursor = self.conn.cursor(dictionary=True)
        try:
            cursor.execute(sql)
            return cursor.fetchall()
        except Exception as e:
            print(f"[ERROR] 查询失败: {e}")
            return []
        finally:
            cursor.close()

    # ========================================================================
    # 指标收集方法
    # ========================================================================

    def collect_table_io_metrics(self):
        """
        收集表 I/O 等待统计

        返回：
          {
            'total_operations': 整数,
            'total_io_wait_sec': 浮点数,
            'avg_io_latency_ms': 浮点数,
            'max_io_latency_ms': 浮点数,
          }
        """
        sql = """
        SELECT
            COUNT_STAR as total_ops,
            SUM_TIMER_WAIT / 1e12 as total_wait_sec,
            AVG_TIMER_WAIT / 1e9 as avg_wait_ms,
            MAX_TIMER_WAIT / 1e9 as max_wait_ms
        FROM performance_schema.table_io_waits_summary_by_table
        WHERE OBJECT_SCHEMA = 'test_innodb'
        AND OBJECT_NAME = 'test_table';
        """

        rows = self.query(sql)
        if rows:
            row = rows[0]
            return {
                'total_operations': int(row['total_ops']) if row['total_ops'] else 0,
                'total_io_wait_sec': float(row['total_wait_sec']) if row['total_wait_sec'] else 0,
                'avg_io_latency_ms': float(row['avg_wait_ms']) if row['avg_wait_ms'] else 0,
                'max_io_latency_ms': float(row['max_wait_ms']) if row['max_wait_ms'] else 0,
            }
        return {}

    def collect_lock_waits(self):
        """
        收集锁等待统计

        返回：
          {
            'current_waits': 当前等待数,
            'total_wait_time_sec': 总等待时间,
            'avg_wait_per_lock_ms': 平均单个锁的等待时间,
          }
        """
        # 当前等待中的锁
        current_wait_sql = """
        SELECT COUNT(*) as cnt FROM sys.innodb_lock_waits;
        """

        rows = self.query(current_wait_sql)
        current_waits = int(rows[0]['cnt']) if rows else 0

        # 锁的历史等待统计
        # 注：这个指标需要从 performance_schema 的事件表收集
        wait_stat_sql = """
        SELECT
            COUNT_STAR,
            SUM_TIMER_WAIT / 1e12 as total_wait_sec,
            AVG_TIMER_WAIT / 1e9 as avg_wait_ms
        FROM performance_schema.events_waits_summary_global_by_event_name
        WHERE EVENT_NAME LIKE '%lock%'
        LIMIT 1;
        """

        wait_rows = self.query(wait_stat_sql)
        if wait_rows:
            w = wait_rows[0]
            return {
                'current_waits': current_waits,
                'total_wait_events': int(w['COUNT_STAR']) if w['COUNT_STAR'] else 0,
                'total_wait_time_sec': float(w['total_wait_sec']) if w['total_wait_sec'] else 0,
                'avg_wait_per_lock_ms': float(w['avg_wait_ms']) if w['avg_wait_ms'] else 0,
            }

        return {
            'current_waits': current_waits,
            'total_wait_events': 0,
            'total_wait_time_sec': 0,
            'avg_wait_per_lock_ms': 0,
        }

    def collect_statement_metrics(self):
        """
        收集语句执行统计

        返回：
          {
            'total_statements': 总语句数,
            'total_statement_time_sec': 总执行时间,
            'avg_statement_latency_ms': 平均语句延迟,
          }
        """
        sql = """
        SELECT
            SUM(COUNT_STAR) as total_stmts,
            SUM(SUM_TIMER_WAIT) / 1e12 as total_time_sec,
            AVG(AVG_TIMER_WAIT) / 1e9 as avg_latency_ms
        FROM performance_schema.events_statements_summary_by_digest
        WHERE DIGEST_TEXT LIKE '%test_table%';
        """

        rows = self.query(sql)
        if rows and rows[0]['total_stmts']:
            row = rows[0]
            return {
                'total_statements': int(row['total_stmts']),
                'total_statement_time_sec': float(row['total_time_sec']) if row['total_time_sec'] else 0,
                'avg_statement_latency_ms': float(row['avg_latency_ms']) if row['avg_latency_ms'] else 0,
            }
        return {
            'total_statements': 0,
            'total_statement_time_sec': 0,
            'avg_statement_latency_ms': 0,
        }

    def collect_innodb_status(self):
        """
        收集 InnoDB 内部状态信息（如行锁竞争）

        返回：
          {
            'row_lock_waits': 行级锁等待次数,
            'row_lock_wait_time_sec': 行级锁等待总时间,
          }
        """
        sql = "SHOW ENGINE INNODB STATUS;"

        cursor = self.conn.cursor()
        try:
            cursor.execute(sql)
            rows = cursor.fetchall()

            # 从 Status 字符串中提取行锁信息
            status_text = rows[0][2] if rows else ""

            # 简单的字符串匹配（生产环境应该用正则表达式解析）
            row_lock_waits = 0
            if "row locks waited" in status_text.lower():
                # 提取数字（简化版）
                for line in status_text.split('\n'):
                    if "row locks waited" in line.lower():
                        parts = line.split()
                        for i, part in enumerate(parts):
                            if 'row' in part.lower() and i > 0:
                                try:
                                    row_lock_waits = int(parts[i-1])
                                except:
                                    pass
                        break

            return {
                'row_lock_waits': row_lock_waits,
                'row_lock_wait_time_sec': 0,  # 需要更复杂的解析
                'innodb_status_raw': status_text[:500],  # 前 500 字符
            }
        except Exception as e:
            print(f"[WARNING] 无法收集 InnoDB Status: {e}")
            return {}
        finally:
            cursor.close()

    def collect_all_metrics(self):
        """收集所有指标"""
        print("\n[INFO] 开始收集性能指标...")
        print(f"[INFO] 时间: {datetime.now().isoformat()}")

        metrics = {
            'timestamp': datetime.now().isoformat(),
            'table_io': self.collect_table_io_metrics(),
            'lock_waits': self.collect_lock_waits(),
            'statements': self.collect_statement_metrics(),
            'innodb': self.collect_innodb_status(),
        }

        return metrics

    def print_metrics(self, metrics):
        """美化打印指标"""
        print("\n" + "="*70)
        print("性能指标汇总")
        print("="*70)

        if 'table_io' in metrics and metrics['table_io']:
            print("\n[表 I/O 统计]")
            for k, v in metrics['table_io'].items():
                print(f"  {k}: {v}")

        if 'lock_waits' in metrics and metrics['lock_waits']:
            print("\n[锁等待统计]")
            for k, v in metrics['lock_waits'].items():
                if k != 'innodb_status_raw':
                    print(f"  {k}: {v}")

        if 'statements' in metrics and metrics['statements']:
            print("\n[语句执行统计]")
            for k, v in metrics['statements'].items():
                print(f"  {k}: {v}")

        print("\n" + "="*70 + "\n")

    def save_to_csv(self, metrics, output_file):
        """保存指标到 CSV"""
        with open(output_file, 'w', newline='') as f:
            writer = csv.writer(f)

            # 表头
            writer.writerow(['指标', '值', '单位'])

            # 表 I/O 指标
            if 'table_io' in metrics:
                writer.writerow(['-- 表 I/O 统计 --', '', ''])
                for k, v in metrics['table_io'].items():
                    unit = 'sec' if 'sec' in k else 'ms' if 'ms' in k else ''
                    writer.writerow([k, f'{v:.3f}', unit])

            # 锁等待指标
            if 'lock_waits' in metrics:
                writer.writerow(['-- 锁等待统计 --', '', ''])
                for k, v in metrics['lock_waits'].items():
                    if k != 'innodb_status_raw':
                        unit = 'sec' if 'sec' in k else 'ms' if 'ms' in k else ''
                        writer.writerow([k, f'{v:.3f}', unit])

            # 语句执行指标
            if 'statements' in metrics:
                writer.writerow(['-- 语句执行统计 --', '', ''])
                for k, v in metrics['statements'].items():
                    unit = 'sec' if 'sec' in k else 'ms' if 'ms' in k else ''
                    writer.writerow([k, f'{v:.3f}', unit])

        print(f"[INFO] 指标已保存到 {output_file}")


# ============================================================================
# 主函数
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='InnoDB Performance Metrics Collector',
        epilog="""
示例用法：
  # 收集指标并输出到 CSV
  python3 metrics_collector.py --host=127.0.0.1 --port=3306 --output=metrics.csv

  # 实时监控（每 5 秒收集一次）
  python3 metrics_collector.py --host=127.0.0.1 --interval=5 --duration=60
        """
    )

    parser.add_argument('--host', default='127.0.0.1', help='MySQL 主机')
    parser.add_argument('--port', type=int, default=3306, help='MySQL 端口')
    parser.add_argument('--user', default='root', help='MySQL 用户')
    parser.add_argument('--password', default='', help='MySQL 密码')
    parser.add_argument('--output', default=None, help='输出 CSV 文件')
    parser.add_argument('--interval', type=int, default=5, help='监控间隔（秒）')
    parser.add_argument('--duration', type=int, default=60, help='监控总时间（秒）')

    args = parser.parse_args()

    # 创建收集器
    collector = MetricsCollector(args.host, args.port, args.user, args.password)
    collector.connect()

    try:
        # 单次收集或连续监控
        if args.output:
            metrics = collector.collect_all_metrics()
            collector.print_metrics(metrics)
            collector.save_to_csv(metrics, args.output)
        else:
            # 连续监控
            elapsed = 0
            while elapsed < args.duration:
                metrics = collector.collect_all_metrics()
                collector.print_metrics(metrics)

                print(f"[INFO] 下次收集在 {args.interval} 秒后... (已运行 {elapsed}s / {args.duration}s)")
                time.sleep(args.interval)
                elapsed += args.interval

    finally:
        collector.disconnect()


if __name__ == '__main__':
    main()
