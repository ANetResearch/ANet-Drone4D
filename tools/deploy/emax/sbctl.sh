#!/usr/bin/env bash
# tools/deploy/emax/sbctl.sh：公开演示站的沙盒池运维命令（M17-FR-065；M17 PRD §8.5）。以 root 执行，降权为 awr 调用
# `python -m awr.sandbox.ctl`（读取 var/sandbox/pool.secret 签名，经池命名空间 awr/_sandbox/pool 通信）。
#
#   sudo bash /opt/anet-drone4d/current/tools/deploy/emax/sbctl.sh status                 N_max、Q、委派、各会话、降级级别、队列
#   sudo bash …/sbctl.sh list                                                             会话表
#   sudo bash …/sbctl.sh stop <sid>                                                       结束一个会话（快照保留 10 min）
#   sudo bash …/sbctl.sh drain --notice 60                                                维护：提前告知后结束全部会话
#   sudo bash …/sbctl.sh accept on|off                                                    接受或拒绝新沙盒（503 502{maintenance}）
#   sudo bash …/sbctl.sh limits --max-sessions 1|auto                                     临时降容（写入 state.json，重启保留）
#   在子命令前加 --json 输出机器可读结果（sbctl.sh --json status）。退出码：0 成功；1 池拒绝；2 参数或密钥错误；3 池不可达。
set -euo pipefail

PREFIX=/opt/anet-drone4d
ENV_FILE=/etc/anet-drone4d/env
SVC_USER=awr

[ "$(id -u)" -eq 0 ] || { echo "sbctl: 请以 root 执行（sudo bash $0 ...）" >&2; exit 2; }
[ $# -ge 1 ] || { sed -n '2,11p' "$0"; exit 2; }
PY="$PREFIX/current/.venv/bin/python"
[ -x "$PY" ] || { echo "sbctl: $PY 不存在（尚未安装？）" >&2; exit 2; }

env_get() { [ -f "$ENV_FILE" ] && sed -n "s/^$1=//p" "$ENV_FILE" | tail -1 || true; }
STATE=$(env_get AWR_SANDBOX_STATE_DIR)
STATE=${STATE:-$PREFIX/var/sandbox}
# 进程间总线汇合点：public profile 的 bus.rendezvous（只允许回环）
RDV=$("$PY" -c 'import sys, yaml; c = yaml.safe_load(open(sys.argv[1], encoding="utf-8")); print(c["profiles"]["public"].get("bus", {}).get("rendezvous") or c["bus"]["rendezvous"])' "$PREFIX/current/configs/runtime.yaml" 2>/dev/null || echo tcp/127.0.0.1:18647)

exec runuser -u "$SVC_USER" -- env HOME="$PREFIX" PYTHONDONTWRITEBYTECODE=1 AWR_PROFILE=public AWR_RENDEZVOUS="$RDV" \
  AWR_SANDBOX_STATE_DIR="$STATE" "$PY" -m awr.sandbox.ctl --state-dir "$STATE" --connect "$RDV" "$@"
