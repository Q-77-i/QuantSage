"""因子量化角色：**该标的在全市场截面里的位置**（不是 IC 报告）。

为什么不做 IC / 分层：那是 M5c 的截面管道（全市场、按日、~0.6–0.9s），一次深度研究里
跑它属于「为了看起来专业而多花一秒」。这里要回答的是更朴素也更相关的问题——
**它今天在全市场里排第几、20 日动量什么水平**。

**如实口径**：M5c 实测事件信号因子 RankIC 在噪声区间（−0.0081 / t −0.61），
价格反转 +0.0696 / t +2.44 但样本期特定——所以这里只报**分位与动量本身**，
不把「因子有效」这种话写进材料，模型也就无从复述。
"""

from __future__ import annotations

import asyncio

from app.agent.deep.roles.base import RoleContext, interpret
from app.agent.deep.types import RoleResult
from app.data import duckdb_client as dc

ROLE = "factor"


def rank_numbers(rank_rows: dict[str, dict]) -> dict[str, str]:
    """分位读数 → 对外数字（纯函数，便于对拍）。"""
    numbers: dict[str, str] = {}
    for symbol, row in rank_rows.items():
        total = row.get("total")
        if not total:
            continue
        pct = row.get("percentile_change")
        rank = row.get("rank_change")
        if pct is not None:
            numbers[f"{symbol} 涨跌幅分位"] = f"{float(pct) * 100:.0f}%（{rank}/{total}）"
        if row.get("percentile_amount") is not None:
            numbers[f"{symbol} 成交额分位"] = f"{float(row['percentile_amount']) * 100:.0f}%"
        if row.get("change_pct") is not None:
            numbers[f"{symbol} 当日涨跌"] = f"{float(row['change_pct']):+.2f}%"
        numbers[f"{symbol} 截面日期"] = str(row.get("trade_date"))
    return numbers


async def run(ctx: RoleContext) -> RoleResult:
    if not ctx.symbols:
        return RoleResult(ROLE, errors=("没有标的，跳过因子取证",))

    day = ctx.brief.as_of.date().isoformat()
    rank_rows: dict[str, dict] = {}
    for symbol in ctx.symbols:
        row = await asyncio.to_thread(dc.symbol_rank, symbol, day, data_dir=ctx.data_dir)
        if row:
            rank_rows[symbol] = row

    if not rank_rows:
        return RoleResult(ROLE, errors=("该时点没有可用的截面数据",))

    numbers = rank_numbers(rank_rows)
    # 材料用**格式化后的 numbers**渲染，不 dump 原始 dict——M7 的教训：给原始浮点，
    # 模型会原样复述一长串尾数（实测 `1.864711309668321` 被逐字抄进要点）
    material = (
        f"截面位置（截止 {day}，全市场为当日有价 bar 的全部标的）：\n"
        + "\n".join(
            f"- {symbol}：" + "；".join(
                f"{label.split(' ', 1)[-1]} {value}"
                for label, value in numbers.items()
                if label.startswith(f"{symbol} ")
            )
            for symbol in rank_rows
        )
        + "\n\n注：分位是位置描述，不代表预测力——本地实测的事件信号因子 RankIC 在噪声区间内。"
    )
    return await interpret(ROLE, ctx, material=material, numbers=numbers)
