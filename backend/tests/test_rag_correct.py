"""检索一轮纠错的离线用例（SPEC 附录 A.2）。

全部注入假检索/假改写——不连 Qdrant、不调模型。守三条：
**只零命中触发一次**、**as_of 逐字不变**、**改写失败不静默换查询**。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.backtest.types import CN_TZ
from app.rag.correct import correction_note, search_with_correction
from app.rag.retrieve import SearchQuery

HIT = object()
MOMENT = datetime(2026, 8, 20, tzinfo=CN_TZ)


def spec(**overrides) -> SearchQuery:
    base = dict(query="降准 银行", as_of=MOMENT, symbol="600036", event_type="news", top_k=5)
    base.update(overrides)
    return SearchQuery(**base)


@pytest.mark.asyncio
async def test_hits_never_rewrite() -> None:
    calls: list[SearchQuery] = []
    rewrites: list[SearchQuery] = []

    def fake_search(query: SearchQuery, **_: object) -> list[object]:
        calls.append(query)
        return [HIT]

    async def fake_rewrite(query: SearchQuery) -> str:
        rewrites.append(query)
        return "不应被调用"

    result = await search_with_correction(spec(), search_fn=fake_search, rewrite_fn=fake_rewrite)

    assert result.hits == [HIT]
    assert result.correction is None
    assert len(calls) == 1
    assert rewrites == []


@pytest.mark.asyncio
async def test_zero_hit_rewrites_once_and_keeps_pit_fields() -> None:
    calls: list[SearchQuery] = []

    def fake_search(query: SearchQuery, **_: object) -> list[object]:
        calls.append(query)
        return [] if len(calls) == 1 else [HIT]

    async def fake_rewrite(query: SearchQuery) -> str:
        return "  “降准 银行股”  "

    result = await search_with_correction(spec(), search_fn=fake_search, rewrite_fn=fake_rewrite)

    assert result.hits == [HIT]
    assert result.correction is not None
    assert result.correction.original == "降准 银行"
    assert result.correction.rewritten == "降准 银行股"
    assert result.correction.retried is True
    assert result.correction.reason == "rewritten"

    first, second = calls
    assert first.query == "降准 银行"
    assert second.query == "降准 银行股"
    # PIT 红线：只换 query，其余字段逐字保留
    assert second.as_of == first.as_of == MOMENT
    assert second.symbol == first.symbol == "600036"
    assert second.event_type == first.event_type == "news"
    assert second.top_k == first.top_k == 5


@pytest.mark.asyncio
async def test_second_search_still_empty_is_reported() -> None:
    calls: list[SearchQuery] = []

    def fake_search(query: SearchQuery, **_: object) -> list[object]:
        calls.append(query)
        return []

    async def fake_rewrite(query: SearchQuery) -> str:
        return "降准 银行股"

    result = await search_with_correction(spec(), search_fn=fake_search, rewrite_fn=fake_rewrite)

    assert result.hits == []
    assert result.correction is not None
    assert result.correction.retried is True
    assert result.correction.reason == "rewritten"
    assert len(calls) == 2
    assert "仍无结果" not in correction_note(result.correction)  # 说明的是「已改写再检索」
    assert "降准 银行股" in correction_note(result.correction)


@pytest.mark.asyncio
async def test_rewrite_empty_does_not_retry() -> None:
    calls: list[SearchQuery] = []

    def fake_search(query: SearchQuery, **_: object) -> list[object]:
        calls.append(query)
        return []

    async def fake_rewrite(query: SearchQuery) -> str:
        return "   "

    result = await search_with_correction(spec(), search_fn=fake_search, rewrite_fn=fake_rewrite)

    assert result.hits == []
    assert result.correction is not None
    assert result.correction.retried is False
    assert result.correction.reason == "unavailable"
    assert len(calls) == 1
    assert "改写未成功" in correction_note(result.correction)


@pytest.mark.asyncio
async def test_rewrite_same_as_original_does_not_retry() -> None:
    calls: list[SearchQuery] = []

    def fake_search(query: SearchQuery, **_: object) -> list[object]:
        calls.append(query)
        return []

    async def fake_rewrite(query: SearchQuery) -> str:
        return "降准 银行"

    result = await search_with_correction(spec(), search_fn=fake_search, rewrite_fn=fake_rewrite)

    assert result.correction is not None
    assert result.correction.retried is False
    assert result.correction.reason == "unchanged"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_rewrite_exception_does_not_break_search() -> None:
    calls: list[SearchQuery] = []

    def fake_search(query: SearchQuery, **_: object) -> list[object]:
        calls.append(query)
        return []

    async def boom(query: SearchQuery) -> str:
        raise RuntimeError("模型网关挂了")

    result = await search_with_correction(spec(), search_fn=fake_search, rewrite_fn=boom)

    assert result.hits == []
    assert result.correction is not None
    assert result.correction.reason == "unavailable"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_mode_is_forwarded_to_both_calls() -> None:
    modes: list[object] = []

    def fake_search(query: SearchQuery, **kwargs: object) -> list[object]:
        modes.append(kwargs.get("mode"))
        return [] if len(modes) == 1 else [HIT]

    async def fake_rewrite(query: SearchQuery) -> str:
        return "降准 银行股"

    await search_with_correction(
        spec(), mode="hybrid", search_fn=fake_search, rewrite_fn=fake_rewrite
    )

    assert modes == ["hybrid", "hybrid"]


def test_correction_note_is_honest_for_every_reason() -> None:
    from app.rag.correct import Correction

    assert "已改写" in correction_note(Correction("a", "b", True, "rewritten"))
    assert "相同" in correction_note(Correction("a", "a", False, "unchanged"))
    assert "未成功" in correction_note(Correction("a", None, False, "unavailable"))
    assert correction_note(None) == ""
