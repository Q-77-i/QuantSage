"""数据快照与指纹（M7a）：报告「看的是哪一段数据」的自证，以及 `report_hash`。

三条口径：

1. **digest 从实际分片重算**（逐文件 sha256 → 汇总），**不采信 `_meta/*.json` 清单**——
   清单是记录不是测量（体检 B1 同规）。落盘文件名是本地唯一可用的分片身份；回执里的
   `object_key` 属于「下载回执」那一侧，M2a 已记它连顶层 manifest_version 都对不上号。
2. **events 只留 digest 与计数**，不逐片列 93 行；bars 逐片列（21 片，是 M2a 的数据版本
   指纹约定，跨版本比数字前先比指纹）。
3. `canonical_json` 是**唯一**的规范化入口：键排序、紧凑分隔符、日期与 Decimal 转字符串
   ——`snapshot_hash` 与 `report_hash` 都走它，两次生成才能得同一个值。

成本实测（2026-10-10，真实数据）：bars 860MB 逐片 sha256 **2.3s**、events 80MB **0.28s**。
报告生成是用户点一次的动作（之后冻结复用），这个量级可接受。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.data import duckdb_client as dc
from app.paper.store import config_to_payload, decision_to_payload
from app.paper.types import Decision, PaperConfig

#: 读文件的块大小：860MB 的分片集用 1MB 块，实测与其他块大小无异，取一个不折腾内存的值
_CHUNK = 1 << 20


def _default(value: Any) -> str | float:
    """JSON 兜底：日期 / UUID / 十进制数的规范形态。

    **UUID 是必须的**：`psycopg` 从真库读回的 `account_id` 是 `uuid.UUID` 对象（内存替身里
    是字符串），指纹要在两种来源下都算得出来——这条路是集成用例逮出来的。
    Decimal 走 float（它本来就来自 NUMERIC 金额）。
    """
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"无法规范化 {type(value).__name__}：{value!r}")


def canonical_json(obj: Any) -> str:
    """规范化 JSON：键排序 + 紧凑分隔符 + 日期可序列化。hash 只认这一种写法。"""
    return json.dumps(
        obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=_default
    )


def plain_json(obj: Any) -> Any:
    """规范化 + 解析回来 = **纯 JSON 类型**的同一棵树（UUID → str、Decimal → float、date → ISO）。

    冻结产物落库前必过这一道：`psycopg` 的 Jsonb 适配器不认 UUID，而真库读回来的
    `account_id` / 决策 `id` 就是 UUID 对象（内存替身里是字符串——集成用例逮出来的）。
    过了这道，落库的、分享出去的、算 `report_hash` 的永远是同一份东西。
    """
    return json.loads(canonical_json(obj))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def digests(paths: Iterable[Path]) -> list[dict[str, str]]:
    """逐文件 `{name, sha256}`，按文件名排序（顺序稳定，digest 才可复现）。"""
    return [{"name": p.name, "sha256": _sha256_file(p)} for p in sorted(paths)]


def _digest_of(shard_list: Sequence[Mapping[str, str]]) -> str:
    return hashlib.sha256(canonical_json(list(shard_list)).encode("utf-8")).hexdigest()


def data_snapshot(data_dir: Path | None = None) -> dict[str, Any]:
    """行情与语料的分片指纹 + 覆盖区间。形状见 SPEC §8「持久化与快照」。"""
    base = dc.resolve_data_dir(data_dir)
    bars = digests((base / "bars").glob("*.parquet"))
    events = digests((base / "events").glob("*.parquet"))
    coverage = dc.event_coverage(base)
    latest = dc.latest_dates(base)

    return {
        "bars": {"shards": bars, "digest": _digest_of(bars)},
        "events": {
            "digest": _digest_of(events),
            "days": len(events),
            "last_day": coverage["end"],
            "rows": coverage["rows"],
        },
        "corpus": {"start": coverage["start"], "end": coverage["end"]},
        "market_end": latest["latest_trade_date"],
    }


def snapshot_hash(snapshot: Mapping[str, Any]) -> str:
    """快照身份：同一批输入必得同一个值（报告的幂等复用与重放一致都以它为键）。"""
    return hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()


def decisions_digest(decisions: Sequence[Decision]) -> str:
    """决策日志指纹：按 id 排序后取信封（顺序无关——库里读回的顺序不该改变身份）。"""
    payloads = sorted((decision_to_payload(d) for d in decisions), key=lambda p: p["id"])
    return hashlib.sha256(canonical_json(payloads).encode("utf-8")).hexdigest()


def config_digest(config: PaperConfig) -> str:
    """创建时配置的指纹（快照的一部分）：池子 / 策略 / 区间 / 参数改了就换一份报告。"""
    return hashlib.sha256(canonical_json(config_to_payload(config)).encode("utf-8")).hexdigest()


def report_hash(report: Mapping[str, Any]) -> str:
    """冻结产物正文的 sha256（报告行落库时算一次，之后不再重算）。"""
    return hashlib.sha256(canonical_json(report).encode("utf-8")).hexdigest()
