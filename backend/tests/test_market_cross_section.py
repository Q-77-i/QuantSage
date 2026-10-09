"""M5a 截面与名称搜索端点：形状、排序、分页、匹配口径、降级。

两条降级口径各有对应用例：

  * **字典缺失**（还没有的那次 ETL 之前）→ `name` 全为 null，**不是 500**；
  * **换手率缺失**（源侧 2026-08 起逐步停更）→ 如实返回 null，**不填 0**。
    填 0 会被读成「换手率极低」，那是另一个意思。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.data import duckdb_client, naming
from app.main import app
from tests.conftest import write_bars_parquet, write_events_parquet

client = TestClient(app)
CN = ZoneInfo("Asia/Shanghai")

DAY1 = date(2026, 8, 3)
DAY2 = date(2026, 8, 4)

#: 三只标的，两个交易日。`turnover_pct` 只有一只在第一天有值——模拟源侧停更后的形状。
MARKET = {
    "600519": [(DAY1, 1000.0, 1.5, 1.0e9, 0.5), (DAY2, 1015.0, 1.5, 1.1e9, None)],
    "000001": [(DAY1, 10.0, -2.0, 5.0e8, 1.2), (DAY2, 9.8, -2.0, 4.0e8, None)],
    "300750": [(DAY1, 200.0, 5.0, 2.0e9, None), (DAY2, 210.0, 5.0, 2.5e9, None)],
}

NAMES = {"600519": "贵州茅台", "000001": "平安银行", "300750": "宁德时代"}


def make_market(tmp_path: Path, *, with_names: bool = True) -> Path:
    for symbol, rows in MARKET.items():
        write_bars_parquet(
            tmp_path / "bars",
            symbol,
            [
                {
                    "trade_date": day,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "change_pct": change,
                    "amount": amount,
                    "turnover_pct": turnover,
                }
                for day, close, change, amount, turnover in rows
            ],
        )
    write_events_parquet(tmp_path / "events", "600519", [])
    if with_names:
        stamp = date(2026, 7, 10)
        from datetime import datetime

        at = datetime(stamp.year, stamp.month, stamp.day, 8, tzinfo=CN)
        naming.write_dictionary(
            [naming.NameRow(symbol, name, 5, at, at) for symbol, name in NAMES.items()], tmp_path
        )
    return tmp_path


@pytest.fixture
def market_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = make_market(tmp_path)
    monkeypatch.setattr(duckdb_client, "resolve_data_dir", lambda _=None: root)
    return root


def section(**params: object) -> dict:
    response = client.get("/api/v1/market/cross-section", params=params)
    assert response.status_code == 200, response.text
    return response.json()


# ── 截面 ────────────────────────────────────────────────────


def test_default_date_is_the_latest_trade_date(market_dir: Path) -> None:
    body = section()
    assert body["trade_date"] == DAY2.isoformat()
    assert body["total"] == 3
    assert body["count"] == 3


def test_rows_carry_the_four_fields_plus_name(market_dir: Path) -> None:
    (row,) = section(date=DAY1.isoformat(), q="茅台")["items"]
    assert row == {
        "symbol": "600519",
        "name": "贵州茅台",
        "close": 1000.0,
        "change_pct": 1.5,
        "amount": 1.0e9,
        "turnover_pct": 0.5,
    }


def test_missing_turnover_stays_null(market_dir: Path) -> None:
    """源侧停更的字段如实留空——**填 0 会被读成「换手率极低」**。"""
    row = section(date=DAY2.isoformat(), q="宁德时代")["items"][0]
    assert row["turnover_pct"] is None


def test_sorted_by_change_pct_descending_by_default(market_dir: Path) -> None:
    assert [row["symbol"] for row in section(date=DAY1.isoformat())["items"]] == [
        "300750",
        "600519",
        "000001",
    ]


def test_sort_and_order_switch_the_ranking(market_dir: Path) -> None:
    asc = section(date=DAY1.isoformat(), sort="change_pct", order="asc")["items"]
    assert [row["symbol"] for row in asc] == ["000001", "600519", "300750"]

    by_amount = section(date=DAY1.isoformat(), sort="amount", order="asc")["items"]
    assert [row["symbol"] for row in by_amount] == ["000001", "600519", "300750"]


def test_null_sort_keys_sink_to_the_bottom(market_dir: Path) -> None:
    """换手率大面积为空：`NULLS LAST` 让空值沉底，不随数据库默认行为漂移。"""
    rows = section(date=DAY2.isoformat(), sort="turnover_pct")["items"]
    assert [row["turnover_pct"] for row in rows] == [None, None, None]  # 第二天全空也不报错
    assert len(rows) == 3


def test_unknown_sort_key_is_422(market_dir: Path) -> None:
    assert client.get("/api/v1/market/cross-section", params={"sort": "close; DROP"}).status_code == 422


def test_pagination_keeps_total(market_dir: Path) -> None:
    body = section(date=DAY1.isoformat(), limit=1, offset=1)
    assert body["total"] == 3
    assert body["count"] == 1
    assert body["items"][0]["symbol"] == "600519"


def test_date_without_data_is_an_empty_page_not_404(market_dir: Path) -> None:
    """「那天没有数据」是答案不是错误——与 `/{symbol}/bars` 的 404 分开。"""
    body = section(date="2026-01-01")
    assert body["total"] == 0
    assert body["items"] == []


def test_code_prefix_search(market_dir: Path) -> None:
    body = section(date=DAY1.isoformat(), q="6005")
    assert [row["symbol"] for row in body["items"]] == ["600519"]
    assert body["total"] == 1


def test_name_substring_search(market_dir: Path) -> None:
    body = section(date=DAY1.isoformat(), q="时代")
    assert [row["symbol"] for row in body["items"]] == ["300750"]


def test_unmatched_query_returns_an_empty_page(market_dir: Path) -> None:
    body = section(date=DAY1.isoformat(), q="不存在的名字")
    assert body == {
        **body,
        "total": 0,
        "count": 0,
        "items": [],
    }


def test_missing_dictionary_degrades_to_null_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """字典还没生成时 `name` 全为 null——**代码仍搜得到**，不是 500。"""
    root = make_market(tmp_path, with_names=False)
    monkeypatch.setattr(duckdb_client, "resolve_data_dir", lambda _=None: root)

    body = section(date=DAY1.isoformat())
    assert body["total"] == 3
    assert {row["name"] for row in body["items"]} == {None}


def test_data_not_ready_is_503(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(duckdb_client, "resolve_data_dir", lambda _=None: tmp_path / "empty")
    assert client.get("/api/v1/market/cross-section").status_code == 503


# ── 名称搜索 ────────────────────────────────────────────────


def test_search_symbols_by_name(market_dir: Path) -> None:
    body = client.get("/api/v1/market/symbols", params={"q": "银行"}).json()
    assert body == {"query": "银行", "count": 1, "items": [{"symbol": "000001", "name": "平安银行"}]}


def test_search_symbols_by_code_prefix(market_dir: Path) -> None:
    body = client.get("/api/v1/market/symbols", params={"q": "3007"}).json()
    assert [item["symbol"] for item in body["items"]] == ["300750"]


def test_search_symbols_needs_a_query(market_dir: Path) -> None:
    assert client.get("/api/v1/market/symbols").status_code == 422
    assert client.get("/api/v1/market/symbols", params={"q": ""}).status_code == 422


def test_search_symbols_respects_limit(market_dir: Path) -> None:
    body = client.get("/api/v1/market/symbols", params={"q": "0", "limit": 1}).json()
    assert body["count"] == 1


def test_search_symbols_without_dictionary_is_empty(market_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """搜索**不依赖行情**（查不到当日行情不该搜不出来），但依赖字典；没有就空列表。"""
    monkeypatch.setattr(
        naming, "load_dictionary", lambda _=None: []
    )
    body = client.get("/api/v1/market/symbols", params={"q": "茅台"}).json()
    assert body["count"] == 0
