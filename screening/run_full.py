"""全市场筛选：支持全 A 股（5000+ 只），不只指数成分股。

与旧版（screening/run.py）的区别
--------------------------------
旧版：只能选指数成分股（800 只），4 条硬规则剔除率 0.9%
新版：支持全市场（5000+ 只），硬规则才有用武之地

新增能力
--------
- 元数据：行业（申万一级）、市值、板块、上市日期
- 分层规则：市值分位、行业分位、流动性分位
- 行业上限：单行业最多占候选池 N%
- 全市场支持

用法
----
    python -m screening.run_full                     # 全市场
    python -m screening.run_full --universe index    # 仅指数成分股（旧行为）
    python -m screening.run_full --limit 100         # 快速验证
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tradingagents  # noqa: E402,F401

from screening import cache, fetch, industry, metadata  # noqa: E402

logger = logging.getLogger(__name__)


def load_config(path: Path) -> dict:
    import yaml

    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def config_fingerprint(config: dict) -> str:
    blob = json.dumps(config, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def build_universe(
    config: dict,
    universe_mode: str = "all",
) -> pd.DataFrame:
    """构建候选宇宙。

    universe_mode:
        "all"   -> 全 A 股（5000+）
        "index" -> 仅指数成分股（旧行为）
    """
    print(f"[1/5] 构建元数据（模式：{universe_mode}）…")
    meta = metadata.build_metadata(use_cache=True)
    print(f"      元数据：{len(meta)} 只（有市值 {int(meta['market_cap_yi'].notna().sum())}）")

    # 补申万行业
    print("[2/5] 补申万行业分类…")
    meta = industry.enrich_with_industry(meta, use_cache=True)
    has_ind = int((meta["industry_final"].fillna("") != "").sum())
    print(f"      有行业：{has_ind}/{len(meta)}")

    # 加板块（从代码判断，比交易所列表可靠）
    meta["board_code"] = meta["code"].map(metadata.board_from_code)

    if universe_mode == "index":
        print("[2.5/5] 限制为指数成分股…")
        indices = config.get("universe", {}).get("indices", [])
        index_codes: set[str] = set()
        for item in indices:
            try:
                frame = fetch.fetch_index_constituents([item], retries=3, backoff=1.5)
                for row in frame:
                    index_codes.add(row["code"])
            except Exception as exc:  # noqa: BLE001
                print(f"      ⚠️  {item.get('name')} 失败：{str(exc)[:50]}")
        meta = meta[meta["code"].isin(index_codes)]
        print(f"      指数成分股：{len(meta)} 只")

    return meta


def apply_rules(
    meta: pd.DataFrame,
    daily_map: dict,
    rules: dict,
    today: date,
) -> pd.DataFrame:
    """应用全部规则，返回带「是否通过 + 剔除原因 + 指标」的 DataFrame。"""
    df = meta.copy()
    df["pass"] = True
    df["reject_rule"] = ""
    df["reject_reason"] = ""

    # 预计算指标
    metrics = []
    for _, row in df.iterrows():
        code = row["code"]
        daily = daily_map.get(code)
        m = _compute_metrics(daily, today)
        metrics.append(m)
    metrics_df = pd.DataFrame(metrics, index=df.index)
    df = pd.concat([df, metrics_df], axis=1)

    # 用上市日期覆盖 listed_months（更准确，避免窗口截断问题）
    if "list_date" in df.columns:
        def _months_from_list_date(val):
            if not val or str(val) in ("", "nan", "None"):
                return None
            try:
                s = str(val).strip()[:10]
                d = datetime.strptime(s, "%Y-%m-%d").date()
                return (today.year - d.year) * 12 + (today.month - d.month)
            except (ValueError, TypeError):
                return None

        precise = df["list_date"].apply(_months_from_list_date)
        # 只覆盖有上市日期的（深交所全部有，上交所部分有）
        mask = precise.notna()
        df.loc[mask, "listed_months"] = precise[mask]
        df["listed_months_source"] = "list_date"
        df.loc[~mask, "listed_months_source"] = "first_bar"

    # --- 规则 1：ST ---
    rule = rules.get("exclude_st", {})
    if rule.get("enabled", True):
        keywords = rule.get("keywords", ["ST", "*ST", "退", "PT"])
        mask = df["name"].astype(str).apply(
            lambda n: any(k.upper() in n.upper() for k in keywords))
        df.loc[mask & df["pass"], "reject_rule"] = "exclude_st"
        df.loc[mask & df["pass"], "reject_reason"] = "名称含 ST/退市"
        df.loc[mask, "pass"] = False

    # --- 规则 2：上市时长 ---
    rule = rules.get("min_listing_months", {})
    if rule.get("enabled", True):
        min_months = int(rule.get("months", 12))
        mask = df["listed_months"].notna() & (df["listed_months"] < min_months)
        hit = mask & df["pass"]
        df.loc[hit, "reject_rule"] = "min_listing_months"
        df.loc[hit, "reject_reason"] = (
            "上市 " + df.loc[hit, "listed_months"].astype(str) + f" 个月 < {min_months}")
        df.loc[mask, "pass"] = False

    # --- 规则 3：流动性（绝对额）---
    rule = rules.get("min_avg_amount", {})
    if rule.get("enabled", True):
        min_yuan = float(rule.get("min_amount_yuan", 5e7))
        mask = df["avg_amount"].notna() & (df["avg_amount"] < min_yuan)
        hit = mask & df["pass"]
        df.loc[hit, "reject_rule"] = "min_avg_amount"
        df.loc[hit, "reject_reason"] = (
            "日均成交额 " + (df.loc[hit, "avg_amount"] / 1e8).round(2).astype(str)
            + f" 亿 < {min_yuan / 1e8:.2f} 亿")
        df.loc[mask, "pass"] = False

    # --- 规则 4：流动性（分位）---
    rule = rules.get("min_amount_percentile", {})
    if rule.get("enabled", False):
        pct = float(rule.get("percentile", 0.2))
        valid = df["avg_amount"].notna()
        if valid.sum() > 100:
            threshold = df.loc[valid, "avg_amount"].quantile(pct)
            mask = valid & (df["avg_amount"] < threshold)
            hit = mask & df["pass"]
            df.loc[hit, "reject_rule"] = "min_amount_percentile"
            df.loc[hit, "reject_reason"] = (
                f"成交额处于后 {pct:.0%} 分位（< {threshold / 1e8:.2f} 亿）")
            df.loc[mask, "pass"] = False

    # --- 规则 5：市值（分位）---
    rule = rules.get("min_market_cap_percentile", {})
    if rule.get("enabled", False):
        pct = float(rule.get("percentile", 0.2))
        valid = df["market_cap_yi"].notna() & (df["market_cap_yi"] > 0)
        if valid.sum() > 100:
            threshold = df.loc[valid, "market_cap_yi"].quantile(pct)
            mask = valid & (df["market_cap_yi"] < threshold)
            hit = mask & df["pass"]
            df.loc[hit, "reject_rule"] = "min_market_cap_percentile"
            df.loc[hit, "reject_reason"] = (
                f"市值处于后 {pct:.0%} 分位（< {threshold:.1f} 亿）")
            df.loc[mask, "pass"] = False

    # --- 规则 6：停牌 ---
    rule = rules.get("exclude_suspended", {})
    if rule.get("enabled", True):
        mask = df["zero_volume_days"].notna() & (
            df["zero_volume_days"] >= int(rule.get("no_trade_days", 5)))
        hit = mask & df["pass"]
        df.loc[hit, "reject_rule"] = "exclude_suspended"
        df.loc[hit, "reject_reason"] = "近期全无成交（停牌）"
        df.loc[mask, "pass"] = False

    # --- 规则 7：行业上限（在通过的股票里限制单行业占比）---
    rule = rules.get("max_industry_weight", {})
    if rule.get("enabled", False):
        max_weight = float(rule.get("max_weight", 0.15))
        passed = df[df["pass"]].copy()
        if len(passed) > 0:
            counts = passed["industry_final"].value_counts()
            limit = int(len(passed) * max_weight)
            over = counts[counts > limit]
            if len(over) > 0:
                to_drop = []
                for ind_name in over.index:
                    members = passed[passed["industry_final"] == ind_name].copy()
                    # 保留成交额大的，剔除小的
                    members = members.sort_values("avg_amount", ascending=False)
                    to_drop.extend(members.index[limit:].tolist())
                if to_drop:
                    df.loc[to_drop, "pass"] = False
                    df.loc[to_drop, "reject_rule"] = "max_industry_weight"
                    df.loc[to_drop, "reject_reason"] = (
                        f"行业「{ind_name}」超出 {max_weight:.0%} 上限")

    return df


def _compute_metrics(daily, today: date) -> dict:
    """算单只股票的指标。"""
    if daily is None or len(daily) == 0:
        return {
            "avg_amount": np.nan,
            "listed_months": np.nan,
            "first_bar": "",
            "zero_volume_days": np.nan,
            "last_close": np.nan,
        }

    amount_col = "amount" if "amount" in daily.columns else "成交额"
    volume_col = "volume" if "volume" in daily.columns else "成交量"

    recent = daily.tail(20)
    avg_amount = (
        float(recent[amount_col].astype(float).mean())
        if amount_col in daily.columns else np.nan)

    first = daily.iloc[0]["date"]
    if hasattr(first, "date"):
        first = first.date()
    elif isinstance(first, str):
        first = datetime.strptime(first[:10], "%Y-%m-%d").date()
    months = (today.year - first.year) * 12 + (today.month - first.month)

    # 关键：如果最早日线就是窗口起点（如 3 年前），说明上市早于窗口，
    # 真实上市月数无法从日线判断。用上市日期字段（如果有）覆盖。
    # 不修的话，所有老股票都会显示「35 个月」，上市时长规则形同虚设。

    zero_days = (
        int((daily.tail(5)[volume_col].astype(float) == 0).sum())
        if volume_col in daily.columns else 0)

    return {
        "avg_amount": avg_amount,
        "listed_months": months,
        "first_bar": str(first),
        "zero_volume_days": zero_days,
        "last_close": float(daily.iloc[-1]["close"]) if "close" in daily.columns else np.nan,
    }


def run(
    config: dict,
    universe_mode: str = "all",
    limit: int | None = None,
    today: date | None = None,
) -> dict:
    started = time.time()
    today = today or date.today()

    ds = config.get("data_sources", {})
    retries = int(ds.get("retries", 3))
    backoff = float(ds.get("retry_backoff_seconds", 1.5))
    interval = float(ds.get("request_interval_seconds", 0.05))
    workers = int(ds.get("workers", 10))
    adjust = str(ds.get("adjust", "qfq"))
    rules = config.get("filters", {})

    # 1. 宇宙
    meta = build_universe(config, universe_mode)

    if limit:
        # 优先保留大市值，便于观察
        meta = meta.nlargest(limit, "market_cap_yi") if meta["market_cap_yi"].notna().any() else meta.head(limit)
        print(f"      【测试模式】只跑 {len(meta)} 只（按市值降序）")

    # 2. 日线
    print(f"[3/5] 拉日线（{len(meta)} 只，复权={adjust}）…")
    stocks = [
        {"code": r["code"], "name": r["name"], "exchange": r["exchange"]}
        for _, r in meta.iterrows()
    ]
    # **关键优化**：筛选只需要近期日线（20 日均成交额 + 停牌判断），
    # 不需要 3 年历史——上市时长用 list_date 字段判断，更准更快。
    # 拉 3 个月而非 3 年，数据量减少 90%，速度提升约 8 倍。
    lookback_days = int(rules.get("data_lookback_days", 90))
    from datetime import timedelta as _td
    end = today
    start = end - _td(days=lookback_days)

    def progress(idx, total, code):
        if idx % 200 == 0 or idx == total:
            print(f"      {idx}/{total}")

    # 用带缓存的拉取（首次全量，之后增量）
    from concurrent.futures import ThreadPoolExecutor, as_completed

    daily_map: dict = {}
    failed: list[str] = []
    done_count = 0

    def task(stock: dict):
        code = stock["code"]
        try:
            frame = cache.fetch_with_cache(
                code, stock["exchange"], start, end,
                fetch.fetch_daily, adjust=adjust)
            return code, frame, None
        except Exception as exc:  # noqa: BLE001
            return code, None, type(exc).__name__

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(task, s): s for s in stocks}
        for future in as_completed(futures):
            code, frame, err = future.result()
            done_count += 1
            if err:
                failed.append(code)
            elif frame is not None and len(frame) > 0:
                daily_map[code] = frame
            if progress and (done_count % 200 == 0 or done_count == len(stocks)):
                progress(done_count, len(stocks), code)

    print(f"      成功 {len(daily_map)}，失败 {len(failed)}")
    stats = cache.cache_stats()
    print(f"      日线缓存: {stats['files']} 个文件, {stats['size_mb']} MB")

    # 3. 应用规则
    print("[4/5] 应用规则…")
    result = apply_rules(meta, daily_map, rules, today)

    passed = result[result["pass"]]
    rejected = result[~result["pass"]]

    print(f"      通过 {len(passed)} / 剔除 {len(rejected)}"
          f"（剔除率 {len(rejected)/len(result)*100:.1f}%）")
    print()
    print("      各规则剔除数：")
    for rule_name, count in rejected["reject_rule"].value_counts().items():
        print(f"        {rule_name:28s} {count:5d} 只")

    # 4. 输出
    print()
    print("[5/5] 输出…")
    elapsed = time.time() - started

    out_cfg = config.get("output", {})
    out_dir = _REPO_ROOT / out_cfg.get("dir", "screening/output") / today.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    encoding = out_cfg.get("encoding", "utf-8-sig")

    # 候选
    cand_cols = [
        "code", "name", "board_code", "industry_final", "sw_industry",
        "market_cap_yi", "float_cap_yi", "avg_amount", "listed_months",
        "last_close", "price",
    ]
    cand_cols = [c for c in cand_cols if c in passed.columns]
    passed_out = passed[cand_cols].copy()
    passed_out["avg_amount_yi"] = (passed_out["avg_amount"] / 1e8).round(2)
    passed_out = passed_out.drop(columns=["avg_amount"])
    passed_out["analysis_date"] = today.isoformat()
    passed_out = passed_out.sort_values("market_cap_yi", ascending=False)
    passed_out.to_csv(out_dir / "candidates.csv", index=False, encoding=encoding)

    # 剔除
    rej_cols = ["code", "name", "board_code", "industry_final", "reject_rule",
                "reject_reason", "market_cap_yi", "avg_amount", "listed_months"]
    rej_cols = [c for c in rej_cols if c in rejected.columns]
    rejected_out = rejected[rej_cols].copy()
    if "avg_amount" in rejected_out.columns:
        rejected_out["avg_amount_yi"] = (rejected_out["avg_amount"] / 1e8).round(2)
        rejected_out = rejected_out.drop(columns=["avg_amount"])
    rejected_out["analysis_date"] = today.isoformat()
    rejected_out.to_csv(out_dir / "rejected.csv", index=False, encoding=encoding)

    # 行业分布
    ind_dist = passed["industry_final"].fillna("未知").value_counts()
    ind_dist.to_csv(out_dir / "industry_distribution.csv", encoding=encoding)

    # 审计
    audit = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "analysis_date": today.isoformat(),
        "universe_mode": universe_mode,
        "config_fingerprint": config_fingerprint(config),
        "config": config,
        "data_window": {"start": start.isoformat(), "end": end.isoformat()},
        "data_sources": {
            "daily_prices": ds.get("daily_prices", "tencent"),
            "adjust": adjust,
            "daily_success": len(daily_map),
            "daily_failed": len(stocks) - len(daily_map),
        },
        "summary": {
            "universe": len(result),
            "passed": len(passed),
            "rejected": len(rejected),
            "reject_rate": round(len(rejected) / len(result), 4) if len(result) else 0,
            "by_rule": rejected["reject_rule"].value_counts().to_dict(),
            "by_industry": ind_dist.head(15).to_dict(),
        },
        "elapsed_seconds": round(elapsed, 1),
    }
    (out_dir / "audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")

    print(f"      输出目录: {out_dir}")
    print(f"      候选: {len(passed_out)} 只 -> candidates.csv")
    print(f"      剔除: {len(rejected_out)} 只 -> rejected.csv")
    print(f"      行业分布 -> industry_distribution.csv")
    print(f"      审计: audit.json（指纹 {audit['config_fingerprint']}）")
    print(f"      耗时 {elapsed:.1f}s")

    return audit


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="全市场筛选")
    parser.add_argument("--config", default=str(_REPO_ROOT / "screening" / "config.yaml"))
    parser.add_argument("--universe", default="all", choices=["all", "index"])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--date", default=None)
    args = parser.parse_args(argv[1:])

    logging.basicConfig(level=logging.WARNING)
    config = load_config(Path(args.config))
    today = date.fromisoformat(args.date) if args.date else None

    try:
        run(config, universe_mode=args.universe, limit=args.limit, today=today)
    except Exception as exc:  # noqa: BLE001
        print(f"\n失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
