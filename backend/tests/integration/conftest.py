"""集成测试公用件。

集成用例打的是**开发库**（`quantsage`），所以建出来的账号、会话必须自己收干净，
不留垃圾——这里放共用的清库出口、鉴权夹具与注册助手。

M1c 起回测与自选股也要登录，`real_stack` / `sign_up` 被三个文件共用，故上提到这里
（各文件自备一份会让「跑 lifespan」这件事出现三种写法）。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app

#: langgraph 自己的三张表，thread_id 是 TEXT
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")

PASSWORD = "integration-pass"
#: 32 字节以上：PyJWT 对短 HMAC 密钥会告警（RFC 7518 §3.2）
JWT_SECRET = "integration-secret-not-a-real-key-0123456789"


@pytest.fixture
def jwt_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """给集成栈一个确定的密钥：环境变量优先于 `.env`，结果与本机配置无关。"""
    monkeypatch.setenv("JWT_SECRET", JWT_SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def real_stack(jwt_env: None) -> Iterator[TestClient]:
    """起真实应用（跑 lifespan：连库、建表、装配 Agent；小石 MCP 起不来也只是降级）。

    **必须是 `with`**：不跑 lifespan 就没有 `app.state.db`，受保护端点会一律 503。
    """
    with TestClient(app) as client:
        yield client


def wait_for_task(client: TestClient, task_id: str, *, timeout: float = 60.0) -> dict:
    """等任务到终态（P3-E1a）。集成栈跑的是**真 lifespan**——worker 真的在跑。

    所以这里**轮询真路径**而不是手动驱动：单次回测 0.3–2s + 取件间隔 200ms，
    `timeout=60` 只是防挂；顺带把「提交 → worker 取件 → 落库」整条链路验在这一处。
    """
    import time

    deadline = time.monotonic() + timeout
    while True:
        task = client.get(f"/api/v1/tasks/{task_id}").json()
        if task["status"] in {"done", "failed", "cancelled"}:
            return task
        if time.monotonic() > deadline:
            raise AssertionError(f"任务 {task_id} 超过 {timeout:.0f}s 仍未结束：{task}")
        time.sleep(0.1)


def run_backtest(client: TestClient, body: dict) -> tuple[dict, dict]:
    """提交一次回测并等它跑完，返回 `(任务, 报告信封)`。失败时断言先炸在调用方那里。"""
    response = client.post("/api/v1/backtest", json=body)
    assert response.status_code == 202, response.text
    task = wait_for_task(client, response.json()["task_id"])
    if task["status"] != "done":
        return task, {}
    detail = client.get(f"/api/v1/backtest/runs/{task['result']['run_id']}").json()
    return task, {"run_id": detail["id"], "report": detail["report"]}


def sign_up(email: str) -> TestClient:
    """注册一个账号，返回带会话 cookie 的客户端（注册即登录态，与端点同口径）。"""
    client = TestClient(app)
    response = client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    return client


def purge_rows(emails: list[str], thread_ids: list[str]) -> None:
    """删账号（`chat_threads` 随 users 级联）与残留 checkpoint。

    走同步 psycopg 直连：应用侧的异步池绑在 TestClient 的事件循环上，测试线程碰不得。
    """
    import psycopg

    with psycopg.connect(get_settings().postgres_dsn) as conn:
        if emails:
            conn.execute("DELETE FROM users WHERE email = ANY(%s)", (emails,))
        if thread_ids:
            for table in CHECKPOINT_TABLES:
                conn.execute(
                    f"DELETE FROM {table} WHERE thread_id = ANY(%s)", (thread_ids,)
                )
