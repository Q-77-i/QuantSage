#!/usr/bin/env bash
# QuantSage 一键开发起停脚本（2026-10-18）
#
# 用法:
#   ./scripts/dev.sh                  # 启动基础设施 + RAG + 后端 + 前端（默认 start）
#   ./scripts/dev.sh start
#   ./scripts/dev.sh stop [--force]   # 只停应用；--force 清理手动起的进程
#   ./scripts/dev.sh restart [--force] # 停应用后重新起；手动起的进程用 --force 清理
#   ./scripts/dev.sh status           # 基础设施 / 应用端口 / 健康检查
#   ./scripts/dev.sh health           # 只做健康检查；任一依赖失败退出非 0
#   ./scripts/dev.sh logs [backend|frontend|rag]
#   ./scripts/dev.sh monitoring start|stop|status  # Prometheus + Alertmanager + alert_hook（默认不启）
#   ./scripts/dev.sh down             # 停应用 + 全部 compose 服务（含 observability / monitoring profile）
#
# prod 形态（S4）：用 bash scripts/prod.sh；它设 QS_MODE=prod 后委派本脚本，差别只有
# 前端 build+start、uvicorn 15s 优雅停机、日志 copytruncate 轮转、prod 独立日志/PID 目录。
#
# RAG 服务（S3）：.env 里 RAG_SERVICE_URL 非空时随 start 一起起、随 stop 一起停；
# 为空则跳过（进程内回滚），不白占 ~2.3GB 内存。
#
# 约定:
# - 后端用 backend/.venv/bin/python -m uvicorn（避免 uv run 包装层，PID 可控）
# - 前端 dev 用 pnpm dev、prod 用 pnpm start；next dev 会改名为 next-server，
#   stop 按 PID 树递归杀
# - 日志 logs/$QS_MODE/{backend,frontend,rag}.log；PID 同目录
# - prod 日志轮转：copytruncate（20MB × 3 份，30min 检查）；dev 不轮转
# - 不自动安装依赖；缺 .env / .venv / node_modules 时给出明确命令
set -euo pipefail

# 变量紧邻中文/全角标点时一律写 ${var}：非 UTF-8 locale 下 Bash 会把多字节首字节
# 当成变量名的一部分（实测 `$pid）` 报 `pid�: unbound variable`）。

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# QS_MODE=prod 由 scripts/prod.sh 设置；直接跑本脚本时是 dev。
MODE="${QS_MODE:-dev}"

# 本地密钥文件默认 600：同机其他用户不能读 .env（P1 安全卫生）。
if [[ -f "$ROOT/.env" ]]; then
  chmod 600 "$ROOT/.env" 2>/dev/null || true
fi
# S5：API worker 数。shell 环境优先；没设则读 .env（app 也从 .env 读同名键，
# 两处必须是同一个值——否则 dev.sh 起 1 个而 app 以为自己有 2 个，限流降级分母会错）。
API_WORKERS="${API_WORKERS:-}"
if [[ -z "$API_WORKERS" && -f "$ROOT/.env" ]]; then
  # grep 找不到键会 exit 1；set -o pipefail 下必须吞掉，否则整个脚本在解析期退出。
  API_WORKERS="$(grep -E '^[[:space:]]*API_WORKERS=' "$ROOT/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d '[:space:]' || true)"
fi
API_WORKERS="${API_WORKERS:-1}"
export API_WORKERS
LOG_DIR="$ROOT/logs/$MODE"
BACKEND_PID="$LOG_DIR/backend.pid"
FRONTEND_PID="$LOG_DIR/frontend.pid"
RAG_PID="$LOG_DIR/rag.pid"
ROTATE_PID="$LOG_DIR/rotate.pid"
ALERT_PID="$LOG_DIR/alert_hook.pid"
BACKEND_LOG="$LOG_DIR/backend.log"
FRONTEND_LOG="$LOG_DIR/frontend.log"
RAG_LOG="$LOG_DIR/rag.log"
ALERT_LOG="$LOG_DIR/alert_hook.log"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-3001}"
RAG_PORT="${RAG_PORT:-8200}"
# prod 日志轮转：超过阈值就 copytruncate，保留 N 份；检查间隔（秒）。
LOG_MAX_BYTES="${PROD_LOG_MAX_BYTES:-20971520}"
LOG_KEEP="${PROD_LOG_KEEP:-3}"
LOG_CHECK_SECONDS="${PROD_LOG_CHECK_SECONDS:-1800}"

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

