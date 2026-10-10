"""M7a 综述（`app.report.narrative`）：prompt 是纯函数、失败一律降级。

综述是报告里唯一的推断型正文；它的**缺席**必须是结构化的（`text=None` + 原因），
而不是让报告生成失败——这条在 `test_build_narrative_degrades_*` 里钉死。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from app.report.narrative import PROMPT_VERSION, build_narrative, narrative_prompt

FACTS: dict[str, Any] = {
    "account_name": "银行事件驱动",
    "strategy_name": "event_driven",
    "start": "2026-07-02",
    "as_of": "2026-09-30",
    "metrics": {
        "total_return": 0.021,
        "benchmark_return": -0.019,
        "excess_return": 0.04,
        "max_drawdown": 0.05,
        "win_rate": 0.4,
        "trade_count": 5,
    },
    "open_trips": 9,
    "market_end": "2026-09-30",
    "direction_groups": [{"label": "利多", "trips": 14}],
    "industry_groups": [{"label": "银行", "trips": 3}],
    "notes": ["样本 60 个交易日"],
}


def _fake(text: str = "本期间策略累计收益 2.1%，跑赢全市场等权基准。") -> Any:
    return FakeMessagesListChatModel(responses=[AIMessage(content=text)])


class _Boom(FakeMessagesListChatModel):
    def _generate(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        raise RuntimeError("上游 500")


class _Slow(FakeMessagesListChatModel):
    async def _agenerate(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        await asyncio.sleep(0.3)
        raise AssertionError("不该走到这里")


def test_prompt_carries_facts_and_the_no_invention_rule() -> None:
    """prompt 带上关键事实与「不许算、不许编」三条禁令（同一个事实输入 → 同一段 prompt）。

    **数字在 prompt 里就格式化好**：真跑时模型把原始浮点原样复述
    （`0.02509640439999994`），报告里的数字该由事实层给。
    """
    prompt = narrative_prompt(FACTS)

    assert "银行事件驱动" in prompt and "event_driven" in prompt
    assert "2.10%" in prompt  # 0.021 已格式化为百分数
    assert "0.02509640439" not in prompt
    assert "-1.90%" in prompt  # benchmark_return = -0.019
    assert "5" in prompt and "已平仓回合" in prompt
    assert "利多 14 笔" in prompt and "银行 3 笔" in prompt
    assert "未平仓" in prompt and "2026-09-30" in prompt
    assert "禁止" in prompt and "编造" in prompt
    assert narrative_prompt(FACTS) == prompt


def test_build_narrative_returns_text_with_model_identity() -> None:
    """正常路径：正文 + 模型名 + prompt 版本一起进报告（重放复用凭它们判断同一份）。"""
    narrative = asyncio.run(build_narrative(FACTS, chat=_fake(), model="deepseek/deepseek-flash"))

    assert narrative.available
    assert narrative.text is not None and narrative.text.startswith("本期间策略")
    assert narrative.model == "deepseek/deepseek-flash"
    assert narrative.prompt_version == PROMPT_VERSION
    assert narrative.note is None


def test_build_narrative_degrades_on_upstream_error() -> None:
    """上游报错：缺席 + 原因，不向上抛（报告照常生成）。"""
    narrative = asyncio.run(build_narrative(FACTS, chat=_Boom(responses=[])))

    assert not narrative.available
    assert narrative.text is None
    assert narrative.note is not None and "RuntimeError" in narrative.note


def test_build_narrative_degrades_on_timeout() -> None:
    """超时：缺席 + 原因里带阈值（默认 20s，测试给 0.05s）。"""
    narrative = asyncio.run(build_narrative(FACTS, chat=_Slow(responses=[]), timeout=0.05))

    assert not narrative.available
    assert narrative.note is not None and "超时" in narrative.note


def test_empty_model_text_is_also_a_degradation() -> None:
    """模型返回空文本同样是缺席——不能把空串当综述塞进报告。"""
    narrative = asyncio.run(build_narrative(FACTS, chat=_fake("   ")))

    assert not narrative.available
    assert narrative.note is not None


def test_narrative_payload_is_json_ready() -> None:
    payload = asyncio.run(build_narrative(FACTS, chat=_fake())).to_payload()

    assert set(payload) == {"text", "model", "prompt_version", "note"}
    assert isinstance(payload["text"], str)


@pytest.mark.parametrize("missing", ["metrics", "direction_groups"])
def test_prompt_tolerates_missing_sections(missing: str) -> None:
    """事实摘要缺块（如没有事件来源）时 prompt 照常成立，不 KeyError。"""
    facts = {k: v for k, v in FACTS.items() if k != missing}

    assert "【账户】" in narrative_prompt(facts)
