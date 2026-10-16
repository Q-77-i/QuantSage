"""admin 视图（E2b）：配额用量 + 拒绝记录。

**只读，不做管理后台**（PRD 写死）：没有用户增删改、没有改配额的表单——配额上限是**部署参数**
（进 settings），改角色走 `scripts/grant_admin.py`。这里只把库里的读数摆出来。

门是 `require_admin`（403，理由写在 `core/auth.py`）。审计这半边的内容就是 `quota_rejections`：
E2a 落地时顺带写下的，只有用量快照的话审计没有东西可看（SPEC §3.5）。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core import quota
from app.core.auth import require_admin, require_db
from app.core.config import get_settings

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

#: 时间窗的三个档（用户 2026-10-16 定：固定档而不是自定义起止日期）
BUCKETS = (1, 7, 30)


@router.get("/usage")
async def usage(
    _: Annotated[dict[str, Any], Depends(require_admin)],
    db: Any = Depends(require_db),
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    q: Annotated[str, Query(max_length=200)] = "",
    role: Annotated[str, Query(pattern="^(admin|user)?$")] = "",
    active_only: bool = False,
    # 刻意**不用** `Literal[1, 7, 30]`：Pydantic 的 Literal 不会把查询串 `"7"` 当成 7
    # （按类型比对 ⇒ 恒 422）。用 int 收窄取值范围 + 下面显式校验，顺带能给中文提示
    days: Annotated[int, Query()] = 1,
    sort: Annotated[
        Literal["id", "email", "active_tasks", "agent_calls", "rejected"],
        Query(),
    ] = "id",
    order: Annotated[Literal["asc", "desc"], Query()] = "asc",
) -> dict[str, Any]:
    """一页用户：在飞任务 / 当期 Agent 调用 / 当期被拒。上限放**顶层一份**。

    **服务端分页 + 搜索 + 两个筛选**（E2c，用户 2026-10-16 拍板）：视图的规模随用户数走，
    129 个账号只是当下。`total` 是**过滤后**的命中数，分页控件靠它算总页数。

    两个筛选：`role`（留空 = 全部；只收 `admin` / `user`，别的值 422）与
    `active_only`（只看三读数任一非零的账号——134 个账号里绝大多数是历史探针，一屏全 0）。

    排序 `sort` / `order` 走**白名单**（`Literal` 挡在 422；`ORDER BY` 拼的是标识符、
    参数占位符在那儿没用）。分页与排序**必须同侧**：只排当前页等于没排——所以排序在 SQL 里。

    **时间窗 `days`（1 / 7 / 30，默认 1 = 今日）**：两个计数列（`agent_calls` / `rejected`）
    是区间量，跟随窗口；**`active_tasks` 是 gauge，恒为此刻**——「近 7 天的在飞任务」这个词
    本身不成立（SPEC §3.5 写明，界面上也标了）。
    """
    if days not in BUCKETS:
        raise HTTPException(status_code=422, detail=f"days 只接受 {' / '.join(map(str, BUCKETS))}")
    settings = get_settings()
    # 时间窗（E2c 追加）：两个计数列跟随它；`active_tasks` 是 gauge、**不随窗口变**
    period_from, period_to, since = quota.window(days)
    rows = await db.admin_usage(
        period_from,
        period_to,
        since,
        limit=limit,
        offset=offset,
        q=q,
        role=role,
        active_only=active_only,
        sort=sort,
        order=order,
    )
    return {
        "limits": {
            "active_tasks": settings.quota_active_tasks,
            "agent_calls_per_day": settings.quota_agent_calls_per_day,
        },
        "total": await db.admin_usage_total(
            period_from,
            period_to,
            since,
            q=q,
            role=role,
            active_only=active_only,
        ),
        "offset": offset,
        "limit": limit,
        "users": rows,
    }


@router.get("/rejections")
async def rejections(
    _: Annotated[dict[str, Any], Depends(require_admin)],
    db: Any = Depends(require_db),
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """一页拒绝事件（新的在前）——「超配额被拒」的可查证据。

    **同样分页**：拒绝是**事件**、只增不减，涨得比账号快。不做搜索——它是日志，
    按时间倒序扫即可（要按人查时用 `/usage` 那一列）。
    """
    return {
        "total": await db.quota_rejections_total(),
        "offset": offset,
        "limit": limit,
        "rejections": await db.quota_rejections(limit, offset),
    }
