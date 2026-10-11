"""数字可追溯（纯函数）：角色层对 claim 闸门的预演。

规则一句话：**角色写的每个数字，要么在它自己的 `numbers` 里，要么在它读到的证据原文里**。
两处都没有的数字 = 编的，标出来（M8b 的风控闸门据此要求重写或降级）。

两个必须处理的形状（都是真跑会撞上的）：
  * **列表序号**不是数字（`1.` / `2、` 开头的行首标记先剥掉），否则每条要点都误报；
  * 数字常带后缀（`12.3亿元` / `8%` / `2.5 倍`），比对时**去掉空白后整体比**，不做算术归一
    （`0.5` 与 `50%` 是同一个量的两种写法，但那要角色自己写进 `numbers`——这里不替它猜）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

#: 数字 + 常见后缀（含中文量词）。**不做算术归一**，只做字面比对。
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:%|‰|亿元|亿|万元|万|元|倍|pp|个基点)?")

#: 行首列表标记（`1. ` / `2、` / `3) ` / `- `）——剥掉再扫，否则序号会被当成数字。
#: **点号后不能跟数字**：`12.34%` 是数字不是序号（第一版写宽了，`12.34%` 被剥成 `34%`，
#: 是这条用例逮出来的）；同理 `-`/`*`/`·` 后必须跟空白，否则 `-5.2%` 会被吃掉负号。
_LIST_MARKER_RE = re.compile(
    r"^[ \t]*(?:\d+[、)]|\d+\.(?!\d)|[-*·](?=[ \t]))[ \t]*", flags=re.MULTILINE
)


#: 全角 → 半角（中文模型偶发全角数字与百分号；不归一就会**漏判**，而漏判的方向是
#: 「编造的数字过关」——这道闸门的假阴性比假阳性贵）
_FULLWIDTH = str.maketrans("０１２３４５６７８９％．", "0123456789%.")


def extract_numbers(text: str) -> list[str]:
    """从正文里抽数字 token（已剥行首序号、全角归一、去空白）。保序、含重复。"""
    stripped = _LIST_MARKER_RE.sub("", str(text).translate(_FULLWIDTH))
    return [_normalize(match.group(0)) for match in _NUMBER_RE.finditer(stripped)]


def _normalize(token: str) -> str:
    return re.sub(r"\s+", "", token)


#: 数字主体（去掉后缀）：供「`67969.50万元` 与 `67969.50` 同源」这类比对用
_CORE_RE = re.compile(r"\d+(?:\.\d+)?")


def numeric_core(token: str) -> str:
    """`12.3亿元` → `12.3`；没有数字时原样返回。

    **为什么按主体比**：语料的表格里单位在表头（`主力资金流量（万元）`），值是 `67969.50`；
    模型复述时补上单位写成 `67969.50万元`——它没有编造任何东西，判它违规是假阳性
    （真实数据实测逮到）。代价是「同一数字换单位」也会放行（`12.3亿元` vs `12.3%`），
    这类改写更少见、危害也更小（真值仍在 `numbers` 里，M8b 的风控闸门是第二层）。
    """
    found = _CORE_RE.match(token)
    return found.group(0) if found else token


def allowed_numbers(
    numbers: Mapping[str, str] | None = None, evidence_texts: Sequence[str] = ()
) -> set[str]:
    """本次调用允许出现的数字全集 = 角色自己的 `numbers` 值 ∪ 证据原文里的数字。

    每个 token **连同它的数字主体**一起进集合（`12.3亿元` 同时贡献 `12.3亿元` 与 `12.3`），
    比对时两侧都按主体核（见 `numeric_core`）。
    """
    allowed: set[str] = set()
    for value in (numbers or {}).values():
        allowed.update(extract_numbers(str(value)))
    for text in evidence_texts:
        allowed.update(extract_numbers(text))
    return allowed | {numeric_core(token) for token in allowed}


def untraceable_numbers(text: str, allowed: set[str]) -> list[str]:
    """正文里**找不到出处**的数字（去重、保序）。空列表 = 全可追溯。"""
    seen: list[str] = []
    for token in extract_numbers(text):
        if token in allowed or numeric_core(token) in allowed:
            continue
        if token not in seen:
            seen.append(token)
    return seen
