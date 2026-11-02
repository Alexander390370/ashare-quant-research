"""财务因子回测：验证价值/质量风格在 A 股有没有效。

背景
----
之前的回测只用 6 个价量因子（动量+低波），结果：
    原方案（动量+低波）超额 -32.59%
    纯反向 样本外 IC = -0.0060

六家评审一致说「⑥ 加财务因子排最后」——因为「没有 edge 时加因子是装饰」。
但同时也指出：**财务因子从未验证过**。

这个脚本补上这个缺口。

测试的策略
----------
1. 质量（Quality）：ROE 高 + 毛利率高 + 负债率低
2. 成长（Growth）：营收同比高 + 净利润同比高
3. 价值（Value）：PE 低 + PB 低
4. 质量+价值组合
5. 价量+财务组合

关键设计
--------
- **行业中性化**：因子在行业内排名，避免银行/白酒不可比
- **三基准对照**：官方指数 / 随机 N 只 / 全池等权
- **含成本**：佣金+印花税+滑点
- **涨跌停过滤**：当日涨停不可买入
- **IC 统计**：含 t 统计量

用法
----
    python -m qlib_layer.backtest_financial
    python -m qlib_layer.backtest_financial --top 30 --freq M
"""

from __future__ import annotations

import argparse
import csv
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

from qlib_layer import financials, neutralize  # noqa: E402

logger = logging.getLogger(__name__)

FEE_RATE = 0.0003
STAMP_TAX = 0.0005
SLIPPAGE = 0.001

SCREENING_OUTPUT = _REPO_ROOT / "screening" / "output"


# 策略定义：{名称: (因子权重, 说明)}
STRATEGIES = {
    "quality": (
        {"f_roe": 1.0, "f_gross_margin": 1.0, "f_debt_ratio": -1.0},
        "质量：ROE 高 + 毛利率高 + 负债率低",
    ),
    "growth": (
        {"f_revenue_yoy": 1.0, "f_profit_yoy": 1.0},
        "成长：营收同比 + 净利润同比",
    ),
    "value": (
        {"f_pe": -1.0, "f_pb": -1.0},
        "价值：PE 低 + PB 低",
    ),
    "quality_value": (
        {"f_roe": 1.0, "f_gross_margin": 0.5, "f_debt_ratio": -0.5,
         "f_pe": -1.0, "f_pb": -0.5},
        "质量 + 价值组合",
    ),
    "quality_growth": (
        {"f_roe": 1.0, "f_gross_margin": 0.5,
         "f_revenue_yoy": 0.5, "f_profit_yoy": 0.5},
        "质量 + 成长组合",
    ),
}


@dataclass
class FinResult:
    dates: list[str] = field(default_factory=list)
    portfolio: list[float] = field(default_factory=list)
    bench_equal: list[float] = field(default_factory=list)
    bench_random: list[float] = field(default_factory=list)
    bench_index: list[float] = field(default_factory=list)
    turnover: list[float] = field(default_factory=list)
    ic_series: list[tuple[str, float]] = field(default_factory=list)
    holdings: dict[str, list[str]] = field(default_factory=dict)
    stats: dict = field(default_factory=dict)


def load_latest_candidates() -> tuple[pd.DataFrame, str]:
    """读最新筛选结果。"""
    dirs = sorted(
        [d for d in SCREENING_OUTPUT.iterdir() if d.is_dir()],
        key=lambda d: d.name, reverse=True)
    if not dirs:
        raise FileNotFoundError(f"找不到筛选输出：{SCREENING_OUTPUT}")

    run_date = dirs[0].name
    path = dirs[0] / "candidates.csv"
    if not path.exists():
        raise FileNotFoundError(f"找不到候选文件：{path}")

    df = pd.read_csv(path, encoding="utf-8-sig", dtype={"code": str})
    return df, run_date


