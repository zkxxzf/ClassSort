# -*- coding: utf-8 -*-
# ============================================================
# 版权所有 (C) 2026 @zkxxzf
# 教务分班软件 ClassSort
# 版本: 1.13 | 日期: 2026-07-28 | 作者: @zkxxzf
# ============================================================
"""约束管理：预设班级 / 互斥分散 / 同班捆绑 / 姓名同音分散"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import random

from .models import Student
try:
    from pypinyin import lazy_pinyin, Style
    _HAS_PYPINYIN = True
except Exception:
    _HAS_PYPINYIN = False


@dataclass
class BindingGroup:
    """同班捆绑组：组内学生必须分到同一班"""
    sids: list[int]
    target_class: Optional[int] = None  # 可选：捆绑到指定班

    def representative(self) -> int:
        return self.sids[0]


@dataclass
class MutexGroup:
    """互斥分散组：组内学生必须分到不同班"""
    sids: list[int]


@dataclass
class NameConflict:
    """姓名同音/相近冲突组：分散到不同班"""
    sids: list[int]


@dataclass
class ConstraintReport:
    satisfied: bool
    violations: list[str] = field(default_factory=list)


def _pinyin_key(name: str) -> str:
    """取姓名的拼音串（去声调、去空格），用于读音相近判断"""
    name = (name or "").replace(" ", "").strip()
    if not name:
        return ""
    if _HAS_PYPINYIN:
        try:
            py = lazy_pinyin(name, style=Style.NORMAL, errors="ignore")
            return "".join(py).lower()
        except Exception:
            pass
    # 退化为字符本身
    return name.lower()


class ConstraintManager:
    """约束管理器"""

    def __init__(self):
        self.bindings: list[BindingGroup] = []
        self.mutexes: list[MutexGroup] = []
        self.name_conflicts: list[NameConflict] = []
        # sid -> group 索引，便于快速查询
        self._binding_of: dict[int, int] = {}
        self._mutex_of: dict[int, list[int]] = {}
        self._name_of: dict[int, list[int]] = {}

    # —— 添加约束 ——
    def add_binding(self, sids: list[int], target_class: Optional[int] = None):
        if len(sids) < 2:
            return
        g = BindingGroup(sids=sids, target_class=target_class)
        idx = len(self.bindings)
        self.bindings.append(g)
        for s in sids:
            self._binding_of[s] = idx

    def add_mutex(self, sids: list[int]):
        if len(sids) < 2:
            return
        g = MutexGroup(sids=sids)
        idx = len(self.mutexes)
        self.mutexes.append(g)
        for s in sids:
            self._mutex_of.setdefault(s, []).append(idx)

    def detect_name_conflicts(self, students: list[Student]):
        """自动检测姓名读音相同/相近的学生，构建冲突组"""
        self.name_conflicts.clear()
        self._name_of.clear()
        buckets: dict[str, list[int]] = {}
        for st in students:
            key = _pinyin_key(st.name)
            if not key:
                continue
            buckets.setdefault(key, []).append(st.sid)
        idx = 0
        for key, sids in buckets.items():
            if len(sids) < 2:
                continue
            self.name_conflicts.append(NameConflict(sids=sids))
            for s in sids:
                self._name_of.setdefault(s, []).append(idx)
            idx += 1

    def add_name_conflict(self, sids: list[int]):
        if len(sids) < 2:
            return
        idx = len(self.name_conflicts)
        self.name_conflicts.append(NameConflict(sids=sids))
        for s in sids:
            self._name_of.setdefault(s, []).append(idx)

    # —— 约束检查 ——
    def binding_of(self, sid: int) -> Optional[BindingGroup]:
        idx = self._binding_of.get(sid)
        return self.bindings[idx] if idx is not None else None

    def mutex_groups_of(self, sid: int) -> list[MutexGroup]:
        return [self.mutexes[i] for i in self._mutex_of.get(sid, [])]

    def name_groups_of(self, sid: int) -> list[NameConflict]:
        return [self.name_conflicts[i] for i in self._name_of.get(sid, [])]

    def allowed_classes(self, sid: int, num_classes: int,
                        assignment: dict[int, int]) -> set[int]:
        """计算某学生允许分配的班级集合（基于互斥/姓名冲突约束）"""
        allowed = set(range(num_classes))
        # 互斥：同组学生所在班级不可去
        for g in self.mutex_groups_of(sid):
            for s in g.sids:
                if s == sid:
                    continue
                c = assignment.get(s)
                if c is not None and c in allowed:
                    allowed.discard(c)
        # 姓名冲突：同音学生所在班级不可去
        for g in self.name_groups_of(sid):
            for s in g.sids:
                if s == sid:
                    continue
                c = assignment.get(s)
                if c is not None and c in allowed:
                    allowed.discard(c)
        return allowed

    def validate(self, students: list[Student], num_classes: int) -> ConstraintReport:
        rep = ConstraintReport(satisfied=True)
        assignment = {st.sid: st.assigned_class for st in students
                      if st.assigned_class is not None}
        # 捆绑
        for g in self.bindings:
            cs = {assignment.get(s) for s in g.sids if s in assignment}
            if len(cs) > 1:
                rep.satisfied = False
                rep.violations.append(
                    f"捆绑组 {g.sids} 被分到不同班: {cs}")
            if g.target_class is not None:
                for s in g.sids:
                    if assignment.get(s) is not None and assignment[s] != g.target_class:
                        rep.satisfied = False
                        rep.violations.append(
                            f"捆绑组 {g.sids} 未绑到指定班 {g.target_class}")
        # 互斥
        for g in self.mutexes:
            cs = [assignment.get(s) for s in g.sids if s in assignment]
            if len(cs) != len(set(cs)):
                rep.satisfied = False
                rep.violations.append(f"互斥组 {g.sids} 有同班: {cs}")
        # 姓名冲突
        for g in self.name_conflicts:
            cs = [assignment.get(s) for s in g.sids if s in assignment]
            if len(cs) != len(set(cs)):
                rep.satisfied = False
                rep.violations.append(f"姓名冲突组 {g.sids} 有同班: {cs}")
        # 预设班级
        for st in students:
            if st.preset_class is not None and st.assigned_class is not None:
                if st.assigned_class != st.preset_class - 1:
                    rep.satisfied = False
                    rep.violations.append(
                        f"学生 {st.name}({st.sid}) 预设 {st.preset_class} 班，"
                        f"实际 {st.assigned_class + 1} 班")
        return rep

    def hard_constraint_score(self, assignment: dict[int, int],
                              num_classes: int) -> float:
        """计算硬约束违反度（越低越好，0 表示完全满足）"""
        pen = 0.0
        # 捆绑
        for g in self.bindings:
            cs = [assignment.get(s) for s in g.sids if s in assignment]
            if len(set(cs)) > 1:
                pen += 1000.0 * (len(set(cs)) - 1)
            if g.target_class is not None:
                for s in g.sids:
                    if assignment.get(s) is not None and assignment[s] != g.target_class:
                        pen += 1000.0
        # 互斥
        for g in self.mutexes:
            cs = [assignment.get(s) for s in g.sids if s in assignment]
            if len(cs) != len(set(cs)):
                pen += 500.0 * (len(cs) - len(set(cs)))
        # 姓名冲突
        for g in self.name_conflicts:
            cs = [assignment.get(s) for s in g.sids if s in assignment]
            if len(cs) != len(set(cs)):
                pen += 200.0 * (len(cs) - len(set(cs)))
        return pen
