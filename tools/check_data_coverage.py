"""数据链路覆盖度诊断工具。

用法：
    python tools/check_data_coverage.py [TICKER ...]

默认检查 600519.SS（A 股）与 AAPL（美股）做对照，逐个调用四个分析师实际
会用的 10 个数据工具，输出 成功/失败/空 表格。

用途：任何"某只标的分析结果怪怪的"的时刻，先跑这个，确认是数据层还是模型层。
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

# 允许从任意目录直接运行：把仓库根目录放进 sys.path。
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

warnings.filterwarnings("ignore")

import tradingagents  # noqa: E402,F401  触发 netfix（网络开关 + 数据目录重定向）
from tradingagents.agents import tools as T  # noqa: E402

DEFAULT_TICKERS = ["600519.SS", "AAPL"]


def build_cases(ticker: str, start: str, end: str, curr: str):
    """返回 (分析师, 工具名, 调用函数) 三元组列表。"""
    return [
        ("技术", "get_stock_data", lambda: T.get_stock_data.invoke(
            {"symbol": ticker, "start_date": start, "end_date": end, "trade_date": curr})),
        ("技术", "get_indicators", lambda: T.get_indicators.invoke(
            {"symbol": ticker, "indicator": "rsi", "curr_date": curr,
             "look_back_days": 20, "trade_date": curr})),
        ("技术", "market_snapshot", lambda: T.get_verified_market_snapshot.invoke(
            {"symbol": ticker, "curr_date": curr, "look_back_days": 20, "trade_date": curr})),
        ("基本面", "get_fundamentals", lambda: T.get_fundamentals.invoke(
            {"ticker": ticker, "curr_date": curr})),
        ("基本面", "get_balance_sheet", lambda: T.get_balance_sheet.invoke(
            {"ticker": ticker, "freq": "quarterly", "curr_date": curr})),
        ("基本面", "get_cashflow", lambda: T.get_cashflow.invoke(
            {"ticker": ticker, "freq": "quarterly", "curr_date": curr})),
        ("基本面", "get_income_statement", lambda: T.get_income_statement.invoke(
            {"ticker": ticker, "freq": "quarterly", "curr_date": curr})),
        ("新闻", "get_news", lambda: T.get_news.invoke(
            {"ticker": ticker, "start_date": start, "end_date": end})),
        ("新闻", "get_global_news", lambda: T.get_global_news.invoke(
            {"curr_date": curr, "look_back_days": 7, "limit": 10})),
        ("情绪", "get_insider_transactions", lambda: T.get_insider_transactions.invoke(
            {"ticker": ticker})),
    ]


def classify(out) -> tuple[str, str]:
    """把工具返回值归类为 (状态, 说明)。"""
    text = "" if out is None else str(out)
    stripped = text.strip()
    if not stripped:
        return "EMPTY", "返回空"
    low = stripped[:400].lower()
    if any(k in low for k in ("data_unavailable", "no data", "unavailable", "not available")):
        return "SOFT-FAIL", stripped.splitlines()[0][:54]
    return "OK", f"{len(text)} chars | {stripped.splitlines()[0][:42]}"


def check(ticker: str, start: str, end: str, curr: str) -> tuple[int, int]:
    print("=" * 84)
    print(f"数据链路覆盖度  ticker={ticker}  ({start} ~ {end})")
    print("=" * 84)
    print("%-8s %-24s %-10s %s" % ("分析师", "工具", "结果", "说明"))  # noqa: UP031
    print("-" * 84)
    ok = total = 0
    for group, name, fn in build_cases(ticker, start, end, curr):
        total += 1
        try:
            status, note = classify(fn())
            if status == "OK":
                ok += 1
        except Exception as exc:  # noqa: BLE001 - 诊断工具，任何异常都要显示
            status, note = "FAIL", f"{type(exc).__name__}: {str(exc)[:54]}"
        print(f"{group:<8} {name:<24} {status:<10} {note}")
    print("-" * 84)
    print(f"小计: {ok}/{total} 可用\n")
    return ok, total


def main(argv: list[str]) -> int:
    tickers = argv[1:] or DEFAULT_TICKERS
    curr = "2026-10-26"
    start, end = "2026-09-26", curr
    failures = 0
    for ticker in tickers:
        ok, total = check(ticker, start, end, curr)
        failures += total - ok
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
