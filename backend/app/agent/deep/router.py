"""M8a 意图路由：快路径（现有 ReAct 图）⇄ 深路径（多 Agent）。

两条纪律：

1. **失败一律回落 quick**。路由是省钱的手段，不是必须成功的一步——解析失败、模型报错、
   超时、模型没配，全都当 quick（宁可少花钱，不可卡住用户）。显式 `mode=deep` 跳过判定。
2. **标的名解析不花 LLM**（F7 实测：字典加载 0.21s / 5,611 只 / 零重名）。六位码走正则，
   全名精确匹配，简称走包含匹配；**长名压短名**（「中国平安」命中后，短名不再把「平安银行」
   拉进来）。解析不出就如实记一条 note，不编。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Any

from langchain_core.language_models import BaseChatModel

from app.agent.deep.types import Brief
from app.core.llm import DEFAULT_MODEL, complete
from app.data.naming import load_dictionary, normalize_name, primary_names

PATH_QUICK = "quick"
PATH_DEEP = "deep"
PATHS = (PATH_QUICK, PATH_DEEP)

#: 六位代码（前后不接数字，避免把长数字串里的六位截出来）
_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")

#: 一次解析最多带回几只标的（超了就是「问题太宽」，让它先说清楚）
MAX_SYMBOLS = 5

#: 路由的默认超时：分类是个小任务，8s 还回不来就该走 quick 了
ROUTE_TIMEOUT = 8.0

ROUTE_PROMPT = """你是投研助手的前置路由。判断下面这个问题该走哪条路：

- quick：事实查询、单点检索、闲聊、寒暄（一句话能答的）
- deep：需要多角度取证与权衡的研究型问题（例如「值不值得买」「帮我看看风险」「出一份分析」）

只输出一个 JSON 对象，不要任何其他文字：
{{"path": "quick" 或 "deep", "reason": "一句话理由", "scope": "关注面（可空字符串）"}}

问题：{question}"""


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """路由结论。`symbols` 是**代码解析出来的**，不是模型说的（模型只管快/深）。"""

    path: str
    reason: str
    symbols: tuple[str, ...] = ()
    scope: str = ""
    notes: tuple[str, ...] = ()
    usage: dict[str, Any] = field(default_factory=dict)

    @property
    def deep(self) -> bool:
        return self.path == PATH_DEEP

    def to_payload(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "reason": self.reason,
            "symbols": list(self.symbols),
            "scope": self.scope,
            "notes": list(self.notes),
        }


@lru_cache(maxsize=1)
def name_index() -> dict[str, str]:
    """名称 → 代码（精确名）。字典缺失时返回空表——调用方按「没有名字」处理，不炸。"""
    return {normalize_name(name): symbol for symbol, name in primary_names(load_dictionary()).items()}


#: 片段匹配的长度范围：短到 2（「茅台」这样的简称），长到 8（够覆盖常见全名）
FRAGMENT_MIN = 2
FRAGMENT_MAX = 8

#: 同一个长度上命中超过这个数 = 歧义（例如问题里只写了「科技」），**不猜**
MAX_FRAGMENT_HITS = 3


def _build_fragments(table: dict[str, str]) -> tuple[dict[str, tuple[str, ...]], dict[str, str]]:
    """建两张表：片段 → 代码（每个名字的**全部前缀与后缀**，长度 2..8，含全名本身），以及 代码 → 名字。

    建一次的钱（5,611 只 × ~14 个键）比每次扫全表便宜，且让匹配变成几次字典查找。
    名字表是给排序用的——排序键是**名字长度**，不是命中片段长度（真字典实测：
    按片段长度排会把「值得买」排在「贵州茅台」前面）。
    """
    frags: dict[str, list[str]] = {}
    names: dict[str, str] = {}
    for name, symbol in table.items():
        if len(name) < FRAGMENT_MIN:
            continue
        names.setdefault(symbol, name)
        upper = min(FRAGMENT_MAX, len(name))
        for size in range(FRAGMENT_MIN, upper + 1):
            for fragment in {name[:size], name[-size:]}:
                bucket = frags.setdefault(fragment, [])
                if symbol not in bucket:
                    bucket.append(symbol)
    return {fragment: tuple(symbols) for fragment, symbols in frags.items()}, names


@lru_cache(maxsize=1)
def _global_fragments() -> tuple[dict[str, tuple[str, ...]], dict[str, str]]:
    return _build_fragments(name_index())


def match_names(
    text: str,
    table: dict[str, str] | None = None,
    *,
    limit: int = MAX_SYMBOLS,
    min_fragment: int = FRAGMENT_MIN,
) -> tuple[tuple[str, ...], str | None]:
    """名字片段匹配。返回 `(代码元组, 如实标注或 None)`。

    两条实测得来的规则（都别删）：

    1. **包含去冗**：文本里同时命中「中国平安」与「平安」时，后者不提供任何额外信息，
       把被包含的片段丢掉——否则「中国平安」会连同「平安银行」一起被拉进来。
    2. **候选并列时如实全列，不猜**：真字典里存在「值得买」（300785）这样的公司名——
       「茅台还值得买吗」会同时命中它和「贵州茅台」。宁可在 `note` 里把候选说清楚，
       也不擅自丢掉用户真正问的那只。命中过多（> `MAX_FRAGMENT_HITS`）才是歧义：返回空 + 标注。
    """
    fragments, names = _global_fragments() if table is None else _build_fragments(table)
    if not fragments:
        return (), None

    matched: dict[str, set[str]] = {}
    for size in range(min(FRAGMENT_MAX, len(text)), min_fragment - 1, -1):
        for start in range(len(text) - size + 1):
            for symbol in fragments.get(text[start : start + size], ()):
                matched.setdefault(symbol, set()).add(text[start : start + size])
    if not matched:
        return (), None

    every = {fragment for frags in matched.values() for fragment in frags}
    effective = {
        symbol: {f for f in frags if not any(f != other and f in other for other in every)}
        for symbol, frags in matched.items()
    }
    hits = [symbol for symbol, frags in effective.items() if frags]
    if not hits:
        return (), None
    if len(hits) > MAX_FRAGMENT_HITS:
        return (), f"名字片段命中 {len(hits)} 只标的，歧义不猜——请用代码或全名"

    # 长名优先（「贵州茅台」排在「值得买」前），同长按代码稳定排序
    hits.sort(key=lambda symbol: (-len(names.get(symbol, "")), symbol))
    chosen = tuple(hits[:limit])
    if len(hits) > 1:
        labels = "、".join(f"{names.get(s, s)}({s})" for s in chosen)
        return chosen, f"命中 {len(hits)} 只候选：{labels}——按名字长度排序，请确认"
    return chosen, None


def resolve_symbols(
    text: str, *, index: dict[str, str] | None = None, limit: int = MAX_SYMBOLS
) -> tuple[tuple[str, ...], str | None]:
    """从问题里解析标的：**六位码优先**，其次名字片段匹配（不花 LLM，F7）。

    **代码出现时名字匹配只认 ≥3 字片段**：代码已经把标的锚死了，此时 2 字片段只会添噪音——
    真实字典实测「帮我看下 600519 的事件驱动机会」会把「捷昌驱动」「驱动力」一起拉进来
    （「驱动」既是前者的后缀又是后者的前缀）。
    """
    found: list[str] = []

    def _push(symbol: str) -> None:
        if symbol not in found:
            found.append(symbol)

    codes = _CODE_RE.findall(text)
    for code in codes:
        _push(code)

    names, note = match_names(
        normalize_name(text), index, limit=limit, min_fragment=3 if codes else FRAGMENT_MIN
    )
    for symbol in names:
        _push(symbol)

    if not found:
        return (), note or "问题里没解析出标的"
    return tuple(found[:limit]), note


def extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出第一个 JSON 对象。容忍 ``` 围栏与前后闲话（真实模型两种都会写）。"""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


