"""M8a 数字可追溯（`app.agent.deep.numbers`）：角色层对 claim 闸门的预演。

这条闸门的方向性很明确——**假阴性（编造的数字过关）比假阳性贵**。所以用例里既有
「该拦的必须拦」，也有「不该误报的三类」（行首序号、证据原文里的数字、全角写法）。
"""

from __future__ import annotations

from app.agent.deep.numbers import allowed_numbers, extract_numbers, untraceable_numbers

EVIDENCE = [
    "中标金额 12.3 亿元，约占 2025 年营收 8%",
    "控股股东拟减持不超过 2% 股份",
]


def test_leading_list_markers_are_not_numbers() -> None:
    """行首序号不是数字——不剥掉的话每条要点都误报（真实模型就是这么写的）。"""
    assert extract_numbers("1. 中标 12.3 亿元\n2、减持不超 2%\n3) 需补证据") == [
        "12.3亿元", "2%",
    ]


def test_suffixes_and_fullwidth_are_normalized() -> None:
    assert extract_numbers("涨了 8%（12.3亿元）、2.5 倍") == ["8%", "12.3亿元", "2.5倍"]
    assert extract_numbers("涨幅 ８％") == ["8%"]


def test_allowed_set_covers_own_numbers_and_evidence_text() -> None:
    allowed = allowed_numbers({"区间收益": "12.34%", "成交额": "3.2 亿元"}, EVIDENCE)

    assert {"12.34%", "3.2亿元", "12.3亿元", "8%", "2025", "2%"} <= allowed


def test_fabricated_number_is_flagged_once_and_in_order() -> None:
    allowed = allowed_numbers({"区间收益": "12.34%"}, EVIDENCE)
    text = "3. 区间收益 12.34%，中标 12.3 亿元；若叠加 9.9% 的溢价则更可观，另有 9.9% 的上行空间。"

    assert untraceable_numbers(text, allowed) == ["9.9%"]


def test_clean_text_passes() -> None:
    allowed = allowed_numbers({"区间收益": "12.34%"}, EVIDENCE)
    text = "1. 中标 12.3 亿元（占 2025 年营收 8%）\n2. 拟减持不超 2%\n3. 两者方向相反，需补履约证据。"

    assert untraceable_numbers(text, allowed) == []


def test_unit_added_by_the_model_is_not_a_fabrication() -> None:
    """真数据实测：语料表格里单位在表头（`主力资金流量（万元）`）、值是 `67969.50`，
    模型复述时补上单位写成 `67969.50万元`——它没编造任何东西，判它违规是假阳性。"""
    assert untraceable_numbers("主力资金流量 67969.50万元", allowed_numbers({}, ["主力资金流量（万元） 67969.50"])) == []
    # 反向也成立：证据写全、模型只写数值
    assert untraceable_numbers("净流入 67969.50", allowed_numbers({}, ["净流入 67969.50 万元"])) == []


def test_number_absent_from_evidence_and_numbers_is_flagged() -> None:
    """角色没把数字写进 `numbers`、证据里也没有——那就是编的（哪怕它"看起来合理"）。"""
    assert untraceable_numbers("预计上行 15%", allowed_numbers({}, EVIDENCE)) == ["15%"]
