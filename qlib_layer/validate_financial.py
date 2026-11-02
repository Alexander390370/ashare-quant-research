"""财务因子验证：三项替代解释排查。

发现（2026-10-31）
-----------------
1000 只池子，34 期，行业中性化后：
    quality_growth  t=+2.92  vs随机 +80.99%
    quality         t=+2.26  vs随机 +48.93%
    growth          t=+2.14  vs随机 +91.35%
    quality_value   t=+2.17  vs随机 +11.71%
    value           t=+0.60  vs随机 +4.78%

4/5 策略 IC 统计显著（|t| > 1.96）—— 这是之前所有价量因子都没做到的。

但必须排除三个替代解释
----------------------
1. **小市值效应**：A 股 2023-2026 小盘股强，1000 只池子含大量中小盘。
   验证：市值分层中性化，看超额是否消失。

2. **样本内选择偏差**：34 期就是调参用的数据，看到结果再选 best。
   验证：2018-2023 样本外。

3. **前视偏差（最严重）**：用 2026-09 的财务数据选 2023 年的股票。
   验证：财务数据必须用「当时可得」的，即 PIT。

用法
----
    python -m qlib_layer.validate_financial --check cap        # 市值中性化
    python -m qlib_layer.validate_financial --check oos        # 样本外
    python -m qlib_layer.validate_financial --check pit        # 前视检查
    python -m qlib_layer.validate_financial --check all
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tradingagents  # noqa: E402,F401

from qlib_layer import financials, neutralize  # noqa: E402
from qlib_layer.backtest_financial import (  # noqa: E402
    STRATEGIES,
    FinResult,
    load_latest_candidates,
    prepare_data,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 检查 1：市值中性化
# ---------------------------------------------------------------------------
def check_market_cap_neutral(
    prep: dict,
    strategies: list[str],
    top_n: int = 30,
    n_cap_buckets: int = 5,
) -> dict:
    """市值分层中性化：在每个市值分位内选股，消除小盘效应。

    做法：
        1. 按市值分 5 档
        2. 每档内按财务因子打分
        3. 每档选 top_n/5 只（总持仓数不变）
        4. 对比 vs 随机的超额

    如果超额消失 -> 之前的结果是小盘效应
    如果超额保留 -> 财务因子有独立贡献
    """
    print("\n" + "=" * 88)
    print("检查 1：市值中性化（排除小盘效应）")
    print("=" * 88)

    merged = prep["merged"]
    by_code = prep["by_code"]
    month_ends = prep["month_ends"]

    if "market_cap_yi" not in merged.columns:
        print("  ❌ 缺市值字段，无法检查")
        return {"error": "缺市值字段"}

    valid_cap = merged["market_cap_yi"].notna() & (merged["market_cap_yi"] > 0)
    print(f"  有市值数据：{int(valid_cap.sum())}/{len(merged)}")

    if valid_cap.sum() < top_n * 5:
        print(f"  ❌ 市值数据太少（{int(valid_cap.sum())}）")
        return {"error": "市值数据太少"}

    # 市值分档
    work = merged[valid_cap].copy()
    work["cap_bucket"] = pd.qcut(
        work["market_cap_yi"], n_cap_buckets,
        labels=[f"Q{i+1}" for i in range(n_cap_buckets)], duplicates="drop")

    print(f"  市值分档：")
    for bucket, group in work.groupby("cap_bucket", observed=True):
        print(f"    {bucket}: {len(group):4d} 只，"
              f"市值 {group['market_cap_yi'].min():.0f} ~ "
              f"{group['market_cap_yi'].max():.0f} 亿")

    import bisect

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

    out = {}
    rng = np.random.default_rng(42)
    per_bucket = max(1, top_n // n_cap_buckets)

    for strategy_name in strategies:
        weights, desc = STRATEGIES[strategy_name]

        # 行业中性化打分
        scored = neutralize.neutralize_composite(
            work.reset_index(), weights, use_rank=True).set_index("code")

        if "composite_neutral" not in scored.columns:
            continue

        scored = scored[scored["composite_neutral"].notna()]
        if len(scored) < top_n * 2:
            continue

        # 每档内选 top
        picks_by_date = {}
        for reb in month_ends[:-1]:
            picks = []
            for bucket, group in scored.groupby("cap_bucket", observed=True):
                tradable = [c for c in group.index if is_tradable(c, reb)]
                if not tradable:
                    continue
                ranked = group.loc[tradable].sort_values(
                    "composite_neutral", ascending=False)
                picks.extend(ranked.index[:per_bucket].tolist())
            picks_by_date[reb] = picks

        # 回测
        port_rets = []
        rand_rets_list = []
        ic_list = []

        for idx, reb in enumerate(month_ends[:-1]):
            nxt = month_ends[idx + 1]
            picks = picks_by_date.get(reb, [])
            if not picks:
                continue

            rets = [r for c in picks if (r := get_ret(c, reb, nxt)) is not None]
            if not rets:
                continue
            port_rets.append(float(np.mean(rets)))

            # 随机对照：同档内随机选
            rand_sample = []
            for bucket, group in scored.groupby("cap_bucket", observed=True):
                tradable = [c for c in group.index if is_tradable(c, reb)]
                if not tradable:
                    continue
                n_pick = min(per_bucket, len(tradable))
                sample = rng.choice(len(tradable), size=n_pick, replace=False)
                rand_sample.extend([tradable[i] for i in sample])
            r_rets = [r for c in rand_sample if (r := get_ret(c, reb, nxt)) is not None]
            if r_rets:
                rand_rets_list.append(float(np.mean(r_rets)))

            # IC
            all_tradable = [c for c in scored.index if is_tradable(c, reb)]
            fwd = {}
            for c in all_tradable:
                r = get_ret(c, reb, nxt)
                if r is not None:
                    fwd[c] = r
            common = [c for c in all_tradable if c in fwd]
            if len(common) >= 30:
                from scipy.stats import spearmanr

                s_vals = scored.loc[common, "composite_neutral"].values
                r_vals = np.array([fwd[c] for c in common])
                ic, _ = spearmanr(s_vals, r_vals)
                if not np.isnan(ic):
                    ic_list.append(float(ic))

        if not port_rets:
            continue

        port = np.array(port_rets)
        rand = np.array(rand_rets_list) if rand_rets_list else np.array([])

        cum_port = float(np.prod(1 + port) - 1)
        cum_rand = float(np.prod(1 + rand) - 1) if len(rand) else None

        # 超额
        excess = None
        if len(rand) == len(port) and len(port) > 0:
            excess = float(np.prod(1 + (port - rand)) - 1)

        ic_mean = float(np.mean(ic_list)) if ic_list else 0.0
        ic_std = float(np.std(ic_list, ddof=1)) if len(ic_list) > 1 else 0.0
        ic_t = (ic_mean / ic_std * np.sqrt(len(ic_list))) if ic_std > 0 else 0.0

        out[strategy_name] = {
            "periods": len(port),
            "cum_portfolio": round(cum_port, 4),
            "cum_random": round(cum_rand, 4) if cum_rand is not None else None,
            "excess_vs_random": round(excess, 4) if excess is not None else None,
            "ic_mean": round(ic_mean, 4),
            "ic_t_stat": round(float(ic_t), 2),
            "ic_significant": bool(abs(ic_t) > 1.96),
        }

        mark = "✅" if abs(ic_t) > 1.96 else "❌"
        print(f"\n  {mark} {strategy_name:16s} "
              f"累计 {cum_port:+.2%} | vs随机 {excess:+.2%} | "
              f"IC {ic_mean:+.4f} (t={ic_t:+.2f})")

    return out


# ---------------------------------------------------------------------------
# 检查 2：前视偏差（用当期财报而非最新财报）
# ---------------------------------------------------------------------------
def check_lookahead(
    prep: dict,
    strategies: list[str],
    top_n: int = 30,
) -> dict:
    """前视检查：用「当时可得」的财务数据，而非最新数据。

    **这是最严重的替代解释。**

    当前做法：用 2026-09-30 的财报，选 2023-2026 的股票。
    -> 2023 年选股时，2026 年的财报根本不存在。

    正确做法：每个调仓日，只能用「公告日 <= 调仓日」的财报。

    但东财财务接口不提供公告日，只有报告期。
    所以用**报告期 + 法定披露截止日**近似：
        一季报 (03-31) -> 04-30 可得
        半年报 (06-30) -> 08-31 可得
        三季报 (09-30) -> 10-31 可得
        年报   (12-31) -> 次年 04-30 可得

    这个检查需要重新拉历史财务数据，成本较高。
    简化版：只检查「最新财务数据的报告期」与「回测起始日」的差距。
    """
    print("\n" + "=" * 88)
    print("检查 2：前视偏差（用当期财报而非最新财报）")
    print("=" * 88)

    fin = prep["fin"]
    month_ends = prep["month_ends"]

    if len(month_ends) == 0:
        return {"error": "无调仓日"}

    backtest_start = month_ends[0]
    print(f"  回测起始日：{backtest_start}")

    # 检查财务数据的报告期
    report_dates = []
    for col in ["report_date", "report_date_ths"]:
        if col in fin.columns:
            vals = fin[col].dropna().astype(str)
            report_dates.extend(vals.tolist())

    if not report_dates:
        print("  ⚠️  财务数据无报告期字段，无法检查")
        return {"error": "无报告期字段"}

    from collections import Counter

    date_counts = Counter(d[:10] for d in report_dates)
    most_common = date_counts.most_common(3)

    print(f"  财务报告期分布（前 3）：")
    for d, cnt in most_common:
        print(f"    {d}: {cnt} 只")

    latest_report = most_common[0][0]
    print(f"\n  最新报告期：{latest_report}")
    print(f"  回测起始日：{backtest_start}")

    # 判断前视程度
    try:
        report_dt = pd.to_datetime(latest_report)
        start_dt = pd.to_datetime(backtest_start)
        gap_days = (report_dt - start_dt).days
        gap_years = gap_days / 365
    except Exception:  # noqa: BLE001
        gap_years = 0

    print(f"  时间差：{gap_days} 天（约 {gap_years:.1f} 年）")

    if gap_years > 2:
        verdict = "🔴 严重前视"
        note = (f"用 {latest_report} 的财报选 {backtest_start} 的股票，"
                f"前视 {gap_years:.1f} 年。结果不可信。")
    elif gap_years > 0.5:
        verdict = "⚠️  中度前视"
        note = f"前视 {gap_years:.1f} 年，结果需打折。"
    else:
        verdict = "✅ 前视轻微"
        note = "财务数据与回测期接近，前视影响小。"

    print(f"\n  {verdict}")
    print(f"  {note}")

    return {
        "latest_report": latest_report,
        "backtest_start": backtest_start,
        "gap_days": int(gap_days),
        "gap_years": round(float(gap_years), 2),
        "verdict": verdict,
        "note": note,
    }


# ---------------------------------------------------------------------------
# 检查 3：样本外（2018-2023）
# ---------------------------------------------------------------------------
def check_oos(
    candidates: pd.DataFrame,
    strategies: list[str],
    top_n: int = 30,
) -> dict:
    """样本外验证：用 2018-2023 的数据。

    问题：财务数据只有当前的（2026-09），拿不到历史财务。
    所以样本外只能验证「价格行为」，不能验证「财务因子」。

    替代方案：用当前财务因子选股，但回测 2018-2023 的收益。
    这个仍有前视问题，但至少能看「这批高质量股票在 2018-2023 表现如何」。
    """
    print("\n" + "=" * 88)
    print("检查 3：样本外验证（2018-2023）")
    print("=" * 88)
    print()
    print("  ⚠️  限制说明：")
    print("     东财财务接口只给当前财报（2026-09），无法获取 2018 年的历史财务。")
    print("     所以这里做的是「用当前财务因子选股，回测 2018-2023 收益」。")
    print("     这仍有前视问题，但能回答：这批高质量股票在过去表现如何？")
    print()

    import bisect
    from datetime import timedelta

    from screening import cache, fetch

    prep = prepare_data(candidates, top_n=top_n)

    # 重新拉 2018-2023 日线
    print("  拉 2018-2023 日线…")
    merged = prep["merged"]
    by_code_long = {}

    for code in merged.index[:min(len(merged), 800)]:
        row = merged.loc[code]
        ex = row.get("exchange", "")
        if not ex:
            if code.startswith("6"):
                ex = "上海证券交易所"
            elif code.startswith(("0", "3")):
                ex = "深圳证券交易所"
        try:
            frame = cache.fetch_with_cache(
                code, ex, date(2018, 1, 1), date(2023, 10, 30),
                fetch.fetch_daily, adjust="qfq")
            if frame is not None and len(frame) > 0:
                g = frame.sort_values("date")
                by_code_long[code] = {
                    "dates": [str(d)[:10] for d in g["date"]],
                    "close": g["close"].astype(float).values,
                }
        except Exception:  # noqa: BLE001
            continue

    print(f"  日线：{len(by_code_long)} 只")

    if len(by_code_long) < top_n * 2:
        return {"error": f"样本外日线太少（{len(by_code_long)}）"}

    all_dates = sorted({d for e in by_code_long.values() for d in e["dates"]})
    month_ends = [
        all_dates[i] for i in range(len(all_dates) - 1)
        if all_dates[i][:7] != all_dates[i + 1][:7]
    ]
    month_ends = [d for d in month_ends if d <= "2023-10-30"]

    print(f"  调仓日：{len(month_ends)} 个")

    def get_ret(code: str, d0: str, d1: str) -> float | None:
        e = by_code_long.get(code)
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

    out = {}
    rng = np.random.default_rng(42)

    for strategy_name in strategies:
        weights, desc = STRATEGIES[strategy_name]
        scored = neutralize.neutralize_composite(
            merged.reset_index(), weights, use_rank=True).set_index("code")
        scored = scored[scored["composite_neutral"].notna()]
        scored = scored[scored.index.isin(by_code_long.keys())]

        if len(scored) < top_n * 2:
            continue

        ranked = scored.sort_values("composite_neutral", ascending=False)
        ranked_codes = ranked.index.tolist()

        port_rets = []
        rand_rets_list = []

        for idx, reb in enumerate(month_ends[:-1]):
            nxt = month_ends[idx + 1]
            tradable = [c for c in ranked_codes if get_ret(c, reb, reb) is not None]
            if len(tradable) < top_n * 2:
                continue

            picks = tradable[:top_n]
            rets = [r for c in picks if (r := get_ret(c, reb, nxt)) is not None]
            if not rets:
                continue
            port_rets.append(float(np.mean(rets)))

            rand_sample = []
            for _ in range(50):
                sample = rng.choice(len(tradable), size=top_n, replace=False)
                r_rets = [r for i in sample
                          if (r := get_ret(tradable[i], reb, nxt)) is not None]
                if r_rets:
                    rand_sample.append(float(np.mean(r_rets)))
            if rand_sample:
                rand_rets_list.append(float(np.mean(rand_sample)))

        if not port_rets:
            continue

        port = np.array(port_rets)
        rand = np.array(rand_rets_list) if rand_rets_list else np.array([])

        cum_port = float(np.prod(1 + port) - 1)
        cum_rand = float(np.prod(1 + rand) - 1) if len(rand) else None
        excess = (float(np.prod(1 + (port - rand)) - 1)
                  if len(rand) == len(port) and len(port) else None)

        out[strategy_name] = {
            "periods": len(port),
            "cum_portfolio": round(cum_port, 4),
            "cum_random": round(cum_rand, 4) if cum_rand is not None else None,
            "excess_vs_random": round(excess, 4) if excess is not None else None,
        }

        mark = "✅" if excess and excess > 0 else "❌"
        print(f"  {mark} {strategy_name:16s} 期数 {len(port):3d} | "
              f"累计 {cum_port:+.2%} | vs随机 {excess:+.2%}"
              if excess is not None else
              f"  {mark} {strategy_name:16s} 期数 {len(port):3d} | 累计 {cum_port:+.2%}")

    return out


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------
def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="财务因子验证")
    parser.add_argument("--check", default="all",
                        choices=["cap", "pit", "oos", "all"])
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--strategies", default="quality_growth,growth,quality")
    args = parser.parse_args(argv[1:])

    logging.basicConfig(level=logging.WARNING)

    try:
        candidates, run_date = load_latest_candidates()
    except FileNotFoundError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1

    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    strategies = [s for s in strategies if s in STRATEGIES]

    print("=" * 88)
    print("财务因子验证：三项替代解释排查")
    print("=" * 88)
    print(f"候选池：{len(candidates)} 只（{run_date}）")
    print(f"策略：{', '.join(strategies)}")

    # 准备数据（一次）
    prep = prepare_data(candidates, top_n=args.top)

    results = {}

    if args.check in ("pit", "all"):
        results["lookahead"] = check_lookahead(prep, strategies, args.top)

    if args.check in ("cap", "all"):
        results["market_cap_neutral"] = check_market_cap_neutral(
            prep, strategies, args.top)

    if args.check in ("oos", "all"):
        results["out_of_sample"] = check_oos(candidates, strategies, args.top)

    # 汇总
    print()
    print("=" * 88)
    print("汇总")
    print("=" * 88)
    print()

    pit = results.get("lookahead", {})
    cap = results.get("market_cap_neutral", {})
    oos = results.get("out_of_sample", {})

    if pit and "verdict" in pit:
        print(f"  前视检查：{pit['verdict']}")
        print(f"    {pit['note']}")
    print()

    if cap:
        print("  市值中性化后：")
        for name, s in cap.items():
            if isinstance(s, dict) and "ic_t_stat" in s:
                mark = "✅" if s["ic_significant"] else "❌"
                print(f"    {mark} {name:16s} t={s['ic_t_stat']:+.2f} "
                      f"vs随机 {s['excess_vs_random']:+.2%}")
    print()

    if oos:
        print("  样本外（2018-2023）：")
        for name, s in oos.items():
            if isinstance(s, dict) and "excess_vs_random" in s:
                e = s["excess_vs_random"]
                mark = "✅" if e and e > 0 else "❌"
                print(f"    {mark} {name:16s} vs随机 {e:+.2%}" if e is not None
                      else f"    {mark} {name:16s} N/A")

    # 落盘
    out_dir = _REPO_ROOT / "qlib_layer" / "output" / "validate_financial"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "validation.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    print()
    print(f"输出：{out_dir}")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
