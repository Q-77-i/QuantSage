"""交易日历离线单测：session 逻辑、长假边界、越界报错、对账函数。

断言里用的是**冻结文件的真实值**（不是手写的日历），这样重生成日历后若边界变了，
测试会立刻红——那正是需要人看一眼的时候。
真实数据（21 片行情）的全期对账在 scripts/audit_calendar.py，不进单测。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.data import calendar as cal


def test_coverage_matches_frozen_file() -> None:
    first, last = cal.coverage()
    assert first == date(2015, 1, 5)
    assert last == date(2026, 12, 31)  # 上界受库的假期记录限制，重生成才会前移


def test_weekend_and_holidays_are_not_sessions() -> None:
    assert cal.is_session(date(2026, 9, 30)) is True
    # 国庆：10-01 → 10-07 休市，10-08 恢复（2026 年实际安排）
    for day in (date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 7)):
        assert cal.is_session(day) is False
    assert cal.is_session(date(2026, 10, 8)) is True
    # 元旦：01-01 / 01-02 连休
    assert cal.is_session(date(2026, 1, 1)) is False
    assert cal.is_session(date(2026, 1, 2)) is False
    # 周末
    assert cal.is_session(date(2026, 9, 27)) is False  # 周日


def test_spring_festival_boundary() -> None:
    """春节边界：02-16 起休市，02-24 恢复——长假是最容易在日历里出错的地方。"""
    assert cal.is_session(date(2026, 2, 13)) is True
    assert cal.is_session(date(2026, 2, 16)) is False
    assert cal.is_session(date(2026, 2, 23)) is False
    assert cal.is_session(date(2026, 2, 24)) is True

    assert cal.previous_session(date(2026, 2, 16)) == date(2026, 2, 13)
    assert cal.next_session(date(2026, 2, 13)) == date(2026, 2, 24)


def test_sessions_range_is_inclusive_and_ordered() -> None:
    assert cal.sessions(date(2026, 9, 30), date(2026, 10, 8)) == [
        date(2026, 9, 30),
        date(2026, 10, 8),
    ]
    assert cal.sessions(date(2026, 10, 1), date(2026, 10, 7)) == []
    assert cal.sessions(date(2026, 10, 8), date(2026, 10, 1)) == []


def test_session_navigation_skips_holidays() -> None:
    assert cal.next_session(date(2026, 9, 30)) == date(2026, 10, 8)
    assert cal.last_session_on_or_before(date(2026, 10, 1)) == date(2026, 9, 30)
    assert cal.last_session_on_or_before(date(2026, 10, 8)) == date(2026, 10, 8)


@pytest.mark.parametrize("day", [date(2027, 1, 4), date(2014, 12, 31)])
def test_out_of_range_raises_instead_of_guessing(day: date) -> None:
    """越界必须是「我不知道」，不是「不是交易日」——静默 False 会让 ETL 悄悄停下。"""
    with pytest.raises(cal.CalendarOutOfRange):
        cal.is_session(day)
    with pytest.raises(cal.CalendarOutOfRange):
        cal.sessions(day, day)
    with pytest.raises(cal.CalendarOutOfRange):
        cal.last_session_on_or_before(day)


def test_next_session_at_the_end_of_coverage_raises() -> None:
    with pytest.raises(cal.CalendarOutOfRange):
        cal.next_session(date(2026, 12, 31))


def test_audit_reports_both_directions() -> None:
    """两个方向都要报：日历有而数据无（数据缺口）、数据有而日历无（日历漏记）。"""
    # 窗口 09-29 → 10-08 内日历有 [09-29, 09-30, 10-08]；数据少了 09-30、多了 10-03（周六）
    result = cal.audit([date(2026, 9, 29), date(2026, 10, 3), date(2026, 10, 8)])

    assert result.window == (date(2026, 9, 29), date(2026, 10, 8))
    assert result.only_in_calendar == (date(2026, 9, 30),)
    assert result.only_in_data == (date(2026, 10, 3),)
    assert not result.ok


def test_audit_ok_on_exact_match() -> None:
    days = cal.sessions(date(2026, 9, 1), date(2026, 9, 30))
    result = cal.audit(days)
    assert result.ok
    assert result.calendar_days == result.data_days == len(days)


def test_audit_rejects_empty_input() -> None:
    with pytest.raises(ValueError):
        cal.audit([])
