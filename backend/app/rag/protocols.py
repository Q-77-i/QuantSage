"""RAG 的协议层：编码器/重排器接口与批量编码结果。

**单独成模块而不是留在 `encoder.py`**：API/MCP 进程只需要协议与远程实现，
不该为了一个类型注解把 FlagEmbedding/torch 的实现模块拉进来（SPEC §4.2 的进程边界）。
`encoder.py` 与 `remote.py` 都从这里取协议；`encoder.py` 继续 re-export，兼容既有导入。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

#: 句向量维度（BGE-M3 稠密）
DIM = 1024


@dataclass(frozen=True, slots=True)
class EncodedDocs:
    """一批文本的稠密 + 稀疏表示。稀疏用 `{token_id: weight}`（BGE-M3 的原生形状）。"""

    dense: list[list[float]]
    sparse: list[dict[int, float]]


class Embedder(Protocol):
    """编码器协议。测试用假实现注入，**离线用例不下载任何模型**。"""

    dim: int

    def encode_documents(self, texts: Sequence[str]) -> EncodedDocs: ...

    def encode_query(self, text: str) -> tuple[list[float], dict[int, float]]: ...


class Reranker(Protocol):
    """精排器协议：给 (query, doc) 打分，分越高越相关。

    `request_class` 只对远程实现有意义（服务端按它选排队上限/超时）；本地实现接受并忽略。
    """

    def rerank(
        self, query: str, docs: Sequence[str], *, request_class: str = "interactive"
    ) -> list[float]: ...
