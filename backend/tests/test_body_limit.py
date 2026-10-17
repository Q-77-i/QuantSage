"""请求体硬上限与无界字段（安全修复 C#14）。

两条判据：
* 超过 `MAX_REQUEST_BYTES` 的请求在**进入应用层之前**就 413（chunked 也受约束）；
* `axes[].values` / `params` 这类集合字段有显式上限，不能靠请求体上限兜底。
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.core.body_limit import BodyLimitMiddleware
from app.core.config import get_settings
from app.main import app

client = TestClient(app)


def test_oversized_body_is_rejected_before_the_app() -> None:
    limit = get_settings().max_request_bytes
    response = client.post(
        "/api/v1/auth/login",
        content=b"x" * (limit + 1),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    assert "上限" in response.json()["detail"]


def test_body_limit_middleware_allows_body_within_limit() -> None:
    async def echo(scope: Any, receive: Any, send: Any) -> None:
        message = await receive()
        body = message.get("body", b"")
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-length", str(len(body)).encode("ascii"))],
            }
        )
        await send({"type": "http.response.body", "body": body})

    limited = TestClient(BodyLimitMiddleware(echo, max_bytes=8))

    assert limited.post("/", content=b"12345678").status_code == 200
    assert limited.post("/", content=b"123456789").status_code == 413


def test_grid_axis_values_are_bounded(signed_in: Any) -> None:
    response = client.post(
        "/api/v1/optimize/grid",
        json={
            "strategy": "ma_cross",
            "symbol": "600519",
            "axes": [{"param": "fast", "values": list(range(51))}],
        },
    )

    assert response.status_code == 422


def test_nan_and_infinity_are_rejected(signed_in: Any) -> None:
    """`json.loads` 接受 NaN/Infinity，Pydantic 默认也接受；落 JSONB 时才会炸成 500。"""
    nan = client.post(
        "/api/v1/optimize/grid",
        content=(
            '{"strategy":"ma_cross","symbol":"600519",'
            '"axes":[{"param":"fast","values":[NaN,5]}]}'
        ),
        headers={"content-type": "application/json"},
    )
    infinity = client.post(
        "/api/v1/backtest",
        content=(
            '{"strategy":"ma_cross","symbol":"600519",'
            '"params":{"fast":Infinity}}'
        ),
        headers={"content-type": "application/json"},
    )

    assert nan.status_code == 422
    assert infinity.status_code == 422


def test_backtest_params_are_bounded(signed_in: Any) -> None:
    response = client.post(
        "/api/v1/backtest",
        json={
            "strategy": "ma_cross",
            "symbol": "600519",
            "params": {f"p{index}": index for index in range(51)},
        },
    )

    assert response.status_code == 422
