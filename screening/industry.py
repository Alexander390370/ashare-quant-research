"""申万行业分类映射（个股 → 行业）。

为什么需要
----------
深交所列表自带「所属行业」，但**上交所列表没有行业字段**（实测确认）。
所以上交所的 2318 只股票缺行业，无法做行业中性化。

数据源实测（2026-10-31，每项 3 次）
-----------------------------------
    ✅ 申万一级行业列表        3/3   31 行（行业代码 + 名称）
    ✅ 申万行业成分股          3/3   如 801080 电子 -> 497 行
    ❌ 东财行业成分            0/3   ProxyError
    ❌ 巨潮行业分类            0/3   SSLError
    ✅ 同花顺行业汇总          3/3   90 行（但只有板块汇总，无个股映射）

方案
----
1. 拉 31 个申万一级行业列表
2. 逐个拉成分股（31 次请求，每次约 1-2 秒）
3. 拼成 code -> 行业名 的映射
4. 缓存 24 小时

这样全市场 5000+ 只都能拿到申万一级行业。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = _REPO_ROOT / "run_data" / "cache" / "metadata"
CACHE_TTL_HOURS = 24


class IndustryError(RuntimeError):
    """行业数据获取失败。"""


def _retry(func, *args, retries=3, backoff=1.5, label="", **kwargs):
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt >= retries:
                break
            time.sleep(backoff * attempt)
    raise IndustryError(f"{label} 失败：{type(last).__name__}: {last}")


def fetch_sw_industries() -> pd.DataFrame:
    """获取申万一级行业列表。

    实测坑（2026-10-31）：
    `ak.sw_index_first_info()` 依赖 legulegu.com，实测 SSLEOFError 不可用。
    但 `ak.index_component_sw()`（申万官网）是 3/3 稳定的。

    所以：优先尝试 API，失败则回退到硬编码的 31 个一级行业代码
    （申万一级行业自 2021 版以来稳定，代码不会变）。
    """
    import akshare as ak

    try:
        frame = _retry(ak.sw_index_first_info, retries=2, label="申万一级行业列表")
        if frame is not None and len(frame) > 0:
            return pd.DataFrame({
                "industry_code": frame["行业代码"].astype(str)
                .str.replace(".SI", "", regex=False).str.strip(),
                "industry_name": frame["行业名称"].astype(str).str.strip(),
            })
    except IndustryError as exc:
        logger.warning("申万行业列表 API 失败，用内置清单：%s", str(exc)[:60])

    # 回退：申万一级行业 2021 版（31 个，代码稳定）
    return pd.DataFrame(SW_LEVEL1_BUILTIN)


# 申万一级行业 2021 版（31 个）
SW_LEVEL1_BUILTIN = [
    {"industry_code": "801010", "industry_name": "农林牧渔"},
    {"industry_code": "801030", "industry_name": "基础化工"},
    {"industry_code": "801040", "industry_name": "钢铁"},
    {"industry_code": "801050", "industry_name": "有色金属"},
    {"industry_code": "801080", "industry_name": "电子"},
    {"industry_code": "801110", "industry_name": "家用电器"},
    {"industry_code": "801120", "industry_name": "食品饮料"},
    {"industry_code": "801130", "industry_name": "纺织服饰"},
    {"industry_code": "801140", "industry_name": "轻工制造"},
    {"industry_code": "801150", "industry_name": "医药生物"},
    {"industry_code": "801160", "industry_name": "公用事业"},
    {"industry_code": "801170", "industry_name": "交通运输"},
    {"industry_code": "801180", "industry_name": "房地产"},
    {"industry_code": "801200", "industry_name": "商贸零售"},
    {"industry_code": "801210", "industry_name": "社会服务"},
    {"industry_code": "801230", "industry_name": "综合"},
    {"industry_code": "801710", "industry_name": "建筑材料"},
    {"industry_code": "801720", "industry_name": "建筑装饰"},
    {"industry_code": "801730", "industry_name": "电力设备"},
    {"industry_code": "801740", "industry_name": "国防军工"},
    {"industry_code": "801750", "industry_name": "计算机"},
    {"industry_code": "801760", "industry_name": "传媒"},
    {"industry_code": "801770", "industry_name": "通信"},
    {"industry_code": "801780", "industry_name": "银行"},
    {"industry_code": "801790", "industry_name": "非银金融"},
    {"industry_code": "801880", "industry_name": "汽车"},
    {"industry_code": "801890", "industry_name": "机械设备"},
    {"industry_code": "801950", "industry_name": "煤炭"},
    {"industry_code": "801960", "industry_name": "石油石化"},
    {"industry_code": "801970", "industry_name": "环保"},
    {"industry_code": "801980", "industry_name": "美容护理"},
]


def fetch_sw_members(industry_code: str, industry_name: str) -> pd.DataFrame:
    """拉某个申万行业的成分股。"""
    import akshare as ak

    frame = _retry(
        ak.index_component_sw, symbol=industry_code,
        label=f"申万成分 {industry_name}")
    if frame is None or len(frame) == 0:
        return pd.DataFrame(columns=["code", "name", "sw_industry", "sw_code"])

    return pd.DataFrame({
        "code": frame["证券代码"].astype(str).str.zfill(6).str.strip(),
        "name": frame["证券名称"].astype(str).str.strip(),
        "sw_industry": industry_name,
        "sw_code": industry_code,
    })


def build_sw_mapping(use_cache: bool = True) -> pd.DataFrame:
    """构建全市场 code -> 申万一级行业 映射。

    返回列：code, name, sw_industry, sw_code
    """
    cache_path = CACHE_DIR / "sw_industry_map.parquet"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if use_cache and cache_path.exists():
        age = datetime.now() - datetime.fromtimestamp(cache_path.stat().st_mtime)
        if age < timedelta(hours=CACHE_TTL_HOURS):
            cached = pd.read_parquet(cache_path)
            logger.info("使用缓存：%d 只", len(cached))
            return cached

    industries = fetch_sw_industries()
    logger.info("申万一级行业：%d 个", len(industries))

    frames = []
    failed = []
    for idx, row in industries.iterrows():
        code = row["industry_code"]
        name = row["industry_name"]
        try:
            members = fetch_sw_members(code, name)
            if len(members) > 0:
                frames.append(members)
                logger.info("[%d/%d] %s：%d 只", idx + 1, len(industries), name, len(members))
        except IndustryError as exc:
            failed.append(name)
            logger.warning("[%d/%d] %s 失败：%s", idx + 1, len(industries), name, str(exc)[:50])
        time.sleep(0.3)

    if not frames:
        raise IndustryError(f"所有行业都失败（{len(failed)} 个）")

    result = pd.concat(frames, ignore_index=True)
    result = result.drop_duplicates(subset=["code"], keep="first")

    try:
        result.to_parquet(cache_path, index=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("缓存写入失败：%s", exc)

    logger.info("申万映射完成：%d 只（%d 个行业，%d 个行业失败）",
                len(result), len(frames), len(failed))
    return result


def enrich_with_industry(meta: pd.DataFrame, use_cache: bool = True) -> pd.DataFrame:
    """给元数据补充申万行业（用于上交所那些缺行业的）。

    优先保留已有的行业（深交所的「所属行业」是证监会分类，更细），
    缺失的用申万一级补齐，并在 sw_industry 列单独记录。
    """
    result = meta.copy()
    result["sw_industry"] = ""

    try:
        mapping = build_sw_mapping(use_cache=use_cache)
    except IndustryError as exc:
        logger.warning("申万行业映射失败，跳过：%s", str(exc)[:60])
        return result

    sw_map = dict(zip(mapping["code"], mapping["sw_industry"], strict=False))
    result["sw_industry"] = result["code"].map(sw_map).fillna("")

    # **关键设计**：行业字段必须统一到同一套分类体系，否则中性化会失效。
    #
    # 深交所的「所属行业」是证监会门类（C 制造业、I 信息技术、J 金融业...），
    # 申万的是行业名（电子、医药生物、银行...），两套不能混用。
    #
    # 实测问题（2026-10-31）：混用后「C 制造业」和「电子」并存，
    # 同一行业被拆成两种命名，行业中性化会把它们当成不同行业。
    #
    # 方案：统一用申万一级（覆盖 5221 只，粒度更细，适合中性化）。
    result["industry_final"] = result["sw_industry"].fillna("")
    # 申万缺失时（极少）回退到证监会门类
    missing_sw = result["industry_final"] == ""
    result.loc[missing_sw, "industry_final"] = (
        result.loc[missing_sw, "industry"].fillna(""))

    # 保留两个字段便于审计
    result["industry_csrc"] = result["industry"].fillna("")

    filled = int((result["industry_final"].fillna("") != "").sum())
    sw_filled = int((result["sw_industry"].fillna("") != "").sum())
    logger.info("行业统一到申万：%d/%d 有行业（申万 %d，回退证监会 %d）",
                filled, len(result), sw_filled, filled - sw_filled)
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    mapping = build_sw_mapping(use_cache=False)
    print()
    print(f"总映射: {len(mapping)} 只")
    print()
    print("行业分布:")
    print(mapping["sw_industry"].value_counts().to_string())
    print()
    print("样例:")
    print(mapping.head(8).to_string())
