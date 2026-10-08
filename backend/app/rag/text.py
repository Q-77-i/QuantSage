"""嵌入文本构造：元数据 header + 标题 + 摘要。

这是「上下文增强的零成本版」（规划报告 §五 决策 2）：不花 LLM 调用、不额外存储，
把标题、时间、方向、重要度、行业、标的拼在正文前——财经文本「标题即主题」，
元数据拼接已经吃掉了大部分收益，且**稀疏（BM25 类）分支同样受益**。

三条口径：

1. **空字段不写标签**：公告 83% 摘要为空（实测），无脑拼会留下 `【行业】\n` 这类空壳，
   既占 token 又给稀疏分支塞进无意义的 token。
2. **时间为 `event_time` 的日期**（不含时分）：评测集有「时间型」问题（如「9 月 15 日的
   政策新闻」），日期进文本才能被检索到；时分秒对检索无增益，只会拉长文本。
3. **标的用六位码**：`symbols` 已是归一化结果（M2b），与用户提问里的代码同形。

**这份构造是 spike 量吞吐时用的同一格式**——文本一改，SPEC §4 里的全量嵌入耗时估算
就要重测，别把它当成可以随手调的样式。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def build_embedding_text(row: Mapping[str, Any]) -> str:
    """一行语料 → 嵌入文本。字段缺失/为空时整段不出现。"""
    parts: list[str] = []

    title = _clean(row.get("title"))
    if title:
        parts.append(f"【标题】{title}")

    head: list[str] = []
    if _clean(row.get("event_type")):
        head.append(f"【类型】{_clean(row['event_type'])}")
    event_time = row.get("event_time")
    if event_time is not None:
        head.append(f"【时间】{str(event_time)[:10]}")
    if _clean(row.get("direction_norm")):
        head.append(f"【方向】{_clean(row['direction_norm'])}")
    score = row.get("importance_score")
    if isinstance(score, (int, float)):
        head.append(f"【重要度】{float(score):.1f}")
    industries = [v for v in (_clean(x) for x in (row.get("industries") or ())) if v]
    if industries:
        head.append(f"【行业】{'/'.join(industries)}")
    symbols = [v for v in (_clean(x) for x in (row.get("symbols") or ())) if v]
    if symbols:
        head.append(f"【标的】{'/'.join(symbols)}")
    if head:
        parts.append(" ".join(head))

    summary = _clean(row.get("summary"))
    if summary:
        parts.append(summary)

    return "\n".join(parts)
