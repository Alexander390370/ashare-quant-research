"""三个缺口补齐后的单元测试。

不触网，只测纯逻辑：
1. 港股代码映射与报表参数校验
2. A 股 point-in-time 过滤规则
3. 宏观指标的别名解析与排序方向判断
"""

import pytest

from tradingagents.dataflows.vendors.akshare.disclosure import (
    filter_by_point_in_time,
    infer_period,
    is_a_share,
    to_plain_symbol,
)
from tradingagents.dataflows.vendors.akshare.hk_financials import (
    is_hk,
    to_akshare_hk_symbol,
)
from tradingagents.dataflows.vendors.akshare.macro import _resolve, available_indicators


@pytest.mark.unit
class TestHKSymbolMapping:
    @pytest.mark.parametrize(
        "ticker,expected",
        [
            ("0700.HK", "00700"),
            ("700.HK", "00700"),
            ("9988.HK", "09988"),
            ("0001.HK", "00001"),
        ],
    )
    def test_hk_symbol_zero_padded_to_five(self, ticker, expected):
        assert to_akshare_hk_symbol(ticker) == expected

    @pytest.mark.parametrize("ticker", ["600519.SS", "AAPL"])
    def test_non_hk_not_padded(self, ticker):
        assert to_akshare_hk_symbol(ticker) == ticker

    @pytest.mark.parametrize("ticker", ["0700.HK", "9988.hk"])
    def test_is_hk(self, ticker):
        assert is_hk(ticker) is True

    @pytest.mark.parametrize("ticker", ["600519.SS", "AAPL", "0700"])
    def test_is_not_hk(self, ticker):
        assert is_hk(ticker) is False


@pytest.mark.unit
class TestPointInTime:
    def test_a_share_detection(self):
        assert is_a_share("600519.SS") is True
        assert is_a_share("000001.SZ") is True
        assert is_a_share("AAPL") is False

    def test_symbol_stripping(self):
        assert to_plain_symbol("600519.SS") == "600519"
        assert to_plain_symbol("000001.SZ") == "000001"

    def test_filter_keeps_only_announced_before_cutoff(self):
        items = [
            {"title": "一季报", "published": "2026-04-25", "kind": "report"},
            {"title": "半年报", "published": "2026-08-15", "kind": "report"},
            {"title": "三季报", "published": "2026-10-25", "kind": "report"},
        ]
        kept = filter_by_point_in_time(items, "2026-08-14")
        assert [i["title"] for i in kept] == ["一季报"]

    def test_filter_includes_announcement_on_the_day(self):
        """公告当天就应可见——这是 point-in-time 的边界。"""
        items = [{"title": "半年报", "published": "2026-08-15", "kind": "report"}]
        assert len(filter_by_point_in_time(items, "2026-08-15")) == 1
        assert len(filter_by_point_in_time(items, "2026-08-14")) == 0

    def test_filter_drops_unparseable_dates(self):
        items = [{"title": "坏数据", "published": "", "kind": "other"}]
        assert filter_by_point_in_time(items, "2026-10-27") == []

    def test_filter_rejects_invalid_as_of(self):
        with pytest.raises(ValueError):
            filter_by_point_in_time([], "not-a-date")

    @pytest.mark.parametrize(
        "title,expected",
        [
            ("贵州茅台2026年半年度报告", "06-30"),
            ("某某2026年第一季度报告", "03-31"),
            ("某某2026年第三季度报告", "09-30"),
            ("某某2025年年度报告", "12-31"),
            ("关于召开业绩说明会的公告", None),
        ],
    )
    def test_infer_period_from_title(self, title, expected):
        assert infer_period(title) == expected


@pytest.mark.unit
class TestMacroIndicators:
    def test_available_indicators_non_empty(self):
        keys = available_indicators()
        assert "ppi" in keys
        assert "pmi" in keys
        assert "lpr" in keys

    @pytest.mark.parametrize(
        "alias,expected",
        [
            ("利率", "lpr"),
            ("货币", "m2"),
            ("经济", "gdp"),
            ("制造业", "pmi"),
            ("储备", "fx"),
        ],
    )
    def test_aliases_resolve(self, alias, expected):
        assert _resolve(alias) == expected

    def test_unknown_indicator_raises(self):
        with pytest.raises(ValueError, match="未知宏观指标"):
            _resolve("不存在的指标")

    def test_cpi_gives_explicit_unavailable_message(self):
        """CPI 源端不可用，应给出明确说明而不是笼统报错。"""
        with pytest.raises(ValueError, match="CPI"):
            _resolve("cpi")

    def test_recent_handles_descending_order(self):
        """PPI/PMI/GDP 是降序（首行最新），应直接取前 N。"""
        from tradingagents.dataflows.vendors.akshare.macro import _recent

        rows = [
            {"月份": "2026年09月份", "v": 1},
            {"月份": "2026年08月份", "v": 2},
            {"月份": "2026年07月份", "v": 3},
        ]
        out = _recent(rows, 2)
        assert [r["月份"] for r in out] == ["2026年09月份", "2026年08月份"]

    def test_recent_handles_ascending_order(self):
        """FX 是升序（尾行最新），应取尾 N 并翻转。"""
        from tradingagents.dataflows.vendors.akshare.macro import _recent

        rows = [
            {"月份": "2026年07月份", "v": 3},
            {"月份": "2026年08月份", "v": 2},
            {"月份": "2026年09月份", "v": 1},
        ]
        out = _recent(rows, 2)
        assert [r["月份"] for r in out] == ["2026年09月份", "2026年08月份"]