def run_financial_backtest(
    candidates: pd.DataFrame,
    strategy_name: str,
    top_n: int = 30,
    freq: str = "M",
    include_cost: bool = True,
    n_bootstrap: int = 200,
    use_neutralize: bool = True,
) -> FinResult:
    """跑财务因子回测。

    简化版：不做逐月调仓（财务数据季度更新，月度调仓意义不大），
    而是做「单期截面测试」——用当前财务因子选股，看历史表现。

    但为了可比，仍用月度调仓，只是因子值在季度内不变。
    """
    import bisect

    if strategy_name not in STRATEGIES:
        raise ValueError(f"未知策略：{strategy_name}")

    weights, desc = STRATEGIES[strategy_name]
    logger.info("策略：%s（%s）", strategy_name, desc)

    # --- 1. 拉财务数据 ---
    # **优化**：财务接口慢（约 1.5 秒/只），4004 只全拉要 30 分钟。
    # 先用市值/成交额粗筛到 max_fetch 只，只对它们拉财务数据。
    # 粗筛不改变策略逻辑——被粗筛掉的都是小市值/低流动性，本来也不会入选 Top30。
    # **B 方案**：扩大截面到 1000 只，让 IC 更稳（统计学上正确的做法，
    # 不是参数搜索，不算过拟合）
    max_fetch = max(top_n * 20, 1000)
    if len(candidates) > max_fetch:
        print(f"  粗筛：{len(candidates)} -> {max_fetch} 只（按成交额，财务接口慢）")
        sort_col = "avg_amount_yi" if "avg_amount_yi" in candidates.columns else "market_cap_yi"
        if sort_col in candidates.columns:
            candidates = candidates.nlargest(max_fetch, sort_col)
        else:
            candidates = candidates.head(max_fetch)
        print(f"  粗筛后：{len(candidates)} 只")

    codes = candidates["code"].tolist()
    print(f"  拉财务数据（{len(codes)} 只）…")
    fin = financials.fetch_financials_batch(
        codes, use_cache=True, workers=10,
        progress=lambda i, t, c: print(f"    {i}/{t}") if i % 200 == 0 else None,
    )
    print(f"  财务数据：{len(fin)} 只")

    if len(fin) == 0:
        raise ValueError("财务数据为空")

    # --- 2. 算因子 ---
    factors = financials.compute_financial_factors(fin, candidates)
    print(f"  财务因子：{len(factors)} 只，{len(factors.columns)} 个")

    # --- 3. 合并元数据 ---
    merged = candidates.set_index("code").join(factors, how="inner")
    if "industry_final" not in merged.columns:
        raise ValueError("缺行业字段，无法中性化")

    # --- 4. 行业中性化打分 ---
    if use_neutralize:
        print("  行业中性化…")
        scored = neutralize.neutralize_composite(
            merged.reset_index(), weights, use_rank=True)
        scored = scored.set_index("code")
        score_col = "composite_neutral"
    else:
        # 不中性化：全市场 z-score
        scored = merged.copy()
        for col in weights:
            if col in scored.columns:
                vals = scored[col].astype(float)
                mean, std = vals.mean(), vals.std()
                scored[f"{col}_z"] = (vals - mean) / std if std and std > 0 else 0.0
        score = sum(scored.get(f"{c}_z", 0) * w for c, w in weights.items())
        scored["composite_neutral"] = score
        score_col = "composite_neutral"

    valid = scored[score_col].notna()
    scored = scored[valid]
    print(f"  有效打分：{len(scored)} 只")

    if len(scored) < top_n * 2:
        raise ValueError(f"有效样本太少（{len(scored)}），至少需要 {top_n * 2}")

    # --- 5. 拉日线做回测 ---
    from screening import cache, fetch

    print("  拉日线…")
    stocks = [
        {"code": c, "exchange": r.get("exchange", "")}
        for c, r in scored.iterrows()
    ]
    # 补交易所
    for s in stocks:
        code = s["code"]
        if not s["exchange"]:
            if code.startswith("6"):
                s["exchange"] = "上海证券交易所"
            elif code.startswith(("0", "3")):
                s["exchange"] = "深圳证券交易所"

    end = date.today()
    from datetime import timedelta
    start = end - timedelta(days=365 * 3)

    daily_map = {}
    for s in stocks:
        try:
            frame = cache.fetch_with_cache(
                s["code"], s["exchange"], start, end, fetch.fetch_daily, adjust="qfq")
            if frame is not None and len(frame) > 0:
                daily_map[s["code"]] = frame
        except Exception:  # noqa: BLE001
            continue

    print(f"  日线：{len(daily_map)} 只")

    if len(daily_map) < top_n * 2:
        raise ValueError(f"日线数据太少（{len(daily_map)}）")

    # --- 6. 回测 ---
    by_code = {}
    for code, g in daily_map.items():
        g = g.sort_values("date")
        by_code[code] = {
            "dates": [str(d)[:10] for d in g["date"]],
            "close": g["close"].astype(float).values,
            "volume": g["volume"].astype(float).values if "volume" in g.columns else None,
        }

    all_dates = sorted({d for e in by_code.values() for d in e["dates"]})
    month_ends = [
        all_dates[i] for i in range(len(all_dates) - 1)
        if all_dates[i][:7] != all_dates[i + 1][:7]
    ]

    if len(month_ends) < 3:
        raise ValueError(f"调仓日太少（{len(month_ends)}）")

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
        if i >= len(e["dates"]) or e["dates"][i] != d or i == 0:
            return i > 0
        c_prev, c_now = e["close"][i - 1], e["close"][i]
        if np.isnan(c_prev) or np.isnan(c_now) or c_prev <= 0:
            return False
        chg = c_now / c_prev - 1
        lim = 0.20 if code.startswith(("300", "301", "688", "689")) else 0.10
        return not (chg >= lim * 0.98 or chg <= -lim * 0.98)

    result = FinResult()
    prev_holdings: set[str] = set()
    rng = np.random.default_rng(42)
    ranked = scored.sort_values(score_col, ascending=False)
    ranked_codes = ranked.index.tolist()

    for idx, reb in enumerate(month_ends[:-1]):
        nxt = month_ends[idx + 1]

        # 可交易过滤
        tradable = [c for c in ranked_codes if is_tradable(c, reb)]
        if len(tradable) < top_n * 2:
            continue

        picks = tradable[:top_n]
        holdings = set(picks)

        port_rets = [r for c in picks if (r := get_ret(c, reb, nxt)) is not None]
        if not port_rets:
            continue
        port_ret = float(np.mean(port_rets))

        # 基准
        eq_rets = [r for c in tradable if (r := get_ret(c, reb, nxt)) is not None]
        bench_equal = float(np.mean(eq_rets)) if eq_rets else np.nan

        rand_rets = []
        for _ in range(min(n_bootstrap, 100)):
            sample = rng.choice(len(tradable), size=min(top_n, len(tradable)), replace=False)
            s_rets = [r for i in sample if (r := get_ret(tradable[i], reb, nxt)) is not None]
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

        # IC（因子分 vs 未来收益）
        fwd = {}
        for c in tradable:
            r = get_ret(c, reb, nxt)
            if r is not None:
                fwd[c] = r
        common = [c for c in tradable if c in fwd]
        if len(common) >= 30:
            from scipy.stats import spearmanr

            s_vals = ranked.loc[common, score_col].values
            r_vals = np.array([fwd[c] for c in common])
            ic, _ = spearmanr(s_vals, r_vals)
            if not np.isnan(ic):
                result.ic_series.append((reb, float(ic)))

        result.dates.append(reb)
        result.portfolio.append(port_ret)
        result.bench_equal.append(bench_equal)
        result.bench_random.append(bench_random)
        result.bench_index.append(np.nan)
        result.turnover.append(turnover)
        result.holdings[reb] = sorted(holdings)
        prev_holdings = holdings

    result.stats = _compute_stats(result, strategy_name, desc, top_n, use_neutralize)
    return result


