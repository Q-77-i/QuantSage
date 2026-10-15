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
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

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
    """构造 LangChain 回调处理器；未启用追踪时返回 None。

    handler **不带运行期信息**（SDK 4.16 的构造签名只有 public_key / trace_context）——
    每次运行的 `session_id` / `user_id` / `trace_name` / `tags` 由调用方经 LangChain
    config 的 `metadata` 传（见 `make_langfuse_metadata`），handler 因此可以建一次、
    跨请求复用。
    """
    public_key = _registered_public_key()
    return CallbackHandler(public_key=public_key) if public_key else None


#: 运行时 metadata 的四个键——SDK 4.16 就在 `langfuse/langchain/CallbackHandler.py`
#: 的 `_parse_langfuse_trace_attributes()` 里按这四个名字取值，**拼错即静默丢失**。
TRACE_NAME_CHAT = "chat"
TRACE_NAME_DEEP = "deep-research"
TRACE_NAME_REPORT = "report"
TRACE_NAME_SETTLE = "settle"


def langfuse_callbacks(state: Any) -> list[CallbackHandler] | None:
    """从 `app.state` 取 handler 并包成 LangChain 的 callbacks 列表；没挂载则 None。

    lifespan 没建 handler（追踪关 / key 缺 / 测试里没铺）时返回 None——调用方照传，
    LangChain 对 None 是「无回调」，全链路零影响。
    """
    handler = getattr(state, "langfuse_handler", None)
    return [handler] if handler is not None else None


def make_langfuse_metadata(
    *,
    session_id: str,
    user_id: int | str,
    trace_name: str,
    tags: Sequence[str] = (),
) -> dict[str, Any]:
    """构造给 LangChain config 用的 `metadata`（四个 `langfuse_*` 键的唯一落点）。

    键名与类型都是 SDK 的契约，别在调用方手拼：
      * `langfuse_session_id` / `langfuse_user_id` / `langfuse_trace_name` 必须是 **str**；
      * `langfuse_tags` 必须是 **list**（SDK 用 `isinstance(..., list)` 判，元组会被忽略）。
    """
    return {
        "langfuse_session_id": str(session_id),
        "langfuse_user_id": str(user_id),
        "langfuse_trace_name": trace_name,
        "langfuse_tags": [str(tag) for tag in tags],
    }


@dataclass(frozen=True, slots=True)
class TraceConfig:
    """一次调用的观测配置：回调 handler + 四个运行期字段。

    **两者必须同行**：四个字段是 SDK 在**运行时**从 LangChain config 的 `metadata` 里读的，
    而 callbacks 决定 handler 在不在场——只传一半就是「有 handler 但字段空」或
    「字段构造了没人读」。打包成一个对象传，杜绝各调用点自己拼漏。
    """

    callbacks: list[CallbackHandler] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        """追踪是否真的会落痕（handler 在场）。"""
        return bool(self.callbacks)

    def kwargs(self) -> dict[str, Any]:
        """展开进 `complete` / `ask_once` 的调用参数（空配置给 None，LangChain 视作无回调）。

        `run_name` 取业务名：裸模型调用要套一层具名壳才拿得到正确的 trace 名
        （理由见 `llm.complete` 的 `run_name` 参数）。
        """
        name = self.metadata.get("langfuse_trace_name")
        return {
            "callbacks": self.callbacks or None,
            "metadata": self.metadata or None,
            "run_name": str(name) if name else None,
        }

    def with_metadata(self, **kwargs: Any) -> TraceConfig:
        """换一组字段、沿用同一批 handler（定时结算逐账户改 session/user 用）。"""
        return TraceConfig(callbacks=self.callbacks, metadata=make_langfuse_metadata(**kwargs))


def trace_config(state: Any) -> TraceConfig:
    """从 app.state 取 handler，包成 `TraceConfig`（字段由调用点补，见各调用点）。"""
    return TraceConfig(callbacks=langfuse_callbacks(state) or [])


def settle_trace(state: Any, *, account_id: Any, user_id: Any) -> TraceConfig:
    """结算/反思路径的标准观测配置——各调用点（报告生成、复盘、手动结算、定时 job）同口径。"""
    return trace_config(state).with_metadata(
        session_id=account_id, user_id=user_id, trace_name=TRACE_NAME_SETTLE, tags=("settle",)
    )
