"""PIT 条件下的补充验证：市值中性化 + 样本外。

背景
----
PIT 回测结果（2026-10-31）：
    quality 策略：75 期，vs随机 -32.96%，IC -0.0123，t=-1.35

非 PIT（前视 2.8 年）：34 期，vs随机 +48.93%，IC +0.0316，t=+2.26

结论：前视偏差是之前「显著」的主因。

本模块补最后两项验证
--------------------
1. **市值中性化**：在市值分位内选股，排除小盘效应
   - 如果 PIT 后仍是负的，市值中性化后大概率还是负的
   - 但要测，不能猜

2. **样本外**：PIT 数据覆盖 2020-2026，可以切分
   - 样本内：2020-06 ~ 2023-10
   - 样本外：2023-11 ~ 2026-09

用法
----
    python -m qlib_layer.validate_pit --check cap
    python -m qlib_layer.validate_pit --check oos
    python -m qlib_layer.validate_pit --check all
"""

from __future__ import annotations

import argparse
import bisect
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

from qlib_layer import neutralize, pit_financials  # noqa: E402
from qlib_layer.backtest_pit import (  # noqa: E402
    FEE_RATE,
    PIT_STRATEGIES,
    SLIPPAGE,
    STAMP_TAX,
    load_candidates,
)

logger = logging.getLogger(__name__)


