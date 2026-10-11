"""M8a 预算四计数器（`app.agent.deep.budget`）。

判据三条：**reasoning 计入输出**（`output_tokens` 本身就含它，不能加两次）、
**价目表按模型查**（未知模型不猜价，记 0）、**到限报第一个被突破的计数器**。
"""

from __future__ import annotations

from app.agent.deep.budget import Budget, Limits, estimate_cost_usd

FLASH_USAGE = {"input_tokens": 1000, "output_tokens": 500, "output_token_details": {"reasoning": 400}}
PRO_USAGE = {"input_tokens": 1000, "output_tokens": 500}


def test_cost_uses_local_price_table_and_counts_reasoning_as_output() -> None:
    # flash：1000×0.30e-6 + 500×1.20e-6（reasoning 400 已在 output 里，不再加）
    assert estimate_cost_usd("deepseek/deepseek-flash", FLASH_USAGE) == 1000 * 0.30e-6 + 500 * 1.20e-6
    assert estimate_cost_usd("deepseek/deepseek-v4-pro", PRO_USAGE) == 1000 * 1.32e-6 + 500 * 3.96e-6


def test_unknown_model_is_not_priced() -> None:
    """未知模型记 0 —— **宁可少算，不猜价**（猜出来的成本会污染预算判断）。"""
    assert estimate_cost_usd("some/other-model", PRO_USAGE) == 0.0
    assert estimate_cost_usd("deepseek/deepseek-flash", None) == 0.0


def test_record_accumulates_and_wait_counts_toward_elapsed() -> None:
    budget = Budget()
    budget.record_llm("deepseek/deepseek-flash", FLASH_USAGE, seconds=1.5)
    budget.record_wait(2.0)

    assert budget.llm_calls == 1
    assert (budget.tokens_in, budget.tokens_out, budget.tokens) == (1000, 500, 1500)
    assert budget.elapsed_s == 3.5
    assert budget.cost_usd > 0


def test_exceeded_reports_first_broken_counter_in_fixed_order() -> None:
    budget = Budget(limits=Limits(llm_calls=2, tokens=10_000, seconds=60.0, cost_usd=1.0))
    assert budget.exceeded() is None

    budget.record_llm("deepseek/deepseek-flash", FLASH_USAGE)
    assert budget.exceeded() is None
    budget.record_llm("deepseek/deepseek-flash", FLASH_USAGE)

    # 调用数先到限，即便 token 还没到，也报 llm_calls（顺序固定，便于断言与排障）
    assert budget.exceeded() == "llm_calls"


def test_exceeded_covers_each_counter() -> None:
    tokens = Budget(limits=Limits(llm_calls=99, tokens=100, seconds=60.0, cost_usd=9.0))
    tokens.record_llm("deepseek/deepseek-flash", FLASH_USAGE)
    assert tokens.exceeded() == "tokens"

    seconds = Budget(limits=Limits(llm_calls=99, tokens=10**9, seconds=1.0, cost_usd=9.0))
    seconds.record_wait(1.2)
    assert seconds.exceeded() == "seconds"

    cost = Budget(limits=Limits(llm_calls=99, tokens=10**9, seconds=60.0, cost_usd=0.001))
    cost.record_llm("deepseek/deepseek-v4-pro", {"input_tokens": 0, "output_tokens": 1000})
    assert cost.exceeded() == "cost_usd"


def test_payload_shape_is_json_safe() -> None:
    budget = Budget()
    budget.record_llm("deepseek/deepseek-flash", FLASH_USAGE, seconds=1.0)

    payload = budget.to_payload()

    assert payload["tokens"] == 1500 and payload["limits"]["llm_calls"] == 40
    assert payload["exceeded"] is None
    assert isinstance(payload["cost_usd"], float)
