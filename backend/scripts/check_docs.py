"""对外文档的机检：链接 / 环境变量 / 脚本引用。

**只做三件**——文档里写的相对链接、环境变量名、`scripts/*.py` 引用是否真的存在。
别的都不查：CLI 参数、API 端点、界面文案都会逐版本漂移，查了只会变成噪声源；
界面文案的漂移由人工走查兜（见 docs/private/Pn-n/P2-Mn/P2-M9.md 的 S6）。

用法：
    cd backend && uv run python scripts/check_docs.py [--json out.json]

退出码：0 全部通过 / 1 有失败项。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"

#: 检查哪几份文档。**新写对外文档时加进来**——漏掉一份等于没检查。
DOCS = (
    REPO / "README.md",
    REPO / "docs" / "tutorials" / "getting-started.md",
    REPO / "docs" / "tutorials" / "faq.md",
    REPO / "docs" / "tutorials" / "ma_cross.md",
    REPO / "docs" / "tutorials" / "event_driven.md",
)

#: 文档里出现这些**前缀**的大写标识符不算环境变量（框架名、产物名等）。
NOT_ENV_PREFIXES = ("HTTP", "UTF", "JSON", "CPU", "API", "SSE", "PDF", "URL", "SQL", "PIT", "AST")

#: 这些名字是文档里解释性提到的**系统 / 语言常量**，不来自 .env.example（已知豁免）。
#: 第一次跑逮到的第一例就是 `RLIMIT_CPU`——它是 Python `resource` 模块的常量。
ENV_ALLOWLIST: frozenset[str] = frozenset(
    {"PATH", "PYTHONSAFEPATH", "RLIMIT_CPU", "RU_MAXRSS", "MPS"}
)

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
ENV_RE = re.compile(r"`([A-Z][A-Z0-9_]{3,})`")
SCRIPT_RE = re.compile(r"scripts/([a-z_0-9]+\.py)")


def check_links() -> list[str]:
    """文档里的相对链接是否都存在（锚点、外链、mailto 一律跳过）。"""
    failures: list[str] = []
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        for raw in LINK_RE.findall(text):
            target = raw.split("#", 1)[0].strip()
            if not target or "://" in target or target.startswith(("mailto:", "#")):
                continue
            resolved = (doc.parent / target).resolve()
            if not resolved.exists():
                failures.append(f"{doc.relative_to(REPO)}: 链接不存在 → {raw}")
    return failures


def check_env_vars() -> list[str]:
    """文档里 `反引号包起来的大写标识符` 是否都在 .env.example 里。"""
    env_example = (REPO / ".env.example").read_text(encoding="utf-8")
    declared = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]{3,})=", env_example, re.MULTILINE))
    failures: list[str] = []
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        for name in sorted(set(ENV_RE.findall(text))):
            if name in declared or name in ENV_ALLOWLIST:
                continue
            if "_" not in name and not name.startswith(NOT_ENV_PREFIXES):
                # 单个词的大写（如 PASSWORD）多半不是环境变量名，别误报
                continue
            if name.startswith(NOT_ENV_PREFIXES):
                continue
            failures.append(f"{doc.relative_to(REPO)}: 环境变量未在 .env.example 里 → {name}")
    return failures


def check_scripts() -> list[str]:
    """文档里提到的 `scripts/xxx.py` 是否真的存在。"""
    failures: list[str] = []
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        for name in sorted(set(SCRIPT_RE.findall(text))):
            if not (BACKEND / "scripts" / name).exists():
                failures.append(f"{doc.relative_to(REPO)}: 脚本不存在 → scripts/{name}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="对外文档机检（链接 / 环境变量 / 脚本）")
    parser.add_argument("--json", type=Path, help="把结果写成 JSON")
    args = parser.parse_args()

    checks = {
        "links": check_links(),
        "env_vars": check_env_vars(),
        "scripts": check_scripts(),
    }
    total = sum(len(items) for items in checks.values())

    for name, failures in checks.items():
        status = "✓" if not failures else "✗"
        print(f"{status} {name}：{len(failures)} 个问题")
        for item in failures:
            print(f"    {item}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps({"checks": checks, "total": total}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    print(f"\n合计 {total} 个问题（检查了 {len(DOCS)} 份文档）")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
