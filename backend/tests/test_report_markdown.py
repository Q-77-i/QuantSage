"""M7a Markdown 导出（`app.report.markdown`）：分享出去的那份必须自带证据链。

验收口径（PRD）：**导出含证据链**——每条证据的标题、双时间戳（事发 / 可得并列）、
来源三元组都要出现在正文里；`None` 一律渲染成「—」而不是空白或 `None`。
"""

from __future__ import annotations

from typing import Any

from app.report.markdown import render_markdown
from tests.test_report_builder import BENCHMARK, _inputs
from app.report.builder import assemble_report, build_facts
from app.report.narrative import Narrative


def _body() -> dict[str, Any]:
    return assemble_report(build_facts(**_inputs()), Narrative("本期间策略小幅盈利。", "m"))


def test_markdown_carries_header_metrics_and_disclaimer() -> None:
    text = render_markdown(_body())

    assert text.startswith("# 银行事件驱动")
    assert "event_driven" in text and "600519" in text
    assert "2026-08-03 → 2026-09-30" in text
    assert "数据止于 2026-09-30" in text
    assert "累计收益" in text and "1.08%" in text.replace("+", "")
    assert "不构成投资建议" in text


def test_markdown_evidence_section_has_dual_timestamps_and_source_triple() -> None:
    """一条证据四件套：标题、事发与可得并列、来源 / 原始来源 / 内容哈希。"""
    text = render_markdown(_body())

    assert "## 证据链" in text
    assert "重大合同公告" in text
    assert "2026-08-03 09:00:00+08:00" in text and "2026-08-03 09:10:00+08:00" in text
    assert "事发" in text and "可得" in text
    assert "xiaoshi-archive" in text and "华尔街见闻" in text
    assert "hash-a"[:12] in text
    assert "摘要" in text


def test_markdown_renders_attribution_tables_and_warnings() -> None:
    text = render_markdown(_body())

    assert "### 标的级" in text and "600519" in text
    assert "### 驱动事件方向" in text and "利多" in text
    assert "### 驱动事件行业" in text and "银行" in text
    assert "尚未平仓" in text  # warnings 如实带出
    assert "全市场等权" in text


def test_markdown_marks_absent_narrative_and_none_values() -> None:
    """综述缺席时写原因；缺值一律「—」。"""
    body = assemble_report(
        build_facts(**_inputs()), Narrative(None, "m", note="综述超时（>20s），本次缺席")
    )
    body["metrics"]["sharpe"] = None

    text = render_markdown(body)

    assert "综述超时" in text
    assert "—" in text


def test_markdown_is_deterministic() -> None:
    assert render_markdown(_body()) == render_markdown(_body())


def test_markdown_shows_report_and_snapshot_short_hashes() -> None:
    """报告页与分享页都要能自证「这份报告对应哪批数据」——两个短码都印出来。"""
    from app.report.snapshot import report_hash

    body = _body()
    text = render_markdown(body, report_hash_value=report_hash(body))

    assert report_hash(body)[:12] in text
    assert BENCHMARK.total_return is not None  # 基准口径自述在正文里
    assert "全市场等权" in text
