"""M4b 静态检查器：规则正/负样例矩阵。

两条口径（SPEC §5 M4b）：

- **不误报是硬要求**：负样例必须**零命中**——`[-1]`、`i-1`、切片上的 `+1`、相邻两根的
  `closes[i+1]`、字符串与注释里的 `shift(-1)`、变量名叫 `bfill`、`fillna(method="ffill")`、
  生成器 `on_bar` 的裸 `return`、嵌套函数里的 `return 1`
- **元测试**：规则表里每条规则都必须在样例矩阵里有正/负样例（新增规则漏配样例 → 红）

另有两组**一致性**用例，防「检查器与运行时两套判据各说各话」：白名单同源、入口判据与
`api.load_strategy` 结论一致。检查器本身**从不执行用户代码**——这里跑 `load_strategy` 的
用例只喂无害的自造源码，且那是**运行期**的对照组，不是检查器的实现路径。
"""

from __future__ import annotations

import warnings
from datetime import date

import pytest

from app.backtest.types import Bar, BarContext, Position
from app.strategy import api as strategy_api
from app.strategy import StrategyRejected
from app.strategy.api import _SAFE_BUILTINS, load_strategy
from app.strategy.static_check import (
    ALL_RULE_IDS,
    FORBIDDEN_NAMES,
    RULES,
    SYNTAX_RULE,
    check_source,
    format_findings,
    has_errors,
)

#: 样例用例：源码、期望命中的片段（None = 不校验片段）、期望出现在 message 里的关键词、级别
Case = tuple[str, "str | None", str, str]


def good(body: str = "return []", *, module: str = "", uses_events: bool = False) -> str:
    """一份骨架源码：可选的模块级代码 + 一个合法的 `on_bar`，`body` 是它的函数体。

    负样例都用它拼——骨架自身零命中，命中必然来自 `body` / `module`。
    """
    head = f"USES_EVENTS = {uses_events}\n"
    if module:
        head += module.rstrip("\n") + "\n"
    indented = "\n".join("    " + line if line.strip() else line for line in body.splitlines())
    return f"{head}\n\ndef on_bar(ctx):\n{indented}\n"


# ── 正样例：每条规则至少一条，逐条断言行号片段 / 关键词 / 级别 ──────────────────

