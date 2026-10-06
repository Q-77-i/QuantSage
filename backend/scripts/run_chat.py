"""T3 验收脚本：真实跑一轮对话，把 SSE 事件流打到终端。

与 API 走同一套装配（模型 + 白名单工具 + Postgres checkpointer + Langfuse），
差别只是把事件直接打印而非编码成 SSE 帧。

用法：
    cd backend && uv run python scripts/run_chat.py --ask "贵州茅台最近行情"
    uv run python scripts/run_chat.py --ask "那宁德时代呢" --thread-id <上一轮的 uuid>

退出码：0 = 本轮有工具调用且模型作了答；1 = 未达成（--strict 之外也返回 1，便于 CI 判定）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402,F401 —— 导入即锁死 LANGGRAPH_STRICT_MSGPACK
from app.agent.graph import astream_chat, build_agent  # noqa: E402
from app.agent.tools import load_xiaoshi_tools, query_market_bars  # noqa: E402
from app.core.checkpoint import open_checkpointer  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.langfuse import build_langfuse_handler  # noqa: E402
from app.core.llm import build_chat_model  # noqa: E402

DIVIDER = "─" * 64


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ask", required=True, help="要问的问题")
    parser.add_argument("--thread-id", default=None, help="续聊时传上一轮的 thread_id")
    parser.add_argument(
        "--skip-mcp", action="store_true", help="只挂本地行情工具（排查小石问题时用）"
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    thread_id = args.thread_id or str(uuid.uuid4())

    async with open_checkpointer(settings.postgres_dsn) as saver:
        tools = [query_market_bars]
        if not args.skip_mcp:
            try:
                xiaoshi_tools, missing = await load_xiaoshi_tools()
                tools.extend(xiaoshi_tools)
                note = f"，白名单缺项 {sorted(missing)}" if missing else ""
                print(f"小石工具 {len(xiaoshi_tools)} 个{note}")
            except Exception as exc:  # noqa: BLE001 —— 验收脚本允许降级跑
                print(f"⚠ 小石 MCP 不可用（{type(exc).__name__}），降级为仅本地工具")

        agent = build_agent(build_chat_model(), tools, checkpointer=saver)
        handler = build_langfuse_handler()
        callbacks = [handler] if handler is not None else None

        print(f"工具集：{[t.name for t in tools]}")
        print(f"thread_id：{thread_id}")
        print(f"问题：{args.ask}")
        print(DIVIDER)

        tool_calls = 0
        tokens = 0
        usage: dict | None = None
        started = time.perf_counter()

        try:
            async for event, payload in astream_chat(
                agent, message=args.ask, thread_id=thread_id, callbacks=callbacks
            ):
                if event == "tool_call":
                    tool_calls += 1
                    print(f"\n🔧 {payload['name']}({payload['args']})")
                elif event == "tool_result":
                    mark = "✗" if payload["is_error"] else "↳"
                    preview = payload["content"].replace("\n", " ")[:120]
                    print(f"  {mark} {preview}")
                elif event == "token":
                    tokens += 1
                    print(payload["text"], end="", flush=True)
                elif event == "done":
                    usage = payload.get("usage")
        except Exception as exc:  # noqa: BLE001 —— 验收脚本要给出可读结论而非堆栈
            print(f"\n\n✗ 对话失败：{type(exc).__name__}: {exc}")
            return 1

        elapsed = time.perf_counter() - started
        print(f"\n{DIVIDER}")
        print(
            f"耗时 {elapsed:.1f}s ｜ token 事件 {tokens} 个 ｜ 工具调用 {tool_calls} 次"
        )
        if usage:
            print(
                f"用量：输入 {usage.get('input_tokens')} / 输出 {usage.get('output_tokens')}"
                f"（其中推理 {usage.get('output_token_details', {}).get('reasoning', 0)}）"
            )
        if handler is not None:
            from langfuse import get_client

            get_client().flush()
            print(f"Langfuse：{settings.langfuse_base_url}")

    passed = tool_calls > 0 and tokens > 0
    print("✓ 验收通过：Agent 自主调用了工具并逐 token 作答" if passed else "✗ 未达验收：无工具调用或无流式输出")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
