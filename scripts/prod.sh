#!/usr/bin/env bash
# QuantSage 生产形态入口（P4-S4）
#
# 用法（未加执行位时用 bash scripts/prod.sh）:
#   bash scripts/prod.sh start            # 基础设施 → checkpointer → RAG → API → 前端（build 缺失时先 build）
#   bash scripts/prod.sh stop             # 优雅停应用：API 最多等 20s（drain 15s + 收尾），不动基础设施
#   bash scripts/prod.sh restart
#   bash scripts/prod.sh status           # compose / 端口 / 健康
#   bash scripts/prod.sh health           # 只做健康检查；任一依赖失败退出非 0
#   bash scripts/prod.sh logs [backend|frontend|rag]
#   bash scripts/prod.sh build            # 显式重建前端（会覆盖 .next；3001 被占时拒绝）
#
# 与 dev.sh 的差别只有三处：前端 pnpm build + pnpm start、uvicorn 15s 优雅停机、
# 日志 copytruncate 轮转。其余启停/PID/健康检查复用 dev.sh，避免两份实现漂移。
# 不自启、不守护：进程退出后由人重跑；需要保活时再上 launchd（S4 不做）。
#
# 约定:
# - 日志与 PID 在 logs/prod/（dev 是 logs/dev/），端口相同，不能同时起
# - 代码改动后要显式 bash scripts/prod.sh build；start 只在 .next/BUILD_ID 缺失时自动构建
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say() { printf '\033[1m%s\033[0m\n' "$*"; }
die() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

if [[ "${1:-}" == "help" || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  sed -n '2,19p' "$0"
  exit 0
fi

if [[ "${1:-}" == "build" ]]; then
  if command -v lsof >/dev/null 2>&1 \
    && lsof -nP -iTCP:3001 -sTCP:LISTEN >/dev/null 2>&1; then
    die "3001 被占用（dev/prod 在跑？）：pnpm build 会覆盖 .next，先 bash scripts/dev.sh stop"
  fi
  [[ -d "$ROOT/frontend/node_modules" ]] \
    || die "缺少 frontend/node_modules：先 cd frontend && pnpm install"
  say "→ pnpm build"
  (cd "$ROOT/frontend" && pnpm build)
  exit 0
fi

export QS_MODE=prod
exec "$ROOT/scripts/dev.sh" "$@"
