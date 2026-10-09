"""因子分析（M5c）：独立数据管道——事件 / 价格 → PIT 因子面板 → 截面统计。

**不属于回测引擎**：引擎是单标的的（`BacktestConfig.symbol: str`），而因子分层是
日频截面再平衡的组合（走 `analysis` 里的独立小循环，只复用 `CostModel` 与 `metrics`）。
"""
