"""绩效研报 API（M7a）：生成 / 查看 / 分享 / 公开只读 / Markdown 导出。

业务全在 `app.report`（纯函数与组装），这里只做取数、编排与形状转换——同 `api/paper.py`
的分工。三条把关写在端点之前：

1. **归属**：报告与账户都带 `user_id` 查，越权与不存在同为 404（M1 口径）；
2. **幂等复用**：同 `(account_id, snapshot_hash)` 已有报告即原样返回，不新建行——
   分享链接因此永远同一份，`report_hash` 也因此可复现（综述复用已存文本）；
3. **公开只读**只经 token，路径前缀 `/api/v1/public/reports` 与受保护端点**在路径上就分开**，
   响应不含任何用户身份字段。

快照（逐分片 sha256，实测约 2.6s）与基准计算都走 `asyncio.to_thread`——真 IO + CPU，
不能在事件循环里直接跑（P1 立下的规矩）。快照就是查重的键，生成路径上总要算一次；
而**调 LLM 在查重之后**：同一份数据第二次点「生成研报」不再花钱。
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from app.backtest.benchmark import market_benchmark
from app.core.auth import require_db, require_user
from app.data import duckdb_client as dc
from app.memory.decision_store import DecisionMemory, MemoryUnavailable, memory_for
from app.memory.scheduler import next_run_time
from app.memory.service import settle_account
from app.memory.settle import mark_open_trips, pair_trips
from app.paper.store import config_from_payload, decision_from_row, equity_from_row
from app.paper.types import Decision, PaperConfig
from app.report.builder import (
    ClaimError,
    assemble_report,
    build_facts,
    equity_dates,
    freeze_review,
    validate_claims,
)
from app.report.evidence import resolve_evidence
from app.report.markdown import render_markdown
from app.report.narrative import build_narrative
from app.report.pdf import PdfRenderError, issue_export_token, render_report_pdf, verify_export_token
from app.report.snapshot import (
    config_digest,
    data_snapshot,
    decisions_digest,
    report_hash,
    snapshot_hash,
)

router = APIRouter(prefix="/api/v1", tags=["reports"])

#: 决策流水上限。池子 ≤20、区间 ≤250 个交易日 ⇒ 最多约 1000 张单；这个上限是防呆，不是分页。
#: 注意 `paper_decisions` 是**从新到旧**截断的：取不满才说明真取全了，故上限远大于理论上限。
DECISION_LIMIT = 5000

#: 分享 token：`token_urlsafe(24)` ≈ 32 字符、192 位熵——不可猜
SHARE_TOKEN_BYTES = 24


async def require_memory(request: Request) -> DecisionMemory:
    """决策记忆出口：没起来（库不可用 / 降级启动）时 503——与 `require_db` 同姿态。"""
    try:
        return await memory_for(request.app.state)
    except MemoryUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"决策记忆不可用（{exc}）") from exc


class ReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: uuid.UUID


# ── 形状转换 ────────────────────────────────────────────────


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _row_payload(row: dict[str, Any], *, reused: bool = False) -> dict[str, Any]:
    token = row.get("share_token")
    return {
        "id": str(row["id"]),
        "created_at": _iso(row.get("created_at")),
        "report": row["report"],
        "report_hash": row["report_hash"],
        "snapshot_hash": row["snapshot_hash"],
        "share_token": token,
        # URL 由前端按当前 origin 拼（`/r/<token>`）——部署换域名不用改后端配置
        "share_path": f"/r/{token}" if token else None,
        "reused": reused,
    }


def _public_payload(row: dict[str, Any]) -> dict[str, Any]:
    """匿名只读响应：**只有冻结产物本身**，不带 user_id / account_id / 任何身份字段。"""
    return {
        "id": str(row["id"]),
        "report": row["report"],
        "report_hash": row["report_hash"],
        "snapshot_hash": row["snapshot_hash"],
        "created_at": _iso(row.get("created_at")),
        "shared_at": _iso(row.get("shared_at")),
    }


# ── 组装 ────────────────────────────────────────────────────


def _snapshot_for(
    row: dict[str, Any], config: PaperConfig, decisions: list[Decision]
) -> dict[str, Any]:
    """数据指纹 + 决策日志指纹 + 账户配置指纹（线程里跑：逐分片 sha256 是真 IO）。"""
    snapshot = data_snapshot()
    snapshot["decisions_hash"] = decisions_digest(decisions)
    snapshot["account"] = {
        "id": str(row["id"]),
        "as_of": row["as_of"].isoformat(),
        "config_hash": config_digest(config),
    }
    return snapshot


async def _facts_for(
    *,
    row: dict[str, Any],
    config: PaperConfig,
    decisions: list[Decision],
    equity: list[dict[str, Any]],
    snapshot: dict[str, Any],
    review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """取齐剩余输入并组装事实层（不含 LLM）。"""
    closes = await asyncio.to_thread(dc.closes_through, list(config.symbols), row["as_of"])
    trips = mark_open_trips(pair_trips(decisions), closes)
    dates = equity_dates(equity)
    benchmark = (
        await asyncio.to_thread(market_benchmark, dates, float(config.initial_cash))
        if dates
        else None
    )
    sources = [d.sources if d.sources else None for d in decisions]
    evidence = await asyncio.to_thread(
        resolve_evidence, sources, decision_ids=[d.id for d in decisions]
    )
    return build_facts(
        account=dict(row),
        config=config,
        decisions=decisions,
        equity_rows=equity,
        trips=trips,
        benchmark=benchmark,
        snapshot=snapshot,
        evidence=evidence,
        review=review,
    )


def _narrative_brief(facts: dict[str, Any]) -> dict[str, Any]:
    """交给综述的事实摘要（**只给事实**：模型不许算、不许编）。"""
    account = facts["account"]
    attribution = facts["attribution"]
    return {
        "account_name": account.get("name"),
        "strategy_name": account.get("strategy_name") or account.get("strategy"),
        "start": account.get("start"),
        "as_of": account.get("as_of"),
        "market_end": account.get("data_end"),
        "metrics": facts["metrics"],
        "open_trips": sum(row["trips"] - row["closed"] for row in attribution["symbols"]),
        "direction_groups": attribution["direction"],
        "industry_groups": attribution["industry"],
        "notes": facts["warnings"],
    }


async def _load_account(
    db: Any, user_id: int, account_id: str
) -> tuple[dict[str, Any], PaperConfig, list[Decision], list[dict[str, Any]]]:
    row = await db.get_paper_account(user_id, account_id)
    if row is None:
        raise HTTPException(status_code=404, detail="模拟盘会话不存在")
    config = config_from_payload(row["config"])
    decisions = [
        decision_from_row(item) for item in await db.paper_decisions(account_id, DECISION_LIMIT)
    ]
    equity = [equity_from_row(item) for item in await db.paper_equity(account_id)]
    return row, config, decisions, equity


# ── 端点 ────────────────────────────────────────────────────


@router.post("/reports", status_code=201)
async def create_report(
    payload: ReportRequest,
    request: Request,
    user: dict[str, Any] = Depends(require_user),
    db: Any = Depends(require_db),
) -> dict[str, Any]:
    """生成并冻结一份绩效研报（同 `(账户, 快照)` 幂等复用；不存在则 201 新建）。

    生成前先做一次**惰性结算**（M7b）：新到期的回合才调模型，已结算的直接复用记忆——
    所以「同一份数据第二次点生成」既不重算分片、也不花钱。记忆不可用时（降级启动）
    报告照常出，只是没有逐笔复盘块。
    """
    account_id = str(payload.account_id)
    row, config, decisions, equity = await _load_account(db, user["id"], account_id)

    review: dict[str, Any] | None = None
    try:
        memory = await memory_for(request.app.state)
    except MemoryUnavailable:
        memory = None  # 记忆不可用：报告照常出，只是没有逐笔复盘块
    if memory is not None:
        review, _run = await settle_account(
            db, memory, user_id=user["id"], account_id=account_id
        )

    snapshot = await asyncio.to_thread(_snapshot_for, row, config, decisions)
    if review is not None:
        # 复盘内容进快照身份：结算变了（新到期 / 新教训）就该出一份**新的**报告，
        # 而不是复用旧的那份没复盘的
        snapshot["review_hash"] = report_hash(freeze_review(review))
    digest = snapshot_hash(snapshot)
    existing = await db.find_research_report(user["id"], account_id, digest)
    if existing is not None:
        return _row_payload(existing, reused=True)

    facts = await _facts_for(
        row=row, config=config, decisions=decisions, equity=equity, snapshot=snapshot,
        review=review,
    )
    body = assemble_report(facts, await build_narrative(_narrative_brief(facts)))
    try:
        validate_claims(body)
    except ClaimError as exc:  # 闸门不过：拒收，不落库（自证不过的报告不该存在）
        raise HTTPException(status_code=500, detail=f"报告未通过 claim 闸门：{exc}") from exc

    created = await db.create_research_report(
        report_id=str(uuid.uuid4()),
        user_id=user["id"],
        account_id=account_id,
        snapshot=snapshot,
        snapshot_hash=digest,
        report=body,
        report_hash=report_hash(body),
    )
    return _row_payload(
        {
            "id": created["id"],
            "created_at": created["created_at"],
            "report": body,
            "report_hash": report_hash(body),
            "snapshot_hash": digest,
            "share_token": None,
        },
        reused=False,
    )


@router.get("/reports")
async def list_reports(
    account_id: uuid.UUID = Query(...),
    user: dict[str, Any] = Depends(require_user),
    db: Any = Depends(require_db),
) -> dict[str, Any]:
    """某账户已出的报告摘要（不含正文）——`/paper` 页的入口状态用它。"""
    account = await db.get_paper_account(user["id"], str(account_id))
    if account is None:
        raise HTTPException(status_code=404, detail="模拟盘会话不存在")
    rows = await db.list_research_reports(user["id"], str(account_id))
    return {
        "reports": [
            {
                "id": str(item["id"]),
                "created_at": _iso(item["created_at"]),
                "snapshot_hash": item["snapshot_hash"],
                "report_hash": item["report_hash"],
                "share_token": item.get("share_token"),
                "share_path": f"/r/{item['share_token']}" if item.get("share_token") else None,
            }
            for item in rows
        ]
    }


@router.get("/reports/{report_id}")
async def get_report(
    report_id: uuid.UUID,
    user: dict[str, Any] = Depends(require_user),
    db: Any = Depends(require_db),
) -> dict[str, Any]:
    row = await db.get_research_report(user["id"], str(report_id))
    if row is None:
        raise HTTPException(status_code=404, detail="研报不存在")
    return _row_payload(row)


@router.post("/reports/{report_id}/share")
async def share_report(
    report_id: uuid.UUID,
    user: dict[str, Any] = Depends(require_user),
    db: Any = Depends(require_db),
) -> dict[str, Any]:
    """生成（或取回）分享链接。幂等：已分享过就原样返回同一个 token。"""
    row = await db.get_research_report(user["id"], str(report_id))
    if row is None:
        raise HTTPException(status_code=404, detail="研报不存在")
    token = row.get("share_token") or secrets.token_urlsafe(SHARE_TOKEN_BYTES)
    updated = await db.set_report_share(str(report_id), user["id"], token=token)
    if updated is None:  # 竞态：查询后被撤或删
        raise HTTPException(status_code=404, detail="研报不存在")
    return {
        "id": str(report_id),
        "share_token": token,
        "share_path": f"/r/{token}",
        "shared_at": _iso(updated.get("shared_at")),
    }


@router.delete("/reports/{report_id}/share")
async def unshare_report(
    report_id: uuid.UUID,
    user: dict[str, Any] = Depends(require_user),
    db: Any = Depends(require_db),
) -> dict[str, Any]:
    """撤销分享：token 置空，公开端点随即 404（旧链接失效）。"""
    updated = await db.set_report_share(str(report_id), user["id"], token=None)
    if updated is None:
        raise HTTPException(status_code=404, detail="研报不存在")
    return {"id": str(report_id), "share_token": None, "share_path": None, "shared_at": None}


@router.get("/reports/{report_id}/markdown")
async def report_markdown(
    report_id: uuid.UUID,
    user: dict[str, Any] = Depends(require_user),
    db: Any = Depends(require_db),
) -> Response:
    row = await db.get_research_report(user["id"], str(report_id))
    if row is None:
        raise HTTPException(status_code=404, detail="研报不存在")
    return _markdown_response(row)


@router.get("/reports/{report_id}/print")
async def report_for_print(report_id: uuid.UUID, t: str = Query(...), db: Any = Depends(require_db)) -> dict[str, Any]:
    """**打印页的取数口**（M7d）：凭一次性导出令牌读冻结产物。

    渲染器（无会话 cookie 的浏览器）走的就是这条路；令牌由后端签发、默认 10 分钟过期、
    签名绑死 report_id——它不改变分享状态，也不需要登录。
    """
    if not verify_export_token(str(report_id), t):
        raise HTTPException(status_code=404, detail="导出链接无效或已过期")
    row = await db.get_report_by_id(str(report_id))
    if row is None:
        raise HTTPException(status_code=404, detail="研报不存在")
    return _public_payload(row)


@router.get("/reports/{report_id}/pdf")
async def report_pdf(
    report_id: uuid.UUID,
    user: dict[str, Any] = Depends(require_user),
    db: Any = Depends(require_db),
) -> Response:
    """**一键导出 PDF**（服务端渲染，与屏幕同款）。越权与不存在同为 404。"""
    row = await db.get_research_report(user["id"], str(report_id))
    if row is None:
        raise HTTPException(status_code=404, detail="研报不存在")
    return await _pdf_response(
        str(report_id),
        (row["report"].get("account") or {}).get("name"),
        str(row.get("report_hash") or ""),
    )


@router.get("/public/reports/{token}/pdf")
async def public_report_pdf(token: str, db: Any = Depends(require_db)) -> Response:
    """**匿名**导出：分享链接里的「导出 PDF」走这条（凭 share token）。"""
    row = await db.get_report_by_token(token)
    if row is None:
        raise HTTPException(status_code=404, detail="分享链接不存在或已撤销")
    return await _pdf_response(
        str(row["id"]),
        (row["report"].get("account") or {}).get("name"),
        str(row.get("report_hash") or ""),
    )


async def _pdf_response(
    report_id: str, account_name: str | None, report_hash: str
) -> Response:
    try:
        content = await render_report_pdf(report_id)
    except PdfRenderError as exc:
        raise HTTPException(status_code=500, detail=f"PDF 渲染失败：{exc}") from exc
    name = _safe_name(account_name or "")
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": _disposition(name, report_hash, suffix=".pdf")},
    )


@router.get("/public/reports/{token}")
async def public_report(token: str, db: Any = Depends(require_db)) -> dict[str, Any]:
    """**匿名只读**：凭分享 token 取冻结产物（撤销即 404）。"""
    row = await db.get_report_by_token(token)
    if row is None:
        raise HTTPException(status_code=404, detail="分享链接不存在或已撤销")
    return _public_payload(row)


# ── 决策记忆与到期结算（M7b）────────────────────────────────


@router.get("/accounts/{account_id}/review")
async def account_review(
    account_id: uuid.UUID,
    request: Request,
    user: dict[str, Any] = Depends(require_user),
    db: Any = Depends(require_db),
    memory: DecisionMemory = Depends(require_memory),
) -> dict[str, Any]:
    """逐笔复盘：已到期的回合（结算 + 一句话教训）+ 未到期 + 定了没交易的。

    **惰性结算**：新到期的回合才调模型，已结算的直接复用记忆——同一个账户连看两次，
    第二次一分钱不花。
    """
    try:
        review, _run = await settle_account(
            db, memory, user_id=user["id"], account_id=str(account_id)
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="模拟盘会话不存在") from exc
    return review


@router.post("/accounts/{account_id}/settle")
async def settle_now(
    account_id: uuid.UUID,
    request: Request,
    user: dict[str, Any] = Depends(require_user),
    db: Any = Depends(require_db),
    memory: DecisionMemory = Depends(require_memory),
) -> dict[str, Any]:
    """手动触发结算（幂等；与定时 job 走**同一个函数**）。返回本次读数与下次定时时刻。"""
    try:
        _review, run = await settle_account(
            db, memory, user_id=user["id"], account_id=str(account_id)
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="模拟盘会话不存在") from exc
    return {
        "account_id": str(account_id),
        **run.to_payload(),
        "next_run": next_run_time(getattr(request.app.state, "settle_scheduler", None)),
    }


@router.get("/lessons")
async def lessons(
    request: Request,
    symbol: str | None = Query(None),
    direction: Literal["bullish", "bearish", "neutral"] | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    user: dict[str, Any] = Depends(require_user),
    memory: DecisionMemory = Depends(require_memory),
) -> dict[str, Any]:
    """跨账户 / 跨标的的教训聚合（只收**有反思文本**的已平仓回合）。"""
    rows = await memory.lessons(
        user_id=user["id"], symbol=symbol, direction=direction, limit=limit
    )
    return {
        "lessons": rows,
        "symbols": list(await memory.symbols(user_id=user["id"])),
        "filters": {"symbol": symbol, "direction": direction, "limit": limit},
    }


@router.get("/public/reports/{token}/markdown")
async def public_report_markdown(token: str, db: Any = Depends(require_db)) -> Response:
    row = await db.get_report_by_token(token)
    if row is None:
        raise HTTPException(status_code=404, detail="分享链接不存在或已撤销")
    return _markdown_response(row)


def _markdown_response(row: dict[str, Any]) -> Response:
    body = render_markdown(row["report"], report_hash_value=row.get("report_hash"))
    name = (row["report"].get("account") or {}).get("name") or ""
    return Response(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": _disposition(name, str(row.get("report_hash") or ""), suffix=".md")
        },
    )


def _disposition(name: str, report_hash: str, *, suffix: str) -> str:
    """附件名 = **`{账户名}-{报告指纹前 8 位}{后缀}`**，两段编码都给。

    三条要点（前两条是用户实测反馈，第三条是 starlette 的硬约束）：

    1. **名字必须能区分开**：一个账户会有很多份研报（不同快照各一份），只叫账户名必然重名；
       带上 `report_hash` 前 8 位，同一份报告每次导出同名、不同报告天然不同名；
    2. **ASCII 兜底也要有意义且唯一**：全中文的账户名过去退化成 `report`——那是句废话。
       没有 ASCII 字符时兜底 `quantsage`，指纹照样带上（`quantsage-31c1ae55.pdf`）；
    3. HTTP 头是 latin-1 ⇒ 中文只能进 `filename*`（RFC 5987），塞进 `filename` 会让
       starlette 在编码头时就抛。**这一段用双引号写 f-string**：`f'…UTF-8''{x}'` 会被 Python
       当成相邻字面量的**隐式拼接**（第二段没有 `f` 前缀 ⇒ 花括号原样输出），实测踩过一次。
    """
    from urllib.parse import quote

    stem = _safe_name(name) or "quantsage"
    short = (report_hash or "")[:8] or "unknown"
    filename = f"{stem}-{short}{suffix}"
    ascii_stem = "".join(ch for ch in stem if ch.isascii() and ch.isalnum()) or "quantsage"
    ascii_name = f"{ascii_stem}-{short}{suffix}"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def _safe_name(name: str) -> str:
    """文件名只留安全字符（中文保留，路径分隔符与控制字符去掉）。

    **空了就返回空**，由 `_disposition` 统一给兜底名（原先这里兜 "report"，于是
    「全中文 + 无 hash」会拼出 `report-unknown.pdf`——名字里带 "report" 正是用户嫌的那件事）。
    """
    return "".join(
        ch for ch in str(name) if ch.isprintable() and ch not in '/\\:*?"<>|'
    )[:60]
