"""唐奇安通道突破：收盘创 N 日新高买入，跌破 N 日新低卖出。

三个模板通用说明（每个模板文件开头都写一遍，因为用户会**只复制一个文件**）：

1. 这份文件是**交给沙箱执行的源码文本**，不是可导入模块。`Signal` / `Side` 由沙箱预注入，
   不需要也不能 import；`ctx` 是引擎每根 bar 传进来的上下文。
2. 数据只能经 `ctx` 取：`ctx.history` 是**截断到当前 bar** 的元组——未来在物理上拿不到，
   所以不需要（也无法）自己防前视。
3. **我们故意不给 pandas**：`shift` / `bfill` 正是前视泄漏的常见来源。想回看过去就索引
   `[-1]`（当前 bar）、`[-2]`（上一根）；`ffill`（前向填充）只用过去值，是允许的。

通道口径写明：突破位取**当根之前**的 N 根（`ctx.history[-N-1:-1]`），不含当根——把当根的
high 也算进通道，突破就成了「自己跟自己比」。比较用的是收盘价（回测按下一根开盘撮合）。
"""

PARAMS = {
    "window": {"type": "int", "default": 20, "min": 2, "max": 250, "label": "通道窗口（日）"},
}
USES_EVENTS = False


def on_bar(ctx, p):
    window = p["window"]
    if len(ctx.history) <= window:
        return []

    past = ctx.history[-window - 1:-1]  # 当根之前的 window 根
    close = ctx.bar.close

    if ctx.position.is_flat:
        if close > max(bar.high for bar in past):
            return [Signal(Side.BUY, reason=f"donchian:breakout {window}d high")]
        return []
    if close < min(bar.low for bar in past):
        return [Signal(Side.SELL, reason=f"donchian:breakdown {window}d low")]
    return []
