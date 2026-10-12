"""M5a A 股规则单测：板别幅度 / 涨跌停价 / 一字板判据 / 拒绝码矩阵。

**本模块是纯函数层**，全部用手搓 bar 断言，不碰行情数据——真实数据上的取证另有
一条（挑一只真一字板标的跑回测，见 `logs/m5a/limit.md`）。单测只钉口径。

板别前缀不是抄来的，是**实测全市场 `symbol` 前缀全集**（14 个，M2a 已核）：
`000/001/002/003`（深主板）、`300/301/302`（创业板）、`600/601/603/605`（沪主板）、
`688/689`（科创板）、`920`（北交所）。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.backtest.a_share_rules import (
    LOT_SIZE,
    LimitBand,
    LimitCounts,
    RejectCode,
    board_limit_pct,
    count_limit_hits,
    is_one_word_limit_down,
    is_one_word_limit_up,
    is_st_name,
    limit_band,
    limit_pct,
    order_reject,
    round_to_cent,
)
from app.backtest.types import Bar, Position, Side

DAY = date(2026, 8, 3)


def bar(
    *,
    open_price: float = 100.0,
    high: float | None = None,
    low: float | None = None,
    close: float | None = None,
    suspended: bool = False,
) -> Bar:
    """手搓一根 bar；`high`/`low`/`close` 缺省即「与开盘同价」（一字形态）。"""
    return Bar(
        symbol="600519",
        trade_date=DAY,
        open=open_price,
        high=open_price if high is None else high,
        low=open_price if low is None else low,
        close=open_price if close is None else close,
        volume=1e5,
        is_suspended=suspended,
    )


# ── 板别幅度 ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("000001", 0.10),
        ("001246", 0.10),
        ("002336", 0.10),
        ("003816", 0.10),
        ("600519", 0.10),
        ("601398", 0.10),
        ("603186", 0.10),
        ("605117", 0.10),
        ("300750", 0.20),
        ("301699", 0.20),
        ("302132", 0.20),
        ("688111", 0.20),
        ("689009", 0.20),
        ("920438", 0.30),
    ],
)
def test_board_limit_pct_covers_every_prefix_in_the_market(symbol: str, expected: float) -> None:
    assert board_limit_pct(symbol) == expected


def test_board_limit_pct_returns_none_for_unknown_prefix() -> None:
    """未知板别返回 None 而不是猜一个 10%——猜错会**误拦**真实的成交。

    行情宇宙只有上表那 14 个前缀，所以这条只在数据源引入新板别（或代码形状异常）时
    才会走到；到那时正确行为是**降级放行**（由调用方把 `LimitBand` 置 None），
    而不是拿主板幅度去套一只 30% 的北交所股票。
    """
    assert board_limit_pct("999999") is None
    assert board_limit_pct("123456") is None
    assert board_limit_pct("60051") is None  # 位数不对


def test_main_board_st_uses_the_measured_band() -> None:
    """主板 ST 走 **10%**（2026-10-12 实测定的，见 `_ST_MAIN_BOARD` 的读数表）。

    原先写 5% 是照惯例，被数据否掉了：全市场 168 只主板 ST 标的、1,341 个「前收 ≥10 元」的
    样本里，收盘精确落在 10% 档涨跌停价上的有 **57** 个，落在 5% 档上的只有 **3** 个。
    创业板 / 科创板 ST 仍是 20%、北交所 30%，这条不变。
    """
    assert limit_pct("600519", is_st=True) == 0.10
    assert limit_pct("000001", is_st=True) == 0.10
    assert limit_pct("300750", is_st=True) == 0.20
    assert limit_pct("688111", is_st=True) == 0.20
    assert limit_pct("920438", is_st=True) == 0.30
    # 非 ST 时与板别默认值同源
    assert limit_pct("600519") == board_limit_pct("600519")


def test_main_board_st_band_currently_collapses_into_the_board_band() -> None:
    """**如实钉住一个已知的怪处**：这一档目前与主板档同值，`is_st` 在数学上是空操作。

    写这条不是认可它，是让它显形——哪天有人把 `_ST_MAIN_BOARD` 改回 5%，
    这条会红，逼他回去看那张实测读数表。真要合并是一次跨 engine / paper / rules 的清理。
    """
    assert limit_pct("600519", is_st=True) == limit_pct("600519", is_st=False)


def test_unknown_board_stays_none_even_with_st() -> None:
    assert limit_pct("999999", is_st=True) is None


# ── ST 名称判据 ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    ["ST元道", "*ST南华", "  *st 华业", "ST长油", "*ST 沐邦"],
)
def test_is_st_name_accepts_risk_warning_names(name: str) -> None:
    assert is_st_name(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "万科A",  # 全角与空白已由调用方归一，这里只判前缀
        "华虹公司",
        "Starbucks",  # 语料里的美股名——**宽松的 startswith("ST") 会误判它**
        "Strategy",
        "STLA",  # ST 后接 ASCII 一律不算
        "ST",
        "",
        "   ",
    ],
)
def test_is_st_name_rejects_everything_else(name: str) -> None:
    assert is_st_name(name) is False


def test_is_st_name_handles_none() -> None:
    """名称字典允许缺名（覆盖率 99.2%，44 只没有）——缺名时按非 ST 处理。"""
    assert is_st_name(None) is False


# ── 涨跌停价 ────────────────────────────────────────────────


def test_round_to_cent_is_half_up_not_bankers() -> None:
    """交易所是四舍五入到分，Python 的 `round()` 是银行家舍入——两者在 .xx5 上分岔。

    `round(2.675, 2)` 在 Python 里给 **2.67**（二进制表示的锅，2.675 实际是 2.67499…），
    而正确的涨停价是 2.68。这一条挡的是「用 `round()` 算钱」这个经典错误。
    """
    assert round_to_cent(2.675) == 2.68
    assert round(2.675, 2) == 2.67  # 反证：内置 round 是错的
    assert round_to_cent(0.125) == 0.13  # 银行家舍入会给 0.12
    assert round_to_cent(11.225) == 11.23


@pytest.mark.parametrize(
    ("prev_close", "pct", "up", "down"),
    [
        (10.00, 0.10, 11.00, 9.00),
        (10.00, 0.20, 12.00, 8.00),
        (10.00, 0.05, 10.50, 9.50),
        (3.33, 0.10, 3.66, 3.00),  # 3.663 → 3.66；2.997 → 3.00（涨回整数分）
        (4.09, 0.30, 5.32, 2.86),  # 5.317 → 5.32；2.863 → 2.86
        (100.00, 0.10, 110.00, 90.00),
    ],
)
def test_limit_band_rounds_to_cent(
    prev_close: float, pct: float, up: float, down: float
) -> None:
    band = limit_band(prev_close, pct)
    assert band == LimitBand(up=up, down=down)


def test_limit_band_of_a_penny_stock_still_has_a_cent_step() -> None:
    """低价股的跌停价可能舍入到同一个分——如实给出，不做「至少差一分」的修正。"""
    band = limit_band(0.10, 0.10)
    assert band == LimitBand(up=0.11, down=0.09)


# ── 一字板判据 ──────────────────────────────────────────────


def test_one_word_limit_up_needs_open_and_low_pinned() -> None:
    band = limit_band(10.0, 0.10)  # 涨停 11.00
    assert is_one_word_limit_up(bar(open_price=11.0), band) is True


def test_open_below_limit_is_not_a_one_word_board() -> None:
    """**成对用例的另一半**：开盘没封住 → 不拦。单侧的拒单用例证明不了不误伤。"""
    band = limit_band(10.0, 0.10)
    assert is_one_word_limit_up(bar(open_price=10.80, low=10.50, high=11.0), band) is False


def test_limit_up_opened_then_broken_is_not_a_one_word_board() -> None:
    """开盘封在涨停但盘中打开过（`low` 掉下来）→ 当天买得到，不拦。"""
    band = limit_band(10.0, 0.10)
    assert is_one_word_limit_up(bar(open_price=11.0, low=10.60, high=11.0), band) is False


def test_one_word_limit_down_needs_open_and_high_pinned() -> None:
    band = limit_band(10.0, 0.10)  # 跌停 9.00
    assert is_one_word_limit_down(bar(open_price=9.0), band) is True
    assert is_one_word_limit_down(bar(open_price=9.0, high=9.40), band) is False


def test_price_tolerance_absorbs_sub_cent_noise() -> None:
    """落盘的 raw 价有 5,444 行不是整两位小数（实测）——半分容差吸收这点噪声。"""
    band = limit_band(10.0, 0.10)
    assert is_one_word_limit_up(bar(open_price=11.0000001, low=10.9999998), band) is True
    assert is_one_word_limit_up(bar(open_price=10.999), band) is True
    assert is_one_word_limit_up(bar(open_price=10.99), band) is False


# ── 拒绝码矩阵 ──────────────────────────────────────────────


def test_suspended_bar_is_rejected_for_both_sides() -> None:
    flat = Position()
    for side in (Side.BUY, Side.SELL):
        assert order_reject(side, bar(suspended=True), band=None, bar_index=0, position=flat) == (
            RejectCode.SUSPENDED
        )


def test_non_positive_open_is_rejected_as_suspended() -> None:
    """开盘价非正同样是「按开盘价撮合不了」——归入同一个码，不新造一个。"""
    flat = Position()
    assert order_reject(Side.BUY, bar(open_price=0.0), band=None, bar_index=0, position=flat) == (
        RejectCode.SUSPENDED
    )


def test_one_word_limit_up_blocks_buy_but_not_sell() -> None:
    """一字涨停：买不到（没人卖），但**卖得掉**——所以只拦买单。"""
    band = limit_band(10.0, 0.10)
    locked = bar(open_price=11.0)
    held = Position(shares=100, entry_price=10.0, entry_index=0)
    assert order_reject(Side.BUY, locked, band=band, bar_index=5, position=Position()) == (
        RejectCode.LIMIT_UP
    )
    assert order_reject(Side.SELL, locked, band=band, bar_index=5, position=held) is None


def test_one_word_limit_down_blocks_sell_but_not_buy() -> None:
    """一字跌停：卖不掉（没人接），但**买得到**——所以只拦卖单。"""
    band = limit_band(10.0, 0.10)
    locked = bar(open_price=9.0)
    held = Position(shares=100, entry_price=10.0, entry_index=0)
    assert order_reject(Side.SELL, locked, band=band, bar_index=5, position=held) == (
        RejectCode.LIMIT_DOWN
    )
    assert order_reject(Side.BUY, locked, band=band, bar_index=5, position=Position()) is None


def test_missing_band_degrades_to_no_limit_check() -> None:
    """缺 raw 序列（或除权日）时 `band` 为 None → 跳过涨跌停判定，**不静默按 qfq 猜**。

    降级必须是「放行 + 标注」，不是「拒单」——拒单会让回测凭空少掉一批成交，
    而少成交是看不出来的（报告里只少几笔，没有痕迹）。
    """
    locked = bar(open_price=11.0)
    assert order_reject(Side.BUY, locked, band=None, bar_index=5, position=Position()) is None


def test_t1_blocks_same_bar_sell_only() -> None:
    """T+1 只限制卖出，且只在「买与卖落在同一根 bar」时成立。

    当前引擎结构上不可能触发（信号在 bar 收盘生成、成交在下一根开盘），这条守卫是
    **把结构保证写成可测判据**——将来若有人让成交落在信号当根，这里会红。
    """
    band = limit_band(10.0, 0.10)
    calm = bar(open_price=10.50, low=10.20, high=10.80)
    same_bar = Position(shares=100, entry_price=10.0, entry_index=7)
    assert order_reject(Side.SELL, calm, band=band, bar_index=7, position=same_bar) == (
        RejectCode.T1
    )
    assert order_reject(Side.BUY, calm, band=band, bar_index=7, position=Position()) is None
    # 隔了一根就放行
    assert order_reject(Side.SELL, calm, band=band, bar_index=8, position=same_bar) is None


def test_t1_not_triggered_by_flat_or_unindexed_position() -> None:
    band = limit_band(10.0, 0.10)
    calm = bar(open_price=10.50, low=10.20, high=10.80)
    assert order_reject(Side.SELL, calm, band=band, bar_index=7, position=Position()) is None
    assert (
        order_reject(
            Side.SELL,
            calm,
            band=band,
            bar_index=7,
            position=Position(shares=100, entry_price=10.0),
        )
        is None
    )


def test_suspension_wins_over_limit_when_both_apply() -> None:
    """停牌优先：判据顺序写死（停牌 → 涨跌停 → T+1），免得同一根 bar 报两种码。"""
    band = limit_band(10.0, 0.10)
    both = bar(open_price=11.0, suspended=True)
    assert order_reject(Side.BUY, both, band=band, bar_index=0, position=Position()) == (
        RejectCode.SUSPENDED
    )


def test_lot_size_is_one_hundred() -> None:
    """最小手数是 A 股规则的一部分，常量放在本模块做单一真源（broker 从这import）。"""
    assert LOT_SIZE == 100


def test_reject_codes_are_the_five_in_spec() -> None:
    """SPEC §6 M5a 点名的五个码——UI 直接吃它，改名要同步改 SPEC。"""
    assert [code.value for code in RejectCode] == [
        "REJECT_T1",
        "REJECT_LIMIT_UP",
        "REJECT_LIMIT_DOWN",
        "REJECT_SUSPENDED",
        "REJECT_LOT",
    ]


# ── M10：收盘触板家数（大盘总览用，与一字板是两件事）────────────────

def _row(symbol: str, close: float, prev_close: float | None) -> dict[str, object]:
    return {"symbol": symbol, "close": close, "prev_close": prev_close}


class TestCountLimitHits:
    def test_主板涨停与跌停各命中(self) -> None:
        got = count_limit_hits(
            [_row("600519", 110.0, 100.0), _row("600000", 90.0, 100.0), _row("601398", 101.0, 100.0)]
        )
        assert got.to_json() == {"limit_up": 1, "limit_down": 1, "judged": 3, "skipped": 0}

    def test_涨停价按分四舍五入(self) -> None:
        """前收 10.03 → 涨停价 11.033 → 舍到 **11.03**（银行家舍入会得 11.03 也可能 11.04，
        故实现走 Decimal ROUND_HALF_UP；这条钉住结果）。"""
        got = count_limit_hits([_row("600519", 11.03, 10.03)])
        assert got.up == 1

    def test_差一分不算涨停(self) -> None:
        """容差是**半分**：109.99 够不着 110.00 的涨停价。"""
        assert count_limit_hits([_row("600519", 109.99, 100.0)]).up == 0

    @pytest.mark.parametrize(
        ("symbol", "close", "hit"),
        [
            ("300750", 110.0, False),   # 创业板 10% 够不着 20% 档
            ("300750", 120.0, True),
            ("688111", 120.0, True),
            ("920001", 130.0, True),    # 北交所 30% 档
            ("920001", 120.0, False),
        ],
    )
    def test_板别档位逐条(self, symbol: str, close: float, hit: bool) -> None:
        """防的是「一律按 10% 判」——那会把创业板的 +10% 误报成涨停。"""
        assert (count_limit_hits([_row(symbol, close, 100.0)]).up == 1) is hit

    def test_ST集合不再改变主板读数(self) -> None:
        """主板 ST 已实测为 10%（同板块档），故 `st_symbols` 对主板标的**不再改变结果**。

        这条取代了原来那条「ST 档成对样例」（5% 时代它证明 ST 档真的生效）。
        底线仍然钉住：**传不传 ST 集合，判据都不会崩、也不会把没封板的数成封板**。
        创业板 ST 另有一条（若按 5% 误算，+10% 会被数成涨停）。"""
        row = [_row("600519", 105.0, 100.0)]
        assert count_limit_hits(row).up == 0
        assert count_limit_hits(row, st_symbols={"600519"}).up == 0
        # 真封在 10% 档上：两种传法都得数出来
        limit_up = [_row("600519", 110.0, 100.0)]
        assert count_limit_hits(limit_up).up == 1
        assert count_limit_hits(limit_up, st_symbols={"600519"}).up == 1

    def test_ST只压主板(self) -> None:
        """创业板 ST 仍是 20%——把 300750 当 ST 不该让 +10% 变成涨停。"""
        assert count_limit_hits([_row("300750", 110.0, 100.0)], st_symbols={"300750"}).up == 0

    def test_缺前收跳过并计数(self) -> None:
        got = count_limit_hits([_row("600519", 110.0, None)])
        assert got.to_json() == {"limit_up": 0, "limit_down": 0, "judged": 0, "skipped": 1}

    def test_未知板别跳过而不是当成没涨停(self) -> None:
        """猜一个 10% 会**误判**——宁可少一个数，也不给一个假数。"""
        got = count_limit_hits([_row("999999", 110.0, 100.0)])
        assert got.judged == 0 and got.skipped == 1

    def test_缺名不算跳过(self) -> None:
        """缺名只让 ST 档不生效，判据本身仍成立——那不是「判不了」。"""
        assert count_limit_hits([_row("600519", 110.0, 100.0)], st_symbols=set()).skipped == 0

    def test_空输入不炸(self) -> None:
        assert count_limit_hits([]) == LimitCounts(up=0, down=0, judged=0, skipped=0)

    def test_涨停不重复计入跌停(self) -> None:
        """一根极端 bar 不会同时算进两端（elif 而非两个 if）。"""
        got = count_limit_hits([_row("600519", 110.0, 100.0)])
        assert got.up == 1 and got.down == 0

    def test_收盘跑到区间外面算跳过而不是封板(self) -> None:
        """**除权日防误判**（2026-10-12 实测逮到，真数据：300980 祥源新材）。

        raw 前收 22.90 → 收盘 15.98 是 −30%，超出创业板 20% 的物理上限——它当日有除权，
        而 raw 前收没反映除权参考价。交易所的真实跌停价约 14.72，收盘 15.98 **根本没封板**；
        但用虚高的前收算出的跌停价是 18.32，单侧比较（`close <= down`）会把它数成跌停。

        封板的定义是「收盘 = 涨跌停价」，不是「够得着」。够不着与跑到外面是两件事：
        前者是没封板（judged），后者是序列不可比（skipped）。
        """
        # 22.90 × 0.8 = 18.32；15.98 远在它之下 ⇒ 跳过，不计入跌停
        got = count_limit_hits([_row("300980", 15.98, 22.90)])
        assert got.down == 0
        assert got.skipped == 1
        assert got.judged == 0

    def test_区间内没封板算判过而不是跳过(self) -> None:
        """反面：正常跌 3% 的标的**判过了**（judged），不是「判不了」——别把两件事混起来。"""
        got = count_limit_hits([_row("300980", 97.0, 100.0)])
        assert got.judged == 1 and got.skipped == 0 and got.down == 0

    def test_高于涨停价也算跳过(self) -> None:
        """上侧同理：复权序列上「涨过头」同样是不可比，不是涨停。"""
        got = count_limit_hits([_row("600519", 130.0, 100.0)])
        assert got.up == 0 and got.skipped == 1
