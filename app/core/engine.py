# -*- coding: utf-8 -*-
# ============================================================
# 版权所有 (C) 2026 @zkxxzf
# 教务分班软件 ClassSort
# 版本: 1.13 | 日期: 2026-07-28 | 作者: @zkxxzf
# ============================================================
"""分班核心引擎

算法分层：
1. 预处理：约束构建、预设班级、捆绑代表、班型筛选
2. 初始分配：蛇形分配（snake draft）保证分段均衡
   - 延迟处理姓名冲突学生，避免破坏蛇形
3. 定向优化：高均分班↔低均分班定向换班（高效降低偏差）
4. 全局贪心：成对换班，最小化加权偏差成本
5. 模拟退火：跳出局部最优
6. 终局微调：逼近 0.01 均分偏差
"""
from __future__ import annotations
import random
import math
from dataclasses import dataclass
from typing import Optional, Callable

from .models import (Student, ClassRoom, TaskConfig, SegmentRule, ClassTypeSpec)
from .constraints import ConstraintManager, BindingGroup


@dataclass
class EngineStats:
    """分班结果统计"""
    iterations: int = 0
    final_cost: float = 0.0
    total_dev: float = 0.0
    subject_dev: float = 0.0
    segment_dev: int = 0
    category_dev: int = 0
    size_dev: int = 0
    pointwise_dev: int = 0
    hard_penalty: float = 0.0
    converged: bool = False
    # 班型分组统计（按班型分别计算偏差）
    group_stats: list[dict] = None  # [{name, classes, total_dev, subject_dev, ...}]


class _ClassStats:
    """班级实时统计缓存，支持增量更新"""
    __slots__ = ("n", "cnt", "sums", "cat", "seg", "seg_of")

    def __init__(self, num_classes: int, score_fields: list[str],
                 category_fields: list[str], seg_of: Optional[dict] = None):
        self.n = num_classes
        self.cnt = [0] * num_classes
        self.sums = {f: [0.0] * num_classes for f in score_fields}
        self.cat = {f: [{} for _ in range(num_classes)] for f in category_fields}
        self.seg_of = seg_of or {}
        self.seg = [[0] * 3 for _ in range(num_classes)] if seg_of else None

    def add(self, st: Student, c: int, score_fields: list[str]):
        self.cnt[c] += 1
        for f in score_fields:
            self.sums[f][c] += st.score_of(f)
        for f, v in st.categories.items():
            d = self.cat.setdefault(f, [{} for _ in range(self.n)])
            d[c][v] = d[c].get(v, 0) + 1
        if self.seg is not None and st.sid in self.seg_of:
            self.seg[c][self.seg_of[st.sid]] += 1

    def remove(self, st: Student, c: int, score_fields: list[str]):
        self.cnt[c] -= 1
        for f in score_fields:
            self.sums[f][c] -= st.score_of(f)
        for f, v in st.categories.items():
            d = self.cat.get(f)
            if d and v in d[c]:
                d[c][v] -= 1
                if d[c][v] <= 0:
                    del d[c][v]
        if self.seg is not None and st.sid in self.seg_of:
            self.seg[c][self.seg_of[st.sid]] -= 1

    def means(self, f: str) -> list[float]:
        return [self.sums[f][c] / self.cnt[c] if self.cnt[c] else 0.0
                for c in range(self.n)]

    def dev(self, f: str) -> float:
        m = self.means(f)
        return max(m) - min(m) if m else 0.0

    def dev_in_group(self, f: str, group: list[int]) -> float:
        """计算某字段在指定班级组内的均分偏差"""
        m = self.means(f)
        gm = [m[c] for c in group if self.cnt[c] > 0]
        return max(gm) - min(gm) if gm else 0.0

    def size_dev(self) -> int:
        return max(self.cnt) - min(self.cnt) if self.cnt else 0

    def size_dev_in_group(self, group: list[int]) -> int:
        gc = [self.cnt[c] for c in group]
        return max(gc) - min(gc) if gc else 0

    def category_dev(self) -> int:
        """计算类别人数最大偏差（跨班级、跨类别值）"""
        max_dev = 0
        for f, class_dicts in self.cat.items():
            all_values: set[str] = set()
            for d in class_dicts:
                all_values.update(d.keys())
            for v in all_values:
                counts = [d.get(v, 0) for d in class_dicts]
                dev = max(counts) - min(counts)
                if dev > max_dev:
                    max_dev = dev
        return max_dev

    def category_dev_in_group(self, group: list[int]) -> int:
        """计算指定班级组内的类别人数最大偏差"""
        max_dev = 0
        for f, class_dicts in self.cat.items():
            all_values: set[str] = set()
            for c in group:
                all_values.update(class_dicts[c].keys())
            for v in all_values:
                counts = [class_dicts[c].get(v, 0) for c in group]
                dev = max(counts) - min(counts)
                if dev > max_dev:
                    max_dev = dev
        return max_dev

    def segment_dev(self) -> int:
        """计算分段人数最大偏差（O(1) 增量缓存）"""
        if self.seg is None:
            return 0
        return max(max(self.seg[c][s] for c in range(self.n)) -
                   min(self.seg[c][s] for c in range(self.n))
                   for s in range(3))

    def segment_dev_in_group(self, group: list[int]) -> int:
        """计算指定班级组内的分段人数最大偏差"""
        if self.seg is None:
            return 0
        return max(max(self.seg[c][s] for c in group) -
                   min(self.seg[c][s] for c in group)
                   for s in range(3))


