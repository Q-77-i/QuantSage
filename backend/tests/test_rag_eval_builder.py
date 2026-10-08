"""评测集构建的纯函数用例：判废规则、LLM 输出解析、真值抽样。

这些规则是**评测信度的闸门**：判废规则形同虚设的话，评测集里的问题会带着
答案（日期/代码/标题片段）去做检索，分数再高也证明不了什么。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_rag_eval.py"
_spec = importlib.util.spec_from_file_location("build_rag_eval", SCRIPT)
builder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(builder)


# ── 判废规则 ──────────────────────────────────────────────────────────────


def test_rejects_query_with_stock_code() -> None:
    reason = builder.leak_check("002842 这家公司发了什么业绩预告？", "广东翔鹭钨业业绩预告", "事实型")
    assert reason and "股票代码" in reason


def test_rejects_date_in_non_temporal_category() -> None:
    reason = builder.leak_check("2026年7月10日哪家公司发了业绩预告？", "某公司业绩预告", "事实型")
    assert reason and "日期" in reason


def test_allows_date_in_temporal_category() -> None:
    assert builder.leak_check("8月27日豪尔赛发布了什么公告？", "豪尔赛：股票交易异常波动公告", "时间型") is None


def test_rejects_title_fragment() -> None:
    title = "广东翔鹭钨业股份有限公司2026年半年度业绩预告"
    reason = builder.leak_check("广东翔鹭钨业股份有限公司的业绩预告说了什么？", title, "事实型")
    assert reason and "抄了标题" in reason


def test_accepts_clean_query() -> None:
    assert builder.leak_check("哪家钨业公司披露了半年度业绩预告？", "广东翔鹭钨业股份有限公司2026年半年度业绩预告", "事实型") is None


def test_ignores_whitespace_when_matching_title() -> None:
    title = "豪尔赛：股票交易异常波动公告"
    assert builder.leak_check("股票交易异常波动公告是谁发的？", title, "事件型") is not None


# ── LLM 输出解析 ──────────────────────────────────────────────────────────


def test_parses_plain_json_array() -> None:
    raw = '[{"i": 1, "grade": 2, "why": "就是它"}, {"i": 2, "grade": 0, "why": "无关"}]'
    assert builder._parse_grades(raw) == {1: 2, 2: 0}


def test_parses_json_wrapped_in_fence_and_prose() -> None:
    raw = '好的，结果如下：\n```json\n[{"i": 3, "grade": 1}]\n```\n以上。'
    assert builder._parse_grades(raw) == {3: 1}


def test_clamps_out_of_range_grades() -> None:
    assert builder._parse_grades('[{"i": 1, "grade": 9}, {"i": 2, "grade": -3}]') == {1: 2, 2: 0}


def test_raises_when_no_array() -> None:
    with pytest.raises(ValueError):
        builder._parse_grades("我给不出结论")


# ── 真值抽样 ──────────────────────────────────────────────────────────────


def _row(event_id: str, title: str, *, symbols=("600519",), industries=(), event_type="news",
         day="2026-08-17") -> dict:
    return {
        "event_id": event_id,
        "title": title,
        "summary": None,
        "event_type": event_type,
        "symbols": list(symbols),
        "industries": list(industries),
        "day": day,
        "event_time": f"{day} 09:00:00+08:00",
    }


def test_sampling_is_deterministic_and_deduplicated() -> None:
    rows = [_row(f"news:{i}", f"第{i}条新闻", symbols=("600519",)) for i in range(200)]
    first = builder._pick_truth_events(rows)
    second = builder._pick_truth_events(list(reversed(rows)))
    assert [r["event_id"] for _, r in first] == [r["event_id"] for _, r in second]
    keys = [f"{r['day']}|{r['event_id']}" for _, r in first]
    assert len(keys) == len(set(keys))  # 同一事件不重复出题


def test_sampling_skips_events_irrelevant_to_a_shares() -> None:
    """不带标的也不带行业的事件（国际新闻等）不进池——用户问的是 A 股。"""
    rows = [_row(f"news:{i}", f"国际新闻{i}", symbols=(), industries=()) for i in range(50)]
    picks = builder._pick_truth_events(rows)
    assert picks == []


def test_numeric_pool_requires_digits_in_title() -> None:
    rows = [_row("news:1", "某公司中标3.5亿元项目")] + [
        _row(f"news:{i}", f"第{i}条普通新闻") for i in range(2, 10)
    ]
    numeric = [r for category, r in builder._pick_truth_events(rows) if category == "数值型"]
    assert [r["event_id"] for r in numeric] == ["news:1"]
