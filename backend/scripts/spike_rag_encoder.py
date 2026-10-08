"""M3 spike：在真实语料上量 BGE-M3 的两条运行时路线（FlagEmbedding/torch vs fastembed/ONNX）。

**这是一次性测量工具，不是产品代码**（结论回填 SPEC §4 与 `docs/private/Pn-n/P2-Mn/P2-M3.md`）。
写成脚本而不是临时命令，只为让「同一批样本、同一段文本构造」可复现——两组数字若不是在
同一输入上量的，比较就没有意义。

三条口径：
* 样本按**真实类型分布 + 长度分层**抽，不用随机 200 条——随机抽会把长尾样本漏掉，
  外推总耗时会系统性偏乐观；
* 文本构造**照抄生产口径**（元数据 header + 标题 + 摘要，空字段不写标签），
  否则量的是另一套输入；
* 吞吐换算用 **字符数**而不是条数——本语料长短差 70 倍，按条数外推误差极大。

用法：
    python scripts/spike_rag_encoder.py sample --out /tmp/rag_sample.json
    python scripts/spike_rag_encoder.py run --backend flagembedding --device cpu --sample /tmp/rag_sample.json
    python scripts/spike_rag_encoder.py run --backend fastembed --sample /tmp/rag_sample.json
    python scripts/spike_rag_encoder.py rerank --backend flag --sample /tmp/rag_sample.json
"""

from __future__ import annotations

import argparse
import json
import resource
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"

#: 抽样按真实类型分布收口到 200 条（news 占 67.7%，按比例取整后对齐）
SAMPLE_PLAN: dict[str, int] = {
    "news": 100,
    "announcement": 60,
    "policy": 20,
    "person": 10,
    "research": 10,
}

#: 每个类型内按长度分三档（短/中/长），保证长尾样本一定被量到
LENGTH_BUCKETS = 3

RERANK_QUERIES = (
    "央行降准对银行股的影响",
    "半导体板块的业绩预告",
    "新能源车企的定增公告",
    "某公司股东减持计划",
    "9月15日的政策新闻",
)


def build_text(row: dict) -> str:
    """生产口径的嵌入文本：元数据 header + 标题 + 摘要，空字段不写标签。"""
    parts: list[str] = []
    if row.get("title"):
        parts.append(f"【标题】{row['title']}")
    head: list[str] = []
    if row.get("event_type"):
        head.append(f"【类型】{row['event_type']}")
    if row.get("event_time"):
        head.append(f"【时间】{str(row['event_time'])[:10]}")
    if row.get("direction_norm"):
        head.append(f"【方向】{row['direction_norm']}")
    if row.get("importance_score") is not None:
        head.append(f"【重要度】{row['importance_score']:.1f}")
    if row.get("industries"):
        head.append(f"【行业】{'/'.join(row['industries'])}")
    if row.get("symbols"):
        head.append(f"【标的】{'/'.join(row['symbols'])}")
    if head:
        parts.append(" ".join(head))
    if row.get("summary"):
        parts.append(row["summary"])
    return "\n".join(parts)


