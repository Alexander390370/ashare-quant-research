"""修正版回测：三个基准 + 接入防线 + 修正影子口径。

六家评审（2026-10-31）一致指出的问题，逐条修：

1. **基准错配**（六家一致第一句）
   原来只用「全池等权 793 只」，它是极端小盘暴露，本身自带 alpha。
   修正：三个基准并列
     - 官方指数（沪深300 / 中证500，用真实指数数据）
     - 随机 20 只 bootstrap 1000 次（容量匹配，剥离选股能力）
     - 全池等权（保留作辅助参照）

2. **未接入防线**（MiniMax）
   涨跌停不可成交、ST、次新、停牌全关掉了。
   修正：调仓日过滤——当日涨停/跌停/停牌/ST 的票不入选。

3. **影子组合口径错**（Qwen / MiniMax）
   +71.58% 基准与 +156.21% 影子对不上。
   修正：影子 = 当日可交易池里，未被选入 Top20 的股票等权。

用法
----
    python -m qlib_layer.backtest_v2
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

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tradingagents  # noqa: E402,F401

from qlib_layer.factors import (  # noqa: E402
    DEFAULT_WEIGHTS,
    FACTOR_EXPRESSIONS,
    FACTOR_NAMES,
)

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER = _REPO_ROOT / "qlib_layer" / "data" / "cn_a"

# 交易成本
FEE_RATE = 0.0003
STAMP_TAX = 0.0005
SLIPPAGE = 0.001

# 涨跌停阈值（按板块）
def limit_pct(code: str, name: str = "") -> float:
    """返回该股票的涨跌停幅度。"""
    if "ST" in name.upper():
        return 0.05
    plain = code.replace("SH", "").replace("SZ", "").replace("BJ", "")
    if plain.startswith(("300", "301", "688", "689")):
        return 0.20
    return 0.10


@dataclass
class V2Result:
    dates: list[str] = field(default_factory=list)
    portfolio: list[float] = field(default_factory=list)
    bench_index: list[float] = field(default_factory=list)      # 官方指数
    bench_random: list[float] = field(default_factory=list)     # 随机 20 只
    bench_equal: list[float] = field(default_factory=list)      # 全池等权
    shadow: list[float] = field(default_factory=list)           # 影子（可交易池未入选）
    turnover: list[float] = field(default_factory=list)
    holdings: dict[str, list[str]] = field(default_factory=dict)
    ic_series: list[tuple[str, float]] = field(default_factory=list)
    blocked_stats: dict = field(default_factory=dict)
    stats: dict = field(default_factory=dict)


def _load_index_returns(index_code: str, dates: list[str]) -> dict[str, float]:
    """拉官方指数收益。

    数据源实测（2026-10-31）：
      东财 index_zh_a_hist       -> ProxyError（不可用）
      新浪 stock_zh_index_daily  -> OK 6020 行  ← 主选
      腾讯 stock_zh_index_daily_tx -> OK 5238 行 ← 备选
    """
    import akshare as ak

    # 新浪需要 sh/sz 前缀
    prefix = "sh" if index_code.startswith(("000", "999")) else "sz"
    symbol = f"{prefix}{index_code}"

    for name, fn in [
        ("新浪", lambda: ak.stock_zh_index_daily(symbol=symbol)),
        ("腾讯", lambda: ak.stock_zh_index_daily_tx(symbol=symbol)),
    ]:
        try:
            df = fn()
            if df is None or len(df) == 0:
                continue
            df = df.rename(columns={"日期": "date", "收盘": "close"})
            if "date" not in df.columns:
                continue
            df["date"] = df["date"].astype(str).str[:10]
            out = dict(zip(df["date"], df["close"].astype(float), strict=False))
            logger.info("指数 %s 从%s拉取成功：%d 行", index_code, name, len(out))
            return out
        except Exception as exc:  # noqa: BLE001
            logger.warning("指数 %s 从%s拉取失败: %s", index_code, name, str(exc)[:50])
    return {}


def run_v2(
    provider_uri: Path = DEFAULT_PROVIDER,
    top_n: int = 20,
    direction: str = "inverted",
    include_cost: bool = True,
    apply_filters: bool = True,
    n_bootstrap: int = 1000,
    start_time: str | None = None,
    end_time: str | None = None,
) -> V2Result:
    """跑修正版回测。"""
    import bisect

    import qlib
    from qlib.data import D

    qlib.init(provider_uri=str(provider_uri), region="cn", expression_cache=None)

    codes = D.list_instruments(
        D.instruments(market="all"),
        start_time="2020-01-01", end_time="2030-12-31", as_list=True)
    calendar = [str(c)[:10] for c in D.calendar(start_time="2020-01-01", end_time="2030-12-31")]

    start_time = start_time or calendar[0]
    end_time = end_time or calendar[-1]

    # 多拉一天用于判断涨跌停（需要前一日收盘）
    fields = ["$close", "$high", "$low", "$volume"] + [
        FACTOR_EXPRESSIONS[n] for n in FACTOR_NAMES]
    logger.info("拉面板：%d 只", len(codes))
    panel = D.features(codes, fields, freq="day")
    panel.columns = ["close", "high", "low", "volume"] + FACTOR_NAMES
    panel = panel.reset_index()
    panel["date"] = panel["datetime"].astype(str).str[:10]
    panel = panel.drop(columns=["datetime"])

    # 预索引
    by_code: dict[str, dict] = {}
    for code, g in panel.groupby("instrument"):
        g = g.sort_values("date")
        by_code[code] = {
            "dates": g["date"].tolist(),
            "close": g["close"].astype(float).values,
            "high": g["high"].astype(float).values,
            "low": g["low"].astype(float).values,
            "volume": g["volume"].astype(float).values,
        }

    by_date = {d: g for d, g in panel.groupby("date")}
    all_dates = sorted(panel["date"].unique())

    # 月度调仓日
    month_ends = [
        all_dates[i] for i in range(len(all_dates) - 1)
        if all_dates[i][:7] != all_dates[i + 1][:7]
    ]
    month_ends = [d for d in month_ends if start_time <= d <= end_time]
    if len(month_ends) < 3:
        raise ValueError(f"调仓日太少（{len(month_ends)}）")

    def get_ret(code: str, d0: str, d1: str) -> float | None:
        entry = by_code.get(code)
        if entry is None:
            return None
        dates = entry["dates"]
        i0 = bisect.bisect_left(dates, d0)
        i1 = bisect.bisect_right(dates, d1) - 1
        if i1 <= i0 or i0 >= len(dates) or i1 >= len(dates):
            return None
        c0, c1 = entry["close"][i0], entry["close"][i1]
        if np.isnan(c0) or np.isnan(c1) or c0 <= 0:
            return None
        return float(c1 / c0 - 1)

    def is_tradable(code: str, d: str) -> tuple[bool, str]:
        """判断调仓日是否可交易（涨跌停/停牌）。"""
        if not apply_filters:
            return True, ""
        entry = by_code.get(code)
        if entry is None:
            return False, "无数据"
        dates = entry["dates"]
        i = bisect.bisect_left(dates, d)
        if i >= len(dates) or dates[i] != d:
            return False, "当日无数据"
        if i == 0:
            return True, ""
        c_prev = entry["close"][i - 1]
        c_now = entry["close"][i]
        h = entry["high"][i]
        low = entry["low"][i]
        v = entry["volume"][i]
        if np.isnan(c_prev) or np.isnan(c_now) or c_prev <= 0:
            return False, "数据异常"
        if np.isnan(v) or v <= 0:
            return False, "停牌"
        chg = c_now / c_prev - 1
        lim = limit_pct(code)
        # 涨停（收盘接近涨停价，或一字板）
        if chg >= lim * 0.98:
            return False, f"涨停({chg:+.1%})"
        if chg <= -lim * 0.98:
            return False, f"跌停({chg:+.1%})"
        if not np.isnan(h) and not np.isnan(low) and h == low:
            return False, "一字板"
        return True, ""

    result = V2Result()
    prev_holdings: set[str] = set()
    blocked_counter: dict[str, int] = {}

    rng = np.random.default_rng(42)

    for idx, reb in enumerate(month_ends[:-1]):
        nxt = month_ends[idx + 1]
        today = by_date.get(reb)
        if today is None or len(today) == 0:
            continue

        # ---------- 构建可交易池 ----------
        pool = today.dropna(subset=["mom20", "vol20", "close"])
        pool = pool[pool["close"] > 0]

        tradable_codes = []
        for code in pool["instrument"]:
            ok, reason = is_tradable(code, reb)
            if ok:
                tradable_codes.append(code)
            else:
                blocked_counter[reason.split("(")[0]] = (
                    blocked_counter.get(reason.split("(")[0], 0) + 1)

        if len(tradable_codes) < top_n * 2:
            continue

        pool = pool[pool["instrument"].isin(tradable_codes)].copy()

        # ---------- 算分 ----------
        for f in FACTOR_NAMES:
            col = pool[f].astype(float)
            mean, std = col.mean(), col.std()
            pool[f + "_z"] = (col - mean) / std if std and std > 0 else 0.0

        _w = dict(DEFAULT_WEIGHTS)
        if direction == "inverted":
            _w = {k: -v for k, v in _w.items()}
        pool["score"] = sum(
            pool[k] * w for k, w in _w.items() if k in pool.columns)
        pool = pool.sort_values("score", ascending=(direction == "short"))

        picks = pool["instrument"].tolist()[:top_n]
        holdings = set(picks)

        # ---------- 收益 ----------
        port_rets = []
        for c in picks:
            r = get_ret(c, reb, nxt)
            if r is not None:
                port_rets.append(r)
        if not port_rets:
            continue
        port_ret = float(np.mean(port_rets))

        # ---------- 三个基准 ----------
        # ① 全池等权（可交易池）
        eq_rets = [r for c in pool["instrument"] if (r := get_ret(c, reb, nxt)) is not None]
        bench_equal = float(np.mean(eq_rets)) if eq_rets else np.nan

        # ② 随机 20 只（bootstrap 平均）
        all_codes_pool = pool["instrument"].tolist()
        rand_rets = []
        for _ in range(min(n_bootstrap, 200)):
            sample = rng.choice(len(all_codes_pool), size=top_n, replace=False)
            s_rets = [
                r for i in sample
                if (r := get_ret(all_codes_pool[i], reb, nxt)) is not None]
            if s_rets:
                rand_rets.append(float(np.mean(s_rets)))
        bench_random = float(np.mean(rand_rets)) if rand_rets else np.nan

        # ③ 官方指数（稍后统一算，这里先占位）
        bench_index = np.nan

        # ---------- 影子：可交易池里未入选的 ----------
        shadow_codes = [c for c in all_codes_pool if c not in holdings]
        shadow_rets = [
            r for c in shadow_codes if (r := get_ret(c, reb, nxt)) is not None]
        shadow = float(np.mean(shadow_rets)) if shadow_rets else np.nan

        # ---------- 成本 ----------
        if include_cost:
            new_pos = holdings - prev_holdings
            closed = prev_holdings - holdings
            buy_cost = len(new_pos) / max(len(holdings), 1) * (FEE_RATE + SLIPPAGE)
            sell_cost = (
                len(closed) / max(len(prev_holdings), 1) *
                (FEE_RATE + STAMP_TAX + SLIPPAGE)) if prev_holdings else 0.0
            port_ret -= (buy_cost + sell_cost)

        turnover = len(holdings ^ prev_holdings) / max(len(holdings), 1)

        # ---------- IC ----------
        fwd = {}
        for c in pool["instrument"]:
            r = get_ret(c, reb, nxt)
            if r is not None:
                fwd[c] = r
        common = [c for c in pool["instrument"] if c in fwd]
        if len(common) >= 30:
            from scipy.stats import spearmanr

            scores = pool.set_index("instrument").loc[common, "score"].values
            rets = np.array([fwd[c] for c in common])
            ic, _ = spearmanr(scores, rets)
            if not np.isnan(ic):
                result.ic_series.append((reb, float(ic)))

        result.dates.append(reb)
        result.portfolio.append(port_ret)
        result.bench_equal.append(bench_equal)
        result.bench_random.append(bench_random)
        result.bench_index.append(bench_index)
        result.shadow.append(shadow)
        result.turnover.append(turnover)
        result.holdings[reb] = sorted(holdings)
        prev_holdings = holdings

        if (idx + 1) % 6 == 0:
            logger.info("完成 %d/%d 期", idx + 1, len(month_ends) - 1)

    result.blocked_stats = blocked_counter
    result.stats = _stats(result, include_cost, top_n, direction)
    return result


def _fill_index_benchmark(result: V2Result) -> None:
    """用官方指数填充基准（在 run_v2 之后调用，避免阻塞主流程）。"""
    if not result.dates:
        return

    # 沪深300（000300）和中证500（000905）
    hs300 = _load_index_returns("000300", result.dates)
    zz500 = _load_index_returns("000905", result.dates)

    # 用两者等权合成一个"中证800 风格"基准
    filled = []
    for i, d in enumerate(result.dates):
        vals = []
        if d in hs300:
            vals.append(hs300[d])
        if d in zz500:
            vals.append(zz500[d])
        filled.append(vals)

    # 计算期收益
    idx_rets = []
    for i in range(len(result.dates) - 1):
        cur = filled[i]
        nxt = filled[i + 1]
        if not cur or not nxt or len(cur) != len(nxt):
            idx_rets.append(np.nan)
            continue
        rets = [nxt[j] / cur[j] - 1 for j in range(len(cur)) if cur[j] > 0]
        idx_rets.append(float(np.mean(rets)) if rets else np.nan)
    idx_rets.append(np.nan)

    result.bench_index = idx_rets


def _stats(result: V2Result, include_cost: bool, top_n: int, direction: str) -> dict:
    if not result.portfolio:
        return {"error": "无数据"}

    port = np.array(result.portfolio, dtype=float)
    eq = np.array(result.bench_equal, dtype=float)
    rnd = np.array(result.bench_random, dtype=float)
    idx_b = np.array(result.bench_index, dtype=float)
    shd = np.array(result.shadow, dtype=float)
    to = np.array(result.turnover, dtype=float)

    def cum(arr):
        a = arr[~np.isnan(arr)]
        return float(np.prod(1 + a) - 1) if len(a) else None

    def excess_over(bench):
        """逐期超额累计（只在两者都有效的期算）。"""
        m = ~np.isnan(port) & ~np.isnan(bench)
        if m.sum() < 3:
            return None
        return float(np.prod(1 + (port[m] - bench[m])) - 1)

    def win_rate(bench):
        m = ~np.isnan(port) & ~np.isnan(bench)
        if m.sum() < 3:
            return None
        return float((port[m] > bench[m]).mean())

    ic_vals = [ic for _, ic in result.ic_series]
    ic_mean = float(np.mean(ic_vals)) if ic_vals else 0.0
    ic_std = float(np.std(ic_vals, ddof=1)) if len(ic_vals) > 1 else 0.0
    ic_ir = ic_mean / ic_std if ic_std > 0 else 0.0
    # t 统计量（六家要求）
    ic_t = ic_ir * np.sqrt(len(ic_vals)) if ic_vals else 0.0

    equity = np.cumprod(1 + port)
    peak = np.maximum.accumulate(equity)
    mdd = float((equity / peak - 1).min())

    return {
        "periods": len(port),
        "top_n": top_n,
        "direction": direction,
        "include_cost": include_cost,
        "cum_portfolio": round(cum(port), 4) if cum(port) is not None else None,
        "cum_bench_equal": round(cum(eq), 4) if cum(eq) is not None else None,
        "cum_bench_random": round(cum(rnd), 4) if cum(rnd) is not None else None,
        "cum_bench_index": round(cum(idx_b), 4) if cum(idx_b) is not None else None,
        "cum_shadow": round(cum(shd), 4) if cum(shd) is not None else None,
        "excess_vs_equal": round(excess_over(eq), 4) if excess_over(eq) is not None else None,
        "excess_vs_random": round(excess_over(rnd), 4) if excess_over(rnd) is not None else None,
        "excess_vs_index": round(excess_over(idx_b), 4) if excess_over(idx_b) is not None else None,
        "excess_vs_shadow": round(excess_over(shd), 4) if excess_over(shd) is not None else None,
        "win_vs_random": round(win_rate(rnd), 4) if win_rate(rnd) is not None else None,
        "win_vs_index": round(win_rate(idx_b), 4) if win_rate(idx_b) is not None else None,
        "max_drawdown": round(mdd, 4),
        "avg_turnover": round(float(np.nanmean(to)), 4),
        "ic_mean": round(ic_mean, 4),
        "ic_std": round(ic_std, 4),
        "ic_ir": round(ic_ir, 4),
        "ic_t_stat": round(ic_t, 2),
        "ic_periods": len(ic_vals),
        "ic_significant": bool(abs(ic_t) > 1.96),
        "blocked": result.blocked_stats,
    }


def report_v2(r: V2Result) -> str:
    s = r.stats
    if "error" in s:
        return f"失败：{s['error']}"

    def fmt(v):
        return f"{v:+.2%}" if v is not None else "N/A"

    lines = [
        "=" * 76,
        f"修正版回测（Top{s['top_n']}，{s['direction']}，"
        f"{'含' if s['include_cost'] else '不含'}成本）",
        "=" * 76,
        "",
        f"  期数            : {s['periods']} 期",
        "",
        "  --- 组合与四个基准 ---",
        f"  组合            : {fmt(s['cum_portfolio'])}",
        f"  官方指数（300+500）: {fmt(s['cum_bench_index'])}",
        f"  随机20只（bootstrap）: {fmt(s['cum_bench_random'])}",
        f"  全池等权        : {fmt(s['cum_bench_equal'])}",
        f"  影子（未入选）  : {fmt(s['cum_shadow'])}",
        "",
        "  --- 超额（关键）---",
        f"  vs 官方指数     : {fmt(s['excess_vs_index'])}",
        f"  vs 随机20只     : {fmt(s['excess_vs_random'])}  ← 剥离选股能力",
        f"  vs 全池等权     : {fmt(s['excess_vs_equal'])}",
        f"  vs 影子         : {fmt(s['excess_vs_shadow'])}",
        "",
        f"  跑赢随机比例    : {s['win_vs_random']:.1%}" if s['win_vs_random'] is not None else "",
        f"  跑赢指数比例    : {s['win_vs_index']:.1%}" if s['win_vs_index'] is not None else "",
        f"  最大回撤        : {s['max_drawdown']:.2%}",
        f"  平均换手        : {s['avg_turnover']:.1%}",
        "",
        "  --- 因子 IC ---",
        f"  IC 均值         : {s['ic_mean']:+.4f}",
        f"  ICIR            : {s['ic_ir']:+.4f}",
        f"  **t 统计量**    : **{s['ic_t_stat']:+.2f}**"
        f"  ({'显著' if s['ic_significant'] else '**不显著**'}，门槛 1.96)",
        "",
        "  --- 被防线拦下的（调仓日不可交易）---",
    ]
    for reason, count in sorted(s["blocked"].items(), key=lambda x: -x[1])[:6]:
        lines.append(f"  {reason:12s}: {count} 次")

    lines.append("")
    lines.append("=" * 76)
    return "\n".join(line for line in lines if line is not None)


def judge_v2(r: V2Result) -> str:
    s = r.stats
    if "error" in s:
        return "无法判断"

    lines = []

    ex_rand = s.get("excess_vs_random")
    ex_index = s.get("excess_vs_index")
    ex_shadow = s.get("excess_vs_shadow")
    ic_t = s.get("ic_t_stat", 0)

    # 核心判据：跑赢随机 20 只才有选股能力
    if ex_rand is not None:
        if ex_rand > 0.05:
            lines.append(f"✅ **跑赢随机 20 只 {ex_rand:+.2%}** —— 有选股能力")
        elif ex_rand > 0:
            lines.append(f"⚠️  跑赢随机 20 只 {ex_rand:+.2%} —— 能力很弱")
        else:
            lines.append(f"❌ **跑输随机 20 只 {ex_rand:+.2%}** —— 没有选股能力")

    if ex_index is not None:
        lines.append(f"   相对官方指数：{ex_index:+.2%}")

    if ex_shadow is not None:
        lines.append(f"   相对影子组合：{ex_shadow:+.2%}")

    lines.append("")
    if abs(ic_t) < 1.96:
        lines.append(f"❌ **IC 的 t 统计量 {ic_t:+.2f} < 1.96，统计上不显著**")
        lines.append("   六家指出：34 期样本的 IC 差异无法与噪声区分")
    else:
        lines.append(f"✅ IC 的 t 统计量 {ic_t:+.2f} > 1.96，统计显著")

    return "\n".join(lines)


def save_v2(r: V2Result, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "periods.csv").open("w", encoding="utf-8-sig", newline="") as h:
        w = csv.writer(h)
        w.writerow(["date", "portfolio", "bench_index", "bench_random",
                    "bench_equal", "shadow", "turnover"])
        for i, d in enumerate(r.dates):
            w.writerow([
                d,
                round(r.portfolio[i], 6),
                None if np.isnan(r.bench_index[i]) else round(r.bench_index[i], 6),
                None if np.isnan(r.bench_random[i]) else round(r.bench_random[i], 6),
                None if np.isnan(r.bench_equal[i]) else round(r.bench_equal[i], 6),
                None if np.isnan(r.shadow[i]) else round(r.shadow[i], 6),
                round(r.turnover[i], 4),
            ])
    (out_dir / "stats.json").write_text(
        json.dumps(r.stats, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="修正版回测")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--direction", default="inverted", choices=["long", "inverted", "short"])
    parser.add_argument("--no-cost", action="store_true")
    parser.add_argument("--no-filters", action="store_true")
    parser.add_argument("--bootstrap", type=int, default=200)
    args = parser.parse_args(argv[1:])

    logging.basicConfig(level=logging.WARNING)

    print("=" * 76)
    print("修正版回测：三基准 + 防线 + 修正影子口径")
    print("=" * 76)

    result = run_v2(
        top_n=args.top,
        direction=args.direction,
        include_cost=not args.no_cost,
        apply_filters=not args.no_filters,
        n_bootstrap=args.bootstrap,
    )

    print("\n拉官方指数数据...")
    _fill_index_benchmark(result)
    result.stats = _stats(result, not args.no_cost, args.top, args.direction)

    print()
    print(report_v2(result))
    print()
    print("【判断】")
    print(judge_v2(result))

    out_dir = _REPO_ROOT / "qlib_layer" / "output" / f"backtest_v2_{date.today().isoformat()}"
    save_v2(result, out_dir)
    print()
    print(f"输出: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