POSITIVES: dict[str, list[Case]] = {
    SYNTAX_RULE: [
        ("def on_bar(ctx)\n    return []\n", None, "语法错误", "error"),
    ],
    "R1": [
        ('import duckdb\n\ndef on_bar(ctx):\n    return []\n', "import duckdb", "不允许 import duckdb", "error"),
        ("import pandas as pd\n\ndef on_bar(ctx):\n    return []\n", "import pandas as pd", "不允许 import pandas", "error"),
        ("from os import path\n\ndef on_bar(ctx):\n    return []\n", "from os import path", "不允许 import os", "error"),
        # 同一行两个越界模块：一条 finding 里列全，不拆成两条
        ("import os, sys\n\ndef on_bar(ctx):\n    return []\n", "import os, sys", "os、sys", "error"),
        ("from . import helper\n\ndef on_bar(ctx):\n    return []\n", None, "相对导入", "error"),
        (good("x = open('/etc/passwd')\nreturn []"), "open('/etc/passwd')", "open", "error"),
        (good("return eval('1 + 1')"), "eval('1 + 1')", "eval", "error"),
        (good(module="__import__('os')"), "__import__('os')", "__import__", "error"),
        (good("import importlib\nreturn []"), "import importlib", "importlib", "error"),
        (good(module="importlib.import_module('os')"), "importlib.import_module", "importlib", "error"),
        (good(module="x = __builtins__"), "x = __builtins__", "__builtins__", "error"),
    ],
    "R2": [
        (good("closes = [b.close for b in ctx.history]\ns = closes.shift(-1)\nreturn s"), ".shift(-1)", ".shift(-1)", "error"),
        (good("s = closes.shift(periods=-2)\nreturn s"), "periods=-2", "未来 2 根", "error"),
        (good("filled = closes.bfill()\nreturn filled"), ".bfill()", "bfill", "error"),
        (good("filled = closes.backfill()\nreturn filled"), ".backfill()", "backfill", "error"),
        (good('filled = closes.fillna(method="bfill")\nreturn filled'), 'method="bfill"', "未来函数", "error"),
    ],
    "R3": [
        (good("nxt = ctx.history[ctx.index + 1]\nreturn []"), "ctx.history[ctx.index + 1]", "越过当前 bar", "error"),
        (good("nxt = ctx.history[len(ctx.history) + 1]\nreturn []"), "len(ctx.history) + 1", "越过当前 bar", "error"),
        # ctx 改名也认（名字从 on_bar 第一个形参取）
        (
            'def on_bar(c, p):\n    nxt = c.history[c.index + 1]\n    return []\n',
            "c.history[c.index + 1]",
            "越过当前 bar",
            "error",
        ),
        # 其它名字 + 正数 → warning（可能是相邻比较，拦死会误伤合法写法）
        (
            good("for i in range(len(ctx.history) - 1):\n    pair = ctx.history[i + 1]\nreturn []"),
            "ctx.history[i + 1]",
            "zip(closes, closes[1:])",
            "warning",
        ),
        (good("nxt = ctx.new_events[seed + 1]\nreturn []"), "ctx.new_events[seed + 1]", "下一根", "warning"),
    ],
    "R4": [
        ("PARAMS = dict(fast=5)\n\ndef on_bar(ctx, p):\n    return []\n", None, "必须是字面量", "error"),
        ('USES_EVENTS = "yes"\n\ndef on_bar(ctx):\n    return []\n', None, "USES_EVENTS 必须是字面量", "error"),
        # 读了事件却没声明 → 缺省窗口会按「不消费事件」取
        (
            good("return [e for e in ctx.new_events]"),
            "ctx.new_events",
            "声明 USES_EVENTS=False",
            "warning",
        ),
        # 声明了却不读 → 缺省窗口会取事件语料起点，白等一段
        (good("return []", uses_events=True), None, "没有读 ctx.events", "warning"),
    ],
    "R5": [
        ("X = 1\n", None, "没有找到模块级 on_bar", "error"),
        ("async def on_bar(ctx):\n    return []\n", "async def on_bar", "不能是 async", "error"),
        ("def on_bar(ctx, p, q):\n    return []\n", "def on_bar(ctx, p, q)", "形参必须是 (ctx) 或 (ctx, p)", "error"),
        ("def on_bar(ctx, *, p):\n    return []\n", "*, p", "形参必须是 (ctx) 或 (ctx, p)", "error"),
        ("def on_bar():\n    return []\n", None, "形参必须是 (ctx) 或 (ctx, p)", "error"),
        ("on_bar = 3\n", "on_bar = 3", "不是函数", "error"),
        (good("return 3"), "return 3", "list[Signal]", "error"),
        (good("if ctx.position.is_flat:\n    return\nreturn []"), None, "空的 return", "error"),
        (good("return Signal(Side.BUY)"), "return Signal(Side.BUY)", "包在列表里", "error"),
    ],
}

# ── 负样例：必须零命中 ────────────────────────────────────────────────────