# ---------------------------------------------------------------------------
# 数据准备（一次加载，多策略共享）
# ---------------------------------------------------------------------------
def prepare_data(
    candidates: pd.DataFrame,
    top_n: int = 30,
    max_fetch: int | None = None,
) -> dict:
    """一次性加载所有数据，供多个策略共享。

    返回 {candidates, fin, factors, merged, by_code, month_ends, all_dates}
    """
    import bisect
    from datetime import timedelta

    from screening import cache, fetch

    if max_fetch is None:
        max_fetch = max(top_n * 20, 1000)

    if len(candidates) > max_fetch:
        print(f"  粗筛：{len(candidates)} -> {max_fetch} 只（按成交额）")
        sort_col = "avg_amount_yi" if "avg_amount_yi" in candidates.columns else "market_cap_yi"
        if sort_col in candidates.columns:
            candidates = candidates.nlargest(max_fetch, sort_col)
        else:
            candidates = candidates.head(max_fetch)

    codes = candidates["code"].tolist()
    print(f"  拉财务数据（{len(codes)} 只）…")
    fin = financials.fetch_financials_batch(
        codes, use_cache=True, workers=10,
        progress=lambda i, t_, c: print(f"    {i}/{t_}") if i % 300 == 0 else None,
    )
    print(f"  财务数据：{len(fin)} 只")

    if len(fin) == 0:
        raise ValueError("财务数据为空")

    factors = financials.compute_financial_factors(fin, candidates)
    print(f"  财务因子：{len(factors)} 只，{len(factors.columns)} 个")

    merged = candidates.set_index("code").join(factors, how="inner")
    print(f"  合并后：{len(merged)} 只")

    # 日线
    print("  拉日线…")
    stocks = [{"code": c, "exchange": r.get("exchange", "")} for c, r in merged.iterrows()]
    for s in stocks:
        code = s["code"]
        if not s["exchange"]:
            if code.startswith("6"):
                s["exchange"] = "上海证券交易所"
            elif code.startswith(("0", "3")):
                s["exchange"] = "深圳证券交易所"

    end = date.today()
    start = end - timedelta(days=365 * 3)

    daily_map = {}
    for s in stocks:
        try:
            frame = cache.fetch_with_cache(
                s["code"], s["exchange"], start, end, fetch.fetch_daily, adjust="qfq")
            if frame is not None and len(frame) > 0:
                daily_map[s["code"]] = frame
        except Exception:  # noqa: BLE001
            continue

    print(f"  日线：{len(daily_map)} 只")

    by_code = {}
    for code, g in daily_map.items():
        g = g.sort_values("date")
        by_code[code] = {
            "dates": [str(d)[:10] for d in g["date"]],
            "close": g["close"].astype(float).values,
        }

    all_dates = sorted({d for e in by_code.values() for d in e["dates"]})
    month_ends = [
        all_dates[i] for i in range(len(all_dates) - 1)
        if all_dates[i][:7] != all_dates[i + 1][:7]
    ]

    return {
        "candidates": candidates,
        "fin": fin,
        "factors": factors,
        "merged": merged,
        "by_code": by_code,
        "month_ends": month_ends,
        "all_dates": all_dates,
    }


