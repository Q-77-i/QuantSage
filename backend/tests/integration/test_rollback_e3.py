"""E3b 回滚的真数据集成验收——**在真 `data/` 的一份临时副本上**跑完整一轮。

默认不收集；用 `uv run pytest -m integration tests/integration/test_rollback_e3.py` 触发。
先跑 `scripts/download_bars.py` 与 `scripts/download_events.py`。

**为什么非要用真数据**：离线用例验的是判据与拒绝（自建的 sha 寻址夹具），这里验的是
「**真清单里的东西真的还能按 sha 找回来**」——21 片行情的 `bars.json` 与全量语料的
`events.json` 都是真实形状，`materialize_day` 要的那三十来列只有真分片才齐。

⚠ **绝不拿真 `data/` 做破坏性实验**：副本里分片与对象用**硬链接**铺（零拷贝），
但 `_meta/` 必须**真复制**——回滚会往清单里写，硬链接会把原目录的清单一起改掉
（E3a 冒烟踩过同一个坑，两次都是「先写后改」的路径）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.core.config import REPO_ROOT
from app.data import rollback, snapshots

pytestmark = pytest.mark.integration

#: 副本里删掉的那片——回滚必须把它按 sha 变回来
VICTIM = "cn-daily_CN_qfq_2025.parquet"


@pytest.fixture
def copy_of_real_data(tmp_path: Path) -> Iterator[Path]:
    """真 `data/` 的临时副本：分片硬链接、清单真复制。真目录一个字节都不动。"""
    real = REPO_ROOT / "data"
    if not (real / "bars").is_dir() or not (real / "_meta" / "bars.json").is_file():
        pytest.skip(f"没有真实数据目录（{real}）——先跑 scripts/download_*.py")
    base = tmp_path / "data"
    for name in ("bars", "events", "naming"):
        if not (real / name).is_dir():
            continue
        (base / name).mkdir(parents=True)
        for item in (real / name).iterdir():
            if item.is_file():
                os.link(item, base / name / item.name)
    (base / "_meta").mkdir(parents=True)
    for item in (real / "_meta").iterdir():
        if item.is_file():
            shutil.copy2(item, base / "_meta" / item.name)
    for sub in (real / "raw").rglob("*"):
        if sub.is_dir():
            (base / "raw" / sub.relative_to(real / "raw")).mkdir(parents=True, exist_ok=True)
        elif sub.is_file():
            os.link(sub, base / "raw" / sub.relative_to(real / "raw"))
    yield base
    shutil.rmtree(base, ignore_errors=True)


def test_plan_verifies_every_object_in_the_real_snapshot(copy_of_real_data: Path) -> None:
    """先验证这一关在真清单上过得去：**21 片行情 + 全量语料逐日分片都在本地库里**。

    这一条是整段回滚能力的**前提**：清单里的 sha 若有一条对不上本地对象，回滚就整次拒绝。
    语料天数是数据增量，不写死；按清单自己的 `days` 对账。
    """
    row = snapshots.take(copy_of_real_data, scope="daily", reason="集成用例")
    events_manifest = json.loads((copy_of_real_data / "_meta" / "events.json").read_text())

    job = rollback.plan(copy_of_real_data, ts=row["ts"])

    assert len(job.bars) == 21
    assert len(job.events) == len(events_manifest["days"])
    assert all(source.is_file() for source, _ in job.bars)


def test_rollback_restores_both_channels_from_the_real_manifests(copy_of_real_data: Path) -> None:
    """打快照 → 改数据（删一片行情、删一天语料）→ 回滚 → **逐片 sha 与 fingerprint 回旧值**。"""
    base = copy_of_real_data
    row = snapshots.take(base, scope="daily", reason="回滚前")
    before = json.loads((base / "_meta" / "bars.json").read_text())
    events_before = json.loads((base / "_meta" / "events.json").read_text())

    (base / "bars" / VICTIM).unlink()  # 注入：一片行情没了
    last_day = events_before["days"][-1]["file"]
    (base / "events" / last_day).unlink()  # 注入：一天语料没了

    result = rollback.execute(base, ts=row["ts"])

    assert result["ok"] is True
    assert result["restored"]["bars"] == 21
    assert result["restored"]["events"] == len(events_before["days"])
    # ① 逐片 sha 与**磁盘实测**一致（不是拿清单自己比自己对）
    manifest = json.loads((base / "_meta" / "bars.json").read_text())
    assert manifest["fingerprint"]["shards"] == before["fingerprint"]["shards"]
    for output in manifest["outputs"]:
        on_disk = (base / "bars" / Path(output["path"]).name).read_bytes()
        assert hashlib.sha256(on_disk).hexdigest() == output["sha256"]
    # ② 语料清单也回到旧值，且少掉的那天真的回来了
    events_after = json.loads((base / "_meta" / "events.json").read_text())
    assert events_after["fingerprint"] == events_before["fingerprint"]
    assert events_after["days"] == events_before["days"]
    assert (base / "events" / last_day).is_file()
    # ③ 回滚后的体检当场落了档
    from app.data import health_log

    _total, runs = health_log.read_runs(base)
    assert runs[0]["source"] == "rollback"
    # ④ 真数据目录没被动过（副本是硬链接，删的是链接不是文件）
    assert (REPO_ROOT / "data" / "bars" / VICTIM).is_file()