# .env 里 RAG_SERVICE_URL 非空（或 shell 已导出）才起 RAG 服务；否则进程内回滚，
# 不白占 ~2.3GB 模型内存。只 grep 键名，不读也不回显其他配置。
rag_enabled() {
  [[ -n "${RAG_SERVICE_URL:-}" ]] && return 0
  grep -Eq '^[[:space:]]*RAG_SERVICE_URL=.+' "$ROOT/.env" 2>/dev/null
}

# 按键名取环境变量：shell 环境优先，没有才读 .env。**只返回值给调用方**，
# 不打印——token 走这条路径进文件，回显就破了红线。
env_value() {
  local key="$1" value
  value="$(printenv "$key" 2>/dev/null || true)"
  if [[ -z "$value" && -f "$ROOT/.env" ]]; then
    value="$(grep -E "^[[:space:]]*${key}=" "$ROOT/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d '[:space:]' || true)"
  fi
  printf '%s' "$value"
}

# token 文件给容器只读挂载。644 而非 600：Prometheus 以 nobody 运行，
# 600 在 Docker Desktop 的绑定挂载下可能读不到；logs/ 已 gitignore。
write_token() {
  local key="$1" path="$2" value
  value="$(env_value "$key")"
  [[ -n "$value" ]] || die "monitoring 需要 ${key}：在 .env 或当前 shell 设置（不会回显值）"
  (umask 022; printf '%s' "$value" >"$path") || die "写入 ${path} 失败"
}

start_alert_hook() {
  local pid
  pid="$(read_pid "$ALERT_PID")"
  if pid_alive "$pid"; then
    say "alert_hook 已在运行（PID ${pid}）"
    return 0
  fi
  if port_busy 9099; then
    warn "端口 9099 已被占用（不是本脚本启动的？）；跳过 alert_hook"
    return 0
  fi
  say "→ 启动 alert_hook :9099（告警写 ${ALERT_LOG}）"
  (
    cd "$ROOT"
    nohup python3 scripts/alert_hook.py >>"$ALERT_LOG" 2>&1 &
    echo $! >"$ALERT_PID"
  )
  if ! wait_http "http://127.0.0.1:9099/health" 10; then
    warn "alert_hook 未在 10s 内就绪；日志尾部："
    tail -n 20 "$ALERT_LOG" >&2 || true
    return 1
  fi
}

stop_alert_hook() {
  local pid
  pid="$(read_pid "$ALERT_PID")"
  if pid_alive "$pid"; then
    say "→ 停止 alert_hook（PID ${pid}）"
    kill_tree "$pid" TERM
  fi
  rm -f "$ALERT_PID"
}

start_monitoring() {
  command -v docker >/dev/null 2>&1 || die "未找到 docker"
  mkdir -p "$ROOT/logs" "$LOG_DIR"
  write_token METRICS_TOKEN "$ROOT/logs/metrics_token"
  write_token RAG_SERVICE_TOKEN "$ROOT/logs/rag_token"
  say "→ 启动 monitoring profile（Prometheus :9092 / Alertmanager :9093）"
  (cd "$ROOT" && docker compose --profile monitoring up -d --wait) || return 1
  if [[ "$(env_value ALERT_HOOK_ENABLED)" == "true" ]]; then
    start_alert_hook || return 1
  else
    warn "ALERT_HOOK_ENABLED 不为 true：未启动 alert_hook（告警仍可在 Alertmanager UI 查看）"
  fi
  say "Prometheus   http://127.0.0.1:9092"
  say "Alertmanager http://127.0.0.1:9093"
}

stop_monitoring() {
  stop_alert_hook
  say "→ 停止 Prometheus / Alertmanager（不动 postgres/redis/qdrant）"
  # 只停这两个服务：`docker compose down` 会把整个项目的容器都停掉
  (cd "$ROOT" && docker compose stop prometheus alertmanager) || true
}

