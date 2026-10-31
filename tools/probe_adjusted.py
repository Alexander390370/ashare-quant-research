"""验证前复权数据源。

东财 stock_zh_a_hist 支持 adjust="qfq" 但本机 0/8 不可用。
需要确认腾讯/新浪是否有前复权接口。

同时验证「复权污染是否真实存在」——取几只已知有分红送转的票，
对比复权前后 20 日动量的差异。
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


def probe(name, fn, retries=3):
    for attempt in range(1, retries + 1):
        try:
            data = fn()
            if data is None:
                return False, None, "返回 None"
            return True, data, f"{len(data)} 行"
        except Exception as exc:  # noqa: BLE001
            if attempt == retries:
                return False, None, f"{type(exc).__name__}: {str(exc)[:60]}"
            time.sleep(1.5)


def main() -> int:
    import akshare as ak

    print("=" * 76)
    print("前复权数据源探测")
    print("=" * 76)

    # 候选股票：选几只已知有分红/送转的
    # 茅台每年分红，银行股高股息，送转多的中小盘
    tests = [
        ("600519", "sh600519", "贵州茅台（高分红）"),
        ("601398", "sh601398", "工商银行（高股息）"),
        ("000001", "sz000001", "平安银行"),
        ("002594", "sz002594", "比亚迪"),
    ]

    print("\n【1】腾讯日线是否有复权参数")
    import inspect

    sig = inspect.signature(ak.stock_zh_a_hist_tx)
    print(f"  参数: {list(sig.parameters.keys())}")
    has_adjust = "adjust" in sig.parameters
    print(f"  支持 adjust: {has_adjust}")

    print("\n【2】新浪日线是否有复权参数")
    sig2 = inspect.signature(ak.stock_zh_a_daily)
    print(f"  参数: {list(sig2.parameters.keys())}")
    has_adjust_sina = "adjust" in sig2.parameters
    print(f"  支持 adjust: {has_adjust_sina}")

    print("\n【3】实际测试前复权拉取")
    for code, sym, name in tests:
        print(f"\n  --- {code} {name} ---")
        for source, fn, kwargs in [
            ("新浪qfq", ak.stock_zh_a_daily,
             {"symbol": sym, "start_date": "20250101", "end_date": "20261030", "adjust": "qfq"}),
            ("腾讯(默认)", ak.stock_zh_a_hist_tx,
             {"symbol": sym, "start_date": "20250101", "end_date": "20261030"}),
        ]:
            ok, df, msg = probe(f"{source}", lambda f=fn, k=kwargs: f(**k))
            if ok:
                cols = list(df.columns)
                first_close = float(df.iloc[0]["close"])
                last_close = float(df.iloc[-1]["close"])
                print(f"    {source:10s} OK  {msg}  首{first_close:.2f} 尾{last_close:.2f}  cols={cols[:5]}")
            else:
                print(f"    {source:10s} FAIL {msg}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