def cmd_sample(args: argparse.Namespace) -> int:
    import duckdb

    files = sorted((DATA_DIR / "events").glob("cn-events_*.parquet"))
    if not files:
        print(f"语料不存在：{DATA_DIR / 'events'}", file=sys.stderr)
        return 1
    con = duckdb.connect()
    rows = con.execute(
        f"""
        SELECT event_id, event_type, title, summary, event_time, available_at,
               direction_norm, importance_score, industries, symbols,
               length(coalesce(title, '') || coalesce(summary, '')) AS text_len
        FROM read_parquet({[str(f) for f in files]!r})
        WHERE event_type IN ({", ".join(repr(t) for t in SAMPLE_PLAN)})
        """
    ).fetch_arrow_table()
    all_rows = rows.to_pylist()  # pyarrow 给的是 dict 列表，别再 zip 一次列名（会拿键当值）
    total_chars = sum(len(build_text(r)) for r in all_rows)

    by_type: dict[str, list[dict]] = {}
    for row in all_rows:
        by_type.setdefault(row["event_type"], []).append(row)

    picked: list[dict] = []
    for event_type, want in SAMPLE_PLAN.items():
        pool = sorted(by_type.get(event_type, []), key=lambda r: (r["text_len"], r["event_id"]))
        if not pool:
            print(f"⚠️  {event_type} 无样本", file=sys.stderr)
            continue
        per_bucket = max(1, want // LENGTH_BUCKETS)
        step = max(1, len(pool) // (per_bucket * LENGTH_BUCKETS))
        # 等距取样：既覆盖长度全谱，又不受随机种子影响（可复现）
        chosen = [pool[i] for i in range(0, len(pool), step)][:want]
        picked.extend(chosen)
        print(f"{event_type:<13} 池 {len(pool):>7}  取 {len(chosen):>3}")

    lens = [r["text_len"] for r in picked]
    payload = {
        "note": "M3 spike 样本：真实语料 + 生产文本构造；按类型与长度分层等距抽样",
        "corpus_rows": len(all_rows),
        "corpus_chars": total_chars,
        "sample": [
            {
                "event_id": r["event_id"],
                "event_type": r["event_type"],
                "text": build_text(r),
                "text_len": r["text_len"],
            }
            for r in picked
        ],
    }
    out = Path(args.out)
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(
        f"\n样本 {len(picked)} 条 | 字符 {sum(lens)}（均值 {statistics.mean(lens):.0f}、"
        f"中位 {statistics.median(lens):.0f}、最长 {max(lens)}）"
    )
    print(f"全语料 {len(all_rows)} 行 / {total_chars} 字符 → 抽样比 {sum(lens) / total_chars:.4%}")
    print(f"写入 {out}")
    return 0


def _rss_mb() -> float:
    """进程峰值内存（macOS 上 ru_maxrss 单位是字节）。"""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024


def _load_sample(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def cmd_run(args: argparse.Namespace) -> int:
    payload = _load_sample(args.sample)
    texts = [item["text"] for item in payload["sample"]]
    chars = sum(len(t) for t in texts)
    print(f"样本 {len(texts)} 条 / {chars} 字符｜后端 {args.backend}")

    t0 = time.perf_counter()
    if args.backend == "flagembedding":
        from FlagEmbedding import BGEM3FlagModel

        model = BGEM3FlagModel(
            "BAAI/bge-m3", use_fp16=args.device == "mps", device=args.device
        )
        load_s = time.perf_counter() - t0
        print(f"加载（含下载）{load_s:.1f}s｜device={args.device} 峰值内存 {_rss_mb():.0f}MB")

        t1 = time.perf_counter()
        out = model.encode(
            texts,
            batch_size=args.batch_size,
            max_length=args.max_length,
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=False,
        )
        encode_s = time.perf_counter() - t1
        dense = out["dense_vecs"]
        sparse = out["lexical_weights"]
        nnz = [len(s) for s in sparse]
        dim = len(dense[0])
    elif args.backend == "fastembed":
        from fastembed import SparseTextEmbedding, TextEmbedding

        dense_model = TextEmbedding(model_name="BAAI/bge-m3", max_length=args.max_length)
        load_s = time.perf_counter() - t0
        print(f"加载（含下载）{load_s:.1f}s｜峰值内存 {_rss_mb():.0f}MB")

        t1 = time.perf_counter()
        dense = list(dense_model.embed(texts, batch_size=args.batch_size))
        encode_s = time.perf_counter() - t1
        dim = len(dense[0])
        nnz = None
        try:
            t2 = time.perf_counter()
            sparse_model = SparseTextEmbedding(model_name="BAAI/bge-m3")
            sparse = list(sparse_model.embed(texts, batch_size=args.batch_size))
            sparse_s = time.perf_counter() - t2
            nnz = [len(s.indices) for s in sparse]
            print(f"✅ sparse 原生支持：{sparse_s:.1f}s")
        except Exception as exc:  # noqa: BLE001 —— spike 要把「不支持」如实报出来
            print(f"❌ sparse 不可用（{type(exc).__name__}: {exc}）")
    else:
        print(f"未知后端 {args.backend}", file=sys.stderr)
        return 2

    rate = len(texts) / encode_s
    char_rate = chars / encode_s
    print(
        f"\n编码 {len(texts)} 条 / {encode_s:.1f}s → **{rate:.1f} 条/s、{char_rate:.0f} 字符/s**"
        f"｜向量维度 {dim}｜峰值内存 {_rss_mb():.0f}MB"
    )
    if nnz:
        print(f"sparse 非零项：均值 {statistics.mean(nnz):.0f}、最大 {max(nnz)}")
    est_s = payload["corpus_chars"] / char_rate
    print(
        f"外推全语料 {payload['corpus_rows']} 行 / {payload['corpus_chars']} 字符 → "
        f"**{est_s / 3600:.1f} 小时**"
    )
    return 0


def cmd_rerank(args: argparse.Namespace) -> int:
    payload = _load_sample(args.sample)
    docs = [item["text"] for item in payload["sample"][: args.candidates]]
    print(f"重排 {len(RERANK_QUERIES)} 条 query × {len(docs)} 篇候选")

    if args.backend == "flag":
        from FlagEmbedding import FlagReranker

        t0 = time.perf_counter()
        reranker = FlagReranker("BAAI/bge-reranker-v2-m3", use_fp16=False)
        print(f"加载 {time.perf_counter() - t0:.1f}s｜峰值内存 {_rss_mb():.0f}MB")
        pairs = [[q, d] for q in RERANK_QUERIES for d in docs]
        t1 = time.perf_counter()
        scores = reranker.compute_score([[q, d] for q, d in pairs], batch_size=args.batch_size)
        total = time.perf_counter() - t1
        n = len(scores)
    elif args.backend == "fastembed":
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        names = [m["model"] for m in TextCrossEncoder.list_supported_models()]
        print(f"fastembed 支持的 reranker：{names}")
        if args.model not in names:
            print(f"❌ {args.model} 不在支持列表")
            return 1
        t0 = time.perf_counter()
        reranker = TextCrossEncoder(model_name=args.model)
        print(f"加载 {time.perf_counter() - t0:.1f}s｜峰值内存 {_rss_mb():.0f}MB")
        t1 = time.perf_counter()
        n = 0
        for q in RERANK_QUERIES:
            scores = list(reranker.rerank(q, docs))
            n += len(scores)
        total = time.perf_counter() - t1
    else:
        print(f"未知后端 {args.backend}", file=sys.stderr)
        return 2

    print(
        f"\n重排 {n} 对 / {total:.2f}s → **{total / n * 1000:.0f} ms/对**"
        f"｜70 对（=50 候选 + 余量）约 {total / n * 70:.2f}s｜峰值内存 {_rss_mb():.0f}MB"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M3 spike：BGE-M3 运行时对比")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sample = sub.add_parser("sample", help="从真实语料抽 200 条样本")
    p_sample.add_argument("--out", default="/tmp/rag_sample.json")
    p_sample.set_defaults(func=cmd_sample)

    p_run = sub.add_parser("run", help="量嵌入吞吐与内存")
    p_run.add_argument("--backend", choices=["flagembedding", "fastembed"], required=True)
    p_run.add_argument("--device", default="cpu", choices=["cpu", "mps"])
    p_run.add_argument("--sample", default="/tmp/rag_sample.json")
    p_run.add_argument("--batch-size", type=int, default=8)
    p_run.add_argument("--max-length", type=int, default=512)
    p_run.set_defaults(func=cmd_run)

    p_rerank = sub.add_parser("rerank", help="量重排延迟")
    p_rerank.add_argument("--backend", choices=["flag", "fastembed"], required=True)
    p_rerank.add_argument("--model", default="BAAI/bge-reranker-v2-m3")
    p_rerank.add_argument("--sample", default="/tmp/rag_sample.json")
    p_rerank.add_argument("--candidates", type=int, default=50)
    p_rerank.add_argument("--batch-size", type=int, default=8)
    p_rerank.set_defaults(func=cmd_rerank)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
