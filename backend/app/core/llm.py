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
