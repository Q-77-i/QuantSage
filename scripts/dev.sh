#!/usr/bin/env bash
# QuantSage 一键开发起停脚本（2026-10-18）
#
# 用法:
#   ./scripts/dev.sh                  # 启动基础设施 + 后端 + 前端（默认 start）
#   ./scripts/dev.sh start
#   ./scripts/dev.sh stop [--force]   # 只停应用；--force 清理手动起的进程
#   ./scripts/dev.sh restart [--force] # 停应用后重新起；手动起的进程用 --force 清理
#   ./scripts/dev.sh status           # 基础设施 / 应用端口 / 健康检查
#   ./scripts/dev.sh logs [backend|frontend]
#   ./scripts/dev.sh down             # 停应用 + docker compose down
#
# 约定:
# - 后端用 backend/.venv/bin/python -m uvicorn（避免 uv run 包装层，PID 可控）
# - 前端用 pnpm dev；next dev 会改名为 next-server，stop 按 PID 树递归杀
# - 日志 logs/dev/{backend,frontend}.log；PID logs/dev/{backend,frontend}.pid
# - 不自动安装依赖；缺 .env / .venv / node_modules 时给出明确命令
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT/logs/dev"
BACKEND_PID="$LOG_DIR/backend.pid"
FRONTEND_PID="$LOG_DIR/frontend.pid"
BACKEND_LOG="$LOG_DIR/backend.log"
FRONTEND_LOG="$LOG_DIR/frontend.log"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-3001}"

say() { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }
die() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

port_busy() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
  elif command -v nc >/dev/null 2>&1; then
    nc -z 127.0.0.1 "$port" >/dev/null 2>&1
  else
    die "需要 lsof 或 nc 来探测端口"
  fi
}

pid_alive() { [[ -n "${1:-}" ]] && kill -0 "$1" 2>/dev/null; }

read_pid() { [[ -f "$1" ]] && cat "$1" 2>/dev/null || true; }

force_flag() { [[ "${1:-}" == "--force" || "${1:-}" == "true" ]] && echo true || echo false; }

wait_http() {
  local url="$1" tries="${2:-60}" i
  for ((i = 1; i <= tries; i++)); do
    if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  return 1
}

check_prereqs() {
  command -v docker >/dev/null 2>&1 || die "未找到 docker"
  docker compose version >/dev/null 2>&1 || die "未找到 docker compose（需要 v2）"
  command -v curl >/dev/null 2>&1 || die "未找到 curl"
  [[ -f "$ROOT/.env" ]] || die "缺少 .env：先 cp .env.example .env 并填好密钥"
  [[ -x "$ROOT/backend/.venv/bin/python" ]] || die "缺少 backend/.venv：先 cd backend && uv sync"
  [[ -d "$ROOT/frontend/node_modules" ]] || die "缺少 frontend/node_modules：先 cd frontend && pnpm install"
}

start_infra() {
  say "→ 启动基础设施（postgres / redis / qdrant；compose 配置有变时会重建容器，命名卷数据保留）"
  (cd "$ROOT" && docker compose up -d --wait)
}

init_checkpoint() {
  say "→ 初始化 checkpointer 表（幂等）"
  (cd "$ROOT/backend" && .venv/bin/python scripts/init_checkpoint_db.py)
}

start_backend() {
  local pid
  pid="$(read_pid "$BACKEND_PID")"
  if pid_alive "$pid"; then
    say "后端已在运行（PID $pid）"
    return 0
  fi
  if port_busy "$BACKEND_PORT"; then
    warn "端口 $BACKEND_PORT 已被占用（不是本脚本启动的？）；跳过后端启动"
    return 0
  fi
  say "→ 启动后端 :$BACKEND_PORT"
  (
    cd "$ROOT/backend"
    nohup .venv/bin/python -m uvicorn app.main:app --port "$BACKEND_PORT" \
      >>"$BACKEND_LOG" 2>&1 &
    echo $! >"$BACKEND_PID"
  )
  if ! wait_http "http://127.0.0.1:$BACKEND_PORT/health" 60; then
    warn "后端未在 60s 内就绪；日志尾部："
    tail -n 30 "$BACKEND_LOG" >&2 || true
    return 1
  fi
  say "后端就绪：http://127.0.0.1:$BACKEND_PORT"
}

start_frontend() {
  local pid
  pid="$(read_pid "$FRONTEND_PID")"
  if pid_alive "$pid"; then
    say "前端已在运行（PID $pid）"
    return 0
  fi
  if port_busy "$FRONTEND_PORT"; then
    warn "端口 $FRONTEND_PORT 已被占用（不是本脚本启动的？）；跳过前端启动"
    return 0
  fi
  say "→ 启动前端 :$FRONTEND_PORT"
  (
    cd "$ROOT/frontend"
    nohup pnpm dev >>"$FRONTEND_LOG" 2>&1 &
    echo $! >"$FRONTEND_PID"
  )
  if ! wait_http "http://127.0.0.1:$FRONTEND_PORT/login" 120; then
    warn "前端未在 120s 内就绪；日志尾部："
    tail -n 30 "$FRONTEND_LOG" >&2 || true
    warn "若日志出现 vendor-chunks / ENOENT：停 dev 后把 frontend/.next 移开再试"
    return 1
  fi
  say "前端就绪：http://127.0.0.1:$FRONTEND_PORT"
}

