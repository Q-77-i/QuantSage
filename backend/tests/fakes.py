"""离线测试用的假模型与内存版业务库。

假模型：langchain_core 自带的两个都**不够用**（源码核对过）：
  * `FakeMessagesListChatModel` 能承载带 tool_calls 的消息，但没实现 `bind_tools`，
    而 `create_agent` 运行时会调用它（基类直接抛 NotImplementedError）；
  * `GenericFakeChatModel` 能逐 token 流式，但它的 `_stream` 只处理 content，
    不产出 `tool_calls`——agent 永远不会去调工具。

所以这里补一个合体：既能按脚本返回工具调用，又能逐字符流式输出。

假业务库：`FakeDatabase` 与 `app.core.db.Database` **同方法面**，供离线用例注入
`app.state.db`，让「越权一律 404」矩阵不必依赖真实 Postgres（真库那份在
`tests/integration/` 里双跑，见 SPEC §12）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.tools import BaseTool

from app.core.db import DEFAULT_GROUP, EmailTaken, StrategyNameTaken, SymbolTracked


class ToolCallingFakeModel(FakeMessagesListChatModel):
    """按 `responses` 脚本逐条作答，支持 tool_calls 与逐字符流式。

    注意它**有状态且会循环**：走到脚本末尾会回到第一条。一轮「调工具 → 作答」
    正好消耗两条，多轮用例要按这个语义写脚本。
    """

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> ToolCallingFakeModel:
        return self  # 脚本已固定，绑不绑都一样

    def _next_message(self) -> AIMessage:
        message = self.responses[self.i]
        self.i = (self.i + 1) % len(self.responses)
        if not isinstance(message, AIMessage):
            raise TypeError(f"脚本只支持 AIMessage，收到 {type(message).__name__}")
        return message

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        message = self._next_message()

        if message.tool_calls:
            # 工具调用轮：content 为空，参数以 JSON 增量分片下发（与真实模型同形）
            for index, call in enumerate(message.tool_calls):
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content="",
                        tool_call_chunks=[
                            {
                                "name": call["name"],
                                "args": json.dumps(call["args"], ensure_ascii=False),
                                "id": call["id"],
                                "index": index,
                                "type": "tool_call_chunk",
                            }
                        ],
                    )
                )
            return

        for char in str(message.content):
            yield ChatGenerationChunk(message=AIMessageChunk(content=char))


class FakeDatabase:
    """内存版业务库：与 `Database` 同方法面，方法语义也照抄（含唯一约束）。

    `list_threads` 按「最近活动」倒序——用列表头部插入模拟 `last_active_at DESC`，
    真实实现靠 SQL 排序，两边行为必须一致（集成用例会拿真库再验一遍）。

    自选股的唯一约束**必须照抄**：`add_watchlist_item` 撞号要抛 `SymbolTracked`，
    否则离线那条 409 用例测的是替身自己的宽容，不是真库的行为。策略的
    `UNIQUE(user_id, name)`（M4c）同理——少了它，409 与归属矩阵都是假的。
    """

    def __init__(self) -> None:
        self.users: dict[int, dict[str, Any]] = {}
        self.threads: dict[str, int] = {}  # thread_id → user_id
        self._recent: list[str] = []  # 最近活动倒序
        self._active_at: dict[str, datetime] = {}
        self._next_id = 1
        # 自选股：(user_id, symbol) → 行；回测：run_id → 行（顺序另记，见下）
        self.watchlist: dict[tuple[int, str], dict[str, Any]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self._run_order: list[str] = []  # 插入序，列表按它倒序给「最新在前」
        # 策略：(user_id, strategy_id) → 行（名字唯一性另按 user 判，见 create_strategy）
        self.strategies: dict[tuple[int, str], dict[str, Any]] = {}
        # 批量/网格汇总：run_id → 行（顺序另记，列表倒序给「最新在前」）
        self.optimizations: dict[str, dict[str, Any]] = {}
        self._optimization_order: list[str] = []

    async def create_user(self, email: str, password_hash: str) -> dict[str, Any]:
        if any(user["email"] == email for user in self.users.values()):
            raise EmailTaken(email)
        user = {"id": self._next_id, "email": email, "password_hash": password_hash}
        self.users[self._next_id] = user
        self._next_id += 1
        return user

    async def find_user_by_email(self, email: str) -> dict[str, Any] | None:
        return next((u for u in self.users.values() if u["email"] == email), None)

    async def find_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        return self.users.get(user_id)

    async def claim_thread(self, thread_id: str, user_id: int) -> None:
        self.threads.setdefault(thread_id, user_id)
        self._touch(thread_id)

    async def touch_thread(self, thread_id: str, user_id: int) -> None:
        if self.threads.get(thread_id) == user_id:
            self._touch(thread_id)

    async def thread_owner(self, thread_id: str) -> int | None:
        return self.threads.get(thread_id)

    async def list_threads(self, user_id: int, limit: int) -> list[dict[str, Any]]:
        mine = [tid for tid in self._recent if self.threads.get(tid) == user_id]
        return [
            {"thread_id": tid, "last_active_at": self._active_at[tid]} for tid in mine[:limit]
        ]

    async def drop_thread(self, thread_id: str, user_id: int) -> None:
        if self.threads.get(thread_id) == user_id:
            self.threads.pop(thread_id, None)
            self._recent = [tid for tid in self._recent if tid != thread_id]
            self._active_at.pop(thread_id, None)

    # ── 自选股 ──────────────────────────────────────────────

    async def list_watchlist(self, user_id: int) -> list[dict[str, Any]]:
        mine = [row for (uid, _), row in self.watchlist.items() if uid == user_id]
        # 与真库的 `ORDER BY group_name, added_at` 同序
        mine.sort(key=lambda row: (row["group_name"], row["added_at"]))
        return [dict(row) for row in mine]

    async def add_watchlist_item(
        self, user_id: int, symbol: str, group_name: str, added_price: float | None
    ) -> dict[str, Any]:
        if (user_id, symbol) in self.watchlist:
            raise SymbolTracked(symbol)
        row = {
            "symbol": symbol,
            "group_name": group_name,
            "added_at": datetime.now(UTC),
            "added_price": added_price,
        }
        self.watchlist[(user_id, symbol)] = row
        return dict(row)

    async def move_watchlist_item(self, user_id: int, symbol: str, group_name: str) -> bool:
        row = self.watchlist.get((user_id, symbol))
        if row is None:
            return False
        row["group_name"] = group_name
        return True

    async def drop_watchlist_item(self, user_id: int, symbol: str) -> bool:
        return self.watchlist.pop((user_id, symbol), None) is not None

    async def rename_watchlist_group(self, user_id: int, old: str, new: str) -> bool:
        return self._retarget_group(user_id, old, new)

    async def drop_watchlist_group(self, user_id: int, group: str) -> bool:
        # 与真库同义：组内标的回落默认分组，标的本身留着
        return self._retarget_group(user_id, group, DEFAULT_GROUP)

    def _retarget_group(self, user_id: int, old: str, new: str) -> bool:
        hit = False
        for (uid, _), row in self.watchlist.items():
            if uid == user_id and row["group_name"] == old:
                row["group_name"] = new
                hit = True
        return hit

    # ── 用户策略（M4c）─────────────────────────────────────

    async def list_strategies(self, user_id: int) -> list[dict[str, Any]]:
        """摘要形状与真库一致：**不带 code**，最近改的在前。"""
        rows = [row for (uid, _), row in self.strategies.items() if uid == user_id]
        rows.sort(key=lambda row: row["updated_at"], reverse=True)
        return [
            {key: row[key] for key in ("id", "name", "created_at", "updated_at")} for row in rows
        ]

    async def create_strategy(
        self, strategy_id: str, user_id: int, name: str, code: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        if self._name_taken(user_id, name):
            raise StrategyNameTaken(name)
        now = datetime.now(UTC)
        row = {
            "id": strategy_id,
            "name": name,
            "code": code,
            "params": dict(params),
            "created_at": now,
            "updated_at": now,
        }
        self.strategies[(user_id, strategy_id)] = row
        return dict(row)

    async def get_strategy(self, user_id: int, strategy_id: str) -> dict[str, Any] | None:
        row = self.strategies.get((user_id, strategy_id))
        return dict(row) if row is not None else None

    async def update_strategy(
        self,
        user_id: int,
        strategy_id: str,
        *,
        name: str | None = None,
        code: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        row = self.strategies.get((user_id, strategy_id))
        if row is None:
            return None
        if name is not None:
            if self._name_taken(user_id, name, exclude=strategy_id):
                raise StrategyNameTaken(name)
            row["name"] = name
        if code is not None:
            row["code"] = code
        if params is not None:
            row["params"] = dict(params)
        row["updated_at"] = datetime.now(UTC)
        return dict(row)

    async def drop_strategy(self, user_id: int, strategy_id: str) -> bool:
        return self.strategies.pop((user_id, strategy_id), None) is not None

    def _name_taken(self, user_id: int, name: str, *, exclude: str | None = None) -> bool:
        return any(
            uid == user_id and row["name"] == name and sid != exclude
            for (uid, sid), row in self.strategies.items()
        )

    # ── 回测记录 ────────────────────────────────────────────

    async def save_backtest_run(
        self,
        run_id: str,
        user_id: int,
        request: dict[str, Any],
        report: dict[str, Any],
        *,
        strategy_id: str | None = None,
        code_sha256: str | None = None,
    ) -> None:
        self.runs[run_id] = {
            "id": run_id,
            "user_id": user_id,
            "created_at": datetime.now(UTC),
            "request": request,
            "report": report,
            "strategy_id": strategy_id,
            "code_sha256": code_sha256,
        }
        self._run_order.append(run_id)

    async def list_backtest_runs(self, user_id: int, limit: int) -> list[dict[str, Any]]:
        """摘要形状与真库的 JSONB 子集抽取一致：`metrics` 整块给，不逐键摊平。"""
        summaries: list[dict[str, Any]] = []
        for run_id in reversed(self._run_order):
            row = self.runs[run_id]
            if row["user_id"] != user_id:
                continue
            request = row["request"]
            summaries.append(
                {
                    "id": run_id,
                    "created_at": row["created_at"],
                    "symbol": request["symbol"],
                    "strategy": request["strategy"],
                    "start": request["start"],
                    "end": request["end"],
                    "pit_mode": request["pit_mode"],
                    "metrics": row["report"]["metrics"],
                    # 与真库的 `report->'meta'->>'strategy_name'` 同义（内置策略为 None）
                    "strategy_name": row["report"]["meta"].get("strategy_name"),
                }
            )
            if len(summaries) == limit:
                break
        return summaries

    async def get_backtest_run(self, user_id: int, run_id: str) -> dict[str, Any] | None:
        row = self.runs.get(run_id)
        if row is None or row["user_id"] != user_id:
            return None
        return {
            "id": row["id"],
            "created_at": row["created_at"],
            "request": row["request"],
            "report": row["report"],
            "strategy_id": row["strategy_id"],
            "code_sha256": row["code_sha256"],
        }

    # ── 批量 / 网格汇总（M5b）────────────────────────────────

    async def save_optimization_run(
        self, run_id: str, user_id: int, request: dict[str, Any], summary: dict[str, Any]
    ) -> None:
        self.optimizations[run_id] = {
            "id": run_id,
            "user_id": user_id,
            "created_at": datetime.now(UTC),
            "request": request,
            "summary": summary,
        }
        self._optimization_order.append(run_id)

    async def list_optimization_runs(self, user_id: int, limit: int) -> list[dict[str, Any]]:
        """**照抄真库的 `summary - 'cells'`**：摘要是「去掉每格矩阵之后的 summary」。

        替身这里若图省事返回整份 summary，前端在离线态就能拿到 cells、在真库上却拿不到——
        正是 M4c 那条「离线替身会骗人」的同类坑。
        """
        summaries: list[dict[str, Any]] = []
        for run_id in reversed(self._optimization_order):
            row = self.optimizations[run_id]
            if row["user_id"] != user_id:
                continue
            summaries.append(
                {
                    "id": run_id,
                    "created_at": row["created_at"],
                    "request": row["request"],
                    "summary": {k: v for k, v in row["summary"].items() if k != "cells"},
                }
            )
            if len(summaries) == limit:
                break
        return summaries

    async def get_optimization_run(self, user_id: int, run_id: str) -> dict[str, Any] | None:
        row = self.optimizations.get(run_id)
        if row is None or row["user_id"] != user_id:
            return None
        return {
            "id": row["id"],
            "created_at": row["created_at"],
            "request": row["request"],
            "summary": row["summary"],
        }

    def _touch(self, thread_id: str) -> None:
        self._recent = [tid for tid in self._recent if tid != thread_id]
        self._recent.insert(0, thread_id)
        self._active_at[thread_id] = datetime.now(UTC)
