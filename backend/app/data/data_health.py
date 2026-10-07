"""数据质量体检（M2c）——`scripts/run_health_check.py` 的引擎，规格见 SPEC §3 M2c。

**三条硬规则（改动前先读）**

1. **每个检查自带 try/except**：异常折成 `level="error"` 的 `Check`。一条 SQL 炸掉不该
   吞掉整份报告——「哪一项没跑成」本身就是要看得见的信息。
2. **全部检查共用一条连接**：`run_all()` 建一次 `duckdb_client.connect(data_dir)` 传给每个
   检查；`DataNotReady` 时直接生成一条 error Check 并短路（不打 traceback）。
3. **集合式 SQL、一条连接**：禁止 `bars()` / `latest_closes()` 这类逐标的 API（5,798 只
   × 每次 connect 起步 11ms），禁止对全量结果 `to_pylist()`；读 TIMESTAMPTZ 必须走
   arrow——缺 pytz 时裸 `fetchall()` 直接抛 `Invalid Input Error`。

**分级口径**：`error` = 不变量被破坏（重复行、倒挂、分片缺失或 sha 不符、日历不一致、
分片截断、台账损坏、交易日上的事件缺口）；`warn` = 需要人看一眼；`info` = 记录与基线。

`BASELINE` 里的数字是 2026-10-07 的实测值，**只用于报告里做对照，不是硬阈值**——数据每天
在长，把基线写成断言等于给自己埋雷。判级只由不变量决定。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Literal

import duckdb

from app.core.config import REPO_ROOT
from app.data import calendar as cal
from app.data import duckdb_client as dc
from app.etl import runner as etl_runner
from app.etl import store as etl_store

Level = Literal["error", "warn", "info"]

#: 冻结日历余量低于此天数即告警——到期 `CalendarOutOfRange` 会硬停 ETL 与回测
CALENDAR_RUNWAY_WARN_DAYS = 90
#: B5 自洽性容差（百分点）：close 环比与 `change_pct` 的允许偏差。**必须写死**——
#: 容差取 0 时实测命中 762 万条，判据就不可复现了。
STEP_TOLERANCE_PP = 0.011
#: B7 相邻交易日标的数跌幅：超 10% 告警
ENVELOPE_DROP = 0.10
#: 涨跌幅理论上限：北交所 30%。超它的必须是复牌首日/重新上市这类参考价重置
MAX_DAILY_MOVE_PCT = 31.0
#: 允许的 `trading_status`。`unknown` 实测存在（仅 qfq、5,099 行、价格齐全、无消费方）
#: 但**故意不在白名单里**——它需要一次归类决策，报告要把它顶出来
TRADING_STATUS_OK = ("traded", "no_turnover_observed", "suspended")
#: `direction_norm` 只可能由 `etl.store.DIRECTION_MAP` 产生；NULL 是「类别不可映射」的正常结果
DIRECTION_NORM_OK = ("bullish", "bearish", "neutral")

#: 2026-10-07 实测基线。只作对照，不作阈值。
BASELINE: dict[str, object] = {
    "bars_rows": 23_972_942,
    "bars_symbols": 5_798,
    "bars_window": ("2020-01-02", "2026-09-30"),
    "events_rows": 289_519,
    "events_days": 84,
    "no_price_rows": 271,  # 每个复权口径
    "no_price_symbols": 49,
    "step_self_consistency": {"hfq": 40, "qfq": 448, "raw": 28_686},
    "step_over_limit": 106,
    "env_min": 3_761,
    "env_max": 5_572,
    "event_id_reuse": 18,
    "empty_symbols": 102_080,
    "legacy_files": 3,
    "calendar_runway_days": 85,
}
#: B5 自洽性命中超过基线的这个倍数才告警——单日新增不可能把它推高，只有口径变化会
STEP_ALERT_FACTOR = 10


@dataclass(frozen=True, slots=True)
class Check:
    """一项检查的结论。`data` 放机读明细——清单只进这里，不撑爆人读报告。"""

    id: str
    level: Level
    title: str
    detail: str = ""
    data: dict = field(default_factory=dict)


# ── 取数小工具 ──────────────────────────────────────────────


def _rows(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> list[dict]:
    # arrow 而非 fetchall：TIMESTAMPTZ 的 Python 转换依赖已废弃的 pytz（同 duckdb_client）
    return con.execute(sql, params or []).to_arrow_table().to_pylist()


def _one(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> dict:
    rows = _rows(con, sql, params)
    return rows[0] if rows else {}


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _pct(part: int, total: int) -> str:
    return f"{part:,}（{100 * part / total:.4f}%）" if total else f"{part:,}"


def _read_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"缺少清单 {path}——先跑 scripts/download_bars.py / download_events.py")
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _data_dir(data_dir: Path | None) -> Path:
    return Path(data_dir) if data_dir is not None else REPO_ROOT / "data"


def _brief(items: list[str], limit: int = 3) -> str:
    if not items:
        return "无"
    head = "、".join(items[:limit])
    return f"{head} 等 {len(items)} 项" if len(items) > limit else head


# ── 行情（bars）────────────────────────────────────────────


def check_b1_shards(con, data_dir) -> Check:
    """分片对账：`_meta/bars.json` 声明 vs 磁盘实际。sha 逐片重算——清单是记录不是测量。"""
    base = _data_dir(data_dir)
    manifest = _read_json(base / "_meta" / "bars.json")
    declared = {Path(item["path"]).name: item for item in manifest.get("outputs", [])}
    actual = {path.name: path for path in sorted((base / "bars").glob("*.parquet"))}

    missing = sorted(set(declared) - set(actual))
    extra = sorted(set(actual) - set(declared))
    bad: list[str] = []
    for name in sorted(set(declared) & set(actual)):
        item, path = declared[name], actual[name]
        count = _one(con, f"SELECT count(*) AS n FROM read_parquet({_sql_str(str(path))})")["n"]
        if count != item["rows"] or _sha256(path) != item["sha256"]:
            bad.append(name)

    problems = len(missing) + len(extra) + len(bad)
    detail = (
        f"清单声明 {len(declared)} 片、磁盘 {len(actual)} 片；"
        f"缺片 {len(missing)}、多片 {len(extra)}、行数或 sha 不符 {len(bad)}"
    )
    if problems:
        detail += f"；{'；'.join(filter(None, [
            f'缺 {_brief(missing)}' if missing else '',
            f'多 {_brief(extra)}' if extra else '',
            f'不符 {_brief(bad)}' if bad else '',
        ]))}"
    return Check(
        id="B1",
        level="error" if problems else "info",
        title="分片与清单一致" if not problems else f"分片与清单不一致（{problems} 处）",
        detail=detail,
        data={"missing": missing, "extra": extra, "mismatched": bad},
    )


def check_b2_duplicates(con, data_dir) -> Check:
    """`(symbol, trade_date, adjustment)` 唯一——重复即同一根 bar 被算两遍。"""
    dupes = _rows(
        con,
        "SELECT symbol, trade_date, adjustment, count(*) AS n FROM bars"
        " GROUP BY 1, 2, 3 HAVING count(*) > 1 ORDER BY n DESC",
    )
    labels = [f"{r['symbol']}@{r['trade_date']}×{r['n']}" for r in dupes]
    return Check(
        id="B2",
        level="error" if dupes else "info",
        title="无重复 bar" if not dupes else f"重复 bar（{len(dupes)} 组）",
        detail=f"重复键 {len(dupes)} 组" + (f"：{_brief(labels)}" if dupes else "（基线 0）"),
        data={"duplicates": labels[:50]},
    )


def check_b3_nulls(con, data_dir) -> Check:
    """缺失值比例。逐列只报数（多处属实设计）；**只对「有成交却无价」设不变量**。"""
    columns = [
        "open", "high", "low", "close", "volume", "amount",
        "change_pct", "turnover_pct", "adj_factor", "available_at",
        "is_suspended", "trading_status",
    ]
    picked = ", ".join(f"count(*) FILTER (WHERE {c} IS NULL) AS \"{c}\"" for c in columns)
    row = _one(con, f"SELECT count(*) AS total, {picked} FROM bars")
    total = row["total"]
    # 无价即无价、不是零价：只有「没成交」允许整行价为空，别处出现空价就是数据坏了
    stray = _one(
        con,
        "SELECT count(*) AS n FROM bars WHERE close IS NULL"
        " AND trading_status <> 'no_turnover_observed'",
    )["n"]
    ratios = "、".join(
        f"{c} {_pct(int(row[c]), total)}" for c in columns if row[c]
    ) or "各列均无空值"
    return Check(
        id="B3",
        level="error" if stray else "info",
        title="缺失值比例正常" if not stray else f"有 {stray} 行无价却非「未观察到成交」",
        detail=(
            f"共 {total:,} 行；非空列略，有空值的列：{ratios}。"
            f"「有成交却无价」例外 {stray} 行（基线 0）"
        ),
        data={f"null_{c}": int(row[c]) for c in columns} | {"total": total, "stray": stray},
    )


def check_b4_no_price(con, data_dir) -> Check:
    """无价 bar 计数——与 `duckdb_client.bars()` 的剔除口径显式对齐。"""
    buckets = _rows(
        con,
        "SELECT adjustment, count(*) AS n, count(DISTINCT symbol) AS symbols,"
        " min(trade_date) AS first_day, max(trade_date) AS last_day"
        " FROM bars WHERE close IS NULL GROUP BY 1 ORDER BY 1",
    )
    if not buckets:
        return Check(id="B4", level="info", title="无无价 bar", detail="基线 271 行/复权")
    per_adjust = {b["adjustment"]: b["n"] for b in buckets}
    first = min(b["first_day"] for b in buckets)
    last = max(b["last_day"] for b in buckets)
    symbols = max(b["symbols"] for b in buckets)  # 各复权口径的只数未必相同，取并集上界
    return Check(
        id="B4",
        level="info",
        title=f"无价 bar {buckets[0]['n']} 行/复权",
        detail=(
            f"按复权 {per_adjust}；{symbols} 只，{first} → {last}。"
            f"`bars()` 与 `latest_closes()` 已按同口径剔除（基线 {BASELINE['no_price_rows']} 行/复权）"
        ),
        data={"by_adjust": per_adjust, "first_day": str(first), "last_day": str(last)},
    )


def check_b5_steps(con, data_dir) -> Check:
    """异常步长，两条分开报：涨跌幅越界（粗）与 close 环比对不上 `change_pct`（细）。"""
    over = _rows(
        con,
        f"""
        WITH ranked AS (
            SELECT symbol, trade_date, change_pct, trading_status,
                   row_number() OVER (PARTITION BY symbol ORDER BY trade_date) AS rn
            FROM bars WHERE adjustment = 'qfq' AND change_pct IS NOT NULL
        )
        SELECT symbol, trade_date, change_pct FROM ranked
        WHERE rn > 5 AND abs(change_pct) > {MAX_DAILY_MOVE_PCT}
        ORDER BY abs(change_pct) DESC
        """,
    )
    # 前 5 个交易日不参与：新股上市初期无涨跌幅限制，那是规则不是异常
    labels = [f"{r['symbol']}@{r['trade_date']} {r['change_pct']:.1f}%" for r in over]

    inconsistent: dict[str, int] = {}
    beyond_1pp: dict[str, int] = {}
    adjusts = [r["adjustment"] for r in _rows(con, "SELECT DISTINCT adjustment FROM bars ORDER BY 1")]
    for adjust in adjusts:
        row = _one(
            con,
            f"""
            WITH step AS (
                SELECT close, change_pct,
                       lag(close) OVER (PARTITION BY symbol ORDER BY trade_date) AS prev_close
                FROM bars WHERE adjustment = ? AND close IS NOT NULL
            )
            SELECT count(*) FILTER (WHERE abs((close / prev_close - 1) * 100 - change_pct) > ?) AS n,
                   count(*) FILTER (WHERE abs((close / prev_close - 1) * 100 - change_pct) > 1) AS n_1pp
            FROM step WHERE prev_close IS NOT NULL AND change_pct IS NOT NULL
            """,
            [adjust, STEP_TOLERANCE_PP],
        )
        inconsistent[adjust] = int(row["n"])
        beyond_1pp[adjust] = int(row["n_1pp"])

    # raw 复权本就与 change_pct 不同源（M2a 实测不一致率是 qfq 的 60 倍），只报不判
    # 没见过的复权口径按基线 0 处理——任何命中都值得看一眼，而 raw 已知不干净，只报不判
    known = BASELINE["step_self_consistency"]
    alerts = [
        adjust
        for adjust, n in inconsistent.items()
        if adjust != "raw" and n > STEP_ALERT_FACTOR * int(known.get(adjust, 0))
    ]
    level: Level = "warn" if over or alerts else "info"
    pieces = []
    if over:
        pieces.append(f"单日涨跌超 {MAX_DAILY_MOVE_PCT:.0f}% 且非上市初期：{len(over)} 条（{_brief(labels)}）")
    else:
        pieces.append(f"无单日涨跌超 {MAX_DAILY_MOVE_PCT:.0f}% 的行")
    pieces.append(
        f"自洽性（容差 {STEP_TOLERANCE_PP}pp，>1pp 另计）：{inconsistent}／{beyond_1pp}"
    )
    pieces.append(f"对照基线 {BASELINE['step_self_consistency']}")
    return Check(
        id="B5",
        level=level,
        title="步长正常" if not over and not alerts else f"步长需复核（越界 {len(over)} 条）",
        detail="。".join(pieces),
        data={
            "over_limit": labels[:50],
            "inconsistent": inconsistent,
            "beyond_1pp": beyond_1pp,
            "tolerance_pp": STEP_TOLERANCE_PP,
        },
    )


def check_b6_status(con, data_dir) -> Check:
    """`trading_status` 取值白名单——出现新取值意味着下游的过滤口径要重看一眼。"""
    buckets = _rows(
        con,
        "SELECT trading_status, adjustment, count(*) AS n FROM bars GROUP BY 1, 2 ORDER BY 3 DESC",
    )
    unknown = [b for b in buckets if b["trading_status"] not in TRADING_STATUS_OK]
    summary = "、".join(f"{b['trading_status']}({b['adjustment']}) {b['n']:,}" for b in unknown)
    known = "、".join(
        f"{b['trading_status']}({b['adjustment']}) {b['n']:,}"
        for b in buckets
        if b["trading_status"] in TRADING_STATUS_OK
    )
    return Check(
        id="B6",
        level="warn" if unknown else "info",
        title="trading_status 取值在册" if not unknown else f"出现白名单外取值 {summary}",
        detail=(
            f"取值分布：{known}。"
            + (f"白名单外：{summary}——需一次归类决策，本轮只报不改" if unknown else "白名单外：无")
        ),
        data={"unknown": [dict(b) for b in unknown]},
    )


def check_b7_envelope(con, data_dir) -> Check:
    """每日标的数包络——抓的是**分片截断**（某天只落了一半标的）。

    不查「某标的是否少几天」：那是停牌，数据层本来就不记录停牌日（`is_suspended` 只有
    21 行），写成判据会命中 500 只长期停牌股。
    """
    row = _one(
        con,
        f"""
        WITH daily AS (
            SELECT trade_date, count(DISTINCT symbol) AS n FROM bars GROUP BY 1
        ), steps AS (
            SELECT trade_date, n, lag(n) OVER (ORDER BY trade_date) AS prev FROM daily
        )
        SELECT count(*) AS days, min(n) AS lo, max(n) AS hi,
               count(*) FILTER (WHERE prev IS NOT NULL AND n < prev * {1 - ENVELOPE_DROP}) AS drops,
               arg_min(trade_date, n) AS lo_day
        FROM steps
        """,
    )
    drops = int(row["drops"] or 0)
    return Check(
        id="B7",
        level="error" if drops else "info",
        title="每日标的数连续" if not drops else f"有 {drops} 天标的数骤降（疑似分片截断）",
        detail=(
            f"{row['days']} 个交易日，标的数 {row['lo']:,} → {row['hi']:,}"
            f"（最低 {row['lo_day']}）；相邻日跌幅超 {ENVELOPE_DROP:.0%} 的 {drops} 天"
            f"（基线 {BASELINE['env_min']:,} → {BASELINE['env_max']:,}、0 天）"
        ),
        data={"days": int(row["days"]), "min": int(row["lo"]), "max": int(row["hi"]), "drops": drops},
    )


def check_b8_calendar(con, data_dir) -> Check:
    """交易日历 × 行情 `trade_date` 双向对账（复用 `calendar.audit`，不另写一份）。"""
    days = [r["trade_date"] for r in _rows(con, "SELECT DISTINCT trade_date FROM bars")]
    result = cal.audit(days)
    first, last = result.window
    detail = (
        f"窗口 {first} → {last}；行情 {result.data_days} 个交易日、日历同期 {result.calendar_days} 个"
    )
    if not result.ok:
        detail += (
            f"；仅日历有 {len(result.only_in_calendar)} 天、仅数据有 {len(result.only_in_data)} 天："
            f"{_brief([str(d) for d in (*result.only_in_calendar, *result.only_in_data)])}"
        )
    return Check(
        id="B8",
        level="error" if not result.ok else "info",
        title="日历与行情双向一致" if result.ok else "日历与行情不一致",
        detail=detail,
        data={
            "only_in_calendar": [str(d) for d in result.only_in_calendar],
            "only_in_data": [str(d) for d in result.only_in_data],
        },
    )


# ── 事件（events）───────────────────────────────────────────


def check_e1_partitions(con, data_dir) -> Check:
    """日分区完整性：清单 vs 磁盘 vs 字节。旁注里的 sha 是记录，须重算才算数。"""
    base = _data_dir(data_dir)
    events_dir, raw_dir = base / "events", base / "raw" / "events"
    entries = etl_store.read_day_entries(events_dir, raw_dir)
    on_disk = {e.file for e in entries}
    manifest = _read_json(base / "_meta" / "events.json")
    declared = {d["file"]: d for d in manifest.get("days", [])}

    missing_sidecar = [e.file for e in entries if e.rows < 0]
    only_in_manifest = sorted(set(declared) - on_disk)
    only_on_disk = sorted(on_disk - set(declared))
    corrupt = [
        e.file
        for e in entries
        if e.rows >= 0 and _sha256(events_dir / e.file) != e.sha256
    ]
    rows_off = [
        e.file
        for e in entries
        if e.file in declared and e.rows >= 0 and e.rows != declared[e.file]["rows"]
    ]

    problems = missing_sidecar + only_in_manifest + only_on_disk + corrupt + rows_off
    detail = (
        f"磁盘 {len(entries)} 个日分区、清单 {len(declared)} 个；"
        f"缺旁注 {len(missing_sidecar)}、清单独有 {len(only_in_manifest)}、磁盘独有 {len(only_on_disk)}、"
        f"字节与旁注不符 {len(corrupt)}、行数不符 {len(rows_off)}"
    )
    if problems:
        detail += f"；{_brief(problems)}"
    return Check(
        id="E1",
        level="error" if problems else "info",
        title="日分区完整" if not problems else f"日分区有问题（{len(problems)} 处）",
        detail=detail,
        data={
            "missing_sidecar": missing_sidecar,
            "only_in_manifest": only_in_manifest,
            "only_on_disk": only_on_disk,
            "corrupt": corrupt,
            "rows_off": rows_off,
        },
    )


def check_e2_duplicates(con, data_dir) -> Check:
    """重复行：平台 `dedup_key` 为准（日内），`(event_id, content_hash)` 兜底（全局）。"""
    by_key = _one(
        con,
        "SELECT count(*) AS n FROM (SELECT event_time::DATE AS day, dedup_key FROM events"
        " WHERE dedup_key IS NOT NULL GROUP BY 1, 2 HAVING count(*) > 1)",
    )["n"]
    by_pair = _one(
        con,
        "SELECT count(*) AS n FROM (SELECT event_id, content_hash FROM events"
        " GROUP BY 1, 2 HAVING count(*) > 1)",
    )["n"]
    total = int(by_key) + int(by_pair)
    return Check(
        id="E2",
        level="error" if total else "info",
        title="无重复事件" if not total else f"重复事件（dedup_key {by_key} 组、锚点对 {by_pair} 组）",
        detail=f"日内 dedup_key 重复 {by_key} 组、全局 (event_id, content_hash) 重复 {by_pair} 组（基线 0）",
        data={"dedup_key_groups": int(by_key), "pair_groups": int(by_pair)},
    )


def check_e3_available_at(con, data_dir) -> Check:
    """`available_at` 空值 / 倒挂——倒挂即 PIT 语义被破坏，是不变量。"""
    row = _one(
        con,
        "SELECT count(*) AS total, count(*) FILTER (WHERE available_at IS NULL) AS nulls,"
        " count(*) FILTER (WHERE available_at IS NOT NULL AND event_time IS NOT NULL"
        "   AND available_at < event_time) AS inverted,"
        " count(*) FILTER (WHERE reported_available_at IS NULL) AS reported_nulls"
        " FROM events",
    )
    problems = int(row["nulls"]) + int(row["inverted"])
    return Check(
        id="E3",
        level="error" if problems else "info",
        title="available_at 语义完好" if not problems else "available_at 有空值或倒挂",
        detail=(
            f"共 {row['total']:,} 行；空值 {row['nulls']}、倒挂（< event_time）{row['inverted']}、"
            f"reported_available_at 空 {row['reported_nulls']}（基线 0 / 0 / 0）"
        ),
        data={k: int(row[k]) for k in ("total", "nulls", "inverted", "reported_nulls")},
    )


def check_e4_direction(con, data_dir) -> Check:
    """方向映射：`direction_norm` 是自产列的取值不变量；`direction` 只挑长串脏值。"""
    bad_norm = _rows(
        con,
        "SELECT direction_norm, count(*) AS n FROM events"
        f" WHERE direction_norm IS NOT NULL AND direction_norm NOT IN {DIRECTION_NORM_OK}"
        " GROUP BY 1 ORDER BY 2 DESC",
    )
    dirty = _rows(
        con,
        "SELECT event_id, direction FROM events WHERE length(direction) > 12 ORDER BY event_id",
    )
    labels = [f"{r['event_id']}={r['direction']!r}" for r in dirty]
    level: Level = "error" if bad_norm else ("warn" if dirty else "info")
    detail = (
        f"direction_norm 白名单外 {len(bad_norm)} 种（基线 0；NULL 是「类别不可映射」的正常结果）；"
        f"长串 direction（>12 字符）{len(dirty)} 条（基线 1，源数据损坏）"
        + (f"：{_brief(labels)}" if dirty else "")
    )
    return Check(
        id="E4",
        level=level,
        title="方向映射合法" if not bad_norm and not dirty else f"方向取值待复核（脏值 {len(dirty)} 条）",
        detail=detail,
        data={"bad_norm": [dict(r) for r in bad_norm], "dirty": labels[:50]},
    )


def check_e5_symbols(con, data_dir) -> Check:
    """`symbols` 数组形态。空数组占三分之一是**设计要求**（宏观/政策类事件不挂标的）。"""
    row = _one(
        con,
        "SELECT count(*) AS total, count(*) FILTER (WHERE len(symbols) = 0) AS empty FROM events",
    )
    # UNNEST 不能直接跟在 WHERE 后面用，必须先摊平成子查询
    malformed = _rows(
        con,
        "SELECT s FROM (SELECT DISTINCT unnest(symbols) AS s FROM events)"
        " WHERE NOT regexp_matches(s, '^[0-9]{6}$')",
    )
    uniq = _one(
        con,
        "SELECT count(*) AS n FROM (SELECT DISTINCT unnest(symbols) AS s FROM events)",
    )["n"]
    return Check(
        id="E5",
        level="warn" if malformed else "info",
        title="symbols 形态正常" if not malformed else f"有 {len(malformed)} 个非六位码",
        detail=(
            f"{row['total']:,} 行中空数组 {_pct(int(row['empty']), int(row['total']))}"
            f"（基线 35.3%，设计要求）；展开后 {uniq:,} 只标的；非六位码 {len(malformed)} 个"
        ),
        data={"empty": int(row["empty"]), "unique_symbols": int(uniq),
              "malformed": [r["s"] for r in malformed][:50]},
    )


def check_e6_event_id_reuse(con, data_dir) -> Check:
    """`event_id` 跨日复用——平台行为（按月复发的同题事件复用 id），**永不判 error**。"""
    rows = _rows(
        con,
        "SELECT event_id, count(DISTINCT event_time::DATE) AS days FROM events"
        " GROUP BY 1 HAVING count(DISTINCT event_time::DATE) > 1 ORDER BY days DESC",
    )
    return Check(
        id="E6",
        level="info",
        title=f"event_id 跨日复用 {len(rows)} 例（平台行为）",
        detail=(
            f"{len(rows)} 例、最多跨 {rows[0]['days'] if rows else 0} 天（基线 {BASELINE['event_id_reuse']}）。"
            "查询层不得拿它当主键去重，前端列表 key 必须带时间"
        ),
        data={"reused": [r["event_id"] for r in rows][:50]},
    )


def check_e7_gap(con, data_dir) -> Check:
    """覆盖缺口——**本地算**。

    不复用 `etl.runner.status()`：它调 `archive.coverage()`（子进程调 xiaoshi CLI，需网络与
    密钥、超时 300s），不可达时 `archive_last=None` 使缺口列表静默为空——那会把「查不到
    归档」打印成「缺口 0」。这里只比「本地语料窗口内、磁盘上却没有的日分区」，落在交易日
    上的缺口比自然日缺口严重。
    """
    base = _data_dir(data_dir)
    entries = etl_store.read_day_entries(base / "events", base / "raw" / "events")
    if not entries:
        return Check(id="E7", level="warn", title="本地无事件语料", detail="缺口无从判定")

    present = {e.date for e in entries}
    start = date.fromisoformat(min(present))
    end = date.fromisoformat(max(present))
    # 台账里记为 `empty` 的日子不算缺口——那是归档当天真的没有可收内容（与 status() 同一份语义）
    empty_days = etl_runner.empty_day_set(etl_runner.paths(base))

    missing: list[tuple[str, bool]] = []
    day = start
    while day <= end:
        stamp = day.isoformat()
        if stamp not in present and stamp not in empty_days:
            try:
                on_trading_day = cal.is_session(day)
            except cal.CalendarOutOfRange:
                on_trading_day = False
            missing.append((stamp, on_trading_day))
        day = date.fromordinal(day.toordinal() + 1)

    trading = [stamp for stamp, is_trading in missing if is_trading]
    natural = [stamp for stamp, is_trading in missing if not is_trading]
    level: Level = "error" if trading else ("warn" if natural else "info")
    return Check(
        id="E7",
        level=level,
        title="语料窗口内无缺口" if not missing else f"语料缺口 {len(missing)} 天",
        detail=(
            f"窗口 {start} → {end}（{len(present)} 天有分区）；"
            f"交易日缺口 {len(trading)} 天、自然日缺口 {len(natural)} 天；"
            f"台账记 empty 的 {len(empty_days)} 天不计入"
            + (f"；{_brief([*trading, *natural])}" if missing else "")
        ),
        data={"trading_day_gaps": trading, "natural_day_gaps": natural, "empty_days": sorted(empty_days)},
    )


# ── 日历、遗留物与挂账 ──────────────────────────────────────


def check_x1_calendar_runway(con, data_dir) -> Check:
    """冻结日历余量。到期 `CalendarOutOfRange` 会硬停 ETL 与回测——不是「再等等」能解决的。"""
    first, last = cal.coverage()
    runway = (last - date.today()).days
    return Check(
        id="X1",
        level="warn" if runway < CALENDAR_RUNWAY_WARN_DAYS else "info",
        title=f"日历余量 {runway} 天",
        detail=(
            f"冻结日历 {first} → {last}，余量 {runway} 天（阈值 {CALENDAR_RUNWAY_WARN_DAYS} 天，"
            f"基线 {BASELINE['calendar_runway_days']} 天）。余量耗尽的出路是**升级 "
            "exchange_calendars 后重跑 scripts/generate_calendar.py**——上界受库的假期记录限制"
        ),
        data={"first": str(first), "last": str(last), "runway_days": runway},
    )


def check_x2_legacy_files(con, data_dir) -> Check:
    """P1 遗留物。parquet 侧已清干净，`raw/` 无人管——留着会被日后误读成语料。"""
    base = _data_dir(data_dir)
    legacy = sorted(
        path.name for path in (base / "raw" / "events").glob("*.jsonl")
    )
    lock = base / "raw" / ".xiaoshi-execution.lock"
    lock_note = ""
    if lock.exists():
        age_days = (date.today() - datetime.fromtimestamp(lock.stat().st_mtime).date()).days
        lock_note = f"；锁文件存在（{age_days} 天前，非阻塞信息）"
    return Check(
        id="X2",
        level="warn" if legacy else "info",
        title=f"raw/events 有 {len(legacy)} 个 P1 遗留文件" if legacy else "无遗留文件",
        detail=(
            f"`raw/events/` 下 P1 形状的 `{{symbol}}.jsonl` {len(legacy)} 个"
            f"（基线 {BASELINE['legacy_files']}）：{_brief(legacy)}{lock_note}"
        ),
        data={"legacy": legacy, "lock_exists": lock.exists()},
    )


def check_x3_ledger(con, data_dir) -> Check:
    """`etl_runs.jsonl` 逐行可解析。**宽松解析、缺键容忍**——schema 已演进过。"""
    path = _data_dir(data_dir) / "_meta" / "etl_runs.jsonl"
    if not path.exists():
        return Check(id="X3", level="warn", title="无 ETL 台账", detail=f"{path} 不存在")
    bad: list[str] = []
    scopes: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            scopes.append(json.loads(line).get("scope", "?"))
        except json.JSONDecodeError as exc:
            bad.append(f"第 {number} 行：{exc.msg}")
    return Check(
        id="X3",
        level="error" if bad else "info",
        title="ETL 台账可解析" if not bad else f"台账有 {len(bad)} 行无法解析",
        detail=f"{len(scopes)} 次运行（{_brief(scopes, 5)}）；解析失败 {len(bad)} 行"
        + (f"：{_brief(bad)}" if bad else ""),
        data={"runs": len(scopes), "scopes": scopes, "bad_lines": bad},
    )


def check_g1_datanotready(con, data_dir) -> Check:
    """挂账（只报不修）：`DataNotReady` 判据仍是「目录里有没有 parquet」。"""
    return Check(
        id="G1",
        level="info",
        title="挂账：DataNotReady 判据仍是「目录里有没有 parquet」",
        detail=(
            "`duckdb_client.connect()` 只看 bars/ 与 events/ 有没有文件。物化的原子换入已把"
            "「静默残缺」堵在写入侧，判据本身改成「清单声明的分片全在」留 M5——本模块的 B1/E1 "
            "已经具备该对账能力"
        ),
        data={"owner": "M5"},
    )


def check_g2_fingerprint(con, data_dir) -> Check:
    """挂账（只报不修）：回测落库不含数据版本指纹（M2a 遗留①）。"""
    return Check(
        id="G2",
        level="info",
        title="挂账：backtest_runs 不含数据版本指纹",
        detail=(
            "指纹落在 `_meta/bars.json`（数据目录被 gitignore，不入库）。写进回测落库要动表结构 + "
            "API + 前端回填，留 M5 与基准对比一起做"
        ),
        data={"owner": "M5"},
    )


CHECKS: tuple[Callable[..., Check], ...] = (
    check_b1_shards,
    check_b2_duplicates,
    check_b3_nulls,
    check_b4_no_price,
    check_b5_steps,
    check_b6_status,
    check_b7_envelope,
    check_b8_calendar,
    check_e1_partitions,
    check_e2_duplicates,
    check_e3_available_at,
    check_e4_direction,
    check_e5_symbols,
    check_e6_event_id_reuse,
    check_e7_gap,
    check_x1_calendar_runway,
    check_x2_legacy_files,
    check_x3_ledger,
    check_g1_datanotready,
    check_g2_fingerprint,
)


def run_all(*, data_dir: Path | None = None, only: set[str] | None = None) -> list[Check]:
    """跑完全部检查。数据没落盘时短路成一条 error——不打 traceback。"""
    base = _data_dir(data_dir)
    selected = [fn for fn in CHECKS if not only or fn.__name__.split("_")[1].upper() in only]
    try:
        con = dc.connect(base)
    except dc.DataNotReady as exc:
        return [Check(id="DATA", level="error", title="数据未就绪", detail=str(exc))]

    try:
        results: list[Check] = []
        for fn in selected:
            try:
                results.append(fn(con, base))
            except Exception as exc:  # 一条 SQL 炸掉不该吞掉整份报告
                results.append(
                    Check(
                        id=fn.__name__.split("_")[1].upper(),
                        level="error",
                        title="检查执行失败",
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )
        return results
    finally:
        con.close()


def exit_code(checks: list[Check], *, strict: bool = False) -> int:
    """有 error 退 1；`--strict` 时 warn 也退 1。"""
    levels = {check.level for check in checks}
    if "error" in levels or (strict and "warn" in levels):
        return 1
    return 0


def summarize(checks: list[Check]) -> str:
    """一行总账，给脚本收尾用。"""
    counts = {level: sum(1 for c in checks if c.level == level) for level in ("error", "warn", "info")}
    return (
        f"共 {len(checks)} 项：error {counts['error']}、warn {counts['warn']}、info {counts['info']}"
    )
