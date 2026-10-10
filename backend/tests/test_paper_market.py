"""M6 取数层单测：批量取数、**窗口涨跌停价 == 引擎全史口径**、降级如实标注、坏输入明说。

那条同源断言（`test_window_bands_equal_the_engine_full_history_bands`）是 SPEC §7 写死的：
模拟盘不走引擎的 `_load_bars` / `_limit_bands`（逐标的 raw 全史 403ms/标的，20 只就 8 秒），
而是批量取 + 只算窗口内——**两条路径算出的涨跌停价必须逐日相等**，否则「与回测同口径」
就只剩一句话。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from app.backtest import engine as engine_module
from app.backtest.costs import CostModel
from app.backtest.engine import BacktestConfig
from app.backtest.types import Bar
from app.data import calendar as cal
from app.data import duckdb_client as dc
from app.paper import PaperError
from app.paper.market import load_market_data
from app.paper.types import PaperConfig
from tests.conftest import make_backtest_dir, write_bars_parquet

START = date(2026, 8, 3)
SYMBOL = "600519"


def sessions(count: int, start: date = START) -> list[date]:
    days = cal.sessions(start, start + timedelta(days=count * 3 + 10))
    assert len(days) >= count
    return days[:count]


def wavy_rows(days: list[date]) -> list[dict]:
    """价格每天涨一点、带一点波动——好让涨跌停价不是常数（常数会让同源断言失去分辨力）。"""
    return [
        {
            "trade_date": day,
            "open": 100.0 + 3 * i,
            "close": 100.0 + 3 * i + (1 if i % 2 else -1),
            "high": 100.0 + 3 * i + 2,
            "low": 100.0 + 3 * i - 2,
        }
        for i, day in enumerate(days)
    ]


def build_dir(tmp_path: Path, rows: list[dict], symbol: str = SYMBOL) -> Path:
    root = make_backtest_dir(tmp_path, rows, events=[], symbol=symbol)
    write_bars_parquet(root / "bars", symbol, rows, adjust="raw")
    return root


def config(start: date, end: date, **over: object) -> PaperConfig:
    base: dict = {
        "initial_cash": 1_000_000.0,
        "symbols": (SYMBOL,),
        "strategy": "ma_cross",
        "start": start,
        "end": end,
        "costs": CostModel(),
    }
    base.update(over)
    return PaperConfig(**base)  # type: ignore[arg-type]


def test_window_bands_equal_the_engine_full_history_bands(tmp_path: Path) -> None:
    days = sessions(8)
    root = build_dir(tmp_path, wavy_rows(days))
    window = (days[3], days[-1])

    # 引擎口径：raw **不带 start**（它要窗口第一根的前收，那在窗口之外），逐日 Decimal 算
    engine_config = BacktestConfig(
        symbol=SYMBOL, strategy="ma_cross", start=window[0], end=window[1], data_dir=root
    )
    engine_bars = [
        Bar.from_row(row)
        for row in dc.bars(
            SYMBOL, start=window[0].isoformat(), end=window[1].isoformat(), data_dir=root
        )
    ]
    engine_bands, engine_status = engine_module._limit_bands(engine_config, engine_bars)

    # 模拟盘口径：批量取数 + raw 补窗 90 天
    market = load_market_data(config(*window), data_dir=root)
    paper = market.symbols[SYMBOL]

    assert engine_status.limit_check == paper.rule.limit_check == "on"
    assert set(paper.bands) == {bar.trade_date for bar in engine_bars}
    for day, band in paper.bands.items():
        assert (band.up, band.down) == (engine_bands[day].up, engine_bands[day].down), day


def test_loader_batches_all_symbols_in_one_read(tmp_path: Path) -> None:
    days = sessions(5)
    rows = wavy_rows(days)
    build_dir(tmp_path, rows, symbol=SYMBOL)
    write_bars_parquet(tmp_path / "bars", "000001", rows, adjust="qfq")
    write_bars_parquet(tmp_path / "bars", "000001", rows, adjust="raw")

    market = load_market_data(config(days[0], days[-1], symbols=(SYMBOL, "000001")), data_dir=tmp_path)
    assert set(market.symbols) == {SYMBOL, "000001"}
    assert len(market.symbols[SYMBOL].bars) == len(days)
    assert market.days == tuple(days)


def test_missing_symbol_is_named_in_the_error(tmp_path: Path) -> None:
    days = sessions(4)
    build_dir(tmp_path, wavy_rows(days))
    with pytest.raises(PaperError) as caught:
        load_market_data(config(days[0], days[-1], symbols=(SYMBOL, "300750")), data_dir=tmp_path)
    assert "300750" in str(caught.value)


def test_single_day_window_is_rejected(tmp_path: Path) -> None:
    """一个交易日做不了模拟盘：T 日生成决策、T+1 开盘成交，至少要两天。"""
    days = sessions(4)
    build_dir(tmp_path, wavy_rows(days))
    with pytest.raises(PaperError) as caught:
        load_market_data(config(days[1], days[1]), data_dir=tmp_path)
    assert "至少要两个" in str(caught.value)


def test_unknown_board_degrades_to_no_band(tmp_path: Path) -> None:
    """板别认不出 ⇒ 不给 band（放行 + 如实标注），**不是拒单**——与引擎同一条降级姿态。"""
    days = sessions(4)
    rows = wavy_rows(days)
    root = make_backtest_dir(tmp_path, rows, events=[], symbol="123456")  # 前缀不在板别表里
    write_bars_parquet(root / "bars", "123456", rows, adjust="raw")

    market = load_market_data(config(days[0], days[-1], symbols=("123456",)), data_dir=root)
    rule = market.symbols["123456"].rule
    assert rule.limit_check == "skipped"
    assert rule.reason == "unknown_board"
    assert dict(market.symbols["123456"].bands) == {}
