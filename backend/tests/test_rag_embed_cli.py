"""嵌入 CLI 薄壳的用例（离线：只测壳，不连 Qdrant、不加载模型）。

薄壳的价值全在**闸门与退出码**上——逻辑在 `app/rag/embed.py`，那里有完整用例。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "embed_events.py"
_spec = importlib.util.spec_from_file_location("embed_events", SCRIPT)
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


def test_full_without_yes_is_refused_before_touching_anything(capsys) -> None:
    """破坏性操作要在**连接与模型加载之前**被拦下——误敲一次不该有任何副作用。"""
    code = cli.main(["--full"])
    assert code == 1
    assert "--yes" in capsys.readouterr().err


def test_rebuild_day_requires_no_extra_flag(tmp_path: Path, monkeypatch) -> None:
    """`--rebuild-day` 不该被 --full 的闸门误伤（它是单日重嵌，不是删库）。"""
    called: dict[str, object] = {}

    class FakeClient:
        def collection_exists(self, name: str) -> bool:
            return True

    monkeypatch.setattr(cli.col, "get_client", lambda: FakeClient())
    monkeypatch.setattr(cli.embed, "load_manifest", lambda base: {"2026-08-17": {"rows": 1}})
    monkeypatch.setattr(
        cli.embed, "sync", lambda **kwargs: called.update(kwargs) or _empty_report()
    )
    monkeypatch.setattr(cli.embed, "status", lambda **kwargs: _clean_status())

    code = cli.main(["--data-dir", str(tmp_path), "--rebuild-day", "2026-08-17"])
    assert code == 0
    assert called["only"] == ["2026-08-17"]
    assert called["force"] is True
    assert called["recreate"] is False


def _empty_report():
    from app.rag.embed import SyncReport

    return SyncReport(embedded=(), skipped=(), points=0, seconds=0.0)


def _clean_status():
    from app.rag.embed import StatusReport

    return StatusReport(missing={}, stale={}, extra={}, ready_days=1)


@pytest.mark.parametrize("extra", [[], ["--json", "/tmp/rag_status.json"]])
def test_status_mode_exits_zero_when_clean(extra: list[str], tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cli.col, "get_client", lambda: object())
    monkeypatch.setattr(cli.embed, "load_manifest", lambda base: {"2026-08-17": {"rows": 1}})
    monkeypatch.setattr(cli.embed, "status", lambda **kwargs: _clean_status())
    assert cli.main(["--data-dir", str(tmp_path), "--status", *extra]) == 0


def test_status_mode_exits_one_when_not_clean(tmp_path: Path, monkeypatch) -> None:
    """对账不干净必须退 1——退出码是这条命令进 CI/脚本的唯一信号。"""
    from app.rag.embed import StatusReport

    monkeypatch.setattr(cli.col, "get_client", lambda: object())
    monkeypatch.setattr(cli.embed, "load_manifest", lambda base: {"2026-08-17": {"rows": 1}})
    monkeypatch.setattr(
        cli.embed,
        "status",
        lambda **kwargs: StatusReport(missing={"2026-08-17": 1}, stale={}, extra={}, ready_days=0),
    )
    assert cli.main(["--data-dir", str(tmp_path), "--status"]) == 1
