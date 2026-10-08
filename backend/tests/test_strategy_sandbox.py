"""M4a 沙箱：真子进程的端到端与**故障注入矩阵**。

这是 M4a 的验收主体：坏代码不拖垮服务，靠的不是「相信用户」，而是把每种坏法都跑一遍，
断言拿到的是**明确错误**而不是挂死、也不是静默成功。

用例都 spawn 真子进程（每个约 0.3–0.5s），行情走 conftest 的合成 Parquet（真 Parquet 文件，
不 mock 查询层，沿用 P1 口径）。
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.backtest.engine import BacktestConfig
from app.backtest.report import build_report
from app.backtest.types import Mode
from app.strategy import SandboxError, SandboxLimits, StrategyRejected
from app.strategy.sandbox import (
    EXIT_MEMORY,
    config_from_payload,
    config_to_payload,
    run_user_strategy_sync,
)
from tests.conftest import make_backtest_dir

SYMBOL = "600519"
START = date(2026, 1, 5)
BARS = 60


def _waves() -> list[dict[str, object]]:
    """确定性的波动序列：够长到 MA5/MA20 都能算，且会真的产生金叉死叉。"""
    rows = []
    for i in range(BARS):
        close = 10.0 + math.sin(i / 2.7) * 0.8 + i * 0.02
        rows.append(
            {
                "trade_date": START + timedelta(days=i),
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1e5,
            }
        )
    return rows


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return make_backtest_dir(tmp_path, _waves(), events=[])


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


def run_source(
    source: str,
    data_dir: Path,
    *,
    params: dict[str, float] | None = None,
    limits: SandboxLimits | None = None,
    name: str = "测试策略",
):
    return run_user_strategy_sync(
        source,
        config=config_for(data_dir, params),
        strategy_name=name,
        limits=limits,
    )


MA_USER_SOURCE = '''
PARAMS = {
    "fast": {"type": "int", "default": 5, "min": 1, "max": 250, "label": "快线周期"},
    "slow": {"type": "int", "default": 20, "min": 2, "max": 250, "label": "慢线周期"},
}
USES_EVENTS = False

def validate_params(p):
    return ["快线须小于慢线"] if p["fast"] >= p["slow"] else []

def mean(values):
    return sum(values) / len(values)

def on_bar(ctx, p):
    closes = [bar.close for bar in ctx.history]
    index = len(closes) - 1
    if index < p["slow"]:
        return []
    fast_now = mean(closes[index - p["fast"] + 1: index + 1])
    fast_prev = mean(closes[index - p["fast"]: index])
    slow_now = mean(closes[index - p["slow"] + 1: index + 1])
    slow_prev = mean(closes[index - p["slow"]: index])
    golden = fast_prev <= slow_prev and fast_now > slow_now
    death = fast_prev >= slow_prev and fast_now < slow_now
    if golden and ctx.position.is_flat:
        return [Signal(Side.BUY, reason=f"ma_cross:golden MA{p['fast']}/MA{p['slow']}")]
    if death and not ctx.position.is_flat:
        return [Signal(Side.SELL, reason=f"ma_cross:death MA{p['fast']}/MA{p['slow']}")]
    return []
'''


# ── 正常路径 ──────────────────────────────────────────────────────────────


def test_runs_user_strategy_end_to_end(data_dir: Path) -> None:
    outcome = run_source(MA_USER_SOURCE, data_dir, name="我的双均线")
    meta = outcome.report["meta"]
    assert meta["strategy_kind"] == "user" and meta["strategy_name"] == "我的双均线"
    assert meta["strategy"] == "user" and meta["bars"] == BARS
    assert outcome.report["metrics"]["trade_count"] > 0
    assert outcome.duration_s > 0


def test_defaults_fill_params_without_request(data_dir: Path) -> None:
    """`PARAMS` 的 default 在子侧填满：不传参数也能跑，且策略拿到的一定是完整字典。"""
    source = (
        'PARAMS = {"fast": {"type": "int", "default": 5}}\n'
        "def on_bar(ctx, p):\n    assert p['fast'] == 5\n    return []\n"
    )
    assert run_source(source, data_dir).report["meta"]["bars"] == BARS


def test_report_matches_builtin_strategy_point_by_point(data_dir: Path) -> None:
    """用户源码版与注册表版**逐点一致**——等价性测试的雏形（M4b 的模板库沿用同法）。

    一份证据同时证明两件事：用户 API 的表达力与内置策略等价、沙箱执行没有偷偷改口径。
    """
    from app.backtest.strategies import MaCross

    params = {"fast": 5, "slow": 20}
    user = run_source(MA_USER_SOURCE, data_dir, params=params).report
    builtin = build_report(
        BacktestConfig(
            symbol=SYMBOL,
            strategy=MaCross.name,
            start=START,
            end=START + timedelta(days=BARS - 1),
            pit_mode=Mode.PIT,
            params=params,
            data_dir=data_dir,
        )
    )
    for report in (user, builtin):
        report["meta"].pop("strategy_kind")
        report["meta"].pop("strategy_name")
        report["meta"].pop("strategy")
    assert user == builtin


# ── 用户要改的问题：走结构化拒绝，不是崩溃 ────────────────────────────────


def test_rejects_syntax_error_with_line(data_dir: Path) -> None:
    with pytest.raises(StrategyRejected) as info:
        run_source("def on_bar(ctx)\n    return []\n", data_dir)
    assert info.value.line == 1 and "语法错误" in str(info.value)


def test_rejects_runtime_exception_with_bar_date(data_dir: Path) -> None:
    source = "def on_bar(ctx):\n    return 1 / 0\n"
    with pytest.raises(StrategyRejected) as info:
        run_source(source, data_dir)
    message = str(info.value)
    assert "ZeroDivisionError" in message and "2026-01-05" in message
    assert info.value.line == 2


def test_rejects_forbidden_import(data_dir: Path) -> None:
    """数据绕行（唯一真实的泄漏路径）在运行前就被白名单挡住，报错带行号。"""
    source = "import duckdb\ndef on_bar(ctx):\n    return []\n"
    with pytest.raises(StrategyRejected) as info:
        run_source(source, data_dir)
    assert "不允许 import" in str(info.value) and info.value.line == 1


def test_user_validate_params_hook_blocks_run(data_dir: Path) -> None:
    with pytest.raises(StrategyRejected) as info:
        run_source(MA_USER_SOURCE, data_dir, params={"fast": 20, "slow": 5})
    assert "快线须小于慢线" in str(info.value)


def test_unknown_param_key_is_rejected(data_dir: Path) -> None:
    with pytest.raises(StrategyRejected) as info:
        run_source(MA_USER_SOURCE, data_dir, params={"min_score": 50})
    assert "不接受参数" in str(info.value)


def test_escape_hatches_are_closed(data_dir: Path) -> None:
    """`sys` / `os` 不在白名单里 —— 用户连 `sys.exit` 的入口都拿不到，谈不上把子进程搞哑。

    （子进程侧仍留着 `except BaseException` 的兜底，那是防我们自己出错的，不是防用户的。）
    """
    for source, keyword in (
        ("import sys\ndef on_bar(ctx):\n    sys.exit(0)\n", "sys"),
        ("import os\ndef on_bar(ctx):\n    os._exit(0)\n", "os"),
    ):
        with pytest.raises(StrategyRejected) as info:
            run_source(source, data_dir)
        assert "不允许 import" in str(info.value) and keyword in str(info.value)


# ── 配额：坏代码的代价关在子进程里 ────────────────────────────────────────


def test_cpu_limit_kills_endless_loop(data_dir: Path) -> None:
    limits = SandboxLimits(wall_seconds=30.0, cpu_seconds=2, memory_mb=512)
    with pytest.raises(SandboxError) as info:
        run_source("def on_bar(ctx):\n    while True:\n        pass\n", data_dir, limits=limits)
    assert info.value.kind == "cpu"


def test_wall_timeout_is_the_backstop(data_dir: Path) -> None:
    """墙钟兜底：CPU 上限调高，强制走到父进程这一层（真实场景里对应卡在不吃 CPU 的等待）。"""
    limits = SandboxLimits(wall_seconds=1.5, cpu_seconds=600, memory_mb=512)
    with pytest.raises(SandboxError) as info:
        run_source("def on_bar(ctx):\n    while True:\n        pass\n", data_dir, limits=limits)
    assert info.value.kind == "wall"


def test_memory_watchdog_kills_big_allocation(data_dir: Path) -> None:
    """内存看门狗：macOS 上 rlimit 设不下去（SPEC §5 实测），这一层是唯一的内存闸门。

    子进程基线约 48MB，上限压到 200MB 后分配 400MB 必触发。
    """
    source = "def on_bar(ctx):\n    x = bytearray(400 * 1024 * 1024)\n    return []\n"
    limits = SandboxLimits(wall_seconds=30.0, cpu_seconds=30, memory_mb=200)
    with pytest.raises(SandboxError) as info:
        run_source(source, data_dir, limits=limits)
    assert info.value.kind == "memory"
    assert EXIT_MEMORY == 137


def test_stdout_overflow_is_killed(data_dir: Path) -> None:
    """stdout 是协议通道：信封只有一行、远小于上限，超限即杀（正常报告 0.2MB 左右）。"""
    limits = SandboxLimits(wall_seconds=30.0, cpu_seconds=30, memory_mb=512, output_bytes=200)
    with pytest.raises(SandboxError) as info:
        run_source(MA_USER_SOURCE, data_dir, limits=limits)
    assert info.value.kind == "output"


def test_noisy_print_is_truncated_and_run_survives(data_dir: Path) -> None:
    """用户 print 走 stderr：量再大也只截断，不杀进程、更不污染协议通道。"""
    source = (
        "def on_bar(ctx):\n"
        "    print('调试信息' * 20)\n"
        "    return []\n"
    )
    limits = SandboxLimits(wall_seconds=30.0, cpu_seconds=30, memory_mb=512, stderr_bytes=1024)
    outcome = run_source(source, data_dir, limits=limits)
    assert outcome.report["meta"]["bars"] == BARS  # 报告完好
    assert 0 < len(outcome.stderr_tail.encode()) <= 1024


def test_service_still_healthy_after_faults(data_dir: Path) -> None:
    """故障注入之后，同一个父进程仍能正常跑——这就是「不拖垮服务」的直接证据。"""
    for source in (
        "def on_bar(ctx):\n    while True:\n        pass\n",
        "def on_bar(ctx):\n    x = bytearray(400 * 1024 * 1024)\n    return []\n",
        "def on_bar(ctx):\n    return 1 / 0\n",
    ):
        limits = SandboxLimits(wall_seconds=2.0, cpu_seconds=2, memory_mb=200)
        with pytest.raises((SandboxError, StrategyRejected)):
            run_source(source, data_dir, limits=limits)
    assert run_source(MA_USER_SOURCE, data_dir).report["meta"]["bars"] == BARS


# ── 配置往返 ──────────────────────────────────────────────────────────────


def test_config_payload_round_trip(data_dir: Path) -> None:
    from app.backtest.costs import CostModel

    config = BacktestConfig(
        symbol=SYMBOL,
        strategy="user",
        start=START,
        end=START + timedelta(days=10),
        pit_mode=Mode.NON_PIT,
        params={"fast": 3.0},
        costs=CostModel(slippage_bps=12.0, fee_enabled=False),
        data_dir=data_dir,
    )
    assert config_from_payload(config_to_payload(config)) == config
