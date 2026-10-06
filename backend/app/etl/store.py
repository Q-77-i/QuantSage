"""事件语料落盘：归档分片 → 「全市场按日」的日分区 Parquet + 溯源旁注 + 清单。

三条口径写死在这里（都有实测依据，见 SPEC §3 M2b）：

1. **只收事件，不收状态快照**：归档里的 `sector` / `sector_constituent`（板块成分快照）
   与 `future_dynamic`（未来概率观察流）都是**状态流**而非事件，实测两天 112,866 行里
   占 103,451 行（91.7%）。留进来会把语料变成快照仓库，`title` / `summary` 还全为空。
2. **分区轴是 `event_time` 的北京日**——归档与在线接口同轴（实测 `filters` 回显与
   `date_field` 双证），一条事件只属于一天，`symbols` 才是跨标的的检索维度。
3. **`symbols` 从 `stocks` 归一化**：`code` 可为 null（此时退回按 `name` 取六位码）、
   非空时可能带 `.SH` / `.SZ` 后缀，也可能带 `.KS` 等**非 A 股后缀**（实测混进过韩股）。
   非 CN 后缀一律丢弃——留下会让「查 000660 查到韩股新闻」。原始 `stocks` 原样存 JSON 备审计。

写盘一律**先 staging、校验通过再原子换入**：查询层的 glob 只认 `*.parquet`，半截文件
会被当成正常分片读进去，那种「看起来有数据」的静默残缺最难发现。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

import duckdb

from app.etl.archive import Shard

#: 收进语料的事件类型。`macro` 目前归档里没出现过，先留着——出现即收，不必改代码
KEPT_EVENT_TYPES: tuple[str, ...] = ("announcement", "news", "person", "policy", "research", "macro")
#: 明确排除并记录（不是「忘了收」）
EXCLUDED_EVENT_TYPES: tuple[str, ...] = ("future_dynamic", "sector", "sector_constituent")

#: 方向归一。三类取值各有语义，**不是**同一件事：
#:   * 情绪（新闻/人物）：利多/看多/positive… → bullish
#:   * 政策立场（`event_type=policy`）：dovish（鸽派/宽松）偏多、hawkish（鹰派/紧缩）偏空；
#:     实测 220 + 165 条，原先不在表里被整批归成 NULL——那是**语义被丢掉**，不是「无方向」
#:   * 公告类别（高管人事/融资定增/业绩预告…）**不映射**：类别不等于方向（「融资定增」既可能
#:     是扩张也可能是摊薄），要映射得先有一张业务认可的表，属 M8 的 PIT 基本面事件
DIRECTION_MAP: dict[str, str] = {
    "利多": "bullish", "bullish": "bullish", "positive": "bullish", "看多": "bullish",
    "利空": "bearish", "bearish": "bearish", "negative": "bearish", "看空": "bearish",
    "中性": "neutral", "neutral": "neutral",
    "dovish": "bullish", "hawkish": "bearish",
}

DAY_PREFIX = "cn-events_"
SCHEMA = "quantsage.events_meta/v2"

#: 归一化 SQL 片段：code → 6 位码；非 CN 后缀丢弃；无名无码则空串（随后被过滤掉）
_RAW_CODE = (
    "COALESCE(NULLIF(json_extract_string(s, '$.code'), ''),"
    " NULLIF(json_extract_string(s, '$.name'), ''), '')"
)
_NORMALIZED_CODE = (
    f"CASE WHEN {_RAW_CODE} LIKE '%.%'"
    f" AND regexp_extract({_RAW_CODE}, '\\.([A-Z]+)$', 1) NOT IN ('SH', 'SZ') THEN ''"
    f" ELSE regexp_extract({_RAW_CODE}, '([0-9]{{6}})', 1) END"
)


@dataclass(frozen=True, slots=True)
class DayEntry:
    """一天的落盘结果——清单与溯源旁注共用这一份结构。"""

    date: str
    file: str
    rows: int
    symbols: int
    sha256: str
    materialized_at: str
    shards: tuple[dict, ...]

    def to_json(self) -> dict:
        return asdict(self)


def day_file_name(day: date) -> str:
    return f"{DAY_PREFIX}{day.isoformat()}.parquet"


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _direction_case() -> str:
    whens = " ".join(
        f"WHEN '{raw}' THEN '{norm}'" for raw, norm in DIRECTION_MAP.items()
    )
    return f"CASE trim(COALESCE(json_extract_string(payload_json, '$.direction'), '')) {whens} END"


def _select_sql(paths: list[Path]) -> str:
    """归档分片 → 语料行的 SELECT。列集按 `quant-event-v2` 定（SPEC §3 M2b）。"""
    files = ", ".join(_sql_str(str(path)) for path in paths)
    return f"""
    WITH raw AS (
        SELECT * FROM read_parquet([{files}])
        WHERE event_type IN ({", ".join(_sql_str(t) for t in KEPT_EVENT_TYPES)})
    )
    SELECT
        event_id,
        event_type,
        event_time,
        available_at,
        reported_available_at,
        CAST(json_extract_string(payload_json, '$.observed_at') AS TIMESTAMPTZ) AS observed_at,
        json_extract_string(payload_json, '$.title')   AS title,
        json_extract_string(payload_json, '$.summary') AS summary,
        json_extract_string(payload_json, '$.direction') AS direction,
        {_direction_case()} AS direction_norm,
        CAST(json_extract_string(payload_json, '$.confidence') AS DOUBLE) AS confidence,
        importance_score,
        CAST(json_extract_string(payload_json, '$.factor_value') AS DOUBLE) AS factor_value,
        json_extract_string(payload_json, '$.factor_scores') AS factor_scores,
        list_transform(CAST(json_extract(payload_json, '$.industries') AS JSON[]),
                       x -> json_extract_string(x, '$')) AS industries,
        json_extract_string(payload_json, '$.stocks') AS stocks,
        list_sort(list_distinct(list_filter(
            list_transform(CAST(json_extract(payload_json, '$.stocks') AS JSON[]),
                           s -> {_NORMALIZED_CODE}),
            x -> x <> ''))) AS symbols,
        'xiaoshi-archive' AS source,
        json_extract_string(payload_json, '$.source')     AS original_source,
        json_extract_string(payload_json, '$.source_url') AS source_url,
        content_hash,
        json_extract_string(payload_json, '$.quality_status')      AS quality_status,
        json_extract_string(payload_json, '$.source_time_quality') AS source_time_quality,
        json_extract_string(payload_json, '$.dedup_key')        AS dedup_key,
        CAST(json_extract_string(payload_json, '$.record_version') AS INTEGER) AS record_version,
        json_extract_string(payload_json, '$.revision_id')      AS revision_id,
        CAST(json_extract_string(payload_json, '$.revision_time') AS TIMESTAMPTZ) AS revision_time,
        CAST(json_extract_string(payload_json, '$.is_corrected') AS BOOLEAN) AS is_corrected,
        CAST(json_extract_string(payload_json, '$.correction_count') AS INTEGER) AS correction_count,
        json_extract_string(payload_json, '$.person') AS person
    FROM raw
    ORDER BY event_time, event_id
    """


def materialize_day(
    day: date,
    shards: Mapping[str, Shard],
    events_dir: Path,
    raw_dir: Path,
) -> DayEntry:
    """把一个归档日的分片合并成一片日分区 Parquet（原子换入），并写溯源旁注。

    分片里没有保留类型时**不写空文件**，抛 `ValueError` 由调用方按「该日无事件」处理
    ——空 parquet 会让「有分区但没数据」和「没拉过」看起来一样。
    """
    kept = [shards[t] for t in KEPT_EVENT_TYPES if t in shards]
    if not kept:
        raise ValueError(f"{day} 的分片里没有可收的事件类型：{sorted(shards)}")

    events_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    target = events_dir / day_file_name(day)
    staging = events_dir / f".{day_file_name(day)}.partial"

    con = duckdb.connect()
    try:
        staging.unlink(missing_ok=True)
        con.execute(
            f"COPY ({_select_sql([shard.path for shard in kept])})"
            f" TO {_sql_str(str(staging))} (FORMAT PARQUET)"
        )
        rows, null_avail, pit_violations, dup_ids, symbols_empty = con.execute(
            "SELECT count(*),"
            " count(*) FILTER (WHERE available_at IS NULL),"
            " count(*) FILTER (WHERE available_at < event_time),"
            " count(*) - count(DISTINCT event_id),"
            " count(*) FILTER (WHERE len(symbols) = 0)"
            f" FROM read_parquet({_sql_str(str(staging))})"
        ).fetchone()
        if rows == 0:
            raise ValueError(f"{day} 物化后没有数据")
        if null_avail or pit_violations or dup_ids:
            raise ValueError(
                f"{day} 落盘校验失败：available_at 空值 {null_avail}、"
                f"available_at < event_time {pit_violations}、event_id 重复 {dup_ids}"
            )
        distinct_symbols = con.execute(
            f"SELECT count(DISTINCT s) FROM (SELECT unnest(symbols) AS s FROM read_parquet({_sql_str(str(staging))}))"
        ).fetchone()[0]
        # 逐分片行数（含被排除的类型）：审计「这个日分区来自哪几片、各多少行」时要看
        shard_rows = {
            str(row[0]): int(row[1])
            for row in con.execute(
                "SELECT event_type, count(*) FROM read_parquet(["
                + ", ".join(_sql_str(str(shard.path)) for shard in kept)
                + "]) GROUP BY 1"
            ).fetchall()
        }
    except Exception:
        staging.unlink(missing_ok=True)
        raise
    finally:
        con.close()

    staging.replace(target)
    entry = DayEntry(
        date=day.isoformat(),
        file=target.name,
        rows=rows,
        symbols=distinct_symbols,
        sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
        materialized_at=datetime.now(timezone.utc).isoformat(),
        shards=tuple(
            {
                "event_type": shard.event_type,
                "object_key": shard.object_key,
                "sha256": shard.sha256,
                "size": shard.size,
                "rows": shard_rows.get(shard.event_type, 0),
            }
            for shard in kept
        ),
    )
    (raw_dir / f"{day.isoformat()}.json").write_text(
        json.dumps({**entry.to_json(), "symbols_without_code": symbols_empty}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return entry


def read_day_entries(events_dir: Path, raw_dir: Path) -> list[DayEntry]:
    """按磁盘实际内容重建清单条目：先看数据文件在不在，再看有没有溯源旁注。

    清单因此**永远与磁盘一致**——文件被手工删掉时清单不会继续宣称它有。
    """
    entries: list[DayEntry] = []
    for path in sorted(events_dir.glob(f"{DAY_PREFIX}*.parquet")):
        day = path.name[len(DAY_PREFIX) : -len(".parquet")]
        sidecar = raw_dir / f"{day}.json"
        if not sidecar.exists():
            entries.append(
                DayEntry(
                    date=day,
                    file=path.name,
                    rows=-1,
                    symbols=-1,
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    materialized_at="",
                    shards=(),
                )
            )
            continue
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        entries.append(
            DayEntry(
                date=payload["date"],
                file=payload["file"],
                rows=int(payload["rows"]),
                symbols=int(payload["symbols"]),
                sha256=payload["sha256"],
                materialized_at=payload["materialized_at"],
                shards=tuple(payload.get("shards") or ()),
            )
        )
    return entries


def write_manifest(meta_dir: Path, entries: Iterable[DayEntry], *, coverage_last: str | None) -> Path:
    """重建 `data/_meta/events.json`（v2）：逐日分片 + 指纹，体例对齐 `bars.json`。"""
    items = sorted(entries, key=lambda item: item.date)
    meta_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(
        "".join(f"{item.date}:{item.sha256};" for item in items).encode("utf-8")
    ).hexdigest()
    payload = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "channel": "archive-cli",
            "dataset": "event-timeline",
            "note": "小石归档按日整片；(日 × 事件类型) 分片，本清单逐日记录来源分片 sha256",
        },
        "partition": {"key": "event_time 的北京日"},
        "kept_event_types": list(KEPT_EVENT_TYPES),
        "excluded_event_types": list(EXCLUDED_EVENT_TYPES),
        "archive_coverage_last": coverage_last,
        "window": {
            "start": items[0].date if items else None,
            "end": items[-1].date if items else None,
        },
        "days": [item.to_json() for item in items],
        "fingerprint": {
            "algorithm": "sha256(date:file_sha256;…)",
            "digest": digest,
            "days": len(items),
            "rows": sum(item.rows for item in items if item.rows > 0),
        },
    }
    path = meta_dir / "events.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def clean_legacy_files(events_dir: Path) -> list[str]:
    """清掉 P1 的按标的文件 `{六位码}.parquet`——留着会被 glob 捞进去，同一事件算两遍。

    与 M2a 清 `{symbol}.{adjust}.parquet` 是同一类静默错误点：查询层的 glob 不区分来源。
    """
    removed: list[str] = []
    for path in sorted(events_dir.glob("*.parquet")):
        stem = path.name[: -len(".parquet")]
        if len(stem) == 6 and stem.isdigit():
            path.unlink()
            removed.append(path.name)
    return removed
