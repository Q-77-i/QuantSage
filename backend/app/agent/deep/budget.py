"""预算管理器（M8）：四个计数器 + 硬上限，**到限在节点边界停**。

为什么要有它：辩论阶段的 token 是分析阶段的 3–5 倍（规划报告实测经验），而 M8 一次要跑
十几次调用——「成本在预算内」这条验收必须由一个**真的会拦**的东西保证，不是事后统计。

三个口径写死在这里：
  * **reasoning token 计入输出**（usage 的 `output_tokens` 本身就含它，别再单独加一次）；
  * **单价是一张本地表**（LiteLLM 注册表读数），不联网、不引依赖——价格变了改这张表；
  * **到限即停**：`exceeded()` 在每个 LLM 调用**之后**问一次，命中就停在节点边界，
    不打断已经发出去的调用（打断会留下半个响应，比多花一次钱更难处理）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

#: 每 token 美元价（输入, 输出），来源 LiteLLM 注册表（2026-10-11 读数）：
#: flash $0.30/M 入、$1.20/M 出；pro $1.32/M 入、$3.96/M 出。
PRICES: dict[str, tuple[float, float]] = {
    "deepseek/deepseek-flash": (0.30e-6, 1.20e-6),
    "deepseek/deepseek-v4-pro": (1.32e-6, 3.96e-6),
}


def estimate_cost_usd(model: str, usage: Mapping[str, Any] | None) -> float:
    """按本地价目表估算一次调用的美元成本。未知模型返回 0.0（**宁可少算，不猜价**）。"""
    if not usage:
        return 0.0
    price = PRICES.get(model)
    if price is None:
        return 0.0
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    return input_tokens * price[0] + output_tokens * price[1]


@dataclass
class Limits:
    """四计数器的上限。默认值取 `core/config.py`（SPEC §9 的 `RESEARCH_*`）。"""

    llm_calls: int = 40
    tokens: int = 200_000
    seconds: float = 300.0
    cost_usd: float = 0.50


@dataclass
class Budget:
    """累计用量 + 判限。`record_llm` 在**每次 LLM 调用之后**调一次。"""

    limits: Limits = field(default_factory=Limits)
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    elapsed_s: float = 0.0

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    def record_llm(
        self, model: str, usage: Mapping[str, Any] | None, *, seconds: float = 0.0
    ) -> None:
        """记一次调用。`usage` 直接给 `usage_metadata`（缺字段按 0 计，不抛）。"""
        self.llm_calls += 1
        self.tokens_in += int((usage or {}).get("input_tokens") or 0)
        self.tokens_out += int((usage or {}).get("output_tokens") or 0)
        self.cost_usd += estimate_cost_usd(model, usage)
        self.elapsed_s += max(0.0, seconds)

    def record_wait(self, seconds: float) -> None:
        """记一段**不含 LLM 的**等待（检索、取数）——墙钟预算要把它们算进去。"""
        self.elapsed_s += max(0.0, seconds)

    def exceeded(self) -> str | None:
        """返回第一个被突破的计数器名（`"llm_calls"` / `"tokens"` / `"seconds"` / `"cost_usd"`）。"""
        if self.llm_calls >= self.limits.llm_calls:
            return "llm_calls"
        if self.tokens >= self.limits.tokens:
            return "tokens"
        if self.elapsed_s >= self.limits.seconds:
            return "seconds"
        if self.cost_usd >= self.limits.cost_usd:
            return "cost_usd"
        return None

    def to_payload(self) -> dict[str, Any]:
        return {
            "llm_calls": self.llm_calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "tokens": self.tokens,
            "cost_usd": round(self.cost_usd, 6),
            "elapsed_s": round(self.elapsed_s, 2),
            "limits": {
                "llm_calls": self.limits.llm_calls,
                "tokens": self.limits.tokens,
                "seconds": self.limits.seconds,
                "cost_usd": self.limits.cost_usd,
            },
            "exceeded": self.exceeded(),
        }
