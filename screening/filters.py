"""筛选层：硬性过滤规则。

设计原则
--------
**只做排除，不做排序。**

每条规则独立可开关，返回 (是否通过, 剔除原因)。
所有规则跑完后统计每条排除了多少只——这是审计的核心。

为什么不做排序
--------------
排序需要「因子假设」（动量有效？价值有效？），那是下一步的事。
这一层只回答「哪些股票是能正常交易的」——这是客观的、无争议的。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class FilterResult:
    """单只股票的过滤结果。"""

    code: str
    name: str
    passed: bool
    reject_rule: str = ""
    reject_reason: str = ""
    # 通过时附带的指标，便于人工复核
    metrics: dict = field(default_factory=dict)


def check_st(name: str, keywords: list[str]) -> tuple[bool, str]:
    """① ST / 退市风险。返回 (是否被剔除, 原因)。"""
    upper = str(name).upper()
    for kw in keywords:
        if kw.upper() in upper:
            return True, f"名称含「{kw}」"
    return False, ""


def check_listing_months(
    daily: Any,
    min_months: int,
    today: date,
    lookback_years: int = 3,
) -> tuple[bool, str, dict]:
    """② 上市不足 N 个月。

    用日线最早日期近似上市日。**关键**：判断时必须用足够长的回看窗口，
    否则「最早日期」永远等于窗口起点，规则形同虚设。

    实测（2026-10-30）：腾讯日线可回溯到 2010 年（4078 行），足够判断。

    判定逻辑：
    - 若最早日期 == 窗口起点（说明上市早于窗口），视为「上市足够久」
    - 否则用最早日期算月数
    """
    if daily is None or len(daily) == 0:
        return False, "", {}

    first = daily.iloc[0]["date"]
    if hasattr(first, "date"):
        first = first.date()
    elif isinstance(first, str):
        from datetime import datetime as _dt

        first = _dt.strptime(first[:10], "%Y-%m-%d").date()

    months = (today.year - first.year) * 12 + (today.month - first.month)
    metrics = {"first_bar": str(first), "listed_months_est": months}

    # 最早日期贴近窗口起点 => 上市早于窗口，肯定超过 min_months
    window_start = today.replace(year=today.year - lookback_years) if today.month == 2 and today.day == 29 else None
    try:
        from datetime import timedelta as _td

        window_start = today - _td(days=365 * lookback_years)
    except Exception:  # pragma: no cover
        pass

    if window_start and abs((first - window_start).days) <= 10:
        metrics["listed_months_est"] = f">{min_months}"
        return False, "", metrics

    if months < min_months:
        return True, f"最早日线 {first}，约 {months} 个月 < {min_months}", metrics
    return False, "", metrics


def check_avg_amount(
    daily: Any,
    min_amount_yuan: float,
    lookback_days: int,
) -> tuple[bool, str, dict]:
    """③ 日均成交额不足。"""
    if daily is None or len(daily) == 0:
        return True, "无日线数据", {}

    amount_col = "amount" if "amount" in daily.columns else "成交额"
    if amount_col not in daily.columns:
        return True, f"日线缺成交额字段（有：{list(daily.columns)}）", {}

    recent = daily.tail(lookback_days)
    avg = float(recent[amount_col].astype(float).mean())
    metrics = {"avg_amount_yuan": avg, "avg_amount_yi": avg / 1e8}

    if avg < min_amount_yuan:
        return True, f"近{lookback_days}日日均成交额 {avg/1e8:.2f} 亿 < {min_amount_yuan/1e8:.2f} 亿", metrics
    return False, "", metrics


def check_suspended(
    daily: Any,
    no_trade_days: int,
) -> tuple[bool, str, dict]:
    """④ 停牌 / 长期无成交。"""
    if daily is None or len(daily) == 0:
        return True, "无日线数据", {}

    volume_col = "volume" if "volume" in daily.columns else "成交量"
    if volume_col not in daily.columns:
        return False, "", {}

    recent = daily.tail(no_trade_days)
    zero_days = int((recent[volume_col].astype(float) == 0).sum())
    metrics = {"zero_volume_days": zero_days}

    if zero_days >= no_trade_days:
        return True, f"最近 {no_trade_days} 日全部无成交", metrics
    return False, "", metrics


def apply_all(
    stock: dict,
    daily: Any,
    rules: dict,
    today: date,
) -> FilterResult:
    """对单只股票跑全部规则。

    顺序按「成本从低到高」：名称判断（无 IO）→ 日线相关（已有数据）。
    一旦被剔除就短路，不再跑后续规则。
    """
    code = stock["code"]
    name = stock["name"]
    metrics: dict = {}

    # ① ST
    rule = rules.get("exclude_st", {})
    if rule.get("enabled", False):
        hit, reason = check_st(name, rule.get("keywords", ["ST"]))
        if hit:
            return FilterResult(code, name, False, "exclude_st", reason, metrics)

    # ② 上市时长
    rule = rules.get("min_listing_months", {})
    if rule.get("enabled", False):
        hit, reason, m = check_listing_months(
            daily, int(rule.get("months", 12)), today,
            int(rule.get("lookback_years", 3)))
        metrics.update(m)
        if hit:
            return FilterResult(code, name, False, "min_listing_months", reason, metrics)

    # ③ 流动性
    rule = rules.get("min_avg_amount", {})
    if rule.get("enabled", False):
        hit, reason, m = check_avg_amount(
            daily,
            float(rule.get("min_amount_yuan", 5e7)),
            int(rule.get("lookback_days", 20)),
        )
        metrics.update(m)
        if hit:
            return FilterResult(code, name, False, "min_avg_amount", reason, metrics)

    # ④ 停牌
    rule = rules.get("exclude_suspended", {})
    if rule.get("enabled", False):
        hit, reason, m = check_suspended(daily, int(rule.get("no_trade_days", 5)))
        metrics.update(m)
        if hit:
            return FilterResult(code, name, False, "exclude_suspended", reason, metrics)

    return FilterResult(code, name, True, metrics=metrics)


def summarize(results: list[FilterResult], rules: dict) -> dict:
    """统计每条规则排除了多少只。"""
    total = len(results)
    passed = sum(1 for r in results if r.passed)

    by_rule: dict[str, int] = {}
    for r in results:
        if not r.passed and r.reject_rule:
            by_rule[r.reject_rule] = by_rule.get(r.reject_rule, 0) + 1

    return {
        "total": total,
        "passed": passed,
        "rejected": total - passed,
        "by_rule": by_rule,
        "rules_enabled": [k for k, v in rules.items() if v.get("enabled")],
    }
