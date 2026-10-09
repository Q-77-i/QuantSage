"""M5b 网格取证：在**真实数据**上跑一次 5×5 网格，产出 `logs/m5b/` 的三份证据。

它同时验四件事，前三件是自动化层看不见的：

1. **网格真的跑通**（SPEC §6 验收第一条）：逐格耗时、成功率、最优格、DSR；
2. **并发上限确实生效**：仪器化探针记录同时在跑的格数峰值，并给出串行 / 并发-2 的
   **耗时时间线**——判据是「峰值 ≤2 且结果逐格相等」，**耗时比值如实记录、不设阈值**
   （实测只有 0.83~0.91：DuckDB 默认 `threads=8` 已吃满核心，再并发只是分时）；
3. **Deflated Sharpe 与独立实现对照**：同一组输入用 `scipy` 再算一遍，两次结果必须一致
   （`scipy` 只在**这个取证脚本**里用，运行期与单测一律纯标准库）；
4. **单格与既有回测同口径**：某一格的 `metrics` 与直接 `POST /backtest` 同配置的报告逐键相等。

```
cd backend && uv run python scripts/run_grid.py                  # 全期窗口
cd backend && uv run python scripts/run_grid.py --start 2026-07-01 --end 2026-09-30
```

退出码：0 全部断言通过；1 有断言未过。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtest import batch as batch_module  # noqa: E402
from app.backtest.batch import CellSpec, RunOptions, grid_cells, run_cells  # noqa: E402
from app.backtest.costs import CostModel  # noqa: E402
from app.backtest.engine import BacktestConfig  # noqa: E402
from app.backtest.report import build_report  # noqa: E402
from app.backtest.strategies import known_params, validate_params  # noqa: E402
from app.backtest.types import Mode  # noqa: E402

SYMBOL = "600519"
STRATEGY = "ma_cross"
FASTS = [3, 5, 8, 10, 12]
SLOWS = [15, 20, 30, 40, 60]
OUT = Path(__file__).resolve().parents[2] / "logs" / "m5b"


@dataclass
class Check:
    label: str
    ok: bool
    detail: str = ""


def independent_dsr(out: dict, sharpes: list[float | None]) -> float | None:
    """用 **scipy** 独立实现重算一遍 DSR（只在取证脚本里用，不进运行期依赖）。"""
    import numpy as np
    from scipy.stats import norm

    gamma = 0.5772156649015329
    valid = [s / np.sqrt(252) for s in sharpes if s is not None]
    if len(valid) < 2 or out["sr"] is None or out["sr0"] is None:
        return None
    variance = np.var(valid)  # ddof=0，与模块口径一致
    sr0 = np.sqrt(variance) * (
        (1 - gamma) * norm.ppf(1 - 1 / len(valid)) + gamma * norm.ppf(1 - 1 / (len(valid) * np.e))
    )
    sr = out["sr"]
    denominator = np.sqrt(1 - out["skew"] * sr + (out["kurt"] - 1) / 4 * sr**2)
    return float(norm.cdf((sr - sr0) * np.sqrt(out["observations"] - 1) / denominator))


async def measure(
    specs: list[CellSpec], options: RunOptions, *, concurrency: int | None
) -> tuple[float, dict, int]:
    """跑一轮并返回 (耗时, summary, **同时在跑的格数峰值**)。

    峰值靠探针量：把 `_execute_cell` 临时换成计数版。这是「并发上限生效」唯一的
    直接证据——只测耗时是测不出上限的（把闸门设成 1，耗时只会更慢）。
    """
    live = peak = 0
    real = batch_module._execute_cell

    async def probe(spec, opts, windows):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        started = time.perf_counter()
        try:
            return await real(spec, opts, windows)
        finally:
            live -= 1
            timings.append((spec.params.get("fast"), spec.params.get("slow"),
                            round(time.perf_counter() - started, 3)))

    timings: list[tuple] = []
    batch_module._execute_cell = probe
    previous = batch_module.CONCURRENCY
    if concurrency is not None:
        batch_module.CONCURRENCY = concurrency
        batch_module._gate = None  # 闸门按事件循环缓存，改了上限要让它重建
    try:
        started = time.perf_counter()
        summary = await run_cells(specs, options, kind="grid")
        elapsed = time.perf_counter() - started
    finally:
        batch_module._execute_cell = real
        batch_module.CONCURRENCY = previous
        batch_module._gate = None
    summary["_timings"] = timings
    return elapsed, summary, peak


async def main() -> int:
    parser = argparse.ArgumentParser(description="M5b 网格取证")
    parser.add_argument("--start", default=None, help="YYYY-MM-DD，缺省取该标的全部历史")
    parser.add_argument("--end", default=None)
    parser.add_argument("--json", default=None, help="额外写一份 JSON 的路径")
    args = parser.parse_args()

    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None
    checks: list[Check] = []

    nodes = grid_cells(
        base={},
        axes=[("fast", FASTS), ("slow", SLOWS)],
        known=known_params(STRATEGY),
        normalize=lambda p: (dict(p), validate_params(STRATEGY, p)),
    )
    specs = [CellSpec(symbol=SYMBOL, strategy=STRATEGY, params=p) for p in nodes]
    options = RunOptions(costs=CostModel(), pit_mode=Mode.PIT, start=start, end=end)

    # ── 并发 2（带峰值探针）与串行 ──────────────────────────
    concurrent_s, summary, peak = await measure(specs, options, concurrency=None)
    serial_s, serial_summary, serial_peak = await measure(specs, options, concurrency=1)

    checks.append(Check("并发上限生效（峰值 ≤ 2）", peak <= batch_module.CONCURRENCY, f"峰值 {peak}"))
    checks.append(
        Check(
            "并发与串行逐格结果相等",
            [(c["params"], c["metrics"]) for c in summary["cells"]]
            == [(c["params"], c["metrics"]) for c in serial_summary["cells"]],
            f"并发 {len(summary['cells'])} 格 / 串行 {len(serial_summary['cells'])} 格",
        )
    )
    checks.append(Check(f"25 格全部成功（串行峰值 {serial_peak}）", summary["cells_ok"] == 25, f"{summary['cells_ok']}/25"))

    windows = {
        (cell["window"]["start"], cell["window"]["end"]) for cell in summary["cells"]
    }
    checks.append(Check("同标的同策略 ⇒ 窗口唯一", len(windows) == 1, str(sorted(windows))))

    # ── DSR 与独立实现对照 ──────────────────────────────────
    overfit = summary["overfit"]
    reference = independent_dsr(overfit, [c["metrics"]["sharpe"] for c in summary["cells"]])
    delta = (
        abs(overfit["dsr"] - reference)
        if overfit["dsr"] is not None and reference is not None
        else None
    )
    checks.append(
        Check(
            "Deflated Sharpe 与独立实现（scipy）一致",
            delta is not None and delta < 1e-12,
            f"本模块 {overfit['dsr']} / scipy {reference} / 差 {delta}",
        )
    )
    # 退化闭式：γ₄=1 时分母为 1 ⇒ DSR = Φ((SR−SR₀)·√(T−1))
    from statistics import NormalDist

    closed = NormalDist().cdf(
        (overfit["sr"] - overfit["sr0"]) * (overfit["observations"] - 1) ** 0.5
    ) if overfit["sr"] is not None else None
    from app.backtest.overfit import deflated_sharpe

    checks.append(
        Check(
            "退化闭式（γ₃=0、γ₄=1 ⇒ 分母为 1）可复现",
            closed is not None
            and abs(deflated_sharpe(overfit["sr"], overfit["sr0"], 0.0, 1.0, overfit["observations"]) - closed)
            < 1e-15,
            f"Φ((SR−SR₀)·√(T−1)) = {closed}",
        )
    )

    # ── 单格与既有回测同口径 ────────────────────────────────
    best = summary["cells"][summary["best_index"]]
    standalone = await asyncio.to_thread(
        build_report,
        BacktestConfig(
            symbol=SYMBOL,
            strategy=STRATEGY,
            start=date.fromisoformat(best["window"]["start"]),
            end=date.fromisoformat(best["window"]["end"]),
            params=best["params"],
        ),
    )
    checks.append(
        Check(
            "最优格 metrics 与 POST /backtest 同配置逐键相等",
            best["metrics"] == standalone["metrics"],
        )
    )
    # 逐格都验一遍（不止最优格）
    mismatch = []
    for cell in summary["cells"]:
        report = await asyncio.to_thread(
            build_report,
            BacktestConfig(
                symbol=SYMBOL,
                strategy=STRATEGY,
                start=date.fromisoformat(cell["window"]["start"]),
                end=date.fromisoformat(cell["window"]["end"]),
                params=cell["params"],
            ),
        )
        if report["metrics"] != cell["metrics"]:
            mismatch.append(cell["params"])
    checks.append(Check("25 格逐格与独立回测一致", not mismatch, f"不一致：{mismatch}"))

    # ── 出证据 ──────────────────────────────────────────────
    OUT.mkdir(parents=True, exist_ok=True)
    ratio = concurrent_s / serial_s
    _write_grid(checks, summary, concurrent_s, serial_s, ratio, peak)
    _write_concurrency(summary, serial_summary, concurrent_s, serial_s, ratio, peak)
    _write_deflated(overfit, summary, reference)

    payload = {
        "symbol": SYMBOL,
        "strategy": STRATEGY,
        "window": sorted(windows)[0],
        "cells": len(summary["cells"]),
        "serial_s": round(serial_s, 2),
        "concurrent_s": round(concurrent_s, 2),
        "ratio": round(ratio, 3),
        "peak_in_flight": peak,
        "overfit": overfit,
        "checks": [{"label": c.label, "ok": c.ok, "detail": c.detail} for c in checks],
    }
    if args.json:
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    for check in checks:
        print(f"  {'✓' if check.ok else '✗'} {check.label}{'：' + check.detail if check.detail else ''}")
    print(f"\n串行 {serial_s:.2f}s / 并发 2 {concurrent_s:.2f}s（比值 {ratio:.2f}，峰值 {peak}）")
    print(f"最优格 {best['params']} 夏普 {best['metrics']['sharpe']:.4f} DSR {overfit['dsr']}")
    return 0 if all(check.ok for check in checks) else 1


def _write_grid(checks, summary, concurrent_s, serial_s, ratio, peak) -> None:
    best = summary["cells"][summary["best_index"]]
    by_fast: dict[float, list[str]] = {}
    for cell in summary["cells"]:
        sharpe = cell["metrics"]["sharpe"]
        by_fast.setdefault(cell["params"]["fast"], []).append(
            f"{sharpe:.3f}" if sharpe is not None else "—"
        )

    lines = [
        "# M5b 网格取证（真实数据）",
        "",
        f"- 标的 `{SYMBOL}` · 策略 `{STRATEGY}` · 区间 "
        f"{summary['cells'][0]['window']['start']} → {summary['cells'][0]['window']['end']}"
        f"（{summary['cells'][0]['window']['bars']} bars）",
        f"- 网格 {len(FASTS)} × {len(SLOWS)} = **{len(summary['cells'])} 格**，"
        f"成功 **{summary['cells_ok']}** / 失败 {summary['cells_total'] - summary['cells_ok']}",
        f"- 串行 **{serial_s:.2f}s** · 并发 2 **{concurrent_s:.2f}s**（比值 **{ratio:.2f}**，"
        f"同时最多 {peak} 格在跑）",
        f"- 最优格 **{best['params']}** · 夏普 **{best['metrics']['sharpe']:.4f}** · "
        f"总收益 {best['metrics']['total_return']:.2%} · 回撤 {best['metrics']['max_drawdown']:.2%}",
        "",
        "## 夏普矩阵（行 = fast，列 = slow）",
        "",
        "| fast \\\\ slow | " + " | ".join(str(s) for s in SLOWS) + " |",
        "|---|" + "---|" * len(SLOWS),
    ]
    for fast, cells in sorted(by_fast.items()):
        lines.append(f"| {fast} | " + " | ".join(cells) + " |")

    lines += [
        "",
        "## 逐格耗时（并发轮，秒）",
        "",
        "| 格 | 参数 | 秒 |",
        "|---|---|---|",
    ]
    for index, entry in enumerate(sorted(summary["_timings"])):
        fast, slow, seconds = entry
        lines.append(f"| {index} | fast={fast}, slow={slow} | {seconds} |")

    lines += ["", "## 断言", ""]
    for check in checks:
        lines.append(f"- [{'x' if check.ok else ' '}] {check.label}" + (f" —— {check.detail}" if check.detail else ""))
    (OUT / "grid.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_concurrency(summary, serial_summary, concurrent_s, serial_s, ratio, peak) -> None:
    cells = summary["cells"]
    durations = [cell["duration_ms"] for cell in cells]
    lines = [
        "# M5b 并发上限取证",
        "",
        "> 判据**不是「快了多少」**。SPEC v1.18 曾写「并发 2 的耗时 ≈ 串行的 55%」——",
        "> 2026-10-09 规划期实测**推翻**：本机 8 核、DuckDB 默认 `threads=8`，一次查询就已吃满",
        "> 全部核心，再并发只是在同一批核心上分时。进程池与线程池几乎一样 ⇒ **瓶颈不是 GIL**。",
        "",
        "| 量 | 读数 |",
        "|---|---|",
        f"| 格数 | {len(cells)} |",
        f"| 串行总耗时 | **{serial_s:.2f}s** |",
        f"| 并发 2 总耗时 | **{concurrent_s:.2f}s** |",
        f"| 比值 | **{ratio:.2f}**（不设阈值，如实记录） |",
        f"| 同时在跑的格数**峰值** | **{peak}**（上限 2） |",
        f"| 单格耗时 中位 / 最短 / 最长 | {statistics.median(durations):.0f}ms / "
        f"{min(durations):.0f}ms / {max(durations):.0f}ms |",
        "",
        "## 并发与串行逐格结果相等",
        "",
        "| 参数 | 并发 sharpe | 串行 sharpe | 相等 |",
        "|---|---|---|---|",
    ]
    for cell, plain in zip(cells, serial_summary["cells"], strict=True):
        sharpe = cell["metrics"]["sharpe"]
        lines.append(
            f"| {cell['params']} | {sharpe} | {plain['metrics']['sharpe']} | "
            f"{'✓' if cell['metrics'] == plain['metrics'] else '✗'} |"
        )
    lines += [
        "",
        "**结论**：并发上限 2 的价值是「单格失败互不牵连 / 一个网格不独占服务 / P3-E1 队列的前置形态」，",
        "**不是提速**——对外不得宣称接近翻倍。",
    ]
    (OUT / "concurrency.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_deflated(overfit, summary, reference) -> None:
    sharpes = [cell["metrics"]["sharpe"] for cell in summary["cells"]]
    lines = [
        "# M5b Deflated Sharpe 取证",
        "",
        "## 网格的夏普分布",
        "",
        f"- 有效格 **{overfit['n_valid']} / {overfit['n_trials']}**",
        f"- 最优格下标 {overfit['best_index']}（判据：夏普最大，`None` 排最后、总收益破平）",
        f"- 夏普：最短 {min(s for s in sharpes if s is not None):.4f} · "
        f"最长 {max(s for s in sharpes if s is not None):.4f}",
        "",
        "## DSR 与它的全部输入",
        "",
        "| 量 | 值 | 说明 |",
        "|---|---|---|",
        f"| SR（每期） | {overfit['sr']!r} | 年化值已 `/√252`——混用会静默差 ≈15.9 倍 |",
        f"| SR₀（每期） | {overfit['sr0']!r} | N 次试验下「最大夏普」的期望 |",
        f"| V[{{SRₙ}}] | {overfit['sr_variance']!r} | 各格每期夏普的**总体**方差（ddof=0，同参考实现） |",
        f"| γ₃ 偏度 | {overfit['skew']!r} | 最优格自身日收益的总体矩 |",
        f"| γ₄ 峰度 | {overfit['kurt']!r} | **原始**峰度（正态 = 3），不是超额峰度 |",
        f"| T 观测数 | {overfit['observations']} | |",
        f"| **DSR** | **{overfit['dsr']!r}** | |",
        f"| 独立实现（scipy） | {reference!r} | 差 {abs(overfit['dsr'] - reference) if overfit['dsr'] is not None and reference is not None else None} |",
        "",
        "## 三条正交验算",
        "",
        "1. **`SR = SR₀ ⇒ DSR = Φ(0) = 0.5`**——不含任何常数，见 `tests/test_overfit.py`",
        "2. **γ₃=0 且 γ₄=1 ⇒ 分母恰为 1 ⇒ `DSR = Φ((SR−SR₀)·√(T−1))`** 可闭式手算",
        "   （γ₄=1 是**对称两点分布**的原始峰度：±a 各半时 m4/m2² = a⁴/a⁴ = 1）",
        "3. **一组固定输入的期望值由 scipy 独立实现算得**并钉成常数（0.7315995277598144），",
        "   只钉常数、不引依赖——运行期与单测一律纯标准库",
        "",
        "## 标注（如实写进报告与 UI）",
        "",
        f"> {overfit['note']}",
    ]
    (OUT / "deflated_sharpe.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
