"""批量 / 网格端点（M5b）：SSE 逐格推送 + 汇总的落库与重开。

四个端点：`POST /grid`、`POST /batch`（均 SSE）、`GET /runs`、`GET /runs/{run_id}`。

三条口径（SPEC §6 M5b 写死，理由都在那里）：

* **请求级错误在流开始前就是 422**（与 chat 同姿态）——`BatchRequestError` 由 `main.py`
  统一翻 422，端点里不写 try/except。轴数、轴参数、逐格参数、格数上限全部在这一层挡下，
  **绝不静默跳过某一格**。
* **断线即断**：`stop` 置位后不再派发新格，已在跑的最多 2 格自然跑完且**不落库**
  （不取消 in-flight——`to_thread` 取消不了，硬取消还会在沙箱里漏子进程）。刷新等于重跑。
* **落库发生在 `done`**：流走完才写 `optimization_runs`。**全失败也照落**——那是事实，不是错误。

网格与批量的**窗口解析时机不同**，这不是随手定的：网格只有一个标的，先解析再开流
（标的没数据就是 404，与 `POST /backtest` 同响应，且 `start` 帧能带上确定的窗口）；
批量有多个标的，逐个在格内解析，一格没有数据不该带走其余格。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import date
from typing import Any, Literal, Self

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api.backtest import CostOptions, normalize_run_id
from app.api.strategies import normalize_strategy_id
from app.backtest.batch import (
    MAX_CELLS,
    MAX_STRATEGIES,
    MAX_SYMBOLS,
    BatchRequestError,
    CellSpec,
    RunOptions,
    grid_cells,
    run_cells,
)
from app.backtest.report import resolve_window
from app.backtest.strategies import EventDriven, available_strategies, known_params
from app.backtest.strategies import validate_params as validate_builtin_params
from app.backtest.types import Mode
from app.core.auth import require_db, require_user
from app.strategy import USER_STRATEGY, StrategyCheckFailed, StrategyRejected
from app.strategy.api import parse_meta
from app.strategy.params import validate_params as validate_user_params
from app.strategy.static_check import check_source, has_errors

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/optimize", tags=["optimize"])

SYMBOL_PATTERN = r"^\d{6}$"

#: 静默超过这个秒数发一个注释帧。逐格推送密集得多（每格 70–500ms），这里是兜底。
KEEPALIVE_SECONDS = 15.0


class Axis(BaseModel):
    """一条参数轴。`values` 的**顺序即展开顺序**，热力图的行列跟着它走。"""

    model_config = ConfigDict(extra="forbid")

    param: str
    values: list[float | int]


class GridRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: str
    strategy_id: str | None = None
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    start: date | None = None
    end: date | None = None
    costs: CostOptions = Field(default_factory=CostOptions)
    # 网格**不收 `both`**：那等于把每一格的工作量翻倍，而 PIT 对比在单格重跑时照样能看
    pit_mode: Literal["pit", "non_pit"] = "pit"
    params: dict[str, float | int] = Field(default_factory=dict)
    axes: list[Axis]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        _check_strategy_pair(self.strategy, self.strategy_id)
        _check_window(self.start, self.end)
        return self


class StrategyPick(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: str
    strategy_id: str | None = None
    params: dict[str, float | int] = Field(default_factory=dict)


class BatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbols: list[str]
    strategies: list[StrategyPick]
    start: date | None = None
    end: date | None = None
    costs: CostOptions = Field(default_factory=CostOptions)
    pit_mode: Literal["pit", "non_pit"] = "pit"

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        for item in self.strategies:
            _check_strategy_pair(item.strategy, item.strategy_id)
        _check_window(self.start, self.end)
        if not self.symbols:
            raise ValueError("至少要有一个标的")
        if len(self.symbols) > MAX_SYMBOLS:
            raise ValueError(f"标的数最多 {MAX_SYMBOLS} 个（当前 {len(self.symbols)} 个）")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError(f"标的列表里有重复：{self.symbols}")
        for symbol in self.symbols:
            if not symbol.isdigit() or len(symbol) != 6:
                raise ValueError(f"{symbol!r} 不是六位标的代码")
        if not self.strategies:
            raise ValueError("至少要有一个策略")
        if len(self.strategies) > MAX_STRATEGIES:
            raise ValueError(f"策略数最多 {MAX_STRATEGIES} 条（当前 {len(self.strategies)} 条）")
        total = len(self.symbols) * len(self.strategies)
        if total > MAX_CELLS:
            raise ValueError(
                f"{len(self.symbols)} 个标的 × {len(self.strategies)} 条策略 = {total} 格，"
                f"超过单次上限 {MAX_CELLS} 格"
            )
        return self


def _check_strategy_pair(strategy: str, strategy_id: str | None) -> None:
    if strategy == USER_STRATEGY:
        if not strategy_id:
            raise ValueError(
                'strategy="user" 时必须带 strategy_id——策略要先保存才能跑（跑的是库里的源码）'
            )
    elif strategy_id is not None:
        raise ValueError('strategy_id 只能与 strategy="user" 一起用')
    elif strategy not in available_strategies():
        names = "、".join([*available_strategies(), USER_STRATEGY])
        raise ValueError(f"未知策略 {strategy!r}；可用策略：{names}")


def _check_window(start: date | None, end: date | None) -> None:
    if start and end and start > end:
        raise ValueError(f"区间起点 {start} 晚于终点 {end}")


# ── 策略闸门（与 M4c 的回测分支同序，但**只做一次**）──────────────


class PreparedStrategy:
    """一条就绪的策略：参数集合、逐格归一器、沙箱所需的源码与元数据。

    用户策略的**归属 404 → 静态检查 `error`=0 → 参数 schema** 在这里一次做完——
    SPEC §6 M5b 明写「不是逐格」：25 格各查一遍库、各跑一遍 AST 是纯浪费，
    而其中任何一步失败都该在开流之前以 404 / 422 结束。

    `source` 非空即用户策略：它与 `strategy_id` 一起进 `CellSpec`，逐格只 spawn 子进程。
    """

    __slots__ = ("kind", "name", "known", "source", "strategy_id", "uses_events", "_normalize")

    def __init__(
        self,
        *,
        kind: str,
        name: str,
        known: frozenset[str] | set[str],
        normalize: Any,
        source: str | None = None,
        strategy_id: str | None = None,
        uses_events: bool = False,
    ) -> None:
        self.kind = kind
        self.name = name
        self.known = known
        self._normalize = normalize
        self.source = source
        self.strategy_id = strategy_id
        self.uses_events = uses_events

    def resolve(self, values: dict[str, float | int]) -> tuple[dict[str, float | int], list[str]]:
        """校验并归一（内置只报错、用户策略顺带把 `PARAMS` 缺省值填满）。"""
        return self._normalize(values)


async def _prepare_builtin(strategy: str) -> PreparedStrategy:
    def normalize(values: dict[str, float | int]) -> tuple[dict[str, float | int], list[str]]:
        # 缺省值由引擎的 `from_params` 在跑的时候补，这里**不代为填充**：
        # 填了就会在热力图的参数标注里凭空多出用户没写过的键
        return dict(values), validate_builtin_params(strategy, values)

    return PreparedStrategy(
        kind="builtin",
        name=strategy,
        known=known_params(strategy),
        normalize=normalize,
        uses_events=strategy == EventDriven.name,
    )


async def _prepare_user(request: Request, user_id: int, strategy_id: str) -> PreparedStrategy:
    normalized = normalize_strategy_id(strategy_id)
    row = await require_db(request).get_strategy(user_id, normalized)
    if row is None:
        raise HTTPException(status_code=404, detail="策略不存在")

    findings = check_source(row["code"])
    if has_errors(findings):
        # 与单次回测同一条闸门：检查必须在 spawn **之前**（那是「运行前带行号给话术」的前提）
        raise StrategyCheckFailed(findings)
    meta = parse_meta(row["code"])

    def normalize(values: dict[str, float | int]) -> tuple[dict[str, float | int], list[str]]:
        try:
            return validate_user_params(meta.params, values), []
        except StrategyRejected as exc:
            return {}, [str(exc)]

    return PreparedStrategy(
        kind="user",
        name=row["name"],
        known=frozenset(meta.params),
        normalize=normalize,
        source=row["code"],
        strategy_id=normalized,
        uses_events=meta.uses_events,
    )


async def _prepare(
    request: Request, user_id: int, strategy: str, strategy_id: str | None
) -> PreparedStrategy:
    if strategy == USER_STRATEGY:
        assert strategy_id is not None  # Pydantic 已挡
        return await _prepare_user(request, user_id, strategy_id)
    return await _prepare_builtin(strategy)


def _spec_of(item: PreparedStrategy, pick: StrategyPick, symbol: str, params: dict) -> CellSpec:
    return CellSpec(
        symbol=symbol,
        strategy=pick.strategy,
        params=params,
        strategy_id=item.strategy_id,
        # 内置策略的展示名与 `strategy` 同值，只有用户策略要单列（`user` 不是给人看的名字）
        strategy_name=item.name if item.kind == "user" else None,
        source=item.source,
        uses_events=item.uses_events,
    )


# ── SSE ────────────────────────────────────────────────────


def sse_frame(event: str, payload: dict[str, Any]) -> str:
    """编码一个 SSE 帧（与 `api/chat.py` 同形；那边的不复用是为了不碰 chat 那份代码）。"""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


#: 断线后仍在跑的 pump 任务。与 `api/chat.py::_DETACHED` 同一个理由：asyncio 只持弱引用
_DETACHED: set[asyncio.Task[None]] = set()


def _detach(task: asyncio.Task[None]) -> None:
    _DETACHED.add(task)

    def _done(finished: asyncio.Task[None]) -> None:
        _DETACHED.discard(finished)
        if finished.cancelled():
            return
        error = finished.exception()
        if error is not None:  # pragma: no cover - 正常路径不走到（异常已转成 error 帧）
            log.warning("断连后仍在跑的批处理出错：%r", error)

    task.add_done_callback(_done)


def streaming_headers() -> dict[str, str]:
    """SSE 响应的两条头。**不回传运行号**——本端点断连即断、不续传（SPEC §6 M5b），
    没有「靠响应头兜底」的用法（那是 chat 的会话号才需要的）。"""
    return {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",  # 反代下禁用缓冲
    }


async def _stream(
    *,
    request: Request,
    user_id: int,
    specs: list[CellSpec],
    options: RunOptions,
    kind: str,
    start_payload: dict[str, Any],
    stored_request: dict[str, Any],
) -> AsyncIterator[str]:
    """逐格推送 + 收尾落库。**落库在 `done`**：流没走完就不留记录（刷新即重跑）。"""
    yield sse_frame("start", start_payload)

    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    stop = asyncio.Event()

    async def on_cell(cell: dict[str, Any]) -> None:
        # 队列无界、`put_nowait` 不会阻塞：消费者走了也只是把结果丢弃，不会把执行器卡住
        queue.put_nowait(("cell", cell))

    async def pump() -> None:
        try:
            summary = await run_cells(
                specs, options, kind=kind, on_cell=on_cell, stop=stop
            )
            queue.put_nowait(("done", summary))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 流内转成 error 帧
            queue.put_nowait(("error", exc))
        finally:
            queue.put_nowait(("end", None))

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                item_kind, payload = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            if item_kind == "end":
                break
            if item_kind == "error":
                log.exception("批处理失败 kind=%s", kind, exc_info=payload)
                yield sse_frame("error", {"code": "internal", "message": "内部错误，请重试"})
                break
            if item_kind == "cell":
                yield sse_frame("cell", payload)
                continue

            # done：先落库再通知（run_id 要给出去，落失败宁可报错也不给一个打不开的号）
            summary = payload
            run_id = str(uuid.uuid4())
            await require_db(request).save_optimization_run(
                run_id, user_id, stored_request, summary
            )
            yield sse_frame(
                "done",
                {
                    "run_id": run_id,
                    "kind": kind,
                    "cells_total": summary["cells_total"],
                    "cells_ok": summary["cells_ok"],
                    "best_index": summary["best_index"],
                    "overfit": summary["overfit"],
                    "duration_s": summary["duration_s"],
                },
            )
            break
    finally:
        # 断线时不取消在跑的格（取消不了，硬取消还会漏子进程），只掐掉「还要开跑的」。
        # 任务留着引用：它跑完会往没人读的队列里塞一条 done，不落库——正是「刷新即重跑」
        stop.set()
        if not task.done():
            _detach(task)


def _stored_request(body: BaseModel, resolved: tuple[date, date] | None, **extra: Any) -> dict[str, Any]:
    """落库的 `request` = **解析后的请求**（网格的区间已填好），不是原始请求体。

    `mode="json"` 不是可选项：`start`/`end` 是 `date`，原样塞进 Jsonb 会在 dump 时 TypeError。
    """
    stored = body.model_dump(mode="json")
    if resolved is not None:
        stored["start"], stored["end"] = resolved[0].isoformat(), resolved[1].isoformat()
    stored["adjust"] = "qfq"
    stored.update(extra)
    return stored


# ── 端点 ───────────────────────────────────────────────────


@router.post("/grid")
async def run_grid(
    request: Request, body: GridRequest, user: dict[str, Any] = Depends(require_user)
) -> StreamingResponse:
    """参数网格：笛卡尔展开 → 逐格回测 → 热力图数据 + Deflated Sharpe。"""
    user_id = int(user["id"])
    prepared = await _prepare(request, user_id, body.strategy, body.strategy_id)

    cells = grid_cells(
        base=body.params,
        axes=[(axis.param, axis.values) for axis in body.axes],
        known=prepared.known,
        normalize=prepared.resolve,
    )

    # 网格只有一个标的：窗口先解析好。标的没数据就是 404（与 POST /backtest 同响应），
    # 而不是开一条只会吐失败格的流——`start` 帧也就带得上确定的窗口
    resolved = await asyncio.to_thread(
        resolve_window,
        body.symbol,
        body.strategy,
        body.start,
        body.end,
        uses_events=prepared.uses_events,
    )
    options = RunOptions(
        costs=body.costs.to_model(),
        pit_mode=Mode(body.pit_mode),
        start=resolved[0],
        end=resolved[1],
        adjust="qfq",
    )
    pick = StrategyPick(
        strategy=body.strategy, strategy_id=body.strategy_id, params=body.params
    )
    specs = [_spec_of(prepared, pick, body.symbol, params) for params in cells]
    axes = [{"param": axis.param, "values": list(axis.values)} for axis in body.axes]
    return StreamingResponse(
        _stream(
            request=request,
            user_id=user_id,
            specs=specs,
            options=options,
            kind="grid",
            start_payload={
                "kind": "grid",
                "total": len(specs),
                "symbol": body.symbol,
                "strategy": body.strategy,
                "strategy_name": prepared.name if prepared.kind == "user" else None,
                "axes": axes,
                "window": {"start": resolved[0].isoformat(), "end": resolved[1].isoformat()},
                "pit_mode": body.pit_mode,
            },
            stored_request=_stored_request(
                body, resolved, kind="grid", strategy_name=prepared.name if prepared.kind == "user" else None
            ),
        ),
        media_type="text/event-stream",
        headers=streaming_headers(),
    )


@router.post("/batch")
async def run_batch(
    request: Request, body: BatchRequest, user: dict[str, Any] = Depends(require_user)
) -> StreamingResponse:
    """批量：多标的 × 多策略。窗口逐格解析（一格没数据不该带走其余格）。"""
    user_id = int(user["id"])
    prepared = [
        await _prepare(request, user_id, pick.strategy, pick.strategy_id)
        for pick in body.strategies
    ]
    # 每条策略只归一**一次**（不是每个标的各来一遍），失败即整单 422
    resolved: list[tuple[StrategyPick, PreparedStrategy, dict[str, float | int]]] = []
    for pick, item in zip(body.strategies, prepared, strict=True):
        params, errors = item.resolve(dict(pick.params))
        if errors:
            raise BatchRequestError(f"策略「{item.name}」的参数不合法：{'；'.join(errors)}")
        resolved.append((pick, item, params))

    # 格序：**标的在外、策略在内**——前端表格每一行是一个标的，正好对上 `index // 策略数`
    specs = [
        _spec_of(item, pick, symbol, params)
        for symbol in body.symbols
        for pick, item, params in resolved
    ]
    options = RunOptions(
        costs=body.costs.to_model(),
        pit_mode=Mode(body.pit_mode),
        start=body.start,
        end=body.end,
        adjust="qfq",
    )
    return StreamingResponse(
        _stream(
            request=request,
            user_id=user_id,
            specs=specs,
            options=options,
            kind="batch",
            start_payload={
                "kind": "batch",
                "total": len(specs),
                "symbols": list(body.symbols),
                "strategies": [
                    {"strategy": pick.strategy, "strategy_name": item.name if item.kind == "user" else None}
                    for pick, item in zip(body.strategies, prepared, strict=True)
                ],
                "pit_mode": body.pit_mode,
            },
            stored_request=_stored_request(body, None, kind="batch"),
        ),
        media_type="text/event-stream",
        headers=streaming_headers(),
    )


@router.get("/runs")
async def list_runs(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    user: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    """我的优化（摘要），最近在前。**不含每格矩阵**（列表不为每行拖一份矩阵回来）。"""
    return await require_db(request).list_optimization_runs(int(user["id"]), limit)


@router.get("/runs/{run_id}")
async def get_run(
    request: Request, run_id: str, user: dict[str, Any] = Depends(require_user)
) -> dict[str, Any]:
    """重开：按 id 取回完整汇总（`request` + `summary`）。越权与不存在同返 404。"""
    normalized = normalize_run_id(run_id)
    row = await require_db(request).get_optimization_run(int(user["id"]), normalized)
    if row is None:
        raise HTTPException(status_code=404, detail="优化记录不存在")
    return row
