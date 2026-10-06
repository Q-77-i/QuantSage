"""pytest 全局配置与合成数据夹具。

放在任何 langgraph.checkpoint 导入之前锁死反序列化白名单（与 app/__init__.py 同源）。

`write_bars_parquet` / `write_events_parquet` 供回测用例使用：直接写真实 Parquet，
形状与 `data/` 落盘一致（**不用 mock**）。事件侧刻意保留 `factor_scores` 的
**双重编码**——落盘值是「内容为 JSON 文本的 str」，与生产数据同形。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402
import pytest  # noqa: E402

from app.core.auth import require_user  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402
from tests.fakes import FakeDatabase  # noqa: E402

CN_TZ = "Asia/Shanghai"

BARS_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("adjustment", pa.string()),
        ("trade_date", pa.date32()),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.float64()),
        ("is_suspended", pa.bool_()),
    ]
)

EVENTS_SCHEMA = pa.schema(
    [
        ("event_id", pa.string()),
        # M2b 起一条事件一行、标的是数组（同一事件挂多只股票只存一行）
        ("symbols", pa.list_(pa.string())),
        ("event_type", pa.string()),
        ("title", pa.string()),
        ("event_time", pa.timestamp("us", tz=CN_TZ)),
        ("available_at", pa.timestamp("us", tz=CN_TZ)),
        ("direction_norm", pa.string()),
        ("factor_scores", pa.string()),
        # 来源三元组：生产数据本就有，T6 的 events 端点必须原样带出（PRD §5 硬性要求），
        # 夹具缺这几列就没法在离线测试里验这条映射
        ("source", pa.string()),
        ("original_source", pa.string()),
        ("content_hash", pa.string()),
    ]
)


def encode_factor_scores(score: object) -> str | None:
    """模仿落盘的双重编码：内层 dict → JSON 文本 → 再 JSON 编码一次。"""
    if score is None:
        return None
    return json.dumps(json.dumps({"score": score, "version": "factor-v2"}))


def write_bars_parquet(
    directory: Path,
    symbol: str,
    rows: Iterable[Mapping[str, Any]],
    adjust: str = "qfq",
) -> Path:
    """写单标的单复权日线，列名与真实落盘一致。"""
    directory.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "symbol": symbol,
            "adjustment": adjust,
            "trade_date": row["trade_date"],
            "open": float(row["open"]),
            "high": float(row.get("high", row["open"])),
            "low": float(row.get("low", row["open"])),
            "close": float(row.get("close", row["open"])),
            "volume": float(row.get("volume", 1e5)),
            "is_suspended": bool(row.get("is_suspended", False)),
        }
        for row in rows
    ]
    target = directory / f"{symbol}.{adjust}.parquet"
    table = pa.Table.from_pylist(records, schema=BARS_SCHEMA)
    pq.write_table(table, target)
    return target


def write_events_parquet(
    directory: Path,
    symbol: str,
    rows: Iterable[Mapping[str, Any]],
) -> Path:
    """写该标的的事件语料；`score` 传入即按双重编码落盘。

    文件名刻意**不用** P1 的 `{六位码}.parquet` 形状——那正是 `clean_legacy_files`
    要清理的遗留命名，用它会让人分不清「夹具」和「待清理的旧文件」。
    """
    directory.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "event_id": row["event_id"],
            # 传了 `symbols` 就按它写（多标的场景），否则单标的
            "symbols": list(row.get("symbols") or [symbol]),
            "event_type": row.get("event_type", "news"),
            "title": str(row.get("title", "")),
            "event_time": row["event_time"],
            "available_at": row.get("available_at", row["event_time"]),
            "direction_norm": row.get("direction_norm"),
            "factor_scores": row.get("factor_scores", encode_factor_scores(row.get("score"))),
            "source": row.get("source"),
            "original_source": row.get("original_source"),
            "content_hash": row.get("content_hash"),
        }
        for row in rows
    ]
    target = directory / f"cn-events_{symbol}.parquet"
    table = pa.Table.from_pylist(records, schema=EVENTS_SCHEMA)
    pq.write_table(table, target)
    return target


def make_backtest_dir(
    root: Path,
    bars: Iterable[Mapping[str, Any]],
    events: Iterable[Mapping[str, Any]] = (),
    symbol: str = "600519",
) -> Path:
    """一次性建好 bars/ 与 events/ 两个子目录（查询层要求两者都有 Parquet）。"""
    write_bars_parquet(root / "bars", symbol, bars)
    write_events_parquet(root / "events", symbol, events)
    return root


def trading_days(start: date, count: int) -> list[date]:
    """连续日历日序列（测试不需要真实交易日历，只需单调递增）。"""
    from datetime import timedelta

    return [start + timedelta(days=i) for i in range(count)]


def ts(text: str) -> datetime:
    """'2026-08-03 10:00:00' → tz-aware Asia/Shanghai。"""
    from zoneinfo import ZoneInfo

    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(CN_TZ))


# ── M1：鉴权夹具 ────────────────────────────────────────────

#: 回归用例里的「当前用户」。真鉴权行为在 test_auth_api.py / 集成用例里验
TEST_USER = {"id": 1, "email": "tester@example.com"}


@pytest.fixture
def jwt_secret(monkeypatch: pytest.MonkeyPatch) -> Any:
    """给鉴权用例一个确定的密钥。环境变量优先于 .env，结果与本机配置无关。"""
    # 32 字节以上：PyJWT 对短 HMAC 密钥会告警（RFC 7518 §3.2）
    monkeypatch.setenv("JWT_SECRET", "test-secret-not-a-real-key-0123456789abcdef")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def signed_in(jwt_secret: Any) -> Any:
    """已登录姿态：注入内存业务库，并把 `require_user` 换成固定用户。

    只替换「当前用户是谁」这一层，端点仍走真实路由——供 P1 既有端点用例回归
    （它们要验的是 503/404/422，不是鉴权本身）。返回 `FakeDatabase` 便于用例直接
    摆会话归属。
    """
    db = FakeDatabase()
    app.state.db = db
    app.dependency_overrides[require_user] = lambda: dict(TEST_USER)
    yield db
    app.dependency_overrides.clear()
    app.state.db = None
