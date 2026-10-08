"""`search_events` 工具的离线用例：输出格式、降级话术、参数解析。

工具返回值是**给模型看的文本**，不是给程序消费的结构——所以这里断言的是
「模型能不能据此说人话」：双时间戳、来源、覆盖区间、以及「查不到」与「不可用」
必须是两句不同的话（把不可用说成查不到，模型就会答「没有相关事件」，那是撒谎）。
"""

from __future__ import annotations

import pytest

from app.agent import tools as agent_tools
from app.backtest.types import CN_TZ
from app.rag import RagNotReady
from app.rag.retrieve import RetrievedEvent
from datetime import datetime


def hit(event_id: str = "news:1", **overrides) -> RetrievedEvent:
    base = dict(
        event_id=event_id,
        day="2026-08-17",
        title="央行宣布降准0.5个百分点",
        summary="释放长期资金约1万亿元。",
        event_time="2026-08-17T10:00:00+08:00",
        available_at="2026-08-17T10:12:00+08:00",
        event_type="news",
        direction_norm="bullish",
        importance_score=78.3,
        symbols=("600036", "000001"),
        industries=("银行",),
        source="xiaoshi-archive",
        original_source="证券时报",
        source_url="https://example.com/x",
        content_hash="abc",
        text="【标题】央行宣布降准0.5个百分点",
        score=3.2,
        score_kind="rerank",
    )
    base.update(overrides)
    return RetrievedEvent(**base)  # type: ignore[arg-type]


@pytest.fixture()
def patched(monkeypatch: pytest.MonkeyPatch):
    """把检索层换成脚本化返回，工具用例不碰 Qdrant 与模型。"""

    def install(hits, *, window=("2026-07-07", "2026-09-29"), error=None):
        def fake_search(spec, **kwargs):
            if error:
                raise error
            return hits

        monkeypatch.setattr("app.rag.retrieve.search", fake_search)
        monkeypatch.setattr("app.rag.retrieve.coverage_window", lambda **kwargs: window)

    return install


@pytest.mark.asyncio
async def test_renders_both_timestamps_and_source(patched) -> None:
    patched([hit()])
    text = await agent_tools.search_events.ainvoke({"query": "降准"})
    assert "本地语料覆盖 2026-07-07 ~ 2026-09-29" in text
    assert "事发 2026-08-17 10:00｜可得 2026-08-17 10:12" in text
    assert "xiaoshi-archive / 证券时报" in text
    assert "600036/000001" in text


@pytest.mark.asyncio
async def test_empty_result_says_what_was_searched(patched) -> None:
    patched([])
    text = await agent_tools.search_events.ainvoke({"query": "不存在的事"})
    assert "没有检索到事件" in text
    assert "2026-07-07" in text  # 覆盖区间要带上，模型才能解释「为什么没有」


@pytest.mark.asyncio
async def test_unavailable_is_not_reported_as_no_results(patched) -> None:
    """降级与「查不到」必须是两句不同的话。"""
    patched([], error=RagNotReady("Qdrant 不可用（http://127.0.0.1:6333）"))
    text = await agent_tools.search_events.ainvoke({"query": "降准"})
    assert "不可用" in text
    assert "没有检索到" not in text


@pytest.mark.asyncio
async def test_degraded_ranking_is_labelled(patched) -> None:
    """精排不可用时的降级排序要如实写在结果里，不能冒充已精排。"""
    patched([hit(score_kind="rrf")])
    text = await agent_tools.search_events.ainvoke({"query": "降准"})
    assert "未精排" in text


@pytest.mark.asyncio
async def test_invalid_as_of_is_rejected_with_hint(patched) -> None:
    patched([hit()])
    text = await agent_tools.search_events.ainvoke({"query": "降准", "as_of": "昨天"})
    assert "时间格式" in text


@pytest.mark.asyncio
async def test_top_k_is_clamped(patched, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_search(spec, **kwargs):
        captured["top_k"] = spec.top_k
        return []

    monkeypatch.setattr("app.rag.retrieve.search", fake_search)
    monkeypatch.setattr("app.rag.retrieve.coverage_window", lambda **kwargs: (None, None))
    await agent_tools.search_events.ainvoke({"query": "x", "top_k": 99})
    assert captured["top_k"] == 10


def test_parse_as_of_accepts_date_and_iso() -> None:
    day = agent_tools._parse_as_of("2026-08-17")
    assert isinstance(day, datetime) and day.year == 2026 and day.tzinfo is None
    full = agent_tools._parse_as_of("2026-08-17T10:12:00+08:00")
    assert full.utcoffset().total_seconds() == 8 * 3600
    assert agent_tools._parse_as_of(None) is None
    assert agent_tools._parse_as_of("昨天") is agent_tools._INVALID


def test_naive_as_of_is_read_as_beijing() -> None:
    from app.rag.retrieve import resolve_as_of

    assert resolve_as_of(datetime(2026, 8, 17)).tzinfo is CN_TZ
