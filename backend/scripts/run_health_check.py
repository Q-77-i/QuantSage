"""数据体检（M2c）——SPEC §3 M2c 的验收脚本。

一次跑完行情、事件、日历、遗留物四组判据，外加退市覆盖核实（生存者偏差）。逻辑全在
`app/data/data_health.py` 与 `app/data/delisting.py`，本脚本只负责参数与打印。

判据分级：`error` = 不变量被破坏、`warn` = 需要人看一眼、`info` = 记录与基线。
**有 error 退出码 1**（可直接挂定时告警）；`--strict` 时 warn 也计入失败。

用法：
    cd backend && uv run python scripts/run_health_check.py
    cd backend && uv run python scripts/run_health_check.py --strict --json ../logs/m2c/health.json
    cd backend && uv run python scripts/run_health_check.py --only B5,E3
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import REPO_ROOT  # noqa: E402
from app.data import data_health as health  # noqa: E402
from app.data import delisting  # noqa: E402

GROUPS = (
    ("B", "行情"),
    ("E", "事件"),
    ("X", "日历与遗留物"),
    ("G", "挂账（只报不修）"),
)
MARKERS = {"error": "✗", "warn": "⚠", "info": "✔"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--data-dir", type=Path, default=None, help="数据目录（默认 <repo>/data）")
    parser.add_argument("--json", type=Path, default=None, help="把机读副本写到这个路径")
    parser.add_argument("--strict", action="store_true", help="warn 也计入失败")
    parser.add_argument("--only", default=None, help="只跑指定检查，逗号分隔（如 B5,E3）")
    return parser.parse_args(argv)


def _print_checks(checks: list[health.Check]) -> None:
    for prefix, label in GROUPS:
        group = [check for check in checks if check.id.startswith(prefix)]
        if not group:
            continue
        print(f"\n{label}")
        for check in group:
            print(f"  {MARKERS[check.level]} {check.id} {check.title}")
            if check.detail:
                print(f"      {check.detail}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    base = Path(args.data_dir) if args.data_dir is not None else REPO_ROOT / "data"
    only = {item.strip().upper() for item in args.only.split(",")} if args.only else None

    checks = health.run_all(data_dir=base, only=only)
    print(f"数据质量体检 · {base}")

    report = None
    # 数据没就绪（一条 DATA error）时不再跑退市核实——它要的是同一份数据
    if not any(check.id == "DATA" for check in checks):
        try:
            report = delisting.run(base)
        except Exception as exc:  # 核实失败不该吞掉体检本身
            checks.append(
                health.Check(
                    id="U0",
                    level="error",
                    title="退市覆盖核实失败",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )

    _print_checks(checks)
    if report is not None:
        print("\n退市覆盖核实")
        for line in report.summary_lines:
            print(f"  · {line}")
        print(f"      {report.method_note}")

    code = health.exit_code(checks, strict=args.strict)
    print(f"\n{health.summarize(checks)}；退出码 {code}" + ("（--strict）" if args.strict else ""))

    if args.json:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "data_dir": str(base),
            "strict": args.strict,
            "summary": {
                level: sum(1 for c in checks if c.level == level)
                for level in ("error", "warn", "info")
            },
            "exit_code": code,
            "checks": [asdict(check) for check in checks],
            "delisting": asdict(report) if report is not None else None,
        }
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print(f"机读副本：{args.json}")

    return code


if __name__ == "__main__":
    raise SystemExit(main())
