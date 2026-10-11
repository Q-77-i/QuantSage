"""行情技术面角色：**数字全是代码算的**，模型只负责把它说成人话。

这条分工不是洁癖：`numbers` 里的每个值都会进证据链的可复算部分，模型算出来的数字
既不可复算也不可回链（M7 的教训：直接给模型原始浮点，它会原样复述一长串尾数）。
"""

from __future__ import annotations

import asyncio
import statistics
from collections.abc import Mapping, Sequence
from datetime import timedelta

from app.agent.deep.roles.base import RoleContext, interpret
from app.agent.deep.types import RoleResult
from app.data import duckdb_client as dc

ROLE = "market"

#: 取数窗口（自然日）：够 20 日均线与 20 日动量，又不至于把全史拖进来
WINDOW_DAYS = 180

#: 参与统计的最小 bar 数（不足就只能给区间收益，不给年化波动率）
MIN_BARS = 20


def bars_start(ctx: RoleContext) -> str:
    return (ctx.brief.as_of.date() - timedelta(days=WINDOW_DAYS)).isoformat()


def market_numbers(rows: Sequence[Mapping[str, object]]) -> dict[str, str]:
    """由 bar 序列算出全部对外数字（纯函数）。**格式在这里定死**，模型只准引用。"""
    closes = [float(row["close"]) for row in rows if row.get("close") is not None]
    if len(closes) < 2:
        return {}
    numbers: dict[str, str] = {
        "区间收益": f"{(closes[-1] / closes[0] - 1) * 100:.2f}%",
        "期末收盘": f"{closes[-1]:.2f} 元",
        "区间最高": f"{max(closes):.2f} 元",
        "区间最低": f"{min(closes):.2f} 元",
        "样本交易日": f"{len(closes)} 日",
    }
    if len(closes) >= MIN_BARS:
        rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1]]
        numbers["年化波动率"] = f"{statistics.stdev(rets) * (252 ** 0.5) * 100:.2f}%"
        ma20 = sum(closes[-MIN_BARS:]) / MIN_BARS
        numbers["20 日均线"] = f"{ma20:.2f} 元"
        numbers["相对 20 日均线"] = f"{(closes[-1] / ma20 - 1) * 100:+.2f}%"
    if len(closes) > MIN_BARS:
        numbers["20 日动量"] = f"{(closes[-1] / closes[-MIN_BARS - 1] - 1) * 100:+.2f}%"
    amounts = [float(r["amount"]) for r in rows if r.get("amount") is not None]
    if amounts:
        numbers["近 20 日日均成交额"] = f"{sum(amounts[-MIN_BARS:]) / min(MIN_BARS, len(amounts)) / 1e8:.2f} 亿元"
    return numbers


async def run(ctx: RoleContext) -> RoleResult:
    """逐标的取行情 → 算数字 → 一次解读。取数失败只影响本角色（抛给调用方兜）。"""
    if not ctx.symbols:
        return RoleResult(ROLE, errors=("没有标的，跳过行情取证",))

    blocks: list[str] = []
    numbers: dict[str, str] = {}
    for symbol in ctx.symbols:
        # DuckDB 是阻塞调用，扔线程池——否则并行取证里这条会把事件循环按住（并行就成了假的）
        rows = await asyncio.to_thread(
            dc.bars,
            symbol,
            start=bars_start(ctx),
            end=ctx.brief.as_of.date().isoformat(),
            data_dir=ctx.data_dir,
        )
        stats = market_numbers(rows)
        if not stats:
            blocks.append(f"【{symbol}】窗口内没有可用行情")
            continue
        numbers.update({f"{symbol} {label}": value for label, value in stats.items()})
        rendered = "；".join(f"{label} {value}" for label, value in stats.items())
        blocks.append(f"【{symbol}】{rendered}")

    material = (
        "行情读数（来自本地日线，截止 " + ctx.brief.as_of.date().isoformat() + "）：\n"
        + "\n".join(blocks)
    )
    return await interpret(ROLE, ctx, material=material, numbers=numbers)
