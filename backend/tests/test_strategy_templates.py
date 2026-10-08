"""M4b 模板库：5 个模板既要**过检查器**，也要**跑得动**。

前两个是内置策略的源码等价版——等价性测试**遍历 `TEMPLATES` 按 `builtin` 自动配对**
（SPEC §5 M4b）：新增模板时忘了配测试会直接红。

另 3 个模板没有内置对应物，证据是「检查器零命中 + 沙箱跑通 + 合成触发序列上确实成交」——
一个从不交易的模板比没有模板更糟。
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.backtest.engine import BacktestConfig
from app.backtest.report import build_report
from app.backtest.types import Mode
from app.strategy.api import load_strategy, parse_meta
from app.strategy.params import validate_params
from app.strategy.sandbox import run_user_strategy_sync
from app.strategy.static_check import check_source, format_findings
from app.strategy.templates import TEMPLATES, Template, get_template, template_source
from tests.conftest import make_backtest_dir, ts

SYMBOL = "600519"
START = date(2026, 1, 5)


def bars(
    closes: list[float],
    *,
    opens: list[float] | None = None,
    volumes: list[float] | None = None,
    start: date = START,
) -> list[dict[str, object]]:
    """按收盘价序列造日线；开盘价/量可单独给（放量突破要用），高低默认等于收盘。"""
    return [
        {
            "trade_date": start + timedelta(days=index),
            "open": opens[index] if opens else close,
            "high": max(close, opens[index]) if opens else close,
            "low": min(close, opens[index]) if opens else close,
            "close": close,
            "volume": volumes[index] if volumes else 1e5,
        }
        for index, close in enumerate(closes)
    ]


def waves(count: int = 60) -> list[dict[str, object]]:
    """确定性的正弦波动：够长到 MA5/MA20 都能算，且会真的产生金叉死叉。"""
    return bars([10.0 + math.sin(i / 2.7) * 0.8 + i * 0.02 for i in range(count)])


def window(rows: list[dict[str, object]]) -> tuple[date, date]:
    return rows[0]["trade_date"], rows[-1]["trade_date"]  # type: ignore[return-value]


def run_template(
    key: str,
    rows: list[dict[str, object]],
    params: dict[str, float],
    *,
    tmp_path: Path,
    events: list[dict[str, object]] | None = None,
) -> dict:
    """模板源码经**沙箱**跑一遍（与用户在线跑的是同一条路）。"""
    template = get_template(key)
    assert template is not None
    data_dir = make_backtest_dir(tmp_path, rows, events=events or [])
    meta = parse_meta(template.source)
    start, end = window(rows)
    config = BacktestConfig(
        symbol=SYMBOL,
        strategy="user",
        start=start,
        end=end,
        pit_mode=Mode.PIT,
        params=validate_params(meta.params, params),
        data_dir=data_dir,
    )
    return run_user_strategy_sync(
        template.source, config=config, strategy_name=template.title, uses_events=meta.uses_events
    ).report


def run_builtin(key: str, rows: list[dict[str, object]], params: dict, *, tmp_path: Path, events=None) -> dict:
    data_dir = make_backtest_dir(tmp_path, rows, events=events or [])
    start, end = window(rows)
    config = BacktestConfig(
        symbol=SYMBOL,
        strategy=key,
        start=start,
        end=end,
        pit_mode=Mode.PIT,
        params=params,
        data_dir=data_dir,
    )
    return build_report(config)


# ── 模板库自身的形状 ──────────────────────────────────────────────────────


def test_template_keys_are_unique_and_files_exist() -> None:
    keys = [template.key for template in TEMPLATES]
    assert len(TEMPLATES) == 5
    assert len(set(keys)) == len(keys)
    for template in TEMPLATES:
        assert template.title and template.summary
        assert "def on_bar(" in template_source(template.key)


def test_exactly_two_templates_have_builtin_counterparts() -> None:
    """等价性是按 `builtin` 字段自动配对的——这个断言保证配对不会被悄悄漏掉。"""
    paired = [template for template in TEMPLATES if template.builtin]
    assert {template.builtin for template in paired} == {"ma_cross", "event_driven"}


def test_unknown_key_returns_none_and_raises() -> None:
    assert get_template("nope") is None
    with pytest.raises(KeyError):
        template_source("nope")


# ── 每个模板：过检查器 + 装得上 ────────────────────────────────────────────


@pytest.mark.parametrize("template", TEMPLATES, ids=[t.key for t in TEMPLATES])
def test_template_passes_the_checker(template: Template) -> None:
    findings = check_source(template.source)
    assert findings == [], format_findings(findings)


@pytest.mark.parametrize("template", TEMPLATES, ids=[t.key for t in TEMPLATES])
def test_template_loads_as_a_strategy(template: Template) -> None:
    """装载走 `api.load_strategy`（会 exec 源码）——只喂我们自己仓库里的模板。"""
    strategy = load_strategy(template.source)
    assert callable(strategy.on_bar)


@pytest.mark.parametrize("template", TEMPLATES, ids=[t.key for t in TEMPLATES])
def test_template_declares_meta_that_parses(template: Template) -> None:
    meta = parse_meta(template.source)
    assert isinstance(meta.uses_events, bool)
    assert meta.params, f"{template.key} 一个参数都没有，模板应当演示 PARAMS 怎么写"


# ── 等价性：模板 vs 注册表策略，逐点一致 ──────────────────────────────────

EQUIVALENCE: dict[str, dict] = {
    "ma_cross": {"rows": waves(), "params": {"fast": 5, "slow": 20}, "events": None},
    "event_driven": {
        "rows": bars([10.0] * 30),
        "params": {"min_score": 50.0, "hold_days": 5},
        "events": [
            {
                "event_id": "evt-bullish-1",
                "event_time": ts("2026-01-10 09:30:00"),
                "available_at": ts("2026-01-10 09:30:00"),
                "direction_norm": "bullish",
                "score": 60.0,
            }
        ],
    },
}


@pytest.mark.parametrize(
    "template", [t for t in TEMPLATES if t.builtin], ids=[t.key for t in TEMPLATES if t.builtin]
)
def test_template_matches_builtin_point_by_point(template: Template, tmp_path: Path) -> None:
    """一份证据同时证明：用户 API 的表达力与内置策略等价、沙箱执行没有偷偷改口径。"""
    assert template.builtin is not None
    case = EQUIVALENCE[template.key]
    user = run_template(
        template.key, case["rows"], case["params"], tmp_path=tmp_path, events=case["events"]
    )
    builtin = run_builtin(
        template.builtin, case["rows"], case["params"], tmp_path=tmp_path, events=case["events"]
    )
    assert user["metrics"]["trade_count"] >= 1, "等价性用例得真成交，否则等于没比"
    for report in (user, builtin):
        report["meta"].pop("strategy_kind")
        report["meta"].pop("strategy_name")
        report["meta"].pop("strategy")
    assert user == builtin


# ── 3 个新模板：合成触发序列上确实成交 ────────────────────────────────────

TRIGGERS: dict[str, dict] = {
    "donchian_breakout": {
        "rows": bars([10.0] * 25 + [11.0] * 3 + [9.0] * 25),
        "params": {"window": 20},
    },
    "rsi_reversal": {
        "rows": bars([12.0 - 0.2 * i for i in range(20)] + [8.4 + 0.2 * i for i in range(20)]),
        "params": {"period": 14, "oversold": 35.0, "exit_level": 65.0},
    },
    "volume_breakout": {
        "rows": bars(
            [10.0] * 10 + [10.5] + [10.5] * 9,
            opens=[10.0] * 10 + [9.8] + [10.5] * 9,
            volumes=[1e5] * 10 + [5e5] + [1e5] * 9,
        ),
        "params": {"window": 5, "multiple": 2.0, "hold_days": 2},
    },
}


@pytest.mark.parametrize("key", sorted(TRIGGERS))
def test_new_template_actually_trades(key: str, tmp_path: Path) -> None:
    case = TRIGGERS[key]
    report = run_template(key, case["rows"], case["params"], tmp_path=tmp_path)
    assert report["metrics"]["trade_count"] >= 1, f"{key} 在触发序列上一笔都没成交"
    assert report["meta"]["bars"] == len(case["rows"])
