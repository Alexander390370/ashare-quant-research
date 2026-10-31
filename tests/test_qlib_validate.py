"""因子管线校验的单元测试。

2026-10-31 重写：适配新架构
- 因子定义统一在 factors.py（唯一真源）
- 方向断言跑真实 Qlib 表达式引擎
- 加变异测试
"""

from __future__ import annotations

import numpy as np
import pytest

from qlib_layer import validate as V
from qlib_layer.factors import (
    DEFAULT_WEIGHTS,
    FACTOR_DIRECTION,
    FACTOR_EXPRESSIONS,
    FACTOR_NAMES,
    reference_factors,
)


@pytest.mark.unit
class TestFactorDefinitions:
    """因子定义（唯一真源）的完整性。"""

    def test_all_factors_have_expression(self):
        for name in FACTOR_NAMES:
            assert name in FACTOR_EXPRESSIONS
            assert FACTOR_EXPRESSIONS[name]

    def test_all_factors_have_direction(self):
        for name in FACTOR_NAMES:
            assert name in FACTOR_DIRECTION, f"{name} 缺方向定义"

    def test_momentum_expressions_are_forward(self):
        """动量必须是 $close/Ref(...)，不是 Ref(...)/$close。"""
        for name in ("mom5", "mom20"):
            expr = FACTOR_EXPRESSIONS[name]
            assert expr.startswith("$close /"), f"{name} 方向可能写反: {expr}"

    def test_rev1_is_reversal(self):
        """rev1 是反转因子，应为 Ref/close。"""
        expr = FACTOR_EXPRESSIONS["rev1"]
        assert expr.startswith("Ref($close"), f"rev1 应为反转: {expr}"

    def test_weights_reference_known_factors(self):
        for key in DEFAULT_WEIGHTS:
            base = key.removesuffix("_z")
            assert base in FACTOR_NAMES, f"权重引用了未知因子 {base}"


@pytest.mark.unit
class TestReferenceFactors:
    """参照实现的正确性（ddof=1 与 Qlib 对齐）。"""

    def test_mom20_exact_20_percent(self):
        closes = np.concatenate([
            np.full(10, 100.0),
            np.linspace(100.0, 120.0, 20),
        ])
        vols = np.full(len(closes), 1e6)
        f = reference_factors(closes, vols)
        assert f["mom20"] == pytest.approx(0.2, abs=1e-9)

    def test_mom20_exact_negative(self):
        closes = np.concatenate([
            np.full(10, 120.0),
            np.linspace(120.0, 96.0, 20),
        ])
        vols = np.full(len(closes), 1e6)
        f = reference_factors(closes, vols)
        assert f["mom20"] == pytest.approx(-0.2, abs=1e-9)

    def test_flat_series(self):
        f = reference_factors(np.full(30, 100.0), np.full(30, 1e6))
        assert f["mom20"] == pytest.approx(0.0, abs=1e-12)
        assert f["mom5"] == pytest.approx(0.0, abs=1e-12)
        assert f["vol20"] == pytest.approx(0.0, abs=1e-12)

    def test_vol20_uses_ddof_1(self):
        """关键：vol20 必须用 ddof=1（与 Qlib 一致）。

        用 [100×19, 110] 验证：
          ddof=0 -> 0.021686
          ddof=1 -> 0.022249（Qlib 实际值）
        """
        closes = np.full(20, 100.0)
        closes[-1] = 110.0
        f = reference_factors(closes, np.full(20, 1e6))
        expected_ddof1 = float(np.std(closes, ddof=1) / np.mean(closes))
        assert f["vol20"] == pytest.approx(expected_ddof1, abs=1e-9)
        # 明确不等于 ddof=0
        expected_ddof0 = float(np.std(closes, ddof=0) / np.mean(closes))
        assert abs(f["vol20"] - expected_ddof0) > 1e-4

    def test_insufficient_data(self):
        f = reference_factors(np.array([100.0, 101.0]), np.array([1e6, 1e6]))
        assert "rev1" in f
        assert "mom20" not in f

    def test_dev20_positive_when_above_mean(self):
        closes = np.linspace(100.0, 130.0, 30)
        f = reference_factors(closes, np.full(30, 1e6))
        assert f["dev20"] > 0


@pytest.mark.unit
class TestDirectionViaQlib:
    """防线 1：方向断言跑真实 Qlib 引擎。"""

    def test_all_directions_pass(self):
        results = V.check_direction_via_qlib()
        failed = [r for r in results if not r.passed]
        assert not failed, f"方向断言失败: {[(r.name, r.detail) for r in failed]}"

    def test_uses_qlib_engine(self):
        """关键：断言名称里必须标明用 Qlib 引擎。"""
        results = V.check_direction_via_qlib()
        names = [r.name for r in results]
        assert any("Qlib 引擎" in n for n in names), \
            "方向断言必须跑 Qlib 表达式引擎，不能只用 numpy 参照"

    def test_has_precision_checks(self):
        """必须有精确值校验（抓窗口错位、分子分母颠倒）。"""
        results = V.check_direction_via_qlib()
        names = [r.name for r in results]
        assert any("vol20 精确性" in n for n in names), "缺 vol20 精确性校验"
        assert any("vol_ratio 精确性" in n for n in names), "缺 vol_ratio 精确性校验"
        assert any("mom20 精确性" in n for n in names), "缺 mom20 精确性校验"


@pytest.mark.unit
class TestWeightInversion:
    def test_passes(self):
        results = V.check_weight_inversion()
        assert all(r.passed for r in results)


@pytest.mark.unit
class TestNoLookahead:
    def test_passes(self):
        results = V.check_no_lookahead()
        assert all(r.passed for r in results)

    def test_detects_lookahead_if_present(self):
        """验证测试用例本身能区分前视与无前视。"""
        base = np.linspace(100.0, 130.0, 30)
        future_a = np.linspace(130.0, 150.0, 10)
        future_b = np.linspace(130.0, 90.0, 10)
        vols = np.full(40, 1e6)

        # 含未来 -> 两场景不同
        bad_a = reference_factors(np.concatenate([base, future_a]), vols)
        bad_b = reference_factors(np.concatenate([base, future_b]), vols)
        assert not np.isclose(bad_a["mom20"], bad_b["mom20"])


@pytest.mark.unit
class TestDeterministic:
    def test_passes(self):
        results = V.check_deterministic()
        assert all(r.passed for r in results)


@pytest.mark.unit
class TestRunAll:
    def test_quick_mode(self):
        passed, results = V.run_all(None, quick=True, verbose=False)
        assert passed is True
        assert len(results) >= 7

    def test_full_mode(self):
        passed, results = V.run_all(None, quick=False, verbose=False)
        failed = [r for r in results if not r.passed]
        assert not failed, [(r.name, r.detail) for r in failed]


@pytest.mark.unit
class TestIndependentRecompute:
    def test_handles_missing_data(self, tmp_path):
        results = V.check_independent_recompute(tmp_path)
        assert len(results) >= 1
        assert isinstance(results[0].passed, bool)


@pytest.mark.unit
class TestMutationInfrastructure:
    """变异测试基础设施（不真跑子进程，只验证逻辑）。"""

    def test_mutation_target_exists(self):
        """factors.py 必须存在，且含 mom20 定义。"""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        factors_path = root / "qlib_layer" / "factors.py"
        assert factors_path.exists()
        content = factors_path.read_text(encoding="utf-8")
        assert '"mom20": "$close / Ref($close, 20) - 1",' in content
