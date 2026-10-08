"""RAG 层（M3）：事件语料的嵌入、索引与 PIT 语义检索。

分工：

* `collection.py` —— Qdrant 索引（schema / point id / 写入 / 按日重建 / 逐日点数对账）
* `encoder.py` —— 编码器与重排器（协议 + FlagEmbedding 实现；懒加载）
* `embed.py` —— 离线批处理（逐日分区、长度分桶、断点续跑）
* `retrieve.py` —— 检索管线（PIT 硬过滤 → 双路召回 → RRF → rerank）

三条贯穿全层的口径（依据见 SPEC §4 与 `docs/private/Pn-n/P2-Mn/P2-M3.md`）：

1. **PIT 过滤在服务端**：`available_at <= as_of` 写进 Qdrant 查询，不做事后过滤；
2. **主键带时间**：`event_id` 与 `dedup_key` 都实测可重复，point id 一律 `uuid5(day|event_id)`；
3. **降级不静默**：依赖不可用抛 `RagNotReady`，由调用方转成一句人话，
   **不得用空结果冒充「查不到」**。
"""

from __future__ import annotations


class RagNotReady(RuntimeError):
    """RAG 依赖不可用（Qdrant 未起 / 模型未装 / collection 未建）。

    与 `DataNotReady` 同族语义：调用方据此降级并**如实说明原因**。
    """
