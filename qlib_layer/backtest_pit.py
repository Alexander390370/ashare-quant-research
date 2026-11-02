"""PIT 财务因子回测：用「当时可得」的财报，消除前视偏差。

与 backtest_financial.py 的区别
------------------------------
backtest_financial.py：
    用 2026-09-30 的财报选 2023-11 的股票 -> **前视 2.8 年**，结果不可信

本模块：
    每个调仓日 T，只用 `可得日 <= T` 的最新财报 -> 无前视

数据条件
--------
东财财务接口提供 2020Q1 起的多期数据（实测 27 期，2020-03-31 ~ 2026-09-30）。
所以 PIT 回测可以覆盖 2020-2026（约 6 年），比只用最新数据好得多。

但仍有的限制
------------
- 无法覆盖 2020 年之前（无更早财务数据）
- 股票池仍是「2026 年当前成分股」-> 幸存者偏差仍在
- 这两条无法用免费数据解决

用法
----
    python -m qlib_layer.backtest_pit
    python -m qlib_layer.backtest_pit --top 30 --start 2020-06-01
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tradingagents  # noqa: E402,F401

from qlib_layer import neutralize, pit_financials  # noqa: E402

logger = logging.getLogger(__name__)

FEE_RATE = 0.0003
STAMP_TAX = 0.0005
SLIPPAGE = 0.001

SCREENING_OUTPUT = _REPO_ROOT / "screening" / "output"


# 策略定义（只用 PIT 可得的字段）
PIT_STRATEGIES = {
    "quality": (
        {"roe": 1.0, "gross_margin": 1.0, "debt_ratio": -1.0},
        "质量：ROE + 毛利率 - 负债率",
    ),
    "quality_only": (
        {"roe": 1.0},
        "纯 ROE",
    ),
    "margin": (
        {"gross_margin": 1.0, "net_margin": 1.0},
        "毛利率 + 净利率",
    ),
    "low_debt": (
        {"debt_ratio": -1.0},
        "低负债率",
    ),
}


@dataclass
class PITResult:
    dates: list[str] = field(default_factory=list)
    portfolio: list[float] = field(default_factory=list)
    bench_equal: list[float] = field(default_factory=list)
    bench_random: list[float] = field(default_factory=list)
    turnover: list[float] = field(default_factory=list)
    ic_series: list[tuple[str, float]] = field(default_factory=list)
    holdings: dict[str, list[str]] = field(default_factory=dict)
    stats: dict = field(default_factory=dict)


def load_candidates() -> tuple[pd.DataFrame, str]:
    dirs = sorted(
        [d for d in SCREENING_OUTPUT.iterdir() if d.is_dir()],
        key=lambda d: d.name, reverse=True)
    if not dirs:
        raise FileNotFoundError(f"找不到筛选输出：{SCREENING_OUTPUT}")
    run_date = dirs[0].name
    path = dirs[0] / "candidates.csv"
    df = pd.read_csv(path, encoding="utf-8-sig", dtype={"code": str})
    return df, run_date


def run_pit_backtest(
    candidates: pd.DataFrame,
    strategy_name: str,
    top_n: int = 30,
    max_stocks: int = 500,
    start_date: str = "2020-06-01",
    end_date: str | None = None,
    include_cost: bool = True,
    n_bootstrap: int = 100,
) -> PITResult:
    """用 PIT 财务数据跑回测。"""
    import bisect

    from screening import cache, fetch

    if strategy_name not in PIT_STRATEGIES:
        raise ValueError(f"未知策略：{strategy_name}")

    weights, desc = PIT_STRATEGIES[strategy_name]
    print(f"  策略：{desc}")

    # 粗筛
    if len(candidates) > max_stocks:
        sort_col = ("avg_amount_yi" if "avg_amount_yi" in candidates.columns
                    else "market_cap_yi")
        if sort_col in candidates.columns:
            candidates = candidates.nlargest(max_stocks, sort_col)
    print(f"  股票池：{len(candidates)} 只")

    codes = candidates["code"].tolist()

    # --- 1. 拉 PIT 财务数据 ---
    print(f"  拉 PIT 财务数据（{len(codes)} 只 × 27 期）…")
    pit_data = pit_financials.build_pit_dataset(
        codes, start_year="2020", workers=10,
        progress=lambda i, t, c: print(f"    {i}/{t}") if i % 100 == 0 else None,
    )
    print(f"  PIT 数据：{len(pit_data)} 只")

    if len(pit_data) < top_n * 3:
        raise ValueError(f"PIT 数据太少（{len(pit_data)}）")

    # --- 2. 拉日线（并发）---
    print("  拉日线（并发 10）…")
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # 预建 code -> exchange 映射（避免每只都做 pandas 过滤）
    ex_map = {}
    if "exchange" in candidates.columns:
        ex_map = dict(zip(candidates["code"], candidates["exchange"], strict=False))

    def _fetch_one(code: str):
        ex = ex_map.get(code, "")
        if not ex or pd.isna(ex):
            if code.startswith("6"):
                ex = "上海证券交易所"
            elif code.startswith(("0", "3")):
                ex = "深圳证券交易所"
            else:
                return code, None
        try:
            frame = cache.fetch_with_cache(
                code, ex, date(2019, 1, 1),
                date.today(), fetch.fetch_daily, adjust="qfq")
            if frame is None or len(frame) == 0:
                return code, None
            g = frame.sort_values("date")
            return code, {
                "dates": [str(d)[:10] for d in g["date"]],
                "close": g["close"].astype(float).values,
            }
        except Exception:  # noqa: BLE001
            return code, None

    by_code = {}
    done = 0
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(_fetch_one, c): c for c in pit_data}
        for future in as_completed(futures):
            code, data = future.result()
            done += 1
            if data is not None:
                by_code[code] = data
            if done % 100 == 0:
                print(f"    {done}/{len(pit_data)}")

    print(f"  日线：{len(by_code)} 只")

    if len(by_code) < top_n * 2:
        raise ValueError(f"日线太少（{len(by_code)}）")

    # --- 3. 调仓日 ---
    all_dates = sorted({d for e in by_code.values() for d in e["dates"]})
    month_ends = [
        all_dates[i] for i in range(len(all_dates) - 1)
        if all_dates[i][:7] != all_dates[i + 1][:7]
    ]
    month_ends = [d for d in month_ends if d >= start_date]
    if end_date:
        month_ends = [d for d in month_ends if d <= end_date]

    if len(month_ends) < 6:
        raise ValueError(f"调仓日太少（{len(month_ends)}）")

    print(f"  调仓日：{len(month_ends)} 个（{month_ends[0]} ~ {month_ends[-1]}）")

    def get_ret(code: str, d0: str, d1: str) -> float | None:
        e = by_code.get(code)
        if e is None:
            return None
        i0 = bisect.bisect_left(e["dates"], d0)
        i1 = bisect.bisect_right(e["dates"], d1) - 1
        if i1 <= i0 or i0 >= len(e["dates"]) or i1 >= len(e["dates"]):
            return None
        c0, c1 = e["close"][i0], e["close"][i1]
        if np.isnan(c0) or np.isnan(c1) or c0 <= 0:
            return None
        return float(c1 / c0 - 1)

    def is_tradable(code: str, d: str) -> bool:
        e = by_code.get(code)
        if e is None:
            return False
        i = bisect.bisect_left(e["dates"], d)
        if i >= len(e["dates"]) or e["dates"][i] != d:
            return False
        if i == 0:
            return True
        c_prev, c_now = e["close"][i - 1], e["close"][i]
        if np.isnan(c_prev) or np.isnan(c_now) or c_prev <= 0:
            return False
        chg = c_now / c_prev - 1
        lim = 0.20 if code.startswith(("300", "301", "688", "689")) else 0.10
        return not (chg >= lim * 0.98 or chg <= -lim * 0.98)

    # **性能优化**：预建 PIT 索引 {code: (sorted_available_dates, rows)}
    print("  构建 PIT 索引…")
    pit_index = {}
    for code, table in pit_data.items():
        if table is None or len(table) == 0:
            continue
        t_sorted = table.sort_values("available_date")
        avail_list = [d for d in t_sorted["available_date"]]
        rows = t_sorted.to_dict("records")
        pit_index[code] = (avail_list, rows)
    print(f"  PIT 索引：{len(pit_index)} 只")

    # 行业映射
    industry_map = {}
    if "industry_final" in candidates.columns:
        industry_map = dict(zip(
            candidates["code"], candidates["industry_final"], strict=False))

    # --- 4. 逐期回测 ---
    result = PITResult()
    prev_holdings: set[str] = set()
    rng = np.random.default_rng(42)
    pit_used_dates: list[tuple[str, str]] = []

    for idx, reb in enumerate(month_ends[:-1]):
        nxt = month_ends[idx + 1]
        reb_dt = date.fromisoformat(reb)

        # **核心**：取该时点已公布的财报
        # 性能优化：用预建的 (available_date 排序列表 + 行数据) 做二分查找，
        # 而不是每期对每只股票做 pandas 过滤（500 只 × 70 期 = 3.5 万次）
        snapshots = {}
        report_dates_used = []
        for code, (avail_list, rows) in pit_index.items():
            pos = bisect.bisect_right(avail_list, reb_dt)
            if pos == 0:
                continue
            snap = rows[pos - 1]
            snapshots[code] = snap
            report_dates_used.append(str(snap.get("report_date", "")))

        if len(snapshots) < top_n * 3:
            continue

        # 记录本期用的财报期（用于审计）
        if report_dates_used:
            from collections import Counter

            common = Counter(report_dates_used).most_common(1)[0][0]
            pit_used_dates.append((reb, common))

        # 打分（行业中性化）
        df = pd.DataFrame.from_dict(snapshots, orient="index")
        df["code"] = df.index
        df["industry_final"] = df["code"].map(industry_map).fillna("未知")

        scored = neutralize.neutralize_composite(
            df.reset_index(drop=True), weights, use_rank=True)

        if "composite_neutral" not in scored.columns:
            continue

        scored = scored[scored["composite_neutral"].notna()]
        if len(scored) < top_n * 2:
            continue

        # 可交易过滤
        tradable_df = scored[scored["code"].apply(lambda c: is_tradable(c, reb))]
        if len(tradable_df) < top_n:
            continue

        ranked = tradable_df.sort_values("composite_neutral", ascending=False)
        picks = ranked["code"].tolist()[:top_n]
        holdings = set(picks)

        # 收益
        port_rets = [r for c in picks if (r := get_ret(c, reb, nxt)) is not None]
        if not port_rets:
            continue
        port_ret = float(np.mean(port_rets))

        # 基准
        tradable_codes = tradable_df["code"].tolist()
        eq_rets = [r for c in tradable_codes if (r := get_ret(c, reb, nxt)) is not None]
        bench_equal = float(np.mean(eq_rets)) if eq_rets else np.nan

        rand_rets = []
        for _ in range(min(n_bootstrap, 50)):
            sample = rng.choice(len(tradable_codes), size=top_n, replace=False)
            s_rets = [r for i in sample
                      if (r := get_ret(tradable_codes[i], reb, nxt)) is not None]
            if s_rets:
                rand_rets.append(float(np.mean(s_rets)))
        bench_random = float(np.mean(rand_rets)) if rand_rets else np.nan

        # 成本
        if include_cost:
            new_pos = holdings - prev_holdings
            closed = prev_holdings - holdings
            buy = len(new_pos) / max(len(holdings), 1) * (FEE_RATE + SLIPPAGE)
            sell = (len(closed) / max(len(prev_holdings), 1) *
                    (FEE_RATE + STAMP_TAX + SLIPPAGE)) if prev_holdings else 0.0
            port_ret -= (buy + sell)

        turnover = len(holdings ^ prev_holdings) / max(len(holdings), 1)

        # IC
        fwd = {}
        for c in tradable_codes:
            r = get_ret(c, reb, nxt)
            if r is not None:
                fwd[c] = r
        common = [c for c in tradable_codes if c in fwd]
        if len(common) >= 30:
            from scipy.stats import spearmanr

            score_map = dict(zip(
                ranked["code"], ranked["composite_neutral"], strict=False))
            s_vals = np.array([score_map[c] for c in common])
            r_vals = np.array([fwd[c] for c in common])
            ic, _ = spearmanr(s_vals, r_vals)
            if not np.isnan(ic):
                result.ic_series.append((reb, float(ic)))

        result.dates.append(reb)
        result.portfolio.append(port_ret)
        result.bench_equal.append(bench_equal)
        result.bench_random.append(bench_random)
        result.turnover.append(turnover)
        result.holdings[reb] = sorted(holdings)
        prev_holdings = holdings

    result.stats = _stats(result, strategy_name, desc, top_n, pit_used_dates)
    return result


def _stats(r: PITResult, name: str, desc: str,
           top_n: int, pit_dates: list[tuple[str, str]]) -> dict:
    if not r.portfolio:
        return {"error": "无有效数据", "strategy": name}

    port = np.array(r.portfolio)
    eq = np.array(r.bench_equal)
    rnd = np.array(r.bench_random)
    to = np.array(r.turnover)

    def cum(a):
        a = a[~np.isnan(a)]
        return float(np.prod(1 + a) - 1) if len(a) else None

    def excess(bench):
        m = ~np.isnan(port) & ~np.isnan(bench)
        if m.sum() < 3:
            return None
        return float(np.prod(1 + (port[m] - bench[m])) - 1)

    ic_vals = [ic for _, ic in r.ic_series]
    ic_mean = float(np.mean(ic_vals)) if ic_vals else 0.0
    ic_std = float(np.std(ic_vals, ddof=1)) if len(ic_vals) > 1 else 0.0
    ic_t = (ic_mean / ic_std * np.sqrt(len(ic_vals))) if ic_std > 0 else 0.0

    equity = np.cumprod(1 + port)
    peak = np.maximum.accumulate(equity)
    mdd = float((equity / peak - 1).min())

    return {
        "strategy": name,
        "description": desc,
        "periods": len(port),
        "top_n": top_n,
        "pit": True,
        "cum_portfolio": round(cum(port), 4) if cum(port) is not None else None,
        "cum_bench_equal": round(cum(eq), 4) if cum(eq) is not None else None,
        "cum_bench_random": round(cum(rnd), 4) if cum(rnd) is not None else None,
        "excess_vs_equal": round(excess(eq), 4) if excess(eq) is not None else None,
        "excess_vs_random": round(excess(rnd), 4) if excess(rnd) is not None else None,
        "max_drawdown": round(mdd, 4),
        "avg_turnover": round(float(np.nanmean(to)), 4),
        "ic_mean": round(ic_mean, 4),
        "ic_std": round(ic_std, 4),
        "ic_t_stat": round(float(ic_t), 2),
        "ic_periods": len(ic_vals),
        "ic_significant": bool(abs(ic_t) > 1.96),
        "pit_dates_sample": pit_dates[:5],
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="PIT 财务因子回测")
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--max-stocks", type=int, default=500)
    parser.add_argument("--start", default="2020-06-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--strategy", default="all")
    args = parser.parse_args(argv[1:])

    logging.basicConfig(level=logging.WARNING)

    print("=" * 92)
    print("PIT 财务因子回测（无前视）")
    print("=" * 92)

    try:
        candidates, run_date = load_candidates()
    except FileNotFoundError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1

    print(f"候选池：{len(candidates)} 只（{run_date}）")
    print()

    strategies = (list(PIT_STRATEGIES) if args.strategy == "all"
                  else [args.strategy])

    results = []
    for name in strategies:
        print(f"[{name}] {PIT_STRATEGIES[name][1]}")
        try:
            r = run_pit_backtest(
                candidates, name,
                top_n=args.top,
                max_stocks=args.max_stocks,
                start_date=args.start,
                end_date=args.end,
            )
            results.append(r)
            s = r.stats
            if "error" not in s:
                print(f"  期数 {s['periods']} | 累计 {s['cum_portfolio']:+.2%} | "
                      f"vs随机 {s['excess_vs_random']:+.2%} | "
                      f"IC {s['ic_mean']:+.4f} (t={s['ic_t_stat']:+.2f})")
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ {type(exc).__name__}: {str(exc)[:80]}")
        print()

    if not results:
        return 1

    # 汇总
    print("=" * 92)
    print("汇总（PIT 无前视）")
    print("=" * 92)
    print()
    print(f"  {'策略':<16} {'期数':>5} {'累计':>10} {'vs等权':>10} "
          f"{'vs随机':>10} {'回撤':>8} {'IC':>8} {'t值':>7} {'显著':>5}")
    print("  " + "-" * 88)

    for r in results:
        s = r.stats
        if "error" in s:
            print(f"  {s['strategy']:<16} {s['error']}")
            continue
        print(f"  {s['strategy']:<16} {s['periods']:>5} "
              f"{s['cum_portfolio']:>+9.2%} "
              f"{(s['excess_vs_equal'] or 0):>+9.2%} "
              f"{(s['excess_vs_random'] or 0):>+9.2%} "
              f"{s['max_drawdown']:>7.2%} "
              f"{s['ic_mean']:>+8.4f} {s['ic_t_stat']:>+7.2f} "
              f"{'是' if s['ic_significant'] else '否':>5}")

    # 判断
    print()
    print("=" * 92)
    print("判断")
    print("=" * 92)
    print()

    valid = [r for r in results if "error" not in r.stats]
    sig = [r for r in valid if r.stats.get("ic_significant")]

    if sig:
        print(f"  ✅ **{len(sig)}/{len(valid)} 个策略在 PIT 条件下仍统计显著**")
        print()
        for r in sig:
            s = r.stats
            print(f"     {s['strategy']:16s} t={s['ic_t_stat']:+.2f}  "
                  f"vs随机 {s['excess_vs_random']:+.2%}  "
                  f"回撤 {s['max_drawdown']:.2%}")
        print()
        print("  这意味着：**排除前视偏差后，财务因子仍有预测力。**")
        print()
        print("  但仍需注意：")
        print("    1. 幸存者偏差仍在（股票池是 2026 年当前成分股）")
        print("    2. 样本期 2020-2026，未覆盖 2018-2019")
        print("    3. 未做真正的样本外（全部数据都参与了这次检验）")
    else:
        print(f"  ❌ **所有策略在 PIT 条件下都不显著**")
        print()
        print("  这说明之前看到的 t>1.96 主要是前视偏差造成的。")
        print("  **财务因子在修正前视后，没有可验证的预测力。**")

    print()
    print("=" * 92)

    # 落盘
    out_dir = _REPO_ROOT / "qlib_layer" / "output" / "backtest_pit"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(
        json.dumps([r.stats for r in results], ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    print(f"输出：{out_dir}")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
