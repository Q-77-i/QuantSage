"""离线单测：LLM 调用面的**单点注入**（P3-E1c）。

改造之前，7 个调用方是 `from app.core.llm import complete` ——**绑定导入**，patch
`app.core.llm.complete` 打不中任何调用点（要逐模块各自打一遍，而 E4 的「杀 LLM」故障注入
只能有一个注入面）。本文件就是那条性质的证据：**只 patch 一次**，三条互不相干的路径
（深路径取证角色 / 研报综述 / 决策反思）全都吃到假实现。

为什么不需要 `LLMGateway` 类：`ask_once` 内部调的是模块全局的 `complete`，所以连它一起
被同一个 patch 覆盖——模块属性访问本身就够用（SPEC §2.5）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from app.agent.deep.budget import Budget
from app.agent.deep.roles.base import RoleContext, interpret
from app.agent.deep.types import Brief
from app.core import llm
from app.memory.reflection import build_reflection
from app.report.narrative import build_narrative

AS_OF = datetime.fromisoformat("2026-09-30T15:00:00+08:00")


def test_one_patch_serves_roles_narrative_and_reflection(
    monkeypatch: Any,
) -> None:
    """patch `app.core.llm.complete` 一处 → 三条路径全部走假实现。"""
    prompts: list[str] = []

    async def fake_complete(prompt: str, **kwargs: Any) -> llm.Completion:
        prompts.append(prompt)
        return llm.Completion(text="- 假要点", model="fake-model", usage={})

    monkeypatch.setattr(llm, "complete", fake_complete)

    async def scenario() -> tuple[Any, Any, Any]:
        brief = Brief(question="分析一下", symbols=("600519",), as_of=AS_OF)
        ctx = RoleContext(brief=brief, budget=Budget(), thinking=False)
        role = await interpret("market", ctx, material="材料", numbers={"区间收益": "1.00%"})
        narrative = await build_narrative({"总收益": "1.00%"})
        reflection = await build_reflection({"总收益": "1.00%"})
        return role, narrative, reflection

    role, narrative, reflection = asyncio.run(scenario())

    # 三条路径都真的调了模型（而不是各自造了一个真模型 / 静默降级）
    assert len(prompts) == 3
    assert role.ok and role.findings == ("假要点",)  # 角色侧会剥掉「- 」前缀
    assert narrative.text == "- 假要点"
    assert reflection.text == "- 假要点"
    # 三次调用各带各的 prompt（不是同一份被复用）
    assert "取证角色" in prompts[0]
    assert len(set(prompts)) == 3


def test_call_sites_share_one_injectable_global() -> None:
    """七个调用方 import 的是**同一个模块对象**——所以打桩一处、七处同时生效。

    这条是上一条用例成立的原因，单独写出来是因为它最容易在重构里被悄悄破坏
    （有人改回 `from app.core.llm import complete`，行为用例可能仍然绿，注入面却没了）。
    """
    from app.agent.deep import debate, router, synthesize
    from app.agent.deep.roles import base as roles_base
    from app import main as main_module
    from app.memory import reflection as reflection_module
    from app.report import narrative as narrative_module

    importers = (
        roles_base, debate, router, synthesize, reflection_module, narrative_module, main_module
    )
    for module in importers:
        assert module.llm is llm, module.__name__
