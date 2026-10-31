"""AKShare 新闻 vendor 的单元测试。

不触网：只测纯逻辑部分（代码映射、A 股判定、日期解析、区间过滤）。
真实取数由 tools/check_data_coverage.py 做集成验证。
"""

import pytest

from tradingagents.dataflows.vendors.akshare.news import (
    _parse_date,
    is_a_share,
    to_akshare_symbol,
)


@pytest.mark.unit
class TestSymbolMapping:
    @pytest.mark.parametrize(
        "ticker,expected",
        [
            ("600519.SS", "600519"),
            ("000001.SZ", "000001"),
            ("430047.BJ", "430047"),
            ("600519.ss", "600519"),
            ("600519.SS", "600519"),
        ],
    )
    def test_yahoo_suffix_is_stripped(self, ticker, expected):
        assert to_akshare_symbol(ticker) == expected

    @pytest.mark.parametrize("ticker", ["AAPL", "0700.HK", "7203.T"])
    def test_non_a_share_passes_through(self, ticker):
        assert to_akshare_symbol(ticker) == ticker


@pytest.mark.unit
class TestIsAShare:
    @pytest.mark.parametrize("ticker", ["600519.SS", "000001.SZ", "430047.BJ"])
    def test_a_share_suffixes(self, ticker):
        assert is_a_share(ticker) is True

    @pytest.mark.parametrize("ticker", ["AAPL", "0700.HK", "600519", "", "BTC-USD"])
    def test_non_a_share(self, ticker):
        assert is_a_share(ticker) is False


@pytest.mark.unit
class TestDateParsing:
    @pytest.mark.parametrize(
        "value,expected_year",
        [
            ("2026-10-26", 2026),
            ("2026/10/26", 2026),
            ("20261026", 2026),
            ("2026-10-26 15:30:00", 2026),
        ],
    )
    def test_common_formats(self, value, expected_year):
        parsed = _parse_date(value)
        assert parsed is not None
        assert parsed.year == expected_year

    @pytest.mark.parametrize("value", ["", None, "not-a-date", "----"])
    def test_unparseable_returns_none(self, value):
        assert _parse_date(value) is None
