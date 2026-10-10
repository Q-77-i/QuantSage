"""M7a 集成测试：`research_reports` 表、幂等复用、分享与匿名只读，打真实 Postgres 与真实 `data/`。

离线那份（`tests/test_reports_api.py`）注入内存业务库，**证明不了 SQL 里的归属过滤与
`UNIQUE(share_token)` 真的生效**（同 M1c/M4c/M6 的双跑口径）。四件只有真库能验：

  * `research_reports` **幂等建表**（连起两次 `init_schema`）；
  * **幂等复用**按 `(account_id, snapshot_hash)` 走 SQL，真库上第二次生成不新建行；
  * **分享 / 匿名只读 / 撤销** 全链路（未登录的新客户端能读到冻结产物，撤销后 404）；
  * **跨用户**拿不到（归属过滤在 SQL 的 `WHERE user_id` 里）。

综述在本文件里一律注入假模型：集成用例该验的是库与端点，不该依赖外部 LLM 的可用性。
真模型的链路由 `scripts/report_evidence.py` 在真实账户上跑一次留证。

前置条件：`docker compose up -d --wait` + `data/` 已落盘。用 `pytest -m integration` 触发。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import reports as reports_api
from app.core.config import get_settings
from app.core.db import init_schema, open_pool
from app.data import duckdb_client as dc
from app.main import app
from app.report.narrative import Narrative
from tests.integration.conftest import purge_rows, real_stack, sign_up  # noqa: F401

pytestmark = pytest.mark.integration

SYMBOL = "600519"


async def _fake_narrative(facts: dict[str, Any], **kwargs: Any) -> Narrative:
    return Narrative("集成测试综述。", "fake-model")


@pytest.fixture
def stub_narrative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reports_api, "build_narrative", _fake_narrative)


def _fresh_emails(count: int = 2) -> list[str]:
    suffix = uuid.uuid4().hex[:8]
    return [f"m7-{suffix}-{index}@example.com" for index in range(count)]


def _window() -> tuple[date, date]:
    latest = date.fromisoformat(str(dc.latest_dates()["latest_trade_date"]))
    return latest - timedelta(days=90), latest


def _make_account(client: TestClient, name: str) -> str:
    start, end = _window()
    response = client.post(
        "/api/v1/paper/accounts",
        json={
            "name": name,
            "initial_cash": 300_000.0,
            "symbols": [SYMBOL],
            "strategy": "ma_cross",
            "params": {"fast": 5, "slow": 20},
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
    )
    assert response.status_code == 201, response.text
    account_id = response.json()["account"]["id"]
    finished = client.post(f"/api/v1/paper/accounts/{account_id}/run", json={"approve": "all"})
    assert finished.status_code == 200, finished.text
    return account_id


def test_schema_is_idempotent_on_the_real_database() -> None:
    """`research_reports` 的建表语句连跑两次不报错（迁移风格与既有表一致）。"""

    async def setup_twice() -> None:
        async with open_pool(get_settings().postgres_dsn) as pool:
            await init_schema(pool)
            await init_schema(pool)

    asyncio.run(setup_twice())


def test_full_round_trip_on_the_real_database(
    real_stack: TestClient, stub_narrative: None
) -> None:
    """生成 → 幂等复用 → 分享 → 匿名只读 → 撤销 → 404 → 跨用户 404。"""
    emails = _fresh_emails()
    first = sign_up(emails[0])
    account_id = _make_account(first, "M7 集成会话")

    created = first.post("/api/v1/reports", json={"account_id": account_id})
    assert created.status_code == 201, created.text
    payload = created.json()
    body = payload["report"]

    # 冻结产物自证：真实数据的 21 片行情逐片带 sha256
    shards = body["snapshot"]["bars"]["shards"]
    assert len(shards) >= 21 and all(len(s["sha256"]) == 64 for s in shards)
    assert body["snapshot"]["market_end"] == _window()[1].isoformat()
    assert body["blocks"][-1]["kind"] == "inference"

    again = first.post("/api/v1/reports", json={"account_id": account_id})
    assert again.status_code == 201
    assert again.json()["id"] == payload["id"] and again.json()["reused"] is True

    shared = first.post(f"/api/v1/reports/{payload['id']}/share")
    assert shared.status_code == 200
    token = shared.json()["share_token"]
    assert shared.json()["share_path"] == f"/r/{token}"

    # 换一个「浏览器」：新客户端没有 cookie，读公开端点
    anonymous = TestClient(app)
    public = anonymous.get(f"/api/v1/public/reports/{token}")
    assert public.status_code == 200, public.text
    assert public.json()["report"]["account"]["name"] == "M7 集成会话"
    assert "user_id" not in public.json()

    markdown = anonymous.get(f"/api/v1/public/reports/{token}/markdown")
    assert markdown.status_code == 200 and "## 证据链" in markdown.text

    revoked = first.delete(f"/api/v1/reports/{payload['id']}/share")
    assert revoked.status_code == 200
    assert anonymous.get(f"/api/v1/public/reports/{token}").status_code == 404

    # 跨用户：另一个人拿不到这份报告
    second = sign_up(emails[1])
    assert second.get(f"/api/v1/reports/{payload['id']}").status_code == 404
    assert second.post(f"/api/v1/reports/{payload['id']}/share").status_code == 404

    purge_rows(emails, [])


def test_account_advancing_yields_a_new_report(
    real_stack: TestClient, stub_narrative: None
) -> None:
    """账户往前推进后，决策日志变了 ⇒ 快照变了 ⇒ 出一份**新的**报告（旧的留在库里）。"""
    emails = _fresh_emails(1)
    client = sign_up(emails[0])
    start, end = _window()
    created = client.post(
        "/api/v1/paper/accounts",
        json={
            "name": "M7 半程会话",
            "initial_cash": 300_000.0,
            "symbols": [SYMBOL],
            "strategy": "ma_cross",
            "params": {"fast": 5, "slow": 20},
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
    ).json()
    account_id = created["account"]["id"]
    for _ in range(5):  # 只推进几天：账户没跑完（`step` 不接受 body，一次一天）
        stepped = client.post(f"/api/v1/paper/accounts/{account_id}/step")
        assert stepped.status_code == 200, stepped.text

    first = client.post("/api/v1/reports", json={"account_id": account_id})
    assert first.status_code == 201, first.text

    client.post(f"/api/v1/paper/accounts/{account_id}/run", json={"approve": "all"})
    second = client.post("/api/v1/reports", json={"account_id": account_id})
    assert second.status_code == 201, second.text

    assert second.json()["id"] != first.json()["id"]
    assert second.json()["snapshot_hash"] != first.json()["snapshot_hash"]
    listed = client.get(f"/api/v1/reports?account_id={account_id}").json()["reports"]
    assert len(listed) == 2

    purge_rows(emails, [])
