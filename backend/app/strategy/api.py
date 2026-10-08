"""用户策略契约：静态解析（AST）与命名空间装载。

`PARAMS` / `USES_EVENTS` **只从 AST 读，不执行代码**——两个消费者都要求这一点：父进程要用
`uses_events` 决定缺省窗口，编辑器要拿参数 schema 渲染表单，两者都不该为此跑一遍用户代码。
`on_bar` 则必须执行才能装载，一律在沙箱子进程里做（`load_strategy` 只应被子侧调用）。

契约（SPEC §5）：

    PARAMS = {"fast": {"type": "int", "default": 5, "min": 1, "max": 250, "label": "快线周期"}}
    USES_EVENTS = False
    def validate_params(p): ...      # 可选，返回中文错误串列表
    def on_bar(ctx): ...             # 唯一入口，返回 list[Signal]
    def on_bar(ctx, p): ...          # 需要参数时用第二个形参（与 validate_params 同形）

参数取值走**第二个形参**而不是注入全局名：来源在签名上看得见，不必猜 `fast` 是哪儿来的；
`p` 已被 `PARAMS` 的缺省值填满，放心 `p["fast"]`。

命名空间白名单**不含 pandas**：`shift` / `bfill` 正是前视泄漏的常见来源，我们故意不给
（产品立场，写进模板注释与 M9 教程）。数据只能经 `ctx` 取。
"""

from __future__ import annotations

import ast
import builtins
import importlib
import inspect
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any

from app.backtest.types import BarContext, Signal, Side
from app.strategy import USER_STRATEGY, StrategyRejected
from app.strategy.params import ParamSpec, parse_param_schema

#: 允许 import 的模块（纯计算）。数据层、IO、网络、进程一律不在列。
ALLOWED_MODULES = frozenset(
    {
        "math",
        "statistics",
        "itertools",
        "functools",
        "collections",
        "dataclasses",
        "typing",
        "datetime",
    }
)

#: 编译单元名：报错与回溯里的文件名，也是 `_line_of` 认人用的锚。
SOURCE_NAME = "<strategy>"

#: 内置函数白名单。**这不是安全边界**（见包 docstring）：`type` / `getattr` 留着，
#: 是因为去掉它们只会让正常代码难写，挡不住真心要绕的人。挡的是「手滑」与「无意」。
_SAFE_BUILTINS: dict[str, Any] = {
    name: getattr(builtins, name)
    for name in (
        "abs", "all", "any", "ascii", "bin", "bool", "bytearray", "bytes", "callable", "chr",
        "classmethod", "complex", "dict", "dir", "divmod", "enumerate", "filter", "float",
        "format", "frozenset", "getattr", "hasattr", "hash", "hex", "id", "int", "isinstance",
        "issubclass", "iter", "len", "list", "map", "max", "min", "next", "object", "oct", "ord",
        "pow", "print", "property", "range", "repr", "reversed", "round", "set", "setattr",
        "slice", "sorted", "staticmethod", "str", "sum", "super", "tuple", "type", "vars", "zip",
        "__build_class__",
        # 异常类：用户要能写 try/except，也要能自己抛
        "ArithmeticError", "AssertionError", "AttributeError", "Exception", "IndexError",
        "KeyError", "NameError", "NotImplementedError", "RuntimeError", "StopIteration",
        "TypeError", "ValueError", "ZeroDivisionError",
    )
}


def _restricted_import(
    name: str,
    globals_: Mapping[str, Any] | None = None,  # noqa: A002 - 与 __import__ 签名一致
    locals_: Mapping[str, Any] | None = None,  # noqa: A002
    fromlist: tuple[str, ...] = (),
    level: int = 0,
) -> Any:
    root = name.split(".")[0]
    if level != 0 or root not in ALLOWED_MODULES:
        allowed = "、".join(sorted(ALLOWED_MODULES))
        raise ImportError(
            f"策略里不允许 import {name!r}。可用：{allowed}；"
            "行情与事件数据只能经 ctx 取（不许自己读数据源——那正是前视泄漏的入口）"
        )
    # 与内置 __import__ 语义对齐：`import a.b` 绑定顶层包，`from a.b import c` 返回子模块
    return importlib.import_module(name if fromlist else root)


