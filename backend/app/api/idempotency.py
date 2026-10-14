"""幂等键（E4b / L7）：一次用户动作一个 key，重试复用同一个。

三条判据：

  * **同 key 同 body** → 回既有那个任务（202 + 同一个 `task_id`，`status` 取**行里的真值**
    ——重放一个已经跑完的任务却回 `queued` 就是在撒谎）；
  * **同 key 异 body** → **409**。宁可吵一声，也不静默跑错的那一份；
  * **没给 key** → 照旧每次新建。幂等是**可选的**：不给键的调用方（脚本、curl）
    要的就是「每次都跑」。

**顺序**（SPEC §5.5）：短路在**闸门之前**——重复提交不该再走一遍静态检查与窗口解析
（后者要真读一次行情）。所以指纹取的是**原始请求体**而不是落库后的 `request`：
后者含 `resolve_window` 解析出的区间，拿它算指纹就得先跑闸门，正好把要省的那步赔回去。

指纹落在 `request["_fingerprint"]`：下划线开头 = 内部字段，与 `_EXECUTION_ONLY`
同一约定（落 `optimization_runs` 时会被剔掉，不进用户可见的那一份）。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import Header, HTTPException
from pydantic import BaseModel

#: 头名。**由前端生成**：同一次用户动作重试复用同一个，新动作换一个（前端 `lib/idempotency.ts`）
HEADER_ALIAS = "Idempotency-Key"

#: 键长上限：它进唯一索引，不设上限等于让调用方往索引里塞任意长的东西
KEY_MAX = 128

FINGERPRINT_KEY = "_fingerprint"


class IdempotencyConflict(Exception):
    """同一个 key 换了另一份请求体——是误用，不是重试。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)


def idempotency_key(
    x_idempotency_key: str | None = Header(default=None, alias=HEADER_ALIAS),
) -> str | None:
    """把请求头翻成规范化后的键（空串等于没给；超长 422）。"""
    if x_idempotency_key is None:
        return None
    key = x_idempotency_key.strip()
    if not key:
        return None
    if len(key) > KEY_MAX:
        raise HTTPException(status_code=422, detail=f"{HEADER_ALIAS} 不能超过 {KEY_MAX} 个字符")
    return key


def body_fingerprint(body: BaseModel) -> str:
    """请求体的指纹。`sort_keys` 保证同内容不同键序算出同一个值。"""
    payload = json.dumps(body.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _task_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {"task_id": str(row["id"]), "status": str(row["status"])}


def _replay(row: dict[str, Any], fingerprint: str) -> dict[str, Any]:
    stored = (row.get("request") or {}).get(FINGERPRINT_KEY)
    if stored is not None and stored != fingerprint:
        raise IdempotencyConflict(
            "同一个 Idempotency-Key 提交了不同的请求体——换一个键，或让请求体与首次一致"
        )
    return _task_payload(row)


async def replay(
    db: Any, user_id: int, key: str | None, fingerprint: str
) -> dict[str, Any] | None:
    """闸门**之前**的短路。返回既有任务（重放），或 None（继续走闸门）。"""
    if key is None:
        return None
    row = await db.task_by_idempotency_key(user_id, key)
    if row is None:
        return None
    return _replay(row, fingerprint)


async def create_task(
    db: Any,
    *,
    task_id: str,
    user_id: int,
    kind: str,
    request: dict[str, Any],
    key: str | None,
    fingerprint: str,
    total: int = 1,
) -> dict[str, Any]:
    """建任务行（带幂等语义），返回 `{task_id, status}`。

    冲突判定交给唯一索引（见 `db.create_task`），这里只管两件事：把指纹写进 `request`，
    以及**撞上了就是重放**——两次调用之间的并发窗口由索引兜住，不靠「先查后插」。
    """
    if key is not None:
        request = {**request, FINGERPRINT_KEY: fingerprint}
    row = await db.create_task(
        task_id, user_id, kind, request, total=total, idempotency_key=key
    )
    if str(row["id"]) == task_id:
        return {"task_id": task_id, "status": "queued"}
    return _replay(row, fingerprint)