monitoring_status() {
  say "== 监控 =="
  (cd "$ROOT" && docker compose ps prometheus alertmanager) || true
  if curl -fsS --max-time 2 http://127.0.0.1:9092/-/ready >/dev/null 2>&1; then
    say "Prometheus：就绪 http://127.0.0.1:9092"
  else
    warn "Prometheus 未就绪"
  fi
  if curl -fsS --max-time 2 http://127.0.0.1:9093/-/ready >/dev/null 2>&1; then
    say "Alertmanager：就绪 http://127.0.0.1:9093"
  else
    warn "Alertmanager 未就绪"
  fi
  if curl -fsS --max-time 2 http://127.0.0.1:9099/health >/dev/null 2>&1; then
    say "alert_hook：运行中"
  else
    warn "alert_hook 未运行（ALERT_HOOK_ENABLED=$(env_value ALERT_HOOK_ENABLED)）"
  fi
}

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
  say "→ 启动基础设施（postgres / redis / redis-coord / qdrant；compose 配置有变时会重建容器，命名卷数据保留）"
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
    say "后端已在运行（PID ${pid}）"
    return 0
  fi
  if port_busy "$BACKEND_PORT"; then
    warn "端口 $BACKEND_PORT 已被占用（不是本脚本启动的？）；跳过后端启动"
    return 0
  fi
  say "→ 启动后端 :${BACKEND_PORT}（${API_WORKERS} worker）"
  (
    cd "$ROOT/backend"
    # S5：多 worker 才开 Prometheus 多进程聚合。只在本子 shell export，
    # RAG/前端进程不继承（它们有自己的 registry）。
    # 每次启动清空重建，避免上一轮死进程的 mmap 文件把计数带进来。
    if (( API_WORKERS > 1 )); then
      PROM_DIR="$LOG_DIR/prom"
      rm -rf "$PROM_DIR"
      mkdir -p "$PROM_DIR"
      export PROMETHEUS_MULTIPROC_DIR="$PROM_DIR"
    fi
    # S4：SIGTERM 后先停接新请求、drain 在飞/SSE（上限 15s），再走 lifespan 收尾。
    # `--no-access-log`：uvicorn 的 access log 记原始 path（含分享/导出 token，
    # 且 propagate=false，脱敏过滤器盖不到）；访问日志由 ObservabilityMiddleware 统一记路由模板。
    nohup .venv/bin/python -m uvicorn app.main:app --port "$BACKEND_PORT" \
      --no-access-log \
      --workers "$API_WORKERS" \
      --timeout-graceful-shutdown 15 >>"$BACKEND_LOG" 2>&1 &
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
    say "前端已在运行（PID ${pid}）"
    return 0
  fi
  if port_busy "$FRONTEND_PORT"; then
    warn "端口 $FRONTEND_PORT 已被占用（不是本脚本启动的？）；跳过前端启动"
    return 0
  fi
  say "→ 启动前端 :${FRONTEND_PORT}（${MODE}）"
  if [[ "$MODE" == "prod" && ! -f "$ROOT/frontend/.next/BUILD_ID" ]]; then
    # build 会覆盖 .next；上面的端口检查已经保证 dev server 不在跑。
    say "→ 前端没有 build（.next/BUILD_ID 不存在），先 pnpm build"
    (cd "$ROOT/frontend" && pnpm build) || return 1
  fi
  (
    cd "$ROOT/frontend"
    if [[ "$MODE" == "prod" ]]; then
      nohup pnpm start >>"$FRONTEND_LOG" 2>&1 &
    else
      nohup pnpm dev >>"$FRONTEND_LOG" 2>&1 &
    fi
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

start_rag() {
  if ! rag_enabled; then
    say "RAG_SERVICE_URL 为空：跳过 RAG 服务（进程内回滚）"
    return 0
  fi
  local pid
  pid="$(read_pid "$RAG_PID")"
  if pid_alive "$pid"; then
    say "RAG 服务已在运行（PID ${pid}）"
    return 0
  fi
  if port_busy "$RAG_PORT"; then
    warn "端口 $RAG_PORT 已被占用（不是本脚本启动的？）；跳过 RAG 启动"
    return 0
  fi
  say "→ 启动 RAG 推理服务 :$RAG_PORT"
  (
    cd "$ROOT/backend"
    # 服务自己读 .env；RAG_PRELOAD=true 时启动后后台预热（首次检索不必等加载）
    nohup .venv/bin/python -m app.rag.service >>"$RAG_LOG" 2>&1 &
    echo $! >"$RAG_PID"
  )
  if ! wait_http "http://127.0.0.1:$RAG_PORT/health" 60; then
    warn "RAG 服务未在 60s 内就绪；日志尾部："
    tail -n 30 "$RAG_LOG" >&2 || true
    return 1
  fi
  say "RAG 服务就绪：http://127.0.0.1:$RAG_PORT/health（模型预热中属正常）"
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
  local name="$1" pidfile="$2" grace="${3:-4}"
  local pid
  pid="$(read_pid "$pidfile")"
  if pid_alive "$pid"; then
    say "→ 停止${name}（PID ${pid}；优雅等待 ${grace}s）"
    kill_tree "$pid" TERM
    local i
    for ((i = 0; i < grace * 5; i++)); do
      pid_alive "$pid" || break
      sleep 0.2
    done
    if pid_alive "$pid"; then
      warn "$name 未在 ${grace}s 内退出，SIGKILL"
      kill_tree "$pid" KILL
    fi
  fi
  rm -f "$pidfile"
}

rotate_logs() {
  # copytruncate：uvicorn/pnpm 的 stdout 是继承的 fd，mv 不会让写入方换文件；
  # 先拷走再原地清空。复制与清空之间写入的几行可能丢——如实记录，不假装是 logrotate。
  local f size
  shopt -s nullglob
  for f in "$LOG_DIR"/*.log; do
    size="$(wc -c <"$f" 2>/dev/null || echo 0)"
    ((size > LOG_MAX_BYTES)) || continue
    local i
    for ((i = LOG_KEEP - 1; i >= 1; i--)); do
      [[ -f "$f.$i" ]] && mv -f "$f.$i" "$f.$((i + 1))"
    done
    cp -f "$f" "$f.1" 2>/dev/null || continue
    : >"$f"
    say "日志轮转：${f}（$((size / 1024 / 1024))MB）"
  done
  shopt -u nullglob
}

start_rotator() {
  [[ "$MODE" == "prod" ]] || return 0
  local pid
  pid="$(read_pid "$ROTATE_PID")"
  pid_alive "$pid" && return 0
  (
    while true; do
      sleep "$LOG_CHECK_SECONDS"
      rotate_logs
    done
  ) >/dev/null 2>&1 &
  echo $! >"$ROTATE_PID"
}

stop_rotator() {
  local pid
  pid="$(read_pid "$ROTATE_PID")"
  if pid_alive "$pid"; then
    kill_tree "$pid" TERM
  fi
  rm -f "$ROTATE_PID"
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
    say "→ 清理端口 $port 的监听进程（PID ${pid}）"
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
  local api_grace=4 front_grace=4 rag_grace=4
  if [[ "$MODE" == "prod" ]]; then
    # API 的 drain 上限 15s，留 5s 给 lifespan 收尾（worker 释放租约、池关闭）。
    api_grace=20
    front_grace=10
    rag_grace=10
  fi
  stop_rotator
  stop_one "后端" "$BACKEND_PID" "$api_grace"
  stop_one "前端" "$FRONTEND_PID" "$front_grace"
  stop_one "RAG 服务" "$RAG_PID" "$rag_grace"
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
    if port_busy "$RAG_PORT"; then
      warn "端口 $RAG_PORT 仍被占用，按监听进程清理"
      kill_listeners "$RAG_PORT"
    fi
  elif port_busy "$BACKEND_PORT" || port_busy "$FRONTEND_PORT" || port_busy "$RAG_PORT"; then
    warn "仍有端口被占用，但进程不是本脚本启动的（无 PID 文件）；需要时用 stop --force"
  fi
}

start_all() {
  check_prereqs
  mkdir -p "$LOG_DIR"
  if [[ "$MODE" == "prod" ]]; then
    say "→ prod 模式：前端 build+start、API 15s 优雅停机、日志轮转（${LOG_DIR}）"
    rotate_logs
    start_rotator
  fi
  start_infra
  init_checkpoint
  # 顺序：infra → checkpoint → RAG → 后端 → 前端（后端起时 RAG 已在，首个检索不用等进程）
  start_rag || exit 1
  start_backend || exit 1
  start_frontend || exit 1
  say ""
  say "就绪（${MODE}）："
  say "  前端  http://127.0.0.1:$FRONTEND_PORT"
  say "  后端  http://127.0.0.1:$BACKEND_PORT/docs"
  if rag_enabled; then
    say "  RAG   http://127.0.0.1:$RAG_PORT/health"
  fi
  say "  日志  $LOG_DIR"
  if [[ "$MODE" == "prod" ]]; then
    say "停止  bash scripts/prod.sh stop"
  else
    say "停止  $0 stop"
  fi
}

health_all() {
  local ok=0
  curl -fsS --max-time 2 "http://127.0.0.1:$BACKEND_PORT/health" >/dev/null 2>&1 \
    || { warn "后端 /health 不可达"; ok=1; }
  curl -fsS --max-time 2 "http://127.0.0.1:$BACKEND_PORT/health/ready" >/dev/null 2>&1 \
    || { warn "后端 /health/ready 未就绪"; ok=1; }
  curl -fsS --max-time 2 -o /dev/null "http://127.0.0.1:$FRONTEND_PORT/login" 2>/dev/null \
    || { warn "前端 /login 不可达"; ok=1; }
  if rag_enabled || port_busy "$RAG_PORT"; then
    curl -fsS --max-time 2 "http://127.0.0.1:$RAG_PORT/health" >/dev/null 2>&1 \
      || { warn "RAG /health 不可达"; ok=1; }
  fi
  if ((ok == 0)); then
    say "健康检查通过（${MODE}）"
  fi
  return "$ok"
}

status_all() {
  say "模式：${MODE}（日志 ${LOG_DIR}）"
  say "== 基础设施 =="
  (cd "$ROOT" && docker compose ps) || true
  say ""
  say "== 应用 =="
  local bpid fpid rpid
  bpid="$(read_pid "$BACKEND_PID")"
  fpid="$(read_pid "$FRONTEND_PID")"
  rpid="$(read_pid "$RAG_PID")"
  if pid_alive "$bpid"; then
    say "后端：运行中（PID ${bpid}，端口 ${BACKEND_PORT}）"
  elif port_busy "$BACKEND_PORT"; then
    say "后端：端口 $BACKEND_PORT 被占用（非本脚本 PID 文件）"
  else
    say "后端：未运行"
  fi
  if pid_alive "$fpid"; then
    say "前端：运行中（PID ${fpid}，端口 ${FRONTEND_PORT}）"
  elif port_busy "$FRONTEND_PORT"; then
    say "前端：端口 $FRONTEND_PORT 被占用（非本脚本 PID 文件）"
  else
    say "前端：未运行"
  fi
  if pid_alive "$rpid"; then
    say "RAG：运行中（PID ${rpid}，端口 ${RAG_PORT}）"
  elif port_busy "$RAG_PORT"; then
    say "RAG：端口 $RAG_PORT 被占用（非本脚本 PID 文件）"
  elif rag_enabled; then
    say "RAG：未运行（.env 已配置 RAG_SERVICE_URL）"
  else
    say "RAG：未启用（RAG_SERVICE_URL 为空，进程内回滚）"
  fi
  say ""
  say "== 健康 =="
  curl -fsS --max-time 2 "http://127.0.0.1:$BACKEND_PORT/health" 2>/dev/null || echo "后端 /health 不可达"
  echo
  curl -fsS --max-time 2 -o /dev/null -w '前端 /login HTTP %{http_code}\n' \
    "http://127.0.0.1:$FRONTEND_PORT/login" 2>/dev/null || echo "前端 /login 不可达"
  if rag_enabled || port_busy "$RAG_PORT"; then
    curl -fsS --max-time 2 "http://127.0.0.1:$RAG_PORT/health" 2>/dev/null || echo "RAG /health 不可达"
    echo
  fi
}

case "${1:-start}" in
  start) start_all ;;
  stop) stop_apps "$(force_flag "${2:-}")" ;;
  restart) stop_apps "$(force_flag "${2:-}")"; start_all ;;
  status) status_all ;;
  health) health_all ;;
  logs)
    mkdir -p "$LOG_DIR"
    touch "$BACKEND_LOG" "$FRONTEND_LOG" "$RAG_LOG"
    case "${2:-all}" in
      backend) tail -n 100 -f "$BACKEND_LOG" ;;
      frontend) tail -n 100 -f "$FRONTEND_LOG" ;;
      rag) tail -n 100 -f "$RAG_LOG" ;;
      *) tail -n 50 -f "$BACKEND_LOG" "$FRONTEND_LOG" "$RAG_LOG" ;;
    esac
    ;;
  monitoring)
    case "${2:-status}" in
      start) start_monitoring ;;
      stop) stop_monitoring ;;
      status) monitoring_status ;;
      *) die "未知 monitoring 子命令：${2:-}（start|stop|status）" ;;
    esac
    ;;
  down)
    stop_apps true
    stop_alert_hook
    say "→ docker compose --profile observability --profile monitoring down（含 Langfuse / 监控）"
    (cd "$ROOT" && docker compose --profile observability --profile monitoring down)
    ;;
  help | -h | --help)
    sed -n '2,27p' "$0"
    ;;
  *)
    die "未知命令：${1}（用 help 看用法）"
    ;;
esac