def _prepare_pit_common(
    candidates: pd.DataFrame,
    max_stocks: int = 500,
    use_cache: bool = True,
) -> dict:
    """准备 PIT 数据 + 日线（两个检查共用）。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from screening import cache, fetch

    if len(candidates) > max_stocks:
        sort_col = ("avg_amount_yi" if "avg_amount_yi" in candidates.columns
                    else "market_cap_yi")
        if sort_col in candidates.columns:
            candidates = candidates.nlargest(max_stocks, sort_col)

    codes = candidates["code"].tolist()
    print(f"  股票池：{len(codes)} 只")

    print(f"  拉 PIT 财务（{len(codes)} 只）…")
    pit_data = pit_financials.build_pit_dataset(
        codes, start_year="2020", workers=10,
        progress=lambda i, t, c: print(f"    {i}/{t}") if i % 200 == 0 else None,
    )
    print(f"  PIT 数据：{len(pit_data)} 只")

    # 预建索引
    pit_index = {}
    for code, table in pit_data.items():
        if table is None or len(table) == 0:
            continue
        t_sorted = table.sort_values("available_date")
        pit_index[code] = (
            [d for d in t_sorted["available_date"]],
            t_sorted.to_dict("records"),
        )

    # 日线（并发）
    print("  拉日线（并发 10）…")
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
                code, ex, date(2019, 1, 1), date.today(),
                fetch.fetch_daily, adjust="qfq")
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
        futures = {executor.submit(_fetch_one, c): c for c in pit_index}
        for future in as_completed(futures):
            code, data = future.result()
            done += 1
            if data is not None:
                by_code[code] = data
            if done % 200 == 0:
                print(f"    {done}/{len(pit_index)}")

    print(f"  日线：{len(by_code)} 只")

    # 行业 + 市值映射
    industry_map = {}
    cap_map = {}
    if "industry_final" in candidates.columns:
        industry_map = dict(zip(
            candidates["code"], candidates["industry_final"], strict=False))
    if "market_cap_yi" in candidates.columns:
        cap_map = dict(zip(
            candidates["code"], candidates["market_cap_yi"], strict=False))

    return {
        "candidates": candidates,
        "pit_index": pit_index,
        "by_code": by_code,
        "industry_map": industry_map,
        "cap_map": cap_map,
    }


def _month_ends(all_dates: list[str], start: str | None = None,
                end: str | None = None) -> list[str]:
    ends = [
        all_dates[i] for i in range(len(all_dates) - 1)
        if all_dates[i][:7] != all_dates[i + 1][:7]
    ]
    if start:
        ends = [d for d in ends if d >= start]
    if end:
        ends = [d for d in ends if d <= end]
    return ends


def _make_helpers(by_code: dict):
    """构造 get_ret / is_tradable 辅助函数。"""

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

    return get_ret, is_tradable


def _snapshot_at(pit_index: dict, as_of: date) -> dict[str, dict]:
    """取某时点所有股票已公布的财报。"""
    out = {}
    for code, (avail_list, rows) in pit_index.items():
        pos = bisect.bisect_right(avail_list, as_of)
        if pos == 0:
            continue
        out[code] = rows[pos - 1]
    return out


def _score(
    snapshots: dict[str, dict],
    weights: dict[str, float],
    industry_map: dict,
    cap_map: dict | None = None,
    cap_buckets: int | None = None,
) -> pd.DataFrame:
    """给快照打分（行业中性化，可选市值分档）。"""
    df = pd.DataFrame.from_dict(snapshots, orient="index")
    df["code"] = df.index
    df["industry_final"] = df["code"].map(industry_map).fillna("未知")

    if cap_map:
        df["market_cap_yi"] = df["code"].map(cap_map)

    scored = neutralize.neutralize_composite(
        df.reset_index(drop=True), weights, use_rank=True)

    if cap_buckets and "market_cap_yi" in scored.columns:
        valid = scored["market_cap_yi"].notna() & (scored["market_cap_yi"] > 0)
        scored = scored[valid].copy()
        if len(scored) >= cap_buckets * 5:
            scored["cap_bucket"] = pd.qcut(
                scored["market_cap_yi"], cap_buckets,
                labels=[f"Q{i+1}" for i in range(cap_buckets)],
                duplicates="drop")

    return scored


def check_market_cap_neutral(
    prep: dict,
    strategies: list[str],
    top_n: int = 30,
    n_cap_buckets: int = 5,
    start: str = "2020-06-01",
) -> dict:
    """检查 1：市值中性化（PIT 条件下）。"""
    print("\n" + "=" * 88)
    print("检查 A：市值中性化（PIT 条件）")
    print("=" * 88)

    pit_index = prep["pit_index"]
    by_code = prep["by_code"]
    industry_map = prep["industry_map"]
    cap_map = prep["cap_map"]

    if not cap_map or sum(1 for v in cap_map.values() if v and not pd.isna(v)) < top_n * 5:
        print("  ❌ 市值数据不足，跳过")
        return {"error": "市值数据不足"}

    all_dates = sorted({d for e in by_code.values() for d in e["dates"]})
    month_ends = _month_ends(all_dates, start=start)
    if len(month_ends) < 6:
        return {"error": f"调仓日太少（{len(month_ends)}）"}

    print(f"  调仓日：{len(month_ends)} 个")
    get_ret, is_tradable = _make_helpers(by_code)
    rng = np.random.default_rng(42)
    per_bucket = max(1, top_n // n_cap_buckets)

    out = {}
    for name in strategies:
        weights, desc = PIT_STRATEGIES[name]

        port_rets = []
        rand_rets_list = []
        ic_list = []
        bucket_counts = []

        for idx, reb in enumerate(month_ends[:-1]):
            nxt = month_ends[idx + 1]
            snapshots = _snapshot_at(pit_index, date.fromisoformat(reb))
            if len(snapshots) < top_n * 3:
                continue

            scored = _score(snapshots, weights, industry_map,
                            cap_map, n_cap_buckets)
            if "composite_neutral" not in scored.columns or "cap_bucket" not in scored.columns:
                continue
            scored = scored[scored["composite_neutral"].notna()]
            if len(scored) < top_n * 2:
                continue

            # 每档内选 top
            picks = []
            for bucket, group in scored.groupby("cap_bucket", observed=True):
                tradable = group[group["code"].apply(lambda c: is_tradable(c, reb))]
                if len(tradable) == 0:
                    continue
                ranked = tradable.sort_values("composite_neutral", ascending=False)
                picks.extend(ranked["code"].tolist()[:per_bucket])

            if len(picks) < top_n // 2:
                continue

            bucket_counts.append(len(picks))

            rets = [r for c in picks if (r := get_ret(c, reb, nxt)) is not None]
            if not rets:
                continue
            port_rets.append(float(np.mean(rets)))

            # 随机对照（同档内随机）
            rand_picks = []
            for bucket, group in scored.groupby("cap_bucket", observed=True):
                tradable = group[group["code"].apply(lambda c: is_tradable(c, reb))]
                if len(tradable) == 0:
                    continue
                codes_b = tradable["code"].tolist()
                n_pick = min(per_bucket, len(codes_b))
                sample = rng.choice(len(codes_b), size=n_pick, replace=False)
                rand_picks.extend([codes_b[i] for i in sample])
            r_rets = [r for c in rand_picks if (r := get_ret(c, reb, nxt)) is not None]
            if r_rets:
                rand_rets_list.append(float(np.mean(r_rets)))

            # IC（全池）
            all_tradable = scored[scored["code"].apply(lambda c: is_tradable(c, reb))]
            fwd = {}
            for c in all_tradable["code"]:
                r = get_ret(c, reb, nxt)
                if r is not None:
                    fwd[c] = r
            common = [c for c in all_tradable["code"] if c in fwd]
            if len(common) >= 30:
                from scipy.stats import spearmanr

                score_map = dict(zip(
                    all_tradable["code"], all_tradable["composite_neutral"],
                    strict=False))
                s_vals = np.array([score_map[c] for c in common])
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
        excess = (float(np.prod(1 + (port - rand)) - 1)
                  if len(rand) == len(port) and len(port) else None)

        ic_mean = float(np.mean(ic_list)) if ic_list else 0.0
        ic_std = float(np.std(ic_list, ddof=1)) if len(ic_list) > 1 else 0.0
        ic_t = (ic_mean / ic_std * np.sqrt(len(ic_list))) if ic_std > 0 else 0.0

        out[name] = {
            "periods": len(port),
            "avg_holdings": round(float(np.mean(bucket_counts)), 1) if bucket_counts else 0,
            "cum_portfolio": round(cum_port, 4),
            "cum_random": round(cum_rand, 4) if cum_rand is not None else None,
            "excess_vs_random": round(excess, 4) if excess is not None else None,
            "ic_mean": round(ic_mean, 4),
            "ic_t_stat": round(float(ic_t), 2),
            "ic_significant": bool(abs(ic_t) > 1.96),
        }

        mark = "✅" if abs(ic_t) > 1.96 else "❌"
        print(f"  {mark} {name:14s} 期数 {len(port):3d} | "
              f"累计 {cum_port:+.2%} | vs随机 {excess:+.2%} | "
              f"IC {ic_mean:+.4f} (t={ic_t:+.2f})")

    return out


def check_out_of_sample(
    prep: dict,
    strategies: list[str],
    top_n: int = 30,
    split_date: str = "2023-10-31",
) -> dict:
    """检查 2：样本内 vs 样本外（PIT 条件下）。"""
    print("\n" + "=" * 88)
    print("检查 B：样本内 vs 样本外（PIT 条件）")
    print("=" * 88)
    print(f"  切分点：{split_date}")
    print()

    pit_index = prep["pit_index"]
    by_code = prep["by_code"]
    industry_map = prep["industry_map"]

    all_dates = sorted({d for e in by_code.values() for d in e["dates"]})
    all_ends = _month_ends(all_dates, start="2020-06-01")

    is_ends = [d for d in all_ends if d < split_date]
    oos_ends = [d for d in all_ends if d >= split_date]

    print(f"  样本内：{len(is_ends)} 个调仓日"
          f"（{is_ends[0] if is_ends else 'N/A'} ~ {is_ends[-1] if is_ends else 'N/A'}）")
    print(f"  样本外：{len(oos_ends)} 个调仓日"
          f"（{oos_ends[0] if oos_ends else 'N/A'} ~ {oos_ends[-1] if oos_ends else 'N/A'}）")
    print()

    get_ret, is_tradable = _make_helpers(by_code)
    rng = np.random.default_rng(42)

    def run_segment(name: str, weights: dict, month_ends: list[str]) -> dict | None:
        if len(month_ends) < 4:
            return None

        port_rets = []
        rand_rets_list = []
        ic_list = []

        for idx, reb in enumerate(month_ends[:-1]):
            nxt = month_ends[idx + 1]
            snapshots = _snapshot_at(pit_index, date.fromisoformat(reb))
            if len(snapshots) < top_n * 3:
                continue

            scored = _score(snapshots, weights, industry_map)
            if "composite_neutral" not in scored.columns:
                continue
            scored = scored[scored["composite_neutral"].notna()]
            if len(scored) < top_n * 2:
                continue

            tradable = scored[scored["code"].apply(lambda c: is_tradable(c, reb))]
            if len(tradable) < top_n:
                continue

            ranked = tradable.sort_values("composite_neutral", ascending=False)
            picks = ranked["code"].tolist()[:top_n]

            rets = [r for c in picks if (r := get_ret(c, reb, nxt)) is not None]
            if not rets:
                continue
            port_rets.append(float(np.mean(rets)))

            codes_t = tradable["code"].tolist()
            rand_sample = []
            for _ in range(50):
                sample = rng.choice(len(codes_t), size=top_n, replace=False)
                s_rets = [r for i in sample
                          if (r := get_ret(codes_t[i], reb, nxt)) is not None]
                if s_rets:
                    rand_sample.append(float(np.mean(s_rets)))
            if rand_sample:
                rand_rets_list.append(float(np.mean(rand_sample)))

            fwd = {}
            for c in codes_t:
                r = get_ret(c, reb, nxt)
                if r is not None:
                    fwd[c] = r
            common = [c for c in codes_t if c in fwd]
            if len(common) >= 30:
                from scipy.stats import spearmanr

                score_map = dict(zip(
                    ranked["code"], ranked["composite_neutral"], strict=False))
                # 用全部可交易股票算 IC
                full_map = dict(zip(
                    tradable["code"], tradable["composite_neutral"], strict=False))
                s_vals = np.array([full_map[c] for c in common])
                r_vals = np.array([fwd[c] for c in common])
                ic, _ = spearmanr(s_vals, r_vals)
                if not np.isnan(ic):
                    ic_list.append(float(ic))

        if not port_rets:
            return None

        port = np.array(port_rets)
        rand = np.array(rand_rets_list) if rand_rets_list else np.array([])

        cum_port = float(np.prod(1 + port) - 1)
        cum_rand = float(np.prod(1 + rand) - 1) if len(rand) else None
        excess = (float(np.prod(1 + (port - rand)) - 1)
                  if len(rand) == len(port) and len(port) else None)

        ic_mean = float(np.mean(ic_list)) if ic_list else 0.0
        ic_std = float(np.std(ic_list, ddof=1)) if len(ic_list) > 1 else 0.0
        ic_t = (ic_mean / ic_std * np.sqrt(len(ic_list))) if ic_std > 0 else 0.0

        return {
            "periods": len(port),
            "cum_portfolio": round(cum_port, 4),
            "cum_random": round(cum_rand, 4) if cum_rand is not None else None,
            "excess_vs_random": round(excess, 4) if excess is not None else None,
            "ic_mean": round(ic_mean, 4),
            "ic_t_stat": round(float(ic_t), 2),
            "ic_significant": bool(abs(ic_t) > 1.96),
        }

    out = {}
    for name in strategies:
        weights, _ = PIT_STRATEGIES[name]
        is_r = run_segment(name, weights, is_ends)
        oos_r = run_segment(name, weights, oos_ends)

        out[name] = {"in_sample": is_r, "out_of_sample": oos_r}

        if is_r and oos_r:
            print(f"  {name}:")
            print(f"    样本内 {is_r['periods']:3d} 期 | "
                  f"vs随机 {is_r['excess_vs_random']:+.2%} | "
                  f"IC {is_r['ic_mean']:+.4f} (t={is_r['ic_t_stat']:+.2f})")
            print(f"    样本外 {oos_r['periods']:3d} 期 | "
                  f"vs随机 {oos_r['excess_vs_random']:+.2%} | "
                  f"IC {oos_r['ic_mean']:+.4f} (t={oos_r['ic_t_stat']:+.2f})")

            both_neg = (is_r["ic_mean"] < 0 and oos_r["ic_mean"] < 0)
            consistent = (is_r["ic_mean"] > 0) == (oos_r["ic_mean"] > 0)
            if both_neg:
                print("    → 🔴 两段都为负，因子方向可能反了")
            elif consistent:
                print("    → ✅ 方向一致")
            else:
                print("    → ⚠️  方向反转，信号不稳定")
            print()

    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="PIT 补充验证")
    parser.add_argument("--check", default="all", choices=["cap", "oos", "all"])
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--max-stocks", type=int, default=500)
    parser.add_argument("--strategies", default="quality,quality_only,margin")
    args = parser.parse_args(argv[1:])

    logging.basicConfig(level=logging.WARNING)

    try:
        candidates, run_date = load_candidates()
    except FileNotFoundError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1

    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    strategies = [s for s in strategies if s in PIT_STRATEGIES]

    print("=" * 88)
    print("PIT 补充验证：市值中性化 + 样本外")
    print("=" * 88)
    print(f"候选池：{len(candidates)} 只（{run_date}）")
    print(f"策略：{', '.join(strategies)}")

    prep = _prepare_pit_common(candidates, max_stocks=args.max_stocks)

    results = {}

    if args.check in ("cap", "all"):
        results["market_cap_neutral"] = check_market_cap_neutral(
            prep, strategies, args.top)

    if args.check in ("oos", "all"):
        results["out_of_sample"] = check_out_of_sample(
            prep, strategies, args.top)

    # 汇总
    print()
    print("=" * 88)
    print("汇总")
    print("=" * 88)
    print()

    cap = results.get("market_cap_neutral", {})
    if cap and "error" not in cap:
        print("  市值中性化（PIT）：")
        for name, s in cap.items():
            if isinstance(s, dict) and "ic_t_stat" in s:
                mark = "✅" if s["ic_significant"] else "❌"
                print(f"    {mark} {name:14s} t={s['ic_t_stat']:+.2f} "
                      f"vs随机 {s['excess_vs_random']:+.2%}")
        print()

    oos = results.get("out_of_sample", {})
    if oos:
        print("  样本内 vs 样本外（PIT）：")
        for name, s in oos.items():
            if isinstance(s, dict) and s.get("in_sample") and s.get("out_of_sample"):
                i = s["in_sample"]
                o = s["out_of_sample"]
                print(f"    {name:14s} "
                      f"内 IC {i['ic_mean']:+.4f}(t={i['ic_t_stat']:+.2f}) / "
                      f"外 IC {o['ic_mean']:+.4f}(t={o['ic_t_stat']:+.2f})")

    # 落盘
    out_dir = _REPO_ROOT / "qlib_layer" / "output" / "validate_pit"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "validation.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    print()
    print(f"输出：{out_dir}")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
