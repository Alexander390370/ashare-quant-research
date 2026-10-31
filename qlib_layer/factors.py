"""因子定义：唯一真源（Single Source of Truth）。

为什么单独抽出来
----------------
2026-10-30 的教训：`run.py` 和 `validate.py` 各存一份因子表达式，
校验器只审自己那份副本。把 `run.py` 的 mom20 改回写反，12 项校验照样全绿
—— 防线形同虚设（变异测试已实锤）。

修复：表达式、因子名、权重只在本文件定义一次，`run.py` 和 `validate.py`
都必须从这里导入。改这里 = 同时改生产代码和被校验对象，无法再各自漂移。

Qlib 表达式语法要点
-------------------
- `Ref($close, N)` = N 个交易日前的收盘价
- 所以动量是 `$close / Ref($close, N) - 1`（现在比过去）
- 写成 `Ref($close, N) / $close - 1` 就是反转（过去比现在），方向相反
"""

from __future__ import annotations

import numpy as np

# ---------------------------------------------------------------------------
# 因子表达式（Qlib 语法）
# ---------------------------------------------------------------------------
FACTOR_EXPRESSIONS: dict[str, str] = {
    "rev1": "Ref($close, 1) / $close - 1",
    "mom5": "$close / Ref($close, 5) - 1",
    "mom20": "$close / Ref($close, 20) - 1",
    "vol20": "Std($close, 20) / Mean($close, 20)",
    "vol_ratio": "Mean($volume, 5) / Mean($volume, 20)",
    "dev20": "($close - Mean($close, 20)) / Mean($close, 20)",
}

FACTOR_NAMES: list[str] = list(FACTOR_EXPRESSIONS)

# ---------------------------------------------------------------------------
# 合成权重（z-score 后）
# ---------------------------------------------------------------------------
# 风格：动量 + 低波动。想换价值风格需加财务因子，那是另一个数据工程。
DEFAULT_WEIGHTS: dict[str, float] = {
    "mom5_z": 1.0,
    "mom20_z": 1.0,
    "vol20_z": -1.0,
    "dev20_z": -1.0,
    "vol_ratio_z": 0.5,
}

# ---------------------------------------------------------------------------
# 方向语义：价格单调上涨时，该因子应为正(+1)、负(-1)、或 0
# ---------------------------------------------------------------------------
FACTOR_DIRECTION: dict[str, int] = {
    "rev1": -1,      # 反转：涨 -> 昨天比今天低 -> 负
    "mom5": +1,
    "mom20": +1,
    "vol20": 0,      # 单调序列波动率为 0
    "vol_ratio": 0,  # 量能比与价格方向无关
    "dev20": +1,     # 当前价高于均线 -> 正
}


# ---------------------------------------------------------------------------
# 纯 Python 参照实现
# ---------------------------------------------------------------------------
# 用途：不依赖 Qlib 就能算因子，方便单测和交叉验证。
# **注意**：这只是参照，不是防线。防线必须跑 Qlib 表达式引擎。
# ---------------------------------------------------------------------------
def reference_factors(closes: np.ndarray, volumes: np.ndarray) -> dict[str, float]:
    """用 numpy 复算全部因子，语义与 FACTOR_EXPRESSIONS 一致。"""
    closes = np.asarray(closes, dtype=float)
    volumes = np.asarray(volumes, dtype=float)
    n = len(closes)
    out: dict[str, float] = {}

    if n >= 2:
        out["rev1"] = closes[-2] / closes[-1] - 1
    if n >= 6:
        out["mom5"] = closes[-1] / closes[-6] - 1
    if n >= 21:
        out["mom20"] = closes[-1] / closes[-21] - 1
    if n >= 20:
        window = closes[-20:]
        mean20 = float(np.mean(window))
        # **必须用 ddof=1**：Qlib 的 Std 是样本标准差。
        # 用 ddof=0 会让 vol20 恒定偏低约 2.6%（20 个样本时）。
        std20 = float(np.std(window, ddof=1))
        out["vol20"] = std20 / mean20 if mean20 else np.nan
        out["dev20"] = (closes[-1] - mean20) / mean20 if mean20 else np.nan
    if n >= 20 and len(volumes) >= 20:
        v5 = float(np.mean(volumes[-5:]))
        v20 = float(np.mean(volumes[-20:]))
        out["vol_ratio"] = v5 / v20 if v20 else np.nan

    return out


def composite_score(factor_values: dict[str, float], weights: dict[str, float]) -> float:
    """按权重合成总分。factor_values 是未标准化的原始值。

    注意：真实流程里先做截面 z-score 再加权。这个函数用于单只股票的
    单元测试，不做标准化。
    """
    total = 0.0
    for key, weight in weights.items():
        base = key.removesuffix("_z")
        if base in factor_values and factor_values[base] is not None:
            total += factor_values[base] * weight
    return total
