"""策略模板库（M4b）：5 份**交给沙箱执行的源码文本**，是模板的唯一真源。

模板不是可导入模块——`Signal` / `Side` 由沙箱预注入，`ctx` 是引擎给的，直接在进程里 import
这些文件只会得到一堆未定义的全局名。加载一律走 `importlib`/`Path` 读文本（本模块的 `_read`）。

`builtin` 非空 = 这份模板是某个内置策略的**源码等价版**，等价性测试遍历 `TEMPLATES` 按它自动
配对（并断言恰好 2 个带 `builtin`）——新增模板时忘了配测试会直接红。

模板不进内置注册表（`app/backtest/strategies/`）：那要再写一份类实现，等于两处真源、两处维护。
外三个模板（唐奇安 / RSI / 放量突破）没有内置对应物，证据是「检查器零命中 + 沙箱跑通 +
合成触发序列上确实成交」。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_DIR = Path(__file__).parent


@dataclass(frozen=True, slots=True)
class Template:
    key: str
    title: str
    summary: str
    source: str
    #: 内置策略名（`app/backtest/strategies/` 注册表里的键）；None = 无内置对应物
    builtin: str | None = None


def _read(key: str) -> str:
    return (_DIR / f"{key}.py").read_text(encoding="utf-8")


TEMPLATES: tuple[Template, ...] = (
    Template(
        key="ma_cross",
        title="双均线交叉",
        summary="快线上穿慢线金叉买入、下穿死叉卖出；与内置「双均线」策略同源的源码版",
        source=_read("ma_cross"),
        builtin="ma_cross",
    ),
    Template(
        key="event_driven",
        title="事件驱动",
        summary="利多事件（评分达标）触发买入，持有 N 个交易日后卖出；演示 PIT 语义",
        source=_read("event_driven"),
        builtin="event_driven",
    ),
    Template(
        key="donchian_breakout",
        title="唐奇安通道突破",
        summary="收盘创 N 日新高买入、跌破 N 日新低卖出（通道只用当根之前的数据）",
        source=_read("donchian_breakout"),
    ),
    Template(
        key="rsi_reversal",
        title="RSI 超卖反转",
        summary="Wilder RSI 跌破阈值买入、回到高位卖出（纯 Python 实现，不用 pandas）",
        source=_read("rsi_reversal"),
    ),
    Template(
        key="volume_breakout",
        title="放量突破",
        summary="成交量放大且收阳时买入，持有 N 个交易日后卖出",
        source=_read("volume_breakout"),
    ),
)

_BY_KEY = {template.key: template for template in TEMPLATES}


def get_template(key: str) -> Template | None:
    return _BY_KEY.get(key)


def template_source(key: str) -> str:
    """按 key 取源码文本；未知 key 抛 `KeyError`（HTTP 层自己映射成 404）。"""
    template = get_template(key)
    if template is None:
        raise KeyError(key)
    return template.source


__all__ = ["TEMPLATES", "Template", "get_template", "template_source"]
