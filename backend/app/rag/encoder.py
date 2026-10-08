"""编码器与重排器：协议 + BGE-M3 实现（FlagEmbedding / torch）。

**运行时选择是量出来的，不是选的**（M3 spike，2026-10-07）：fastembed（ONNX）路线被
实测否决——它到 0.8.1（PyPI 最新）仍**不支持 `BAAI/bge-m3`**（dense 与 sparse 都不在支持列表），
reranker 列表里也没有 `bge-reranker-v2-m3`。故走 BAAI 官方实现 FlagEmbedding。

三条实现口径：

1. **懒加载**：模型只在第一次真用到时载入（bge-m3 约 2.3GB、reranker 另 2.3GB，
   进程常驻代价大；测试与不用 RAG 的请求不该被它拖累）。
2. **权重目录从配置读**（`rag_model_dir`），不靠 `HF_HOME` 环境变量——环境变量要在
   import 前设好才生效，那种「换个进程就失效」的隐式依赖是排查噩梦。
3. **不可用即抛 `RagNotReady`**：不吞异常、不返回假向量。

CPU 上 `use_fp16` 必须为 False（fp16 在 CPU 上是慢而非快）；MPS 上为 True。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

from app.core.config import get_settings
from app.rag import RagNotReady

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
    """精排器协议：给 (query, doc) 打分，分越高越相关。"""

    def rerank(self, query: str, docs: Sequence[str]) -> list[float]: ...


def _cache_dir() -> str:
    """HF 的**仓库缓存根**：`<rag_model_dir>/hub`。

    必须与 `HF_HOME` 的布局对齐——`HF_HOME=X` 时 huggingface_hub 用的就是 `X/hub`。
    传 `cache_dir=X`（不带 `hub`）会让同一个模型落到第二份缓存里，实测白下 2.3GB。
    """
    path = get_settings().rag_model_dir / "hub"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _to_sparse(weights: dict[str, Any]) -> dict[int, float]:
    """FlagEmbedding 的 lexical weights → `{int(token_id): weight}`。

    键是**字符串**形式的 token id（实测），权重为 0 的项直接丢弃——Qdrant 的稀疏向量
    里零权重只占位置不产生分数。
    """
    return {int(k): float(v) for k, v in weights.items() if float(v) > 0}


class FlagEmbeddingEmbedder:
    """BGE-M3 稠密 + 稀疏（一次前向同时产出）。"""

    def __init__(
        self,
        *,
        model_name: str = "BAAI/bge-m3",
        device: str | None = None,
        batch_size: int | None = None,
        max_length: int | None = None,
    ) -> None:
        settings = get_settings()
        self._model_name = model_name
        self._device = device or settings.rag_device
        self._batch_size = batch_size or settings.rag_embed_batch_size
        self._max_length = max_length or settings.rag_max_length
        self._model: Any | None = None

    @property
    def dim(self) -> int:
        return DIM

    def _load(self) -> Any:
        if self._model is None:
            try:
                from FlagEmbedding import BGEM3FlagModel
            except Exception as exc:  # noqa: BLE001 —— 依赖缺失/版本不兼容都归为「不可用」
                raise RagNotReady(
                    f"FlagEmbedding 不可用（{type(exc).__name__}）："
                    "请在后端环境安装 rag 依赖组"
                ) from exc
            try:
                self._model = BGEM3FlagModel(
                    self._model_name,
                    use_fp16=self._device != "cpu",  # CPU 上 fp16 是慢而非快
                    devices=self._device,
                    cache_dir=_cache_dir(),
                    # 用不到 colbert 头，别把它的权重也载进来
                    return_colbert_vecs=False,
                )
            except Exception as exc:  # noqa: BLE001
                raise RagNotReady(
                    f"BGE-M3 载入失败（{type(exc).__name__}）：检查权重目录与磁盘空间"
                ) from exc
        return self._model

    def _encode(self, texts: Sequence[str]) -> EncodedDocs:
        out = self._load().encode(
            list(texts),
            batch_size=self._batch_size,
            max_length=self._max_length,
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=False,
        )
        dense = [[float(x) for x in vec] for vec in out["dense_vecs"]]
        sparse = [_to_sparse(w) for w in out["lexical_weights"]]
        return EncodedDocs(dense=dense, sparse=sparse)

    def encode_documents(self, texts: Sequence[str]) -> EncodedDocs:
        if not texts:
            return EncodedDocs(dense=[], sparse=[])
        return self._encode(texts)

    def encode_query(self, text: str) -> tuple[list[float], dict[int, float]]:
        encoded = self._encode([text])
        return encoded.dense[0], encoded.sparse[0]


class FlagReranker:
    """bge-reranker-v2-m3（编码器架构 cross-encoder，Mac 无 GPU 下的可用选择）。"""

    def __init__(
        self,
        *,
        model_name: str = "BAAI/bge-reranker-v2-m3",
        device: str | None = None,
        batch_size: int | None = None,
        max_length: int | None = None,
    ) -> None:
        settings = get_settings()
        self._model_name = model_name
        self._device = device or settings.rag_device
        self._batch_size = batch_size or settings.rag_rerank_batch_size
        self._max_length = max_length or settings.rag_max_length
        self._model: Any | None = None

    def _load(self) -> Any:
        if self._model is None:
            try:
                from FlagEmbedding import FlagReranker as _FlagReranker
            except Exception as exc:  # noqa: BLE001
                raise RagNotReady(f"FlagEmbedding 不可用（{type(exc).__name__}）") from exc
            try:
                self._model = _FlagReranker(
                    self._model_name,
                    use_fp16=self._device != "cpu",
                    devices=self._device,
                    cache_dir=_cache_dir(),
                    batch_size=self._batch_size,
                    max_length=self._max_length,
                )
            except Exception as exc:  # noqa: BLE001
                raise RagNotReady(
                    f"重排模型载入失败（{type(exc).__name__}）：检查权重目录与磁盘空间"
                ) from exc
        return self._model

    def rerank(self, query: str, docs: Sequence[str]) -> list[float]:
        if not docs:
            return []
        pairs = [(query, doc) for doc in docs]
        scores = self._load().compute_score(pairs, batch_size=self._batch_size)
        if isinstance(scores, (int, float)):  # 单对时返回标量
            return [float(scores)]
        return [float(s) for s in scores]


@lru_cache(maxsize=1)
def get_embedder() -> Embedder:
    """进程内单例（懒加载：函数被调用时才构造，模型在首次 `encode_*` 时才载入）。"""
    return FlagEmbeddingEmbedder()


@lru_cache(maxsize=1)
def get_reranker() -> Reranker:
    return FlagReranker()


def configure_hf_env() -> None:
    """把 HF 的传输口径钉死在进程内（供脚本在 import 模型库之前调用）。

    * `HF_HUB_DISABLE_XET=1`：spike 实测 Xet 通道在本机**卡死**（自适应并发一路降到 50，
      20 分钟只落 43MB），关掉后回落普通 HTTP 稳定 3–4 MB/s；
    * `HF_HOME` 指向仓库内 `.tools/models`，避免权重散落到 `~/.cache`。
    """
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    os.environ.setdefault("HF_HOME", str(get_settings().rag_model_dir))
