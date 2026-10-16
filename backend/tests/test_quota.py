"""E2 / 用户配额：两个维度、拒绝契约与顺序（P3-E2a）。

四条判据：

  * **两种语义**——在飞任务数是 gauge（直查任务表，进终态**自动**释放）；每日 Agent 请求数是
    counter（跨重启持久，自然日切换，`period_key` 是纯函数）；
  * **拒绝契约**——429 + `kind="quota_exceeded"` + `metric` / `limit` / `used`（计数器另带 `reset_at`）；
  * **顺序**——幂等重放**不被配额挡**：重复提交不建任务，也就不该被挡；
  * **边界**——按用户分、admin 豁免、上限 ≤0 不限额。

真库那份（`ON CONFLICT DO UPDATE … WHERE` 的**原子性**，内存替身证明不了）在
`tests/integration/test_quota_e2.py` 双跑——与 E4b 幂等键同一条理由：替身会把 SQL 语义
当 Python 语义糊过去。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.graph import build_agent
from app.core import quota
from app.core.auth import require_user
from app.core.config import get_settings
from app.data import duckdb_client
from app.main import app
from tests.conftest import TEST_USER, drive_tasks, make_backtest_dir, trading_days
from tests.fakes import FakeDatabase, ToolCallingFakeModel

client = TestClient(app)

CN_TZ = ZoneInfo("Asia/Shanghai")
SYMBOL = "600519"


# ── 夹具 ────────────────────────────────────────────────────


@pytest.fixture
def limits(monkeypatch: pytest.MonkeyPatch) -> Any:
    """把两个维度都压到「打两下就满」——测的是机制，不是默认额度。"""

    def _apply(agent_calls: int = 2, active_tasks: int = 2) -> None:
        monkeypatch.setenv("QUOTA_AGENT_CALLS_PER_DAY", str(agent_calls))
        monkeypatch.setenv("QUOTA_ACTIVE_TASKS", str(active_tasks))
        get_settings.cache_clear()

    _apply()
    yield _apply
    get_settings.cache_clear()


@pytest.fixture
def agent() -> Any:
    """装一台假模型的 agent——**整条 chat 链路走通**（真路由、真 SSE），只换模型与 checkpointer。

    配额点排在就绪检查之后，所以「跑得通」是用例的前提而不是它的断言。
    """
    saver = InMemorySaver()
    previous = getattr(app.state, "agent", None)
    app.state.agent = build_agent(
        ToolCallingFakeModel(responses=[AIMessage("好的")]), [], checkpointer=saver
    )
    yield app.state.agent
    app.state.agent = previous


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """合成行情：够 ma_cross 跑一次回测（与 `test_tasks_api` 同法）。"""
    bars = [
        {"trade_date": day, "open": 100.0 + i, "close": 100.5 + i, "high": 101.0 + i, "low": 99.0 + i}
        for i, day in enumerate(trading_days(datetime(2026, 7, 1).date(), 60))
    ]
    make_backtest_dir(tmp_path, bars, [], symbol=SYMBOL)
    monkeypatch.setattr(duckdb_client, "resolve_data_dir", lambda _=None: tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def ask() -> Any:
    return client.post("/api/v1/chat", json={"message": "你好"})


def submit(**overrides: Any) -> Any:
    return client.post(
        "/api/v1/backtest", json={"strategy": "ma_cross", "symbol": SYMBOL, **overrides}
    )


# ── period_key：纯函数 ──────────────────────────────────────


def test_period_key_is_a_cst_day_not_utc() -> None:
    """按 UTC 会算成 16 号：2026-10-16T17:30Z 在 +08 已经是 17 号凌晨。"""
    moment = datetime(2026, 10, 16, 17, 30, tzinfo=UTC)
    assert quota.period_key(moment) == "2026-10-17"


def test_period_key_rolls_at_midnight_cst() -> None:
    before = datetime(2026, 10, 16, 23, 59, 59, tzinfo=CN_TZ)
    after = datetime(2026, 10, 17, 0, 0, 1, tzinfo=CN_TZ)

    assert quota.period_key(before) == "2026-10-16"
    assert quota.period_key(after) == "2026-10-17"


def test_reset_at_is_the_next_cst_midnight() -> None:
    moment = datetime(2026, 10, 16, 9, 0, tzinfo=CN_TZ)

    assert quota.reset_at(moment) == "2026-10-17T00:00:00+08:00"


# ── 计数器（Agent 请求数）：HTTP 面 ─────────────────────────


def test_agent_quota_blocks_when_exhausted(signed_in: FakeDatabase, agent: Any, limits: Any) -> None:
    for _ in range(2):
        assert ask().status_code == 200

    response = ask()

    assert response.status_code == 429
    # **未开流**：SSE 首字节一旦发出就改不了状态码（与 L0 同一条约束）
    assert response.headers["content-type"].startswith("application/json")


def test_agent_quota_rejection_names_metric_and_reset(
    signed_in: FakeDatabase, agent: Any, limits: Any
) -> None:
    for _ in range(2):
        ask()

    body = ask().json()

    assert body["kind"] == "quota_exceeded"
    assert body["metric"] == "agent_calls"
    assert body["limit"] == 2
    assert body["used"] == 2  # 被拒的这次不计数
    assert body["reset_at"] == quota.reset_at()
    assert "Agent 调用" in body["detail"]  # 前端照抄这句


def test_agent_quota_is_per_user(signed_in: FakeDatabase, agent: Any, limits: Any) -> None:
    for _ in range(2):
        ask()
    assert ask().status_code == 429

    app.dependency_overrides[require_user] = lambda: {**TEST_USER, "id": 2}
    assert ask().status_code == 200


def test_limit_zero_means_unlimited(signed_in: FakeDatabase, agent: Any, limits: Any) -> None:
    limits(agent_calls=0)

    for _ in range(4):
        assert ask().status_code == 200


def test_admin_is_exempt(signed_in: FakeDatabase, agent: Any, limits: Any) -> None:
    app.dependency_overrides[require_user] = lambda: {**TEST_USER, "role": "admin"}

    for _ in range(4):
        assert ask().status_code == 200


def test_counter_rolls_over_on_the_next_day(limits: Any) -> None:
    """跨日重置：换 `period_key` 即归零，不需要定时清理。"""
    import asyncio

    limits(agent_calls=1)
    db = FakeDatabase()
    user = {"id": 1, "email": "tester@example.com"}
    today = datetime(2026, 10, 16, 10, 0, tzinfo=CN_TZ)
    tomorrow = today + timedelta(days=1)

    asyncio.run(quota.enforce(db, user, quota.METRIC_AGENT_CALLS, now=today))
    with pytest.raises(quota.QuotaExceeded):
        asyncio.run(quota.enforce(db, user, quota.METRIC_AGENT_CALLS, now=today))
    asyncio.run(quota.enforce(db, user, quota.METRIC_AGENT_CALLS, now=tomorrow))


def test_unknown_metric_fails_loudly() -> None:
    """维度名写错**不许静默放行**——那会是一处「配额看起来在管、其实没管」的洞。"""
    import asyncio

    with pytest.raises(ValueError):
        asyncio.run(quota.enforce(FakeDatabase(), {"id": 1}, "agnet_calls"))


# ── gauge（在飞任务数）：HTTP 面 ────────────────────────────


def test_active_task_quota_blocks_extra_submits(
    signed_in: FakeDatabase, data_dir: Path, limits: Any
) -> None:
    assert submit().status_code == 202
    assert submit().status_code == 202

    response = submit()

    assert response.status_code == 429
    body = response.json()
    assert body["kind"] == "quota_exceeded"
    assert body["metric"] == "active_tasks"
    assert body["limit"] == 2 and body["used"] == 2
    assert "reset_at" not in body  # 不由时钟释放
    assert "等一个结束" in body["detail"]


def test_active_task_quota_releases_on_terminal_state(
    signed_in: FakeDatabase, data_dir: Path, limits: Any
) -> None:
    """跑完成即释放——gauge 直查任务表，不需要任何减操作。"""
    submit()
    submit()
    assert submit().status_code == 429

    assert drive_tasks(signed_in) == 2

    assert submit().status_code == 202


def test_idempotent_replay_is_not_blocked_by_quota(
    signed_in: FakeDatabase, data_dir: Path, limits: Any
) -> None:
    """顺序的守卫：幂等短路在**配额之前**（重复提交不建任务，配额也就无从谈起）。"""
    headers = {"Idempotency-Key": "same-action"}
    first = client.post(
        "/api/v1/backtest",
        json={"strategy": "ma_cross", "symbol": SYMBOL},
        headers=headers,
    )
    assert first.status_code == 202

    # 额度已满（1 个在飞 / 上限 2 的下一发就会满，这里先把额度用掉）
    assert submit().status_code == 202
    assert submit().status_code == 429

    replayed = client.post(
        "/api/v1/backtest",
        json={"strategy": "ma_cross", "symbol": SYMBOL},
        headers=headers,
    )

    assert replayed.status_code == 202
    assert replayed.json()["task_id"] == first.json()["task_id"]


# ── 拒绝记录（admin 的「审计」半边读它）────────────────────


def test_rejection_is_recorded(signed_in: FakeDatabase, agent: Any, limits: Any) -> None:
    for _ in range(2):
        ask()
    ask()

    assert len(signed_in.quota_rejection_rows) == 1
    row = signed_in.quota_rejection_rows[0]
    assert row["user_id"] == 1
    assert row["metric"] == "agent_calls"
    assert (row["used"], row["limit_value"]) == (2, 2)
