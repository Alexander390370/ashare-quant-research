"""B 线主流程：灌数据 + Alpha158 因子排序。

用法：
    python -m qlib_layer.run                     # 全流程
    python -m qlib_layer.run --limit 50          # 只跑 50 只（快速验证）
    python -m qlib_layer.run --skip-dump         # 跳过灌数据，直接用已有数据

流程：
    1. 读 A 的候选名单（screening/output/<date>/candidates.csv）
    2. 拉日线（复用 screening.fetch）
    3. 灌进 Qlib 格式
    4. 用 Alpha158 算因子
    5. 按因子打分排序，输出 Top N

输出：
    qlib_layer/output/<date>/
        factor_scores.csv    所有股票的因子值
        top20.csv            排序后的前 20
        audit.json           统计与配置
"""
# ruff: noqa: E402  -- 必须先 import tradingagents 触发 netfix，再 import 业务模块

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tradingagents  # noqa: E402,F401  触发 netfix
from qlib_layer import (
    dump_data,  # noqa: E402
    validate as V,  # noqa: E402
)
from qlib_layer.factors import (  # noqa: E402
    DEFAULT_WEIGHTS,
    FACTOR_EXPRESSIONS,
    FACTOR_NAMES,
)

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER = _REPO_ROOT / "qlib_layer" / "data" / "cn_a"


