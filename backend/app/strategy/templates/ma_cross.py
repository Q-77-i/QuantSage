"""双均线交叉：快线上穿慢线（金叉）买入，下穿（死叉）卖出。

三个模板通用说明（每个模板文件开头都写一遍，因为用户会**只复制一个文件**）：

1. 这份文件是**交给沙箱执行的源码文本**，不是可导入模块。`Signal` / `Side` 由沙箱预注入，
   不需要也不能 import；`ctx` 是引擎每根 bar 传进来的上下文。
2. 数据只能经 `ctx` 取：`ctx.history` 是**截断到当前 bar** 的元组——未来在物理上拿不到，
   所以不需要（也无法）自己防前视。
3. **我们故意不给 pandas**：`shift` / `bfill` 正是前视泄漏的常见来源。想回看过去就索引
   `[-1]`（当前 bar）、`[-2]`（上一根）；`ffill`（前向填充）只用过去值，是允许的。

参数在 `PARAMS` 里声明，`on_bar` 通过第二个形参 `p` 取（`p` 已用缺省值填满）。
"""

PARAMS = {
    "fast": {"type": "int", "default": 5, "min": 1, "max": 250, "label": "快线周期"},
    "slow": {"type": "int", "default": 20, "min": 2, "max": 250, "label": "慢线周期"},
}
USES_EVENTS = False


def validate_params(p):
    """跨字段约束：快线必须短于慢线（引擎在跑之前会调它）。"""
    if p["fast"] >= p["slow"]:
        return ["快线周期必须小于慢线周期"]
    return []


def mean(values):
    return sum(values) / len(values)


def on_bar(ctx, p):
    closes = [bar.close for bar in ctx.history]
    index = len(closes) - 1
    if index < p["slow"]:
        return []

    # 交叉判定要「前一根 vs 当前根」两套均线，所以要多留一根：切片的上界是不含的
    fast_now = mean(closes[index - p["fast"] + 1: index + 1])
    fast_prev = mean(closes[index - p["fast"]: index])
    slow_now = mean(closes[index - p["slow"] + 1: index + 1])
    slow_prev = mean(closes[index - p["slow"]: index])

    golden = fast_prev <= slow_prev and fast_now > slow_now
    death = fast_prev >= slow_prev and fast_now < slow_now

    if golden and ctx.position.is_flat:
        return [Signal(Side.BUY, reason=f"ma_cross:golden MA{p['fast']}/MA{p['slow']}")]
    if death and not ctx.position.is_flat:
        return [Signal(Side.SELL, reason=f"ma_cross:death MA{p['fast']}/MA{p['slow']}")]
    return []
