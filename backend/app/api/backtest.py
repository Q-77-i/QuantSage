"""回测 API：SPEC §5 报告结构的同步出口。

`build_report` 是**同步 CPU + DuckDB IO**，必须卸载到线程——直接在 async 端点里跑会
阻塞事件循环（T3 在 DuckDB 工具上已踩过同一个坑）。

错误映射由 `main.py` 的异常处理器统一完成：`DataNotReady` → 503、`NoDataError` → 404、
其余 `BacktestError` → 400，这里不写 try/except。
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any, Literal, Self

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.backtest.costs import CostModel
from app.backtest.engine import BacktestConfig
from app.backtest.report import build_report, resolve_window
from app.backtest.strategies import EventDriven, available_strategies, known_params
from app.backtest.types import Mode

router = APIRouter(prefix="/api/v1/backtest", tags=["backtest"])

SYMBOL_PATTERN = r"^\d{6}$"


class CostOptions(BaseModel):
    """费用与滑点，映射到 `CostModel` 的两个独立开关 + 滑点档位。

    开关保持独立：T4 的验收项之一就是「手续费/滑点各有可见影响」，合并成一个
    `costs: bool` 会让这项验收没法在前端复现。
    """

    fees: bool = True
    slippage: bool = True
    slippage_bps: float = Field(default=5.0, ge=0.0, le=100.0)

    def to_model(self) -> CostModel:
        model = CostModel(slippage_bps=self.slippage_bps)
        if not self.fees:
            model = model.without_fees()
        if not self.slippage:
            model = model.without_slippage()
        return model


class BacktestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: str
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    start: date | None = None
    end: date | None = None
    costs: CostOptions = Field(default_factory=CostOptions)
    pit_mode: Literal["pit", "non_pit", "both"] = "pit"
    params: dict[str, float] = Field(default_factory=dict)

    @field_validator("strategy")
    @classmethod
    def _known_strategy(cls, value: str) -> str:
        if value not in available_strategies():
            raise ValueError(
                f"未知策略 {value!r}；可用策略：{'、'.join(available_strategies())}"
            )
        return value

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        # 拼错的参数键必须挡在这里：from_params 会静默忽略它，用户会以为参数生效了
        unknown = sorted(set(self.params) - known_params(self.strategy))
        if unknown:
            allowed = "、".join(sorted(known_params(self.strategy))) or "（无）"
            raise ValueError(f"{self.strategy} 不接受参数 {unknown}；可用：{allowed}")
        if self.start and self.end and self.start > self.end:
            raise ValueError(f"区间起点 {self.start} 晚于终点 {self.end}")
        return self


@router.post("")
async def run_backtest(request: BacktestRequest) -> dict[str, Any]:
    """跑一次回测，返回 SPEC §5 的报告结构。"""
    start, end = await asyncio.to_thread(
        resolve_window, request.symbol, request.strategy, request.start, request.end
    )

    # ma_cross 不消费事件语料，两模式必然同结果，跑第二遍只会误导（与 CLI 同口径）
    compare_pit = request.pit_mode == "both" and request.strategy == EventDriven.name

    config = BacktestConfig(
        symbol=request.symbol,
        strategy=request.strategy,
        start=start,
        end=end,
        adjust="qfq",
        pit_mode=Mode.PIT if request.pit_mode == "both" else Mode(request.pit_mode),
        costs=request.costs.to_model(),
        params=request.params,
    )
    return await asyncio.to_thread(build_report, config, compare_pit=compare_pit)
