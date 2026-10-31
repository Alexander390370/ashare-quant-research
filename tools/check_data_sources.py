"""数据源体检：筛选层需要的数据，在这台机器上到底拿不拿得到。

验四件事（不管最后用简单版还是 Qlib/Vibe，这四样都得有）：
1. 指数成分股名单（沪深300 / 中证500）
2. 股票基础信息（上市日期、ST 标记）
3. 成交额（判断流动性）
4. 复权因子 / 前复权价

每项都带重试，记录耗时。失败要给出具体原因，不能含糊。
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

import tradingagents  # noqa: E402,F401  触发 netfix（代理 + 数据目录）


def probe(name, fn, retries=3):
    """跑一个探测，返回 (成功, 数据, 耗时秒, 说明)。"""
    started = time.time()
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            data = fn()
            elapsed = time.time() - started
            if data is None:
                return False, None, elapsed, "返回 None"
            count = len(data) if hasattr(data, "__len__") else 1
            return True, data, elapsed, f"{count} 行"
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < retries:
                time.sleep(1.5 * attempt)
    elapsed = time.time() - started
    return False, None, elapsed, f"{type(last_error).__name__}: {str(last_error)[:70]}"


def main() -> int:
    import akshare as ak

    print("=" * 78)
    print("数据源体检（筛选层地基）")
    print("=" * 78)

    results = {}

    # ---------- 1. 指数成分股 ----------
    print("\n【1】指数成分股")
    ok, hs300, t, msg = probe(
        "沪深300", lambda: ak.index_stock_cons_csindex(symbol="000300"))
    results["hs300"] = ok
    print(f"  沪深300      {'OK  ' if ok else 'FAIL'}  {t:5.1f}s  {msg}")
    if ok and len(hs300) > 0:
        cols = list(hs300.columns)
        print(f"      字段: {cols[:8]}")
        print(f"      样例: {dict(list(hs300.iloc[0].items())[:4])}")

    ok, zz500, t, msg = probe(
        "中证500", lambda: ak.index_stock_cons_csindex(symbol="000905"))
    results["zz500"] = ok
    print(f"  中证500      {'OK  ' if ok else 'FAIL'}  {t:5.1f}s  {msg}")
    if ok and len(zz500) > 0:
        print(f"      样例: {dict(list(zz500.iloc[0].items())[:4])}")

    # ---------- 2. 股票基础信息 ----------
    print("\n【2】股票基础信息（上市日期 / ST 标记）")
    ok, info, t, msg = probe(
        "全A代码名称", lambda: ak.stock_info_a_code_name())
    results["code_name"] = ok
    print(f"  全A代码名称  {'OK  ' if ok else 'FAIL'}  {t:5.1f}s  {msg}")
    if ok and len(info) > 0:
        print(f"      字段: {list(info.columns)}")
        print(f"      样例: {dict(list(info.iloc[0].items()))}")
        # 看有没有 ST 股
        if "name" in info.columns:
            st_count = int(info["name"].astype(str).str.contains("ST").sum())
            print(f"      含 ST 的股票数: {st_count}")

    ok, one, t, msg = probe(
        "个股信息(600519)", lambda: ak.stock_individual_info_em(symbol="600519"))
    results["individual"] = ok
    print(f"  个股信息     {'OK  ' if ok else 'FAIL'}  {t:5.1f}s  {msg}")
    # stock_individual_info_em 返回两列：item / value
    if ok and one is not None and len(one) > 0 and "item" in one.columns:
                keys = list(one["item"].astype(str))
                print(f"      可用字段: {keys[:10]}")
                has_list_date = any("上市" in k for k in keys)
                print(f"      含上市日期: {has_list_date}")

    # ---------- 3. 成交额（流动性判断）----------
    print("\n【3】日线 + 成交额（流动性判断）")
    print("  注：东财 stock_zh_a_hist 在本机不稳定（ProxyError），腾讯/新浪可替代")

    daily_source = None
    ok, hist, t, msg = probe(
        "东财日线",
        lambda: ak.stock_zh_a_hist(
            symbol="600519", period="daily",
            start_date="20260901", end_date="20261030", adjust=""))
    print(f"  东财日线     {'OK  ' if ok else 'FAIL'}  {t:5.1f}s  {msg}")
    if ok:
        daily_source = "东财"

    if not ok:
        ok, hist, t, msg = probe(
            "腾讯日线",
            lambda: ak.stock_zh_a_hist_tx(
                symbol="sh600519", start_date="20260901", end_date="20261030"))
        print(f"  腾讯日线     {'OK  ' if ok else 'FAIL'}  {t:5.1f}s  {msg}")
        if ok:
            daily_source = "腾讯"

    if not ok:
        ok, hist, t, msg = probe(
            "新浪日线",
            lambda: ak.stock_zh_a_daily(
                symbol="sh600519", start_date="20260901", end_date="20261030"))
        print(f"  新浪日线     {'OK  ' if ok else 'FAIL'}  {t:5.1f}s  {msg}")
        if ok:
            daily_source = "新浪"

    results["daily"] = ok
    results["daily_source"] = daily_source
    if ok and hist is not None and len(hist) > 0:
        cols = list(hist.columns)
        has_amount = "amount" in cols or "成交额" in cols
        print(f"      数据源: {daily_source} | 字段: {cols}")
        print(f"      含成交额: {has_amount}")
        if has_amount:
            col = "amount" if "amount" in cols else "成交额"
            avg = float(hist[col].astype(float).mean())
            print(f"      区间日均成交额: {avg/1e8:.2f} 亿元")

    # ---------- 4. 复权 ----------
    print("\n【4】复权（回测必需）")
    ok, qfq, t, msg = probe(
        "东财前复权",
        lambda: ak.stock_zh_a_hist(
            symbol="600519", period="daily",
            start_date="20260101", end_date="20261030", adjust="qfq"))
    print(f"  东财前复权   {'OK  ' if ok else 'FAIL'}  {t:5.1f}s  {msg}")
    results["qfq"] = ok
    if ok and qfq is not None and len(qfq) > 0:
        print(f"      首行收盘: {qfq.iloc[0]['收盘']}  尾行收盘: {qfq.iloc[-1]['收盘']}")
    elif daily_source in ("腾讯", "新浪"):
        print(f"      替代：{daily_source} 日线未复权，需自算复权因子")
        results["qfq"] = daily_source  # 标记为"有替代"

    # ---------- 汇总 ----------
    print("\n" + "=" * 78)
    print("汇总")
    print("=" * 78)
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    for key, ok_flag in results.items():
        print(f"  {key:14s} {'✅' if ok_flag else '❌'}")
    print()
    print(f"  {passed}/{total} 通过")

    if passed == total:
        print("\n  结论：数据地基可用。无论走简单版还是 Qlib/Vibe，这四样都够。")
        return 0
    if passed >= total - 1:
        print("\n  结论：基本可用，个别项需换源。")
        return 0
    print("\n  结论：数据地基不稳，先解决数据再谈框架。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