# 递归杀进程树（先子后父）：pnpm -> sh -> node next -> next-server
kill_tree() {
  local pid="$1" sig="${2:-TERM}" child
  for child in $(pgrep -P "$pid" 2>/dev/null || true); do
    kill_tree "$child" "$sig"
  done
  kill -"$sig" "$pid" 2>/dev/null || true
}

stop_one() {
  local name="$1" pidfile="$2"
  local pid
  pid="$(read_pid "$pidfile")"
  if pid_alive "$pid"; then
    say "→ 停止$name（PID $pid）"
    kill_tree "$pid" TERM
    local i
    for i in {1..20}; do
      pid_alive "$pid" || break
      sleep 0.2
    done
    if pid_alive "$pid"; then
      warn "$name 未在 4s 内退出，SIGKILL"
      kill_tree "$pid" KILL
    fi
  fi
  rm -f "$pidfile"
}

kill_listeners() {
  # 只清理指定端口上的监听进程（及其子进程），不用 pkill 宽匹配误伤别的项目
  local port="$1" pids pid
  if ! command -v lsof >/dev/null 2>&1; then
    warn "没有 lsof，无法按端口清理；请手动停止端口 $port 上的进程"
    return 0
  fi
  pids="$(lsof -t -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -z "$pids" ]]; then
    warn "端口 $port 没有可识别的监听进程"
    return 0
  fi
  for pid in $pids; do
    say "→ 清理端口 $port 的监听进程（PID $pid）"
    kill_tree "$pid" TERM
  done
  sleep 1
  for pid in $pids; do
    if pid_alive "$pid"; then
      warn "PID $pid 未退出，SIGKILL"
      kill_tree "$pid" KILL
    fi
  done
}

stop_apps() {
  local force="${1:-false}"
  stop_one "后端" "$BACKEND_PID"
  stop_one "前端" "$FRONTEND_PID"
  if [[ "$force" == "true" ]]; then
    # PID 文件缺失时（手动起的进程）按端口清理；只碰本项目端口上的监听进程
    if port_busy "$BACKEND_PORT"; then
      warn "端口 $BACKEND_PORT 仍被占用，按监听进程清理"
      kill_listeners "$BACKEND_PORT"
    fi
    if port_busy "$FRONTEND_PORT"; then
      warn "端口 $FRONTEND_PORT 仍被占用，按监听进程清理"
      kill_listeners "$FRONTEND_PORT"
    fi
  elif port_busy "$BACKEND_PORT" || port_busy "$FRONTEND_PORT"; then
    warn "仍有端口被占用，但进程不是本脚本启动的（无 PID 文件）；需要时用 stop --force"
  fi
}

start_all() {
  check_prereqs
  mkdir -p "$LOG_DIR"
  start_infra
  init_checkpoint
  start_backend || exit 1
  start_frontend || exit 1
  say ""
  say "就绪："
  say "  前端  http://127.0.0.1:$FRONTEND_PORT"
  say "  后端  http://127.0.0.1:$BACKEND_PORT/docs"
  say "  日志  $LOG_DIR"
  say "停止  $0 stop"
}

status_all() {
  say "== 基础设施 =="
  (cd "$ROOT" && docker compose ps) || true
  say ""
  say "== 应用 =="
  local bpid fpid
  bpid="$(read_pid "$BACKEND_PID")"
  fpid="$(read_pid "$FRONTEND_PID")"
  if pid_alive "$bpid"; then
    say "后端：运行中（PID $bpid，端口 $BACKEND_PORT）"
  elif port_busy "$BACKEND_PORT"; then
    say "后端：端口 $BACKEND_PORT 被占用（非本脚本 PID 文件）"
  else
    say "后端：未运行"
  fi
  if pid_alive "$fpid"; then
    say "前端：运行中（PID $fpid，端口 $FRONTEND_PORT）"
  elif port_busy "$FRONTEND_PORT"; then
    say "前端：端口 $FRONTEND_PORT 被占用（非本脚本 PID 文件）"
  else
    say "前端：未运行"
  fi
  say ""
  say "== 健康 =="
  curl -fsS --max-time 2 "http://127.0.0.1:$BACKEND_PORT/health" 2>/dev/null || echo "后端 /health 不可达"
  echo
  curl -fsS --max-time 2 -o /dev/null -w '前端 /login HTTP %{http_code}\n' \
    "http://127.0.0.1:$FRONTEND_PORT/login" 2>/dev/null || echo "前端 /login 不可达"
}

case "${1:-start}" in
  start) start_all ;;
  stop) stop_apps "$(force_flag "${2:-}")" ;;
  restart) stop_apps "$(force_flag "${2:-}")"; start_all ;;
  status) status_all ;;
  logs)
    mkdir -p "$LOG_DIR"
    touch "$BACKEND_LOG" "$FRONTEND_LOG"
    case "${2:-all}" in
      backend) tail -n 100 -f "$BACKEND_LOG" ;;
      frontend) tail -n 100 -f "$FRONTEND_LOG" ;;
      *) tail -n 50 -f "$BACKEND_LOG" "$FRONTEND_LOG" ;;
    esac
    ;;
  down)
    stop_apps true
    say "→ docker compose down"
    (cd "$ROOT" && docker compose down)
    ;;
  help | -h | --help)
    sed -n '2,20p' "$0"
    ;;
  *)
    die "未知命令：$1（用 help 看用法）"
    ;;
esac
