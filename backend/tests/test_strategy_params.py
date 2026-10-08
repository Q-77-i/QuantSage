"""M4a 用户策略参数：schema 解析与取值归一的矩阵。

口径：只拒绝、不静默改写——未知键 / 类型不符 / 非整值 / 越界都要报错，缺省值一律填满。
"""

from __future__ import annotations

import pytest

from app.strategy import StrategyRejected
from app.strategy.params import parse_param_schema, validate_params

SCHEMA_RAW = {
    "fast": {"type": "int", "default": 5, "min": 1, "max": 250, "label": "快线周期"},
    "threshold": {"type": "float", "default": 0.5, "min": 0.0, "max": 1.0},
    "enabled": {"type": "bool", "default": True},
}


def schema():
    return parse_param_schema(SCHEMA_RAW)


def test_schema_parses_full_spec() -> None:
    specs = schema()
    assert set(specs) == {"fast", "threshold", "enabled"}
    assert specs["fast"].type == "int" and specs["fast"].min == 1 and specs["fast"].label == "快线周期"
    assert specs["threshold"].max == 1.0
    assert specs["enabled"].default is True


def test_schema_allows_empty() -> None:
    assert parse_param_schema({}) == {}


@pytest.mark.parametrize(
    ("raw", "keyword"),
    [
        ([], "必须是字典"),
        ({"1fast": {"type": "int", "default": 1}}, "合法标识符"),
        ({"fast": 5}, "必须是字典"),
        ({"fast": {"default": 5}}, "type"),
        ({"fast": {"type": "number", "default": 5}}, "type"),
        ({"fast": {"type": "int"}}, "缺 'default'"),
        ({"fast": {"type": "int", "default": 5.5}}, "不符"),
        ({"flag": {"type": "int", "default": True}}, "不符"),  # bool 不是 int
        ({"fast": {"type": "int", "default": 5, "min": "1"}}, "必须是数字"),
        ({"fast": {"type": "int", "default": 5, "min": 9, "max": 1}}, "min(9) 大于 max(1)"),
        ({"fast": {"type": "int", "default": 5, "label": 3}}, "label"),
    ],
)
def test_schema_rejects_bad_specs(raw: object, keyword: str) -> None:
    with pytest.raises(StrategyRejected) as info:
        parse_param_schema(raw)
    assert keyword in str(info.value)


def test_validate_fills_defaults() -> None:
    assert validate_params(schema(), {}) == {"fast": 5, "threshold": 0.5, "enabled": True}


def test_validate_normalizes_types() -> None:
    values = validate_params(schema(), {"fast": 20.0, "threshold": 1, "enabled": False})
    assert values == {"fast": 20, "threshold": 1.0, "enabled": False}
    assert isinstance(values["fast"], int) and isinstance(values["threshold"], float)


def test_validate_rejects_unknown_key_with_available_list() -> None:
    with pytest.raises(StrategyRejected) as info:
        validate_params(schema(), {"minscore": 50})
    message = str(info.value)
    assert "minscore" in message and "fast" in message


@pytest.mark.parametrize(
    ("values", "keyword"),
    [
        ({"fast": 5.5}, "需要整数"),
        ({"fast": True}, "需要整数"),
        ({"fast": "5"}, "需要整数"),
        ({"fast": 0}, "小于下限"),
        ({"fast": 251}, "大于上限"),
        ({"threshold": 1.01}, "大于上限"),
        ({"enabled": 1}, "true / false"),
    ],
)
def test_validate_rejects_bad_values(values: dict[str, object], keyword: str) -> None:
    with pytest.raises(StrategyRejected) as info:
        validate_params(schema(), values)
    assert keyword in str(info.value)


def test_validate_accepts_bounds_inclusive() -> None:
    assert validate_params(schema(), {"fast": 1})["fast"] == 1
    assert validate_params(schema(), {"fast": 250})["fast"] == 250
