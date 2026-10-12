"""M10 大盘总览与板块块两条端点：形状、口径、PIT 边界、降级。

三条口径各自有用例钉住：

  * **涨跌停走 raw 前收自算**（不复权、不读 `change_pct`）——夹具给了 qfq 与 raw 两套，
    故意让两者的收盘不同，走错序列读数就会变；
  * **板块归属是 PIT 累积**——夹具里放一条 `available_at` 在收盘**之后**的事件，
    它对当日**不可见**；这条断言是 SPEC §11「PIT 归属可验」的落地判据；
  * **覆盖率如实返回**——有标的没有任何标签，`covered < total`，界面照登。

离线用例继续用**合成 Parquet**（同 M5a 口径），不读真实数据。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.data import duckdb_client
from app.main import app
from tests.conftest import ts, write_bars_parquet, write_events_parquet

client = TestClient(app)

DAY1 = date(2026, 8, 3)
DAY2 = date(2026, 8, 4)

#: raw 收盘：600519 封涨停（+10%）、000001 封跌停（−10%）、300750 涨 5%（20% 档够不着）、
#: 600000 与 601398 平收。raw 才是涨跌停判定的正确序列。
RAW_DAY2 = {"600519": 110.0, "600000": 100.0, "000001": 90.0, "300750": 105.0, "601398": 100.0}

#: qfq 的日涨跌幅（源给的列）。**刻意与 raw 的收盘关系一致但不被实现读取**：
#: 涨跌停若错读这一列，数据形状仍「看起来对」——这正是要防的那类静默错。
QFQ_DAY2_PCT = {"600519": 10.0, "600000": 0.0, "000001": -10.0, "300750": 5.0, "601398": 0.0}

AMOUNT_DAY2 = {"600519": 3.0e9, "600000": 1.0e9, "000001": 2.0e9, "300750": 4.0e9, "601398": 5.0e8}

#: 事件：E1 在 DAY1 盘中说「半导体 = 这 3 只」；E2 **DAY2 收盘后**才说「半导体 还有 300750」
#: ——它对 DAY2 的板块块不可见；E3 是 DAY2 盘前的另一档。
EVENTS = [
    {
        "event_id": "e1",
        "event_time": ts("2026-08-03 10:00:00"),
        "available_at": ts("2026-08-03 10:00:00"),
        "industries": ["半导体"],
        "symbols": ["600519", "600000", "000001"],
    },
    {
        "event_id": "e2",
        "event_time": ts("2026-08-04 16:00:00"),
        "available_at": ts("2026-08-04 16:00:00"),
        "industries": ["半导体"],
        "symbols": ["300750"],
    },
    {
        "event_id": "e3",
        "event_time": ts("2026-08-04 09:00:00"),
        "available_at": ts("2026-08-04 09:00:00"),
        "industries": ["白酒"],
        "symbols": ["600519", "600000", "300750"],
    },
]


@pytest.fixture
def market_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """五个标的 × 两个交易日，qfq 与 raw 两套序列 + 一份事件语料。"""
    for symbol, day2_close in RAW_DAY2.items():
        write_bars_parquet(
            tmp_path / "bars",
            symbol,
            [
                {"trade_date": DAY1, "open": 100.0, "close": 100.0, "change_pct": 0.0, "amount": 1.0e9},
                {
                    "trade_date": DAY2,
                    "open": 100.0,
                    "close": day2_close,
                    "change_pct": QFQ_DAY2_PCT[symbol],
                    "amount": AMOUNT_DAY2[symbol],
                },
            ],
            adjust="qfq",
        )
        # raw 的 `change_pct` 在真实数据里自 2026-09-21 起整列为空，夹具照抄这个形状
        write_bars_parquet(
            tmp_path / "bars",
            symbol,
            [
                {"trade_date": DAY1, "open": 100.0, "close": 100.0},
                {"trade_date": DAY2, "open": 100.0, "close": day2_close},
            ],
            adjust="raw",
        )
    write_events_parquet(tmp_path / "events", "cn", EVENTS)
    monkeypatch.setattr(duckdb_client, "resolve_data_dir", lambda _=None: tmp_path)
    return tmp_path


def test_overview_breadth_and_amount(market_dir: Path) -> None:
    body = client.get("/api/v1/market/overview", params={"date": DAY2.isoformat()}).json()
    assert body["trade_date"] == DAY2.isoformat()
    assert body["breadth"] == {"total": 5, "up": 2, "down": 1, "flat": 2, "without_pct": 0}
    assert body["amount"] == pytest.approx(sum(AMOUNT_DAY2.values()))


def test_overview_limits_come_from_raw_prev_close(market_dir: Path) -> None:
    """涨停 1（600519）、跌停 1（000001），且 300750 的 +10% 不该被当成涨停（20% 档）。"""
    body = client.get("/api/v1/market/overview", params={"date": DAY2.isoformat()}).json()
    assert body["limits"] == {"limit_up": 1, "limit_down": 1, "judged": 5, "skipped": 0}
    assert body["limits_meta"]["adjust"] == "raw"
    # 口径说明随响应出，界面照登——前端不自己拼一份免责话术
    assert "raw" in body["limits_meta"]["note"]


def test_overview_index_is_the_same_ruler_as_backtest(market_dir: Path) -> None:
    """等权代理 = 当日 change_pct 的等权均值；基点 1000 锚在第一个有收益的交易日。"""
    body = client.get("/api/v1/market/overview", params={"date": DAY2.isoformat()}).json()
    index = body["index"]
    assert index["kind"] == "market_equal_weight"
    assert index["change_pct"] == pytest.approx(1.0)  # (10 + 0 − 10 + 5 + 0) / 5
    assert index["level"] == pytest.approx(1010.0)
    assert index["base_level"] == 1000.0
    assert index["base_date"] == DAY1.isoformat()
    assert index["days"] == 2
    # 「不是指数」这句话必须随响应出，界面才有得照登
    assert "不覆盖指数" in index["note"]


def test_overview_defaults_to_latest_session(market_dir: Path) -> None:
    body = client.get("/api/v1/market/overview").json()
    assert body["trade_date"] == DAY2.isoformat()


def test_overview_reports_both_as_of_dates(market_dir: Path) -> None:
    """行情与事件两个「截至」并列给出——它们天然不同步，界面不该挑一个假装同步。"""
    fresh = client.get("/api/v1/market/overview").json()["as_of"]
    assert fresh["bars"] == DAY2.isoformat()
    assert fresh["events"] == DAY2.isoformat()


def test_sectors_pit_cutoff_hides_after_close_event(market_dir: Path) -> None:
    """**本段最要紧的一条**：`available_at` 在当日收盘之后的事件，对当日板块块不可见。

    E2 把 300750 挂进「半导体」但发生在 16:00 ⇒ DAY2 的半导体必须仍是 3 只、
    均值仍是 0.0；若 PIT 卡口失效，会变成 4 只、均值 1.25。**计数与数值两条一起断言**——
    只断计数的话，一条「算进去了但恰好不影响均值」的实现会假绿。
    """
    body = client.get(
        "/api/v1/market/sectors", params={"date": DAY2.isoformat(), "min_symbols": 3}
    ).json()
    semiconductor = next(item for item in body["items"] if item["industry"] == "半导体")
    assert semiconductor["symbols"] == 3
    assert semiconductor["avg_pct"] == pytest.approx(0.0)
    assert semiconductor["median_pct"] == pytest.approx(0.0)


def test_sectors_are_sorted_by_average_desc(market_dir: Path) -> None:
    body = client.get(
        "/api/v1/market/sectors", params={"date": DAY2.isoformat(), "min_symbols": 3}
    ).json()
    assert [item["industry"] for item in body["items"]] == ["白酒", "半导体"]
    assert body["items"][0]["avg_pct"] == pytest.approx(5.0)  # (10 + 0 + 5) / 3


def test_sectors_carry_coverage_and_note(market_dir: Path) -> None:
    """601398 没有任何标签 ⇒ 覆盖率 4/5。**不许画成一张看起来完整的图。**"""
    body = client.get(
        "/api/v1/market/sectors", params={"date": DAY2.isoformat(), "min_symbols": 3}
    ).json()
    assert body["coverage"] == {"total": 5, "covered": 4, "ratio": pytest.approx(0.8)}
    assert "非权威行业分类" in body["note"]
    # 「一只标的可同时属于多个标签」这件事必须写在响应里——它是「成交额之和 > 全市场」的原因
    assert "不可读作份额" in body["note"]


def test_sectors_min_symbols_filters_small_samples(market_dir: Path) -> None:
    body = client.get(
        "/api/v1/market/sectors", params={"date": DAY2.isoformat(), "min_symbols": 4}
    ).json()
    assert body["items"] == []


def test_sectors_min_symbols_floor_is_422(market_dir: Path) -> None:
    """样本下限压到 1 就等于取消这道护栏——端点层直接拒。"""
    assert client.get("/api/v1/market/sectors", params={"min_symbols": 1}).status_code == 422


def test_overview_503_when_data_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """数据没落盘是「依赖没就绪」（503），不是 500，也不是一屏零。"""
    monkeypatch.setattr(duckdb_client, "resolve_data_dir", lambda _=None: tmp_path / "空")
    assert client.get("/api/v1/market/overview").status_code == 503
    assert client.get("/api/v1/market/sectors").status_code == 503