def build_namespace() -> dict[str, Any]:
    """构造用户代码的执行命名空间：受限 builtins + 预注入模块与 `Signal` / `Side`。

    模块**预注入**，所以「只想算个均值」的策略一个 import 都不用写。
    """
    namespace: dict[str, Any] = {
        "__name__": "user_strategy",
        "__builtins__": {**_SAFE_BUILTINS, "__import__": _restricted_import},
        "Signal": Signal,
        "Side": Side,
    }
    for module_name in sorted(ALLOWED_MODULES):
        try:
            namespace[module_name] = importlib.import_module(module_name)
        except ImportError:  # pragma: no cover - 标准库模块不会缺
            continue
    return namespace


@dataclass(frozen=True, slots=True)
class StrategyMeta:
    """静态解析出的策略元信息（不执行代码）。"""

    params: dict[str, ParamSpec]
    uses_events: bool = False


def parse_meta(source: str) -> StrategyMeta:
    """静态读 `PARAMS` / `USES_EVENTS`。非字面量、语法错一律拒绝（带行号）。"""
    try:
        tree = ast.parse(source, filename=SOURCE_NAME)
    except SyntaxError as exc:
        raise StrategyRejected(f"语法错误：{exc.msg}", line=exc.lineno) from exc

    params_raw: object = {}
    uses_events = False
    for node in tree.body:  # 只看模块级：函数体内的同名变量不算声明
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        if value is None:
            continue
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id == "PARAMS":
                params_raw = _literal(value, "PARAMS")
            elif target.id == "USES_EVENTS":
                uses_events = _bool_literal(value)
    return StrategyMeta(params=parse_param_schema(params_raw), uses_events=uses_events)


def _literal(node: ast.expr, what: str) -> object:
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError) as exc:
        raise StrategyRejected(
            f"{what} 必须是字面量（静态解析要读它，编辑器据此渲染参数表单；"
            "不许用函数调用 / 变量拼出来）",
            line=node.lineno,
        ) from exc


def _bool_literal(node: ast.expr) -> bool:
    if not isinstance(node, ast.Constant) or not isinstance(node.value, bool):
        raise StrategyRejected(
            "USES_EVENTS 必须是字面量 True / False（静态解析要读它，决定缺省窗口与 PIT 对比）",
            line=node.lineno,
        )
    return node.value


