# -*- coding: utf-8 -*-
# ============================================================
# 版权所有 (C) 2026 @zkxxzf
# 教务分班软件 ClassSort
# 版本: 1.13 | 日期: 2026-07-28 | 作者: @zkxxzf
# ============================================================
"""教务分班软件 - Python 版入口

特性:
  1. Excel 导入导出，字段智能识别（K/M/T 前缀 + 别名表）
  2. 总分/单科均分偏差 ≤ 0.01
  3. 高分低分逐段自动均衡
  4. 自定义分数段均衡（按分数值/年级排名）
  5. 预设班级强制分配
  6. 类别均衡（男女生/民族/学校…）偏差 ≤ 1
  7. 超强逐点均衡
  8. 互斥分散（早恋/不良小团体）
  9. 同班捆绑（双胞胎/学伴）
 10. 手动辅助调班（一对一/多对多）
 11. 自定义班级人数（大小班）
 12. 指标优化调节
 13. 姓名同音/相近自动分散
 14. 异常分值/结果自动检测
 15. 多次考试成绩综合均衡
 16. 重点班/卓越班/普通班分层次分班

用法:
    python main.py            # 启动 GUI
    python main.py --cli ...  # 命令行模式
"""
from __future__ import annotations
import os
import sys
import argparse

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def main():
    parser = argparse.ArgumentParser(description="教务分班软件")
    parser.add_argument("--cli", action="store_true", help="命令行模式")
    parser.add_argument("--input", help="输入 Excel 文件")
    parser.add_argument("--output", help="输出 Excel 文件")
    parser.add_argument("--classes", type=int, default=5, help="班级数")
    parser.add_argument("--seed", type=int, default=20260726, help="随机种子")
    args = parser.parse_args()

    if not args.cli:
        # GUI 模式
        from gui import main as gui_main
        gui_main()
        return

    # 命令行模式
    if not args.input:
        parser.error("命令行模式需要 --input")
    if not args.output:
        base, _ = os.path.splitext(args.input)
        args.output = base + "_分班结果.xlsx"

    from core.models import TaskConfig
    from core.constraints import ConstraintManager
    from core.engine import BalanceEngine
    from dataio.excel_handler import load_students, load_teachers, export_classes
    from adjust import detect_anomalies

    print(f"[*] 加载 {args.input} ...")
    students, fields, _ = load_students(args.input)
    teachers = load_teachers(args.input)
    print(f"[*] 学生 {len(students)} 名 / 字段 {len(fields)} 个 / 班主任 {len(teachers)} 名")

    score_fields = [fd.name for fd in fields if fd.is_score]
    rep = detect_anomalies(students, score_fields)
    if rep.anomalies:
        print(f"[!] 检测到 {len(rep.anomalies)} 项异常:")
        for a in rep.anomalies[:20]:
            print(f"    - {a}")

    config = TaskConfig(num_classes=args.classes, seed=args.seed)
    constraints = ConstraintManager()
    constraints.detect_name_conflicts(students)

    def cb(it, total, msg):
        print(f"\r[{it}/{total}] {msg}", end="", flush=True)

    engine = BalanceEngine(students, config, constraints, progress_cb=cb)
    classes, stats = engine.run()
    print()
    print(f"[*] 完成 - 迭代 {stats.iterations} 次, 收敛={stats.converged}")
    print(f"    总分均分偏差: {stats.total_dev:.4f}")
    print(f"    单科均分偏差: {stats.subject_dev:.4f}")
    print(f"    分段人数偏差: {stats.segment_dev}")
    print(f"    类别人数偏差: {stats.category_dev}")
    print(f"    班级人数偏差: {stats.size_dev}")

    saved = export_classes(args.output, students, fields, classes, teachers)
    print(f"[*] 已导出: {saved}")


if __name__ == "__main__":
    main()
