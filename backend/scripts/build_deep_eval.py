"""深研报 rubric 评测（P3 收尾项 5c，**手动轨**）。

与 `build_rag_eval.py` 的分工：那边评**检索**（NDCG@10，200 条），这边评**报告**（12 题，
自动项 + LLM-judge 五维）。它需要真模型、真 Qdrant、真 `data/`，**不进 CI**。

```
cd backend && uv run python scripts/build_deep_eval.py --limit 3      # 先冒烟
cd backend && uv run python scripts/build_deep_eval.py                # 全量 12 题
```

产物：`logs/p3/deep_eval.json`（含每题报告与读数）+ `logs/p3/deep_eval.md`（一页纸）。
**不设及格线**：PIT 违规与辩论轮次是硬事实，judge 分数只作主观读数。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.deep.budget import Budget  # noqa: E402
from app.agent.deep.roles.base import RoleContext  # noqa: E402
from app.agent.deep.router import build_brief, route_question  # noqa: E402
from app.agent.deep.runner import astream_deep_run  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.llm import DEFAULT_MODEL, complete  # noqa: E402
from app.report.deep_eval import aggregate, auto_score, parse_judge, render_markdown  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SET = Path(__file__).resolve().parents[1] / "tests" / "research" / "eval_set_v1.json"
OUT = ROOT / "logs" / "p3"

JUDGE_PROMPT = """你是投研报告评测员。只根据下面这份报告的内容打分，不要补充外部知识。
按五维各给 1–5 分（3 = 合格）：
1. pit：是否只使用了 as_of 之前可得的信息，有没有穿越未来；
2. evidence：事实性结论是否有证据/引用支撑；
3. numbers：正文数字是否能回溯到同源材料；
4. balance：是否同时呈现多空证据，而不是只列一边；
5. risk：是否说明了风险、分歧与数据缺口。

只输出一行 JSON：
{{"pit": n, "evidence": n, "numbers": n, "balance": n, "risk": n, "reason": "一句话"}}

