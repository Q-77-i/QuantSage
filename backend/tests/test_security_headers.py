"""离线单测：安全响应头（E8a，SPEC §8.2）。

三条主张，各验一条：① 每个响应都有那五个头；② **流式响应也有**（三条 SSE 路由直出
`StreamingResponse`，纯 ASGI 中间件方案就是为它选的）；③ 错误响应也有（`HTTPException`
由内层 `ExceptionMiddleware` 处理，头由更外层的中间件补上）。
"""

from __future__ import annotations

import pytest
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.types import Receive, Scope, Send

from app.core.security import SECURITY_HEADERS, SecurityHeadersMiddleware
from app.main import app

client = TestClient(app)


@pytest.mark.parametrize(
    ("name", "value"), SECURITY_HEADERS, ids=[name.decode() for name, _ in SECURITY_HEADERS]
)
def test_api_response_carries_the_header(name: bytes, value: bytes) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.headers[name.decode()] == value.decode()


def test_docs_pages_skip_only_the_csp() -> None:
    """Swagger/ReDoc 是 HTML 页面，需要 CDN + 内联脚本；CSP 只对 JSON API 成立。

    跳过 CSP 是为了不白屏；其余四个安全头（尤其 `frame-ancestors` 对应的
    `x-frame-options`）必须保留。
    """
    for path in ("/docs", "/redoc", "/docs/oauth2-redirect"):
        response = client.get(path)
        assert response.status_code == 200
        assert "content-security-policy" not in response.headers
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"

    # 普通 API 仍必须是严格 CSP（防止把例外写成全局）
    assert "content-security-policy" in client.get("/openapi.json").headers


def test_csp_is_the_api_flavour() -> None:
    """API 返回的是 JSON，没有子资源 ⇒ `default-src 'none'` 一行成立，不是空头。"""
    csp = dict((k.decode(), v.decode()) for k, v in SECURITY_HEADERS)["content-security-policy"]
    assert "default-src 'none'" in csp
    # 本地是 http：HSTS 加了也没有浏览器会理它（SPEC §8.2），这里顺带钉住「确实没加」
    assert "strict-transport-security" not in dict(
        (k.decode(), v.decode()) for k, v in SECURITY_HEADERS
    )


def test_headers_land_on_error_responses() -> None:
    """401 也要带头——浏览器侧对错误响应同样执行 nosniff 与 frame-ancestors。"""
    response = client.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert response.headers["x-content-type-options"] == "nosniff"


def test_cors_preflight_allows_idempotency_key() -> None:
    """提交类端点的跨源预检必须放行 `Idempotency-Key`。

    前端 3001 ↔ API 8000 是跨源，带自定义头必走预检；漏放行时浏览器在**发出真实请求之前**
    就 400，表现为「点提交没反应」。后端集成用例直接发 POST，不跑预检，所以这条只能在
    CORS 层钉住（E4b 的实际故障）。
    """
    response = client.options(
        "/api/v1/backtest",
        headers={
            "Origin": "http://127.0.0.1:3001",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,idempotency-key",
        },
    )

    assert response.status_code == 200
    allowed = response.headers["access-control-allow-headers"].lower()
    assert "idempotency-key" in allowed


def test_headers_land_on_streamed_responses() -> None:
    """**直出 `StreamingResponse` 的响应也带头**——chat / research / tasks events 都走这条路。

    用一个最小 ASGI 应用验：`BaseHTTPMiddleware` 会包装响应体迭代器、给 SSE 引入缓冲与
    额外任务，所以本仓用的是纯 ASGI 中间件（`app/core/security.py` 的 docstring）；这条
    用例把「换掉中间件基类也不会掉头」这件事钉在行为上。
    """

    async def stream_app(scope: Scope, receive: Receive, send: Send) -> None:
        await StreamingResponse(iter(["a", "b"]))(scope, receive, send)

    streamed = TestClient(SecurityHeadersMiddleware(stream_app))
    response = streamed.get("/")

    assert response.status_code == 200
    assert response.text == "ab"  # 流没被吃改
    assert response.headers["x-frame-options"] == "DENY"
