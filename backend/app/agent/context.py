"""Agent 的请求级上下文（P3 收尾项 5a）。

图是 lifespan 里的**单例**（`main.py` 只建一次），被所有用户共享；工具又是普通函数，
没有请求对象。把「当前用户 + 业务库」放进 `ContextVar`，由 `api/chat.py` 的 runner
在每次跑图前 set、跑完 reset。

**红线**：`user_id` 只能从这里取，绝不能做成 LLM 可传的工具参数——否则模型可以替
任意用户写模拟盘。工具在上下文缺失时返回人话（"模拟盘不可用"），不抛异常。
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class AgentContext:
    user_id: int
    db: Any


_current: ContextVar[AgentContext | None] = ContextVar("quantsage_agent_context", default=None)


def set_agent_context(context: AgentContext) -> Token:
    return _current.set(context)


def reset_agent_context(token: Token) -> None:
    _current.reset(token)


def current_agent_context() -> AgentContext | None:
    return _current.get()


__all__ = [
    "AgentContext",
    "current_agent_context",
    "reset_agent_context",
    "set_agent_context",
]
