"""M8b 深路径端点：SSE 全过程 / HITL 恢复 / 越权 / 状态机 / 报告落库。

离线替身是 `FakeDatabase`（同 M1c/M4c/M7 口径）；行情与语料走**真实 Parquet**；
LLM 一律注入假模型（`build_context` 是留好的缝）——离线用例不该真去调模型。
checkpointer 用 `InMemorySaver`，**因为 HITL 的恢复语义只有 checkpointer 能证**。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.deep.roles.base import RoleContext
from app.api import research as research_api
from app.main import app
from tests.fakes import FakeDatabase
from tests.test_deep_graph import DeepFake

client = TestClient(app)
AS_OF = "2026-09-30"
SYMBOL = "600519"


class _Boom(FakeDatabase):
    """未登录/越权用例用不到它的内容，这里只借用容器。"""


def _sse(response: Any) -> list[tuple[str, dict[str, Any]]]:
    """把 SSE 响应读成 `[(事件名, 载荷)]`。"""
    frames: list[tuple[str, dict[str, Any]]] = []
    name: str | None = None
    for line in response.iter_lines():
        text = line if isinstance(line, str) else line.decode("utf-8")
        if text.startswith("event: "):
            name = text[len("event: ") :]
        elif text.startswith("data: ") and name:
            frames.append((name, json.loads(text[len("data: ") :])))
            name = None
    return frames


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signed_in: Any) -> dict[str, Any]:
    """假库 + 内存 checkpointer + 假模型 + RAG 替身（行情/语料走真实 Parquet）。"""
    import app.agent.deep.roles.news as news_mod
    from app.rag.retrieve import RetrievedEvent

    db = FakeDatabase()
    app.state.db = db
    app.state.checkpointer = InMemorySaver()
    app.state.settle_scheduler = None

    def fake_build_context(*, brief: Any, budget_payload: Any = None) -> RoleContext:
        return RoleContext(brief=brief, budget=research_api.Budget(limits=research_api._limits()),
                           chat=DeepFake(), thinking=False, timeout=5.0)

    def fake_search(spec: Any, **kwargs: Any) -> list[Any]:
        return [
            RetrievedEvent(
                event_id="news:9001", day="2026-08-15", title="贵州茅台中报净利下滑",
                summary="净利同比下滑", event_time="2026-08-15T10:11:51+08:00",
                available_at="2026-08-15T10:40:10+08:00", event_type="news",
                direction_norm="bearish", importance_score=80.0, symbols=(SYMBOL,),
                industries=("食品饮料",), source="xiaoshi-archive", original_source="东方财富",
                source_url="https://example.invalid/1", content_hash="a" * 12,
                text="贵州茅台中报净利下滑", score=0.9, score_kind="rerank",
            )
        ]

    monkeypatch.setattr(research_api, "build_context", fake_build_context)
    monkeypatch.setattr(news_mod, "search", fake_search)
    return {"db": db, "user": signed_in}


def _post_run(**overrides: Any) -> Any:
    body = {"question": "分析一下 600519 值不值得买", "mode": "deep", "as_of": f"{AS_OF}T15:00:00+08:00"}
    body.update(overrides)
    return client.post("/api/v1/research/runs", json=body)


# ── 鉴权与形状 ──


def test_all_endpoints_require_login() -> None:
    with TestClient(app) as anon:
        assert anon.post("/api/v1/research/runs", json={"question": "你好呀", "mode": "deep"}).status_code == 401
        assert anon.get("/api/v1/research/runs").status_code == 401
        assert anon.get("/api/v1/research/runs/whatever").status_code == 401
        assert anon.post("/api/v1/research/runs/whatever/resume", json={"choice": "continue"}).status_code == 401


def test_unknown_run_is_404(env: dict[str, Any]) -> None:
    assert client.get("/api/v1/research/runs/00000000-0000-0000-0000-000000000000").status_code == 404


def test_bad_mode_is_422(env: dict[str, Any]) -> None:
    assert _post_run(mode="medium").status_code == 422


def test_question_without_symbol_is_422(env: dict[str, Any]) -> None:
    """解析不出标的就 422 说清楚——不拿一个空标的池去跑五个角色。"""
    resp = _post_run(question="最近大盘怎么样啊")

    assert resp.status_code == 422 and "标的" in resp.json()["detail"]


def test_resume_on_a_run_without_pending_approval_is_409(env: dict[str, Any]) -> None:
    with client.stream("POST", "/api/v1/research/runs", json={
        "question": "分析一下 600519 值不值得买", "mode": "deep",
        "as_of": f"{AS_OF}T15:00:00+08:00",
    }) as resp:
        frames = _sse(resp)
    run_id = next(payload for name, payload in frames if name == "done")["run_id"]

    conflict = client.post(f"/api/v1/research/runs/{run_id}/resume", json={"choice": "continue"})

    assert conflict.status_code == 409 and "待批" in conflict.json()["detail"]


# ── 全流程 ──
#
# ⚠ 下面两条标 `integration`：它们走的是**真跑一遍深路径**（真实行情 + 真实语料），
# 没有 `data/` 的环境里角色全降级 ⇒ 「跑完落库」这类断言不成立。
# 同文件其余各条把 runner 打桩，验的是端点契约，留在离线轨。


@pytest.mark.integration
def test_full_run_streams_events_and_persists_report(env: dict[str, Any]) -> None:
    with client.stream("POST", "/api/v1/research/runs", json={
        "question": "分析一下 600519 值不值得买", "mode": "deep",
        "as_of": f"{AS_OF}T15:00:00+08:00",
    }) as resp:
        assert resp.status_code == 200
        assert resp.headers["x-run-id"]
        frames = _sse(resp)

    names = [name for name, _ in frames]
    assert names.count("node_start") >= 8 and names.count("node_end") >= 8
    assert "report" in names and names[-1] == "done"

    done = frames[-1][1]
    assert done["status"] == "done" and done["report_hash"]
    run_id = done["run_id"]

    stored = client.get(f"/api/v1/research/runs/{run_id}").json()
    assert stored["status"] == "done"
    assert [event["node"] for event in stored["events"] if event["phase"] == "end"][:5] == [
        "market", "news", "fundamental", "policy", "factor",
    ] or {event["node"] for event in stored["events"]} >= {"market", "news", "fundamental", "policy", "factor"}
    assert stored["report"]["kind"] == "deep_research_report"
    assert stored["report"]["blocks"][0]["id"] == "overview"
    assert stored["report"]["evidence"], "报告里必须有可回链的证据"
    # 快照进正文（研报页的指纹行要显示它）——**它是这份报告可复现的凭据**
    assert stored["report"]["snapshot"]["bars"]["digest"]
    assert stored["done"] is True


def test_quick_route_returns_only_a_route_frame(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """`mode=auto` 判成快路径时**不建运行**——只回一帧 route，让前端回落对话页。"""
    from app.agent.deep.router import PATH_QUICK, RouteDecision

    async def fake_route(question: str, **kwargs: Any) -> RouteDecision:
        return RouteDecision(PATH_QUICK, "事实查询", ())

    monkeypatch.setattr(research_api, "route_question", fake_route)

    with client.stream("POST", "/api/v1/research/runs", json={
        "question": "600519 今天涨了吗", "mode": "auto",
    }) as resp:
        frames = _sse(resp)

    assert [name for name, _ in frames] == ["route", "done"]
    assert frames[0][1]["path"] == "quick"
    assert env["db"].runs == {}


# ── HITL ──


class _WordingFake(DeepFake):
    """综合一律给「买卖指令」措辞 ⇒ 必被闸门拦下、转人工审批。"""

    synthesis_script: list[dict[str, Any]] = [{"conclusion": "建议买入并长期持有。", "citations": []}]

    def _answer(self, messages: list[Any]) -> Any:
        payload = self.synthesis_script.pop(0) if '"conclusion"' in str(messages[-1].content) else None
        if payload is not None:
            self.synthesis_script.append(payload)  # 重写后仍是同一句：保持被拦
            from langchain_core.messages import AIMessage

            return AIMessage(content=json.dumps(payload, ensure_ascii=False))
        return super()._answer(messages)


@pytest.mark.integration
def test_interrupt_then_resume_completes_the_run(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    def wording_context(*, brief: Any, budget_payload: Any = None) -> RoleContext:
        return RoleContext(brief=brief, budget=research_api.Budget(limits=research_api._limits()),
                           chat=_WordingFake(), thinking=False, timeout=5.0)

    monkeypatch.setattr(research_api, "build_context", wording_context)

    with client.stream("POST", "/api/v1/research/runs", json={
        "question": "分析一下 600519 值不值得买", "mode": "deep",
        "as_of": f"{AS_OF}T15:00:00+08:00",
    }) as resp:
        created_headers = dict(resp.headers)
        frames = _sse(resp)

    interrupt = next(payload for name, payload in frames if name == "interrupt")
    # 帧与落库事件**同形**：`{phase, at, payload}`（前端两条路共用一套解析）
    assert interrupt["phase"] == "interrupt"
    assert interrupt["payload"]["kind"] == "wording"
    assert interrupt["payload"]["options"] == ["continue", "stop"]
    run_id = created_headers["x-run-id"]
    stored = client.get(f"/api/v1/research/runs/{run_id}").json()
    assert stored["status"] == "awaiting_approval"
    # 断连重连的客户端只能走详情端点——待批载荷必须**也**落进事件流（SSE 帧错过就没了）
    pending = [e for e in stored["events"] if e.get("phase") == "interrupt"]
    assert pending and pending[-1]["payload"]["kind"] == "wording"
    # 被 interrupt 的节点「起了没落」——收尾时补成 interrupted，而不是假装它没跑过
    assert any(e.get("status") == "interrupted" for e in stored["events"])

    with client.stream("POST", f"/api/v1/research/runs/{run_id}/resume", json={"choice": "continue"}) as resp:
        resumed = _sse(resp)

    assert resumed[-1][1]["status"] == "done"
    assert client.get(f"/api/v1/research/runs/{run_id}").json()["status"] == "done"


def test_run_list_is_scoped_to_the_owner(env: dict[str, Any]) -> None:
    with client.stream("POST", "/api/v1/research/runs", json={
        "question": "分析一下 600519 值不值得买", "mode": "deep",
        "as_of": f"{AS_OF}T15:00:00+08:00",
    }) as resp:
        frames = _sse(resp)
    run_id = next(payload for name, payload in frames if name == "done")["run_id"]

    listed = client.get("/api/v1/research/runs").json()["runs"]
    assert [row["id"] for row in listed] == [run_id]
    assert "events" not in listed[0]  # 摘要列表不带事件（动辄上百条）
