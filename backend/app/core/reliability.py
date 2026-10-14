"""可靠性策略：重试 + 熔断，一套对象、多个依赖共用（E4 / SPEC §5）。

**三层生命周期分明**（别把它们混成一层，这是本模块的枢纽）：

* **进程级**——熔断状态机，每依赖一份，进程内单例（`get_dep`）。重启清零是如实语义：
  限流/熔断状态不落盘、不绑 Redis（关键路径不绑外部组件，PRD 定）。
* **运行级**——`Budget`（四计数器）仍归深路径的 `RoleContext` 所有，这里**只引用不复制**。
* **调用级**——尝试次数与退避时长，是 `guard()` 的栈上局部量。

**错误分类是纯的**：`classify()` 只看异常的结构属性（`status_code` / `retry_after`），
不 import litellm——实测它家所有异常都带 `status_code`（Timeout 408、APIConnectionError 500、
RateLimitError 429、AuthenticationError 401…），小石的 `RateLimited` 则带 `retry_after`。
依赖结构而非类型，换来的是这一层不认识任何上游 SDK。

**红线**：`rate_limited` **立刻开路、也不参与连续失败计数**——它是平台在叫我们退后，
不是依赖故障。「429 按 Retry-After 停」由此从「每个调用点自觉」变成策略层强制。

**不做插件框架**（PRD 定）：四个依赖、两个策略类、一个 `guard()`。也不引 `tenacity`——
它表达不了「读 `Retry-After` 并对其封顶」这条语义（`langchain-litellm` 自带的那层就没读）。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

#: 一次失败的处置分类。`cancelled` 与 `permanent` 都**不重试**，区别在要不要向上抛。
Kind = Literal["retryable", "permanent", "rate_limited", "cancelled"]

#: 这些状态码值得再试一把（5xx 与「服务端说现在不行」的几种 4xx）
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429})


class CircuitOpen(RuntimeError):
    """依赖熔断中——**没发起调用**就拒了。`retry_after` 是建议的等待秒数。"""

    def __init__(self, name: str, retry_after: float) -> None:
        self.name = name
        self.retry_after = max(0.0, retry_after)
        super().__init__(f"「{name}」熔断中，约 {self.retry_after:.0f}s 后可再试")


def retry_after_of(exc: BaseException) -> float | None:
    """异常是否表达了「平台要求退后多少秒」。

    两个来源：小石那句 `RateLimited.retry_after`（属性），与 HTTP 的 `Retry-After` 头
    （litellm 的 429 自带 `response.headers`）。读不出来就返回 None——**不猜**。
    """
    raw = getattr(exc, "retry_after", None)
    if raw is None:
        headers = getattr(getattr(exc, "response", None), "headers", None)
        if headers is not None:
            try:
                raw = headers.get("retry-after")
            except Exception:  # noqa: BLE001 —— 头取不到就当没给
                raw = None
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None  # HTTP-date 形态之类：不解析，交给退避
    return seconds if seconds >= 0 else None


def classify(exc: BaseException) -> Kind:
    """把异常归到四类之一。**只认结构，不认类型**（见模块 docstring）。

    `CircuitOpen` 归 `permanent`：它是**闸门自己抛的**，重试没有意义（闸还开着）。
    注意它身上也有 `retry_after` 属性（给调用方做 `Retry-After` 头用），所以这一条
    必须**排在 `retry_after_of` 之前**——否则会被结构化成 `rate_limited`。
    降级链那层问的是另一个问题（「换个档还能不能成」），另有判断，见 `llm._with_fallback`。
    """
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    if isinstance(exc, CircuitOpen):
        return "permanent"
    if retry_after_of(exc) is not None:
        return "rate_limited"
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return "retryable"
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        return "permanent"  # 不认识的异常不盲重试——宁可如实失败
    if status == 429:
        return "rate_limited"
    if status >= 500 or status in _RETRYABLE_STATUS:
        return "retryable"
    return "permanent"


@dataclass(frozen=True)
class Retry:
    """一个依赖的重试预算。`max_attempts` 是**总尝试次数**，不是「重试次数」。

    这个措辞是被坑出来的：`langchain-litellm` 的装饰器写的是
    `stop_after_attempt(max_retries)`，默认 `max_retries=1` ⇒ 1 次尝试 = 压根不重试
    （实测 1→1 次调用、3→3 次）。这里从名字上就不给自己留这个歧义。
    """

    max_attempts: int = 3
    base: float = 0.5
    factor: float = 2.0
    cap: float = 8.0
    jitter: float = 0.25
    #: 尝试累计时长的硬顶——防「超时 × 次数」相乘把节点预算顶穿（SPEC §5.2）
    max_total_seconds: float = 90.0
    #: 是否听平台的 `Retry-After`
    respect_retry_after: bool = True
    #: 超过这个秒数就不等了、直接降级（44 分钟的 Retry-After 不该占着连接）
    max_retry_after: float = 30.0


@dataclass(frozen=True)
class Circuit:
    """熔断阈值。`threshold` 数的是**连续**失败（成功即清零）。"""

    threshold: int = 5
    cooldown: float = 30.0
    #: 半开时允许多少个探测（并发场景下用；默认 1）
    probes: int = 1


@dataclass
class Dependency:
    """一个被保护的依赖：策略（不可变）+ 活状态（可变）。"""

    name: str
    retry: Retry = field(default_factory=Retry)
    circuit: Circuit = field(default_factory=Circuit)
    #: 可注入——测试用假时钟推进「冷却」，不必真睡
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    #: 同步路径（`ChatLiteLLM` 的同步 `completion_with_retry`）用的那一对
    tsleep: Callable[[float], None] = time.sleep
    # ── 活状态 ──
    state: str = "closed"  # closed / open / half_open
    failures: int = 0
    opened_at: float = 0.0
    probes_left: int = 0

    def allow(self, now: float) -> float | None:
        """闸门：放行返回 None，熔断中返回「还要等多少秒」。

        半开时按 `probes` 放行探测；探测名额用完的并发调用按冷却时长劝退。
        """
        if self.state == "closed":
            return None
        if self.state == "half_open":
            if self.probes_left > 0:
                self.probes_left -= 1
                return None
            return self.circuit.cooldown
        waited = now - self.opened_at
        if waited < self.circuit.cooldown:
            return self.circuit.cooldown - waited
        self.state = "half_open"
        self.probes_left = self.circuit.probes - 1  # 本次就是第一个探测
        return None

    def record_success(self) -> None:
        self.state = "closed"
        self.failures = 0
        self.opened_at = 0.0
        self.probes_left = 0

    def record_failure(self, now: float) -> None:
        """连续失败 +1；到阈值（或半开探测失败）即开路。"""
        self.failures += 1
        if self.state == "half_open" or self.failures >= self.circuit.threshold:
            self.trip(now)

    def trip(self, now: float) -> None:
        """立刻开路并重置冷却起算点。"""
        if self.state != "open":
            log.warning("依赖「%s」熔断打开（连续失败 %d 次）", self.name, self.failures)
        self.state = "open"
        self.opened_at = now
        self.probes_left = 0


#: 进程级注册表。单机单进程的既定形态（同 E1a 的单实例假设）。
_DEPS: dict[str, Dependency] = {}


def get_dep(name: str) -> Dependency:
    dep = _DEPS.get(name)
    if dep is None:
        dep = _DEPS[name] = Dependency(name=name)
    return dep


def install(dep: Dependency) -> Dependency:
    """装一个依赖（测试注入假时钟用；运行时如需按 settings 调参也走这里）。"""
    _DEPS[dep.name] = dep
    return dep


def reset(*names: str) -> None:
    """清掉指定（或缺省全部）依赖——**测试用**，避免用例之间串状态。"""
    if not names:
        _DEPS.clear()
        return
    for name in names:
        _DEPS.pop(name, None)


def snapshot() -> dict[str, dict[str, Any]]:
    """只读状态快照（日志 / 看板 / 用例断言）。"""
    return {
        name: {
            "state": dep.state,
            "failures": dep.failures,
            "opened_at": dep.opened_at,
        }
        for name, dep in _DEPS.items()
    }


def _backoff(retry: Retry, attempt: int) -> float:
    delay = min(retry.cap, retry.base * retry.factor ** (attempt - 1))
    if retry.jitter:
        delay *= 1.0 + random.uniform(-retry.jitter, retry.jitter)
    return max(0.0, delay)


def _wait_plan(dep: Dependency, kind: Kind, exc: BaseException, attempt: int, started: float) -> float | None:
    """还要不要再试一次？要的话等几秒。`None` = 不重试。"""
    if kind not in ("retryable", "rate_limited"):
        return None
    if attempt >= dep.retry.max_attempts:
        return None
    elapsed = dep.clock() - started
    if elapsed >= dep.retry.max_total_seconds:
        return None

    delay: float | None = None
    if kind == "rate_limited":
        asked = retry_after_of(exc)
        if asked is not None:
            if not dep.retry.respect_retry_after or asked > dep.retry.max_retry_after:
                return None  # 平台让等太久：不占着，交给降级
            delay = asked
    if delay is None:
        delay = _backoff(dep.retry, attempt)
    if elapsed + delay > dep.retry.max_total_seconds:
        return None  # 等完就顶穿了预算，不如现在收手
    return delay


def _on_failure(dep: Dependency, exc: BaseException, attempt: int, started: float) -> float | None:
    """失败之后：记账（熔断计数）并决定「还要不要再来一次、等几秒」。

    异步与同步两条循环共用这一处——**决策只有一份**，两边不会走样。
    调用方负责在返回 None 时把 `exc` 原样抛出。
    """
    kind = classify(exc)
    if kind == "cancelled":
        return None
    if kind == "rate_limited":
        dep.trip(dep.clock())  # 红线：一次就开路，不等攒够
    elif kind == "retryable":
        dep.record_failure(dep.clock())  # permanent 不计数：那是请求的问题，不是依赖的状态
    return _wait_plan(dep, kind, exc, attempt, started)


async def guard(name: str, fn: Callable[[], Awaitable[T]]) -> T:
    """在策略下跑一次调用：熔断闸 → 尝试 → 失败分类 → 退避重试。

    **熔断只拦新调用，不打断进行中的重试**——重试自己由 `max_total_seconds` 兜底。
    这样语义简单：一次 `guard()` 的行为只取决于它自己的尝试序列。

    失败时把**最后一个异常原样抛出**（调用方据此决定降级还是如实报错）。
    """
    dep = get_dep(name)
    now = dep.clock()
    wait = dep.allow(now)
    if wait is not None:
        raise CircuitOpen(name, wait)

    started = now
    attempt = 0
    while True:
        attempt += 1
        try:
            result = await fn()
        except BaseException as exc:  # noqa: BLE001 —— 分类后再决定，分类函数是纯的
            delay = _on_failure(dep, exc, attempt, started)
            if delay is None:
                raise
            log.warning(
                "「%s」第 %d 次失败（%s：%s），%.2fs 后重试",
                name, attempt, type(exc).__name__, exc, delay,
            )
            await dep.sleep(delay)
        else:
            dep.record_success()
            return result


def guard_sync(name: str, fn: Callable[[], T]) -> T:
    """`guard` 的同步孪生（`ChatLiteLLM` 的同步路径用）。语义逐条相同。"""
    dep = get_dep(name)
    now = dep.clock()
    wait = dep.allow(now)
    if wait is not None:
        raise CircuitOpen(name, wait)

    started = now
    attempt = 0
    while True:
        attempt += 1
        try:
            result = fn()
        except BaseException as exc:  # noqa: BLE001
            delay = _on_failure(dep, exc, attempt, started)
            if delay is None:
                raise
            log.warning(
                "「%s」第 %d 次失败（%s：%s），%.2fs 后重试",
                name, attempt, type(exc).__name__, exc, delay,
            )
            dep.tsleep(delay)
        else:
            dep.record_success()
            return result
