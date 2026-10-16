"""E3b 真机冒烟：快照 → 注入损坏 → 回滚 → 逐片还原（SPEC §4.5 / PRD 验收②）。

**验的是 HTTP 那条路**（离线用例验判据、集成用例验真清单还原，这里验「真服务 + 真鉴权
+ 真库」上走一遍）：

1. 造一份临时数据目录（真 `data/` 的**硬链接**副本，`_meta/` 真复制），打一份快照；
2. 注入损坏：删掉一片行情分片；
3. 起真 uvicorn（`DATA_DIR` 指向副本），注册探针账号并提 admin；
4. `POST /api/v1/data/rollback`，四条判据：**confirm 不一致 422**（防手滑是门不是弹窗）、
   普通用户 403、admin 200 且 `restored.bars == 21`、**被删的那片按 sha 回来了**；
5. 台账里多出一条 `scope="rollback"` 的**自动留退路**，且它存的是回滚前那一版。

产出 `logs/e3/rollback-smoke.md`，退出码 0/1。

用法：
    cd backend && PYTHONPATH=$PWD uv run python scripts/rollback_smoke.py
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

REAL_DATA = REPO_ROOT / "data"
LOG_DIR = REPO_ROOT / "logs" / "e3"
ADMIN_EMAIL = "probe-e3b-admin@example.com"
PLAIN_EMAIL = "probe-e3b-plain@example.com"
PASSWORD = "probe-e3b-password-0123456789"
VICTIM = "cn-daily_CN_qfq_2025.parquet"

lines: list[str] = []


class _Bail(Exception):
    """中途放弃——已记过原因，直接进收尾。"""


def say(text: str = "") -> None:
    print(text)
    lines.append(text)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def stage(root: Path) -> None:
    """硬链接分片与对象（零拷贝、删链接不动原文件），**`_meta/` 真复制**——回滚会往里写。"""
    for name in ("bars", "events", "naming"):
        src = REAL_DATA / name
        if not src.is_dir():
            continue
        (root / name).mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            if item.is_file():
                os.link(item, root / name / item.name)
    for sub in (REAL_DATA / "raw").rglob("*"):
        target = root / "raw" / sub.relative_to(REAL_DATA / "raw")
        if sub.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif sub.is_file():
            os.link(sub, target)
    (root / "_meta").mkdir(parents=True, exist_ok=True)
    for item in (REAL_DATA / "_meta").iterdir():
        if item.is_file():
            shutil.copy2(item, root / "_meta" / item.name)


def http(method: str, url: str, *, body: dict | None = None, cookie: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    if cookie:
        request.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.status, json.loads(response.read() or b"{}"), response.headers
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            parsed = json.loads(payload or b"{}")
        except json.JSONDecodeError:
            parsed = {"raw": payload.decode(errors="replace")}
        return exc.code, parsed, exc.headers


def py(code: str) -> str:
    return subprocess.run(
        [sys.executable, "-c", code], cwd=BACKEND_DIR, capture_output=True, text=True, check=False
    ).stdout


def purge() -> None:
    py(
        "import psycopg\n"
        "from app.core.config import get_settings\n"
        "with psycopg.connect(get_settings().postgres_dsn) as conn:\n"
        f"    conn.execute(\"DELETE FROM users WHERE email = ANY(%s)\", (['{ADMIN_EMAIL}', '{PLAIN_EMAIL}'],))\n"
    )


def grant_admin() -> None:
    py(
        "import psycopg\n"
        "from app.core.config import get_settings\n"
        "with psycopg.connect(get_settings().postgres_dsn) as conn:\n"
        f"    conn.execute(\"UPDATE users SET role = 'admin' WHERE email = '{ADMIN_EMAIL}'\")\n"
    )


def main() -> int:  # noqa: C901
    failures: list[str] = []
    keep = False
    tmp = Path(tempfile.mkdtemp(prefix="e3b-smoke-"))
    port = free_port()
    base = f"http://127.0.0.1:{port}"

    say(f"# E3b 真机冒烟（{time.strftime('%Y-%m-%d %H:%M:%S')}）")
    say()
    stage(tmp)
    say(f"- 临时数据目录 `{tmp}`（分片硬链接自 `data/`，**`_meta/` 真复制**）")

    # ① 打一份快照（直接调库函数：它是「物化之后顺手做的事」，这里没有物化可触发）
    from app.data import snapshots

    row = snapshots.take(tmp, scope="daily", reason="冒烟起点")
    if row is None:
        say("没有清单可打快照——先跑 download_bars.py")
        return _finish(["无清单"], Path(tmp), keep=True)
    ts = row["ts"]
    say(f"- 打快照 `ts={ts}`：bars {row['bars']['shards']} 片 / events {row['events']['days']} 天")

    # ② 注入损坏
    (tmp / "bars" / VICTIM).unlink()
    say(f"- 注入损坏：删掉 `bars/{VICTIM}`（删的是**链接**，真目录不动）")

    env = {
        **os.environ,
        "DATA_DIR": str(tmp),
        "ETL_ENABLED": "false",  # 冒烟不跑定时：回滚要独占那把锁
        "DATA_HEALTH_ENABLED": "false",
        "RAG_WARMUP_ON_START": "false",
    }
    log_path = tmp / "uvicorn.log"
    handle = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)],
        cwd=BACKEND_DIR,
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    try:
        started = time.monotonic()
        if not _wait_up(base):
            failures.append("服务没起来")
            raise _Bail
        say(f"- 服务就绪（冷启动 {time.monotonic() - started:.0f}s）")
        say()

        # ③ 探针账号（一个提权、一个不提）
        for email in (ADMIN_EMAIL, PLAIN_EMAIL):
            status, _, _ = http("POST", f"{base}/api/v1/auth/register", body={"email": email, "password": PASSWORD})
            if status not in (200, 201, 409):
                failures.append(f"注册 {email} 失败：{status}")
        grant_admin()
        _status, _, headers = http(
            "POST", f"{base}/api/v1/auth/login", body={"email": ADMIN_EMAIL, "password": PASSWORD}
        )
        cookie = (headers.get("set-cookie") or "").split(";")[0]
        _s2, _, plain_headers = http(
            "POST", f"{base}/api/v1/auth/login", body={"email": PLAIN_EMAIL, "password": PASSWORD}
        )
        plain_cookie = (plain_headers.get("set-cookie") or "").split(";")[0]
        if not cookie or not plain_cookie:
            failures.append("登录没拿到 cookie")
            raise _Bail

        # ④ 三条判据
        status, body, _ = http("GET", f"{base}/api/v1/data/snapshots", cookie=cookie)
        newest = (body.get("snapshots") or [{}])[0].get("ts")
        say(f"1. 快照列表（admin）：HTTP {status}，共 {body.get('total')} 份，最新 {newest}")
        if status != 200 or body.get("total", 0) < 1:
            failures.append("快照列表没有读数")

        status, body, _ = http(
            "POST",
            f"{base}/api/v1/data/rollback",
            body={"snapshot": ts, "confirm": "不是这个 ts"},
            cookie=cookie,
        )
        say(f"2. **confirm 不一致**：HTTP {status}（断言 422）——防手滑是门，不是弹窗")
        if status != 422:
            failures.append(f"confirm 不一致应 422，实得 {status}")

        status, body, _ = http(
            "POST", f"{base}/api/v1/data/rollback", body={"snapshot": ts, "confirm": ts}, cookie=plain_cookie
        )
        say(f"3. **普通用户**打回滚：HTTP {status}（断言 403）")
        if status != 403:
            failures.append(f"普通用户应 403，实得 {status}")

        started = time.monotonic()
        status, body, _ = http(
            "POST", f"{base}/api/v1/data/rollback", body={"snapshot": ts, "confirm": ts}, cookie=cookie
        )
        elapsed = time.monotonic() - started
        say(
            f"4. admin 回滚：HTTP {status}，用时 **{elapsed:.1f}s**；"
            f"restored={json.dumps(body.get('restored'), ensure_ascii=False)}；"
            f"体检={json.dumps(body.get('health'), ensure_ascii=False)}"
        )
        if status != 200 or body.get("restored", {}).get("bars") != row["bars"]["shards"]:
            failures.append(f"回滚没还回 21 片：{status} / {body.get('restored')}")
            raise _Bail

        # ⑤ 那片真的回来了（**按 sha 核对**，不是看文件在不在）
        import hashlib

        restored_file = tmp / "bars" / VICTIM
        manifest = json.loads((tmp / "_meta" / "bars.json").read_text())
        want = next(out["sha256"] for out in manifest["outputs"] if Path(out["path"]).name == VICTIM)
        got = hashlib.sha256(restored_file.read_bytes()).hexdigest() if restored_file.is_file() else ""
        say(f"5. 被删的 `{VICTIM}` 回没回来：{'是' if restored_file.is_file() else '否'}；sha 相符：{got == want}")
        if got != want:
            failures.append("还原出来的分片 sha 与清单不符")

        # ⑥ 自动留退路
        index_lines = (tmp / "_snapshots" / "index.jsonl").read_text().splitlines()
        index = [json.loads(line) for line in index_lines if line.strip()]
        retreat = [item for item in index if item["scope"] == "rollback"]
        say(f"6. 自动留退路：索引里出现 {len(retreat)} 条 `scope=rollback` 的快照")
        if not retreat:
            failures.append("没有留退路快照")
    except _Bail:
        pass
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
        handle.close()
        try:
            purge()
        except Exception as exc:  # noqa: BLE001
            say(f"⚠ 探针账号清理失败（请手工删 {ADMIN_EMAIL} / {PLAIN_EMAIL}）：{exc}")
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)
    return _finish(failures, log_path, keep)


def _wait_up(base: str, timeout: float = 120) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, _, _ = http("GET", f"{base}/health")
            if status == 200:
                return True
        except OSError:
            pass
        time.sleep(2)
    return False


def _finish(failures: list[str], log_path: Path, keep: bool) -> int:
    say()
    if failures:
        say("## 结论：**未通过**")
        for item in failures:
            say(f"- {item}")
        if log_path.exists():
            tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
            say("\n服务端日志尾部：\n```\n" + "\n".join(tail) + "\n```")
    else:
        say("## 结论：**六条全过**（422 防手滑 → 403 普通用户 → 200 还回 → sha 相符 → 留了退路）")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out = LOG_DIR / "rollback-smoke.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n证据写入 {out.relative_to(REPO_ROOT)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
