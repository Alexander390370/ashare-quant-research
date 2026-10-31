"""筛选层过滤规则的单元测试。

不触网：用构造的数据测纯逻辑。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from screening import filters as F


def make_daily(rows):
    """rows: [(date_str, close, volume, amount)]"""
    return pd.DataFrame(
        {
            "date": [dt.date.fromisoformat(r[0]) for r in rows],
            "close": [r[1] for r in rows],
            "volume": [r[2] for r in rows],
            "amount": [r[3] for r in rows],
        }
    )


TODAY = dt.date(2026, 10, 30)


@pytest.mark.unit
class TestSTCheck:
    @pytest.mark.parametrize(
        "name,expected_hit",
        [
            ("ST海王", True),
            ("*ST美丽", True),
            ("退市海润", True),
            ("PT水仙", True),
            ("贵州茅台", False),
            ("平安银行", False),
        ],
    )
    def test_st_detection(self, name, expected_hit):
        hit, reason = F.check_st(name, ["ST", "*ST", "退", "PT"])
        assert hit is expected_hit
        if hit:
            assert reason

    def test_case_insensitive(self):
        hit, _ = F.check_st("st海王", ["ST"])
        assert hit is True


@pytest.mark.unit
class TestListingMonths:
    def test_recent_listing_rejected(self):
        """上市不满 12 个月 -> 剔除。"""
        daily = make_daily([
            ("2026-06-01", 10.0, 1000, 1e7),
            ("2026-10-30", 11.0, 1200, 1.2e7),
        ])
        hit, reason, metrics = F.check_listing_months(daily, 12, TODAY)
        assert hit is True
        assert "个月" in reason
        assert metrics["listed_months_est"] == 4

    def test_old_listing_passes(self):
        daily = make_daily([
            ("2020-01-02", 10.0, 1000, 1e7),
            ("2026-10-30", 11.0, 1200, 1.2e7),
        ])
        hit, _, metrics = F.check_listing_months(daily, 12, TODAY)
        assert hit is False
        assert metrics["listed_months_est"] > 12

    def test_window_truncation_means_old_listing(self):
        """最早日期贴近窗口起点 -> 视为上市够久（关键修复）。"""
        window_start = TODAY - dt.timedelta(days=365 * 3)
        daily = make_daily([
            (window_start.isoformat(), 10.0, 1000, 1e7),
            ("2026-10-30", 11.0, 1200, 1.2e7),
        ])
        hit, _, metrics = F.check_listing_months(daily, 12, TODAY, lookback_years=3)
        assert hit is False
        assert ">" in str(metrics["listed_months_est"])

    def test_empty_daily_passes(self):
        hit, _, _ = F.check_listing_months(None, 12, TODAY)
        assert hit is False


@pytest.mark.unit
class TestAvgAmount:
    def test_low_liquidity_rejected(self):
        """日均成交额不足 -> 剔除。"""
        daily = make_daily([("2026-10-30", 10.0, 1000, 1e6)] * 20)
        hit, reason, metrics = F.check_avg_amount(daily, 5e7, 20)
        assert hit is True
        assert "亿" in reason
        assert metrics["avg_amount_yuan"] == pytest.approx(1e6)

    def test_high_liquidity_passes(self):
        daily = make_daily([("2026-10-30", 10.0, 1000, 1e8)] * 20)
        hit, _, metrics = F.check_avg_amount(daily, 5e7, 20)
        assert hit is False
        assert metrics["avg_amount_yi"] == pytest.approx(1.0)

    def test_uses_only_recent_days(self):
        """只看近 N 日，历史高成交不影响。"""
        rows = [("2025-01-02", 10.0, 1000, 1e9)] * 10  # 历史很高
        rows += [("2026-10-30", 10.0, 1000, 1e6)] * 20  # 近期很低
        daily = make_daily(rows)
        hit, _, _ = F.check_avg_amount(daily, 5e7, 20)
        assert hit is True

    def test_missing_amount_column(self):
        frame = pd.DataFrame({"date": [dt.date(2026, 10, 30)], "close": [10.0]})
        hit, reason, _ = F.check_avg_amount(frame, 5e7, 20)
        assert hit is True
        assert "缺成交额" in reason


@pytest.mark.unit
class TestSuspended:
    def test_all_zero_volume_rejected(self):
        daily = make_daily([("2026-10-30", 10.0, 0, 0)] * 5)
        hit, reason, metrics = F.check_suspended(daily, 5)
        assert hit is True
        assert metrics["zero_volume_days"] == 5

    def test_some_trading_passes(self):
        daily = make_daily([("2026-10-30", 10.0, 1000, 1e7)] * 5)
        hit, _, _ = F.check_suspended(daily, 5)
        assert hit is False

    def test_partial_zero_days_passes(self):
        rows = [("2026-10-30", 10.0, 0, 0)] * 2
        rows += [("2026-10-30", 10.0, 1000, 1e7)] * 3
        daily = make_daily(rows)
        hit, _, metrics = F.check_suspended(daily, 5)
        assert hit is False
        assert metrics["zero_volume_days"] == 2


@pytest.mark.unit
class TestApplyAll:
    RULES = {
        "exclude_st": {"enabled": True, "keywords": ["ST"]},
        "min_listing_months": {"enabled": True, "months": 12, "lookback_years": 3},
        "min_avg_amount": {"enabled": True, "min_amount_yuan": 5e7, "lookback_days": 20},
        "exclude_suspended": {"enabled": True, "no_trade_days": 5},
    }

    def test_st_rejected_first(self):
        """ST 是最便宜的判断，应最先短路。"""
        stock = {"code": "000001", "name": "ST测试", "exchange": "深圳证券交易所"}
        result = F.apply_all(stock, None, self.RULES, TODAY)
        assert result.passed is False
        assert result.reject_rule == "exclude_st"

    def test_all_pass(self):
        stock = {"code": "600519", "name": "贵州茅台", "exchange": "上海证券交易所"}
        daily = make_daily([("2020-01-02", 10.0, 1000, 1e8)] * 20)
        result = F.apply_all(stock, daily, self.RULES, TODAY)
        assert result.passed is True
        assert result.metrics["avg_amount_yi"] == pytest.approx(1.0)

    def test_rule_disabled(self):
        """规则关掉后不生效。"""
        rules = {**self.RULES, "exclude_st": {"enabled": False}}
        stock = {"code": "000001", "name": "ST测试", "exchange": "深圳证券交易所"}
        daily = make_daily([("2020-01-02", 10.0, 1000, 1e8)] * 20)
        result = F.apply_all(stock, daily, rules, TODAY)
        assert result.passed is True


@pytest.mark.unit
class TestSummarize:
    def test_counts_by_rule(self):
        results = [
            F.FilterResult("1", "a", True),
            F.FilterResult("2", "b", True),
            F.FilterResult("3", "c", False, "exclude_st", "ST"),
            F.FilterResult("4", "d", False, "exclude_st", "ST"),
            F.FilterResult("5", "e", False, "min_avg_amount", "低"),
        ]
        rules = {
            "exclude_st": {"enabled": True},
            "min_avg_amount": {"enabled": True},
        }
        summary = F.summarize(results, rules)
        assert summary["total"] == 5
        assert summary["passed"] == 2
        assert summary["rejected"] == 3
        assert summary["by_rule"]["exclude_st"] == 2
        assert summary["by_rule"]["min_avg_amount"] == 1
