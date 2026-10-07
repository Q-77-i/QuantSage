"""退市股覆盖核实（M2c）——生存者偏差核实。

**口径是「本地可得清单」，不是「全市场清单」**：本地没有标的清单文件，小石 10 个数据集
里没有证券主数据 / 退市清单，事件归档是 `(date, event_type)` 分区、**无 symbol 维度**——
SPEC 原话「对照全市场清单」结构上执行不了。故改走数据内双证据链：

1. **全期结构证据**：按 `max(trade_date)` 找出「行情早于数据末端就结束」的标的及其年度
   分布。分片若只含活下来的公司，这里会是空的。
2. **窗口内逐只实证**：事件语料覆盖区间内，末日落在区间里的标的，逐个去语料里找退市类
   事件——找到即**确证**，找不到记**待解释**（长期停牌与退市在纯行情数据里不可辨，如实
   区分，不合并计数）。

**宁可少说**：窗口内能实证的只有个位数。不得把「8 只候选 / 2 只确证」表述成「逐只实证
通过」——那是把「没找到证据」讲成了「证据表明没问题」。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import duckdb

from app.core.config import REPO_ROOT
from app.data import duckdb_client as dc

#: 末次交易日早于数据末端这么多天，才算「停止交易」。短于此的更像临时停牌——
#: 数据层不记录停牌日，用天数把两种情形分开
STOPPED_GAP_DAYS = 30

#: 退市类信号：公告类别里的「退市风险」「停复牌」，或标题里直接出现「退市」
_DELISTING_PREDICATE = (
    "direction IN ('退市风险', '停复牌') OR title LIKE '%退市%'"
)

METHOD_NOTE = (
    "口径：本地可得清单，非全市场清单。本地无标的清单文件，小石无证券主数据 / 退市清单，"
    "事件归档为 (date, event_type) 分区、无 symbol 维度。本报告由「行情末次交易日分布」"
    "与「事件语料窗口内逐只实证」两条数据内证据构成。"
)


@dataclass(frozen=True, slots=True)
class DelistingReport:
    """退市覆盖核实结果。完整清单只进 `--json`，人读只出两行。"""

    method_note: str
    data_end: date
    stopped: tuple[str, ...]
    by_year: dict[int, int]
    window: tuple[date, date] | None
    window_candidates: tuple[str, ...]
    confirmed: tuple[tuple[str, str], ...]
    unexplained: tuple[str, ...]

    @property
    def summary_lines(self) -> list[str]:
        years = "、".join(f"{year}:{count}" for year, count in sorted(self.by_year.items()))
        lines = [
            f"U1 停止交易标的 {len(self.stopped)} 只（末次交易日早于数据末端 {STOPPED_GAP_DAYS} 天以上）：{years}",
        ]
        if self.window is None:
            lines.append("U2 事件语料窗口内无候选（无事件语料时无从实证）")
        else:
            lines.append(
                f"U2 窗口 {self.window[0]} → {self.window[1]} 内候选 {len(self.window_candidates)} 只："
                f"确证 {len(self.confirmed)}、待解释 {len(self.unexplained)}"
            )
        return lines


def _rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[dict]:
    return con.execute(sql).to_arrow_table().to_pylist()


def _evidence_score(title: str) -> int:
    """证据强度：进入退市整理期最硬，其次「退市」二字，再次是「停复牌」这类间接信号。"""
    if "退市整理期" in title:
        return 2
    return 1 if "退市" in title else 0


def audit(con: duckdb.DuckDBPyConnection) -> DelistingReport:
    """从已建好视图的连接上做核实（调用方给连接，便于与体检共用一条）。"""
    data_end = _rows(con, "SELECT max(trade_date) AS data_end FROM bars")[0]["data_end"]
    cutoff = data_end - timedelta(days=STOPPED_GAP_DAYS)

    last_days = _rows(
        con,
        "SELECT symbol, max(trade_date) AS last_day FROM bars"
        " WHERE adjustment = 'qfq' GROUP BY 1 HAVING max(trade_date) <"
        f" DATE '{cutoff.isoformat()}' ORDER BY 2, 1",
    )
    stopped = tuple(item["symbol"] for item in last_days)
    by_year: dict[int, int] = {}
    for item in last_days:
        by_year[item["last_day"].year] = by_year.get(item["last_day"].year, 0) + 1

    window_row = _rows(
        con,
        "SELECT min(event_time)::DATE AS first_day, max(event_time)::DATE AS last_day FROM events",
    )[0]
    first_day, last_day = window_row["first_day"], window_row["last_day"]
    if first_day is None or last_day is None:
        return DelistingReport(
            method_note=METHOD_NOTE,
            data_end=data_end,
            stopped=stopped,
            by_year=by_year,
            window=None,
            window_candidates=(),
            confirmed=(),
            unexplained=(),
        )

    # 候选 = 停止交易的标的里，末日落在事件语料能覆盖到的那一段
    candidates = tuple(
        item["symbol"] for item in last_days if item["last_day"] >= first_day
    )
    evidence: dict[str, str] = {}
    if candidates:
        placeholders = ", ".join(f"'{symbol}'" for symbol in candidates)
        rows = _rows(
            con,
            "SELECT DISTINCT unnest(symbols) AS symbol, title FROM events"
            f" WHERE ({_DELISTING_PREDICATE}) AND list_has_any(symbols, [{placeholders}])",
        )
        # 一个标的可能命中多条，取最能说明问题的那条当证据——「退市整理期」>「退市」> 其余
        for item in rows:
            symbol, title = item["symbol"], item["title"] or ""
            if symbol not in evidence or _evidence_score(title) > _evidence_score(evidence[symbol]):
                evidence[symbol] = title
    confirmed = tuple((symbol, evidence[symbol]) for symbol in candidates if symbol in evidence)
    unexplained = tuple(symbol for symbol in candidates if symbol not in evidence)

    return DelistingReport(
        method_note=METHOD_NOTE,
        data_end=data_end,
        stopped=stopped,
        by_year=by_year,
        window=(first_day, last_day),
        window_candidates=candidates,
        confirmed=confirmed,
        unexplained=unexplained,
    )


def run(data_dir=None) -> DelistingReport:
    """独立入口：自建连接跑一次（体检脚本与 CLI 都用它）。

    取数只走 `bars` / `events` 两个视图的集合式聚合——5,798 只标的，逐标的 API 起步就是
    11ms × 次数。
    """
    con = dc.connect(data_dir if data_dir is not None else REPO_ROOT / "data")
    try:
        return audit(con)
    finally:
        con.close()