问题：{question}
时点：{as_of}
结论：
{conclusion}
分歧 / 风险：
{disagreements}
{risks}
证据标题（最多 12 条）：
{evidence}
闸门：{gate}
缺口：{gaps}
"""


def _block_text(report: dict[str, Any], block_id: str) -> str:
    for block in report.get("blocks") or []:
        if block.get("id") == block_id:
            return str(block.get("text") or "")
    return ""


def _conclusion(report: dict[str, Any]) -> str:
    return _block_text(report, "conclusion")[:4000]


def _evidence_titles(report: dict[str, Any]) -> str:
    rows = report.get("evidence") or []
    if not rows:
        return "（无）"
    return "\n".join(
        f"- [{row.get('event_time')} / 可得 {row.get('available_at')}] {row.get('title')}"
        for row in rows[:12]
    )


async def judge_report(report: dict[str, Any]) -> dict[str, Any] | None:
    """一次 flash judge；解析失败/调用失败返回 None（不伪造分数）。"""
    gate = report.get("gate") or {}
    prompt = JUDGE_PROMPT.format(
        question=(report.get("brief") or {}).get("question", ""),
        as_of=(report.get("brief") or {}).get("as_of", ""),
        conclusion=_conclusion(report) or "（无结论块）",
        disagreements=_block_text(report, "disagreements")[:1500] or "（无）",
        risks=_block_text(report, "risks")[:1500] or "（无）",
        evidence=_evidence_titles(report),
        gate=json.dumps(
            {
                "decision": gate.get("decision"),
                "stray_numbers": gate.get("stray_numbers") or [],
                "notes": gate.get("notes") or [],
            },
            ensure_ascii=False,
        ),
        gaps="；".join(report.get("gaps") or []) or "（无）",
    )
    try:
        result = await complete(prompt, timeout=45.0, thinking=False)
    except Exception as exc:  # noqa: BLE001 —— judge 失败是读数的一部分，不中断整轮
        return {"error": f"{type(exc).__name__}: {exc}"}
    parsed = parse_judge(result.text)
    if parsed is None:
        return {"error": "judge 输出不是可解析的五维 JSON", "raw": result.text[:300]}
    parsed["model"] = result.model
    parsed["usage"] = result.usage
    return parsed


async def run_one(item: dict[str, Any], *, use_judge: bool) -> dict[str, Any]:
    from langgraph.checkpoint.memory import InMemorySaver

    as_of = datetime.fromisoformat(f"{item['as_of']}T15:00:00+08:00")
    decision = await route_question(item["question"], force="deep")
    brief = build_brief(
        item["question"],
        decision=decision,
        as_of=as_of,
        symbols=tuple(item.get("symbols") or ()),
    )
    ctx = RoleContext(brief=brief, budget=Budget())
    run_id = f"eval-{item['id']}-{datetime.now().strftime('%H%M%S')}"
    frames: list[tuple[str, dict[str, Any]]] = []
    started = time.perf_counter()
    async for frame in astream_deep_run(
        ctx=ctx,
        checkpointer=InMemorySaver(),
        run_id=run_id,
        brief=brief,
    ):
        frames.append(frame)
    elapsed = round(time.perf_counter() - started, 1)

    done = next((payload for name, payload in frames if name == "done"), {})
    report = next((payload.get("report") for name, payload in frames if name == "report"), None)
    status = str(done.get("status") or ("failed" if report is None else "done"))
    auto = auto_score(
        report,
        status=status,
        budget=ctx.budget.to_payload(),
        elapsed_s=elapsed,
    )
    judge = await judge_report(report) if use_judge and report else None
    return {
        "id": item["id"],
        "question": item["question"],
        "symbols": item.get("symbols") or [],
        "as_of": item["as_of"],
        "focus": item.get("focus"),
        "rubric_hint": item.get("rubric_hint"),
        "status": status,
        "report_hash": done.get("report_hash"),
        "auto": auto,
        "judge": judge,
        "report": report,
        "error": None if report is not None else "深路径没有产出 report 帧（失败/等待审批）",
    }


async def run_set(items: list[dict[str, Any]], *, use_judge: bool) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        print(f"[{index}/{len(items)}] {item['id']} {item['as_of']} …", flush=True)
        try:
            result = await run_one(item, use_judge=use_judge)
        except Exception as exc:  # noqa: BLE001 —— 单题失败不拖垮整轮评测
            result = {
                "id": item["id"],
                "question": item["question"],
                "symbols": item.get("symbols") or [],
                "as_of": item["as_of"],
                "focus": item.get("focus"),
                "rubric_hint": item.get("rubric_hint"),
                "status": "failed",
                "report_hash": None,
                "auto": auto_score(None, status="failed"),
                "judge": None,
                "report": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
        auto = result["auto"]
        print(
            f"    {result['status']} · 证据 {auto['evidence_count']} · "
            f"PIT 违规 {len(auto['pit_violations'])} · 缺口 {auto['gaps']} · "
            f"judge {'—' if not result['judge'] else '✓'}",
            flush=True,
        )
        results.append(result)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="深研报 rubric 评测（手动轨）")
    parser.add_argument("--set", default=str(DEFAULT_SET), help="评测集 JSON")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题（0 = 全量）")
    parser.add_argument("--no-judge", action="store_true", help="只跑自动项，不调 judge")
    parser.add_argument("--output", default=str(OUT / "deep_eval.json"))
    parser.add_argument("--md", default=str(OUT / "deep_eval.md"))
    args = parser.parse_args()

    dataset = Path(args.set)
    items = json.loads(dataset.read_text(encoding="utf-8"))
    if args.limit > 0:
        items = items[: args.limit]
    if not items:
        print("评测集为空")
        return 1

    settings = get_settings()
    results = asyncio.run(run_set(items, use_judge=not args.no_judge))

    meta = {
        "dataset": str(dataset.relative_to(ROOT)) if dataset.is_relative_to(ROOT) else str(dataset),
        "count": len(results),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "fast_model": settings.research_fast_model,
        "deep_model": settings.research_deep_model,
        "judge_model": "off" if args.no_judge else DEFAULT_MODEL,
        "as_of_rule": "每题 as_of 当日 15:00 Asia/Shanghai；证据 available_at 不得越过它",
    }
    payload = {"meta": meta, "summary": aggregate(results), "items": results}
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = Path(args.md)
    markdown.parent.mkdir(parents=True, exist_ok=True)
    markdown.write_text(render_markdown(results, meta), encoding="utf-8")

    summary = payload["summary"]
    print("\n聚合：" + json.dumps(summary, ensure_ascii=False))
    print(f"产物：{target}\n      {markdown}")
    return 0 if summary["report_assembled"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
