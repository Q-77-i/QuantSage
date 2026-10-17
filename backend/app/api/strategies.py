"""用户策略 CRUD、静态检查与模板库（M4c）。

三条口径，各有出处：

  * **保存不因 findings 被拒**：存的是草稿，findings 只是随响应告知（SPEC §5 M4c）。
    闸门在**回测提交前**（`POST /api/v1/backtest`），不在这里——否则用户连半成品都存不下。
  * **检查是纯函数、不落库、不执行代码**：`check_source` 从不抛异常，语法错也是一条 finding；
    `parse_meta` 静态读 `PARAMS` / `USES_EVENTS`。编辑器要的标注与参数表单**出自同一次响应**
    （`{findings, meta}`），两边不会互相对不上。
  * **归属一律 404**（越权与不存在同码，不泄露存在性），同 M1c 口径；异常到状态码的映射
    统一在 `main.py` 注册，这里不写 try/except。

**路由顺序要紧**：`/templates` 与 `/check` 必须声明在 `/{strategy_id}` **之前**——FastAPI 按
声明顺序匹配，否则 `templates` 会被当成 UUID 解析（422）。
"""

from __future__ import annotations

import difflib
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.auth import require_db, require_user
from app.strategy import StrategyRejected, code_sha256
from app.strategy.api import parse_meta
from app.strategy.static_check import check_source
from app.strategy.templates import TEMPLATES

router = APIRouter(prefix="/api/v1/strategies", tags=["strategies"])

#: 策略名上限。够用即可，真正的约束是「同一账号内唯一」（DB 的 `UNIQUE(user_id, name)`）
NAME_MAX = 60

#: 源码上限：一条策略塞爆 JSONB 没有意义，超了明确拒绝而不是让库去扛
CODE_MAX_BYTES = 64 * 1024


def clean_name(value: str) -> str:
    """策略名校验（Pydantic validator 里抛 ValueError → 422）。"""
    name = value.strip()
    if not name:
        raise ValueError("策略名不能为空")
    if len(name) > NAME_MAX:
        raise ValueError(f"策略名最长 {NAME_MAX} 个字符")
    if not name.isprintable():
        raise ValueError("策略名不能包含控制字符")
    return name


def clean_code(value: str) -> str:
    """源码校验：只要非空且不超限。**语法错不在这里挡**——它由检查器给行号（草稿可存）。"""
    if not value.strip():
        raise ValueError("策略代码不能为空")
    if len(value.encode("utf-8")) > CODE_MAX_BYTES:
        raise ValueError(f"策略代码最长 {CODE_MAX_BYTES // 1024}KB")
    return value


def normalize_strategy_id(raw: str) -> str:
    """非 UUID 直接 422：`%s::uuid` 会在驱动层抛 DataError，那是拿 500 报客户端错误。"""
    try:
        return str(uuid.UUID(raw))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="策略 id 必须是 UUID") from exc


class StrategyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    code: str
    params: dict[str, Any] = Field(default_factory=dict, max_length=50)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return clean_name(value)

    @field_validator("code")
    @classmethod
    def _code(cls, value: str) -> str:
        return clean_code(value)


