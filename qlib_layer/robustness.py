"""稳健性检验：A 分时段 / B 分市场状态 / C 参数敏感性。

目的
----
纯反向策略在 2023-10 ~ 2026-10 跑出 +99.79% 超额。
但这是**事后调参**（看到 IC 为负才反向），极可能是过拟合。

三种检验
--------
A. **分时段**：把 2.83 年切成 3 段，看每段超额是否稳定。
   若某段为负 -> 说明高收益来自特定时段，不可信。

B. **分市场状态**：按基准涨跌切分，看策略在上涨市/下跌市表现。
   若只在单一状态下有效 -> 说明是 beta 暴露不是 alpha。

C. **参数敏感性**：Top10/20/30、月度/周度。
   若参数一变收益崩 -> 说明过拟合。

用法
----
    python -m qlib_layer.robustness
    python -m qlib_layer.robustness --only A    # 只跑分时段
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tradingagents  # noqa: E402,F401

from qlib_layer.backtest import DEFAULT_PROVIDER, run_backtest  # noqa: E402

logger = logging.getLogger(__name__)

# 反向策略的权重（诊断得出的 IC 方向）
REVERSED_WEIGHTS = {
    "rev1_z": 0.0293,
    "mom5_z": -0.0394,
    "mom20_z": -0.0328,
    "dev20_z": -0.0393,
    "vol20_z": 0.0,
    "vol_ratio_z": 0.0,
}

# 纯反向（全取反）
INVERTED = "inverted"


def _summary(result) -> dict:
    s = result.stats
    if "error" in s:
        return {"error": s["error"]}
    return {
        "periods": s["periods"],
        "cum_portfolio": s["cum_portfolio"],
        "cum_benchmark": s["cum_benchmark"],
        "cum_excess": s["cum_excess"],
        "ann_portfolio": s["ann_portfolio"],
        "max_drawdown": s["max_drawdown"],
        "ic_mean": s["ic_mean"],
        "avg_turnover": s["avg_turnover"],
        "shadow_cum": s.get("shadow_cum"),
    }


# ---------------------------------------------------------------------------
# A. 分时段检验
# ---------------------------------------------------------------------------
def test_periods() -> dict:
    """把整个区间切成 3 段，分别跑反向策略。"""
    print("\n" + "=" * 78)
    print("A. 分时段检验（把 2.83 年切成 3 段）")
    print("=" * 78)

    # 整个区间是 2023-10-31 ~ 2026-10-30
    segments = [
        ("第1段 2023-11 ~ 2024-10", "2023-10-31", "2024-10-31"),
        ("第2段 2024-11 ~ 2025-10", "2024-10-31", "2025-10-31"),
        ("第3段 2025-11 ~ 2026-10", "2025-10-31", "2026-10-30"),
    ]

    out = {}
    for label, start, end in segments:
        print(f"\n  --- {label} ---")
        try:
            result = run_backtest(
                provider_uri=DEFAULT_PROVIDER,
                top_n=20,
                freq="M",
                include_cost=True,
                start_time=start,
                end_time=end,
                direction=INVERTED,
            )
            s = _summary(result)
            out[label] = s
            if "error" in s:
                print(f"    ❌ {s['error']}")
                continue
            print(f"    期数 {s['periods']} | 累计 {s['cum_portfolio']:+.2%} | "
                  f"基准 {s['cum_benchmark']:+.2%} | **超额 {s['cum_excess']:+.2%}** | "
                  f"回撤 {s['max_drawdown']:.2%}")
        except Exception as exc:  # noqa: BLE001
            print(f"    ❌ 失败: {str(exc)[:60]}")
            out[label] = {"error": str(exc)[:80]}

    # 判断
    print()
    excesses = [s.get("cum_excess") for s in out.values() if "error" not in s]
    if excesses:
        positive = sum(1 for e in excesses if e > 0)
        print(f"  【判断】{positive}/{len(excesses)} 段超额为正")
        if positive == len(excesses):
            print("    ✅ 全部为正，策略在各时段稳定")
        elif positive == 0:
            print("    🔴 全部为负，之前的 +99.79% 是特定区间的运气")
        else:
            print("    ⚠️  部分为正，说明收益依赖特定时段，不可信")

    return out


# ---------------------------------------------------------------------------
# B. 分市场状态检验
# ---------------------------------------------------------------------------
def test_market_states() -> dict:
    """按基准涨跌切分，看策略在上涨市/下跌市表现。"""
    print("\n" + "=" * 78)
    print("B. 分市场状态检验（上涨市 vs 下跌市）")
    print("=" * 78)

    print("\n  先跑全区间，拿到逐期基准收益...")
    result = run_backtest(
        provider_uri=DEFAULT_PROVIDER,
        top_n=20,
        freq="M",
        include_cost=True,
        direction=INVERTED,
    )

    if "error" in result.stats:
        print(f"  ❌ {result.stats['error']}")
        return {}

    dates = result.dates
    port = np.array(result.portfolio_returns)
    bench = np.array(result.benchmark_returns)

    # 剔除 NaN
    mask = ~np.isnan(bench)
    dates = [d for d, m in zip(dates, mask, strict=True) if m]
    port, bench = port[mask], bench[mask]

    # 按基准涨跌分组
    up_mask = bench > 0
    down_mask = bench <= 0

    out = {}
    for label, m in [("上涨市（基准>0）", up_mask), ("下跌市（基准<=0）", down_mask)]:
        n = int(m.sum())
        if n == 0:
            out[label] = {"periods": 0}
            print(f"\n  --- {label} ---")
            print("    无样本")
            continue

        p = port[m]
        b = bench[m]
        cum_p = float(np.prod(1 + p) - 1)
        cum_b = float(np.prod(1 + b) - 1)
        # 平均单期
        avg_p = float(p.mean())
        avg_b = float(b.mean())

        out[label] = {
            "periods": n,
            "cum_portfolio": round(cum_p, 4),
            "cum_benchmark": round(cum_b, 4),
            "cum_excess": round(cum_p - cum_b, 4),
            "avg_portfolio": round(avg_p, 4),
            "avg_benchmark": round(avg_b, 4),
            "avg_excess": round(avg_p - avg_b, 4),
            "win_rate": round(float((p > b).mean()), 4),
        }
        print(f"\n  --- {label} ---")
        print(f"    期数 {n}")
        print(f"    累计：组合 {cum_p:+.2%} | 基准 {cum_b:+.2%} | 超额 {cum_p - cum_b:+.2%}")
        print(f"    单期均值：组合 {avg_p:+.2%} | 基准 {avg_b:+.2%} | 超额 {avg_p - avg_b:+.2%}")
        print(f"    跑赢基准比例 {out[label]['win_rate']:.1%}")

    # 判断
    print()
    up = out.get("上涨市（基准>0）", {})
    down = out.get("下跌市（基准<=0）", {})
    if up.get("periods") and down.get("periods"):
        up_avg = up["avg_excess"]
        down_avg = down["avg_excess"]
        print("  【判断】")
        print(f"    上涨市单期超额 {up_avg:+.2%} | 下跌市单期超额 {down_avg:+.2%}")
        if up_avg > 0 and down_avg > 0:
            print("    ✅ 两种状态都为正，可能是真 alpha")
        elif up_avg > 0 and down_avg < 0:
            print("    ⚠️  只在上涨市有效 —— 这是 beta 暴露，不是 alpha")
        elif up_avg < 0 and down_avg > 0:
            print("    ⚠️  只在下跌市有效 —— 防御型，牛市会跑输")
        else:
            print("    🔴 两种状态都为负，策略无效")

    return out


# ---------------------------------------------------------------------------
# C. 参数敏感性检验
# ---------------------------------------------------------------------------
def test_parameters() -> dict:
    """测不同 Top N 和调仓频率。"""
    print("\n" + "=" * 78)
    print("C. 参数敏感性检验")
    print("=" * 78)

    configs = [
        ("Top10 月度", 10, "M"),
        ("Top20 月度", 20, "M"),
        ("Top30 月度", 30, "M"),
        ("Top50 月度", 50, "M"),
        ("Top20 周度", 20, "W"),
    ]

    out = {}
    for label, top_n, freq in configs:
        print(f"\n  --- {label} ---")
        try:
            result = run_backtest(
                provider_uri=DEFAULT_PROVIDER,
                top_n=top_n,
                freq=freq,
                include_cost=True,
                direction=INVERTED,
            )
            s = _summary(result)
            out[label] = s
            if "error" in s:
                print(f"    ❌ {s['error']}")
                continue
            print(f"    期数 {s['periods']} | 累计 {s['cum_portfolio']:+.2%} | "
                  f"**超额 {s['cum_excess']:+.2%}** | "
                  f"回撤 {s['max_drawdown']:.2%} | 换手 {s['avg_turnover']:.0%}")
        except Exception as exc:  # noqa: BLE001
            print(f"    ❌ 失败: {str(exc)[:60]}")
            out[label] = {"error": str(exc)[:80]}

    # 判断
    print()
    excesses = [s.get("cum_excess") for s in out.values() if "error" not in s]
    if excesses:
        arr = np.array(excesses)
        print("  【判断】")
        print(f"    超额范围：{arr.min():+.2%} ~ {arr.max():+.2%}")
        print(f"    均值 {arr.mean():+.2%} | 标准差 {arr.std():+.2%}")
        positive = int((arr > 0).sum())
        print(f"    {positive}/{len(arr)} 个参数组合超额为正")
        if positive == len(arr) and arr.std() < abs(arr.mean()):
            print("    ✅ 参数稳健：所有组合都为正且波动小于均值")
        elif positive >= len(arr) * 0.6:
            print("    ⚠️  多数为正但不稳定，有过拟合风险")
        else:
            print("    🔴 多数为负，说明 +99.79% 是特定参数的运气")

    return out


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def final_verdict(results: dict) -> str:
    """综合三项检验给出最终判断。"""
    lines = ["", "=" * 78, "综合裁决", "=" * 78, ""]

    # A
    a = results.get("A", {})
    a_excess = [s.get("cum_excess") for s in a.values() if "error" not in s]
    a_pos = sum(1 for e in a_excess if e > 0) if a_excess else 0
    a_total = len(a_excess)

    # B
    b = results.get("B", {})
    up = b.get("上涨市（基准>0）", {})
    down = b.get("下跌市（基准<=0）", {})
    both_positive = (
        up.get("avg_excess", -1) > 0 and down.get("avg_excess", -1) > 0)

    # C
    c = results.get("C", {})
    c_excess = [s.get("cum_excess") for s in c.values() if "error" not in s]
    c_pos = sum(1 for e in c_excess if e > 0) if c_excess else 0
    c_total = len(c_excess)

    score = 0
    if a_total and a_pos == a_total:
        score += 1
        lines.append(f"  ✅ A 分时段：{a_pos}/{a_total} 段超额为正")
    else:
        lines.append(f"  ❌ A 分时段：{a_pos}/{a_total} 段超额为正")

    if both_positive:
        score += 1
        lines.append("  ✅ B 市场状态：上涨市和下跌市都为正")
    else:
        lines.append("  ❌ B 市场状态：只在单一状态有效")

    if c_total and c_pos >= c_total * 0.6:
        score += 1
        lines.append(f"  ✅ C 参数敏感性：{c_pos}/{c_total} 个组合为正")
    else:
        lines.append(f"  ❌ C 参数敏感性：{c_pos}/{c_total} 个组合为正")

    lines.append("")
    lines.append(f"  得分：{score}/3")
    lines.append("")

    if score == 3:
        lines.append("  **裁决：通过三项稳健性检验。**")
        lines.append("  纯反向策略在分时段、分市场状态、参数变动下都稳定。")
        lines.append("  这仍不等于「可以实盘」——还需样本外验证和实盘跟踪。")
        lines.append("  但至少不是明显的过拟合。")
    elif score == 2:
        lines.append("  **裁决：部分通过。**")
        lines.append("  策略在某些维度稳定、某些不稳定。")
        lines.append("  **不建议直接实盘**，但值得继续研究。")
    else:
        lines.append("  **裁决：未通过。**")
        lines.append("  +99.79% 大概率是过拟合或特定时段的运气。")
        lines.append("  六家的警告成立：「数据错误恰好拟合了历史」。")
        lines.append("  建议：不要基于这个结果做任何决策。")

    lines.append("")
    lines.append("=" * 78)
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="稳健性检验 A/B/C")
    parser.add_argument("--only", default=None, choices=["A", "B", "C"])
    args = parser.parse_args(argv[1:])

    logging.basicConfig(level=logging.WARNING)

    print("=" * 78)
    print("稳健性检验：+99.79% 是真信号还是过拟合？")
    print("=" * 78)

    results = {}

    if args.only in (None, "A"):
        results["A"] = test_periods()
    if args.only in (None, "B"):
        results["B"] = test_market_states()
    if args.only in (None, "C"):
        results["C"] = test_parameters()

    if args.only is None:
        print(final_verdict(results))

    # 落盘
    out_dir = _REPO_ROOT / "qlib_layer" / "output" / "robustness"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "robustness.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n输出: {out_dir}")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
