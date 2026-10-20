"""离线单测：对外 MCP Server 的工具面（E8b，SPEC §8.6）。

全部注入假检索层——**不连 Qdrant、不载模型**（真链路那份在 `scripts/verify_quantsage_mcp.py`
里用真 stdio 握手跑，两者分工：这里验判据与边界，那里验「真拉得起来、真的按 PIT 过滤」）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.mcp import server
from app.rag import RagNotReady
from app.rag.retrieve import RetrievedEvent


def make_event(**overrides: Any) -> RetrievedEvent:
    fields: dict[str, Any] = {
        "event_id": "news:1",
        "day": "2026-08-17",
        "title": "央行宣布降准 0.25 个百分点",
        "summary": "自 8 月 20 日起下调金融机构存款准备金率。",
        "event_time": "2026-08-17T09:00:00+08:00",
        "available_at": "2026-08-17T09:03:00+08:00",
        "event_type": "news",
        "direction_norm": "利好",
        "importance_score": 82.0,
        "symbols": ("600519",),
        "industries": ("银行",),
        "source": "财联社",
        "original_source": "央行官网",
        "source_url": "https://example.com/news/1",
        "content_hash": "abc123",
        "text": "央行宣布降准 0.25 个百分点，释放长期资金约 5000 亿元。",
        "score": 0.8123456789,
        "score_kind": "rrf",
    }
    fields.update(overrides)
    return RetrievedEvent(**fields)


@pytest.fixture
def rag(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """把检索层换成假的，并记下**调用参数**（档位、top_k、as_of 都要被断言）。"""
    calls: dict[str, Any] = {}

    def fake_search(spec, *, mode=None, **_kwargs):
        calls["spec"] = spec
        calls["mode"] = mode
        return calls.get("hits", [make_event()])

    def fake_coverage(**_kwargs):
        return ("2026-07-07", "2026-09-29")

    monkeypatch.setattr(server.retrieve, "search", fake_search)
    monkeypatch.setattr(server.retrieve, "coverage_window", fake_coverage)
    calls["hits"] = [make_event()]
    return calls


async def test_payload_has_pit_fields_and_source_triple(rag: dict[str, Any]) -> None:
    result = await server.search_events("降准", as_of="2026-08-20")

    assert result["ok"] is True
    assert result["coverage"] == {"start": "2026-07-07", "end": "2026-09-29"}
    assert result["score_kind"] == "rrf"  # 未精排就如实标
    assert result["count"] == 1
    hit = result["hits"][0]
    # 双时间戳并列 + 来源三元组：PRD §5 的硬性要求，MCP 这层也得带
    assert hit["event_time"] and hit["available_at"]
    assert hit["source"] == {
        "name": "财联社",
        "original": "央行官网",
        "url": "https://example.com/news/1",
    }
    assert hit["symbols"] == ["600519"]


async def test_effective_as_of_is_echoed(rag: dict[str, Any]) -> None:
    """**回显实际生效的时点**——省略 `as_of` 时是「现在」，模型必须看得出这一点。"""
    explicit = await server.search_events("降准", as_of="2026-08-20")
    assert explicit["as_of"].startswith("2026-08-20T00:00:00")  # 只有日期 ⇒ 当日零点（北京时间）

    implicit = await server.search_events("降准")
    assert datetime.fromisoformat(implicit["as_of"]) > datetime(2026, 1, 1, tzinfo=UTC)


async def test_default_path_is_hybrid_without_rerank(rag: dict[str, Any]) -> None:
    """默认档位写死 hybrid：精排要多载一份 2.3GB 模型，且与后端进程争 MPS。"""
    await server.search_events("降准")

    assert rag["mode"] == server.retrieve.MODE_HYBRID


async def test_top_k_is_clamped(rag: dict[str, Any]) -> None:
    await server.search_events("降准", top_k=50)
    assert rag["spec"].top_k == server.MAX_TOP_K

    await server.search_events("降准", top_k=0)
    assert rag["spec"].top_k == 1


async def test_blank_filters_become_none(rag: dict[str, Any]) -> None:
    """客户端塞空串是常态；空串必须当「没传」，否则会去匹配空标的。"""
    await server.search_events("降准", symbol="  ", event_type="")

    assert rag["spec"].symbol is None
    assert rag["spec"].event_type is None


@pytest.mark.parametrize("bad", ["昨天", "2026-13-45", "not-a-date"])
async def test_unparseable_as_of_is_a_structured_error(rag: dict[str, Any], bad: str) -> None:
    result = await server.search_events("降准", as_of=bad)

    assert result["ok"] is False
    assert result["error"] == "bad_argument"
    assert "YYYY-MM-DD" in result["detail"]  # 给出可自纠的写法


async def test_blank_query_is_a_structured_error(rag: dict[str, Any]) -> None:
    result = await server.search_events("   ")

    assert result["ok"] is False
    assert result["error"] == "bad_argument"


async def test_rag_unavailable_is_not_reported_as_empty(
    rag: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """**「不可用」与「查不到」是两回事**——混成一句会让模型以为那段时间没有事件。"""

    def boom(*_args, **_kwargs):
        raise RagNotReady("Qdrant 未就绪")

    monkeypatch.setattr(server.retrieve, "search", boom)

    result = await server.search_events("降准")

    assert result["ok"] is False
    assert result["error"] == "rag_unavailable"
    assert "Qdrant" in result["detail"]


async def test_unexpected_failure_does_not_raise(
    rag: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """任何异常都翻成结构化错误：把栈甩给外部客户端等于什么都没有说。"""

    def boom(*_args, **_kwargs):
        raise RuntimeError("segment fault-ish")

    monkeypatch.setattr(server.retrieve, "search", boom)

    result = await server.search_events("降准")

    assert result["ok"] is False
    assert result["error"] == "search_failed"
    assert "RuntimeError" in result["detail"]


async def test_oversized_payload_drops_the_tail_and_says_so(rag: dict[str, Any]) -> None:
    """总长硬上限：超了从**尾部**丢，并如实标注丢了几条（不静默截断）。"""
    rag["hits"] = [make_event(event_id=f"news:{i}", title="长" * 2000) for i in range(10)]

    result = await server.search_events("降准", top_k=10)

    assert result["truncated"] is True
    assert result["dropped"] > 0
    assert result["count"] == len(result["hits"]) < 10
    assert server._size(result) <= server.MAX_PAYLOAD_BYTES
    # 保留的是**头部**：命中按相关度排序，尾部本来就最弱
    assert result["hits"][0]["event_id"] == "news:0"


async def test_summary_is_clipped(rag: dict[str, Any]) -> None:
    rag["hits"] = [make_event(summary="长" * 1000)]

    result = await server.search_events("降准")

    summary = result["hits"][0]["summary"]
    assert len(summary) == server.SUMMARY_CLIP + 1  # +1 是省略号
    assert summary.endswith("…")


async def test_empty_result_says_ok_but_zero(rag: dict[str, Any]) -> None:
    """查得到但没命中：`ok: true` + `count: 0`——与「不可用」区分开。"""
    rag["hits"] = []

    result = await server.search_events("降准", as_of="2020-01-01")

    assert result["ok"] is True
    assert result["count"] == 0
    assert result["hits"] == []
    assert result["score_kind"] is None
    # 离线套件默认关改写（conftest），如实标「没有真正改写」而不是假装试过
    assert result["query"]["rewritten"] is False
    assert result["query"]["effective"] == "降准"
    assert "未成功" in result["query"]["note"]


async def test_long_query_echo_is_clipped_to_keep_payload_cap(rag: dict[str, Any]) -> None:
    """超长 query 只截回显，不能让 12KB 硬上限失效。"""
    rag["hits"] = []

    result = await server.search_events("长" * 20000)

    assert result["ok"] is True
    assert result["query"]["clipped"] is True
    assert len(result["query"]["original"]) <= server.MAX_QUERY_CLIP + 1  # +1 是省略号
    assert len(result["query"]["effective"]) <= server.MAX_QUERY_CLIP + 1
    assert server._size(result) <= server.MAX_PAYLOAD_BYTES


async def test_empty_result_is_rewritten_once_with_same_as_of(
    rag: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """零命中 → 改写一次；对外如实回显原查询/实际查询，as_of 不变。"""
    calls: list[tuple[str, object]] = []

    def fake_search(spec, *, mode=None, **_kwargs):
        calls.append((spec.query, spec.as_of))
        return [] if len(calls) == 1 else [make_event()]

    async def fake_rewrite(spec):
        return "降准 银行股"

    monkeypatch.setattr(server.retrieve, "search", fake_search)
    monkeypatch.setattr("app.rag.correct.rewrite_query", fake_rewrite)

    result = await server.search_events("降准", as_of="2026-08-20")

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["query"] == {
        "original": "降准",
        "effective": "降准 银行股",
        "rewritten": True,
        "clipped": False,
        "note": "首次检索无结果，已改写为「降准 银行股」再检索（同一 as_of）",
    }
    assert len(calls) == 2
    assert calls[0][1] == calls[1][1]  # PIT：as_of 逐字不变
