"""Alertmanager 本地接收端（P4-S8）。

只绑 127.0.0.1:9099，收 Alertmanager 的 webhook，把每条 alert 写成
`logs/alerts.log` 的一行 JSON，并对 firing 发 macOS 通知。不接外部付费服务、
不写密钥；被测函数（`handle_payload` / `summarize`）是纯逻辑，供 pytest 直接调用。

启动（由 dev.sh 在 ALERT_HOOK_ENABLED=true 时一并起）：

    python3 scripts/alert_hook.py [--port 9099] [--no-notify]

**不做鉴权**：只监听回环，容器经 `host.docker.internal` 访问；这是本机演练姿态。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG = ROOT / "logs" / "alerts.log"
DEFAULT_PORT = 9099
#: 告警 JSONL 的轮转阈值（P2）：单文件 10MB、保留 3 份。
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUPS = 3

#: 参数走 argv，避免把告警文本拼进 AppleScript 源码（引号/反斜杠会坏）
_NOTIFY_SCRIPT = (
    "on run argv\n"
    "display notification (item 1 of argv) with title (item 2 of argv)\n"
    "end run"
)


def summarize(alert: dict[str, Any]) -> str:
    """一行可读摘要：`[firing] BackupStale 备份超过 26 小时未更新`。"""
    labels = alert.get("labels") or {}
    annotations = alert.get("annotations") or {}
    status = str(alert.get("status") or "firing")
    name = str(labels.get("alertname") or "unknown")
    summary = str(annotations.get("summary") or "")
    return f"[{status}] {name} {summary}".strip()


def _append_jsonl(
    log_path: Path, lines: list[str], *, max_bytes: int = LOG_MAX_BYTES
) -> None:
    """追加 JSONL；超过阈值先滚动 ``alerts.log.1..N``（P2，防无限追加）。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    incoming = sum(len(line.encode("utf-8")) + 1 for line in lines)
    try:
        size = log_path.stat().st_size
    except OSError:
        size = 0
    if size + incoming > max_bytes and log_path.exists():
        for index in range(LOG_BACKUPS - 1, 0, -1):
            source = log_path.with_name(f"{log_path.name}.{index}")
            if source.exists():
                source.replace(log_path.with_name(f"{log_path.name}.{index + 1}"))
        log_path.replace(log_path.with_name(f"{log_path.name}.1"))
    with log_path.open("a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")


def handle_payload(
    payload: dict[str, Any],
    log_path: Path,
    *,
    notify: bool = True,
    notify_fn: Any = None,
    max_bytes: int = LOG_MAX_BYTES,
) -> int:
    """把 Alertmanager payload 追加进 JSONL；返回处理的 alert 条数。

    `notify_fn` 可注入（测试里不碰 osascript）。只对 `firing` 发通知；
    `resolved` 只落盘——避免恢复时再弹一次。
    """
    alerts = payload.get("alerts") or []
    if not isinstance(alerts, list):
        return 0
    # dev.sh 用系统 python3（本机 3.9）起这个脚本，没有 datetime.UTC
    received_at = datetime.now(timezone.utc).isoformat()  # noqa: UP017
    lines: list[str] = []
    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        lines.append(
            json.dumps(
                {
                    "received_at": received_at,
                    "status": alert.get("status"),
                    "labels": alert.get("labels") or {},
                    "annotations": alert.get("annotations") or {},
                    "startsAt": alert.get("startsAt"),
                    "endsAt": alert.get("endsAt"),
                    "generatorURL": alert.get("generatorURL"),
                },
                ensure_ascii=False,
            )
        )
    if lines:
        _append_jsonl(log_path, lines, max_bytes=max_bytes)
    if notify:
        sender = notify_fn or _notify
        for alert in alerts:
            if isinstance(alert, dict) and alert.get("status") == "firing":
                sender("QuantSage 告警", summarize(alert))
    return len(lines)


def _notify(title: str, message: str) -> None:
    """macOS 通知；失败只让通知丢失，不影响落盘。"""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["osascript", "-e", _NOTIFY_SCRIPT, message, title],
            check=False,
            capture_output=True,
            timeout=5,
        )


class AlertHandler(BaseHTTPRequestHandler):
    """`POST /alerts` 收告警；`GET /health` 供 dev.sh 就绪探测。"""

    server_version = "QuantSageAlertHook/1.0"

    def _respond(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 —— BaseHTTPRequestHandler 的命名
        if self.path == "/health":
            self._respond(200, b'{"status":"ok"}')
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/alerts":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._respond(400, b'{"detail":"bad content-length"}')
            return
        raw = self.rfile.read(max(0, length))
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self._respond(400, b'{"detail":"bad json"}')
            return
        if not isinstance(payload, dict):
            self._respond(400, b'{"detail":"bad payload"}')
            return
        count = handle_payload(
            payload,
            self.server.log_path,  # type: ignore[attr-defined]
            notify=self.server.notify,  # type: ignore[attr-defined]
        )
        self._respond(200, json.dumps({"received": count}).encode())

    def log_message(self, fmt: str, *args: Any) -> None:
        """默认访问日志太吵；只把错误行留给 stderr。"""
        if args and str(args[1]).startswith(("4", "5")):
            super().log_message(fmt, *args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--no-notify", action="store_true")
    args = parser.parse_args(argv)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), AlertHandler)
    server.log_path = args.log  # type: ignore[attr-defined]
    server.notify = not args.no_notify  # type: ignore[attr-defined]
    print(f"alert hook listening on 127.0.0.1:{args.port} → {args.log}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
