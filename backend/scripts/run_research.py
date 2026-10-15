"""M8a 取证 CLI：路由 → brief → 五个角色**并行取证** → 证据归并，跑真实数据与真实模型。

它验四件事（自动化层看不见的那些）：

1. **路由真的会分岔**：研究型问题走深路径、事实型回快路径，标的名解析用的是真字典；
2. **并行取证是真的并行**：墙钟 ≈ 最慢那条（含 MPS 串行的检索），不是五条之和；
3. **降级如实**：某角色取不到材料时进 `gaps`，其余角色照常产出；
4. **数字闸门在角色层就生效**：编造数字的要点被剔除并留痕。

```
cd backend && uv run python scripts/run_research.py "分析一下贵州茅台现在值不值得买"
cd backend && uv run python scripts/run_research.py --symbols 600519 --as-of 2026-09-30 --mode deep
```

退出码：0 有证据归并成功；1 全部角色都没取到材料。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.deep.budget import Budget  # noqa: E402
from app.agent.deep.merge import merge  # noqa: E402
from app.agent.deep.roles import ROLE_RUNNERS  # noqa: E402
from app.agent.deep.roles.base import RoleContext  # noqa: E402
from app.agent.deep.router import build_brief, route_question  # noqa: E402
from app.agent.deep.types import ROLE_LABELS, RoleResult  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.data import duckdb_client as dc  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "logs" / "m8"


def default_as_of() -> datetime:
    """缺省时点 = **行情最后一根 bar 的交易日 15:00（北京时间）**，不写死「今天」。"""
    days = dc.trade_dates()
    if not days:
        raise SystemExit("本地没有行情数据（先跑 scripts/download_bars.py）")
    return datetime.fromisoformat(f"{days[-1].isoformat()}T15:00:00+08:00")


async def run_role(name: str, ctx: RoleContext) -> tuple[str, RoleResult, float]:
    """跑一个角色，**异常在这里收口成 `errors`**（与 M8b 图节点的口径一致）。"""
    started = time.perf_counter()
    try:
        result = await ROLE_RUNNERS[name](ctx)
    except Exception as exc:  # noqa: BLE001 —— 一处收口，异常类型不散在五个角色里
        result = RoleResult(name, errors=(f"取证异常（{type(exc).__name__}: {exc}）",))
    return name, result, round(time.perf_counter() - started, 2)


async def gather(question: str, *, symbols: tuple[str, ...] | None, as_of: datetime, mode: str | None):
    settings = get_settings()
    decision = await route_question(question, force=mode) if mode else await route_question(question)
    brief = build_brief(question, decision=decision, as_of=as_of, symbols=symbols)
    budget = Budget()
    ctx = RoleContext(brief=brief, budget=budget)

    started = time.perf_counter()
    gathered = await asyncio.gather(*(run_role(name, ctx) for name in ROLE_RUNNERS))
    wall = round(time.perf_counter() - started, 2)
    results = [result for _, result, _ in gathered]
    timings = {ROLE_LABELS.get(name, name): seconds for name, _, seconds in gathered}
    return decision, brief, merge(results), budget, wall, timings, settings


def render(decision, brief, merged, budget, wall, timings) -> str:
    lines = [
        f"路由：{decision.path}（{decision.reason}）",
        f"标的：{'、'.join(brief.symbols) or '（未解析出）'}　时点：{brief.as_of.isoformat()}",
    ]
    if brief.notes:
        lines.append("解析标注：" + "；".join(brief.notes))
    lines.append("")
    for label in (*ROLE_LABELS.values(),):
        role = next((r for r in merged.results if ROLE_LABELS.get(r.role) == label), None)
        if role is None:
            continue
        state = "降级" if role.errors else "正常"
        lines.append(f"【{label}】{state}　{timings.get(label, 0)}s　"
                     f"要点 {len(role.findings)} / 数字 {len(role.numbers)} / 证据 {len(role.evidence)}")
        for finding in role.findings:
            lines.append(f"    - {finding}")
        for error in role.errors:
            lines.append(f"    ⚠ {error}")
        for note in role.notes:
            lines.append(f"    · {note}")
    lines += ["", f"并行取证墙钟 {wall}s（五条之和 {round(sum(timings.values()), 2)}s）",
              f"归并：证据 {len(merged.evidence)} 条（去重 {merged.duplicates}）/ 缺口 {len(merged.gaps)} 条"]
    for gap in merged.gaps:
        lines.append(f"    ⚠ {gap}")
    lines += ["", "证据集（键为 `event_id|day`，与 claim 闸门同形）："]
    for item in merged.evidence[:10]:
        lines.append(f"    [{item.key}] {item.event_time} / 可得 {item.available_at}　{item.title[:40]}")
    lines += ["", "数字表："]
    for key, value in list(merged.numbers.items())[:20]:
        lines.append(f"    {key} = {value}")
    lines.append("")
    lines.append("预算：" + json.dumps(budget.to_payload(), ensure_ascii=False))
    return "\n".join(lines)


async def run_deep(question: str, *, as_of: datetime, symbols: tuple[str, ...] | None, mode: str | None) -> int:
    """M8b：跑**整条深路径**（真模型 + 真数据），产出端到端读数与报告摘要。"""
    from langgraph.checkpoint.memory import InMemorySaver

    from app.agent.deep.runner import astream_deep_run
    from app.report.deep import REPORT_KIND

    decision = await route_question(question, force=mode) if mode else await route_question(question)
    brief = build_brief(question, decision=decision, as_of=as_of, symbols=symbols)
    if not brief.symbols:
        print("没有解析出标的——请在问题里写明代码或全名，或用 --symbols 指定")
        return 1

    ctx = RoleContext(brief=brief, budget=Budget())
    run_id = f"cli-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    started = time.perf_counter()
    frames: list[tuple[str, dict]] = []
    async for frame in astream_deep_run(
        ctx=ctx, checkpointer=InMemorySaver(), run_id=run_id, brief=brief
    ):
        frames.append(frame)

    elapsed = round(time.perf_counter() - started, 1)
    names = [name for name, _ in frames]
    done = next((payload for name, payload in frames if name == "done"), {})
    report_payload = next((payload for name, payload in frames if name == "report"), {})

    print(f"路由：{decision.path}（{decision.reason}）")
    print(f"标的：{'、'.join(brief.symbols)}　时点：{brief.as_of.isoformat()}")
    print(f"帧序列：{names.count('node_start')} 个 node_start / {names.count('node_end')} 个 node_end"
          f"{' / 有 interrupt' if 'interrupt' in names else ''}")
    print(f"**端到端墙钟 {elapsed}s**（F2 读数）")
    print(f"状态：{done.get('status')}　report_hash：{str(done.get('report_hash'))[:16]}")
    print("预算：" + json.dumps(ctx.budget.to_payload(), ensure_ascii=False))

    report = report_payload.get("report")
    if report:
        print(f"报告 kind：{report.get('kind')}（期望 {REPORT_KIND}）　块：{[b['id'] for b in report['blocks']]}")
        print(f"证据 {len(report['evidence'])} 条　缺口 {len(report.get('gaps') or [])} 条")
        debate = report.get("debate") or []
        rounds = {turn["round"] for turn in debate}
        print(f"辩论 {len(rounds)} 轮 / {len(debate)} 次发言（硬上限 3）")
        conclusion = next(b for b in report["blocks"] if b["id"] == "conclusion")
        print("\n结论：\n" + str(conclusion.get("text"))[:800])
        if report.get("gaps"):
            print("\n缺口：" + "；".join(report["gaps"]))

    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / "deep.json"
    target.write_text(json.dumps({
        "question": question, "route": decision.to_payload(), "brief": brief.to_payload(),
        "elapsed_seconds": elapsed, "budget": ctx.budget.to_payload(),
        "frames": [name for name, _ in frames],
        "node_events": next((p.get("events") for n, p in frames if n == "done"), []),
        "report": report,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n证据：{target}")
    return 0 if report else 1


async def main() -> int:
    parser = argparse.ArgumentParser(description="M8 深度研究取证")
    parser.add_argument("question", nargs="?", default="分析一下贵州茅台现在值不值得买")
    parser.add_argument("--symbols", default="", help="逗号分隔；给了就跳过路由解析")
    parser.add_argument("--as-of", default="", help="YYYY-MM-DD（缺省取行情末端当日 15:00）")
    parser.add_argument("--mode", default="", choices=["", "deep", "quick"], help="跳过判定")
    parser.add_argument("--json", default="", help="把证据包写到该路径（默认 logs/m8/research.json）")
    parser.add_argument("--deep", action="store_true", help="跑整条深路径（含辩论与综合，真模型）")
    args = parser.parse_args()

    as_of = (
        datetime.fromisoformat(f"{args.as_of}T15:00:00+08:00")
        if args.as_of
        else default_as_of()
    )
    symbols = tuple(s.strip() for s in args.symbols.split(",") if s.strip()) or None

    if args.deep:
        return await run_deep(
            args.question, as_of=as_of, symbols=symbols, mode=args.mode or None
        )

    decision, brief, merged, budget, wall, timings, settings = await gather(
        args.question, symbols=symbols, as_of=as_of, mode=args.mode or None
    )
    print(render(decision, brief, merged, budget, wall, timings))

    OUT.mkdir(parents=True, exist_ok=True)
    target = Path(args.json) if args.json else OUT / "research.json"
    payload = {
        "question": args.question,
        "route": decision.to_payload(),
        "brief": brief.to_payload(),
        "merged": merged.to_payload(),
        "budget": budget.to_payload(),
        "wall_seconds": wall,
        "role_seconds": timings,
        "models": {
            "fast": settings.research_fast_model,
            "deep": settings.research_deep_model,
            "thinking": settings.research_thinking,
        },
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n证据：{target}")
    return 0 if merged.evidence or merged.numbers else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
