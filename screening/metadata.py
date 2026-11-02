"""股票元数据：行业、市值、板块、上市日期。

数据源实测（2026-10-31，每项 4 次）
-----------------------------------
    ✅ 深交所列表      4/4  2905 行  含「所属行业」「A股总股本」「A股流通股本」
    ✅ 上交所主板      4/4  1700 行  含上市日期
    ✅ 上交所科创板    3/5  618 行
    ✅ 全A代码名称     4/4  5577 行
    ⚠️ 新浪全A快照     3/4  5576 行  含最新价（算市值用）
    ❌ 东财行业板块    0/8  已弃用

设计
----
- 交易所列表给「行业 + 股本」，新浪快照给「价格」，两者相乘得市值
- 全部带重试 + 缓存（元数据变动慢，缓存 1 天）
- 缓存存 parquet（比 CSV 快 10 倍）
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = _REPO_ROOT / "run_data" / "cache" / "metadata"
CACHE_TTL_HOURS = 24

# 交易所前缀
_PREFIX = {"上海证券交易所": "SH", "深圳证券交易所": "SZ", "北京证券交易所": "BJ"}


class MetadataError(RuntimeError):
    """元数据获取失败。"""


def _retry(func, *args, retries=3, backoff=1.5, label="", **kwargs) -> Any:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt >= retries:
                break
            time.sleep(backoff * attempt)
    raise MetadataError(f"{label} 失败（{retries} 次）：{type(last).__name__}: {last}")


def _cache_path(name: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{name}.parquet"


def _load_cache(name: str) -> pd.DataFrame | None:
    path = _cache_path(name)
    if not path.exists():
        return None
    age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    if age > timedelta(hours=CACHE_TTL_HOURS):
        logger.info("缓存过期（%.1f 小时）：%s", age.total_seconds() / 3600, name)
        return None
    try:
        return pd.read_parquet(path)
    except Exception:  # noqa: BLE001
        return None


def _save_cache(name: str, frame: pd.DataFrame) -> None:
    try:
        frame.to_parquet(_cache_path(name), index=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("缓存写入失败 %s: %s", name, exc)


def _parse_number(series: pd.Series) -> pd.Series:
    """解析数字，兼容带千分位逗号的字符串。

    实测坑（2026-10-31）：深交所「A股总股本」返回的是字符串
    `"19,405,918,198"`（带千分位逗号），`pd.to_numeric` 直接返回 NaN。
    必须先去掉逗号再转。
    """
    cleaned = (
        series.astype(str)
        .str.replace(",", "", regex=False)
        .str.replace(" ", "", regex=False)
        .str.strip()
    )
    cleaned = cleaned.replace({"": None, "nan": None, "None": None, "--": None})
    return pd.to_numeric(cleaned, errors="coerce")


# ---------------------------------------------------------------------------
# 1. 交易所列表（行业 + 股本 + 上市日期）
# ---------------------------------------------------------------------------
def fetch_exchange_listings(use_cache: bool = True) -> pd.DataFrame:
    """拉深交所 + 上交所股票列表，返回统一格式。

    返回列：code, name, exchange, board, industry, total_share, float_share, list_date

    实测：深交所列表含「所属行业」「A股总股本」，这是行业分类的免费来源。
    注意股本字段是带千分位逗号的字符串，必须用 _parse_number 处理。
    """
    if use_cache:
        cached = _load_cache("exchange_listings")
        if cached is not None:
            logger.info("使用缓存：%d 行", len(cached))
            return cached

    import akshare as ak

    frames: list[pd.DataFrame] = []

    # --- 深交所（含行业）---
    try:
        sz = _retry(ak.stock_info_sz_name_code, label="深交所列表")
        if sz is not None and len(sz) > 0:
            normalized = pd.DataFrame({
                "code": sz["A股代码"].astype(str).str.strip(),
                "name": sz["A股简称"].astype(str).str.strip(),
                "exchange": "深圳证券交易所",
                "board": sz.get("板块", pd.Series([""] * len(sz))).astype(str),
                "industry": sz.get("所属行业", pd.Series([""] * len(sz))).astype(str),
                "total_share": _parse_number(sz["A股总股本"])
                if "A股总股本" in sz.columns else np.nan,
                "float_share": _parse_number(sz["A股流通股本"])
                if "A股流通股本" in sz.columns else np.nan,
                "list_date": sz.get("A股上市日期", pd.Series([""] * len(sz))).astype(str),
            })
            frames.append(normalized)
            logger.info("深交所：%d 行（股本非空 %d）",
                        len(normalized), int(normalized["total_share"].notna().sum()))
    except MetadataError as exc:
        logger.warning("深交所列表失败：%s", str(exc)[:80])

    # --- 上交所（无行业，无股本）---
    for board_name in ["主板A股", "科创板"]:
        try:
            sh = _retry(
                ak.stock_info_sh_name_code, symbol=board_name,
                label=f"上交所{board_name}")
            if sh is not None and len(sh) > 0:
                normalized = pd.DataFrame({
                    "code": sh["证券代码"].astype(str).str.strip(),
                    "name": sh["证券简称"].astype(str).str.strip(),
                    "exchange": "上海证券交易所",
                    "board": board_name,
                    "industry": "",
                    "total_share": np.nan,
                    "float_share": np.nan,
                    "list_date": sh.get("上市日期", pd.Series([""] * len(sh))).astype(str),
                })
                frames.append(normalized)
                logger.info("上交所%s：%d 行", board_name, len(normalized))
        except MetadataError as exc:
            logger.warning("上交所%s失败：%s", board_name, str(exc)[:60])

    if not frames:
        raise MetadataError("所有交易所列表都失败")

    result = pd.concat(frames, ignore_index=True)
    result = result[result["code"].str.len() == 6]
    result = result.drop_duplicates(subset=["code"], keep="first")

    _save_cache("exchange_listings", result)
    logger.info("合计：%d 只（含行业 %d 只，含股本 %d 只）",
                len(result),
                int((result["industry"] != "").sum()),
                int(result["total_share"].notna().sum()))
    return result


# ---------------------------------------------------------------------------
# 2. 实时快照（价格 → 算市值）
# ---------------------------------------------------------------------------
def fetch_snapshot(use_cache: bool = True) -> pd.DataFrame:
    """拉全 A 快照，返回 code / price / pct_chg。

    实测：新浪 stock_zh_a_spot 3/4（偶发失败，需重试）
    """
    if use_cache:
        cached = _load_cache("snapshot")
        if cached is not None:
            return cached

    import akshare as ak

    frame = _retry(ak.stock_zh_a_spot, retries=4, label="新浪全A快照")
    if frame is None or len(frame) == 0:
        raise MetadataError("快照为空")

    result = pd.DataFrame({
        "code": frame["代码"].astype(str).str.replace(r"^(sh|sz|bj)", "", regex=True).str.strip(),
        "price": pd.to_numeric(frame.get("最新价"), errors="coerce"),
        "pct_chg": pd.to_numeric(frame.get("涨跌幅"), errors="coerce"),
    })
    result = result[result["code"].str.len() == 6]
    _save_cache("snapshot", result)
    logger.info("快照：%d 行", len(result))
    return result


# ---------------------------------------------------------------------------
# 3. 合并成完整元数据
# ---------------------------------------------------------------------------
def build_metadata(use_cache: bool = True) -> pd.DataFrame:
    """合并交易所列表 + 快照，算出市值。

    返回列：
        code, name, exchange, board, industry,
        price, total_share, float_share, list_date,
        market_cap_yi（总市值，亿元）, float_cap_yi（流通市值，亿元）

    市值单位说明
    ------------
    深交所「A股总股本」单位是**股**，价格是**元/股**。
    市值（亿元）= 股本 × 价格 / 1e8
    """
    listings = fetch_exchange_listings(use_cache=use_cache)

    try:
        snapshot = fetch_snapshot(use_cache=use_cache)
        merged = listings.merge(snapshot, on="code", how="left")
    except MetadataError as exc:
        logger.warning("快照失败，市值将为空：%s", str(exc)[:60])
        merged = listings.copy()
        merged["price"] = np.nan
        merged["pct_chg"] = np.nan

    # 算市值（亿元）
    merged["market_cap_yi"] = (
        merged["total_share"] * merged["price"] / 1e8).round(2)
    merged["float_cap_yi"] = (
        merged["float_share"] * merged["price"] / 1e8).round(2)

    return merged


# ---------------------------------------------------------------------------
# 4. 板块判断（从代码前缀，比交易所列表更可靠）
# ---------------------------------------------------------------------------
def board_from_code(code: str) -> str:
    """从代码前缀判断板块。"""
    code = str(code).strip()
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith(("300", "301")):
        return "创业板"
    if code.startswith(("4", "8")):
        return "北交所"
    if code.startswith("6"):
        return "沪主板"
    if code.startswith(("0", "3")):
        return "深主板"
    return "未知"


def limit_pct_from_code(code: str, is_st: bool = False) -> float:
    """涨跌停幅度（与回测共用同一套规则）。"""
    if is_st:
        return 0.05
    board = board_from_code(code)
    if board in ("科创板", "创业板"):
        return 0.20
    if board == "北交所":
        return 0.30
    return 0.10


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    meta = build_metadata(use_cache=False)
    print()
    print(f"总股票数: {len(meta)}")
    print(f"有行业: {int((meta['industry'].fillna('') != '').sum())}")
    print(f"有市值: {int(meta['market_cap_yi'].notna().sum())}")
    print()
    print("行业分布（前 15）:")
    print(meta["industry"].value_counts().head(15).to_string())
    print()
    print("板块分布:")
    print(meta["board"].value_counts().to_string())
    print()
    print("样例:")
    print(meta[["code", "name", "board", "industry", "market_cap_yi"]].head(8).to_string())