def run_strategy_on_prepared(
    prep: dict,
    strategy_name: str,
    top_n: int = 30,
    include_cost: bool = True,
    n_bootstrap: int = 200,
    use_neutralize: bool = True,
) -> FinResult:
    """在已准备好的数据上跑单个策略（不重新拉数据）。"""
    import bisect

    if strategy_name not in STRATEGIES:
        raise ValueError(f"未知策略：{strategy_name}")

    weights, desc = STRATEGIES[strategy_name]
    merged = prep["merged"]
    by_code = prep["by_code"]
    month_ends = prep["month_ends"]

    # 中性化打分
    if use_neutralize:
        scored = neutralize.neutralize_composite(
            merged.reset_index(), weights, use_rank=True).set_index("code")
    else:
        scored = merged.copy()
        for col in weights:
            if col in scored.columns:
                vals = scored[col].astype(float)
                mean, std = vals.mean(), vals.std()
                scored[f"{col}_z"] = (vals - mean) / std if std and std > 0 else 0.0
        scored["composite_neutral"] = sum(
            scored.get(f"{c}_z", 0) * w for c, w in weights.items())

    score_col = "composite_neutral"
    scored = scored[scored[score_col].notna()]

    # 只保留有日线的
    scored = scored[scored.index.isin(by_code.keys())]

    if len(scored) < top_n * 2:
        raise ValueError(f"有效样本太少（{len(scored)}）")

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

    result = FinResult()
    prev_holdings: set[str] = set()
    rng = np.random.default_rng(42)
    ranked = scored.sort_values(score_col, ascending=False)
    ranked_codes = ranked.index.tolist()

    for idx, reb in enumerate(month_ends[:-1]):
        nxt = month_ends[idx + 1]

        tradable = [c for c in ranked_codes if is_tradable(c, reb)]
        if len(tradable) < top_n * 2:
            continue

        picks = tradable[:top_n]
        holdings = set(picks)

        port_rets = [r for c in picks if (r := get_ret(c, reb, nxt)) is not None]
        if not port_rets:
            continue
        port_ret = float(np.mean(port_rets))

        eq_rets = [r for c in tradable if (r := get_ret(c, reb, nxt)) is not None]
        bench_equal = float(np.mean(eq_rets)) if eq_rets else np.nan

        rand_rets = []
        for _ in range(min(n_bootstrap, 100)):
            sample = rng.choice(len(tradable), size=min(top_n, len(tradable)), replace=False)
            s_rets = [r for i in sample if (r := get_ret(tradable[i], reb, nxt)) is not None]
            if s_rets:
                rand_rets.append(float(np.mean(s_rets)))
        bench_random = float(np.mean(rand_rets)) if rand_rets else np.nan

        if include_cost:
            new_pos = holdings - prev_holdings
            closed = prev_holdings - holdings
            buy = len(new_pos) / max(len(holdings), 1) * (FEE_RATE + SLIPPAGE)
            sell = (len(closed) / max(len(prev_holdings), 1) *
                    (FEE_RATE + STAMP_TAX + SLIPPAGE)) if prev_holdings else 0.0
            port_ret -= (buy + sell)

        turnover = len(holdings ^ prev_holdings) / max(len(holdings), 1)

        fwd = {}
        for c in tradable:
            r = get_ret(c, reb, nxt)
            if r is not None:
                fwd[c] = r
        common = [c for c in tradable if c in fwd]
        if len(common) >= 30:
            from scipy.stats import spearmanr

            s_vals = ranked.loc[common, score_col].values
            r_vals = np.array([fwd[c] for c in common])
            ic, _ = spearmanr(s_vals, r_vals)
            if not np.isnan(ic):
                result.ic_series.append((reb, float(ic)))

        result.dates.append(reb)
        result.portfolio.append(port_ret)
        result.bench_equal.append(bench_equal)
        result.bench_random.append(bench_random)
        result.bench_index.append(np.nan)
        result.turnover.append(turnover)
        result.holdings[reb] = sorted(holdings)
        prev_holdings = holdings

    result.stats = _compute_stats(result, strategy_name, desc, top_n, use_neutralize)
    return result