NEGATIVES: dict[str, list[str]] = {
    SYNTAX_RULE: [good("return []"), good("x = 1\nreturn []")],
    "R1": [
        good("import math\nimport statistics\nfrom collections import deque\nreturn []"),
        good(module='NOTE = "import os / open() / eval() 都只是文本"'),
        good(module="# import duckdb  —— 注释里也不算"),
        good('x = {"open": 1}\nreturn []'),
    ],
    "R2": [
        good("prev = closes.shift(1)\nreturn prev"),
        good("prev = closes.shift(n)\nreturn prev"),  # 变量偏移：静态判不了，不报
        good(module='NOTE = "closes.shift(-1) 只是文本"'),
        good(module="# .bfill() 只是注释"),
        good("bfill = 3\nreturn [bfill]"),  # 变量名叫 bfill 不是未来函数
        good("def bfill(values):\n    return values\nreturn bfill([])"),
        good('filled = closes.fillna(method="ffill")\nreturn filled'),
        good("x = closes.rolling(5).mean()\nreturn x"),
    ],
    "R3": [
        good("last = ctx.history[-1]\nreturn []"),
        good("prev = ctx.history[i - 1]\nreturn []"),
        good("for i in range(len(ctx.history)):\n    bar = ctx.history[i]\nreturn []"),
        # 切片：双均线模板原样，`+1` 只是排除上界
        (
            good(
                "closes = [b.close for b in ctx.history]\n"
                "index = len(closes) - 1\n"
                "window = closes[index - 5 + 1: index + 1]\n"
                "return []"
            )
        ),
        good("past = ctx.history[-5 - 1:-1]\nreturn []"),
        # 自有列表上的相邻比较：静态分不清它来自 ctx.history 还是自己攒的，不报
        good("for i in range(len(closes) - 1):\n    pair = closes[i + 1]\nreturn []"),
        good("first = ctx.events[0]\nreturn []", uses_events=True),
        good("head = ctx.history[:3]\nreturn head"),
    ],
    "R4": [
        good("return [e for e in ctx.new_events]", uses_events=True),
        good("return []", uses_events=False),
        ("PARAMS = {}\n\ndef on_bar(ctx, p):\n    return []\n"),
    ],
    "R5": [
        good("return []"),
        # 生成器版 on_bar 合法：裸 return 只是结束迭代
        good("yield Signal(Side.BUY)\nreturn"),
        # 嵌套函数里的 return 1 不是入口的返回值
        good("def helper():\n    return 1\nreturn [Signal(Side.BUY) for _ in [helper()]]"),
        good("return []", module="def pick():\n    return 1"),
    ],
}


def check(source: str) -> list:
    return check_source(source)


def ids(findings: list) -> list[str]:
    return [f.rule for f in findings]


# ── 矩阵 ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "case",
    [(rule, case) for rule, cases in POSITIVES.items() for case in cases],
    ids=[f"{rule}-{i}" for rule, cases in POSITIVES.items() for i in range(len(cases))],
)
def test_positive_cases_hit_with_line_and_severity(case: tuple[str, Case]) -> None:
    rule_id, (source, marker, keyword, severity) = case
    hits = [f for f in check(source) if f.rule == rule_id]
    assert hits, f"{rule_id} 没有命中：{source!r}"
    first = hits[0]
    assert first.severity == severity
    assert keyword in first.message, first.message
    if marker is not None:
        assert marker in first.snippet, f"行号归属不对：{first.line} / {first.snippet!r}"
        assert source.splitlines()[first.line - 1].strip() == first.snippet
    assert all(1 <= f.line <= len(source.splitlines()) for f in hits)


@pytest.mark.parametrize(
    "case",
    [(rule, src) for rule, sources in NEGATIVES.items() for src in sources],
    ids=[f"{rule}-{i}" for rule, sources in NEGATIVES.items() for i in range(len(sources))],
)
def test_negative_cases_are_silent(case: tuple[str, str]) -> None:
    """负样例**整份源码零命中**——不只看目标规则有没有误报。"""
    rule_id, source = case
    findings = check(source)
    assert findings == [], f"{rule_id} 负样例误报：{[ (f.rule, f.line, f.message) for f in findings ]}"


def test_every_rule_has_positive_and_negative_samples() -> None:
    """元测试：规则表里的规则与样例矩阵必须一一对上。"""
    assert set(POSITIVES) == set(ALL_RULE_IDS)
    assert set(NEGATIVES) == set(ALL_RULE_IDS)
    assert {rule.id for rule in RULES} == set(ALL_RULE_IDS) - {SYNTAX_RULE}
    assert all(rule.severity in {"error", "warning"} for rule in RULES)
    assert all(rule.message and rule.match for rule in RULES)


