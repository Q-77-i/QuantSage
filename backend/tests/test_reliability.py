"""E4a 统一 policy：错误分类 / 重试与退避 / 熔断三态。

**一律用假时钟**——熔断的「冷却后半开」若靠真等待，测试会又慢又脆。
`FakeClock.sleep` 把「睡」直接换成「把钟往前拨」，于是 `max_total_seconds` 这类
按墙钟算的硬顶也一并可测。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.core import reliability
from app.core.reliability import Circuit, CircuitOpen, Dependency, Retry, classify


class FakeClock:
    """可注入的时钟：`sleep` 即是「把钟拨快」。"""

    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def make_dep(**kwargs: Any) -> tuple[Dependency, FakeClock]:
    """装一个只对本次用例生效的依赖（时钟与睡眠都是假的，jitter 关掉保证可断言）。"""
    clock = FakeClock()
    retry = kwargs.pop("retry", Retry(jitter=0.0))
    circuit = kwargs.pop("circuit", Circuit())
    dep = Dependency(name="test:dep", retry=retry, circuit=circuit, clock=clock, sleep=clock.sleep, **kwargs)
    reliability.install(dep)
    return dep, clock


def http_error(status: int, *, retry_after: float | None = None) -> Exception:
    """造一个「像 litellm 那样」的异常：有 `status_code`，429 另带 `response.headers`。"""

    class Response:
        def __init__(self) -> None:
            self.headers = {"retry-after": str(retry_after)} if retry_after is not None else {}

    exc = Exception(f"HTTP {status}")
    exc.status_code = status  # type: ignore[attr-defined]
    exc.response = Response() if retry_after is not None else None  # type: ignore[attr-defined]
    return exc


class Boom(Exception):
    """小石那类「平台要求退后」的异常：带 `retry_after` 属性。"""

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("rate limited")
        self.retry_after = retry_after


@pytest.fixture(autouse=True)
def _clean_registry():
    reliability.reset()
    yield
    reliability.reset()


# ── classify：纯函数 ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "exc, expected",
    [
        (asyncio.CancelledError(), "cancelled"),
        (TimeoutError("慢"), "retryable"),
        (ConnectionError("断"), "retryable"),
        (http_error(408), "retryable"),
        (http_error(500), "retryable"),
        (http_error(503), "retryable"),
        (http_error(429), "rate_limited"),
        (http_error(429, retry_after=3), "rate_limited"),
        (Boom(44 * 60), "rate_limited"),
        (http_error(400), "permanent"),
        (http_error(401), "permanent"),
        (http_error(422), "permanent"),
        (ValueError("未知"), "permanent"),
    ],
)
def test_classify(exc: BaseException, expected: str) -> None:
    assert classify(exc) == expected


def test_retry_after_reads_header_and_attribute() -> None:
    assert reliability.retry_after_of(http_error(429, retry_after=7)) == 7.0
    assert reliability.retry_after_of(Boom(120)) == 120.0
    assert reliability.retry_after_of(http_error(429)) is None
    assert reliability.retry_after_of(Boom(None)) is None


# ── guard：重试与退避 ────────────────────────────────────────────


def test_success_returns_and_closes() -> None:
    make_dep()
    calls = []

    async def fn() -> str:
        calls.append(1)
        return "ok"

    assert asyncio.run(reliability.guard("test:dep", fn)) == "ok"
    assert len(calls) == 1


def test_retryable_failure_retries_with_backoff_then_succeeds() -> None:
    _, clock = make_dep(retry=Retry(max_attempts=3, base=0.5, factor=2.0, jitter=0.0))
    calls: list[int] = []

    async def fn() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise TimeoutError("第一次第二次都超时")
        return "ok"

    assert asyncio.run(reliability.guard("test:dep", fn)) == "ok"
    assert len(calls) == 3
    assert clock.slept == [0.5, 1.0]  # 0.5 → 1.0，指数退避


def test_retry_stops_at_max_attempts_and_reraises() -> None:
    make_dep(retry=Retry(max_attempts=3, jitter=0.0))
    calls: list[int] = []

    async def fn() -> str:
        calls.append(1)
        raise ConnectionError("一直断")

    with pytest.raises(ConnectionError):
        asyncio.run(reliability.guard("test:dep", fn))
    assert len(calls) == 3


def test_permanent_failure_is_not_retried() -> None:
    make_dep()
    calls: list[int] = []

    async def fn() -> str:
        calls.append(1)
        raise http_error(400)

    with pytest.raises(Exception):
        asyncio.run(reliability.guard("test:dep", fn))
    assert len(calls) == 1


def test_cancelled_is_not_retried_and_propagates() -> None:
    make_dep()
    calls: list[int] = []

    async def fn() -> str:
        calls.append(1)
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(reliability.guard("test:dep", fn))
    assert len(calls) == 1


def test_rate_limited_waits_the_asked_seconds() -> None:
    _, clock = make_dep(retry=Retry(max_attempts=3, jitter=0.0))
    calls: list[int] = []

    async def fn() -> str:
        calls.append(1)
        if len(calls) < 2:
            raise Boom(12)
        return "ok"

    assert asyncio.run(reliability.guard("test:dep", fn)) == "ok"
    assert clock.slept == [12.0]  # 按平台给的秒数等，不是退避猜的


def test_rate_limited_beyond_cap_degrades_without_retry() -> None:
    make_dep(retry=Retry(max_attempts=3, max_retry_after=30.0, jitter=0.0))
    calls: list[int] = []

    async def fn() -> str:
        calls.append(1)
        raise Boom(44 * 60)  # 平台让等 44 分钟——宁可降级也不占着不撒手

    with pytest.raises(Boom):
        asyncio.run(reliability.guard("test:dep", fn))
    assert len(calls) == 1


def test_rate_limited_without_hint_falls_back_to_backoff() -> None:
    _, clock = make_dep(retry=Retry(max_attempts=2, base=0.5, factor=2.0, jitter=0.0))
    calls: list[int] = []

    async def fn() -> str:
        calls.append(1)
        if len(calls) < 2:
            raise http_error(429)  # 429 但没给 Retry-After
        return "ok"

    assert asyncio.run(reliability.guard("test:dep", fn)) == "ok"
    assert clock.slept == [0.5]


def test_max_total_seconds_caps_the_retrying() -> None:
    """单次超时 6s × 3 次会把节点预算顶穿——`max_total_seconds` 就是那道闸。

    判据是「**等待前**先算账」：已耗 6s + 还要等 6s > 10s 的额度，就压根不等第二次。
    """
    _, clock = make_dep(
        retry=Retry(max_attempts=5, base=6.0, factor=1.0, jitter=0.0, max_total_seconds=10.0)
    )
    calls: list[int] = []

    async def fn() -> str:
        calls.append(1)
        clock.advance(6.0)  # 假装这次调用耗了 6s
        raise TimeoutError("每次都超时")

    with pytest.raises(TimeoutError):
        asyncio.run(reliability.guard("test:dep", fn))
    assert len(calls) == 1  # 6 + 6 > 10：连等都不等

    # 额度放宽到 20s：同样的调用就重试得起来（12s ≤ 20s），第三次才收手（18 + 6 > 20）
    calls.clear()
    clock.t = 0.0
    clock.slept.clear()
    _, clock = make_dep(
        retry=Retry(max_attempts=5, base=6.0, factor=1.0, jitter=0.0, max_total_seconds=20.0)
    )
    with pytest.raises(TimeoutError):
        asyncio.run(reliability.guard("test:dep", fn))
    assert len(calls) == 2


# ── 熔断：三态 ──────────────────────────────────────────────────


def test_circuit_opens_after_threshold_and_fails_fast() -> None:
    dep, _ = make_dep(retry=Retry(max_attempts=1, jitter=0.0), circuit=Circuit(threshold=3, cooldown=30.0))
    calls: list[int] = []

    async def fn() -> str:
        calls.append(1)
        raise ConnectionError("依赖挂了")

    for _ in range(3):
        with pytest.raises(ConnectionError):
            asyncio.run(reliability.guard("test:dep", fn))
    assert dep.state == "open"

    with pytest.raises(CircuitOpen) as info:
        asyncio.run(reliability.guard("test:dep", fn))
    assert len(calls) == 3  # 熔断后**没有**再调一次
    assert info.value.retry_after == pytest.approx(30.0)


def test_circuit_half_opens_after_cooldown_and_recovers() -> None:
    dep, clock = make_dep(
        retry=Retry(max_attempts=1, jitter=0.0), circuit=Circuit(threshold=2, cooldown=30.0)
    )
    fail = True

    async def fn() -> str:
        if fail:
            raise ConnectionError("挂了")
        return "ok"

    for _ in range(2):
        with pytest.raises(ConnectionError):
            asyncio.run(reliability.guard("test:dep", fn))
    assert dep.state == "open"

    clock.advance(30.0)
    fail = False
    assert asyncio.run(reliability.guard("test:dep", fn)) == "ok"
    assert dep.state == "closed"
    assert dep.failures == 0


def test_failed_probe_reopens_and_resets_cooldown() -> None:
    dep, clock = make_dep(
        retry=Retry(max_attempts=1, jitter=0.0), circuit=Circuit(threshold=2, cooldown=30.0)
    )

    async def fn() -> str:
        raise ConnectionError("挂了")

    for _ in range(2):
        with pytest.raises(ConnectionError):
            asyncio.run(reliability.guard("test:dep", fn))
    clock.advance(30.0)
    with pytest.raises(ConnectionError):  # 半开探测失败
        asyncio.run(reliability.guard("test:dep", fn))
    assert dep.state == "open"

    # 冷却重新起算：刚失败过，立刻再进仍然被拒
    with pytest.raises(CircuitOpen):
        asyncio.run(reliability.guard("test:dep", fn))


def test_rate_limited_trips_the_circuit_immediately() -> None:
    """小石红线：429 是停止信号——一次就够，不必等连续失败攒够。"""
    dep, _ = make_dep(circuit=Circuit(threshold=99))

    async def fn() -> str:
        raise Boom(600)

    with pytest.raises(Boom):
        asyncio.run(reliability.guard("test:dep", fn))
    assert dep.state == "open"


def test_permanent_failure_does_not_trip_the_circuit() -> None:
    """400 是「这次请求的问题」，不是「依赖的状态」——攒够 5 次也不该把电路打开。"""
    dep, _ = make_dep(retry=Retry(max_attempts=1), circuit=Circuit(threshold=2))

    async def fn() -> str:
        raise http_error(400)

    for _ in range(5):
        with pytest.raises(Exception):
            asyncio.run(reliability.guard("test:dep", fn))
    assert dep.state == "closed"


def test_success_resets_the_failure_streak() -> None:
    dep, _ = make_dep(retry=Retry(max_attempts=1), circuit=Circuit(threshold=3))
    boom = True

    async def fn() -> str:
        if boom:
            raise ConnectionError("挂了")
        return "ok"

    for _ in range(2):
        with pytest.raises(ConnectionError):
            asyncio.run(reliability.guard("test:dep", fn))
    boom = False
    asyncio.run(reliability.guard("test:dep", fn))
    assert dep.failures == 0


def test_snapshot_reports_each_dependency() -> None:
    make_dep()
    assert reliability.snapshot()["test:dep"]["state"] == "closed"
