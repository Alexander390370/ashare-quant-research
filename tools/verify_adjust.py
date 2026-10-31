"""复权核验：量化「未复权到底改变了什么」。

六家评审一致要求的第一步（① 修复权的验收工具）。

做什么
------
1. 对同一批股票，分别用**不复权**和**前复权**拉日线
2. 用同一份因子公式各算一遍 mom20 / mom5 / vol20
3. 输出对比表：哪些票因子值差异超过阈值（这就是除权污染证据）
4. 输出「前复权版 Top20」与「不复权版 Top20」的差异

用法
----
    python tools/verify_adjust.py                     # 用 A 线候选前 50 只
    python tools/verify_adjust.py --limit 100         # 前 100 只
    python tools/verify_adjust.py --codes 600519,000001,002594
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tradingagents  # noqa: E402,F401  触发 netfix
from screening import fetch  # noqa: E402

# 因子值差异超过这个阈值就视为「被污染」
POLLUTION_THRESHOLD = 0.05  # 5 个百分点


def compute_factors(daily) -> dict:
    """用同一份公式算因子。返回 None 表示数据不足。"""
    if daily is None or len(daily) < 25:
        return None

    closes = daily["close"].astype(float).values
    volumes = daily["volume"].astype(float).values if "volume" in daily.columns else None

    last = closes[-1]
    mom5 = last / closes[-6] - 1 if len(closes) >= 6 else None
    mom20 = last / closes[-21] - 1 if len(closes) >= 21 else None

    import numpy as np

    window = closes[-20:]
    mean20 = float(np.mean(window))
    std20 = float(np.std(window))
    vol20 = std20 / mean20 if mean20 else None
    dev20 = (last - mean20) / mean20 if mean20 else None

    vol_ratio = None
    if volumes is not None and len(volumes) >= 20:
        v5 = float(np.mean(volumes[-5:]))
        v20 = float(np.mean(volumes[-20:]))
        vol_ratio = v5 / v20 if v20 else None

    return {
        "mom5": mom5,
        "mom20": mom20,
        "vol20": vol20,
        "dev20": dev20,
        "vol_ratio": vol_ratio,
    }


def compare_one(code: str, exchange: str, start: date, end: date) -> dict | None:
    """对单只股票做复权前后对比。"""
    try:
        raw = fetch.fetch_daily(code, exchange, start, end, source="tencent", adjust="")
        adj = fetch.fetch_daily(code, exchange, start, end, source="tencent", adjust="qfq")
    except Exception as exc:  # noqa: BLE001
        return {"code": code, "error": f"{type(exc).__name__}: {str(exc)[:50]}"}

    f_raw = compute_factors(raw)
    f_adj = compute_factors(adj)
    if f_raw is None or f_adj is None:
        return {"code": code, "error": "数据不足"}

    diffs = {}
    polluted = False
    for key in ("mom5", "mom20", "vol20", "dev20", "vol_ratio"):
        a, b = f_raw.get(key), f_adj.get(key)
        if a is None or b is None:
            diffs[key] = None
            continue
        d = abs(a - b)
        diffs[key] = round(d, 6)
        if d > POLLUTION_THRESHOLD:
            polluted = True

    # 首价对比：如果复权因子不为 1，说明有除权
    raw_first = float(raw.iloc[0]["close"])
    adj_first = float(adj.iloc[0]["close"])
    ratio = adj_first / raw_first if raw_first else None

    return {
        "code": code,
        "raw_first": round(raw_first, 2),
        "adj_first": round(adj_first, 2),
        "adjust_ratio": round(ratio, 4) if ratio else None,
        "raw_mom20": round(f_raw["mom20"], 4) if f_raw.get("mom20") is not None else None,
        "adj_mom20": round(f_adj["mom20"], 4) if f_adj.get("mom20") is not None else None,
        "mom20_diff": diffs.get("mom20"),
        "max_diff": max((v for v in diffs.values() if v is not None), default=0),
        "polluted": polluted,
        "diffs": diffs,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="复权核验")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--codes", default=None, help="逗号分隔的股票代码")
    parser.add_argument("--workers", type=int, default=10)
    args = parser.parse_args(argv[1:])

    print("=" * 80)
    print("复权核验：量化未复权对因子的污染")
    print("=" * 80)

    # ---------- 确定股票列表 ----------
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        stocks = []
        for c in codes:
            if c.startswith("6"):
                stocks.append({"code": c, "exchange": "上海证券交易所"})
            elif c.startswith(("0", "3")):
                stocks.append({"code": c, "exchange": "深圳证券交易所"})
            else:
                stocks.append({"code": c, "exchange": ""})
    else:
        cand_path = _REPO_ROOT / "screening" / "output" / args.date / "candidates.csv"
        if not cand_path.exists():
            print(f"找不到 A 线候选: {cand_path}", file=sys.stderr)
            return 1
        with cand_path.open("r", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        stocks = [
            {"code": r["code"], "name": r.get("name", ""), "exchange": ""}
            for r in rows[: args.limit]
        ]
        # 补交易所
        for s in stocks:
            if s["code"].startswith("6"):
                s["exchange"] = "上海证券交易所"
            elif s["code"].startswith(("0", "3")):
                s["exchange"] = "深圳证券交易所"

    print(f"股票数: {len(stocks)}")
    start, end = fetch.default_window(3)
    print(f"数据窗口: {start} ~ {end}")
    print()

    # ---------- 并发对比 ----------
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results = []
    started = time.time()

    def task(stock):
        return compare_one(stock["code"], stock["exchange"], start, end)

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(task, s) for s in stocks]
        for idx, future in enumerate(as_completed(futures), 1):
            r = future.result()
            if r:
                results.append(r)
            if idx % 10 == 0 or idx == len(stocks):
                print(f"  {idx}/{len(stocks)}")

    elapsed = time.time() - started
    print(f"\n完成，耗时 {elapsed:.1f}s")

    # ---------- 汇总 ----------
    ok = [r for r in results if "error" not in r]
    errors = [r for r in results if "error" in r]
    polluted = [r for r in ok if r["polluted"]]

    print()
    print("=" * 80)
    print("结果")
    print("=" * 80)
    print(f"  成功: {len(ok)}  失败: {len(errors)}")
    print(f"  被污染（因子差异 > {POLLUTION_THRESHOLD:.0%}）: {len(polluted)} 只"
          f"  ({len(polluted)/len(ok)*100:.0f}%)" if ok else "  无数据")

    if polluted:
        print()
        print("  === 污染票清单（按 max_diff 排序）===")
        print(f"  {'代码':<8} {'复权比':>8} {'未复权mom20':>12} {'前复权mom20':>12} {'差异':>8}")
        print("  " + "-" * 56)
        for r in sorted(polluted, key=lambda x: x["max_diff"], reverse=True)[:20]:
            print(
                f"  {r['code']:<8} {r['adjust_ratio']:>8.4f} "
                f"{r['raw_mom20']:>11.2%} {r['adj_mom20']:>11.2%} "
                f"{r['max_diff']:>7.2%}"
            )

    # ---------- 输出 ----------
    out_dir = _REPO_ROOT / "screening" / "output" / args.date
    out_dir.mkdir(parents=True, exist_ok=True)

    with (out_dir / "adjust_verification.csv").open(
            "w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = [
            "code", "adjust_ratio", "raw_first", "adj_first",
            "raw_mom20", "adj_mom20", "mom20_diff", "max_diff", "polluted",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sorted(ok, key=lambda x: x["max_diff"], reverse=True))

    summary = {
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "date": args.date,
        "stocks_checked": len(ok),
        "errors": len(errors),
        "polluted_count": len(polluted),
        "pollution_rate": round(len(polluted) / len(ok), 4) if ok else 0,
        "threshold": POLLUTION_THRESHOLD,
        "elapsed_seconds": round(elapsed, 1),
    }
    (out_dir / "adjust_verification.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print(f"  输出: {out_dir / 'adjust_verification.csv'}")
    print(f"        {out_dir / 'adjust_verification.json'}")

    # ---------- 结论 ----------
    print()
    print("=" * 80)
    if ok and len(polluted) / len(ok) > 0.2:
        print(f"  结论：污染率 {len(polluted)/len(ok)*100:.0f}%，**必须修复权**。")
        print("        不复权数据会系统性扭曲动量因子，Top20 不可信。")
    elif ok:
        print(f"  结论：污染率 {len(polluted)/len(ok)*100:.0f}%，影响可控但仍建议用前复权。")
    print("=" * 80)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
