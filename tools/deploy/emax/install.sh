#!/usr/bin/env bash
# tools/deploy/emax/install.sh：在 emax（Ubuntu 24.04，x86-64，Python 3.12.3）上安装或更新 ANet Drone4D 公开演示站 V0.2-demo
# （T4，AWR-19 §3.7；ADR-085、ADR-091、ADR-092、ADR-109、ADR-118、ADR-123；M17-FR-063、FR-064）。幂等：重复执行只补齐缺失的
# 部分；安装、回滚与状态以 root 执行，--dry-run 在任何机器上以普通用户执行。
#
#   sudo bash install.sh --bundle DIR [--worlds DIR] [--admin-secret | --admin-secret-file PATH|-]
#                        [--cpu-quota PCT] [--memory-high SIZE] [--reset-limits] [--no-restart] [--no-smoke]
#   sudo bash install.sh --secret-only --admin-secret-file PATH|-   只写入或轮换内部身份口令并重启服务（不重装发布）
#   sudo bash install.sh --rollback [RELEASE]          切回上一个（或指定）发布并重启
#   sudo bash install.sh --status                      发布、服务、cgroup 委派与两个 api 的健康
#   bash install.sh --dry-run --bundle DIR [--worlds DIR] [--admin-secret-file PATH|-] [--work DIR]
#                                                      本机模拟：校验部署包、世界包与派生缓存（解到临时目录后按发布的
#                                                      代码做 deep 校验）与口令文件，打印安装计划；不需要 root，不改系统
#
#   --worlds DIR              build-local.sh 生成的世界包目录（WORLDS.json、WORLDS.sha256 与各包）：七个世界与派生缓存
#   --world-pack TAR          D1 的 synthcity 单包（兼容）
#   --admin-secret            随机生成管理口令到 /etc/anet-drone4d/admin.secret（D1；已存在时不覆盖）
#   --admin-secret-file F|-   内部身份口令（ADR-123）：从本地文件（- 为标准输入，终端上不回显）读取，校验为单行、可打印 ASCII、
#                             无空白、长度 ≥ 8，以 0600 awr 写入 /etc/anet-drone4d/admin.secret 并在 env 设置
#                             AWR_ADMIN_SECRET_FILE；口令从不回显、不写日志、不进入部署包；轮换即令全部身份令牌失效
#   --cpu-quota PCT、--memory-high SIZE   写 drop-in anet-drone4d.service.d/limits.conf（Q-D2-01、Q-D2-09 未获批时
#                             用 200% 与 1.6G）；--reset-limits 删除它，回到单元文件的 250% 与 1.8G
#   --no-smoke                不做安装后的沙盒冒烟（创建 → READY → 结束）
#
# 做什么：创建系统用户 awr 与目录（/opt/anet-drone4d、/etc/anet-drone4d；var/sandbox、var/identity 为 0700）；把包复制到
# releases/<release>（root 所有，awr 只读）；按 requirements.lock 用 uv 建该发布的 .venv；预编译字节码，以共享展示与沙盒两套
# 插件预热 numba 缓存；装入七个世界包与派生缓存（内容版本与派生键相同者跳过）并按发布的代码 deep 校验（派生缓存键与世界
# contentVersion、coordinate.sha256、GeoParams、DERIVE_VERSION 一致，AWR-04 §4.4.7）；首次安装时由 env.example 生成
# /etc/anet-drone4d/env（0600）；安装 systemd 单元（Delegate=yes、MemoryHigh、cgroup-exec.sh），切换 current 并重启，
# 等待健康检查（失败自动回滚）；报告 cgroup 委派结论；沙盒冒烟；保留最近 3 个发布。
# 不做什么：不 reload、不修改 nginx，也不申请证书（以打印的命令交给人工执行，只新增本站点）；不打开防火墙端口（两个 api 只
# 监听 127.0.0.1:18640 与 18641）；不执行任何 git 操作。
set -euo pipefail

PREFIX=/opt/anet-drone4d
ETC=/etc/anet-drone4d
SVC=anet-drone4d
SVC_USER=awr
DOMAIN=drone4d.agentnetwork.org.cn
PORT=18640
SB_PORT=18641
UV_VERSION=0.12.19
PY=${PYTHON:-/usr/bin/python3.12}
KEEP=3
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
BUNDLE=$(cd "$HERE/../../.." && pwd)
WORLD_PACK=""
WORLDS_SRC=""
ADMIN_SECRET=0
SECRET_SRC=""
SECRET=""
CPU_QUOTA=""
MEM_HIGH=""
RESET_LIMITS=0
RESTART=1
SMOKE=1
MODE=install
ROLLBACK_TO=""
WORK=""
KEEP_WORK=0
DROPIN_DIR=/etc/systemd/system/$SVC.service.d
PUBLIC_WORLDS="synthcity shenzhen suzhou newyork chicago shanghai sanfrancisco"

