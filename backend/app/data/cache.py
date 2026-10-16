"""数据层缓存（E3c）：**L1 进程内 + L2 Redis，代际键，可降级**。

三条口径写死在这里（依据 SPEC §4.6，都是实测定的）：

1. **失效 = 换键，不是 DEL 扫键**。键里带**数据代际** `gen`（两份 `_meta` 清单的摘要），
   清单一变 `gen` 就变、旧键自然不可达（TTL 兜底回收）。**不需要「ETL 之后记得清缓存」
   这种靠人记的约定**——那是漏一处就静默返回旧数据的那类设计，在一个以「前视偏差为零」
   为护城河的项目里尤其不能有。
   ⚠ 与 §7.7「不采信清单」**不矛盾**：那里清单当**身份**（必须逐片重算），这里当**代际**
   （要的正是「什么时候换过」）。两件事，别混。

2. **L1 不看 `CACHE_ENABLED`**。进程内记忆化没有外部依赖，实测收益（25 次连读 26.9×）
   比 Redis（8–17×）还大；关掉它没有任何好处。**L1 命中就不碰 Redis**（先本地后远端），
   否则网格那类场景会从「0.078s」变成「25 次 Redis 往返」。

3. **缓存层的任何异常都不得让请求失败**。Redis 不可达 ⇒ 熔断开路（`reliability` 的
   `redis-cache`，**失败即开路**，冷却 30s）⇒ 记一条 warn ⇒ 直读。连接超时压到 0.2s，
   否则 Redis 挂掉时每个请求都要先干等一遍超时——那比没有缓存还慢。

**为什么用同步客户端**（`redis.Redis` 而非 `redis.asyncio`）：本模块挂在 `duckdb_client`
**内部**（§10 的纪律：一处实现，28 个调用点不会各做一半），而那边全是同步函数、调用方靠
`asyncio.to_thread` 卸载到线程。同步函数里用异步客户端只有 `asyncio.run()` 一条路，且调用方
已在事件循环里时直接抛 `RuntimeError`（见 §4.6 的订正）。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import threading
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.reliability import get_dep

log = logging.getLogger(__name__)

#: 单次 Redis 操作的超时。**必须短**——它是 Redis 挂掉时每个请求的白等时长
SOCKET_TIMEOUT = 0.2
#: L1 的条目上限。约 200 份「单标的全史」量级，够覆盖一次网格/批量；超了按插入序淘汰最旧
LOCAL_MAX = 256
#: 非 JSON 原生类型的标记（`date` / `datetime` 往返要还原成对象，不然缓存命中与直读的
#: **类型**就不一致了——「逐字段相等」是这一节的验收判据）
MARK = "$qs"

_MISS = object()
#: 进程内 L1。`threading.Lock` 护着：调用点分布在线程池里，字典本身不是线程安全的
_LOCAL: dict[tuple, Any] = {}
_LOCK = threading.Lock()
_CLIENT: Any = None
#: gen 的 mtime 门——每次调用都重读两份 JSON 要 0.5ms，而 L1 命中本该接近零成本
_GEN: tuple[tuple, str] | None = None


def generation(data_dir: Path | None = None) -> str:
    """数据代际：两份清单的摘要。**mtime 没变就不重算**（0.5ms → 约 0.05ms）。

    代际是「数据什么时候换过」的记录 ⇒ 读清单正是对的（§4.6 已写明它与 §7.7 的分工）。
    任一份清单缺失时**退化成按剩余那份算**——数据还没落盘的目录照样能跑（缓存命中率差些而已）。
    """
    global _GEN
    meta = _base(data_dir) / "_meta"
    # ⚠ **键里必须带目录**：不带的话两份清单都不存在的目录会算出同一个 stamp
    # （全是 0,0），不同数据目录会互相串缓存——测试里当场就撞了（多个 tmp 目录）
    stamp: list[tuple] = [("dir", str(meta), 0, 0)]
    for name in ("bars.json", "events.json"):
        path = meta / name
        try:
            stat = path.stat()
            stamp.append((name, stat.st_mtime_ns, stat.st_size))
        except OSError:
            stamp.append((name, 0, 0))
    key = tuple(stamp)
    if _GEN is not None and _GEN[0] == key:
        return _GEN[1]
    digest = hashlib.sha256()
    for name in ("bars.json", "events.json"):
        try:
            digest.update((meta / name).read_bytes())
        except OSError:
            # 清单不在（还没落过盘）时**用目录顶替**：都拿同一个占位符的话，两个都没有
            # 清单的目录会算出同一个 gen、互相串缓存（测试里当场撞了这个——多个 tmp 目录）。
            # 清单在的时候就按内容算，那才是 SPEC §4.6 要的「同一份数据同一个代际」。
            digest.update(str(meta).encode("utf-8"))
    value = digest.hexdigest()[:16]
    _GEN = (key, value)
    return value


def through(
    name: str,
    parts: tuple,
    producer: Callable[[], Any],
    *,
    data_dir: Path | None = None,
) -> Any:
    """取缓存或算一次。`producer` 是真读（DuckDB 查询）——**只在两层都未命中时才调**。"""
    # L1 的键**带上目录**：两层各自都要能区分数据目录（L1 是进程内的，测试里尤其明显）
    key = (str(_base(data_dir)), generation(data_dir), name, *(_part(p) for p in parts))
    blob = _local_get(key)
    if blob is not _MISS:
        # ⚠ **每次解码一副新对象**，不是直接把缓存里的那份递出去。理由：`bars()` 这类
        # 返回值是嵌套的 list[dict]，按引用递出去之后调用方一次原地改写就会**静默污染缓存**
        # （改一次，之后所有请求都读错——本项目最忌的就是「静默返回错数据」）。实测代价：
        # 424 行载荷解码约 1ms，相对直读 75ms 仍是一个数量级；而「谁的返回值归谁」这条
        # 不需要 28 个调用点各自守规矩。
        return _decode(blob)
    value = _redis_get(key)
    if value is _MISS:
        value = producer()
        _redis_set(key, value)
    _local_put(key, _encode(value))
    return value


def clear_local() -> None:
    """清 L1（测试用；也供将来的「手动刷新」入口——它不影响正确性，键里带 gen）。"""
    with _LOCK:
        _LOCAL.clear()


def drop_client() -> None:
    """丢掉 L2 连接（测试用：换 URL / 造降级场景）。"""
    global _CLIENT
    with _LOCK:
        client, _CLIENT = _CLIENT, None
    if client is not None:
        with contextlib.suppress(Exception):  # 关连接失败没有后果
            client.close()


def stats() -> dict[str, Any]:
    """只读快照（排查用）。`local_entries` 是 L1 的规模，`breaker` 是 L2 熔断状态。"""
    dep = get_dep("redis-cache")
    return {
        "local_entries": len(_LOCAL),
        "breaker": dep.state,
        "generation": generation(),
    }


# ── L1 ──────────────────────────────────────────────────────


def _local_get(key: tuple) -> Any:
    with _LOCK:
        return _LOCAL.get(key, _MISS)


def _local_put(key: tuple, value: Any) -> None:
    with _LOCK:
        if len(_LOCAL) >= LOCAL_MAX:
            # 插入序淘汰最旧的一条（dict 保序）。不做 LRU：命中统计的复杂度不值得，
            # 而 L1 的寿命就是一次运行
            for oldest in list(_LOCAL)[: max(1, LOCAL_MAX // 8)]:
                _LOCAL.pop(oldest, None)
        _LOCAL[key] = value


# ── L2 ──────────────────────────────────────────────────────


def _redis_key(key: tuple, prefix: str) -> str:
    """L2 的键**不带目录**——远端的语义是「同一代际的数据共享一份缓存」，
    而代际本身在清单缺失时已经带了目录（见 `generation`）。"""
    _dir, gen, name, *parts = key
    return ":".join([prefix, gen, name, *(str(p) for p in parts)])


def _client() -> Any:
    """惰性建连接。**建失败也记账**（熔断），否则每次都要重试一遍连接。"""
    global _CLIENT
    if not get_settings().cache_enabled:
        return None
    with _LOCK:
        if _CLIENT is not None:
            return _CLIENT
    dep = get_dep("redis-cache")
    if dep.allow(dep.clock()) is not None:
        return None  # 冷却中，直接走直读
    try:
        import redis

        client = redis.Redis.from_url(
            get_settings().redis_url,
            socket_timeout=SOCKET_TIMEOUT,
            socket_connect_timeout=SOCKET_TIMEOUT,
        )
        client.ping()
    except Exception as exc:  # noqa: BLE001 —— 缓存连不上不是错误，只是没缓存
        dep.record_failure(dep.clock())
        log.warning("数据缓存不可用（%s: %s），本次起直读", type(exc).__name__, exc)
        return None
    dep.record_success()
    with _LOCK:
        _CLIENT = client
    return client


def _redis_get(key: tuple) -> Any:
    client = _client()
    if client is None:
        return _MISS
    dep = get_dep("redis-cache")
    try:
        raw = client.get(_redis_key(key, get_settings().cache_key_prefix))
    except Exception as exc:  # noqa: BLE001
        _l2_failed(exc)
        return _MISS
    dep.record_success()
    if raw is None:
        return _MISS
    try:
        return _decode(json.loads(raw))
    except (json.JSONDecodeError, TypeError):  # 坏值当没有，别让一条脏数据打穿整个请求
        return _MISS


def _redis_set(key: tuple, value: Any) -> None:
    client = _client()
    if client is None:
        return
    dep = get_dep("redis-cache")
    try:
        client.set(
            _redis_key(key, get_settings().cache_key_prefix),
            json.dumps(_encode(value), ensure_ascii=False).encode("utf-8"),
            ex=get_settings().cache_ttl_seconds,
        )
    except Exception as exc:  # noqa: BLE001 —— 写失败只影响命中率，不影响正确性
        _l2_failed(exc)
        return
    dep.record_success()


def _l2_failed(exc: Exception) -> None:
    """一次 L2 故障：记账（可能开路）并丢掉连接——下次由熔断决定要不要重连。"""
    dep = get_dep("redis-cache")
    dep.record_failure(dep.clock())
    if dep.state == "open":
        log.warning("数据缓存熔断（%s: %s），冷却期内直读", type(exc).__name__, exc)
    drop_client()


# ── 序列化 ──────────────────────────────────────────────────


def _encode(value: Any) -> Any:
    """`date` / `datetime` 打标记——**不打标记就还原不回来**，缓存命中与直读的类型就不一致了。"""
    if isinstance(value, (date, datetime)):
        return {MARK: value.isoformat()}
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, dict):
        if len(value) == 1 and MARK in value:
            return datetime.fromisoformat(value[MARK]) if "T" in value[MARK] else date.fromisoformat(value[MARK])
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


def _part(value: Any) -> str:
    """键里的一段。`None` 与空串要分开（`bars` 的 `start=None` 是「全史」，`""` 不是）。"""
    return "-" if value is None else str(value)


def _base(data_dir: Path | None) -> Path:
    """与 `duckdb_client.resolve_data_dir` **同一条解析**（settings 优先）——
    两处各写一份会让「缓存按 A 目录算代际、数据从 B 目录读」。"""
    return Path(data_dir) if data_dir is not None else get_settings().data_dir
