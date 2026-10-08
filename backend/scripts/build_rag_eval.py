"""M3c 评测集：生成 → 池化 → 标注 → 报告。四步分开跑，每步都落盘可审。

```
# 1. 抽样真值事件，让 LLM 按五类各写 40 条 query（不泄漏答案）
python scripts/build_rag_eval.py generate --out tests/rag/eval_set_v1.json

# 2. 三档管线各取 top-20，与真值事件合并成候选池（需要索引已建好）
python scripts/build_rag_eval.py pool --path tests/rag/eval_set_v1.json

# 3. LLM 分级 0/1/2（真值事件锚定为 2），并把判定写回
python scripts/build_rag_eval.py grade --path tests/rag/eval_set_v1.json

# 4. 出报告：三档消融 + 分类别明细 + 延迟
python scripts/build_rag_eval.py report --path tests/rag/eval_set_v1.json --out ../logs/m3/rag_eval
```

四条纪律（对应 SPEC §4 M3c）：

* **query 必须能被本语料回答**——真值事件自身含答案；数值型只取标题/摘要里真带数字的事件；
* **五类各 40 条**：事实型 / 事件型 / 数值型 / 时间型 / 多跳型；
* **分级而非二值**：二值会把「次优结果」和「完全无关」压成同一个数，精排的增益就看不出来；
* **判定由 LLM 出、人工抽检订正**：抽检结论写回 JSON 的 `human_review`，报告里如实标注
  「未经人工逐条复核」。

池化口径的已知局限写进报告：池子外的文档一律视为不相关（无法判断 ≠ 不相关）。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtest.types import CN_TZ  # noqa: E402
from app.rag import RagNotReady  # noqa: E402
from app.rag.evaluate import (  # noqa: E402
    JudgedQuery,
    ModeScore,
    compare_modes,
    doc_key,
    score_query,
)
from app.rag.retrieve import MODE_DENSE, MODE_HYBRID, MODE_RERANK, SearchQuery, search  # noqa: E402

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "tests" / "rag" / "eval_set_v1.json"
CATEGORIES = ("事实型", "事件型", "数值型", "时间型", "多跳型")
PER_CATEGORY = 40
MODES = (MODE_DENSE, MODE_HYBRID, MODE_RERANK)

GENERATE_PROMPT = """你是金融投研问答的用户。下面给你一条 2026 年的 A 股事件，
请写一个用户会真实提出的中文问题，使得**这条事件就是它的答案**。

类别：{category}
{category_hint}

事件（仅供你理解，不要照抄标题）：
- 标题：{title}
- 摘要：{summary}
- 时间：{event_time}
- 类型：{event_type}
- 标的：{symbols}
- 行业：{industries}

硬性要求（**违反任一条这条 query 会被判废**）：
1. 只输出问题本身，一行，不要任何前缀、解释或引号；
2. **不得出现事件标题里的连续 8 个字以上**——防止把答案抄进问题；
3. **不得出现六位股票代码**——代码写在事件的元数据里，写进问题就变成关键字查表而非语义检索；
4. **不得出现具体日期**（年月日）——同理，日期也在元数据里。**唯一例外是「时间型」**，
   它的定义就是要带日期；
5. 问题要能靠检索到这条事件来回答，不要问需要外部知识才能答的东西；
6. 不要问「最新」「今天」这类相对时间。"""

CATEGORY_HINTS = {
    "事实型": "问「谁/哪家公司/什么机构」做了什么。",
    "事件型": "问某件事的经过或结果（发生了什么、进展如何）。",
    "数值型": "问事件里的具体数字（比例、金额、数量、股数），答案必须是事件里明写的数字。",
    "时间型": "问题里带一个具体日期（如「9 月 15 日」），答案必须落在那一天前后——**这一类必须带日期**。",
    "多跳型": "问题需要同时满足两个条件（如某公司 + 某类事件、某行业 + 某类公告），单看一个条件会命中大量无关事件。",
}

GRADE_PROMPT = """你在为检索评测做相关性标注。给定一个问题、一个「正解事件」和若干候选事件，
请给每个候选打相关性等级：

