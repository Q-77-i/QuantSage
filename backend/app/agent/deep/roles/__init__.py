"""五个取证角色（SPEC §9「取证角色」表）。

注册表是**唯一**的顺序真源：并行 fan-out 的节点、过程事件的键、报告里的缺口清单
都从 `ROLE_RUNNERS` 取，避免三处各写一份。

角色签名统一是 `async (RoleContext) -> RoleResult`，降级分两层：

* **可预期的降级**（RAG 不可用、LLM 超时、该时点无材料）→ 角色自己吞掉并写 `errors`；
* **意外异常**（数据源抛、解析炸）→ **原样抛出**，由 fan-out 的节点兜（M8b 的图）——
  一处收口胜过五个角色各写一遍 try/except，而且异常类型不会在五处走样。

两层合起来才是「拔掉任一数据源不崩溃」：前者让报告如实标注缺口，后者保证兄弟节点不被拖垮。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.agent.deep.roles import factor, fundamental, market, news, policy
from app.agent.deep.roles.base import RoleContext
from app.agent.deep.types import ROLES, RoleResult

RoleRunner = Callable[[RoleContext], Awaitable[RoleResult]]

ROLE_RUNNERS: dict[str, RoleRunner] = {
    "market": market.run,
    "news": news.run,
    "fundamental": fundamental.run,
    "policy": policy.run,
    "factor": factor.run,
}

assert tuple(ROLE_RUNNERS) == ROLES, "注册表顺序必须与 types.ROLES 一致（过程事件与渲染都依赖它）"

__all__ = ["ROLE_RUNNERS", "RoleContext", "RoleRunner"]
