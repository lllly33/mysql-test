#!/usr/bin/env python3
# ============================================================================
# compare.py - Performance Comparison and Visualization
# ============================================================================
"""
对比两个版本的性能测试结果，生成可视化报告。

功能：
  1. 对比基准版本与优化版本的性能指标
  2. 计算改进百分比与统计显著性
    3. 生成 HTML 报告（图表内嵌在 HTML 中）

用法：
  python3 compare.py --baseline=baseline_results.csv --optimized=optimized_results.csv --output=report.html
"""

import argparse
import csv
import json
import sys
import base64
from datetime import datetime
from pathlib import Path

# 尝试导入绘图库（如果缺少，会提示安装）
try:
    import matplotlib.pyplot as plt
    import numpy as np
    MATPLOTLIB_AVAILABLE = True

    # 尽量设置可用的中文字体（存在则使用，不存在则自动回退）
    try:
        plt.rcParams['font.sans-serif'] = ['Noto Sans CJK SC', 'SimHei', 'Microsoft YaHei', 'DejaVu Sans']
        plt.rcParams['axes.unicode_minus'] = False
    except Exception:
        pass
except ImportError:
    MATPLOTLIB_AVAILABLE = False
    print("[WARNING] matplotlib 未安装，图表功能将不可用")
    print("  安装：pip install matplotlib numpy")

# ============================================================================
# 数据加载与解析
# ============================================================================

