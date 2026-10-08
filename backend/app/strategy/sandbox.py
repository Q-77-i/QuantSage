"""沙箱执行器（父侧，M4a）。

一次用户策略回测 = 一个子进程：

    parent ──stdin（一行 JSON：源码 + 配置 + 配额 + 策略名）──▶ worker
    parent ◀──stdout（一行 JSON 信封：报告或结构化错误）────── worker

**为什么不在 API 进程里跑**：用户代码可以死循环、吃内存、抛异常、`sys.exit`——进程内跑，
随便一样都能拖垮整个服务。子进程 + 三层配额把「坏代码」的代价关在一个可回收的进程里。

**为什么源码走 stdin 而不是 argv**：argv 对同机任何用户都可见（`ps`）。

**配额三层**（SPEC §5）：CPU（`RLIMIT_CPU`，macOS 实测生效）、内存（子进程内 `ru_maxrss`
看门狗——macOS 的 `RLIMIT_AS`/`DATA` 设不下去，实测见 SPEC）、墙钟（父进程 `wait_for` + 杀进程组）。

父侧只做「读 JSON、映射错误码」，报告仍由子进程里的 `build_report` 产出——与内置策略同一份代码。
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from app.backtest.costs import CostModel
from app.backtest.engine import BacktestConfig
from app.backtest.types import Mode
from app.core.config import get_settings
from app.strategy import SandboxError, SandboxLimits, StrategyRejected

#: 子进程内存看门狗触发时的退出码（128 + SIGKILL，沿用惯例）
EXIT_MEMORY = 137

#: backend/ 目录：子进程的 cwd，保证 `python -m app.strategy.worker` 能 import 到 app 包
BACKEND_DIR = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class SandboxOutcome:
    report: dict[str, Any]
    duration_s: float
    stderr_tail: str = ""


def default_limits() -> SandboxLimits:
    settings = get_settings()
    return SandboxLimits(
        wall_seconds=settings.strategy_wall_seconds,
        cpu_seconds=settings.strategy_cpu_seconds,
        memory_mb=settings.strategy_memory_mb,
        output_bytes=settings.strategy_output_bytes,
    )


def config_to_payload(config: BacktestConfig) -> dict[str, Any]:
    """`BacktestConfig` → 可 JSON 化的 dict（含 CostModel 的七个字段）。"""
    return {
        "symbol": config.symbol,
        "strategy": config.strategy,
        "start": config.start.isoformat() if config.start else None,
        "end": config.end.isoformat() if config.end else None,
        "adjust": config.adjust,
        "initial_cash": config.initial_cash,
        "pit_mode": Mode(config.pit_mode).value,
        "params": dict(config.params),
        "data_dir": str(config.data_dir) if config.data_dir else None,
        "costs": {
            "commission_rate": config.costs.commission_rate,
            "commission_min": config.costs.commission_min,
            "stamp_tax_rate": config.costs.stamp_tax_rate,
            "slippage_bps": config.costs.slippage_bps,
            "fee_enabled": config.costs.fee_enabled,
            "slippage_enabled": config.costs.slippage_enabled,
        },
    }


def config_from_payload(data: dict[str, Any]) -> BacktestConfig:
    """`config_to_payload` 的逆运算（子进程侧用）。"""
    return BacktestConfig(
        symbol=data["symbol"],
        strategy=data["strategy"],
        start=date.fromisoformat(data["start"]) if data.get("start") else None,
        end=date.fromisoformat(data["end"]) if data.get("end") else None,
        adjust=data.get("adjust", "qfq"),
        initial_cash=float(data.get("initial_cash", 1_000_000.0)),
        pit_mode=Mode(data.get("pit_mode", "pit")),
        params=dict(data.get("params") or {}),
        data_dir=Path(data["data_dir"]) if data.get("data_dir") else None,
        costs=CostModel(**data["costs"]),
    )


def _kill(proc: asyncio.subprocess.Process) -> None:
    """杀整个进程组：子进程自己也可能 fork（`start_new_session=True` 让它自成一组）。"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):  # pragma: no cover - 已退出即无事可做
        pass
    if proc.returncode is None:  # pragma: no cover - getpgid 失败时的兜底
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def _pump(
    stream: asyncio.StreamReader,
    cap: int,
    sink: bytearray,
    on_overflow: Any = None,
) -> None:
    """把一路流读进 `sink`，超过 `cap` 就停止累积。

    stdout 超限时 `on_overflow` 会杀子进程——但**仍然继续读**：停止读取会让管道写满、
    子进程卡在写上不退出，杀也杀不利索。stderr 超限只丢内容，不杀（它是诊断通道）。
    """
    total = 0
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return
        # 截断要截在**字节**上：`read()` 一次可能返回远超 cap 的块，
        # 按「整块收或不收」判会把该留下的前 cap 字节一起丢掉（实测踩过）
        room = cap - total
        if room > 0:
            sink.extend(chunk[:room])
        total += len(chunk)
        if total > cap and on_overflow is not None:
            on_overflow()
            on_overflow = None