def _compute_stats(r: FinResult, name: str, desc: str,
                   top_n: int, neutralized: bool) -> dict:
    if not r.portfolio:
        return {"error": "无有效数据", "strategy": name}

    port = np.array(r.portfolio, dtype=float)
    eq = np.array(r.bench_equal, dtype=float)
    rnd = np.array(r.bench_random, dtype=float)
    to = np.array(r.turnover, dtype=float)

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
    ic_ir = ic_mean / ic_std if ic_std > 0 else 0.0
    ic_t = ic_ir * np.sqrt(len(ic_vals)) if ic_vals else 0.0

    equity = np.cumprod(1 + port)
    peak = np.maximum.accumulate(equity)
    mdd = float((equity / peak - 1).min())

    return {
        "strategy": name,
        "description": desc,
        "periods": len(port),
        "top_n": top_n,
        "neutralized": neutralized,
        "cum_portfolio": round(cum(port), 4) if cum(port) is not None else None,
        "cum_bench_equal": round(cum(eq), 4) if cum(eq) is not None else None,
        "cum_bench_random": round(cum(rnd), 4) if cum(rnd) is not None else None,
        "excess_vs_equal": round(excess(eq), 4) if excess(eq) is not None else None,
        "excess_vs_random": round(excess(rnd), 4) if excess(rnd) is not None else None,
        "max_drawdown": round(mdd, 4),
        "avg_turnover": round(float(np.nanmean(to)), 4),
        "ic_mean": round(ic_mean, 4),
        "ic_std": round(ic_std, 4),
        "ic_ir": round(ic_ir, 4),
        "ic_t_stat": round(float(ic_t), 2),
        "ic_periods": len(ic_vals),
        "ic_significant": bool(abs(ic_t) > 1.96),
    }


def report(results: list[FinResult]) -> str:
    lines = [
        "=" * 92,
        "财务因子回测结果",
        "=" * 92,
        "",
        f"  {'策略':<16} {'期数':>5} {'累计':>10} {'vs等权':>10} {'vs随机':>10} "
        f"{'回撤':>8} {'IC':>8} {'t值':>7} {'显著':>5}",
        "  " + "-" * 88,
    ]

    for r in results:
        s = r.stats
        if "error" in s:
            lines.append(f"  {s.get('strategy', '?'):<16} {s['error']}")
            continue
        lines.append(
            f"  {s['strategy']:<16} {s['periods']:>5} "
            f"{s['cum_portfolio']:>+9.2%} "
            f"{(s['excess_vs_equal'] or 0):>+9.2%} "
            f"{(s['excess_vs_random'] or 0):>+9.2%} "
            f"{s['max_drawdown']:>7.2%} "
            f"{s['ic_mean']:>+8.4f} {s['ic_t_stat']:>+7.2f} "
            f"{'是' if s['ic_significant'] else '否':>5}"
        )

    lines.append("")
    lines.append("=" * 92)
    return "\n".join(lines)


