"""小石 MCP 真实握手验收（T1 硬指标）。

由客户端进程真实拉起 stdio 服务并完成：
    initialize → tools/list → get_agent_workflow → 一次有界认证数据查询
再用 langchain-mcp-adapters（T3 生产路径）复跑一遍工具注册。

约定：
  * 密钥只经子进程环境变量传递，绝不进 argv；
  * 落盘证据只记元数据（工具名/计数/耗时/HTTP 码），不记响应正文；
  * 脚本结束前自检：证据中不得出现明文密钥。

用法：
    cd backend && uv run python scripts/verify_xiaoshi_mcp.py [--evidence logs/mcp-verification.json]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

import app  # noqa: E402,F401 —— 导入即锁死 LANGGRAPH_STRICT_MSGPACK
from app.core.config import get_settings  # noqa: E402
from app.data.xiaoshi import mcp_stdio_params  # noqa: E402

KEY_CHECK_URL = "https://api.shizixi.com/api/v3/auth/api-key/check"
WORKFLOW_TOOL = "get_agent_workflow"

EVIDENCE: dict = {
    "schema": "quantsage.mcp_verification/v1",
    "started_utc": datetime.now(timezone.utc).isoformat(),
    "steps": [],
}


def mask_secret(value: str) -> str:
    """只暴露长度与哈希前缀，便于跨次比对而不泄漏内容。"""
    if not value:
        return "<unset>"
    digest = hashlib.sha256(value.encode()).hexdigest()[:12]
    return f"<set len={len(value)} sha256={digest}>"


def record(step: str, **fields) -> None:
    EVIDENCE["steps"].append({"step": step, **fields})
    printable = {k: v for k, v in fields.items() if k != "elapsed_ms"}
    suffix = f" ({fields['elapsed_ms']}ms)" if "elapsed_ms" in fields else ""
    print(f"[{step}] {json.dumps(printable, ensure_ascii=False)}{suffix}")


async def check_key() -> None:
    """核验原 Key（不轮换、不重注册）。"""
    key = get_settings().xiaoshi_api_key.get_secret_value()
    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.get(KEY_CHECK_URL, headers={"Authorization": f"Bearer {key}"})
    body = response.json()
    record(
        "auth_check",
        http_status=response.status_code,
        valid=body.get("valid"),
        key=mask_secret(key),
    )
    assert response.status_code == 200 and body.get("valid") is True, (
        f"Key 核验失败：HTTP {response.status_code}"
    )


def _tool_text(result) -> str:
    return " ".join(
        getattr(block, "text", "") or "" for block in result.content
    ).strip()


def pick_bounded_query(tools: list, symbol: str) -> tuple[str, dict]:
    """按 tools/list 的实际结果绑定有界查询（不猜死工具名）。"""
    by_name = {tool.name: tool for tool in tools}
    if "get_live_quote" in by_name:
        return "get_live_quote", {"symbol": symbol}

    # 退化：时间窗 ≤1 天、条数 =1 的事件时间线，任何一项无法安全合成即报错
    if "get_event_timeline" in by_name:
        now = datetime.now(timezone.utc)
        return "get_event_timeline", {
            "since": (now - timedelta(days=1)).strftime("%Y-%m-%d"),
            "to": now.strftime("%Y-%m-%d"),
            "limit": 1,
        }

    raise SystemExit(
        "未找到可安全合成的有界查询工具；实际工具清单："
        + ", ".join(sorted(by_name))
        + "。请人工指定后再跑，不要退化成无参调用（那是无界查询）。"
    )


async def protocol_gate(args: argparse.Namespace) -> None:
    """协议层：最小依赖（mcp SDK）的真实握手。"""
    params = StdioServerParameters(**mcp_stdio_params())
    errlog = None if args.show_server_stderr else open("/dev/null", "w")  # noqa: SIM115
    try:
        async with asyncio.timeout(args.timeout):
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    started = time.perf_counter()
                    init = await session.initialize()
                    record(
                        "initialize",
                        elapsed_ms=int((time.perf_counter() - started) * 1000),
                        protocol_version=init.protocolVersion,
                        server_name=init.serverInfo.name,
                        server_version=getattr(init.serverInfo, "version", None),
                        tools_capability=init.capabilities.tools is not None,
                    )
                    assert init.protocolVersion and init.serverInfo.name, "initialize 回包不完整"

                    started = time.perf_counter()
                    tools = (await session.list_tools()).tools
                    names = sorted(tool.name for tool in tools)
                    record(
                        "tools_list",
                        elapsed_ms=int((time.perf_counter() - started) * 1000),
                        count=len(names),
                        names=names,
                    )
                    assert names, "tools/list 为空"
                    assert WORKFLOW_TOOL in names, f"未暴露 {WORKFLOW_TOOL}：{names}"

                    started = time.perf_counter()
                    workflow = await session.call_tool(WORKFLOW_TOOL, {})
                    workflow_text = _tool_text(workflow)
                    record(
                        "get_agent_workflow",
                        elapsed_ms=int((time.perf_counter() - started) * 1000),
                        is_error=workflow.isError,
                        content_chars=len(workflow_text),
                    )
                    assert not workflow.isError and workflow_text, "get_agent_workflow 失败"

                    tool_name, tool_args = pick_bounded_query(tools, args.symbol)
                    started = time.perf_counter()
                    result = await session.call_tool(tool_name, tool_args)
                    text = _tool_text(result)
                    record(
                        "bounded_query",
                        elapsed_ms=int((time.perf_counter() - started) * 1000),
                        tool=tool_name,
                        args=tool_args,
                        is_error=result.isError,
                        content_chars=len(text),
                    )
                    assert not result.isError, f"有界查询失败：{text[:200]}"
                    assert text, "有界查询返回空"
                    for marker in ("401", "Unauthorized", "API Key 与当前账号不匹配"):
                        assert marker not in text, f"认证未通过：{marker}"
                    print(f"    响应预览：{text[:160]}")
    finally:
        if errlog is not None:
            errlog.close()


async def adapters_gate(args: argparse.Namespace) -> None:
    """生产路径：langchain-mcp-adapters 注册工具（T3 会用同一路径）。"""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    from app.data.xiaoshi import adapter_connection

    client = MultiServerMCPClient(adapter_connection())
    async with asyncio.timeout(args.timeout):
        tools = await client.get_tools()
    names = sorted(tool.name for tool in tools)
    record("adapters_get_tools", count=len(names), names=names)
    assert names, "适配器未取到任何工具"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="600519", help="有界查询用的标的（默认贵州茅台）")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--evidence", type=Path, default=None)
    parser.add_argument("--skip-adapters", action="store_true")
    parser.add_argument("--show-server-stderr", action="store_true")
    args = parser.parse_args()

    await check_key()
    await protocol_gate(args)
    if not args.skip_adapters:
        await adapters_gate(args)

    EVIDENCE["verdict"] = "pass"
    EVIDENCE["finished_utc"] = datetime.now(timezone.utc).isoformat()

    blob = json.dumps(EVIDENCE, ensure_ascii=False)
    secret = get_settings().xiaoshi_api_key.get_secret_value()
    assert secret not in blob, "证据里出现明文密钥，拒绝落盘"

    print("\n" + json.dumps(EVIDENCE, ensure_ascii=False, indent=2))
    if args.evidence:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(EVIDENCE, ensure_ascii=False, indent=2))
        print(f"\n证据已写入 {args.evidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
