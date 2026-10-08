"""RSI 超卖反转：Wilder RSI 跌破阈值买入，回到高位卖出。

三个模板通用说明（每个模板文件开头都写一遍，因为用户会**只复制一个文件**）：

1. 这份文件是**交给沙箱执行的源码文本**，不是可导入模块。`Signal` / `Side` 由沙箱预注入，
   不需要也不能 import；`ctx` 是引擎每根 bar 传进来的上下文。
2. 数据只能经 `ctx` 取：`ctx.history` 是**截断到当前 bar** 的元组——未来在物理上拿不到，
   所以不需要（也无法）自己防前视。
3. **我们故意不给 pandas**：它的 `ewm(...).mean()` 一行就能算 RSI，但 `shift` / `bfill`
   这类前视入口也在同一个包里，所以这里用纯 Python 手写——顺带演示「不用第三方库也能写」。

RSI 用 Wilder 的递推平滑（从序列开头一路推过来，只用过去收盘价）。每根 bar 重算一遍是 O(n)，
整段回测 O(n²)，千根级别完全够用。
"""

PARAMS = {
    "period": {"type": "int", "default": 14, "min": 2, "max": 60, "label": "RSI 周期"},
    "oversold": {"type": "float", "default": 30.0, "min": 1, "max": 50, "label": "超卖阈值"},
    "exit_level": {"type": "float", "default": 70.0, "min": 50, "max": 99, "label": "离场阈值"},
}
USES_EVENTS = False


def validate_params(p):
    if p["oversold"] >= p["exit_level"]:
        return ["超卖阈值必须小于离场阈值"]
    return []


def wilder_rsi(closes, period):
    """返回 0~100 的 RSI；数据不足 `period + 1` 根时返回 None。"""
    if len(closes) < period + 1:
        return None

    gains = 0.0
    losses = 0.0
    for index in range(1, period + 1):
        change = closes[index] - closes[index - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period

    for index in range(period + 1, len(closes)):
        change = closes[index] - closes[index - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period

    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def on_bar(ctx, p):
    closes = [bar.close for bar in ctx.history]
    value = wilder_rsi(closes, p["period"])
    if value is None:
        return []

    if ctx.position.is_flat and value < p["oversold"]:
        return [Signal(Side.BUY, reason=f"rsi:oversold {value:.1f}")]
    if not ctx.position.is_flat and value > p["exit_level"]:
        return [Signal(Side.SELL, reason=f"rsi:recovered {value:.1f}")]
    return []
