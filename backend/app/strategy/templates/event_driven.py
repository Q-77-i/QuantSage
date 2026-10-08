"""事件驱动：利多事件（评分达标）触发买入，持有 N 个交易日后卖出。

三个模板通用说明（每个模板文件开头都写一遍，因为用户会**只复制一个文件**）：

1. 这份文件是**交给沙箱执行的源码文本**，不是可导入模块。`Signal` / `Side` 由沙箱预注入，
   不需要也不能 import；`ctx` 是引擎每根 bar 传进来的上下文。
2. 数据只能经 `ctx` 取：`ctx.new_events` 是**本根 bar 新可见**的事件（引擎按 `available_at`
   设卡，PIT 语义在数据层就完成了）——同一事件天然只触发一次，不必自己记 `seen_ids`。
3. **我们故意不给 pandas**：`shift` / `bfill` 正是前视泄漏的常见来源。想回看过去就索引
   `[-1]`（当前 bar）、`[-2]`（上一根）；`ffill`（前向填充）只用过去值，是允许的。

`USES_EVENTS = True` 有两个后果：缺省回测窗口取**事件语料起点**、报告给出 PIT 对比——
声明与代码不一致会拿到检查器的 warning。
"""

PARAMS = {
    "min_score": {"type": "float", "default": 50.0, "min": 0, "max": 100, "label": "最低评分"},
    "hold_days": {"type": "int", "default": 5, "min": 1, "max": 60, "label": "持有天数"},
}
USES_EVENTS = True


def validate_params(p):
    if p["hold_days"] < 1:
        return ["持有天数至少为 1"]
    return []


def on_bar(ctx, p):
    position = ctx.position
    if not position.is_flat:
        if position.entry_index is None:
            return []
        held = ctx.index - position.entry_index + 1  # 成交日算第 1 天
        if held >= p["hold_days"]:
            return [Signal(Side.SELL, reason=f"event_driven:hold {held}d")]
        return []  # 持有期内忽略新利多，不加仓

    # 只看本根 bar 新可见的事件：方向利多、评分达标，取第一条就够
    for event in ctx.new_events:
        if event.direction_norm != "bullish":
            continue
        if event.score is None or event.score < p["min_score"]:
            continue
        return [
            Signal(
                Side.BUY,
                reason=f"event_driven:{event.event_id} score={event.score:g}",
                event_id=event.event_id,
            )
        ]
    return []
