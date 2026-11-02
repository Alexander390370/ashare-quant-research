"""日线本地缓存：避免重复拉取。

问题
----
单只日线约 2.7 秒（网络延迟主导，并发提升有限）。
全市场 5223 只 = 约 30 分钟。每次筛选都重拉是巨大浪费。

方案
----
本地 parquet 缓存，按股票代码分文件：
    run_data/cache/daily/<code>.parquet

增量逻辑：
- 缓存存在且覆盖所需区间 -> 直接用
- 缓存存在但缺最新数据 -> 只拉缺失部分，追加
- 缓存不存在 -> 全量拉

效果：第一次 30 分钟，之后增量更新约 1-2 分钟。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
DAILY_CACHE_DIR = _REPO_ROOT / "run_data" / "cache" / "daily"

# 缓存过期：超过这么多天没更新就重拉
STALE_DAYS = 1


def _cache_path(code: str) -> Path:
    DAILY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return DAILY_CACHE_DIR / f"{code}.parquet"


def load_cached(code: str) -> pd.DataFrame | None:
    """读单只股票的日线缓存。"""
    path = _cache_path(code)
    if not path.exists():
        return None
    try:
        return pd.read_parquet(path)
    except Exception:  # noqa: BLE001
        return None


def save_cached(code: str, frame: pd.DataFrame) -> None:
    """写缓存。"""
    if frame is None or len(frame) == 0:
        return
    try:
        _cache_path(code).parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(_cache_path(code), index=False)
    except Exception as exc:  # noqa: BLE001
        logger.debug("缓存写入失败 %s: %s", code, exc)


def _frame_dates(frame: pd.DataFrame) -> tuple[date | None, date | None]:
    """取 DataFrame 的日期范围。"""
    if frame is None or len(frame) == 0 or "date" not in frame.columns:
        return None, None
    dates = pd.to_datetime(frame["date"])
    return dates.min().date(), dates.max().date()


def fetch_with_cache(
    code: str,
    exchange: str,
    start: date,
    end: date,
    fetch_func,
    adjust: str = "qfq",
) -> pd.DataFrame | None:
    """带缓存的日线获取。

    参数
    ----
    fetch_func: 原始的 fetch_daily 函数
    """
    cached = load_cached(code)

    if cached is not None and len(cached) > 0:
        cache_start, cache_end = _frame_dates(cached)
        if cache_start is not None and cache_end is not None:
            # 缓存覆盖所需区间，且不太旧
            covers_start = cache_start <= start + timedelta(days=10)
            covers_end = cache_end >= end - timedelta(days=STALE_DAYS)
            if covers_start and covers_end:
                return cached

            # 缓存缺最新数据 -> 只拉缺失部分
            if cache_start <= start + timedelta(days=10) and cache_end < end:
                try:
                    extra = fetch_func(
                        code, exchange, cache_end + timedelta(days=1), end,
                        source="tencent", adjust=adjust)
                    if extra is not None and len(extra) > 0:
                        combined = pd.concat([cached, extra], ignore_index=True)
                        combined = combined.drop_duplicates(subset=["date"], keep="last")
                        combined = combined.sort_values("date").reset_index(drop=True)
                        save_cached(code, combined)
                        return combined
                except Exception:  # noqa: BLE001
                    pass

    # 全量拉
    try:
        frame = fetch_func(code, exchange, start, end, source="tencent", adjust=adjust)
        if frame is not None and len(frame) > 0:
            save_cached(code, frame)
        return frame
    except Exception as exc:  # noqa: BLE001
        # 拉失败但缓存有旧数据 -> 用旧的
        if cached is not None and len(cached) > 0:
            logger.debug("%s 拉取失败，用缓存", code)
            return cached
        raise exc


def cache_stats() -> dict:
    """缓存统计。"""
    if not DAILY_CACHE_DIR.exists():
        return {"files": 0, "size_mb": 0}
    files = list(DAILY_CACHE_DIR.glob("*.parquet"))
    total = sum(f.stat().st_size for f in files)
    return {
        "files": len(files),
        "size_mb": round(total / 1e6, 1),
        "dir": str(DAILY_CACHE_DIR),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(cache_stats())
