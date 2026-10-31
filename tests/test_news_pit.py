"""新闻 point-in-time 裁剪的单元测试。

不触网：纯文本处理。
"""

import pytest

from tradingagents.dataflows.vendors.akshare import news_pit as N


@pytest.mark.unit
class TestDateParsing:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("2026-10-20 15:30", (2026, 10, 20)),
            ("2026/10/20", (2026, 10, 20)),
            ("2026年10月20日", (2026, 10, 20)),
            ("发布于 2026-8-5", (2026, 8, 5)),
        ],
    )
    def test_formats(self, text, expected):
        parsed = N._parse_date(text)
        assert parsed is not None
        assert (parsed.year, parsed.month, parsed.day) == expected

    @pytest.mark.parametrize("text", ["", "no date", "2026-13-45"])
    def test_invalid(self, text):
        assert N._parse_date(text) is None


@pytest.mark.unit
class TestTrim:
    SAMPLE = """## 600519.SS News

### 1. 半年报发布
- 2026-08-15 18:00 · 东方财富
半年报正文

### 2. 三季报预告
- 2026-10-20 09:00 · 证券时报
三季报正文

### 3. 无日期条目
没有日期信息
"""

    def test_removes_future_items(self):
        out = N.trim_news_to_date(self.SAMPLE, "2026-08-14")
        assert "半年报发布" not in out
        assert "三季报预告" not in out
        assert "无日期条目" in out  # 无日期保留

    def test_keeps_all_when_cutoff_is_late(self):
        out = N.trim_news_to_date(self.SAMPLE, "2026-12-31")
        assert out == self.SAMPLE  # 无需删时原样返回

    def test_partial_removal(self):
        out = N.trim_news_to_date(self.SAMPLE, "2026-09-01")
        assert "半年报发布" in out
        assert "三季报预告" not in out

    def test_all_removed_gives_explicit_message(self):
        out = N.trim_news_to_date(self.SAMPLE, "2026-01-01")
        assert "没有已发布的新闻" in out or "Point-in-time" in out

    def test_adds_notice_when_dropped(self):
        out = N.trim_news_to_date(self.SAMPLE, "2026-09-01")
        assert "Point-in-time" in out
        assert "移除" in out

    def test_invalid_cutoff_returns_unchanged(self):
        assert N.trim_news_to_date(self.SAMPLE, "not-a-date") == self.SAMPLE

    def test_empty_text(self):
        assert N.trim_news_to_date("", "2026-08-14") == ""

    def test_no_items_returns_unchanged(self):
        text = "just a plain sentence without items"
        assert N.trim_news_to_date(text, "2026-08-14") == text


@pytest.mark.unit
class TestConfigToggle:
    def test_default_enabled(self):
        assert N.trim_enabled(None) is True
        assert N.trim_enabled({}) is True

    def test_can_disable(self):
        assert N.trim_enabled({"news_point_in_time": False}) is False
