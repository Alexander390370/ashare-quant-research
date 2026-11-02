"""财务因子：把财务数据变成可用的因子。

数据源实测（2026-10-31，每项 3 次）
-----------------------------------
    ✅ 东财-财务分析指标      3/3  每股收益、每股净资产、ROE 等 11 项
    ✅ 东财-个股指标          3/3  净利润、营收、同比增长率 28 项
    ✅ 同花顺-关键指标        3/3  80 项指标，多年数据
    ✅ 东财-资产负债表        3/3  完整三表
    ❌ 东财-利润表            0/3  SSLError
    ❌ 东财-现金流表          0/3  SSLError
    ⚠️ 雪球-财务              2/3  备用

可用的财务因子
--------------
从「财务分析指标」和「个股指标」能算出：

    质量类：
        roe         净资产收益率
        gross_margin 毛利率
        net_margin   净利率
        ocf_to_profit 经营现金流/净利润（防造假，需现金流表，暂无）

    成长类：
        revenue_yoy  营收同比增长
        profit_yoy   净利润同比增长

    估值类（需配合市值）：
        pe           市盈率
        pb           市净率
        ps           市销率

设计
----
- 财务数据按报告期（季度）缓存，变动慢
- 所有因子返回「最新可得值」，不做 PIT（数据源不含公告日）
- **重要**：无公告日 = 有前视风险，这在文档里明确标注
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = _REPO_ROOT / "run_data" / "cache" / "financials"
CACHE_TTL_HOURS = 72  # 财务数据 3 天更新一次足够


class FinancialError(RuntimeError):
    """财务数据获取失败。"""


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
    raise FinancialError(f"{label} 失败：{type(last).__name__}: {str(last)[:60]}")


def _parse_cn_number(value) -> float | None:
    """解析中文数字字符串（可能带「亿」「万」「%」）。"""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    s = str(value).strip()
    if not s or s in ("--", "nan", "None", "False"):
        return None
    multiplier = 1.0
    if "亿" in s:
        multiplier = 1e8
        s = s.replace("亿", "")
    elif "万" in s:
        multiplier = 1e4
        s = s.replace("万", "")
    s = s.replace("%", "").replace(",", "").replace("元", "").strip()
    try:
        return float(s) * multiplier
    except (ValueError, TypeError):
        return None


def fetch_financial_indicators(code: str) -> dict:
    """拉单只股票的财务指标。

    用东财「财务分析指标」（实测 3/3 稳定）。

    返回：{指标名: 最新值}，含 roe / eps / bps / gross_margin / net_margin 等
    """
    import akshare as ak

    # 带交易所前缀
    prefix = "SH" if code.startswith("6") else "SZ" if code.startswith(("0", "3")) else "BJ"
    symbol = f"{code}"

    frame = _retry(
        ak.stock_financial_analysis_indicator,
        symbol=symbol, start_year="2023",
        retries=2, backoff=0.8, label=f"财务指标 {code}")

    if frame is None or len(frame) == 0:
        return {}

    # **关键**：东财财务指标按日期**升序**排列（实测 2026-10-31），
    # iloc[0] 是最老的（2023-03-31），iloc[-1] 才是最新（2026-09-30）。
    # 之前写成 iloc[0]，导致所有股票的财务数据都是 2023 年的。
    frame = frame.sort_values("日期") if "日期" in frame.columns else frame
    latest = frame.iloc[-1]

    out = {}
    # 字段名是中文，做映射
    field_map = {
        "摊薄每股收益(元)": "eps",
        "加权每股收益(元)": "eps_weighted",
        "每股净资产_调整后(元)": "bps",
        "每股经营性现金流(元)": "ocf_per_share",
        "每股资本公积金(元)": "capital_reserve_per_share",
        "每股未分配利润(元)": "retained_earnings_per_share",
        "净资产收益率(%)": "roe",
        "加权净资产收益率(%)": "roe_weighted",
        "销售毛利率(%)": "gross_margin",
        "销售净利率(%)": "net_margin",
        "主营业务利润率(%)": "core_margin",
        "资产负债率(%)": "debt_ratio",
        "流动比率": "current_ratio",
        "速动比率": "quick_ratio",
        "总资产周转率(次)": "asset_turnover",
        "存货周转率(次)": "inventory_turnover",
        "应收账款周转率(次)": "receivable_turnover",
        "总资产(元)": "total_assets",
        "净资产(元)": "net_assets",
    }

    for cn_name, en_name in field_map.items():
        if cn_name in frame.columns:
            val = _parse_cn_number(latest[cn_name])
            if val is not None:
                out[en_name] = val

    # 报告期
    if "日期" in frame.columns:
        out["report_date"] = str(latest["日期"])[:10]

    return out


def fetch_growth_indicators(code: str) -> dict:
    """拉成长性指标（营收/净利润同比）。

    用东财「个股指标」（实测 3/3 稳定）。
    """
    import akshare as ak

    try:
        frame = _retry(
            ak.stock_financial_abstract_ths,
            symbol=code, indicator="按报告期",
            retries=2, backoff=0.8, label=f"成长指标 {code}")
    except FinancialError:
        return {}

    if frame is None or len(frame) == 0:
        return {}

    # 同样按报告期升序排列，取最后一个
    if "报告期" in frame.columns:
        frame = frame.sort_values("报告期")
    latest = frame.iloc[-1]
    out = {}

    field_map = {
        "营业总收入": "revenue",
        "营业总收入同比增长率": "revenue_yoy",
        "净利润": "net_profit",
        "净利润同比增长率": "profit_yoy",
        "扣非净利润": "net_profit_deducted",
        "扣非净利润同比增长率": "profit_yoy_deducted",
        "基本每股收益": "eps_basic",
        "每股净资产": "bps_ths",
        "净资产收益率": "roe_ths",
        "销售毛利率": "gross_margin_ths",
        "销售净利率": "net_margin_ths",
    }

    for cn_name, en_name in field_map.items():
        if cn_name in frame.columns:
            val = _parse_cn_number(latest[cn_name])
            if val is not None:
                out[en_name] = val

    if "报告期" in frame.columns:
        out["report_date_ths"] = str(latest["报告期"])[:10]

    return out


def fetch_financials_batch(
    codes: list[str],
    use_cache: bool = True,
    workers: int = 10,
    progress=None,
) -> pd.DataFrame:
    """批量拉财务数据，返回 DataFrame（index=code）。

    并发 6（财务接口限流较严，不宜太高）。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / "financials.parquet"

    # 缓存
    cached_df = None
    if use_cache and cache_file.exists():
        age = datetime.now() - datetime.fromtimestamp(cache_file.stat().st_mtime)
        if age < timedelta(hours=CACHE_TTL_HOURS):
            try:
                cached_df = pd.read_parquet(cache_file)
                cached_codes = set(cached_df.index)
                missing = [c for c in codes if c not in cached_codes]
                if not missing:
                    logger.info("财务数据全部命中缓存：%d 只", len(codes))
                    return cached_df.loc[[c for c in codes if c in cached_codes]]
                logger.info("缓存命中 %d，需补 %d", len(cached_codes), len(missing))
                codes = missing
            except Exception:  # noqa: BLE001
                cached_df = None

    def task(code: str) -> tuple[str, dict]:
        merged = {}
        try:
            merged.update(fetch_financial_indicators(code))
        except FinancialError as exc:
            logger.debug("%s 财务指标失败: %s", code, str(exc)[:40])
        try:
            merged.update(fetch_growth_indicators(code))
        except FinancialError as exc:
            logger.debug("%s 成长指标失败: %s", code, str(exc)[:40])
        return code, merged

    rows = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(task, c): c for c in codes}
        for future in as_completed(futures):
            code, data = future.result()
            done += 1
            if data:
                data["code"] = code
                rows.append(data)
            if progress and (done % 50 == 0 or done == len(codes)):
                progress(done, len(codes), code)

    new_df = pd.DataFrame(rows).set_index("code") if rows else pd.DataFrame()

    # 合并缓存
    if cached_df is not None and len(new_df) > 0:
        combined = pd.concat([cached_df, new_df])
        combined = combined[~combined.index.duplicated(keep="last")]
    elif cached_df is not None:
        combined = cached_df
    else:
        combined = new_df

    if len(combined) > 0:
        try:
            combined.to_parquet(cache_file)
        except Exception as exc:  # noqa: BLE001
            logger.warning("财务缓存写入失败：%s", exc)

    logger.info("财务数据：%d 只", len(combined))
    return combined


