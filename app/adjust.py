# -*- coding: utf-8 -*-
# ============================================================
# 版权所有 (C) 2026 @zkxxzf
# 教务分班软件 ClassSort
# 版本: 1.13 | 日期: 2026-07-28 | 作者: @zkxxzf
# ============================================================
"""手动智能辅助调班 + 异常检测 + 多次考试综合均衡"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import math

from core.models import Student, TaskConfig, ClassTypeSpec, SegmentRule
from core.constraints import ConstraintManager
from core.engine import BalanceEngine, EngineStats


@dataclass
class SwapResult:
    success: bool
    message: str
    old_cost: float = 0.0
    new_cost: float = 0.0


class ManualAdjuster:
    """手动调班：一对一换班 / 多对多调班 / 单人移班"""

    def __init__(self, students: list[Student], config: TaskConfig,
                 constraints: ConstraintManager, engine: BalanceEngine):
        self.students = students
        self.config = config
        self.constraints = constraints
        self.engine = engine

    def _assignment(self) -> dict[int, int]:
        return {st.sid: st.assigned_class for st in self.students
                if st.assigned_class is not None}

    def _restore(self, assignment: dict[int, int]):
        for st in self.students:
            st.assigned_class = assignment.get(st.sid)

    def swap_pair(self, sid_a: int, sid_b: int,
                  force_class_type: bool = False) -> SwapResult:
        """一对一换班。force_class_type=True 时允许跨班型（仍检查硬约束）"""
        assignment = self._assignment()
        if sid_a not in assignment or sid_b not in assignment:
            return SwapResult(False, "学生未分配班级")
        if assignment[sid_a] == assignment[sid_b]:
            return SwapResult(False, "两学生在同一班，无需换班")
        # 重建统计缓存，确保成本计算准确（手动调班后缓存可能过期）
        self.engine._resync_stats(assignment)
        old_cost = self.engine._total_cost(assignment)
        can = (self.engine._can_swap_force(sid_a, sid_b, assignment) if force_class_type
               else self.engine._can_swap(sid_a, sid_b, assignment))
        if not can:
            reason = "硬约束（预设/捆绑/互斥/姓名）" if force_class_type else "约束（预设/捆绑/互斥/姓名/班型）"
            return SwapResult(False, f"换班违反{reason}")
        ca, cb = assignment[sid_a], assignment[sid_b]
        assignment[sid_a], assignment[sid_b] = cb, ca
        self.engine._resync_stats(assignment)
        new_cost = self.engine._total_cost(assignment)
        self._restore(assignment)
        return SwapResult(True, "换班成功", old_cost, new_cost)

    def move_one(self, sid: int, target_class: int,
                 force_class_type: bool = False) -> SwapResult:
        """单人移班。force_class_type=True 时允许跨班型"""
        assignment = self._assignment()
        if sid not in assignment:
            return SwapResult(False, "学生未分配班级")
        if assignment[sid] == target_class:
            return SwapResult(False, "已在目标班级")
        self.engine._resync_stats(assignment)
        old_cost = self.engine._total_cost(assignment)
        can = (self.engine._can_move_force(sid, target_class, assignment) if force_class_type
               else self.engine._can_move(sid, target_class, assignment))
        if not can:
            reason = "硬约束（预设/捆绑/互斥/姓名）" if force_class_type else "约束（预设/捆绑/互斥/姓名/班型）"
            return SwapResult(False, f"移班违反{reason}")
        assignment[sid] = target_class
        self.engine._resync_stats(assignment)
        new_cost = self.engine._total_cost(assignment)
        self._restore(assignment)
        return SwapResult(True, "移班成功", old_cost, new_cost)

    def swap_chain(self, swaps: list[tuple[int, int]]) -> SwapResult:
        """多对多链式换班：依次执行，全通过才提交"""
        assignment = self._assignment()
        old_cost = self.engine._total_cost(assignment)
        backup = dict(assignment)
        for a, b in swaps:
            if a not in assignment or b not in assignment:
                self._restore(backup)
                return SwapResult(False, f"学生 {a}/{b} 未分配")
            if not self.engine._can_swap(a, b, assignment):
                self._restore(backup)
                return SwapResult(False, f"换班 {a}<->{b} 违反约束")
            assignment[a], assignment[b] = assignment[b], assignment[a]
        new_cost = self.engine._total_cost(assignment)
        self._restore(assignment)
        return SwapResult(True, "链式换班成功", old_cost, new_cost)

    def reoptimize(self, max_iter: int = 1500) -> EngineStats:
        """在当前基础上重新优化（用户手动调班后微调）"""
        assignment = self._assignment()
        # 直接调用引擎优化
        stats = self.engine._optimize(assignment)
        self._restore(assignment)
        return stats


# =================== 异常检测 ===================
@dataclass
class AnomalyReport:
    anomalies: list[str]


def detect_anomalies(students: list[Student], score_fields: list[str]) -> AnomalyReport:
    """异常检测：异常分值 / 缺失 / 越界 / 重复姓名"""
    rep = AnomalyReport(anomalies=[])
    if not students:
        rep.anomalies.append("无学生数据")
        return rep
    # 1. 缺失姓名
    for st in students:
        if not st.name.strip():
            rep.anomalies.append(f"第 {st.sid + 2} 行姓名为空")
    # 2. 重复姓名
    name_map: dict[str, list[int]] = {}
    for st in students:
        name_map.setdefault(st.name.strip(), []).append(st.sid)
    for n, sids in name_map.items():
        if n and len(sids) > 1:
            rep.anomalies.append(f"姓名重复: {n}（{len(sids)} 人）")
    # 3. 各科分数异常（超出 0-150 常见范围 / 极端值）
    for f in score_fields:
        if f == "总分":
            continue
        vals = [s.score_of(f) for s in students if s.score_of(f) > 0]
        if not vals:
            continue
        avg = sum(vals) / len(vals)
        std = math.sqrt(sum((v - avg) ** 2 for v in vals) / len(vals))
        for st in students:
            v = st.score_of(f)
            if v < 0:
                rep.anomalies.append(f"学生 {st.name} {f} 分为负: {v}")
            elif v > 200:
                rep.anomalies.append(f"学生 {st.name} {f} 分过高: {v}")
            elif std > 0 and abs(v - avg) > 4 * std and v > 0:
                rep.anomalies.append(
                    f"学生 {st.name} {f} 分异常: {v}（均值 {avg:.1f}, 4σ外）")
    # 4. 总分与单科和不一致
    for st in students:
        if "总分" in st.scores and st.scores:
            subjects = sum(v for k, v in st.scores.items() if k != "总分")
            if subjects > 0 and abs(subjects - st.scores["总分"]) > 0.5:
                rep.anomalies.append(
                    f"学生 {st.name} 总分 {st.scores['总分']} 与单科和 {subjects} 不一致")
    # 5. 预设班级越界
    for st in students:
        if st.preset_class is not None and st.preset_class < 1:
            rep.anomalies.append(f"学生 {st.name} 预设班级非法: {st.preset_class}")
    return rep


# =================== 多次考试综合均衡 ===================
def merge_multi_exam(records: list[list[Student]],
                     weights: list[float]) -> list[Student]:
    """将多次考试的学生数据合并为综合分

    records: 每次考试的学生列表（按 sid 对齐）
    weights: 每次考试的权重
    返回：带综合分的学生列表（scores 中的字段为加权后值）
    """
    if not records:
        return []
    if len(weights) < len(records):
        weights = weights + [1.0] * (len(records) - len(weights))
    total_w = sum(weights[:len(records)])
    if total_w == 0:
        total_w = 1.0
    # 以第一次为基础
    merged: list[Student] = []
    for sid in range(len(records[0])):
        base = records[0][sid]
        st = Student(
            sid=sid, name=base.name,
            preset_class=base.preset_class,
            raw=base.raw,
        )
        # 收集所有分数字段
        all_fields: set[str] = set()
        for rec in records:
            all_fields.update(rec[sid].scores.keys())
        for f in all_fields:
            s = 0.0
            for i, rec in enumerate(records):
                v = rec[sid].scores.get(f, 0.0)
                s += v * weights[i]
            st.scores[f] = s / total_w * len(records)  # 归一化到单次量级
        # 类别取第一次
        st.categories = dict(base.categories)
        st.texts = dict(base.texts)
        merged.append(st)
    return merged
