"""多采样共识的单元测试。

不触网：用假 graph 模拟不同采样结果。
"""

import pytest

from tradingagents.consensus import (
    ConsensusResult,
    extract_rating,
    run_consensus,
)


@pytest.mark.unit
class TestExtractRating:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("**Rating**: Buy", "Buy"),
            ("**Rating**: sell", "Sell"),
            ("最终评级 **Rating**： Overweight", "Overweight"),
            ("no explicit rating, conclude Buy at the end", "Buy"),
            ("", None),
            ("nothing here", None),
        ],
    )
    def test_extraction(self, text, expected):
        assert extract_rating(text) == expected

    def test_takes_last_mention_without_explicit(self):
        text = "We considered Sell but finally recommend Buy"
        assert extract_rating(text) == "Buy"


@pytest.mark.unit
class TestConsensusResult:
    def test_majority_wins(self):
        r = ConsensusResult(
            ticker="X", date="2026-01-01", samples=3,
            ratings=["Buy", "Buy", "Hold"],
        )
        assert r.majority == "Buy"
        assert r.agreement == pytest.approx(2 / 3)

    def test_unanimous(self):
        r = ConsensusResult(
            ticker="X", date="2026-01-01", samples=3,
            ratings=["Sell", "Sell", "Sell"],
        )
        assert r.majority == "Sell"
        assert r.agreement == pytest.approx(1.0)
        assert r.direction_stable is True

    def test_tie_prefers_neutral(self):
        """并列时取方向最中性的，避免虚假方向性结论。"""
        r = ConsensusResult(
            ticker="X", date="2026-01-01", samples=2,
            ratings=["Buy", "Hold"],
        )
        assert r.majority == "Hold"

    def test_direction_unstable(self):
        r = ConsensusResult(
            ticker="X", date="2026-01-01", samples=3,
            ratings=["Buy", "Sell", "Hold"],
        )
        assert r.direction_stable is False

    def test_describe_unanimous(self):
        r = ConsensusResult(
            ticker="X", date="2026-01-01", samples=3,
            ratings=["Buy"] * 3,
        )
        text = r.describe()
        assert "完全一致" in text
        assert "100%" in text

    def test_describe_divergent(self):
        r = ConsensusResult(
            ticker="X", date="2026-01-01", samples=3,
            ratings=["Buy", "Sell", "Hold"],
        )
        text = r.describe()
        assert "分歧显著" in text

    def test_describe_all_failed(self):
        r = ConsensusResult(
            ticker="X", date="2026-01-01", samples=2,
            errors=["boom", "bang"],
        )
        text = r.describe()
        assert "全部失败" in text

    def test_empty_ratings_agreement_is_zero(self):
        r = ConsensusResult(ticker="X", date="2026-01-01", samples=1)
        assert r.agreement == 0.0
        assert r.majority is None


@pytest.mark.unit
class TestRunConsensus:
    def test_collects_ratings(self):
        class FakeGraph:
            def __init__(self):
                self.calls = 0
                self.ratings = ["Buy", "Buy", "Hold"]

            def propagate(self, ticker, date, asset_type, portfolio=None):
                rating = self.ratings[self.calls]
                self.calls += 1
                return {}, f"**Rating**: {rating}"

        graph = FakeGraph()
        result = run_consensus(graph, "X", "2026-01-01", samples=3)
        assert result.successful == 3
        assert result.majority == "Buy"
        assert graph.calls == 3

    def test_single_failure_does_not_abort(self):
        class FlakyGraph:
            def __init__(self):
                self.calls = 0

            def propagate(self, ticker, date, asset_type, portfolio=None):
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("transient")
                return {}, "**Rating**: Sell"

        result = run_consensus(FlakyGraph(), "X", "2026-01-01", samples=3)
        assert result.successful == 2
        assert len(result.errors) == 1
        assert result.majority == "Sell"

    def test_missing_rating_recorded_as_error(self):
        class NoRatingGraph:
            def propagate(self, ticker, date, asset_type, portfolio=None):
                return {}, "no rating in this text at all"

        result = run_consensus(NoRatingGraph(), "X", "2026-01-01", samples=2)
        assert result.successful == 0
        assert len(result.errors) == 2
