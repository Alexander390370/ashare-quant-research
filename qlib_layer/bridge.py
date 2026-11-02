"""筛选层 → 深挖层的桥接。

闭环设计
--------
    A 线（筛选）产出 candidates.csv
              ↓
    bridge.py 挑出 N 只（按规则或人工）
              ↓
    TradingAgents 批量深挖
              ↓
    结果回写 candidates.csv（新增「深挖结论」列）

为什么要这个桥
--------------
六家评审（2026-10-31）：
    "TradingAgents 是放大器不是验证器"——它把未验证的排序包装成可信结论
    "拿未验证的名单去深挖，是给错误答案写小作文"

所以桥接必须做三件事：
1. **明确输入契约**：筛选层输出什么字段，深挖层吃这些字段
2. **限制数量**：600 只跑不完（27 分钟/只），必须人工挑或规则挑
3. **标注状态**：深挖结论是「待验证假设」，不是「交易指令」

用法
----
    python -m qlib_layer.bridge --list                    # 看候选池
    python -m qlib_layer.bridge --pick 5                  # 按规则挑 5 只
    python -m qlib_layer.bridge --pick 5 --run            # 挑完直接跑深挖
    python -m qlib_layer.bridge --ticker 600519.SS        # 单只
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tradingagents  # noqa: E402,F401

logger = logging.getLogger(__name__)

SCREENING_OUTPUT = _REPO_ROOT / "screening" / "output"
BRIDGE_OUTPUT = _REPO_ROOT / "qlib_layer" / "output" / "bridge"


def load_candidates(run_date: str | None = None) -> pd.DataFrame:
    """读筛选层输出的候选池。"""
    if run_date is None:
        # 找最新的
        dirs = sorted(
            [d for d in SCREENING_OUTPUT.iterdir() if d.is_dir()],
            key=lambda d: d.name, reverse=True) if SCREENING_OUTPUT.exists() else []
        if not dirs:
            raise FileNotFoundError(f"找不到筛选输出：{SCREENING_OUTPUT}")
        run_date = dirs[0].name

    cand_path = SCREENING_OUTPUT / run_date / "candidates.csv"
    if not cand_path.exists():
        raise FileNotFoundError(f"找不到候选文件：{cand_path}")

    return pd.read_csv(cand_path, encoding="utf-8-sig", dtype={"code": str})


def to_tradingagents_symbol(code: str) -> str:
    """把 6 位代码转成 TradingAgents 的 ticker 格式。

    600519 -> 600519.SS（上交所）
    000001 -> 000001.SZ（深交所）
    430047 -> 430047.BJ（北交所）
    """
    code = str(code).strip().zfill(6)
    if code.startswith("6"):
        return f"{code}.SS"
    if code.startswith(("0", "3")):
        return f"{code}.SZ"
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    return code


def pick_by_rules(
    df: pd.DataFrame,
    n: int = 5,
    rule: str = "largest",
) -> pd.DataFrame:
    """按规则挑 N 只。

    rule:
        "largest"   -> 市值最大的 N 只（流动性最好，最安全）
        "smallest"  -> 市值最小的 N 只（弹性大，风险高）
        "liquid"    -> 成交额最大的 N 只
        "random"    -> 随机 N 只（用于对照）
        "spread"    -> 按行业分散挑（每个行业最多 1 只）
    """
    if len(df) == 0:
        return df.head(0)

    if rule == "largest" and "market_cap_yi" in df.columns:
        return df.nlargest(n, "market_cap_yi")
    if rule == "smallest" and "market_cap_yi" in df.columns:
        return df.nsmallest(n, "market_cap_yi")
    if rule == "liquid" and "avg_amount_yi" in df.columns:
        return df.nlargest(n, "avg_amount_yi")
    if rule == "random":
        return df.sample(n=min(n, len(df)), random_state=42)
    if rule == "spread" and "industry_final" in df.columns:
        picked = []
        used_industries: set[str] = set()
        # 先按市值降序，每个行业最多 1 只
        for _, row in df.sort_values("market_cap_yi", ascending=False).iterrows():
            ind = str(row.get("industry_final", "") or "未知")
            if ind in used_industries:
                continue
            picked.append(row)
            used_industries.add(ind)
            if len(picked) >= n:
                break
        if picked:
            return pd.DataFrame(picked)

    return df.head(n)


def run_deep_analysis(
    tickers: list[str],
    analysis_date: str,
    analysts: str = "market,news",
    timeout_seconds: int = 3600,
) -> list[dict]:
    """批量跑 TradingAgents 深挖。

    每只约 10-27 分钟，所以默认只跑少量。
    """
    results = []
    py = sys.executable

    for idx, ticker in enumerate(tickers, 1):
        print(f"\n[{idx}/{len(tickers)}] 深挖 {ticker} …")
        started = time.time()
        cmd = [
            py, "-m", "cli.main",
            "--ticker", ticker,
            "--date", analysis_date,
            "--analysts", analysts,
            "--save",
            "--no-show",
        ]
        try:
            proc = subprocess.run(
                cmd, cwd=str(_REPO_ROOT),
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=timeout_seconds,
            )
            elapsed = time.time() - started
            ok = proc.returncode == 0
            results.append({
                "ticker": ticker,
                "ok": ok,
                "elapsed_seconds": round(elapsed, 1),
                "returncode": proc.returncode,
                "stderr_tail": (proc.stderr or "")[-300:] if not ok else "",
            })
            print(f"    {'✅' if ok else '❌'} {elapsed:.0f}s")
        except subprocess.TimeoutExpired:
            results.append({
                "ticker": ticker,
                "ok": False,
                "elapsed_seconds": timeout_seconds,
                "returncode": -1,
                "stderr_tail": "超时",
            })
            print(f"    ❌ 超时（{timeout_seconds}s）")

    return results


def write_bridge_record(
    picked: pd.DataFrame,
    results: list[dict] | None,
    out_dir: Path,
) -> Path:
    """写桥接记录。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    record = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "picked_count": len(picked),
        "picked": picked.to_dict("records"),
        "deep_analysis": results,
        "status_note": (
            "深挖结论是「待验证假设」，不是「交易指令」。"
            "六家评审明确指出：TradingAgents 是放大器，不是验证器。"
        ),
    }

    path = out_dir / f"bridge_{stamp}.json"
    path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")

    # 同时写一份 CSV 便于查看
    csv_path = out_dir / f"picked_{stamp}.csv"
    picked.to_csv(csv_path, index=False, encoding="utf-8-sig")

    return path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="筛选 → 深挖桥接")
    parser.add_argument("--date", default=None, help="筛选结果日期（默认最新）")
    parser.add_argument("--list", action="store_true", help="只看候选池")
    parser.add_argument("--pick", type=int, default=0, help="挑 N 只")
    parser.add_argument("--rule", default="largest",
                        choices=["largest", "smallest", "liquid", "random", "spread"])
    parser.add_argument("--run", action="store_true", help="挑完直接跑深挖")
    parser.add_argument("--analysts", default="market,news")
    parser.add_argument("--ticker", default=None, help="单只股票代码")
    args = parser.parse_args(argv[1:])

    logging.basicConfig(level=logging.WARNING)

    # --- 单只模式 ---
    if args.ticker:
        ticker = to_tradingagents_symbol(args.ticker)
        print(f"深挖 {ticker} …")
        results = run_deep_analysis(
            [ticker], args.date or date.today().isoformat(), args.analysts)
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    # --- 读候选池 ---
    try:
        df = load_candidates(args.date)
    except FileNotFoundError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1

    print("=" * 70)
    print(f"候选池：{len(df)} 只")
    print("=" * 70)
    print()
    print(f"  字段: {list(df.columns)}")
    print()

    if "industry_final" in df.columns:
        print("  行业分布（前 10）:")
        for ind, cnt in df["industry_final"].value_counts().head(10).items():
            print(f"    {str(ind):20s} {cnt:5d}")
        print()

    if "market_cap_yi" in df.columns and df["market_cap_yi"].notna().any():
        print(f"  市值范围: {df['market_cap_yi'].min():.1f} ~ "
              f"{df['market_cap_yi'].max():.1f} 亿")
        print(f"  中位市值: {df['market_cap_yi'].median():.1f} 亿")
        print()

    if args.list or args.pick == 0:
        print("  前 10 只:")
        show_cols = [c for c in ["code", "name", "board_code", "industry_final",
                                 "market_cap_yi", "avg_amount_yi"]
                     if c in df.columns]
        print(df[show_cols].head(10).to_string(index=False))
        print()
        print("提示：用 --pick N --rule largest 挑 N 只，加 --run 直接跑深挖")
        return 0

    # --- 挑 N 只 ---
    picked = pick_by_rules(df, n=args.pick, rule=args.rule)
    print(f"按规则「{args.rule}」挑了 {len(picked)} 只：")
    show_cols = [c for c in ["code", "name", "industry_final", "market_cap_yi"]
                 if c in picked.columns]
    print(picked[show_cols].to_string(index=False))
    print()

    tickers = [to_tradingagents_symbol(c) for c in picked["code"]]
    print(f"对应 ticker: {', '.join(tickers)}")
    print()

    # --- 跑深挖 ---
    results = None
    if args.run:
        print("=" * 70)
        print(f"开始深挖（{len(tickers)} 只 × 约 10-27 分钟）")
        print("=" * 70)
        results = run_deep_analysis(
            tickers, args.date or date.today().isoformat(), args.analysts)

    # --- 落盘 ---
    path = write_bridge_record(picked, results, BRIDGE_OUTPUT)
    print()
    print(f"记录: {path}")

    if results:
        ok = sum(1 for r in results if r["ok"])
        print(f"深挖完成：{ok}/{len(results)} 成功")
        print()
        print("⚠️  提醒：深挖结论是「待验证假设」，不是「交易指令」。")
        print("    六家评审：TradingAgents 是放大器，不是验证器。")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
