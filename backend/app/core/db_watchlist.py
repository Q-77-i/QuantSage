"""自选股与分组（M1c）。"""

from __future__ import annotations

from typing import Any

from psycopg import errors

from app.core.db_errors import SymbolTracked
from app.core.schema import DEFAULT_GROUP


class WatchlistMixin:
    # ── 自选股 ──────────────────────────────────────────────

    async def list_watchlist(self, user_id: int) -> list[dict[str, Any]]:
        """本人全部自选，按（分组, 加入时间）升序。分组视图由上层聚合，这里只给扁平事实。"""
        rows = await self._all(
            "SELECT symbol, group_name, added_at, added_price FROM watchlist "
            "WHERE user_id = %s ORDER BY group_name, added_at",
            (user_id,),
        )
        return [_watchlist_row(row) for row in rows]

    async def add_watchlist_item(
        self, user_id: int, symbol: str, group_name: str, added_price: float | None
    ) -> dict[str, Any]:
        """加自选；已在自选里抛 `SymbolTracked`（不返回 None，避免调用方漏判）。

        `added_price` 允许为 None —— 样例数据只覆盖少数标的，取不到价就如实留空。
        """
        try:
            row = await self._one(
                "INSERT INTO watchlist (user_id, symbol, group_name, added_price) "
                "VALUES (%s, %s, %s, %s) "
                "RETURNING symbol, group_name, added_at, added_price",
                (user_id, symbol, group_name, added_price),
            )
        except errors.UniqueViolation as exc:
            raise SymbolTracked(symbol) from exc
        assert row is not None  # INSERT ... RETURNING 必然有行
        return _watchlist_row(row)

    async def move_watchlist_item(self, user_id: int, symbol: str, group_name: str) -> bool:
        """改分组。返回是否命中（False 由调用方翻 404，不区分「不存在」与「不是本人」）。"""
        return (
            await self._one(
                "UPDATE watchlist SET group_name = %s "
                "WHERE user_id = %s AND symbol = %s RETURNING id",
                (group_name, user_id, symbol),
            )
            is not None
        )

    async def drop_watchlist_item(self, user_id: int, symbol: str) -> bool:
        return (
            await self._one(
                "DELETE FROM watchlist WHERE user_id = %s AND symbol = %s RETURNING id",
                (user_id, symbol),
            )
            is not None
        )

    async def rename_watchlist_group(self, user_id: int, old: str, new: str) -> bool:
        """重命名分组 = 一条 UPDATE。目标名已存在时两组自然合并（不设 409）。"""
        return await self._group_update(user_id, new, old)

    async def drop_watchlist_group(self, user_id: int, group: str) -> bool:
        """删组 = 组内标的回落 `DEFAULT_GROUP`，同样只是一条 UPDATE（无分组实体表）。"""
        return await self._group_update(user_id, DEFAULT_GROUP, group)

    async def _group_update(self, user_id: int, new: str, old: str) -> bool:
        return (
            await self._one(
                "UPDATE watchlist SET group_name = %s "
                "WHERE user_id = %s AND group_name = %s RETURNING id",
                (new, user_id, old),
            )
            is not None
        )



def _watchlist_row(row: dict[str, Any]) -> dict[str, Any]:
    """`added_price` 是 NUMERIC，psycopg 给的是 `Decimal`。

    在数据层就转成 float：JSON 契约里它是 number，而 `Decimal - float` 会直接 TypeError——
    让 Decimal 漂到端点里做算术是个只会炸在运行期的坑。
    """
    price = row["added_price"]
    return {**row, "added_price": float(price) if price is not None else None}
