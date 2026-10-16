"""E2c：资源隔离的**集中越权矩阵**（SPEC §3.6 的落地物）。

八类资源各归 A（另有「任务」两类端点），然后**以 B 的身份**逐条打代表端点。每行三条判据：

  1. **B 拿 404**，且与「拿一个不存在的 id」**同状态码、同响应体**——只比状态码会漏掉
     「404 但文案不同」这种泄露（正文差一个字就等于承认了「这个 id 存在」）；
  2. **A 拿 200**（对照）——没有这一条，「所有请求都 404」的实现也能让整张矩阵绿；
  3. **打完之后 A 的资源还在**（越权尝试不该是删除；删除类端点单独比）。

矩阵末尾两行是**另一种判据**（`admin` 面与 `ETL` → **403**）：那两处缺的是「角色」而不是
「别人的资源」，与上面八行刻意不同（理由见 `core/auth.py::require_admin`）。

为什么要集齐这一张：逐个功能文件里的归属用例（chat / backtest / paper / strategies…）各自都对，
但**没有人一眼能看全**「哪些资源受保护、拿什么判据」。这张表是给人读的验收证据。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.graph import astream_chat, build_agent
from app.core.auth import require_user
from app.main import app
from tests.conftest import TEST_USER
from tests.fakes import FakeDatabase, ToolCallingFakeModel

client = TestClient(app)

A = dict(TEST_USER)  # id = 1
B = {**TEST_USER, "id": 2, "email": "bob@example.com"}

#: 每一行：`(资源, 造好的 id 键, 端点模板, HTTP 方法, 「不存在」的取值)`。
#: 第五列必须**过得了路径校验**：自选股那条的 `symbol` 有 6 位代码的 pattern，
#: 拿 UUID 去比会先撞 422——那样比的就不是「越权与不存在同响应」了。
CASES: list[tuple[str, str, str, str, str | None]] = [
    ("会话", "thread", "/api/v1/chat/threads/{thread}/messages", "GET", None),
    ("自选股", "symbol", "/api/v1/watchlist/{symbol}", "DELETE", "000001"),
    ("策略", "strategy", "/api/v1/strategies/{strategy}", "GET", None),
    ("回测记录", "run", "/api/v1/backtest/runs/{run}", "GET", None),
    ("网格记录", "optimization", "/api/v1/optimize/runs/{optimization}", "GET", None),
    ("模拟盘", "account", "/api/v1/paper/accounts/{account}", "GET", None),
    ("研报", "report", "/api/v1/reports/{report}", "GET", None),
    ("深研运行", "research_run", "/api/v1/research/runs/{research_run}", "GET", None),
    ("任务", "task", "/api/v1/tasks/{task}", "GET", None),
    ("任务取消", "task", "/api/v1/tasks/{task}/cancel", "POST", None),
]


def as_user(who: dict[str, Any]) -> None:
    app.dependency_overrides[require_user] = lambda: dict(who)


@pytest.fixture(autouse=True)
def saver() -> Any:
    """会话的消息端点要 checkpointer；不装它 A 自己都拿 503，对照就没了（同 chat 归属用例）。"""
    checker = InMemorySaver()
    app.state.checkpointer = checker
    yield checker
    app.state.checkpointer = None


@pytest.fixture
def seeded(signed_in: FakeDatabase) -> dict[str, str]:
    """A 名下八类资源各一行。用假库直接造——矩阵关心的是**端点**对 B 的态度。"""
    db = signed_in
    ids = {
        "thread": str(uuid.uuid4()),
        "strategy": str(uuid.uuid4()),
        "run": str(uuid.uuid4()),
        "optimization": str(uuid.uuid4()),
        "account": str(uuid.uuid4()),
        "report": str(uuid.uuid4()),
        "research_run": str(uuid.uuid4()),
        "task": str(uuid.uuid4()),
        "symbol": "600519",
    }
    # 会话要**真有消息**：消息端点读的是 checkpointer，只落归属行不够（A 的对照会 404）。
    # 走一遍真图（假模型），与 chat 归属用例的 `_seed_thread` 同法。
    asyncio.run(db.claim_thread(ids["thread"], A["id"]))
    agent = build_agent(
        ToolCallingFakeModel(responses=[AIMessage("在的")]),
        [],
        checkpointer=app.state.checkpointer,
    )

    async def drain() -> None:
        async for _ in astream_chat(agent, message="问一句", thread_id=ids["thread"]):
            pass

    asyncio.run(drain())
    asyncio.run(db.add_watchlist_item(A["id"], ids["symbol"], "默认分组", 100.0))
    asyncio.run(db.create_strategy(ids["strategy"], A["id"], "A 的策略", "x = 1", {}))
    asyncio.run(db.save_backtest_run(ids["run"], A["id"], {"symbol": "600519"}, {"meta": {}}))
    asyncio.run(db.save_optimization_run(ids["optimization"], A["id"], {}, {"kind": "grid"}))
    asyncio.run(
        db.create_paper_account(
            account_id=ids["account"],
            user_id=A["id"],
            name="A 的模拟盘",
            # 整份 config 都要给：读回时走 `config_from_payload`，缺键会在 A 的对照那一发炸
            config={
                "initial_cash": 100_000.0,
                "symbols": ["600519"],
                "strategy": "ma_cross",
                "start": "2026-07-01",
                "end": "2026-09-30",
                "params": {},
                "costs": {},
            },
            cash=100_000.0,
            as_of=date(2026, 9, 30),
            rules={},
            positions=[],
            decisions=[],
            equity=[],
        )
    )
    asyncio.run(
        db.create_research_report(
            report_id=ids["report"],
            user_id=A["id"],
            account_id=ids["account"],
            snapshot={},
            snapshot_hash="snap",
            report={"blocks": []},
            report_hash="hash",
        )
    )
    asyncio.run(
        db.create_research_run(
            run_id=ids["research_run"], user_id=A["id"], question="值不值得买", subject={}
        )
    )
    asyncio.run(db.create_task(ids["task"], A["id"], "backtest", {"symbol": "600519"}))
    return ids


@pytest.mark.parametrize(("label", "key", "path", "method", "absent"), CASES)
def test_other_user_gets_the_same_404_as_a_missing_id(
    seeded: dict[str, str], label: str, key: str, path: str, method: str, absent: str | None
) -> None:
    target = path.format(**{key: seeded[key]})

    as_user(A)
    assert client.request(method, target).status_code == 200, f"{label}：A 自己应当拿得到"

    as_user(B)
    foreign = client.request(method, target)
    missing = client.request(method, path.format(**{key: absent or uuid.uuid4()}))
    assert foreign.status_code == missing.status_code == 404, label
    # 正文也一致：差一个字就等于承认了「这个 id 存在」
    assert foreign.json() == missing.json(), label


@pytest.mark.parametrize(("label", "key", "path", "method", "absent"), CASES)
def test_the_resource_survives_a_trespass_attempt(
    seeded: dict[str, str], label: str, key: str, path: str, method: str, absent: str | None
) -> None:
    """越权尝试不该是删除：打完之后 A 拿到的还是原来那个东西。"""
    as_user(B)
    client.request(method, path.format(**{key: seeded[key]}))

    as_user(A)
    assert client.request("GET", path.format(**{key: seeded[key]})).status_code != 404, label


def test_admin_surface_is_403_not_404(seeded: dict[str, str]) -> None:
    """**另一种判据**：角色不足 → 403。它保护的从来不是「别人的资源」，见 §3.4。"""
    as_user(B)
    for path in ("/api/v1/admin/usage", "/api/v1/admin/rejections"):
        assert client.get(path).status_code == 403, path


def test_etl_surface_is_403_not_404(seeded: dict[str, str]) -> None:
    """同上：一次 ETL 触发是**全局**动作，普通用户不该够得着（E2b 的越权面修复）。"""
    as_user(B)
    assert client.get("/api/v1/etl/status").status_code == 403
    assert client.post("/api/v1/etl/run").status_code == 403


def test_public_surfaces_stay_public() -> None:
    """刻意公开的两处**不受影响**（回归钉子）：行情与事件非用户资产。"""
    for path in ("/api/v1/market/freshness", "/api/v1/events/calendar"):
        assert client.get(path).status_code != 401, path
