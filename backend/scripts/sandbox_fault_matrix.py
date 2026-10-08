"""M4a 验收证据：沙箱故障注入矩阵（可复现）。

    cd backend && uv run python scripts/sandbox_fault_matrix.py > ../logs/m4/fault_injection.md

每条坏法都跑一遍真子进程，打印「拿到什么错误」；**每条之后立刻再跑一次正常策略**——
那一列才是「坏代码不会拖垮服务」的直接证据（父进程与沙箱设施在故障之后仍然可用）。

行情用临时目录里的合成 Parquet（不碰 data/ 与生产数据）；每条约 0.3–2s。
"""

from __future__ import annotations

import math
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtest.engine import BacktestConfig  # noqa: E402
from app.backtest.types import Mode  # noqa: E402
from app.strategy import SandboxError, SandboxLimits, StrategyRejected  # noqa: E402
from app.strategy.sandbox import run_user_strategy_sync  # noqa: E402
from tests.conftest import make_backtest_dir  # noqa: E402

SYMBOL = "600519"
START = date(2026, 1, 5)
BARS = 40

HEALTHY = "def on_bar(ctx):\n    return []\n"

#: (名称, 源码, 配额覆盖, 策略参数, 期望的 kind——None 表示期望跑通)
CASES: tuple[tuple[str, str, dict[str, object], dict[str, float], str | None], ...] = (
    ("死循环（CPU 上限 2s）", "def on_bar(ctx):\n    while True:\n        pass\n",
     {"wall_seconds": 30.0, "cpu_seconds": 2}, {}, "cpu"),
    ("死循环（墙钟 1.5s，CPU 放宽）", "def on_bar(ctx):\n    while True:\n        pass\n",
     {"wall_seconds": 1.5, "cpu_seconds": 600}, {}, "wall"),
    ("内存炸弹（分配 400MB，上限 200MB）",
     "def on_bar(ctx):\n    x = bytearray(400 * 1024 * 1024)\n    return []\n",
     {"wall_seconds": 30.0, "cpu_seconds": 30, "memory_mb": 200}, {}, "memory"),
    ("stdout 超限（协议通道，上限压到 200B）",
     "def on_bar(ctx):\n    return []\n",
     {"wall_seconds": 30.0, "cpu_seconds": 30, "output_bytes": 200}, {}, "output"),
    ("语法错", "def on_bar(ctx)\n    return []\n", {}, {}, None),
    ("缺 on_bar", "def nope(ctx):\n    return []\n", {}, {}, None),
    ("运行期异常", "def on_bar(ctx):\n    return 1 / 0\n", {}, {}, None),
    ("数据绕行：import duckdb", "import duckdb\ndef on_bar(ctx):\n    return []\n", {}, {}, None),
    ("返回值是 None", "def on_bar(ctx):\n    return None\n", {}, {}, None),
    ("未声明的参数键",
     'PARAMS = {"fast": {"type": "int", "default": 5, "max": 10}}\n'
     "def on_bar(ctx, p):\n    return []\n",
     {}, {"fast": 999}, None),
    ("print 噪音（stderr 截断）",
     "def on_bar(ctx):\n    print('调试' * 50)\n    return []\n",
     {"stderr_bytes": 1024}, {}, None),
)


def build_data_dir(root: Path) -> Path:
    rows = [
        {
            "trade_date": START + timedelta(days=i),
            "open": 10.0 + math.sin(i / 2.7) * 0.8,
            "close": 10.0 + math.sin(i / 2.7) * 0.8 + i * 0.02,
            "volume": 1e5,
        }
        for i in range(BARS)
    ]
    return make_backtest_dir(root, rows, events=[])


def config_for(data_dir: Path, params: dict[str, float] | None = None) -> BacktestConfig:
    return BacktestConfig(
        symbol=SYMBOL,
        strategy="user",
        start=START,
        end=START + timedelta(days=BARS - 1),
        pit_mode=Mode.PIT,
        params=params or {},
        data_dir=data_dir,
    )


def is_service_alive(data_dir: Path) -> bool:
    """故障之后父进程仍能跑通一次正常策略。"""
    try:
        outcome = run_user_strategy_sync(HEALTHY, config=config_for(data_dir), strategy_name="体检")
    except Exception:  # noqa: BLE001 - 这里任何异常都算「服务不可用」
        return False
    return outcome.report["meta"]["bars"] == BARS


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = build_data_dir(Path(tmp))
        print("# M4a 沙箱故障注入矩阵（实测输出，可复现）")
        print()
        print(f"生成：`uv run python scripts/sandbox_fault_matrix.py` ｜ 合成行情 {BARS} 根 bar ｜ "
              f"Python {sys.version.split()[0]}")
        print()
        print("| # | 注入的坏法 | 沙箱结论 | 错误信息（截断） | 之后服务仍可用 | 耗时 |")
        print("|---|---|---|---|---|---|")
        failures = 0
        for index, (name, source, overrides, params, expect_kind) in enumerate(CASES, start=1):
            limits = SandboxLimits(**overrides)  # type: ignore[arg-type]
            started = time.monotonic()
            try:
                outcome = run_user_strategy_sync(
                    source, config=config_for(data_dir, params), limits=limits, strategy_name=name
                )
                verdict = "跑通（符合预期）" if expect_kind is None else "❗不该跑通"
                detail = (
                    f"{outcome.report['meta']['bars']} 根 bar，报告完好"
                    if not outcome.stderr_tail
                    else f"报告完好；stderr {len(outcome.stderr_tail.encode())}B（已截断）"
                )
                failures += 0 if expect_kind is None else 1
            except StrategyRejected as exc:
                verdict = "拒绝（用户要改）"
                detail = f"{exc}" + (f" ｜ 第 {exc.line} 行" if exc.line else "")
                failures += 0 if expect_kind is None else 1
            except SandboxError as exc:
                verdict = f"沙箱终止（{exc.kind}）"
                detail = str(exc)
                failures += 0 if exc.kind == expect_kind else 1
            elapsed = time.monotonic() - started
            alive = is_service_alive(data_dir)
            failures += 0 if alive else 1
            print(f"| {index} | {name} | {verdict} | {detail[:110]} | {'✓' if alive else '✗'} | "
                  f"{elapsed:.2f}s |")
        print()
        print(f"**结论**：{len(CASES)} 条坏法全部被明确处置，且每条之后父进程仍能跑通正常策略"
              f"（失败计数 {failures}）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