die() { echo "install: $*" >&2; exit 1; }
step() { echo "==> $*"; }
note() { echo "    $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --bundle) [ $# -gt 1 ] || die "--bundle 需要目录"; BUNDLE=$(cd "$2" && pwd); shift ;;
    --worlds) [ $# -gt 1 ] || die "--worlds 需要目录"; WORLDS_SRC=$(cd "$2" && pwd); shift ;;
    --world-pack) [ $# -gt 1 ] || die "--world-pack 需要文件"; WORLD_PACK=$(readlink -f "$2"); shift ;;
    --admin-secret) ADMIN_SECRET=1 ;;
    --admin-secret-file) [ $# -gt 1 ] || die "--admin-secret-file 需要文件路径或 -"; SECRET_SRC=$2; shift ;;
    --cpu-quota) [ $# -gt 1 ] || die "--cpu-quota 需要百分比"; CPU_QUOTA=$2; shift ;;
    --memory-high) [ $# -gt 1 ] || die "--memory-high 需要大小"; MEM_HIGH=$2; shift ;;
    --reset-limits) RESET_LIMITS=1 ;;
    --no-restart) RESTART=0 ;;
    --no-smoke) SMOKE=0 ;;
    --dry-run) MODE=dry-run ;;
    --secret-only) MODE=secret ;;
    --work) [ $# -gt 1 ] || die "--work 需要目录"; WORK=$2; KEEP_WORK=1; shift ;;
    --rollback) MODE=rollback; if [ $# -gt 1 ] && [ "${2#--}" = "$2" ]; then ROLLBACK_TO=$2; shift; fi ;;
    --status) MODE=status ;;
    -h|--help) sed -n '2,34p' "$0"; exit 0 ;;
    *) die "未知参数 $1（--help 查看用法）" ;;
  esac
  shift
done

[ "$ADMIN_SECRET" -eq 0 ] || [ -z "$SECRET_SRC" ] || die "--admin-secret 与 --admin-secret-file 只能取其一"
if [ -n "$CPU_QUOTA" ]; then
  [[ "$CPU_QUOTA" =~ ^[0-9]{2,3}%$ ]] && [ "${CPU_QUOTA%\%}" -ge 50 ] && [ "${CPU_QUOTA%\%}" -le 400 ] \
    || die "--cpu-quota 取 50% 至 400%（例如 200%）"
fi
if [ -n "$MEM_HIGH" ]; then
  [[ "$MEM_HIGH" =~ ^[0-9]+(\.[0-9]+)?[MG]$ ]] || die "--memory-high 取 <数字>M 或 <数字>G（例如 1.6G）"
fi
[ "$RESET_LIMITS" -eq 0 ] || [ -z "$CPU_QUOTA$MEM_HIGH" ] || die "--reset-limits 与 --cpu-quota、--memory-high 不能同时使用"

# ---- 内部身份口令（ADR-123，AWR-04 §15.9.5）：读入 shell 变量（不导出、不出现在任何命令行参数中），校验失败时只说原因 -----
read_secret() {
  local src=$1 raw="" n
  if [ "$src" = - ]; then
    if [ -t 0 ]; then
      printf '输入内部身份口令（不回显，回车结束）：' >&2
      IFS= read -rs raw || true
      echo >&2
    else
      raw=$(cat; printf x)
      raw=${raw%x}
    fi
  else
    [ -f "$src" ] && [ -r "$src" ] || die "口令文件 $src 不存在或不可读"
    n=$(stat -c %s -- "$src")
    [ "$n" -le 4096 ] || die "口令文件 $src 超过 4096 字节，不像单行口令"
    raw=$(cat -- "$src"; printf x)
    raw=${raw%x}
  fi
  raw=${raw%$'\n'}
  raw=${raw%$'\r'}
  case "$raw" in
    *$'\n'*) raw=""; die "口令必须为单行" ;;
  esac
  if [ "${#raw}" -lt 8 ]; then raw=""; die "口令长度必须 ≥ 8 个字符"; fi
  if ! (LC_ALL=C; [[ $raw =~ ^[[:graph:]]+$ ]]); then raw=""; die "口令只能由可打印 ASCII 字符组成，且不含空白"; fi
  SECRET=$raw
  raw=""
}

check_bundle() {  # 部署包完整性与形态（dry-run 与安装共用）
  [ -f "$BUNDLE/RELEASE" ] && [ -f "$BUNDLE/requirements.lock" ] && [ -f "$BUNDLE/apps/web/dist/index.html" ] \
    || die "$BUNDLE 不是 build-local.sh 生成的部署包（缺少 RELEASE、requirements.lock 或 apps/web/dist）"
  REL=$(sed -n 's/^release=//p' "$BUNDLE/RELEASE")
  [ -n "$REL" ] || die "RELEASE 中没有 release="
  [[ "$REL" =~ ^[0-9]{8}-[0-9]{6}-([0-9a-f]{10}|nogit)(-dirty)?$ ]] || die "RELEASE 的 release=$REL 格式不对（17 §17.20.1）"
  grep -q 'data-landing' "$BUNDLE/apps/web/dist/index.html" || die "前端不是 VITE_AWR_DEMO=public 演示构建"
  [ ! -e "$BUNDLE/apps/web/dist/bench" ] || die "前端构建含 dist/bench（flight60 夹具，ADR-109 第 5 条待评估），拒绝安装"
  (cd "$BUNDLE" && sha256sum --quiet -c MANIFEST.sha256) || die "MANIFEST.sha256 校验失败（包不完整或被改动）"
  BI="$BUNDLE/apps/web/dist/build-info.json"
  if [ -f "$BI" ]; then
    grep -q "\"release\": *\"$REL\"" "$BI" || die "前端 build-info.json 的 release 与 RELEASE 不一致（前后端不是同一次构建）"
  else
    note "警告：前端没有 build-info.json（buildInfoPlugin 之前的构建）；启动屏不显示版本号" >&2
  fi
  for f in anet-drone4d.service cgroup-exec.sh drone4d.agentnetwork.org.cn.conf env.example worldpack.py smoke.py; do
    [ -f "$BUNDLE/tools/deploy/emax/$f" ] || die "部署包缺少 tools/deploy/emax/$f"
  done
  [ -x "$BUNDLE/tools/deploy/emax/cgroup-exec.sh" ] || die "cgroup-exec.sh 没有可执行位（重新用 build-local.sh 打包）"
  grep -q '^Delegate=yes' "$BUNDLE/tools/deploy/emax/anet-drone4d.service" || die "单元文件缺少 Delegate=yes"
  VERSION=$(sed -n 's/^version=//p' "$BUNDLE/RELEASE")
  echo "发布 $REL（V${VERSION:-?}，来自 $BUNDLE）"
}

