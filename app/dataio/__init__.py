# -*- coding: utf-8 -*-
# ============================================================
# 版权所有 (C) 2026 @zkxxzf
# 教务分班软件 ClassSort
# 版本: 1.13 | 日期: 2026-07-28 | 作者: @zkxxzf
# ============================================================
"""io 模块"""
from .excel_handler import load_students, load_teachers, export_classes

__all__ = ["load_students", "load_teachers", "export_classes"]
