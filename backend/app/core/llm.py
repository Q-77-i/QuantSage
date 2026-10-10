"""LLM 网关：全项目唯一的模型构造入口（LiteLLM 统一调用）。

两个必须显式处理的坑（都实测过）：
  1. **模型名要带 provider 前缀**：LiteLLM 对裸名 `deepseek-flash` 直接报
     「LLM Provider NOT provided」，必须写 `deepseek/deepseek-flash`；
  2. **密钥从 Settings 显式传入**：pydantic 只把 .env 读进 Settings，**不写进
     os.environ**，指望 litellm 自己读环境变量会拿不到 key。
"""

from __future__ import annotations

from langchain_litellm import ChatLiteLLM

from app.core.config import get_settings

# provider 前缀是 LiteLLM 的路由依据（deepseek/ → api.deepseek.com/beta），换供应商只改这里
DEFAULT_MODEL = "deepseek/deepseek-flash"


class LLMNotConfigured(RuntimeError):
    """DEEPSEEK_API_KEY 未在 .env 配置。"""


def build_chat_model(
    model: str = DEFAULT_MODEL, *, temperature: float = 0.0
) -> ChatLiteLLM:
    """构造对话模型。

    temperature 默认 0：投研问答要的是事实稳定而非措辞多样，低温也能减少工具参数漂移。
    """
    api_key = get_settings().deepseek_api_key.get_secret_value()
    if not api_key:
        raise LLMNotConfigured("DEEPSEEK_API_KEY 未在 .env 配置")
    return ChatLiteLLM(model=model, api_key=api_key, temperature=temperature)


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


async def ask_once(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    timeout: float = 20.0,
    chat: ChatLiteLLM | None = None,
) -> str:
    """一句话问答（M7 的综述与反思用）。**超时即抛 `TimeoutError`，其余异常原样抛**——
    降级由调用方决定（报告与结算各有自己的缺失表述），这里只负责「不无限等」。

    `chat` 供离线测试注入假模型（不传则现造一个真模型）；超时用 `asyncio.wait_for`，
    底层 HTTP 请求随之取消，不会留一个跑飞的调用。
    """
    import asyncio

    client = chat if chat is not None else build_chat_model(model)
    response = await asyncio.wait_for(client.ainvoke(prompt), timeout=timeout)
    return text_of(response)
