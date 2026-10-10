"""决策记忆（M7）：把模拟盘的决策日志变成可复盘、可结算的长期记忆。

- `settle.py`：**回合配对**（本片交付）与结算（M7b 在本模块续写 alpha 与结算结果）——纯函数；
- `reflection.py`：flash 一句话教训（M7b）；
- `decision_store.py`：LangGraph Store 封装（M7b）。

为什么配对放在这里而不是 `report/`：报告只是消费方之一（逐笔复盘块），结算本身是
「决策记忆」的核心概念——账户账本的 docstring 早就写明「开平配对留给 M7 按需从决策日志算」。
"""
