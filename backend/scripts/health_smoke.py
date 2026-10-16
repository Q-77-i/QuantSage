"""E3a 真机冒烟：体检定时化 → 落档 → 看板红灯（SPEC §4.8 的真机轨）。

四步，每步都要有真读数：

1. **造一份临时数据目录**：真实 `data/` 的分片用**硬链接**铺进去（零拷贝、不占空间），
   `_meta/` 则**真复制**（体检要往里追加台账——硬链接会把原目录的台账一起写坏）；
2. **注入一处脏数据**：删掉一片行情分片（模拟「物化产物受损」）。删链接不动原文件，
   所以真实 `data/` 毫发无损；
3. **起真 uvicorn**：`DATA_DIR` 指向临时目录、体检开关打开、cron 定在**下一分钟**
   ——等它自己跑，不是手动调函数（那样就退回单测了）；
4. **读两条证据**：落档里出现了 error（`_meta/health_runs.jsonl`）+ 看板接口回
   `check_status == "red"`（探针 admin 账号，用完即删）。

产出 `logs/e3/health-smoke.md`，退出码 0/1。

用法：
    cd backend && PYTHONPATH=$PWD uv run python scripts/health_smoke.py
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
from datetime import datetime, timedelta
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402

REAL_DATA = REPO_ROOT / "data"
LOG_DIR = REPO_ROOT / "logs" / "e3"
PROBE_EMAIL = "probe-e3a@example.com"
PROBE_PASSWORD = "probe-e3a-password-0123456789"
#: 冒烟删掉的那片——选一个「不影响回测窗口」的年份，删了照样能看出 B1 的对账能力
VICTIM = "cn-daily_CN_qfq_2025.parquet"

class _Bail(Exception):
    """冒烟中途放弃——已经记过原因，直接进收尾（不重复写失败项）。"""


lines: list[str] = []


def say(text: str = "") -> None:
    print(text)
    lines.append(text)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def stage_data_dir(root: Path) -> None:
    """硬链接分片、真复制清单——`_meta` 必须是真文件，体检要往里写台账。"""
    for name in ("bars", "events", "naming"):
        src = REAL_DATA / name
        if not src.exists():
            continue
        (root / name).mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            if item.is_file():
                os.link(item, root / name / item.name)
    (root / "_meta").mkdir(parents=True, exist_ok=True)
    for item in (REAL_DATA / "_meta").iterdir():
        if item.is_file():
            shutil.copy2(item, root / "_meta" / item.name)
    # 事件侧车（`raw/events/{day}.json`）也要铺：体检 E1 拿它与磁盘对账，
    # 缺了会报 93 处「日分区有问题」——**那是铺数据的锅，不是注入的脏数据**，
    # 证据里必须只剩一个 error，否则读的人分不清哪条是注入造成的
    raw_events = root / "raw" / "events"
    raw_events.mkdir(parents=True, exist_ok=True)
    for item in (REAL_DATA / "raw" / "events").iterdir():
        if item.is_file():
            shutil.copy2(item, raw_events / item.name)


def inject_dirt(root: Path) -> None:
    (root / "bars" / VICTIM).unlink()


def wait_for(predicate, *, timeout: float, interval: float = 2.0):  # noqa: ANN001, ANN201
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def http(method: str, url: str, *, body: dict | None = None, cookie: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    if cookie:
        request.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read() or b"{}"), response.headers
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            parsed = json.loads(payload or b"{}")
        except json.JSONDecodeError:
            parsed = {"raw": payload.decode(errors="replace")}
        return exc.code, parsed, exc.headers


def set_probe_admin() -> None:
    import psycopg

    with psycopg.connect(get_settings().postgres_dsn) as conn:
        conn.execute("UPDATE users SET role = 'admin' WHERE email = %s", (PROBE_EMAIL,))


def drop_probe() -> None:
    import psycopg

    with psycopg.connect(get_settings().postgres_dsn) as conn:
        conn.execute("DELETE FROM users WHERE email = %s", (PROBE_EMAIL,))


def main() -> int:  # noqa: C901
    failures: list[str] = []
    keep = False
    tmp = Path(tempfile.mkdtemp(prefix="e3a-smoke-"))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    now = datetime.now()
    # +2 分钟而不是 +1：服务冷启动实测约 65 秒（MCP + 工具 + worker），
    # 定在「下一分钟」会撞上「调度器起来时那一分钟已经过去了」
    fire = now + timedelta(minutes=2)
    # cron 的分与小时：跨小时/跨天时小时要进位
    fire_hour, fire_minute = fire.hour, fire.minute

    say(f"# E3a 真机冒烟（{now:%Y-%m-%d %H:%M:%S}）")
    say()
    say(f"- 临时数据目录：`{tmp}`（分片硬链接自 `data/`，清单真复制）")
    say(f"- 注入的脏数据：删掉 `bars/{VICTIM}`")
    say(f"- 起 uvicorn :{port}，体检 cron 定在 **{fire_hour:02d}:{fire_minute:02d}**（下一分钟，等它自己跑）")
    say()

    stage_data_dir(tmp)
    inject_dirt(tmp)

    env = {
        **os.environ,
        "DATA_DIR": str(tmp),
        "ETL_ENABLED": "true",
        "ETL_CATCHUP_ON_START": "false",  # 冒烟不拉真数据
        "ETL_HOUR": "3",
        "ETL_MINUTE": "0",
        "BARS_REFRESH_ENABLED": "false",
        "DATA_HEALTH_ENABLED": "true",
        "DATA_HEALTH_HOUR": str(fire_hour),
        "DATA_HEALTH_MINUTE": str(fire_minute),
        "DATA_HEALTH_CATCHUP_ON_START": "false",
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
        if not wait_for(lambda: _up(base), timeout=120):
            failures.append("服务没起来")
            raise _Bail
        say(f"   （服务就绪，冷启动 {time.monotonic() - started:.0f}s）")

        # ── 门（真 HTTP）：未登录 401 ──────────────────────────────────
        status, _, _ = http("GET", f"{base}/api/v1/data/health")
        say(f"1. 未登录打看板：HTTP {status}（断言 401）")
        if status != 401:
            failures.append(f"未登录应 401，实得 {status}")

        # ── 等定时 job 自己跑 ─────────────────────────────────────────
        say(f"2. 等 {fire_hour:02d}:{fire_minute:02d} 的定时体检跑到落档（最多 240s）…")
        runs_path = tmp / "_meta" / "health_runs.jsonl"
        row = wait_for(lambda: _first_row(runs_path), timeout=240)
        if row is None:
            failures.append("到点后没有落档记录")
            say(f"   （临时目录保留待查：{tmp}）")
            keep = True
            raise _Bail
        say(f"   落档：{json.dumps({k: v for k, v in row.items() if k != 'checks'}, ensure_ascii=False)}")
        bad = [c for c in row.get("checks", []) if c["level"] == "error"]
        say(f"   error 项：{[(c['id'], c['title']) for c in bad] or '（无）'}")
        if row.get("error", 0) < 1:
            failures.append("注入脏数据后 error 仍为 0——告警没触发")

        # ── 探针 admin 账号 → 看板红灯 ────────────────────────────────
        status, _, _ = http(
            "POST", f"{base}/api/v1/auth/register", body={"email": PROBE_EMAIL, "password": PROBE_PASSWORD}
        )
        say(f"3. 注册探针账号：HTTP {status}")
        set_probe_admin()
        status, _, headers = http(
            "POST", f"{base}/api/v1/auth/login", body={"email": PROBE_EMAIL, "password": PROBE_PASSWORD}
        )
        cookie = (headers.get("set-cookie") or "").split(";")[0]
        say(f"   登录：HTTP {status}，拿到会话 cookie：{'是' if cookie else '否'}")
        if not cookie:
            failures.append("登录没拿到 cookie")

        status, body, _ = http("GET", f"{base}/api/v1/data/health", cookie=cookie)
        last = body.get("last") or {}
        say(
            f"4. 看板 `GET /api/v1/data/health`：HTTP {status}；"
            f"check_status={last.get('check_status')!r}；error={last.get('error')}、"
            f"warn={last.get('warn')}、info={last.get('info')}"
        )
        say(f"   stale_source={json.dumps(body.get('stale_source'), ensure_ascii=False)}")
        if status != 200 or last.get("check_status") != "red":
            failures.append(f"看板没报红：{status} / {last.get('check_status')!r}")

        status, history, _ = http("GET", f"{base}/api/v1/data/health/history", cookie=cookie)
        newest = history.get("runs", [{}])[0].get("check_status")
        say(f"5. 台账接口：HTTP {status}，共 {history.get('total')} 轮，最新一轮 status={newest!r}")
        if status != 200 or history.get("total", 0) < 1:
            failures.append("台账接口没有读数")
    except _Bail:
        pass  # 已经记过失败原因，收尾要走同一条路（日志句柄必须先关，_finish 才读得到）
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
        handle.close()  # 必须先关：_finish 要读这个文件（不关读到的是没刷盘的半截）
        try:
            drop_probe()
        except Exception as exc:  # noqa: BLE001
            say(f"⚠ 探针账号清理失败（请手工删 {PROBE_EMAIL}）：{exc}")
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)
    return _finish(failures, log_path)


def _up(base: str) -> bool:
    try:
        status, _, _ = http("GET", f"{base}/health")
        return status == 200
    except OSError:
        return False


def _first_row(path: Path) -> dict | None:
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            return json.loads(line)
    return None


def _finish(failures: list[str], log_path: Path) -> int:
    say()
    if failures:
        say("## 结论：**未通过**")
        for item in failures:
            say(f"- {item}")
        if log_path.exists():
            tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
            say("\n服务端日志尾部：\n```\n" + "\n".join(tail) + "\n```")
    else:
        say("## 结论：**五条全过**（401 → 定时落档 → error>0 → 看板红 → 台账可读）")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out = LOG_DIR / "health-smoke.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n证据写入 {out.relative_to(REPO_ROOT)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
