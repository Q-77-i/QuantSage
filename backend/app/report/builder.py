"""冻结产物组装 + claim 校验闸门（M7a）。

**M7 建容器、M8 建生成器**：正文是一串 `blocks`，容器只认结构不认来源——M8 的深度研报
按同一结构追加块（`kind="fact"` 的块要能回链证据或数字），渲染 / 分享 / 导出 / 快照全复用。

闸门（`validate_claims`）钉死三件事，不过**不落库**：

1. `kind="fact"` 的块必须能自证——要么带 `numbers`（键路径指向正文里的数，**值必须对得上**，
   即「可复算」），要么带 `evidence`（引用 `body["evidence"]` 里存在的键）；
2. 块引用的证据键必须在 `body["evidence"]` 里真的存在（悬空引用 = 拒收）；
3. `kind="inference"` 的块只承载模型正文，**不得携带 `numbers`**（推断不能冒充事实）。

关于 `report_hash` 的可复现性：正文里含综述（inference 块），所以「同快照重放同 hash」的
前提是**综述复用已存文本**——接口层的幂等复用（同 `(account, snapshot_hash)` 返回既有行）
就是这条前提的落地。事实层（除综述外的一切）与快照无关地逐字段可复现，另有用例钉住。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from app.backtest.benchmark import MarketBenchmark
from app.backtest.metrics import SHORT_WINDOW_BARS
from app.backtest.types import Side
from app.memory.settle import Trip
from app.paper.types import Decision, DecisionStatus, PaperConfig
from app.report.attribution import signal_attribution, symbol_attribution
from app.report.evidence import EvidenceItem, evidence_key
from app.report.narrative import Narrative
from app.report.performance import performance_metrics
from app.report.snapshot import plain_json

#: 正文结构版本：M8 追加块（辩论 / 多 Agent 取证）时递增，前端据此兼容旧报告
REPORT_VERSION = 1
REPORT_KIND = "paper_account_report"


class ClaimError(RuntimeError):
    """claim 闸门拒收（悬空证据键 / 事实块不自证 / 推断块带数字）。"""


def freeze_review(review: Mapping[str, Any]) -> dict[str, Any]:
    """报告里嵌的复盘：**只留事实**——去掉墙钟时间（`settled_at`）与本次运行读数（`saved`/`reused`）。

    两者都是「这次跑出来的东西」，不是事实：留着它们，`review_hash` 每次结算都会变
    （首次 saved=3/reused=0，二次反过来），报告就再也复用不上了——这是 `report_hash`
    可复现性的一部分，不是清理。
    """
    summary = {
        key: value
        for key, value in (review.get("summary") or {}).items()
        if key not in ("saved", "reused")
    }
    return {
        "summary": summary,
        "settled": [
            {key: value for key, value in item.items() if key != "settled_at"}
            for item in review.get("settled") or []
        ],
        "open": [
            {key: value for key, value in item.items() if key != "settled_at"}
            for item in review.get("open") or []
        ],
        "unfilled": [dict(item) for item in review.get("unfilled") or []],
    }


def _resolve_path(body: Mapping[str, Any], path: str) -> Any:
    """按点分路径取值；任一段不存在返回 `_MISSING`（与「值是 None」区分开）。"""
    node: Any = body
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return _MISSING
        node = node[part]
    return node


_MISSING = object()


def validate_claims(body: Mapping[str, Any]) -> None:
    """闸门：不通过即抛 `ClaimError`（调用方拒收、不落库）。"""
    evidence = body.get("evidence") or []
    known = {item["event_id"] + "|" + item["day"] for item in evidence}
    for block in body.get("blocks") or []:
        kind = block.get("kind")
        block_id = block.get("id", "?")
        if kind == "inference":
            if block.get("numbers"):
                raise ClaimError(f"推断块 {block_id} 不得携带 numbers（推断不能冒充事实）")
            continue
        if kind != "fact":
            raise ClaimError(f"块 {block_id} 的 kind={kind!r} 非法（只认 fact / inference）")
        numbers = block.get("numbers") or {}
        if numbers:
            for path, expected in numbers.items():
                actual = _resolve_path(body, path)
                if actual is _MISSING:
                    raise ClaimError(f"事实块 {block_id} 的数字路径 {path} 在正文里不存在")
                if actual != expected:
                    raise ClaimError(
                        f"事实块 {block_id} 的数字 {path} 与正文不一致（{expected!r} != {actual!r}）"
                    )
            continue
        keys = list(block.get("evidence") or [])
        if not keys:
            raise ClaimError(f"事实块 {block_id} 既没有 numbers 也没有 evidence，无法自证")
        for key in keys:
            if key not in known:
                raise ClaimError(f"事实块 {block_id} 引用了不存在的证据 {key}")


def _block(block_id: str, title: str, *, kind: str = "fact", **extra: Any) -> dict[str, Any]:
    return {"id": block_id, "kind": kind, "title": title, **extra}


def equity_dates(equity_rows: Sequence[Mapping[str, Any]]) -> list[date]:
    """净值序列的交易日（统一回 `date`）。

    `paper.store.equity_from_row` 给的是 **ISO 字符串**（那是 API 响应的形状），而
    `market_benchmark` 按 `date` 查表——直接把字符串喂进去会**全数落空、基准静默变 0**
    （真实数据取证逮出来的：账户跑赢 2.51%、基准却显示 0.00%）。两种形状这里都收。
    """
    return [date.fromisoformat(str(row["trade_date"])) for row in equity_rows]


def build_facts(
    *,
    account: Mapping[str, Any],
    config: PaperConfig,
    decisions: Sequence[Decision],
    equity_rows: Sequence[Mapping[str, Any]],
    trips: Sequence[Trip],
    benchmark: MarketBenchmark | None,
    snapshot: Mapping[str, Any],
    evidence: Mapping[str, EvidenceItem],
    review: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """事实层正文（不含综述）。**纯函数**：同一批输入逐字段相等。"""
    equity = [float(row["equity"]) for row in equity_rows]
    initial_cash = float(config.initial_cash)
    metrics = performance_metrics(
        equity,
        initial_cash,
        trips=trips,
        benchmark_return=(benchmark.total_return if benchmark is not None else None),
    )
    symbols = symbol_attribution(trips, initial_cash)
    buys = [d for d in decisions if d.side is Side.BUY and d.status is DecisionStatus.FILLED]
    signals = signal_attribution(buys, trips, evidence)

    ordered_evidence = sorted(evidence.values(), key=lambda item: (item.day, item.event_id))
    evidence_keys = sorted(
        key
        for key in (
            evidence_key(
                str((d.sources or {}).get("event_id")),
                str((d.sources or {}).get("event_time")),
            )
            for d in buys
        )
        if key and key in evidence
    )

    open_trips = [t for t in trips if t.is_open]
    closed_trips = [t for t in trips if not t.is_open and t.pnl is not None]
    #: 归因块的自证数字：**回合口径**的合计（与账本 `realized_pnl` 同源但有舍入差，
    #: 故键名就叫 `trips_*`，不冒充账本数）。纯价量策略（双均线）没有事件证据，
    #: 归因块就靠这组数自证——没有它，闸门会把整份报告拒收。
    attribution_summary = {
        "trips": len(trips),
        "closed": len(closed_trips),
        "symbols": len({t.symbol for t in trips}),
        "trips_realized_pnl": sum(t.pnl or 0.0 for t in closed_trips),
        "trips_unrealized_pnl": sum(t.pnl or 0.0 for t in open_trips),
        "unmarked": sum(1 for t in open_trips if t.pnl is None),
    }
    warnings: list[str] = []
    market_end = snapshot.get("market_end")
    if open_trips:
        unmarked = sum(1 for t in open_trips if t.pnl is None)
        warnings.append(
            f"{len(open_trips)} 笔买入尚未平仓（数据止于 {market_end}），未到期不结算"
            + (f"，其中 {unmarked} 笔取不到收盘价、浮盈留空" if unmarked else "")
        )
    if len(equity) < SHORT_WINDOW_BARS:
        warnings.append(
            f"区间只有 {len(equity)} 个交易日（< {SHORT_WINDOW_BARS}）："
            "年化与夏普按 252 折算会放大噪声，如实标注"
        )
    revised = [item for item in ordered_evidence if item.revised]
    if revised:
        warnings.append(f"{len(revised)} 条证据被平台修订过（报告内已标注两个 hash）")
    missing = [item for item in ordered_evidence if not item.found]
    if missing:
        warnings.append(f"{len(missing)} 条证据在本地语料里查无此行（如实留空，未编造）")
    if benchmark is not None:
        warnings.append("基准为全市场等权组合代理（数据源不覆盖指数，M2a 已核）")

    blocks = [
        _block(
            "overview",
            "概览",
            text=(
                f"{config.strategy_name or config.strategy} 在 {len(config.symbols)} "
                f"只标的上、自 {config.start.isoformat()} 模拟至 {_as_of(account)}；"
                f"数据止于 {market_end}。"
            ),
            numbers={
                "account.initial_cash": initial_cash,
                "metrics.final_equity": metrics.final_equity,
            },
        ),
        _block(
            "performance",
            "绩效",
            numbers={
                "metrics.total_return": metrics.total_return,
                "metrics.benchmark_return": metrics.benchmark_return,
                "metrics.excess_return": metrics.excess_return,
                "metrics.max_drawdown": metrics.max_drawdown,
                "metrics.sharpe": metrics.sharpe,
                "metrics.volatility": metrics.volatility,
                "metrics.win_rate": metrics.win_rate,
                "metrics.trade_count": metrics.trade_count,
            },
        ),
        _block(
            "attribution",
            "归因",
            text=(
                "标的级：按已实现盈亏与未平仓浮盈合计对初始资金的贡献。"
                "事件级：按驱动事件的方向与行业分组——**本地无标的行业分类数据**，"
                "行业口径是事件自身的 industries，且一笔回合计入其驱动事件的每个行业，"
                "故各行业之和大于整体是正常的。纯价量策略（如双均线）的买入不带事件来源，"
                "归入「无事件来源」组，行业表为空是正常的。"
            ),
            numbers={
                "attribution.summary.trips": attribution_summary["trips"],
                "attribution.summary.trips_realized_pnl": attribution_summary[
                    "trips_realized_pnl"
                ],
            },
            evidence=evidence_keys,
        ),
    ]

    frozen_review: dict[str, Any] | None = None
    if review is not None:
        frozen_review = freeze_review(review)
        summary = frozen_review.get("summary") or {}
        known = {f"{item.event_id}|{item.day.isoformat()}" for item in ordered_evidence}
        review_evidence = sorted(
            {
                str(item["evidence_key"])
                for group in ("settled", "open")
                for item in frozen_review[group]
                if item.get("evidence_key") and str(item["evidence_key"]) in known
            }
        )
        missing_notes = sum(
            1
            for item in frozen_review["settled"]
            if (item.get("reflection") or {}).get("note")
        )
        if missing_notes:
            warnings.append(f"{missing_notes} 条反思不可用（原因见逐笔复盘，未编造）")
        blocks.append(
            _block(
                "review",
                "逐笔复盘",
                text=(
                    "已到期的回合逐笔给出结算与一句话教训（教训是模型综合，标注为推断型）；"
                    "期末仍未平仓的标「未到期」——数据止于 where，没有结果就不总结教训；"
                    "定了没交易的决策如实列状态，不做反事实收益。"
                ).replace("where", str(market_end)),
                numbers={
                    "review.summary.settled": summary.get("settled"),
                    "review.summary.open": summary.get("open"),
                    "review.summary.lessons": summary.get("lessons"),
                    "review.summary.unfilled": summary.get("unfilled"),
                },
                evidence=review_evidence,
            )
        )

    return {
        "version": REPORT_VERSION,
        "kind": REPORT_KIND,
        "account": {
            "id": account.get("id"),
            "name": account.get("name"),
            "status": account.get("status"),
            "strategy": config.strategy,
            "strategy_name": config.strategy_name,
            "symbols": list(config.symbols),
            "initial_cash": initial_cash,
            "start": config.start.isoformat(),
            "as_of": _as_of(account),
            "data_end": market_end,
        },
        "metrics": _metrics_payload(metrics),
        "benchmark": (
            benchmark.describe()
            if benchmark is not None
            else {"kind": None, "note": "基准取不到（窗口内无行情样本），超额收益留空"}
        ),
        "equity_curve": [
            {
                "date": _iso(row["trade_date"]),
                "equity": float(row["equity"]),
                "benchmark": (
                    float(benchmark.levels[index]) if benchmark is not None else None
                ),
            }
            for index, row in enumerate(equity_rows)
        ],
        "review": frozen_review,
        "attribution": {
            "summary": attribution_summary,
            "symbols": [row.to_payload() for row in symbols],
            "direction": [row.to_payload() for row in signals["direction"]],
            "industry": [row.to_payload() for row in signals["industry"]],
        },
        "blocks": blocks,
        "evidence": [item.to_payload() for item in ordered_evidence],
        "snapshot": dict(snapshot),
        "warnings": warnings,
    }


def assemble_report(facts: Mapping[str, Any], narrative: Narrative) -> dict[str, Any]:
    """事实层 + 综述 → 冻结产物正文（`report_hash` 在它上面算）。

    最后一步是 `plain_json`：把 UUID / Decimal / date 一次性翻成纯 JSON 类型——
    落库（psycopg 的 Jsonb 不认 UUID）、分享、导出、算 hash 用的一定是同一棵树。
    """
    body = dict(facts)
    body["blocks"] = [
        *facts["blocks"],
        _block(
            "narrative",
            "综述",
            kind="inference",
            text=narrative.text,
            note=narrative.note,
            model=narrative.model,
            prompt_version=narrative.prompt_version,
        ),
    ]
    body = plain_json(body)
    validate_claims(body)
    return body


def _metrics_payload(metrics: Any) -> dict[str, Any]:
    return {
        "total_return": metrics.total_return,
        "annual_return": metrics.annual_return,
        "max_drawdown": metrics.max_drawdown,
        "sharpe": metrics.sharpe,
        "volatility": metrics.volatility,
        "win_rate": metrics.win_rate,
        "trade_count": metrics.trade_count,
        "final_equity": metrics.final_equity,
        "benchmark_return": metrics.benchmark_return,
        "excess_return": metrics.excess_return,
    }


def _as_of(account: Mapping[str, Any]) -> str | None:
    value = account.get("as_of")
    return _iso(value) if value is not None else None


def _iso(value: Any) -> str:
    if isinstance(value, (date,)):
        return value.isoformat()
    return str(value)