if [ -n "$SECRET_SRC" ]; then read_secret "$SECRET_SRC"; fi

# ---- --dry-run：本机模拟（不需要 root，不改系统） -------------------------------------------------------------------
if [ "$MODE" = dry-run ]; then
  check_bundle
  [ -z "$SECRET" ] || { SECRET=""; note "口令校验通过（不回显）：安装时以 0600 awr 写入 $ETC/admin.secret 并设置 AWR_ADMIN_SECRET_FILE"; }
  [ -z "$CPU_QUOTA$MEM_HIGH" ] || note "将写入 $DROPIN_DIR/limits.conf：${CPU_QUOTA:+CPUQuota=$CPU_QUOTA }${MEM_HIGH:+MemoryHigh=$MEM_HIGH}"
  DPY=${AWR_DRY_PYTHON:-}
  if [ -z "$DPY" ]; then
    for c in "$BUNDLE/.venv/bin/python" "$HERE/../../../.venv/bin/python"; do [ -x "$c" ] && { DPY=$c; break; }; done
  fi
  if [ -n "$WORLDS_SRC" ]; then
    [ -n "$DPY" ] || die "--dry-run 校验世界需要带依赖的 Python（设 AWR_DRY_PYTHON，例如仓库的 .venv/bin/python）"
    if [ -z "$WORK" ]; then WORK=$(mktemp -d "${TMPDIR:-/tmp}/anet-drone4d-dry.XXXXXX"); fi
    trap '[ "$KEEP_WORK" -eq 1 ] || rm -rf "$WORK"' EXIT
    step "世界包校验并解到 $WORK/worlds（模拟 $PREFIX/var/worlds）"
    "$DPY" "$BUNDLE/tools/deploy/emax/worldpack.py" install --src "$WORLDS_SRC" --worlds-dir "$WORK/worlds" \
      || die "世界包校验或解包失败"
    step "按发布的代码 deep 校验派生缓存与世界状态"
    PYTHONPATH="$BUNDLE/python" AWR_GEO_CACHE_DIR="" "$DPY" -m awr.world.geometry.cli check --all --deep --worlds "$WORK/worlds" \
      || die "派生缓存校验失败（键与世界 contentVersion、坐标、GeoParams 或 DERIVE_VERSION 不一致，或文件损坏）"
    PYTHONPATH="$BUNDLE/python" "$DPY" - "$WORK/worlds" "$WORLDS_SRC/WORLDS.json" <<'PY' || die "世界状态校验失败"
import json, subprocess, sys
worlds, index = sys.argv[1], json.load(open(sys.argv[2], encoding="utf-8"))
out = subprocess.run([sys.executable, "-m", "awr.world.package.cli", "status", "--json", "--worlds", worlds, "--raw",
                      worlds + "/.no-raw"], check=True, capture_output=True, text=True).stdout
rows = {r["world_id"]: r for r in json.loads(out)["worlds"]}
bad = [w["world_id"] for w in index["worlds"] if (rows.get(w["world_id"]) or {}).get("rebuild") is not False]
for w in index["worlds"]:
    r = rows.get(w["world_id"]) or {}
    print(f"    [{w['world_id']}] 状态 {r.get('status')}，需重建 {r.get('rebuild')}（{r.get('reason') or '-'}）")
sys.exit(1 if bad else 0)
PY
  fi
  step "安装计划（--dry-run 不执行）"
  note "releases/$REL ← $BUNDLE（uv pip sync requirements.lock，numba 预热：共享展示与沙盒插件集）"
  note "var/worlds ← ${WORLDS_SRC:-（不更新世界包）}；var/sandbox、var/identity（0700 $SVC_USER）"
  note "/etc/systemd/system/$SVC.service（CPUQuota 250%、MemoryHigh 1.8G、MemoryMax 2G、Delegate=yes、cgroup-exec.sh）"
  note "切换 current，重启并等待 show-api 就绪（失败自动回滚），报告 cgroup 委派，沙盒冒烟$([ "$SMOKE" -eq 1 ] || echo '（跳过）')"
  note "nginx：人工安装 $DOMAIN.conf 并 reload（install.sh 从不改动 nginx）"
  echo "dry-run 通过"
  exit 0
fi

# ---- 以下需要 root ----------------------------------------------------------------------------------------------------
[ "$(id -u)" -eq 0 ] || die "请以 root 执行（sudo bash $0 ...）；本机模拟用 --dry-run"
[ "$(uname -m)" = x86_64 ] || die "只支持 x86-64（StateRing 依赖 TSO，AWR-19 OPS-NFR-011）"
for c in systemctl runuser rsync curl sha256sum tar stat; do command -v "$c" >/dev/null || die "缺少命令 $c（apt install $c）"; done

