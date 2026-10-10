"""M7a flash 综述（`app.report.narrative`）：报告里**唯一**的推断型正文。

三条纪律：
1. **只依据给定事实**——数字全部由事实层给好，模型不许算、不许编；prompt 里写死这句话，
   输出里出现的任何新数字都属于越界（校验在 builder 的 claim 闸门看住事实块，综述本身
   标注为 inference，不冒充事实）。
2. **超时 + 失败降级**：`ask_once` 抛什么错都翻成「缺席 + 原因」，报告照常生成——
   综述没了是少一段话，不是报告失败。
3. **prompt 版本号进正文**：`prompt_version` 与 `model` 一起落进报告，重放复用已存文本时
   凭它判断「同一份综述」。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel

from app.core.llm import DEFAULT_MODEL, LLMNotConfigured, ask_once

#: prompt 版本：改一个字就升一版（报告里的综述以它为身份，与模型名一起构成缓存键）
PROMPT_VERSION = "m7-narrative-1"

#: 综述长度上限（字）：一段话，不是研报正文——M8 的深度研报才是长篇
MAX_CHARS = 400

_SYSTEM = (
    "你是量化研究助手。只依据用户给出的事实写一段中文综述（不超过 {max_chars} 字），"
    "覆盖：这段时间的策略表现、与基准的对比、信号来源与最值得注意的一件事。"
    "禁止：计算或编造任何未给出的数字、给出投资建议、预测未来行情。"
)


@dataclass(frozen=True, slots=True)
class Narrative:
    """一段综述（或它的缺席）。`text is None` 时 `note` 必给出原因。"""

    text: str | None
    model: str
    prompt_version: str = PROMPT_VERSION
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


#: 事实摘要里的指标 → (中文标签, 格式)。**格式在这里就定好**：直接给模型原始浮点，
#: 它会原样复述（真跑逮出来的 `0.02509640439999994`）——报告里的数字该由我们给，不是它抄。
_METRIC_LINES: tuple[tuple[str, str, str], ...] = (
    ("total_return", "累计收益", "pct"),
    ("benchmark_return", "全市场等权基准", "pct"),
    ("excess_return", "超额收益", "pct"),
    ("max_drawdown", "最大回撤", "pct"),
    ("volatility", "年化波动率", "pct"),
    ("win_rate", "胜率", "pct"),
    ("trade_count", "已平仓回合", "int"),
)


def _fmt(value: Any, kind: str) -> str:
    if kind == "pct":
        return f"{float(value):.2%}"
    return f"{int(value)}"


def narrative_prompt(facts: Mapping[str, Any]) -> str:
    """把事实摘要渲染成 prompt。**纯函数**：同样的输入永远同样的 prompt（可测、可复现）。"""
    lines = [f"【账户】{facts.get('account_name')}（{facts.get('strategy_name')}）",
             f"【区间】{facts.get('start')} → {facts.get('as_of')}"]
    for key, label, kind in _METRIC_LINES:
        value = (facts.get("metrics") or {}).get(key)
        if value is not None:
            lines.append(f"【{label}】{_fmt(value, kind)}")
    lines.append(f"【未平仓】{facts.get('open_trips', 0)} 笔（数据止于 {facts.get('market_end')}）")
    groups: Sequence[Mapping[str, Any]] = facts.get("direction_groups") or []
    if groups:
        rendered = "、".join(f"{g['label']} {g['trips']} 笔" for g in groups)
        lines.append(f"【信号方向分布】{rendered}")
    industries: Sequence[Mapping[str, Any]] = facts.get("industry_groups") or []
    if industries:
        rendered = "、".join(f"{g['label']} {g['trips']} 笔" for g in industries[:5])
        lines.append(f"【驱动事件行业】{rendered}")
    if facts.get("notes"):
        lines.append("【注意事项】" + "；".join(str(n) for n in facts["notes"]))
    body = "\n".join(lines)
    return _SYSTEM.format(max_chars=MAX_CHARS) + "\n\n" + body


async def build_narrative(
    facts: Mapping[str, Any],
    *,
    chat: BaseChatModel | None = None,
    model: str = DEFAULT_MODEL,
    timeout: float = 20.0,
) -> Narrative:
    """生成综述；任何失败都降级为「缺席 + 原因」，不向上抛。"""
    try:
        text = await ask_once(narrative_prompt(facts), model=model, timeout=timeout, chat=chat)
    except LLMNotConfigured as exc:
        return Narrative(None, model, note=f"模型未配置：{exc}")
    except TimeoutError:
        return Narrative(None, model, note=f"综述超时（>{timeout:.0f}s），本次缺席")
    except Exception as exc:  # noqa: BLE001 —— 综述失败不该拖垮报告，原因如实记
        return Narrative(None, model, note=f"综述不可用（{type(exc).__name__}）")

    clean = text.strip()
    if not clean:
        return Narrative(None, model, note="模型返回空文本")
    return Narrative(clean[:MAX_CHARS], model)