async def route_question(
    question: str,
    *,
    chat: BaseChatModel | None = None,
    model: str = DEFAULT_MODEL,
    timeout: float = ROUTE_TIMEOUT,
    force: str | None = None,
    index: dict[str, str] | None = None,
) -> RouteDecision:
    """判定快 / 深路径。**任何失败都回落 quick 并把原因写在 `reason` 里**（降级必须留痕）。"""
    symbols, note = resolve_symbols(question, index=index)
    notes = (note,) if note else ()

    if force in PATHS:
        return RouteDecision(force, "请求指定路径", symbols, notes=notes)

    try:
        result = await complete(ROUTE_PROMPT.format(question=question), model=model, timeout=timeout, chat=chat)
    except TimeoutError:
        return RouteDecision(PATH_QUICK, f"路由超时（>{timeout:.0f}s），回落快路径", symbols, notes=notes)
    except Exception as exc:  # noqa: BLE001 —— 路由失败不该让提问失败
        return RouteDecision(
            PATH_QUICK, f"路由不可用（{type(exc).__name__}），回落快路径", symbols, notes=notes
        )

    payload = extract_json(result.text)
    if payload is None:
        return RouteDecision(
            PATH_QUICK, "路由输出无法解析，回落快路径", symbols, notes=notes, usage=result.usage
        )
    path = payload.get("path")
    if path not in PATHS:
        return RouteDecision(
            PATH_QUICK, f"路由给出未知路径 {path!r}，回落快路径", symbols, notes=notes, usage=result.usage
        )
    return RouteDecision(
        path,
        str(payload.get("reason") or "").strip() or "（模型未给理由）",
        symbols,
        scope=str(payload.get("scope") or "").strip(),
        notes=notes,
        usage=result.usage,
    )


def build_brief(
    question: str,
    *,
    decision: RouteDecision,
    as_of: datetime,
    symbols: tuple[str, ...] | None = None,
) -> Brief:
    """把问题 + 路由结论拼成 `Brief`。`symbols` 显式给了就以它为准（请求参数优先）。"""
    chosen = symbols if symbols else decision.symbols
    notes = decision.notes if chosen else (*decision.notes, "未指定标的：按问题原文取证")
    return Brief(
        question=question,
        symbols=tuple(chosen),
        as_of=as_of,
        scope=decision.scope,
        notes=tuple(notes),
    )
