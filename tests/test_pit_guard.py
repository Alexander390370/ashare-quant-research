"""point-in-time 门禁的单元测试。

不触网：把公告查询与法定截止判定分开测。
"""

import datetime as dt

import pytest

from tradingagents.dataflows.vendors.akshare import pit_guard as P


@pytest.mark.unit
class TestQuarterEnds:
    def test_only_past_quarters(self):
        as_of = dt.date(2026, 8, 14)
        ends = P.quarter_ends_up_to(as_of)
        assert ends[0] == dt.date(2026, 6, 30)
        assert all(e <= as_of for e in ends)

    def test_sorted_newest_first(self):
        ends = P.quarter_ends_up_to(dt.date(2026, 10, 27))
        assert ends == sorted(ends, reverse=True)

    def test_excludes_future_quarter(self):
        ends = P.quarter_ends_up_to(dt.date(2026, 8, 14))
        assert dt.date(2026, 9, 30) not in ends


@pytest.mark.unit
class TestStatutoryDeadline:
    @pytest.mark.parametrize(
        "period,expected",
        [
            (dt.date(2026, 3, 31), dt.date(2026, 4, 30)),
            (dt.date(2026, 6, 30), dt.date(2026, 8, 31)),
            (dt.date(2026, 9, 30), dt.date(2026, 10, 31)),
            (dt.date(2025, 12, 31), dt.date(2026, 4, 30)),
        ],
    )
    def test_deadlines(self, period, expected):
        assert P._statutory_deadline(period) == expected


@pytest.mark.unit
class TestLatestPublishedPeriod:
    def test_deadline_passed_means_published(self):
        """法定截止日已过 -> 必然已公布，无需查公告。"""
        period, reason = P.latest_published_period("600519.SS", "2026-05-15")
        assert period == dt.date(2026, 3, 31)
        assert "法定" in reason

    def test_quarter_end_but_before_deadline_falls_back(self, monkeypatch):
        """报告期已过但截止日未到 -> 需要查公告；查不到则退回上一期。"""
        monkeypatch.setattr(P, "quarter_ends_up_to", lambda as_of, years_back=3: [dt.date(2026, 6, 30)])
        # 让公告查询失败，模拟"没有公告证据"
        import tradingagents.dataflows.vendors.akshare.disclosure as D

        def boom(*a, **k):
            raise ValueError("no announcements")

        monkeypatch.setattr(D, "get_announcements", boom)
        period, reason = P.latest_published_period("600519.SS", "2026-07-01")
        assert period is None

    def test_non_a_share_returns_none(self):
        period, reason = P.latest_published_period("AAPL", "2026-07-01")
        assert period is None
        assert "非 A 股" in reason

    def test_invalid_date_returns_none(self):
        period, reason = P.latest_published_period("600519.SS", "not-a-date")
        assert period is None


@pytest.mark.unit
class TestTruncation:
    HEADER = ",2026-06-30,2026-03-31,2025-12-31"
    ROW = "Total Assets,100,90,80"

    def test_removes_later_columns(self):
        text = "\n".join([self.HEADER, self.ROW])
        out = P.truncate_to_published(text, dt.date(2026, 3, 31))
        assert "2026-06-30" not in out
        assert "2026-03-31" in out
        assert "2025-12-31" in out

    def test_keeps_row_cell_alignment(self):
        text = "\n".join([self.HEADER, self.ROW])
        out = P.truncate_to_published(text, dt.date(2026, 3, 31))
        lines = out.splitlines()
        assert lines[0].count(",") == lines[1].count(",")

    def test_none_period_returns_unchanged(self):
        text = "\n".join([self.HEADER, self.ROW])
        assert P.truncate_to_published(text, None) == text

    def test_text_without_dates_unchanged(self):
        text = "no dates here\njust text"
        assert P.truncate_to_published(text, dt.date(2026, 3, 31)) == text

    def test_markdown_pipe_table(self):
        text = "| 科目 | 2026-06-30 | 2026-03-31 |\n|---|---|---|\n| 资产 | 100 | 90 |"
        out = P.truncate_to_published(text, dt.date(2026, 3, 31))
        assert "2026-06-30" not in out
        assert "2026-03-31" in out


@pytest.mark.unit
class TestEnrich:
    def test_non_a_share_passthrough(self):
        text = "some data 2026-06-30"
        assert P.enrich_with_pit("AAPL", "2026-08-14", text) == text

    def test_future_date_passthrough(self):
        """分析日期 >= 今天 -> 原样返回，不做截断。"""
        text = "some data 2026-06-30"
        future = (dt.date.today() + dt.timedelta(days=30)).isoformat()
        assert P.enrich_with_pit("600519.SS", future, text) == text

    def test_adds_notice_and_truncates(self, monkeypatch):
        monkeypatch.setattr(
            P, "latest_published_period",
            lambda t, d: (dt.date(2026, 3, 31), "测试用"),
        )
        text = ",2026-06-30,2026-03-31\nTotal,100,90"
        out = P.enrich_with_pit("600519.SS", "2026-08-14", text, "Balance Sheet")
        assert "Point-in-time" in out
        assert "2026-03-31" in out
        assert "2026-06-30" not in out

    def test_failure_is_non_blocking(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("network down")

        monkeypatch.setattr(P, "latest_published_period", boom)
        text = "original data"
        assert P.enrich_with_pit("600519.SS", "2026-08-14", text) == text
