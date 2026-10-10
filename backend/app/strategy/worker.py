"""沙箱子进程入口（M4a，`python -m app.strategy.worker`）。

协议：stdin 收一行 JSON（源码 + 配置 + 配额 + 策略名），stdout 回**一行** JSON 信封：

    {"ok": true,  "report": {...}}      # kind 缺省 / "backtest"
    {"ok": true,  "result": {...}}      # kind = "paper"（M6 模拟盘重放）
    {"ok": false, "error": {"kind": "rejected|backtest|memory", "message": "...", "line": 12}}

三个实现要点：

- **用户的 `print` 不能污染协议通道**：装载与运行期间把 `sys.stdout` 指向 stderr，
  信封最后走保存下来的真句柄。用户不必知道这条规矩，随手 print 也不会毁掉报告
- **配额两层在子侧、一层在父侧**：CPU 走 `RLIMIT_CPU`（macOS 实测生效，SIGXCPU 如期送达）；
  内存走 `ru_maxrss` 峰值看门狗——**不用 rlimit**：macOS 上 `setrlimit(RLIMIT_AS/DATA)` 与
  `ulimit -v/-d` 都设不下去（实测见 SPEC §5），而 Linux 上虚拟地址空间动辄 >2GB，
  按 512MB 设会在 import 期就自杀——不按平台分叉，一律看看门狗；墙钟由父进程兜底
- **参数归一在子侧再确认一遍**：`PARAMS` 是静态可读的字面量，子进程据此填满缺省值，
  用户 `on_bar` 里可以放心 `p["fast"]`。父侧那道校验是为了**不 spawn 就能返回 422**，
  这里这道是保证策略拿到的字典一定完整

两类任务（回测 / 模拟盘）共用同一个入口与同一套配额：**M6 的整段重放跑在这里**，
父进程只收账户状态与决策日志（见 `app.paper.replay` 的模块 docstring）。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import replace
from datetime import date
from typing import Any

import resource  # noqa: S404 - 只用于给自己设配额，不碰用户输入

from app.backtest.report import build_report
from app.backtest.types import BacktestError
from app.paper import PaperError
from app.paper.replay import replay
from app.paper.store import (
    config_from_payload as paper_config_from_payload,
)
from app.paper.store import (
    decision_from_payload,
    result_to_payload,
)
from app.paper.types import DecisionStatus
from app.strategy import USER_STRATEGY, SandboxLimits, StrategyRejected
from app.strategy.api import format_traceback, load_strategy, parse_meta
from app.strategy.params import validate_params
from app.strategy.sandbox import EXIT_MEMORY, config_from_payload

#: 看门狗轮询间隔。0.05s 对「峰值」语义够用：`ru_maxrss` 单调不减，
#: 一次瞬时大分配即使发生在上一次轮询之间，下一次轮询照样看得见。
WATCHDOG_INTERVAL = 0.05


def _die(code: int, message: str) -> None:
    """直接写 stderr 后 `os._exit`：绕过 Python 清理，避免死在半路又留下半个信封。"""
    try:
        os.write(2, f"[sandbox] {message}\n".encode())
    finally:
        os._exit(code)


def _install_cpu_limit(cpu_seconds: int) -> None:
    try:
        # 硬限比软限多 2s：软限到了先 SIGXCPU（默认动作即终止），万一卡在 C 扩展里不响应，
        # 硬限再 SIGKILL 兜底
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 2))
    except (ValueError, OSError) as exc:  # pragma: no cover - 正常机器不会走到
        print(f"[sandbox] CPU 配额设置失败（{exc}）：仍由父进程墙钟兜底", file=sys.stderr)


def _start_memory_watchdog(memory_mb: int) -> None:
    limit_bytes = memory_mb * 1024 * 1024

    def watch() -> None:
        while True:
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # macOS 返回字节、Linux 返回 KB —— 单位不归一会让守门形同虚设
            peak_bytes = peak if sys.platform == "darwin" else peak * 1024
            if peak_bytes > limit_bytes:
                _die(
                    EXIT_MEMORY,
                    f"内存峰值 {peak_bytes / 1024 / 1024:.0f}MB 超过上限 {memory_mb}MB，已终止",
                )
            time.sleep(WATCHDOG_INTERVAL)

    threading.Thread(target=watch, name="sandbox-memory-watchdog", daemon=True).start()


def install_limits(limits: SandboxLimits) -> None:
    _install_cpu_limit(limits.cpu_seconds)
    _start_memory_watchdog(limits.memory_mb)


def _rejected(message: str, line: int | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"kind": "rejected", "message": message}
    if line:
        error["line"] = line
    return {"ok": False, "error": error}


def _execute(payload: dict[str, Any]) -> dict[str, Any]:
    source = payload["source"]
    name = str(payload.get("name") or USER_STRATEGY)
    try:
        meta = parse_meta(source)
        config = replace(
            config_from_payload(payload["config"]),
            params=validate_params(meta.params, payload["config"].get("params") or {}),
        )
        strategy = load_strategy(source, name=name, params=config.params)
        errors = strategy.check_values(config.params)
        if errors:
            return _rejected("；".join(errors))
        report = build_report(
            config,
            compare_pit=bool(payload.get("compare_pit")),
            # 每遍新建实例：用户代码可以在模块级持状态，复用会让第二遍带上第一遍的残留
            strategy_factory=lambda: load_strategy(source, name=name, params=config.params),
            strategy_name=name,
            uses_events=bool(payload.get("uses_events", meta.uses_events)),
        )
        return {"ok": True, "report": report}
    except StrategyRejected as exc:
        # 面向人的报错已在 message 里（带 bar 日期与行号），不再泼一遍栈
        return _rejected(str(exc), line=exc.line)
    except MemoryError:
        return {"ok": False, "error": {"kind": "memory", "message": "策略内存不足（MemoryError）"}}
    except BacktestError as exc:
        # 数据层问题（区间无 bar 一类）：父侧通常已拦，这里是兜底，别谎报成用户的错
        return {"ok": False, "error": {"kind": "backtest", "message": str(exc)}}
    except BaseException as exc:  # noqa: BLE001 - 含 SystemExit / KeyboardInterrupt
        print(format_traceback(exc), file=sys.stderr)
        return _rejected(f"策略执行时发生 {type(exc).__name__}: {exc}")


def _execute_paper(payload: dict[str, Any]) -> dict[str, Any]:
    """模拟盘重放（M6）：**整段重放**在这里跑，父进程只收账户状态与决策日志。

    与 `_execute` 的差别只有产物：那边回一份报告，这边回账户状态 + 决策日志 + 净值曲线。
    闸门语义全在 `app.paper.replay` 里，两侧共用同一份实现——回测与模拟盘的口径不会分叉。
    """
    source = payload["source"]
    name = str(payload.get("name") or USER_STRATEGY)
    try:
        meta = parse_meta(source)
        raw = payload["config"]
        config = replace(
            paper_config_from_payload(raw),
            params=validate_params(meta.params, raw.get("params") or {}),
        )
        strategy = load_strategy(source, name=name, params=config.params)
        errors = strategy.check_values(config.params)
        if errors:
            return _rejected("；".join(errors))
        result = replay(
            config,
            [decision_from_payload(d) for d in payload.get("decisions") or []],
            through=date.fromisoformat(payload["through"]) if payload.get("through") else None,
            auto=DecisionStatus(payload["auto"]) if payload.get("auto") else None,
            strategy=strategy,
            account_id=str(payload.get("account_id") or ""),
        )
        return {"ok": True, "result": result_to_payload(result)}
    except StrategyRejected as exc:
        return _rejected(str(exc), line=exc.line)
    except MemoryError:
        return {"ok": False, "error": {"kind": "memory", "message": "策略内存不足（MemoryError）"}}
    except (BacktestError, PaperError) as exc:
        # 数据层问题（区间无 bar、标的缺数据一类）：父侧通常已拦，这里是兜底，别谎报成用户的错
        return {"ok": False, "error": {"kind": "backtest", "message": str(exc)}}
    except BaseException as exc:  # noqa: BLE001 - 含 SystemExit / KeyboardInterrupt
        print(format_traceback(exc), file=sys.stderr)
        return _rejected(f"策略执行时发生 {type(exc).__name__}: {exc}")


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
    except ValueError as exc:
        print(f"worker: 输入不是合法 JSON：{exc}", file=sys.stderr)
        return 2

    real_stdout = sys.stdout
    sys.stdout = sys.stderr  # 用户的 print 默认走 stderr，协议通道留给信封
    try:
        install_limits(SandboxLimits(**payload["limits"]))
        envelope = _execute_paper(payload) if payload.get("kind") == "paper" else _execute(payload)
    finally:
        sys.stdout = real_stdout

    # ensure_ascii 保持默认 True：信封是纯 ASCII，跨 locale 不会在编码上翻车
    real_stdout.write(json.dumps(envelope) + "\n")
    real_stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
