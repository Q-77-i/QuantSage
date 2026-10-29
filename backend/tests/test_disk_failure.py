"""S6：数据目录只读时，回滚要明确 503，而不是 500 或半截写入。

真实故障（磁盘满 / 只读）在 API 层落到 `OSError`；`rollback.execute` 的
staging + `os.replace` 保证不会把目标目录写成半截，但端点必须把这类
「环境不可写」翻成 503（可重试/可运维），而不是内部错误 500。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.data import snapshots
from app.main import app


@pytest.fixture
def data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _chmod_tree(root: Path, *, writable: bool) -> None:
    mode = 0o755 if writable else 0o555
    file_mode = 0o644 if writable else 0o444
    for path in (root, *root.rglob("*")):
        path.chmod(mode if path.is_dir() else file_mode)


def test_rollback_on_readonly_data_dir_returns_503(
    signed_in_admin: Any, data_dir: Path
) -> None:
    from tests.test_rollback import write_bars

    write_bars(data_dir, 1)
    ts = snapshots.take(data_dir, scope="bars-refresh", reason="只读演练")["ts"]
    bars = data_dir / "bars" / "cn-daily_CN_qfq_2026.parquet"
    before = bars.read_bytes()

    _chmod_tree(data_dir, writable=False)
    try:
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/api/v1/data/rollback",
            json={"snapshot": ts, "confirm": ts, "channels": ["bars"]},
        )
    finally:
        _chmod_tree(data_dir, writable=True)

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "不可写" in detail or "只读" in detail
    assert not (data_dir / "bars" / "_staging").exists()
    assert bars.read_bytes() == before
