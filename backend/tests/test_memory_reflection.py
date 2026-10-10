"""M7b 一句话反思（`app.memory.reflection`）：**只有已了结的回合才有教训**。

三条纪律与综述同规：只依据给定事实（数字在 prompt 里就格式化好，模型不许算、不许编）、
超时/失败一律降级为「缺席 + 原因」、prompt 版本进正文（反思也进记忆，重放要能认出来）。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from app.memory.reflection import REFLECTION_PROMPT_VERSION, build_reflection, reflection_prompt

FACTS: dict[str, Any] = {
    "symbol": "600519",
    "entry_date": "2026-08-03",
    "exit_date": "2026-08-10",
    "window_days": 6,
    "pnl": 984.50,
    "return_pct": 0.0984008,
    "benchmark_pct": 0.02,
    "alpha_pp": 7.84008,
    "entry_reason": "MA 金叉",
    "exit_reason": "持有到期",
}


def _fake(text: str = "这笔决策吃到了金叉后的主升段，比全市场等权多 7.8 个百分点。") -> Any:
    return FakeMessagesListChatModel(responses=[AIMessage(content=text)])


class _Boom(FakeMessagesListChatModel):
    def _generate(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        raise RuntimeError("上游 500")


class _Slow(FakeMessagesListChatModel):
    async def _agenerate(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        await asyncio.sleep(0.3)
        raise AssertionError("不该走到这里")


def test_prompt_carries_formatted_facts_and_the_no_invention_rule() -> None:
    """数字在 prompt 里就格式化好（真跑教训：原始浮点会被模型原样复述）。"""
    prompt = reflection_prompt(FACTS)

    assert "600519" in prompt and "2026-08-03" in prompt and "2026-08-10" in prompt
    assert "9.84%" in prompt and "2.00%" in prompt and "7.84 pp" in prompt
    assert "0.0984008" not in prompt
    assert "MA 金叉" in prompt and "持有到期" in prompt
    assert "禁止" in prompt and "编造" in prompt
    assert reflection_prompt(FACTS) == prompt


def test_build_reflection_returns_text_with_identity() -> None:
    reflection = asyncio.run(build_reflection(FACTS, chat=_fake(), model="deepseek/deepseek-flash"))

    assert reflection.available
    assert reflection.text is not None and reflection.text.startswith("这笔决策")
    assert reflection.model == "deepseek/deepseek-flash"
    assert reflection.prompt_version == REFLECTION_PROMPT_VERSION
    assert reflection.note is None
    assert set(reflection.to_payload()) == {"text", "model", "prompt_version", "note"}


def test_build_reflection_degrades_on_error_and_timeout() -> None:
    """上游挂了 / 超时：缺席 + 原因，绝不向上抛（结算结果照常落 Store）。"""
    broken = asyncio.run(build_reflection(FACTS, chat=_Boom(responses=[])))
    assert not broken.available and broken.note and "RuntimeError" in broken.note

    timed = asyncio.run(build_reflection(FACTS, chat=_Slow(responses=[]), timeout=0.05))
    assert not timed.available and timed.note and "超时" in timed.note


def test_empty_text_is_a_degradation_not_a_silent_success() -> None:
    empty = asyncio.run(build_reflection(FACTS, chat=_fake("   ")))

    assert not empty.available
    assert empty.note is not None


@pytest.mark.parametrize("missing", ["benchmark_pct", "exit_reason"])
def test_prompt_tolerates_missing_sections(missing: str) -> None:
    """基准取不到 / 没有出场理由时 prompt 照常成立（缺的字段不写那一行）。"""
    facts = {key: value for key, value in FACTS.items() if key != missing}

    assert "600519" in reflection_prompt(facts)
