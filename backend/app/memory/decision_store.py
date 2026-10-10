"""M7b 决策记忆（`app.memory.decision_store`）：LangGraph Store 的**唯一**封装。

落 Store 的只有「结算快照 + 反思文本」——回合配对与 alpha 都可从决策日志重算（纯函数），
**重算得出的事实不落库**；反思是模型产物、不可重算，才需要持久化。这条分工是 SPEC §8
写死的，也是「事实层可重算」与「记忆要留痕」两件事各归其位的地方。

namespace = `("decisions", user_id, account_id)`，key = 买入决策 id：
**用户隔离靠 namespace 前缀**（跨用户 search 取不到别人的），跨标的聚合走
`asearch(("decisions", user_id))` 再按 symbol / direction 过滤。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import date
from typing import Any

from langgraph.store.base import BaseStore

log = logging.getLogger(__name__)

from app.memory.reflection import Reflection
from app.memory.settle import SettledTrip

#: namespace 根段（跨用户聚合时以 `("decisions", user_id)` 作前缀）
NAMESPACE_ROOT = "decisions"

#: 未平仓记录的「还作不作数」判据里的金额容差（元）：JSONB 往返后按分位比较
PNL_TOLERANCE = 0.005


def memory_namespace(user_id: int, account_id: str) -> tuple[str, ...]:
    return (NAMESPACE_ROOT, str(user_id), account_id)


def is_current(stored: Mapping[str, Any] | None, settled: SettledTrip, *, as_of: date) -> bool:
    """记忆里那条还作不作数（**幂等判据**）。

    * 已平仓：平仓事实永不改变 ⇒ 存过就永久作数（省下一次反思调用）；
    * 未平仓：随账户推进而变 ⇒ 逐字段比 `as_of` / 窗口 / 盈亏，任一变了就重结。
    """
    if stored is None:
        return False
    if settled.settled:
        return bool(stored.get("settled"))
    if stored.get("settled") is not False:
        return False
    if stored.get("as_of") != as_of.isoformat():
        return False
    if stored.get("window_days") != settled.window_days:
        return False
    stored_pnl = stored.get("pnl")
    if (stored_pnl is None) != (settled.pnl is None):
        return False
    if stored_pnl is not None and settled.pnl is not None:
        return abs(float(stored_pnl) - settled.pnl) < PNL_TOLERANCE
    return True


class MemoryUnavailable(RuntimeError):
    """决策记忆没起来（库不可用 / 未配置）。端点翻 503，报告降级为「没有复盘块」。"""


class MemoryHolder:
    """决策记忆的**按需**持有者：第一次用到时，才在**当前事件循环**里建 Store 并建表。

    为什么不放在 lifespan 里建：`AsyncBatchedBaseStore` 在**构造时**捕获
    `asyncio.get_running_loop()`，此后所有 `aget` / `asearch` 都在那个 loop 上建 Future。
    在 lifespan 的 loop 里建、在请求的 loop 上用 —— 实测直接炸
    `Future attached to a different loop`（uvicorn 单 loop 侥幸不炸，那是运气，不是设计）。
    按需建 + **按 loop 缓存**，两条路都对。

    `setup()` 幂等（`CREATE TABLE IF NOT EXISTS`），跟着创建走一次；建不起来就返回 None，
    调用方降级——**不抛给请求**（记忆是研报的增强，不是它的前置）。
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._stack: AsyncExitStack | None = None
        self._store: BaseStore | None = None
        self._loop: Any = None
        self._lock: asyncio.Lock | None = None

    async def get(self) -> DecisionMemory | None:
        from langgraph.store.postgres import AsyncPostgresStore

        loop = asyncio.get_running_loop()
        if self._store is not None and self._loop is loop:
            return DecisionMemory(self._store)
        if self._lock is None or self._loop is not loop:
            self._lock = asyncio.Lock()  # 每个 loop 一把（锁不做跨 loop 复用）
        async with self._lock:
            if self._store is not None and self._loop is loop:
                return DecisionMemory(self._store)
            try:
                await self.aclose()
                stack = AsyncExitStack()
                store = await stack.enter_async_context(
                    AsyncPostgresStore.from_conn_string(self._dsn)
                )
                await store.setup()
            except Exception as exc:  # noqa: BLE001 —— 记忆不可用不是请求的错，调用方降级
                # 降级可以，但**原因必须留痕**：静默吞掉它，503 就成了一句无从下手的空话
                log.warning("决策记忆建不起来（%s: %s），本次降级", type(exc).__name__, exc)
                return None
            self._stack, self._store, self._loop = stack, store, loop
        return DecisionMemory(self._store)

    async def aclose(self) -> None:
        if self._stack is not None:
            stack, self._stack, self._store, self._loop = self._stack, None, None, None
            await stack.aclose()


