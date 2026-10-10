"""M7d 导出令牌与 PDF 端点：令牌是**能力**（绑死报告 + 短时），端点是两条路（登录 / 分享）。

渲染本身（起 Chrome 出 PDF）在 `tests/integration` 外验不了（要真浏览器），这里把
`render_report_pdf` 打成桩，验的是**路由、归属与头**；真渲染由界面验证脚本点「导出 PDF」覆盖。
"""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import reports as reports_api
from app.data import duckdb_client
from app.main import app
from app.report import pdf as pdf_module
from app.report.pdf import issue_export_token, verify_export_token
from tests.conftest import TEST_USER, make_backtest_dir, ts, write_bars_parquet
from tests.test_reports_api import _account_body, _bars, _days, _events, _fake_narrative

client = TestClient(app)
SYMBOL = "600519"

FAKE_PDF = b"%PDF-1.4\n% fake pdf for tests\n%%EOF\n"


# ── 令牌 ────────────────────────────────────────────────────


def test_token_binds_report_and_expires() -> None:
    """令牌绑死 report_id，且有有效期——换 id、过期、乱写一律 False。"""
    report_id = str(uuid.uuid4())
    token = issue_export_token(report_id, now=1_000.0)

    assert verify_export_token(report_id, token, now=1_100.0)
    assert not verify_export_token(str(uuid.uuid4()), token, now=1_100.0)
    assert not verify_export_token(report_id, token, now=99_999.0)  # 过期
    assert not verify_export_token(report_id, "not-a-token", now=1_100.0)
    assert not verify_export_token(report_id, "abc.def", now=1_100.0)
    assert not verify_export_token(report_id, "", now=1_100.0)


def test_token_signature_actually_covers_the_payload() -> None:
    """把过期时间改一位，签名必须对不上（否则等于没有签名）。"""
    report_id = str(uuid.uuid4())
    token = issue_export_token(report_id, now=1_000.0)
    expires, signature = token.split(".", 1)

    tampered = f"{int(expires) + 3600}.{signature}"
    assert not verify_export_token(report_id, tampered, now=1_100.0)


def test_print_page_url_carries_the_token() -> None:
    url = pdf_module.print_page_url("r1", "tok", base_url="http://front/")

    assert url == "http://front/print/r1?t=tok"


# ── 端点 ────────────────────────────────────────────────────


@pytest.fixture
def pdf_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signed_in: Any) -> dict[str, Any]:
    """一份已生成的报告 + 已分享的 token；渲染器打桩（不真起浏览器）。"""
    from langgraph.store.memory import InMemoryStore

    from app.memory import service as memory_service
    from app.memory.decision_store import DecisionMemory

    days = _days()
    root = make_backtest_dir(tmp_path, _bars(days), events=_events(), symbol=SYMBOL)
    write_bars_parquet(root / "bars", SYMBOL, _bars(days), adjust="raw")
    monkeypatch.setattr(duckdb_client, "resolve_data_dir", lambda _=None: root)
    monkeypatch.setattr(reports_api, "build_narrative", _fake_narrative)

    async def fake_reflection(facts: dict[str, Any], **kwargs: Any) -> Any:
        from app.report.narrative import Narrative

        return Narrative("教训", "fake-model")

    monkeypatch.setattr(memory_service, "build_reflection", fake_reflection)
    app.state.memory = DecisionMemory(InMemoryStore())

    rendered: list[str] = []

    async def fake_render(report_id: str, **kwargs: Any) -> bytes:
        rendered.append(report_id)
        return FAKE_PDF

    monkeypatch.setattr(reports_api, "render_report_pdf", fake_render)

    account_id = client.post("/api/v1/paper/accounts", json=_account_body(days)).json()["account"][
        "id"
    ]
    client.post(f"/api/v1/paper/accounts/{account_id}/run", json={"approve": "all"})
    report = client.post("/api/v1/reports", json={"account_id": account_id}).json()
    share = client.post(f"/api/v1/reports/{report['id']}/share").json()
    return {
        "report_id": report["id"],
        "account_name": report["report"]["account"]["name"],
        "share_token": share["share_token"],
        "rendered": rendered,
    }


def test_pdf_download_requires_sign_in(pdf_env: dict[str, Any]) -> None:
    app.dependency_overrides.clear()
    try:
        response = client.get(f"/api/v1/reports/{pdf_env['report_id']}/pdf")
        assert response.status_code == 401
    finally:
        app.dependency_overrides[reports_api.require_user] = lambda: dict(TEST_USER)


