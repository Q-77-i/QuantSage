#!/usr/bin/env bash
# QuantSage 备份入口（P4-S6）
#
# 用法:
#   ./scripts/backup.sh [--target DIR] [--keep-daily 7] [--keep-weekly 4]
#                       [--no-pg] [--no-qdrant] [--no-data] [--dry-run]
#
# 实现见 scripts/backup.py；默认落点 ~/QuantSageBackups。
# 外置盘：--target /Volumes/<disk>/QuantSageBackups（未挂载会明确失败）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/backend/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "缺少 backend/.venv/bin/python；先按 README 安装后端依赖" >&2
  exit 2
fi
exec "$PY" "$ROOT/scripts/backup.py" backup "$@"
