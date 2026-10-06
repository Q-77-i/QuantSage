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
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.tools import BaseTool

from app.core.db import EmailTaken


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

    `list_thread_ids` 按「最近活动」倒序——用列表头部插入模拟 `last_active_at DESC`，
    真实实现靠 SQL 排序，两边行为必须一致（集成用例会拿真库再验一遍）。
    """

    def __init__(self) -> None:
        self.users: dict[int, dict[str, Any]] = {}
        self.threads: dict[str, int] = {}  # thread_id → user_id
        self._recent: list[str] = []  # 最近活动倒序
        self._next_id = 1

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

    async def list_thread_ids(self, user_id: int, limit: int) -> list[str]:
        mine = [tid for tid in self._recent if self.threads.get(tid) == user_id]
        return mine[:limit]

    async def drop_thread(self, thread_id: str, user_id: int) -> None:
        if self.threads.get(thread_id) == user_id:
            self.threads.pop(thread_id, None)
            self._recent = [tid for tid in self._recent if tid != thread_id]

    def _touch(self, thread_id: str) -> None:
        self._recent = [tid for tid in self._recent if tid != thread_id]
        self._recent.insert(0, thread_id)
