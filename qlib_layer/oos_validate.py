"""样本外验证：用 2018-2023 数据检验反转信号是否跨周期存在。

为什么必须做
------------
六家评审一致结论：+137.37% 是**样本内自我指认**的结果。
「看到 IC 为负才反向」= 在同一份数据上完成假设生成、参数选择与绩效评估。

唯一能解决的方法：**用未参与调参的数据做样本外验证**。

设计（严格遵循六家要求）
------------------------
1. **冻结参数**：权重、TopN、调仓频率全部锁定，不许回头调
2. **样本内 / 样本外切分**：
   - 样本外（OOS）：2018-01 ~ 2023-10（5.75 年，未参与任何调参）
   - 样本内（IS）： 2023-10 ~ 2026-10（2.83 年，之前调参用的）
3. **判据提前写死**：
   - OOS 超额 > 0 且 IC 均值方向一致 → 信号跨周期存在
   - OOS 超额 <= 0 或 IC 反向 → 信号是样本内拟合，**这条线就地埋掉**

用法
----
    python -m qlib_layer.oos_validate              # 完整流程
    python -m qlib_layer.oos_validate --dump-only  # 只灌数据
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tradingagents  # noqa: E402,F401

logger = logging.getLogger(__name__)

# 独立的 Qlib 数据目录（不复用 2023 起的那份，避免污染）
OOS_PROVIDER = _REPO_ROOT / "qlib_layer" / "data" / "cn_long"
IS_PROVIDER = _REPO_ROOT / "qlib_layer" / "data" / "cn_a"

# 样本切分
OOS_START = "2018-01-01"
OOS_END = "2023-10-30"
IS_START = "2023-10-31"
IS_END = "2026-10-30"


def dump_long_history(
    start: str = OOS_START,
    end: str = IS_END,
    workers: int = 10,
) -> dict:
    """拉 2018 年起的全量数据，灌进新目录。"""
    from datetime import date as _date

    from qlib_layer import dump_data
    from screening import fetch

    # 用当前的成分股名单（这是幸存者偏差来源，六家已指出，此处接受并记录）
    cand_path = _REPO_ROOT / "screening" / "output" / "2026-10-30" / "candidates.csv"
    if not cand_path.exists():
        raise FileNotFoundError(f"找不到候选名单: {cand_path}")

    import csv

    with cand_path.open("r", encoding="utf-8-sig") as h:
        rows = list(csv.DictReader(h))

    stocks = []
    for r in rows:
        code = r["code"]
        if code.startswith("6"):
            ex = "上海证券交易所"
        elif code.startswith(("0", "3")):
            ex = "深圳证券交易所"
        elif code.startswith(("4", "8")):
            ex = "北京证券交易所"
        else:
            continue
        stocks.append({"code": code, "name": r.get("name", ""), "exchange": ex})

    print(f"  股票数: {len(stocks)}")
    print(f"  区间: {start} ~ {end}")

    start_d = _date.fromisoformat(start)
    end_d = _date.fromisoformat(end)

    def progress(idx, total, code):
        if idx % 100 == 0 or idx == total:
            print(f"    {idx}/{total}")

    daily_map = fetch.fetch_all_daily(
        stocks, start_d, end_d,
        retries=3, backoff=1.5, interval=0.05,
        source="tencent", progress=progress, workers=workers,
        adjust="qfq",
    )
    print(f"  成功 {len(daily_map)} / {len(stocks)}")

    meta = {s["code"]: s for s in stocks}
    stats = dump_data.dump_to_qlib(daily_map, meta, OOS_PROVIDER, verbose=False)
    print(f"  灌入: {stats['stocks_dumped']} 只, {stats['calendar_days']} 天")
    print(f"  日历: {stats['calendar_start']} ~ {stats['calendar_end']}")
    return stats


def run_segment(
    provider_uri: Path,
    start: str,
    end: str,
    top_n: int = 20,
    direction: str = "inverted",
) -> dict:
    """在指定区间跑回测，返回统计。"""
    from qlib_layer.backtest_v2 import _fill_index_benchmark, run_v2

    result = run_v2(
        provider_uri=provider_uri,
        top_n=top_n,
        direction=direction,
        include_cost=True,
        apply_filters=True,
        n_bootstrap=200,
        start_time=start,
        end_time=end,
    )
    _fill_index_benchmark(result)
    # 重算 stats（因为指数是后填的）
    from qlib_layer.backtest_v2 import _stats

    result.stats = _stats(result, True, top_n, direction)
    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="样本外验证")
    parser.add_argument("--dump-only", action="store_true")
    parser.add_argument("--skip-dump", action="store_true")
    args = parser.parse_args(argv[1:])

    logging.basicConfig(level=logging.WARNING)

    print("=" * 78)
    print("样本外验证：反转信号是否跨周期存在？")
    print("=" * 78)
    print()
    print("  判据（提前写死，不许改）：")
    print("    OOS 超额 > 0 且 IC 方向一致 -> 信号跨周期存在")
    print("    OOS 超额 <= 0 或 IC 反向    -> 样本内拟合，这条线埋掉")
    print()

    # ---------- 1. 灌数据 ----------
    if not args.skip_dump:
        print("[1/3] 拉 2018 年起的全量数据（约 25 分钟）...")
        stats = dump_long_history()
        print()
    else:
        print("[1/3] 跳过灌数据")

    if args.dump_only:
        return 0

    if not (OOS_PROVIDER / "calendars" / "day.txt").exists():
        print(f"  ❌ 数据不存在: {OOS_PROVIDER}", file=sys.stderr)
        return 1

    # ---------- 2. 样本外 ----------
    print(f"[2/3] 样本外（OOS）: {OOS_START} ~ {OOS_END}")
    try:
        oos = run_segment(OOS_PROVIDER, OOS_START, OOS_END)
        oos_stats = oos.stats
    except Exception as exc:  # noqa: BLE001
        print(f"  ❌ 失败: {exc}")
        return 1

    # ---------- 3. 样本内（对照）----------
    print(f"\n[3/3] 样本内（IS，对照）: {IS_START} ~ {IS_END}")
    try:
        is_r = run_segment(IS_PROVIDER, IS_START, IS_END)
        is_stats = is_r.stats
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️  样本内失败: {exc}")
        is_stats = {}

    # ---------- 对比 ----------
    print()
    print("=" * 78)
    print("对比")
    print("=" * 78)
    print()
    print(f"  {'指标':<24} {'样本外(OOS)':>16} {'样本内(IS)':>16}")
    print("  " + "-" * 60)

    def fmt(v, pct=True):
        if v is None:
            return "N/A"
        return f"{v:+.2%}" if pct else f"{v:+.4f}"

    rows = [
        ("期数", "periods", False),
        ("组合累计", "cum_portfolio", True),
        ("官方指数", "cum_bench_index", True),
        ("随机20只", "cum_bench_random", True),
        ("vs 指数超额", "excess_vs_index", True),
        ("vs 随机超额", "excess_vs_random", True),
        ("最大回撤", "max_drawdown", True),
        ("IC 均值", "ic_mean", False),
        ("IC t 统计量", "ic_t_stat", False),
    ]
    for label, key, is_pct in rows:
        o = oos_stats.get(key)
        i = is_stats.get(key)
        if not is_pct:
            o_str = "N/A" if o is None else (
                f"{o:+.4f}" if key != "periods" else str(o))
            i_str = "N/A" if i is None else (
                f"{i:+.4f}" if key != "periods" else str(i))
        else:
            o_str = fmt(o)
            i_str = fmt(i)
        print(f"  {label:<24} {o_str:>16} {i_str:>16}")

    # ---------- 裁决 ----------
    print()
    print("=" * 78)
    print("裁决")
    print("=" * 78)
    print()

    oos_ex = oos_stats.get("excess_vs_random")
    oos_ic = oos_stats.get("ic_mean")
    oos_t = oos_stats.get("ic_t_stat")

    passed = False
    if oos_ex is not None and oos_ex > 0 and oos_ic is not None and oos_ic > 0:
        passed = True

    if passed:
        print(f"  ✅ **样本外通过**")
        print(f"     OOS 超额（vs 随机）: {oos_ex:+.2%}")
        print(f"     OOS IC 均值: {oos_ic:+.4f}（方向一致）")
        print(f"     OOS IC t 值: {oos_t:+.2f}")
        print()
        if oos_t is not None and abs(oos_t) > 1.96:
            print("     IC 统计显著 -> 信号跨周期存在，值得继续")
        else:
            print("     ⚠️  IC 统计不显著 -> 方向对但强度不足，需更多样本")
    else:
        print(f"  ❌ **样本外未通过**")
        if oos_ex is not None:
            print(f"     OOS 超额（vs 随机）: {oos_ex:+.2%}")
        if oos_ic is not None:
            print(f"     OOS IC 均值: {oos_ic:+.4f}")
        print()
        print("  **按事先写死的判据：这条线就地埋掉。**")
        print("  不要再调参、不要换因子、不要加择时。")
        print("  反转信号是 2023-2026 特定区间的产物，不是结构性规律。")

    print()
    print("=" * 78)

    # 落盘
    out_dir = _REPO_ROOT / "qlib_layer" / "output" / "oos_validation"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "oos_vs_is.json").write_text(
        json.dumps({
            "oos": {"range": [OOS_START, OOS_END], "stats": oos_stats},
            "is": {"range": [IS_START, IS_END], "stats": is_stats},
            "passed": passed,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"输出: {out_dir}")

    return 0 if passed else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
