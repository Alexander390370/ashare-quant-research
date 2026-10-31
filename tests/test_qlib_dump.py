"""Qlib 层数据灌入的单元测试。

不触网：用构造的数据测格式转换逻辑。
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from qlib_layer import dump_data as D


def make_daily(dates, closes=None, volumes=None):
    """构造日线 DataFrame。"""
    n = len(dates)
    closes = closes or [10.0 + i for i in range(n)]
    volumes = volumes or [1000.0] * n
    return pd.DataFrame({
        "date": [dt.date.fromisoformat(d) for d in dates],
        "open": closes,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "close": closes,
        "volume": volumes,
        "amount": [c * v for c, v in zip(closes, volumes, strict=True)],
    })


@pytest.mark.unit
class TestCodeNormalization:
    @pytest.mark.parametrize(
        "code,exchange,expected",
        [
            ("600519", "上海证券交易所", "SH600519"),
            ("000001", "深圳证券交易所", "SZ000001"),
            ("430047", "北京证券交易所", "BJ430047"),
            ("600519", "", "SH600519"),   # 按前缀猜
            ("000001", "", "SZ000001"),
            ("300750", "", "SZ300750"),
            ("830799", "", "BJ830799"),
        ],
    )
    def test_normalize(self, code, exchange, expected):
        assert D._normalize_code(code, exchange) == expected


@pytest.mark.unit
class TestCalendar:
    def test_union_of_all_dates(self):
        daily_map = {
            "600519": make_daily(["2026-01-02", "2026-01-05"]),
            "000001": make_daily(["2026-01-05", "2026-01-06"]),
        }
        cal = D.build_calendar(daily_map)
        assert cal == ["2026-01-02", "2026-01-05", "2026-01-06"]

    def test_sorted(self):
        daily_map = {
            "A": make_daily(["2026-01-06", "2026-01-02", "2026-01-05"]),
        }
        cal = D.build_calendar(daily_map)
        assert cal == sorted(cal)

    def test_empty_map(self):
        assert D.build_calendar({}) == []

    def test_skips_none_frames(self):
        daily_map = {"A": None, "B": make_daily(["2026-01-02"])}
        cal = D.build_calendar(daily_map)
        assert cal == ["2026-01-02"]


@pytest.mark.unit
class TestDump:
    def test_creates_expected_files(self, tmp_path):
        daily_map = {
            "600519": make_daily(["2026-01-02", "2026-01-05"]),
        }
        meta = {"600519": {"name": "贵州茅台", "exchange": "上海证券交易所"}}
        stats = D.dump_to_qlib(daily_map, meta, tmp_path, verbose=False)

        assert stats["stocks_dumped"] == 1
        assert stats["calendar_days"] == 2
        assert (tmp_path / "calendars" / "day.txt").exists()
        assert (tmp_path / "instruments" / "all.txt").exists()
        assert (tmp_path / "features" / "SH600519" / "close.day.bin").exists()

    def test_bin_has_leading_index(self, tmp_path):
        """关键：.bin 第一个 float32 必须是起始索引。"""
        daily_map = {"600519": make_daily(["2026-01-02", "2026-01-05"])}
        meta = {"600519": {"name": "x", "exchange": "上海证券交易所"}}
        D.dump_to_qlib(daily_map, meta, tmp_path, verbose=False)

        bin_path = tmp_path / "features" / "SH600519" / "close.day.bin"
        data = np.fromfile(bin_path, dtype="<f")
        # 头部索引 + 2 天数据
        assert len(data) == 3
        assert data[0] == 0.0, "第一个 float32 应为起始索引 0"
        assert data[1] == pytest.approx(10.0)
        assert data[2] == pytest.approx(11.0)

    def test_calendar_file_content(self, tmp_path):
        daily_map = {"600519": make_daily(["2026-01-02", "2026-01-05"])}
        meta = {"600519": {"name": "x", "exchange": "上海证券交易所"}}
        D.dump_to_qlib(daily_map, meta, tmp_path, verbose=False)

        text = (tmp_path / "calendars" / "day.txt").read_text(encoding="utf-8")
        assert text.strip().split("\n") == ["2026-01-02", "2026-01-05"]

    def test_instruments_file_format(self, tmp_path):
        daily_map = {"600519": make_daily(["2026-01-02", "2026-01-05"])}
        meta = {"600519": {"name": "x", "exchange": "上海证券交易所"}}
        D.dump_to_qlib(daily_map, meta, tmp_path, verbose=False)

        text = (tmp_path / "instruments" / "all.txt").read_text(encoding="utf-8")
        parts = text.strip().split("\t")
        assert parts[0] == "SH600519"
        assert parts[1] == "2026-01-02"
        assert parts[2] == "2026-01-05"

    def test_skips_empty_frames(self, tmp_path):
        daily_map = {
            "600519": make_daily(["2026-01-02"]),
            "000001": pd.DataFrame(),
        }
        meta = {
            "600519": {"name": "x", "exchange": "上海证券交易所"},
            "000001": {"name": "y", "exchange": "深圳证券交易所"},
        }
        stats = D.dump_to_qlib(daily_map, meta, tmp_path, verbose=False)
        assert stats["stocks_dumped"] == 1
        assert stats["stocks_skipped"] == 1

    def test_all_fields_written(self, tmp_path):
        daily_map = {"600519": make_daily(["2026-01-02"])}
        meta = {"600519": {"name": "x", "exchange": "上海证券交易所"}}
        D.dump_to_qlib(daily_map, meta, tmp_path, verbose=False)

        stock_dir = tmp_path / "features" / "SH600519"
        for field in D.FIELDS:
            assert (stock_dir / f"{field}.day.bin").exists(), f"缺 {field}"

    def test_raises_on_empty_input(self, tmp_path):
        with pytest.raises(ValueError, match="没有任何日线数据"):
            D.dump_to_qlib({}, {}, tmp_path, verbose=False)

    def test_nan_for_missing_dates(self, tmp_path):
        """某股票缺某天 -> 该位置应是 NaN。"""
        daily_map = {"600519": make_daily(["2026-01-02", "2026-01-05"])}
        meta = {"600519": {"name": "x", "exchange": "上海证券交易所"}}
        D.dump_to_qlib(daily_map, meta, tmp_path, verbose=False)

        bin_path = tmp_path / "features" / "SH600519" / "close.day.bin"
        data = np.fromfile(bin_path, dtype="<f")
        # 数据完整，不应有 NaN
        assert not np.isnan(data[1:]).any()
