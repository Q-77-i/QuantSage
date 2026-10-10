"""Markdown 导出（M7a）：分享出去的**一份能自己站住**的文件。

它是冻结产物的另一种渲染（不是第二份实现）：数字、归因、证据链、如实标注全部来自
`body`，`None` 一律「—」。末尾固定两条「研究用途」声明（PRD 已定）。

M8 的深度研报接进来时，未知块按 `text` 逐段追加（`_extra_sections`），不必改这里。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

#: 指标顺序与中文标签（与 `report/builder.py` 的 metrics 键同名；表头一次定义）
METRIC_LABELS: tuple[tuple[str, str, str], ...] = (
    ("total_return", "累计收益", "pct"),
    ("annual_return", "年化收益", "pct"),
    ("benchmark_return", "全市场等权基准", "pct"),
    ("excess_return", "超额收益", "pct"),
    ("max_drawdown", "最大回撤", "pct"),
    ("volatility", "年化波动率", "pct"),
    ("sharpe", "夏普", "num"),
    ("win_rate", "胜率（已平仓）", "pct"),
    ("trade_count", "已平仓回合", "int"),
    ("final_equity", "期末权益", "money"),
)

DISCLAIMER = "本报告由 QuantSage 生成，仅供研究用途，不构成投资建议。"

#: 容器固定渲染的块（其余块走 `_extra_sections` —— M8 的块不用改这里）
_HANDLED_BLOCKS = {"overview", "performance", "attribution", "narrative"}


def _value(raw: Any, kind: str) -> str:
    if raw is None:
        return "—"
    if kind == "pct":
        return f"{float(raw):.2%}"
    if kind == "int":
        return f"{int(raw)}"
    if kind == "money":
        return f"{float(raw):,.2f}"
    return f"{float(raw):,.4f}"


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    if not rows:
        return ["（无）", ""]
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    lines.append("")
    return lines


def _short(text: Any, length: int = 12) -> str:
    return "—" if not text else str(text)[:length]


def _evidence_section(evidence: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = ["## 证据链", ""]
    if not evidence:
        return [*lines, "（本次报告没有可回链的事件证据）", ""]
    for index, item in enumerate(evidence, start=1):
        lines.append(f"### {index}. {item.get('title') or '（无标题）'}")
        lines.append(
            f"- 事发（event_time）：{item.get('event_time') or '—'} ｜ "
            f"可得（available_at）：{item.get('available_at') or '—'}"
        )
        lines.append(f"- 摘要：{item.get('summary') or '—'}")
        lines.append(
            f"- 来源：{item.get('source') or '—'} ｜ 原始来源：{item.get('original_source') or '—'}"
            f" ｜ 内容哈希：{_short(item.get('content_hash'))}"
        )
        if item.get("source_url"):
            lines.append(f"- 原文：{item['source_url']}")
        if item.get("revised"):
            lines.append(
                f"- ⚠ 该事件已被平台修订（快照 {_short(item.get('content_hash'))} → "
                f"现在 {_short(item.get('corpus_hash'))}）"
            )
        if not item.get("found", True):
            lines.append("- ⚠ 本地语料查无此行（按决策时的快照如实展示）")
        lines.append("")
    return lines


def _extra_sections(body: Mapping[str, Any]) -> list[str]:
    """容器没专门渲染的块（M8 追加的）按 `text` 逐段带出，附 note。"""
    lines: list[str] = []
    for block in body.get("blocks") or []:
        if block.get("id") in _HANDLED_BLOCKS:
            continue
        lines.append(f"## {block.get('title') or block.get('id')}")
        lines.append("")
        if block.get("text"):
            lines.append(str(block["text"]))
        if block.get("note"):
            lines.append(f"（{block['note']}）")
        lines.append("")
    return lines


def render_markdown(body: Mapping[str, Any], *, report_hash_value: str | None = None) -> str:
    """冻结正文 → Markdown。同一份正文永远同一份文件（可 diff、可归档）。"""
    account = body.get("account") or {}
    metrics = body.get("metrics") or {}
    attribution = body.get("attribution") or {}
    narrative = (body.get("blocks") or [{}])[-1]

    lines: list[str] = [
        f"# {account.get('name') or '模拟盘'} · 策略复盘研报",
        "",
        f"> 策略 {account.get('strategy_name') or account.get('strategy') or '—'}"
        f" ｜ 标的 {'、'.join(account.get('symbols') or []) or '—'}"
        f" ｜ 区间 {account.get('start')} → {account.get('as_of')}"
        f" ｜ 数据止于 {account.get('data_end')}",
        f"> 报告指纹 {_short(report_hash_value)}"
        f" ｜ 数据快照 {_short((body.get('snapshot') or {}).get('bars', {}).get('digest'))}",
        "",
        "## 绩效",
        "",
    ]
    lines += _table(
        ("指标", "数值"),
        [[label, _value(metrics.get(key), kind)] for key, label, kind in METRIC_LABELS],
    )
    benchmark = body.get("benchmark") or {}
    if benchmark.get("note"):
        lines += [f"基准口径：{benchmark['note']}", ""]

    lines += ["## 归因", "", "### 标的级", ""]
    summary = attribution.get("summary") or {}
    if summary:
        lines += [
            f"回合合计（**按回合重算**）：{summary.get('trips')} 笔（已平仓 "
            f"{summary.get('closed')}）｜已实现 {_value(summary.get('trips_realized_pnl'), 'money')}"
            f"｜未平仓浮盈 {_value(summary.get('trips_unrealized_pnl'), 'money')}"
            f"｜未估值 {summary.get('unmarked')} 笔",
            "",
        ]
    lines += _table(
        ("标的", "回合", "已平仓", "胜", "已实现盈亏", "未平仓浮盈", "贡献(pp)", "未估值"),
        [
            [
                row.get("symbol"),
                str(row.get("trips")),
                str(row.get("closed")),
                str(row.get("wins")),
                _value(row.get("realized_pnl"), "money"),
                _value(row.get("unrealized_pnl"), "money"),
                _value(row.get("contribution_pp"), "num"),
                str(row.get("unmarked")),
            ]
            for row in attribution.get("symbols") or []
        ],
    )
    lines += ["### 驱动事件方向", ""]
    lines += _signal_table(attribution.get("direction") or [])
    lines += ["### 驱动事件行业", ""]
    lines += _signal_table(attribution.get("industry") or [])
    lines += [
        "> 行业口径 = 驱动事件自身的 industries（**本地无标的行业分类数据**，M2a 已核）；"
        "一笔回合计入其驱动事件的每个行业，故各行业之和大于整体是正常的。",
        "",
    ]

    lines += _extra_sections(body)

    lines += ["## 综述", ""]
    if narrative.get("text"):
        lines += [str(narrative["text"]), ""]
    else:
        lines += [f"（{narrative.get('note') or '本次没有综述'}）", ""]
    lines += [
        f"*模型 {narrative.get('model') or '—'} · prompt {narrative.get('prompt_version') or '—'}"
        "（本节为模型综合，标注为推断型）*",
        "",
    ]

    lines += _evidence_section(body.get("evidence") or [])

    warnings = body.get("warnings") or []
    lines += ["## 如实标注", ""]
    lines += [f"- {item}" for item in warnings] or ["- （无）"]
    lines += ["", "---", "", DISCLAIMER, ""]
    return "\n".join(lines)


def _signal_table(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    return _table(
        ("分组", "回合", "已平仓", "胜", "盈亏", "未估值"),
        [
            [
                row.get("label"),
                str(row.get("trips")),
                str(row.get("closed")),
                str(row.get("wins")),
                _value(row.get("pnl"), "money"),
                str(row.get("unmarked")),
            ]
            for row in rows
        ],
    )
