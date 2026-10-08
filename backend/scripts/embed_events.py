"""事件语料嵌入的薄壳：全量 / 增量 / 按日重建 / 覆盖对账。

逻辑全在 `app/rag/embed.py`，这里只做参数与输出。

```
# 增量（默认）：只嵌「缺失或日分区 sha 变化」的日子
python scripts/embed_events.py

# 对账：本地清单 vs Qdrant 实际点数（不写任何东西）
python scripts/embed_events.py --status

# 强制重嵌指定日子（改了嵌入文本或排查问题时用）
python scripts/embed_events.py --rebuild-day 2026-09-15

# 全量重建：**会删掉整个 collection**，需显式确认
python scripts/embed_events.py --full --yes
```

退出码：0 正常；1 有错（对账不干净、依赖不可用、嵌入失败）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rag import RagNotReady  # noqa: E402
from app.rag import collection as col  # noqa: E402
from app.rag import embed  # noqa: E402
from app.rag.encoder import configure_hf_env  # noqa: E402


def _print_status(report: embed.StatusReport, manifest_days: int) -> None:
    print(f"语料日分区 {manifest_days} 个｜对账干净的 {report.ready_days} 个")
    if report.missing:
        print(f"✗ 本地有、索引无（漏嵌）{len(report.missing)} 天：{_sample(report.missing)}")
    if report.stale:
        print(f"✗ 点数对不上 {len(report.stale)} 天：{_sample(report.stale)}")
    if report.extra:
        print(f"✗ 索引有、本地无（陈旧点）{len(report.extra)} 天：{_sample(report.extra)}")
    if report.clean:
        print("✅ 覆盖对账零差异")


def _sample(mapping: dict[str, int], limit: int = 6) -> str:
    items = sorted(mapping.items())[:limit]
    text = ", ".join(f"{day}({value:+d})" if value < 0 else f"{day}({value})" for day, value in items)
    return text + ("…" if len(mapping) > limit else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="事件语料嵌入（M3a）")
    parser.add_argument("--data-dir", default=None, help="数据目录（默认取配置）")
    parser.add_argument("--status", action="store_true", help="只做覆盖对账，不写数据")
    parser.add_argument("--full", action="store_true", help="全量重建（删 collection 后重嵌）")
    parser.add_argument("--yes", action="store_true", help="确认 --full 的破坏性操作")
    parser.add_argument("--rebuild-day", action="append", default=[], help="强制重嵌某日，可重复")
    # 日期区间：把全量嵌入拆成两个进程并行时用（本机实测单进程只吃 1.15 核 / 8 核）
    parser.add_argument("--days-from", default=None, help="只处理该日及之后（含），YYYY-MM-DD")
    parser.add_argument("--days-to", default=None, help="只处理该日及之前（含），YYYY-MM-DD")
    parser.add_argument("--json", default=None, help="把结果同时写成 JSON")
    parser.add_argument("--device", default=None, help="cpu / mps（覆盖配置）")
    args = parser.parse_args(argv)

    # 破坏性操作先拦：**在任何连接与模型加载之前**——误敲 --full 不该有任何副作用
    if args.full and not args.yes:
        print("✗ --full 会删除整个 collection 后重嵌，请加 --yes 确认", file=sys.stderr)
        return 1

    configure_hf_env()  # 必须在 import 模型库之前（关 Xet、钉住权重目录）
    data_dir = Path(args.data_dir) if args.data_dir else None

    try:
        client = col.get_client()
    except RagNotReady as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1

    manifest = embed.load_manifest(data_dir)

    if args.rebuild_day and (args.days_from or args.days_to):
        print("✗ --rebuild-day 与 --days-from/--days-to 不要混用（force 会作用于整个选择集）", file=sys.stderr)
        return 1
    only = list(args.rebuild_day) or None
    if args.days_from or args.days_to:
        only = [
            day
            for day in sorted(manifest)
            if (not args.days_from or day >= args.days_from)
            and (not args.days_to or day <= args.days_to)
        ]

    if args.status:
        report = embed.status(data_dir=data_dir, client=client)
        _print_status(report, len(manifest))
        payload = {
            "ready_days": report.ready_days,
            "missing": report.missing,
            "stale": report.stale,
            "extra": report.extra,
        }
        if args.json:
            Path(args.json).write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        return 0 if report.clean else 1

    if args.device:
        from app.core.config import get_settings

        get_settings().rag_device = args.device  # 本次进程内生效

    def progress(task: embed.DayTask, count: int) -> None:
        print(f"  {task.day}  {task.action}  {count} 点  （{task.reason}）", flush=True)

    print(f"collection={col.collection_name()}｜{'全量重建' if args.full else '增量同步'}")
    try:
        report = embed.sync(
            data_dir=data_dir,
            client=client,
            only=only,
            force=bool(args.rebuild_day),
            recreate=args.full,
            progress=progress,
        )
    except RagNotReady as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1

    print(
        f"\n嵌入 {len(report.embedded)} 天 / {report.points} 点"
        f"｜跳过 {len(report.skipped)} 天｜耗时 {report.seconds:.1f}s"
    )
    if not report.embedded:
        print("（全部日分区 sha 未变，无需重嵌）")

    status = embed.status(data_dir=data_dir, client=client)
    print()
    _print_status(status, len(manifest))
    payload = {
        "embedded_days": [t.day for t in report.embedded],
        "points": report.points,
        "skipped_days": len(report.skipped),
        "seconds": report.seconds,
        "status": {
            "ready_days": status.ready_days,
            "missing": status.missing,
            "stale": status.stale,
            "extra": status.extra,
        },
    }
    if args.json:
        Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    # 同步模式只对**自己接下的活**负责：全部嵌完即成功。
    # 「索引是否与语料完全对账」是 `--status` 的判据——那里退出码才有意义
    # （否则单日重嵌会因为「别的天还没嵌」而报失败，那是另一回事）。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
