"""M5a 名称字典单测：解析 / 归一化 / 三重过滤 / 众数选名 / 时点查询。

**本段的负样例矩阵是「一名多写 11 例对照表」**（`_REAL_CONFLICTS`）——它的数字是
2026-10-09 在全量语料（92 天 / 303,887 行）上实测出来的，不是编的。真实语料上的
复现证据另有一条脚本产出到 `logs/m5a/naming.md`；这里钉的是**规则**，防止它被改坏。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.data.naming import (
    NameRow,
    build_rows,
    name_as_of,
    normalize_code,
    normalize_name,
    parse_stocks,
    primary_names,
)

CN = ZoneInfo("Asia/Shanghai")


def ts(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=CN)


# ── 代码归一化（与 ETL 的 `store._NORMALIZED_CODE` 同规则）────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("600519", "600519"),
        ("000001", "000001"),
        ("601179.SH", "601179"),  # 沪市后缀剥掉
        ("000858.SZ", "000858"),  # 深市后缀剥掉
        ("005930.KS", ""),  # 韩股：不是 A 股后缀 → 丢弃
        ("603186.SS", ""),  # `.SS` 不在 ETL 白名单内（既有行为，照抄）
        ("0700.HK", ""),
        (None, ""),
        ("", ""),
        ("ST元道", ""),  # 只有名字没有码
    ],
)
def test_normalize_code_mirrors_etl_rule(raw: object, expected: str) -> None:
    assert normalize_code(raw) == expected


# ── 名称归一化 ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("万  科Ａ", "万科A"),  # 全角 Ａ → 半角 A，空白去掉
        ("万科A", "万科A"),
        ("XD戈碧迦", "戈碧迦"),  # 除权前缀
        ("XR某某", "某某"),
        ("DR某某", "某某"),
        ("N电科思", "电科思"),  # 新股首日前缀
        ("C洛轴", "洛轴"),  # 新股次日前缀
        ("华虹公司", "华虹公司"),
        ("华测检测", "华测检测"),
        ("*ST 南华", "*ST南华"),  # 空白去掉，但 **ST 不能剥**
        ("ST元道", "ST元道"),
        ("宁德时代", "宁德时代"),  # 首字是汉字「宁」，不是字母 N——不许误剥
        ("Samsung", "Samsung"),  # 拉丁字母开头不动
        ("Strategy", "Strategy"),
        ("", ""),
        (None, ""),
        ("  ", ""),
    ],
)
def test_normalize_name(raw: object, expected: str) -> None:
    assert normalize_name(raw) == expected


def test_normalize_name_only_strips_prefix_before_a_chinese_character() -> None:
    """`N` / `C` / `XD` 一类前缀只在**后面紧跟汉字**时才剥。

    语料里混着美股名（`STLA` / `Starbucks`），若按「以 N 开头就剥」的宽松写法，
    `NIO` 会变成 `IO`。这类条目最终会被行情宇宙过滤掉，但归一化层不该先造出脏数据。
    """
    assert normalize_name("NIO") == "NIO"
    assert normalize_name("CDNS") == "CDNS"
    assert normalize_name("XD") == "XD"


# ── stocks 列解析 ───────────────────────────────────────────


def test_parse_stocks_reads_code_and_name() -> None:
    raw = '[{"code":"300012","name":"华测检测","reason":null}]'
    assert parse_stocks(raw) == [("300012", "华测检测")]


def test_parse_stocks_keeps_multiple_entries() -> None:
    raw = '[{"code":"600519","name":"贵州茅台"},{"code":"000858","name":"五粮液"}]'
    assert parse_stocks(raw) == [("600519", "贵州茅台"), ("000858", "五粮液")]


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        None,
        "",
        "not json",
        '[{"code":null,"name":"SK海力士"}]',  # 只有名没有码
        '[{"code":"005930.KS","name":"SK海力士"}]',  # 韩股代码
        '[{"code":"300728","name":null}]',  # 只有码没有名
        '[{"code":"159937"}]',  # ETF 也是六位码，靠宇宙过滤挡（这里只验解析不崩）
    ],
)
def test_parse_stocks_degrades_to_empty(raw: object) -> None:
    result = parse_stocks(raw)
    assert all(code and name for code, name in result)
    if raw in ("[]", None, "", "not json", '[{"code":null,"name":"SK海力士"}]',
               '[{"code":"005930.KS","name":"SK海力士"}]', '[{"code":"300728","name":null}]'):
        assert result == []


def test_parse_stocks_accepts_an_already_decoded_list() -> None:
    """落盘是单层 JSON 文本；真出现已解码的 list 也不该炸（防御，非主路径）。"""
    assert parse_stocks([{"code": "600519", "name": "贵州茅台"}]) == [("600519", "贵州茅台")]


# ── 三重过滤 + 合并 ─────────────────────────────────────────


def record(stocks: str, symbols: list[str], available_at: str) -> dict[str, object]:
    return {"stocks": stocks, "symbols": symbols, "available_at": ts(available_at)}


def test_build_rows_requires_the_code_in_the_same_events_symbols() -> None:
    """过滤 ① ：同一事件的 `symbols` 里没有这个码，就不认这条 `stocks`。

    这条挡的是归档里的港股/美股条目——`stocks` 写 `{"code":"005930.KS","name":"SK海力士"}`，
    同事件的 `symbols` 是空的。少了这一条，字典会凭空多出一批外部标的。
    """
    rows = build_rows(
        [record('[{"code":"600519","name":"贵州茅台"}]', [], "2026-07-10 10:00:00")],
        universe={"600519"},
    )
    assert rows == []


def test_build_rows_requires_the_code_in_the_universe() -> None:
    """过滤 ② ：不在行情宇宙里的码（ETF、港股、已退市）一律不进字典。"""
    rows = build_rows(
        [
            record('[{"code":"159937","name":"博时黄金ETF"}]', ["159937"], "2026-07-10 10:00:00"),
            record('[{"code":"600519","name":"贵州茅台"}]', ["600519"], "2026-07-10 10:00:00"),
        ],
        universe={"600519"},
    )
    assert [row.symbol for row in rows] == ["600519"]


def test_build_rows_merges_normalized_names_and_counts() -> None:
    """归一化后同名的合并计数——`万  科Ａ` 与 `万科A` 是同一条。"""
    rows = build_rows(
        [
            record('[{"code":"000002","name":"万  科Ａ"}]', ["000002"], "2026-07-11 09:00:00"),
            record('[{"code":"000002","name":"万科A"}]', ["000002"], "2026-07-12 09:00:00"),
            record('[{"code":"000002","name":"万  科Ａ"}]', ["000002"], "2026-07-13 09:00:00"),
        ],
        universe={"000002"},
    )
    assert rows == [
        NameRow(
            symbol="000002",
            name="万科A",
            count=3,
            first_seen=ts("2026-07-11 09:00:00"),
            last_seen=ts("2026-07-13 09:00:00"),
        )
    ]


def test_build_rows_records_available_at_bounds_per_name() -> None:
    """**名称带时点**：改名在语料里是两段，各自有 `available_at` 边界。

    用的是 `available_at` 而不是 `event_time`——PIT 立场在展示层的延伸：
    「平台什么时候开始这么叫它」才是可得的时刻。
    """
    rows = build_rows(
        [
            record('[{"code":"688347","name":"华虹宏力"}]', ["688347"], "2026-07-09 08:00:00"),
            record('[{"code":"688347","name":"华虹公司"}]', ["688347"], "2026-07-20 08:00:00"),
            record('[{"code":"688347","name":"华虹公司"}]', ["688347"], "2026-09-30 08:00:00"),
        ],
        universe={"688347"},
    )
    assert rows == [
        NameRow("688347", "华虹宏力", 1, ts("2026-07-09 08:00:00"), ts("2026-07-09 08:00:00")),
        NameRow("688347", "华虹公司", 2, ts("2026-07-20 08:00:00"), ts("2026-09-30 08:00:00")),
    ]


def test_build_rows_is_deterministic_in_output_order() -> None:
    rows = build_rows(
        [
            record('[{"code":"000858","name":"五粮液"}]', ["000858"], "2026-07-10 08:00:00"),
            record('[{"code":"600519","name":"贵州茅台"}]', ["600519"], "2026-07-10 08:00:00"),
        ],
        universe={"600519", "000858"},
    )
    assert [row.symbol for row in rows] == ["000858", "600519"]


# ── 众数选名：11 例一名多写对照表（实测）────────────────────


#: 2026-10-09 在全量语料上实测的 11 例一名多写：`标的名 → [(归一化名, 出现次数)]`，
#: **次数降序**（故首项即众数选出的那个）。前 6 例是源侧错配（把别家公司或海外公司的
#: 名字挂到了这个码上），后 5 例是前缀与全角形态。
#:
#: 注意这是**过三重过滤之前**的候选分布。真实语料上加过滤后只剩 5 只仍带别名
#: （`688111` / `600876` / `600778` / `000830` 的错配名连 `symbols` 都没进，直接消失；
#: `920438` / `000002` 被归一化合并）。这里刻意用**未过滤**的分布跑，是因为本文件验的是
#: 「众数规则选得对不对」——过滤是 `build_rows` 的职责，另有 `test_build_rows_*` 那组覆盖。
_REAL_CONFLICTS: dict[str, list[tuple[str, int]]] = {
    "688111": [("金山办公", 52), ("华虹宏力", 1)],  # 源把华虹宏力挂错到金山办公的码上
    "600800": [("渤海化学", 40), ("新华传媒", 1)],
    "600876": [("凯盛新能", 29), ("农夫山泉", 1)],  # 农夫山泉是港股
    "600778": [("友好集团", 17), ("美光科技", 1)],  # 美光科技是美股
    "000830": [("鲁西化工", 27), ("Samsung", 1)],  # 三星的韩股码被写成裸六位
    "688347": [("华虹公司", 104), ("华虹宏力", 5)],  # 真实改名，新名占多数
    "301689": [("电科思仪", 64), ("电科思", 5)],  # N 前缀剥掉后仍差一个字
    "920229": [("世纪数码", 43), ("世纪", 1)],  # N 前缀
    "920438": [("戈碧迦", 91), ("戈碧迦", 1)],  # XD 前缀剥掉后与正名合并
    "000002": [("万科A", 81), ("万科A", 1)],  # 全角与空白归一后合并
    "301699": [("洛轴", 39), ("洛轴股份", 12)],
}

#: 人工核对后的**正确名**（同一天逐个查过）。与 `_REAL_CONFLICTS` 分开写，是为了让
#: 「规则怎么选」与「选得对不对」是两件事——合在一起会让断言变成同义反复。
_CORRECT_NAMES: dict[str, str] = {
    "688111": "金山办公",
    "600800": "渤海化学",
    "600876": "凯盛新能",
    "600778": "友好集团",
    "000830": "鲁西化工",
    "688347": "华虹公司",
    "301689": "电科思仪",
    "920229": "世纪数码",
    "920438": "戈碧迦",
    "000002": "万科A",
    "301699": "洛轴股份",
}


def _conflict_rows() -> list[NameRow]:
    return [
        NameRow(symbol, name, count, ts("2026-07-10 08:00:00"), ts("2026-09-30 08:00:00"))
        for symbol, candidates in _REAL_CONFLICTS.items()
        for name, count in candidates
    ]


@pytest.mark.parametrize(("symbol", "candidates"), sorted(_REAL_CONFLICTS.items()))
def test_primary_name_takes_the_most_frequent_candidate(
    symbol: str, candidates: list[tuple[str, int]]
) -> None:
    rows = [row for row in _conflict_rows() if row.symbol == symbol]
    assert primary_names(rows)[symbol] == candidates[0][0]


def test_the_eleven_conflicts_come_down_to_exactly_one_known_miss() -> None:
    """11 例里**只有 301699 一例与人工核对不符**——这条把「不宣称 100% 准」写成了断言。

    301699 归一化后是「洛轴」（39 次，新股 `C` 前缀期的截断名）与「洛轴股份」（12 次）
    并存，众数取到前者。要修得引入「前缀期不算数」的时序规则，而全市场只有这一例，
    性价比不划算——**如实记着，比偷偷对了更重要**。
    """
    resolved = primary_names(_conflict_rows())
    wrong = {
        symbol: resolved[symbol]
        for symbol, correct in _CORRECT_NAMES.items()
        if resolved[symbol] != correct
    }
    assert wrong == {"301699": "洛轴"}


def test_conflict_fixture_is_ordered_by_count_descending() -> None:
    """夹具本身的自检：首项必须是众数，否则上面两条断言会指向别处。"""
    for symbol, candidates in _REAL_CONFLICTS.items():
        assert candidates == sorted(candidates, key=lambda item: -item[1]), symbol


def test_primary_name_breaks_ties_by_recency() -> None:
    """次数并列时取 `last_seen` 晚的——业务上「最近还在这么叫」比字典序合理。"""
    rows = [
        NameRow("600000", "旧名", 3, ts("2026-07-10 08:00:00"), ts("2026-07-20 08:00:00")),
        NameRow("600000", "新名", 3, ts("2026-08-01 08:00:00"), ts("2026-09-30 08:00:00")),
    ]
    assert primary_names(rows) == {"600000": "新名"}


# ── as-of 查询 ──────────────────────────────────────────────


def test_name_as_of_returns_the_name_current_at_that_moment() -> None:
    """改名前后各查一次——这是「名称带时点」的最小可验证形态。"""
    rows = [
        NameRow("688347", "华虹宏力", 5, ts("2026-07-09 08:00:00"), ts("2026-07-10 08:00:00")),
        NameRow("688347", "华虹公司", 104, ts("2026-07-20 08:00:00"), ts("2026-09-30 08:00:00")),
    ]
    assert name_as_of(rows, "688347", ts("2026-07-15 00:00:00")) == "华虹宏力"
    assert name_as_of(rows, "688347", ts("2026-08-01 00:00:00")) == "华虹公司"


def test_name_as_of_returns_none_before_any_observation() -> None:
    """时点早于所有观测 → 没有名字是**答案**，不回退到「当前名」（那会造假历史）。"""
    rows = [
        NameRow("600519", "贵州茅台", 10, ts("2026-07-10 08:00:00"), ts("2026-09-30 08:00:00"))
    ]
    assert name_as_of(rows, "600519", ts("2026-07-01 00:00:00")) is None
    assert name_as_of(rows, "000001", ts("2026-08-01 00:00:00")) is None


def test_name_as_of_prefers_the_most_recently_seen_on_ties() -> None:
    rows = [
        NameRow("600000", "甲名", 2, ts("2026-07-10 08:00:00"), ts("2026-07-12 08:00:00")),
        NameRow("600000", "乙名", 2, ts("2026-07-11 08:00:00"), ts("2026-08-01 08:00:00")),
    ]
    assert name_as_of(rows, "600000", ts("2026-07-20 00:00:00")) == "乙名"


# ── 落盘与读取 ──────────────────────────────────────────────


def test_dictionary_round_trip(tmp_path) -> None:
    from app.data import naming

    rows = [
        NameRow("600519", "贵州茅台", 7, ts("2026-07-10 08:00:00"), ts("2026-09-30 08:00:00")),
        NameRow("688347", "华虹公司", 104, ts("2026-07-20 08:00:00"), ts("2026-09-30 08:00:00")),
    ]
    path = naming.write_dictionary(rows, tmp_path)
    assert path == tmp_path / naming.DICT_SUBDIR / naming.DICT_FILE
    assert naming.load_dictionary(tmp_path) == rows


def test_write_dictionary_is_byte_identical_on_rerun(tmp_path) -> None:
    """**幂等**：同一份输入重写必须逐字节一致，否则 ETL 的「重跑不产生新版本」不成立。"""
    from app.data import naming

    rows = build_rows(
        [record('[{"code":"600519","name":"贵州茅台"}]', ["600519"], "2026-07-10 08:00:00")],
        universe={"600519"},
    )
    first = naming.write_dictionary(rows, tmp_path).read_bytes()
    second = naming.write_dictionary(rows, tmp_path).read_bytes()
    assert first == second


def test_load_dictionary_returns_empty_when_missing(tmp_path) -> None:
    """字典缺失不是错误：它只影响展示与 ST 幅度档，不该让回测跑不起来。"""
    from app.data import naming

    assert naming.load_dictionary(tmp_path) == []


def test_st_symbols_reads_the_current_name(tmp_path) -> None:
    """ST 判定走字典的**当前名**，判据复用 a_share_rules 的那一条。"""
    from app.data import naming

    rows = [
        NameRow("000004", "ST国华", 9, ts("2026-07-10 08:00:00"), ts("2026-09-30 08:00:00")),
        NameRow("600519", "贵州茅台", 9, ts("2026-07-10 08:00:00"), ts("2026-09-30 08:00:00")),
        NameRow("688111", "金山办公", 9, ts("2026-07-10 08:00:00"), ts("2026-09-30 08:00:00")),
    ]
    assert naming.st_symbols(rows) == {"000004"}


def test_st_symbols_is_empty_without_a_dictionary() -> None:
    from app.data import naming

    assert naming.st_symbols([]) == set()
