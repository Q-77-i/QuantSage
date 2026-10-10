"""模拟盘的数据结构：**回测里没有的那一层**——决策（带审批状态的订单）与账户配置。

撮合与费用的类型原样复用（`Fill` / `Position` / `Side` / `CostModel`），本模块不重新造一份。
同 `app.backtest.types` 的规矩：**只依赖 `app.backtest.*`，不回依赖本包其他模块**，
免得 account → types → account 成环。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Mapping

from app.backtest.costs import CostModel
from app.backtest.types import Fill, Position, Side


class DecisionStatus(StrEnum):
    """决策的六态（SPEC §7）。**每个非法转移一律 409**，判据在 `replay` 里。"""

    PENDING = "pending"  # 待审批（T 日收盘生成）
    APPROVED = "approved"  # 已批准，等 T+1 开盘成交
    FILLED = "filled"  # 已成交（成交价与股数在 T+1 开盘才确定）
    REJECTED = "rejected"  # 用户驳回
    EXPIRED = "expired"  # 未审批过期——**未审批不成交**
    UNFILLED = "unfilled"  # 已批准但成交不了（一字板 / 停牌顺延超限 / 资金不足一手）


#: 状态 → 中文文案。**与前端同源**（M5b 的 `overfit.reason_text` 同一条规矩：
#: 前端不自己拼一句，免得两处话术漂移）。
STATUS_LABELS: dict[DecisionStatus, str] = {
    DecisionStatus.PENDING: "待审批",
    DecisionStatus.APPROVED: "已批准（待次日开盘成交）",
    DecisionStatus.FILLED: "已成交",
    DecisionStatus.REJECTED: "已驳回",
    DecisionStatus.EXPIRED: "未审批过期（未审批不成交）",
    DecisionStatus.UNFILLED: "已批准未成交",
}


#: 决策 id 的命名空间：固定值，跨进程/跨环境稳定（同 M3 的 `uuid5(day|event_id)` 口径）
NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "quantsage/paper")


def decision_id(account_id: str, trade_date: date, symbol: str, side: Side) -> str:
    """决策 id = `uuid5(account | 决策日 | 标的 | 方向)`。

    **确定性**是这里的全部要点：重放能复现同一个 id，落库因此天然幂等（UPSERT 不会产生重复行），
    而「今天这批决策」与「上次重放生成的那批」也能一对一地对上。
    """
    key = f"{account_id}|{trade_date.isoformat()}|{symbol}|{side.value}"
    return str(uuid.uuid5(NAMESPACE, key))


@dataclass(frozen=True, slots=True)
class Decision:
    """一张决策单：**同时是订单、日志与账本条目**（SPEC §7，M7 只读它）。

    `est_qty` 是按决策日收盘价**预估**的整手数；真正的成交股数 `fill.qty` 在次日开盘价上重算
    ——两个数都留着，跳空日的差额有据可查。
    """

    id: str
    account_id: str
    symbol: str
    trade_date: date  # 决策日（信号生成的交易日）
    side: Side
    est_qty: int
    est_price: float  # 决策日收盘价（预估依据）
    reason: str = ""
    event_id: str | None = None
    #: 来源三元组（由 `Signal.event_id` 回链事件行得到）；卖出（持有到期一类）**如实留空，不编**
    sources: Mapping[str, str] | None = None
    status: DecisionStatus = DecisionStatus.PENDING
    decided_at: datetime | None = None
    fill: Fill | None = None
    reject_code: str | None = None
    reject_reason: str | None = None

    @property
    def label(self) -> str:
        return STATUS_LABELS[self.status]

    def replace(self, **changes: object) -> Decision:
        """不可变对象的状态转移出口（`dataclasses.replace` 的薄封装，省掉到处 import）。"""
        from dataclasses import replace as _replace

        return _replace(self, **changes)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class PaperConfig:
    """会话创建时定死的全部参数——它与决策日志一起构成重放的全部输入。"""

    initial_cash: float
    symbols: tuple[str, ...]
    strategy: str
    start: date
    end: date
    params: Mapping[str, float | int] = field(default_factory=dict)
    costs: CostModel = field(default_factory=CostModel)
    #: 用户策略的名字（内置策略为 None）——进报告与界面，服务端不据此做分支
    strategy_name: str | None = None

    @property
    def quota(self) -> float:
        """每只标的的等额配额（SPEC §7）：初始资金 / 池子大小。单标的是 n=1 的特例。"""
        return self.initial_cash / len(self.symbols)


@dataclass(frozen=True, slots=True)
class AccountState:
    """账户的当前态（也就是落库的那几个数）。持仓只保留非空的。"""

    cash: float
    positions: Mapping[str, Position]
    realized_pnl: float = 0.0


@dataclass(frozen=True, slots=True)
class EquityMark:
    """一个交易日的收盘估值点。

    不复用回测的 `EquityPoint`：那个带一个单标的的 `close` 字段，多标的账户里它没有意义
    （把它填 0 或填某一只的收盘价，两种都是在撒谎）。
    """

    trade_date: date
    cash: float
    market_value: float
    equity: float


@dataclass(frozen=True, slots=True)
class DayOutcome:
    """一天推进下来发生了什么（供 `step` 的响应与前端逐日展示）。"""

    trade_date: date
    filled: tuple[Decision, ...] = ()
    expired: tuple[Decision, ...] = ()
    unfilled: tuple[Decision, ...] = ()
    generated: tuple[Decision, ...] = ()


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """重放的产物：账户状态 + 全量决策日志 + 净值曲线 + 逐日发生了什么。"""

    state: AccountState
    decisions: tuple[Decision, ...]
    equity_curve: tuple[EquityMark, ...]
    days: tuple[DayOutcome, ...]
    #: 每只标的的**涨跌停判定生效情况**（`engine.RuleStatus.to_dict()` 的形状）。
    #: 放这里而不是只留在内存里：降级（判不出板别 / 缺 raw 序列）必须如实带出去，
    #: 否则「规则跑了但没生效」在界面上看不出来（M5a 的 `meta.a_share_rules` 同一条理由）。
    rules: Mapping[str, Mapping[str, object]] = field(default_factory=dict)

    @property
    def as_of(self) -> date:
        return self.days[-1].trade_date

    @property
    def last(self) -> DayOutcome:
        return self.days[-1]
