"""E3b 回滚的离线用例（SPEC §4.5）。

夹具**自建一份 sha 寻址的对象库**（`raw/o/<sha[:2]>/<sha>.parquet`），而不是复用
`test_bars_refresh.py` 的 `FakeCli`——那边刻意用「`{adjust}-{year}/data.parquet`」这种
**陌生布局**来证明「我们只跟回执走、不猜 CLI 的目录形状」（`archive.py:139` 的原则）。
回滚是**唯一破例**的地方（理由与被实测过的布局写在 `rollback.object_path` 的 docstring），
所以它的用例要按**真实布局**铺数据——用一个不是真实布局的夹具去测它，测的就不是它了。

**语料的还原**在集成用例里验（真清单 + 真对象，见 `tests/integration/`）：那边一行
`materialize_day` 要的列集有三十来个，在离线夹具里手搓一份只是为了让它看起来像。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.data import rollback, snapshots


def object_for(root: Path, content: bytes) -> str:
    """按**内容寻址**落一个对象，返回它的 sha（回滚就是按这个 sha 找回来）。"""
    sha = hashlib.sha256(content).hexdigest()
    target = root / "raw" / "o" / sha[:2] / f"{sha}.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return sha


def bars_bytes(marker: int) -> bytes:
    import io

    buffer = io.BytesIO()
    pq.write_table(pa.table({"close": [float(marker)]}), buffer)
    return buffer.getvalue()


def write_bars(root: Path, marker: int) -> str:
    """把「当前行情」写成第 marker 版：对象落库 + 分片落盘 + 清单声明它。"""
    content = bars_bytes(marker)
    sha = object_for(root, content)
    name = "cn-daily_CN_qfq_2026.parquet"
    (root / "bars").mkdir(parents=True, exist_ok=True)
    (root / "bars" / name).write_bytes(content)
    (root / "_meta").mkdir(parents=True, exist_ok=True)
    (root / "_meta" / "bars.json").write_text(
        json.dumps(
            {
                "schema": "quantsage.bars_meta/v2",
                "fingerprint": {
                    "shards": [{"object_key": "…/year=2026/data.parquet", "sha256": sha}],
                    "receipt_manifest_version": "history-public-v1-20261012T200508Z",
                },
                "outputs": [
                    {
                        "path": f"data/bars/{name}",
                        "sha256": sha,
                        "rows": 1,
                        "adjust": "qfq",
                        "year": 2026,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return sha


def write_events(root: Path, day: date, *, with_object: bool = True) -> str:
    """铺一天的语料清单 + 一个来源分片对象（`with_object=False` 用来造「缺片」）。"""
    content = b"fake-event-shard-" + day.isoformat().encode()
    sha = hashlib.sha256(content).hexdigest()
    if with_object:
        target = root / "raw" / "o" / sha[:2] / f"{sha}.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    (root / "_meta").mkdir(parents=True, exist_ok=True)
    (root / "_meta" / "events.json").write_text(
        json.dumps(
            {
                "schema": "quantsage.events_meta/v2",
                "days": [
                    {
                        "date": day.isoformat(),
                        "file": f"cn-events_{day.isoformat()}.parquet",
                        "sha256": "d" * 64,
                        "shards": [
                            {
                                "event_type": "news",
                                "object_key": f"…/date={day}/event_type=news/data.parquet",
                                "sha256": sha,
                                "size": len(content),
                                "rows": 1,
                            }
                        ],
                    }
                ],
                "fingerprint": {"digest": "c" * 64, "days": 1, "rows": 1},
            }
        ),
        encoding="utf-8",
    )
    return sha


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    write_bars(tmp_path, 1)
    return tmp_path


# ── 判据 ───────────────────────────────────────────────────


def test_plan_refuses_when_an_object_is_gone(data_dir: Path) -> None:
    """**缺片即整次拒绝**——一半旧一半新比不动更糟。"""
    ts = snapshots.take(data_dir, scope="bars-refresh", reason="一")["ts"]
    for shard in (data_dir / "bars").glob("*.parquet"):
        shard.unlink()
    for obj in (data_dir / "raw" / "o").rglob("*.parquet"):
        obj.unlink()

    with pytest.raises(rollback.SnapshotIncomplete) as excinfo:
        rollback.plan(data_dir, ts=ts)

    assert excinfo.value.channel == "bars"
    assert len(excinfo.value.missing) == 1


def test_plan_refuses_a_missing_event_shard(data_dir: Path) -> None:
    """语料那侧同一条判据（缺的是**来源分片**，不是日分区的产物）。"""
    write_events(data_dir, date(2026, 9, 1), with_object=False)
    ts = snapshots.take(data_dir, scope="daily", reason="一")["ts"]

    with pytest.raises(rollback.SnapshotIncomplete) as excinfo:
        rollback.plan(data_dir, ts=ts)

    assert excinfo.value.channel == "events"


def test_plan_rejects_an_unknown_snapshot(data_dir: Path) -> None:
    with pytest.raises(rollback.SnapshotUnknown):
        rollback.plan(data_dir, ts="20200101T000000Z")


def test_channels_select_what_gets_restored(data_dir: Path) -> None:
    write_events(data_dir, date(2026, 9, 1))
    ts = snapshots.take(data_dir, scope="daily", reason="一")["ts"]

    only_bars = rollback.plan(data_dir, ts=ts, channels=("bars",))
    assert only_bars.bars and not only_bars.events

    only_events = rollback.plan(data_dir, ts=ts, channels=("events",))
    assert only_events.events and not only_events.bars


def test_a_refused_plan_touches_nothing(data_dir: Path) -> None:
    """验证失败必须**一个字节都不动**——先验证后动手是这条护栏的全部意义。"""
    ts = snapshots.take(data_dir, scope="bars-refresh", reason="一")["ts"]
    write_bars(data_dir, 2)  # 盘上已经是第 2 版
    before = {p.name: p.read_bytes() for p in (data_dir / "bars").glob("*.parquet")}
    (data_dir / "raw" / "o").rename(data_dir / "raw" / "o-moved")  # 把对象库藏起来

    with pytest.raises(rollback.SnapshotIncomplete):
        rollback.execute(data_dir, ts=ts)

    assert {p.name: p.read_bytes() for p in (data_dir / "bars").glob("*.parquet")} == before
    assert json.loads((data_dir / "_meta" / "bars.json").read_text())["fingerprint"]["shards"][0][
        "sha256"
    ] == hashlib.sha256(bars_bytes(2)).hexdigest()  # 清单也还是第 2 版


# ── 还原 ───────────────────────────────────────────────────


def test_rollback_restores_the_bars_bytes_and_manifest(data_dir: Path) -> None:
    first = write_bars(data_dir, 1)
    ts = snapshots.take(data_dir, scope="bars-refresh", reason="第一版")["ts"]
    write_bars(data_dir, 2)  # 换了一版数据

    result = rollback.execute(data_dir, ts=ts)

    assert result["ok"] is True
    assert result["restored"]["bars"] == 1
    assert (data_dir / "bars" / "cn-daily_CN_qfq_2026.parquet").read_bytes() == bars_bytes(1)
    manifest = json.loads((data_dir / "_meta" / "bars.json").read_text())
    assert manifest["fingerprint"]["shards"][0]["sha256"] == first
    # 逐片 sha 与**磁盘实测**一致（清单是抄回来的，但内容必须真的对得上）
    on_disk = (data_dir / "bars" / "cn-daily_CN_qfq_2026.parquet").read_bytes()
    assert hashlib.sha256(on_disk).hexdigest() == first


def test_rollback_drops_shards_that_are_not_in_the_snapshot(data_dir: Path) -> None:
    """目标状态之外的分片要删掉——留着就是「快照里没有的数据也在查询里」。"""
    ts = snapshots.take(data_dir, scope="bars-refresh", reason="只有 2026")["ts"]
    extra = data_dir / "bars" / "cn-daily_CN_qfq_2025.parquet"
    extra.write_bytes(bars_bytes(9))

    rollback.execute(data_dir, ts=ts)

    assert not extra.exists()


def test_rollback_takes_a_retreat_snapshot_first(data_dir: Path) -> None:
    """回滚前自动留退路——它自己不可撤销，先给一个「刚才是什么样」。"""
    write_bars(data_dir, 1)
    ts = snapshots.take(data_dir, scope="bars-refresh", reason="第一版")["ts"]
    write_bars(data_dir, 2)

    rollback.execute(data_dir, ts=ts)

    rows = snapshots.read_index(data_dir)
    retreat = [row for row in rows if row["scope"] == "rollback"]
    assert len(retreat) == 1
    assert "自动留档" in retreat[0]["reason"]
    # 退路里存的是**回滚前**那一版（第 2 版），不是回滚目标那一版
    stored = snapshots.manifest(data_dir, retreat[0]["ts"], "bars")
    assert stored["fingerprint"]["shards"][0]["sha256"] == hashlib.sha256(bars_bytes(2)).hexdigest()


def test_rollback_reruns_health_and_records_it(data_dir: Path) -> None:
    """回滚后的状态当场可见（`source="rollback"`），不靠人记得再跑一次体检。"""
    ts = snapshots.take(data_dir, scope="bars-refresh", reason="一")["ts"]

    result = rollback.execute(data_dir, ts=ts)

    assert result["health"]["status"] in {"red", "yellow", "green"}
    from app.data import health_log

    _total, rows = health_log.read_runs(data_dir)
    assert rows[0]["source"] == "rollback"
    assert "error" in rows[0]


def test_rollback_is_refused_while_a_sync_is_running(data_dir: Path) -> None:
    """与 ETL / 行情刷新串行——撞上就拒，不排队（排队会在半路把数据换走）。"""
    from app.data.data_lock import LOCK

    ts = snapshots.take(data_dir, scope="bars-refresh", reason="一")["ts"]
    assert not LOCK.locked()
    LOCK.acquire()
    try:
        with pytest.raises(Exception, match="同步"):
            rollback.execute(data_dir, ts=ts)
    finally:
        LOCK.release()


def test_rollback_verifies_sha_while_copying(data_dir: Path) -> None:
    """文件名的声明不作数：对象内容被换掉（sha 不符）⇒ 拒绝，不把坏字节换进去。"""
    ts = snapshots.take(data_dir, scope="bars-refresh", reason="一")["ts"]
    (data_dir / "bars" / "cn-daily_CN_qfq_2026.parquet").unlink()
    for obj in (data_dir / "raw" / "o").rglob("*.parquet"):
        obj.write_bytes("被换过的内容".encode())  # 路径没变，内容变了

    with pytest.raises(rollback.SnapshotIncomplete) as excinfo:
        rollback.execute(data_dir, ts=ts)

    assert "sha 不符" in excinfo.value.channel
    assert not (data_dir / "bars" / "cn-daily_CN_qfq_2026.parquet").exists()
    assert not list((data_dir / "bars").glob("_staging/*"))
