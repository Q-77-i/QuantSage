"""M2d 离线单测：行情的两级门禁、预算、限流中止、互斥与零回归。

**全部走假 CLI**（`run_cli` 是注入点）：要验的是「判据与编排」，不是小石服务端。
真拉一次的验收在 `scripts/download_bars.py` 的实跑与 SPEC §3 M2d 的验收清单里。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.data import bars, data_lock, ledger, xiaoshi_cli
from app.etl import runner
from tests.conftest import BARS_SCHEMA

SESSIONS = date(2026, 10, 12)  # 周一，交易日
SATURDAY = date(2026, 10, 10)


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout, "")


def _fail(stderr: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 1, "", stderr)


def _write_shard(
    raw_dir: Path, year: int, adjust: str, *, last_day: str = "2026-10-09", marker: float = 1.0
) -> tuple[str, dict]:
    """写一片合成分片（列集与真实落盘一致），返回 (object_key, 回执条目)。"""
    target = raw_dir / "o" / f"{adjust}-{year}-{marker}" / "data.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "symbol": "600519",
            "adjustment": adjust,
            "trade_date": date.fromisoformat(last_day),
            "open": marker,
            "high": marker,
            "low": marker,
            "close": marker,
            "volume": 1e5,
            "is_suspended": False,
            "change_pct": None,
            "amount": None,
            "turnover_pct": None,
        }
    ]
    pq.write_table(pa.Table.from_pylist(rows, schema=BARS_SCHEMA), target)
    key = f"/data/cn-daily/market=CN/adjustment={adjust}/year={year}/data.parquet"
    entry = {
        "path": str(target),
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "size": target.stat().st_size,
    }
    return key, entry


class FakeCli:
    """假的小石 CLI：coverage 回清单、download 写分片并更新回执、verify 写证据。

    这三个动作正好是被测代码**真正依赖**的外部行为（不是 mock 掉被测逻辑）。
    """

    def __init__(
        self,
        raw_dir: Path,
        *,
        manifest: str = "pub-1",
        source_last: str = "2026-10-09",
        marker: float = 1.0,
        rate_limit_on_call: int | None = None,
        fail_on_call: int | None = None,
        timeout_on_call: int | None = None,
    ) -> None:
        self.raw_dir = raw_dir
        self.manifest = manifest
        self.source_last = source_last
        self.marker = marker
        self.rate_limit_on_call = rate_limit_on_call
        self.fail_on_call = fail_on_call
        self.timeout_on_call = timeout_on_call
        self.calls: list[list[str]] = []
        self.timeouts: list[int | None] = []

    # ── 断言用的读法 ──
    @property
    def downloads(self) -> list[list[str]]:
        return [call for call in self.calls if call[0] == "download"]

    def __call__(self, args: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        self.timeouts.append(timeout)
        command = args[0]
        if command == "coverage":
            return _ok(
                json.dumps(
                    {
                        "dataset": "cn-daily",
                        "manifest_version": self.manifest,
                        "quality_status": "passed",
                        "coverage": {"last": self.source_last, "files": 21},
                    },
                    ensure_ascii=False,
                )
            )
        if command == "download":
            index = len(self.downloads) - 1  # 本次调用是第几次（0 起）——注入点按它命中
            if self.rate_limit_on_call == index:
                return _fail("xiaoshi: rate_limited_no_retry HTTP 429 Retry-After=2621")
            if self.fail_on_call == index:
                return _fail("xiaoshi: network_error")
            if self.timeout_on_call == index:
                raise xiaoshi_cli.CliTimeout(f"CLI 调用超过 {timeout}s 未返回（已终止）")
            year = int(args[args.index("--year") + 1])
            adjust = args[args.index("--adjust") + 1]
            key, entry = _write_shard(self.raw_dir, year, adjust, marker=self.marker)
            receipt_path = self.raw_dir / bars.RECEIPT_NAME
            receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
            receipt.setdefault("files", {})[key] = entry
            receipt["manifest_version"] = self.manifest
            receipt_path.write_text(json.dumps(receipt))
            return _ok("复用对象并校验完成")
        if command == "verify":
            evidence = Path(args[args.index("--output") + 1])
            evidence.parent.mkdir(parents=True, exist_ok=True)
            evidence.write_text(
                json.dumps(
                    {"valid": True, "issues": [], "file_count": 21, "row_count": 21, "provenance_verified": True}
                )
            )
            return _ok("校验通过")
        raise AssertionError(f"未预料的 CLI 调用：{args}")


@pytest.fixture(autouse=True)
def _clean_lock() -> None:
    """每个用例前后都确认锁是空的——互斥用例会手动占锁。"""
    if data_lock.LOCK.locked():
        data_lock.LOCK.release()
    yield
    if data_lock.LOCK.locked():
        data_lock.LOCK.release()


def _prime_shards(tmp_path: Path, marker: float = 1.0) -> FakeCli:
    """先跑一轮把 21 片与指纹落定（后续用例以此为「已同步」的起点）。"""
    cli = FakeCli(tmp_path / "raw", marker=marker)
    report = bars.refresh(data_dir=tmp_path, run_cli=cli, today=SESSIONS)
    assert report.status == "updated"
    return cli


# ── 判据（纯函数）────────────────────────────────────────────


def test_decide_prefers_the_cheapest_gate_first() -> None:
    """顺序即代价顺序：本地（零调用）→ 发布标识（一次调用）→ 下载。"""
    covered = dict(local_last=date(2026, 10, 9), expected=date(2026, 10, 9))
    assert bars.decide(**covered, synced_manifest="a", current_manifest="b") == "skipped_no_gap"
    assert (
        bars.decide(local_last=date(2026, 9, 30), expected=SESSIONS, synced_manifest="a", current_manifest="a")
        == "up_to_date"
    )
    assert (
        bars.decide(local_last=date(2026, 9, 30), expected=SESSIONS, synced_manifest="a", current_manifest="b")
        == "refresh"
    )
    # 日历越界（expected=None）不拦刷新：这颗炸弹不该由行情刷新引爆
    assert (
        bars.decide(local_last=date(2026, 9, 30), expected=None, synced_manifest="a", current_manifest="a")
        == "up_to_date"
    )
    assert (
        bars.decide(local_last=date(2026, 9, 30), expected=None, synced_manifest="a", current_manifest="b")
        == "refresh"
    )
    # 本地一片都没有时不能早退
    assert (
        bars.decide(local_last=None, expected=SESSIONS, synced_manifest="a", current_manifest="a")
        == "up_to_date"
    )
    assert (
        bars.decide(local_last=date(2026, 10, 9), expected=date(2026, 10, 9), synced_manifest="a", current_manifest="a", force=True)
        == "refresh"
    )


def test_expected_session_falls_back_when_calendar_is_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    """日历到期返回 None 而不是抛错——刷新不该被日历拖垮。"""
    from app.data import calendar as cal

    def boom(_day: date) -> date:
        raise cal.CalendarOutOfRange("越界")

    monkeypatch.setattr(cal, "last_session_on_or_before", boom)
    assert bars.expected_session(date(2027, 1, 5)) is None


# ── 门禁 ────────────────────────────────────────────────────


def test_local_early_exit_makes_zero_api_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """本地已覆盖最近交易日（周末、长假）⇒ 一次 API 调用都不发。"""
    cli = FakeCli(tmp_path / "raw")
    monkeypatch.setattr(bars, "local_last_trade_date", lambda data_dir=None: date(2026, 10, 9))

    report = bars.refresh(data_dir=tmp_path, run_cli=cli, today=SATURDAY)

    assert report.status == "skipped_no_gap"
    assert report.download_calls == 0 and cli.calls == []
    assert report.expected_session == "2026-10-09"


def test_manifest_unchanged_makes_one_probe_and_no_download(tmp_path: Path) -> None:
    """发布标识没变 ⇒ 只花一次 coverage，21 次下载会话省掉。"""
    _prime_shards(tmp_path)
    cli = FakeCli(tmp_path / "raw", manifest="pub-1")
    before = {p.name: p.stat().st_mtime_ns for p in (tmp_path / "bars").glob("*.parquet")}

    report = bars.refresh(data_dir=tmp_path, run_cli=cli, today=SESSIONS)

    assert report.status == "up_to_date"
    assert report.download_calls == 0
    assert [call[0] for call in cli.calls] == ["coverage"]
    assert {p.name: p.stat().st_mtime_ns for p in (tmp_path / "bars").glob("*.parquet")} == before


def test_new_publication_with_identical_shards_does_not_touch_bars(tmp_path: Path) -> None:
    """**只比交易日会漏掉的那一类**：源侧换了发布但数据没变 ⇒ 21 次会话、零写入。"""
    _prime_shards(tmp_path)
    cli = FakeCli(tmp_path / "raw", manifest="pub-2")  # 新发布标识，分片内容一模一样
    before = {p.name: p.stat().st_mtime_ns for p in (tmp_path / "bars").glob("*.parquet")}
    fingerprint = json.loads((tmp_path / "_meta" / "bars.json").read_text())["fingerprint"]["shards"]

    report = bars.refresh(data_dir=tmp_path, run_cli=cli, today=SESSIONS)

    assert report.status == "up_to_date"
    assert len(cli.downloads) == 21 and report.changed_shards == []
    assert {p.name: p.stat().st_mtime_ns for p in (tmp_path / "bars").glob("*.parquet")} == before
    assert json.loads((tmp_path / "_meta" / "bars.json").read_text())["fingerprint"]["shards"] == fingerprint
    # 标识要记下：不记的话明天还会再跑 21 次会话
    assert bars.read_sync_state(tmp_path / "_meta").manifest_version == "pub-2"


def test_changed_shards_are_materialized_and_recorded(tmp_path: Path) -> None:
    """源侧真换了数据：整片换入 + 指纹更新（原子换入，不留暂存目录）。"""
    _prime_shards(tmp_path)
    cli = FakeCli(tmp_path / "raw", manifest="pub-2", marker=2.0)

    report = bars.refresh(data_dir=tmp_path, run_cli=cli, today=SESSIONS)

    assert report.status == "updated"
    assert len(report.changed_shards) == 21
    meta = json.loads((tmp_path / "_meta" / "bars.json").read_text())
    assert len(meta["fingerprint"]["shards"]) == 21
    # 指纹里那 21 个 sha 必须都能在回执（= 盘上分片）里找到：清单不是自说自话
    receipt = json.loads((tmp_path / "raw" / bars.RECEIPT_NAME).read_text())
    landed = {entry["sha256"] for entry in receipt["files"].values()}
    assert {item["sha256"] for item in meta["fingerprint"]["shards"]} <= landed
    assert not (tmp_path / "bars" / "_staging").exists()
    assert len(list((tmp_path / "bars").glob("*.parquet"))) == 21


def test_missing_fingerprint_counts_everything_as_changed(tmp_path: Path) -> None:
    """清单缺失时按「全变」处理：宁多重物化，也不静默漏一批修订。"""
    for year, adjust in bars.PARTITIONS:
        key, entry = _write_shard(tmp_path / "raw", year, adjust)
        receipt = tmp_path / "raw" / bars.RECEIPT_NAME
        data = json.loads(receipt.read_text()) if receipt.exists() else {}
        data.setdefault("files", {})[key] = entry
        receipt.write_text(json.dumps(data))

    assert len(bars.changed_shards(tmp_path / "raw", tmp_path / "_meta")) == 21


# ── 限流、预算、互斥 ─────────────────────────────────────────


def test_rate_limit_aborts_the_round_without_retry(tmp_path: Path) -> None:
    """429 是停止信号：立刻中止、不留重试痕迹，把 Retry-After 写进台账。"""
    cli = FakeCli(tmp_path / "raw", rate_limit_on_call=2)

    report = bars.refresh(data_dir=tmp_path, run_cli=cli, today=SESSIONS)

    assert report.status == "rate_limited"
    assert report.retry_after_seconds == 2621
    assert len(cli.downloads) == 3  # 第 3 次撞上限流即停，没有第 4 次
    # 台账要如实记「已经发出几次会话」——失败路径若记 0，运维会以为一次都没试
    assert report.download_calls == 3
    assert "不重试" in report.note
    last = ledger.last(tmp_path / "_meta", scope=ledger.BARS_SCOPE)
    assert last is not None and last["status"] == "rate_limited" and last["retry_after_seconds"] == 2621
    # 失败的轮次不能把发布标识记成「已同步」，否则次日以为追平了
    assert bars.read_sync_state(tmp_path / "_meta").manifest_version == ""


def test_budget_exhaustion_reports_incomplete_and_keeps_progress(tmp_path: Path) -> None:
    """整轮预算用尽：如实记「已下几片」，已下的分片留盘上（次日按 sha 复用即续）。"""
    cli = FakeCli(tmp_path / "raw")

    report = bars.refresh(data_dir=tmp_path, run_cli=cli, today=SESSIONS, budget_seconds=0)

    assert report.status == "incomplete"
    assert report.download_calls == 0
    assert "预算" in report.error


def test_each_call_gets_a_bounded_timeout(tmp_path: Path) -> None:
    """每次**下载**调用都要带上限（`min(单次上限, 剩余预算)`）——没有上限正是 10-12 卡 20 分钟的原因。"""
    cli = FakeCli(tmp_path / "raw", timeout_on_call=1)

    report = bars.refresh(data_dir=tmp_path, run_cli=cli, today=SESSIONS, call_timeout=60, budget_seconds=300)

    assert report.status == "incomplete"
    first_download = next(i for i, call in enumerate(cli.calls) if call[0] == "download")
    assert cli.timeouts[first_download] == 60  # 单次上限生效（探测那次用的是默认 300）
    assert report.download_calls == 2  # 被杀的那次也算一次真实会话
    assert "重跑即续" in report.note


def test_cli_timeout_really_kills_the_child_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """超时必须**杀掉子进程**，不是「等它跑完再说」——用真进程验，不信注入点。"""
    monkeypatch.setattr(xiaoshi_cli, "cli_command", lambda: "/bin/sleep")

    started = time.monotonic()
    with pytest.raises(xiaoshi_cli.CliTimeout):
        xiaoshi_cli.run(["5"], timeout=1)
    elapsed = time.monotonic() - started

    assert elapsed < 3, f"超时后仍在等（{elapsed:.1f}s）——子进程没被杀掉"


def test_rows_are_not_requested_while_another_sync_holds_the_lock(tmp_path: Path) -> None:
    """两条通道共用一把锁：撞上就跳过并留痕，不排队等、不撞 data_directory_busy。"""
    cli = FakeCli(tmp_path / "raw")
    data_lock.LOCK.acquire()
    try:
        with pytest.raises(data_lock.Busy):
            bars.refresh(data_dir=tmp_path, run_cli=cli, today=SESSIONS)
    finally:
        data_lock.LOCK.release()
    assert cli.calls == []


def test_ledger_scopes_stay_apart(tmp_path: Path) -> None:
    """两条通道写同一份台账：语料那路的 `last_run` 不能被行情那路顶掉。"""
    _prime_shards(tmp_path)
    ledger.append(tmp_path / "_meta", {"scope": "daily", "started_at": "2026-10-12T21:10:00+08:00"})

    assert bars.last_run(tmp_path)["status"] == "updated"
    assert runner._last_ledger(runner.paths(tmp_path))["scope"] == "daily"


# ── 零回归：`--skip-download` 那条路 ─────────────────────────


def test_rebuild_uses_local_shards_without_any_download(tmp_path: Path) -> None:
    """本地重物化：零网络会话（只调 verify），产物与指纹与刷新路径一致。"""
    _prime_shards(tmp_path)
    for name in (tmp_path / "bars").glob("*.parquet"):
        name.unlink()  # 模拟产物受损，分片还在
    cli = FakeCli(tmp_path / "raw")

    outputs = bars.rebuild(tmp_path / "raw", tmp_path / "bars", tmp_path / "_meta", run_cli=cli)

    assert len(outputs) == 21
    assert [call[0] for call in cli.calls] == ["verify"]  # 一次下载会话都没有
    assert len(list((tmp_path / "bars").glob("*.parquet"))) == 21
    meta = json.loads((tmp_path / "_meta" / "bars.json").read_text())
    assert meta["schema"] == "quantsage.bars_meta/v2"
    assert len(meta["fingerprint"]["shards"]) == 21


async def test_bars_job_is_registered_only_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """调度接线本身也要验：开关关着就不该有这条 job（默认关是刻意的）。

    `ETL_ENABLED` 是总闸——它关着两个 job 都不起，这条如实钉住（SPEC §3 M2d「落点」）。
    """
    from app.etl import scheduler as sched

    def stub(**overrides: object) -> SimpleNamespace:
        base = dict(
            etl_enabled=True,
            etl_hour=21,
            etl_minute=10,
            etl_trailing_days=7,
            etl_catchup_on_start=False,
            bars_refresh_enabled=True,
            bars_refresh_hour=21,
            bars_refresh_minute=40,
            bars_refresh_budget_seconds=1200,
            bars_refresh_catchup_on_start=False,
        )
        return SimpleNamespace(**{**base, **overrides})

    monkeypatch.setattr(sched, "get_settings", lambda: stub())
    active = sched.start_scheduler()
    assert active is not None
    try:
        assert active.get_job(sched.BARS_JOB_ID) is not None
        assert active.get_job(sched.JOB_ID) is not None
    finally:
        active.shutdown(wait=False)

    monkeypatch.setattr(sched, "get_settings", lambda: stub(bars_refresh_enabled=False))
    off = sched.start_scheduler()
    assert off is not None
    try:
        assert off.get_job(sched.BARS_JOB_ID) is None
        assert off.get_job(sched.JOB_ID) is not None
    finally:
        off.shutdown(wait=False)


def test_local_last_trade_date_measures_the_parquet(tmp_path: Path) -> None:
    """本地末端是**测量**出来的（与体检 X5 同一条 SQL 的口径），不是读清单。"""
    bars_dir = tmp_path / "bars"
    bars_dir.mkdir(parents=True)
    rows = [
        {
            "symbol": "600519",
            "adjustment": "qfq",
            "trade_date": date(2026, 10, 9),
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "volume": 1e5,
            "is_suspended": False,
            "change_pct": None,
            "amount": None,
            "turnover_pct": None,
        }
    ]
    pq.write_table(pa.Table.from_pylist(rows, schema=BARS_SCHEMA), bars_dir / "cn-daily_CN_qfq_2026.parquet")

    assert bars.local_last_trade_date(tmp_path) == date(2026, 10, 9)
    # 只有 bars/ 也要量得出来（不要求 events/ 已回填）
    assert not (tmp_path / "events").exists()
