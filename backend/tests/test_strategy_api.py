"""M4a 用户策略契约层：静态解析、命名空间白名单、on_bar 装载与返回值收口。

这些都是**纯函数级**用例：不 spawn 子进程、不读数据（沙箱那一层在 test_strategy_sandbox.py）。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.backtest.types import Bar, BarContext, Position, Side, Signal
from app.strategy import StrategyRejected
from app.strategy.api import ALLOWED_MODULES, build_namespace, load_strategy, parse_meta

START = date(2026, 8, 3)

GOOD_SOURCE = '''
PARAMS = {"fast": {"type": "int", "default": 5, "min": 1, "max": 250, "label": "快线周期"}}
USES_EVENTS = False

def validate_params(p):
    return ["快线须小于慢线"] if p["fast"] >= 20 else []

def on_bar(ctx):
    if ctx.index < 3:
        return []
    closes = [bar.close for bar in ctx.history]
    if closes[-1] > math.fsum(closes[-3:]) / 3:
        return [Signal(Side.BUY, reason="均值上行")]
    return []
'''


def ctx_for(closes: list[float]) -> BarContext:
    """与 test_backtest_strategies.ctx_for 同形：history 覆盖 [0..index]。"""
    bars = tuple(
        Bar("600519", START + timedelta(days=i), c, c, c, c, 1e5) for i, c in enumerate(closes)
    )
    index = len(bars) - 1
    return BarContext(
        bar=bars[index],
        index=index,
        history=bars,
        position=Position(),
        cash=0.0,
        equity=0.0,
        events=(),
        new_events=(),
    )


# ── 静态解析 ──────────────────────────────────────────────────────────────


def test_parse_meta_reads_literals() -> None:
    meta = parse_meta(GOOD_SOURCE)
    assert meta.uses_events is False
    assert meta.params["fast"].default == 5


def test_parse_meta_defaults_when_absent() -> None:
    meta = parse_meta("def on_bar(ctx):\n    return []\n")
    assert meta.params == {} and meta.uses_events is False


def test_parse_meta_ignores_names_inside_functions() -> None:
    """函数体里的同名变量不是声明——否则一个局部 `params = ...` 就能改掉表单。"""
    source = "USES_EVENTS = True\ndef on_bar(ctx):\n    PARAMS = 1\n    return []\n"
    assert parse_meta(source).uses_events is True


def test_parse_meta_rejects_syntax_error_with_line() -> None:
    with pytest.raises(StrategyRejected) as info:
        parse_meta("def on_bar(ctx)\n    return []\n")
    assert info.value.line == 1 and "语法错误" in str(info.value)


def test_parse_meta_rejects_non_literal_params_with_line() -> None:
    source = "PARAMS = dict(fast=5)\ndef on_bar(ctx):\n    return []\n"
    with pytest.raises(StrategyRejected) as info:
        parse_meta(source)
    assert info.value.line == 1 and "字面量" in str(info.value)


def test_parse_meta_rejects_non_bool_uses_events() -> None:
    source = 'USES_EVENTS = "yes"\ndef on_bar(ctx):\n    return []\n'
    with pytest.raises(StrategyRejected) as info:
        parse_meta(source)
    assert "USES_EVENTS" in str(info.value)


# ── 命名空间 ──────────────────────────────────────────────────────────────


def test_namespace_preloads_modules_and_types() -> None:
    namespace = build_namespace()
    for name in ("math", "statistics", "Signal", "Side"):
        assert name in namespace
    assert "pandas" not in namespace  # 产品立场：不给 pandas，shift/bfill 的来源


def test_namespace_import_whitelist() -> None:
    namespace = build_namespace()
    exec("import math\nfrom collections import deque\n", namespace)
    assert namespace["deque"].__name__ == "deque"
    for forbidden in ("duckdb", "pandas", "os", "socket", "subprocess", "pathlib"):
        with pytest.raises(ImportError) as info:
            exec(f"import {forbidden}", build_namespace())
        assert "不允许 import" in str(info.value)
    assert "duckdb" not in ALLOWED_MODULES and "pandas" not in ALLOWED_MODULES


def test_namespace_blocks_relative_and_builtins_open() -> None:
    with pytest.raises(ImportError):
        exec("from . import api", build_namespace())
    with pytest.raises(NameError):
        exec("open('/etc/passwd')", build_namespace())


# ── on_bar 装载 ───────────────────────────────────────────────────────────


def test_load_strategy_runs_and_returns_signals() -> None:
    strategy = load_strategy(GOOD_SOURCE, name="我的策略")
    assert strategy.name == "我的策略"
    assert strategy.on_bar(ctx_for([1.0, 2.0, 3.0])) == []
    signals = strategy.on_bar(ctx_for([3.0, 2.0, 1.0, 5.0]))
    assert [signal.side for signal in signals] == [Side.BUY]


def test_on_bar_second_param_carries_params() -> None:
    """需要参数的策略用第二个形参拿（`def on_bar(ctx, p)`），来源写在签名上。"""
    source = "def on_bar(ctx, p):\n    return [Signal(Side.BUY, reason=f\"fast={p['fast']}\")]\n"
    strategy = load_strategy(source, params={"fast": 7})
    assert strategy.on_bar(ctx_for([1.0]))[0].reason == "fast=7"


def test_load_strategy_accepts_generator_return() -> None:
    source = "def on_bar(ctx):\n    return (s for s in [Signal(Side.SELL, reason='x')])\n"
    assert len(load_strategy(source).on_bar(ctx_for([1.0]))) == 1


@pytest.mark.parametrize(
    ("source", "keyword"),
    [
        ("def nope(ctx):\n    return []\n", "没有找到 on_bar"),
        ("on_bar = 3\n", "不是函数"),
        ("def on_bar(ctx, p, extra):\n    return []\n", "必须是 (ctx) 或 (ctx, p)"),
        ("def on_bar():\n    return []\n", "必须是 (ctx) 或 (ctx, p)"),
        ("def on_bar(*, ctx):\n    return []\n", "必须是 (ctx) 或 (ctx, p)"),
        ("def on_bar(ctx)\n    return []\n", "语法错误"),
        ("raise ValueError('模块级就炸了')\ndef on_bar(ctx):\n    return []\n", "加载时抛出"),
        ("validate_params = 7\ndef on_bar(ctx):\n    return []\n", "validate_params 不是函数"),
    ],
)
def test_load_strategy_rejects_bad_entry(source: str, keyword: str) -> None:
    with pytest.raises(StrategyRejected) as info:
        load_strategy(source)
    assert keyword in str(info.value)


@pytest.mark.parametrize(
    ("body", "keyword"),
    [
        ("return None", "是 None"),
        ("return 'buy'", "是字符串"),
        ("return 1", "不可迭代"),
        ("return [('buy', 1)]", "必须都是 Signal"),
    ],
)
def test_on_bar_rejects_bad_return_values(body: str, keyword: str) -> None:
    source = f"def on_bar(ctx):\n    {body}\n"
    strategy = load_strategy(source)
    with pytest.raises(StrategyRejected) as info:
        strategy.on_bar(ctx_for([1.0]))
    assert keyword in str(info.value)


def test_on_bar_wraps_user_exception_with_bar_date_and_line() -> None:
    source = "def on_bar(ctx):\n    x = 1\n    return [Signal(Side.BUY)] / 0\n"
    strategy = load_strategy(source)
    with pytest.raises(StrategyRejected) as info:
        strategy.on_bar(ctx_for([1.0]))
    message = str(info.value)
    assert "TypeError" in message and str(START) in message
    assert info.value.line == 3  # 出错那一行，编辑器据此标注


# ── 跨字段校验钩子 ────────────────────────────────────────────────────────


def test_check_values_delegates_to_user_hook() -> None:
    strategy = load_strategy(GOOD_SOURCE)
    assert strategy.check_values({"fast": 5}) == []
    assert strategy.check_values({"fast": 20}) == ["快线须小于慢线"]


def test_check_values_without_hook_is_empty() -> None:
    assert load_strategy("def on_bar(ctx):\n    return []\n").check_values({"x": 1}) == []


def test_check_values_coerces_single_string_and_rejects_garbage() -> None:
    single = load_strategy("def validate_params(p):\n    return '太慢'\ndef on_bar(ctx):\n    return []\n")
    assert single.check_values({}) == ["太慢"]
    with pytest.raises(StrategyRejected):
        load_strategy("def validate_params(p):\n    return 7\ndef on_bar(ctx):\n    return []\n").check_values({})
    with pytest.raises(StrategyRejected) as info:
        load_strategy(
            "def validate_params(p):\n    raise ValueError('钩子炸')\ndef on_bar(ctx):\n    return []\n"
        ).check_values({})
    assert "validate_params 抛出" in str(info.value)
