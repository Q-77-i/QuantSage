"""用户策略与沙箱（M4）：把「前视偏差为零」从数据层延伸到用户代码层。

**主闸门是结构，不是静态检查**：用户策略只拿到 `ctx`——`history` 是截断到当前 bar 的元组、
`events` 已过 PIT 闸门，命名空间里没有 pandas / duckdb / 文件与网络。未来数据在 API 形状上
不可达，与内置策略是同一份保证。此处的 import 白名单是**正确性闸门**：它拦的是唯一真实的
泄漏路径「读全量 Parquet 自己索引未来」，不是可选加固。

**它不是安全边界**：同机同用户、无容器，恶意作者仍可绕（`object.__subclasses__()` 一类）。
目标是可用性（坏代码不拖垮服务）与阻断无意 / 半有意的绕行；容器级隔离留 P3-E8（SPEC §5）。

分层：

- `api.py`     契约层：命名空间白名单、`PARAMS` / `USES_EVENTS` 的**静态**解析（AST，不执行代码）、
               `on_bar` 装载与适配。编辑器与父进程都靠静态解析——不跑用户代码就能拿到参数 schema
- `params.py`  参数层：`PARAMS` schema 校验与取值归一（纯函数）
- `sandbox.py` 执行层（父侧）：spawn 子进程、三层配额、输出上限、错误码映射
- `worker.py`  执行层（子侧）：stdin 收源码与配置、装配额、跑 `build_report`、stdout 回 JSON
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

#: 用户策略在 `BacktestConfig.strategy` 里的占位名（真名在 `strategy_name` 里）。
#: 放进这里是为了让 API 层（M4c）、CLI 与沙箱共用同一个常量，不各写一份字面量。
USER_STRATEGY = "user"


def code_sha256(source: str) -> str:
    """策略源码的 sha256（十六进制）。

    回测记录存它，与策略**当前**代码的同一个 hash 比对，就能如实标注「已非当次运行的代码」
    （M4c 运行契约）。两处必须用同一个函数算——各写一份必然漂移。
    """
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


class StrategyError(RuntimeError):
    """策略层公共错误基类。"""


class StrategyRejected(StrategyError):
    """用户要改的问题：语法 / 入口 / 参数 / 运行期异常。

    `line` 是从 AST 或异常回溯里能拿到的行号（拿不到就是 None），供编辑器标注。
    """

    def __init__(self, message: str, *, line: int | None = None) -> None:
        super().__init__(message)
        self.line = line


class _HasSeverity(Protocol):
    severity: str


class StrategyCheckFailed(StrategyError):
    """静态检查有 `error`：拒绝执行（M4c 在回测提交前的闸门）。

    `findings` 随异常一起带出去、由 API 层放进 422 响应体——编辑器与检查面板直接吃它。
        `Finding` 的完整定义在 `static_check.py`，这里只按 `severity` 鸭子类型判，不反向依赖。
    """

    def __init__(self, findings: Sequence[_HasSeverity]) -> None:
        errors = sum(1 for item in findings if item.severity == "error")
        others = len(findings) - errors
        super().__init__(
            f"策略未通过前视静态检查：{errors} 个 error"
            + (f"（另有 {others} 个 warning）" if others else "")
        )
        self.findings = list(findings)


class SandboxError(StrategyError):
    """执行层的问题：配额、超时、崩溃、输出超限。

    `kind` 供上层给不同状态码与话术：`wall` 墙钟 / `cpu` CPU / `memory` 内存 /
    `output` 输出超限 / `crash` 子进程异常退出。
    """

    KINDS = ("wall", "cpu", "memory", "output", "crash")

    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    """三层配额（SPEC §5）。

    默认值由 `Settings` 传入；单独成类是为了让「父侧构造、子侧装配额」共用同一份定义。
    """

    wall_seconds: float = 20.0
    cpu_seconds: int = 15
    #: 子进程**总** RSS 上限（含引擎读数据的开销，真数据实测峰值 300–400MB，见 SPEC §5 M4a）
    memory_mb: int = 1024
    output_bytes: int = 2_000_000
    stderr_bytes: int = 16_384


__all__ = [
    "USER_STRATEGY",
    "SandboxError",
    "SandboxLimits",
    "StrategyCheckFailed",
    "StrategyError",
    "StrategyRejected",
    "code_sha256",
]
