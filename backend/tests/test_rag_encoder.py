"""离线单测：编码器的设备闸门（M3 补口，2026-10-09）。

这一条是**可用性**测试，不是性能测试：PyTorch 的 MPS 后端不是线程安全的，两个线程同时
提交命令缓冲会在 Metal 层触发 `MTLReleaseAssertionFailure` → `abort()` **整个进程**。
实测崩溃（uvicorn 被 SIGABRT 打死，崩溃报告里一个线程卡在 `MPSStream::synchronize`、
另一个正在 `setCurrentCommandEncoder`），调用方是 `asyncio.to_thread` 的线程池。

这里能离线断言的是「同一时刻只有一个 MPS 调用在跑」；「Metal 会不会真的断言」由系统决定，
不写进单测。假模型注入，**不下载任何权重、不碰 GPU**。
"""

from __future__ import annotations

import threading
import time
from typing import Any

from app.rag import encoder as enc


class _OverlapProbe:
    """记录「同时有多少个调用在执行」的假模型。"""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def _enter(self) -> None:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)

    def _exit(self) -> None:
        with self._lock:
            self.active -= 1

    def compute_score(self, pairs: Any, batch_size: int | None = None) -> list[float]:
        self._enter()
        time.sleep(0.05)  # 把窗口拉开：没有闸门时这几条调用必然重叠
        self._exit()
        return [1.0] * len(pairs)

    def encode(self, texts: Any, **_: Any) -> dict[str, Any]:
        self._enter()
        time.sleep(0.05)
        self._exit()
        return {
            "dense_vecs": [[0.0] * enc.DIM for _ in texts],
            "lexical_weights": [{} for _ in texts],
        }


def _hammer(reranker: enc.FlagReranker, count: int = 6) -> None:
    threads = [
        threading.Thread(target=lambda: reranker.rerank("茅台利好", ["公告一", "公告二"]))
        for _ in range(count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def test_mps_calls_are_serialized() -> None:
    """device=mps 时调用串行——这是防进程 abort 的那道闸门。"""
    probe = _OverlapProbe()
    reranker = enc.FlagReranker(device="mps")
    reranker._model = probe  # 绕过真实载入

    _hammer(reranker)

    assert probe.max_active == 1, f"有 {probe.max_active} 个调用同时进了 MPS"


def test_cpu_calls_stay_parallel() -> None:
    """device=cpu 不加锁：那里没有共享的 GPU 命令队列，串行只会白白压吞吐。"""
    probe = _OverlapProbe()
    reranker = enc.FlagReranker(device="cpu")
    reranker._model = probe

    _hammer(reranker)

    assert probe.max_active > 1, "CPU 路径不该被串行化"


def test_embedder_uses_the_same_gate() -> None:
    """嵌入器走同一道闸门（`RAG_EMBED_DEVICE=mps` 时它也在 GPU 上）。"""
    probe = _OverlapProbe()
    embedder = enc.FlagEmbeddingEmbedder(device="mps")
    embedder._model = probe

    threads = [
        threading.Thread(target=lambda: embedder.encode_documents(["一段文本", "另一段"]))
        for _ in range(6)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert probe.max_active == 1
