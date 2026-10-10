"""一次重放要用的全部行情与事件：**批量取，不逐标的取**。

形态是被实测逼出来的（SPEC §7）：20 只标的 × 181 个交易日，逐标的取数 4,958ms，
一次批量 480ms（10×）——差的全是每次查询各自的「建连接 + 建视图」。而引擎里最贵的单项
是它的 `_limit_bands`（raw **全史** + `Decimal` 逐日算，单标的 403ms）：
模拟盘只需要**窗口内**的涨跌停价，故这里只在窗口前补一段 raw 即可。

**两条路径必须同源**：涨跌停价用同一批纯函数（`limit_pct` / `limit_band` / 名称字典的 ST 判定），
且「窗口口径算出的价 == 引擎全史口径算出的同一批日子」是一条断言（`tests/test_paper_market.py`）。
降级口径也照抄引擎：判不出板别就**不给 band**（`limit_check="skipped"`，放行 + 如实标注），
而不是拒单——拒单会让模拟盘凭空少掉一批成交。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from app.backtest.a_share_rules import LimitBand, limit_band, limit_pct
from app.backtest.engine import RuleStatus
from app.backtest.events import EventFeed, build_feed
from app.backtest.types import Bar, Mode
from app.data import calendar as cal
from app.data import duckdb_client as dc
from app.data import naming
from app.paper import PaperError
from app.paper.types import PaperConfig

#: raw 补窗的自然日：窗口第一根的涨跌停价要它**之前**那根的 raw 收盘价。
#: 取 90 天不是随手写的——长期停牌的标的要靠它把「停牌前最后一个收盘价」捞进来；
#: 真要更长（停牌超过 90 天），那一天就按「无 band」放行并如实标注（同引擎的降级姿态）。
RAW_PADDING_DAYS = 90


@dataclass(frozen=True, slots=True)
class SymbolData:
    """单只标的在本次重放里的一切：bars（含下标索引）、涨跌停带、规则生效情况。"""

    symbol: str
    bars: tuple[Bar, ...]
    #: `trade_date → 该标的 bars 里的下标`（`BarContext.index` 与 T+1 判据都用它）
    index: Mapping[date, int]
    bands: Mapping[date, LimitBand]
    rule: RuleStatus

    def bar_at(self, day: date) -> Bar | None:
        position = self.index.get(day)
        return None if position is None else self.bars[position]


@dataclass(frozen=True, slots=True)
class MarketData:
    """账户推进用的交易日序列 + 每只标的的数据 + 每只标的的事件行。"""

    days: tuple[date, ...]
    symbols: Mapping[str, SymbolData]
    events: Mapping[str, tuple[dict, ...]]

    def feed_for(self, symbol: str, mode: Mode) -> EventFeed:
        """每只标的**一份 feed**——与引擎逐标的的语义一致（事件表本来就是按标的取的）。"""
        return build_feed(self.events.get(symbol, ()), mode)


def load_market_data(config: PaperConfig, *, data_dir: Path | None = None) -> MarketData:
    """取数并组装（一次连接三条通道）。

    **事件不做时间过滤**：`dc.events()` 的窗口打在 `event_time` 上，拿它预筛会误删
    「事发在窗口前、窗口内才可得」的事件（与引擎同一条理由）。可见性一律交给 feed。
    """
    try:
        days = tuple(cal.sessions(config.start, config.end))
    except cal.CalendarOutOfRange as exc:  # 越界不是休市——如实报错而不是静默跑空
        raise PaperError(str(exc)) from exc
    if len(days) < 2:
        raise PaperError(
            f"区间 {config.start} → {config.end} 内只有 {len(days)} 个交易日；"
            "模拟盘至少要两个（T 日生成决策、T+1 开盘成交）"
        )

    symbols = list(config.symbols)
    raw_start = (config.start - timedelta(days=RAW_PADDING_DAYS)).isoformat()
    con = dc.connect(data_dir)
    try:
        qfq_rows = dc.bars_multi(
            symbols,
            start=config.start.isoformat(),
            end=config.end.isoformat(),
            adjust="qfq",
            con=con,
        )
        raw_rows = dc.bars_multi(
            symbols, start=raw_start, end=config.end.isoformat(), adjust="raw", con=con
        )
        event_rows = dc.events_multi(symbols, con=con)
    finally:
        con.close()

    missing = [s for s in symbols if not any(row["symbol"] == s for row in qfq_rows)]
    if missing:
        raise PaperError(
            f"{'、'.join(missing)} 在 {config.start} → {config.end} 没有行情数据；"
            "换标的或调区间（先跑 scripts/download_bars.py 也可）"
        )

    dictionary = naming.load_dictionary(data_dir)
    st = naming.st_symbols(dictionary)

    by_symbol: dict[str, list[dict]] = {s: [] for s in symbols}
    for row in qfq_rows:
        by_symbol[row["symbol"]].append(row)
    raw_by_symbol: dict[str, list[dict]] = {s: [] for s in symbols}
    for row in raw_rows:
        raw_by_symbol[row["symbol"]].append(row)

    per_symbol: dict[str, SymbolData] = {}
    for symbol in symbols:
        bars = tuple(Bar.from_row(row) for row in by_symbol[symbol])
        bands, rule = _bands_for(
            symbol,
            raw_by_symbol[symbol],
            is_st=symbol in st,
            window=(config.start, config.end),
        )
        per_symbol[symbol] = SymbolData(
            symbol=symbol,
            bars=bars,
            index={bar.trade_date: i for i, bar in enumerate(bars)},
            bands=bands,
            rule=rule,
        )

    events: dict[str, list[dict]] = {s: [] for s in symbols}
    for row in event_rows:
        for code in row.get("symbols") or ():
            if code in events:
                events[code].append(row)

    return MarketData(
        days=days,
        symbols=per_symbol,
        events={s: tuple(rows) for s, rows in events.items()},
    )


def _bands_for(
    symbol: str, raw_rows: Sequence[dict], *, is_st: bool, window: tuple[date, date]
) -> tuple[dict[date, LimitBand], RuleStatus]:
    """按 **raw 前收**算窗口内每根 bar 的涨跌停价，与 `engine._limit_bands` 同口径。

    三处降级与引擎逐条相同（都往「判大 / 跳过」一侧倒，见 `a_share_rules` 的不对称性说明）：
    板别认不出 → 不给 band；raw 序列不成序列 → 全不给；ST 判定取**当前**名字
    （名称只在事件语料覆盖期内有，更早的窗口查不到名字，按板块默认幅度）。

    raw 是**补窗读的**（要窗口第一根的前收），但返回的 bands **裁到窗口内**——
    补窗那几天算得出来却永远查不到，留着只会让「这批 band 是什么范围」变得含糊。
    """
    pct = limit_pct(symbol, is_st=is_st)
    if pct is None:
        return {}, RuleStatus("skipped", "unknown_board", is_st, None)

    closes = [(row["trade_date"], row["close"]) for row in raw_rows if row.get("close") is not None]
    if len(closes) < 2:
        return {}, RuleStatus("skipped", "no_raw_series", is_st, pct)

    start, end = window
    bands = {
        day: limit_band(prev_close, pct)
        for (_, prev_close), (day, _) in zip(closes, closes[1:], strict=False)
        if start <= day <= end
    }
    return bands, RuleStatus("on", None, is_st, pct)


__all__ = ["RAW_PADDING_DAYS", "MarketData", "SymbolData", "load_market_data"]
