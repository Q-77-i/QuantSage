"""离线单测：注册 / 登录 / 退出 / 当前用户四个端点的真实依赖链。

业务库用 `FakeDatabase`（与真库同方法面），其余（路由、依赖、cookie、JWT）全是真的——
所以这里验的是「端点的行为」，真库那份在 `tests/integration/test_auth_m1.py` 里双跑。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.auth import COOKIE_NAME, require_user
from app.core.config import get_settings
from app.main import app
from tests.fakes import FakeDatabase

pytestmark = pytest.mark.usefixtures("jwt_secret")

CREDENTIALS = {"email": "Alice@Example.com", "password": "s3cret-passphrase"}


@pytest.fixture
def db() -> Any:
    """注入内存业务库（不覆盖鉴权依赖——本文件要验的就是鉴权本身）。"""
    database = FakeDatabase()
    app.state.db = database
    yield database
    app.state.db = None


@pytest.fixture
def client(db: Any) -> TestClient:
    return TestClient(app)


def test_register_creates_user_and_signs_in(client: TestClient) -> None:
    response = client.post("/api/v1/auth/register", json=CREDENTIALS)

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "alice@example.com"  # 统一转小写入库
    assert "password" not in response.text  # 明文不回显
    assert COOKIE_NAME in response.cookies


def test_register_stores_hash_not_plaintext(client: TestClient, db: Any) -> None:
    client.post("/api/v1/auth/register", json=CREDENTIALS)
    stored = db.users[1]["password_hash"]

    assert stored != CREDENTIALS["password"]
    assert stored.startswith("$2b$")


def test_register_duplicate_email_is_409(client: TestClient) -> None:
    client.post("/api/v1/auth/register", json=CREDENTIALS)
    again = client.post(
        "/api/v1/auth/register",
        json={"email": "alice@example.com", "password": "another-passphrase"},
    )

    assert again.status_code == 409


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "not-an-email", "password": "long-enough-pass"},
        {"email": "a@b.com", "password": "short"},  # 少于 8 字节
        {"email": "a@b.com", "password": "密" * 25},  # 75 字节 > bcrypt 的 72
        {"email": "a@b.com", "password": ""},
    ],
)
def test_register_rejects_bad_input(client: TestClient, payload: dict[str, str]) -> None:
    assert client.post("/api/v1/auth/register", json=payload).status_code == 422


def test_password_byte_limit_is_about_bytes(client: TestClient) -> None:
    """24 个汉字 = 72 字节，正好压线通过；25 个就越界。"""
    ok = client.post(
        "/api/v1/auth/register",
        json={"email": "bytes@example.com", "password": "密" * 24},
    )
    assert ok.status_code == 201


def test_login_with_normalized_email(client: TestClient) -> None:
    client.post("/api/v1/auth/register", json=CREDENTIALS)
    client.post("/api/v1/auth/logout")

    response = client.post(
        "/api/v1/auth/login",
        json={"email": "ALICE@example.com", "password": CREDENTIALS["password"]},
    )

    assert response.status_code == 200
    assert response.json()["email"] == "alice@example.com"
    assert COOKIE_NAME in response.cookies


def test_remember_me_controls_cookie_persistence(client: TestClient) -> None:
    """勾「记住我」→ 持久 cookie（带 Max-Age）；不勾 → 会话 cookie（关浏览器即失效）。"""
    client.post("/api/v1/auth/register", json=CREDENTIALS)
    client.post("/api/v1/auth/logout")

    remembered = client.post(
        "/api/v1/auth/login",
        json={**CREDENTIALS, "remember": True},
    )
    forgotten = client.post(
        "/api/v1/auth/login",
        json={**CREDENTIALS, "remember": False},
    )

    assert "Max-Age=" in remembered.headers["set-cookie"]
    assert "Max-Age=" not in forgotten.headers["set-cookie"]
    assert "Expires=" not in forgotten.headers["set-cookie"]
    # 两种都是有效登录，只是存活方式不同
    assert remembered.status_code == forgotten.status_code == 200


def test_login_failures_are_indistinguishable(client: TestClient) -> None:
    """邮箱不存在与密码错误：同一状态码、同一文案，不泄露邮箱是否注册过。"""
    client.post("/api/v1/auth/register", json=CREDENTIALS)

    wrong_password = client.post(
        "/api/v1/auth/login",
        json={"email": "alice@example.com", "password": "wrong-passphrase"},
    )
    unknown_email = client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": "wrong-passphrase"},
    )

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()


def test_logout_is_idempotent(client: TestClient) -> None:
    """没登录也能调：清一个不存在的 cookie 没有副作用。"""
    response = client.post("/api/v1/auth/logout")

    assert response.status_code == 200
    assert "Max-Age=0" in response.headers["set-cookie"]


def test_me_requires_login(client: TestClient) -> None:
    assert client.get("/api/v1/auth/me").status_code == 401


def test_me_returns_current_user(client: TestClient) -> None:
    client.post("/api/v1/auth/register", json=CREDENTIALS)

    response = client.get("/api/v1/auth/me")

    assert response.status_code == 200
    assert response.json() == {"id": 1, "email": "alice@example.com"}


def test_me_after_logout_is_401(client: TestClient) -> None:
    client.post("/api/v1/auth/register", json=CREDENTIALS)
    client.post("/api/v1/auth/logout")

    assert client.get("/api/v1/auth/me").status_code == 401


def test_me_with_token_of_deleted_user_is_401(client: TestClient, db: Any) -> None:
    """库被重置/用户被删后，旧 cookie 还能解出 id——端点必须兜住。"""
    client.post("/api/v1/auth/register", json=CREDENTIALS)
    db.users.clear()

    assert client.get("/api/v1/auth/me").status_code == 401


def test_auth_endpoints_are_503_without_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """JWT_SECRET 缺失时不静默降级为无鉴权，而是显式 503。"""
    monkeypatch.setenv("JWT_SECRET", "")
    get_settings.cache_clear()
    app.state.db = FakeDatabase()
    try:
        response = TestClient(app).get("/api/v1/auth/me")
    finally:
        app.state.db = None
        get_settings.cache_clear()

    assert response.status_code == 503


def test_auth_endpoints_are_503_without_database(client: TestClient) -> None:
    """库没起来（降级启动）时 503，而不是 AttributeError。"""
    app.state.db = None
    app.dependency_overrides.pop(require_user, None)

    assert client.post("/api/v1/auth/login", json=CREDENTIALS).status_code == 503
