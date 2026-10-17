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
# 离线套件不调真模型：空命中用例默认不走「查询改写」这一轮（SPEC 附录 A.2）。
# 要验改写本身的用例注入 `rewrite_fn`，与这个开关无关。
os.environ.setdefault("RAG_QUERY_REWRITE", "false")
# 沙箱隔离：本机有 sandbox-exec / bwrap 就真跑隔离（安全用例也在这里验）；
# 没有就降级，避免 CI 因缺少系统包而整片红。生产默认仍是 require（见 core/config.py）。
os.environ.setdefault("STRATEGY_ISOLATION", "auto")

import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.auth import require_user  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402
from tests.fakes import FakeDatabase  # noqa: E402

CN_TZ = "Asia/Shanghai"

#: 「内存这一层不掺和」的策略配额上限。凡是用例验的是**别的**闸门（CPU / 墙钟 / 输出 /
#: stderr）时，内存阈值就要高到不会抢先触发——**同一份负载在 Linux 上的 RSS 比 macOS 高**，
#: 贴着实测值设（如 512）在 CI 上会改成内存先死、报出来的 `kind` 不是用例期望的那个
#: （2026-10-15 第二跑，7 条一起红）。看门狗本身没问题，`worker.py` 已按平台折算
#: `ru_maxrss` 的单位（macOS 字节 / Linux KB）。反过来，**要验内存闸门**的用例仍该给紧值。
NO_MEMORY_LIMIT = 8192

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
        # M5a 起：全市场等权基准按 change_pct 聚合（源给的日涨跌幅，含除权调整），
        # 夹具不写它时全为 None —— 基准那一段就没有样本，净值恒为初始资金
        ("change_pct", pa.float64()),
        # M5a 截面端点吃这两列（成交额 / 换手率）；换手率在源侧 2026-08 起逐步停更，
        # 夹具不写即为 None —— 端点必须如实返回 null 而不是 0
        ("amount", pa.float64()),
        ("turnover_pct", pa.float64()),
    ]
)

