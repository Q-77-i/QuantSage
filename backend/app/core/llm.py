"""LLM 网关：全项目唯一的模型构造入口（LiteLLM 统一调用）。

两个必须显式处理的坑（都实测过）：
  1. **模型名要带 provider 前缀**：LiteLLM 对裸名 `deepseek-flash` 直接报
     「LLM Provider NOT provided」，必须写 `deepseek/deepseek-flash`；
  2. **密钥从 Settings 显式传入**：pydantic 只把 .env 读进 Settings，**不写进
     os.environ**，指望 litellm 自己读环境变量会拿不到 key。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from langchain_litellm import ChatLiteLLM

from app.core import reliability
from app.core.config import get_settings

log = logging.getLogger(__name__)

# provider 前缀是 LiteLLM 的路由依据（deepseek/ → api.deepseek.com/beta），换供应商只改这里
DEFAULT_MODEL = "deepseek/deepseek-flash"

#: 深度档（M8 的辩论与综合）。**与 flash 的差别是模型能力，不是思考档**——见 `thinking` 参数。
DEEP_MODEL = "deepseek/deepseek-v4-pro"

#: 关思考的传参形状。三条通路实测都能关（`reasoning_effort=none` 0.81s / `thinking={"type":"disabled"}`
#: 0.89s / `extra_body` 1.12s），取第一条（LiteLLM 原生参数，少一层嵌套）。
#: ⚠ `reasoning_effort="minimal"` **不生效**（实测 12.78s / 2,973 tokens）——别拿它当「少想一点」。
THINKING_OFF = {"reasoning_effort": "none"}


class LLMNotConfigured(RuntimeError):
    """DEEPSEEK_API_KEY 未在 .env 配置。"""


#: chat 主链路的请求超时。**此前这条路径完全没有超时**——上游挂死就是 SSE 挂死
#: （`complete()` 那条有 `asyncio.wait_for` 兜着，chat 走的是图，没人兜）。
CHAT_TIMEOUT = 60.0

#: 降级链：**单向**（SPEC §5.2）。反向升档不做——那是把故障掩盖成「更贵的成功」，
#: 且会污染预算口径。
FALLBACK_MODEL: dict[str, str] = {DEEP_MODEL: DEFAULT_MODEL}


def dep_name(model: str) -> str:
    """熔断依赖键。**按档位分**：降级链要能绕过已经熔断的那一档（SPEC §5.3）。"""
    return f"llm:{model.rsplit('/', 1)[-1]}"


def _with_fallback(model: str, exc: BaseException) -> str | None:
    """失败之后该不该换档？返回要换到的模型，或 None（不换）。

    这里问的问题与重试层不同：**「换个档还能不能成」**。所以熔断打开（`CircuitOpen`）
    恰恰是必须换档的情形——那一档此刻已知不可用（SPEC §5.3）；而 `permanent`
    （400 这类）换档也是同样的错，不浪费调用。
    """
    if not isinstance(exc, reliability.CircuitOpen) and reliability.classify(exc) == "permanent":
        return None
    fallback = FALLBACK_MODEL.get(model)
    if fallback is None:
        return None  # flash 已经是兜底档
    log.warning("「%s」不可用（%s：%s），降级到「%s」", model, type(exc).__name__, exc, fallback)
    return fallback


class PolicyChatModel(ChatLiteLLM):
    """把失败语义收口成一条策略的 `ChatLiteLLM`（E4a，SPEC §5.2）。

    **为什么只重写这两个方法就够**：实测四条执行路径——`_generate` / `_stream` /
    `_astream` / `_agenerate`——**全部经由** `completion_with_retry` /
    `acompletion_with_retry`。于是 chat 流式、工具调用、深路径一并受管，
    包括 `create_agent` 那条我们够不着节点的路。

    换档**改的是 `kwargs["model"]`，不碰实例状态**：chat 的模型实例在 lifespan 建一次、
    跨请求共享，改 `self.model` 会并发污染。

    ⚠ 重写的是**包内方法**（非 LangChain 接口契约）。包版本已精确 pin
    （`langchain-litellm==0.11.0`），**升级该包时必须复核这两个方法仍在**（SPEC §5.8）。
    """

    #: 库自带那层 tenacity 显式关掉：它的 `stop_after_attempt(max_retries)` 默认 1
    #: ⇒ 1 次尝试 = 压根不重试，且等待固定 4–10s、不读 `Retry-After`。
    #: 策略归我们一处，别让它半开半闭地叠在下面（双重重试 + 重试不进预算）。
    max_retries: int = 0

    #: 见 `CHAT_TIMEOUT`——chat 主链路唯一的超时兜底
    request_timeout: float | tuple[float, float] | None = CHAT_TIMEOUT

    #: 「这次**实际**用的是哪个模型」。降级链换过档时它不等于请求的那个——
    #: 用回调而不是实例属性：chat 的模型实例在 lifespan 建一次、跨请求共享，
    #: 写实例状态会串（`complete()` 是自己现造实例，所以回调也够精确）。
    on_model_used: Callable[[str], None] | None = None

    def _report_used(self, model: str) -> None:
        if self.on_model_used is not None:
            self.on_model_used(model)

    async def acompletion_with_retry(self, run_manager: Any = None, **kwargs: Any) -> Any:
        parent = super()
        model = str(kwargs.get("model") or self.model)

        async def attempt(which: str) -> Any:
            return await reliability.guard(
                dep_name(which),
                lambda: parent.acompletion_with_retry(
                    run_manager=run_manager, **{**kwargs, "model": which}
                ),
            )

        used = model
        try:
            result = await attempt(model)
        except Exception as exc:  # CancelledError 是 BaseException，不会落进来
            fallback = _with_fallback(model, exc)
            if fallback is None:
                raise
            used = fallback
            result = await attempt(fallback)
        self._report_used(used)
        return result

    def completion_with_retry(self, run_manager: Any = None, **kwargs: Any) -> Any:
        parent = super()
        model = str(kwargs.get("model") or self.model)

        def attempt(which: str) -> Any:
            return reliability.guard_sync(
                dep_name(which),
                lambda: parent.completion_with_retry(
                    run_manager=run_manager, **{**kwargs, "model": which}
                ),
            )

        used = model
        try:
            result = attempt(model)
        except Exception as exc:
            fallback = _with_fallback(model, exc)
            if fallback is None:
                raise
            used = fallback
            result = attempt(fallback)
        self._report_used(used)
        return result


def build_chat_model(
    model: str = DEFAULT_MODEL,
    *,
    temperature: float = 0.0,
    thinking: bool = True,
    on_model_used: Callable[[str], None] | None = None,
) -> ChatLiteLLM:
    """构造对话模型（E4a 起返回 `PolicyChatModel`：重试 / 熔断 / 降级链随行）。

    temperature 默认 0：投研问答要的是事实稳定而非措辞多样，低温也能减少工具参数漂移。

    `thinking=False` 关掉 reasoning（M8 默认关，实测把单次调用从 8.99s/107.03s 压到
    0.84s/1.48s 且这类摘要解读任务的正文信息不变；见 SPEC §9 M8「模型与思考开关」）。
    """
    settings = get_settings()
    api_key = settings.deepseek_api_key.get_secret_value()
    if not api_key:
        raise LLMNotConfigured("DEEPSEEK_API_KEY 未在 .env 配置")
    kwargs: dict = {} if thinking else {"model_kwargs": dict(THINKING_OFF)}
    # 上游地址只在配了的时候传：空串会被 LiteLLM 当成一个真的 base（而不是「没配」）
    if settings.deepseek_api_base:
        kwargs["api_base"] = settings.deepseek_api_base
    return PolicyChatModel(
        model=model,
        api_key=api_key,
        temperature=temperature,
        on_model_used=on_model_used,
        **kwargs,
    )


@dataclass(frozen=True, slots=True)
class Completion:
    """一次调用的结果：正文 + 身份 + 用量。`usage` 是 `usage_metadata` 的原样副本（可能为空）。"""

    text: str
    model: str
    usage: dict = field(default_factory=dict)

    @property
    def output_tokens(self) -> int:
        return int(self.usage.get("output_tokens") or 0)

    @property
    def reasoning_tokens(self) -> int:
        details = self.usage.get("output_token_details") or {}
        return int(details.get("reasoning") or 0)


def text_of(response: object) -> str:
    """取消息正文。兼容 content 为内容块列表的形态（与 `scripts/build_rag_eval.py` 同规）。"""
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(block.get("text", "")) if isinstance(block, dict) else str(block)
            for block in content
        )
    return str(content)


async def complete(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    timeout: float = 20.0,
    chat: ChatLiteLLM | None = None,
    thinking: bool = True,
) -> Completion:
    """一次调用，连正文带用量一起回（M8 的取证/辩论/综合用）。

    **超时即抛 `TimeoutError`，其余异常原样抛**——降级由调用方决定，这里只负责「不无限等」。
    `chat` 供离线测试注入假模型（不传则按 `thinking` 现造一个真模型）；超时走
    `asyncio.wait_for`，底层 HTTP 请求随之取消，不会留一个跑飞的调用。

    **`Completion.model` 是实际用的那个模型**（不是 `model` 参数）——降级链换过档时两者不同，
    报告里那句「哪个模型产出的」据此才不是假话（SPEC §5.2）。回调而非 contextvar：
    `wait_for` 会把调用放进子任务，contextvar 的写入传不回父上下文（实测过的坑）。
    """
    import asyncio

    used: list[str] = []
    if chat is None:
        client = build_chat_model(model, thinking=thinking, on_model_used=used.append)
    else:
        client = chat
        if isinstance(client, PolicyChatModel):
            # 注入进来的**真**模型（用例）也把实际模型报回来。注入物归调用方所有，
            # 且这条分支在生产路径上不会走到（`chat` 只为离线测试而存在）。
            client.on_model_used = used.append
    response = await asyncio.wait_for(client.ainvoke(prompt), timeout=timeout)
    usage = getattr(response, "usage_metadata", None)
    return Completion(
        text=text_of(response),
        model=used[0] if used else model,
        usage=dict(usage) if isinstance(usage, dict) else {},
    )


async def ask_once(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    timeout: float = 20.0,
    chat: ChatLiteLLM | None = None,
) -> str:
    """一句话问答（M7 的综述与反思用）：只要正文，其余同 `complete`（**行为保持一字不变**）。"""
    result = await complete(prompt, model=model, timeout=timeout, chat=chat)
    return result.text