- 2 = 就是正解（与正解事件同一条，或内容等价）
- 1 = 沾边（同一主体/同一事件的不同报道，或能部分回答问题）
- 0 = 不相关

问题：{query}
正解事件：{truth_title}（{truth_time}）

候选：
{candidates}

只输出 JSON 数组，每个元素形如 {{"i": 序号, "grade": 0|1|2, "why": "不超过 15 字的理由"}}，
不要输出任何其它内容。"""


# ── 通用 ─────────────────────────────────────────────────────────────────


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def _llm():
    from app.core.llm import build_chat_model

    return build_chat_model()


def _text_of(response) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, list):  # 内容块
        content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return str(content).strip()


def _ask(model, prompt: str) -> str:
    return _text_of(model.invoke(prompt))


async def _ask_many(model, prompts: Sequence[str], *, concurrency: int = 6) -> list[str | None]:
    """并发跑一批 prompt（**调用是网络等待型，串行纯属浪费墙钟**）。

    单条失败不拖累整批：返回 None 占位，由调用方决定跳过还是重试。
    限流靠信号量；真被 429 打回时 litellm 自己有退避，这里不再叠一层。
    """
    import asyncio

    semaphore = asyncio.Semaphore(concurrency)

    async def one(prompt: str) -> str | None:
        async with semaphore:
            try:
                return _text_of(await model.ainvoke(prompt))
            except Exception as exc:  # noqa: BLE001 —— 单条失败不该毁掉整批
                print(f"    · 调用失败：{type(exc).__name__}", file=sys.stderr)
                return None

    return list(await asyncio.gather(*(one(p) for p in prompts)))


_CODE_RE = None
_DATE_RE = None


def leak_check(query: str, title: str, category: str) -> str | None:
    """判废规则（**可复现的机器判据，不靠信任提示词**）：答案泄漏进问题就要重写。

    泄漏之所以致命：日期与股票代码都写在嵌入文本的元数据 header 里，
    问题里带上它们，检索就退化成关键字查表，量出来的分数虚高且什么都证明不了。
    """
    global _CODE_RE, _DATE_RE
    import re

    if _CODE_RE is None:
        _CODE_RE = re.compile(r"\b\d{6}\b")
        _DATE_RE = re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月|(?<!\d)\d{1,2}\s*月\s*\d{1,2}\s*日")

    if _CODE_RE.search(query):
        return "问题里出现六位股票代码"
    if category != "时间型" and _DATE_RE.search(query):
        return "非时间型问题里出现具体日期"
    plain = "".join(ch for ch in title if not ch.isspace())
    for start in range(0, max(0, len(plain) - 7)):
        if plain[start : start + 8] in query:
            return f"问题里抄了标题的连续片段「{plain[start:start + 8]}」"
    return None


def _stable_order(rows: Sequence[dict]) -> list[dict]:
    """按内容哈希排序取样：可复现，且不依赖文件顺序（分片会增删）。"""

    def key(row: dict) -> str:
        return hashlib.sha256(f"{row['day']}|{row['event_id']}".encode()).hexdigest()

    return sorted(rows, key=key)


# ── 1. 生成 ──────────────────────────────────────────────────────────────


def payload_of(queries: list[dict], rows: Sequence[dict]) -> dict:
    """评测集的外壳（版本 / 窗口 / 标注口径）。生成与中途落盘共用同一份。"""
    return {
        "version": "v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "corpus_window": [min(r["day"] for r in rows), max(r["day"] for r in rows)],
        "categories": list(CATEGORIES),
        "annotation": {
            "generator": "deepseek-flash（经 LiteLLM）",
            "grader": "deepseek-flash（经 LiteLLM）",
            "guide": "2=正解 / 1=沾边 / 0=不相关；池化口径：池外一律视为不相关",
            "human_review": [],
        },
        "queries": queries,
    }


def cmd_generate(args: argparse.Namespace) -> int:
    import duckdb

    files = sorted((Path(args.data_dir) / "events").glob("cn-events_*.parquet"))
    if not files:
        print("语料不存在", file=sys.stderr)
        return 1
    con = duckdb.connect()
    rows = con.execute(
        f"""
        SELECT event_id, event_type, title, summary, event_time, available_at,
               symbols, industries, importance_score,
               strftime(event_time, '%Y-%m-%d') AS day
        FROM read_parquet({[str(f) for f in files]!r})
        WHERE coalesce(title, '') <> ''
        """
    ).to_arrow_table().to_pylist()
    print(f"候选事件 {len(rows)} 条")

    # 候选**过量抽取**：判废会淘汰一部分，按 40 抽就永远凑不满 40 条合格样本
    quota = max(1, args.limit // len(CATEGORIES)) if args.limit else PER_CATEGORY
    picks = _pick_truth_events(rows, per_category=int(quota * 1.35) + 3)
    model = _llm()
    prompts = [
        GENERATE_PROMPT.format(
            category=category,
            category_hint=CATEGORY_HINTS[category],
            title=row["title"],
            summary=row["summary"] or "（无）",
            event_time=str(row["event_time"])[:16],
            event_type=row["event_type"],
            symbols="/".join(row["symbols"] or []) or "（无）",
            industries="/".join(row["industries"] or []) or "（无）",
        )
        for category, row in picks
    ]
    print(f"并发 {args.concurrency} 路生成 {len(prompts)} 条候选…", flush=True)
    answers = asyncio.run(_ask_many(model, prompts, concurrency=args.concurrency))

    queries: list[dict] = []
    accepted: dict[str, int] = {}
    for (category, row), answer in zip(picks, answers, strict=True):
        if accepted.get(category, 0) >= quota or answer is None:
            continue
        question = answer.splitlines()[0].strip().strip('"“”') if answer.strip() else ""
        if len(question) < 6:
            print(f"  判废（过短）：{question!r}")
            continue
        leak = leak_check(question, row["title"], category)
        if leak:
            print(f"  判废（{leak}）：{question[:40]}")
            continue
        accepted[category] = accepted.get(category, 0) + 1
        queries.append(
            {
                "id": f"q{len(queries) + 1:03d}",
                "category": category,
                "query": question,
                "truth": doc_key(row["day"], row["event_id"]),
                "truth_title": row["title"],
                "truth_time": str(row["event_time"])[:16],
                "judgments": {},
            }
        )

    payload = payload_of(queries, rows)
    _save(Path(args.out), payload)
    print(f"✅ 生成 {len(queries)} 条 query → {args.out}")
    return 0


def _pick_truth_events(rows: Sequence[dict], *, per_category: int = PER_CATEGORY) -> list[tuple[str, dict]]:
    """按类别各挑 40 条真值事件。判据全部落在数据本身（可复现）。

    **A 股相关性是准入条件**：带标的或带行业的事件才算——语料里混着大量国际新闻，
    拿它们出题会得到一个「和这个产品无关」的评测集（用户问的是 A 股）。
    """
    import re

    relevant = [r for r in rows if (r["symbols"] or r["industries"])]
    numeric = [
        r
        for r in relevant
        if re.search(r"\d+(\.\d+)?\s*(亿|万|%|％|元|百分点|股)", r["title"] or "")
    ]
    multi = [
        r
        for r in relevant
        if len(r["symbols"] or []) >= 2 or (r["symbols"] and r["industries"])
    ]
    temporal = [r for r in relevant if r["event_type"] in ("news", "policy", "announcement")]
    general = [r for r in relevant if r["event_type"] in ("news", "announcement")]

    pools = {
        "事实型": general,
        "事件型": general,
        "数值型": numeric,
        "时间型": temporal,
        "多跳型": multi,
    }
    picks: list[tuple[str, dict]] = []
    used: set[str] = set()
    # **稀缺类别先挑**：数值型/多跳型的池子是「事实型」的子集，
    # 让宽松类别先挑会把它们吃光（一个事件只能出一道题）。
    for category in ("多跳型", "数值型", "时间型", "事实型", "事件型"):
        taken = 0
        for row in _stable_order(pools[category]):
            key = doc_key(row["day"], row["event_id"])
            if key in used:
                continue
            used.add(key)
            picks.append((category, row))
            taken += 1
            if taken >= per_category:
                break
        if taken < per_category:
            print(f"⚠️  {category} 只凑到 {taken} 条（池子 {len(pools[category])}）")
    return sorted(picks, key=lambda pair: CATEGORIES.index(pair[0]))


# ── 2. 池化 ──────────────────────────────────────────────────────────────


def cmd_pool(args: argparse.Namespace) -> int:
    payload = _load(Path(args.path))
    for index, item in enumerate(payload["queries"], start=1):
        if item.get("pool"):
            continue
        seen: dict[str, dict] = {}
        for mode in MODES:
            hits = _run_search(item["query"], mode)
            item.setdefault("rankings", {})[mode] = [
                doc_key(h.day, h.event_id) for h in hits
            ]
            for hit in hits:
                seen.setdefault(
                    doc_key(hit.day, hit.event_id),
                    {"title": hit.title, "time": (hit.event_time or "")[:10], "type": hit.event_type},
                )
        truth = item["truth"]
        seen.setdefault(truth, {"title": item["truth_title"], "time": item["truth_time"][:10], "type": "（正解）"})
        item["pool"] = [{"key": key, **meta} for key, meta in seen.items()]
        if index % 10 == 0:
            print(f"  已池化 {index}/{len(payload['queries'])}…", flush=True)
            _save(Path(args.path), payload)
    _save(Path(args.path), payload)
    print(f"✅ 池化完成：{len(payload['queries'])} 条 query")
    return 0


def _run_search(query: str, mode: str, *, top_k: int = 20):
    return search(SearchQuery(query=query, top_k=top_k), mode=mode)


# ── 3. 分级 ──────────────────────────────────────────────────────────────


def cmd_grade(args: argparse.Namespace) -> int:
    payload = _load(Path(args.path))
    todo = [item for item in payload["queries"] if not item.get("judgments")]
    if not todo:
        print("没有待标注的样本")
        return 0
    model = _llm()
    prompts = [_grade_prompt(item) for item in todo]
    print(f"并发 {args.concurrency} 路标注 {len(todo)} 条…", flush=True)
    answers = asyncio.run(_ask_many(model, prompts, concurrency=args.concurrency))

    failed = 0
    for item, raw in zip(todo, answers, strict=True):
        if raw is None:
            failed += 1
            continue
        try:
            grades = _parse_grades(raw)
        except Exception as exc:  # noqa: BLE001 —— 解析不了就跳过，下轮重跑会补上
            print(f"  [{item['id']}] 标注解析失败：{type(exc).__name__}")
            failed += 1
            continue
        judgments = {entry["key"]: grades.get(i, 0) for i, entry in enumerate(item["pool"], 1)}
        judgments[item["truth"]] = 2  # 真值事件锚定为 2（正解由构造保证）
        item["judgments"] = judgments

    _save(Path(args.path), payload)
    graded = sum(1 for q in payload["queries"] if q.get("judgments"))
    print(f"✅ 标注完成 {graded}/{len(payload['queries'])} 条（本轮失败 {failed} 条）")
    return 0


def _grade_prompt(item: dict) -> str:
    lines = [
        f"{i}. {entry['title']}（{entry['time']}，{entry['type']}）"
        for i, entry in enumerate(item["pool"], start=1)
    ]
    return GRADE_PROMPT.format(
        query=item["query"],
        truth_title=item["truth_title"],
        truth_time=item["truth_time"],
        candidates="\n".join(lines),
    )


def _parse_grades(raw: str) -> dict[int, int]:
    """从 LLM 输出里抠出 JSON 数组（它常带 ```json 围栏或前后废话）。"""
    text = raw.strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < 0:
        raise ValueError(f"没有 JSON 数组：{text[:80]}")
    items = json.loads(text[start : end + 1])
    out: dict[int, int] = {}
    for entry in items:
        if not isinstance(entry, dict) or "i" not in entry:
            continue
        grade = int(entry.get("grade", 0))
        out[int(entry["i"])] = max(0, min(2, grade))
    return out


# ── 4. 报告 ──────────────────────────────────────────────────────────────


def cmd_report(args: argparse.Namespace) -> int:
    payload = _load(Path(args.path))
    judged = [
        JudgedQuery(
            id=item["id"],
            category=item["category"],
            query=item["query"],
            judgments=item["judgments"],
            truth=item["truth"],
        )
        for item in payload["queries"]
        if item.get("judgments")
    ]
    if not judged:
        print("没有已标注的样本", file=sys.stderr)
        return 1

    missing = [q["id"] for q in payload["queries"] if not q.get("rankings")]
    if missing:
        print(f"✗ {len(missing)} 条 query 没有池化排名，先跑 pool（如 {missing[:3]}）", file=sys.stderr)
        return 1

    scores: list[ModeScore] = []
    for mode in MODES:
        bucket = ModeScore(mode=mode)
        for item in payload["queries"]:
            if not item.get("judgments"):
                continue
            ranked = item["rankings"][mode]
            bucket.add(item["id"], score_query(ranked, _judged(item)))
        scores.append(bucket)

    # 延迟**单独测**：从存储的排名里算不出耗时，而延迟是 NFR 争议的焦点，必须实测
    latencies: dict[str, dict[str, float]] = {}
    sample = [q["query"] for q in payload["queries"] if q.get("judgments")][: args.latency_samples]
    for mode in MODES:
        times = []
        if sample:
            # **预热一拍**：模型是懒加载的，第一次调用会把「载入 2.3GB 权重」算进延迟里
            # （不预热时实测把 dense 的 p95 抬到 11s、rerank 均值抬到 5.5s，全是假象）
            _run_search(sample[0], mode)
        for query in sample:
            started = time.perf_counter()
            _run_search(query, mode)
            times.append(time.perf_counter() - started)
        times.sort()
        latencies[mode] = {
            "mean": sum(times) / len(times) if times else 0.0,
            "p95": times[min(len(times) - 1, int(len(times) * 0.95))] if times else 0.0,
            "n": len(times),
        }

    categories = {item["id"]: item["category"] for item in payload["queries"] if item.get("judgments")}
    target = next(s for s in scores if s.mode == MODE_RERANK)
    summary = {
        "queries": len(judged),
        "by_mode": {s.mode: s.mean for s in scores},
        "by_category": {s.mode: s.by_category(categories) for s in scores},
        "ablation": {
            "dense→hybrid": compare_modes(scores, baseline=MODE_DENSE, target=MODE_HYBRID),
            "hybrid→rerank": compare_modes(scores, baseline=MODE_HYBRID, target=MODE_RERANK),
        },
        "latency_seconds": latencies,
        "corpus_window": payload.get("corpus_window"),
        "annotation": payload.get("annotation", {}),
        "caveats": [
            "池化口径：候选池 = 三档 top-20 ∪ 真值事件，池外一律视为不相关（无法判断 ≠ 不相关）",
            "相关性由 LLM 分级，人工仅抽检（见 annotation.human_review）",
            f"评测集冻结于 {payload.get('created_at', '')[:10]}，语料窗口随日增前移，重建请另起版本号",
        ],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    out.with_suffix(".md").write_text(_render_markdown(summary), encoding="utf-8")
    print(_render_markdown(summary))
    print(f"\n✅ 报告写入 {out.with_suffix('.json')} / {out.with_suffix('.md')}")

    ndcg = target.mean.get("ndcg@10", 0.0)
    beats = all(
        ndcg > other.mean.get("ndcg@10", 0.0) for other in scores if other.mode != MODE_RERANK
    )
    ok = ndcg >= 0.5 and beats
    print(f"验收（NDCG@10 ≥ 0.5 且优于另两档）：{'✅ 通过' if ok else '❌ 未通过'}")
    return 0 if ok else 1


def _judged(item: dict) -> JudgedQuery:
    return JudgedQuery(
        id=item["id"], category=item["category"], query=item["query"], judgments=item["judgments"]
    )


def _render_markdown(summary: dict) -> str:
    lines = ["# M3 检索评测报告", ""]
    lines.append(f"样本 {summary['queries']} 条｜语料窗口 {summary.get('corpus_window')}")
    lines.append("")
    lines.append("## 三档消融")
    lines.append("")
    lines.append("| 档位 | NDCG@10 | MRR | Recall@20 | 延迟均值 | 延迟 p95 |")
    lines.append("|---|---|---|---|---|---|")
    for mode, mean in summary["by_mode"].items():
        latency = summary["latency_seconds"].get(mode) or {}
        mean_text = f"{latency['mean']:.2f}s" if latency else "—"
        p95_text = f"{latency['p95']:.2f}s" if latency else "—"
        lines.append(
            f"| {mode} | {mean.get('ndcg@10', 0):.4f} | {mean.get('mrr', 0):.4f} | "
            f"{mean.get('recall@20', 0):.4f} | {mean_text} | {p95_text} |"
        )
    lines.append("")
    lines.append("## 逐档增益")
    lines.append("")
    for name, diff in summary["ablation"].items():
        text = "、".join(f"{k} {v:+.4f}" for k, v in diff.items())
        lines.append(f"- {name}：{text}")
    lines.append("")
    lines.append("## 分类别（NDCG@10）")
    lines.append("")
    categories = sorted({c for per_mode in summary["by_category"].values() for c in per_mode})
    lines.append("| 类别 | " + " | ".join(summary["by_category"]) + " |")
    lines.append("|---" * (len(summary["by_category"]) + 1) + "|")
    for category in categories:
        cells = [
            f"{summary['by_category'][mode].get(category, {}).get('ndcg@10', 0):.4f}"
            for mode in summary["by_category"]
        ]
        lines.append(f"| {category} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("## 口径与局限")
    lines.append("")
    for caveat in summary["caveats"]:
        lines.append(f"- {caveat}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M3c 评测集构建与报告")
    sub = parser.add_subparsers(dest="cmd", required=True)
    #: LLM 调用是网络等待型的，串行纯属浪费墙钟（实测 200 条串行要约 1 小时）
    concurrency = {"default": 6, "type": int, "help": "LLM 并发路数"}

    p_gen = sub.add_parser("generate", help="抽样真值事件并让 LLM 写 query")
    p_gen.add_argument("--out", default=str(DEFAULT_PATH))
    p_gen.add_argument("--data-dir", default=str(Path(__file__).resolve().parents[2] / "data"))
    p_gen.add_argument("--limit", type=int, default=0, help="只生成前 N 条（冒烟用，0=全量）")
    p_gen.add_argument("--concurrency", **concurrency)
    p_gen.set_defaults(func=cmd_generate)

    p_pool = sub.add_parser("pool", help="三档各取 top-20 建候选池")
    p_pool.add_argument("--path", default=str(DEFAULT_PATH))
    p_pool.set_defaults(func=cmd_pool)

    p_grade = sub.add_parser("grade", help="LLM 分级标注")
    p_grade.add_argument("--path", default=str(DEFAULT_PATH))
    p_grade.add_argument("--concurrency", **concurrency)
    p_grade.set_defaults(func=cmd_grade)

    p_report = sub.add_parser("report", help="出消融报告")
    p_report.add_argument("--path", default=str(DEFAULT_PATH))
    p_report.add_argument("--out", default="../logs/m3/rag_eval")
    p_report.add_argument(
        "--latency-samples", type=int, default=20, help="每档测多少条 query 的延迟"
    )
    p_report.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RagNotReady as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