def _parse_envelope(raw: bytes) -> dict[str, Any] | None:
    """取 stdout 的**最后一行非空**做 JSON 解析。

    正常时 stdout 只有一行；留「最后一行」是为了容忍意外写入（协议仍然只有一个信封）。
    """
    for line in reversed(raw.decode("utf-8", "replace").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            return None
        return data if isinstance(data, dict) and "ok" in data else None
    return None


async def run_user_strategy(
    source: str,
    *,
    config: BacktestConfig,
    strategy_name: str | None = None,
    uses_events: bool = False,
    compare_pit: bool = False,
    limits: SandboxLimits | None = None,
) -> SandboxOutcome:
    """把用户策略放进沙箱子进程跑一次回测。

    返回报告 dict；用户要改的问题抛 `StrategyRejected`（4xx），配额与执行层问题抛
    `SandboxError`（带 `kind`）。调用方负责把 `StrategyRejected` 里的 `line` 交给编辑器。
    """
    limits = limits or default_limits()
    payload = json.dumps(
        {
            "source": source,
            "name": strategy_name or config.strategy,
            "uses_events": uses_events,
            "compare_pit": compare_pit,
            "config": config_to_payload(config),
            "limits": {
                "wall_seconds": limits.wall_seconds,
                "cpu_seconds": limits.cpu_seconds,
                "memory_mb": limits.memory_mb,
                "output_bytes": limits.output_bytes,
                "stderr_bytes": limits.stderr_bytes,
            },
        }
    ).encode("utf-8")

    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "app.strategy.worker",
            cwd=str(BACKEND_DIR),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # 自成进程组：超时后能连子孙一起收
        )
    except OSError as exc:  # pragma: no cover - 解释器缺失一类
        raise SandboxError(f"沙箱子进程启动失败：{exc}", kind="crash") from exc

    stdout, stderr = bytearray(), bytearray()
    killed_for_output = False

    def on_output_overflow() -> None:
        nonlocal killed_for_output
        killed_for_output = True
        _kill(proc)

    pumps = asyncio.gather(
        _pump(proc.stdout, limits.output_bytes, stdout, on_output_overflow),  # type: ignore[arg-type]
        _pump(proc.stderr, limits.stderr_bytes, stderr),  # type: ignore[arg-type]
    )
    try:
        assert proc.stdin is not None
        proc.stdin.write(payload)
        await proc.stdin.drain()
        proc.stdin.close()
    except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - 子进程早退
        pass

    try:
        await asyncio.wait_for(pumps, timeout=limits.wall_seconds)
        await proc.wait()
    except TimeoutError:
        _kill(proc)
        await proc.wait()
        raise SandboxError(
            f"策略执行超过墙钟上限 {limits.wall_seconds:g}s，已终止（死循环或超长计算）",
            kind="wall",
        ) from None

    duration = time.monotonic() - started
    tail = stderr.decode("utf-8", "replace").strip()
    returncode = proc.returncode

    if killed_for_output:
        raise SandboxError(
            f"策略的 stdout 输出超过 {limits.output_bytes} 字节上限，已终止"
            "（stdout 是沙箱协议通道，调试信息请用 print——它默认走 stderr）",
            kind="output",
        )
    if returncode == EXIT_MEMORY:
        raise SandboxError(
            f"策略内存峰值超过 {limits.memory_mb}MB 上限，已终止", kind="memory"
        )

    envelope = _parse_envelope(stdout)
    if envelope is None:
        if returncode == -signal.SIGXCPU:
            raise SandboxError(
                f"策略 CPU 时间超过 {limits.cpu_seconds}s 上限，已终止", kind="cpu"
            )
        detail = f"：{tail[-400:]}" if tail else ""
        raise SandboxError(
            f"沙箱子进程异常退出（code={returncode}）{detail}", kind="crash"
        )

    if not envelope.get("ok"):
        error = envelope.get("error") or {}
        message = str(error.get("message") or "策略执行失败")
        if error.get("kind") == "rejected":
            line = error.get("line")
            raise StrategyRejected(message, line=int(line) if line else None)
        raise SandboxError(message, kind=str(error.get("kind") or "crash"))

    return SandboxOutcome(
        report=envelope["report"], duration_s=duration, stderr_tail=tail[-2000:]
    )


def run_user_strategy_sync(
    source: str,
    *,
    config: BacktestConfig,
    strategy_name: str | None = None,
    uses_events: bool = False,
    compare_pit: bool = False,
    limits: SandboxLimits | None = None,
) -> SandboxOutcome:
    """同步包装：CLI 脚本与离线用例用（API 走 async 版，不阻塞事件循环）。"""
    return asyncio.run(
        run_user_strategy(
            source,
            config=config,
            strategy_name=strategy_name,
            uses_events=uses_events,
            compare_pit=compare_pit,
            limits=limits,
        )
    )


__all__ = [
    "BACKEND_DIR",
    "EXIT_MEMORY",
    "SandboxOutcome",
    "config_from_payload",
    "config_to_payload",
    "default_limits",
    "run_user_strategy",
    "run_user_strategy_sync",
]
