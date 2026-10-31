#!/usr/bin/env python3
"""稳定跑法：集成套件按文件逐进程执行。

为什么不是一条 `pytest -m integration`：单进程跑到约 20 次 app lifespan 后，会在
`TestClient(app)` 的启动/关闭处卡在 Python 锁上（真实 MCP / Langfuse 会话与 worker
线程累积，详见 `docs/private/audit_20261030/INTEGRATION-TEST-ISOLATION-PLAN.md`）。
按文件起独立进程能把这类累积状态关在进程内；每个文件仍是正常的 `pytest -m integration`，
断言语义不变。

用法：
    cd backend
    uv run python scripts/run_integration.py
    uv run python scripts/run_integration.py tests/integration/test_metrics_e7e.py
    uv run python scripts/run_integration.py --timeout 300  # 单文件超时秒数

覆盖：`tests/integration/test_*.py` 全部文件 + 三个根目录里带 integration 标记的文件
（`test_deep_graph.py` / `test_optimize_api.py` / `test_research_api.py`），合计 155 项。
"""

from __future__ import annotations

import argparse
import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
INTEGRATION_DIR = BACKEND / "tests" / "integration"
ROOT_FILES = (
    BACKEND / "tests" / "test_deep_graph.py",
    BACKEND / "tests" / "test_optimize_api.py",
    BACKEND / "tests" / "test_research_api.py",
)


def discover() -> list[Path]:
    return [*sorted(INTEGRATION_DIR.glob("test_*.py")), *ROOT_FILES]


def resolve(path: Path) -> Path:
    """绝对路径原样；相对路径先按当前目录、再按 `backend/` 解析。"""
    if path.is_absolute():
        return path
    for base in (Path.cwd(), BACKEND):
        candidate = base / path
        if candidate.is_file():
            return candidate.resolve()
    return (BACKEND / path).resolve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="只跑这些文件（默认全部）")
    parser.add_argument(
        "--timeout",
        type=float,
        default=900.0,
        help="单个文件的超时秒数（默认 900；超时按失败计）",
    )
    args = parser.parse_args(argv)

    files = [resolve(path) for path in args.paths] or discover()
    missing = [path for path in files if not path.is_file()]
    if missing:
        for path in missing:
            print(f"✗ 文件不存在：{path}", file=sys.stderr)
        return 2

    failures: list[tuple[Path, int]] = []
    started_all = time.monotonic()
    for index, path in enumerate(files, start=1):
        try:
            label = path.relative_to(BACKEND)
        except ValueError:
            label = path
        print(f"\n=== [{index}/{len(files)}] {label} ===", flush=True)
        cmd = [sys.executable, "-m", "pytest", "-q", "-m", "integration", str(path)]
        started = time.monotonic()
        proc = subprocess.Popen(cmd, cwd=BACKEND, start_new_session=True)
        try:
            code = proc.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            print(
                f"✗ 超时（>{args.timeout:.0f}s），终止 pytest 及其 MCP 子进程组",
                file=sys.stderr,
            )
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait()
            code = 124
        elapsed = time.monotonic() - started
        print(f"exit={code}  ({elapsed:.1f}s)", flush=True)
        if code != 0:
            failures.append((path, code))

    total = time.monotonic() - started_all
    print(f"\n结果：{len(files) - len(failures)}/{len(files)} 个文件通过，总耗时 {total:.1f}s")
    if failures:
        print("失败文件：", file=sys.stderr)
        for path, code in failures:
            print(f"  - {path}（exit {code}）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
