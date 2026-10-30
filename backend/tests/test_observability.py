"""E7e / P1：访问日志只记路由模板，不把分享 token 写进日志。"""

from __future__ import annotations

import logging

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.core.observability import ObservabilityMiddleware


async def _endpoint(request):  # type: ignore[no-untyped-def]
    return PlainTextResponse("ok")


def test_access_log_uses_route_template_not_the_raw_path(caplog) -> None:  # type: ignore[no-untyped-def]
    app = Starlette(routes=[Route("/api/v1/public/reports/{token}", _endpoint)])
    app.add_middleware(ObservabilityMiddleware)

    with caplog.at_level(logging.INFO, logger="quantsage.http"):
        response = TestClient(app).get("/api/v1/public/reports/secret-token")

    assert response.status_code == 200
    message = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("http_request")
    )
    assert "secret-token" not in message
    assert "/api/v1/public/reports/{token}" in message
