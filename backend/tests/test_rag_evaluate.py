"""评测指标的用例：用手算样例钉住公式（指标算错，整套验收就是自欺）。"""

from __future__ import annotations

import math

import pytest

from app.rag.evaluate import (
    JudgedQuery,
    ModeScore,
    compare_modes,
    dcg,
    doc_key,
    mrr,
    ndcg_at_k,
    recall_at_k,
    score_query,
)


def test_dcg_matches_hand_computation() -> None:
    # 等级 [2, 0] → (2²-1)/log2(2) + (2⁰-1)/log2(3) = 3
    assert dcg([2, 0]) == 3.0
    assert dcg([1, 1]) == 1.0 + 1.0 / math.log2(3)


def test_ndcg_perfect_ranking_is_one() -> None:
    judgments = {"a": 2, "b": 1, "c": 0}
    assert ndcg_at_k(["a", "b", "c"], judgments) == 1.0


def test_ndcg_at_10_ignores_beyond_k() -> None:
    judgments = {"a": 2, "z": 2}
    ranked = ["x"] * 10 + ["a"]  # 正解被挤到第 11 位
    assert ndcg_at_k(ranked, judgments, 10) == 0.0


def test_ndcg_without_relevant_docs_is_zero() -> None:
    assert ndcg_at_k(["a", "b"], {"a": 0, "b": 0}) == 0.0


def test_ndcg_penalises_worse_position() -> None:
    judgments = {"a": 2, "b": 1}
    assert ndcg_at_k(["a", "b"], judgments) > ndcg_at_k(["b", "a"], judgments)


def test_mrr_takes_first_relevant() -> None:
    assert mrr(["x", "a", "b"], {"a": 2, "b": 2}) == 0.5
    assert mrr(["a"], {"a": 1}) == 1.0
    assert mrr(["x"], {"x": 0}) == 0.0


def test_recall_counts_all_graded_relevant() -> None:
    judgments = {"a": 1, "b": 2, "c": 2}
    assert recall_at_k(["a", "c"], judgments, 20) == 2 / 3
    assert recall_at_k(["a", "c"], judgments, 1) == 1 / 3


def test_score_query_bundles_three_metrics() -> None:
    judged = JudgedQuery(id="q1", category="事实型", query="x", judgments={"a": 2})
    scores = score_query(["a"], judged)
    assert scores == {"ndcg@10": 1.0, "mrr": 1.0, "recall@20": 1.0}


def test_mode_score_averages_and_groups_by_category() -> None:
    score = ModeScore(mode="rerank")
    score.add("q1", {"ndcg@10": 1.0})
    score.add("q2", {"ndcg@10": 0.0})
    assert score.mean["ndcg@10"] == 0.5
    grouped = score.by_category({"q1": "事实型", "q2": "时间型"})
    assert grouped["事实型"]["ndcg@10"] == 1.0


def test_compare_modes_reports_difference() -> None:
    base = ModeScore(mode="dense")
    base.add("q1", {"ndcg@10": 0.4})
    target = ModeScore(mode="rerank")
    target.add("q1", {"ndcg@10": 0.7})
    diff = compare_modes([base, target], baseline="dense", target="rerank")
    assert diff["ndcg@10"] == pytest.approx(0.3)


def test_doc_key_includes_day() -> None:
    """`event_id` 跨月复用，键必须带日期，否则判定表会串行。"""
    assert doc_key("2026-08-17", "news:1818329") != doc_key("2026-09-17", "news:1818329")