as_awr() { runuser -u "$SVC_USER" -- env HOME="$PREFIX" NUMBA_CACHE_DIR="$PREFIX/var/cache/numba" PYTHONDONTWRITEBYTECODE=1 "$@"; }

env_get() { [ -f "$ETC/env" ] && sed -n "s/^$1=//p" "$ETC/env" | tail -1 || true; }

set_env_kv() {  # set_env_kv KEY VALUE：替换已有（含注释掉的）行，否则追加
  if grep -qE "^#?$1=" "$ETC/env"; then
    sed -i -E "s|^#?$1=.*|$1=$2|" "$ETC/env"
  else
    printf '%s=%s\n' "$1" "$2" >> "$ETC/env"
  fi
}

health() {  # 等待 show-api 就绪（经回环、带站点 Host；ready 需要共享展示 sim-core 环与世界都已就绪）
  local i
  for i in $(seq 1 90); do
    if curl -fsS -o /dev/null -H "Host: $DOMAIN" "http://127.0.0.1:$PORT/api/health/ready"; then
      note "show-api 就绪（${i} s）：http://127.0.0.1:$PORT（Host: $DOMAIN）"
      return 0
    fi
    sleep 1
  done
  echo "    90 s 内未就绪：journalctl -u $SVC -n 100；ls $PREFIX/var/runs/current/logs/" >&2
  return 1
}

sandbox_enabled() {
  case "$(env_get AWR_SANDBOX_ENABLED)" in 0|false|False|no) return 1 ;; *) return 0 ;; esac
}

cgroup_report() {  # 委派结论（AWR-04 §4.4.4 第 3 条）与 cgroup-exec.sh 的那一行日志
  local cg fst
  fst=$(stat -fc %T /sys/fs/cgroup 2>/dev/null || echo unknown)
  cg=$(systemctl show -p ControlGroup --value "$SVC" 2>/dev/null || true)
  if [ "$fst" != cgroup2fs ]; then
    note "cgroup：/sys/fs/cgroup 为 $fst，不是 cgroup v2；沙盒按 N_max − 1、nice +5 与 RSS 看门狗运行"
  elif [ -n "$cg" ] && [ -d "/sys/fs/cgroup$cg/sandbox/sb" ] && [ -d "/sys/fs/cgroup$cg/core" ]; then
    note "cgroup：已委派（$cg：core/ 与 sandbox/{ctl,plan,sb}；sandbox/cpu.max $(cat "/sys/fs/cgroup$cg/sandbox/cpu.max" 2>/dev/null)，memory.high $(cat "/sys/fs/cgroup$cg/sandbox/memory.high" 2>/dev/null)）"
  else
    note "cgroup：未委派（systemd 不支持 Delegate 或包装脚本跳过）；沙盒按 N_max − 1、nice +5 与 RSS 看门狗运行"
  fi
  journalctl -u "$SVC" -n 400 -o cat 2>/dev/null | grep '"proc": "cgroup-exec"' | tail -1 | sed 's/^/    /' || true
}

releases() { find "$PREFIX/releases" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null | sort; }
current_release() { basename "$(readlink -f "$PREFIX/current" 2>/dev/null || echo none)"; }

switch_to() {  # 原子切换 current 符号链接
  ln -sfn "releases/$1" "$PREFIX/.current.tmp"
  mv -Tf "$PREFIX/.current.tmp" "$PREFIX/current"
}

restart_service() {
  systemctl daemon-reload
  systemctl enable "$SVC" >/dev/null 2>&1 || true
  if [ "$RESTART" -eq 1 ]; then
    step "重启 $SVC"
    systemctl restart "$SVC"
    health
  else
    note "--no-restart：稍后执行 systemctl restart $SVC"
  fi
}

P=$(env_get AWR_PORT); PORT=${P:-$PORT}
P=$(env_get AWR_SANDBOX_PORT); SB_PORT=${P:-$SB_PORT}

write_secret() {  # --admin-secret-file：printf 是 bash 内建命令，口令不出现在任何进程的命令行；先写临时文件再原子改名
  local old_umask tmps
  old_umask=$(umask)
  umask 077
  tmps=$(mktemp "$ETC/.admin.secret.XXXXXX")
  printf '%s\n' "$SECRET" > "$tmps"
  SECRET=""
  chown "$SVC_USER:$SVC_USER" "$tmps"
  chmod 0600 "$tmps"
  mv -f "$tmps" "$ETC/admin.secret"
  umask "$old_umask"
  set_env_kv AWR_ADMIN_SECRET_FILE "$ETC/admin.secret"
  note "已写入内部身份口令 $ETC/admin.secret（0600 $SVC_USER，不回显）并设置 AWR_ADMIN_SECRET_FILE；重启后生效，旧的身份令牌全部失效"
}

# ---- --secret-only：只写入或轮换内部身份口令 --------------------------------------------------------------------------
if [ "$MODE" = secret ]; then
  [ -n "$SECRET" ] || die "--secret-only 需要 --admin-secret-file PATH|-"
  [ -f "$ETC/env" ] && getent passwd "$SVC_USER" >/dev/null || die "尚未安装（先按 README 2.2 安装）"
  step "内部身份口令"
  write_secret
  restart_service
  exit 0
fi

