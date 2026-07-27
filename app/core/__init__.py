# -*- coding: utf-8 -*-
# ============================================================
# 版权所有 (C) 2026 @zkxxzf
# 教务分班软件 ClassSort
# 版本: 1.13 | 日期: 2026-07-28 | 作者: @zkxxzf
# ============================================================
"""教务分班软件 - 核心模块"""
from .models import Student, ClassRoom, TaskConfig, FieldDef
from .engine import BalanceEngine
from .constraints import ConstraintManager

__all__ = ["Student", "ClassRoom", "TaskConfig", "FieldDef",
           "BalanceEngine", "ConstraintManager"]