class ResultsLoader:
    """加载和解析测试结果"""

    @staticmethod
    def attach_adjacent_json_if_any(filepath, results_dict):
        """当输入是 CSV 时，尝试加载同名 .json 作为详细信息并挂到 _details。"""
        try:
            path = Path(filepath)
            if path.suffix.lower() != '.csv':
                return results_dict
            json_path = path.with_suffix('.json')
            if not json_path.exists():
                return results_dict
            details = ResultsLoader.load_json(str(json_path))
            if details is None:
                return results_dict
            # 避免污染主指标空间，统一挂到 _details
            results_dict = dict(results_dict)
            results_dict['_details'] = details
            return results_dict
        except Exception:
            return results_dict

    @staticmethod
    def load_csv(filepath):
        """加载 CSV 结果文件"""
        results = {}
        try:
            with open(filepath, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # 将所有数值转换为浮点数
                    for key in row:
                        try:
                            results[key] = float(row[key])
                        except ValueError:
                            results[key] = row[key]
            return results
        except FileNotFoundError:
            print(f"[ERROR] 文件不存在: {filepath}")
            return None
        except Exception as e:
            print(f"[ERROR] 无法读取 {filepath}: {e}")
            return None

    @staticmethod
    def load_json(filepath):
        """加载 JSON 结果文件"""
        try:
            with open(filepath, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            print(f"[ERROR] 文件不存在: {filepath}")
            return None
        except Exception as e:
            print(f"[ERROR] 无法读取 {filepath}: {e}")
            return None


# ============================================================================
# 性能对比分析
# ============================================================================

class PerformanceComparison:
    """对比两个版本的性能"""

    def __init__(self, baseline, optimized):
        self.baseline = baseline
        self.optimized = optimized
        self.comparison = {}

    def compute_improvements(self):
        """
        计算改进百分比

        改进公式（值越大越好，如 TPS）：
          improvement = (optimized - baseline) / baseline * 100%

        改进公式（值越小越好，如延迟）：
          improvement = (baseline - optimized) / baseline * 100%
        """

        # 定义指标及其"越大越好"或"越小越好"的特性
        metrics = {
            'tps': {'direction': 'higher', 'name': '吞吐量'},
            'avg_latency_ms': {'direction': 'lower', 'name': '平均延迟'},
            'p50_ms': {'direction': 'lower', 'name': 'P50 延迟'},
            'p95_ms': {'direction': 'lower', 'name': 'P95 延迟'},
            'p99_ms': {'direction': 'lower', 'name': 'P99 延迟'},
            'operations': {'direction': 'higher', 'name': '总操作数'},
        }

        for key, info in metrics.items():
            # 支持多种字段名变体
            base_val = self.baseline.get(key)
            opt_val = self.optimized.get(key)

            # 处理字段名的不同形式
            if base_val is None:
                for alias in [key.replace('_', ''), key.replace('_', '-')]:
                    base_val = self.baseline.get(alias)
                    if base_val is not None:
                        break

            if opt_val is None:
                for alias in [key.replace('_', ''), key.replace('_', '-')]:
                    opt_val = self.optimized.get(alias)
                    if opt_val is not None:
                        break

            if base_val is None or opt_val is None:
                continue

            try:
                base_val = float(base_val)
                opt_val = float(opt_val)

                if info['direction'] == 'higher':
                    # 值越大越好
                    improvement = (opt_val - base_val) / base_val * 100 if base_val != 0 else 0
                else:
                    # 值越小越好
                    improvement = (base_val - opt_val) / base_val * 100 if base_val != 0 else 0

                self.comparison[key] = {
                    'baseline': base_val,
                    'optimized': opt_val,
                    'improvement_percent': improvement,
                    'name': info['name'],
                    'direction': info['direction'],
                }
            except (ValueError, TypeError):
                pass

        return self.comparison

    def get_summary(self):
        """生成对比摘要"""
        if not self.comparison:
            return "未找到对比数据"

        summary = []
        summary.append("性能对比摘要：")
        summary.append("-" * 60)

        for key, data in self.comparison.items():
            improvement = data['improvement_percent']
            direction = "↑ 更好" if improvement > 0 else "↓ 更差" if improvement < 0 else "→ 相同"

            summary.append(
                f"{data['name']:15s} | "
                f"基准: {data['baseline']:10.2f} | "
                f"优化: {data['optimized']:10.2f} | "
                f"改进: {improvement:+7.2f}% {direction}"
            )

        return "\n".join(summary)


# ============================================================================
# HTML 报告生成
# ============================================================================

class ReportGenerator:
    """生成 HTML 报告"""

    def __init__(self, baseline, optimized, comparison):
        self.baseline = baseline
        self.optimized = optimized
        self.comparison = comparison

    def generate_html(self, output_file, embedded_charts_png_base64=None):
        """生成 HTML 报告"""

        def _pick_field(name):
            if isinstance(self.baseline, dict):
                v = self.baseline.get(name)
                if v not in (None, ''):
                    return v
            if isinstance(self.optimized, dict):
                v = self.optimized.get(name)
                if v not in (None, ''):
                    return v
            return None

        threads_display = 'N/A'
        _threads = _pick_field('threads')
        if _threads is not None:
            try:
                threads_display = str(int(float(_threads)))
            except Exception:
                threads_display = str(_threads)

        workload_display = 'N/A'
        _workload = _pick_field('workload')
        if _workload is not None:
            workload_display = str(_workload)

        html_content = f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>InnoDB 性能测试对比报告</title>
    <style>
        body {{
            font-family: 'Microsoft YaHei', Arial, sans-serif;
            margin: 20px;
            background-color: #f5f5f5;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
            background-color: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        h1 {{
            color: #333;
            border-bottom: 3px solid #0066cc;
            padding-bottom: 10px;
        }}
        h2 {{
            color: #0066cc;
            margin-top: 30px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin: 20px 0;
        }}
        th, td {{
            border: 1px solid #ddd;
            padding: 12px;
            text-align: right;
        }}
        th {{
            background-color: #0066cc;
            color: white;
        }}
        tr:nth-child(even) {{
            background-color: #f9f9f9;
        }}
        .positive {{
            color: #00aa00;
            font-weight: bold;
        }}
        .negative {{
            color: #aa0000;
            font-weight: bold;
        }}
        .metric-name {{
            text-align: left;
            font-weight: bold;
        }}
        .summary {{
            background-color: #e8f4f8;
            padding: 15px;
            border-left: 4px solid #0066cc;
            margin: 20px 0;
        }}
        .timestamp {{
            color: #666;
            font-size: 12px;
        }}
        .chart {{
            margin: 16px 0 6px;
            padding: 14px;
            border: 1px solid #eee;
            border-radius: 8px;
            background: #fafafa;
        }}
        .chart img {{
            width: 100%;
            height: auto;
            display: block;
            border-radius: 6px;
        }}
        .chart-notes {{
            margin-top: 10px;
            color: #333;
            line-height: 1.55;
        }}
        .muted {{
            color: #666;
            font-size: 13px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>InnoDB B+Tree 性能测试对比报告</h1>
        <p class="timestamp">生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>

        <div class="summary">
            <h2>执行摘要</h2>
            <p>
                本报告对比了两个 MySQL 版本在高并发 B+树 SMO（结构修改）场景下的性能表现。
                具体场景参数（如线程数、workload）以结果文件中记录的值为准。
            </p>
        </div>

        <h2>关键指标对比</h2>
        <table>
            <tr>
                <th style="text-align: left;">指标</th>
                <th>基准版本</th>
                <th>优化版本</th>
                <th>改进（%）</th>
                <th>方向</th>
            </tr>
"""

        for key, data in self.comparison.items():
            improvement = data['improvement_percent']

            # 判断改进方向
            if improvement > 0:
                direction = "✓ 改善"
                class_name = "positive"
            elif improvement < 0:
                direction = "✗ 恶化"
                class_name = "negative"
            else:
                direction = "→ 相同"
                class_name = ""

            html_content += f"""
            <tr>
                <td class="metric-name">{data['name']}</td>
                <td>{data['baseline']:.2f}</td>
                <td>{data['optimized']:.2f}</td>
                <td><span class="{class_name}">{improvement:+.2f}</span></td>
                <td>{direction}</td>
            </tr>
"""

        html_content += """
        </table>

        <h2>B+Tree 结构修改计数（本次测试区间增量）</h2>
        <table>
            <tr>
                <th style="text-align: left;">指标</th>
                <th>基准版本</th>
                <th>优化版本</th>
            </tr>
"""

        def _get_num(d, key, default=0.0):
            try:
                v = d.get(key)
                return float(v) if v is not None else float(default)
            except Exception:
                return float(default)

        btree_rows = [
            ('btree_splits', '页分裂次数 (index_page_splits)'),
            ('btree_merge_attempts', '页合并尝试次数 (index_page_merge_attempts)'),
            ('btree_merge_successful', '页合并成功次数 (index_page_merge_successful)'),
        ]
        for key, label in btree_rows:
            b = _get_num(self.baseline, key)
            o = _get_num(self.optimized, key)
            html_content += f"""
            <tr>
                <td class=\"metric-name\">{label}</td>
                <td>{b:.0f}</td>
                <td>{o:.0f}</td>
            </tr>
"""

        html_content += """
        </table>

        <h2>等待/锁热点（Top 15，按本次测试区间 SUM_TIMER_WAIT）</h2>
"""

        def _render_waits_table(title, details_obj):
            waits = []
            try:
                waits = (details_obj or {}).get('stats', {}).get('db_metrics', {}).get('waits_top', [])
            except Exception:
                waits = []

            table = f"<h3>{title}</h3>"
            if not waits:
                return table + "<p>无数据（可能未开启 performance_schema 计时，或本次区间无明显等待）。</p>"

            table += """
            <table>
                <tr>
                    <th style="text-align: left;">事件</th>
                    <th>次数</th>
                    <th>累计等待 (s)</th>
                    <th>max（截至结束，ms）</th>
                </tr>
            """
            for r in waits:
                ev = r.get('event_name', '')
                cnt = r.get('count', 0)
                sum_s = r.get('sum_s', 0.0)
                max_ms = r.get('max_ms_end', 0.0)
                table += (
                    "<tr>"
                    f"<td class=\"metric-name\" style=\"text-align:left;\">{ev}</td>"
                    f"<td>{cnt}</td>"
                    f"<td>{sum_s:.6f}</td>"
                    f"<td>{max_ms:.3f}</td>"
                    "</tr>"
                )
            table += "</table>"
            return table

        base_details = self.baseline.get('_details') if isinstance(self.baseline, dict) else None
        opt_details = self.optimized.get('_details') if isinstance(self.optimized, dict) else None

        html_content += _render_waits_table('基准版本', base_details)
        html_content += _render_waits_table('优化版本', opt_details)

        if embedded_charts_png_base64:
            html_content += f"""

        <h2>图表概览</h2>
        <div class="chart">
            <img alt="charts" src="data:image/png;base64,{embedded_charts_png_base64}">
            <div class="chart-notes">
                <ul>
                    <li><strong>Absolute Values</strong>：各指标的基准/优化绝对值对比（更直观）。</li>
                    <li><strong>Improvement</strong>：各指标改进百分比；TPS 为正更好，延迟为正更好（因为计算的是“降低比例”）。</li>
                    <li><strong>Throughput</strong>：TPS 单独放大展示，便于快速看吞吐变化。</li>
                    <li><strong>P95 Latency</strong>：P95 尾延迟（ms），用于观察尾部抖动/排队。</li>
                    <li><strong>B+Tree split/merge</strong>：本轮区间内 split/merge 计数器增量（用于确认 SMO 活跃度）。</li>
                    <li><strong>Wait Hotspots</strong>：等待热点 Top15 的累计等待秒数之和（粗略反映锁/等待压力）。</li>
                </ul>
            </div>
        </div>
"""

        html_content += f"""

        <h2>测试环境</h2>
        <table>
            <tr>
                <th style="text-align: left;">参数</th>
                <th>值</th>
            </tr>
            <tr>
                <td class="metric-name">MySQL 版本</td>
                <td>8.0.34</td>
            </tr>
            <tr>
                <td class="metric-name">并发线程数</td>
                <td>{threads_display}</td>
            </tr>
            <tr>
                <td class="metric-name">工作负载</td>
                <td>{workload_display}</td>
            </tr>
        </table>
"""

        html_content += f"""

        <hr>
        <p style="text-align: center; color: #999; font-size: 12px;">
            InnoDB B+Tree 性能测试框架 | {datetime.now().strftime('%Y-%m-%d')}
        </p>
    </div>
</body>
</html>
"""

        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(html_content)

        print(f"[INFO] HTML 报告已生成: {output_file}")


# ============================================================================
# 可视化图表（可选）
# ============================================================================

class ChartGenerator:
    """生成内嵌图表（PNG Base64）"""

    def __init__(self, comparison, baseline=None, optimized=None):
        self.comparison = comparison
        self.baseline = baseline or {}
        self.optimized = optimized or {}

    def render_charts_png_base64(self):
        """生成对比图表并返回 base64 编码的 PNG（用于内嵌到 HTML）"""
        if not MATPLOTLIB_AVAILABLE:
            print("[WARNING] matplotlib 不可用，跳过图表生成")
            return None

        # 尽量选择更美观的默认风格（存在则用，不存在不强求）
        try:
            plt.style.use('seaborn-v0_8-whitegrid')
        except Exception:
            pass

        # 统一配色（色盲友好、对比清晰）
        c_base = '#4C78A8'
        c_opt = '#F58518'
        c_pos = '#54A24B'
        c_neg = '#E45756'

        # PNG 图表统一使用英文，避免缺少中文字体导致乱码
        metric_name_en = {
            'tps': 'TPS',
            'avg_latency_ms': 'Avg Latency (ms)',
            'p50_ms': 'P50 (ms)',
            'p95_ms': 'P95 (ms)',
            'p99_ms': 'P99 (ms)',
            'operations': 'Operations',
        }

        fig, axes = plt.subplots(3, 2, figsize=(14, 13))
        fig.suptitle('InnoDB Performance (Baseline vs Optimized)', fontsize=16, fontweight='bold')

        # 提取数据用于绘图
        metrics_names = []
        baseline_vals = []
        optimized_vals = []
        improvements = []

        for key, data in self.comparison.items():
            metrics_names.append(metric_name_en.get(key, str(data.get('name', key))))
            baseline_vals.append(data['baseline'])
            optimized_vals.append(data['optimized'])
            improvements.append(data['improvement_percent'])

        # 1. 绝对值对比（柱状图）
        ax = axes[0, 0]
        x = np.arange(len(metrics_names))
        width = 0.35

        ax.bar(x - width/2, baseline_vals, width, label='Baseline', color=c_base)
        ax.bar(x + width/2, optimized_vals, width, label='Optimized', color=c_opt)

        ax.set_ylabel('Value')
        ax.set_title('Absolute Values')
        ax.set_xticks(x)
        ax.set_xticklabels(metrics_names, rotation=45, ha='right')
        ax.legend()
        ax.grid(axis='y', alpha=0.3)

        # 2. 改进百分比（柱状图）
        ax = axes[0, 1]
        colors = [c_pos if x > 0 else c_neg for x in improvements]
        x2 = np.arange(len(metrics_names))
        ax.bar(x2, improvements, color=colors, alpha=0.7)
        ax.axhline(y=0, color='black', linestyle='-', linewidth=0.8)
        ax.set_ylabel('Improvement (%)')
        ax.set_title('Improvement')
        ax.set_xticks(x2)
        ax.set_xticklabels(metrics_names, rotation=45, ha='right')
        ax.grid(axis='y', alpha=0.3)

        # 3. 吞吐量对比（如果有 TPS 数据）
        ax = axes[1, 0]
        if 'tps' in self.comparison:
            tps_data = self.comparison['tps']
            versions = ['Baseline', 'Optimized']
            tps_vals = [tps_data['baseline'], tps_data['optimized']]
            colors_tps = [c_base, c_opt]
            bars = ax.bar(versions, tps_vals, color=colors_tps, alpha=0.7)

            # 在柱子上标注数值
            for bar, val in zip(bars, tps_vals):
                height = bar.get_height()
                ax.text(bar.get_x() + bar.get_width()/2., height,
                       f'{val:.0f}',
                       ha='center', va='bottom', fontweight='bold')

            ax.set_ylabel('TPS (ops/s)')
            ax.set_title('Throughput')
            ax.grid(axis='y', alpha=0.3)

        # 4. 延迟对比（P95 为例）
        ax = axes[1, 1]
        if 'p95_ms' in self.comparison:
            p95_data = self.comparison['p95_ms']
            versions = ['Baseline', 'Optimized']
            p95_vals = [p95_data['baseline'], p95_data['optimized']]
            colors_p95 = [c_base, c_opt]
            bars = ax.bar(versions, p95_vals, color=colors_p95, alpha=0.7)

            # 在柱子上标注数值
            for bar, val in zip(bars, p95_vals):
                height = bar.get_height()
                ax.text(bar.get_x() + bar.get_width()/2., height,
                       f'{val:.1f}ms',
                       ha='center', va='bottom', fontweight='bold')

            ax.set_ylabel('Latency (ms)')
            ax.set_title('P95 Latency')
            ax.grid(axis='y', alpha=0.3)

        def _f(d, k, default=0.0):
            try:
                v = d.get(k)
                return float(v) if v is not None else float(default)
            except Exception:
                return float(default)

        # 5. B+Tree split/merge 计数（区间增量）
        ax = axes[2, 0]
        labels = ['splits', 'merge_attempts', 'merge_success']
        b_vals = [
            _f(self.baseline, 'btree_splits'),
            _f(self.baseline, 'btree_merge_attempts'),
            _f(self.baseline, 'btree_merge_successful'),
        ]
        o_vals = [
            _f(self.optimized, 'btree_splits'),
            _f(self.optimized, 'btree_merge_attempts'),
            _f(self.optimized, 'btree_merge_successful'),
        ]
        x = np.arange(len(labels))
        width = 0.35
        ax.bar(x - width/2, b_vals, width, label='Baseline', color=c_base)
        ax.bar(x + width/2, o_vals, width, label='Optimized', color=c_opt)
        ax.set_title('B+Tree split/merge (delta)')
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=0)
        ax.set_ylabel('Count')
        ax.grid(axis='y', alpha=0.3)
        ax.legend()

        # 6. 等待/锁热点汇总（TopN SUM_TIMER_WAIT）
        ax = axes[2, 1]
        versions = ['Baseline', 'Optimized']
        wait_sum = [_f(self.baseline, 'waits_top_sum_s'), _f(self.optimized, 'waits_top_sum_s')]
        bars = ax.bar(versions, wait_sum, color=[c_base, c_opt], alpha=0.9)
        for bar, val in zip(bars, wait_sum):
            ax.text(bar.get_x() + bar.get_width()/2., bar.get_height(), f'{val:.3f}s',
                    ha='center', va='bottom', fontweight='bold')
        ax.set_title('Wait Hotspots (Top15 sum)')
        ax.set_ylabel('Seconds (s)')
        ax.grid(axis='y', alpha=0.3)

        plt.tight_layout()
        import io
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=160, bbox_inches='tight')
        plt.close()
        buf.seek(0)
        return base64.b64encode(buf.read()).decode('ascii')


# ============================================================================
# 主函数
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='InnoDB 性能测试对比与报告生成',
        epilog="""
示例用法：
    # 对比两个 CSV 文件，生成 HTML 报告（图表内嵌）
  python3 compare.py --baseline=baseline_results.csv --optimized=optimized_results.csv --output=report

    # 不生成内嵌图表
  python3 compare.py --baseline=baseline_results.csv --optimized=optimized_results.csv --output=report.html --no-charts
        """
    )

    parser.add_argument('--baseline', required=True, help='基准版本的结果文件 (CSV 或 JSON)')
    parser.add_argument('--optimized', required=True, help='优化版本的结果文件 (CSV 或 JSON)')
    parser.add_argument('--output', default='comparison_report',
                       help='输出文件前缀（HTML 或文件夹）')
    parser.add_argument('--no-charts', action='store_true', help='不生成图表')

    args = parser.parse_args()

    def _compute_output_html_path(output_arg: str, baseline_path: str, optimized_path: str):
        out = Path(output_arg)
        is_dir_mode = str(output_arg).endswith('/') or (out.exists() and out.is_dir())
        if is_dir_mode:
            out.mkdir(parents=True, exist_ok=True)

            def _extract_workload(p: str) -> str:
                """从 run 目录名中提取 workload 参数"""
                path = Path(p)
                parent_name = path.parent.name if path.stem.lower() == 'result' else path.stem
                # 从目录名中提取 workload=xxx
                import re
                match = re.search(r'workload=(\w+)', parent_name)
                return match.group(1) if match else 'unknown'

            baseline_wl = _extract_workload(baseline_path)
            optimized_wl = _extract_workload(optimized_path)

            ts = datetime.now().strftime('%Y%m%dT%H%M%S')

            # 简化文件名：只保留 workload 和时间戳
            if baseline_wl == optimized_wl:
                filename = f"compare_{baseline_wl}_{ts}.html"
            else:
                filename = f"compare_{baseline_wl}_vs_{optimized_wl}_{ts}.html"

            html = out / filename
            return str(html)

        # file/prefix mode (保持兼容)：
        output_html = output_arg if output_arg.endswith('.html') else f"{output_arg}.html"
        return output_html

    # 加载结果
    print("🚥🚥🚥[INFO] 加载基准版本结果...")
    baseline = ResultsLoader.load_csv(args.baseline)
    if baseline is None:
        baseline = ResultsLoader.load_json(args.baseline)

    if baseline is not None:
        baseline = ResultsLoader.attach_adjacent_json_if_any(args.baseline, baseline)

    if baseline is None:
        sys.exit(1)

    print("🚥🚥 [INFO] 加载优化版本结果...")
    optimized = ResultsLoader.load_csv(args.optimized)
    if optimized is None:
        optimized = ResultsLoader.load_json(args.optimized)

    if optimized is not None:
        optimized = ResultsLoader.attach_adjacent_json_if_any(args.optimized, optimized)

    if optimized is None:
        sys.exit(1)

    # 进行对比
    print("🚥  [INFO] 计算性能改进...")
    comparison = PerformanceComparison(baseline, optimized)
    comparison.compute_improvements()

    # 打印摘要
    print("\n" + comparison.get_summary())

    embedded_png = None
    if not args.no_charts:
        chart_gen = ChartGenerator(comparison.comparison, baseline=baseline, optimized=optimized)
        embedded_png = chart_gen.render_charts_png_base64()

    # 生成 HTML 报告（图表内嵌）
    output_html = _compute_output_html_path(args.output, args.baseline, args.optimized)
    reporter = ReportGenerator(baseline, optimized, comparison.comparison)
    reporter.generate_html(output_html, embedded_charts_png_base64=embedded_png)

    print("\n[SUCCESS] 报告生成完成！✅✅")


if __name__ == '__main__':
    main()
