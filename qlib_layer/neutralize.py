"""行业中性化：让因子在行业内比较，而不是全市场一刀切。

为什么必须做
------------
六家评审第一轮就指出：「银行 ROE 10% 已是优秀，白酒 25% 只是及格线」。
全市场一刀切会让某些行业整批入选或整批出局。

实测证据（2026-10-31）
---------------------
    银行股负债率 91-92%（天然如此）
    制造业负债率 40-70%
    -> 用「资产负债率 < 70%」会把整个银行业灭掉

三种中性化方法
--------------
1. **行业内 z-score**：因子在行业内标准化，消除行业均值差异
2. **行业内分位**：因子在行业内排名，输出 0-1 分位
3. **行业哑变量回归**：用行业哑变量回归取残差（最严格，计算量大）

本模块实现前两种（够用且快），第三种留作扩展。
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def industry_zscore(
    df: pd.DataFrame,
    factor_cols: list[str],
    industry_col: str = "industry_final",
    min_industry_size: int = 5,
) -> pd.DataFrame:
    """行业内 z-score 标准化。

    参数
    ----
    df: 含因子值和行业列的数据
    factor_cols: 要标准化的因子列
    industry_col: 行业列名
    min_industry_size: 行业样本少于此数时，退化为全市场标准化

    返回
    ----
    新的 DataFrame，含 `<factor>_ind_z` 列
    """
    result = df.copy()

    if industry_col not in result.columns:
        logger.warning("缺行业列 %s，跳过中性化", industry_col)
        return result

    industries = result[industry_col].fillna("未知")
    result["_ind"] = industries

    for col in factor_cols:
        if col not in result.columns:
            continue

        values = pd.to_numeric(result[col], errors="coerce")
        out = pd.Series(np.nan, index=result.index)

        for ind_name, group in result.groupby("_ind"):
            idx = group.index
            vals = values.loc[idx]
            valid = vals.notna()

            if valid.sum() < min_industry_size:
                # 行业太小，退化为全市场
                continue

            mean = vals[valid].mean()
            std = vals[valid].std()
            if std and std > 0:
                out.loc[idx] = (vals - mean) / std
            else:
                out.loc[idx] = 0.0

        # 未覆盖的（小行业）用全市场标准化
        missing = out.isna() & values.notna()
        if missing.sum() > 0:
            global_mean = values.mean()
            global_std = values.std()
            if global_std and global_std > 0:
                out.loc[missing] = (values.loc[missing] - global_mean) / global_std
            else:
                out.loc[missing] = 0.0

        result[f"{col}_ind_z"] = out

    result = result.drop(columns=["_ind"])
    return result


def industry_rank(
    df: pd.DataFrame,
    factor_cols: list[str],
    industry_col: str = "industry_final",
    min_industry_size: int = 5,
    ascending: bool = True,
) -> pd.DataFrame:
    """行业内分位排名（0-1）。

    比 z-score 更稳健（不受极端值影响）。

    ascending=True 表示值越大分位越高。
    """
    result = df.copy()

    if industry_col not in result.columns:
        return result

    result["_ind"] = result[industry_col].fillna("未知")

    for col in factor_cols:
        if col not in result.columns:
            continue

        values = pd.to_numeric(result[col], errors="coerce")
        out = pd.Series(np.nan, index=result.index)

        for ind_name, group in result.groupby("_ind"):
            idx = group.index
            vals = values.loc[idx]
            valid = vals.notna()

            if valid.sum() < min_industry_size:
                continue

            out.loc[idx] = vals.rank(pct=True, ascending=ascending)

        # 小行业用全市场分位
        missing = out.isna() & values.notna()
        if missing.sum() > 0:
            out.loc[missing] = values.loc[missing].rank(pct=True, ascending=ascending)

        result[f"{col}_ind_rank"] = out

    result = result.drop(columns=["_ind"])
    return result


def neutralize_composite(
    df: pd.DataFrame,
    weights: dict[str, float],
    industry_col: str = "industry_final",
    use_rank: bool = True,
    min_industry_size: int = 5,
) -> pd.DataFrame:
    """行业中性化后合成打分。

    参数
    ----
    weights: {因子列名: 权重}，如 {"f_roe": 1.0, "f_pe": -0.5}
    use_rank: True 用行业分位，False 用行业内 z-score

    返回
    ----
    含 `<factor>_ind` 和 `composite_neutral` 列
    """
    result = df.copy()
    factor_cols = [c for c in weights if c in result.columns]

    if not factor_cols:
        logger.warning("没有可用的因子列")
        return result

    if use_rank:
        # 分位：越大越好 -> ascending=True
        # 对负向因子（如 PE），需要反转
        positive = {k: v for k, v in weights.items() if v > 0 and k in result.columns}
        negative = {k: v for k, v in weights.items() if v < 0 and k in result.columns}

        if positive:
            result = industry_rank(
                result, list(positive), industry_col, min_industry_size, ascending=True)
            for col in positive:
                result[f"{col}_ind"] = result[f"{col}_ind_rank"]

        if negative:
            result = industry_rank(
                result, list(negative), industry_col, min_industry_size, ascending=False)
            for col in negative:
                result[f"{col}_ind"] = result[f"{col}_ind_rank"]
    else:
        result = industry_zscore(result, factor_cols, industry_col, min_industry_size)
        for col in factor_cols:
            result[f"{col}_ind"] = result[f"{col}_ind_z"]

    # 加权合成
    total_weight = sum(abs(w) for w in weights.values() if w != 0) or 1.0
    score = pd.Series(0.0, index=result.index)
    valid_count = pd.Series(0, index=result.index)

    for col, weight in weights.items():
        ind_col = f"{col}_ind"
        if ind_col not in result.columns:
            continue
        vals = result[ind_col]
        mask = vals.notna()
        # 负向因子：分位已反转，权重取绝对值
        w = abs(weight) if use_rank else weight
        score.loc[mask] += vals.loc[mask] * w
        valid_count.loc[mask] += 1

    # 归一化（按有效因子数）
    result["composite_neutral"] = np.where(
        valid_count > 0, score / valid_count, np.nan)

    return result


def industry_exposure_report(
    df: pd.DataFrame,
    industry_col: str = "industry_final",
    weight_col: str | None = None,
) -> pd.DataFrame:
    """行业暴露报告：各行业数量、占比、平均市值。"""
    if industry_col not in df.columns:
        return pd.DataFrame()

    grouped = df.groupby(df[industry_col].fillna("未知"))

    report = pd.DataFrame({
        "count": grouped.size(),
    })
    report["pct"] = (report["count"] / len(df)).round(4)

    if "market_cap_yi" in df.columns:
        report["avg_market_cap"] = grouped["market_cap_yi"].mean().round(1)
        report["total_market_cap"] = grouped["market_cap_yi"].sum().round(1)

    if weight_col and weight_col in df.columns:
        report["avg_weight"] = grouped[weight_col].mean().round(4)

    return report.sort_values("count", ascending=False)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # 构造测试数据
    np.random.seed(42)
    n = 200
    test = pd.DataFrame({
        "code": [f"{i:06d}" for i in range(n)],
        "industry_final": np.random.choice(["银行", "白酒", "电子", "医药"], n),
        "f_roe": np.random.normal(10, 5, n),
        "f_pe": np.random.normal(30, 15, n),
        "market_cap_yi": np.random.uniform(50, 5000, n),
    })
    # 让银行业 ROE 系统性偏低（模拟真实）
    test.loc[test["industry_final"] == "银行", "f_roe"] -= 5

    print("=== 原始数据 ===")
    print(test.groupby("industry_final")["f_roe"].agg(["mean", "std", "count"]).round(2))

    print()
    print("=== 行业中性化后 ===")
    neutralized = neutralize_composite(
        test, {"f_roe": 1.0, "f_pe": -0.5}, use_rank=False)
    print(neutralized.groupby("industry_final")["f_roe_ind_z"]
          .agg(["mean", "std", "count"]).round(4))

    print()
    print("=== 行业暴露报告 ===")
    print(industry_exposure_report(test).to_string())
