"""S6 故障演练（SPEC §7.2）——cache / PG / Qdrant / LLM / 磁盘只读。

与 `drill_reliability.py` 的分工：那边演「策略层会不会触发」，这边演
「真进程 + 真容器下，用户会看到什么」。每个子命令自断言，写
`logs/s6-drill/<时间戳>/report.md`；退出码 0 = 全过。

约定：
* 默认只动 dev/测试栈；`QS_MODE=prod` 或非回环 base URL 需 `--yes`。
* 会停/起 `redis` / `postgres` / `qdrant`，请避开 ETL（21:10–22:00）。
* 需要 `docker compose` 与 backend venv；不碰 `.env`、不回显密钥。

用法：
    cd backend
    .venv/bin/python scripts/drill_s6.py cache-down
    .venv/bin/python scripts/drill_s6.py pg-restart
    .venv/bin/python scripts/drill_s6.py qdrant-down
    .venv/bin/python scripts/drill_s6.py llm-429
    .venv/bin/python scripts/drill_s6.py llm-timeout
    .venv/bin/python scripts/drill_s6.py disk-readonly
    .venv/bin/python scripts/drill_s6.py all
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error, parse, request

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
PY = BACKEND / ".venv/bin/python"
DRILL_ROOT = ROOT / "logs" / "s6-drill"
DEFAULT_BASE = "http://127.0.0.1:8000"
PASSWORD = "drill-pass-1234"


class CheckFailure(AssertionError):
    """一条断言失败——停止当前子命令，报告里保留已通过的步骤。"""


def _env_file() -> dict[str, str]:
    values: dict[str, str] = {}
    path = ROOT / ".env"
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


ENV = _env_file()


def env(name: str, default: str = "") -> str:
    return os.environ.get(name) or ENV.get(name) or default


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["docker", "compose", *args], cwd=ROOT, check=False, capture_output=True, text=True
    )
    if check and proc.returncode != 0:
        raise CheckFailure(f"docker compose {' '.join(args)} 失败：{proc.stderr.strip()[:400]}")
    return proc


def wait_http(url: str, timeout: float = 60.0, *, expect: int = 200) -> float:
    started = time.monotonic()
    deadline = started + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            with request.urlopen(url, timeout=2) as response:
                if response.status == expect:
                    return time.monotonic() - started
                last = f"HTTP {response.status}"
        except error.HTTPError as exc:
            last = f"HTTP {exc.code}"
        except error.URLError as exc:
            last = type(exc.reason).__name__
        time.sleep(0.5)
    raise CheckFailure(f"{url} 在 {timeout:.0f}s 内未到 {expect}（最后：{last}）")


def pg_connect(db: str):
    import psycopg

    return psycopg.connect(
        host="127.0.0.1",
        port=int(env("POSTGRES_HOST_PORT", "5433")),
        user=env("POSTGRES_USER", "quantsage"),
        password=env("POSTGRES_PASSWORD", ""),
        dbname=db,
        autocommit=True,
    )


def create_db(name: str) -> None:
    compose("exec", "-T", "postgres", "createdb", "-U", env("POSTGRES_USER", "quantsage"), name)


def drop_db(name: str) -> None:
    compose(
        "exec", "-T", "postgres", "dropdb", "-U", env("POSTGRES_USER", "quantsage"),
        "--if-exists", name, check=False,
    )


class Report:
    def __init__(self, name: str) -> None:
        self.name = name
        self.started = datetime.now(UTC)
        self.checks: list[tuple[bool, str]] = []
        self.notes: list[str] = []
        self.out = DRILL_ROOT / self.started.strftime("%Y%m%d-%H%M%S-%f")
        self.out.mkdir(parents=True, exist_ok=True)

    def note(self, text: str) -> None:
        self.notes.append(text)
        print(f"  · {text}", flush=True)

    def check(self, condition: bool, text: str) -> None:
        self.checks.append((bool(condition), text))
        print(f"  {'✓' if condition else '✗'} {text}", flush=True)

    def require(self, condition: bool, text: str) -> None:
        self.check(condition, text)
        if not condition:
            raise CheckFailure(text)

    def write(self, ok: bool) -> Path:
        lines = [
            f"# S6 演练：{self.name}",
            "",
            f"- 开始：{self.started.isoformat(timespec='seconds')}",
            f"- 结果：{'✅ 通过' if ok else '❌ 失败'}",
            "",
            "## 断言",
            "",
            *[f"- {'✓' if passed else '✗'} {text}" for passed, text in self.checks],
            "",
            "## 记录",
            "",
            *[f"- {note}" for note in self.notes],
            "",
        ]
        path = self.out / "report.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path


class Session:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.jar = CookieJar()
        self.opener = request.build_opener(request.HTTPCookieProcessor(self.jar))
        self.email = ""

    def _open(self, method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 30):
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"} if data is not None else {}
        req = request.Request(f"{self.base}{path}", data=data, headers=headers, method=method)
        return self.opener.open(req, timeout=timeout)

    def json(self, method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 30):
        try:
            with self._open(method, path, body, timeout) as response:
                return response.status, json.loads(response.read() or b"null")
        except error.HTTPError as exc:
            return exc.code, {"detail": exc.read().decode(errors="replace")[:400]}

    def register_login(self, prefix: str) -> str:
        self.email = f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"
        status, _ = self.json("POST", "/api/v1/auth/register", {"email": self.email, "password": PASSWORD})
        if status not in (201, 409):
            raise CheckFailure(f"注册失败：{status}")
        status, body = self.json("POST", "/api/v1/auth/login", {"email": self.email, "password": PASSWORD})
        if status != 200:
            raise CheckFailure(f"登录失败：{status} {body}")
        return self.email

    def sse(self, path: str, body: dict[str, Any], timeout: float = 90.0) -> tuple[int, list[str]]:
        lines: list[str] = []
        try:
            with self._open("POST", path, body, timeout=timeout) as response:
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    raw = response.readline()
                    if not raw:
                        break
                    text = raw.decode(errors="replace").strip()
                    if text:
                        lines.append(text)
                    if text.startswith("event: done") or text.startswith("event: error"):
                        # error 后再读一个 data 行
                        if text.startswith("event: error"):
                            extra = response.readline().decode(errors="replace").strip()
                            if extra:
                                lines.append(extra)
                        break
                return response.status, lines
        except error.HTTPError as exc:
            return exc.code, [exc.read().decode(errors="replace")[:400]]
        except TimeoutError:
            return 200, lines


def _tail(path: Path, lines: int = 30) -> str:
    if not path.is_file():
        return "(无日志)"
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def start_api(
    port: int,
    *,
    extra: dict[str, str] | None = None,
    db: str | None = None,
    data_dir: Path | None = None,
    log_name: str = "api",
) -> tuple[subprocess.Popen[bytes], Path]:
    cfg = {
        "PYTHONPATH": str(BACKEND),
        "LANGFUSE_TRACING_ENABLED": "false",
        "ETL_ENABLED": "false",
        "BARS_REFRESH_ENABLED": "false",
        "DATA_HEALTH_ENABLED": "false",
        "MEMORY_SETTLE_ENABLED": "false",
        "RAG_WARMUP_ON_START": "false",
        "RAG_SERVICE_URL": "",
        "API_WORKERS": "1",
        "RATE_LIMIT_SHARED": "false",
        "STARTUP_REQUIRE_DB": "true",
        "LOG_FORMAT": "text",
        "RATE_LIMIT_CHAT": "100000",
        "RATE_LIMIT_RESEARCH": "100000",
        "QUOTA_ACTIVE_TASKS": "1000",
        "QUOTA_AGENT_CALLS_PER_DAY": "100000",
    }
    if db:
        cfg["POSTGRES_DB"] = db
        cfg["DATABASE_URL"] = ""
    if data_dir is not None:
        cfg["DATA_DIR"] = str(data_dir)
    cfg.update(extra or {})
    environment = {**os.environ, **cfg}
    if db:
        init = subprocess.run(
            [str(PY), str(BACKEND / "scripts" / "init_checkpoint_db.py")],
            cwd=BACKEND,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        if init.returncode != 0:
            raise CheckFailure(f"checkpointer 建表失败：{init.stdout[-300:]} {init.stderr[-300:]}")
    log = DRILL_ROOT / f"{log_name}.log"
    handle = log.open("ab")
    proc = subprocess.Popen(
        [
            str(PY), "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(port),
            "--timeout-graceful-shutdown", "5", "--log-level", "warning",
        ],
        cwd=BACKEND,
        env=environment,
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    handle.close()
    try:
        wait_http(f"http://127.0.0.1:{port}/health/ready", 90)
    except Exception:
        print(_tail(log, 60), file=sys.stderr)
        stop_api(proc)
        raise
    return proc, log


def stop_api(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, 15)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, 9)
        proc.wait(timeout=5)


def promote_admin(db: str, email: str) -> None:
    with pg_connect(db) as conn:
        conn.execute("UPDATE users SET role = 'admin' WHERE email = %s", (email,))


def delete_user(db: str, email: str) -> None:
    with pg_connect(db) as conn:
        conn.execute("DELETE FROM users WHERE email = %s", (email,))


def _bars(session: Session, count: int = 20, *, offset: int = 0) -> list[int]:
    """不同日期区间 = 不同缓存键；同一 URL 会命中 L1，测不到 Redis。"""
    statuses = []
    for index in range(count):
        n = offset + index
        year = 2024 + n // 12
        month = n % 12 + 1
        start = f"{year}-{month:02d}-01"
        end = f"{year}-{month:02d}-28"
        status, _ = session.json(
            "GET", f"/api/v1/market/000001/bars?start={start}&end={end}"
        )
        statuses.append(status)
    return statuses


# ── cache-down ─────────────────────────────────────────────────


def cache_down(report: Report) -> None:
    db = "quantsage_s6_cache"
    drop_db(db)
    create_db(db)
    proc = None
    session = None
    try:
        proc, log = start_api(8002, extra={"CACHE_ENABLED": "true"}, db=db, log_name="cache-api")
        session = Session("http://127.0.0.1:8002")
        session.register_login("cache")
        warm = _bars(session, 10, offset=0)
        report.require(all(status == 200 for status in warm), f"L2 预热 10 次全 200：{set(warm)}")
        compose("stop", "redis")
        report.note("已停 redis（cache）")
        # offset=12：换一批日期区间，绕开 L1，逼出 L2/Redis 路径
        statuses = _bars(session, 20, offset=12)
        report.require(all(status == 200 for status in statuses), f"停 Redis 后 20 次仍全 200：{set(statuses)}")
        time.sleep(1)
        text = _tail(log, 80)
        report.check(
            "数据缓存" in text or "熔断" in text or "redis-cache" in text,
            "日志出现数据缓存熔断/直读记录",
        )
    finally:
        compose("start", "redis", check=False)
        wait_http("http://127.0.0.1:8002/health/ready", 30) if proc and proc.poll() is None else None
        if proc:
            stop_api(proc)
        if session and session.email:
            with contextlib.suppress(Exception):
                delete_user(db, session.email)
        drop_db(db)


# ── pg-restart ─────────────────────────────────────────────────


def _health_status(base: str) -> int:
    try:
        with request.urlopen(f"{base}/health/ready", timeout=1) as response:
            return response.status
    except error.HTTPError as exc:
        return exc.code
    except error.URLError:
        return 0


def pg_restart(report: Report, base: str) -> None:
    session = Session(base)
    session.register_login("pg")
    task_ids: list[str] = []
    try:
        for _ in range(3):
            status, body = session.json(
                "POST",
                "/api/v1/backtest",
                {"strategy": "ma_cross", "symbol": "600519", "start": "2026-07-01", "end": "2026-09-30"},
            )
            if status == 202:
                task_ids.append(str(body.get("task_id") or body.get("id")))
        report.require(len(task_ids) == 3, f"重启前提交 3 个回测：{task_ids}")
        # 重启在后台线程跑，主线程才能采样到 503 窗口（restart 本身会阻塞到容器起来）
        done = threading.Event()

        def restart() -> None:
            try:
                compose("restart", "postgres")
            finally:
                done.set()

        threading.Thread(target=restart, daemon=True).start()
        report.note("已发出 postgres restart")
        down = 0
        recovered = False
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            status = _health_status(base)
            if status in (0, 503):
                down += 1
            elif status == 200 and down:
                recovered = True
                break
            time.sleep(0.2)
        done.wait(30)
        # /health/ready 是**启动态**探针（db_ready 只在 lifespan 置位），PG 重启期间可能仍 200；
        # 如实记录，恢复以真实 DB 端点 / 任务 / chat 为准。
        report.note(f"重启窗口 /health/ready 非 200 采样 {down} 次；探针只反映启动态")
        report.check(recovered or down == 0, "重启后 /health/ready 为 200（未观察到探针翻转）")
        wait_http(f"{base}/health/ready", 60)
        status, _ = session.json("GET", "/api/v1/auth/me")
        report.require(status == 200, "重启后 DB 端点 /auth/me 200（池已重连）")
        report.require(all(status == 200 for status in _bars(session, 3)), "重启后 bars 3 次全 200")
        chat_status, chat_lines = session.sse("/api/v1/chat", {"message": "你好"}, timeout=60)
        report.check(
            chat_status in (200, 503),
            f"重启后 chat 有明确响应：HTTP {chat_status}",
        )
        if chat_status == 200:
            report.check("event: error" not in "\n".join(chat_lines), "chat 流内无 error 帧")
        terminal = 0
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline and terminal < len(task_ids):
            terminal = 0
            for task_id in task_ids:
                status, body = session.json("GET", f"/api/v1/tasks/{task_id}")
                if status == 200 and body.get("status") in {"done", "failed", "cancelled"}:
                    terminal += 1
            if terminal < len(task_ids):
                time.sleep(2)
        report.require(terminal == len(task_ids), f"3 个任务均到终态：{terminal}/3")
        for task_id in task_ids:
            status, body = session.json("GET", f"/api/v1/tasks/{task_id}")
            report.check(status == 200 and body.get("id") == task_id, f"任务 {task_id} 只剩一条记录")
    finally:
        compose("start", "postgres", check=False)
        try:
            time.sleep(1)
            if session.email:
                delete_user(env("POSTGRES_DB", "quantsage"), session.email)
        except Exception:
            pass


# ── qdrant-down ────────────────────────────────────────────────


def qdrant_down(report: Report, base: str) -> None:
    session = Session(base)
    session.register_login("qdrant")
    message = "用本地事件库检索最近的业绩预告，并说明数据源。"
    try:
        compose("stop", "qdrant")
        report.note("已停 qdrant")
        status, lines = session.sse("/api/v1/chat", {"message": message}, timeout=90)
        text = "\n".join(lines)
        report.check(status == 200, f"停 Qdrant 后 chat 仍开流：HTTP {status}")
        report.check(
            "本地事件检索不可用" in text or "event: error" in text,
            "流里明确出现「本地事件检索不可用」或 error 帧",
        )
    finally:
        compose("start", "qdrant", check=False)
        wait_http(f"{base}/health/ready", 60)
        try:
            if session.email:
                delete_user(env("POSTGRES_DB", "quantsage"), session.email)
        except Exception:
            pass


# ── LLM 429 / 超时 ─────────────────────────────────────────────


class _FakeLLM(BaseHTTPRequestHandler):
    mode = "429"

    def log_message(self, *_args: Any) -> None:  # noqa: D102
        return

    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps({"object": "list", "data": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        if length:
            self.rfile.read(length)
        if not self.path.endswith("/chat/completions"):
            self.send_error(404)
            return
        if self.mode == "429":
            body = json.dumps({"error": {"message": "fake rate limit", "type": "rate_limit_error"}}).encode()
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", "1")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        time.sleep(120)
        self.send_error(504)


def _fake_llm(mode: str) -> tuple[ThreadingHTTPServer, threading.Thread]:
    _FakeLLM.mode = mode
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeLLM)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def llm_failure(report: Report, mode: str) -> None:
    db = "quantsage_s6_llm"
    drop_db(db)
    create_db(db)
    server, thread = _fake_llm(mode)
    port = server.server_address[1]
    proc = None
    session = None
    try:
        proc, _ = start_api(
            8004,
            extra={
                "DEEPSEEK_API_BASE": f"http://127.0.0.1:{port}/v1",
                "DEEPSEEK_API_KEY": "fake",
            },
            db=db,
            log_name=f"llm-{mode}",
        )
        session = Session("http://127.0.0.1:8004")
        session.register_login(f"llm-{mode}")
        started = time.monotonic()
        # 超时档：LLM 策略默认重试 1 次，两次 60s 后才会发 error 帧
        status, lines = session.sse(
            "/api/v1/chat", {"message": "只回一句话。"}, timeout=180 if mode == "timeout" else 90
        )
        elapsed = time.monotonic() - started
        text = "\n".join(lines)
        report.check(status == 200, f"chat 开流：HTTP {status}")
        expected = "rate_limited" if mode == "429" else "upstream_timeout"
        report.require(f'"code": "{expected}"' in text, f"收到 error 帧 {expected}")
        if mode == "timeout":
            report.check(elapsed < 150, f"超时在 2×60s 档内返回：{elapsed:.1f}s（含启停）")
        else:
            report.check(elapsed < 30, f"429 快速失败：{elapsed:.1f}s")
    finally:
        if proc:
            stop_api(proc)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        drop_db(db)


# ── 磁盘只读 ───────────────────────────────────────────────────


def _chmod_tree(root: Path, writable: bool) -> None:
    dir_mode = 0o755 if writable else 0o555
    file_mode = 0o644 if writable else 0o444
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        path.chmod(dir_mode if path.is_dir() else file_mode)


def _latest_snapshot(data_dir: Path) -> str | None:
    path = data_dir / "_snapshots" / "index.jsonl"
    if not path.is_file():
        return None
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows[-1].get("ts") if rows else None


def disk_readonly(report: Report) -> None:
    db = "quantsage_s6_disk"
    drop_db(db)
    create_db(db)
    data_dir = DRILL_ROOT / "disk-data"
    if data_dir.exists():
        _chmod_tree(data_dir, writable=True)
        shutil.rmtree(data_dir)
    subprocess.run(
        ["rsync", "-a", "--exclude=_tmp/", f"{ROOT / 'data'}/", f"{data_dir}/"],
        check=True,
    )
    ts = _latest_snapshot(data_dir)
    proc = None
    session = None
    try:
        report.require(ts is not None, f"临时 data 目录有快照：{ts}")
        proc, _ = start_api(8005, data_dir=data_dir, db=db, log_name="disk-api")
        session = Session("http://127.0.0.1:8005")
        email = session.register_login("disk")
        promote_admin(db, email)
        session.json("POST", "/api/v1/auth/login", {"email": email, "password": PASSWORD})
        bars = data_dir / "bars"
        before = {path.name: path.stat().st_size for path in bars.glob("*.parquet")}
        _chmod_tree(data_dir, writable=False)
        report.note("临时 data 目录已 chmod -R a-w")
        status, body = session.json(
            "POST",
            "/api/v1/data/rollback",
            {"snapshot": ts, "confirm": ts, "channels": ["bars"]},
        )
        report.require(status == 503, f"只读回滚返回 503（实际 {status}）")
        report.check("不可写" in str(body.get("detail", "")), f"提示明确：{body.get('detail')}")
        report.check(not (bars / "_staging").exists(), "没有遗留 _staging")
        after = {path.name: path.stat().st_size for path in bars.glob("*.parquet")}
        report.check(before == after, "bars 文件未被改动")
    finally:
        if data_dir.exists():
            _chmod_tree(data_dir, writable=True)
        if proc:
            stop_api(proc)
        try:
            if session and session.email:
                delete_user(db, session.email)
        except Exception:
            pass
        drop_db(db)
        shutil.rmtree(data_dir, ignore_errors=True)


# ── CLI ────────────────────────────────────────────────────────


DRILLS = {
    "cache-down": cache_down,
    "pg-restart": pg_restart,
    "qdrant-down": qdrant_down,
    "llm-429": lambda report: llm_failure(report, "429"),
    "llm-timeout": lambda report: llm_failure(report, "timeout"),
    "disk-readonly": disk_readonly,
}


def run_one(name: str, base: str) -> bool:
    report = Report(name)
    print(f"\n=== {name} ===", flush=True)
    ok = True
    try:
        if name in {"pg-restart", "qdrant-down"}:
            DRILLS[name](report, base)
        else:
            DRILLS[name](report)
    except CheckFailure as exc:
        ok = False
        report.note(f"停止：{exc}")
    except Exception as exc:  # noqa: BLE001 —— 演练要把意外也留成证据
        ok = False
        report.note(f"异常：{type(exc).__name__}: {exc}")
    path = report.write(ok and all(passed for passed, _ in report.checks))
    print(f"报告：{path}", flush=True)
    return ok and all(passed for passed, _ in report.checks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S6 故障演练（SPEC §7.2）")
    parser.add_argument("name", choices=[*DRILLS, "all"])
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--yes", action="store_true", help="允许对非 dev 栈执行")
    args = parser.parse_args(argv)
    parsed = parse.urlparse(args.base_url)
    if parsed.hostname not in {"127.0.0.1", "localhost"} and not args.yes:
        print("非回环 base URL；确认要动真实环境请加 --yes", file=sys.stderr)
        return 2
    if os.environ.get("QS_MODE") == "prod" and not args.yes:
        print("QS_MODE=prod；确认要动生产形态请加 --yes", file=sys.stderr)
        return 2
    names = list(DRILLS) if args.name == "all" else [args.name]
    results = {name: run_one(name, args.base_url) for name in names}
    print("\n=== 汇总 ===")
    for name, ok in results.items():
        print(f"{'✅' if ok else '❌'} {name}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