class UserStrategy:
    """把用户 `on_bar` 适配成引擎认识的内置策略形状。

    `name` 满足 `Strategy` 协议；取值失败与返回值形态都在这里收口成**带 bar 日期**的
    中文报错——用户拿到的是「哪一根 bar 上出了什么事」，不是裸回溯。
    """

    def __init__(
        self,
        on_bar: Callable[..., Any],
        *,
        name: str = USER_STRATEGY,
        validate: Callable[[Mapping[str, Any]], Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> None:
        self.name = name
        self._on_bar = on_bar
        self._validate = validate
        self._params = dict(params or {})
        #: 入口是否声明了第二个形参（`def on_bar(ctx, p)`）——装载时已核对过签名
        self._wants_params = _positional_count(on_bar) == 2

    def check_values(self, values: Mapping[str, Any]) -> list[str]:
        """跑用户自己的跨字段校验（可选钩子），返回中文错误串列表。"""
        if self._validate is None:
            return []
        try:
            result = self._validate(dict(values))
        except Exception as exc:  # noqa: BLE001 - 用户代码的任何异常都要变成可读报错
            raise StrategyRejected(
                f"validate_params 抛出 {type(exc).__name__}: {exc}", line=_line_of(exc)
            ) from exc
        if result is None:
            return []
        if isinstance(result, str):  # 手滑写成单串：当成一条错误，不按字符拆开
            return [result]
        try:
            return [str(item) for item in result]
        except TypeError as exc:
            raise StrategyRejected(
                "validate_params 必须返回错误串列表（没有问题时返回空列表）"
            ) from exc

    def on_bar(self, ctx: BarContext) -> list[Signal]:
        try:
            raw = self._on_bar(ctx, self._params) if self._wants_params else self._on_bar(ctx)
        except StrategyRejected:
            raise
        except Exception as exc:  # noqa: BLE001 - 用户代码的任何异常都要变成可读报错
            raise StrategyRejected(
                f"策略在 {ctx.bar.trade_date} 抛出 {type(exc).__name__}: {exc}",
                line=_line_of(exc),
            ) from exc
        return _coerce_signals(raw, ctx)


def _coerce_signals(raw: object, ctx: BarContext) -> list[Signal]:
    """返回值形态收口：必须是 `Signal` 的可迭代（列表 / 元组 / 生成器皆可）。"""
    where = f"{ctx.bar.trade_date} 的 on_bar 返回值"
    if raw is None:
        raise StrategyRejected(f"{where}是 None；想不下单就返回空列表 []")
    if isinstance(raw, (str, bytes)):
        raise StrategyRejected(f"{where}是字符串；必须返回 list[Signal]")
    try:
        items = list(raw)  # type: ignore[call-overload]
    except TypeError as exc:
        raise StrategyRejected(
            f"{where}不可迭代（{type(raw).__name__}）；必须返回 list[Signal]，例如 "
            "[Signal(Side.BUY, reason='金叉')]"
        ) from exc
    for item in items:
        if not isinstance(item, Signal):
            raise StrategyRejected(
                f"{where}里有 {type(item).__name__}；列表元素必须都是 Signal"
            )
    return items


def _line_of(exc: BaseException) -> int | None:
    """用户代码里最后一帧的行号——编辑器标注用。拿不到返回 None。"""
    tb: TracebackType | None = exc.__traceback__
    line: int | None = None
    while tb is not None:
        if tb.tb_frame.f_code.co_filename == SOURCE_NAME:
            line = tb.tb_lineno
        tb = tb.tb_next
    return line


def load_strategy(
    source: str, *, name: str = USER_STRATEGY, params: Mapping[str, Any] | None = None
) -> UserStrategy:
    """执行源码并取出 `on_bar`（**只在沙箱子进程里调用**）。

    `params` 是**已归一**的取值（缺省已填满），会随 `on_bar(ctx, p)` 的第二个形参交给策略。
    """
    try:
        code = compile(source, SOURCE_NAME, "exec")
    except SyntaxError as exc:
        raise StrategyRejected(f"语法错误：{exc.msg}", line=exc.lineno) from exc

    namespace = build_namespace()
    try:
        exec(code, namespace)  # noqa: S102 - 用户代码执行是本模块的本职，隔离在子进程内
    except Exception as exc:  # noqa: BLE001 - 模块级代码也可能抛
        raise StrategyRejected(
            f"策略加载时抛出 {type(exc).__name__}: {exc}", line=_line_of(exc)
        ) from exc

    entry = namespace.get("on_bar")
    if entry is None:
        raise StrategyRejected("没有找到 on_bar：用户策略必须定义模块级函数 def on_bar(ctx)")
    if not callable(entry):
        raise StrategyRejected(f"on_bar 不是函数（当前是 {type(entry).__name__}）")
    _check_signature(entry)

    validate = namespace.get("validate_params")
    if validate is not None and not callable(validate):
        raise StrategyRejected(
            f"validate_params 不是函数（当前是 {type(validate).__name__}）"
        )
    return UserStrategy(entry, name=name, validate=validate, params=params)


def _positional_count(entry: Callable[..., Any]) -> int:
    return sum(
        1
        for param in inspect.signature(entry).parameters.values()
        if param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD)
    )


def _check_signature(entry: Callable[..., Any]) -> None:
    try:
        signature = inspect.signature(entry)
    except (TypeError, ValueError) as exc:  # pragma: no cover - 极少见
        raise StrategyRejected(f"读不到 on_bar 的签名：{exc}") from exc
    count = _positional_count(entry)
    required_extra = [
        param
        for param in signature.parameters.values()
        if param.kind is param.KEYWORD_ONLY and param.default is param.empty
    ]
    if count not in (1, 2) or required_extra:
        raise StrategyRejected(
            "on_bar 的形参必须是 (ctx) 或 (ctx, p)（p 是参数取值）"
            f"——当前形参：{', '.join(signature.parameters) or '无'}"
        )


def format_traceback(exc: BaseException) -> str:
    """完整回溯文本：走 stderr 作诊断尾巴（父侧按 `stderr_bytes` 截断）。

    面向人的报错在 `StrategyRejected.message` 里（带 bar 日期与行号）；这里是给开发看的栈。
    """
    lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
    return "".join(lines)


__all__ = [
    "ALLOWED_MODULES",
    "SOURCE_NAME",
    "StrategyMeta",
    "UserStrategy",
    "build_namespace",
    "format_traceback",
    "load_strategy",
    "parse_meta",
]
