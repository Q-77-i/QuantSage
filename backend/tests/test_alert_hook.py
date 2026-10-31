"""P4-S8：Alertmanager 本地接收端（`scripts/alert_hook.py`）的落盘逻辑。

不启 HTTP、不碰 osascript：`handle_payload` 是纯逻辑，通知函数可注入。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "alert_hook", REPO_ROOT / "scripts" / "alert_hook.py"
)
assert _SPEC is not None and _SPEC.loader is not None
alert_hook = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(alert_hook)


def test_payload_is_written_as_jsonl(tmp_path: Path) -> None:
    log = tmp_path / "alerts.log"
    payload = {
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "BackupStale"},
                "annotations": {"summary": "备份超时"},
                "startsAt": "2026-10-30T00:00:00Z",
            },
            {"status": "resolved", "labels": {"alertname": "BackupStale"}, "annotations": {}},
        ]
    }

    assert alert_hook.handle_payload(payload, log, notify=False) == 2
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [row["status"] for row in rows] == ["firing", "resolved"]
    assert rows[0]["labels"]["alertname"] == "BackupStale"
    assert rows[0]["annotations"]["summary"] == "备份超时"


def test_only_firing_alerts_notify(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    payload = {
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "A"},
                "annotations": {"summary": "x"},
            },
            {"status": "resolved", "labels": {"alertname": "A"}, "annotations": {}},
        ]
    }

    alert_hook.handle_payload(
        payload,
        tmp_path / "a.log",
        notify=True,
        notify_fn=lambda title, message: calls.append((title, message)),
    )

    assert len(calls) == 1
    assert calls[0][0] == "QuantSage 告警"
    assert calls[0][1].startswith("[firing] A")


def test_non_list_alerts_are_ignored(tmp_path: Path) -> None:
    log = tmp_path / "a.log"
    assert alert_hook.handle_payload({"alerts": "nope"}, log, notify=False) == 0
    assert not log.exists()


def test_jsonl_rotates_at_the_size_limit(tmp_path: Path) -> None:
    """P2：告警日志不再无限追加；超过阈值滚动一份 .1。"""
    log = tmp_path / "alerts.log"
    payload = {
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "A"},
                "annotations": {"summary": "x"},
            }
        ]
    }

    alert_hook.handle_payload(payload, log, notify=False, max_bytes=200)
    alert_hook.handle_payload(payload, log, notify=False, max_bytes=200)

    assert (tmp_path / "alerts.log.1").exists()
    assert log.exists()
