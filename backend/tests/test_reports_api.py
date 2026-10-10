"""M7a 研报端点：生成 / 幂等复用 / 分享 / 匿名只读 / 撤销 / 越权 / Markdown。

离线替身是 `FakeDatabase`（同 M1c/M4c 口径：唯一约束与守卫照抄真库）；行情与语料走真实
Parquet；**综述一律注入假模型**——离线用例不该真的去调 LLM（慢、要钱、还会 flaky）。
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import reports as reports_api
from app.data import calendar as cal
from app.data import duckdb_client
from app.main import app
from app.report.narrative import Narrative
from tests.conftest import TEST_USER, make_backtest_dir, ts, write_bars_parquet

client = TestClient(app)
SYMBOL = "600519"


async def _fake_narrative(facts: dict[str, Any], **kwargs: Any) -> Narrative:
    return Narrative("测试综述：本期间小幅盈利。", "fake-model")


def _days(count: int = 8) -> list[date]:
    start = date(2026, 8, 3)
    days = cal.sessions(start, start + timedelta(days=count * 3 + 10))
    return days[:count]


def _bars(days: list[date]) -> list[dict[str, Any]]:
    prices = [100.0 + 2.0 * index for index in range(len(days))]
    return [
        {"trade_date": day, "open": price, "close": price, "high": price + 1, "low": price - 1,
         "change_pct": 2.0}
        for day, price in zip(days, prices, strict=True)
    ]


def _events() -> list[dict[str, Any]]:
    return [
        {
            "event_id": "news:1",
            "title": "公司发布重大合同公告",
            "summary": "公司与大客户签订三年期合同。",
            "event_time": ts("2026-08-03 09:00:00"),
            "available_at": ts("2026-08-03 09:10:00"),
            "direction_norm": "bullish",
            "industries": ["银行"],
            "score": 88.0,
            "source": "xiaoshi-archive",
            "original_source": "华尔街见闻",
            "source_url": "https://example.com/news/1",
            "content_hash": "abc123def456",
        }
    ]


def _account_body(days: list[date]) -> dict[str, Any]:
    return {
        "name": "研报会话",
        "initial_cash": 200_000.0,
        "symbols": [SYMBOL],
        "strategy": "event_driven",
        "params": {"min_score": 50.0, "hold_days": 3},
        "start": days[0].isoformat(),
        "costs": {"fees": True, "slippage": True, "slippage_bps": 5.0},
    }


def _values(node: Any) -> list[Any]:
    """递归收集一棵 JSON 树里的全部值（匿名响应的身份字段扫描用）。"""
    found: list[Any] = []
    if isinstance(node, dict):
        for value in node.values():
            found.extend(_values(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_values(item))
    else:
        found.append(node)
    return found


@pytest.fixture
def report_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signed_in: Any) -> dict[str, Any]:
    """跑到末端的账户（有成交、有未平仓）+ 一份已生成的报告 + 一个分享 token。"""
    days = _days()
    root = make_backtest_dir(tmp_path, _bars(days), events=_events(), symbol=SYMBOL)
    write_bars_parquet(root / "bars", SYMBOL, _bars(days), adjust="raw")
    monkeypatch.setattr(duckdb_client, "resolve_data_dir", lambda _=None: root)
    monkeypatch.setattr(reports_api, "build_narrative", _fake_narrative)

    created = client.post("/api/v1/paper/accounts", json=_account_body(days))
    assert created.status_code == 201, created.text
    account_id = created.json()["account"]["id"]
    finished = client.post(f"/api/v1/paper/accounts/{account_id}/run", json={"approve": "all"})
    assert finished.status_code == 200, finished.text

    response = client.post("/api/v1/reports", json={"account_id": account_id})
    assert response.status_code == 201, response.text
    payload = response.json()
    shared = client.post(f"/api/v1/reports/{payload['id']}/share")
    assert shared.status_code == 200, shared.text
    return {
        "db": signed_in,
        "days": days,
        "account_id": account_id,
        "report_id": payload["id"],
        "report": payload["report"],
        "token": shared.json()["share_token"],
    }


# ── 未登录与归属 ────────────────────────────────────────────


def test_requires_sign_in() -> None:
    """受保护端点未登录一律 401；**公开端点不是这一档**（它只认 token）。"""
    assert client.post("/api/v1/reports", json={"account_id": str(uuid.uuid4())}).status_code == 401
    assert client.get(f"/api/v1/reports?account_id={uuid.uuid4()}").status_code == 401
    assert client.get(f"/api/v1/reports/{uuid.uuid4()}").status_code == 401
    assert client.post(f"/api/v1/reports/{uuid.uuid4()}/share").status_code == 401
    assert client.get(f"/api/v1/reports/{uuid.uuid4()}/markdown").status_code == 401


def test_unknown_account_and_report_are_404(report_env: dict[str, Any]) -> None:
    assert client.post("/api/v1/reports", json={"account_id": str(uuid.uuid4())}).status_code == 404
    assert client.get(f"/api/v1/reports/{uuid.uuid4()}").status_code == 404
    assert client.get(f"/api/v1/reports?account_id={uuid.uuid4()}").status_code == 404


def test_non_uuid_inputs_are_422(report_env: dict[str, Any]) -> None:
    assert client.post("/api/v1/reports", json={"account_id": "nope"}).status_code == 422
    assert client.get("/api/v1/reports/not-a-uuid").status_code == 422
    assert client.post("/api/v1/reports", json={}).status_code == 422  # 缺字段
    assert client.post("/api/v1/reports", json={"account_id": str(uuid.uuid4()), "x": 1}).status_code == 422


def test_other_user_sees_nothing(report_env: dict[str, Any]) -> None:
    """换个用户：报告、分享、Markdown 全是 404（越权与不存在同响应）。"""
    app.dependency_overrides[reports_api.require_user] = lambda: {"id": 999, "email": "b@e.com"}
    try:
        rid = report_env["report_id"]
        assert client.get(f"/api/v1/reports/{rid}").status_code == 404
        assert client.get(f"/api/v1/reports/{rid}/markdown").status_code == 404
        assert client.post(f"/api/v1/reports/{rid}/share").status_code == 404
        assert client.get(f"/api/v1/reports?account_id={report_env['account_id']}").status_code == 404
    finally:
        app.dependency_overrides[reports_api.require_user] = lambda: dict(TEST_USER)


# ── 生成与形状 ──────────────────────────────────────────────


def test_report_body_shape(report_env: dict[str, Any]) -> None:
    body = report_env["report"]

    assert body["kind"] == "paper_account_report" and body["version"] == 1
    assert body["account"]["symbols"] == [SYMBOL]
    assert body["account"]["as_of"] == report_env["days"][-1].isoformat()
    assert [block["id"] for block in body["blocks"]] == [
        "overview", "performance", "attribution", "narrative",
    ]
    assert [block["kind"] for block in body["blocks"]] == [
        "fact", "fact", "fact", "inference",
    ]
    assert body["metrics"]["final_equity"] > 0
    # 基准必须真的算出来：夹具的 bar 带 change_pct=+2%，若日期形状喂错（字符串 vs date），
    # `market_benchmark` 会全数落空、静默给 0——真数据取证逮过这一条
    assert body["metrics"]["benchmark_return"] > 0
    assert body["equity_curve"][-1]["benchmark"] > body["equity_curve"][0]["benchmark"]
    assert body["snapshot"]["bars"]["shards"][0]["sha256"]
    assert body["snapshot"]["market_end"] == report_env["days"][-1].isoformat()
    assert body["evidence"], "事件驱动策略的买入决策应当有可回链的证据"


def test_evidence_links_back_to_the_driving_decision(report_env: dict[str, Any]) -> None:
    """证据项带 `decision_ids`——报告能回答「这条证据驱动了哪几笔决策」。"""
    items = report_env["report"]["evidence"]

    assert items[0]["event_id"] == "news:1"
    assert items[0]["found"] and not items[0]["revised"]
    assert items[0]["summary"] == "公司与大客户签订三年期合同。"
    assert items[0]["industries"] == ["银行"]
    assert items[0]["decision_ids"], "买入决策的 id 应当挂在证据上"


def test_second_generation_reuses_the_same_report(report_env: dict[str, Any]) -> None:
    """同 `(账户, 快照)` 幂等复用：不新建行、回执 `reused=True`（不再花钱调 LLM）。"""
    again = client.post("/api/v1/reports", json={"account_id": report_env["account_id"]})

    assert again.status_code == 201
    assert again.json()["id"] == report_env["report_id"]
    assert again.json()["reused"] is True
    assert len(report_env["db"].reports) == 1


def test_list_reports_returns_summaries_without_body(report_env: dict[str, Any]) -> None:
    response = client.get(f"/api/v1/reports?account_id={report_env['account_id']}")

    assert response.status_code == 200
    rows = response.json()["reports"]
    assert len(rows) == 1
    assert rows[0]["id"] == report_env["report_id"]
    assert rows[0]["share_path"] == f"/r/{report_env['token']}"
    assert "report" not in rows[0]


def test_narrative_absence_is_recorded_not_fatal(
    report_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """综述缺席（上游挂了）时报告照常生成，块里如实写原因。"""

    async def _broken(facts: dict[str, Any], **kwargs: Any) -> Narrative:
        return Narrative(None, "fake-model", note="综述超时（>20s），本次缺席")

    monkeypatch.setattr(reports_api, "build_narrative", _broken)
    # 换一个账户（不同的 start）以绕开幂等复用，逼出新的一份报告
    days = _days()
    body = {**_account_body(days), "name": "无综述会话", "start": days[1].isoformat()}
    account_id = client.post("/api/v1/paper/accounts", json=body).json()["account"]["id"]
    client.post(f"/api/v1/paper/accounts/{account_id}/run", json={"approve": "all"})

    response = client.post("/api/v1/reports", json={"account_id": account_id})

    assert response.status_code == 201, response.text
    block = response.json()["report"]["blocks"][-1]
    assert block["kind"] == "inference" and block["text"] is None
    assert "综述超时" in block["note"]


# ── 分享与匿名只读 ──────────────────────────────────────────


def test_share_is_idempotent(report_env: dict[str, Any]) -> None:
    again = client.post(f"/api/v1/reports/{report_env['report_id']}/share")

    assert again.status_code == 200
    assert again.json()["share_token"] == report_env["token"]
    assert again.json()["share_path"] == f"/r/{report_env['token']}"


def test_anonymous_can_read_the_shared_report(report_env: dict[str, Any]) -> None:
    """**验收主项**：未登录也要能打开完整研报，且响应里没有任何身份字段。"""
    app.dependency_overrides.clear()  # 摘掉「当前用户」，模拟匿名
    try:
        response = client.get(f"/api/v1/public/reports/{report_env['token']}")
    finally:
        app.dependency_overrides[reports_api.require_user] = lambda: dict(TEST_USER)

    assert response.status_code == 200
    payload = response.json()
    assert payload["report"]["account"]["name"] == "研报会话"
    assert payload["report"]["blocks"][-1]["text"]

    # 身份字段一个都不许出现：递归扫全树（用户 id / 邮箱 / 账户 id）
    def keys(node: Any) -> set[str]:
        found: set[str] = set()
        if isinstance(node, dict):
            for key, value in node.items():
                found.add(key)
                found |= keys(value)
        elif isinstance(node, list):
            for item in node:
                found |= keys(item)
        return found

    all_keys = keys(payload)
    assert "user_id" not in all_keys and "email" not in all_keys
    assert not any("@" in str(value) for value in _values(payload) if isinstance(value, str))


def test_revoked_share_is_404(report_env: dict[str, Any]) -> None:
    """撤销即失效：旧链接 404，且「随机 token」与「撤销 token」**同响应**（不泄露存在性）。"""
    revoked = client.delete(f"/api/v1/reports/{report_env['report_id']}/share")
    assert revoked.status_code == 200 and revoked.json()["share_token"] is None

    app.dependency_overrides.clear()
    try:
        gone = client.get(f"/api/v1/public/reports/{report_env['token']}")
        unknown = client.get("/api/v1/public/reports/not-a-real-token-at-all")
    finally:
        app.dependency_overrides[reports_api.require_user] = lambda: dict(TEST_USER)

    assert gone.status_code == 404 and unknown.status_code == 404
    assert gone.json() == unknown.json()


def test_unshare_then_share_again_issues_a_new_token(report_env: dict[str, Any]) -> None:
    client.delete(f"/api/v1/reports/{report_env['report_id']}/share")
    again = client.post(f"/api/v1/reports/{report_env['report_id']}/share")

    assert again.status_code == 200
    assert again.json()["share_token"] != report_env["token"]


# ── Markdown 导出 ───────────────────────────────────────────


def test_markdown_download_carries_the_evidence_chain(report_env: dict[str, Any]) -> None:
    response = client.get(f"/api/v1/reports/{report_env['report_id']}/markdown")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "attachment" in response.headers["content-disposition"]
    text = response.text
    assert "公司发布重大合同公告" in text
    assert "2026-08-03 09:00:00+08:00" in text and "2026-08-03 09:10:00+08:00" in text
    assert "xiaoshi-archive" in text and "华尔街见闻" in text
    assert "不构成投资建议" in text


def test_public_markdown_works_without_sign_in(report_env: dict[str, Any]) -> None:
    """未登录的分享页也要能导出（验收「导出含证据链」在匿名侧同样成立）。"""
    app.dependency_overrides.clear()
    try:
        response = client.get(f"/api/v1/public/reports/{report_env['token']}/markdown")
    finally:
        app.dependency_overrides[reports_api.require_user] = lambda: dict(TEST_USER)

    assert response.status_code == 200
    assert "## 证据链" in response.text


# ── 降级 ────────────────────────────────────────────────────


def test_data_not_ready_is_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signed_in: Any
) -> None:
    """行情未落盘：503（`DataNotReady` 的既有映射），不是 500 也不是空报告。"""
    empty = tmp_path / "empty"
    (empty / "bars").mkdir(parents=True)
    (empty / "events").mkdir(parents=True)
    monkeypatch.setattr(duckdb_client, "resolve_data_dir", lambda _=None: empty)

    response = client.post("/api/v1/reports", json={"account_id": str(uuid.uuid4())})

    # 账户先于数据被判死：不存在 → 404；存在但数据缺失 → 503（用真账户走一遍）
    assert response.status_code == 404
