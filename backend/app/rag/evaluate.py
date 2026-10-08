"""评测指标：NDCG@10（主）/ MRR / Recall@20，自实现（不引 `pytrec_eval`）。

三条口径：

1. **相关性是分级的**（0 不相关 / 1 沾边 / 2 正解）：二值化会把「次优结果」与「完全无关」
   压成同一个数，精排的增益就看不出来了——而精排正是本项目要验的一跳。
2. **NDCG 的 IDCG 按「本次评测集里该 query 的理想排序」算**：即把所有已标注的相关项
   按分数降序排。没有标注到的文档一律视为 0，这与「池化式评测」的口径一致
   （池子外的文档无法判断，只能当不相关——这是自建评测集的已知局限，写进报告）。
3. **指标只吃「已排序的文档键」与「判定表」**，不碰检索对象——纯函数，可离线算手写样例。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

#: 文档键：`日|event_id`。**不能只用 event_id**——平台会跨月复用（实测 18 例）。
Key = str

DEFAULT_K = 10


def doc_key(day: str, event_id: str) -> Key:
    return f"{day}|{event_id}"


@dataclass(frozen=True, slots=True)
class JudgedQuery:
    """一条评测样本：query + 判定表（文档键 → 相关性等级）。"""

    id: str
    category: str
    query: str
    judgments: Mapping[Key, int]
    truth: Key | None = None

    @property
    def relevant(self) -> set[Key]:
        return {key for key, grade in self.judgments.items() if grade > 0}


def dcg(grades: Sequence[int]) -> float:
    """折损累计增益：`Σ (2^g - 1) / log2(i + 1)`，i 从 1 起。"""
    return sum((2**g - 1) / math.log2(i + 1) for i, g in enumerate(grades, start=1))


def ndcg_at_k(ranked: Sequence[Key], judgments: Mapping[Key, int], k: int = DEFAULT_K) -> float:
    """NDCG@k。理想排序 = 判定表里所有相关项按等级降序（池化口径）。"""
    gains = [int(judgments.get(key, 0)) for key in ranked[:k]]
    best = sorted((int(g) for g in judgments.values()), reverse=True)[:k]
    ideal = dcg(best)
    return dcg(gains) / ideal if ideal > 0 else 0.0


def mrr(ranked: Sequence[Key], judgments: Mapping[Key, int], *, threshold: int = 1) -> float:
    """第一个「达到阈值」的文档的倒数名次；一个都没有则 0。"""
    for index, key in enumerate(ranked, start=1):
        if int(judgments.get(key, 0)) >= threshold:
            return 1.0 / index
    return 0.0


def recall_at_k(
    ranked: Sequence[Key], judgments: Mapping[Key, int], k: int = 20, *, threshold: int = 1
) -> float:
    relevant = {key for key, grade in judgments.items() if int(grade) >= threshold}
    if not relevant:
        return 0.0
    hit = len(relevant & set(ranked[:k]))
    return hit / len(relevant)


@dataclass
class ModeScore:
    """一档管线的分数：逐条明细 + 汇总。"""

    mode: str
    per_query: dict[str, dict[str, float]] = field(default_factory=dict)

    def add(self, query_id: str, scores: Mapping[str, float]) -> None:
        self.per_query[query_id] = dict(scores)

    @property
    def mean(self) -> dict[str, float]:
        if not self.per_query:
            return {}
        names = sorted({name for row in self.per_query.values() for name in row})
        return {
            name: sum(row.get(name, 0.0) for row in self.per_query.values()) / len(self.per_query)
            for name in names
        }

    def by_category(self, categories: Mapping[str, str]) -> dict[str, dict[str, float]]:
        """按类别汇总——五类问题对检索的压力完全不同，只看总分会被大类掩盖。"""
        buckets: dict[str, list[dict[str, float]]] = {}
        for query_id, row in self.per_query.items():
            buckets.setdefault(categories.get(query_id, "未知"), []).append(row)
        out: dict[str, dict[str, float]] = {}
        for category, rows in buckets.items():
            names = sorted({name for row in rows for name in row})
            out[category] = {
                name: sum(row.get(name, 0.0) for row in rows) / len(rows) for name in names
            }
        return out


def score_query(ranked: Sequence[Key], judged: JudgedQuery, *, k: int = DEFAULT_K) -> dict[str, float]:
    """一条 query 的三项指标。"""
    return {
        f"ndcg@{k}": ndcg_at_k(ranked, judged.judgments, k),
        "mrr": mrr(ranked, judged.judgments),
        "recall@20": recall_at_k(ranked, judged.judgments, 20),
    }


def compare_modes(scores: Iterable[ModeScore], *, baseline: str, target: str) -> dict[str, float]:
    """逐指标 diff：`target - baseline`。验收看的是这个差，不是某一档的绝对值。"""
    table = {score.mode: score.mean for score in scores}
    if baseline not in table or target not in table:
        return {}
    return {
        name: table[target].get(name, 0.0) - table[baseline].get(name, 0.0)
        for name in sorted(set(table[baseline]) | set(table[target]))
    }
