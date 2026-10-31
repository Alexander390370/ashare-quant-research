"""把 A 股数据灌进 Qlib 格式。

Qlib 的原生数据格式
-------------------
Qlib 用二进制 `.bin` 存储，目录结构：

    <provider_uri>/
    ├── calendars/day.txt          交易日历（每行一个日期）
    ├── instruments/all.txt        股票列表（每行：代码\\t起始日期\\t结束日期）
    └── features/<股票>/<字段>.day.bin   每只股票每个字段一个文件

`.bin` 文件是 float32 数组，索引对应 calendar 里的行号。

为什么不直接调 dump_bin.py
--------------------------
Qlib 把 `dump_bin.py` 放在 GitHub 仓库的 `scripts/` 目录，**pip 安装不包含**。
所以这里直接用 `FileFeatureStorage` 写入，效果等价。

设计取舍
--------
- **只灌必要字段**：open/high/low/close/volume/amount。Alpha158 需要的都在里面。
- **不复权**：腾讯日线本身未复权。若要复权，需另算因子（见已知限制）。
- **数据源**：腾讯（实测 8/8 稳定）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# 要灌的字段（Qlib 里叫 feature name）
FIELDS = ["open", "high", "low", "close", "volume", "amount"]


def _normalize_code(code: str, exchange: str = "") -> str:
    """Qlib 用大写 + 交易所后缀：600519 -> SH600519。"""
    prefix = "SH"
    if exchange:
        if "深圳" in exchange:
            prefix = "SZ"
        elif "北京" in exchange:
            prefix = "BJ"
    else:
        # 按代码前缀猜
        if code.startswith(("0", "3")):
            prefix = "SZ"
        elif code.startswith(("4", "8")):
            prefix = "BJ"
    return f"{prefix}{code}"


def build_calendar(daily_map: dict[str, Any]) -> list[str]:
    """从所有股票日线的日期并集构造交易日历。"""
    dates: set[str] = set()
    for frame in daily_map.values():
        if frame is None or len(frame) == 0:
            continue
        for value in frame["date"]:
            if hasattr(value, "strftime"):
                dates.add(value.strftime("%Y-%m-%d"))
            else:
                dates.add(str(value)[:10])
    return sorted(dates)


def dump_to_qlib(
    daily_map: dict[str, Any],
    stock_meta: dict[str, dict],
    provider_uri: Path,
    verbose: bool = True,
) -> dict:
    """把日线数据写成 Qlib 格式。

    参数
    ----
    daily_map:
        {code: DataFrame}，DataFrame 需有 date/open/high/low/close/volume/amount
    stock_meta:
        {code: {name, exchange}}，用于生成 Qlib 代码
    provider_uri:
        输出目录，如 ~/.qlib/qlib_data/cn_custom

    返回
    ----
    dict: 统计信息
    """
    provider_uri = Path(provider_uri)
    calendars_dir = provider_uri / "calendars"
    instruments_dir = provider_uri / "instruments"
    features_dir = provider_uri / "features"
    for d in (calendars_dir, instruments_dir, features_dir):
        d.mkdir(parents=True, exist_ok=True)

    # ---------- 1. 交易日历 ----------
    calendar = build_calendar(daily_map)
    if not calendar:
        raise ValueError("没有任何日线数据，无法构造日历")
    (calendars_dir / "day.txt").write_text(
        "\n".join(calendar) + "\n", encoding="utf-8")
    logger.info("日历：%d 个交易日（%s ~ %s）", len(calendar), calendar[0], calendar[-1])

    date_to_idx = {d: i for i, d in enumerate(calendar)}

    # ---------- 2. 股票列表 ----------
    instrument_lines = []
    dumped = 0
    skipped = 0

    for code, frame in daily_map.items():
        if frame is None or len(frame) == 0:
            skipped += 1
            continue

        meta = stock_meta.get(code, {})
        qlib_code = _normalize_code(code, meta.get("exchange", ""))
        stock_dir = features_dir / qlib_code
        stock_dir.mkdir(parents=True, exist_ok=True)

        # 该股票有数据的日期范围
        dates = []
        for value in frame["date"]:
            if hasattr(value, "strftime"):
                dates.append(value.strftime("%Y-%m-%d"))
            else:
                dates.append(str(value)[:10])
        if not dates:
            skipped += 1
            continue

        start_idx = date_to_idx.get(min(dates))
        end_idx = date_to_idx.get(max(dates))
        if start_idx is None or end_idx is None:
            skipped += 1
            continue

        instrument_lines.append(f"{qlib_code}\t{calendar[start_idx]}\t{calendar[end_idx]}")

        # ---------- 3. 每个字段一个 .bin ----------
        # Qlib 的 .bin 格式：第一个 float32 是起始索引，后面才是数据
        # 源码见 qlib/data/storage/file_storage.py:FileFeatureStorage.write
        #   np.hstack([index, data_array]).astype("<f").tofile(fp)
        n = len(calendar)
        for field in FIELDS:
            if field not in frame.columns:
                continue
            arr = np.full(n, np.nan, dtype=np.float32)
            for _, row in frame.iterrows():
                value = row["date"]
                key = value.strftime("%Y-%m-%d") if hasattr(value, "strftime") else str(value)[:10]
                idx = date_to_idx.get(key)
                if idx is None:
                    continue
                try:
                    arr[idx] = float(row[field])
                except (TypeError, ValueError):
                    continue
            # 关键：前面加起始索引 0，与 Qlib 格式一致
            payload = np.hstack([np.array([0], dtype=np.float32), arr]).astype("<f")
            (stock_dir / f"{field}.day.bin").write_bytes(payload.tobytes())

        dumped += 1
        if verbose and dumped % 100 == 0:
            logger.info("已写入 %d 只", dumped)

    # ---------- 4. instruments 文件 ----------
    (instruments_dir / "all.txt").write_text(
        "\n".join(instrument_lines) + "\n", encoding="utf-8")

    stats = {
        "provider_uri": str(provider_uri),
        "calendar_days": len(calendar),
        "calendar_start": calendar[0],
        "calendar_end": calendar[-1],
        "stocks_dumped": dumped,
        "stocks_skipped": skipped,
        "fields": FIELDS,
    }
    logger.info("写入完成：%d 只股票，%d 个交易日", dumped, len(calendar))
    return stats


def verify_qlib_data(provider_uri: Path) -> dict:
    """验证写出的数据 Qlib 能读。"""
    import qlib
    from qlib.data import D

    provider_uri = Path(provider_uri)
    qlib.init(provider_uri=str(provider_uri), region="cn")

    calendar = D.calendar(start_time="2010-01-01", end_time="2030-12-31")
    # 取一只票的收盘价验证
    frame = D.features(
        ["SH600519"], ["$close"], start_time=str(calendar[0]), end_time=str(calendar[-1]))

    return {
        "qlib_calendar_days": len(calendar),
        "qlib_calendar_start": str(calendar[0]),
        "qlib_calendar_end": str(calendar[-1]),
        "sample_rows": 0 if frame is None else len(frame),
        "sample_last_close": (
            None if frame is None or len(frame) == 0
            else float(frame["$close"].dropna().iloc[-1])
        ),
    }
