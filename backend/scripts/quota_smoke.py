"""E2a 真机冒烟：两个配额在真 HTTP 上**可拒、可查、跨重启仍在**。

自起一个**紧配额**的 uvicorn（`QUOTA_AGENT_CALLS_PER_DAY=2` / `QUOTA_ACTIVE_TASKS=1`），
验四件事：

  ① **可拒（计数器）**：Agent 配额打满 → 429 + `kind="quota_exceeded"` + 一句能读的文案；
  ② **可拒（gauge）**：一个网格在飞时，紧接着的第二次提交被拒——在飞任务数是**直查任务表**的
     gauge（网格要跑几十秒，这一发必然落在窗口里，不靠运气）；
  ③ **可查**：两张表在真库里有行（`usage_counters` / `quota_rejections`）——admin 视图与验收
     「超配额被拒并给出明确提示」的证据读的就是它们；
  ④ **跨重启**：**杀进程重启**后再打一发仍然 429——计数落 Postgres，不是进程内状态
     （这正是 SPEC §5.4 把「速率」与「累计用量」分成两件事的原因）。

用完即杀，账号从开发库清掉（同集成用例口径，不留垃圾）。证据写 `logs/e2/quota-smoke.md`。

用法：
    cd backend && uv run python scripts/quota_smoke.py
    # 已有服务时也能打（但那时配额是它的默认值，①②会验不出来）：
    uv run python scripts/quota_smoke.py --base http://127.0.0.1:8010
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:  # 脚本从 backend/scripts 跑，包在 backend/ 下
    sys.path.insert(0, str(BACKEND_DIR))

OUT_DIR = BACKEND_DIR.parent / "logs" / "e2"
OUT_FILE = OUT_DIR / "quota-smoke.md"

PASSWORD = "e2-smoke-pass"
#: 压到「打两下就满」——测的是机制，不是默认额度（默认 200 / 3 要打很久）
TIGHT_QUOTA = {"QUOTA_AGENT_CALLS_PER_DAY": "2", "QUOTA_ACTIVE_TASKS": "1"}

GRID_BODY: dict[str, Any] = {
    "symbol": "600519",
    "strategy": "ma_cross",
    # `axes` 是**列表**（`list[Axis]`，每条 `{param, values}`）——首跑在这里吃了个 422，
    # 而 `test_rate_limit.py` 里同样的错形状只用它触发 429、body 从未被校验过
    "axes": [{"param": "fast", "values": [5, 8]}],
}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def call(
    base: str,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    cookie: str = "",
) -> tuple[int, Any, str]:
    """极简 HTTP 调用：只依赖标准库。返回 `(状态码, 解析后的体或 None, 原文前 400 字)`。

    chat 是 SSE，体不是 JSON——解析失败不当错误，原文交给调用方（只看状态码即够）。
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(f"{base}{path}", data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if cookie:
        request.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(request, timeout=180) as response:  # noqa: S310 - 本机地址
            payload = response.read().decode("utf-8")
            status = response.status
    except urllib.error.HTTPError as error:
        payload = error.read().decode("utf-8")
        status = error.code
    try:
        return status, json.loads(payload), payload[:400]
    except ValueError:
        return status, None, payload[:400]


def wait_ready(base: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, _, _ = call(base, "/health")
            if status == 200:
                return
        except OSError:
            pass
        time.sleep(0.5)
    raise TimeoutError(f"{base} 在 {timeout:g}s 内没有就绪")


def start_server(port: int) -> subprocess.Popen[bytes]:
    env = {**os.environ, **TIGHT_QUOTA}
    return subprocess.Popen(  # noqa: S603 - 命令写死，只传端口
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)],
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def stop_server(server: subprocess.Popen[bytes]) -> None:
    server.terminate()
    server.wait(timeout=10)


def sign_up_cookie(base: str, address: str) -> str:
    """注册（注册即登录）后**再登录一次**取回会话 cookie——`name=value`，属性用不上。"""
    status, _, raw = call(
        base, "/api/v1/auth/register", method="POST", body={"email": address, "password": PASSWORD}
    )
    assert status == 201, (status, raw)

    request = urllib.request.Request(
        f"{base}/api/v1/auth/login",
        data=json.dumps({"email": address, "password": PASSWORD}).encode("utf-8"),
        method="POST",
    )
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - 本机地址
        raw_cookie = response.headers.get("Set-Cookie", "")
    assert raw_cookie, "登录没有回 Set-Cookie"
    return raw_cookie.split(";", 1)[0]


def db_rows(address: str) -> dict[str, Any]:
    """直接读真库：文案与自述都不足以作证，**行**才算。"""
    import psycopg

    from app.core.config import get_settings

    with psycopg.connect(get_settings().postgres_dsn) as conn:
        counters = conn.execute(
            "SELECT c.metric, c.period_key, c.used FROM usage_counters c "
            "JOIN users u ON u.id = c.user_id WHERE u.email = %s ORDER BY c.metric",
            (address,),
        ).fetchall()
        rejections = conn.execute(
            "SELECT r.metric, r.used, r.limit_value FROM quota_rejections r "
            "JOIN users u ON u.id = r.user_id WHERE u.email = %s ORDER BY r.id",
            (address,),
        ).fetchall()
    return {"counters": counters, "rejections": rejections}


def purge_account(address: str) -> None:
    """删账号（`usage_counters` / `quota_rejections` / `tasks` 都随外键级联）。"""
    try:
        import psycopg

        from app.core.config import get_settings

        with psycopg.connect(get_settings().postgres_dsn) as conn:
            conn.execute("DELETE FROM users WHERE email = %s", (address,))
    except Exception as exc:  # noqa: BLE001 - 清不掉不该让证据作废，如实打印
        print(f"（清理账号失败：{type(exc).__name__}: {exc}）")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", help="打已有服务（不传则自起一个紧配额实例）")
    args = parser.parse_args()

    address = f"e2-smoke-{uuid.uuid4().hex[:8]}@example.com"
    lines: list[str] = ["# E2a 配额真机冒烟", ""]
    verdicts: list[tuple[str, bool, str]] = []
    server: subprocess.Popen[bytes] | None = None
    port = 0
    base = args.base or ""
    if not base:
        port = free_port()
        base = f"http://127.0.0.1:{port}"

    try:
        if not args.base:
            server = start_server(port)
        wait_ready(base)
        origin = (
            "（脚本自起，紧配额 " + json.dumps(TIGHT_QUOTA, ensure_ascii=False) + "）"
            if server
            else "（既有服务）"
        )
        lines.append(f"- 服务：`{base}`{origin}")
        lines.append(f"- 冒烟账号：`{address}`")
        lines.append("")

        cookie = sign_up_cookie(base, address)

        # ① 计数器：第三发被拒
        chat_statuses: list[int] = []
        rejection: dict[str, Any] = {}
        for _ in range(3):
            status, _, raw = call(base, "/api/v1/chat", method="POST", body={"message": "你好"}, cookie=cookie)
            chat_statuses.append(status)
            if status == 429:
                rejection = json.loads(raw)
        lines.append(f"## ① Agent 配额：`/chat` 连打三发 → {chat_statuses}")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(rejection, ensure_ascii=False, indent=2))
        lines.append("```")
        verdicts.append(
            (
                "① 前两发放行、第三发 429 且 kind=quota_exceeded",
                chat_statuses[0] != 429
                and chat_statuses[1] != 429
                and chat_statuses[2] == 429
                and rejection.get("kind") == "quota_exceeded",
                f"三发 {chat_statuses}",
            )
        )
        lines.append("")

        # ② gauge：一个网格在飞，第二次提交被拒
        first_status, first_body, first_raw = call(
            base, "/api/v1/optimize/grid", method="POST", body=GRID_BODY, cookie=cookie
        )
        second_status, second_body, second_raw = call(
            base, "/api/v1/optimize/grid", method="POST", body=GRID_BODY, cookie=cookie
        )
        lines.append(f"## ② 在飞任务配额：连提两个网格 → {[first_status, second_status]}")
        lines.append("")
        lines.append("```json")
        lines.append(second_raw)
        lines.append("```")
        verdicts.append(
            (
                "② 首个 202、第二个 429（metric=active_tasks）",
                first_status == 202 and second_status == 429,
                f"第一个 {first_status} task={first_body.get('task_id') if first_body else first_raw[:60]}",
            )
        )
        lines.append("")

        # ③ 真库里的行
        rows = db_rows(address)
        lines.append("## ③ 真库里的行")
        lines.append("")
        lines.append(f"- `usage_counters`：{rows['counters']}")
        lines.append(f"- `quota_rejections`：{rows['rejections']}")
        verdicts.append(
            (
                "③ 计数行 used=2、拒绝记录两条（agent_calls + active_tasks）",
                [r[2] for r in rows["counters"]] == [2]
                and len(rows["rejections"]) == 2,
                f"{rows}",
            )
        )
        lines.append("")

        # ④ 跨重启
        if server is not None:
            stop_server(server)
            server = start_server(port)
            wait_ready(base)
            status, _, raw = call(base, "/api/v1/chat", method="POST", body={"message": "你好"}, cookie=cookie)
            lines.append(f"## ④ 杀进程重启后再打一发 → {status}")
            lines.append("")
            verdicts.append(
                ("④ 重启后仍 429（计数落库，不是进程内状态）", status == 429, raw[:120])
            )
            lines.append("")
        else:
            lines.append("## ④ 跨重启：跳过（打的是既有服务，没由脚本起停）")
            lines.append("")

        ok = all(item[1] for item in verdicts)
        lines.append("## 判定")
        lines.append("")
        for label, passed, note in verdicts:
            lines.append(f"- {'✅' if passed else '❌'} {label} —— {note}")
        lines.append("")
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        OUT_FILE.write_text("\n".join(lines), encoding="utf-8")
        print("\n".join(lines))
        print(f"\n证据已写入 {OUT_FILE}")
        return 0 if ok else 1
    finally:
        if server is not None:
            stop_server(server)
        purge_account(address)


if __name__ == "__main__":
    raise SystemExit(main())
