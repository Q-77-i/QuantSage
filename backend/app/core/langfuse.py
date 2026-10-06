"""Langfuse 观测：注册客户端 + 构造回调处理器。

**两条都必须显式做，否则 trace 会静默丢失**（本机实测）：
  1. `.env` 只进 pydantic Settings、不进 `os.environ`，而 SDK 的自动初始化读的是
     环境变量——不显式传参就会降级成 NoOpTracer；
  2. `CallbackHandler()` 零参构造走 `get_client()` 的「无 key」分支：进程内一旦存在
     多个 Langfuse 实例（测试里很常见），它会**静默返回 tracing=False 的客户端**，
     只在日志留一行 warning。表现为「无报错，但 Langfuse 里一片空白」。

所以：先按 public_key 注册客户端，再用**同一个 key** 取 handler。
注册用 lru_cache 保证幂等——重复构造正是第 2 条的触发条件。
"""

from __future__ import annotations

import logging
from functools import lru_cache

from langfuse import Langfuse
from langfuse.langchain import CallbackHandler

from app.core.config import get_settings

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _registered_public_key() -> str | None:
    """幂等注册 Langfuse 客户端，返回可用于取 handler 的 public_key。

    未启用追踪或 key 缺失时返回 None（调用方据此不挂载回调，对话功能不受影响）。
    """
    settings = get_settings()
    if not settings.langfuse_tracing_enabled:
        log.info("Langfuse 追踪已关闭（LANGFUSE_TRACING_ENABLED=false）")
        return None

    public_key = settings.langfuse_public_key.get_secret_value()
    secret_key = settings.langfuse_secret_key.get_secret_value()
    if not (public_key and secret_key):
        log.warning("Langfuse key 未配置，跳过追踪（不影响对话功能）")
        return None

    Langfuse(
        public_key=public_key,
        secret_key=secret_key,
        base_url=settings.langfuse_base_url,
    )
    log.info("Langfuse 追踪已启用（%s）", settings.langfuse_base_url)
    return public_key


def build_langfuse_handler() -> CallbackHandler | None:
    """构造 LangChain 回调处理器；未启用追踪时返回 None。"""
    public_key = _registered_public_key()
    return CallbackHandler(public_key=public_key) if public_key else None
