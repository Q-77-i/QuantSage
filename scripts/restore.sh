#!/usr/bin/env bash
# QuantSage 恢复入口（P4-S6）
#
# 用法:
#   ./scripts/restore.sh --source YYYYMMDD [--target DIR]
#                        [--pg-db 新库名] [--pg-source quantsage]
#                        [--qdrant-collection 新集合名]
#                        [--data-target 临时目录] [--force [--yes] [--allow-live]]
#                        [--dry-run]
#
# 默认恢复到 scratch 目标，不覆盖 quantsage / cn_events_v1 / data/。
# 覆盖需 --force **再加确认**：TTY 输入 yes，非 TTY 用 --yes；
# 覆盖线上库/集合/仓库 data/ 还要 --allow-live。实现见 scripts/backup.py。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/backend/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "缺少 backend/.venv/bin/python；先按 README 安装后端依赖" >&2
  exit 2
fi
exec "$PY" "$ROOT/scripts/backup.py" restore "$@"
