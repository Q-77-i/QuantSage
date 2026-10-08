"""M4b 薄壳：对一份策略源码跑静态检查并打印 findings（本地排查用，**不执行代码**）。

    用法：
      cd backend && uv run python scripts/check_strategy.py --file my.py
      uv run python scripts/check_strategy.py --file my.py --json /tmp/findings.json

    退出码：0 = 没有 error（warning 不拦运行）｜ 1 = 有 error（提交回测前会被拒）｜ 3 = 文件问题

与 M4c 的 `POST /api/v1/strategies/check` 走**同一个** `check_source`——这里只是给它一个命令行
入口，好在编辑器之外复现用户的报错。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.strategy.static_check import check_source, format_findings, has_errors  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="对策略源码跑静态检查（只读 AST，不执行代码）")
    parser.add_argument("--file", required=True, type=Path, help="策略源码文件")
    parser.add_argument("--json", type=Path, default=None, help="把 findings 写成 JSON")
    args = parser.parse_args(argv)

    try:
        source = args.file.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"✗ 读不到策略文件：{exc}")
        return 3

    findings = check_source(source)
    print(f"策略：{args.file}")
    print(format_findings(findings))
    if args.json:
        args.json.write_text(
            json.dumps([finding.to_dict() for finding in findings], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"findings：{args.json}")
    return 1 if has_errors(findings) else 0


if __name__ == "__main__":
    sys.exit(main())
