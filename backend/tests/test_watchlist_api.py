"""离线单测：自选股六端点（M1c）。

三块重点：
  * **归属**：列表只回本人的、改/删他人条目一律 404（与「不存在」同响应）——注入内存业务库，
    真库那份在 `tests/integration/test_watchlist_m1c.py` 双跑（过滤写在 SQL 里，离线证明不了）；
  * **价格口径**：加自选记当时最近可得收盘价，取不到记 NULL，涨幅随之留空——**不得编数**；
  * **降级**：行情层整体不可用时列表照常返回（价格全空），自选股不该被行情依赖拖死。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.data import duckdb_client
from app.main import app
from tests.conftest import make_backtest_dir
from tests.fakes import FakeDatabase

client = TestClient(app)

SYMBOL = "600519"
#: 样例数据里没有它——用来验「取不到价就留空」
ABSENT = "000001"

#: 最后一根 bar 的收盘价；加自选时记的就是它
LAST_CLOSE = 12.0


def sample_bars() -> list[dict[str, Any]]:
    return [
        {"trade_date": day, "open": close, "close": close, "high": close, "low": close}
        for day, close in zip(
            (date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 3)), (10.0, 11.0, LAST_CLOSE)
        )
    ]


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    make_backtest_dir(tmp_path, sample_bars(), [], symbol=SYMBOL)
    monkeypatch.setattr(duckdb_client, "resolve_data_dir", lambda _=None: tmp_path)
    return tmp_path


def seed(db: FakeDatabase, user_id: int, symbol: str, group: str, added_price: float | None) -> None:
    """直接摆一条自选（等价于「上次加过」），省掉一次 POST 的往返。"""
    db.watchlist[(user_id, symbol)] = {
        "symbol": symbol,
        "group_name": group,
        "added_at": datetime(2026, 7, 3, 15, 0, tzinfo=UTC),
        "added_price": added_price,
    }


# ── 鉴权门 ──────────────────────────────────────────────────────────────────


def test_watchlist_requires_login(jwt_secret: Any) -> None:
    """未登录一律 401（不是 404、更不是空列表——空列表会把「没登录」伪装成「没数据」）。"""
    assert client.get("/api/v1/watchlist").status_code == 401
    assert client.post("/api/v1/watchlist", json={"symbol": SYMBOL}).status_code == 401
    assert client.patch(f"/api/v1/watchlist/{SYMBOL}", json={"group_name": "x"}).status_code == 401
    assert client.delete(f"/api/v1/watchlist/{SYMBOL}").status_code == 401
    assert client.patch("/api/v1/watchlist/groups/x", json={"name": "y"}).status_code == 401
    assert client.delete("/api/v1/watchlist/groups/x").status_code == 401


# ── 增删改 ──────────────────────────────────────────────────────────────────


def test_add_records_price_at_the_moment_of_adding(signed_in: FakeDatabase, data_dir: Path) -> None:
    response = client.post("/api/v1/watchlist", json={"symbol": SYMBOL})

    assert response.status_code == 201
    body = response.json()
    assert body["symbol"] == SYMBOL
    assert body["group_name"] == "默认分组"  # 缺省分组
    assert body["added_price"] == LAST_CLOSE  # 加入时最近可得收盘价
    assert body["latest_close"] == LAST_CLOSE
    assert body["latest_trade_date"] == "2026-07-03"
    assert body["change_pct"] == 0.0  # 刚加：最新价就是加入价


def test_add_without_price_data_keeps_null(
    signed_in: FakeDatabase, data_dir: Path
) -> None:
    """样例数据没有这个标的：价格留空而不是编一个数，加自选本身照常成功。"""
    body = client.post("/api/v1/watchlist", json={"symbol": ABSENT}).json()

    assert body["added_price"] is None
    assert body["latest_close"] is None
    assert body["latest_trade_date"] is None
    assert body["change_pct"] is None


def test_add_twice_is_409(signed_in: FakeDatabase, data_dir: Path) -> None:
    assert client.post("/api/v1/watchlist", json={"symbol": SYMBOL}).status_code == 201
    again = client.post("/api/v1/watchlist", json={"symbol": SYMBOL})

    assert again.status_code == 409
    assert SYMBOL in again.json()["detail"]


def test_add_rejects_bad_symbol(signed_in: FakeDatabase, data_dir: Path) -> None:
    assert client.post("/api/v1/watchlist", json={"symbol": "60051"}).status_code == 422
    assert client.post("/api/v1/watchlist", json={"symbol": "abcdef"}).status_code == 422


@pytest.mark.parametrize("name", ["", "   ", "a/b", "x" * 25, "带\n换行"])
def test_add_rejects_bad_group_name(
    signed_in: FakeDatabase, data_dir: Path, name: str
) -> None:
    """含 `/` 的组名在路由层根本不可达（`%2F` 解码成路径分隔符），必须在写入侧挡住。"""
    response = client.post(
        "/api/v1/watchlist", json={"symbol": SYMBOL, "group_name": name}
    )
    assert response.status_code == 422


def test_group_name_is_trimmed(signed_in: FakeDatabase, data_dir: Path) -> None:
    body = client.post(
        "/api/v1/watchlist", json={"symbol": SYMBOL, "group_name": "  长线  "}
    ).json()
    assert body["group_name"] == "长线"


def test_change_pct_uses_added_price_as_base(
    signed_in: FakeDatabase, data_dir: Path
) -> None:
    """涨幅 = (最新可得收盘 − 加入时价) / 加入时价；加入价是不动的历史事实。"""
    seed(signed_in, 1, SYMBOL, "默认分组", 10.0)
    item = client.get("/api/v1/watchlist").json()[0]
    assert item["change_pct"] == pytest.approx((LAST_CLOSE - 10.0) / 10.0)


def test_move_item_to_another_group(signed_in: FakeDatabase, data_dir: Path) -> None:
    client.post("/api/v1/watchlist", json={"symbol": SYMBOL})
    response = client.patch(f"/api/v1/watchlist/{SYMBOL}", json={"group_name": "长线"})

    assert response.status_code == 200
    assert client.get("/api/v1/watchlist").json()[0]["group_name"] == "长线"


def test_move_absent_item_is_404(signed_in: FakeDatabase, data_dir: Path) -> None:
    response = client.patch(f"/api/v1/watchlist/{SYMBOL}", json={"group_name": "长线"})
    assert response.status_code == 404


def test_delete_item(signed_in: FakeDatabase, data_dir: Path) -> None:
    client.post("/api/v1/watchlist", json={"symbol": SYMBOL})
    assert client.delete(f"/api/v1/watchlist/{SYMBOL}").json() == {
        "symbol": SYMBOL,
        "deleted": True,
    }
    assert client.get("/api/v1/watchlist").json() == []
    assert client.delete(f"/api/v1/watchlist/{SYMBOL}").status_code == 404


# ── 归属 ────────────────────────────────────────────────────────────────────


def test_list_only_returns_own_items(signed_in: FakeDatabase, data_dir: Path) -> None:
    seed(signed_in, 1, SYMBOL, "默认分组", 10.0)
    seed(signed_in, 2, ABSENT, "别人的组", 1.0)

    assert [item["symbol"] for item in client.get("/api/v1/watchlist").json()] == [SYMBOL]


def test_touching_another_users_item_is_404(
    signed_in: FakeDatabase, data_dir: Path
) -> None:
    """越权与不存在同返 404：区分开就等于告诉调用方「这个号确实有人持有」。"""
    seed(signed_in, 2, ABSENT, "默认分组", 1.0)

    assert client.patch(f"/api/v1/watchlist/{ABSENT}", json={"group_name": "x"}).status_code == 404
    assert client.delete(f"/api/v1/watchlist/{ABSENT}").status_code == 404
    assert (2, ABSENT) in signed_in.watchlist  # 没被误删


# ── 分组 ────────────────────────────────────────────────────────────────────


def test_rename_group_merges_when_target_exists(
    signed_in: FakeDatabase, data_dir: Path
) -> None:
    """重命名撞名即合并——一条 UPDATE 的自然语义，不额外设 409。"""
    seed(signed_in, 1, SYMBOL, "长线", 10.0)
    seed(signed_in, 1, ABSENT, "核心", 1.0)

    response = client.patch("/api/v1/watchlist/groups/长线", json={"name": "核心"})

    assert response.status_code == 200
    groups = sorted(item["group_name"] for item in client.get("/api/v1/watchlist").json())
    assert groups == ["核心", "核心"]


def test_rename_absent_group_is_404(signed_in: FakeDatabase, data_dir: Path) -> None:
    assert client.patch("/api/v1/watchlist/groups/没有这个组", json={"name": "x"}).status_code == 404


def test_drop_group_falls_back_to_default(signed_in: FakeDatabase, data_dir: Path) -> None:
    """删组只回落分组，不删标的。"""
    seed(signed_in, 1, SYMBOL, "长线", 10.0)

    response = client.delete("/api/v1/watchlist/groups/长线")

    assert response.status_code == 200
    item = client.get("/api/v1/watchlist").json()[0]
    assert item["symbol"] == SYMBOL
    assert item["group_name"] == "默认分组"


def test_drop_absent_group_is_404(signed_in: FakeDatabase, data_dir: Path) -> None:
    assert client.delete("/api/v1/watchlist/groups/没有这个组").status_code == 404


def test_default_group_cannot_be_renamed_or_dropped(
    signed_in: FakeDatabase, data_dir: Path
) -> None:
    """「默认分组」是回落目标：改名或删掉它，回落目标就没了（且与建表默认值脱节）。"""
    renamed = client.patch("/api/v1/watchlist/groups/默认分组", json={"name": "别的"})
    dropped = client.delete("/api/v1/watchlist/groups/默认分组")

    assert renamed.status_code == 400
    assert dropped.status_code == 400


def test_group_ops_are_scoped_to_the_owner(signed_in: FakeDatabase, data_dir: Path) -> None:
    seed(signed_in, 2, ABSENT, "别人的组", 1.0)

    assert client.patch("/api/v1/watchlist/groups/别人的组", json={"name": "x"}).status_code == 404
    assert client.delete("/api/v1/watchlist/groups/别人的组").status_code == 404
    assert signed_in.watchlist[(2, ABSENT)]["group_name"] == "别人的组"


# ── 降级 ────────────────────────────────────────────────────────────────────


def test_list_degrades_when_market_data_is_unavailable(
    signed_in: FakeDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """行情层不可用不该把自选股列表一起打死：价格降级为「—」，条目照常返回。"""
    monkeypatch.setattr(duckdb_client, "resolve_data_dir", lambda _=None: tmp_path / "nope")
    seed(signed_in, 1, SYMBOL, "默认分组", 10.0)

    response = client.get("/api/v1/watchlist")

    assert response.status_code == 200
    item = response.json()[0]
    assert item["symbol"] == SYMBOL
    assert item["added_price"] == 10.0  # 加入时记下的价还在
    assert item["latest_close"] is None
    assert item["change_pct"] is None
