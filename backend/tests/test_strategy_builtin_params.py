"""M4a 内置策略参数校验：未知键 + 类型 + 值域。

关闭待办「后端不校验策略参数的值」——规则与 T6c 前端表单同源，**不新增限制**：
前端能发出来的请求，后端一律接受（边界值单独有用例守着）。
"""

from __future__ import annotations

import pytest

from app.backtest.strategies import available_strategies, validate_params


def test_every_registered_strategy_declares_value_rules() -> None:
    """新增策略忘了写 `validate_params` 会在运行期炸 AttributeError——在这里提前拦住。"""
    for name in available_strategies():
        assert validate_params(name, {}) == []
        from app.backtest.strategies import _lookup  # noqa: PLC0415 - 只为拿类做断言

        assert callable(getattr(_lookup(name), "validate_params", None)), name


def test_no_params_is_valid() -> None:
    for name in available_strategies():
        assert validate_params(name, {}) == []


def test_unknown_key_lists_available_ones() -> None:
    (error,) = validate_params("event_driven", {"minscore": 50})
    assert "minscore" in error and "min_score" in error


@pytest.mark.parametrize(
    ("name", "params", "keyword"),
    [
        ("ma_cross", {"fast": 0}, "至少为 1"),
        ("ma_cross", {"slow": 1}, "至少为 2"),
        ("ma_cross", {"fast": 20, "slow": 5}, "必须小于"),
        ("ma_cross", {"fast": 5, "slow": 5}, "必须小于"),
        ("ma_cross", {"fast": 5.5}, "需要整数"),
        ("ma_cross", {"fast": True}, "需要整数"),
        ("event_driven", {"min_score": 101}, "0~100"),
        ("event_driven", {"min_score": -1}, "0~100"),
        ("event_driven", {"hold_days": 0}, "至少为 1"),
        ("event_driven", {"hold_days": 2.5}, "需要整数"),
        ("event_driven", {"min_score": "50"}, "需要数字"),
    ],
)
def test_value_rules_reject(name: str, params: dict[str, object], keyword: str) -> None:
    errors = validate_params(name, params)  # type: ignore[arg-type]
    assert errors and any(keyword in error for error in errors)


@pytest.mark.parametrize(
    ("name", "params"),
    [
        ("ma_cross", {"fast": 1, "slow": 2}),  # 边界：前端 min 值
        ("ma_cross", {"fast": 5.0, "slow": 20.0}),  # 整值的浮点写法照收
        ("event_driven", {"min_score": 0, "hold_days": 1}),
        ("event_driven", {"min_score": 100}),
    ],
)
def test_value_rules_accept_frontend_boundaries(name: str, params: dict[str, object]) -> None:
    assert validate_params(name, params) == []  # type: ignore[arg-type]


def test_multiple_errors_are_all_reported() -> None:
    errors = validate_params("ma_cross", {"fast": 0, "slow": 1, "oops": 1})
    assert len(errors) == 3
