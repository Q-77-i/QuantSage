"""M8b 风控闸门（`app.agent.deep.risk_gate`）：三类检查，**纯函数，LLM 不可决定合规**。

每条规则都要有**命中与放行成对样例**（M5a 的纪律：单侧的拦截用例证明不了不误伤）。
闸门自己**从不抛异常**，也从不调用模型——它是这条流水线上唯一「解释权不在模型手里」的一段。
"""

from __future__ import annotations

from app.agent.deep.budget import Budget, Limits
from app.agent.deep.risk_gate import (
    FORBIDDEN_PHRASES,
    GateDecision,
    check_budget,
    check_forbidden,
    check_numbers,
    evaluate,
)
from app.agent.deep.synthesize import Draft

ALLOWED = {"12.34%", "3.2亿元", "600519"}


def draft(text: str, citations: tuple[str, ...] = ()) -> Draft:
    return Draft(conclusion=text, citations=citations, model="deepseek/deepseek-v4-pro")


# ── ① 数字可追溯 ──


def test_check_numbers_passes_traceable_text() -> None:
    assert check_numbers("区间收益 12.34%，成交额 3.2亿元。", ALLOWED) == ()


def test_check_numbers_flags_fabricated_number() -> None:
    assert check_numbers("区间收益 12.34%，另有 45% 上行空间。", ALLOWED) == ("45%",)


# ── ② 绝对化 / 买卖指令（命中与放行成对）──


def test_check_forbidden_flags_absolute_wording() -> None:
    hits = check_forbidden("该股必涨，属于稳赚的机会。")
    assert hits and hits[0].rule == "绝对化表述"


def test_check_forbidden_flags_trading_instruction() -> None:
    hits = check_forbidden("操作上建议买入并持有。")
    assert hits and hits[0].rule == "买卖指令"


def test_check_forbidden_passes_the_disclaimer_and_neutral_wording() -> None:
    """**放行样例**：本项目的标准免责句里就有「投资建议」四个字，它不该被拦。"""
    clean = (
        "本报告仅供研究用途，不构成投资建议。\n"
        "多方认为估值已回到历史中枢，空方担心减持压制，双方的分歧集中在需求端。"
    )
    assert check_forbidden(clean) == ()


def test_forbidden_table_is_labelled() -> None:
    assert all(pattern and label for pattern, label in FORBIDDEN_PHRASES)


# ── ③ 预算 ──


def test_check_budget_returns_none_below_limits() -> None:
    assert check_budget(Budget()) is None


def test_check_budget_names_the_first_broken_counter() -> None:
    budget = Budget(limits=Limits(llm_calls=1, tokens=10**9, seconds=99.0, cost_usd=9.0))
    budget.record_llm("deepseek/deepseek-flash", {"input_tokens": 1, "output_tokens": 1})
    assert check_budget(budget) == "llm_calls"


# ── 汇总：三类各自的处置 ──


def test_evaluate_passes_a_clean_draft() -> None:
    result = evaluate(draft("区间收益 12.34%。"), allowed=ALLOWED, budget=Budget(), rewrite_used=False)

    assert result.decision is GateDecision.PASS and result.approval is None


def test_evaluate_asks_for_one_rewrite_on_fabricated_numbers() -> None:
    result = evaluate(draft("有 45% 上行空间"), allowed=ALLOWED, budget=Budget(), rewrite_used=False)

    assert result.decision is GateDecision.REWRITE and "45%" in result.reason


def test_evaluate_degrades_instead_of_rewriting_twice() -> None:
    """重写只给一次——第二次还不过就降级标注，不无限重写（每次重写都是一次真花钱的调用）。"""
    result = evaluate(draft("有 45% 上行空间"), allowed=ALLOWED, budget=Budget(), rewrite_used=True)

    assert result.decision is GateDecision.DEGRADE and "45%" in result.reason


def test_evaluate_requests_approval_on_forbidden_wording() -> None:
    result = evaluate(draft("建议买入。"), allowed=ALLOWED, budget=Budget(), rewrite_used=False)

    assert result.decision is GateDecision.APPROVAL
    assert result.approval and result.approval["kind"] == "wording"
    assert result.approval["options"] == ["continue", "stop"]


def test_evaluate_requests_approval_when_budget_is_exceeded() -> None:
    budget = Budget(limits=Limits(llm_calls=0, tokens=10**9, seconds=99.0, cost_usd=9.0))

    result = evaluate(draft("区间收益 12.34%。"), allowed=ALLOWED, budget=budget, rewrite_used=False)

    assert result.decision is GateDecision.APPROVAL
    assert result.approval and result.approval["kind"] == "budget"


def test_budget_check_wins_over_wording_and_numbers() -> None:
    """预算超限优先——已经没钱了，再去重写或问措辞都是错的决定顺序。"""
    budget = Budget(limits=Limits(llm_calls=0, tokens=10**9, seconds=99.0, cost_usd=9.0))

    result = evaluate(draft("建议买入，有 45% 上行空间"), allowed=ALLOWED, budget=budget, rewrite_used=False)

    assert result.decision is GateDecision.APPROVAL and result.approval["kind"] == "budget"


def test_decision_payload_is_json_safe() -> None:
    payload = evaluate(draft("建议买入。"), allowed=ALLOWED, budget=Budget(), rewrite_used=False).to_payload()

    assert payload["decision"] == "approval" and isinstance(payload["hits"], list)
