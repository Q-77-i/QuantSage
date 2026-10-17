"""静态能力卡：`GET /.well-known/agent-card.json`（P3 收尾项 5b）。

**这不是 A2A 协议实现**：没有 A2A 传输、任务、推送或对端协商。它只把本平台对外可承诺的
能力与边界写成一份只读 JSON（A2A Agent Card 的形状），供外部客户端/人阅读；发现它不需要
登录，也不需要先起 MCP。MCP 仍是 stdio 单进程、单工具 `search_events`（SPEC §8.6）。

三条纪律：① 只写静态、非敏感信息——不写绝对路径、密钥、内部表名；② `url` 按请求 host
现算，本地与反代都能用；③ 能力描述与代码/SPEC 同步，不预留空壳（CLAUDE.md §三.3）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

router = APIRouter(tags=["capabilities"])

#: 卡片的字段形状版本（不是 A2A 协议版本；协议支持见 `x-quantsage.a2a`）。
SCHEMA = "quantsage.agent-card/v1"

SKILLS: tuple[dict[str, Any], ...] = (
    {
        "id": "pit-event-search",
        "name": "PIT 事件语义检索",
        "description": (
            "按 available_at <= as_of 过滤后做语义检索（新闻 / 公告 / 政策 / 人物 / 研报），"
            "返回双时间戳与来源三元组；零命中时会改写一次查询，但 as_of 不变。"
        ),
        "tags": ["point-in-time", "rag", "events", "a-share"],
        "examples": [
            "2026-08-20 那天市场看到了哪些降准相关消息？",
            "600519 在 2026-08 有哪些业绩预告？",
        ],
        "inputModes": ["text/plain"],
        "outputModes": ["text/plain", "application/json"],
    },
    {
        "id": "deep-research",
        "name": "多 Agent 深度研究",
        "description": (
            "并行取证 → 证据归并 → Bull/Bear 辩论（≤3 轮）→ 单点综合 → 代码化风控闸门；"
            "涉及买卖指令/预算超限时 interrupt() 等人工审批，产出可追溯研报。"
        ),
        "tags": ["multi-agent", "research", "hitl", "citations"],
        "examples": ["以 2026-09-30 为时点，研究 600519 的基本面与风险"],
        "inputModes": ["text/plain"],
        "outputModes": ["text/plain", "text/markdown", "application/json"],
    },
    {
        "id": "paper-proposal",
        "name": "模拟盘下单提案",
        "description": (
            "Agent 只能创建待审批的模拟盘决策（买/卖、当前会话池内、等额配额/全仓），"
            "不批准、不成交、不改风控；用户必须在模拟盘页逐条裁决后，下一次推进按 T+1 开盘价成交。"
        ),
        "tags": ["paper-trading", "human-in-the-loop", "risk-gate"],
        "examples": ["在模拟盘里提案买入 600519，理由写清楚"],
        "inputModes": ["text/plain"],
        "outputModes": ["text/plain", "application/json"],
    },
    {
        "id": "backtest",
        "name": "PIT 回测与策略沙箱",
        "description": (
            "事件驱动回测（信号收盘生成、次根开盘成交），A 股整手/涨跌停/费用规则；"
            "用户策略在子进程沙箱运行，运行前做 shift(-n)/bfill 一类前视静态检查。"
        ),
        "tags": ["backtest", "point-in-time", "sandbox"],
        "examples": ["用 ma_cross 跑 600519 的 2025 年回测"],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json"],
    },
)


def card(request: Request) -> dict[str, Any]:
    """能力卡内容。`url` 按请求 host 现算，不把 localhost 写死。"""
    return {
        "schemaVersion": SCHEMA,
        "name": "知策 QuantSage",
        "description": (
            "前视偏差为零的 AI 投研 Agent：多 Agent 并行取证、PIT 约束语义检索、"
            "结论可回测验证；风控与审批由纯 Python/人工闸门决定，LLM 不可改写。"
        ),
        "url": str(request.base_url).rstrip("/"),
        "version": "0.1.0",
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": list(SKILLS),
        "x-quantsage": {
            "a2a": "static_metadata_only",
            "pit": {
                "gate": "available_at",
                "rule": "只返回 as_of 时点已经可得的事件；as_of 省略即现在",
            },
            "hitl": {
                "deep_research": "措辞/预算超限时 interrupt() 审批",
                "paper": "Agent 只能提案；用户逐条批准/驳回，批准后 T+1 开盘成交",
            },
            "risk_gate": "仓位/集中度/止损/回撤由纯函数判定；止损自动批准且不可驳回",
            "mcp": {
                "transport": "stdio",
                "tool": "search_events",
                "script": "backend/scripts/serve_mcp.py",
                "limits": {"top_k_max": 10, "cursor": False, "default_mode": "hybrid"},
            },
            "known_limits": [
                "日线收盘生成决策、T+1 开盘成交；不模拟盘中触发",
                "模拟盘不支持部分成交/加仓/审批时改单",
                "静态能力卡，不实现 A2A 协议传输",
            ],
        },
    }


@router.get("/.well-known/agent-card.json", include_in_schema=False)
async def agent_card(request: Request) -> dict[str, Any]:
    """公开只读能力卡；不含运行状态、租户信息或密钥。"""
    return card(request)