class BalanceEngine:
    """分班引擎"""

    def __init__(self, students: list[Student], config: TaskConfig,
                 constraints: Optional[ConstraintManager] = None,
                 progress_cb: Optional[Callable[[int, int, str], None]] = None):
        self.students = students
        self.config = config
        self.constraints = constraints or ConstraintManager()
        self.progress_cb = progress_cb
        self.rng = random.Random(config.seed)
        self.num_classes = config.num_classes
        self.score_fields: list[str] = []
        self.category_fields: list[str] = []
        self._scan_fields()
        self._class_type_filter: dict[int, set[int]] = {}
        self._class_type_of: dict[int, str] = {}  # sid -> 班型名
        self._stats: Optional[_ClassStats] = None
        # 缓存：按总分排序的 sid 列表 & 分段映射（不随 assignment 变化）
        self._sorted_sids: list[int] = []
        self._auto_seg_of: dict[int, int] = {}
        self._rebuild_score_cache()

    def _scan_fields(self):
        seen_s, seen_c = set(), set()
        for st in self.students:
            for k in st.scores:
                if k not in seen_s:
                    seen_s.add(k); self.score_fields.append(k)
            for k in st.categories:
                if k not in seen_c:
                    seen_c.add(k); self.category_fields.append(k)
        # 总分置于首位
        if "总分" in seen_s:
            self.score_fields.remove("总分")
        else:
            self.score_fields.append("总分")
        self.score_fields.insert(0, "总分")

    def _rebuild_score_cache(self):
        """预计算按总分排序的 sid 列表 & 自动分段映射 & 最小可达偏差"""
        ordered = sorted(self.students, key=lambda s: -s.total_score)
        self._sorted_sids = [s.sid for s in ordered]
        total = len(ordered)
        c1, c2 = total // 3, total * 2 // 3
        self._auto_seg_of = {}
        for rank, st in enumerate(ordered, start=1):
            self._auto_seg_of[st.sid] = 0 if rank <= c1 else (1 if rank <= c2 else 2)
        # 计算各字段最小可达均分偏差（基于实际分数精度、半整数分布和班级人数）
        n = self.num_classes
        base_size = total // n if total else 1
        self._min_dev: dict[str, float] = {}
        for f in self.score_fields:
            total_sum = sum(s.score_of(f) for s in self.students)
            per_class = total_sum / n
            # 检测该字段的实际分数精度（0.5 或 1.0）并统计半整数分数数量
            precision = 0.5
            half_count = 0
            all_int = True
            for s in self.students:
                v = s.score_of(f)
                frac = v - int(v)
                if abs(frac) < 1e-9:
                    continue
                all_int = False
                if abs(frac - 0.5) < 1e-9:
                    half_count += 1
                    precision = 0.5
                else:
                    precision = 0.25
            if all_int:
                precision = 1.0
            # 判断目标是否可达：
            # 1. per_class 必须是 precision 的倍数
            # 2. 若 per_class 含非零小数部分（如 X.5），每班至少需1个半整数分数学生
            scaled = per_class / precision
            is_multiple = abs(scaled - round(scaled)) < 1e-9
            target_has_fraction = abs(per_class - round(per_class)) > 1e-9
            if is_multiple and (not target_has_fraction or half_count >= n):
                self._min_dev[f] = 0.0
            elif is_multiple and target_has_fraction and half_count < n:
                # 目标是 precision 的倍数但含小数部分，且半整数分数不足
                # 至少 n-half_count 个班只能取整数和，它们之间至少差 1.0
                self._min_dev[f] = 1.0 / base_size
            else:
                self._min_dev[f] = precision / base_size

    def _total_score_of(self, sid: int) -> float:
        """快速取总分"""
        return self.students[sid].total_score

    def _apply_class_types(self):
        """班型筛选：按优先级分配学生到班型，处理同分边界

        优先级：rank_floor 越小优先级越高（重点班 > 卓越班 > 普通班）
        同分边界：rank_ceil 处若有同分，同分学生全部进入该班型
        每个学生只分配到一个班型（第一个匹配的）
        未被任何班型覆盖的学生 → 只能去未被任何班型占用的班级（普通班）
        """
        self._class_type_filter.clear()
        self._class_type_of: dict[int, str] = {}  # sid -> 班型名
        if not self.config.class_types:
            return
        # 预计算年级排名（按总分降序，1-based）
        ordered = sorted(self.students, key=lambda s: -s.total_score)
        rank_map: dict[int, int] = {}
        for rank, st in enumerate(ordered, start=1):
            rank_map[st.sid] = rank
        # 按优先级排序：rank_floor 升序（前名次优先），无 rank 的按 score_floor
        sorted_specs = sorted(
            self.config.class_types,
            key=lambda s: (s.rank_floor if s.rank_floor is not None else 999999,
                           s.score_floor if s.score_floor is not None else -1)
        )
        assigned_sids: set[int] = set()
        # 收集所有被班型覆盖的班级
        covered_classes: set[int] = set()
        for spec in sorted_specs:
            covered_classes.update(spec.class_indices)
        for spec in sorted_specs:
            # 计算有效 rank_ceil（含同分扩展）
            r_floor = spec.rank_floor if spec.rank_floor is not None else 1
            r_ceil = spec.rank_ceil if spec.rank_ceil is not None else len(ordered)
            # 同分扩展：rank_ceil 位置若有同分，扩展到包含所有同分学生
            if spec.rank_ceil is not None and r_ceil < len(ordered):
                ceil_score = ordered[r_ceil - 1].total_score
                while r_ceil < len(ordered) and ordered[r_ceil].total_score == ceil_score:
                    r_ceil += 1
            floor = spec.score_floor if spec.score_floor is not None else -math.inf
            ceil = spec.score_ceil if spec.score_ceil is not None else math.inf
            for st in self.students:
                if st.sid in assigned_sids:
                    continue
                rank = rank_map.get(st.sid, 0)
                if not (r_floor <= rank <= r_ceil):
                    continue
                if not (floor <= st.total_score < ceil):
                    continue
                self._class_type_filter[st.sid] = set(spec.class_indices)
                self._class_type_of[st.sid] = spec.name
                assigned_sids.add(st.sid)
        # 未被任何班型覆盖的学生 → 只能去未被任何班型占用的班级
        # （这样设置后，重点班学生只能进重点班，普通学生只能进普通班）
        if covered_classes and len(covered_classes) < self.num_classes:
            unassigned_classes = set(range(self.num_classes)) - covered_classes
            for st in self.students:
                if st.sid not in assigned_sids:
                    self._class_type_filter[st.sid] = set(unassigned_classes)
                    self._class_type_of[st.sid] = "普通班"

    def _class_type_groups(self) -> list[list[int]]:
        """返回班型分组列表，每组是同班型的班级索引列表。
        无班型时返回 [[0,1,...,n-1]]（全部一班）。"""
        if not self.config.class_types:
            return [list(range(self.num_classes))]
        groups: list[list[int]] = []
        seen: set[tuple] = set()
        for spec in self.config.class_types:
            key = tuple(sorted(spec.class_indices))
            if key not in seen:
                groups.append(list(spec.class_indices))
                seen.add(key)
        # 未被班型覆盖的班级归为一组（普通班）
        covered: set[int] = set()
        for g in groups:
            covered.update(g)
        uncovered = [c for c in range(self.num_classes) if c not in covered]
        if uncovered:
            groups.append(uncovered)
        return groups

    def _group_of_sid(self, sid: int) -> int:
        """返回学生所属的班型组索引"""
        allowed = self._class_type_filter.get(sid)
        if not allowed:
            return 0
        groups = self._class_type_groups()
        for gi, group in enumerate(groups):
            if set(group) == allowed:
                return gi
        # 回退：找有交集的组
        for gi, group in enumerate(groups):
            if set(group) & allowed:
                return gi
        return 0

    def _rebuild_group_segments(self):
        """为每个班型组单独计算分段映射（高/中/低三段）

        按班型组分别分段：重点班内部三段、普通班内部三段，互不干扰。
        """
        self._group_seg_of: dict[int, int] = {}
        # 记录各组的分段切点（用于日志/显示）
        self._group_seg_bounds: dict[int, tuple] = {}  # group_idx -> (c1, c2, total, lo_score, mid_score)
        groups = self._class_type_groups()
        if len(groups) <= 1:
            # 无班型，使用全局分段
            self._group_seg_of = dict(self._auto_seg_of)
            # 记录全局分段边界分数
            ordered = sorted(self.students, key=lambda s: -s.total_score)
            total = len(ordered)
            c1, c2 = total // 3, total * 2 // 3
            hi_score = ordered[c1 - 1].total_score if c1 > 0 and c1 <= total else 0.0
            mid_score = ordered[c2 - 1].total_score if c2 > 0 and c2 <= total else 0.0
            self._group_seg_bounds[0] = (c1, c2, total, hi_score, mid_score)
            return
        for gi, group in enumerate(groups):
            # 收集属于该组的学生（用 _class_type_filter 判断更准确）
            group_class_set = set(group)
            group_sids = []
            for st in self.students:
                allowed = self._class_type_filter.get(st.sid)
                if allowed is not None and allowed == group_class_set:
                    group_sids.append(st.sid)
                elif allowed is None and not group_class_set:
                    group_sids.append(st.sid)
            if not group_sids:
                continue
            group_sids.sort(key=lambda sid: -self.students[sid].total_score)
            total = len(group_sids)
            c1, c2 = total // 3, total * 2 // 3
            # 记录分段边界分数（用于显示）
            hi_score = group_sids[c1 - 1] if c1 > 0 else None
            mid_score = group_sids[c2 - 1] if c2 > 0 else None
            hi_score_val = self.students[hi_score].total_score if hi_score is not None else 0.0
            mid_score_val = self.students[mid_score].total_score if mid_score is not None else 0.0
            self._group_seg_bounds[gi] = (c1, c2, total, hi_score_val, mid_score_val)
            for rank, sid in enumerate(group_sids, start=1):
                seg = 0 if rank <= c1 else (1 if rank <= c2 else 2)
                self._group_seg_of[sid] = seg

    # —— 主入口 ——
    def run(self) -> tuple[list[ClassRoom], EngineStats]:
        self._apply_class_types()
        self._rebuild_group_segments()
        assignment: dict[int, int] = {}
        self._assign_preset(assignment)
        self._assign_bindings(assignment)
        self._snake_draft(assignment)
        # 构建统计缓存（传入分段映射，支持 O(1) 分段偏差计算）
        # 使用组分段映射（按班型分组分段），若无班型则等同于全局分段
        self._stats = _ClassStats(self.num_classes, self.score_fields,
                                  self.category_fields, self._group_seg_of)
        for st in self.students:
            self._stats.add(st, assignment[st.sid], self.score_fields)
        # 优化
        stats = self._optimize(assignment)
        classes = self._build_classes(assignment)
        return classes, stats

    def _assign_preset(self, assignment: dict[int, int]):
        for st in self.students:
            if st.preset_class is not None:
                target = st.preset_class - 1
                if 0 <= target < self.num_classes:
                    # 检查班型筛选：预设班级必须在学生允许的班型范围内
                    if st.sid in self._class_type_filter and target not in self._class_type_filter[st.sid]:
                        # 预设与班型冲突，跳过（后续蛇形分配会按班型正确分配）
                        continue
                    assignment[st.sid] = target
                    st.assigned_class = target

    def _assign_bindings(self, assignment: dict[int, int]):
        for g in self.constraints.bindings:
            rep = g.representative()
            if g.target_class is not None:
                target = g.target_class
                # 检查班型筛选：捆绑目标班必须在所有组员允许的班型范围内
                conflict = False
                for s in g.sids:
                    if s in self._class_type_filter and target not in self._class_type_filter[s]:
                        conflict = True
                        break
                if conflict:
                    target = self._pick_class_for_group(g.sids, assignment)
            elif rep in assignment:
                target = assignment[rep]
            else:
                target = self._pick_class_for_group(g.sids, assignment)
            for s in g.sids:
                assignment[s] = target
                self.students[s].assigned_class = target

    def _pick_class_for_group(self, sids: list[int],
                              assignment: dict[int, int]) -> int:
        candidates = set(range(self.num_classes))
        for s in sids:
            if s in self._class_type_filter:
                candidates &= self._class_type_filter[s]
            allowed = self.constraints.allowed_classes(
                s, self.num_classes, assignment)
            candidates &= allowed
        if not candidates:
            candidates = set(range(self.num_classes))
        cnt = {c: 0 for c in range(self.num_classes)}
        for c in assignment.values():
            cnt[c] = cnt.get(c, 0) + 1
        return min(candidates, key=lambda c: (cnt[c], c))

    def _snake_draft(self, assignment: dict[int, int]):
        """蛇形分配：按总分降序，每轮按 1..n / n..1 顺序填入班级

        核心保护：最后一轮若不完整（学生数不能被班数整除），
        按当前人数最少优先分配，避免最低分学生集中到某班。
        """
        unassigned = [st for st in self.students if st.sid not in assignment]
        unassigned.sort(key=lambda s: (-s.total_score, self.rng.random()))
        n = self.num_classes
        targets = self._compute_targets()
        cnt = [0] * n
        for c in assignment.values():
            cnt[c] += 1

        def assign_one(st: Student, c: int):
            assignment[st.sid] = c
            st.assigned_class = c
            cnt[c] += 1

        # 按 n 个一组分轮
        num_full_rounds = len(unassigned) // n
        has_partial = len(unassigned) % n > 0
        total_rounds = num_full_rounds + (1 if has_partial else 0)

        for round_idx in range(total_rounds):
            start = round_idx * n
            end = min(start + n, len(unassigned))
            round_students = unassigned[start:end]
            is_partial = len(round_students) < n

            if is_partial:
                # 最后一轮不完整：按当前人数最少优先（保证最低分学生均匀分散）
                order = sorted(range(n), key=lambda c: (cnt[c], c))
            elif round_idx % 2 == 0:
                order = list(range(n))
            else:
                order = list(range(n - 1, -1, -1))

            for st, preferred_c in zip(round_students, order):
                # 优先尝试蛇形指定的班
                if (cnt[preferred_c] < targets[preferred_c] and
                        preferred_c in self._allowed_for(st, assignment)):
                    assign_one(st, preferred_c)
                    continue
                # 回退：在允许的班里选人数最少的
                allowed = self._allowed_for(st, assignment)
                candidates = [c for c in allowed if cnt[c] < targets[c]]
                if not candidates:
                    candidates = list(allowed) or list(range(n))
                best = min(candidates, key=lambda c: (cnt[c], c))
                assign_one(st, best)

        # 兜底：极端约束冲突未分配
        remaining = [st for st in unassigned if st.sid not in assignment]
        for st in remaining:
            allowed = self._allowed_for(st, assignment) or set(range(n))
            best = min(allowed, key=lambda c: (cnt[c], c))
            assign_one(st, best)

    def _allowed_for(self, st: Student, assignment: dict[int, int]) -> set[int]:
        allowed = set(range(self.num_classes))
        if st.sid in self._class_type_filter:
            allowed &= self._class_type_filter[st.sid]
        allowed &= self.constraints.allowed_classes(
            st.sid, self.num_classes, assignment)
        return allowed

    def _compute_targets(self) -> list[int]:
        """计算各班目标人数。支持班型平分和自定义大小班。

        班型逻辑：同一班型的班级平分该班型范围内的学生。
        例如重点班(1,2班)前100名 → 1班50人、2班50人
        """
        total = len(self.students)
        # 检查是否有自定义人数
        custom = getattr(self.config, 'custom_sizes', None)
        if custom and len(custom) >= self.num_classes and any(v for v in custom[:self.num_classes]):
            targets = []
            fixed_sum = 0
            unfixed_indices = []
            for i in range(self.num_classes):
                v = custom[i] if i < len(custom) else None
                if v is not None:
                    targets.append(v)
                    fixed_sum += v
                else:
                    targets.append(0)
                    unfixed_indices.append(i)
            if unfixed_indices:
                remaining = total - fixed_sum
                base = remaining // len(unfixed_indices)
                rem = remaining % len(unfixed_indices)
                for idx, i in enumerate(unfixed_indices):
                    targets[i] = base + (1 if idx < rem else 0)
            return targets
        # 班型感知：按班型分组计算目标人数
        if self.config.class_types:
            # 统计每个班型分配的学生数（含自动归入普通班的学生）
            type_students: dict[tuple, int] = {}  # (class_indices tuple) -> count
            for sid, allowed in self._class_type_filter.items():
                key = tuple(sorted(allowed))
                type_students[key] = type_students.get(key, 0) + 1
            # 收集所有被班型覆盖的班级（含普通班）
            assigned_classes: set[int] = set()
            for key in type_students:
                assigned_classes.update(key)
            # 计算各班目标
            targets = [0] * self.num_classes
            unassigned_count = total - sum(type_students.values())
            unassigned_classes = [c for c in range(self.num_classes) if c not in assigned_classes]
            # 班型班级：平分该班型的学生
            for key, count in type_students.items():
                classes = list(key)
                base = count // len(classes)
                rem = count % len(classes)
                for idx, c in enumerate(classes):
                    targets[c] = base + (1 if idx < rem else 0)
            # 非班型班级：平分剩余学生
            if unassigned_classes:
                base = unassigned_count // len(unassigned_classes)
                rem = unassigned_count % len(unassigned_classes)
                for idx, c in enumerate(unassigned_classes):
                    targets[c] = base + (1 if idx < rem else 0)
            return targets
        # 默认：均匀分配
        base = total // self.num_classes
        rem = total % self.num_classes
        return [base + (1 if c < rem else 0) for c in range(self.num_classes)]

    def _build_classes(self, assignment: dict[int, int]) -> list[ClassRoom]:
        classes = [ClassRoom(index=i, name=f"{i+1}班")
                   for i in range(self.num_classes)]
        for st in self.students:
            c = assignment.get(st.sid, 0)
            classes[c].students.append(st)
        return classes

    # =================== 优化 ===================
    def _optimize(self, assignment: dict[int, int]) -> EngineStats:
        stats = EngineStats()
        best_cost = self._total_cost(assignment)
        best_assignment = dict(assignment)
        max_iter = self.config.max_iterations if self.config.max_iterations > 0 else 300
        no_improve = 0

        for it in range(max_iter):
            stats.iterations = it + 1
            # 1. 定向优化：针对最大偏差的班对
            self._targeted_swap(assignment)
            # 2. 单人移班（针对班级人数不均/极端分值）
            self._single_move(assignment)
            # 3. 贪心换班
            self._greedy_swap(assignment)
            # 4. 终局微调：纯均分优化（后段启动）
            if it >= max_iter // 2:
                self._mean_polish(assignment)
            cost = self._total_cost(assignment)
            if cost < best_cost - 1e-9:
                best_cost = cost
                best_assignment = dict(assignment)
                no_improve = 0
            else:
                no_improve += 1
            if self.progress_cb and (it % 20 == 0 or it < 5):
                self.progress_cb(it + 1, max_iter,
                                 f"成本={cost:.2f} 最佳={best_cost:.2f}")
            if self._meets_tolerance(assignment, self.config.tol_mean):
                stats.converged = True
                if self.progress_cb:
                    self.progress_cb(max_iter, max_iter, "已达到容差目标")
                break
            # 退火：从最佳解出发扰动，避免破坏已有进展
            if no_improve >= 30:
                assignment.clear()
                assignment.update(best_assignment)
                self._resync_stats(assignment)
                self._anneal(assignment, temperature=max(0.5, 3.0 - it * 0.005))
                no_improve = 0

        # 恢复最佳
        assignment.clear()
        assignment.update(best_assignment)
        self._resync_stats(assignment)

        # ===== 迭代式后处理：约束满足 + 均分精调 =====
        # 目标：cat_dev≤1, seg_dev≤1, 均分偏差≤tol 同时满足
        # 策略：反复执行 [类别均衡 → 分段均衡 → 均分精调]，直到收敛
        if self.progress_cb:
            self.progress_cb(0, 10, "后处理：约束满足+均分精调")
        for post_round in range(10):
            cat_before = self._stats.category_dev()
            seg_before = self._stats.segment_dev()
            mean_before = max(self._stats.dev(f) for f in self.score_fields) if self.score_fields else 0.0

            # 1. 类别均衡
            self._category_balance(assignment)
            cat_limit = max(self._stats.category_dev(), 1)

            # 2. 分段均衡
            self._segment_balance(assignment, cat_limit)

            # 3. 针对性单科均衡（允许总分临时波动，放宽约束以突破 pairwise 限制）
            seg_limit = max(self._stats.segment_dev(), 2)
            self._targeted_subject_polish(assignment, cat_limit, seg_limit)

            # 3.5 3-way 循环交换（突破 pairwise 局限，消除 0.5 分差）
            self._three_way_polish(assignment, cat_limit, seg_limit)

            # 4. 均分精调（修复总分波动 + 进一步优化所有字段）
            self._final_mean_polish(assignment, cat_limit, seg_limit)

            cat_after = self._stats.category_dev()
            seg_after = self._stats.segment_dev()
            mean_after = max(self._stats.dev(f) for f in self.score_fields) if self.score_fields else 0.0

            if self.progress_cb:
                self.progress_cb(post_round + 1, 10,
                                 f"cat={cat_after} seg={seg_after} mean_dev={mean_after:.4f}")

            # 收敛判定：所有指标达标 或 无改进
            all_ok = (cat_after <= 1 and seg_after <= 1 and
                      mean_after <= self.config.tol_mean + 1e-9)
            no_progress = (cat_after >= cat_before and seg_after >= seg_before and
                           mean_after >= mean_before - 1e-9)
            if all_ok or no_progress:
                break

        # 更新最佳
        best_assignment = dict(assignment)
        best_cost = self._total_cost(assignment)
        for st in self.students:
            st.assigned_class = assignment[st.sid]
        self._fill_stats(assignment, stats)
        stats.final_cost = best_cost
        return stats

    def _single_move(self, assignment: dict[int, int]):
        """单人移班：尝试把每个学生移到其他班，接受成本下降的移动。
        约束：移动后班级人数偏差不得超过 tol_count。
        """
        n = self.num_classes
        ordered = sorted(self.students,
                         key=lambda s: abs(s.total_score - self._grand_mean()))
        base_cost = self._total_cost(assignment)
        cnt = list(self._stats.cnt)
        targets = self._compute_targets()
        cur_size_dev = max(cnt) - min(cnt)
        for st in ordered[:40]:
            c_from = assignment[st.sid]
            for c_to in range(n):
                if c_to == c_from:
                    continue
                # 严格人数约束：移动后 size_dev 不得超过 tol_count
                new_cnt_from = cnt[c_from] - 1
                new_cnt_to = cnt[c_to] + 1
                new_max = max(new_cnt_from, new_cnt_to, max(cnt[c] for c in range(n) if c not in (c_from, c_to)))
                new_min = min(new_cnt_from, new_cnt_to, min(cnt[c] for c in range(n) if c not in (c_from, c_to)))
                if new_max - new_min > self.config.tol_count:
                    continue
                if not self._can_move(st.sid, c_to, assignment):
                    continue
                self._stats.remove(st, c_from, self.score_fields)
                assignment[st.sid] = c_to
                self._stats.add(st, c_to, self.score_fields)
                new_cost = self._total_cost(assignment)
                if new_cost < base_cost - 1e-9:
                    base_cost = new_cost
                    cnt[c_from] -= 1
                    cnt[c_to] += 1
                    st.assigned_class = c_to
                else:
                    self._stats.remove(st, c_to, self.score_fields)
                    assignment[st.sid] = c_from
                    self._stats.add(st, c_from, self.score_fields)

    def _grand_mean(self) -> float:
        if not self.students:
            return 0.0
        return sum(s.total_score for s in self.students) / len(self.students)

    def _mean_polish(self, assignment: dict[int, int]):
        """终局微调：尝试所有可行的两班交换，接受总成本下降的换法"""
        n = self.num_classes
        base_cost = self._total_cost(assignment)
        # 对每个分数字段，找最高/最低班对尝试交换
        for f in self.score_fields:
            means = self._stats.means(f)
            order = sorted(range(n), key=lambda c: means[c])
            c_lo, c_hi = order[0], order[-1]
            if means[c_hi] - means[c_lo] <= self.config.tol_mean:
                continue
            hi_sids = sorted(
                [s for s, c in assignment.items() if c == c_hi],
                key=lambda s: -self.students[s].score_of(f))
            lo_sids = sorted(
                [s for s, c in assignment.items() if c == c_lo],
                key=lambda s: self.students[s].score_of(f))
            for sa in hi_sids[:8]:
                for sb in lo_sids[:8]:
                    if not self._can_swap(sa, sb, assignment):
                        continue
                    self._apply_swap(sa, sb, assignment)
                    new_cost = self._total_cost(assignment)
                    if new_cost < base_cost - 1e-9:
                        base_cost = new_cost
                    else:
                        self._apply_swap(sa, sb, assignment)  # 回退

    def _category_balance(self, assignment: dict[int, int], max_passes: int = 10):
        """类别均衡：通过交换学生使各类别各值人数偏差 ≤ 1

        策略：对每个类别字段，找人数偏差 > 1 的类别值，
        从人数最多的班找一个该类别学生，
        与人数最少的班找一个其他类别学生交换。
        """
        n = self.num_classes
        if n < 2 or not self.category_fields:
            return

        for _ in range(max_passes):
            improved = False
            for f in self.category_fields:
                # 统计各类别值在各班的人数
                value_classes: dict[str, list[int]] = {}  # value -> [count per class]
                all_values: set[str] = set()
                for c in range(n):
                    d = self._stats.cat.get(f, [{} for _ in range(n)])
                    for v, cnt in d[c].items():
                        all_values.add(v)
                for v in all_values:
                    value_classes[v] = [self._stats.cat.get(f, [{} for _ in range(n)])[c].get(v, 0)
                                        for c in range(n)]

                for v, counts in value_classes.items():
                    max_c = counts.index(max(counts))
                    min_c = counts.index(min(counts))
                    if counts[max_c] - counts[min_c] <= 1:
                        continue
                    # 在 max_c 找类别 v 的学生
                    hi_sids = [sid for sid, c in assignment.items()
                               if c == max_c and self.students[sid].category_of(f) == v]
                    # 在 min_c 找其他类别的学生
                    lo_sids = [sid for sid, c in assignment.items()
                               if c == min_c and self.students[sid].category_of(f) != v]
                    if not hi_sids or not lo_sids:
                        continue
                    # 尝试交换，接受不恶化总成本太多的
                    base_cost = self._total_cost(assignment)
                    best_swap = None
                    best_cost = base_cost
                    for sa in hi_sids[:20]:
                        for sb in lo_sids[:20]:
                            if not self._can_swap(sa, sb, assignment):
                                continue
                            self._apply_swap(sa, sb, assignment)
                            new_cost = self._total_cost(assignment)
                            # 接受条件：成本不显著增加
                            if new_cost <= base_cost * 1.15:
                                if new_cost < best_cost:
                                    best_cost = new_cost
                                    best_swap = (sa, sb)
                            self._apply_swap(sa, sb, assignment)  # 回退
                    if best_swap:
                        sa, sb = best_swap
                        self._apply_swap(sa, sb, assignment)
                        improved = True
            if not improved:
                break

    def _segment_balance(self, assignment: dict[int, int], cat_limit: int,
                         max_passes: int = 20):
        """分段均衡：通过交换学生使各班各分段人数偏差 ≤ 1

        策略：
        1. 优先同类别交换（类别中性，不破坏类别均衡）
        2. 允许类别临时波动到 cat_limit+1（后续 category_balance 会修复）
        3. 使用缓存的 _stats.seg 进行 O(1) 分段偏差计算
        """
        n = self.num_classes
        seg_of = self._auto_seg_of
        if n < 2 or not seg_of:
            return

        # 构建类别签名，用于优先选择同类别交换
        cat_sig = {}
        if self.category_fields:
            for st in self.students:
                cat_sig[st.sid] = tuple(st.categories.get(f, "") for f in self.category_fields)

        base_seg = self._stats.segment_dev()
        for _ in range(max_passes):
            improved = False
            seg_cnt = self._stats.seg  # 使用缓存

            for s in range(3):
                counts = [seg_cnt[c][s] for c in range(n)]
                max_c = counts.index(max(counts))
                min_c = counts.index(min(counts))
                if counts[max_c] - counts[min_c] <= 1:
                    continue
                # 在 max_c 找分段 s 的学生
                hi_sids = [sid for sid, c in assignment.items()
                           if c == max_c and seg_of.get(sid) == s]
                # 在 min_c 找其他分段的学生
                lo_sids = [sid for sid, c in assignment.items()
                           if c == min_c and seg_of.get(sid) != s]
                if not hi_sids or not lo_sids:
                    continue
                # 按类别签名排序：同类别优先（若 sa/sb 同类别，交换不影响类别均衡）
                if cat_sig:
                    hi_sids.sort(key=lambda sid: cat_sig.get(sid, ()))
                    lo_sids.sort(key=lambda sid: cat_sig.get(sid, ()))
                best_swap = None
                best_seg = base_seg
                for sa in hi_sids[:40]:
                    for sb in lo_sids[:40]:
                        if not self._can_swap(sa, sb, assignment):
                            continue
                        self._apply_swap(sa, sb, assignment)
                        new_seg = self._stats.segment_dev()
                        new_cat = self._stats.category_dev()
                        # 接受条件：分段偏差减少 + 类别不恶化（允许 cat_limit+1 的临时波动）
                        if new_seg < best_seg and new_cat <= cat_limit + 1:
                            best_seg = new_seg
                            best_swap = (sa, sb)
                        self._apply_swap(sa, sb, assignment)  # 回退
                if best_swap:
                    sa, sb = best_swap
                    self._apply_swap(sa, sb, assignment)
                    base_seg = best_seg
                    improved = True
            if not improved:
                break

    def _final_mean_polish(self, assignment: dict[int, int], cat_limit: int,
                           seg_limit: int, max_rounds: int = 60):
        """最终均分优化：最佳改进策略 + 分层约束保持

        核心策略：
        1. 每轮找全局最佳交换（非首个改进），避免贪心陷入局部最优
        2. 成本 = max偏差×1000 + 总偏差，优先降低最差字段
        3. 同分段交换不改变分段人数（分段中性），优先搜索
        4. 同分段+同类别 → 双重中性，仅需均值改善即可接受
        """
        n = self.num_classes
        cfg = self.config
        if n < 2 or not self.score_fields:
            return

        seg_of = self._auto_seg_of
        has_seg = bool(seg_of)

        # 构建类别签名
        cat_sig = {}
        if self.category_fields:
            for st in self.students:
                cat_sig[st.sid] = tuple(st.categories.get(f, "") for f in self.category_fields)

        def dev_cost() -> float:
            """成本 = 超过最小值的偏差×10000 + 偏差平方和×100
            优先将各字段降至其最小可达偏差（按班型组分别计算）"""
            groups = self._class_type_groups()
            cost = 0.0
            for f in self.score_fields:
                min_d = self._min_dev.get(f, 0.0)
                for group in groups:
                    d = self._stats.dev_in_group(f, group)
                    excess = max(0.0, d - min_d - 1e-9)
                    cost += excess * 10000.0 + d * d * 100.0
            return cost

        base_cost = dev_cost()
        for _ in range(max_rounds):
            best_swap = None
            best_cost = base_cost

            # 构建 (班级, 分段) → 学生列表 索引
            by_class_seg: dict[tuple[int, int], list[int]] = {}
            if has_seg:
                for sid, c in assignment.items():
                    s = seg_of.get(sid, 0)
                    by_class_seg.setdefault((c, s), []).append(sid)

            # 对每个字段，找偏差最大的班对（仅在同班型组内比较）
            groups = self._class_type_groups()
            for f in self.score_fields:
                means = self._stats.means(f)
                for group in groups:
                    if len(group) < 2:
                        continue
                    # 组内按均分排序
                    order = sorted(group, key=lambda c: means[c])
                    for i in range(max(1, len(group) // 2)):
                        c_lo = order[i]
                        c_hi = order[-(i + 1)]
                        if means[c_hi] - means[c_lo] <= cfg.tol_mean * 0.1:
                            continue

                        # Layer 1+2: 同分段交换（分段中性）
                        if has_seg:
                            for s in range(3):
                                hi_pool = by_class_seg.get((c_hi, s), [])
                                lo_pool = by_class_seg.get((c_lo, s), [])
                                if not hi_pool or not lo_pool:
                                    continue
                                hi_sorted = sorted(hi_pool,
                                    key=lambda sid: -self.students[sid].score_of(f))
                                lo_sorted = sorted(lo_pool,
                                    key=lambda sid: self.students[sid].score_of(f))
                                for sa in hi_sorted[:25]:
                                    for sb in lo_sorted[:25]:
                                        if sa == sb:
                                            continue
                                        if not self._can_swap(sa, sb, assignment):
                                            continue
                                        same_cat = (not cat_sig) or cat_sig[sa] == cat_sig[sb]
                                        self._apply_swap(sa, sb, assignment)
                                        new_cost = dev_cost()
                                        ok = (new_cost < best_cost - 1e-12 and
                                              (same_cat or self._stats.category_dev() <= cat_limit))
                                        if ok:
                                            best_cost = new_cost
                                            best_swap = (sa, sb)
                                        self._apply_swap(sa, sb, assignment)  # 回退

                        # Layer 3: 任意交换（检查类别+分段）
                        hi_all = sorted(
                            [sid for sid, c in assignment.items() if c == c_hi],
                            key=lambda sid: -self.students[sid].score_of(f))
                        lo_all = sorted(
                            [sid for sid, c in assignment.items() if c == c_lo],
                            key=lambda sid: self.students[sid].score_of(f))
                        for sa in hi_all[:30]:
                            for sb in lo_all[:30]:
                                if sa == sb:
                                    continue
                                if not self._can_swap(sa, sb, assignment):
                                    continue
                                if has_seg and seg_of.get(sa) == seg_of.get(sb):
                                    continue  # 已在 Layer 1+2 处理
                                self._apply_swap(sa, sb, assignment)
                                new_cost = dev_cost()
                                ok = (new_cost < best_cost - 1e-12 and
                                      self._stats.category_dev() <= cat_limit and
                                      self._stats.segment_dev() <= seg_limit)
                                if ok:
                                    best_cost = new_cost
                                    best_swap = (sa, sb)
                                self._apply_swap(sa, sb, assignment)  # 回退

            # 应用本轮最佳交换
            if best_swap:
                sa, sb = best_swap
                self._apply_swap(sa, sb, assignment)
                base_cost = best_cost
            else:
                break  # 无改进，收敛

    def _targeted_subject_polish(self, assignment: dict[int, int],
                                 cat_limit: int, seg_limit: int,
                                 max_rounds: int = 40):
        """针对性单科均衡：允许总分临时波动以突破 pairwise 限制

        核心策略：
        1. 仅优化单科 excess（忽略总分），允许总分 dev 临时增至 0.0250
        2. 放宽 total_diff 上限到 2.0，增加找到 yd=0.5 交换对的概率
        3. 接受条件：单科 excess 减少 + 总分 dev ≤ 0.025 + 约束满足
        后续 _final_mean_polish 会尝试修复总分波动
        """
        n = self.num_classes
        if n < 2 or len(self.score_fields) < 2:
            return
        seg_of = self._group_seg_of if hasattr(self, '_group_seg_of') and self._group_seg_of else self._auto_seg_of
        has_seg = bool(seg_of)
        groups = self._class_type_groups()

        # 构建类别签名
        cat_sig = {}
        if self.category_fields:
            for st in self.students:
                cat_sig[st.sid] = tuple(st.categories.get(f, "") for f in self.category_fields)

        def subject_excess() -> float:
            """仅单科字段的 excess（不含总分）—— 按班型组分别计算"""
            total = 0.0
            for f in self.score_fields:
                if f == "总分":
                    continue
                min_d = self._min_dev.get(f, 0.0)
                for group in groups:
                    d = self._stats.dev_in_group(f, group)
                    excess = max(0.0, d - min_d - 1e-9)
                    total += excess * excess * 10000.0 + excess * 100.0
            return total

        # 允许总分偏差的上限（最小偏差的2倍，即 0.0250）
        total_tol = max(self._min_dev.get("总分", 0.0) * 2, 0.025)

        for _ in range(max_rounds):
            if subject_excess() <= 1e-9:
                break  # 所有单科已达最小偏差

            best_swap = None
            best_excess = subject_excess()

            tot = {st.sid: st.total_score for st in self.students}

            for f in self.score_fields:
                if f == "总分":
                    continue
                min_d = self._min_dev.get(f, 0.0)
                means = self._stats.means(f)
                sums = self._stats.sums.get(f, [0.0] * n)
                # 按班型组分别处理
                for group in groups:
                    if len(group) < 2:
                        continue
                    # 检查组内是否有偏差超标的
                    if self._stats.dev_in_group(f, group) <= min_d + 1e-9:
                        continue
                    order = sorted(group, key=lambda c: means[c])
                    for i in range(max(1, len(group) // 2)):
                        c_lo = order[i]
                        c_hi = order[-(i + 1)]
                        sum_diff = sums[c_hi] - sums[c_lo]
                        if sum_diff <= 0.5:
                            continue

                        target_subj = sum_diff / 2.0
                        hi_sids = [sid for sid, c in assignment.items() if c == c_hi]
                        lo_sids = [sid for sid, c in assignment.items() if c == c_lo]

                        for sa in hi_sids:
                            sa_subj = self.students[sa].score_of(f)
                            sa_tot = tot[sa]
                            for sb in lo_sids:
                                sb_subj = self.students[sb].score_of(f)
                                sb_tot = tot[sb]
                                total_diff = abs(sa_tot - sb_tot)
                                if total_diff > 2.0:
                                    continue
                                subj_diff = sa_subj - sb_subj
                                if subj_diff <= 0:
                                    continue
                                if abs(subj_diff - target_subj) > 0.75:
                                    continue
                                if not self._can_swap(sa, sb, assignment):
                                    continue
                                same_cat = (not cat_sig) or cat_sig[sa] == cat_sig[sb]
                                same_seg = (not has_seg) or seg_of.get(sa) == seg_of.get(sb)

                                self._apply_swap(sa, sb, assignment)
                                new_excess = subject_excess()
                                new_total_dev = self._stats.dev_in_group("总分", group)
                                new_cat = self._stats.category_dev()
                                new_seg = self._stats.segment_dev()

                                ok = (new_excess < best_excess - 1e-12 and
                                      new_total_dev <= total_tol + 1e-9 and
                                      (same_cat or new_cat <= cat_limit + 1) and
                                      (same_seg or new_seg <= seg_limit + 1))
                                if ok:
                                    best_excess = new_excess
                                    best_swap = (sa, sb)
                                self._apply_swap(sa, sb, assignment)  # 回退

            if best_swap:
                sa, sb = best_swap
                self._apply_swap(sa, sb, assignment)
            else:
                break

    def _apply_cycle(self, sa: int, sb: int, sc: int,
                     assignment: dict[int, int]):
        """执行 3-way 循环交换：sa→c_b, sb→c_c, sc→c_a（同步统计缓存）

        即 sa 去 sb 的班，sb 去 sc 的班，sc 去 sa 的班。
        """
        ca, cb, cc = assignment[sa], assignment[sb], assignment[sc]
        sta, stb, stc = self.students[sa], self.students[sb], self.students[sc]
        self._stats.remove(sta, ca, self.score_fields)
        self._stats.remove(stb, cb, self.score_fields)
        self._stats.remove(stc, cc, self.score_fields)
        assignment[sa], assignment[sb], assignment[sc] = cb, cc, ca
        self._stats.add(sta, cb, self.score_fields)
        self._stats.add(stb, cc, self.score_fields)
        self._stats.add(stc, ca, self.score_fields)
        sta.assigned_class = cb
        stb.assigned_class = cc
        stc.assigned_class = ca

    def _can_cycle(self, sa: int, sb: int, sc: int,
                   assignment: dict[int, int]) -> bool:
        """检查 3-way 循环交换是否满足所有硬约束

        约束：预设班级、班型筛选、捆绑组、互斥组、姓名冲突
        """
        ca, cb, cc = assignment[sa], assignment[sb], assignment[sc]
        # 三人必须在不同班
        if ca == cb or cb == cc or ca == cc:
            return False
        sta, stb, stc = self.students[sa], self.students[sb], self.students[sc]
        # 预设班级
        if sta.preset_class is not None and sta.preset_class - 1 != cb:
            return False
        if stb.preset_class is not None and stb.preset_class - 1 != cc:
            return False
        if stc.preset_class is not None and stc.preset_class - 1 != ca:
            return False
        # 班型筛选
        if sa in self._class_type_filter and cb not in self._class_type_filter[sa]:
            return False
        if sb in self._class_type_filter and cc not in self._class_type_filter[sb]:
            return False
        if sc in self._class_type_filter and ca not in self._class_type_filter[sc]:
            return False
        # 捆绑组：整组必须随代表移动
        for sid, target_c in ((sa, cb), (sb, cc), (sc, ca)):
            g = self.constraints.binding_of(sid)
            if g is not None:
                for s in g.sids:
                    if s == sid:
                        continue
                    if assignment.get(s) != target_c:
                        return False
        # 互斥组 & 姓名冲突：临时应用循环，检查，回退
        assignment[sa], assignment[sb], assignment[sc] = cb, cc, ca
        ok = True
        affected = set()
        for sid in (sa, sb, sc):
            for g in self.constraints.mutex_groups_of(sid):
                affected.add(id(g))
            for g in self.constraints.name_groups_of(sid):
                affected.add(id(g))
        # 重新收集 affected 组并检查
        for sid in (sa, sb, sc):
            for g in self.constraints.mutex_groups_of(sid) + self.constraints.name_groups_of(sid):
                cs = [assignment.get(s) for s in g.sids if s in assignment]
                if len(cs) != len(set(cs)):
                    ok = False; break
            if not ok:
                break
        assignment[sa], assignment[sb], assignment[sc] = ca, cb, cc
        return ok

    def _three_way_polish(self, assignment: dict[int, int],
                          cat_limit: int, seg_limit: int,
                          max_rounds: int = 30):
        """3-way 循环交换：突破 pairwise 局限

        当某字段偏差无法通过两班交换消除时（如需科差=0.5 但无配对），
        通过 3 班循环 sa→c_b, sb→c_c, sc→c_a 实现精细均衡。

        核心场景：c_hi 高均分、c_lo 低均分，需要转移 precision 分差。
        找 sa(c_hi, 分=E+precision)、sb(c_lo, 分=E)、sc(c_mid, 分=E)，
        循环后 c_hi 失 sa 得 sc → -precision；c_lo 得 sa 失 sb → +precision；c_mid 得 sb 失 sc → 0。
        """
        n = self.num_classes
        if n < 3 or len(self.score_fields) < 2:
            return
        seg_of = self._group_seg_of if hasattr(self, '_group_seg_of') and self._group_seg_of else self._auto_seg_of
        has_seg = bool(seg_of)
        groups = self._class_type_groups()

        cat_sig = {}
        if self.category_fields:
            for st in self.students:
                cat_sig[st.sid] = tuple(st.categories.get(f, "") for f in self.category_fields)

        def dev_cost() -> float:
            """按班型组分别计算"""
            cost = 0.0
            for f in self.score_fields:
                min_d = self._min_dev.get(f, 0.0)
                for group in groups:
                    d = self._stats.dev_in_group(f, group)
                    excess = max(0.0, d - min_d - 1e-9)
                    cost += excess * 10000.0 + d * d * 100.0
            return cost

        tot = {st.sid: st.total_score for st in self.students}
        total_tol = max(self._min_dev.get("总分", 0.0) * 2, 0.025)

        for _ in range(max_rounds):
            base_cost = dev_cost()
            if base_cost <= 1e-9:
                break  # 所有字段已达最小偏差

            best_cycle = None
            best_cost = base_cost

            # 按班构建 (分数 → sid列表) 索引，支持 O(1) 查找匹配分数
            by_class_score: dict[int, dict[float, list[int]]] = {
                c: {} for c in range(n)}
            for sid, c in assignment.items():
                for f in self.score_fields:
                    sc = self.students[sid].score_of(f)
                    by_class_score[c].setdefault(f, {}).setdefault(sc, []).append(sid)

            for f in self.score_fields:
                if f == "总分":
                    continue  # 总分通过单科循环间接优化
                min_d = self._min_dev.get(f, 0.0)
                # 按班型组分别处理：只在组内寻找 hi/lo/mid 班
                means = self._stats.means(f)
                sums = self._stats.sums.get(f, [0.0] * n)
                for group in groups:
                    if len(group) < 3:
                        continue  # 3-way 至少需要 3 个班
                    # 检查组内是否有偏差超标
                    if self._stats.dev_in_group(f, group) <= min_d + 1e-9:
                        continue
                    order = sorted(group, key=lambda c: means[c])
                    c_hi = order[-1]  # 组内最高均分班
                    c_lo = order[0]   # 组内最低均分班
                    mid_classes = order[1:-1]  # 组内中间班
                    gap = sums[c_hi] - sums[c_lo]
                    if gap <= 0.5:
                        continue

                    # 检测该字段精度（0.5 或 1.0）
                    precision = 0.5
                    for s in self.students:
                        frac = s.score_of(f) - int(s.score_of(f))
                        if abs(frac) < 1e-9:
                            continue
                        if abs(frac - 0.5) < 1e-9:
                            precision = 0.5
                            break
                        precision = 0.25
                        break

                    hi_score_map = by_class_score[c_hi].get(f, {})
                    lo_score_map = by_class_score[c_lo].get(f, {})

                    # 遍历 c_lo 中每个分数 E，在 c_hi 找 E+precision
                    for score_lo, lo_sids in lo_score_map.items():
                        target_hi_score = score_lo + precision
                        hi_sids = hi_score_map.get(target_hi_score, [])
                        if not hi_sids:
                            continue

                        for sb in lo_sids:
                            sb_tot = tot[sb]
                            for sa in hi_sids:
                                sa_tot = tot[sa]
                                # 总分接近（避免总分波动）
                                if abs(sa_tot - sb_tot) > 3.0:
                                    continue

                                # 在组内中间班找 sc：sc 分数 = score_lo（与 sb 同分）
                                for c_mid in mid_classes:
                                    mid_score_map = by_class_score[c_mid].get(f, {})
                                    mid_sids = mid_score_map.get(score_lo, [])
                                    if not mid_sids:
                                        continue
                                    for sc in mid_sids:
                                        sc_tot = tot[sc]
                                        if abs(sc_tot - sa_tot) > 3.0 or abs(sc_tot - sb_tot) > 3.0:
                                            continue
                                        if not self._can_cycle(sa, sb, sc, assignment):
                                            continue

                                        same_cat = (not cat_sig) or (
                                            cat_sig[sa] == cat_sig[sb] == cat_sig[sc])
                                        same_seg = (not has_seg) or (
                                            seg_of.get(sa) == seg_of.get(sb) == seg_of.get(sc))

                                        self._apply_cycle(sa, sb, sc, assignment)
                                        new_cost = dev_cost()
                                        # 组内总分偏差检查
                                        new_total_dev = self._stats.dev_in_group("总分", group)
                                        new_cat = self._stats.category_dev()
                                        new_seg = self._stats.segment_dev()

                                        ok = (new_cost < best_cost - 1e-12 and
                                              new_total_dev <= total_tol + 1e-9 and
                                              (same_cat or new_cat <= cat_limit) and
                                              (same_seg or new_seg <= seg_limit))
                                        if ok:
                                            best_cost = new_cost
                                            best_cycle = (sa, sb, sc)
                                        self._apply_cycle(sa, sc, sb, assignment)  # 反向回退

            if best_cycle:
                sa, sb, sc = best_cycle
                self._apply_cycle(sa, sb, sc, assignment)
            else:
                break

    def _resync_stats(self, assignment: dict[int, int]):
        """根据 assignment 重建 _stats 缓存"""
        seg_of = self._group_seg_of if hasattr(self, '_group_seg_of') and self._group_seg_of else self._auto_seg_of
        self._stats = _ClassStats(self.num_classes, self.score_fields,
                                  self.category_fields, seg_of)
        for st in self.students:
            self._stats.add(st, assignment[st.sid], self.score_fields)

    def _meets_tolerance(self, assignment: dict[int, int], tol: float) -> bool:
        """检查是否达到容差目标 —— 按班型组分别检查"""
        groups = self._class_type_groups()
        n = self.num_classes
        cnt = [0] * n
        for c in assignment.values():
            cnt[c] += 1
        # 班级人数偏差（组内）
        for group in groups:
            gc = [cnt[c] for c in group]
            if max(gc) - min(gc) > self.config.tol_count:
                return False
        # 均分偏差（组内）
        for f in self.score_fields:
            for group in groups:
                if self._stats.dev_in_group(f, group) > tol + 1e-9:
                    return False
        if self.constraints.hard_constraint_score(assignment, n) > 0:
            return False
        return True

    # —— 定向换班：高均分班→低均分班 ——
    def _targeted_swap(self, assignment: dict[int, int]):
        """针对每个分数字段，把高均分班的高分生与低均分班的低分生交换"""
        n = self.num_classes
        for f in self.score_fields:
            means = self._stats.means(f)
            # 按均分排序
            order = sorted(range(n), key=lambda c: means[c])
            lo_classes = order[: max(1, n // 3)]      # 最低的 1/3
            hi_classes = order[-max(1, n // 3):]      # 最高的 1/3
            for c_lo in lo_classes:
                for c_hi in hi_classes:
                    if c_lo == c_hi:
                        continue
                    # 在 c_hi 找高分生，c_lo 找低分生
                    hi_sids = [s for s in self._class_sids(c_lo, c_hi, assignment)
                               if assignment[s] == c_hi]
                    lo_sids = [s for s in self._class_sids(c_lo, c_hi, assignment)
                               if assignment[s] == c_lo]
                    if not hi_sids or not lo_sids:
                        continue
                    hi_sids.sort(key=lambda s: -self.students[s].score_of(f))
                    lo_sids.sort(key=lambda s: self.students[s].score_of(f))
                    # 尝试最佳配对（最多 5x5）
                    best_pair = None
                    best_delta = 0.0
                    base = self._total_cost(assignment)
                    for sa in hi_sids[:5]:
                        for sb in lo_sids[:5]:
                            if not self._can_swap(sa, sb, assignment):
                                continue
                            assignment[sa], assignment[sb] = c_lo, c_hi
                            new_cost = self._total_cost(assignment)
                            assignment[sa], assignment[sb] = c_hi, c_lo
                            delta = base - new_cost
                            if delta > best_delta:
                                best_delta = delta
                                best_pair = (sa, sb)
                    if best_pair:
                        sa, sb = best_pair
                        self._apply_swap(sa, sb, assignment)

    def _class_sids(self, c1: int, c2: int, assignment: dict[int, int]) -> list[int]:
        """返回 c1 或 c2 班的所有学生 sid"""
        return [s for s, c in assignment.items() if c in (c1, c2)]

    def _apply_swap(self, sa: int, sb: int, assignment: dict[int, int]):
        """执行交换并同步统计缓存"""
        ca, cb = assignment[sa], assignment[sb]
        sta, stb = self.students[sa], self.students[sb]
        self._stats.remove(sta, ca, self.score_fields)
        self._stats.remove(stb, cb, self.score_fields)
        assignment[sa], assignment[sb] = cb, ca
        self._stats.add(sta, cb, self.score_fields)
        self._stats.add(stb, ca, self.score_fields)
        sta.assigned_class = cb
        stb.assigned_class = ca

    # —— 贪心换班 ——
    def _greedy_swap(self, assignment: dict[int, int]) -> bool:
        n = self.num_classes
        improved = False
        by_class: list[set[int]] = [set() for _ in range(n)]
        for sid, c in assignment.items():
            by_class[c].add(sid)
        base_cost = self._total_cost(assignment)
        pairs = [(a, b) for a in range(n) for b in range(a + 1, n)]
        self.rng.shuffle(pairs)
        for a, b in pairs:
            if not by_class[a] or not by_class[b]:
                continue
            sids_a = list(by_class[a])
            sids_b = list(by_class[b])
            self.rng.shuffle(sids_a)
            self.rng.shuffle(sids_b)
            for sa in sids_a[:6]:
                if sa not in by_class[a]:
                    continue
                for sb in sids_b[:6]:
                    if sb not in by_class[b]:
                        continue
                    if not self._can_swap(sa, sb, assignment):
                        continue
                    # 增量评估
                    self._apply_swap(sa, sb, assignment)
                    new_cost = self._total_cost(assignment)
                    if new_cost < base_cost - 1e-9:
                        base_cost = new_cost
                        improved = True
                        by_class[a].discard(sa); by_class[a].add(sb)
                        by_class[b].discard(sb); by_class[b].add(sa)
                        break
                    else:
                        self._apply_swap(sa, sb, assignment)  # 回退
        return improved

    def _can_swap(self, sa: int, sb: int, assignment: dict[int, int]) -> bool:
        ca, cb = assignment[sa], assignment[sb]
        sta = self.students[sa]; stb = self.students[sb]
        if sta.preset_class is not None and sta.preset_class - 1 != cb:
            return False
        if stb.preset_class is not None and stb.preset_class - 1 != ca:
            return False
        if sa in self._class_type_filter and cb not in self._class_type_filter[sa]:
            return False
        if sb in self._class_type_filter and ca not in self._class_type_filter[sb]:
            return False
        ga = self.constraints.binding_of(sa)
        if ga is not None:
            for s in ga.sids:
                if s == sa:
                    continue
                if assignment.get(s) != cb:
                    return False
        gb = self.constraints.binding_of(sb)
        if gb is not None:
            for s in gb.sids:
                if s == sb:
                    continue
                if assignment.get(s) != ca:
                    return False
        assignment[sa], assignment[sb] = cb, ca
        ok = True
        for g in self.constraints.mutex_groups_of(sa) + self.constraints.mutex_groups_of(sb):
            cs = [assignment.get(s) for s in g.sids if s in assignment]
            if len(cs) != len(set(cs)):
                ok = False; break
        if ok:
            for g in self.constraints.name_groups_of(sa) + self.constraints.name_groups_of(sb):
                cs = [assignment.get(s) for s in g.sids if s in assignment]
                if len(cs) != len(set(cs)):
                    ok = False; break
        assignment[sa], assignment[sb] = ca, cb
        return ok

    def _can_move(self, sid: int, c_to: int, assignment: dict[int, int]) -> bool:
        st = self.students[sid]
        if st.preset_class is not None and st.preset_class - 1 != c_to:
            return False
        if sid in self._class_type_filter and c_to not in self._class_type_filter[sid]:
            return False
        g = self.constraints.binding_of(sid)
        if g is not None:
            for s in g.sids:
                if s == sid:
                    continue
                if assignment.get(s) != c_to:
                    return False
        old = assignment[sid]
        assignment[sid] = c_to
        ok = True
        for grp in self.constraints.mutex_groups_of(sid) + self.constraints.name_groups_of(sid):
            cs = [assignment.get(s) for s in grp.sids if s in assignment]
            if len(cs) != len(set(cs)):
                ok = False; break
        assignment[sid] = old
        return ok

    def _can_swap_force(self, sa: int, sb: int, assignment: dict[int, int]) -> bool:
        """跨班型换班检查：跳过班型筛选，仍检查硬约束（预设/捆绑/互斥/姓名）"""
        ca, cb = assignment[sa], assignment[sb]
        sta = self.students[sa]; stb = self.students[sb]
        if sta.preset_class is not None and sta.preset_class - 1 != cb:
            return False
        if stb.preset_class is not None and stb.preset_class - 1 != ca:
            return False
        # 跳过班型筛选（_class_type_filter）
        ga = self.constraints.binding_of(sa)
        if ga is not None:
            for s in ga.sids:
                if s == sa:
                    continue
                if assignment.get(s) != cb:
                    return False
        gb = self.constraints.binding_of(sb)
        if gb is not None:
            for s in gb.sids:
                if s == sb:
                    continue
                if assignment.get(s) != ca:
                    return False
        assignment[sa], assignment[sb] = cb, ca
        ok = True
        for g in self.constraints.mutex_groups_of(sa) + self.constraints.mutex_groups_of(sb):
            cs = [assignment.get(s) for s in g.sids if s in assignment]
            if len(cs) != len(set(cs)):
                ok = False; break
        if ok:
            for g in self.constraints.name_groups_of(sa) + self.constraints.name_groups_of(sb):
                cs = [assignment.get(s) for s in g.sids if s in assignment]
                if len(cs) != len(set(cs)):
                    ok = False; break
        assignment[sa], assignment[sb] = ca, cb
        return ok

    def _can_move_force(self, sid: int, c_to: int, assignment: dict[int, int]) -> bool:
        """跨班型移班检查：跳过班型筛选，仍检查硬约束"""
        st = self.students[sid]
        if st.preset_class is not None and st.preset_class - 1 != c_to:
            return False
        # 跳过班型筛选（_class_type_filter）
        g = self.constraints.binding_of(sid)
        if g is not None:
            for s in g.sids:
                if s == sid:
                    continue
                if assignment.get(s) != c_to:
                    return False
        old = assignment[sid]
        assignment[sid] = c_to
        ok = True
        for grp in self.constraints.mutex_groups_of(sid) + self.constraints.name_groups_of(sid):
            cs = [assignment.get(s) for s in grp.sids if s in assignment]
            if len(cs) != len(set(cs)):
                ok = False; break
        assignment[sid] = old
        return ok

    def _anneal(self, assignment: dict[int, int], temperature: float = 1.0):
        n = self.num_classes
        sids = list(assignment.keys())
        for _ in range(min(15, len(sids))):
            sa = self.rng.choice(sids)
            sb = self.rng.choice(sids)
            if assignment[sa] == assignment[sb]:
                continue
            if self._can_swap(sa, sb, assignment):
                old_cost = self._total_cost(assignment)
                ca, cb = assignment[sa], assignment[sb]
                self._apply_swap(sa, sb, assignment)
                new_cost = self._total_cost(assignment)
                delta = new_cost - old_cost
                if delta < 0 or self.rng.random() < math.exp(-delta / max(temperature, 1e-6)):
                    continue
                # 回退
                self._apply_swap(sa, sb, assignment)

    # =================== 成本函数 ===================
    def _total_cost(self, assignment: dict[int, int]) -> float:
        """加权总成本。各分项归一化，避免某项过大淹没其他项。
        班型模式下：均分/分段/类别/逐点偏差按班型组分别计算，组间差异不计入成本。
        """
        cfg = self.config
        n = self.num_classes
        groups = self._class_type_groups()
        cost = 0.0
        # 班级人数
        cnt = self._stats.cnt
        targets = self._compute_targets()
        size_pen = 0.0
        for c in range(n):
            size_pen += abs(cnt[c] - targets[c]) ** 2
        cost += cfg.w_size * size_pen
        # 均分（总分 + 各单科）—— 按班型组分别计算偏差
        for f in self.score_fields:
            for group in groups:
                dev = self._stats.dev_in_group(f, group)
                if f == "总分":
                    cost += cfg.w_total * dev * dev * 100.0
                else:
                    adj = cfg.metric_adjust.get(f, 1.0)
                    cost += cfg.w_subject * adj * dev * dev * 100.0
        # 分段均衡 —— 按班型组分别计算
        seg_pen = self._segment_penalty(assignment, cnt)
        cost += cfg.w_segment * seg_pen
        # 类别均衡 —— 按班型组分别计算
        cat_pen = self._category_penalty(assignment, cnt)
        cost += cfg.w_category * cat_pen
        # 逐点均衡 —— 按班型组分别计算
        if cfg.enable_pointwise:
            pw_pen = self._pointwise_penalty(assignment, cnt)
            cost += cfg.w_pointwise * pw_pen
        # 硬约束
        cost += self.constraints.hard_constraint_score(assignment, n)
        return cost

    def _segment_penalty(self, assignment: dict[int, int], cnt: list[int]) -> float:
        """分段均衡惩罚 —— 按班型组分别计算"""
        groups = self._class_type_groups()
        pen = 0.0
        if self.config.enable_segment_auto:
            seg_of = self._group_seg_of if hasattr(self, '_group_seg_of') and self._group_seg_of else self._auto_seg_of
            max_seg = 2  # 自动分段固定为 3 段
            for group in groups:
                ng = len(group)
                seg_cnt = [[0] * (max_seg + 1) for _ in range(ng)]
                # 统计该组内各班各分段人数
                group_set = set(group)
                for sid, c in assignment.items():
                    if c in group_set:
                        gi = group.index(c)
                        seg_cnt[gi][seg_of.get(sid, 0)] += 1
                # 该组内各分段总数
                total_seg = [0] * (max_seg + 1)
                for sid in seg_of:
                    if assignment.get(sid) in group_set:
                        total_seg[seg_of[sid]] += 1
                for s in range(max_seg + 1):
                    expected = total_seg[s] / ng if ng > 0 else 0
                    for gi in range(ng):
                        pen += abs(seg_cnt[gi][s] - expected) ** 2
        for rule in self.config.segment_rules:
            pen += self._apply_segment_rule(rule, assignment, cnt, None)
        return pen

    def _apply_segment_rule(self, rule: SegmentRule, assignment: dict[int, int],
                            cnt: list[int], pre_sorted: Optional[list[Student]]) -> float:
        n = self.num_classes
        seg_of: dict[int, int] = {}
        if rule.by_rank:
            if pre_sorted is not None and rule.field_name == "总分":
                ordered = pre_sorted
            else:
                ordered = sorted(self.students,
                                 key=lambda s: -s.score_of(rule.field_name))
            for rank, st in enumerate(ordered, start=1):
                seg_of[st.sid] = rule.segment_of(0.0, rank=rank)
        else:
            for st in self.students:
                seg_of[st.sid] = rule.segment_of(st.score_of(rule.field_name))
        max_seg = max(seg_of.values()) if seg_of else 0
        seg_cnt = [[0] * (max_seg + 1) for _ in range(n)]
        for sid, c in assignment.items():
            seg_cnt[c][seg_of[sid]] += 1
        total_seg = [0] * (max_seg + 1)
        for sid in seg_of:
            total_seg[seg_of[sid]] += 1
        pen = 0.0
        for s in range(max_seg + 1):
            expected = total_seg[s] / n
            for c in range(n):
                pen += abs(seg_cnt[c][s] - expected) ** 2
        return pen

    def _category_penalty(self, assignment: dict[int, int], cnt: list[int]) -> float:
        """类别均衡惩罚 —— 按班型组分别计算"""
        groups = self._class_type_groups()
        pen = 0.0
        for f in self.category_fields:
            values: dict[str, list[int]] = {}
            for st in self.students:
                v = st.category_of(f)
                if v == "":
                    continue
                values.setdefault(v, []).append(st.sid)
            for v, sids in values.items():
                for group in groups:
                    ng = len(group)
                    group_set = set(group)
                    group_sids = [s for s in sids if assignment.get(s) in group_set]
                    if not group_sids:
                        continue
                    expected = len(group_sids) / ng
                    class_cnt = [0] * ng
                    for s in group_sids:
                        gi = group.index(assignment[s])
                        class_cnt[gi] += 1
                    for gi in range(ng):
                        pen += abs(class_cnt[gi] - expected) ** 2
        return pen

    def _pointwise_penalty(self, assignment: dict[int, int], cnt: list[int]) -> float:
        """逐点均衡惩罚 —— 按班型组分别计算"""
        groups = self._class_type_groups()
        students = self.students
        pen = 0.0
        for group in groups:
            ng = len(group)
            group_set = set(group)
            # 该组学生按总分降序
            group_sids = sorted(
                [s.sid for s in students if assignment.get(s.sid) in group_set],
                key=lambda sid: -students[sid].total_score)
            # 每 ng 个一组
            for i in range(0, len(group_sids), ng):
                chunk = group_sids[i:i + ng]
                class_cnt = [0] * ng
                for sid in chunk:
                    gi = group.index(assignment[sid])
                    class_cnt[gi] += 1
                expected = len(chunk) / ng
                for gi in range(ng):
                    pen += abs(class_cnt[gi] - expected) ** 2
        return pen

    def _fill_stats(self, assignment: dict[int, int], stats: EngineStats):
        n = self.num_classes
        groups = self._class_type_groups()
        cnt = [0] * n
        for c in assignment.values():
            cnt[c] += 1
        stats.size_dev = max(cnt) - min(cnt)
        sums = {f: [0.0] * n for f in self.score_fields}
        for st in self.students:
            c = assignment[st.sid]
            for f in self.score_fields:
                sums[f][c] += st.score_of(f)
        # 全局偏差（兼容旧接口）
        max_total = max_subject = 0.0
        for f in self.score_fields:
            means = [sums[f][c] / cnt[c] if cnt[c] else 0.0 for c in range(n)]
            dev = max(means) - min(means)
            if f == "总分":
                max_total = dev
            else:
                max_subject = max(max_subject, dev)
        stats.total_dev = max_total
        stats.subject_dev = max_subject

        # 按班型组分别计算偏差
        group_stats_list = []
        group_names = self._group_names()
        for gi, group in enumerate(groups):
            gs = {
                "name": group_names[gi] if gi < len(group_names) else f"组{gi+1}",
                "classes": [c + 1 for c in group],  # 1-based for display
                "size_dev": self._stats.size_dev_in_group(group),
            }
            g_total_dev = 0.0
            g_subject_dev = 0.0
            for f in self.score_fields:
                dev = self._stats.dev_in_group(f, group)
                if f == "总分":
                    g_total_dev = dev
                else:
                    g_subject_dev = max(g_subject_dev, dev)
            gs["total_dev"] = g_total_dev
            gs["subject_dev"] = g_subject_dev
            gs["segment_dev"] = self._stats.segment_dev_in_group(group)
            gs["category_dev"] = self._stats.category_dev_in_group(group)
            # 组内各班人数
            gs["counts"] = [cnt[c] for c in group]
            # 组内各班总分均分
            gs["total_means"] = [sums["总分"][c] / cnt[c] if cnt[c] else 0.0 for c in group]
            group_stats_list.append(gs)
        stats.group_stats = group_stats_list

        # 全局分段偏差（取各组最大值）
        seg_of = self._group_seg_of if hasattr(self, '_group_seg_of') and self._group_seg_of else self._auto_seg_of
        seg_cnt = [[0] * 3 for _ in range(n)]
        for st in self.students:
            seg_cnt[assignment[st.sid]][seg_of.get(st.sid, 0)] += 1
        stats.segment_dev = max(max(row) - min(row) for row in
                                [[seg_cnt[c][s] for c in range(n)] for s in range(3)])
        # 全局类别偏差（取各组最大值）
        cat_dev = 0
        for f in self.category_fields:
            values: dict[str, list[int]] = {}
            for st in self.students:
                v = st.category_of(f)
                if v == "":
                    continue
                values.setdefault(v, []).append(st.sid)
            for v, sids in values.items():
                class_cnt = [0] * n
                for s in sids:
                    class_cnt[assignment[s]] += 1
                cat_dev = max(cat_dev, max(class_cnt) - min(class_cnt))
        stats.category_dev = cat_dev
        # 逐点偏差
        pw = 0
        ordered = sorted(self.students, key=lambda s: -s.total_score)
        for i in range(0, len(ordered), n):
            chunk = ordered[i:i + n]
            class_cnt = [0] * n
            for st in chunk:
                class_cnt[assignment[st.sid]] += 1
            pw = max(pw, max(class_cnt) - min(class_cnt))
        stats.pointwise_dev = pw
        stats.hard_penalty = self.constraints.hard_constraint_score(assignment, n)

    def _group_names(self) -> list[str]:
        """返回各班型组的名称"""
        if not self.config.class_types:
            return ["全部"]
        names = []
        seen = set()
        for spec in self.config.class_types:
            key = tuple(sorted(spec.class_indices))
            if key not in seen:
                names.append(spec.name)
                seen.add(key)
        covered = set()
        for spec in self.config.class_types:
            covered.update(spec.class_indices)
        uncovered = [c for c in range(self.num_classes) if c not in covered]
        if uncovered:
            names.append("普通班")
        return names