# ---- --status ---------------------------------------------------------------------------------------------------
if [ "$MODE" = status ]; then
  echo "current:  $(current_release)"
  echo "releases: $(releases | tr '\n' ' ')"
  [ -f "$PREFIX/current/RELEASE" ] && sed 's/^/          /' "$PREFIX/current/RELEASE"
  [ -f "$DROPIN_DIR/limits.conf" ] && echo "limits:   $(grep -E '^(CPUQuota|MemoryHigh)=' "$DROPIN_DIR/limits.conf" | tr '\n' ' ')"
  systemctl --no-pager --lines=0 status "$SVC" || true
  cgroup_report
  echo "show-api:    $(curl -sS -H "Host: $DOMAIN" "http://127.0.0.1:$PORT/api/health/ready" 2>&1 || true)"
  echo "sandbox-api: $(curl -sS -H "Host: $DOMAIN" "http://127.0.0.1:$SB_PORT/api/sandbox/v1/health" 2>&1 || true)"
  echo "capacity:    $(curl -sS -H "Host: $DOMAIN" "http://127.0.0.1:$SB_PORT/api/sandbox/v1/capacity" 2>&1 || true)"
  exit 0
fi

# ---- --rollback -------------------------------------------------------------------------------------------------
if [ "$MODE" = rollback ]; then
  CUR=$(current_release)
  if [ -z "$ROLLBACK_TO" ]; then  # 当前发布之前的那一个（发布名以时间戳开头，字典序即时间序）
    ROLLBACK_TO=$(releases | awk -v c="$CUR" '$0 < c' | tail -1)
  fi
  [ -n "$ROLLBACK_TO" ] && [ -d "$PREFIX/releases/$ROLLBACK_TO" ] || die "没有可回滚的发布（现有：$(releases | tr '\n' ' ')）"
  [ -x "$PREFIX/releases/$ROLLBACK_TO/.venv/bin/python" ] || die "发布 $ROLLBACK_TO 的 .venv 不完整"
  step "回滚：$CUR → $ROLLBACK_TO"
  switch_to "$ROLLBACK_TO"
  install -m 0644 "$PREFIX/releases/$ROLLBACK_TO/tools/deploy/emax/anet-drone4d.service" "/etc/systemd/system/$SVC.service"
  restart_service
  note "世界包与派生缓存不随回滚变化：每个世界保留当前与上一个派生缓存条目；若上一发布的 DERIVE_VERSION 更旧且条目已被清理，"
  note "该世界在沙盒中标为不可用（507 cache_missing），用该发布的 build-local.sh 世界包重新 --worlds 安装即可"
  exit 0
fi

# ---- 安装或更新 ----------------------------------------------------------------------------------------------------
check_bundle
[ -x "$PY" ] || die "找不到 $PY（apt install python3.12 python3.12-venv）"
"$PY" -c 'import sys; assert sys.version_info[:2] == (3, 12)' || die "$PY 不是 Python 3.12"
"$PY" -c 'import ensurepip, venv' 2>/dev/null || die "缺少 venv 模块：apt install python3.12-venv"
if [ -n "$WORLDS_SRC" ]; then
  [ -f "$WORLDS_SRC/WORLDS.json" ] || die "$WORLDS_SRC 不是世界包目录（缺少 WORLDS.json）"
fi

step "用户与目录"
getent group "$SVC_USER" >/dev/null || groupadd --system "$SVC_USER"
getent passwd "$SVC_USER" >/dev/null || useradd --system --gid "$SVC_USER" --home-dir "$PREFIX" --no-create-home \
  --shell /usr/sbin/nologin --comment "ANet Drone4D public demo" "$SVC_USER"
install -d -o root -g root -m 0755 "$PREFIX" "$PREFIX/releases" "$PREFIX/tools"
install -d -o root -g root -m 0700 "$PREFIX/.uv-cache"
# var 与 var/worlds 为 0751：nginx（www-data）以 alias 直出 /worlds/ 需要穿越这两级目录（只穿越、不可列目录）；世界目录
# 本身由 worldpack 以 0755 写出，隐藏目录（.geo-cache、.staging 等）由站点文件拒绝。其余目录保持 0750
install -d -o "$SVC_USER" -g "$SVC_USER" -m 0751 "$PREFIX/var" "$PREFIX/var/worlds"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 0750 "$PREFIX/var/runs" "$PREFIX/var/cache" "$PREFIX/var/cache/numba"
# 沙盒池状态（token.key、pool.secret、state.json、audit.jsonl、runs/、snapshots/、parked/，池首次启动时创建）与内部身份状态
# （lockout.json、revoked.json）：只有 awr 可读写（M17 PRD §8.4、§14.5）
install -d -o "$SVC_USER" -g "$SVC_USER" -m 0700 "$PREFIX/var/sandbox" "$PREFIX/var/identity"
install -d -o root -g "$SVC_USER" -m 0750 "$ETC"

step "uv $UV_VERSION"
UV="$PREFIX/tools/uv/bin/uv"
if [ ! -x "$UV" ] || [ "$("$UV" --version 2>/dev/null | cut -d' ' -f2)" != "$UV_VERSION" ]; then
  rm -rf "$PREFIX/tools/uv"
  "$PY" -m venv "$PREFIX/tools/uv"
  "$PREFIX/tools/uv/bin/pip" install --quiet --disable-pip-version-check "uv==$UV_VERSION" \
    || die "uv 自举失败（检查到 PyPI 的网络；可设 PIP_INDEX_URL 与 UV_INDEX_URL 指向镜像）"
fi

