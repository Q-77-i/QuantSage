"""admin 视图（E2b）：配额用量 + 拒绝记录。

**只读，不做管理后台**（PRD 写死）：没有用户增删改、没有改配额的表单——配额上限是**部署参数**
（进 settings），改角色走 `scripts/grant_admin.py`。这里只把库里的读数摆出来。

门是 `require_admin`（403，理由写在 `core/auth.py`）。审计这半边的内容就是 `quota_rejections`：
E2a 落地时顺带写下的，只有用量快照的话审计没有东西可看（SPEC §3.5）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from app.core import quota
from app.core.auth import require_admin, require_db
from app.core.config import get_settings

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.get("/usage")
async def usage(
    _: Annotated[dict[str, Any], Depends(require_admin)],
    db: Any = Depends(require_db),
) -> dict[str, Any]:
    """每用户一行：在飞任务 / 当期 Agent 调用 / 当期被拒。上限放**顶层一份**。

    上限是全局配置，按用户重复一遍只是噪音（实施期收敛，SPEC §3.5 已改）。
    """
    settings = get_settings()
    rows = await db.admin_usage(quota.period_key(), quota.day_start())
    return {
        "limits": {
            "active_tasks": settings.quota_active_tasks,
            "agent_calls_per_day": settings.quota_agent_calls_per_day,
        },
        "users": rows,
    }


@router.get("/rejections")
async def rejections(
    _: Annotated[dict[str, Any], Depends(require_admin)],
    db: Any = Depends(require_db),
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """最近的拒绝事件（新的在前）——「超配额被拒」的可查证据。"""
    return {"rejections": await db.quota_rejections(limit)}
