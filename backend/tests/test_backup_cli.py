"""恢复门槛（P0）：`--force` 只解除存在性保护，确认与线上目标门槛必须独立。

`scripts/backup.py` 不是包内模块；用 importlib 从路径加载，避免为它造一个包。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_backup() -> ModuleType:
    spec = importlib.util.spec_from_file_location("qs_backup", ROOT / "scripts" / "backup.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def backup() -> ModuleType:
    return _load_backup()


@pytest.mark.parametrize(
    ("force", "yes", "allow_live", "is_tty", "live_target", "needs_prompt", "error_part"),
    [
        (False, False, False, False, False, False, None),
        (True, True, False, False, False, False, None),
        (True, False, False, True, False, True, None),
        (True, False, False, False, False, False, "--yes"),
        (True, True, False, True, True, False, "--allow-live"),
        (True, True, True, False, True, False, None),
        # 线上目标即使 TTY 也不提示确认，直接拒绝缺 --allow-live
        (True, False, False, True, True, False, "--allow-live"),
    ],
)
def test_restore_gate(
    backup: ModuleType,
    force: bool,
    yes: bool,
    allow_live: bool,
    is_tty: bool,
    live_target: bool,
    needs_prompt: bool,
    error_part: str | None,
) -> None:
    prompt, error = backup.restore_gate(
        force=force,
        yes=yes,
        allow_live=allow_live,
        is_tty=is_tty,
        live_target=live_target,
    )
    assert prompt is needs_prompt
    if error_part is None:
        assert error is None
    else:
        assert error is not None and error_part in error


def _manifest(target: Path) -> None:
    (target / "manifests").mkdir(parents=True)
    (target / "manifests" / "20260101.json").write_text(
        '{"partial": false, "pg": {"quantsage": {"file": "pg/x.dump"}}}',
        encoding="utf-8",
    )


def test_restore_dry_run_skips_confirmation(
    backup: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _manifest(tmp_path)
    called: list[str] = []
    monkeypatch.setattr(backup, "restore_pg", lambda *a, **k: called.append("pg") or {})
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    rc = backup.main(
        [
            "restore",
            "--source",
            "20260101",
            "--target",
            str(tmp_path),
            "--pg-db",
            "quantsage",
            "--force",
            "--dry-run",
        ]
    )

    assert rc == 0
    assert called == []


def test_restore_non_tty_requires_yes_before_touching_anything(
    backup: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _manifest(tmp_path)
    called: list[str] = []
    monkeypatch.setattr(backup, "restore_pg", lambda *a, **k: called.append("pg") or {})
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    rc = backup.main(
        [
            "restore",
            "--source",
            "20260101",
            "--target",
            str(tmp_path),
            "--pg-db",
            "scratch_db",
            "--force",
        ]
    )

    assert rc == 2
    assert called == []


def test_restore_live_target_requires_allow_live(
    backup: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _manifest(tmp_path)
    called: list[str] = []
    monkeypatch.setattr(backup, "restore_pg", lambda *a, **k: called.append("pg") or {})
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    rc = backup.main(
        [
            "restore",
            "--source",
            "20260101",
            "--target",
            str(tmp_path),
            "--pg-db",
            "quantsage",
            "--force",
            "--yes",
        ]
    )

    assert rc == 2
    assert called == []


def test_restore_yes_and_allow_live_proceed(
    backup: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _manifest(tmp_path)
    called: list[str] = []
    monkeypatch.setattr(backup, "restore_pg", lambda *a, **k: called.append("pg") or {"db": "quantsage"})
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    rc = backup.main(
        [
            "restore",
            "--source",
            "20260101",
            "--target",
            str(tmp_path),
            "--pg-db",
            "quantsage",
            "--force",
            "--yes",
            "--allow-live",
        ]
    )

    assert rc == 0
    assert called == ["pg"]
