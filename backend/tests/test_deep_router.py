"""M8a 意图路由（`app.agent.deep.router`）：快路径 ⇄ 深路径。

两条纪律的用例都在这里：**失败一律回落 quick**（解析失败 / 模型报错 / 超时三种坏法），
**标的名解析不花 LLM**（六位码走正则、全名精确、简称包含匹配、长名压短名）。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from app.agent.deep.router import (
    PATH_DEEP,
    PATH_QUICK,
    extract_json,
    resolve_symbols,
    route_question,
)

INDEX = {"贵州茅台": "600519", "宁德时代": "300750", "中国平安": "601318", "平安银行": "000001"}


def _fake(text: str) -> Any:
    return FakeMessagesListChatModel(responses=[AIMessage(content=text)])


class _Boom(FakeMessagesListChatModel):
    async def _agenerate(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        raise RuntimeError("上游 500")


class _Slow(FakeMessagesListChatModel):
    async def _agenerate(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        await asyncio.sleep(0.5)
        raise AssertionError("不该走到这里")


# ── 名字解析（不花 LLM，F7 的落地）──


def test_six_digit_code_is_taken_verbatim() -> None:
    symbols, note = resolve_symbols("600519 现在能不能买？", index=INDEX)
    assert symbols == ("600519",) and note is None


def test_full_name_exact_match() -> None:
    symbols, _ = resolve_symbols("贵州茅台最近有什么消息", index=INDEX)
    assert symbols == ("600519",)


def test_short_name_contained_in_text_matches() -> None:
    """「茅台」是简称，字典里没有它——靠包含匹配命中（F7 实测：唯一命中 600519）。"""
    index = {**INDEX, "贵州茅台": "600519"}
    symbols, _ = resolve_symbols("茅台还值得买吗", index=index)
    assert symbols == ("600519",)


def test_longest_name_wins_over_shorter_one() -> None:
    """「中国平安」命中长名后，短名「平安」不该再把「平安银行」也拉进来。"""
    index = {**INDEX, "平安": "000001"}
    symbols, _ = resolve_symbols("中国平安的基本面", index=index)
    assert symbols == ("601318",)


def test_company_name_inside_a_common_phrase_is_reported_not_hidden() -> None:
    """真实字典逮到的：A 股有一家公司叫**值得买**（300785）。

    「茅台还值得买吗」里「值不值得买」的中间三个字就是它的全名——两个候选都要如实列出来，
    **不能把用户真正问的那只丢掉**（真实字典初测把这条问题解析成了 300785）。
    """
    index = {"贵州茅台": "600519", "值得买": "300785"}
    symbols, note = resolve_symbols("茅台还值得买吗", index=index)

    assert symbols == ("600519", "300785")  # 长名优先
    assert note is not None and "候选" in note


def test_code_present_suppresses_short_fragment_noise() -> None:
    """代码锚定后不再吃 2 字片段——真实字典实测「事件**驱动**机会」会拉进捷昌驱动/驱动力。"""
    index = {"捷昌驱动": "603583", "驱动力": "920275", "宁德时代": "300750"}
    assert resolve_symbols("帮我看下 600519 的事件驱动机会", index=index) == (("600519",), None)
    # 长片段仍然认：同时问代码与全名时，两只都要留着
    symbols, _ = resolve_symbols("600519 和宁德时代对比", index=index)
    assert symbols == ("600519", "300750")


def test_ambiguous_fragment_is_not_guessed() -> None:
    """只写了「银行」这种通用词时命中一大片——**不猜**，如实标注请用户说清楚。"""
    index = {"招商银行": "600036", "平安银行": "000001", "兴业银行": "601166", "民生银行": "600016"}
    symbols, note = resolve_symbols("银行板块怎么样", index=index)
    assert symbols == () and note is not None and "歧义" in note


def test_no_symbol_found_is_reported_not_invented() -> None:
    symbols, note = resolve_symbols("最近大盘怎么样", index=INDEX)
    assert symbols == () and note is not None


def test_duplicate_codes_are_deduped_in_order() -> None:
    symbols, _ = resolve_symbols("先看 300750，再看贵州茅台，最后还是 300750", index=INDEX)
    assert symbols == ("300750", "600519")


# ── 结构化输出解析 ──


@pytest.mark.parametrize(
    "text",
    [
        '{"path": "deep", "reason": "需要多角度取证"}',
        '```json\n{"path": "deep", "reason": "需要多角度取证"}\n```',
        '好的，我的判断是：{"path": "deep", "reason": "需要多角度取证"}（完毕）',
    ],
)
def test_extract_json_tolerates_fences_and_prose(text: str) -> None:
    assert extract_json(text) == {"path": "deep", "reason": "需要多角度取证"}


def test_extract_json_returns_none_on_garbage() -> None:
    assert extract_json("我不确定") is None


# ── 路由决策 ──


def test_deep_decision_carries_reason_and_symbols() -> None:
    decision = asyncio.run(
        route_question(
            "分析一下贵州茅台值不值得买",
            chat=_fake('{"path": "deep", "reason": "需要多角度取证", "scope": "事件驱动"}'),
            index=INDEX,
        )
    )
    assert decision.path == PATH_DEEP
    assert decision.symbols == ("600519",)
    assert decision.scope == "事件驱动"
    assert "多角度" in decision.reason


def test_bad_json_falls_back_to_quick() -> None:
    decision = asyncio.run(route_question("你好", chat=_fake("我不确定"), index=INDEX))
    assert decision.path == PATH_QUICK
    assert "解析" in decision.reason


def test_unknown_path_value_falls_back_to_quick() -> None:
    decision = asyncio.run(
        route_question("你好", chat=_fake('{"path": "medium"}'), index=INDEX)
    )
    assert decision.path == PATH_QUICK


def test_model_error_falls_back_to_quick() -> None:
    decision = asyncio.run(route_question("你好", chat=_Boom(responses=[]), index=INDEX))
    assert decision.path == PATH_QUICK and "RuntimeError" in decision.reason


def test_timeout_falls_back_to_quick() -> None:
    decision = asyncio.run(
        route_question("你好", chat=_Slow(responses=[]), timeout=0.05, index=INDEX)
    )
    assert decision.path == PATH_QUICK and "超时" in decision.reason


def test_explicit_force_skips_the_model_call() -> None:
    """显式 `mode=deep` 不花这一次分类调用（模型是「一调就炸」的替身，走不到它才算数）。"""
    decision = asyncio.run(
        route_question("随便问问", chat=_Boom(responses=[]), force=PATH_DEEP, index=INDEX)
    )
    assert decision.path == PATH_DEEP and "指定" in decision.reason


def test_quick_decision_has_no_side_effects() -> None:
    decision = asyncio.run(
        route_question("茅台今天涨了吗", chat=_fake('{"path": "quick", "reason": "事实查询"}'), index=INDEX)
    )
    assert decision.path == PATH_QUICK and decision.symbols == ("600519",)
