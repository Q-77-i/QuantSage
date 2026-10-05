"""T4 的 PIT 闸门——本项目护城河的唯一落点。

事件对某根 bar 是否可见，取决于拿哪个时间戳与该 bar 的**收盘时刻**比较：

- PIT 模式按 `available_at`（平台首次可用）设卡 → 15:00 之后才可得的消息顺延到下一交易日；
- 非 PIT 模式按 `event_time`（事发）设卡 → 等同假设「事发即知」，用于量化前视偏差虚高。

两者的全部差别压缩在 `EventView.stamp()` 一行里，下游 engine / strategy / result 零感知。
`duckdb_client.events()` 明确不做 PIT 过滤，设卡是消费方职责，故在此实现。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, time

from app.backtest.types import CN_TZ, Bar, EventView, Mode

#: 各模式的时间戳字段名——报告里原样打印，作为「本次按哪个字段设卡」的证据。
CUTOFF_FIELD: dict[Mode, str] = {
    Mode.PIT: "available_at",
    Mode.NON_PIT: "event_time",
}

#: A 股连续竞价收盘时刻。日线 bar 的「决策时刻」定在此，信号于此后生成。
MARKET_CLOSE = time(15, 0)


def bar_cutoff(trade_date: date) -> datetime:
    """bar 的收盘时刻 = `trade_date` 15:00 Asia/Shanghai（tz-aware，可与数据直接比较）。"""
    return datetime.combine(trade_date, MARKET_CLOSE, tzinfo=CN_TZ)


def parse_factor_score(raw: object) -> float | None:
    """从 `factor_scores` 落盘值解析出 `score`；任何异常形状一律返回 None（视为无分数）。

    ⚠️ 该列是**双重编码**的 JSON：落盘时把内层 JSON 文本又 JSON 编码了一次，
    经 arrow 读回 Python 后是「内容为 JSON 文本的 str」，故需 `json.loads` 两次。
    为兼容测试夹具与将来可能的落盘修正，单层 dict / 单层 JSON 文本也一并接受。
    """
    obj: object = raw
    if isinstance(obj, str):
        for _ in range(2):  # 最多解两层
            if not isinstance(obj, str):
                break
            try:
                obj = json.loads(obj)
            except (ValueError, TypeError):
                return None
    if not isinstance(obj, Mapping):
        return None
    score = obj.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None
    return float(score)


def event_from_row(row: Mapping[str, object]) -> EventView:
    """从 `duckdb_client.events()` 的行构造 `EventView`。"""
    return EventView(
        event_id=str(row.get("event_id") or ""),
        symbol=str(row.get("symbol") or ""),
        title=str(row.get("title") or ""),
        event_time=row["event_time"],  # type: ignore[arg-type]
        available_at=row["available_at"],  # type: ignore[arg-type]
        direction_norm=row.get("direction_norm"),  # type: ignore[arg-type]
        score=parse_factor_score(row.get("factor_scores")),
    )


class EventFeed:
    """按 bar 推进的事件游标。

    游标只前进，`advance()` 返回「本根新可见」的差集——同一事件不可能返回两次，
    策略因此不需要自己维护 `seen_ids`（去重是结构性的）。
    """

    def __init__(self, events: Sequence[EventView], mode: Mode) -> None:
        self._mode = mode
        self._queue: list[EventView] = sorted(events, key=lambda e: (e.stamp(mode), e.event_id))
        self._cursor = 0
        self._visible: list[EventView] = []

    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def cutoff_field(self) -> str:
        """本次设卡所用字段名（`available_at` / `event_time`）。"""
        return CUTOFF_FIELD[self._mode]

    @property
    def visible(self) -> tuple[EventView, ...]:
        """截至当前 bar 累计可见的全部事件。"""
        return tuple(self._visible)

    def advance(self, bar: Bar) -> tuple[EventView, ...]:
        """推进到该 bar 收盘时刻，返回本根新可见的事件。

        非交易日（周末/节假日）的事件无需特判：`<=` 累积会在下一根 bar 一起放行。
        """
        cutoff = bar_cutoff(bar.trade_date)
        fresh: list[EventView] = []
        while self._cursor < len(self._queue):
            candidate = self._queue[self._cursor]
            if candidate.stamp(self._mode) > cutoff:
                break
            fresh.append(candidate)
            self._cursor += 1
        self._visible.extend(fresh)
        return tuple(fresh)


def build_feed(rows: Iterable[Mapping[str, object]], mode: Mode) -> EventFeed:
    """便捷入口：直接从落盘行构造 feed。"""
    return EventFeed([event_from_row(row) for row in rows], mode)
