"""数据源稳定性压力测试。

目的：区分「偶发失败」和「真的不可用」。

方法：每个源连打 N 次，统计成功率。判断标准：
  ≥90%  → 稳定可用
  50-90% → 不稳定但可用（需重试）
  <50%  → 不可用

覆盖筛选层真正需要的四个源：
  1. 中证官网（指数成分股）
  2. 腾讯（日线 + 成交额）
  3. 新浪（日线备选）
  4. 东财（日线，之前不稳定）
"""

from __future__ import annotations

import sys
import time
import warnings
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

warnings.filterwarnings("ignore")

import tradingagents  # noqa: E402,F401  触发 netfix

ATTEMPTS = 8
SLEEP = 0.5


def stress(name: str, fn, attempts: int = ATTEMPTS) -> dict:
    """连打 N 次，统计成功率与耗时。"""
    ok = fail = 0
    times = []
    errors = []
    rows = 0

    for _ in range(attempts):
        started = time.time()
        try:
            data = fn()
            elapsed = time.time() - started
            times.append(elapsed)
            ok += 1
            if hasattr(data, "__len__"):
                rows = len(data)
        except Exception as exc:  # noqa: BLE001
            elapsed = time.time() - started
            fail += 1
            if len(errors) < 2:
                errors.append(f"{type(exc).__name__}: {str(exc)[:50]}")
        time.sleep(SLEEP)

    rate = ok / attempts
    if rate >= 0.9:
        verdict = "稳定"
    elif rate >= 0.5:
        verdict = "不稳定但可用"
    else:
        verdict = "不可用"

    avg = sum(times) / len(times) if times else 0
    return {
        "name": name,
        "ok": ok,
        "fail": fail,
        "rate": rate,
        "verdict": verdict,
        "avg_sec": avg,
        "rows": rows,
        "errors": errors,
    }


def show(r: dict) -> None:
    bar = "█" * int(r["rate"] * 10) + "░" * (10 - int(r["rate"] * 10))
    print(
        f"  {r['name']:16s} {bar} {r['ok']}/{r['ok']+r['fail']}"
        f"  {r['rate']:5.0%}  {r['verdict']:10s}"
        f"  均{r['avg_sec']:4.1f}s  {r['rows']}行"
    )
    for err in r["errors"]:
        print(f"       └ {err}")


def main() -> int:
    import akshare as ak

    print("=" * 82)
    print(f"数据源稳定性压力测试（每源 {ATTEMPTS} 次）")
    print("=" * 82)
    print()

    results = []

    print("【指数成分股】")
    results.append(stress(
        "中证-沪深300",
        lambda: ak.index_stock_cons_csindex(symbol="000300")))
    show(results[-1])
    results.append(stress(
        "中证-中证500",
        lambda: ak.index_stock_cons_csindex(symbol="000905")))
    show(results[-1])

    print()
    print("【日线行情】")
    results.append(stress(
        "腾讯-日线",
        lambda: ak.stock_zh_a_hist_tx(
            symbol="sh600519", start_date="20260901", end_date="20261030")))
    show(results[-1])

    results.append(stress(
        "新浪-日线",
        lambda: ak.stock_zh_a_daily(
            symbol="sh600519", start_date="20260901", end_date="20261030")))
    show(results[-1])

    results.append(stress(
        "东财-日线",
        lambda: ak.stock_zh_a_hist(
            symbol="600519", period="daily",
            start_date="20260901", end_date="20261030", adjust="")))
    show(results[-1])

    print()
    print("【基础信息】")
    results.append(stress(
        "全A代码名称",
        lambda: ak.stock_info_a_code_name()))
    show(results[-1])

    # ---------- 汇总 ----------
    print()
    print("=" * 82)
    print("汇总")
    print("=" * 82)
    stable = [r for r in results if r["rate"] >= 0.9]
    flaky = [r for r in results if 0.5 <= r["rate"] < 0.9]
    broken = [r for r in results if r["rate"] < 0.5]

    if stable:
        print(f"\n  ✅ 稳定可用（≥90%）：{', '.join(r['name'] for r in stable)}")
    if flaky:
        print(f"  ⚠️  不稳定但可用（需重试）：{', '.join(r['name'] for r in flaky)}")
    if broken:
        print(f"  ❌ 不可用（<50%）：{', '.join(r['name'] for r in broken)}")

    print()
    if len(stable) >= 3:
        print("  结论：核心数据源够用，可以开始做筛选层。")
        return 0
    print("  结论：数据源不足，需先解决。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
