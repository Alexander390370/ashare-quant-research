"""PIT 财务数据：每个调仓日只用「当时已公布」的财报。

问题
----
之前的回测用 2026-09-30 的财报选 2023-11 的股票——**前视 2.8 年**。
这会让所有财务因子策略看起来有效，实际完全不可信。

解决方案
--------
财务数据带**报告期**（report_date），配合**法定披露截止日**推出「可得日」：

    报告期      法定披露截止      可得日（保守估计）
    03-31       04-30            04-30
    06-30       08-31            08-31
    09-30       10-31            10-31
    12-31       次年 04-30       次年 04-30

然后每个调仓日 T，只使用 `可得日 <= T` 的最新一期财报。

数据要求
--------
需要**多期**财务数据（不只是最新一期）。
东财 `stock_financial_analysis_indicator` 返回多期（实测 15 期，2023Q1~2026Q3）。

但只有 2023 年起的数据，覆盖不了 2018-2022。
所以 PIT 回测只能做 2023-2026 区间——**这正好是样本内，无法做样本外。**

诚实结论
--------
- PIT 能修正 2023-2026 内部的前视
- 但无法扩展到 2018-2022（无历史财务数据）
- 所以样本外验证财务因子在免费数据条件下**做不到**

用法
----
    python -m qlib_layer.pit_financials                # 测试
    python -m qlib_layer.pit_financials --check-backtest  # 用 PIT 重跑回测
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tradingagents  # noqa: E402,F401

logger = logging.getLogger(__name__)

CACHE_DIR = _REPO_ROOT / "run_data" / "cache" / "financials_pit"


class PITError(RuntimeError):
    """PIT 数据获取失败。"""


def disclosure_deadline(report_date: date) -> date:
    """从报告期推出法定披露截止日（保守估计）。

    中国证监会规定：
        一季报：报告期结束后 1 个月内（4-30）
        半年报：报告期结束后 2 个月内（8-31）
        三季报：报告期结束后 1 个月内（10-31）
        年报：  次年 4-30
    """
    m = report_date.month
    y = report_date.year

    if m == 3:
        return date(y, 4, 30)
    if m == 6:
        return date(y, 8, 31)
    if m == 9:
        return date(y, 10, 31)
    if m == 12:
        return date(y + 1, 4, 30)
    # 其他月份（不应出现），保守给 4 个月
    return report_date + timedelta(days=120)


def _parse_report_dates(frame: pd.DataFrame) -> pd.DataFrame:
    """解析多期财务数据的报告期列。"""
    if "日期" in frame.columns:
        col = "日期"
    elif "报告期" in frame.columns:
        col = "报告期"
    else:
        return frame

    frame = frame.copy()
    frame["_report_date"] = pd.to_datetime(frame[col], errors="coerce").dt.date
    frame = frame.dropna(subset=["_report_date"])
    frame = frame.sort_values("_report_date")
    frame["_available_date"] = frame["_report_date"].apply(disclosure_deadline)
    return frame


def fetch_pit_history(code: str, start_year: str = "2020") -> pd.DataFrame:
    """拉单只股票的多期财务数据，带可得日。

    返回 DataFrame：每行一期财报，含 _report_date / _available_date / 各指标
    """
    import akshare as ak

    last: Exception | None = None
    for attempt in range(1, 3):
        try:
            frame = ak.stock_financial_analysis_indicator(
                symbol=code, start_year=start_year)
            if frame is None or len(frame) == 0:
                return pd.DataFrame()
            return _parse_report_dates(frame)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < 2:
                time.sleep(0.8)

    raise PITError(f"{code} 财务历史失败：{type(last).__name__}")


# 字段映射（中文 -> 英文）
FIELD_MAP = {
    "摊薄每股收益(元)": "eps",
    "每股净资产_调整后(元)": "bps",
    "净资产收益率(%)": "roe",
    "加权净资产收益率(%)": "roe_weighted",
    "销售毛利率(%)": "gross_margin",
    "销售净利率(%)": "net_margin",
    "资产负债率(%)": "debt_ratio",
    "流动比率": "current_ratio",
    "总资产(元)": "total_assets",
    "净资产(元)": "net_assets",
}


def _parse_num(v) -> float | None:
    """解析中文数字（可能带亿/万/%）。"""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    s = str(v).strip().replace(",", "")
    if not s or s in ("--", "nan", "None"):
        return None
    mult = 1.0
    if "亿" in s:
        mult = 1e8
        s = s.replace("亿", "")
    elif "万" in s:
        mult = 1e4
        s = s.replace("万", "")
    s = s.replace("%", "").replace("元", "").strip()
    try:
        return float(s) * mult
    except (ValueError, TypeError):
        return None


def build_pit_table(code: str, start_year: str = "2020") -> pd.DataFrame:
    """构建单只股票的 PIT 财务表。

    返回列：code, report_date, available_date, roe, gross_margin, debt_ratio, ...
    """
    raw = fetch_pit_history(code, start_year)
    if len(raw) == 0:
        return pd.DataFrame()

    rows = []
    for _, r in raw.iterrows():
        row = {
            "code": code,
            "report_date": r["_report_date"],
            "available_date": r["_available_date"],
        }
        for cn, en in FIELD_MAP.items():
            if cn in raw.columns:
                row[en] = _parse_num(r[cn])
        rows.append(row)

    return pd.DataFrame(rows)


def get_pit_financials_at(
    pit_table: pd.DataFrame,
    as_of: date,
) -> dict | None:
    """取某个时点「已公布」的最新一期财报。

    参数
    ----
    pit_table: build_pit_table 的输出
    as_of: 调仓日

    返回
    ----
    该时点可得的最新财报（dict），或 None（无可用财报）
    """
    if pit_table is None or len(pit_table) == 0:
        return None

    available = pit_table[pit_table["available_date"] <= as_of]
    if len(available) == 0:
        return None

    # 取报告期最新的
    latest = available.sort_values("report_date").iloc[-1]
    return latest.to_dict()


def build_pit_dataset(
    codes: list[str],
    start_year: str = "2020",
    workers: int = 10,
    progress=None,
) -> dict[str, pd.DataFrame]:
    """批量构建 PIT 数据。

    返回 {code: pit_table}
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def task(code: str):
        cache_file = CACHE_DIR / f"{code}.parquet"
        if cache_file.exists():
            try:
                return code, pd.read_parquet(cache_file)
            except Exception:  # noqa: BLE001
                pass
        try:
            table = build_pit_table(code, start_year)
            if len(table) > 0:
                try:
                    table.to_parquet(cache_file, index=False)
                except Exception:  # noqa: BLE001
                    pass
            return code, table
        except PITError:
            return code, pd.DataFrame()

    result = {}
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(task, c): c for c in codes}
        for future in as_completed(futures):
            code, table = future.result()
            done += 1
            if table is not None and len(table) > 0:
                result[code] = table
            if progress and (done % 100 == 0 or done == len(codes)):
                progress(done, len(codes), code)

    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="PIT 财务数据")
    parser.add_argument("--check-backtest", action="store_true")
    parser.add_argument("--codes", default="600519,000001,300750")
    args = parser.parse_args(argv[1:])

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    codes = [c.strip() for c in args.codes.split(",")]

    print("=" * 88)
    print("PIT 财务数据测试")
    print("=" * 88)

    for code in codes:
        print(f"\n--- {code} ---")
        try:
            table = build_pit_table(code)
            if len(table) == 0:
                print("  无数据")
                continue

            print(f"  共 {len(table)} 期")
            print(f"  报告期范围：{table['report_date'].min()} ~ {table['report_date'].max()}")
            print(f"  可得日范围：{table['available_date'].min()} ~ {table['available_date'].max()}")
            print()

            # 演示：不同时点用不同财报
            for as_of in [date(2023, 12, 1), date(2024, 6, 1),
                          date(2025, 6, 1), date(2026, 10, 1)]:
                snap = get_pit_financials_at(table, as_of)
                if snap:
                    print(f"  as_of {as_of}: 报告期 {snap['report_date']} "
                          f"(可得 {snap['available_date']}) "
                          f"ROE={snap.get('roe')}")
                else:
                    print(f"  as_of {as_of}: 无可用财报")
        except PITError as exc:
            print(f"  ❌ {exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
