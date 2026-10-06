"""T4 PIT 闸门单测——本项目护城河，边界必须锁死。

重点：`available_at == 当日 15:00` 含端可见、`15:00:00.000001` 顺延次日，
以及 PIT 与非 PIT 在**同一批事件**上给出不同的可见日。
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.backtest.events import (
    CUTOFF_FIELD,
    EventFeed,
    bar_cutoff,
    event_from_row,
    parse_factor_score,
)
from app.backtest.types import Bar, EventView, Mode

CST = ZoneInfo("Asia/Shanghai")


def at(text: str) -> datetime:
    """'2026-08-03 15:00:00' → tz-aware（Asia/Shanghai）。"""
    return datetime.fromisoformat(text).replace(tzinfo=CST)


def make_bar(day: date) -> Bar:
    return Bar(symbol="600519", trade_date=day, open=100.0, high=101.0, low=99.0, close=100.5, volume=1e5)


def make_event(event_id: str, event_time: datetime, available_at: datetime) -> EventView:
    return EventView(
        event_id=event_id,
        symbols=("600519",),
        title="t",
        event_time=event_time,
        available_at=available_at,
        direction_norm="bullish",
        score=60.0,
    )


def test_bar_cutoff_is_market_close_in_shanghai() -> None:
    cutoff = bar_cutoff(date(2026, 8, 3))
    assert (cutoff.hour, cutoff.minute, cutoff.second) == (15, 0, 0)
    assert cutoff.tzinfo is not None
    # 与数据侧（落盘为 +08:00）可直接比较，不会 naive/aware 混比
    assert cutoff == at("2026-08-03 15:00:00")


def test_cutoff_field_mapping_is_fixed() -> None:
    """结构断言：防止将来有人把 PIT 与非 PIT 的字段悄悄对调。"""
    assert CUTOFF_FIELD[Mode.PIT] == "available_at"
    assert CUTOFF_FIELD[Mode.NON_PIT] == "event_time"


def test_stamp_is_the_only_difference_between_modes() -> None:
    ev = make_event("e1", at("2026-08-03 09:00:00"), at("2026-08-05 10:00:00"))
    assert ev.stamp(Mode.PIT) == ev.available_at
    assert ev.stamp(Mode.NON_PIT) == ev.event_time


def test_event_available_exactly_at_close_is_visible_same_day() -> None:
    """边界含端：15:00:00.000000 视为当日收盘已知。"""
    ev = make_event("e1", at("2026-08-03 09:00:00"), at("2026-08-03 15:00:00"))
    feed = EventFeed([ev], Mode.PIT)
    assert feed.advance(make_bar(date(2026, 8, 3))) == (ev,)


def test_event_available_one_microsecond_after_close_is_deferred() -> None:
    """边界不含端：15:00:00.000001 顺延到下一交易日。"""
    ev = make_event("e1", at("2026-08-03 09:00:00"), at("2026-08-03 15:00:00.000001"))
    feed = EventFeed([ev], Mode.PIT)
    assert feed.advance(make_bar(date(2026, 8, 3))) == ()
    assert feed.advance(make_bar(date(2026, 8, 4))) == (ev,)


def test_pre_open_event_is_visible_same_day() -> None:
    ev = make_event("e1", at("2026-08-03 08:00:00"), at("2026-08-03 08:30:00"))
    feed = EventFeed([ev], Mode.PIT)
    assert feed.advance(make_bar(date(2026, 8, 3))) == (ev,)


def test_after_close_event_is_deferred_under_pit() -> None:
    """盘后消息（实测占 55%）在严格口径下当日不可见。"""
    ev = make_event("e1", at("2026-08-03 19:30:00"), at("2026-08-03 19:31:00"))
    feed = EventFeed([ev], Mode.PIT)
    assert feed.advance(make_bar(date(2026, 8, 3))) == ()
    assert feed.advance(make_bar(date(2026, 8, 4))) == (ev,)


def test_cross_day_availability_is_deferred_under_pit() -> None:
    """跨日才可得（实测占 37.2%）——事发在 3 日、平台 5 日才发布。"""
    ev = make_event("e1", at("2026-08-03 10:00:00"), at("2026-08-05 09:00:00"))
    feed = EventFeed([ev], Mode.PIT)
    assert feed.advance(make_bar(date(2026, 8, 3))) == ()
    assert feed.advance(make_bar(date(2026, 8, 4))) == ()
    assert feed.advance(make_bar(date(2026, 8, 5))) == (ev,)


def test_same_events_yield_different_visibility_by_mode() -> None:
    """同一批事件，两种模式的可见日不同——验收判据③的机制保证。"""
    ev = make_event("e1", at("2026-08-03 10:00:00"), at("2026-08-05 09:00:00"))
    days = [date(2026, 8, d) for d in (3, 4, 5)]

    def first_visible(mode: Mode) -> date | None:
        feed = EventFeed([ev], mode)
        for day in days:
            if feed.advance(make_bar(day)):
                return day
        return None

    assert first_visible(Mode.PIT) == date(2026, 8, 5)
    assert first_visible(Mode.NON_PIT) == date(2026, 8, 3)


def test_weekend_event_rolls_to_next_bar() -> None:
    """非交易日事件无需特判，累积到下一根 bar 一起放行。"""
    saturday_event = make_event("e1", at("2026-08-01 12:00:00"), at("2026-08-01 12:05:00"))
    feed = EventFeed([saturday_event], Mode.PIT)
    # 2026-08-01 是周六，下一根 bar 是 08-03 周一
    assert feed.advance(make_bar(date(2026, 8, 3))) == (saturday_event,)


def test_advance_returns_each_event_exactly_once() -> None:
    """游标差集语义：去重是结构性的，策略无需自维护 seen_ids。"""
    events = [
        make_event("e1", at("2026-08-03 09:00:00"), at("2026-08-03 09:01:00")),
        make_event("e2", at("2026-08-03 10:00:00"), at("2026-08-03 10:01:00")),
    ]
    feed = EventFeed(events, Mode.PIT)
    first = feed.advance(make_bar(date(2026, 8, 3)))
    assert {e.event_id for e in first} == {"e1", "e2"}
    assert feed.advance(make_bar(date(2026, 8, 4))) == ()
    assert feed.advance(make_bar(date(2026, 8, 5))) == ()
    assert len(feed.visible) == 2


def test_feed_exposes_cutoff_field_for_evidence() -> None:
    feed = EventFeed([], Mode.NON_PIT)
    assert feed.cutoff_field == "event_time"
    assert feed.mode is Mode.NON_PIT


# ── parse_factor_score：双重编码与容错 ──────────────────────────────────────


def test_parse_factor_score_handles_double_encoded_json() -> None:
    """落盘真实形状：JSON 列里存的是 JSON 文本，需解两次。"""
    import json

    inner = json.dumps({"score": 69.327, "version": "factor-v2"})
    assert parse_factor_score(json.dumps(inner)) == pytest.approx(69.327)


def test_parse_factor_score_accepts_single_encoded_json_text() -> None:
    assert parse_factor_score('{"score": 51.5}') == pytest.approx(51.5)


def test_parse_factor_score_accepts_plain_dict() -> None:
    assert parse_factor_score({"score": 40}) == pytest.approx(40.0)


def test_parse_factor_score_accepts_integer_score() -> None:
    assert parse_factor_score({"score": 50}) == pytest.approx(50.0)


@pytest.mark.parametrize(
    "raw",
    [
        None,  # 空值
        "",  # 空串
        "not json",  # 非法 JSON
        "[1, 2]",  # 合法 JSON 但不是对象
        '{"other": 1}',  # 缺 score 键
        '{"score": null}',  # score 为 null
        '{"score": "high"}',  # score 非数值
        '{"score": true}',  # bool 不是分数
        12.5,  # 非 str 非 Mapping
    ],
)
def test_parse_factor_score_returns_none_without_raising(raw: object) -> None:
    """容错：任何异常形状都返回 None，绝不让回测中断。"""
    assert parse_factor_score(raw) is None


def test_event_from_row_parses_real_shape() -> None:
    import json

    row = {
        "event_id": "news:1",
        "symbols": ["600519"],
        "title": "标题",
        "event_time": at("2026-08-03 10:00:00"),
        "available_at": at("2026-08-03 12:00:00"),
        "direction_norm": "bullish",
        "factor_scores": json.dumps(json.dumps({"score": 66.6})),
    }
    ev = event_from_row(row)
    assert ev.event_id == "news:1"
    assert ev.score == pytest.approx(66.6)
    assert ev.stamp(Mode.PIT) == row["available_at"]


def test_event_from_row_tolerates_null_direction_and_scores() -> None:
    row = {
        "event_id": "x",
        "symbols": ["600519"],
        "title": None,
        "event_time": at("2026-08-03 10:00:00"),
        "available_at": at("2026-08-03 10:00:00"),
        "direction_norm": None,
        "factor_scores": None,
    }
    ev = event_from_row(row)
    assert ev.direction_norm is None
    assert ev.score is None
