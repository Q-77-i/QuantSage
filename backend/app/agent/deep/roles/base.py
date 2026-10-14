"""取证角色的公共底座：取数（代码）+ 解读（LLM）两层的分界都在这里。

**为什么角色不持工具**（SPEC §9 D5）：取数是确定性的代码，LLM 只做解读。这样一来
（1）注入面收窄——语料文本无法触发任何动作，（2）证据天然带来源三元组，
（3）角色可以离线用假模型完整测。

**降级分两层**（决定了 `RoleResult.errors` 的语义）：

* **可预期的降级** → 角色自己记 `errors`：RAG 不可用（`RagNotReady`）、解读超时/报错、
  该时点确实没有可见材料。此时**代码算出来的 `numbers` 与 `evidence` 照常保留**——
  降级不等于丢弃（行情数字本来就是代码算的，模型只是把它说成人话）。
* **意外异常**（数据源炸、解析抛）→ **原样上抛**，由 fan-out 的节点兜（M8b 的图）：
  一处收口胜过五个角色各写一遍 try/except，异常类型也不会在五处走样。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from langchain_core.language_models import BaseChatModel

from app.agent.deep.budget import Budget
from app.agent.deep.numbers import allowed_numbers, extract_numbers, untraceable_numbers
from app.agent.deep.types import ROLE_LABELS, Brief, EvidenceRef, RoleResult
from app.core.config import get_settings
from app.core import llm

#: 解读的统一硬要求：数字只能来自给定材料，不许算、不许编（claim 闸门在角色层的预演）
INTERPRET_RULES = (
    "硬要求：① 只能用上面出现过的数字，不得计算或引入任何外部信息；"
    "② 只写要点，不写投资建议、不预测股价；③ 每条不超过 60 字。"
)


@dataclass
class RoleContext:
    """一次取证的全部输入。`chat` 供离线测试注入假模型（不传则按配置现造真模型）。"""

    brief: Brief
    budget: Budget
    chat: BaseChatModel | None = None
    data_dir: Path | None = None
    fast_model: str = field(default_factory=lambda: get_settings().research_fast_model)
    deep_model: str = field(default_factory=lambda: get_settings().research_deep_model)
    timeout: float = field(default_factory=lambda: get_settings().research_role_timeout)
    thinking: bool = field(default_factory=lambda: get_settings().research_thinking)

    @property
    def symbols(self) -> tuple[str, ...]:
        return self.brief.symbols


def interpret_prompt(role: str, material: str) -> str:
    """角色解读的 prompt。**纯函数**：同样输入永远同样 prompt（可测、可复现）。"""
    label = ROLE_LABELS.get(role, role)
    return (
        f"你是 A 股{label}取证角色。下面是程序取回的材料。\n"
        f"请用 2–4 条要点概括它对投资研究的意义，每条以「- 」开头。\n"
        f"{INTERPRET_RULES}\n\n{material}"
    )


def allowed_for(
    *,
    material: str,
    numbers: Mapping[str, str],
    evidence: Sequence[EvidenceRef],
    symbols: Sequence[str],
) -> set[str]:
    """本次解读**允许出现**的数字全集。

    口径是「**只能引用材料里出现过的数字**」——prompt 就是这么向模型承诺的，
    闸门必须与承诺一致。所以要并进四类：材料全文（含日期与代码）、`numbers` 的键与值
    （标签里也有数字，如「20 日均线」）、证据的标题与摘要、以及标的代码本身。

    第一版只并了 `numbers` 的值，结果把 `600519`、`20`、`08-15` 全判成编造，
    五条要点被剔得只剩一条（真实数据实测）——**闸门写窄了比写宽了更糟**：
    它会把整份研报的内容清空，而放过的只是模型照抄。
    """
    allowed = allowed_numbers(numbers, [f"{e.title} {e.summary or ''}" for e in evidence])
    for text in (material, " ".join(symbols), *numbers.keys()):
        allowed.update(extract_numbers(str(text)))
    return allowed


async def interpret(
    role: str,
    ctx: RoleContext,
    *,
    material: str,
    numbers: Mapping[str, str] | None = None,
    evidence: Sequence[EvidenceRef] = (),
) -> RoleResult:
    """跑一次关思考的解读调用，并**逐条筛掉材料里根本没有的数字**。

    返回的 `RoleResult` 里：`findings` 已过数字闸门；`notes` 记被筛掉的条数与原因
    （降级必须留痕，不能静默少几条）。
    """
    import time

    numbers = dict(numbers or {})
    allowed = allowed_for(
        material=material, numbers=numbers, evidence=evidence, symbols=ctx.symbols
    )
    base = {
        "role": role,
        "numbers": numbers,
        "evidence": tuple(evidence),
        "model": ctx.fast_model,
    }

    started = time.perf_counter()
    try:
        result = await llm.complete(
            interpret_prompt(role, material),
            model=ctx.fast_model,
            timeout=ctx.timeout,
            chat=ctx.chat,
            thinking=ctx.thinking,
        )
    except TimeoutError:
        ctx.budget.record_wait(time.perf_counter() - started)
        return RoleResult(**base, errors=(f"解读超时（>{ctx.timeout:.0f}s）",))
    except Exception as exc:  # noqa: BLE001 —— 解读失败不拖垮取证
        ctx.budget.record_wait(time.perf_counter() - started)
        return RoleResult(**base, errors=(f"解读不可用（{type(exc).__name__}）",))

    seconds = time.perf_counter() - started
    ctx.budget.record_llm(ctx.fast_model, result.usage, seconds=seconds)

    kept: list[str] = []
    dropped: list[str] = []
    for line in result.text.splitlines():
        clean = line.strip().lstrip("-•·").strip()
        if not clean:
            continue
        stray = untraceable_numbers(clean, allowed)
        if stray:
            dropped.append(f"{clean}（无出处的数字：{'、'.join(stray)}）")
        else:
            kept.append(clean)

    notes: list[str] = []
    if dropped:
        notes.append(f"{len(dropped)} 条要点因数字无出处被剔除：" + "；".join(dropped))
    if not kept:
        notes.append("解读未产出可用要点（数字闸门或空输出）")
    return RoleResult(**base, findings=tuple(kept), notes=tuple(notes))


def event_ref(row: Mapping[str, object]) -> EvidenceRef:
    """语料行 → 证据。**PIT 双时间戳与来源三元组一并带上**（PRD §5 硬性要求）。"""
    event_time = row.get("event_time")
    available_at = row.get("available_at")
    return EvidenceRef(
        event_id=str(row.get("event_id") or ""),
        day=str(event_time)[:10],
        title=str(row.get("title") or ""),
        event_time=str(event_time) if event_time else None,
        available_at=str(available_at) if available_at else None,
        event_type=(str(row["event_type"]) if row.get("event_type") else None),
        summary=(str(row["summary"]) if row.get("summary") else None),
        source=(str(row["source"]) if row.get("source") else None),
        original_source=(str(row["original_source"]) if row.get("original_source") else None),
        source_url=(str(row["source_url"]) if row.get("source_url") else None),
        content_hash=(str(row["content_hash"]) if row.get("content_hash") else None),
        industries=tuple(row.get("industries") or ()),
        direction_norm=(str(row["direction_norm"]) if row.get("direction_norm") else None),
        importance_score=(
            float(row["importance_score"]) if row.get("importance_score") is not None else None
        ),
    )


def pit_filter(rows: Sequence[Mapping[str, object]], as_of: datetime) -> list[Mapping[str, object]]:
    """**PIT 闸门**：只看 `available_at <= as_of` 的行。

    比不了 `available_at` 的行（缺字段 / 解析不了）**一律丢掉**——「无法证明它当时可见」
    就是不可见，这条判据宁可少取不可错取（护城河在这一层的落点）。
    """
    kept: list[Mapping[str, object]] = []
    for row in rows:
        raw = row.get("available_at")
        if raw is None:
            continue
        try:
            stamp = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=as_of.tzinfo)
        if stamp <= as_of:
            kept.append(row)
    return kept