DEST="$PREFIX/releases/$REL"
step "发布目录 $DEST"
if [ -f "$DEST/MANIFEST.sha256" ] && cmp -s "$DEST/MANIFEST.sha256" "$BUNDLE/MANIFEST.sha256" && [ -x "$DEST/.venv/bin/python" ]; then
  note "已存在且内容一致，复用"
else
  install -d -o root -g root -m 0755 "$DEST"
  rsync -a --delete --exclude '/.venv/' --chown=root:root "$BUNDLE/" "$DEST/"
  chmod -R u+rwX,go+rX,go-w "$DEST"
fi
chmod 0755 "$DEST"/tools/deploy/emax/*.sh

step ".venv（uv pip sync requirements.lock）"
export UV_CACHE_DIR="$PREFIX/.uv-cache"
if [ ! -x "$DEST/.venv/bin/python" ]; then
  "$UV" venv --quiet --python "$PY" "$DEST/.venv"
fi
"$UV" pip sync --quiet --python "$DEST/.venv/bin/python" "$DEST/requirements.lock" \
  || die "依赖安装失败（hash 锁定；网络受限时设 UV_INDEX_URL 指向 PyPI 镜像后重试）"
# awr 本身以源码路径加入 sys.path（等价于可编辑安装，但不在只读的源码树里写 egg-info）：ROOT 由源码位置推导（AWR-03 §4.1），
# 因此服务端读到的部署目录 RELEASE 就是 $DEST/RELEASE（构建信息，ADR-118）
SITE=$("$DEST/.venv/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
echo "$DEST/python" > "$SITE/awr-src.pth"
"$DEST/.venv/bin/python" -c 'import awr.runtime.supervisor, awr.api.main' || die "awr 无法导入"
"$DEST/.venv/bin/python" -c 'import awr.sandbox.pool, awr.api.sandbox_main' 2>/dev/null \
  || note "警告：沙盒模块无法导入（sandbox-pool 将以 module_missing 置 FAILED，共享展示不受影响）" >&2
"$DEST/.venv/bin/python" -m compileall -q -j 0 "$DEST/python" >/dev/null || true
chmod -R go+rX,go-w "$DEST/.venv"

step "numba 预热（写入 $PREFIX/var/cache/numba；sim-core 启动时只读缓存）"
as_awr "$DEST/.venv/bin/python" -m awr.sim.runtime.warm
SB_PLUGINS=$("$DEST/.venv/bin/python" -c 'from awr.sandbox.spawner import default_plugins; print(",".join(default_plugins()))' 2>/dev/null || true)
if [ -n "$SB_PLUGINS" ]; then  # 沙盒 sim-core 的插件集（D1 公开剧本插件 + D2 插件），否则首个沙盒在主循环内编译（M17-FR-064）
  as_awr env AWR_PLUGINS="$SB_PLUGINS" "$DEST/.venv/bin/python" -m awr.sim.runtime.warm
fi
as_awr "$DEST/.venv/bin/python" -c "from awr.sim.planning import kernels_track as K, smooth as S, astar25 as A; K.warmup(); S.warmup(); A.warmup()"

step "世界：七个世界包与派生缓存（AWR-04 §4.4.7；ADR-109）"
W="$PREFIX/var/worlds"
PW=$("$DEST/.venv/bin/python" -c 'import sys, yaml; c = yaml.safe_load(open(sys.argv[1], encoding="utf-8")); print(" ".join(c["profiles"]["public"]["net"]["worlds_allow"]))' "$DEST/configs/runtime.yaml" 2>/dev/null || true)
PUBLIC_WORLDS=${PW:-$PUBLIC_WORLDS}
FROM_PACKS=""
if [ -n "$WORLDS_SRC" ]; then
  "$DEST/.venv/bin/python" "$DEST/tools/deploy/emax/worldpack.py" install --src "$WORLDS_SRC" --worlds-dir "$W" \
    --owner "$SVC_USER" || die "世界包安装失败（校验或解包；服务未改动）"
  FROM_PACKS=$("$DEST/.venv/bin/python" -c 'import json, sys; print(" ".join(w["world_id"] for w in json.load(open(sys.argv[1]))["worlds"]))' "$WORLDS_SRC/WORLDS.json")
fi
if [ -n "$WORLD_PACK" ]; then  # D1 的 synthcity 单包（兼容）
  [ -f "$WORLD_PACK" ] || die "世界包 $WORLD_PACK 不存在"
  BAD=$(tar -tzf "$WORLD_PACK" | grep -vE '^(\./)?(synthcity|_shared/env)(/|$)|^(\./)?\.status/(synthcity\.json)?$' | head -3 || true)
  [ -z "$BAD" ] || die "D1 世界包只能包含 synthcity、_shared/env 与 .status/synthcity.json（发现：$BAD）"
  TMPW="$W/.unpack-$$"
  install -d -o "$SVC_USER" -g "$SVC_USER" "$TMPW"
  as_awr tar -C "$TMPW" -xzf "$WORLD_PACK"
  if [ -d "$TMPW/synthcity" ]; then rm -rf "$W/synthcity.old"; [ ! -d "$W/synthcity" ] || mv "$W/synthcity" "$W/synthcity.old"; mv "$TMPW/synthcity" "$W/synthcity"; rm -rf "$W/synthcity.old"; fi
  if [ -d "$TMPW/_shared/env" ]; then as_awr mkdir -p "$W/_shared"; rm -rf "$W/_shared/env"; mv "$TMPW/_shared/env" "$W/_shared/env"; fi
  if [ -f "$TMPW/.status/synthcity.json" ]; then as_awr mkdir -p "$W/.status"; mv -f "$TMPW/.status/synthcity.json" "$W/.status/synthcity.json"; fi
  rm -rf "$TMPW"
fi
case " $FROM_PACKS " in
  *" synthcity "*) ;;
  *)  # 世界包里没有 synthcity：在服务器上生成（约 30 s、峰值约 1.3 GB 内存），并派生它的缓存（小，几秒）
    as_awr env AWR_WORLDS_DIR="$W" "$DEST/.venv/bin/python" -m awr.world.package.cli build synthcity --missing \
      || die "synthcity 生成失败（也可在本机 build-local.sh --worlds synthcity 后用 --worlds 上传）"
    as_awr "$DEST/.venv/bin/python" -m awr.world.geometry.cli warm --world synthcity --worlds "$W" >/dev/null \
      || note "警告：synthcity 派生缓存预热失败" >&2 ;;
esac
step "按新发布的代码校验派生缓存（deep）与世界状态"
GEO_JSON=$(as_awr env AWR_GEO_CACHE_DIR="" "$DEST/.venv/bin/python" -m awr.world.geometry.cli check --all --deep --worlds "$W" --json || true)
STATUS_JSON=$(as_awr env AWR_WORLDS_DIR="$W" "$DEST/.venv/bin/python" -m awr.world.package.cli status --json --worlds "$W" \
  --raw "$W/.no-raw" 2>/dev/null || echo '{"worlds": []}')
"$DEST/.venv/bin/python" - "$FROM_PACKS" "$PUBLIC_WORLDS" "$GEO_JSON" "$STATUS_JSON" <<'PY' || die "世界或派生缓存校验失败（服务未改动；按上面的原因重新打包上传）"
import json, sys
packs, public = sys.argv[1].split(), sys.argv[2].split()
try:
    geo = {r["world_id"]: r for r in json.loads(sys.argv[3] or "[]")}
except ValueError:
    geo = {}
rows = {r["world_id"]: r for r in json.loads(sys.argv[4]).get("worlds", [])}
fail = False
for wid in public:
    g, s = geo.get(wid) or {}, rows.get(wid) or {}
    ok = bool(g.get("available")) and s.get("rebuild") is False
    why = "" if ok else f"：派生缓存 {g.get('reason') or '无'}/{g.get('detail') or '-'}，世界状态 {s.get('status')}/{s.get('reason')}"
    tag = "可用" if ok else ("不可用" if wid in packs else "未部署")
    print(f"    [{wid}] {tag} derive={g.get('derive_sha8') or '-'} cache={(g.get('cache_bytes') or 0) / 2**20:.0f} MiB{why}")
    fail |= (not ok) and wid in packs
sys.exit(1 if fail else 0)
PY
note "未部署或不可用的世界在沙盒世界列表中标为 available: false（507 cache_missing），共享展示的静态浏览同样 404"

step "配置 $ETC/env"
if [ ! -f "$ETC/env" ]; then
  install -o root -g root -m 0600 "$DEST/tools/deploy/emax/env.example" "$ETC/env"
  note "已由 env.example 生成；以后的安装不会覆盖它"
else
  chmod 0600 "$ETC/env"
  if ! diff -q <(grep -E '^[A-Z_]+=' "$DEST/tools/deploy/emax/env.example" | cut -d= -f1 | sort) \
               <(grep -E '^#?[A-Z_]+=' "$ETC/env" | tr -d '#' | cut -d= -f1 | sort -u) >/dev/null; then
    note "提示：env.example 有新增或删除的键，对照后手工合并：diff $DEST/tools/deploy/emax/env.example $ETC/env"
  fi
  WA=$(env_get AWR_WORLDS_ALLOW)
  if [ -n "$WA" ] && [ "$(echo "$WA" | tr ',' '\n' | grep -c .)" -lt 7 ]; then
    note "警告：$ETC/env 设置了 AWR_WORLDS_ALLOW=$WA（D1 只开放 synthcity）；ADR-109 起七个世界全部开放，请删除该行" >&2
  fi
fi
if [ "$ADMIN_SECRET" -eq 1 ]; then
  if [ ! -s "$ETC/admin.secret" ]; then
    umask 077
    head -c 24 /dev/urandom | base64 | tr -d '/+=\n' > "$ETC/admin.secret"
    chown "$SVC_USER:$SVC_USER" "$ETC/admin.secret"
    chmod 0600 "$ETC/admin.secret"
    note "已生成管理口令 $ETC/admin.secret（不打印；查看：sudo cat $ETC/admin.secret）"
  fi
  set_env_kv AWR_ADMIN_SECRET_FILE "$ETC/admin.secret"
fi
if [ -n "$SECRET" ]; then write_secret; fi
P=$(env_get AWR_PORT); PORT=${P:-18640}
P=$(env_get AWR_SANDBOX_PORT); SB_PORT=${P:-18641}

step "systemd 单元、配额与切换"
if [ "$RESET_LIMITS" -eq 1 ]; then
  rm -f "$DROPIN_DIR/limits.conf"
  rmdir "$DROPIN_DIR" 2>/dev/null || true
  note "已删除 drop-in：回到单元文件的 CPUQuota 250%、MemoryHigh 1.8G"
fi
if [ -n "$CPU_QUOTA$MEM_HIGH" ]; then
  install -d -m 0755 "$DROPIN_DIR"
  {
    echo "# install.sh --cpu-quota/--memory-high 写入（Q-D2-01、Q-D2-09）；--reset-limits 删除"
    echo "[Service]"
    [ -z "$CPU_QUOTA" ] || echo "CPUQuota=$CPU_QUOTA"
    [ -z "$MEM_HIGH" ] || echo "MemoryHigh=$MEM_HIGH"
  } > "$DROPIN_DIR/limits.conf"
  chmod 0644 "$DROPIN_DIR/limits.conf"
  note "drop-in $DROPIN_DIR/limits.conf：${CPU_QUOTA:+CPUQuota=$CPU_QUOTA }${MEM_HIGH:+MemoryHigh=$MEM_HIGH}（cgroup-exec.sh 与池按实际值重算）"
fi
[ "$(stat -fc %T /sys/fs/cgroup 2>/dev/null || echo unknown)" = cgroup2fs ] \
  || note "警告：/sys/fs/cgroup 不是 cgroup v2，Delegate 不生效；沙盒以 N_max − 1 运行" >&2
PREV=$(current_release)
install -m 0644 "$DEST/tools/deploy/emax/anet-drone4d.service" "/etc/systemd/system/$SVC.service"
switch_to "$REL"
note "current：$PREV → $REL"
if ! restart_service; then
  if [ "$PREV" != none ] && [ "$PREV" != "$REL" ] && [ -d "$PREFIX/releases/$PREV" ]; then
    echo "    新发布未就绪，自动回滚到 $PREV" >&2
    switch_to "$PREV"
    install -m 0644 "$PREFIX/releases/$PREV/tools/deploy/emax/anet-drone4d.service" "/etc/systemd/system/$SVC.service"
    systemctl daemon-reload
    systemctl restart "$SVC"
    health || true
  fi
  die "新发布 $REL 启动失败（journalctl -u $SVC -n 200）"
fi

SMOKE_RC=0
if [ "$RESTART" -eq 1 ]; then
  step "cgroup 委派"
  cgroup_report
  if sandbox_enabled && [ "$SMOKE" -eq 1 ]; then
    step "沙盒冒烟（回环 127.0.0.1:$SB_PORT：创建 → READY → 结束）"
    set +e
    SMOKE_OUT=$("$DEST/.venv/bin/python" "$DEST/tools/deploy/emax/smoke.py" --base "http://127.0.0.1:$SB_PORT" --host "$DOMAIN" 2>&1)
    SMOKE_RC=$?
    set -e
    printf '%s\n' "$SMOKE_OUT" | sed 's/^/    /'
  fi
fi

step "清理旧发布（保留最近 $KEEP 个）"
mapfile -t ALL < <(releases)
N=${#ALL[@]}
if [ "$N" -gt "$KEEP" ]; then
  for r in "${ALL[@]:0:$((N - KEEP))}"; do
    [ "$r" = "$REL" ] || [ "$r" = "$PREV" ] || { rm -rf "${PREFIX:?}/releases/$r"; note "删除 $r"; }
  done
fi

cat <<EOF

完成：$SVC 运行发布 $REL（V${VERSION:-?}），show-api 只监听 127.0.0.1:$PORT，sandbox-api 只监听 127.0.0.1:$SB_PORT。
以下 nginx 与证书步骤本脚本不会执行，请人工逐条执行（只新增本站点，不修改其他站点；详见 README.md）：

  # 首次部署（尚无证书）：先启用只含 80 端口的引导站点，申请 ECDSA 证书
  sudo install -m 0644 $DEST/tools/deploy/emax/drone4d.agentnetwork.org.cn.bootstrap.conf /etc/nginx/sites-available/$DOMAIN.conf
  sudo ln -sfn /etc/nginx/sites-available/$DOMAIN.conf /etc/nginx/sites-enabled/$DOMAIN.conf
  sudo nginx -t && sudo systemctl reload nginx
  sudo certbot certonly --webroot -w /var/www/certbot -d $DOMAIN --key-type ecdsa --elliptic-curve secp384r1

  # 证书就绪后（以及站点文件有变化时，例如从 V0.1 升级到 V0.2-demo）：换成正式站点文件
  sudo diff /etc/nginx/sites-available/$DOMAIN.conf $DEST/tools/deploy/emax/drone4d.agentnetwork.org.cn.conf
  sudo install -m 0644 $DEST/tools/deploy/emax/drone4d.agentnetwork.org.cn.conf /etc/nginx/sites-available/$DOMAIN.conf
  sudo nginx -t && sudo systemctl reload nginx

  # 验证（README.md 第 2.4 节有完整清单）
  curl -sI https://$DOMAIN/ | head -1
  curl -s https://$DOMAIN/build-info.json
  curl -s https://$DOMAIN/api/sandbox/v1/capacity
  curl -s -o /dev/null -w '%{http_code}\n' https://$DOMAIN/worlds/shenzhen/world.json    # 200（ADR-109）
EOF
case "$SMOKE_RC" in
  0) ;;
  3) echo; echo "注意：沙盒冒烟跳过（见上文原因）；可稍后执行 sudo -u $SVC_USER $DEST/.venv/bin/python $DEST/tools/deploy/emax/smoke.py --host $DOMAIN" ;;
  *) echo; echo "警告：沙盒冒烟失败（共享展示已正常运行）：journalctl -u $SVC -n 200；ls $PREFIX/var/sandbox/runs/" >&2; exit 3 ;;
esac
