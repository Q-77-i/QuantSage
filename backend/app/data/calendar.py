"""交易日历（XSHG）：运行期只读冻结文件，零第三方依赖。

冻结文件 `trading_calendar.json` 由 `scripts/generate_calendar.py` 用 `exchange_calendars`
生成并入库——换库或换版本都不会在运行期引发口径漂移，重生成只需重跑脚本（来源与版本
记在文件头，可追溯）。

**越界一律报错**，不静默返回 False。日历只覆盖文件里 `range` 声明的那一段（上界受库的
假期记录限制：中国假期逐年公布，库不预测未来年份）。「不是交易日」与「我不知道」是两件事
——把后者当后者处理，ETL 才不会在年底悄悄停下、回测才不会把无日历的日子当休市。
"""

from __future__ import annotations

import json
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Collection, Iterable

CALENDAR_FILE = Path(__file__).resolve().parent / "trading_calendar.json"


class CalendarOutOfRange(RuntimeError):
    """查询落到冻结日历的覆盖区间之外——需要重生成日历，而不是当作休市。"""


@lru_cache(maxsize=1)
def _payload() -> dict:
    return json.loads(CALENDAR_FILE.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _sessions() -> tuple[date, ...]:
    return tuple(date.fromisoformat(day) for day in _payload()["sessions"])


def coverage() -> tuple[date, date]:
    """冻结日历的覆盖区间（含端点）。"""
    days = _sessions()
    return days[0], days[-1]


def _require_covered(day: date) -> None:
    first, last = coverage()
    if not first <= day <= last:
        raise CalendarOutOfRange(
            f"{day} 不在冻结日历覆盖区间 {first} → {last} 内；"
            "重跑 scripts/generate_calendar.py 生成新区间"
        )


def is_session(day: date) -> bool:
    """该日是否为交易日。越界抛 `CalendarOutOfRange`。"""
    _require_covered(day)
    days = _sessions()
    index = bisect_left(days, day)
    return index < len(days) and days[index] == day


def sessions(start: date, end: date) -> list[date]:
    """区间内的交易日，升序，含端点。区间任一端越界即抛错。"""
    if start > end:
        return []
    _require_covered(start)
    _require_covered(end)
    days = _sessions()
    return list(days[bisect_left(days, start) : bisect_right(days, end)])


def next_session(day: date) -> date:
    """严格晚于 `day` 的下一个交易日。"""
    _require_covered(day)
    days = _sessions()
    index = bisect_right(days, day)
    if index >= len(days):
        raise CalendarOutOfRange(f"{day} 之后没有交易日了（日历止于 {days[-1]}），需重生成日历")
    return days[index]


def previous_session(day: date) -> date:
    """严格早于 `day` 的上一个交易日。"""
    _require_covered(day)
    days = _sessions()
    index = bisect_left(days, day)
    if index == 0:
        raise CalendarOutOfRange(f"{day} 之前没有交易日了（日历起于 {days[0]}）")
    return days[index - 1]


def last_session_on_or_before(day: date) -> date:
    """不晚于 `day` 的最近一个交易日。"""
    _require_covered(day)
    days = _sessions()
    index = bisect_right(days, day)
    if index == 0:
        raise CalendarOutOfRange(f"{day} 及之前没有交易日（日历起于 {days[0]}）")
    return days[index - 1]


@dataclass(frozen=True, slots=True)
class CalendarAudit:
    """日历与真实数据的双向对账结果。"""

    window: tuple[date, date]
    calendar_days: int
    data_days: int
    only_in_calendar: tuple[date, ...]
    only_in_data: tuple[date, ...]

    @property
    def ok(self) -> bool:
        return not self.only_in_calendar and not self.only_in_data


def audit(days: Collection[date]) -> CalendarAudit:
    """拿真实数据的交易日集合与日历**双向**对账。

    窗口取数据自身的首末日（日历在这之外的日期无可比性），两个方向都要报：
    「日历有而数据无」可能是数据缺口或日历多记，「数据有而日历无」则说明日历漏了交易日。
    """
    if not days:
        raise ValueError("对账需要非空的交易日集合")
    window = (min(days), max(days))
    expected = set(sessions(*window))
    actual = set(days)
    return CalendarAudit(
        window=window,
        calendar_days=len(expected),
        data_days=len(actual),
        only_in_calendar=tuple(sorted(expected - actual)),
        only_in_data=tuple(sorted(actual - expected)),
    )


def describe(day_range: Iterable[date] | None = None) -> str:  # pragma: no cover - 诊断用
    first, last = coverage()
    return f"XSHG {first} → {last}，共 {len(_sessions())} 个交易日"
