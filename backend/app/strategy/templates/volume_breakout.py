"""放量突破：成交量放大到均量的若干倍、且当根收阳时买入，持有 N 个交易日后卖出。

三个模板通用说明（每个模板文件开头都写一遍，因为用户会**只复制一个文件**）：

1. 这份文件是**交给沙箱执行的源码文本**，不是可导入模块。`Signal` / `Side` 由沙箱预注入，
   不需要也不能 import；`ctx` 是引擎每根 bar 传进来的上下文。
2. 数据只能经 `ctx` 取：`ctx.history` 是**截断到当前 bar** 的元组——当根的成交量、开收盘
   在收盘时点已经知道，用它们不构成前视；未来 bar 在物理上拿不到。
3. **我们故意不给 pandas**：`shift` / `bfill` 正是前视泄漏的常见来源。想回看过去就索引
   `[-1]`（当前 bar）、`[-2]`（上一根）；`ffill`（前向填充）只用过去值，是允许的。

均量同样只取**当根之前**的 N 根（当根的天量不该把自己的基准抬高）。
"""

PARAMS = {
    "window": {"type": "int", "default": 20, "min": 2, "max": 250, "label": "均量窗口（日）"},
    "multiple": {"type": "float", "default": 2.0, "min": 1.1, "max": 10.0, "label": "放量倍数"},
    "hold_days": {"type": "int", "default": 3, "min": 1, "max": 60, "label": "持有天数"},
}
USES_EVENTS = False


def on_bar(ctx, p):
    if not ctx.position.is_flat:
        entry = ctx.position.entry_index
        if entry is None:
            return []
        held = ctx.index - entry + 1  # 成交日算第 1 天
        if held >= p["hold_days"]:
            return [Signal(Side.SELL, reason=f"volume:hold {held}d")]
        return []

    window = p["window"]
    if len(ctx.history) <= window:
        return []

    past = ctx.history[-window - 1:-1]  # 当根之前的 window 根
    average = sum(bar.volume for bar in past) / len(past)

    bar = ctx.bar
    if bar.volume > p["multiple"] * average and bar.close > bar.open:
        return [Signal(Side.BUY, reason=f"volume:breakout {bar.volume / average:.1f}x")]
    return []
