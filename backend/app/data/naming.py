"""名称字典（M5a）：`symbol → name`，来源是事件语料的 `stocks` 列。

**纯函数层**——解析、归一化、过滤、选名、as-of 查询都不碰 IO（读写 Parquet 的壳子
在消费方那一侧）。这样「一名多写怎么解」这件事可以被逐条断言，而不是靠跑一遍全量
语料看结果。

## 三重过滤（防外部代码注入）

归档的 `stocks` 里混着港股、美股、ETF，甚至**外部代码与 A 股代码撞号**：

    [{"code":"005930.KS","name":"SK海力士"}]      ← 韩股，后缀不是 .SH/.SZ
    [{"code":"000660","name":"SK海力士"}]          ← 裸六位，与 A 股 *ST南华 撞号
    [{"code":"000830","name":"Samsung"}]           ← 三星的韩股码，与鲁西化工撞号

故收一条 `(code, name)` 要同时满足：

  ① 该 code 出现在**同一事件的 `symbols` 数组**里（`symbols` 由 ETL 归一化，已经把
     非 `.SH`/`.SZ` 后缀的条目清空）——挡掉 `005930.KS` 一类；
  ② 该 code 落在**行情宇宙**内——挡掉 ETF 与已退市；
  ③ 归一化后同名合并、**取众数**——挡掉 `Samsung` 这类一次性的错配名。

撞号的极端情况（`000830` 既出现过「鲁西化工」27 次也出现过「Samsung」1 次）由 ③ 兜住：
众数是对的，**但不宣称 100% 准**——实测 11 例一名多写里解掉 10 例，残留 1 例见
`tests/test_data_naming.py` 的对照表。

## 名称带时点

每行 `(symbol, name)` 各带 `first_seen` / `last_seen`（取自 `available_at`，不是
`event_time`——「平台什么时候开始这么叫它」才是可得的时刻）。改名因此是可查询的：
`name_as_of(rows, symbol, t)` 给出 t 时刻的名字，**早于一切观测时返回 None 而不是回退到
当前名**——回退等于给历史安一个当时并不存在的名字，那是 PIT 立场的反面。
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from app.data import duckdb_client as dc

#: 从任意字符串里抠出六位码（与 `etl.store._NORMALIZED_CODE` 同规则）
_CODE_RE = re.compile(r"([0-9]{6})")
#: 市场后缀。ETL 只认 `.SH` / `.SZ` 两个——**照抄**，不擅自扩大白名单
_SUFFIX_RE = re.compile(r"\.([A-Z]+)$")

#: A 股简称的临时前缀：`N` / `C`（新股上市首日 / 次日）、`XD` / `XR` / `DR`（除权除息）。
#: **只在后面紧跟汉字时才剥**——否则 `NIO` 会变成 `IO`、`CDNS` 会变成 `DNS`。
_PREFIX_RE = re.compile(r"^(?:XD|XR|DR|[NC])(?=[一-鿿])")


@dataclass(frozen=True, slots=True)
class NameRow:
    """某个标的的**一个**名字，以及它被观测到的次数与时间边界。"""

    symbol: str
    name: str
    count: int
    first_seen: datetime
    last_seen: datetime


def normalize_code(raw: object) -> str:
    """把 `stocks` 里的 `code` 归一成六位码；不是 A 股代码返回空串。

    与 `app/etl/store.py` 的 `_NORMALIZED_CODE` **逐分支同规则**：带点且后缀不是
    `SH`/`SZ` 的一律作废（含 `.SS`、小写后缀这两种边界），其余抠第一个六位数字串。
    """
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    if "." in text:
        found = _SUFFIX_RE.search(text)
        if (found.group(1) if found else "") not in ("SH", "SZ"):
            return ""
    code = _CODE_RE.search(text)
    return code.group(1) if code else ""


def normalize_name(raw: object) -> str:
    """全角转半角、去掉全部空白、剥掉临时前缀。返回展示用的规范名。"""
    if raw is None:
        return ""
    text = unicodedata.normalize("NFKC", str(raw))
    text = "".join(text.split())
    return _PREFIX_RE.sub("", text, count=1)


def parse_stocks(raw: object) -> list[tuple[str, str]]:
    """解析一条事件的 `stocks` 列，返回 `[(六位码, 规范名)]`。

    落盘是**单层 JSON 文本**；已解码的 list 也一并接受（防御，非主路径）。
    任何异常形状一律返回空列表——字典是展示用的增强，不该让一条脏数据打断整批构建。
    """
    obj: object = raw
    if isinstance(obj, str):
        text = obj.strip()
        if not text:
            return []
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            return []
    if not isinstance(obj, (list, tuple)):
        return []

    pairs: list[tuple[str, str]] = []
    for item in obj:
        if not isinstance(item, Mapping):
            continue
        code = normalize_code(item.get("code"))
        name = normalize_name(item.get("name"))
        if code and name:
            pairs.append((code, name))
    return pairs


def build_rows(
    records: Iterable[Mapping[str, object]],
    *,
    universe: Collection[str],
) -> list[NameRow]:
    """语料行 → 字典行（已合并同名、已过滤、已排序）。

    每条记录需要三个键：`stocks`（JSON 文本）、`symbols`（该事件的标数组）、
    `available_at`（时间边界取它）。缺 `available_at` 的记录跳过——没有时点的名字
    进不了「名称带时点」的模型。

    输出按 `(symbol, first_seen, name)` 排序：**确定性**是硬要求，否则字典文件
    每次构建逐字节不同，幂等就没法验。
    """
    allowed = set(universe)
    buckets: dict[tuple[str, str], list] = {}

    for record in records:
        when = record.get("available_at")
        if not isinstance(when, datetime):
            continue
        symbols = {str(item) for item in (record.get("symbols") or ())}
        for code, name in parse_stocks(record.get("stocks")):
            if code not in symbols or code not in allowed:
                continue
            bucket = buckets.get((code, name))
            if bucket is None:
                buckets[(code, name)] = [1, when, when]
            else:
                bucket[0] += 1
                bucket[1] = min(bucket[1], when)
                bucket[2] = max(bucket[2], when)

    return [
        NameRow(symbol=symbol, name=name, count=count, first_seen=first, last_seen=last)
        for (symbol, name), (count, first, last) in sorted(
            buckets.items(), key=lambda item: (item[0][0], item[1][1], item[0][1])
        )
    ]


def primary_names(rows: Iterable[NameRow]) -> dict[str, str]:
    """每个标的选一个对外展示的名：**出现次数最多**；并列取最近还见过的那个。"""
    best: dict[str, NameRow] = {}
    for row in rows:
        current = best.get(row.symbol)
        if current is None or (row.count, row.last_seen) > (current.count, current.last_seen):
            best[row.symbol] = row
    return {symbol: row.name for symbol, row in best.items()}


def name_as_of(rows: Iterable[NameRow], symbol: str, as_of: datetime) -> str | None:
    """`symbol` 在 `as_of` 时刻的名字；**早于一切观测返回 None**。

    候选只取 `first_seen <= as_of` 的行，再从中挑 `last_seen` 最晚的（并列取次数多的）。
    「早于观测」不回退到当前名——那等于给历史安一个当时并不存在的名字。
    """
    candidates = [row for row in rows if row.symbol == symbol and row.first_seen <= as_of]
    if not candidates:
        return None
    return max(candidates, key=lambda row: (row.last_seen, row.count)).name


# ── 落盘与读取（薄壳，纯函数在上一段）────────────────────────

#: 字典文件相对 `data/` 的路径。与 `bars/`、`events/` 平级：它是一份**派生物**，
#: 由 ETL 刷新，不参与 `duckdb_client` 的视图（那三个视图是「源数据」）。
DICT_SUBDIR = "naming"
DICT_FILE = "symbol_names.parquet"

_DICT_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("name", pa.string()),
        ("count", pa.int64()),
        ("first_seen", pa.timestamp("us", tz="Asia/Shanghai")),
        ("last_seen", pa.timestamp("us", tz="Asia/Shanghai")),
    ]
)


def dictionary_path(data_dir: Path | None = None) -> Path:
    return dc.resolve_data_dir(data_dir) / DICT_SUBDIR / DICT_FILE


def build_from_events(data_dir: Path | None = None) -> list[NameRow]:
    """读事件语料 + 行情宇宙，构建字典行。实测全量约 435ms。"""
    con = dc.connect(data_dir)
    try:
        universe = {
            row["symbol"]
            for row in con.execute(
                f"SELECT DISTINCT symbol FROM {dc.BARS_VIEW}"
            ).to_arrow_table().to_pylist()
        }
        records = _fetch_records(con)
    finally:
        con.close()
    return build_rows(records, universe=universe)


def _fetch_records(con: dc.duckdb.DuckDBPyConnection) -> list[dict[str, object]]:
    """只取构建需要的三列——30 万行 × 28 列全拉回来是没必要的开销。"""
    return (
        con.execute(f"SELECT stocks, symbols, available_at FROM {dc.EVENTS_VIEW}")
        .to_arrow_table()
        .to_pylist()
    )


def write_dictionary(rows: Iterable[NameRow], data_dir: Path | None = None) -> Path:
    """写字典 Parquet。**同一份输入逐字节一致**——幂等是 ETL 的既有口径。"""
    target = dictionary_path(data_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(
        [
            {
                "symbol": row.symbol,
                "name": row.name,
                "count": row.count,
                "first_seen": row.first_seen,
                "last_seen": row.last_seen,
            }
            for row in rows
        ],
        schema=_DICT_SCHEMA,
    )
    pq.write_table(table, target)
    return target


def load_dictionary(data_dir: Path | None = None) -> list[NameRow]:
    """读字典。文件不存在返回空列表——**字典是展示与 ST 判定的增强，不是关键路径**，
    缺它时调用方按「没有名字」处理，而不是让回测跑不起来。"""
    path = dictionary_path(data_dir)
    if not path.exists():
        return []
    table = pq.read_table(path)
    return [
        NameRow(
            symbol=row["symbol"],
            name=row["name"],
            count=int(row["count"]),
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
        )
        for row in table.to_pylist()
    ]


def st_symbols(rows: Iterable[NameRow]) -> set[str]:
    """当前名字是风险警示的标的集合（供 A 股规则的 ST 幅度档用）。

    判据复用 `a_share_rules.is_st_name` 的那一条正则——**不在这里另写一份**。
    """
    from app.backtest.a_share_rules import is_st_name  # 局部 import：data 层不依赖 backtest 层

    return {symbol for symbol, name in primary_names(rows).items() if is_st_name(name)}


__all__ = [
    "DICT_FILE",
    "DICT_SUBDIR",
    "NameRow",
    "build_from_events",
    "build_rows",
    "dictionary_path",
    "load_dictionary",
    "name_as_of",
    "normalize_code",
    "normalize_name",
    "parse_stocks",
    "primary_names",
    "st_symbols",
    "write_dictionary",
]