class StrategyUpdate(BaseModel):
    """部分更新：三个字段都可缺省，缺省即「不动这一列」。

    `expected_version` 是乐观并发：编辑器打开时的版本若与库里不一致 → 409（5d）。
    不传则保持旧的 last-write-wins（向后兼容）。
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    code: str | None = None
    params: dict[str, Any] | None = Field(default=None, max_length=50)
    expected_version: int | None = Field(default=None, ge=1)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return clean_name(value) if value is not None else None

    @field_validator("code")
    @classmethod
    def _code(cls, value: str | None) -> str | None:
        return clean_code(value) if value is not None else None


class CheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str

    @field_validator("code")
    @classmethod
    def _code(cls, value: str) -> str:
        return clean_code(value)


class StrategyRollback(BaseModel):
    """回滚 = 把目标版本内容作为**新版本**追加（append-only）。"""

    model_config = ConfigDict(extra="forbid")

    version_no: int = Field(ge=1)
    expected_version: int | None = Field(default=None, ge=1)


def meta_of(code: str) -> dict[str, Any] | None:
    """`PARAMS` / `USES_EVENTS` 的 JSON 形态；解析失败返回 None（问题由 R4 findings 承载）。

    **不抛异常**：这里的调用方是编辑器，它需要的是「这次解析没成功」这个事实，
    而不是一个 500——真正的原因（`PARAMS` 不是字面量一类）已经作为 finding 给出去了。
    """
    try:
        meta = parse_meta(code)
    except StrategyRejected:
        return None
    return {
        "params": {
            name: {
                "type": spec.type,
                "default": spec.default,
                "min": spec.min,
                "max": spec.max,
                "label": spec.label,
            }
            for name, spec in meta.params.items()
        },
        "uses_events": meta.uses_events,
    }


def findings_of(code: str) -> list[dict[str, Any]]:
    return [finding.to_dict() for finding in check_source(code)]


def public_row(row: dict[str, Any]) -> dict[str, Any]:
    """策略行 → 写响应的形状：加 `code_sha256`（前端比对用）与 `findings`（草稿也存）。"""
    return {**row, "code_sha256": code_sha256(row["code"]), "findings": findings_of(row["code"])}


@router.get("")
async def list_strategies(
    request: Request, user: dict[str, Any] = Depends(require_user)
) -> list[dict[str, Any]]:
    """本人全部策略（摘要，不带 code），最近改的在前。"""
    return await require_db(request).list_strategies(int(user["id"]))


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_strategy(
    request: Request, body: StrategyCreate, user: dict[str, Any] = Depends(require_user)
) -> dict[str, Any]:
    """建策略。撞名 409（`main.py` 里映射）；**草稿也 201**，findings 随响应返回。"""
    row = await require_db(request).create_strategy(
        str(uuid.uuid4()), int(user["id"]), body.name, body.code, body.params
    )
    return public_row(row)


@router.get("/templates")
async def list_templates(user: dict[str, Any] = Depends(require_user)) -> list[dict[str, Any]]:
    """5 个模板（服务端为唯一真源）。`builtin` 非空即「有内置等价物」。"""
    return [
        {
            "key": template.key,
            "title": template.title,
            "summary": template.summary,
            "builtin": template.builtin,
            "source": template.source,
        }
        for template in TEMPLATES
    ]


@router.post("/check")
async def check_strategy(
    body: CheckRequest, user: dict[str, Any] = Depends(require_user)
) -> dict[str, Any]:
    """源码 → `{findings, meta}`。纯函数：不落库、不 spawn、不执行代码。"""
    return {"findings": findings_of(body.code), "meta": meta_of(body.code)}


@router.get("/{strategy_id}")
async def get_strategy(
    request: Request, strategy_id: str, user: dict[str, Any] = Depends(require_user)
) -> dict[str, Any]:
    """单条（含 code / params / **当前** `code_sha256`）。越权与不存在同返 404。"""
    row = await require_db(request).get_strategy(
        int(user["id"]), normalize_strategy_id(strategy_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="策略不存在")
    return {**row, "code_sha256": code_sha256(row["code"])}


@router.put("/{strategy_id}")
async def update_strategy(
    request: Request,
    body: StrategyUpdate,
    strategy_id: str,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """改策略（同样允许草稿）。策略名撞上本人另一条 → 409。"""
    row = await require_db(request).update_strategy(
        int(user["id"]),
        normalize_strategy_id(strategy_id),
        name=body.name,
        code=body.code,
        params=body.params,
        expected_version=body.expected_version,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="策略不存在")
    return public_row(row)


@router.get("/{strategy_id}/versions")
async def list_versions(
    request: Request, strategy_id: str, user: dict[str, Any] = Depends(require_user)
) -> list[dict[str, Any]]:
    """当前策略的版本摘要（新在前；`current` 标出 head）。越权/不存在 404。"""
    rows = await require_db(request).list_strategy_versions(
        int(user["id"]), normalize_strategy_id(strategy_id)
    )
    if rows is None:
        raise HTTPException(status_code=404, detail="策略不存在")
    return rows


@router.get("/{strategy_id}/versions/{version_no}")
async def get_version(
    request: Request,
    strategy_id: str,
    version_no: int,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """某一版的完整源码与参数（含 `code_sha256`，前端比对用）。"""
    row = await require_db(request).get_strategy_version(
        int(user["id"]), normalize_strategy_id(strategy_id), version_no
    )
    if row is None:
        raise HTTPException(status_code=404, detail="策略版本不存在")
    return {**row, "code_sha256": code_sha256(row["code"])}


@router.get("/{strategy_id}/versions/{version_no}/diff")
async def diff_version(
    request: Request,
    strategy_id: str,
    version_no: int,
    against: int | None = Query(default=None, ge=1),
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """统一 diff。缺省 `against` = 当前 head（比较「选中版本 → 当前」）。

    顺序固定为 `from=against`、`to=version_no`；`params_changed` 单独给（参数是 JSON，
    不塞进代码 diff 里假装成文本行）。
    """
    normalized = normalize_strategy_id(strategy_id)
    db = require_db(request)
    user_id = int(user["id"])
    target = await db.get_strategy_version(user_id, normalized, version_no)
    if target is None:
        raise HTTPException(status_code=404, detail="策略版本不存在")
    if against is None:
        current = await db.get_strategy(user_id, normalized)
        if current is None:
            raise HTTPException(status_code=404, detail="策略不存在")
        other = {
            "version_no": current["version"],
            "code": current["code"],
            "params": current["params"],
        }
    else:
        other = await db.get_strategy_version(user_id, normalized, against)
        if other is None:
            raise HTTPException(status_code=404, detail="对比版本不存在")

    diff = "\n".join(
        difflib.unified_diff(
            other["code"].splitlines(),
            target["code"].splitlines(),
            fromfile=f"v{other['version_no']}",
            tofile=f"v{target['version_no']}",
            lineterm="",
        )
    )
    return {
        "from": other["version_no"],
        "to": target["version_no"],
        "diff": diff,
        "params_changed": dict(other["params"] or {}) != dict(target["params"] or {}),
    }


@router.post("/{strategy_id}/rollback")
async def rollback_strategy(
    request: Request,
    strategy_id: str,
    body: StrategyRollback,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """把目标版本内容作为新版本追加；只回滚 code/params，保留当前名字。"""
    row = await require_db(request).rollback_strategy(
        int(user["id"]),
        normalize_strategy_id(strategy_id),
        body.version_no,
        expected_version=body.expected_version,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="策略或版本不存在")
    return public_row(row)


@router.delete("/{strategy_id}")
async def delete_strategy(
    request: Request, strategy_id: str, user: dict[str, Any] = Depends(require_user)
) -> dict[str, Any]:
    """删策略。**回测记录不级联删**：报告 JSONB 自足，`strategy_id` 悬空即可。"""
    normalized = normalize_strategy_id(strategy_id)
    if not await require_db(request).drop_strategy(int(user["id"]), normalized):
        raise HTTPException(status_code=404, detail="策略不存在")
    return {"id": normalized, "deleted": True}
