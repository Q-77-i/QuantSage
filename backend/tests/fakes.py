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
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.tools import BaseTool

from app.core.db import (
    DEFAULT_GROUP,
    EmailTaken,
    StrategyNameTaken,
    StrategyVersionConflict,
    SymbolTracked,
)


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

        # 真模型的用量在**最后一个包**里（`stream_options.include_usage` 的尾包），
        # 假模型要照抄这一点——否则「按 usage 计数」的护栏在离线用例里永远看不到数
        # （E4c 的 chat 上限就是这么被测出来的）
        text = str(message.content)
        for index, char in enumerate(text):
            last = index == len(text) - 1
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content=char,
                    usage_metadata=message.usage_metadata if last else None,
                    response_metadata=message.response_metadata if last else {},
                )
            )


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
        #: 5d：`(strategy_id, version_no) -> 版本行`。与真库同语义的 append-only。
        self.strategy_versions: dict[tuple[str, int], dict[str, Any]] = {}
        # 批量/网格汇总：run_id → 行（顺序另记，列表倒序给「最新在前」）
        self.optimizations: dict[str, dict[str, Any]] = {}
        self._optimization_order: list[str] = []
        # 模拟盘（M6）：会话 / 持仓 / 决策 / 净值，四份都与真库同形。
        # **乐观并发守卫必须照抄**（`as_of` 对不上返 False）——替身若不挡，
        # 「双击推进只生效一次」那条用例测的就是替身自己的宽容。
        self.paper_accounts: dict[str, dict[str, Any]] = {}
        self._paper_order: list[str] = []
        self.paper_positions_rows: dict[str, list[dict[str, Any]]] = {}
        self.paper_decision_rows: dict[str, dict[str, Any]] = {}
        self.paper_equity_rows: dict[str, list[dict[str, Any]]] = {}
        # E5 风控审计：id → 行（`DO NOTHING` 语义 = 已存在的 id 不覆盖）
        self.paper_audit_rows: dict[str, dict[str, Any]] = {}
        # 绩效研报（M7）：report_id → 行；顺序另记，列表倒序给「最新在前」
        self.reports: dict[str, dict[str, Any]] = {}
        self._report_order: list[str] = []
        self.runs: dict[str, dict[str, Any]] = {}
        self._run_order: list[str] = []
        # 异步任务（P3-E1a）：task_id → 行；顺序另记（真库按 `created_at, id`，替身里
        # 同一微秒内建的两条分不出先后，插入序才是那个判据的忠实等价物）
        self.tasks: dict[str, dict[str, Any]] = {}
        self._task_order: list[str] = []
        # 用户配额（E2）：(user_id, metric, period_key) → 已用；拒绝记录按插入序
        # 拒绝记录的行存 `_rows` 后缀——`quota_rejections` 这个名字要留给方法
        # （实例属性会盖住同名方法，同 `paper_decision_rows` 的理由）
        self.usage_counters: dict[tuple[int, str, str], int] = {}
        self.quota_rejection_rows: list[dict[str, Any]] = []

    async def create_user(self, email: str, password_hash: str) -> dict[str, Any]:
        if any(user["email"] == email for user in self.users.values()):
            raise EmailTaken(email)
        # `token_version` 与真库的列默认值同源（E8a）：撤销版本从 0 起
        user = {
            "id": self._next_id,
            "email": email,
            "password_hash": password_hash,
            "token_version": 0,
            "role": "user",  # 与真库的列默认值同源（E2b）
        }
        self.users[self._next_id] = user
        self._next_id += 1
        return user

    async def find_user_by_email(self, email: str) -> dict[str, Any] | None:
        return next((u for u in self.users.values() if u["email"] == email), None)

    async def find_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        return self.users.get(user_id)

    async def bump_token_version(self, user_id: int) -> None:
        user = self.users.get(user_id)
        if user is not None:
            user["token_version"] += 1

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
            {key: row[key] for key in ("id", "name", "version", "created_at", "updated_at")}
            for row in rows
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
            "version": 1,
            "created_at": now,
            "updated_at": now,
        }
        self.strategies[(user_id, strategy_id)] = row
        self.strategy_versions[(strategy_id, 1)] = {
            "id": str(uuid.uuid4()),
            "strategy_id": strategy_id,
            "version_no": 1,
            "name": name,
            "code": code,
            "params": dict(params),
            "created_at": now,
        }
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
        expected_version: int | None = None,
    ) -> dict[str, Any] | None:
        row = self.strategies.get((user_id, strategy_id))
        if row is None:
            return None
        current = int(row["version"])
        if expected_version is not None and expected_version != current:
            raise StrategyVersionConflict(
                f"策略已在别处更新（当前 v{current}）——请重新打开后再保存"
            )
        changed = (code is not None and code != row["code"]) or (
            params is not None and dict(params) != row["params"]
        )
        if name is not None:
            if self._name_taken(user_id, name, exclude=strategy_id):
                raise StrategyNameTaken(name)
            row["name"] = name
        if code is not None:
            row["code"] = code
        if params is not None:
            row["params"] = dict(params)
        if changed:
            row["version"] = current + 1
            self.strategy_versions[(strategy_id, current + 1)] = {
                "id": str(uuid.uuid4()),
                "strategy_id": strategy_id,
                "version_no": current + 1,
                "name": row["name"],
                "code": row["code"],
                "params": dict(row["params"]),
                "created_at": datetime.now(UTC),
            }
        row["updated_at"] = datetime.now(UTC)
        return dict(row)

    async def list_strategy_versions(
        self, user_id: int, strategy_id: str
    ) -> list[dict[str, Any]] | None:
        row = self.strategies.get((user_id, strategy_id))
        if row is None:
            return None
        versions = [
            v for (sid, _), v in self.strategy_versions.items() if sid == strategy_id
        ]
        versions.sort(key=lambda v: v["version_no"], reverse=True)
        return [
            {
                "id": v["id"],
                "version_no": v["version_no"],
                "name": v["name"],
                "created_at": v["created_at"],
                "current": v["version_no"] == row["version"],
            }
            for v in versions
        ]

    async def get_strategy_version(
        self, user_id: int, strategy_id: str, version_no: int
    ) -> dict[str, Any] | None:
        if (user_id, strategy_id) not in self.strategies:
            return None
        version = self.strategy_versions.get((strategy_id, version_no))
        return dict(version) if version is not None else None

    async def rollback_strategy(
        self,
        user_id: int,
        strategy_id: str,
        version_no: int,
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any] | None:
        row = self.strategies.get((user_id, strategy_id))
        if row is None:
            return None
        current = int(row["version"])
        if expected_version is not None and expected_version != current:
            raise StrategyVersionConflict(
                f"策略已在别处更新（当前 v{current}）——请重新打开后再回滚"
            )
        target = self.strategy_versions.get((strategy_id, version_no))
        if target is None:
            return None
        if target["code"] == row["code"] and dict(target["params"]) == dict(row["params"]):
            return dict(row)
        row["code"] = target["code"]
        row["params"] = dict(target["params"])
        row["version"] = current + 1
        row["updated_at"] = datetime.now(UTC)
        self.strategy_versions[(strategy_id, current + 1)] = {
            "id": str(uuid.uuid4()),
            "strategy_id": strategy_id,
            "version_no": current + 1,
            "name": row["name"],
            "code": target["code"],
            "params": dict(target["params"]),
            "created_at": datetime.now(UTC),
        }
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
        data_digest: str | None = None,
        strategy_version: int | None = None,
    ) -> None:
        self.runs[run_id] = {
            "id": run_id,
            "user_id": user_id,
            "created_at": datetime.now(UTC),
            "request": request,
            "report": report,
            "strategy_id": strategy_id,
            "code_sha256": code_sha256,
            "data_digest": data_digest,
            "strategy_version": strategy_version,
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
                    # E7d 数据指纹（真库是平铺列；旧记录为 None）
                    "data_digest": row.get("data_digest"),
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
            "data_digest": row.get("data_digest"),
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

    # ── 模拟盘（M6）─────────────────────────────────────────

    async def create_paper_account(
        self, *, account_id: str, user_id: int, name: str, config: dict[str, Any],
        cash: float, as_of: date, rules: dict[str, Any], risk_state: dict[str, Any],
        positions: list[dict[str, Any]], decisions: list[dict[str, Any]],
        equity: list[dict[str, Any]], risk_events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        row = {
            "id": account_id,
            "user_id": user_id,
            "name": name,
            "config": config,
            "status": "active",
            "cash": cash,
            "realized_pnl": 0.0,
            "as_of": as_of,
            "rules": rules,
            "risk_state": risk_state,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
        self.paper_accounts[account_id] = row
        self._paper_order.append(account_id)
        self.paper_positions_rows[account_id] = list(positions)
        self.paper_decision_rows.update({d["id"]: d for d in decisions})
        self.paper_equity_rows[account_id] = list(equity)
        self._write_audit(risk_events)
        return row

    async def paper_advance(
        self, *, account_id: str, user_id: int, expected_as_of: date, cash: float,
        realized_pnl: float, as_of: date, status: str, rules: dict[str, Any],
        risk_state: dict[str, Any], positions: list[dict[str, Any]],
        decisions: list[dict[str, Any]], equity: list[dict[str, Any]],
        risk_events: list[dict[str, Any]],
    ) -> bool:
        row = self.paper_accounts.get(account_id)
        if row is None or row["user_id"] != user_id or row["as_of"] != expected_as_of:
            return False  # 守卫没中：**什么都不写**（真库那条 UPDATE 也是这个语义）
        row.update(
            {
                "cash": cash,
                "realized_pnl": realized_pnl,
                "as_of": as_of,
                "status": status,
                "rules": rules,
                "risk_state": risk_state,
                "updated_at": datetime.now(UTC),
            }
        )
        self.paper_positions_rows[account_id] = list(positions)
        self.paper_decision_rows.update({d["id"]: d for d in decisions})
        merged = {e["trade_date"]: e for e in self.paper_equity_rows.get(account_id, [])}
        merged.update({e["trade_date"]: e for e in equity})  # 已写过的日子不改（不可变事实）
        self.paper_equity_rows[account_id] = [merged[d] for d in sorted(merged)]
        self._write_audit(risk_events)
        return True

    def _write_audit(self, rows: list[dict[str, Any]]) -> None:
        """审计行 `ON CONFLICT DO NOTHING`：已存在的 id 不覆盖（不可变事实）。"""
        for row in rows:
            self.paper_audit_rows.setdefault(row["id"], {**row, "created_at": datetime.now(UTC)})

    async def list_paper_accounts(self, user_id: int, limit: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for account_id in reversed(self._paper_order):
            row = self.paper_accounts[account_id]
            if row["user_id"] != user_id:
                continue
            config = row["config"]
            out.append(
                {
                    "id": account_id,
                    "name": row["name"],
                    "status": row["status"],
                    "cash": row["cash"],
                    "as_of": row["as_of"],
                    "rules": row["rules"],
                    "created_at": row["created_at"],
                    "strategy": config.get("strategy"),
                    "strategy_name": config.get("strategy_name"),
                    "symbols": list(config.get("symbols") or []),
                    "start": config.get("start"),
                    "end": config.get("end"),
                }
            )
            if len(out) == limit:
                break
        return out

    async def list_all_paper_accounts(self, limit: int = 500) -> list[dict[str, Any]]:
        """跨用户列出（结算 job 用）——替身同样**不带归属过滤**，与真库那条 SQL 同义。"""
        return [
            {
                "id": account_id,
                "user_id": self.paper_accounts[account_id]["user_id"],
                "name": self.paper_accounts[account_id]["name"],
                "status": self.paper_accounts[account_id]["status"],
                "as_of": self.paper_accounts[account_id]["as_of"],
                "config": self.paper_accounts[account_id]["config"],
            }
            for account_id in reversed(self._paper_order)
        ][:limit]

    async def get_paper_account(self, user_id: int, account_id: str) -> dict[str, Any] | None:
        row = self.paper_accounts.get(account_id)
        return None if row is None or row["user_id"] != user_id else row

    async def paper_positions(self, account_id: str) -> list[dict[str, Any]]:
        return list(self.paper_positions_rows.get(account_id, []))

    async def paper_decisions(self, account_id: str, limit: int) -> list[dict[str, Any]]:
        rows = [r for r in self.paper_decision_rows.values() if r["account_id"] == account_id]
        rows.sort(key=lambda r: (r["trade_date"], r["symbol"], r["side"]), reverse=True)
        return rows[:limit]

    async def paper_equity(self, account_id: str) -> list[dict[str, Any]]:
        return list(self.paper_equity_rows.get(account_id, []))

    async def paper_audit(self, account_id: str, limit: int) -> list[dict[str, Any]]:
        rows = [r for r in self.paper_audit_rows.values() if r["account_id"] == account_id]
        # 与真库 `ORDER BY trade_date DESC, created_at DESC, id` 同义：先按 id 升序，
        # 再做稳定倒序（两次排序比给每个键取反可读）。
        rows.sort(key=lambda r: r["id"])
        rows.sort(key=lambda r: (r["trade_date"], r.get("created_at")), reverse=True)
        return rows[:limit]

    async def paper_decision(self, user_id: int, decision_id: str) -> dict[str, Any] | None:
        row = self.paper_decision_rows.get(decision_id)
        if row is None:
            return None
        account = self.paper_accounts.get(row["account_id"])
        return None if account is None or account["user_id"] != user_id else row

    async def set_paper_decision(self, decision_id: str, status: str) -> bool:
        row = self.paper_decision_rows.get(decision_id)
        if row is None or row["status"] != "pending":
            return False  # 守卫照抄真库：只有待审批的能改
        row.update({"status": status, "decided_at": datetime.now(UTC)})
        return True

    async def set_paper_decisions_bulk(self, account_id: str, status: str) -> int:
        hits = [
            r
            for r in self.paper_decision_rows.values()
            if r["account_id"] == account_id
            and r["status"] == "pending"
            and r.get("origin") != "agent"  # 与真库一致：Agent 提案必须逐条裁决
        ]
        for row in hits:
            row.update({"status": status, "decided_at": datetime.now(UTC)})
        return len(hits)

    async def paper_pending_agent_count(self, account_id: str) -> int:
        return sum(
            1
            for r in self.paper_decision_rows.values()
            if r["account_id"] == account_id
            and r["status"] == "pending"
            and r.get("origin") == "agent"
        )

    async def add_paper_decision(
        self,
        *,
        account_id: str,
        user_id: int,
        expected_as_of: Any,
        decision: dict[str, Any] | None,
        risk_events: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    ) -> bool:
        account = self.paper_accounts.get(account_id)
        if (
            account is None
            or account["user_id"] != user_id
            or account.get("status") != "active"
            or account.get("as_of") != expected_as_of
        ):
            return False
        if decision is not None:
            if decision["id"] in self.paper_decision_rows:
                return False  # 照抄真库的 ON CONFLICT DO NOTHING：绝不覆盖已有决策
            self.paper_decision_rows[decision["id"]] = dict(decision)
        for event in risk_events:
            self.paper_audit_rows.setdefault(event["id"], dict(event))
        return True

    # ── 绩效研报（M7）──────────────────────────────────────

    async def create_research_report(
        self, *, report_id: str, user_id: int, account_id: str | None, snapshot: dict[str, Any],
        snapshot_hash: str, report: dict[str, Any], report_hash: str,
        kind: str = "paper_account_report", subject: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = {
            "id": report_id,
            "user_id": user_id,
            "account_id": account_id,
            "snapshot": snapshot,
            "snapshot_hash": snapshot_hash,
            "report": report,
            "report_hash": report_hash,
            "share_token": None,
            "shared_at": None,
            "created_at": datetime.now(UTC),
            # M8：深研报落同一张表（account_id 为 None），kind / subject 区分来源
            "kind": kind,
            "subject": subject,
        }
        self.reports[report_id] = row
        self._report_order.append(report_id)
        return {"id": report_id, "created_at": row["created_at"]}

    async def find_research_report(
        self, user_id: int, account_id: str, snapshot_hash: str
    ) -> dict[str, Any] | None:
        for report_id in self._report_order:  # 与真库同序：最早的先命中（ORDER BY created_at）
            row = self.reports[report_id]
            if (
                row["user_id"] == user_id
                and row["account_id"] == account_id
                and row["snapshot_hash"] == snapshot_hash
            ):
                return dict(row)
        return None

    async def get_research_report(self, user_id: int, report_id: str) -> dict[str, Any] | None:
        row = self.reports.get(report_id)
        return None if row is None or row["user_id"] != user_id else dict(row)

    async def list_research_reports(self, user_id: int, account_id: str) -> list[dict[str, Any]]:
        out = [
            {k: row[k] for k in ("id", "created_at", "snapshot_hash", "report_hash", "share_token")}
            for report_id in reversed(self._report_order)
            for row in [self.reports[report_id]]
            if row["user_id"] == user_id and row["account_id"] == account_id
        ]
        return out

    # ── 深路径运行记录（M8）────────────────────────────────

    async def create_research_run(
        self, *, run_id: str, user_id: int, question: str, subject: dict[str, Any],
        status: str = "running", events: list[dict[str, Any]] | None = None,
        budget: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = {
            "id": run_id,
            "user_id": user_id,
            "question": question,
            "subject": subject,
            "status": status,
            "events": list(events or []),
            "budget": dict(budget or {}),
            "report_id": None,
            "error": None,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
        self.runs[run_id] = row
        self._run_order.append(run_id)
        return {"id": run_id, "created_at": row["created_at"]}

    async def update_research_run(
        self, run_id: str, *, status: str | None = None,
        events: list[dict[str, Any]] | None = None, budget: dict[str, Any] | None = None,
        report_id: str | None = None, error: str | None = None,
    ) -> bool:
        row = self.runs.get(run_id)
        if row is None:
            return False
        if status is not None:
            row["status"] = status
        if events is not None:
            row["events"] = list(events)
        if budget is not None:
            row["budget"] = dict(budget)
        if report_id is not None:
            row["report_id"] = report_id
        if error is not None:
            row["error"] = error
        row["updated_at"] = datetime.now(UTC)
        return True

    async def get_research_run(self, user_id: int, run_id: str) -> dict[str, Any] | None:
        row = self.runs.get(run_id)
        return None if row is None or row["user_id"] != user_id else dict(row)

    async def list_research_runs(self, user_id: int, limit: int = 20) -> list[dict[str, Any]]:
        return [
            {k: v for k, v in self.runs[run_id].items() if k not in ("events", "budget")}
            for run_id in list(reversed(self._run_order))[:limit]
            if self.runs[run_id]["user_id"] == user_id
        ]

    async def set_report_share(
        self, report_id: str, user_id: int, *, token: str | None
    ) -> dict[str, Any] | None:
        row = self.reports.get(report_id)
        if row is None or row["user_id"] != user_id:
            return None
        # 真库的 UNIQUE(share_token)：撞号会抛，替身照抄这条语义——但**要排除自己那一行**
        # （真库是 `UPDATE ... WHERE id = 自己`，把值改成自己已有的值不违反唯一约束；
        #  不排除的话「重复点分享」在替身上会假报撞号，而真库不会）
        if token is not None and any(
            other["share_token"] == token and other["id"] != report_id
            for other in self.reports.values()
        ):
            raise ValueError(f"share_token 撞号：{token}")
        row["share_token"] = token
        row["shared_at"] = None if token is None else datetime.now(UTC)
        return {"id": row["id"], "share_token": token, "shared_at": row["shared_at"]}

    async def get_report_by_id(self, report_id: str) -> dict[str, Any] | None:
        row = self.reports.get(report_id)
        return {k: v for k, v in row.items() if k != "user_id"} if row is not None else None

    async def get_report_by_token(self, token: str) -> dict[str, Any] | None:
        for row in self.reports.values():
            if row["share_token"] == token:
                # 真库那条 SELECT 不带 user_id——替身也**不带**，否则公开端点会静默多拿一个字段
                return {k: v for k, v in row.items() if k != "user_id"}
        return None

    # ── 异步任务（P3-E1a）──────────────────────────────────

    def _task_row(self, task_id: str) -> dict[str, Any]:
        row = self.tasks[task_id]
        return {k: v for k, v in row.items() if k != "user_id"}

    async def create_task(
        self,
        task_id: str,
        user_id: int,
        kind: str,
        request: dict[str, Any],
        *,
        total: int = 1,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if idempotency_key is not None:
            for row in self.tasks.values():
                if (
                    row["user_id"] == user_id
                    and row.get("idempotency_key") == idempotency_key
                ):
                    return row  # 与真库的 ON CONFLICT 同语义：回既有那一行
        self.tasks[task_id] = {
            "id": task_id,
            "user_id": user_id,
            "kind": kind,
            "status": "queued",
            "request": dict(request),
            "progress": {"done": 0, "total": total},
            "result": None,
            "error": None,
            "idempotency_key": idempotency_key,
            "created_at": datetime.now(UTC),
            "started_at": None,
            "finished_at": None,
        }
        self._task_order.append(task_id)
        return self._task_row(task_id)

    async def task_by_idempotency_key(self, user_id: int, key: str) -> dict[str, Any] | None:
        for row in self.tasks.values():
            if row["user_id"] == user_id and row.get("idempotency_key") == key:
                return row
        return None

    async def get_task(self, user_id: int, task_id: str) -> dict[str, Any] | None:
        row = self.tasks.get(task_id)
        return None if row is None or row["user_id"] != user_id else self._task_row(task_id)

    async def list_tasks(self, user_id: int, limit: int) -> list[dict[str, Any]]:
        """摘要：与真库同形（不带整份 `request`，只抽 symbol / strategy 两列）。"""
        out: list[dict[str, Any]] = []
        for task_id in reversed(self._task_order):
            row = self.tasks[task_id]
            if row["user_id"] != user_id:
                continue
            summary = {
                k: v for k, v in self._task_row(task_id).items() if k != "request"
            }
            summary["symbol"] = row["request"].get("symbol")
            summary["strategy"] = row["request"].get("strategy")
            out.append(summary)
            if len(out) == limit:
                break
        return out

    async def claim_next_task(self) -> dict[str, Any] | None:
        for task_id in self._task_order:
            if self.tasks[task_id]["status"] == "queued":
                self.tasks[task_id].update(status="running", started_at=datetime.now(UTC))
                # 与真库同形：取件**多带 `user_id`**（执行体要拿它落归属），其余对外列照旧
                return dict(self.tasks[task_id])
        return None

    async def update_task_progress(self, task_id: str, *, done: int, total: int) -> None:
        row = self.tasks.get(task_id)
        if row is not None and row["status"] == "running":
            row["progress"] = {"done": done, "total": total}

    async def finish_task(self, task_id: str, *, result: dict[str, Any]) -> None:
        row = self.tasks.get(task_id)
        # 与真库的条件更新同语义：只有 `running` 能被标完成（被扫尾成 failed 的不覆盖）
        if row is not None and row["status"] == "running":
            row.update(status="done", result=dict(result), finished_at=datetime.now(UTC))

    async def finish_task_cancelled(self, task_id: str) -> None:
        row = self.tasks.get(task_id)
        if row is not None and row["status"] == "running":
            row.update(status="cancelled", finished_at=datetime.now(UTC))

    async def fail_task(self, task_id: str, *, error: dict[str, Any]) -> None:
        row = self.tasks.get(task_id)
        if row is not None and row["status"] == "running":
            row.update(status="failed", error=dict(error), finished_at=datetime.now(UTC))

    async def cancel_task(self, user_id: int, task_id: str) -> dict[str, Any] | None:
        row = self.tasks.get(task_id)
        if row is None or row["user_id"] != user_id:
            return None
        if row["status"] == "queued":
            row.update(status="cancelled", finished_at=datetime.now(UTC))
        return self._task_row(task_id)

    async def sweep_interrupted_tasks(self) -> int:
        stale = [row for row in self.tasks.values() if row["status"] == "running"]
        for row in stale:
            row.update(
                status="failed",
                error={"kind": "interrupted", "message": "服务重启中断"},
                finished_at=datetime.now(UTC),
            )
        return len(stale)

    # ── 用户配额（E2）──────────────────────────────────────

    async def task_status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.tasks.values():
            status = str(row["status"])
            counts[status] = counts.get(status, 0) + 1
        return counts

    async def count_active_tasks(self, user_id: int) -> int:
        return sum(
            1
            for row in self.tasks.values()
            if row["user_id"] == user_id and row["status"] in ("queued", "running")
        )

    async def bump_usage_counter(
        self, user_id: int, metric: str, period_key: str, limit: int
    ) -> int | None:
        """与真库同语义：**已达上限不递增**（真库靠 `DO UPDATE … WHERE` 挡住）。

        新行走「插入」分支、**不看上限**——照抄 SQL 的形状（`enforce` 保证不会用 `limit <= 0`
        调它）。替身若在这里自作主张加判断，离线用例测的就是替身自己的想法了。
        """
        key = (user_id, metric, period_key)
        if key not in self.usage_counters:
            self.usage_counters[key] = 1
            return 1
        if self.usage_counters[key] >= limit:
            return None
        self.usage_counters[key] += 1
        return self.usage_counters[key]

    async def record_quota_rejection(
        self, user_id: int, metric: str, used: int, limit: int
    ) -> None:
        self.quota_rejection_rows.append(
            {
                "user_id": user_id,
                "metric": metric,
                "used": used,
                "limit_value": limit,
                "at": datetime.now(UTC),
            }
        )

    def _usage_rows(
        self, period_from: str, period_to: str, since: datetime, q: str
    ) -> list[dict[str, Any]]:
        """过滤后的**全部**行——两个方法共用它（真库那份共用的是一对 SQL 常量）。

        搜索按真库的 `ILIKE %q%` 语义照抄（**不区分大小写**的包含匹配）——替身若用
        `==` 比，离线用例测的就是替身自己的想法了。
        """
        needle = q.strip().lower()
        rows: list[dict[str, Any]] = []
        for user in self.users.values():
            if needle and needle not in str(user["email"]).lower():
                continue
            active = sum(
                1
                for row in self.tasks.values()
                if row["user_id"] == user["id"] and row["status"] in ("queued", "running")
            )
            # 与真库同语义：调用按 `period_key` **区间求和**（`days=1` 是它的 n=1 特例）
            calls = sum(
                used
                for (uid, metric, day), used in self.usage_counters.items()
                if uid == user["id"] and metric == "agent_calls" and period_from <= day <= period_to
            )
            rejected = sum(
                1
                for row in self.quota_rejection_rows
                if row["user_id"] == user["id"] and row["at"] >= since
            )
            rows.append(
                {
                    "id": user["id"],
                    "email": user["email"],
                    "role": user.get("role") or "user",
                    "active_tasks": active,
                    "agent_calls": calls,
                    "rejected": rejected,
                }
            )
        return sorted(rows, key=lambda row: row["id"])

    @staticmethod
    def _filter_usage(
        rows: list[dict[str, Any]], role: str, active_only: bool
    ) -> list[dict[str, Any]]:
        """与真库 `_USAGE_FILTER` 同语义：`role=""` 不过滤；`active_only` 看**三读数任一非零**。"""
        if role:
            rows = [row for row in rows if row["role"] == role]
        if active_only:
            rows = [
                row
                for row in rows
                if row["active_tasks"] or row["agent_calls"] or row["rejected"]
            ]
        return rows

    async def admin_usage(
        self,
        period_from: str,
        period_to: str,
        since: datetime,
        *,
        limit: int,
        offset: int,
        q: str = "",
        role: str = "",
        active_only: bool = False,
        sort: str = "id",
        order: str = "asc",
    ) -> list[dict[str, Any]]:
        """与真库同形：三列各有来源（在飞任务直查任务表、当期调用读计数、当期被拒数事件）。

        排序也照抄真库的语义：**按 `id` 兜底**（同值时顺序确定，否则翻页会重复或漏行）。
        """
        rows = self._filter_usage(
            self._usage_rows(period_from, period_to, since, q), role, active_only
        )
        rows.sort(key=lambda row: (row[sort], row["id"]), reverse=order == "desc")
        return rows[offset : offset + limit]

    async def admin_usage_total(
        self,
        period_from: str,
        period_to: str,
        since: datetime,
        *,
        q: str = "",
        role: str = "",
        active_only: bool = False,
    ) -> int:
        return len(
            self._filter_usage(
                self._usage_rows(period_from, period_to, since, q), role, active_only
            )
        )

    async def quota_rejections(self, limit: int, offset: int = 0) -> list[dict[str, Any]]:
        """与真库同形：新的在前，邮箱随行（读它的只有 admin 面）。"""
        rows = []
        for index, row in enumerate(self.quota_rejection_rows):
            user = self.users.get(row["user_id"])
            rows.append(
                {
                    "id": index + 1,
                    "user_id": row["user_id"],
                    "email": user["email"] if user else None,
                    "metric": row["metric"],
                    "used": row["used"],
                    "limit_value": row["limit_value"],
                    "at": row["at"],
                }
            )
        rows.reverse()
        return rows[offset : offset + limit]

    async def quota_rejections_total(self) -> int:
        return len(self.quota_rejection_rows)

    def _touch(self, thread_id: str) -> None:
        self._recent = [tid for tid in self._recent if tid != thread_id]
        self._recent.insert(0, thread_id)
        self._active_at[thread_id] = datetime.now(UTC)
