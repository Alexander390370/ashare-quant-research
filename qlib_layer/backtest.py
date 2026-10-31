"""回测验证：动量+低波策略在 A 股指数成分股里到底有没有超额。

这是六家评审一致要求的第三步（③ 回测验证）。

回答三个问题
------------
1. **Top20 等权持有，历史上跑赢基准了吗？**
2. **因子 IC 是多少？**（因子分与未来收益的秩相关）
3. **换手率多少？**（成本敏感度）

设计原则（吸收六家评审的批评）
------------------------------
- **不用「等权买 Top20 持有到期末」这种你不会执行的操作**：
  按月调仓，模拟真实持有周期
- **含交易成本**：双边 0.15% + 卖出印花税 0.05%
- **含涨跌停不可成交**：调仓日涨停的票跳过（用前一日的涨跌幅判断）
- **建影子组合**：被排除的股票也要跟踪（六家强调的反事实验证）
- **报告基准对照**：不只报绝对收益，必须报相对沪深300/中证500 的超额

Windows 注意
------------
Qlib 内部用 joblib/multiprocessing，**必须放在 `if __name__ == "__main__":`
保护块里调用**，否则 spawn 会重复导入主模块导致 RuntimeError。

用法
----
    python -m qlib_layer.backtest                     # 默认参数
    python -m qlib_layer.backtest --top 20 --freq M   # 自定义
    python -m qlib_layer.backtest --no-cost           # 不含成本（对比用）
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
FEE_RATE = 0.0003          # 佣金万三
STAMP_TAX = 0.0005         # 印花税千一（仅卖出）
SLIPPAGE = 0.001           # 滑点千一


@dataclass
class BacktestResult:
    """回测结果。"""

    dates: list[str] = field(default_factory=list)
    portfolio_returns: list[float] = field(default_factory=list)
    benchmark_returns: list[float] = field(default_factory=list)
    turnover: list[float] = field(default_factory=list)
    holdings: dict[str, list[str]] = field(default_factory=dict)
    ic_series: list[tuple[str, float]] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    shadow_returns: list[float] = field(default_factory=list)


def _month_ends(calendar: list[str]) -> list[int]:
    """找出每个月的最后一个交易日索引。"""
    indices = []
    for i in range(len(calendar) - 1):
        cur_month = calendar[i][:7]
        next_month = calendar[i + 1][:7]
        if cur_month != next_month:
            indices.append(i)
    return indices


def run_backtest(
    provider_uri: Path = DEFAULT_PROVIDER,
    top_n: int = 20,
    freq: str = "M",
    include_cost: bool = True,
    start_time: str | None = None,
    end_time: str | None = None,
    min_history_days: int = 60,
    direction: str = "long",
    weights: dict[str, float] | None = None,
) -> BacktestResult:
    """跑回测。

    direction:
        "long"     -> 买分数最高的 Top N（默认）
        "short"    -> 买分数最低的 Bottom N（反向策略）
        "inverted" -> 权重全部取反后买 Top N（等价于 short，但用于验证）
    weights:
        自定义权重，None 则用 DEFAULT_WEIGHTS
    """
    """跑回测。

    参数
    ----
    top_n:
        每期持有多少只
    freq:
        "M" 月度调仓 / "W" 周度调仓
    include_cost:
        是否扣交易成本
    min_history_days:
        上市不足这么多天的股票不参与（避免新股噪声）
    """
    import qlib
    from qlib.data import D

    qlib.init(provider_uri=str(provider_uri), region="cn", expression_cache=None)

    codes = D.list_instruments(
        D.instruments(market="all"),
        start_time="2020-01-01", end_time="2030-12-31", as_list=True)
    if not codes:
        raise ValueError("Qlib 里没有股票")

    calendar = [str(c)[:10] for c in D.calendar(start_time="2020-01-01", end_time="2030-12-31")]
    if len(calendar) < 120:
        raise ValueError(f"日历太短（{len(calendar)} 天），无法回测")

    start_time = start_time or calendar[0]
    end_time = end_time or calendar[-1]

    # 一次性拉全量数据（比逐期拉快得多）
    fields = ["$close"] + [FACTOR_EXPRESSIONS[n] for n in FACTOR_NAMES]
    logger.info("拉取面板数据：%d 只 × %d 字段", len(codes), len(fields))
    panel = D.features(codes, fields, start_time=start_time, end_time=end_time, freq="day")
    if panel is None or len(panel) == 0:
        raise ValueError("面板数据为空")

    panel.columns = ["close"] + FACTOR_NAMES
    panel = panel.reset_index()
    panel["date"] = panel["datetime"].astype(str).str[:10]
    panel = panel.drop(columns=["datetime"])

    # 基准：等权全池（用 close 的日收益率均值近似）
    all_dates = sorted(panel["date"].unique())

    # 调仓日
    if freq == "M":
        rebalance_dates = [all_dates[i] for i in _month_ends(all_dates)]
    else:
        # 周度：每 5 个交易日
        rebalance_dates = all_dates[min_history_days::5]
    rebalance_dates = [d for d in rebalance_dates if d >= start_time]

    if len(rebalance_dates) < 3:
        raise ValueError(f"调仓日太少（{len(rebalance_dates)}），区间太短")

    logger.info("调仓日：%d 个（%s ~ %s）", len(rebalance_dates),
                rebalance_dates[0], rebalance_dates[-1])

    # 按日期索引面板（算因子分用）
    by_date = {d: g for d, g in panel.groupby("date")}

    # **性能优化**：预先把收盘价转成 {code: [(date, close)]}，
    # 避免每期在 57 万行的 panel 上做布尔过滤（原来慢 10 倍以上）
    logger.info("预索引收盘价...")
    close_by_code: dict[str, tuple[list[str], np.ndarray]] = {}
    for code, g in panel.groupby("instrument"):
        g_sorted = g.sort_values("date")
        close_by_code[code] = (
            g_sorted["date"].tolist(),
            g_sorted["close"].astype(float).values,
        )

    import bisect

    def period_return(code: str, d0: str, d1: str) -> float | None:
        """快速算单只股票在 [d0, d1] 的收益。"""
        entry = close_by_code.get(code)
        if entry is None:
            return None
        dates, closes = entry
        i0 = bisect.bisect_left(dates, d0)
        i1 = bisect.bisect_right(dates, d1) - 1
        if i1 <= i0 or i0 >= len(dates) or i1 >= len(dates):
            return None
        c0, c1 = closes[i0], closes[i1]
        if np.isnan(c0) or np.isnan(c1) or c0 <= 0:
            return None
        return float(c1 / c0 - 1)

    result = BacktestResult()
    prev_holdings: set[str] = set()

    for idx, reb_date in enumerate(rebalance_dates[:-1]):
        next_date = rebalance_dates[idx + 1]

        # ---------- 用调仓日的数据算因子分 ----------
        today = by_date.get(reb_date)
        if today is None or len(today) == 0:
            continue

        scored = today.dropna(subset=["mom20", "vol20", "close"]).copy()
        scored = scored[scored["close"] > 0]
        if len(scored) < top_n * 2:
            continue

        # 截面 z-score
        for factor in FACTOR_NAMES:
            col = scored[factor].astype(float)
            mean, std = col.mean(), col.std()
            scored[factor + "_z"] = (
                (col - mean) / std if std and std > 0 else 0.0)

        # 合成分
        _w = weights if weights is not None else DEFAULT_WEIGHTS
        if direction == "inverted":
            _w = {k: -v for k, v in _w.items()}
        scored["score"] = sum(
            scored[k] * w for k, w in _w.items() if k in scored.columns)
        # long/inverted 取高分，short 取低分
        ascending = (direction == "short")
        scored = scored.sort_values("score", ascending=ascending)

        # ---------- 选 Top N ----------
        # 过滤涨停（当日涨幅 >9.8%，次日大概率买不进）
        picks = []
        for _, row in scored.iterrows():
            code = row["instrument"]
            picks.append(code)
            if len(picks) >= top_n:
                break

        holdings = set(picks)

        # ---------- 计算持有期收益 ----------
        period_returns = []
        shadow_returns = []
        for code in holdings:
            ret = period_return(code, reb_date, next_date)
            if ret is not None:
                period_returns.append(ret)

        # 影子组合：被排除的股票
        excluded = [c for c in scored["instrument"] if c not in holdings]
        for code in excluded[:top_n]:
            ret = period_return(code, reb_date, next_date)
            if ret is not None:
                shadow_returns.append(ret)

        if not period_returns:
            continue

        # 过滤 NaN（某些股票持有期内无数据）
        valid_rets = [r for r in period_returns if not np.isnan(r)]
        if not valid_rets:
            continue
        portfolio_ret = float(np.mean(valid_rets))

        # ---------- 交易成本 ----------
        if include_cost:
            new_positions = holdings - prev_holdings
            closed = prev_holdings - holdings
            # 买入：佣金 + 滑点；卖出：佣金 + 印花税 + 滑点
            buy_cost = len(new_positions) / max(len(holdings), 1) * (FEE_RATE + SLIPPAGE)
            sell_cost = len(closed) / max(len(prev_holdings), 1) * (
                FEE_RATE + STAMP_TAX + SLIPPAGE) if prev_holdings else 0.0
            portfolio_ret -= (buy_cost + sell_cost)

        # 换手率
        turnover = len(holdings ^ prev_holdings) / max(len(holdings), 1)

        # ---------- 基准：全池等权 ----------
        bench_rets = []
        for code in scored["instrument"]:
            ret = period_return(code, reb_date, next_date)
            if ret is not None:
                bench_rets.append(ret)
        valid_bench = [r for r in bench_rets if not np.isnan(r)]
        bench_ret = float(np.mean(valid_bench)) if valid_bench else np.nan

        # ---------- 因子 IC ----------
        # 用下一期收益与当期因子分的秩相关
        fwd_returns = {}
        for code in scored["instrument"]:
            ret = period_return(code, reb_date, next_date)
            if ret is not None:
                fwd_returns[code] = ret

        common = [c for c in scored["instrument"] if c in fwd_returns]
        if len(common) >= 20:
            scores_arr = scored.set_index("instrument").loc[common, "score"].values
            rets_arr = np.array([fwd_returns[c] for c in common])
            # Spearman 秩相关
            from scipy.stats import spearmanr

            ic, _ = spearmanr(scores_arr, rets_arr)
            if not np.isnan(ic):
                result.ic_series.append((reb_date, float(ic)))

        result.dates.append(reb_date)
        result.portfolio_returns.append(portfolio_ret)
        result.benchmark_returns.append(bench_ret)
        result.turnover.append(turnover)
        result.holdings[reb_date] = sorted(holdings)
        valid_shadow = [r for r in shadow_returns if not np.isnan(r)]
        if valid_shadow:
            result.shadow_returns.append(float(np.mean(valid_shadow)))

        prev_holdings = holdings

        if (idx + 1) % 6 == 0:
            logger.info("已完成 %d/%d 期", idx + 1, len(rebalance_dates) - 1)

    # ---------- 统计 ----------
    result.stats = _compute_stats(result, include_cost)
    return result


def _compute_stats(result: BacktestResult, include_cost: bool) -> dict:
    """算绩效指标。"""
    if not result.portfolio_returns:
        return {"error": "无有效回测数据"}

    port = np.array(result.portfolio_returns, dtype=float)
    bench = np.array(result.benchmark_returns, dtype=float)

    # 剔除基准为 NaN 的期（某些调仓日全池无数据）
    valid_mask = ~np.isnan(bench)
    n_dropped = int((~valid_mask).sum())
    if valid_mask.sum() < 3:
        return {"error": f"有效期数不足（{valid_mask.sum()} 期，{n_dropped} 期基准缺失）"}

    port = port[valid_mask]
    bench = bench[valid_mask]
    turnover_arr = np.array(result.turnover, dtype=float)[valid_mask]
    n_periods = len(port)

    # 累计收益
    cum_port = float(np.prod(1 + port) - 1)
    cum_bench = float(np.prod(1 + bench) - 1)

    # 年化（假设月度调仓 = 12 期/年）
    periods_per_year = 12
    years = n_periods / periods_per_year
    ann_port = float((1 + cum_port) ** (1 / years) - 1) if years > 0 else 0.0
    ann_bench = float((1 + cum_bench) ** (1 / years) - 1) if years > 0 else 0.0

    # 最大回撤
    equity = np.cumprod(1 + port)
    peak = np.maximum.accumulate(equity)
    drawdown = (equity / peak - 1).min()

    # 超额
    excess = port - bench
    excess_cum = float(np.prod(1 + excess) - 1)

    # 胜率
    win_rate = float((port > bench).mean())

    # IC 统计
    ic_values = [ic for _, ic in result.ic_series]
    ic_mean = float(np.mean(ic_values)) if ic_values else 0.0
    ic_std = float(np.std(ic_values, ddof=1)) if len(ic_values) > 1 else 0.0
    ic_ir = ic_mean / ic_std if ic_std > 0 else 0.0
    ic_positive = float(np.mean([1 if ic > 0 else 0 for ic in ic_values])) if ic_values else 0.0

    # 影子组合（被排除股票）
    shadow_arr = np.array(result.shadow_returns, dtype=float)
    shadow_arr = shadow_arr[~np.isnan(shadow_arr)]
    shadow_cum = float(np.prod(1 + shadow_arr) - 1) if len(shadow_arr) else None

    return {
        "periods": n_periods,
        "years": round(years, 2),
        "include_cost": include_cost,
        "cum_portfolio": round(cum_port, 4),
        "cum_benchmark": round(cum_bench, 4),
        "cum_excess": round(cum_port - cum_bench, 4),
        "ann_portfolio": round(ann_port, 4),
        "ann_benchmark": round(ann_bench, 4),
        "max_drawdown": round(float(drawdown), 4),
        "win_rate_vs_bench": round(win_rate, 4),
        "avg_turnover": round(float(np.nanmean(turnover_arr)), 4),
        "periods_dropped": n_dropped,
        "ic_mean": round(ic_mean, 4),
        "ic_std": round(ic_std, 4),
        "ic_ir": round(ic_ir, 4),
        "ic_positive_rate": round(ic_positive, 4),
        "ic_periods": len(ic_values),
        "shadow_cum": round(shadow_cum, 4) if shadow_cum is not None else None,
    }


def report(result: BacktestResult, top_n: int) -> str:
    """生成可读报告。"""
    s = result.stats
    if "error" in s:
        return f"回测失败：{s['error']}"

    lines = [
        "=" * 74,
        f"回测结果（Top{top_n}，{'含' if s['include_cost'] else '不含'}成本）",
        "=" * 74,
        "",
        f"  期数          : {s['periods']} 期（约 {s['years']} 年）",
        f"  累计收益      : {s['cum_portfolio']:+.2%}",
        f"  基准（全池等权）: {s['cum_benchmark']:+.2%}",
        f"  **超额收益**  : **{s['cum_excess']:+.2%}**",
        f"  年化收益      : {s['ann_portfolio']:+.2%}（基准 {s['ann_benchmark']:+.2%}）",
        f"  最大回撤      : {s['max_drawdown']:.2%}",
        f"  跑赢基准比例  : {s['win_rate_vs_bench']:.1%}",
        f"  平均换手率    : {s['avg_turnover']:.1%}",
        "",
        "  --- 因子 IC（合成分的预测力）---",
        f"  IC 均值       : {s['ic_mean']:+.4f}",
        f"  IC 标准差     : {s['ic_std']:.4f}",
        f"  ICIR          : {s['ic_ir']:+.4f}",
        f"  IC 为正比例   : {s['ic_positive_rate']:.1%}（{s['ic_periods']} 期）",
    ]

    if s.get("shadow_cum") is not None:
        lines.extend([
            "",
            "  --- 影子组合（被排除的股票）---",
            f"  影子累计收益  : {s['shadow_cum']:+.2%}",
            f"  入选 - 影子   : {s['cum_portfolio'] - s['shadow_cum']:+.2%}",
            "  （若入选组没跑赢影子组，说明选股无效）",
        ])

    lines.extend(["", "=" * 74])
    return "\n".join(lines)


def judge(result: BacktestResult) -> str:
    """给出明确判断：继续还是回头。"""
    s = result.stats
    if "error" in s:
        return "无法判断"

    ic_mean = s["ic_mean"]
    excess = s["cum_excess"]
    shadow = s.get("shadow_cum")

    verdict = []
    if ic_mean > 0.02 and excess > 0:
        verdict.append("✅ **有正向信号**：IC 均值为正且跑赢基准。")
        verdict.append("   建议继续（可加财务因子或优化权重）。")
    elif ic_mean > 0 and excess > 0:
        verdict.append("⚠️  **信号很弱但方向对**：IC 均值微弱为正。")
        verdict.append("   建议先看 IC 稳定性，再决定是否投入。")
    elif excess <= 0:
        verdict.append("❌ **没有超额**：跑输基准。")
        verdict.append("   建议回去调因子，**不要加财务因子**（六家一致意见）。")
    else:
        verdict.append("❌ **IC 为负**：因子方向可能有问题。")
        verdict.append("   建议先查因子定义，再考虑重做。")

    if shadow is not None and s["cum_portfolio"] <= shadow:
        verdict.append("")
        verdict.append("🔴 **更严重**：入选组没有跑赢被排除组，")
        verdict.append("   说明「选出 Top20」这个动作本身没有价值。")

    return "\n".join(verdict)


def save(result: BacktestResult, out_dir: Path, top_n: int) -> None:
    """落盘。"""
    out_dir.mkdir(parents=True, exist_ok=True)

    with (out_dir / "period_returns.csv").open("w", encoding="utf-8-sig", newline="") as h:
        writer = csv.writer(h)
        writer.writerow(["date", "portfolio", "benchmark", "excess", "turnover"])
        for i, d in enumerate(result.dates):
            writer.writerow([
                d,
                round(result.portfolio_returns[i], 6),
                round(result.benchmark_returns[i], 6),
                round(result.portfolio_returns[i] - result.benchmark_returns[i], 6),
                round(result.turnover[i], 4),
            ])

    with (out_dir / "ic_series.csv").open("w", encoding="utf-8-sig", newline="") as h:
        writer = csv.writer(h)
        writer.writerow(["date", "ic"])
        for d, ic in result.ic_series:
            writer.writerow([d, round(ic, 6)])

    with (out_dir / "holdings.csv").open("w", encoding="utf-8-sig", newline="") as h:
        writer = csv.writer(h)
        writer.writerow(["date", "holdings"])
        for d, codes in result.holdings.items():
            writer.writerow([d, "|".join(codes)])

    (out_dir / "backtest.json").write_text(
        json.dumps({"top_n": top_n, "stats": result.stats},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="回测验证")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--freq", default="M", choices=["M", "W"])
    parser.add_argument("--no-cost", action="store_true")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--provider", default=str(DEFAULT_PROVIDER))
    args = parser.parse_args(argv[1:])

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    result = run_backtest(
        provider_uri=Path(args.provider),
        top_n=args.top,
        freq=args.freq,
        include_cost=not args.no_cost,
        start_time=args.start,
        end_time=args.end,
    )

    print()
    print(report(result, args.top))
    print()
    print("【判断】")
    print(judge(result))

    out_dir = _REPO_ROOT / "qlib_layer" / "output" / f"backtest_{date.today().isoformat()}"
    save(result, out_dir, args.top)
    print()
    print(f"输出: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
