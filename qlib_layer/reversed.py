"""反向策略验证：按诊断结果重配权重，看能不能跑赢。

诊断结论（2026-10-31）
---------------------
单因子 IC（月度，34 期）：
    rev1      +0.0293  ← 正向
    mom5      -0.0394  ← 反向
    mom20     -0.0328  ← 反向
    dev20     -0.0393  ← 反向
    vol20     -0.0101  ← 无效
    vol_ratio -0.0098  ← 无效

分档收益（Q1=高分，Q5=低分）：
    Q1 +1.48% / Q2 +1.41% / Q3 +1.58% / Q4 +2.00% / Q5 +2.51%
    **完美单调反向**

本脚本测三种方案
----------------
1. **原方案**（基线）：动量+低波
2. **纯反向**：权重全取反
3. **按 IC 重配**：只用有效因子，方向按 IC 符号

用法
----
    python -m qlib_layer.reversed
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tradingagents  # noqa: E402,F401

from qlib_layer.backtest import (  # noqa: E402
    DEFAULT_PROVIDER,
    judge,
    report,
    run_backtest,
    save,
)

logger = logging.getLogger(__name__)


# 方案定义
SCHEMES = {
    "baseline": {
        "label": "原方案（动量+低波）",
        "direction": "long",
        "weights": None,  # 用 DEFAULT_WEIGHTS
    },
    "inverted": {
        "label": "纯反向（权重全取反）",
        "direction": "inverted",
        "weights": None,
    },
    "ic_weighted": {
        "label": "按 IC 重配（只用有效因子）",
        "direction": "long",
        # 只用 |IC| > 0.02 的因子，符号按 IC 方向
        # rev1 +0.0293 -> +0.0293
        # mom5 -0.0394 -> -0.0394
        # mom20 -0.0328 -> -0.0328
        # dev20 -0.0393 -> -0.0393
        # vol20/vol_ratio 无效，权重 0
        "weights": {
            "rev1_z": 0.0293,
            "mom5_z": -0.0394,
            "mom20_z": -0.0328,
            "dev20_z": -0.0393,
            "vol20_z": 0.0,
            "vol_ratio_z": 0.0,
        },
    },
}


def main() -> int:
    logging.basicConfig(level=logging.WARNING)

    print("=" * 78)
    print("反向策略验证")
    print("=" * 78)

    results = {}

    for key, scheme in SCHEMES.items():
        print(f"\n{'─' * 78}")
        print(f"方案：{scheme['label']}")
        print(f"{'─' * 78}")

        try:
            result = run_backtest(
                provider_uri=DEFAULT_PROVIDER,
                top_n=20,
                freq="M",
                include_cost=True,
                direction=scheme["direction"],
                weights=scheme["weights"],
            )
            results[key] = result
            print(report(result, 20))
        except Exception as exc:  # noqa: BLE001
            logger.error("方案 %s 失败: %s", key, exc)
            print(f"  ❌ 失败: {exc}")

    # ---------- 对比 ----------
    print()
    print("=" * 78)
    print("三方案对比")
    print("=" * 78)
    print()
    print(f"  {'方案':<26} {'累计收益':>10} {'基准':>10} {'超额':>10} {'IC均值':>9} {'换手':>8}")
    print("  " + "-" * 76)

    for key, scheme in SCHEMES.items():
        result = results.get(key)
        if result is None or "error" in result.stats:
            print(f"  {scheme['label']:<26} {'失败':>10}")
            continue
        s = result.stats
        print(f"  {scheme['label']:<26} {s['cum_portfolio']:>+9.2%} "
              f"{s['cum_benchmark']:>+9.2%} {s['cum_excess']:>+9.2%} "
              f"{s['ic_mean']:>+9.4f} {s['avg_turnover']:>7.1%}")

    # ---------- 最终判断 ----------
    print()
    print("=" * 78)
    print("最终判断")
    print("=" * 78)
    print()

    best_key = None
    best_excess = None
    for key, result in results.items():
        if result is None or "error" in result.stats:
            continue
        excess = result.stats["cum_excess"]
        if best_excess is None or excess > best_excess:
            best_excess = excess
            best_key = key

    if best_key is None:
        print("  所有方案都失败，无法判断")
        return 1

    best = results[best_key]
    print(f"  最佳方案：**{SCHEMES[best_key]['label']}**")
    print(f"  超额收益：{best_excess:+.2%}")
    print()
    print(judge(best))

    # 落盘
    out_dir = _REPO_ROOT / "qlib_layer" / "output" / "reversed"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        key: {
            "label": SCHEMES[key]["label"],
            "direction": SCHEMES[key]["direction"],
            "weights": SCHEMES[key]["weights"],
            "stats": results[key].stats if key in results else None,
        }
        for key in SCHEMES
    }
    (out_dir / "comparison.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    for key, result in results.items():
        if result is not None and "error" not in result.stats:
            save(result, out_dir / key, 20)

    print()
    print(f"输出: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
