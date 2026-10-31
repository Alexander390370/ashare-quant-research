"""三个缺口的验收脚本。

1. 港股财报（0700.HK 三张表）
2. A 股 point-in-time（巨潮公告 + 按分析日期过滤）
3. 中国宏观（免 key，六个指标）
"""

# 允许从任意目录直接运行：把仓库根目录放进 sys.path。
import sys
import warnings
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

warnings.filterwarnings("ignore")

import tradingagents  # noqa: E402,F401  触发 netfix
from tradingagents.agents import tools as T  # noqa: E402
from tradingagents.dataflows.vendors.akshare.disclosure import (  # noqa: E402
    get_announcements,
    is_report_available,
)
from tradingagents.dataflows.vendors.akshare.macro import get_macro_china  # noqa: E402

print("=" * 74)
print("缺口一：港股财报")
print("=" * 74)
for name, fn in [
    ("balance_sheet", lambda: T.get_balance_sheet.invoke(
        {"ticker": "0700.HK", "freq": "quarterly", "curr_date": "2026-10-27"})),
    ("cashflow", lambda: T.get_cashflow.invoke(
        {"ticker": "0700.HK", "freq": "quarterly", "curr_date": "2026-10-27"})),
    ("income_statement", lambda: T.get_income_statement.invoke(
        {"ticker": "0700.HK", "freq": "quarterly", "curr_date": "2026-10-27"})),
]:
    try:
        out = str(fn()).strip()
        ok = not out.startswith("DATA_UNAVAILABLE")
        print(f"  {name:18s} {'OK' if ok else 'FAIL':5s} {len(out):6d} chars")
    except Exception as exc:
        print(f"  {name:18s} FAIL  {type(exc).__name__}: {str(exc)[:50]}")

print()
print("=" * 74)
print("缺口二：A 股 point-in-time")
print("=" * 74)
try:
    items = get_announcements("600519.SS", "2026-01-01", "2026-10-27")
    print(f"  公告总数: {len(items)}")
    reports = [i for i in items if i["kind"] == "report"]
    print(f"  定期报告: {len(reports)}")
    for item in reports[:5]:
        print(f"    {item['published']}  {item['title'][:45]}")

    print()
    print("  point-in-time 判定（半年报 2026-06-30，公告日 2026-08-15）：")
    for as_of in ["2026-07-01", "2026-08-14", "2026-08-15", "2026-10-27"]:
        available = is_report_available("600519.SS", "2026-06-30", as_of)
        flag = "可见" if available else "不可见"
        print(f"    分析日 {as_of} -> {flag}")
except Exception as exc:
    print(f"  FAIL {type(exc).__name__}: {str(exc)[:80]}")

print()
print("=" * 74)
print("缺口三：中国宏观（免 key）")
print("=" * 74)
for ind in ["ppi", "pmi", "m2", "gdp", "lpr", "fx"]:
    try:
        out = get_macro_china(ind, 3)
        print(f"  {ind:4s} OK  {len(out):5d} chars")
    except Exception as exc:
        print(f"  {ind:4s} FAIL {type(exc).__name__}: {str(exc)[:50]}")

print()
print("=" * 74)
print("回归：A 股 / 美股不受影响")
print("=" * 74)
for tk in ["600519.SS", "AAPL"]:
    try:
        out = str(T.get_cashflow.invoke(
            {"ticker": tk, "freq": "quarterly", "curr_date": "2026-10-27"})).strip()
        ok = not out.startswith("DATA_UNAVAILABLE")
        print(f"  {tk:12s} cashflow {'OK' if ok else 'FAIL':5s} {len(out):6d} chars")
    except Exception as exc:
        print(f"  {tk:12s} FAIL {type(exc).__name__}")