EVENTS_SCHEMA = pa.schema(
    [
        ("event_id", pa.string()),
        # M2b 起一条事件一行、标的是数组（同一事件挂多只股票只存一行）
        ("symbols", pa.list_(pa.string())),
        # M5a 名称字典的来源列：`[{code, name, reason}]` 的 JSON 文本
        ("stocks", pa.string()),
        ("event_type", pa.string()),
        ("title", pa.string()),
        ("event_time", pa.timestamp("us", tz=CN_TZ)),
        ("available_at", pa.timestamp("us", tz=CN_TZ)),
        ("direction_norm", pa.string()),
        # M10b 快讯流按它排序、按它筛高分；排序键是 (importance_score DESC, event_id)。
        # 平台给的 0–100 分，**不跨类型可比**（公告满分 100、新闻满分 92），故夹具里两种都放。
        ("importance_score", pa.float64()),
        ("factor_scores", pa.string()),
        # M5c 因子面板吃的就是这一列（带符号的因子值，实测覆盖 100%）：
        # 面板不解析 `factor_scores` 的 JSON，两个字段同源（|factor_value| = score / 100）
        ("factor_value", pa.float64()),
        # 来源三元组：生产数据本就有，T6 的 events 端点必须原样带出（PRD §5 硬性要求），
        # 夹具缺这几列就没法在离线测试里验这条映射
        ("source", pa.string()),
        ("original_source", pa.string()),
        ("content_hash", pa.string()),
        # M7a 起证据面板要用的三列（生产语料本就有，30 列里的三个）：
        # 摘要与行业是「语料行补充展示字段」（决策快照里没有），source_url 直接展示
        ("summary", pa.string()),
        ("industries", pa.list_(pa.string())),
        ("source_url", pa.string()),
        # M10d 事件详情要用的列（生产语料本就有，30 列里剩下的那批）：
        # 方向与置信、人物、三戳（报告可得 / 观察）、质量标注、去重键、修订四件
        ("direction", pa.string()),
        ("confidence", pa.float64()),
        ("person", pa.string()),
        ("reported_available_at", pa.timestamp("us", tz=CN_TZ)),
        ("observed_at", pa.timestamp("us", tz=CN_TZ)),
        ("quality_status", pa.string()),
        ("source_time_quality", pa.string()),
        ("dedup_key", pa.string()),
        ("record_version", pa.int32()),
        ("revision_id", pa.string()),
        ("revision_time", pa.timestamp("us", tz=CN_TZ)),
        ("is_corrected", pa.bool_()),
        ("correction_count", pa.int32()),
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
            "change_pct": row.get("change_pct"),
            "amount": row.get("amount"),
            "turnover_pct": row.get("turnover_pct"),
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
            # 传了 `symbols` 就按它写（多标的场景），否则单标的。
            # **空数组是「传了」**：生产数据里 70.5% 的有向事件一个标的都没挂（M5c 实测），
            # 用 `or` 兜底会把 `[]` 静默换成单标的，那种形状就再也测不出来了
            "symbols": list(row["symbols"]) if row.get("symbols") is not None else [symbol],
            "stocks": row.get("stocks"),
            "event_type": row.get("event_type", "news"),
            "title": str(row.get("title", "")),
            "event_time": row["event_time"],
            "available_at": row.get("available_at", row["event_time"]),
            "direction_norm": row.get("direction_norm"),
            "importance_score": row.get("importance_score"),
            "factor_scores": row.get("factor_scores", encode_factor_scores(row.get("score"))),
            "factor_value": row.get("factor_value"),
            "source": row.get("source"),
            "original_source": row.get("original_source"),
            "content_hash": row.get("content_hash"),
            "summary": row.get("summary"),
            # 传了 `industries` 就按它写；不传即 None（真实语料里 announcement 也是 0% 覆盖）
            "industries": list(row["industries"]) if row.get("industries") is not None else None,
            "source_url": row.get("source_url"),
            # M10d：详情要用的那批。默认值与真实语料的常态一致（版本 1、未修订、无人物）
            "direction": row.get("direction"),
            "confidence": row.get("confidence"),
            "person": row.get("person"),
            "reported_available_at": row.get("reported_available_at"),
            "observed_at": row.get("observed_at", row.get("available_at", row["event_time"])),
            "quality_status": row.get("quality_status", "stable"),
            "source_time_quality": row.get("source_time_quality", "exact"),
            "dedup_key": row.get("dedup_key", row["event_id"]),
            "record_version": row.get("record_version", 1),
            "revision_id": row.get("revision_id"),
            "revision_time": row.get("revision_time"),
            "is_corrected": row.get("is_corrected", False),
            "correction_count": row.get("correction_count", 0),
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

#: 回归用例里的「当前用户」。真鉴权行为在 test_auth_api.py / 集成用例里验。
#: **刻意不带 `role`**——普通用例一律是 ordinary user，否则「非 admin 被拒」那条永远测不出来（E2b）。
TEST_USER = {"id": 1, "email": "tester@example.com"}

#: 管理员姿态（E2b）。与 `TEST_USER` 只差一个 `role`，读法也应当只差这一处。
ADMIN_USER = {"id": 1, "email": "admin@example.com", "role": "admin"}


@pytest.fixture(autouse=True)
def _clean_limiters() -> Any:
    """每例一份干净的令牌桶（E4b）。

    桶是**进程级状态**（这正是它该有的形态：限流就是要跨请求累积），但测试之间共享它
    就成了串味——一个文件里打满 20 发，另一个文件的提交就开始收 429。用例要的是
    「这次调用本身对不对」，不是「整个套件一共发了几发」。
    """
    from app.core import reliability

    reliability.reset_limiters()
    yield
    reliability.reset_limiters()


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


@pytest.fixture
def signed_in_admin(jwt_secret: Any) -> Any:
    """管理员姿态：与 `signed_in` 同法（内存库 + 只换「当前用户是谁」），用户多一个 `role`。"""
    db = FakeDatabase()
    app.state.db = db
    app.dependency_overrides[require_user] = lambda: dict(ADMIN_USER)
    yield db
    app.dependency_overrides.clear()
    app.state.db = None


def drive_tasks(db: Any) -> int:
    """把排队中的任务跑光，返回跑了几件（P3-E1a）。

    **离线用例不起常驻 worker**（SPEC §10 E1）：模块级 `TestClient` 每请求一个事件循环，
    后台常驻协程在那种用法下活不过一次请求。`run_once` 就是 worker 常驻循环里的取件单元，
    手动驱动它测的是同一条路径——不需要 sleep，也没有轮询竞态。
    """
    import asyncio

    from app.tasks.worker import run_once

    count = 0
    while asyncio.run(run_once(db)):
        count += 1
    return count


def submit_and_run(client: TestClient, body: Mapping[str, Any]) -> dict[str, Any]:
    """提交 → 跑光队列 → 取回报告信封 `{run_id, report}`（P3-E1a 的公共取数路径）。

    E1a 起报告不再内联在提交响应里，这三步是每个「要报告」的用例都要走的——
    收在一处，改契约时只改这里。终态断言 `done`：调用方要的是报告，不是「排上了队」；
    要验失败路径的用例自己打 `/api/v1/tasks/{id}` 看 `error`。
    """
    response = client.post("/api/v1/backtest", json=dict(body))
    assert response.status_code == 202, response.text
    drive_tasks(app.state.db)
    task = client.get(f"/api/v1/tasks/{response.json()['task_id']}").json()
    assert task["status"] == "done", task
    detail = client.get(f"/api/v1/backtest/runs/{task['result']['run_id']}").json()
    return {"run_id": detail["id"], "report": detail["report"]}


