"""S5：共享限流（redis-coord + Lua）的降级/熔断语义。

Lua 本身由真机验收覆盖；离线这里钉三件事：
* shared 关闭时完全走原进程内 `Limiter`（回退口）；
* shared 开启时把桶参数/键传给 Lua，并把毫秒等待翻成秒；
* coord 失败后按 `floor(capacity/N)` 降级，连续失败打开熔断、后续不再碰 Redis。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.core import coord, reliability


def _settings(*, shared: bool = True, workers: int = 2) -> SimpleNamespace:
    return SimpleNamespace(
        rate_limit_shared=shared,
        api_workers=workers,
        redis_coord_url="redis://127.0.0.1:6380/0",
    )


class FakeRedis:
    def __init__(self, results: list[object]) -> None:
        self.results = list(results)
        self.calls: list[tuple[object, int, tuple[object, ...]]] = []

    async def eval(self, script: object, numkeys: int, *args: object) -> object:
        self.calls.append((script, numkeys, args))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture(autouse=True)
def _reset() -> None:
    reliability.reset("redis-coord")
    reliability.reset_limiters()
    coord.reset()
    yield
    reliability.reset("redis-coord")
    reliability.reset_limiters()
    coord.reset()


def test_shared_off_delegates_to_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coord, "get_settings", lambda: _settings(shared=False))
    calls: list[tuple[str, int]] = []

    def fake_take(name: str, key: int) -> float:
        calls.append((name, key))
        return 1.25

    monkeypatch.setattr(reliability, "take", fake_take)

    assert asyncio.run(coord.take("llm-chat", 7)) == 1.25
    assert calls == [("llm-chat", 7)]


def test_shared_calls_lua_with_bucket_args(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRedis([0, 1500])
    monkeypatch.setattr(coord, "get_settings", lambda: _settings())
    monkeypatch.setattr(coord, "_client", lambda: fake)

    assert asyncio.run(coord.take("llm-chat", 7)) is None
    assert asyncio.run(coord.take("llm-chat", 7)) == 1.5

    script, numkeys, args = fake.calls[0]
    assert numkeys == 1
    assert args[0] == "rl:llm-chat:7"
    assert args[1] == 10  # 默认 chat 容量
    assert args[2] == 60_000  # 窗口毫秒
    assert "PEXPIRE" in str(script)


def test_coord_failure_degrades_to_floor_capacity_and_opens_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeRedis([RuntimeError("down")] * 3)
    monkeypatch.setattr(coord, "get_settings", lambda: _settings())
    monkeypatch.setattr(coord, "_client", lambda: fake)

    waits = [asyncio.run(coord.take("llm-chat", 9)) for _ in range(6)]

    # 10 // 2 = 5：前 5 次在进程内放行，第 6 次才需要等
    assert waits[:5] == [None] * 5
    assert waits[5] is not None and waits[5] > 0
    assert len(fake.calls) == 3  # 3 次失败后熔断，后续不再碰 Redis


def test_coord_success_resets_the_circuit(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    dep = reliability.get_dep("redis-coord")
    monkeypatch.setattr(dep, "clock", lambda: now[0])
    fake = FakeRedis([RuntimeError("down")] * 3 + [0])
    monkeypatch.setattr(coord, "get_settings", lambda: _settings())
    monkeypatch.setattr(coord, "_client", lambda: fake)

    for _ in range(3):
        asyncio.run(coord.take("llm-chat", 9))
    assert dep.state == "open"

    now[0] += dep.circuit.cooldown + 1
    assert asyncio.run(coord.take("llm-chat", 9)) is None
    assert dep.state == "closed"
    assert len(fake.calls) == 4
