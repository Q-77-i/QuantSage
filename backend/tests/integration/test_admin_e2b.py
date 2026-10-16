"""集成用例：角色与 admin 视图走**真库、真 HTTP**（P3-E2b）。

离线用例用内存替身，验的是**规则**；这里的两条只有真库能证：

  * `users.role` 这一列真的加上了（离线替身是自己造字典，加没加它都不知道）；
  * `admin_usage` 的**一条 SQL 三个子读数**（在飞任务 / 当期调用 / 当期被拒）在真库上跑得通、
    数得对——它的 join 与子查询是假库照抄不出来的那一层（同 M4c-1「替身会骗人」的教训）。

角色用同步 psycopg 直改（集成栈的异步池绑在 TestClient 的事件循环上），
正是 `grant_admin.py` 那条路径的等价物。

运行方式（README）：`uv run pytest -m integration`（需容器与真实密钥）。
"""

from __future__ import annotations

import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from tests.integration.conftest import purge_rows, sign_up

pytestmark = pytest.mark.integration


def email(prefix: str) -> str:
    return f"e2b-{prefix}-{uuid.uuid4().hex[:8]}@example.com"


def set_role(address: str, role: str) -> None:
    """与 `scripts/grant_admin.py` 同一条 SQL（那条脚本不好在用例里 import 着跑）。"""
    with psycopg.connect(get_settings().postgres_dsn) as conn:
        conn.execute("UPDATE users SET role = %s WHERE email = %s", (role, address))


def test_me_carries_the_role_from_the_real_column(real_stack: TestClient) -> None:
    address = email("me")
    member = sign_up(address)
    try:
        assert member.get("/api/v1/auth/me").json()["role"] == "user"

        set_role(address, "admin")

        # 改角色**立即生效**：角色不进 JWT，下一次请求就读到新值
        assert member.get("/api/v1/auth/me").json()["role"] == "admin"
    finally:
        purge_rows([address], [])


def test_etl_gate_flips_with_the_role(real_stack: TestClient) -> None:
    """本段唯一的越权面修复：普通用户 403，提权后 200。"""
    address = email("etl")
    member = sign_up(address)
    try:
        assert member.get("/api/v1/etl/status").status_code == 403

        set_role(address, "admin")

        assert member.get("/api/v1/etl/status").status_code == 200
    finally:
        purge_rows([address], [])


def test_usage_view_reads_the_real_tables(real_stack: TestClient) -> None:
    """造三样读数，逐列断言——「三列都返回 0」的实现过不了这条。"""
    address = email("usage")
    member = sign_up(address)
    try:
        submitted = member.post(
            "/api/v1/backtest",
            json={"strategy": "ma_cross", "symbol": "600519"},
        )
        assert submitted.status_code == 202, submitted.text

        with psycopg.connect(get_settings().postgres_dsn) as conn:
            uid = conn.execute("SELECT id FROM users WHERE email = %s", (address,)).fetchone()[0]
            conn.execute(
                "INSERT INTO usage_counters (user_id, metric, period_key, used) "
                "VALUES (%s, 'agent_calls', %s, 7) "
                "ON CONFLICT (user_id, metric, period_key) DO UPDATE SET used = 7",
                (uid, _today()),
            )
            conn.execute(
                "INSERT INTO quota_rejections (user_id, metric, used, limit_value) "
                "VALUES (%s, 'agent_calls', 7, 7)",
                (uid,),
            )
            conn.execute("UPDATE users SET role = 'admin' WHERE id = %s", (uid,))
            conn.execute(
                "INSERT INTO quota_rejections (user_id, metric, used, limit_value) "
                "VALUES (%s, 'agent_calls', 1, 1)",
                (uid,),
            )

        # 用搜索钉住这一行：**加分页之后**「新账号在不在第一页」取决于库里有几个账号
        # （开发库 130+ 个，默认 limit=50 根本轮不到它）——这正是分页要防的那件事
        body = member.get(f"/api/v1/admin/usage?q={address}").json()

        row = body["users"][0]
        assert row["role"] == "admin"
        assert row["active_tasks"] == 1  # 刚提交的那件还在队列里（worker 要跑几秒）
        assert row["agent_calls_today"] == 7
        assert row["rejected_today"] == 2  # 两行都是今天写的
        assert body["limits"] == {"active_tasks": 3, "agent_calls_per_day": 200}

        # 筛选也打真库（E2c）：这个账号是 admin 且有读数，两个筛选都该把它留下
        filtered = member.get(
            f"/api/v1/admin/usage?role=admin&active_only=true&q={address}"
        ).json()
        assert [item["email"] for item in filtered["users"]] == [address]
        assert filtered["total"] == 1  # total 与行同一份过滤（同一条派生表 + 同一个 WHERE）
        quiet = member.get(f"/api/v1/admin/usage?role=user&active_only=true&q={address}").json()
        assert quiet["users"] == [] and quiet["total"] == 0

        # 排序也打真库（E2c）：`ORDER BY` 拼的是**标识符**（占位符在那儿没用），
        # 白名单键与派生表列名对不上时只有真跑才发现——离线替身按 Python 键排，看不见这层
        ordered = member.get(
            f"/api/v1/admin/usage?sort=agent_calls_today&order=desc&q={address}"
        ).json()
        assert [item["email"] for item in ordered["users"]] == [address]

        listed = member.get("/api/v1/admin/rejections?limit=1").json()["rejections"]
        assert len(listed) == 1  # limit 真的落到了 SQL 上
        assert listed[0]["email"] == address
        assert listed[0]["metric"] == "agent_calls"
        assert listed[0]["limit_value"] == 1  # 新的在前
    finally:
        purge_rows([address], [])


def test_ordinary_user_still_gets_403_on_the_real_stack(real_stack: TestClient) -> None:
    address = email("plain")
    member = sign_up(address)
    try:
        assert member.get("/api/v1/admin/usage").status_code == 403
        assert member.get("/api/v1/admin/rejections").status_code == 403
    finally:
        purge_rows([address], [])


def _today() -> str:
    from app.core import quota

    return quota.period_key()
