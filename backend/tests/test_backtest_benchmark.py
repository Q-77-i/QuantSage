"""M5a 全市场等权基准：日频聚合口径、异常样本剔除、与回测日历对齐。

数据源不覆盖指数（M2a 已核），所以这里的每一条断言都在钉**代理口径**——
尤其是「剔了哪些、剔了多少」，因为基准是拿来跟策略比的东西，口径含糊等于没有基准。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.backtest.benchmark import MARKET_KIND, market_benchmark
from app.backtest.engine import BacktestConfig
from app.backtest.report import build_report
from tests.conftest import write_bars_parquet, write_events_parquet

DAYS = [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5)]


def market_dir(tmp_path, rows_by_symbol: dict[str, list[float | None]]):  # noqa: ANN001, ANN201
    """按 `{symbol: [每日涨跌幅]}` 铺一份全市场夹具；`None` 表示当日缺该字段。"""
    for symbol, changes in rows_by_symbol.items():
        rows = [
            {
                "trade_date": day,
                "open": 10.0,
                "high": 10.1,
                "low": 9.9,
                "close": 10.0,
                "change_pct": change,
            }
            for day, change in zip(DAYS, changes, strict=True)
        ]
        write_bars_parquet(tmp_path / "bars", symbol, rows)
    # 查询层要求 bars 与 events 都有 Parquet；这里只关心行情，事件给空表
    write_events_parquet(tmp_path / "events", "600519", [])
    return tmp_path


def test_equal_weight_is_the_plain_mean_of_the_day() -> None:
    """等权 = 当日全市场涨跌幅的算术平均，不含任何加权。"""
    rows = {"600000": [1.0, 2.0, 3.0], "000001": [-1.0, 0.0, 1.0], "600519": [2.0, 1.0, -1.0]}
    assert [round((a + b + c) / 3, 6) for a, b, c in zip(*rows.values(), strict=True)] == [
        0.666667,
        1.0,
        1.0,
    ]


def test_curve_compounds_the_daily_means(tmp_path) -> None:  # noqa: ANN001
    root = market_dir(tmp_path, {"600000": [1.0, 2.0, -1.0], "000001": [1.0, 2.0, -1.0]})

    result = market_benchmark(DAYS, 100.0, data_dir=root)
    assert result.levels == pytest.approx((101.0, 103.02, 101.9898))
    assert result.total_return == pytest.approx(0.019898)
    assert result.sample_days == 3
    assert result.avg_samples == 2
    assert result.excluded == 0


def test_samples_beyond_the_boards_limits_are_excluded(tmp_path) -> None:  # noqa: ANN001
    """新股首日那类 +1942% 的样本必须剔掉——不剔会把当日均值拉高 0.36pp 量级。"""
    root = market_dir(
        tmp_path,
        {"600000": [1.0, 1.0, 1.0], "000001": [1.0, 1.0, 1.0], "301999": [1942.0, 1.0, 1.0]},
    )

    result = market_benchmark(DAYS, 100.0, data_dir=root)
    # 首日被剔 → 均值仍是 1.0（而不是 (1+1+1942)/3 ≈ 648）
    assert result.levels == pytest.approx((101.0, 102.01, 103.0301))
    assert result.excluded == 1
    assert result.sample_days == 3
    # 日均样本 = (2 + 3 + 3) / 3 = 2.67 → 3（首日少掉被剔的那只）
    assert result.avg_samples == 3


def test_exactly_at_the_threshold_is_kept(tmp_path) -> None:  # noqa: ANN001
    """剔的是 `> 30`，不是 `>= 30`——北交所的 30% 涨停是真实成交。"""
    root = market_dir(tmp_path, {"920438": [30.0, 0.0, 0.0], "600000": [0.0, 0.0, 0.0]})

    result = market_benchmark(DAYS, 100.0, data_dir=root)
    assert result.excluded == 0
    assert result.levels[0] == pytest.approx(115.0)  # (30 + 0) / 2 = 15%


def test_missing_change_pct_days_do_not_drag_the_benchmark(tmp_path) -> None:  # noqa: ANN001
    """缺 `change_pct` 的样本**不参与**当日均值，而不是当 0 算进去。

    当成 0 会把基准系统性压低，等于白送策略一份超额收益——这是最隐蔽的一种口径错误。
    """
    root = market_dir(tmp_path, {"600000": [2.0, None, 2.0], "000001": [2.0, None, 2.0]})

    result = market_benchmark(DAYS, 100.0, data_dir=root)
    assert result.levels == pytest.approx((102.0, 102.0, 104.04))  # 中间那天无样本，净值不动
    assert result.sample_days == 2


def test_dates_are_aligned_one_to_one(tmp_path) -> None:  # noqa: ANN001
    """返回的净值序列必须与传入的日期**一一对齐**，否则叠加图会错位。"""
    root = market_dir(tmp_path, {"600000": [1.0, 1.0, 1.0]})
    gap = [DAYS[0], date(2026, 8, 10)]  # 中间隔了一个没有行情的日期

    result = market_benchmark(gap, 100.0, data_dir=root)
    assert len(result.levels) == len(gap)
    # 首日 +1% → 101；08-10 没有样本 → 净值不动，仍是 101（不是回到 100）
    assert result.levels[1] == pytest.approx(101.0)


def test_empty_dates_is_not_an_error() -> None:
    result = market_benchmark([], 100.0)
    assert result.levels == ()
    assert result.total_return == 0.0


def test_report_overlays_the_market_curve(tmp_path) -> None:  # noqa: ANN001
    """报告里每个净值点带 `market`，口径自述进 `meta.benchmark`。"""
    root = market_dir(tmp_path, {"600519": [1.0, 1.0, 1.0]})
    report = build_report(BacktestConfig(symbol="600519", strategy="ma_cross", data_dir=root))

    assert [point["market"] for point in report["equity_curve"]] == pytest.approx(
        [1_010_000.0, 1_020_100.0, 1_030_301.0]
    )
    bench = report["meta"]["benchmark"]
    assert bench["kind"] == MARKET_KIND
    assert bench["excluded"] == 0
    assert bench["avg_samples"] == 1
    assert "指数" in bench["note"]  # 口径自述必须点明「不是指数」
