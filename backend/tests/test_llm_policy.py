"""E4a 的 LLM 侧：`PolicyChatModel` 把失败语义收口成一条策略。

测的是**策略层**（`acompletion_with_retry` 的两个重写点），所以假客户端直接替换
`ChatLiteLLM.client`——绕过 `_agenerate` 的响应解析，只验「调了几次、拿哪个模型调的、
异常怎么处置」。真正的报文形态由上游包负责，不在这里重复测。
"""

from __future__ import annotations

import asyncio

import pytest

from app.core import llm, reliability
from app.core.llm import CHAT_TIMEOUT, DEEP_MODEL, DEFAULT_MODEL, PolicyChatModel
from app.core.reliability import Circuit, CircuitOpen, Dependency, Retry


def chat_response(text: str) -> dict:
    """最小可穿的 litellm 报文（走 `ainvoke` 时 `_agenerate` 要按这个形状解析）。"""
    return {
        "id": "1",
        "object": "chat.completion",
        "created": 0,
        "model": "deepseek-flash",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class FakeClient:
    """假的 litellm 客户端：按剧本抛/回，并记下每次调用拿到的 `model`。"""

    def __init__(self, *script: object) -> None:
        self.script = list(script)
        self.models: list[str] = []

    def _next(self) -> object:
        item = self.script.pop(0) if self.script else "ok"
        if isinstance(item, BaseException):
            raise item
        return item

    async def acompletion(self, **kwargs: object) -> object:
        self.models.append(str(kwargs.get("model")))
        return self._next()

    def completion(self, **kwargs: object) -> object:
        self.models.append(str(kwargs.get("model")))
        return self._next()


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds

    def tsleep(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture(autouse=True)
def _fast_policy():
    """两个档位都装成「不真睡、jitter 关掉」的策略，用例才可断言。"""
    reliability.reset()
    for model in (DEFAULT_MODEL, DEEP_MODEL):
        clock = FakeClock()
        reliability.install(
            Dependency(
                name=f"llm:{model.rsplit('/', 1)[-1]}",
                retry=Retry(max_attempts=3, jitter=0.0),
                circuit=Circuit(threshold=2, cooldown=30.0),
                clock=clock,
                sleep=clock.sleep,
                tsleep=clock.tsleep,
            )
        )
    yield
    reliability.reset()


def model_with(client: FakeClient, model: str = DEFAULT_MODEL) -> PolicyChatModel:
    chat = PolicyChatModel(model=model, api_key="test-key", temperature=0.0)
    chat.client = client
    return chat


def test_library_layer_is_switched_off() -> None:
    """库自带的 tenacity 层默认 `stop_after_attempt(1)` = 压根不重试，且不读 Retry-After。

    策略归我们一处：显式写 0，别让它半开半闭地叠在下面（双重重试 + 重试不进预算）。
    """
    assert PolicyChatModel(model=DEFAULT_MODEL, api_key="k").max_retries == 0


def test_chat_path_has_a_timeout_by_default() -> None:
    """chat 主链路此前**完全没有超时**（`request_timeout` 默认 None）——上游挂死就是挂死。"""
    assert CHAT_TIMEOUT > 0
    assert PolicyChatModel(model=DEFAULT_MODEL, api_key="k").request_timeout == CHAT_TIMEOUT


def test_retries_then_succeeds() -> None:
    client = FakeClient(TimeoutError("第一次"), TimeoutError("第二次"), "报告")
    chat = model_with(client, DEEP_MODEL)

    result = asyncio.run(chat.acompletion_with_retry(model=DEEP_MODEL, messages=[]))

    assert result == "报告"
    assert client.models == [DEEP_MODEL] * 3


def test_falls_back_to_flash_after_retries_exhausted() -> None:
    """pro 三次都没起来 → 换 flash 顶上（**单向**，SPEC §5.2）。"""
    client = FakeClient(
        TimeoutError("1"), TimeoutError("2"), TimeoutError("3"), "flash 顶上的结论"
    )
    chat = model_with(client, DEEP_MODEL)

    result = asyncio.run(chat.acompletion_with_retry(model=DEEP_MODEL, messages=[]))

    assert result == "flash 顶上的结论"
    assert client.models == [DEEP_MODEL] * 3 + [DEFAULT_MODEL]


def test_flash_has_no_further_fallback() -> None:
    client = FakeClient(TimeoutError("1"), TimeoutError("2"), TimeoutError("3"))
    chat = model_with(client, DEFAULT_MODEL)

    with pytest.raises(TimeoutError):
        asyncio.run(chat.acompletion_with_retry(model=DEFAULT_MODEL, messages=[]))
    assert set(client.models) == {DEFAULT_MODEL}  # 没有「升档」这种反向路径


def test_permanent_error_does_not_fall_back() -> None:
    """400 是这次请求自己的问题——换个模型也是同样的错，别浪费一次调用。"""

    class BadRequest(Exception):
        status_code = 400

    client = FakeClient(BadRequest("内容不合规"))
    chat = model_with(client, DEEP_MODEL)

    with pytest.raises(BadRequest):
        asyncio.run(chat.acompletion_with_retry(model=DEEP_MODEL, messages=[]))
    assert client.models == [DEEP_MODEL]  # 一次都不多


def test_open_circuit_fails_fast_without_calling() -> None:
    client = FakeClient(TimeoutError("挂了"), TimeoutError("挂了"), TimeoutError("挂了"))
    chat = model_with(client, DEFAULT_MODEL)

    with pytest.raises(TimeoutError):
        asyncio.run(chat.acompletion_with_retry(model=DEFAULT_MODEL, messages=[]))
    # 连续失败到阈值（2）→ 开路。此后**不再发起调用**，秒回。
    with pytest.raises(CircuitOpen):
        asyncio.run(chat.acompletion_with_retry(model=DEFAULT_MODEL, messages=[]))
    assert len(client.models) == 3  # 熔断之后一次都没再调


def test_pro_circuit_open_switches_straight_to_flash() -> None:
    """熔断状态按**档位**分：pro 开着的时候，降级链照样能把 flash 用起来。

    这是「一份 llm 状态」表达不了的语义——若两档共用一份，熔断一开，
    降级链换到 flash 也会被同一道闸拒掉，链等于报废（SPEC §5.3）。
    """
    client = FakeClient(*[TimeoutError("挂了")] * 3, "flash 顶上的结论")
    chat = model_with(client, DEEP_MODEL)

    # 第一次：pro 三次全挂（第二次失败即熔断）→ 换 flash，一次就成
    result = asyncio.run(chat.acompletion_with_retry(model=DEEP_MODEL, messages=[]))
    assert result == "flash 顶上的结论"
    assert client.models == [DEEP_MODEL] * 3 + [DEFAULT_MODEL]

    # 第二次：pro 已熔断 → **一次都不碰 pro**，直接走 flash
    assert asyncio.run(chat.acompletion_with_retry(model=DEEP_MODEL, messages=[])) == "ok"
    assert client.models == [DEEP_MODEL] * 3 + [DEFAULT_MODEL, DEFAULT_MODEL]


def test_cancelled_is_not_retried() -> None:
    client = FakeClient(asyncio.CancelledError())
    chat = model_with(client, DEEP_MODEL)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(chat.acompletion_with_retry(model=DEEP_MODEL, messages=[]))
    assert len(client.models) == 1


def test_reports_the_model_that_actually_served() -> None:
    """降级链换过档时，「实际用的是哪个模型」必须能被上层拿到——报告据此才不写假话。"""
    used: list[str] = []
    chat = PolicyChatModel(model=DEEP_MODEL, api_key="k", on_model_used=used.append)
    chat.client = FakeClient(*[TimeoutError("挂了")] * 3, "flash 顶上")

    asyncio.run(chat.acompletion_with_retry(model=DEEP_MODEL, messages=[]))

    assert used == [DEFAULT_MODEL]


def test_complete_reports_the_actual_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """`complete()` 的 `Completion.model` 是**实际**模型，不是 `model` 参数。"""
    chat = PolicyChatModel(model=DEEP_MODEL, api_key="k")
    chat.client = FakeClient(*[TimeoutError("挂了")] * 3, chat_response("结论"))

    result = asyncio.run(llm.complete("问题", model=DEEP_MODEL, chat=chat))

    assert result.text == "结论"
    assert result.model == DEFAULT_MODEL  # 不是 DEEP_MODEL


def test_sync_path_uses_the_same_policy() -> None:
    """同步路径（`completion_with_retry`）也受管——四条执行路径一个都不能漏。"""
    client = FakeClient(TimeoutError("第一次"), "同步成功")
    chat = model_with(client, DEFAULT_MODEL)

    result = chat.completion_with_retry(model=DEFAULT_MODEL, messages=[])

    assert result == "同步成功"
    assert client.models == [DEFAULT_MODEL] * 2