async def memory_for(state: Any) -> DecisionMemory:
    """从 `app.state.memory` 取记忆：既接**现成的** `DecisionMemory`（测试注入），
    也接 `MemoryHolder`（生产：按需建）。拿不到就抛 `MemoryUnavailable`。"""
    holder = getattr(state, "memory", None)
    if holder is None:
        raise MemoryUnavailable("决策记忆未配置")
    if isinstance(holder, DecisionMemory):
        return holder
    memory = await holder.get()
    if memory is None:
        raise MemoryUnavailable("决策记忆不可用（Postgres 未就绪？）")
    return memory


@dataclass(frozen=True, slots=True)
class DecisionMemory:
    """决策记忆的读写口。构造时注入 `BaseStore`（真库 `AsyncPostgresStore` / 离线 `InMemoryStore`）。"""

    store: BaseStore

    async def save(
        self,
        *,
        user_id: int,
        account_id: str,
        settled: SettledTrip,
        reflection: Reflection | None,
        direction: str | None,
        evidence_key: str | None,
        as_of: date,
        settled_at: str,
    ) -> dict[str, Any]:
        """写入（覆盖）一条决策记忆。未到期的回合 `reflection=None`（还没有教训可总结）。"""
        value: dict[str, Any] = {
            **settled.to_payload(),
            "account_id": account_id,
            "direction": direction,
            "evidence_key": evidence_key,
            "as_of": as_of.isoformat(),
            "settled_at": settled_at,
            "reflection": reflection.to_payload() if reflection is not None else None,
        }
        await self.store.aput(memory_namespace(user_id, account_id), settled.decision_id, value)
        return value

    async def get(
        self, *, user_id: int, account_id: str, decision_id: str
    ) -> Mapping[str, Any] | None:
        item = await self.store.aget(memory_namespace(user_id, account_id), decision_id)
        return item.value if item is not None else None

    async def list_for_account(self, *, user_id: int, account_id: str) -> list[dict[str, Any]]:
        """该账户的全部决策记忆，按建仓日倒序（新的在前）。"""
        items = await self.store.asearch(memory_namespace(user_id, account_id), limit=1000)
        return sorted(
            (dict(item.value) for item in items),
            key=lambda row: (str(row.get("entry_date")), str(row.get("decision_id"))),
            reverse=True,
        )

    async def lessons(
        self,
        *,
        user_id: int,
        symbol: str | None = None,
        direction: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """跨账户 / 跨标的的教训聚合（只收**有反思文本**的已平仓回合）。

        `asearch` 的 `filter` 只做值的精确匹配，多条件与排序在本地做——条目量是「一个用户的
        决策数」，远小于分页阈值，不值得为它建索引。
        """
        items = await self.store.asearch((NAMESPACE_ROOT, str(user_id)), limit=1000)
        rows = [
            dict(item.value)
            for item in items
            if (item.value.get("reflection") or {}).get("text")
            and (symbol is None or item.value.get("symbol") == symbol)
            and (direction is None or item.value.get("direction") == direction)
        ]
        # 新的在前（平仓日倒序，同日按 id 定序）——聚合视图读的是「最近的教训」
        rows.sort(
            key=lambda row: (str(row.get("exit_date") or ""), str(row.get("decision_id"))),
            reverse=True,
        )
        return rows[:limit]

    async def symbols(self, *, user_id: int) -> Sequence[str]:
        """出现过的标的（供前端筛选下拉）。"""
        items = await self.store.asearch((NAMESPACE_ROOT, str(user_id)), limit=1000)
        return sorted({str(item.value.get("symbol")) for item in items})