def test_pdf_download_returns_a_file_with_a_safe_filename(pdf_env: dict[str, Any]) -> None:
    response = client.get(f"/api/v1/reports/{pdf_env['report_id']}/pdf")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.content == FAKE_PDF
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    # 中文名走 RFC 5987，ASCII 兜底不能带非 latin-1 字符（否则 starlette 编码头就炸）
    assert "filename*=UTF-8''" in disposition and "%E" in disposition
    assert pdf_env["rendered"] == [pdf_env["report_id"]]


def test_download_filename_identifies_the_report(pdf_env: dict[str, Any]) -> None:
    """附件名 = 账户名 + **报告指纹前 8 位**（用户反馈：一个账户有很多份研报，不能重名、
    更不能叫 report）。中文名走 `filename*`，ASCII 兜底**同样带指纹**。"""
    from app.api.reports import _disposition

    row = client.get(f"/api/v1/reports/{pdf_env['report_id']}").json()
    short = row["report_hash"][:8]
    disposition = client.get(f"/api/v1/reports/{pdf_env['report_id']}/pdf").headers[
        "content-disposition"
    ]

    assert short in disposition  # 同一份报告：每次导出同名
    assert "report." not in disposition and "-report" not in disposition

    # 全中文名：ASCII 兜底也得有意义且唯一（带指纹），不是「report」
    chinese = _disposition("银行事件驱动", "deadbeefcafe0000", suffix=".pdf")
    assert 'filename="quantsage-deadbeef.pdf"' in chinese
    assert "%E9%93%B6%E8%A1%8C" in chinese  # filename* 里是中文真名

    # 两份不同的报告 ⇒ 文件名不同
    other = _disposition("银行事件驱动", "ffffffff00000000", suffix=".pdf")
    assert other != chinese


def test_pdf_download_is_scoped_to_the_owner(pdf_env: dict[str, Any]) -> None:
    app.dependency_overrides[reports_api.require_user] = lambda: {"id": 999, "email": "b@e.com"}
    try:
        assert client.get(f"/api/v1/reports/{pdf_env['report_id']}/pdf").status_code == 404
    finally:
        app.dependency_overrides[reports_api.require_user] = lambda: dict(TEST_USER)


def test_public_pdf_needs_a_valid_share_token(pdf_env: dict[str, Any]) -> None:
    app.dependency_overrides.clear()
    try:
        ok = client.get(f"/api/v1/public/reports/{pdf_env['share_token']}/pdf")
        bad = client.get("/api/v1/public/reports/not-a-real-token/pdf")
    finally:
        app.dependency_overrides[reports_api.require_user] = lambda: dict(TEST_USER)

    assert ok.status_code == 200 and ok.content == FAKE_PDF
    assert bad.status_code == 404


def test_print_page_data_needs_a_valid_export_token(pdf_env: dict[str, Any]) -> None:
    """打印页取数口：令牌对 → 200（且**不带身份字段**）；令牌错 / 过期 → 404。"""
    report_id = pdf_env["report_id"]
    good = issue_export_token(report_id)

    app.dependency_overrides.clear()  # 渲染器没有会话，入口**不需要登录**
    try:
        ok = client.get(f"/api/v1/reports/{report_id}/print", params={"t": good})
        bad = client.get(f"/api/v1/reports/{report_id}/print", params={"t": "1.abc"})
        missing = client.get(f"/api/v1/reports/{report_id}/print")
    finally:
        app.dependency_overrides[reports_api.require_user] = lambda: dict(TEST_USER)

    assert ok.status_code == 200, ok.text
    assert "user_id" not in ok.json()
    assert bad.status_code == 404
    assert missing.status_code == 422  # 缺参数是请求错，不是「链接失效」


def test_render_failure_surfaces_a_readable_reason(
    pdf_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(report_id: str, **kwargs: Any) -> bytes:
        raise pdf_module.PdfRenderError("渲染超时（>120s）")

    monkeypatch.setattr(reports_api, "render_report_pdf", broken)
    response = client.get(f"/api/v1/reports/{pdf_env['report_id']}/pdf")

    assert response.status_code == 500
    assert "渲染超时" in response.json()["detail"]


def test_report_date_import_is_not_dead_code() -> None:
    """（护栏）`date` 在用例里被用于构造窗口——放一行显式断言，免得被当成未用导入清掉。"""
    assert isinstance(_days()[0], date) and ts("2026-08-03 09:00:00").year == 2026
