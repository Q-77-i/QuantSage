"""M8a 证据归并（`app.agent.deep.merge`）：去重、缺口如实列、数字带角色前缀。"""

from __future__ import annotations

from app.agent.deep.merge import merge
from app.agent.deep.types import EvidenceRef, RoleResult


def ref(event_id: str, day: str = "2026-09-01", title: str = "中标公告") -> EvidenceRef:
    return EvidenceRef(event_id=event_id, day=day, title=title)


def test_evidence_is_deduped_by_event_id_and_day_first_wins() -> None:
    """键是 `event_id|day`——同一事件被两个角色取到只留一份（先到先得）。"""
    results = [
        RoleResult("news", evidence=(ref("news:1"), ref("news:2"))),
        RoleResult("fundamental", evidence=(ref("news:1"), ref("announcement:9"))),
    ]

    merged = merge(results)

    assert [e.key for e in merged.evidence] == ["news:1|2026-09-01", "news:2|2026-09-01",
                                                "announcement:9|2026-09-01"]
    assert merged.duplicates == 1


def test_same_event_id_on_different_days_is_not_a_duplicate() -> None:
    """`event_id` 不是全局唯一键（M2b 已记：同题事件跨月复用 id）——日期不同就是两条。"""
    merged = merge([RoleResult("news", evidence=(ref("news:1", "2026-08-01"),
                                                 ref("news:1", "2026-09-01")))])

    assert len(merged.evidence) == 2 and merged.duplicates == 0


def test_degraded_role_becomes_a_visible_gap() -> None:
    merged = merge([
        RoleResult("news", errors=("检索不可用：Qdrant 连不上",)),
        RoleResult("market", findings=("区间收益 12.34%",), numbers={"区间收益": "12.34%"}),
    ])

    assert merged.degraded == ("news",)
    assert any("新闻事件" in gap and "Qdrant" in gap for gap in merged.gaps)
    assert merged.evidence == ()


def test_numbers_are_namespaced_by_role_label() -> None:
    """两个角色各有一个「区间收益」时不能互相覆盖——前缀是角色标签。"""
    merged = merge([
        RoleResult("market", numbers={"区间收益": "12.34%"}),
        RoleResult("factor", numbers={"区间收益": "9.99%"}),
    ])

    assert merged.numbers == {"行情技术面 · 区间收益": "12.34%", "因子量化 · 区间收益": "9.99%"}
    assert merged.numbers_for("market") == {"区间收益": "12.34%"}


def test_role_with_no_output_at_all_is_flagged() -> None:
    merged = merge([RoleResult("policy")])

    assert any("政策面" in gap for gap in merged.gaps)


def test_findings_missing_but_material_present_is_noted_not_hidden() -> None:
    """解读失败但代码算出的数字还在——要点缺失要如实记，数字照常进表。"""
    merged = merge([RoleResult("market", numbers={"区间收益": "12.34%"}, notes=("解读超时",))])

    assert merged.numbers == {"行情技术面 · 区间收益": "12.34%"}
    assert any("未产出解读要点" in gap for gap in merged.gaps)


def test_payload_is_json_safe() -> None:
    payload = merge([RoleResult("news", evidence=(ref("news:1"),))]).to_payload()

    assert payload["evidence"][0]["key"] == "news:1|2026-09-01"
    assert payload["degraded"] == [] and payload["duplicates"] == 0
