"""深研报 rubric 评测的纯函数（5c）：自动项、judge 解析、聚合与一页纸。

不调模型、不跑深路径——评分器本身必须先可测，才谈得上评报告。
"""

from __future__ import annotations

from app.report.deep_eval import aggregate, auto_score, parse_judge, render_markdown


def report(**over):
    base = {
        "brief": {"question": "分析 600519", "as_of": "2026-08-20T15:00:00+08:00"},
        "run": {"status": "done", "as_of": "2026-08-20T15:00:00+08:00"},
        "gate": {"decision": "pass", "stray_numbers": [], "notes": []},
        "debate": [{"round": 1}, {"round": 2}, {"round": 2}],
        "evidence": [
            {
                "event_id": "e1",
                "day": "2026-08-19",
                "title": "公告一",
                "available_at": "2026-08-19T10:00:00+08:00",
            },
            {
                "event_id": "e2",
                "day": "2026-08-20",
                "title": "公告二",
                "available_at": "2026-08-20T14:59:00+08:00",
            },
        ],
        "gaps": [],
        "blocks": [{"id": "conclusion", "text": "结论"}],
    }
    base.update(over)
    return base


def test_auto_score_reads_the_report() -> None:
    auto = auto_score(
        report(gate={"decision": "degrade", "stray_numbers": ["12.3"], "notes": ["x"]}),
        status="degraded",
        budget={"llm_calls": 10, "tokens": 1000, "cost_usd": 0.01},
        elapsed_s=23.4,
    )
    assert auto["status"] == "degraded"
    assert auto["gate"] == "degrade"
    assert auto["stray_numbers"] == 1
    assert auto["debate_rounds"] == 2
    assert auto["debate_within_cap"] is True
    assert auto["evidence_count"] == 2
    assert auto["pit_violations"] == []
    assert auto["report_assembled"] is True
    assert auto["llm_calls"] == 10
    assert auto["elapsed_s"] == 23.4


def test_pit_violation_is_detected() -> None:
    leaked = {
        **report(),
        "evidence": [
            {
                "event_id": "late",
                "day": "2026-08-21",
                "title": "未来公告",
                "available_at": "2026-08-21T09:00:00+08:00",
            }
        ],
    }
    auto = auto_score(leaked, status="done")
    assert auto["pit_violations"] == ["late|2026-08-21"]


def test_missing_report_only_records_status_and_budget() -> None:
    auto = auto_score(None, status="failed", budget={"llm_calls": 3}, elapsed_s=2.0)
    assert auto["status"] == "failed"
    assert auto["report_assembled"] is False
    assert auto["evidence_count"] == 0
    assert auto["pit_violations"] == []
    assert auto["llm_calls"] == 3


def test_parse_judge_accepts_wrapped_json_and_rejects_bad_scores() -> None:
    good = parse_judge(
        '评分如下：\n{"pit": 5, "evidence": 4, "numbers": 3, "balance": 4, "risk": 5, '
        '"reason": "证据齐"}'
    )
    assert good is not None
    assert good["scores"] == {"pit": 5, "evidence": 4, "numbers": 3, "balance": 4, "risk": 5}
    assert good["reason"] == "证据齐"

    assert parse_judge('{"pit": 6, "evidence": 4, "numbers": 3, "balance": 4, "risk": 5}') is None
    assert parse_judge('{"pit": 5, "evidence": 4}') is None
    assert parse_judge("不是 JSON") is None


def test_aggregate_only_counts_judged_items() -> None:
    items = [
        {
            "auto": {"report_assembled": True, "pit_violations": [], "debate_within_cap": True},
            "judge": {"scores": {"pit": 5, "evidence": 4, "numbers": 4, "balance": 3, "risk": 4}},
        },
        {
            "auto": {"report_assembled": True, "pit_violations": ["x|d"], "debate_within_cap": True},
            "judge": {"scores": {"pit": 3, "evidence": 4, "numbers": 2, "balance": 3, "risk": 4}},
        },
        {
            "auto": {"report_assembled": False, "pit_violations": [], "debate_within_cap": True},
            "judge": None,
        },
    ]
    summary = aggregate(items)
    assert summary["count"] == 3
    assert summary["report_assembled"] == 2
    assert summary["pit_violations"] == 1
    assert summary["judge_count"] == 2
    assert summary["avg_judge"]["pit"] == 4.0
    assert summary["avg_judge"]["numbers"] == 3.0


def test_markdown_is_a_reading_not_a_pass_fail_gate() -> None:
    item = {
        "id": "D01",
        "as_of": "2026-08-20",
        "auto": {
            "status": "done",
            "gate": "pass",
            "evidence_count": 2,
            "pit_violations": [],
            "debate_rounds": 2,
            "gaps": 1,
        },
        "judge": {"scores": {"pit": 5, "evidence": 4, "numbers": 4, "balance": 3, "risk": 4},
                  "reason": "证据齐"},
    }
    text = render_markdown([item], {"dataset": "x.json", "count": 1, "generated_at": "now"})
    assert "深研报评测" in text
    assert "不设及格线" in text
    assert "5 / 4 / 4 / 3 / 4" in text
    assert "证据齐" in text
    assert "PIT 违规证据：**0**" in text
