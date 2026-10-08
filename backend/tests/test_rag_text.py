"""嵌入文本构造的用例（纯函数，无依赖）。"""

from __future__ import annotations

from datetime import datetime

from app.backtest.types import CN_TZ
from app.rag.text import build_embedding_text


def row(**overrides) -> dict:
    base = {
        "title": "央行宣布降准0.5个百分点",
        "event_type": "news",
        "event_time": datetime(2026, 5, 7, 16, 31, tzinfo=CN_TZ),
        "direction_norm": "bullish",
        "importance_score": 78.34,
        "industries": ["银行", "房地产"],
        "symbols": ["600036", "000001"],
        "summary": "释放长期资金约1万亿元。",
    }
    base.update(overrides)
    return base


def test_full_row_layout() -> None:
    text = build_embedding_text(row())
    assert text.splitlines() == [
        "【标题】央行宣布降准0.5个百分点",
        "【类型】news 【时间】2026-05-07 【方向】bullish 【重要度】78.3 "
        "【行业】银行/房地产 【标的】600036/000001",
        "释放长期资金约1万亿元。",
    ]


def test_empty_fields_are_omitted_not_left_as_labels() -> None:
    """公告 83% 没有摘要（实测），空壳标签既占 token 又污染稀疏分支。"""
    text = build_embedding_text(
        row(title="锦龙股份：2026年半年度报告", summary=None, industries=[], symbols=[],
            direction_norm=None, importance_score=None)
    )
    assert text == "【标题】锦龙股份：2026年半年度报告\n【类型】news 【时间】2026-05-07"
    assert "【行业】" not in text and "【重要度】" not in text


def test_importance_formatted_to_one_decimal() -> None:
    assert "【重要度】7.1" in build_embedding_text(row(importance_score=7.14))
    # 非数值（脏数据/缺失）不写标签，而不是写成 "【重要度】None"
    assert "【重要度】" not in build_embedding_text(row(importance_score=None))


def test_time_is_date_only() -> None:
    """时分秒对检索无增益，只会拉长文本；「时间型」问题问的是哪天。"""
    assert "【时间】2026-05-07" in build_embedding_text(row())
    assert "16:31" not in build_embedding_text(row())


def test_whitespace_only_values_are_treated_as_empty() -> None:
    text = build_embedding_text(row(summary="   ", industries=["  "]))
    assert text.splitlines()[-1].startswith("【类型】")
    assert "【行业】" not in text
