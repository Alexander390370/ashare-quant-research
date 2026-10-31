"""批量回测网格：挂机跑，断点续跑，跑完汇总。

用途
----
TradingAgents 的每个回测单元格要跑完整 agent 链（本机实测约 27 分钟/格）。
想积累统计上可用的样本（n=30~50），必须挂机跑。这个脚本解决三件事：

1. **断点续跑**：中断后用同一个 run-id 接着跑，已完成的单元格自动跳过
2. **进度可见**：每格开始/结束都打时间戳和累计耗时，方便判断要不要停
3. **跑完自动汇总**：直接给出按评级分组的 hit_rate / mean_alpha

用法
----
    # 小规模验证（先跑这个）
    python tools/run_backtest_grid.py --tickers 600519.SS \
        --start 2026-08-01 --end 2026-09-01 --every 14 --analysts market

    # 夜间挂机（后台跑，日志落文件）
    python tools/run_backtest_grid.py --tickers 600519.SS,000001.SZ \
        --start 2026-06-01 --end 2026-09-01 --every 7 --analysts market \
        --run-id night01

    # 中断后继续
    python tools/run_backtest_grid.py --tickers 600519.SS,000001.SZ \
        --start 2026-06-01 --end 2026-09-01 --every 7 --analysts market \
        --run-id night01

    # 只看汇总，不跑
    python tools/run_backtest_grid.py --summarize night01

设计取舍
--------
- **不并发**：多 agent 链并发会把 LLM 配额打满、日志交错难读。串行更稳。
- **单元格级容错**：一格失败不影响整轮（上游 run_backtest 已实现）。
- **成本预警**：开跑前估算总耗时，超过阈值要求确认（`--yes` 跳过）。
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# 单格实测耗时（分钟），用于成本预警。只跑 market 时约 27 分钟。
_MINUTES_PER_CELL = {"market": 27, "sentiment": 12, "news": 20, "fundamentals": 15}


def estimate_minutes(cells: int, analysts: list[str]) -> int:
    """粗估总耗时（分钟）。"""
    per_cell = sum(_MINUTES_PER_CELL.get(a, 20) for a in analysts)
    return cells * per_cell


def fmt_duration(minutes: float) -> str:
    if minutes < 60:
        return f"{minutes:.0f} 分钟"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.1f} 小时"
    return f"{hours / 24:.1f} 天"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="批量回测网格：挂机跑 + 断点续跑 + 自动汇总",
    )
    parser.add_argument("--tickers", help="逗号分隔的标的，如 600519.SS,000001.SZ")
    parser.add_argument("--start", help="起始分析日期 YYYY-MM-DD")
    parser.add_argument("--end", help="结束分析日期 YYYY-MM-DD")
    parser.add_argument("--every", type=int, default=7, help="每隔几天取一格（默认 7）")
    parser.add_argument(
        "--analysts", default="market",
        help="逗号分隔：market,sentiment,news,fundamentals（默认只跑 market）",
    )
    parser.add_argument("--run-id", help="续跑已有 sweep；不给则新建")
    parser.add_argument("--summarize", metavar="RUN_ID", help="只汇总某个已有 sweep，不跑")
    parser.add_argument("--yes", action="store_true", help="跳过成本预警确认")
    args = parser.parse_args(argv[1:])

    import tradingagents  # noqa: F401  触发 netfix
    from tradingagents.backtest import iter_grid, run_backtest, summarize
    from tradingagents.default_config import DEFAULT_CONFIG

    # 只汇总模式
    if args.summarize:
        run_dir = Path(DEFAULT_CONFIG["results_dir"]) / "backtest" / args.summarize
        log_path = run_dir / "trading_memory.md"
        if not log_path.exists():
            print(f"找不到 sweep 日志: {log_path}", file=sys.stderr)
            return 1
        print(summarize(log_path).render())
        return 0

    if not (args.tickers and args.start and args.end):
        parser.error("需要 --tickers / --start / --end（或用 --summarize）")

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    analysts = [a.strip() for a in args.analysts.split(",") if a.strip()]
    dates = iter_grid(args.start, args.end, every_n_days=args.every)
    cells = len(tickers) * len(dates)
    eta = estimate_minutes(cells, analysts)

    print("=" * 66)
    print("批量回测计划")
    print("=" * 66)
    print(f"  标的      : {', '.join(tickers)}")
    print(f"  日期      : {args.start} ~ {args.end}，每 {args.every} 天")
    print(f"  分析师    : {', '.join(analysts)}")
    print(f"  单元格数  : {len(tickers)} × {len(dates)} = {cells}")
    print(f"  预计耗时  : 约 {fmt_duration(eta)}（单格实测估算，实际可能更长）")
    print(f"  run_id    : {args.run_id or '（自动生成）'}")
    print("=" * 66)

    if not args.yes and eta > 60:
        print()
        print(f"⚠️  预计超过 1 小时（{fmt_duration(eta)}）。")
        print("   建议先跑 2 格验证链路：--every 30 --analysts market")
        print("   确认要继续，加 --yes 重跑本命令。")
        return 2

    config = DEFAULT_CONFIG.copy()
    started = time.time()
    last_cell_at = started

    def progress(done: int, total: int, ticker: str, date: str) -> None:
        nonlocal last_cell_at
        now = time.time()
        elapsed = now - started
        since_last = now - last_cell_at
        last_cell_at = now
        avg = elapsed / max(done - 1, 1) if done > 1 else 0
        remaining = avg * (total - done + 1)
        print(
            f"[{datetime.now():%H:%M:%S}] 第 {done}/{total} 格  {ticker} {date}"
            f"  | 上一格 {since_last / 60:.0f} 分钟"
            f"  | 预计剩余 {fmt_duration(remaining / 60)}"
        )

    result = run_backtest(
        tickers, dates, config,
        selected_analysts=analysts,
        run_id=args.run_id,
        progress=progress,
    )

    total_minutes = (time.time() - started) / 60
    print()
    print("=" * 66)
    print(f"完成：跑了 {result.cells_run} 格，跳过 {result.skipped} 格，"
          f"失败 {len(result.failures)} 格")
    print(f"总耗时：{fmt_duration(total_minutes)}")
    print(f"日志：{result.log_path}")
    print(f"续跑：--run-id {result.run_id}")
    if result.failures:
        print()
        print("失败明细：")
        for ticker, date, reason in result.failures[:10]:
            print(f"  {ticker} {date}: {reason[:80]}")
    print("=" * 66)

    print()
    print(summarize(result).render())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
