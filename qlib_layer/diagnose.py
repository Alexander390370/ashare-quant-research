"""因子有效性诊断：找出哪个因子真的有效、方向对不对。

③ 回测发现合成分 IC = -0.051（负向），Top20 跑输基准 32 个点。
这个脚本拆开看：

1. **单因子 IC**：每个因子单独的预测力
2. **方向测试**：正向 vs 反向，哪个更好
3. **分位数收益**：按因子分 5 档，看单调性

如果单因子 IC 都是负的，说明这套价量因子在 A 股指数池里方向反了。
如果有正有负，说明是权重配错了。

用法
----
    python -m qlib_layer.diagnose
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tradingagents  # noqa: E402,F401

from qlib_layer.factors import FACTOR_EXPRESSIONS, FACTOR_NAMES  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER = _REPO_ROOT / "qlib_layer" / "data" / "cn_a"


def compute_single_factor_ic(
    provider_uri: Path,
    forward_days: int = 20,
    min_stocks: int = 100,
) -> dict:
    """计算每个单因子的 IC 序列。

    IC = Spearman(当期因子值, 未来 forward_days 收益)，逐期算再取均值。
    """
    import qlib
    from qlib.data import D
    from scipy.stats import spearmanr

    qlib.init(provider_uri=str(provider_uri), region="cn", expression_cache=None)

    codes = D.list_instruments(
        D.instruments(market="all"),
        start_time="2020-01-01", end_time="2030-12-31", as_list=True)
    calendar = [str(c)[:10] for c in D.calendar(start_time="2020-01-01", end_time="2030-12-31")]

    fields = ["$close"] + [FACTOR_EXPRESSIONS[n] for n in FACTOR_NAMES]
    panel = D.features(codes, fields, freq="day")
    panel.columns = ["close"] + FACTOR_NAMES
    panel = panel.reset_index()
    panel["date"] = panel["datetime"].astype(str).str[:10]
    panel = panel.drop(columns=["datetime"])

    # 月度调仓日
    all_dates = sorted(panel["date"].unique())
    month_ends = []
    for i in range(len(all_dates) - 1):
        if all_dates[i][:7] != all_dates[i + 1][:7]:
            month_ends.append(all_dates[i])

    # 每期的未来收益
    by_code = {c: g.sort_values("date") for c, g in panel.groupby("instrument")}

    ic_by_factor: dict[str, list[float]] = {f: [] for f in FACTOR_NAMES}

    for idx, reb_date in enumerate(month_ends[:-1]):
        # 未来 forward_days 后的日期
        future_idx = min(idx + 1, len(month_ends) - 1)
        next_date = month_ends[future_idx]
        if reb_date == next_date:
            continue

        today = panel[panel["date"] == reb_date]
        if len(today) < min_stocks:
            continue

        # 算每只股票的未来收益
        fwd = {}
        for code, g in by_code.items():
            sub = g[(g["date"] >= reb_date) & (g["date"] <= next_date)]
            if len(sub) < 2:
                continue
            closes = sub["close"].values
            if np.isnan(closes[0]) or np.isnan(closes[-1]) or closes[0] <= 0:
                continue
            ret = closes[-1] / closes[0] - 1
            if not np.isnan(ret):
                fwd[code] = ret

        common = [c for c in today["instrument"] if c in fwd]
        if len(common) < min_stocks:
            continue

        today_idx = today.set_index("instrument")
        rets = np.array([fwd[c] for c in common])

        for factor in FACTOR_NAMES:
            vals = today_idx.loc[common, factor].values.astype(float)
            mask = ~np.isnan(vals) & ~np.isnan(rets)
            if mask.sum() < min_stocks:
                continue
            ic, _ = spearmanr(vals[mask], rets[mask])
            if not np.isnan(ic):
                ic_by_factor[factor].append(float(ic))

    # 汇总
    out = {}
    for factor, ics in ic_by_factor.items():
        if not ics:
            out[factor] = {"ic_mean": None, "periods": 0}
            continue
        arr = np.array(ics)
        out[factor] = {
            "ic_mean": round(float(arr.mean()), 4),
            "ic_std": round(float(arr.std(ddof=1)), 4) if len(arr) > 1 else 0.0,
            "ic_ir": round(float(arr.mean() / arr.std(ddof=1)), 4)
            if len(arr) > 1 and arr.std(ddof=1) > 0 else 0.0,
            "ic_positive_rate": round(float((arr > 0).mean()), 4),
            "periods": len(arr),
        }
    return out


def quantile_returns(
    provider_uri: Path,
    min_stocks: int = 100,
    n_quantiles: int = 5,
) -> dict:
    """按合成分分 n 档，看每档的未来收益（检验单调性）。"""
    import qlib
    from qlib.data import D

    from qlib_layer.factors import DEFAULT_WEIGHTS

    qlib.init(provider_uri=str(provider_uri), region="cn", expression_cache=None)

    codes = D.list_instruments(
        D.instruments(market="all"),
        start_time="2020-01-01", end_time="2030-12-31", as_list=True)
    fields = ["$close"] + [FACTOR_EXPRESSIONS[n] for n in FACTOR_NAMES]
    panel = D.features(codes, fields, freq="day")
    panel.columns = ["close"] + FACTOR_NAMES
    panel = panel.reset_index()
    panel["date"] = panel["datetime"].astype(str).str[:10]
    panel = panel.drop(columns=["datetime"])

    all_dates = sorted(panel["date"].unique())
    month_ends = [
        all_dates[i] for i in range(len(all_dates) - 1)
        if all_dates[i][:7] != all_dates[i + 1][:7]
    ]
    by_code = {c: g.sort_values("date") for c, g in panel.groupby("instrument")}

    bucket_returns: dict[int, list[float]] = {i: [] for i in range(n_quantiles)}

    for idx, reb_date in enumerate(month_ends[:-1]):
        next_date = month_ends[min(idx + 1, len(month_ends) - 1)]
        if reb_date == next_date:
            continue

        today = panel[panel["date"] == reb_date].dropna(
            subset=["mom20", "vol20", "close"])
        if len(today) < min_stocks:
            continue
        today = today[today["close"] > 0].copy()

        # z-score + 合成分
        for f in FACTOR_NAMES:
            col = today[f].astype(float)
            mean, std = col.mean(), col.std()
            today[f + "_z"] = (col - mean) / std if std and std > 0 else 0.0
        today["score"] = sum(
            today[k] * w for k, w in DEFAULT_WEIGHTS.items() if k in today.columns)

        today = today.sort_values("score", ascending=False)

        # 分档
        n = len(today)
        per_bucket = n // n_quantiles
        if per_bucket < 10:
            continue

        for q in range(n_quantiles):
            start = q * per_bucket
            end = (q + 1) * per_bucket if q < n_quantiles - 1 else n
            members = today.iloc[start:end]["instrument"].tolist()

            rets = []
            for code in members:
                g = by_code.get(code)
                if g is None:
                    continue
                sub = g[(g["date"] >= reb_date) & (g["date"] <= next_date)]
                if len(sub) < 2:
                    continue
                closes = sub["close"].values
                if np.isnan(closes[0]) or np.isnan(closes[-1]) or closes[0] <= 0:
                    continue
                r = closes[-1] / closes[0] - 1
                if not np.isnan(r):
                    rets.append(r)
            if rets:
                bucket_returns[q].append(float(np.mean(rets)))

    out = {}
    for q, rets in bucket_returns.items():
        if rets:
            arr = np.array(rets)
            out[f"Q{q + 1}"] = {
                "mean_period_return": round(float(arr.mean()), 4),
                "cum_return": round(float(np.prod(1 + arr) - 1), 4),
                "periods": len(arr),
            }
        else:
            out[f"Q{q + 1}"] = {"mean_period_return": None, "periods": 0}
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="因子有效性诊断")
    parser.add_argument("--provider", default=str(DEFAULT_PROVIDER))
    args = parser.parse_args(argv[1:])

    logging.basicConfig(level=logging.WARNING)
    provider = Path(args.provider)

    print("=" * 76)
    print("因子有效性诊断")
    print("=" * 76)

    # ---------- 1. 单因子 IC ----------
    print("\n【1】单因子 IC（Spearman，月度，未来 1 期收益）")
    print("  IC > 0 表示因子值越高、未来收益越高（正向预测）")
    print("  IC < 0 表示反向预测")
    print()
    ic_result = compute_single_factor_ic(provider)
    print(f"  {'因子':<12} {'IC 均值':>10} {'ICIR':>8} {'IC>0比例':>10} {'期数':>6}  判定")
    print("  " + "-" * 66)
    for factor, stats in ic_result.items():
        if stats["ic_mean"] is None:
            print(f"  {factor:<12} {'N/A':>10}")
            continue
        ic = stats["ic_mean"]
        if abs(ic) < 0.02:
            verdict = "无效"
        elif ic > 0:
            verdict = "正向有效"
        else:
            verdict = "**反向有效**"
        print(f"  {factor:<12} {ic:>+10.4f} {stats['ic_ir']:>+8.4f} "
              f"{stats['ic_positive_rate']:>9.1%} {stats['periods']:>6}  {verdict}")

    # ---------- 2. 分档收益 ----------
    print("\n【2】合成分 5 档的未来收益（检验单调性）")
    print("  Q1 = 分数最高（当前策略买入的档），Q5 = 分数最低")
    print()
    q_result = quantile_returns(provider)
    print(f"  {'档位':<8} {'单期均收益':>12} {'累计收益':>12} {'期数':>6}")
    print("  " + "-" * 44)
    for q, stats in q_result.items():
        if stats["mean_period_return"] is None:
            print(f"  {q:<8} {'N/A':>12}")
            continue
        print(f"  {q:<8} {stats['mean_period_return']:>+11.2%} "
              f"{stats['cum_return']:>+11.2%} {stats['periods']:>6}")

    # ---------- 3. 判断 ----------
    print()
    print("=" * 76)
    print("判断")
    print("=" * 76)

    valid_factors = {
        f: s for f, s in ic_result.items()
        if s["ic_mean"] is not None and abs(s["ic_mean"]) >= 0.02
    }
    positive = {f: s for f, s in valid_factors.items() if s["ic_mean"] > 0}
    negative = {f: s for f, s in valid_factors.items() if s["ic_mean"] < 0}

    if positive:
        print(f"\n  ✅ 正向有效因子: {', '.join(positive)}")
    if negative:
        print(f"\n  🔄 反向有效因子（应取反用）: {', '.join(negative)}")

    # 分档单调性
    qs = [q_result.get(f"Q{i}") for i in range(1, 6)]
    if all(q and q["mean_period_return"] is not None for q in qs):
        q1 = qs[0]["mean_period_return"]
        q5 = qs[-1]["mean_period_return"]
        if q5 > q1:
            print(f"\n  🔄 **分档反向**：Q5 ({q5:+.2%}) > Q1 ({q1:+.2%})")
            print("     说明当前权重方向错了，应该反过来选（买低分档）")
        else:
            print(f"\n  ✅ 分档正向：Q1 ({q1:+.2%}) > Q5 ({q5:+.2%})")

    print()

    # ---------- 落盘 ----------
    out_dir = _REPO_ROOT / "qlib_layer" / "output" / "diagnose"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "diagnose.json").write_text(
        json.dumps({"single_factor_ic": ic_result, "quantile_returns": q_result},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")

    with (out_dir / "single_factor_ic.csv").open("w", encoding="utf-8-sig", newline="") as h:
        writer = csv.writer(h)
        writer.writerow(["factor", "ic_mean", "ic_std", "ic_ir", "ic_positive_rate", "periods"])
        for f, s in ic_result.items():
            writer.writerow([f, s.get("ic_mean"), s.get("ic_std"),
                             s.get("ic_ir"), s.get("ic_positive_rate"), s.get("periods")])

    print(f"输出: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