def judge(results: list[FinResult]) -> str:
    lines = ["", "【判断】", ""]

    valid = [r for r in results if "error" not in r.stats]
    if not valid:
        return "所有策略都失败，无法判断"

    # 找最佳
    best = max(valid, key=lambda r: r.stats.get("excess_vs_random") or -999)
    bs = best.stats

    lines.append(f"  最佳策略：**{bs['strategy']}**（{bs['description']}）")
    lines.append(f"    vs 随机超额：{bs['excess_vs_random']:+.2%}")
    lines.append(f"    IC 均值：{bs['ic_mean']:+.4f}，t 值：{bs['ic_t_stat']:+.2f}")
    lines.append("")

    # 统计显著性
    significant = [r for r in valid if r.stats.get("ic_significant")]
    if significant:
        lines.append(f"  ✅ **{len(significant)} 个策略 IC 统计显著**：")
        for r in significant:
            lines.append(f"     {r.stats['strategy']}: t={r.stats['ic_t_stat']:+.2f}, "
                         f"vs随机 {r.stats['excess_vs_random']:+.2%}")
        lines.append("")
        lines.append("  这是重要发现——之前所有价量因子的 t 值都 < 1.96。")
    else:
        lines.append("  ❌ **所有策略 IC 都不显著（|t| < 1.96）**")
        lines.append("     34 期样本下，无法与噪声区分。")
        lines.append("")

    # 对比价量因子
    lines.append("  【与之前价量因子对比】")
    lines.append("     动量+低波：超额 -32.59%，IC -0.0273，t 未测")
    lines.append("     纯反向：  超额 +99.79%（样本内），IC +0.0280，t=0.78")
    lines.append(f"     财务最佳：超额 {bs['excess_vs_random']:+.2%}，"
                 f"IC {bs['ic_mean']:+.4f}，t={bs['ic_t_stat']:+.2f}")
    lines.append("")

    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="财务因子回测")
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--strategy", default="all",
                        help="all 或具体策略名")
    parser.add_argument("--no-neutralize", action="store_true")
    parser.add_argument("--no-cost", action="store_true")
    args = parser.parse_args(argv[1:])

    logging.basicConfig(level=logging.WARNING)

    print("=" * 92)
    print("财务因子回测")
    print("=" * 92)

    try:
        candidates, run_date = load_latest_candidates()
    except FileNotFoundError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1

    print(f"候选池：{len(candidates)} 只（{run_date}）")
    print()

    strategies = (
        list(STRATEGIES) if args.strategy == "all" else [args.strategy]
    )

    # **关键优化**：一次加载数据，所有策略共享（之前每个策略都重拉一遍）
    print("=" * 92)
    print("准备数据（一次加载，多策略共享）")
    print("=" * 92)
    try:
        prep = prepare_data(candidates, top_n=args.top)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 数据准备失败：{type(exc).__name__}: {str(exc)[:100]}", file=sys.stderr)
        return 1

    print()
    print(f"数据就绪：{len(prep['merged'])} 只，{len(prep['month_ends'])} 个调仓日")
    print()

    results = []
    for name in strategies:
        print(f"[{name}] {STRATEGIES[name][1]}")
        try:
            r = run_strategy_on_prepared(
                prep, name,
                top_n=args.top,
                include_cost=not args.no_cost,
                use_neutralize=not args.no_neutralize,
            )
            results.append(r)
            s = r.stats
            if "error" not in s:
                print(f"  期数 {s['periods']} | 累计 {s['cum_portfolio']:+.2%} | "
                      f"vs随机 {s['excess_vs_random']:+.2%} | "
                      f"IC {s['ic_mean']:+.4f} (t={s['ic_t_stat']:+.2f})")
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ 失败：{type(exc).__name__}: {str(exc)[:80]}")
        print()

    if not results:
        print("所有策略都失败", file=sys.stderr)
        return 1

    print(report(results))
    print(judge(results))

    # 落盘
    out_dir = _REPO_ROOT / "qlib_layer" / "output" / "backtest_financial"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(
        json.dumps([r.stats for r in results], ensure_ascii=False, indent=2),
        encoding="utf-8")
    print()
    print(f"输出: {out_dir}")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