def compute_financial_factors(
    fin: pd.DataFrame,
    meta: pd.DataFrame,
) -> pd.DataFrame:
    """从原始财务数据算因子。

    参数
    ----
    fin: 财务数据（index=code）
    meta: 元数据（含 market_cap_yi）

    返回
    ----
    DataFrame（index=code），含以下因子：
        f_roe           净资产收益率（%）
        f_gross_margin  毛利率（%）
        f_net_margin    净利率（%）
        f_debt_ratio    资产负债率（%）
        f_revenue_yoy   营收同比（%）
        f_profit_yoy    净利润同比（%）
        f_pe            市盈率（市值/净利润）
        f_pb            市净率（市值/净资产）
        f_ps            市销率（市值/营收）
        f_quality       质量合成分（ROE + 毛利率 - 负债率）
        f_growth        成长合成分（营收同比 + 净利润同比）
        f_value         估值合成分（-PE - PB，越低越好）
    """
    if len(fin) == 0:
        return pd.DataFrame()

    df = fin.copy()

    # 统一字段（两个源可能有重复，优先用有值的）
    def pick(*names):
        for n in names:
            if n in df.columns:
                return df[n]
        return pd.Series(np.nan, index=df.index)

    roe = pick("roe", "roe_ths")
    gross_margin = pick("gross_margin", "gross_margin_ths")
    net_margin = pick("net_margin", "net_margin_ths")
    debt_ratio = pick("debt_ratio")
    revenue_yoy = pick("revenue_yoy")
    profit_yoy = pick("profit_yoy")

    out = pd.DataFrame(index=df.index)
    out["f_roe"] = roe
    out["f_gross_margin"] = gross_margin
    out["f_net_margin"] = net_margin
    out["f_debt_ratio"] = debt_ratio
    out["f_revenue_yoy"] = revenue_yoy
    out["f_profit_yoy"] = profit_yoy

    # 估值因子（需市值）
    if "market_cap_yi" in meta.columns:
        cap = meta.set_index("code")["market_cap_yi"]
        cap = cap.reindex(out.index)

        # PE = 市值 / 净利润。净利润单位可能是元或万元
        net_profit = pick("net_profit")
        revenue = pick("revenue")
        net_assets = pick("net_assets")

        # 净利润：如果是亿元级别，转成元
        np_yuan = net_profit.copy()
        if np_yuan.notna().any() and np_yuan.abs().median() < 1e6:
            np_yuan = np_yuan * 1e4  # 万元 -> 元

        with np.errstate(divide="ignore", invalid="ignore"):
            out["f_pe"] = np.where(
                np_yuan > 0, cap * 1e8 / np_yuan, np.nan)

            rev_yuan = revenue.copy()
            if rev_yuan.notna().any() and rev_yuan.abs().median() < 1e6:
                rev_yuan = rev_yuan * 1e4
            out["f_ps"] = np.where(
                rev_yuan > 0, cap * 1e8 / rev_yuan, np.nan)

            out["f_pb"] = np.where(
                net_assets > 0, cap * 1e8 / net_assets, np.nan)

    # 合成分（都做截面 rank，避免量纲问题）
    def safe_rank(series, ascending=True):
        if series.notna().sum() < 10:
            return pd.Series(0.0, index=series.index)
        ranked = series.rank(pct=True, ascending=ascending)
        return ranked.fillna(0.5)

    quality_parts = []
    if out["f_roe"].notna().sum() > 10:
        quality_parts.append(safe_rank(out["f_roe"], ascending=True))
    if out["f_gross_margin"].notna().sum() > 10:
        quality_parts.append(safe_rank(out["f_gross_margin"], ascending=True))
    if out["f_debt_ratio"].notna().sum() > 10:
        quality_parts.append(safe_rank(out["f_debt_ratio"], ascending=False))
    if quality_parts:
        out["f_quality"] = sum(quality_parts) / len(quality_parts)

    growth_parts = []
    if out["f_revenue_yoy"].notna().sum() > 10:
        growth_parts.append(safe_rank(out["f_revenue_yoy"], ascending=True))
    if out["f_profit_yoy"].notna().sum() > 10:
        growth_parts.append(safe_rank(out["f_profit_yoy"], ascending=True))
    if growth_parts:
        out["f_growth"] = sum(growth_parts) / len(growth_parts)

    value_parts = []
    if "f_pe" in out.columns and out["f_pe"].notna().sum() > 10:
        # PE 越低越好（但负 PE 应排除）
        pe_clean = out["f_pe"].where(out["f_pe"] > 0)
        value_parts.append(safe_rank(pe_clean, ascending=False))
    if "f_pb" in out.columns and out["f_pb"].notna().sum() > 10:
        pb_clean = out["f_pb"].where(out["f_pb"] > 0)
        value_parts.append(safe_rank(pb_clean, ascending=False))
    if value_parts:
        out["f_value"] = sum(value_parts) / len(value_parts)

    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    test_codes = ["600519", "000001", "300750", "002594", "601398"]
    print("测试 5 只股票的财务数据：")
    for c in test_codes:
        print(f"\n--- {c} ---")
        try:
            ind = fetch_financial_indicators(c)
            print(f"  指标: {ind}")
        except FinancialError as exc:
            print(f"  ❌ {exc}")
        time.sleep(0.5)
