"""策略静态检查器（M4b）：不执行代码，只读 AST，把问题前移到运行前。

分工（SPEC §5 开篇）：**主闸门是结构**——用户策略只拿到 `ctx`，`history` 截断到当前 bar、
`events` 已过 PIT 闸门、命名空间里没有 pandas / 文件 / 网络，未来在 API 形状上不可达。本模块是
**第二道门**，干三件实事：① 把数据绕行（`import duckdb` 读全量 Parquet 自己索引未来）在运行前
拦住并给行号；② 把从别的框架搬来的未来函数（`shift(-1)` / `bfill`）变成可读报错——不然用户只会
拿到 `AttributeError: 'tuple' object has no attribute 'shift'`；③ 声明与代码是否一致这类工程检查。
**它不是安全边界**（同 `app/strategy/__init__.py`）：禁用名靠 AST 命中，`getattr` / `type` /
`object` 仍在运行时白名单里；对外表述不得把 AST 写成主闸门。

规则与级别（SPEC §5 M4b）：`error` 命中即拒绝执行，`warning` 照跑但显示。

| 规则 | 级别 | 判据 |
|---|---|---|
| R0 语法 | error | `ast.parse` 失败——此后 R1–R5 全不跑 |
| R1 数据绕行 | error | 白名单外的 `import` / 相对导入；出现 `open` / `eval` / `exec` / `compile` / `__import__` / `importlib` / `__builtins__` |
| R2 未来函数 | error | `.shift(<负整数>)`、`.bfill()` / `.backfill()`、`fillna(method="bfill"/"backfill")` |
| R3 显式未来索引 | error / warning | 见下 |
| R4 声明 | error / warning | `PARAMS` / `USES_EVENTS` / `validate_params` 的形态（`parse_meta` 判）；`USES_EVENTS` 与是否真读事件不一致 |
| R5 结构 | error | 入口缺失 / `async` / 形参不是 `(ctx)` 或 `(ctx, p)` / 返回值形态 |

**R3 的覆盖边界（如实记录，不假装全覆盖）**：

- 只看基对象是 `ctx.history` / `ctx.events` / `ctx.new_events` 的**非切片**下标。自有列表上的
  `closes[i + 1]` **不报**——静态没有数据流，分不清它来自 `ctx.history` 还是用户自己攒的列表，
  报了就会误伤「比较相邻两根 bar」这种合法写法（`for i in range(len(closes) - 1): closes[i + 1]`）
- **双档**：`error` 只留给「明说当前 + n」的写法（`ctx.index + 正数` / `len(ctx.history) + 正数`），
  其它名字 + 正数降 warning。理由是代价不对称：R3 拦不住真实泄漏（未来在物理上不可达），而
  `error` 会让合法策略在「运行前强制 error=0」下**跑不起来**
- `ctx` 名从 `on_bar` 第一个形参取（改名也认）；helper 里另起名字接 `ctx` 会漏
- 只认可加性表达式（`i + 1`、`i + 1 + 2`）；`i + n`（n 是变量）静态判不了，不报
- 切片一律放行：`ctx.history[index - n + 1: index + 1]` 里的 `+1` 只是排除上界

`ffill` / `pad` 与 `.shift(正数)` **不拦**（2026-10-08 拍板）：前向填充与回看只用过去值，
不是未来函数。
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from app.strategy import StrategyRejected
from app.strategy.api import ALLOWED_MODULES, SOURCE_NAME, parse_meta

ERROR = "error"
WARNING = "warning"

#: 语法错的规则号。它建不出 AST（没有 `Module` 节点可分发给它），所以不进 `RULES`——
#: 见 `check_source` 的第一段。
SYNTAX_RULE = "R0"

ALL_RULE_IDS: tuple[str, ...] = (SYNTAX_RULE, "R1", "R2", "R3", "R4", "R5")

#: 明令禁止的名字。**与运行时同向**：这些名字运行时也不给（`api._SAFE_BUILTINS` 里没有它们，
#: `importlib` 也不在预注入模块里）——检查器不会拦下运行期本来跑得起来的东西。
#: `__builtins__` 在沙箱里是**受限字典**（取不到 `open` / `eval`），拦它只是为了给出比
#: `KeyError` 好懂得多的报错。
FORBIDDEN_NAMES = frozenset(
    {"__import__", "open", "eval", "exec", "compile", "importlib", "__builtins__"}
)

#: R3 关注的 ctx 数据属性：回看型数据都在这里
CTX_DATA_ATTRS = frozenset({"history", "events", "new_events"})

#: R4 判「代码到底吃不吃事件」
EVENT_ATTRS = frozenset({"events", "new_events"})


@dataclass(frozen=True, slots=True)
class Finding:
    """一条检查结果。`message` 是给人读的中文话术，`snippet` 是命中那一行的原文。"""

    rule: str
    severity: str
    line: int
    message: str
    snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "line": self.line,
            "message": self.message,
            "snippet": self.snippet,
        }


@dataclass(frozen=True, slots=True)
class Rule:
    """一条规则：判据写在 `match` 里，分发**只按 `node_types` 精确匹配**——新增规则不改执行器。

    `match(node, state) -> str | None` 返回**详情串**（`None` = 未命中）；`id` / `severity` /
    `line` / `snippet` 由遍历器统一组装。同一条规则拆成多条 `Rule` 是允许的（R3 的两个档、
    R1 的两个臂都这么写）——`Finding.rule` 仍然是同一个规则号。
    """

    id: str
    severity: str
    node_types: tuple[type[ast.AST], ...]
    message: str
    match: Callable[[Any, "_Walker"], str | None]


@dataclass(frozen=True, slots=True)
class _Entry:
    """`on_bar` 的静态形态：`function` / `async_function` / `lambda` / `not_callable` /
    `missing`（确实没有）/ `unknown`（有绑定但静态判不了——**不报**，交给运行期）。"""

    kind: str
    node: ast.AST | None = None


@dataclass(frozen=True, slots=True)
class _Facts:
    ctx_name: str
    entry: _Entry
    is_generator: bool
    #: 第一处 `ctx.events` / `ctx.new_events`——R4 的一致性提示要指到这一行的行号上
    events_ref_node: ast.Attribute | None
    #: `parse_meta` 读出来的 `USES_EVENTS`；None = 声明本身解析失败（问题记在 declaration_problem）
    uses_events: bool | None
    declaration_problem: str | None
    declaration_line: int | None
    #: `USES_EVENTS = ...` 那一行（`parse_meta` 只给值不给行号，这里自己扫一次）
    uses_events_line: int | None

    @property
    def references_events(self) -> bool:
        return self.events_ref_node is not None


# ── 遍历器 ───────────────────────────────────────────────────────────────


class _Walker(ast.NodeVisitor):
    """单遍遍历：按节点类型分发规则，并维护函数栈。

    函数栈是 R5 判「这个 `return` 在不在 `on_bar` **自己**体内」的依据——嵌套函数里的
    `return 1` 不是入口的返回值，必须放行。
    """

    def __init__(self, source: str, facts: _Facts) -> None:
        self._lines = source.splitlines()
        self._facts = facts
        self._func_stack: list[ast.AST] = []
        self.findings: list[Finding] = []

    @property
    def facts(self) -> _Facts:
        return self._facts

    @property
    def current_function(self) -> ast.AST | None:
        return self._func_stack[-1] if self._func_stack else None

    def snippet_of(self, node: ast.AST, line: int | None = None) -> str:
        lineno = line if line is not None else getattr(node, "lineno", 0)
        if not lineno or lineno > len(self._lines):
            return ""
        return self._lines[lineno - 1].strip()[:160]

    def visit(self, node: ast.AST) -> None:
        for rule in _BY_TYPE.get(type(node), ()):
            detail = rule.match(node, self)
            if detail:
                line = _node_line(node, self._facts)
                self.findings.append(
                    Finding(rule.id, rule.severity, line, detail, self.snippet_of(node, line))
                )
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            self._func_stack.append(node)
            try:
                self.generic_visit(node)
            finally:
                self._func_stack.pop()
        else:
            self.generic_visit(node)


def _node_line(node: ast.AST, facts: _Facts) -> int:
    lineno = getattr(node, "lineno", None)
    if lineno:
        return int(lineno)
    # 模块级规则（R4 的声明检查）没有节点行号：退回声明所在的那一行
    return facts.declaration_line or facts.uses_events_line or 1


# ── R1 数据绕行 ──────────────────────────────────────────────────────────


def _match_r1_import(node: ast.AST, state: _Walker) -> str | None:
    if isinstance(node, ast.Import):
        names = [alias.name for alias in node.names]
    elif isinstance(node, ast.ImportFrom):
        if node.level:
            allowed = "、".join(sorted(ALLOWED_MODULES))
            return f"不允许相对导入：策略只能 import {allowed}（相对导入依赖包结构，沙箱里没有包）"
        names = [node.module or ""]
    else:  # pragma: no cover - 分发已限定类型
        return None
    blocked = [name for name in names if name.split(".")[0] not in ALLOWED_MODULES]
    if not blocked:
        return None
    allowed = "、".join(sorted(ALLOWED_MODULES))
    return (
        f"不允许 import {'、'.join(blocked)}：策略只能 import {allowed}。"
        "行情与事件数据只能经 ctx 取——自己读数据源正是前视泄漏的入口"
    )


def _match_r1_name(node: ast.AST, state: _Walker) -> str | None:
    if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
        return f"不允许使用 {node.id}：它绕开 ctx 直接取数据 / 执行代码，策略只允许纯计算"
    return None


# ── R2 未来函数 ──────────────────────────────────────────────────────────


def _match_r2_call(node: ast.AST, state: _Walker) -> str | None:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    attr = node.func.attr
    if attr == "shift":
        arg = node.args[0] if node.args else _keyword(node, "periods")
        periods = _int_literal(arg) if arg is not None else None
        if periods is None or periods >= 0:
            return None
        return (
            f".shift({periods}) 会取到未来 {abs(periods)} 根 bar：history 里没有未来——"
            "[-1] 就是当前 bar，往回看用负偏移（如 [-2]）"
        )
    if attr in {"bfill", "backfill"}:
        return (
            f".{attr}() 是「用后面的值填前面」，属未来函数；"
            "要用过去值填，写 ffill（前向填充只用过去值，允许）"
        )
    if attr == "fillna":
        method = _keyword(node, "method")
        if isinstance(method, ast.Constant) and method.value in {"bfill", "backfill"}:
            return (
                f'fillna(method="{method.value}") 是未来函数；'
                '要用过去值填，写 method="ffill"'
            )
    return None


# ── R3 显式未来索引（双档） ───────────────────────────────────────────────


def _match_r3_current_index(node: ast.AST, state: _Walker) -> str | None:
    """error 档：`ctx.history[ctx.index + 1]` 这种**明说「当前 + n」**的写法。"""
    parsed = _future_index(node, state)
    if parsed is None:
        return None
    text, base, terms = parsed
    if not any(_is_current_index(term, state.facts.ctx_name) for term in terms):
        return None
    return (
        f"{text} 越过当前 bar：{base} 在物理上没有下一根（它只到当前 bar），未来数据取不到"
        "——想取当前 bar 用 [-1]"
    )


def _match_r3_name_index(node: ast.AST, state: _Walker) -> str | None:
    """warning 档：`ctx.history[i + 1]` 这类「变量 + 正数」——可能是相邻比较，不拦死，只提示。"""
    parsed = _future_index(node, state)
    if parsed is None:
        return None
    text, base, terms = parsed
    if not any(isinstance(term, ast.Name) for term in terms):
        return None
    return (
        f"{text} 可能越过当前 bar：{base} 只到当前 bar（没有下一根）。"
        "若你是想比较相邻两根 bar，用 zip(closes, closes[1:])；想取当前 bar 用 [-1]"
    )


def _future_index(node: ast.AST, state: _Walker) -> tuple[str, str, list[ast.expr]] | None:
    """公共判据：`ctx 数据属性[...]` 且下标里出现**正整数字面量**的加法。

    返回（下标原文、基对象原文、加法项）。切片直接放行——`[a + 1: b + 1]` 里的 `+1` 是排除上界。
    """
    if not isinstance(node, ast.Subscript) or isinstance(node.slice, ast.Slice):
        return None
    if not _is_ctx_attr(node.value, state.facts.ctx_name, CTX_DATA_ATTRS):
        return None
    terms = _add_terms(node.slice)
    if not any((value := _int_literal(term)) is not None and value > 0 for term in terms):
        return None
    return ast.unparse(node), ast.unparse(node.value), terms


def _is_current_index(node: ast.expr, ctx_name: str) -> bool:
    """`ctx.index` 或 `len(ctx.history)`——用户明说「当前位置」的两种写法。"""
    if isinstance(node, ast.Attribute) and node.attr == "index":
        return isinstance(node.value, ast.Name) and node.value.id == ctx_name
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "len":
        return len(node.args) == 1 and _is_ctx_attr(node.args[0], ctx_name, CTX_DATA_ATTRS)
    return False


# ── R4 声明 ──────────────────────────────────────────────────────────────


def _match_r4_declaration(node: ast.AST, state: _Walker) -> str | None:
    """`PARAMS` / `USES_EVENTS` / `validate_params` 的形态问题：`parse_meta` 已判，原文照搬。"""
    return state.facts.declaration_problem


def _match_r4_events_undeclared(node: ast.AST, state: _Walker) -> str | None:
    """读了事件却没声明成 True——只在**第一处**事件读取上提示一次，不刷屏。"""
    facts = state.facts
    if facts.uses_events is not False or facts.events_ref_node is not node:
        return None
    return (
        "声明 USES_EVENTS=False，但代码里读了 ctx.events / ctx.new_events："
        "缺省窗口会按「不消费事件」取（取不到事件语料起点），报告也不会给 PIT 对比——"
        "把声明改成 True 才作数"
    )


def _match_r4_events_unused(node: ast.AST, state: _Walker) -> str | None:
    """声明了 True 却一处都没读——缺省窗口会白等一段。"""
    facts = state.facts
    if facts.uses_events is not True or facts.references_events:
        return None
    return (
        "声明 USES_EVENTS=True，但代码里没有读 ctx.events / ctx.new_events："
        "缺省窗口会取事件语料起点（白等一段），PIT 对比也会按「消费事件」的口径跑"
    )


# ── R5 结构 ──────────────────────────────────────────────────────────────


def _match_r5_entry(node: ast.AST, state: _Walker) -> str | None:
    entry = state.facts.entry
    if entry.kind == "missing":
        return "没有找到模块级 on_bar：用户策略的入口是 def on_bar(ctx) 或 def on_bar(ctx, p)"
    if entry.kind == "not_callable":
        return "on_bar 不是函数：把它写成 def on_bar(ctx) 或 def on_bar(ctx, p)"
    if entry.kind == "unknown":
        return None  # 静态判不了的形态不报——宁可不报也不误报，运行期照样拦得住
    if entry.kind == "async_function":
        return "on_bar 不能是 async 函数：引擎按同步调用取值，async 只会返回一个协程对象"
    args = entry.node.args  # type: ignore[union-attr] - 上面已限定 kind
    count = len(args.posonlyargs) + len(args.args)
    # kwonly 的默认值单独放在 `kw_defaults` 里（与 kwonlyargs 等长，None = 没有默认值）
    required_kwonly = [
        arg.arg
        for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True)
        if default is None
    ]
    if count not in (1, 2) or required_kwonly:
        names = "、".join(_parameter_names(args)) or "无"
        return (
            f"on_bar 的形参必须是 (ctx) 或 (ctx, p)（当前：{names}）——"
            "参数取值走第二个形参（内部读 p[参数名]）"
        )
    return None


def _match_r5_return(node: ast.AST, state: _Walker) -> str | None:
    facts = state.facts
    if facts.entry.kind not in {"function", "lambda"} or facts.is_generator:
        return None
    if state.current_function is not facts.entry.node:
        return None  # 嵌套函数里的 return 不是入口的返回值
    value = getattr(node, "value", None)
    if value is None:
        return "on_bar 里有一个空的 return：它返回 None，引擎只认 list[Signal]——想不下单就 return []"
    if isinstance(value, ast.Constant):
        return f"on_bar 返回了 {_brief(value)}：引擎只认 list[Signal]——想不下单就 return []"
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "Signal":
        return "on_bar 返回了单个 Signal：要包在列表里——return [Signal(...)]"
    return None


# ── 规则表 ───────────────────────────────────────────────────────────────


RULES: tuple[Rule, ...] = (
    Rule("R1", ERROR, (ast.Import, ast.ImportFrom), "数据绕行：白名单外的 import", _match_r1_import),
    Rule("R1", ERROR, (ast.Name,), "数据绕行：禁用名", _match_r1_name),
    Rule("R2", ERROR, (ast.Call,), "未来函数：shift(负数) / bfill / backfill", _match_r2_call),
    Rule("R3", ERROR, (ast.Subscript,), "显式未来索引：明说「当前 + n」", _match_r3_current_index),
    Rule("R3", WARNING, (ast.Subscript,), "显式未来索引：变量 + 正数", _match_r3_name_index),
    Rule("R4", ERROR, (ast.Module,), "声明：PARAMS / USES_EVENTS / validate_params 形态", _match_r4_declaration),
    Rule("R4", WARNING, (ast.Attribute,), "声明一致性：读了事件却没声明", _match_r4_events_undeclared),
    Rule("R4", WARNING, (ast.Module,), "声明一致性：声明了却不读", _match_r4_events_unused),
    Rule("R5", ERROR, (ast.Module,), "结构：入口缺失 / async / 形参", _match_r5_entry),
    Rule("R5", ERROR, (ast.Return,), "结构：返回值形态", _match_r5_return),
)


def _index_rules(rules: tuple[Rule, ...]) -> dict[type[ast.AST], tuple[Rule, ...]]:
    index: dict[type[ast.AST], list[Rule]] = {}
    for rule in rules:
        for node_type in rule.node_types:
            index.setdefault(node_type, []).append(rule)
    return {node_type: tuple(items) for node_type, items in index.items()}


_BY_TYPE = _index_rules(RULES)


# ── 入口 ─────────────────────────────────────────────────────────────────


def check_source(source: str) -> list[Finding]:
    """跑完所有规则，返回按 `(行, 规则)` 排好序的结果。

    **从不抛异常**：语法错也是一条 finding（抛异常的话 M4c 的编辑器就没得标）。源码一行都不执行。
    """
    try:
        tree = ast.parse(source, filename=SOURCE_NAME)
    except SyntaxError as exc:
        lineno = exc.lineno or 1
        return [Finding(SYNTAX_RULE, ERROR, lineno, f"语法错误：{exc.msg}", _line_snippet(source, lineno))]

    walker = _Walker(source, _collect_facts(source, tree))
    walker.visit(tree)
    return _sorted_unique(walker.findings)


def has_errors(findings: Iterable[Finding]) -> bool:
    """有没有 `error` 档命中——运行前的闸门判据（M4c 用它给 422）。"""
    return any(finding.severity == ERROR for finding in findings)


def format_findings(findings: Sequence[Finding]) -> str:
    """人读文本：CLI 与日志用（M4c 的编辑器直接用 `to_dict()` 的字段）。"""
    if not findings:
        return "✓ 检查通过：没有发现问题"
    errors = sum(1 for finding in findings if finding.severity == ERROR)
    warnings = len(findings) - errors
    head = (
        f"✗ {errors} 个 error（运行前会被拒）、{warnings} 个 warning"
        if errors
        else f"✓ 没有 error；{warnings} 个 warning（不拦运行）"
    )
    lines = [head]
    for finding in findings:
        lines.append(f"  第 {finding.line} 行 [{finding.rule} {finding.severity}] {finding.message}")
        if finding.snippet:
            lines.append(f"      {finding.snippet}")
    return "\n".join(lines)


def _collect_facts(source: str, tree: ast.Module) -> _Facts:
    """一次算清规则要用的公共事实。

    `PARAMS` / `USES_EVENTS` 直接复用 `api.parse_meta`（同一个真源，不在这儿再写一份字面量解析）；
    它内部会再 `ast.parse` 一遍——多一次解析换「声明解析只有一处实现」，值。
    """
    entry = _find_entry(tree)
    func = entry.node if entry.kind in {"function", "async_function", "lambda"} else None
    args = func.args if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) else None
    ctx_name = _first_positional(args) or "ctx"
    is_generator = isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
        isinstance(node, (ast.Yield, ast.YieldFrom)) for node in _own_scope(func)
    )
    declaration_problem: str | None = None
    declaration_line: int | None = None
    uses_events: bool | None = None
    try:
        uses_events = parse_meta(source).uses_events
    except StrategyRejected as exc:
        declaration_problem = str(exc)
        declaration_line = exc.line
    statements = list(_module_level_statements(tree))
    events_ref_node = next(
        (node for node in ast.walk(tree) if _is_ctx_attr(node, ctx_name, EVENT_ATTRS)), None
    )
    # 后定义者生效（与 `_find_entry` 同口径）
    uses_events_line = next(
        (
            node.lineno
            for node in reversed(statements)
            if _assigned_value(node, "USES_EVENTS") is not None
        ),
        None,
    )
    return _Facts(
        ctx_name=ctx_name,
        entry=entry,
        is_generator=is_generator,
        events_ref_node=events_ref_node,
        uses_events=uses_events,
        declaration_problem=declaration_problem,
        declaration_line=declaration_line,
        uses_events_line=uses_events_line,
    )


def _find_entry(tree: ast.Module) -> _Entry:
    """找模块级的 `on_bar` 绑定——与运行期同口径：**后定义者生效**。"""
    for node in reversed(list(_module_level_statements(tree))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "on_bar":
            kind = "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
            return _Entry(kind, node)
        value = _assigned_value(node, "on_bar")
        if value is None:
            continue
        if isinstance(value, ast.Lambda):
            return _Entry("lambda", value)
        if isinstance(value, (ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Set)):
            # 字面量不可能是函数：`on_bar = 3` 这类运行期一定被拒，静态可以直说
            return _Entry("not_callable", node)
        return _Entry("unknown", node)
    return _Entry("missing")


def _module_level_statements(scope: ast.AST) -> Iterator[ast.stmt]:
    """模块级语句（含 `if` / `try` 体内的——运行期同样落在模块命名空间）。

    函数 / 类**语句本身**要产出（`def on_bar` 就是一条），但不进它们的体内：
    那里的 `on_bar` 不是模块级入口。
    """
    for child in ast.iter_child_nodes(scope):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            if isinstance(child, ast.stmt):
                yield child
            continue
        if isinstance(child, ast.stmt):
            yield child
        yield from _module_level_statements(child)


def _assigned_value(node: ast.stmt, name: str) -> ast.expr | None:
    if isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == name for target in node.targets
    ):
        return node.value
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
        return node.value
    return None


def _own_scope(node: ast.AST) -> Iterator[ast.AST]:
    """自身作用域内的所有节点（不含嵌套函数 / 类 / lambda 的体内）。"""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield child
        yield from _own_scope(child)


def _first_positional(args: ast.arguments | None) -> str | None:
    if args is None:
        return None
    positional = [*args.posonlyargs, *args.args]
    return positional[0].arg if positional else None


def _parameter_names(args: ast.arguments) -> list[str]:
    names = [arg.arg for arg in (*args.posonlyargs, *args.args)]
    if args.vararg:
        names.append(f"*{args.vararg.arg}")
    names.extend(arg.arg for arg in args.kwonlyargs)
    if args.kwarg:
        names.append(f"**{args.kwarg.arg}")
    return names


def _is_ctx_attr(node: ast.AST, ctx_name: str, attrs: frozenset[str]) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr in attrs
        and isinstance(node.value, ast.Name)
        and node.value.id == ctx_name
    )


def _add_terms(expr: ast.expr) -> list[ast.expr]:
    """把 `a + 1 + 2` 摊平成 `[a, 1, 2]`（只认加法，其它运算符原样返回）。"""
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        return [*_add_terms(expr.left), *_add_terms(expr.right)]
    return [expr]


def _int_literal(node: ast.expr | None) -> int | None:
    """静态可判定的整数字面量，含 `-1` 这种一元负号写法（AST 里是 `UnaryOp(USub, Constant)`）。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _int_literal(node.operand)
        return -inner if inner is not None else None
    return None


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def _brief(node: ast.AST, limit: int = 40) -> str:
    text = ast.unparse(node)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _line_snippet(source: str, lineno: int) -> str:
    lines = source.splitlines()
    if 1 <= lineno <= len(lines):
        return lines[lineno - 1].strip()[:160]
    return ""


def _sorted_unique(findings: list[Finding]) -> list[Finding]:
    seen: set[tuple[str, int, str]] = set()
    unique: list[Finding] = []
    for finding in sorted(findings, key=lambda f: (f.line, f.rule, f.severity)):
        key = (finding.rule, finding.line, finding.message)
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    return unique


__all__ = [
    "ALL_RULE_IDS",
    "ALLOWED_MODULES",
    "ERROR",
    "FORBIDDEN_NAMES",
    "RULES",
    "SYNTAX_RULE",
    "WARNING",
    "Finding",
    "Rule",
    "check_source",
    "format_findings",
    "has_errors",
]
