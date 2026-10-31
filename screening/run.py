"""筛选层主流程。

用法：
    python -m screening.run                    # 用 config.yaml
    python -m screening.run --limit 30         # 只跑前 30 只（快速验证）
    python -m screening.run --config my.yaml   # 指定配置

输出（都在 screening/output/ 下，按日期分目录）：
    candidates.csv    通过的股票
    rejected.csv      被剔除的股票 + 原因（审计用）
    audit.json        每层通过数、耗时、配置快照、数据源信息

核心设计
--------
**可复现**：同一天跑两次，输出必须完全一致。
为做到这点：
  - 配置快照存进 audit.json
  - 所有数据源、参数都记录
  - 不依赖任何随机性
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tradingagents  # noqa: E402,F401  触发 netfix
from screening import fetch, filters  # noqa: E402

logger = logging.getLogger(__name__)


def load_config(path: Path) -> dict:
    """读 YAML 配置。"""
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("需要 pyyaml：uv pip install pyyaml") from exc

    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def config_fingerprint(config: dict) -> str:
    """配置指纹：用于验证可复现性。"""
    blob = json.dumps(config, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def run(
    config: dict,
    limit: int | None = None,
    today: date | None = None,
) -> dict:
    """执行筛选，返回审计报告。"""
    started = time.time()
    today = today or date.today()

    ds = config.get("data_sources", {})
    retries = int(ds.get("retries", 3))
    backoff = float(ds.get("retry_backoff_seconds", 1.5))
    interval = float(ds.get("request_interval_seconds", 0.05))
    workers = int(ds.get("workers", 10))
    # 复权方式：qfq（前复权，默认）/ hfq（后复权）/ ""（不复权）
    #
    # 2026-10-31 实测：除权日（茅台 2024-06-19）不复权 -1.35%（含假跌），
    # 前复权 +0.75%、后复权 +0.61%，两者都修正了跳空。
    # 跨除权日 20 日动量（工行）：不复权 +14.18% / 前复权 +17.70% / 后复权 +8.51%。
    # 前复权消除了假跌且更接近真实涨跌，故选 qfq。
    adjust = str(ds.get("adjust", "qfq"))
    daily_source = ds.get("daily_prices", "tencent")

    rules = config.get("filters", {})
    universe_cfg = config.get("universe", {})
    indices = universe_cfg.get("indices", [])

    # ---------- 1. 成分股 ----------
    print(f"[1/4] 拉指数成分股（{len(indices)} 个指数）…")
    stocks = fetch.fetch_index_constituents(
        indices, retries=retries, backoff=backoff, interval=interval)
    print(f"      原始 {len(stocks)} 条")

    if universe_cfg.get("deduplicate", True):
        seen: dict[str, dict] = {}
        for s in stocks:
            if s["code"] not in seen:
                seen[s["code"]] = s
            else:
                # 记录所属多个指数
                prev = seen[s["code"]]
                prev_indexes = prev.get("also_in", "")
                seen[s["code"]]["also_in"] = (
                    f"{prev_indexes},{s['index_name']}" if prev_indexes else s["index_name"]
                )
        stocks = list(seen.values())
        print(f"      去重后 {len(stocks)} 只")

    if limit:
        stocks = stocks[:limit]
        print(f"      【测试模式】只跑前 {len(stocks)} 只")

    # ---------- 2. 股票名称（判 ST）----------
    print("[2/4] 拉股票名称（判 ST）…")
    name_map: dict[str, str] = {}
    name_ok = True
    try:
        name_map = fetch.fetch_stock_names(retries=retries, backoff=backoff)
        print(f"      {len(name_map)} 只")
        # 用最新名称覆盖成分股里的名称
        for s in stocks:
            if s["code"] in name_map:
                s["name"] = name_map[s["code"]]
    except fetch.DataFetchError as exc:
        name_ok = False
        print(f"      ⚠️  失败，用成分股自带名称: {str(exc)[:60]}")

    # ---------- 3. 日线 ----------
    print(f"[3/4] 拉日线（{len(stocks)} 只，源={daily_source}，复权={adjust or '不复权'}）…")
    lookback = int(rules.get("min_listing_months", {}).get("lookback_years", 3))
    start, end = fetch.default_window(lookback, today)

    def progress(idx, total, code):
        if idx % 50 == 0 or idx == total:
            print(f"      {idx}/{total}")

    daily_map = fetch.fetch_all_daily(
        stocks, start, end,
        retries=retries, backoff=backoff, interval=interval,
        source=daily_source, progress=progress, workers=workers,
        adjust=adjust,
    )
    print(f"      成功 {len(daily_map)}，失败 {len(stocks) - len(daily_map)}")

    # ---------- 4. 过滤 ----------
    print("[4/4] 应用过滤规则…")
    results = []
    for stock in stocks:
        daily = daily_map.get(stock["code"])
        results.append(
            filters.apply_all(stock, daily, rules, today))
    summary = filters.summarize(results, rules)

    print(f"      通过 {summary['passed']} / 剔除 {summary['rejected']}")
    for rule, count in summary["by_rule"].items():
        print(f"        - {rule}: 剔除 {count} 只")

    elapsed = time.time() - started

    # ---------- 输出 ----------
    audit = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "analysis_date": today.isoformat(),
        "config_fingerprint": config_fingerprint(config),
        "config": config,
        "data_window": {"start": start.isoformat(), "end": end.isoformat()},
        "data_sources": {
            "index_constituents": "csindex",
            "daily_prices": daily_source,
            "adjust": adjust,
            "stock_names_ok": name_ok,
            "daily_success": len(daily_map),
            "daily_failed": len(stocks) - len(daily_map),
        },
        "summary": summary,
        "elapsed_seconds": round(elapsed, 1),
    }

    out_cfg = config.get("output", {})
    out_dir = _REPO_ROOT / out_cfg.get("dir", "screening/output") / today.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    encoding = out_cfg.get("encoding", "utf-8-sig")

    # candidates.csv
    passed_rows = [
        {
            "code": r.code,
            "name": r.name,
            "avg_amount_yi": round(r.metrics.get("avg_amount_yi", 0), 2),
            "listed_months_est": r.metrics.get("listed_months_est", ""),
            "first_bar": r.metrics.get("first_bar", ""),
            "pass_reason": "通过全部规则",
            "analysis_date": today.isoformat(),
        }
        for r in results if r.passed
    ]
    _write_csv(out_dir / "candidates.csv", passed_rows, encoding)

    # rejected.csv（审计用）
    if out_cfg.get("include_rejected", True):
        rejected_rows = [
            {
                "code": r.code,
                "name": r.name,
                "reject_rule": r.reject_rule,
                "reject_reason": r.reject_reason,
                "analysis_date": today.isoformat(),
            }
            for r in results if not r.passed
        ]
        _write_csv(out_dir / "rejected.csv", rejected_rows, encoding)

    # audit.json
    (out_dir / "audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    print()
    print(f"完成，耗时 {elapsed:.1f}s")
    print(f"输出目录: {out_dir}")
    print(f"  候选: {len(passed_rows)} 只 -> candidates.csv")
    print(f"  剔除: {summary['rejected']} 只 -> rejected.csv")
    print(f"  审计: audit.json（配置指纹 {audit['config_fingerprint']}）")

    return audit


def _write_csv(path: Path, rows: list[dict], encoding: str) -> None:
    """写 CSV。空列表也要写表头。"""
    import csv

    if not rows:
        path.write_text("", encoding=encoding)
        return
    with path.open("w", encoding=encoding, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="筛选层：指数成分股 + 硬性过滤")
    parser.add_argument(
        "--config", default=str(_REPO_ROOT / "screening" / "config.yaml"))
    parser.add_argument("--limit", type=int, default=None,
                        help="只跑前 N 只（快速验证用）")
    parser.add_argument("--date", default=None,
                        help="分析日期 YYYY-MM-DD（默认今天）")
    args = parser.parse_args(argv[1:])

    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    config = load_config(Path(args.config))
    today = date.fromisoformat(args.date) if args.date else None

    try:
        run(config, limit=args.limit, today=today)
    except fetch.DataFetchError as exc:
        print(f"\n数据获取失败: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