def test_source_with_no_problem_is_clean() -> None:
    assert check(good("return [Signal(Side.BUY, reason='test')]")) == []
    assert has_errors([]) is False
    assert format_findings([]).startswith("✓")


# ── 一致性：检查器与运行时不许各说各话 ──────────────────────────────────────


def test_forbidden_names_never_in_runtime_whitelist() -> None:
    """检查器禁的名，运行时本来也不给——两边不打架。"""
    assert FORBIDDEN_NAMES & set(_SAFE_BUILTINS) == set()


def test_allowed_modules_are_the_runtime_object() -> None:
    from app.strategy import static_check

    assert static_check.ALLOWED_MODULES is strategy_api.ALLOWED_MODULES


def _ctx() -> BarContext:
    bar = Bar(symbol="600519", trade_date=date(2026, 1, 5), open=10.0, high=10.0, low=10.0, close=10.0, volume=1e5)
    return BarContext(
        bar=bar,
        index=0,
        history=(bar,),
        position=Position(),
        cash=100_000.0,
        equity=100_000.0,
        events=(),
        new_events=(),
    )


def runtime_rejects(source: str) -> bool:
    """运行期（装载 + 调一次 on_bar）会不会拒——错误都收口成 `StrategyRejected`。

    `async def on_bar` 会造出一个没人 await 的协程，这里只关心「会不会被拒」，把那条
    RuntimeWarning 静音。
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        try:
            strategy = load_strategy(source)
            strategy.on_bar(_ctx())
        except StrategyRejected:
            return True
    return False


#: (源码, 运行期是否拒) —— 只放**静态可判定**的形态
ENTRY_SAMPLES: list[tuple[str, bool]] = [
    ("def on_bar(ctx):\n    return []\n", False),
    ("def on_bar(ctx, p):\n    return []\n", False),
    ("def on_bar(ctx, p=None):\n    return []\n", False),
    ("def on_bar(ctx, *args):\n    return []\n", False),
    ("on_bar = lambda ctx: []\n", False),
    ("async def on_bar(ctx):\n    return []\n", True),
    ("def on_bar(ctx, p, q):\n    return []\n", True),
    ("def on_bar(ctx, *, p):\n    return []\n", True),
    ("def on_bar():\n    return []\n", True),
    ("def on_bar(*args):\n    return []\n", True),
    ("on_bar = 3\n", True),
    ("X = 1\n", True),
    ("def on_bar(ctx):\n    return 3\n", True),
    ("def on_bar(ctx):\n    return None\n", True),
    ("def on_bar(ctx):\n    return Signal(Side.BUY)\n", True),
]


@pytest.mark.parametrize(("source", "expected"), ENTRY_SAMPLES, ids=[f"entry-{i}" for i in range(len(ENTRY_SAMPLES))])
def test_entry_verdict_matches_runtime(source: str, expected: bool) -> None:
    hits = [f for f in check(source) if f.rule == "R5" and f.severity == "error"]
    assert bool(hits) is expected, f"检查器结论与用例不符：{[(f.line, f.message) for f in hits]}"
    assert runtime_rejects(source) is expected


def test_unknown_entry_form_is_left_to_runtime() -> None:
    """静态判不了的形态（`on_bar = make()`）**不报**——宁可不报也不误报，运行期照样拦得住。"""
    source = "on_bar = make()\n"
    assert [f for f in check(source) if f.rule == "R5"] == []
    assert runtime_rejects(source) is True


# ── 输出形态 ─────────────────────────────────────────────────────────────


def test_format_findings_has_line_rule_and_snippet() -> None:
    text = format_findings(check("import duckdb\n\ndef on_bar(ctx):\n    return []\n"))
    assert "第 1 行" in text and "R1" in text and "import duckdb" in text


def test_has_errors_counts_only_errors() -> None:
    warned = check(good("for i in range(len(ctx.history) - 1):\n    x = ctx.history[i + 1]\nreturn []"))
    assert [f.severity for f in warned] == ["warning"]
    assert has_errors(warned) is False
    assert has_errors(check("import duckdb\n\ndef on_bar(ctx):\n    return []\n")) is True
