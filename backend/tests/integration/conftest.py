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
