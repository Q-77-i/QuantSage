"""一句话教训（M7b）：**只有已了结的回合才反思**——教训要从结果里长出来。

与 `report/narrative.py` 同一套纪律（超时、降级、prompt 版本进正文），差别只有两处：
输入是**单笔决策**的结果而不是整份报告；输出是一句话（≤120 字）的教训，不是综述。
未到期（期末仍未平仓）的回合**不给反思**：那时还没有结果可总结。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel

from app.core.llm import DEFAULT_MODEL, LLMNotConfigured, ask_once

#: prompt 版本：改一个字就升一版（记忆里以它为身份，配合模型名判断「同一段反思」）
REFLECTION_PROMPT_VERSION = "m7-reflection-1"

#: 反思长度上限（字）：一句话教训，不是复盘长文
MAX_CHARS = 120

_SYSTEM = (
    "你是量化研究助手。下面是一笔**已了结**的模拟盘买入决策的结果。"
    "写一句中文教训（不超过 {max_chars} 字）：这笔决策相对同期全市场等权基准如何，"
    "下次遇到同类信号该注意什么。禁止：计算或编造未给出的数字、给出投资建议、预测未来行情。"
)


@dataclass(frozen=True, slots=True)
class Reflection:
    """一句话教训（或它的缺席）。`text is None` 时 `note` 必给出原因。"""

    text: str | None
    model: str
    prompt_version: str = REFLECTION_PROMPT_VERSION
    note: str | None = None

    @property
    def available(self) -> bool:
        return self.text is not None

    def to_payload(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "note": self.note,
        }


def _pct(value: Any) -> str:
    return f"{float(value):.2%}"


def reflection_prompt(facts: Mapping[str, Any]) -> str:
    """把单笔决策的结果渲染成 prompt。**纯函数**：同样的输入永远同样的 prompt。"""
    lines = [
        f"【标的】{facts.get('symbol')}",
        f"【持有窗口】{facts.get('entry_date')} → {facts.get('exit_date')}"
        f"（{facts.get('window_days')} 个交易日）",
    ]
    if facts.get("entry_reason"):
        lines.append(f"【买入理由】{facts['entry_reason']}")
    if facts.get("exit_reason"):
        lines.append(f"【卖出理由】{facts['exit_reason']}")
    if facts.get("pnl") is not None:
        lines.append(f"【盈亏】{float(facts['pnl']):,.2f} 元")
    if facts.get("return_pct") is not None:
        lines.append(f"【回合收益】{_pct(facts['return_pct'])}")
    if facts.get("benchmark_pct") is not None:
        lines.append(f"【同期全市场等权】{_pct(facts['benchmark_pct'])}")
    if facts.get("alpha_pp") is not None:
        lines.append(f"【超额】{float(facts['alpha_pp']):.2f} pp")
    return _SYSTEM.format(max_chars=MAX_CHARS) + "\n\n" + "\n".join(lines)


async def build_reflection(
    facts: Mapping[str, Any],
    *,
    chat: BaseChatModel | None = None,
    model: str = DEFAULT_MODEL,
    timeout: float = 20.0,
) -> Reflection:
    """生成一句话教训；任何失败都降级为「缺席 + 原因」，不向上抛。"""
    try:
        text = await ask_once(reflection_prompt(facts), model=model, timeout=timeout, chat=chat)
    except LLMNotConfigured as exc:
        return Reflection(None, model, note=f"模型未配置：{exc}")
    except TimeoutError:
        return Reflection(None, model, note=f"反思超时（>{timeout:.0f}s），本次缺席")
    except Exception as exc:  # noqa: BLE001 —— 反思失败不该拖垮结算，原因如实记
        return Reflection(None, model, note=f"反思不可用（{type(exc).__name__}）")

    clean = text.strip()
    if not clean:
        return Reflection(None, model, note="模型返回空文本")
    return Reflection(clean[:MAX_CHARS], model)
