"""绩效研报容器（M7a）：**M7 建容器，M8 建生成器**。

报告的主体是一个模拟盘账户（回测 run 的 trades 没有 `event_id`，证据面板会空一半）；
一次生成即冻结落库，分享链接永远看到同一份。本包只认 `blocks` 结构、不认来源——
M8 的深度研报按同一结构追加块，渲染 / 分享 / 导出 / 快照全部复用。

- `performance.py`：账户绩效指标（复用 `backtest.metrics` 的口径，只补波动率与超额）
- `attribution.py`：标的级与事件级归因
- `evidence.py`：决策来源快照 → 语料行（含平台修订标注）
- `snapshot.py`：数据指纹（逐分片重算 sha256）与 `report_hash`
- `narrative.py`：flash 综述（唯一的 inference 正文；超时与失败降级）
- `builder.py`：冻结产物组装 + claim 校验闸门
- `markdown.py`：Markdown 导出（含证据链）
"""