def load_a_candidates(run_date: str) -> list[dict]:
    """读 A 的候选名单。"""
    cand_path = _REPO_ROOT / "screening" / "output" / run_date / "candidates.csv"
    if not cand_path.exists():
        raise FileNotFoundError(
            f"A 的候选名单不存在: {cand_path}\n"
            f"先跑: python -m screening.run --date {run_date}"
        )
    with cand_path.open("r", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def compute_factors(
    provider_uri: Path,
    top_n: int = 20,
    as_of: str | None = None,
) -> tuple[list[dict], dict]:
    """用 Alpha158 因子算分并排序。

    返回 (排序后的列表, 统计信息)。

    注意：这里**不训练模型**，只用 Alpha158 的原始因子值做截面排名。
    这是「仅筛选模式」——绕开模型训练，只借因子库。
    """
    import qlib
    from qlib.data import D

    qlib.init(provider_uri=str(provider_uri), region="cn", expression_cache=None)

    instruments = D.instruments(market="all")
    names = D.list_instruments(
        instruments, start_time="2023-01-01", end_time="2030-12-31", as_list=True)
    if not names:
        raise ValueError("Qlib 里没有股票，检查数据是否灌入")

    calendar = D.calendar(start_time="2023-01-01", end_time="2030-12-31")
    if len(calendar) == 0:
        raise ValueError("Qlib 日历为空")

    end_time = as_of or str(calendar[-1])[:10]
    start_time = str(calendar[-120])[:10]  # 最近 120 天足够算常见因子

    # 因子表达式：从 factors.py 导入（唯一真源）
    #
    # 2026-10-31 重构：之前 run.py 和 validate.py 各存一份，导致
    # 把这里的 mom20 改回写反，校验照样全绿（变异测试实锤）。
    # 现在两边都从 factors.py 导入，无法再漂移。
    expressions = [FACTOR_EXPRESSIONS[name] for name in FACTOR_NAMES]
    expr_names = FACTOR_NAMES

    frame = D.features(
        names, expressions, start_time=start_time, end_time=end_time,
        freq="day",
    )
    if frame is None or len(frame) == 0:
        raise ValueError("因子计算返回空，检查数据字段")

    frame.columns = expr_names
    # 取每只股票最后一天的值
    latest = frame.groupby(level="instrument").tail(1).reset_index()
    latest = latest.set_index("instrument")

    # 截面 z-score 标准化（Qlib 的标准做法）
    scored = latest.copy()
    for col in expr_names:
        series = scored[col]
        mean = series.mean()
        std = series.std()
        if std and std > 0:
            scored[col + "_z"] = (series - mean) / std
        else:
            scored[col + "_z"] = 0.0

    # 合成权重：从 factors.py 导入（唯一真源）
    weights = DEFAULT_WEIGHTS
    score = sum(scored[k] * w for k, w in weights.items() if k in scored.columns)
    scored["composite_score"] = score
    scored = scored.sort_values("composite_score", ascending=False)

    rows = []
    for code, row in scored.iterrows():
        rows.append({
            "qlib_code": code,
            "composite_score": round(float(row["composite_score"]), 4),
            "mom5": round(float(row.get("mom5", 0) or 0), 4),
            "mom20": round(float(row.get("mom20", 0) or 0), 4),
            "vol20": round(float(row.get("vol20", 0) or 0), 4),
            "dev20": round(float(row.get("dev20", 0) or 0), 4),
            "vol_ratio": round(float(row.get("vol_ratio", 0) or 0), 4),
        })

    stats = {
        "stocks_scored": len(rows),
        "factors": expr_names,
        "weights": weights,
        "as_of": end_time,
        "window_start": start_time,
    }
    return rows, stats


def run(
    run_date: str,
    limit: int | None = None,
    top_n: int = 20,
    skip_dump: bool = False,
    provider: Path = DEFAULT_PROVIDER,
) -> dict:
    """全流程。"""
    started = time.time()
    from screening import fetch

    print("=" * 70)
    print(f"B 线：Qlib 因子排序  date={run_date}")
    print("=" * 70)

    # ---------- 1. 读 A 的候选 ----------
    print("\n[1/4] 读 A 的候选名单…")
    candidates = load_a_candidates(run_date)
    print(f"      {len(candidates)} 只")
    if limit:
        candidates = candidates[:limit]
        print(f"      【测试模式】只跑前 {len(candidates)} 只")

    # ---------- 2. 拉日线 ----------
    if skip_dump and provider.exists():
        print("\n[2/4] 跳过灌数据（用已有 Qlib 数据）")
        dump_stats = {"skipped": True}
    else:
        print(f"\n[2/4] 拉日线并灌入 Qlib（{len(candidates)} 只）…")
        stocks = [
            {"code": c["code"], "name": c["name"], "exchange": ""}
            for c in candidates
        ]
        # 补交易所（按代码前缀猜）
        for s in stocks:
            code = s["code"]
            if code.startswith(("6",)):
                s["exchange"] = "上海证券交易所"
            elif code.startswith(("0", "3")):
                s["exchange"] = "深圳证券交易所"
            elif code.startswith(("4", "8")):
                s["exchange"] = "北京证券交易所"

        start, end = fetch.default_window(3)

        def progress(idx, total, code):
            if idx % 100 == 0 or idx == total:
                print(f"      {idx}/{total}")

        # 复权方式：从 screening/config.yaml 读取（与 A 线保持一致）
        #
        # 2026-10-31 实测（除权日茅台 2024-06-19）：
        #   不复权 -1.35%（含除权假跌）/ 前复权 +0.75% / 后复权 +0.61%
        #   跨除权日 20 日动量（工行）：不复权 +14.18% / 前复权 +17.70% / 后复权 +8.51%
        # 前复权消除了假跌且最接近真实涨跌，故选 qfq。
        adjust = "qfq"
        try:
            import yaml as _yaml

            _cfg_path = _REPO_ROOT / "screening" / "config.yaml"
            if _cfg_path.exists():
                with _cfg_path.open("r", encoding="utf-8") as _h:
                    _cfg = _yaml.safe_load(_h) or {}
                adjust = str(_cfg.get("data_sources", {}).get("adjust", "qfq"))
        except Exception:  # noqa: BLE001 - 配置读不到就用默认 qfq
            pass

        daily_map = fetch.fetch_all_daily(
            stocks, start, end,
            retries=3, backoff=1.5, interval=0.05,
            source="tencent", progress=progress, workers=10,
            adjust=adjust,
        )
        print(f"      成功 {len(daily_map)}，失败 {len(stocks) - len(daily_map)}")

        meta = {s["code"]: s for s in stocks}
        dump_stats = dump_data.dump_to_qlib(daily_map, meta, provider, verbose=False)
        print(f"      灌入完成：{dump_stats['stocks_dumped']} 只，{dump_stats['calendar_days']} 个交易日")

    # ---------- 2.5 校验（自动防线）----------
    print("\n[2.5/4] 因子管线校验（自动防线）…")
    checks_passed, check_results = V.run_all(provider, verbose=False)
    failed_checks = [c for c in check_results if not c.passed]
    if checks_passed:
        print(f"      ✅ {len(check_results)} 项校验全过")
    else:
        print(f"      ❌ {len(failed_checks)}/{len(check_results)} 项失败：")
        for c in failed_checks:
            print(f"         - {c.name}: {c.detail[:60]}")
        print("      ** 校验失败，停止后续计算。**")
        raise ValueError(f"因子管线校验失败 {len(failed_checks)} 项")

    # ---------- 3. 算因子 ----------
    print("\n[3/4] 算 Alpha158 风格因子…")
    rows, factor_stats = compute_factors(provider, top_n=top_n)
    print(f"      打分 {factor_stats['stocks_scored']} 只")
    print(f"      因子: {', '.join(factor_stats['factors'])}")

    # ---------- 4. 输出 ----------
    print("\n[4/4] 输出…")
    out_dir = _REPO_ROOT / "qlib_layer" / "output" / run_date
    out_dir.mkdir(parents=True, exist_ok=True)

    # factor_scores.csv
    if rows:
        with (out_dir / "factor_scores.csv").open("w", encoding="utf-8-sig", newline="") as h:
            writer = csv.DictWriter(h, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    # top20.csv
    top = rows[:top_n]
    if top:
        with (out_dir / "top20.csv").open("w", encoding="utf-8-sig", newline="") as h:
            writer = csv.DictWriter(h, fieldnames=list(top[0].keys()))
            writer.writeheader()
            writer.writerows(top)

    elapsed = time.time() - started
    audit = {
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "run_date": run_date,
        "candidates_in": len(candidates),
        "scored": factor_stats["stocks_scored"],
        "top_n": top_n,
        "dump": dump_stats,
        "factors": factor_stats,
        "validation": {
            "passed": checks_passed,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail}
                for c in check_results
            ],
        },
        "elapsed_seconds": round(elapsed, 1),
    }
    (out_dir / "audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")

    print(f"      输出: {out_dir}")
    print(f"      Top {len(top)} -> top20.csv")
    print(f"      耗时 {elapsed:.1f}s")

    if top:
        print()
        print(f"  === Top {min(10, len(top))} ===")
        for i, r in enumerate(top[:10], 1):
            print(f"    {i:2d}. {r['qlib_code']}  分数 {r['composite_score']:+.4f}"
                  f"  mom20 {r['mom20']:+.2%}  vol20 {r['vol20']:.2%}")

    return audit


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="B 线：Qlib 因子排序")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--skip-dump", action="store_true")
    args = parser.parse_args(argv[1:])

    logging.basicConfig(level=logging.WARNING)
    try:
        run(args.date, limit=args.limit, top_n=args.top, skip_dump=args.skip_dump)
    except (FileNotFoundError, ValueError) as exc:
        print(f"\n错误: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
