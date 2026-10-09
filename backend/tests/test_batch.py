"""M5b 批处理执行器单测：展开与边界、并发上限、单格隔离、最优格判据。

判据来自 SPEC §12 M5b 增量。两条要点决定了本文件的写法：

* **并发上限的判据不是「快了多少」**——实测并发 2 只快 10~17%（DuckDB 默认吃满核心，
  见 SPEC §6 M5b 开头），故这里断言的是「**同时在跑的格数峰值 ≤2**」与「并发结果与串行
  逐格相等」，耗时只记录不设阈值；
* **单格失败必须被隔离**——一个坏格子不能带走整批，也不能只留一句「失败了事」。

数据一律走真实 Parquet（`tests/conftest.py` 的夹具），不 mock 查询层（沿用 P1 口径）。
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pytest

from app.backtest import batch as batch_module
from app.backtest.costs import CostModel
from app.backtest.report import build_report
from app.backtest.strategies import available_strategies, known_params, validate_params
from app.backtest.batch import (
    CONCURRENCY,
    MAX_CELLS,
    BatchRequestError,
    CellSpec,
    RunOptions,
    cartesian,
    grid_cells,
    pick_best,
    run_cells,
)
from app.backtest.engine import BacktestConfig
from tests.conftest import make_backtest_dir, trading_days

START = date(2026, 8, 3)
SYMBOL = "600519"
BARS = 60


def ma_cross_bars(count: int = BARS, symbol: str = SYMBOL) -> list[dict[str, object]]:
    """一段能真的交叉出信号的行情：前段上行、中段回落、尾段再上行。"""
    rows: list[dict[str, object]] = []
    price = 100.0
    for index, day in enumerate(trading_days(START, count)):
        drift = 0.6 if index < count // 3 else (-0.5 if index < 2 * count // 3 else 0.7)
        price = round(price + drift, 2)
        rows.append(
            {
                "trade_date": day,
                "open": price,
                "close": price,
                "high": price + 0.5,
                "low": price - 0.5,
            }
        )
    return rows


BAR_ROWS = ma_cross_bars()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return make_backtest_dir(tmp_path, BAR_ROWS, symbol=SYMBOL)


def options(data_dir: Path, **overrides) -> RunOptions:
    base = dict(
        costs=CostModel(),
        pit_mode=batch_module.Mode.PIT,
        data_dir=data_dir,
        adjust="qfq",
    )
    base.update(overrides)
    return RunOptions(**base)


def ma_normalize(params):
    """内置策略的归一器：只报错、**不代填缺省值**（缺省由引擎的 `from_params` 补）。"""
    return dict(params), validate_params("ma_cross", params)


MA_KNOWN = known_params("ma_cross")


# ── 展开与边界（纯函数，负样例逐条）────────────────────────────


def test_cartesian_keeps_the_written_axis_order() -> None:
    """第一轴最慢、最后一轴最快——格的下标要能反推参数（客户端点热力图靠它对回去）。"""
    combos = cartesian([("fast", [1, 2]), ("slow", [10, 20])])
    assert combos == [
        {"fast": 1, "slow": 10},
        {"fast": 1, "slow": 20},
        {"fast": 2, "slow": 10},
        {"fast": 2, "slow": 20},
    ]


def test_grid_cells_expands_two_axes_and_merges_the_base() -> None:
    cells = grid_cells(
        base={"slow": 20},
        axes=[("fast", [3, 5, 8])],
        known=MA_KNOWN,
        normalize=ma_normalize,
    )
    assert cells == [{"slow": 20, "fast": 3}, {"slow": 20, "fast": 5}, {"slow": 20, "fast": 8}]


def test_grid_cells_keeps_whatever_the_normalizer_returns() -> None:
    """落进格子里的是**归一器的返回值**，不是展开出来的那份。

    这条钉的是**职责边界**：展开只做笛卡尔积；缺省值填不填、填成什么，全由归一器决定。
    用户策略的 `PARAMS` 缺省值正是这样进来的（`validate_user_params` 返回填满的字典），
    内置策略则原样返回——引擎的 `from_params` 在跑的时候才补。
    """
    def normalize(params):
        return {**params, "slow": 20}, []  # 模拟「填缺省值」

    cells = grid_cells(
        base={}, axes=[("fast", [3, 5])], known=frozenset({"fast", "slow"}), normalize=normalize
    )
    assert cells == [{"fast": 3, "slow": 20}, {"fast": 5, "slow": 20}]


@pytest.mark.parametrize(
    ("axes", "reason"),
    [
        ([], "至少要有 1 条参数轴"),
        ([("fast", [1, 2]), ("slow", [3, 4]), ("x", [5, 6])], "最多 2 条"),
    ],
)
def test_grid_cells_rejects_a_bad_axis_count(axes, reason) -> None:
    with pytest.raises(BatchRequestError, match=reason):
        grid_cells(
            base={}, axes=axes, known=frozenset({"fast", "slow", "x"}),
            normalize=lambda p: (dict(p), []),
        )


def test_grid_cells_rejects_an_axis_param_outside_the_strategy() -> None:
    with pytest.raises(BatchRequestError, match="不接受参数"):
        grid_cells(base={}, axes=[("nope", [1, 2])], known=MA_KNOWN, normalize=ma_normalize)


def test_grid_cells_rejects_an_axis_param_that_collides_with_the_base() -> None:
    with pytest.raises(BatchRequestError, match="同时出现在基座参数与参数轴"):
        grid_cells(
            base={"fast": 5}, axes=[("fast", [1, 2])], known=MA_KNOWN, normalize=ma_normalize
        )


def test_grid_cells_rejects_a_repeated_axis() -> None:
    with pytest.raises(BatchRequestError, match="出现了不止一次"):
        grid_cells(
            base={},
            axes=[("fast", [1, 2]), ("fast", [3, 4])],
            known=MA_KNOWN,
            normalize=ma_normalize,
        )


@pytest.mark.parametrize(
    ("values", "reason"),
    [([5], "至少要有 2 个取值"), ([5, 5], "有重复")],
)
def test_grid_cells_rejects_degenerate_axis_values(values, reason) -> None:
    with pytest.raises(BatchRequestError, match=reason):
        grid_cells(base={}, axes=[("fast", values)], known=MA_KNOWN, normalize=ma_normalize)


def test_grid_cells_rejects_a_grid_over_the_cell_cap() -> None:
    """11×11 = 121 > 100：整单拒绝，不截断、不静默取前 100 格。"""
    with pytest.raises(BatchRequestError, match=f"超过单次上限 {MAX_CELLS} 格"):
        grid_cells(
            base={},
            axes=[("fast", list(range(1, 12))), ("slow", list(range(20, 31)))],
            known=MA_KNOWN,
            normalize=ma_normalize,
        )


def test_grid_cells_rejects_the_whole_order_when_one_cell_is_illegal() -> None:
    """SPEC §6 M5b 写死的那一条：**任一格非法即整单 422**，并指出是第几格、哪组参数、为什么。

    `fast=[5, 20] × slow=[10, 30]` 是极自然的一种选法，但 `fast=20/slow=10` 非法——
    静默跳过会在热力图上留一个按不出原因的空洞。
    """
    with pytest.raises(BatchRequestError) as excinfo:
        grid_cells(
            base={},
            axes=[("fast", [5, 20]), ("slow", [10, 30])],
            known=MA_KNOWN,
            normalize=ma_normalize,
        )
    message = str(excinfo.value)
    assert "第 3 格" in message and "fast=20" in message and "slow=10" in message
    assert "小于" in message  # 策略自己那句话原样带出来，不换成泛泛的「参数不合法」


# ── 最优格判据 ───────────────────────────────────────────────


def cell(sharpe: float | None, total_return: float = 0.0, ok: bool = True) -> dict:
    return {
        "ok": ok,
        "metrics": {"sharpe": sharpe, "total_return": total_return} if ok else None,
    }


def test_pick_best_takes_the_highest_sharpe() -> None:
    assert pick_best([cell(0.5), cell(1.2), cell(0.9)]) == 1


def test_pick_best_ranks_an_undefined_sharpe_last() -> None:
    """`None` 排最后——但**不是**被丢掉：它仍参与「有效格计数」，只是当不了最优。"""
    assert pick_best([cell(None, total_return=9.9), cell(-0.3)]) == 1


def test_pick_best_breaks_ties_by_total_return() -> None:
    assert pick_best([cell(1.0, 0.1), cell(1.0, 0.4)]) == 1


def test_pick_best_returns_none_when_nothing_is_rankable() -> None:
    """全体无有效夏普 ⇒ 返 `None`，**不挑一个收益最高的充数**（DSR 的「被选中者」无从谈起）。"""
    assert pick_best([cell(None), cell(None)]) is None
    assert pick_best([cell(1.0, ok=False), cell(0.5, ok=False)]) is None
    assert pick_best([]) is None


# ── 执行：真实 Parquet ───────────────────────────────────────


def grid_specs(count: int = 4) -> list[CellSpec]:
    """`count` 格互不相同的参数组合（`fast` 从 2 起递增，恒小于 `slow=30`）。"""
    return [
        CellSpec(symbol=SYMBOL, strategy="ma_cross", params={"fast": fast, "slow": 30})
        for fast in range(2, 2 + count)
    ]


async def test_run_cells_returns_a_self_contained_summary(data_dir: Path) -> None:
    summary = await run_cells(grid_specs(), options(data_dir), kind="grid")

    assert summary["kind"] == "grid"
    assert len(summary["cells"]) == 4
    assert summary["best_index"] is not None
    assert summary["overfit"]["n_trials"] == 4
    assert summary["adjust"] == "qfq" and summary["pit_mode"] == "pit"
    # `costs` 与报告 `meta.costs` 同形：是给人看的那一句，不是结构体
    assert "佣金" in summary["costs"] and "滑点" in summary["costs"]
    assert summary["duration_s"] >= 0

    first = summary["cells"][0]
    assert first["index"] == 0 and first["ok"] is True
    assert first["params"] == {"fast": 2, "slow": 30}
    assert first["window"]["bars"] > 0
    assert set(first["metrics"]) >= {"total_return", "sharpe", "max_drawdown", "trade_count"}
    assert set(first["moments"]) == {"skew", "kurt", "n"}
    assert summary["window"] is not None  # 同标的同策略 ⇒ 窗口一致，可以共用


async def test_cells_report_progress_one_by_one(data_dir: Path) -> None:
    """`on_cell` 每跑完一格就被 await 一次，且**带 index**——端点靠它逐格推 SSE 帧。"""
    seen: list[int] = []

    async def on_cell(item: dict) -> None:
        seen.append(item["index"])

    await run_cells(grid_specs(), options(data_dir), kind="grid", on_cell=on_cell)
    assert sorted(seen) == [0, 1, 2, 3]


async def test_a_single_cell_matches_a_standalone_backtest(data_dir: Path) -> None:
    """「单格」与既有回测**同口径**：同一配置下 metrics 逐键相等（SPEC §6 M5b 的断言）。"""
    summary = await run_cells(
        [CellSpec(symbol=SYMBOL, strategy="ma_cross", params={"fast": 5, "slow": 20})],
        options(data_dir),
        kind="grid",
    )
    cell_metrics = summary["cells"][0]["metrics"]

    standalone = build_report(
        BacktestConfig(
            symbol=SYMBOL,
            strategy="ma_cross",
            start=date.fromisoformat(summary["cells"][0]["window"]["start"]),
            end=date.fromisoformat(summary["cells"][0]["window"]["end"]),
            adjust="qfq",
            costs=CostModel(),
            params={"fast": 5, "slow": 20},
            data_dir=data_dir,
        )
    )
    assert cell_metrics == standalone["metrics"]


async def test_a_broken_cell_does_not_take_down_the_batch(data_dir: Path) -> None:
    """单格隔离：一个不存在的标的只毁它自己那几格，其余照跑（SPEC §12 M5b 增量）。"""
    specs = [
        CellSpec(symbol=SYMBOL, strategy="ma_cross", params={"fast": 5, "slow": 20}),
        CellSpec(symbol="999999", strategy="ma_cross", params={"fast": 5, "slow": 20}),
        CellSpec(symbol=SYMBOL, strategy="ma_cross", params={"fast": 8, "slow": 20}),
    ]
    summary = await run_cells(specs, options(data_dir), kind="batch")

    by_symbol = {cell["symbol"]: cell for cell in summary["cells"]}
    assert by_symbol[SYMBOL]["ok"] is True
    failed = by_symbol["999999"]
    assert failed["ok"] is False
    assert failed["error"]["kind"] == "no_data"
    assert "999999" in failed["error"]["message"]
    assert sum(1 for cell in summary["cells"] if cell["ok"]) == 2
    # 失败格不参与最优评选，但**仍在 cells 里**（如实落库，不是悄悄扔掉）
    assert len(summary["cells"]) == 3
    assert summary["cells"][summary["best_index"]]["ok"] is True


async def test_all_cells_failing_still_produces_a_summary(data_dir: Path) -> None:
    """全失败也返回完整 summary（是事实，不是错误）——由端点决定照常落库。"""
    specs = [CellSpec(symbol="999999", strategy="ma_cross", params={})]
    summary = await run_cells(specs, options(data_dir), kind="batch")

    assert summary["best_index"] is None
    assert summary["overfit"]["dsr"] is None
    assert summary["overfit"]["reason"] == "best_sharpe_undefined"
    assert summary["window"] is None


async def test_mixed_windows_are_not_papered_over(data_dir: Path) -> None:
    """逐格窗口不同（批量里必然如此）时 `window` 返 `None`——不挑一格冒充全体。"""
    summary = await run_cells(
        [
            CellSpec(symbol=SYMBOL, strategy="ma_cross", params={"fast": 5, "slow": 20}),
            CellSpec(symbol=SYMBOL, strategy="ma_cross", params={"fast": 5, "slow": 20}),
        ],
        options(data_dir, start=date(2026, 8, 20)),
        kind="batch",
    )
    assert summary["window"] is not None  # 同区间 ⇒ 共享

    mixed = await run_cells(
        [
            CellSpec(symbol=SYMBOL, strategy="ma_cross", params={"fast": 5, "slow": 20}),
            CellSpec(symbol="999999", strategy="ma_cross", params={"fast": 5, "slow": 20}),
        ],
        options(data_dir),
        kind="batch",
    )
    assert mixed["window"] is None  # 一格没有 window，另一格有 ⇒ 不共享


# ── 并发上限（判据是「峰值 ≤2」与「与串行逐格相等」，不是快了多少）──────


async def test_concurrency_never_exceeds_the_cap(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """仪器化探针记录**同时在跑的格数峰值**：必须 ≤ `CONCURRENCY`，且确实并发过（≥2）。

    「确实并发过」这一半同样重要——上限设成 1 也能让峰值 ≤2，
    那样这条用例就测不出「上限生效」而只是测出了「没并发」。
    """
    live = 0
    peak = 0
    real = batch_module._execute_cell

    async def probe(spec, opts, windows):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        try:
            await asyncio.sleep(0.02)  # 制造重叠窗口，否则并发上限无从观察
            return await real(spec, opts, windows)
        finally:
            live -= 1

    monkeypatch.setattr(batch_module, "_execute_cell", probe)
    summary = await run_cells(grid_specs(6), options(data_dir), kind="grid")

    assert peak == CONCURRENCY
    assert len(summary["cells"]) == 6


async def test_concurrent_and_serial_runs_agree_cell_by_cell(data_dir: Path) -> None:
    """并发不改变任何一格的结果——逐格 `metrics` / `moments` 相等（SPEC §12 的判据之一）。"""
    specs = grid_specs(6)
    concurrent = await run_cells(specs, options(data_dir), kind="grid")

    serial_options = options(data_dir)
    serial = [
        await batch_module._execute_cell(
            spec, serial_options, batch_module._WindowCache(serial_options)
        )
        for spec in specs
    ]

    by_params = {tuple(sorted(cell["params"].items())): cell for cell in concurrent["cells"]}
    for cell in serial:
        key = tuple(sorted(cell["params"].items()))
        assert by_params[key]["metrics"] == cell["metrics"]
        assert by_params[key]["moments"] == cell["moments"]


async def test_stopping_prevents_new_cells_from_starting(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """中止语义：**停止派发新格**，已在跑的自然跑完（结果照常回调，落不落库由调用方定）。"""
    stop = asyncio.Event()
    started: list[int] = []
    real = batch_module._execute_cell

    async def probe(spec, opts, windows):
        started.append(1)
        stop.set()  # 第一格一开跑就按下中止
        return await real(spec, opts, windows)

    monkeypatch.setattr(batch_module, "_execute_cell", probe)
    summary = await run_cells(grid_specs(8), options(data_dir), kind="grid", stop=stop)

    assert len(started) <= CONCURRENCY  # 只有闸门里那几格真的开跑
    assert len(summary["cells"]) == len(started)


async def test_resolve_window_is_shared_across_cells(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`resolve_window` 是全史扫描（~154ms/次），必须按 (标的, 策略) 复用——逐格解析是纯开销。"""
    calls: list[tuple[str, str]] = []
    real = batch_module.resolve_window

    def spy(symbol, strategy, start=None, end=None, **kwargs):
        calls.append((symbol, strategy))
        return real(symbol, strategy, start, end, **kwargs)

    monkeypatch.setattr(batch_module, "resolve_window", spy)
    await run_cells(grid_specs(6), options(data_dir), kind="grid")

    assert calls == [(SYMBOL, "ma_cross")]  # 6 格只解析一次


def test_every_builtin_strategy_is_usable_as_a_grid_target() -> None:
    """网格不为某条内置策略开小灶：参数集合能直接喂给 `grid_cells`（每条轴取两个值）。"""
    for name in available_strategies():
        params = known_params(name)
        if not params:
            continue
        axis_name = sorted(params)[0]
        cells = grid_cells(
            base={},
            axes=[(axis_name, [1, 2])],
            known=params,
            normalize=lambda p, name=name: (dict(p), validate_params(name, p)),
        )
        assert len(cells) == 2
