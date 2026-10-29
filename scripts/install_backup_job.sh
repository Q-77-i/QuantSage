#!/usr/bin/env bash
# 安装/卸载每日备份 launchd 任务（P4-S6）
#
# 用法:
#   bash scripts/install_backup_job.sh [--target DIR] [--hour 3] [--minute 30]
#   bash scripts/install_backup_job.sh --uninstall
#
# 默认 03:30（ETL 21:10 之后），日志 logs/backup-agent.log。
# **只生成并加载 plist，不在安装时跑备份**；装完可用 launchctl kickstart 手动触发一次。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.quantsage.backup"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
TARGET="${QS_BACKUP_DIR:-$HOME/QuantSageBackups}"
HOUR=3
MINUTE=30
UNINSTALL=0

say() { printf '\033[1m%s\033[0m\n' "$*"; }
die() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target) TARGET="$2"; shift 2 ;;
    --hour) HOUR="$2"; shift 2 ;;
    --minute) MINUTE="$2"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
done

if [[ "$UNINSTALL" -eq 1 ]]; then
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  say "已卸载：$LABEL"
  exit 0
fi

[[ "$HOUR" =~ ^[0-9]+$ && "$MINUTE" =~ ^[0-9]+$ ]] || die "--hour/--minute 需为整数"
if [[ "$TARGET" == /Volumes/* ]]; then
  VOLUME="/Volumes/$(printf '%s' "$TARGET" | cut -d/ -f3)"
  [[ -d "$VOLUME" ]] || die "外置盘未挂载：$VOLUME"
fi
mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/logs"
mkdir -p "$TARGET" 2>/dev/null || die "备份目标不可创建：$TARGET"
TARGET="$(cd "$TARGET" && pwd)"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$ROOT/scripts/backup.sh</string>
    <string>--target</string>
    <string>$TARGET</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>$HOUR</integer>
    <key>Minute</key><integer>$MINUTE</integer>
  </dict>
  <key>StandardOutPath</key><string>$ROOT/logs/backup-agent.log</string>
  <key>StandardErrorPath</key><string>$ROOT/logs/backup-agent.log</string>
  <key>RunAtLoad</key><false/>
</dict>
</plist>
EOF

plutil -lint "$PLIST" >/dev/null || die "plist 校验失败：$PLIST"
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
say "已安装：$PLIST"
say "每日 ${HOUR}:$(printf '%02d' "$MINUTE") 备份到 $TARGET"
say "手动触发一次：launchctl kickstart -k gui/$(id -u)/$LABEL"
