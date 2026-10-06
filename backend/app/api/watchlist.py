"""自选股 API（M1c）。

三条口径，各有出处：

  * **分组是 `group_name` 字符串列，没有分组实体表** ⇒ 不存在「空分组」，分组视图（哪些组、
    组内顺序）由前端从扁平列表聚合；重命名 / 删除各是一条 UPDATE。也因此
    `GET /api/v1/watchlist/groups` 这类单段路由会与 `/watchlist/{symbol}` 相撞，不设。
  * **价格取不到就留空**：样例数据只覆盖少数标的，`added_price` / `latest_close` 为 null、
    涨幅为 null，前端显示「—」，不得编数。
  * **行情层整体不可用不 503**：价格字段全 null 并打 warning。自选股是用户资产，不该被
    行情依赖拖死；只有业务库不可用才 503（由 `require_user` 保证）。

响应里的价格是**当场算的**（加入时价存库、最新价每次查），不落派生列——行情快照冻结在
`data/`，重算成本可忽略，而缓存会多一处需要失效的状态。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.auth import require_db, require_user
from app.core.db import DEFAULT_GROUP
from app.data import duckdb_client as dc
from app.data.duckdb_client import DataNotReady

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/watchlist", tags=["watchlist"])

SYMBOL_PATTERN = r"^\d{6}$"

#: 分组名的上限。够用即可，真正的约束是「不含 /」——见 `clean_group_name`
GROUP_NAME_MAX = 24


def clean_group_name(value: str) -> str:
    """写入侧的分组名校验（Pydantic validator 里抛 ValueError → 422）。

    含 `/` 的名字**在路由层就不可达**：`%2F` 解码后变成路径分隔符，请求会落到别的路由上
    直接 404。与其让用户拿到一个看不懂的 404，不如在能校验的地方明确拒绝。
    （路由里的组名不做这层校验：查不到自然 404，与「命中 0 行 → 404」同规。）
    """
    name = value.strip()
    if not name:
        raise ValueError("分组名不能为空")
    if len(name) > GROUP_NAME_MAX:
        raise ValueError(f"分组名最长 {GROUP_NAME_MAX} 个字符")
    if not name.isprintable() or "/" in name:
        raise ValueError("分组名不能包含「/」或控制字符")
    return name


class WatchlistAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(pattern=SYMBOL_PATTERN)
    group_name: str = Field(default=DEFAULT_GROUP)

    @field_validator("group_name")
    @classmethod
    def _group(cls, value: str) -> str:
        return clean_group_name(value)


class WatchlistMove(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_name: str

    @field_validator("group_name")
    @classmethod
    def _group(cls, value: str) -> str:
        return clean_group_name(value)


class GroupRename(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return clean_group_name(value)


def reject_default_group(name: str, *, action: str) -> None:
    """「默认分组」是回落目标：改名或删掉它，回落目标就消失了（且与建表的默认值脱节）。"""
    if name == DEFAULT_GROUP:
        raise HTTPException(status_code=400, detail=f"「{DEFAULT_GROUP}」不可{action}")


async def _latest_prices(symbols: list[str]) -> dict[str, dict]:
    """行情层取价。整层不可用（数据未落盘）**不抛**，返回空表让价格降级为「—」。

    DuckDB 是同步 IO，必须卸载到线程；这批标的共用一次连接。
    """
    if not symbols:
        return {}
    try:
        return await asyncio.to_thread(dc.latest_closes, symbols)
    except DataNotReady as exc:
        log.warning("行情层不可用，自选股价格降级为「—」：%s", exc)
        return {}


def _change_pct(added: float | None, close: float | None) -> float | None:
    """加自选以来涨幅。任一缺（没取到价）或分母非正 → None，不编数。"""
    if added is None or close is None or added <= 0:
        return None
    return (close - added) / added


def _item(row: dict[str, Any], latest: dict | None) -> dict[str, Any]:
    added = row["added_price"]
    close = latest["close"] if latest else None
    return {
        "symbol": row["symbol"],
        "group_name": row["group_name"],
        "added_at": row["added_at"],
        "added_price": added,
        "latest_close": close,
        "latest_trade_date": latest["trade_date"] if latest else None,
        "change_pct": _change_pct(added, close),
    }


@router.get("")
async def list_watchlist(
    request: Request, user: dict[str, Any] = Depends(require_user)
) -> list[dict[str, Any]]:
    """本人全部自选。扁平数组：分组是前端的事，这里只给事实。"""
    rows = await require_db(request).list_watchlist(int(user["id"]))
    prices = await _latest_prices([row["symbol"] for row in rows])
    return [_item(row, prices.get(row["symbol"])) for row in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_watchlist_item(
    request: Request, body: WatchlistAdd, user: dict[str, Any] = Depends(require_user)
) -> dict[str, Any]:
    """加自选。已在自选里 → 409（`SymbolTracked`，由 `main.py` 统一映射）。

    `added_price` 记的是**加入时**最近可得收盘价——取不到就记 NULL，之后不再补：
    补的话「加自选以来涨幅」的起点会随行情前移，那个数就没有意义了。
    """
    latest = (await _latest_prices([body.symbol])).get(body.symbol)
    row = await require_db(request).add_watchlist_item(
        int(user["id"]), body.symbol, body.group_name, latest["close"] if latest else None
    )
    return _item(row, latest)


@router.patch("/{symbol}")
async def move_watchlist_item(
    request: Request,
    body: WatchlistMove,
    symbol: Annotated[str, Path(pattern=SYMBOL_PATTERN)],
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """改分组。改的是归属，价格不受影响，故只回号与新组名（前端就地替换即可）。"""
    moved = await require_db(request).move_watchlist_item(
        int(user["id"]), symbol, body.group_name
    )
    if not moved:
        raise HTTPException(status_code=404, detail="该标的不在自选股中")
    return {"symbol": symbol, "group_name": body.group_name}


@router.delete("/{symbol}")
async def drop_watchlist_item(
    request: Request,
    symbol: Annotated[str, Path(pattern=SYMBOL_PATTERN)],
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    removed = await require_db(request).drop_watchlist_item(int(user["id"]), symbol)
    if not removed:
        raise HTTPException(status_code=404, detail="该标的不在自选股中")
    return {"symbol": symbol, "deleted": True}


@router.patch("/groups/{name}")
async def rename_group(
    request: Request,
    body: GroupRename,
    name: str,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """重命名分组。目标名已存在时自然合并（一条 UPDATE 的语义，不额外设 409）。"""
    reject_default_group(name, action="重命名")
    renamed = await require_db(request).rename_watchlist_group(
        int(user["id"]), name, body.name
    )
    if not renamed:
        raise HTTPException(status_code=404, detail="分组不存在")
    return {"group_name": body.name, "renamed_from": name}


@router.delete("/groups/{name}")
async def drop_group(
    request: Request, name: str, user: dict[str, Any] = Depends(require_user)
) -> dict[str, Any]:
    """删除分组：组内标的回落「默认分组」，不删标的。"""
    reject_default_group(name, action="删除")
    dropped = await require_db(request).drop_watchlist_group(int(user["id"]), name)
    if not dropped:
        raise HTTPException(status_code=404, detail="分组不存在")
    return {"group_name": name, "deleted": True, "fallback_group": DEFAULT_GROUP}
