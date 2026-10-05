"""T2 事件语料：经 MCP get_event_timeline 按标的拉取 PIT 事件 → data/events/。

在线 PIT 窗口约 3 个月（更早的窗口一律返回空），本期取 2026-07-05 → 2026-09-30。
落盘即冻结：窗口参数写进 data/_meta/events.json，重跑不会静默换窗口。
原始响应留档 data/raw/events/，溯源用 source + original_source + content_hash + quality_status
（数据源没有 source_verified 字段）。

用法：cd backend && uv run python scripts/download_events.py [--since 2026-07-05] [--to 2026-09-30]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import duckdb  # noqa: E402
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

import app  # noqa: E402,F401 —— 导入即锁死 LANGGRAPH_STRICT_MSGPACK
from app.core.config import REPO_ROOT  # noqa: E402
from app.data.xiaoshi import mcp_stdio_params  # noqa: E402

SYMBOLS: dict[str, str] = {"600519": "贵州茅台", "300750": "宁德时代", "600036": "招商银行"}
DEFAULT_SINCE = "2026-07-05"
DEFAULT_TO = "2026-09-30"
TOOL = "get_event_timeline"
PAGE_LIMIT = 500
MAX_PAGES = 5
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 5
RETRYABLE_MARKERS = ("HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504")
ABORT_MARKERS = ("HTTP 429", "bulk_download_required")

# SPEC §3 保留字段 + 落盘类型。类型显式声明，不靠 DuckDB 推断（三份文件推断结果可能不一致）；
# 字段与类型同源，避免「加了字段忘改类型」导致 read_json 静默写 NULL。
COLUMN_TYPES: dict[str, str] = {
    "event_id": "VARCHAR",
    "event_type": "VARCHAR",
    "title": "VARCHAR",
    "summary": "VARCHAR",
    "event_time": "TIMESTAMPTZ",
    "available_at": "TIMESTAMPTZ",
    "observed_at": "TIMESTAMPTZ",
    "direction": "VARCHAR",
    "direction_norm": "VARCHAR",
    "confidence": "DOUBLE",
    "importance_score": "DOUBLE",
    "factor_value": "DOUBLE",
    # 键集随事件类型变化（有的含 macro_profile），强推 struct 会因推断差异炸掉整批落盘
    "factor_scores": "JSON",
    # 实测形状稳定：industries 恒为 list[str]，stocks 元素恒为 {code,name,reason}
    "industries": "VARCHAR[]",
    "stocks": "STRUCT(code VARCHAR, name VARCHAR, reason VARCHAR)[]",
    "source": "VARCHAR",
    "original_source": "VARCHAR",
    "content_hash": "VARCHAR",
    "quality_status": "VARCHAR",
    "source_time_quality": "VARCHAR",
    # 分区键：查询层按标的过滤用，非数据源字段
    "symbol": "VARCHAR",
}
FIELDS: tuple[str, ...] = tuple(name for name in COLUMN_TYPES if name != "symbol")
COLUMNS = "{" + ", ".join(f"'{name}': '{kind}'" for name, kind in COLUMN_TYPES.items()) + "}"
JSON_FIELDS = ("factor_scores",)

DIRECTION_MAP: dict[str, str] = {
    "利多": "bullish", "bullish": "bullish", "positive": "bullish", "看多": "bullish",
    "利空": "bearish", "bearish": "bearish", "negative": "bearish", "看空": "bearish",
    "中性": "neutral", "neutral": "neutral",
}

DEFAULT_RAW_EVENTS_DIR = REPO_ROOT / "data" / "raw" / "events"
DEFAULT_EVENTS_DIR = REPO_ROOT / "data" / "events"
DEFAULT_META_DIR = REPO_ROOT / "data" / "_meta"


def _text_of(result) -> str:
    return " ".join(getattr(block, "text", "") or "" for block in result.content).strip()


async def call_with_retry(session: ClientSession, args: dict) -> dict:
    """服务端过滤路径实测会间歇性 5xx，故允许有限重试；限流与保护语义一律立即中止。

    429 与 bulk_download_required 是平台明确要求停下的信号，重试只会加重问题。
    """
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        result = await session.call_tool(TOOL, args)
        if not result.isError:
            return json.loads(_text_of(result))
        text = _text_of(result)
        if any(marker in text for marker in ABORT_MARKERS):
            raise RuntimeError(f"服务端要求停止，不重试：{text[:300]}")
        if attempt < RETRY_ATTEMPTS and any(marker in text for marker in RETRYABLE_MARKERS):
            delay = RETRY_BACKOFF_SECONDS * attempt
            print(f"    服务端 5xx（第 {attempt} 次），{delay}s 后重试…", flush=True)
            await asyncio.sleep(delay)
            continue
        raise RuntimeError(f"事件拉取失败：{text[:300]}")
    raise AssertionError("unreachable")


async def fetch_symbol(session: ClientSession, symbol: str, since: str, to: str) -> dict:
    """单个标的的事件；游标翻页有上限，不无限循环。"""
    records: list[dict] = []
    cursor: str | None = None
    pages = 0
    while True:
        args: dict = {"since": since, "to": to, "stock": symbol, "limit": PAGE_LIMIT}
        if cursor:
            args["cursor"] = cursor
        payload = await call_with_retry(session, args)
        echoed = (payload.get("filters") or {}).get("stock")
        if echoed not in (None, symbol):
            raise RuntimeError(f"{symbol} 过滤回显不符：{echoed}")
        records.extend(payload.get("data") or [])
        pages += 1
        cursor = payload.get("next_cursor")
        if not cursor or pages >= MAX_PAGES:
            break
    return {"pages": pages, "count": len(records), "truncated": bool(cursor), "records": records}


def normalize(record: dict, symbol: str) -> dict:
    out: dict = {}
    for field in FIELDS:
        value = record.get(field)
        out[field] = json.dumps(value, ensure_ascii=False) if field in JSON_FIELDS else value
    out["symbol"] = symbol
    out["direction_norm"] = DIRECTION_MAP.get(str(record.get("direction") or "").strip())
    return out


async def fetch_all(since: str, to: str, raw_dir: Path) -> dict:
    raw_dir.mkdir(parents=True, exist_ok=True)
    params = StdioServerParameters(**mcp_stdio_params())
    per_symbol: dict[str, dict] = {}
    with open("/dev/null", "w") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                print(f"MCP 已连接：{init.serverInfo.name} {getattr(init.serverInfo, 'version', '')}")
                for symbol, name in SYMBOLS.items():
                    result = await fetch_symbol(session, symbol, since, to)
                    records = [normalize(record, symbol) for record in result["records"]]
                    target = raw_dir / f"{symbol}.jsonl"
                    target.write_text(
                        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
                        encoding="utf-8",
                    )
                    per_symbol[symbol] = {
                        "name": name,
                        "pages": result["pages"],
                        "count": result["count"],
                        "truncated": result["truncated"],
                        "raw_path": str(target.relative_to(REPO_ROOT)),
                        "raw_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    }
                    print(f"  {symbol} {name}：{result['count']} 条（{result['pages']} 页）", flush=True)
    return per_symbol


def reuse_raw(raw_dir: Path) -> dict:
    """复用已落盘的 JSONL 重建清单（改落盘形态时不必再打一次接口）。"""
    per_symbol: dict[str, dict] = {}
    for symbol, name in SYMBOLS.items():
        source = raw_dir / f"{symbol}.jsonl"
        if not source.exists():
            raise RuntimeError(f"缺少原始响应：{source}（去掉 --skip-fetch 重新拉取）")
        per_symbol[symbol] = {
            "name": name,
            "pages": None,
            "count": sum(1 for _ in source.open(encoding="utf-8")),
            "truncated": None,
            "raw_path": str(source.relative_to(REPO_ROOT)),
            "raw_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        }
        print(f"  复用 {source.name}：{per_symbol[symbol]['count']} 条")
    return per_symbol


def _clean_stale(events_dir: Path) -> None:
    """清掉上次失败留下的中间文件：它们会被查询层的 glob 捞进去，报「不是 Parquet」。"""
    for stale in events_dir.glob("tmp_*.parquet"):
        stale.unlink()


def materialize(raw_dir: Path, events_dir: Path) -> list[dict]:
    """JSONL → Parquet；顺带核 PIT 不变量，不合格直接中止。

    先写暂存文件、校验通过后再原子替换，避免失败时留下半截文件污染查询层的 glob。
    """
    events_dir.mkdir(parents=True, exist_ok=True)
    _clean_stale(events_dir)
    outputs: list[dict] = []
    con = duckdb.connect()
    try:
        for symbol in SYMBOLS:
            source = raw_dir / f"{symbol}.jsonl"
            if not source.exists():
                raise RuntimeError(f"缺少原始响应：{source}")
            target = events_dir / f"{symbol}.parquet"
            staging = events_dir / f".{symbol}.partial.parquet"
            staging.unlink(missing_ok=True)
            try:
                con.execute(
                    f"COPY (SELECT * FROM read_json({_sql_str(str(source))},"
                    f" format='newline_delimited', columns={COLUMNS}) ORDER BY event_time, event_id)"
                    f" TO {_sql_str(str(staging))} (FORMAT PARQUET)"
                )
                rows, null_avail, pit_violations = con.execute(
                    "SELECT count(*),"
                    " count(*) FILTER (WHERE available_at IS NULL),"
                    " count(*) FILTER (WHERE available_at < event_time)"
                    f" FROM read_parquet({_sql_str(str(staging))})"
                ).fetchone()
                # direction 里混有「高管人事/股权变动」这类非情绪标签，归为 NULL（无方向语义），
                # 不臆造 sentiment；分布记进清单备审计
                unmapped = con.execute(
                    "SELECT coalesce(direction, '<null>') AS d, count(*)"
                    f" FROM read_parquet({_sql_str(str(staging))})"
                    " WHERE direction_norm IS NULL GROUP BY 1 ORDER BY 2 DESC"
                ).fetchall()
                if rows == 0:
                    raise RuntimeError(f"{target.name} 没有数据")
                if null_avail or pit_violations:
                    raise RuntimeError(
                        f"{target.name} PIT 校验失败：available_at 空值 {null_avail}，"
                        f"available_at < event_time {pit_violations}"
                    )
            except Exception:
                staging.unlink(missing_ok=True)
                raise
            staging.replace(target)
            outputs.append(
                {
                    "path": str(target.relative_to(REPO_ROOT)),
                    "symbol": symbol,
                    "name": SYMBOLS[symbol],
                    "rows": rows,
                    "unmapped_direction": {value: count for value, count in unmapped},
                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                }
            )
    finally:
        con.close()
    return outputs


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def write_meta(meta_dir: Path, since: str, to: str, per_symbol: dict, outputs: list[dict]) -> Path:
    meta_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "schema": "quantsage.events_meta/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "channel": "mcp",
            "tool": TOOL,
            "server": "小石金融数据",
            "note": "在线 PIT 窗口约 3 个月，更早窗口返回空；落盘即冻结，重跑需显式指定窗口",
            "provenance_fields": ["source", "original_source", "content_hash", "quality_status"],
        },
        "window": {"since": since, "to": to},
        "direction_map": DIRECTION_MAP,
        "symbols": per_symbol,
        "outputs": outputs,
    }
    path = meta_dir / "events.json"
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default=DEFAULT_SINCE)
    parser.add_argument("--to", default=DEFAULT_TO)
    parser.add_argument("--skip-fetch", action="store_true", help="复用已有 data/raw/events/ 响应")
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_EVENTS_DIR)
    parser.add_argument("--events-dir", type=Path, default=DEFAULT_EVENTS_DIR)
    parser.add_argument("--meta-dir", type=Path, default=DEFAULT_META_DIR)
    args = parser.parse_args()

    per_symbol = reuse_raw(args.raw_dir) if args.skip_fetch else await fetch_all(args.since, args.to, args.raw_dir)
    outputs = materialize(args.raw_dir, args.events_dir)
    meta_path = write_meta(args.meta_dir, args.since, args.to, per_symbol, outputs)

    total = sum(item["rows"] for item in outputs)
    unmapped = {
        value: count
        for item in outputs
        for value, count in item["unmapped_direction"].items()
    }
    print(f"\n落盘 {len(outputs)} 个文件，共 {total} 条；无方向语义（direction_norm 为 NULL）：{unmapped}")
    print(f"清单 {meta_path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
