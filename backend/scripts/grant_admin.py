"""给某个账号授 / 撤 admin（E2b）。

角色是**数据**（进库），不是行为参数（进 settings）——改它不该要重启服务，所以给的是一次性
脚本而不是配置项。改完**立即生效**：角色不进 JWT，`require_user` 每次都查 users 整行
（理由见 `core/auth.py::require_admin`）。

用法：
    cd backend && uv run python scripts/grant_admin.py who@example.com        # 提为 admin
    uv run python scripts/grant_admin.py who@example.com --revoke             # 收回普通用户
    uv run python scripts/grant_admin.py --list                               # 看当前谁是 admin

退出码：0 成功；2 没有这个账号；1 用法错。

⚠ **无操作审计**（谁在何时提了谁）——本地单机脚本，SPEC §3.7 已把这条如实记为已知边界。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:  # 脚本从 backend/scripts 跑，包在 backend/ 下
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.auth import ROLE_ADMIN, ROLE_USER  # noqa: E402
from app.core.config import get_settings  # noqa: E402


def _connect():  # noqa: ANN202 - psycopg.Connection，避免为注解拉 import
    import psycopg

    return psycopg.connect(get_settings().postgres_dsn)


def list_admins() -> int:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, email FROM users WHERE role = %s ORDER BY id", (ROLE_ADMIN,)
        ).fetchall()
    if not rows:
        print("当前没有 admin（ETL 两端点需要 admin，见 SPEC §3.4）")
        return 0
    for user_id, email in rows:
        print(f"{user_id}\t{email}")
    return 0


def set_role(email: str, role: str) -> int:
    with _connect() as conn:
        row = conn.execute(
            "UPDATE users SET role = %s WHERE email = %s RETURNING id, email, role",
            (role, email.strip().lower()),
        ).fetchone()
    if row is None:
        print(f"没有这个账号：{email}（注册后才有）", file=sys.stderr)
        return 2
    print(f"{row[0]}\t{row[1]}\t→ {row[2]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="授 / 撤 admin（E2b）")
    parser.add_argument("email", nargs="?", help="账号邮箱")
    parser.add_argument("--revoke", action="store_true", help="收回成普通用户")
    parser.add_argument("--list", action="store_true", help="列出当前 admin")
    args = parser.parse_args()

    if args.list:
        return list_admins()
    if not args.email:
        parser.error("要一个邮箱（或 --list）")
    return set_role(args.email, ROLE_USER if args.revoke else ROLE_ADMIN)


if __name__ == "__main__":
    raise SystemExit(main())
