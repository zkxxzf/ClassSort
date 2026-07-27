# -*- coding: utf-8 -*-
# ============================================================
# 版权所有 (C) 2026 @zkxxzf
# 教务分班软件 ClassSort
# 版本: 1.13 | 日期: 2026-07-28 | 作者: @zkxxzf
# ============================================================
"""数据模型定义"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional


# 字段类型常量
FIELD_CATEGORY = "K"   # 类别（枚举型：性别、民族等）
FIELD_MEASURE = "M"    # 数值（分数型：总分、单科）
FIELD_TEXT = "T"       # 文本（一般文本）
FIELD_RESERVED = "R"   # 保留字段（姓名、预设班级）

# 保留字段名
RESERVED_NAME = "姓名"
RESERVED_PRESET = "预设班级"


@dataclass
class FieldDef:
    """字段定义"""
    raw_name: str           # 原始列名（含前缀）
    name: str               # 去除前缀的字段名
    ftype: str              # 字段类型 K/M/T/R
    is_score: bool = False  # 是否为分数字段（参与均分均衡）
    is_category: bool = False  # 是否为类别字段（参与人数均衡）

    @classmethod
    def parse(cls, col_name: str) -> "FieldDef":
        """从列名解析字段定义。前缀 K/M/T 决定类型，无前缀的姓名/预设班级为保留字段。"""
        name = str(col_name).strip()
        if name == RESERVED_NAME:
            return cls(raw_name=name, name=name, ftype=FIELD_RESERVED)
        if name == RESERVED_PRESET:
            return cls(raw_name=name, name=name, ftype=FIELD_RESERVED)
        if not name:
            return cls(raw_name=name, name=name, ftype=FIELD_TEXT)
        prefix = name[0].upper()
        if prefix in ("K", "M", "T") and len(name) > 1:
            ftype = prefix
            clean = name[1:].strip()
            return cls(
                raw_name=name, name=clean, ftype=ftype,
                is_score=(ftype == FIELD_MEASURE),
                is_category=(ftype == FIELD_CATEGORY),
            )
        # 无前缀默认按文本处理
        return cls(raw_name=name, name=name, ftype=FIELD_TEXT)


@dataclass
class Student:
    """学生数据"""
    sid: int                        # 学生内部 ID（从 0 起）
    name: str = ""                  # 姓名
    preset_class: Optional[int] = None  # 预设班级（1-based），None 表示未预设
    scores: dict[str, float] = field(default_factory=dict)   # 字段名 -> 分数
    categories: dict[str, str] = field(default_factory=dict) # 字段名 -> 类别值
    texts: dict[str, str] = field(default_factory=dict)      # 字段名 -> 文本
    raw: dict[str, Any] = field(default_factory=dict)        # 原始行数据
    assigned_class: Optional[int] = None  # 分配的班级（0-based 索引）

    # —— 派生属性 ——
    @property
    def total_score(self) -> float:
        """总分：若数据含 M总分 字段则直接取，否则求所有分数字段之和"""
        if "总分" in self.scores:
            return self.scores["总分"]
        return sum(self.scores.values()) if self.scores else 0.0

    def score_of(self, name: str) -> float:
        """取某分数字段值（含总分派生）"""
        if name == "总分":
            return self.total_score
        return self.scores.get(name, 0.0)

    def category_of(self, name: str) -> str:
        return self.categories.get(name, "")

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.raw)
        d["分班结果"] = "" if self.assigned_class is None else f"{self.assigned_class + 1}班"
        return d


@dataclass
class ClassRoom:
    """班级"""
    index: int                  # 0-based 索引
    name: str                   # 班名（1班、2班…）
    target_size: Optional[int] = None  # 目标人数（None 表示自动）
    class_type: str = "普通班"  # 班型：重点班/卓越班/普通班
    students: list[Student] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.students)


@dataclass
class ClassTypeSpec:
    """班型规格"""
    name: str                       # 班型名
    class_indices: list[int]        # 该班型覆盖的班级 0-based 索引
    score_floor: Optional[float] = None  # 分数下限（仅该班型学生进入）
    score_ceil: Optional[float] = None   # 分数上限
    rank_floor: Optional[int] = None     # 名次下限（年级排名，1-based）
    rank_ceil: Optional[int] = None      # 名次上限


@dataclass
class SegmentRule:
    """分数段均衡规则"""
    field_name: str          # 字段名（总分/语文/数学…）
    by_rank: bool = False    # True=按年级排名分段；False=按分数值分段
    cuts: list[float] = field(default_factory=list)  # 分段切点
    # 例：cuts=[260, 240, 220] 表示 >260 / 240-260 / 220-240 / <220 四段

    def segment_of(self, score: float, rank: int = -1) -> int:
        """返回某分数/排名所在段号（0-based，越小段越优）"""
        v = rank if self.by_rank else score
        asc = self.by_rank  # 排名升序分段；分数降序分段
        cuts = sorted(self.cuts) if asc else sorted(self.cuts, reverse=True)
        seg = 0
        for c in cuts:
            if asc:
                if v <= c:
                    return seg
            else:
                if v >= c:
                    return seg
            seg += 1
        return seg


@dataclass
class TaskConfig:
    """分班任务配置"""
    num_classes: int = 5                         # 班级数
    seed: int = 20260726                         # 随机种子
    # 均衡权重
    w_total: float = 200.0       # 总分均分偏差
    w_subject: float = 150.0     # 单科均分偏差
    w_segment: float = 60.0      # 分段人数偏差
    w_category: float = 50.0     # 类别人数偏差
    w_pointwise: float = 30.0    # 逐点均衡（高低分分布）
    w_size: float = 80.0         # 班级人数偏差
    # 容差
    tol_mean: float = 0.01        # 均分偏差目标
    tol_count: int = 1            # 人数偏差上限
    # 功能开关
    enable_pointwise: bool = True # 超强逐点均衡
    enable_name_pinyin: bool = True  # 姓名同音分散
    enable_segment_auto: bool = True  # 自动分段均衡（高/中/低）
    # 多次考试权重（字段名 -> 权重，默认单次为 1.0）
    exam_weights: dict[str, float] = field(default_factory=dict)
    # 班型
    class_types: list[ClassTypeSpec] = field(default_factory=list)
    # 自定义分段规则
    segment_rules: list[SegmentRule] = field(default_factory=list)
    # 指标优化调节（字段名 -> 调节因子，>1 提升、<1 降低）
    metric_adjust: dict[str, float] = field(default_factory=dict)
    # 零分处理
    zero_score_strategy: str = "keep"  # keep/exclude/avg
    # 自定义班级人数（大小班），None=自动均分
    custom_sizes: list[Optional[int]] = field(default_factory=list)
    # 最大迭代次数
    max_iterations: int = 300
