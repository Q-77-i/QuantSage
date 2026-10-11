"""LLM 网关：全项目唯一的模型构造入口（LiteLLM 统一调用）。

两个必须显式处理的坑（都实测过）：
  1. **模型名要带 provider 前缀**：LiteLLM 对裸名 `deepseek-flash` 直接报
     「LLM Provider NOT provided」，必须写 `deepseek/deepseek-flash`；
  2. **密钥从 Settings 显式传入**：pydantic 只把 .env 读进 Settings，**不写进
     os.environ**，指望 litellm 自己读环境变量会拿不到 key。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from langchain_litellm import ChatLiteLLM

from app.core.config import get_settings

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


def build_chat_model(
    model: str = DEFAULT_MODEL, *, temperature: float = 0.0, thinking: bool = True
) -> ChatLiteLLM:
    """构造对话模型。

    temperature 默认 0：投研问答要的是事实稳定而非措辞多样，低温也能减少工具参数漂移。

    `thinking=False` 关掉 reasoning（M8 默认关，实测把单次调用从 8.99s/107.03s 压到
    0.84s/1.48s 且这类摘要解读任务的正文信息不变；见 SPEC §9 M8「模型与思考开关」）。
    """
    api_key = get_settings().deepseek_api_key.get_secret_value()
    if not api_key:
        raise LLMNotConfigured("DEEPSEEK_API_KEY 未在 .env 配置")
    kwargs: dict = {} if thinking else {"model_kwargs": dict(THINKING_OFF)}
    return ChatLiteLLM(model=model, api_key=api_key, temperature=temperature, **kwargs)


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
    """
    import asyncio

    client = chat if chat is not None else build_chat_model(model, thinking=thinking)
    response = await asyncio.wait_for(client.ainvoke(prompt), timeout=timeout)
    usage = getattr(response, "usage_metadata", None)
    return Completion(
        text=text_of(response),
        model=model,
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
