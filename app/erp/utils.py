# -*- coding: utf-8 -*-
"""天宫ERP系统 —— 通用工具函数"""
import datetime as _dt
from decimal import Decimal, InvalidOperation


def today() -> str:
    return _dt.date.today().strftime("%Y-%m-%d")


def now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def month_start(offset: int = 0) -> str:
    """本月第一天，offset 为往前推的月数"""
    d = _dt.date.today().replace(day=1)
    for _ in range(offset):
        d = (d.replace(day=1) - _dt.timedelta(days=1)).replace(day=1)
    return d.strftime("%Y-%m-%d")


def add_days(date_str: str, days: int) -> str:
    try:
        d = _dt.datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return ""
    return (d + _dt.timedelta(days=int(days))).strftime("%Y-%m-%d")


def days_between(start: str, end: str) -> int:
    try:
        a = _dt.datetime.strptime(start, "%Y-%m-%d").date()
        b = _dt.datetime.strptime(end, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return 0
    return (b - a).days


def is_valid_date(v) -> bool:
    """判断是否为合法的 YYYY-MM-DD 日期。

    修复 v1.5.1：过去非法日期（如 2026-13-45）可直接落库，
    导致账龄计算解析失败、逾期天数恒为 0，账龄表失真。
    单据日期、交货日期、收付款日期在入库前一律先过这一关。
    """
    s = str(v or "").strip()
    if not s:
        return False
    try:
        _dt.datetime.strptime(s, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


def check_date(v, field: str, required: bool = True):
    """校验日期字段，返回错误信息；合法返回空串。"""
    s = str(v or "").strip()
    if not s:
        return "" if not required else f"{field}不能为空"
    if not is_valid_date(s):
        return f"{field}「{s}」不是有效日期，格式应为 YYYY-MM-DD（如 2026-10-14）"
    return ""


def safe_float(v, default=0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        return float(Decimal(str(v)))
    except (InvalidOperation, ValueError, TypeError):
        return float(default)


def safe_int(v, default=0) -> int:
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return int(default)


def money(v, symbol="¥") -> str:
    try:
        n = float(v or 0)
    except (ValueError, TypeError):
        n = 0.0
    return f"{symbol}{n:,.2f}"


def num(v, nd: int = 2) -> str:
    try:
        n = float(v or 0)
    except (ValueError, TypeError):
        n = 0.0
    return f"{n:,.{nd}f}"


def pct(v, nd: int = 1) -> str:
    try:
        n = float(v or 0) * 100
    except (ValueError, TypeError):
        n = 0.0
    return f"{n:.{nd}f}%"


def div(a, b, default=0.0) -> float:
    """安全除法"""
    try:
        b = float(b)
        if b == 0:
            return float(default)
        return float(a) / b
    except (ValueError, TypeError, ZeroDivisionError):
        return float(default)


def trunc(s, n: int = 40) -> str:
    s = str(s or "")
    return s if len(s) <= n else s[: n - 1] + "…"
