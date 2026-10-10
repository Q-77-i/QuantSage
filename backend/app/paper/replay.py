"""推进的实现：**重放是纯函数**（SPEC §7）。

`replay(config, 决策日志, through)` 同一份输入必得同一个账户状态。这条性质是「推进」可幂等、
可对账、可测试的全部依据，也是用户策略能正确进入模拟盘的原因——用户代码可以在模块级持状态
（M4a 已记），而**跨 HTTP 请求保留不了实例**，只有「从会话起点重放」能让它看到同一串 bar。
（那条性质本身由 `tests/test_paper_replay.py` 的不变量断言钉死。）

一天的顺序与引擎同构（见 `backtest/engine.py` 的模块 docstring）：

    ① 开盘：成交上一交易日挂下、状态为 `approved` 的决策；仍 `pending` 的 → `expired`
    ② 收盘：按收盘价估值，追加净值点
    ③ 收盘后：推进事件可见性（PIT 闸门）→ 问策略要信号 → 生成下一批决策

三条口径值得单独写明：

* **过去的日子以决策日志为准**：日志里有 (日, 标的) 的记录就用它，没有的（推进过头之后的新日子）
  才用策略的输出。故策略非确定性——沙箱白名单里 `datetime` 是允许的，用户可写 `datetime.now()`——
  **不污染账本**；代价是同一会话两次推进可能给出不同决策（如实记入已知边界）。
* **`ctx.cash` / `ctx.equity` 是账户级的**（现金与总权益），与引擎里这两个字段的含义一致；
  **数量由撮合层按配额规则决定**，策略不参与（`Signal` 本来就没有数量）。
* **停牌顺延的计数只认「有 bar 但不可交易」的日子**：引擎的循环跑在该标的自己的 bars 上，
  它看不见「整根 bar 都没有」的日子，这里的口径必须与它一致（否则同一场景两处结论不同）。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path

from app.backtest.a_share_rules import REJECT_REASONS, RejectCode, order_reject
from app.backtest.broker import Broker, affordable_qty
from app.backtest.strategies import Strategy, build_strategy
from app.backtest.types import Bar, BarContext, Mode, Side, Signal
from app.paper import MAX_DEFER_DAYS, PaperError
from app.paper.account import PaperAccount
from app.paper.market import MarketData, SymbolData, load_market_data
from app.paper.types import (
    DayOutcome,
    Decision,
    DecisionStatus,
    EquityMark,
    PaperConfig,
    ReplayResult,
    decision_id,
)

log = logging.getLogger(__name__)


def replay(
    config: PaperConfig,
    decisions: Sequence[Decision] = (),
    *,
    through: date | None = None,
    auto: DecisionStatus | None = None,
    strategy: Strategy | None = None,
    account_id: str = "",
    data_dir: Path | None = None,
    market: MarketData | None = None,
) -> ReplayResult:
    """从 `config.start` 重放到 `through`（缺省到 `config.end`）。

    `auto` 是「新生成的决策直接落什么状态」：`None` = `pending`（`step` 用），
    `APPROVED` / `REJECTED` = 一键跑到结束（`run` 用，见 SPEC §7 端点表）。
    `market` 供调用方复用已装载的数据（同 `data_dir` 的用途：测试与多次重放）。
    """
    data = market if market is not None else load_market_data(config, data_dir=data_dir)
    days = data.days if through is None else tuple(d for d in data.days if d <= through)
    if not days:
        raise PaperError(f"重放区间为空（through={through} 早于 {data.days[0]}）")

    strategy = strategy if strategy is not None else build_strategy(config.strategy, config.params)
    broker = Broker(config.costs)
    account = PaperAccount(config)
    feeds = {symbol: data.feed_for(symbol, Mode.PIT) for symbol in config.symbols}

    logged = {(d.trade_date, d.symbol) for d in decisions}
    book: dict[str, Decision] = {}
    for decision in decisions:
        # **只有 `filled` 才带成交明细**：日志里出现「已驳回却带成交」这类组合一律清掉。
        # 账本不受影响（它只认 `filled`），但不清理会让决策流水自相矛盾——
        # 「已驳回」旁边挂着一笔成交价，读的人没法判断到底成没成交
        # （2026-10-10 由 `scripts/run_paper.py` 的驳回取证逮到）。
        book[decision.id] = (
            decision.replace(fill=None)
            if decision.fill is not None and decision.status is not DecisionStatus.FILLED
            else decision
        )
    #: 日志里**已经成交**的决策，按（标的, 成交日）索引。
    #: 重放要重建账户，所以这些成交必须**照原样记回账本**——不记的话，「用日志再跑一遍」
    #: 会得到一条没有成交的净值曲线，而决策列表却显示已成交（自相矛盾；2026-10-10 由
    #: `test_replay_is_a_pure_function` 逮到）。**不重算**：日志是过去的权威，重算等于
    #: 让「策略或数据变了」这件事悄悄改写历史。
    settled: dict[tuple[str, date], list[Decision]] = {}
    for decision in decisions:
        if decision.status is not DecisionStatus.FILLED:
            continue
        if decision.fill is None:
            raise PaperError(f"决策 {decision.id} 标记已成交却没有成交明细——决策日志不一致")
        settled.setdefault((decision.symbol, decision.fill.trade_date), []).append(decision)
    carries: dict[str, Decision] = {}  # 已批准、等成交的决策（每标的最多一张，同引擎的 pending）
    for decision in decisions:
        # 日志里「已批准、还没成交」的单子要接着挂上——否则批准过的单在重放里凭空消失
        if decision.status is DecisionStatus.APPROVED:
            carries.setdefault(decision.symbol, decision)
    deferred: dict[str, int] = {}  # 该标的已顺延的**不可交易日**数
    equity_curve: list[EquityMark] = []
    outcomes: list[DayOutcome] = []

    for day in days:
        filled: list[Decision] = []
        expired: list[Decision] = []
        unfilled: list[Decision] = []
        generated: list[Decision] = []

        # ① 开盘：先记回日志里已成交的，再成交已批准的、过掉仍待审批的
        for symbol in config.symbols:
            symbol_data = data.symbols[symbol]
            for recorded in settled.get((symbol, day), ()):
                assert recorded.fill is not None  # 上面已判过
                index = symbol_data.index.get(recorded.fill.trade_date)
                if index is None:
                    raise PaperError(
                        f"决策 {recorded.id} 的成交日 {recorded.fill.trade_date} 不在 {symbol} 的行情里"
                    )
                account.settle(symbol, recorded.fill, index)
            carry = carries.get(symbol)
            # 挂单只能**在决策日之后**成交：同一次重放里，决策在当日的 ③ 才生成、
            # 天然不会撞上这个判断；但日志里带回来的「已批准」是从头开始跑的，
            # 少了这个守卫它会在**决策日当天**就成交（2026-10-10 由端点用例逮到）。
            if carry is not None and carry.trade_date < day:
                resolved = _resolve_carry(carry, symbol_data, day, account, broker, deferred)
                if resolved is not None:
                    book[resolved.id] = resolved
                    carries.pop(symbol, None)
                    (filled if resolved.status is DecisionStatus.FILLED else unfilled).append(resolved)
        for pending in [d for d in book.values() if d.status is DecisionStatus.PENDING]:
            if pending.trade_date < day:  # 成交窗口已过 —— 未审批不成交
                moved = pending.replace(status=DecisionStatus.EXPIRED)
                book[moved.id] = moved
                expired.append(moved)

        # ② 收盘估值
        closes = {
            symbol: bar.close
            for symbol in config.symbols
            if (bar := data.symbols[symbol].bar_at(day)) is not None
        }
        mark = account.mark(day, closes)
        equity_curve.append(mark)

        # ③ 收盘后：事件推进 + 问策略（过去的日子以日志为准）
        for symbol in config.symbols:
            symbol_data = data.symbols[symbol]
            bar = symbol_data.bar_at(day)
            if bar is None:
                continue
            fresh = feeds[symbol].advance(bar)
            if (day, symbol) in logged:
                continue
            if carries.get(symbol) is not None:
                continue  # 上一单还没成交：本日新信号不叠加（同引擎的处置）
            context = BarContext(
                bar=bar,
                index=symbol_data.index[day],
                history=symbol_data.bars[: symbol_data.index[day] + 1],
                position=account.position(symbol),
                cash=account.cash,
                equity=mark.equity,
                events=feeds[symbol].visible,
                new_events=fresh,
            )
            for signal in strategy.on_bar(context):
                decision = _propose(
                    config, account, symbol, day, bar, signal, account_id, data, auto
                )
                if decision is None or decision.id in book:
                    continue
                book[decision.id] = decision
                generated.append(decision)
                if decision.status is DecisionStatus.APPROVED:
                    carries[symbol] = decision  # 一键全批：下一交易日开盘成交

        outcomes.append(
            DayOutcome(
                trade_date=day,
                filled=tuple(filled),
                expired=tuple(expired),
                unfilled=tuple(unfilled),
                generated=tuple(generated),
            )
        )

    state = account.snapshot()
    ordered = tuple(sorted(book.values(), key=lambda d: (d.trade_date, d.symbol, d.side.value)))
    log.info(
        "模拟盘重放：%s 标的=%d 交易日=%d 决策=%d 期末净值=%.2f",
        config.strategy,
        len(config.symbols),
        len(days),
        len(ordered),
        equity_curve[-1].equity,
    )
    return ReplayResult(
        state=state,
        decisions=ordered,
        equity_curve=tuple(equity_curve),
        days=tuple(outcomes),
        rules={symbol: dict(d.rule.to_dict()) for symbol, d in data.symbols.items()},
    )


def _propose(
    config: PaperConfig,
    account: PaperAccount,
    symbol: str,
    day: date,
    bar: Bar,
    signal: Signal,
    account_id: str,
    data: MarketData,
    auto: DecisionStatus | None,
) -> Decision | None:
    """把一个 `Signal` 变成一张决策单（还**没有**成交——那要等下一交易日开盘）。

    `est_qty` 按**当日收盘价**预估（买用含滑点的价，与成交口径同源），并如实标注它是预估：
    真正的股数在次日开盘价上按同一个 `affordable_qty()` 重算。
    """
    costs = config.costs
    if signal.side is Side.BUY:
        est_price = costs.fill_price(Side.BUY, bar.close)
        budget = min(account.cash, account.budget(symbol))
        est_qty = affordable_qty(costs, budget, est_price)
    else:
        est_price = costs.fill_price(Side.SELL, bar.close)
        est_qty = account.position(symbol).shares
    if est_qty <= 0 and signal.side is Side.BUY:
        return None  # 一手都买不起：不产生决策（省得用户看到一张注定被拒的单）

    return Decision(
        id=decision_id(account_id, day, symbol, signal.side),
        account_id=account_id,
        symbol=symbol,
        trade_date=day,
        side=signal.side,
        est_qty=est_qty,
        est_price=est_price,
        reason=signal.reason,
        event_id=signal.event_id,
        sources=_sources_for(signal.event_id, data),
        status=auto if auto is not None else DecisionStatus.PENDING,
    )


def _sources_for(event_id: str | None, data: MarketData) -> Mapping[str, str] | None:
    """`Signal.event_id` → 事件行的**来源三元组**（买入决策能回链到驱动它的那条事件）。

    找的是本池子的事件行（重放已经按池子取过数，不必再查一次库）；找不到就返回 None——
    **卖出（持有到期一类）本来就没有事件来源，如实留空，不编**（SPEC §7）。
    """
    if not event_id:
        return None
    keys = (
        "event_id",
        "title",
        "event_time",
        "available_at",
        "source",
        "original_source",
        "content_hash",
        "source_url",
    )
    for rows in data.events.values():
        for row in rows:
            if row.get("event_id") == event_id:
                # 列按需取：`source_url` 这类列在夹具与旧分片里可能不存在，缺就是缺
                return {key: str(row[key]) for key in keys if row.get(key) is not None}
    return None


def _resolve_carry(
    carry: Decision,
    symbol_data: SymbolData,
    day: date,
    account: PaperAccount,
    broker: Broker,
    deferred: dict[str, int],
) -> Decision | None:
    """今天这根 bar 上，这张已批准的单能不能成交？返回 None 表示还成交不了（继续挂着）。"""
    symbol = carry.symbol
    bar = symbol_data.bar_at(day)
    if bar is None:
        return None  # 该标的当日没有 bar：引擎的循环里压根没有这一天，故也不计入顺延

    if not bar.tradable:
        count = deferred.get(symbol, 0) + 1
        if count > MAX_DEFER_DAYS:
            deferred.pop(symbol, None)
            return carry.replace(
                status=DecisionStatus.UNFILLED,
                reject_code=RejectCode.SUSPENDED.value,
                reject_reason=f"停牌连续 {count} 根无法撮合",
            )
        deferred[symbol] = count
        return None

    deferred.pop(symbol, None)
    index = symbol_data.index[day]
    position = account.position(symbol)
    blocked = order_reject(
        carry.side, bar, band=symbol_data.bands.get(day), bar_index=index, position=position
    )
    if blocked is not None:
        return carry.replace(
            status=DecisionStatus.UNFILLED,
            reject_code=blocked.value,
            reject_reason=REJECT_REASONS[blocked],
        )

    signal = Signal(carry.side, reason=carry.reason, event_id=carry.event_id)
    fill = broker.execute(signal, bar, account.view(symbol))
    if fill is None:
        # broker 返 None 只有三种来路：已持仓 / 空仓卖出 / 资金不足一手。
        # 只有最后一种算 A 股规则的 `REJECT_LOT`（同引擎的判据）。
        code = (
            RejectCode.LOT
            if carry.side is Side.BUY and account.position(symbol).is_flat
            else None
        )
        return carry.replace(
            status=DecisionStatus.UNFILLED,
            reject_code=code.value if code else None,
            reject_reason=REJECT_REASONS[code] if code else "拒单：资金或持仓不满足",
        )

    account.settle(symbol, fill, index)
    return carry.replace(status=DecisionStatus.FILLED, fill=fill)


__all__ = ["replay"]
