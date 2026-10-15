"""对外 MCP Server 的真实握手验收（E8b，DoD 支柱）。

由客户端进程**真实拉起** `scripts/serve_mcp.py`（stdio），完成：

    initialize → tools/list → 一次无约束检索 → **PIT 双断言**

PIT 双断言是本脚本存在的理由（结构上只有真链路能验）：

 ① **每条命中的 `available_at` 都不晚于生效的 `as_of`**——这是护城河的行为证据，
    直接从返回体自证（不需要另建探针数据）；
 ② **换一个更早的 `as_of`，命中集必须变**——只做 ① 的话，一个「永远返回全库」的实现
    在 `as_of = 现在` 下也照样通过。第二条把「过滤真的生效」钉死。

约定（与 `verify_xiaoshi_mcp.py` 同）：证据只记元数据与**标题级**摘要，不记正文全文；
脚本自检证据里不含任何密钥。

用法：
    cd backend && uv run python scripts/verify_quantsage_mcp.py [--evidence ../logs/e8/mcp-verification.json]

前置：Qdrant 起着、语料清单在本地（首个调用要载嵌入模型，冷启动 7–15s）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.logging import SENSITIVE_ENV_KEYS  # noqa: E402
from app.rag.retrieve import parse_as_of, resolve_as_of  # noqa: E402

TOOL = "search_events"
#: 验收用的两个窗口：早的那个必须让「晚可得」的事件出局，晚的那个必须能把它们放回来。
EARLY_AS_OF = "2026-08-01"
LATE_AS_OF = "2026-09-30"
PROBE_QUERY = "央行 降准 政策 银行"

EVIDENCE: dict[str, Any] = {
    "schema": "quantsage.mcp_verification/v1",
    "started_utc": datetime.now(UTC).isoformat(),
    "steps": [],
}


def record(step: str, **fields: Any) -> None:
    EVIDENCE["steps"].append({"step": step, **fields})
    suffix = f" ({fields['elapsed_ms']}ms)" if "elapsed_ms" in fields else ""
    printable = {k: v for k, v in fields.items() if k != "elapsed_ms"}
    print(f"[{step}] {json.dumps(printable, ensure_ascii=False)}{suffix}")


def structured(result: Any) -> dict[str, Any]:
    """取结构化内容：FastMCP 的 dict 返回同时给 text 与 structuredContent 两路。

    **没有结构化那一路就报错**，不退回解析文本——那会把「协议层给的东西」换成
    「我们猜出来的东西」，验收就不算数了。
    """
    payload = getattr(result, "structuredContent", None)
    if payload is None:
        raise SystemExit(f"工具没有返回结构化内容：{getattr(result, 'content', None)!r}")
    return payload


def server_params() -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).resolve().parent / "serve_mcp.py")],
        cwd=str(Path(__file__).resolve().parents[1]),
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--evidence", type=Path, default=None)
    parser.add_argument("--show-server-stderr", action="store_true")
    args = parser.parse_args()

    errlog = None if args.show_server_stderr else open("/dev/null", "w")  # noqa: SIM115
    try:
        async with asyncio.timeout(args.timeout):
            # 三层嵌套是 MCP SDK 的用法本身；合并成括号形式要整体重排 75 行，不值得
            async with stdio_client(server_params(), errlog=errlog) as (read, write):  # noqa: SIM117
                async with ClientSession(read, write) as session:
                    started = time.perf_counter()
                    init = await session.initialize()
                    record(
                        "initialize",
                        elapsed_ms=int((time.perf_counter() - started) * 1000),
                        protocol_version=init.protocolVersion,
                        server_name=init.serverInfo.name,
                    )

                    started = time.perf_counter()
                    tools = (await session.list_tools()).tools
                    names = sorted(tool.name for tool in tools)
                    record(
                        "tools_list",
                        elapsed_ms=int((time.perf_counter() - started) * 1000),
                        count=len(names),
                        names=names,
                    )
                    assert names == [TOOL], f"暴露的工具与预期不符：{names}"

                    # 无约束检索（as_of 省略 = 现在）：主要看冷启动与返回形状
                    started = time.perf_counter()
                    first = structured(await session.call_tool(TOOL, {"query": PROBE_QUERY, "top_k": 5}))
                    record(
                        "search_now",
                        elapsed_ms=int((time.perf_counter() - started) * 1000),
                        ok=first.get("ok"),
                        as_of=first.get("as_of"),
                        score_kind=first.get("score_kind"),
                        count=first.get("count"),
                        coverage=first.get("coverage"),
                        titles=[hit["title"][:40] for hit in first.get("hits", [])],
                    )
                    assert first.get("ok") is True, f"检索失败：{first}"
                    assert first.get("coverage", {}).get("start"), "没有回语料覆盖窗口"

                    # 两个窗口各拉一次（top_k 拉满，便于比命中集）
                    for label, as_of in (("early", EARLY_AS_OF), ("late", LATE_AS_OF)):
                        started = time.perf_counter()
                        result = structured(
                            await session.call_tool(
                                TOOL, {"query": PROBE_QUERY, "as_of": as_of, "top_k": 10}
                            )
                        )
                        # 冷/热两档读数都留着：冷启动载模型是首个调用的账，不是每次调用的账
                        elapsed = int((time.perf_counter() - started) * 1000)
                        assert result.get("ok") is True, f"{label} 检索失败：{result}"
                        hits = result.get("hits", [])
                        assert result["as_of"].startswith(as_of), (
                            f"{label} 的 as_of 没回显对：{result['as_of']}"
                        )

                        # 断言 ①：PIT 硬约束——每条命中的可得时间都不晚于**生效时点**
                        # （拿回显的那一个比，不是拿我们传进去的字符串比：判据要落在
                        #  「服务端实际用了什么时点」上）
                        cutoff = resolve_as_of(parse_as_of(result["as_of"]))
                        undated = [hit["event_id"] for hit in hits if not hit.get("available_at")]
                        late_hits = [
                            hit["event_id"]
                            for hit in hits
                            if hit.get("available_at")
                            and resolve_as_of(parse_as_of(hit["available_at"])) > cutoff
                        ]
                        record(
                            f"pit_invariant_{label}",
                            elapsed_ms=elapsed,
                            as_of=result["as_of"],
                            count=len(hits),
                            violations=late_hits,
                            missing_available_at=undated,
                            available_at=[hit.get("available_at") for hit in hits],
                        )
                        assert not undated, f"{label} 窗口有命中缺 available_at：{undated}"
                        assert not late_hits, f"{label} 窗口返回了该时点还不知道的事件：{late_hits}"
                        assert hits, f"{label} 窗口一条都没命中——断言①会变成空转，换窗口再跑"
                        EVIDENCE.setdefault("hits", {})[label] = [hit["event_id"] for hit in hits]

                    # 断言 ②：换一个更早的窗口，命中集必须变（只做 ① 的话，
                    # 「永远返回全库」的实现在 as_of=现在 下也能通过）
                    early_ids = set(EVIDENCE["hits"]["early"])
                    late_ids = set(EVIDENCE["hits"]["late"])
                    record(
                        "pit_filter_effective",
                        early=sorted(early_ids),
                        late=sorted(late_ids),
                        delta=sorted(late_ids - early_ids),
                    )
                    assert early_ids != late_ids, (
                        "两个窗口的命中集完全相同——过滤很可能没生效"
                        f"（early={sorted(early_ids)}）"
                    )
    finally:
        if errlog is not None:
            errlog.close()

    EVIDENCE["verdict"] = "pass"
    EVIDENCE["finished_utc"] = datetime.now(UTC).isoformat()

    blob = json.dumps(EVIDENCE, ensure_ascii=False)
    secrets = {
        get_settings().jwt_secret.get_secret_value(),
        get_settings().deepseek_api_key.get_secret_value(),
        get_settings().xiaoshi_api_key.get_secret_value(),
    }
    secrets |= {os.environ.get(key, "") for key in SENSITIVE_ENV_KEYS}
    leaked = [s for s in secrets if s and s in blob]
    assert not leaked, "证据里出现明文密钥，拒绝落盘"

    print("\n" + json.dumps(EVIDENCE, ensure_ascii=False, indent=2))
    if args.evidence:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(EVIDENCE, ensure_ascii=False, indent=2))
        print(f"\n证据已写入 {args.evidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
