"""A 股交易规则（M5a）：板别涨跌停幅度、涨跌停价、一字板判据、拒单原因码。

**纯函数层**——不 import 引擎 / broker / 数据层，也不做 I/O，故可以被逐条手搓
bar 断言（`tests/test_a_share_rules.py`）。引擎与 broker 从这里取判定，不各自实现一份。

三处口径是**实测定下来的**，不是照抄惯例：

1. **涨跌停价从 raw 前收自算，不用 `change_pct`**。实测 `change_pct` 与 raw 序列自算的
   日收益有 **0.3% 偏差**（10,785 / 360 万行，集中在除权日）+ **4.8% 缺失**——拿它算
   涨跌停价会算错。代价是引擎要为涨幅判定多读一条 raw 序列（缺它就降级，见 `order_reject`）。
2. **四舍五入到分用 `Decimal`，不用内置 `round()`**。交易所是四舍五入，Python 的
   `round` 是银行家舍入且在二进制表示上还会失真（`round(2.675, 2) == 2.67`）。
3. **ST 判据要求 `ST` / `*ST` 后接汉字**。语料里混着 `Starbucks` / `Strategy` /
   `STLA` 一类美股名，宽松的 `startswith("ST")` 会把它们判成风险警示股；
   实测「后接汉字」这一条挡掉全部 11 个假阳性，而 250 个真 ST 名一个不少。

**一条贯穿全部降级决策的不对称性**：幅度**判大**只会**漏判**（真实涨停价低于算出来的，
价格够不着），幅度**判小**才会**误拦**（把没封板的日子当成封死）。所以凡是不确定的地方
一律往「大」或「跳过」走——这就是未知板别返回 `None`（而不是猜 10%）、缺名一律按非 ST
（而不是猜 ST）的理由。

**已知边界（如实记录，不假装覆盖）**：
  * **除权日会漏判**（不误判）——分红送转会下调交易所的除权参考价，而 raw 前收还没反映它，
    于是算出的涨跌停价**偏高**，真实的一字板够不着。识别它要比对 qfq/raw 比值跳变，
    成本高收益低，故不做。（缩股一类**上调**参考价的极端除权未验证。）
  * **ST 用「回测终点时刻的名字」判**——名称只在事件语料覆盖期内（2026-07 起）有；更早的
    回测窗口 `name_as_of` 返回 None，一律按板块默认幅度。同样是**判大**方向，只漏判。
  * **上市首日与退市整理期不特判**——前者涨跌幅不受板别限制（实测 raw 极值 +1942%），
    后者规则另有口径。名称字典能认出 `N` / `C` 前缀与「退市」，但历史区间没有名称。
  * **一字板只拦「全日封死」**（`open` 与 `low` 都贴住涨停价）。开盘封涨停、盘中开板的日子
    照常按开盘价成交——引擎只按开盘价撮合，这是简化，写在这里而不是藏起来。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from app.backtest.types import Bar, Position, Side

#: A 股整手股数。broker 从这里 import——最小手数是 A 股规则的一部分，
#: 常量留两份必然漂移。
LOT_SIZE = 100

#: 报价最小变动单位（元）。落盘 raw 价实测有 5,444 行不是整两位小数，
#: 故贴价判定用**半分容差**而不是等值比较。
TICK = 0.005


class RejectCode(StrEnum):
    """拒单原因码。UI 直接吃它，故取值与 SPEC §6 M5a 点名的五个一一对应。"""

    T1 = "REJECT_T1"
    LIMIT_UP = "REJECT_LIMIT_UP"
    LIMIT_DOWN = "REJECT_LIMIT_DOWN"
    SUSPENDED = "REJECT_SUSPENDED"
    LOT = "REJECT_LOT"


#: 原因码 → 展示文本。放这里而不是散在引擎里：文案与码是一套词汇，分开放必然漂移。
REJECT_REASONS: dict[RejectCode, str] = {
    RejectCode.T1: "T+1：当日买入不可当日卖出",
    RejectCode.LIMIT_UP: "一字涨停：全天无卖盘，买单无法成交",
    RejectCode.LIMIT_DOWN: "一字跌停：全天无买盘，卖单无法成交",
    RejectCode.SUSPENDED: "停牌或开盘价非正，无法按开盘价成交",
    RejectCode.LOT: "资金不足以买入一手（100 股）",
}


# 板别 → 涨跌停幅度。键是**实测的全市场代码前缀全集**（14 个）：
# 深主板 000/001/002/003、创业板 300/301/302（302 全市场仅 1 只）、
# 沪主板 600/601/603/605、科创板 688/689、北交所 920。
_MAIN_BOARD = 0.10
_GROWTH_BOARD = 0.20
_BSE = 0.30
_ST_MAIN_BOARD = 0.05

_BOARD_LIMITS: dict[str, float] = {
    "000": _MAIN_BOARD,
    "001": _MAIN_BOARD,
    "002": _MAIN_BOARD,
    "003": _MAIN_BOARD,
    "600": _MAIN_BOARD,
    "601": _MAIN_BOARD,
    "603": _MAIN_BOARD,
    "605": _MAIN_BOARD,
    "300": _GROWTH_BOARD,
    "301": _GROWTH_BOARD,
    "302": _GROWTH_BOARD,
    "688": _GROWTH_BOARD,
    "689": _GROWTH_BOARD,
    "920": _BSE,
}

#: 归一到「无空白 + 大写」后的风险警示前缀；**后面必须跟一个汉字**（见模块 docstring 第 3 条）。
_ST_PATTERN = re.compile(r"^\*?ST[一-鿿]")


def round_to_cent(value: float) -> float:
    """四舍五入到分（交易所口径）。

    走 `Decimal(str(value))` 而不是 `Decimal(value)`：后者会把二进制浮点的误差
    原样带进来（`Decimal(2.675)` 是 2.67499999…），照样舍错。
    """
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def board_limit_pct(symbol: str) -> float | None:
    """按代码前缀给出该板的涨跌停幅度；**认不出就返回 None**（调用方降级）。

    不猜一个默认值：猜错会**误拦**真实成交，而误拦在报告里没有痕迹（只少几笔成交）。
    位数也一并卡住——`"60051"` 前三位看似主板，但它不是本项目的标的编码。
    """
    if len(symbol) != 6 or not symbol.isdigit():
        return None
    return _BOARD_LIMITS.get(symbol[:3])


def limit_pct(symbol: str, *, is_st: bool = False) -> float | None:
    """该标的当日的涨跌停幅度。

    ST 的 5% **只对主板成立**：创业板 / 科创板的 ST 股仍是 20%（注册制后），
    北交所不设 ST 幅度档。所以这里只把主板的 10% 压到 5%。
    """
    board = board_limit_pct(symbol)
    if board is None:
        return None
    if is_st and board == _MAIN_BOARD:
        return _ST_MAIN_BOARD
    return board


def is_st_name(name: str | None) -> bool:
    """名称是否为风险警示（`ST` / `*ST`）。

    缺名（字典覆盖率 99.2%，44 只没有）一律**按非 ST 处理**——宁可少一个 5% 档，
    不可凭猜测把一只正常主板股的幅度砍半（那会把 10% 的日子误判成涨停）。
    """
    if not name:
        return False
    # `str.split()` 无参时按 Unicode 空白切分，全角空格（\\u3000）也在内
    normalized = "".join(name.split()).upper()
    return _ST_PATTERN.match(normalized) is not None


@dataclass(frozen=True, slots=True)
class LimitBand:
    """某标的某日的涨跌停价（元）。"""

    up: float
    down: float


def limit_band(prev_close: float, pct: float) -> LimitBand:
    """由**前收盘价（raw 口径）**与幅度算涨跌停价，四舍五入到分。"""
    base = Decimal(str(prev_close))
    ratio = Decimal(str(pct))
    return LimitBand(
        up=round_to_cent(float(base * (1 + ratio))),
        down=round_to_cent(float(base * (1 - ratio))),
    )


def _at_price(price: float, target: float) -> bool:
    return abs(price - target) <= TICK


def is_one_word_limit_up(bar: Bar, band: LimitBand) -> bool:
    """一字涨停：开盘与最低价都贴住涨停价——**全天没人卖**，买单成交不了。

    只看 `open` 与 `low`：若 `low` 已在涨停价，`high` 必然也在（涨停价就是当日上限），
    所以再比 `high` 是冗余的。
    """
    return _at_price(bar.open, band.up) and _at_price(bar.low, band.up)


def is_one_word_limit_down(bar: Bar, band: LimitBand) -> bool:
    """一字跌停：开盘与最高价都贴住跌停价——**全天没人接**，卖单成交不了。"""
    return _at_price(bar.open, band.down) and _at_price(bar.high, band.down)


def order_reject(
    side: Side,
    bar: Bar,
    *,
    band: LimitBand | None,
    bar_index: int,
    position: Position,
) -> RejectCode | None:
    """这张单能否按**该 bar 的开盘价**成交；不能则给出原因码，能则返回 None。

    判据顺序写死为 **停牌 → 涨跌停 → T+1**，免得同一根 bar 报出两种码。

    `band=None` 表示当日涨跌停价不可得（缺 raw 序列、除权日、未知板别），
    **跳过涨跌停判定**。降级必须是「放行 + 标注」而不是「拒单」——拒单会让回测
    凭空少掉一批成交，而少成交在报告里看不出来。
    """
    if bar.is_suspended or bar.open <= 0:
        return RejectCode.SUSPENDED

    if band is not None:
        if side is Side.BUY and is_one_word_limit_up(bar, band):
            return RejectCode.LIMIT_UP
        if side is Side.SELL and is_one_word_limit_down(bar, band):
            return RejectCode.LIMIT_DOWN

    # T+1：当日买入不可当日卖出。`entry_index` 是成交 bar 的下标，与本次撮合落在
    # 同一根即违规。当前引擎结构上不可能触发（信号在收盘生成、成交在下一根开盘），
    # 这条守卫是**把结构保证写成可测判据**——将来有人让成交落在信号当根，它会红。
    if side is Side.SELL and position.entry_index == bar_index:
        return RejectCode.T1

    return None


__all__ = [
    "LOT_SIZE",
    "REJECT_REASONS",
    "TICK",
    "LimitBand",
    "RejectCode",
    "board_limit_pct",
    "is_one_word_limit_down",
    "is_one_word_limit_up",
    "is_st_name",
    "limit_band",
    "limit_pct",
    "order_reject",
    "round_to_cent",
]
