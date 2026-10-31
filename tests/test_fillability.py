"""可成交性判定的单元测试。

不触网：用构造的行情 DataFrame 直接测判定逻辑。
覆盖主板 10% / 创业板 20% / ST 5% 三档阈值，以及一字板、开盘涨停、
收盘涨停、正常交易、数据缺失五种情形。
"""

import datetime as dt

import pandas as pd
import pytest

from tradingagents.dataflows.vendors.akshare import fillability as F


def make_bars(rows):
    """rows: [(date_str, open, high, low, close)]"""
    return pd.DataFrame(
        {
            "date": [dt.date.fromisoformat(r[0]) for r in rows],
            "open": [r[1] for r in rows],
            "high": [r[2] for r in rows],
            "low": [r[3] for r in rows],
            "close": [r[4] for r in rows],
        }
    )


@pytest.mark.unit
class TestThresholds:
    def test_main_board_is_10_percent(self):
        assert F.limit_threshold("600519.SS") == pytest.approx(0.10)
        assert F.limit_threshold("000001.SZ") == pytest.approx(0.10)

    def test_chinext_and_star_are_20_percent(self):
        assert F.limit_threshold("300750.SZ") == pytest.approx(0.20)
        assert F.limit_threshold("301001.SZ") == pytest.approx(0.20)
        assert F.limit_threshold("688981.SS") == pytest.approx(0.20)
        assert F.limit_threshold("689009.SS") == pytest.approx(0.20)

    def test_st_is_5_percent(self):
        assert F.limit_threshold("600519.SS", is_st=True) == pytest.approx(0.05)


@pytest.mark.unit
class TestSymbolHelpers:
    def test_a_share_detection(self):
        assert F.is_a_share("600519.SS") is True
        assert F.is_a_share("000001.SZ") is True
        assert F.is_a_share("AAPL") is False

    def test_plain_symbol(self):
        assert F.to_plain_symbol("600519.SS") == "600519"
        assert F.to_plain_symbol("000001.SZ") == "000001"

    def test_prefixed_symbol(self):
        assert F._to_prefixed_symbol("600519.SS") == "sh600519"
        assert F._to_prefixed_symbol("000001.SZ") == "sz000001"
        assert F._to_prefixed_symbol("430047.BJ") == "bj430047"


@pytest.mark.unit
class TestFillabilityJudgement:
    """用 monkeypatch 替换取数，纯测判定分支。"""

    def _patch(self, monkeypatch, bars):
        monkeypatch.setattr(F, "_fetch_bars", lambda *a, **k: bars)

    def test_one_word_limit_up_is_unfillable(self, monkeypatch):
        """一字板：全天 high == low，且开盘就涨停。"""
        bars = make_bars([
            ("2026-09-01", 100.0, 100.0, 100.0, 100.0),
            ("2026-09-02", 110.0, 110.0, 110.0, 110.0),  # +10%
        ])
        self._patch(monkeypatch, bars)
        verdict = F.check_fillability("600519.SS", "2026-09-02")
        assert verdict.fillable is False
        assert "一字涨停" in verdict.reason

    def test_open_limit_up_is_unfillable(self, monkeypatch):
        """开盘即涨停，但盘中打开过。"""
        bars = make_bars([
            ("2026-09-01", 100.0, 100.0, 100.0, 100.0),
            ("2026-09-02", 110.0, 112.0, 108.0, 111.0),
        ])
        self._patch(monkeypatch, bars)
        verdict = F.check_fillability("600519.SS", "2026-09-02")
        assert verdict.fillable is False
        assert "开盘涨停" in verdict.reason

    def test_close_limit_up_is_unfillable(self, monkeypatch):
        """收盘涨停：收盘价买不到。"""
        bars = make_bars([
            ("2026-09-01", 100.0, 100.0, 100.0, 100.0),
            ("2026-09-02", 102.0, 110.0, 101.0, 110.0),
        ])
        self._patch(monkeypatch, bars)
        verdict = F.check_fillability("600519.SS", "2026-09-02")
        assert verdict.fillable is False
        assert "收盘涨停" in verdict.reason

    def test_normal_day_is_fillable(self, monkeypatch):
        bars = make_bars([
            ("2026-09-01", 100.0, 100.0, 100.0, 100.0),
            ("2026-09-02", 101.0, 103.0, 99.0, 102.0),  # +2%
        ])
        self._patch(monkeypatch, bars)
        verdict = F.check_fillability("600519.SS", "2026-09-02")
        assert verdict.fillable is True
        assert "正常交易" in verdict.reason

    def test_chinext_10_percent_is_fillable(self, monkeypatch):
        """创业板 +10% 不是涨停，应可成交——验证阈值分档正确。"""
        bars = make_bars([
            ("2026-09-01", 100.0, 100.0, 100.0, 100.0),
            ("2026-09-02", 101.0, 110.5, 100.5, 110.0),  # +10%，创业板阈值 20%
        ])
        self._patch(monkeypatch, bars)
        verdict = F.check_fillability("300750.SZ", "2026-09-02")
        assert verdict.fillable is True

    def test_chinext_20_percent_is_unfillable(self, monkeypatch):
        bars = make_bars([
            ("2026-09-01", 100.0, 100.0, 100.0, 100.0),
            ("2026-09-02", 101.0, 120.0, 100.5, 120.0),  # +20%
        ])
        self._patch(monkeypatch, bars)
        verdict = F.check_fillability("300750.SZ", "2026-09-02")
        assert verdict.fillable is False

    def test_missing_entry_bar_is_unknown(self, monkeypatch):
        """入场日无行情（停牌）——标记未知，不阻断结算。"""
        bars = make_bars([("2026-09-01", 100.0, 100.0, 100.0, 100.0)])
        self._patch(monkeypatch, bars)
        verdict = F.check_fillability("600519.SS", "2026-09-02")
        assert verdict.unknown is True
        assert verdict.fillable is True

    def test_missing_prior_bar_is_unknown(self, monkeypatch):
        bars = make_bars([("2026-09-02", 100.0, 101.0, 99.0, 100.0)])
        self._patch(monkeypatch, bars)
        verdict = F.check_fillability("600519.SS", "2026-09-02")
        assert verdict.unknown is True

    def test_non_a_share_is_fillable(self, monkeypatch):
        """美股不适用涨跌停规则。"""
        verdict = F.check_fillability("AAPL", "2026-09-02")
        assert verdict.fillable is True
        assert "非 A 股" in verdict.reason

    def test_fetch_failure_is_unknown_not_blocking(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("network down")

        monkeypatch.setattr(F, "_fetch_bars", boom)
        verdict = F.check_fillability("600519.SS", "2026-09-02")
        assert verdict.unknown is True
        assert verdict.fillable is True  # 不阻断结算

    def test_describe_covers_all_states(self):
        assert "可成交" in F.Fillability(fillable=True, reason="正常交易").describe()
        assert "不可成交" in F.Fillability(fillable=False, reason="涨停").describe()
        assert "未知" in F.Fillability(fillable=True, reason="x", unknown=True).describe()
