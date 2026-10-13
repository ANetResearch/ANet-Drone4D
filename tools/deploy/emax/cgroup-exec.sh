#!/usr/bin/env bash
# tools/deploy/emax/cgroup-exec.sh：systemd 单元 anet-drone4d 的 ExecStart 包装（AWR-04 §4.4.4 第 1、3 条；M17-FR-063；
# ADR-091）。用法：cgroup-exec.sh <命令> [参数…]，最后总是 exec 该命令（缺省是 supervisor）。
#
# 单元以 Delegate=yes 运行时，在单元的 cgroup（"单元根"，cgroup v2）下建立两组布局并写入限值：
#   core/             supervisor、show-api、共享展示 sim-core 与规划进程（本脚本把自身移入，子进程继承）
#                     cpu.weight 400、memory.min 450M（不设 cpu.max）
#   sandbox/          无进程；cpu.max = 0.9·Q − C_core（Q 为单元 CPUQuota 的核数，250% 时 1.36 核）、cpu.weight 100、
#                     memory.high 1300M、memory.max 1450M（单元 MemoryHigh 1.8G 时；1.6G 时 1150M、1300M）
#   sandbox/ctl/      sandbox-pool、sandbox-api（启动时按 AWR_CGROUP=sandbox/ctl 自行加入）：cpu.weight 300、memory.min 150M
#   sandbox/plan/     共享规划 worker：cpu.max 0.5 核、cpu.weight 50、memory.max 250M（池启动时按同一值复写）
#   sandbox/sb/       每会话叶子组 sandbox/sb/<sid>/ 的父组（池建立与删除叶子组）：cpu.max = sandbox/cpu.max − C_ctl
#                     （1.14 核；D2-FX-R2，会话超预算时在本组限流，控制面不随 sandbox/ 整组停顿）
# 单元根、sandbox/、sandbox/sb/ 的 cgroup.subtree_control 启用 +cpu +memory（单元根须先无进程：cgroup v2 "无内部进程"）。
# 另外导出 AWR_SANDBOX_MEM_MB（池的世界加权准入额度 M_sb = sandbox/memory.high − 控制面 200 MB，AWR-04 §4.4.2），环境文件
# 已设置时不覆盖。
#
# 无委派（单元根不可写）、非 cgroup v2、AWR_CGROUP_DISABLE=1 时跳过布局，照常 exec（池按 N_max − 1、nice +5 与 RSS 看门狗
# 兜底，M17-FR-026）。任何一步失败都不阻止服务启动；结论以一行 JSON（与 supervisor 同格式）写到 stderr（journal）。
#
# 环境变量（均可在 /etc/anet-drone4d/env 中设置，一般不需要）：
#   AWR_CGROUP_FS（缺省 /sys/fs/cgroup）、AWR_CGROUP_ROOT（单元根，缺省取 /proc/self/cgroup 的 v2 行）、AWR_CGROUP_DISABLE=1、
#   AWR_CGROUP_SIMULATE=1（测试：普通目录充当 cgroupfs，控制文件按普通文件创建，与 awr.runtime.cgroup 同义）、
#   AWR_CG_C_CORE（0.89 核）、AWR_CG_SANDBOX_CPU_MAX（核）、AWR_CG_SANDBOX_MEM_HIGH、AWR_CG_SANDBOX_MEM_MAX（如 1300M）、
#   AWR_CG_CORE_MEM_MIN（450M）、AWR_CG_CTL_MEM_MIN（150M）、AWR_CG_PLAN_CPU_MAX（0.5 核）、AWR_CG_PLAN_MEM_MAX（250M）、
#   AWR_CG_CTL_PLANE_MB（200）、AWR_CG_CTL_CPU_RESERVE（0.22 核，sandbox/sb/ 的 cpu.max 为 sandbox/cpu.max 减去该值）。
set -u

[ $# -ge 1 ] || { echo "用法：cgroup-exec.sh <命令> [参数…]" >&2; exit 2; }

FS=${AWR_CGROUP_FS:-/sys/fs/cgroup}
SIM=${AWR_CGROUP_SIMULATE:-0}
PERIOD=100000
WARN=()

jstr() {  # JSON 字符串转义（只含本脚本自己生成的 ASCII 与路径）
  local s=${1//\\/\\\\}
  s=${s//\"/\\\"}
  printf '"%s"' "$s"
}

emit() {  # emit <lvl> <msg> <kv-json>
  local now
  now=$(date +%s%N 2>/dev/null || echo 0)
  printf '{"t_wall_ns": %s, "lvl": "%s", "proc": "cgroup-exec", "pid": %s, "logger": "awr.deploy.cgroup", "msg": "%s", "kv": %s}\n' \
    "$now" "$1" "$$" "$2" "$3" >&2
}

undelegated() {  # undelegated <原因> <命令…>：跳过布局并照常启动（已算出沙盒组额度时同样导出 AWR_SANDBOX_MEM_MB）
  local why=$1
  shift
  if [ -z "${AWR_SANDBOX_MEM_MB:-}" ] && [ -n "${SB_HIGH_MB:-}" ]; then
    AWR_SANDBOX_MEM_MB=$(( SB_HIGH_MB - ${AWR_CG_CTL_PLANE_MB:-200} ))
    export AWR_SANDBOX_MEM_MB
  fi
  emit WARN "cgroup.undelegated" "{\"why\": $(jstr "$why"), \"root\": $(jstr "${ROOT:-}"), \"sandbox_mem_mb\": ${AWR_SANDBOX_MEM_MB:-null}, \"fallback\": \"N_max - 1, nice +5, RSS watchdog\"}"
  exec "$@"
}

to_bytes() {  # 1300M、1.8G、123456、max → 字节数（max 原样）
  local v=$1
  case "$v" in
    max|"") echo "$v"; return ;;
  esac
  awk -v v="$v" 'BEGIN {
    n = v + 0; u = toupper(substr(v, length(v), 1)); m = 1
    if (u == "K") m = 1024; else if (u == "M") m = 1048576; else if (u == "G") m = 1073741824; else if (u == "T") m = 1099511627776
    printf "%.0f\n", n * m }'
}

wr() {  # wr <相对单元根的文件> <值>：写控制文件；失败记入 WARN（不中止）
  local f="$ROOT/$1"
  if [ "$SIM" = 1 ] && [ ! -e "$f" ]; then : > "$f" 2>/dev/null || true; fi
  if ! { printf '%s' "$2" > "$f"; } 2>/dev/null; then
    WARN+=("$1")
    return 1
  fi
  return 0
}

rd() {  # rd <相对单元根的文件>：读控制文件首行（不存在时空）
  local f="$ROOT/$1"
  [ -r "$f" ] && head -n 1 "$f" 2>/dev/null || true
}

# ---- 单元根 ---------------------------------------------------------------------------------------------------------
ROOT=""
if [ "${AWR_CGROUP_DISABLE:-0}" = 1 ]; then
  undelegated "AWR_CGROUP_DISABLE=1" "$@"
fi
if [ -n "${AWR_CGROUP_ROOT:-}" ]; then
  case "$AWR_CGROUP_ROOT" in
    "$FS"|"$FS"/*) ROOT=$AWR_CGROUP_ROOT ;;
    *) ROOT="$FS/${AWR_CGROUP_ROOT#/}" ;;
  esac
else
  CUR=$(sed -n 's/^0:://p' /proc/self/cgroup 2>/dev/null | head -n 1)
  [ -n "$CUR" ] || undelegated "no cgroup v2 entry in /proc/self/cgroup (cgroup v1 or hybrid)" "$@"
  ROOT="$FS${CUR%/}"
  case "$ROOT" in */core) ROOT=${ROOT%/core} ;; esac   # 重复执行（已在 core/ 中）时回到单元根
fi
ROOT=${ROOT%/}
if [ "$SIM" != 1 ]; then
  FST=$(stat -fc %T "$FS" 2>/dev/null || echo unknown)
  [ "$FST" = cgroup2fs ] || undelegated "$FS is $FST, not cgroup2fs" "$@"
fi
[ -d "$ROOT" ] || undelegated "unit cgroup $ROOT does not exist" "$@"

# ---- 单元的配额：Q（核）与 MemoryHigh ---------------------------------------------------------------------------------
CPU_MAX=$(rd cpu.max)
Q=""
if [ -n "$CPU_MAX" ] && [ "${CPU_MAX%% *}" != max ]; then
  Q=$(awk -v s="$CPU_MAX" 'BEGIN { split(s, a, " "); p = (a[2] > 0) ? a[2] : 100000; printf "%.3f\n", a[1] / p }')
fi
Q_EFF=${Q:-${AWR_SANDBOX_CPU_QUOTA:-2.0}}
MH=$(rd memory.high)
[ -n "$MH" ] && [ "$MH" != max ] || MH=$(rd memory.max)
[ -n "$MH" ] && [ "$MH" != max ] || MH=$(to_bytes 1.8G)
MH_MB=$(awk -v b="$MH" 'BEGIN { printf "%d\n", b / 1048576 }')

C_CORE=${AWR_CG_C_CORE:-0.89}
if [ -n "${AWR_CG_SANDBOX_CPU_MAX:-}" ]; then
  SB_CORES=$AWR_CG_SANDBOX_CPU_MAX
else
  SB_CORES=$(awk -v q="$Q_EFF" -v c="$C_CORE" 'BEGIN { v = 0.9 * q - c; if (v < 0.2) v = 0.2; printf "%.2f\n", v }')
fi
SB_QUOTA=$(awk -v c="$SB_CORES" -v p="$PERIOD" 'BEGIN { printf "%d\n", c * p + 0.5 }')
# 沙盒组内存：MemoryHigh ≥ 1.8G 时 1300M/1450M，≥ 1.6G 时 1150M/1300M（M17 PRD §8.3），更小时为 MemoryHigh − 500M（≥ 256M）
# 与其 + 150M。档位按 1800 MiB 与 1600 MiB 判定：systemd 的 1.8G 是 1843 MiB，cgroup 模拟与运维手写的"1800M"同样指 AWR-04 的
# "MemoryHigh 1.8 GB"；D2-ACC-1 中 1800M 被归入 1.6G 档（M_sb 950），世界准入因此偏紧（D2-FX-R2，D2-AC-04 变体 B）
if [ -n "${AWR_CG_SANDBOX_MEM_HIGH:-}" ]; then
  SB_HIGH_MB=$(awk -v b="$(to_bytes "$AWR_CG_SANDBOX_MEM_HIGH")" 'BEGIN { printf "%d\n", b / 1048576 }')
elif [ "$MH_MB" -ge 1800 ]; then SB_HIGH_MB=1300
elif [ "$MH_MB" -ge 1600 ]; then SB_HIGH_MB=1150
else SB_HIGH_MB=$(( MH_MB - 500 )); [ "$SB_HIGH_MB" -ge 256 ] || SB_HIGH_MB=256
fi
if [ -n "${AWR_CG_SANDBOX_MEM_MAX:-}" ]; then
  SB_MAX_MB=$(awk -v b="$(to_bytes "$AWR_CG_SANDBOX_MEM_MAX")" 'BEGIN { printf "%d\n", b / 1048576 }')
else
  SB_MAX_MB=$(( SB_HIGH_MB + 150 ))
fi
CTL_PLANE_MB=${AWR_CG_CTL_PLANE_MB:-200}
PLAN_QUOTA=$(awk -v c="${AWR_CG_PLAN_CPU_MAX:-0.5}" -v p="$PERIOD" 'BEGIN { printf "%d\n", c * p + 0.5 }')
# 会话父组 sandbox/sb/ 的 cpu.max = sandbox/cpu.max − C_ctl（0.22 核，下限 0.2 核）：会话超出预算时先在 sb/ 内限流，控制面
# （sandbox-pool、sandbox-api）不随整个 sandbox/ 组的带宽耗尽而停顿（D2-FX-R2：D2-ACC-1 变体 A 的 sandbox-api 事件循环 p99 110 ms
# 来自 sandbox/ 组整体限流）。池启动时按同一公式复写（awr.sandbox.cgroups）
SB_SESS_CORES=$(awk -v c="$SB_CORES" -v r="${AWR_CG_CTL_CPU_RESERVE:-0.22}" 'BEGIN { v = c - r; if (v < 0.2) v = 0.2; printf "%.2f\n", v }')
SB_SESS_QUOTA=$(awk -v c="$SB_SESS_CORES" -v p="$PERIOD" 'BEGIN { printf "%d\n", c * p + 0.5 }')

[ -w "$ROOT" ] && [ -w "$ROOT/cgroup.procs" ] && [ -w "$ROOT/cgroup.subtree_control" ] \
  || undelegated "unit cgroup is not writable (Delegate=yes missing or systemd too old)" "$@"

# ---- 布局：先建组、把自身移入 core/，再启用控制器（单元根此时已无进程） -------------------------------------------------
for d in core sandbox sandbox/ctl sandbox/plan sandbox/sb; do
  mkdir -p "$ROOT/$d" 2>/dev/null || undelegated "cannot create $d (no delegation)" "$@"
done
if [ "$SIM" = 1 ]; then
  printf '%s\n' "$$" >> "$ROOT/core/cgroup.procs"
else
  # 单元根中可能还有本脚本之外的进程（ExecStartPre 残留等）：一并移入 core/
  while read -r pid; do
    [ -n "$pid" ] || continue
    printf '%s' "$pid" > "$ROOT/core/cgroup.procs" 2>/dev/null || true
  done < "$ROOT/cgroup.procs"
  printf '%s' "$$" > "$ROOT/core/cgroup.procs" 2>/dev/null || undelegated "cannot move self into core/" "$@"
fi

AVAIL=$(rd cgroup.controllers)
[ "$SIM" = 1 ] && [ -z "$AVAIL" ] && AVAIL="cpu memory"
ENABLED=()
for c in cpu memory; do
  case " $AVAIL " in
    *" $c "*) ;;
    *) WARN+=("controller $c not available"); continue ;;
  esac
  if wr cgroup.subtree_control "+$c"; then
    ENABLED+=("$c")
    wr sandbox/cgroup.subtree_control "+$c" || true
    wr sandbox/sb/cgroup.subtree_control "+$c" || true
  fi
done
if [ "$SIM" = 1 ]; then  # 模拟：subtree_control 记录全部已启用控制器
  printf '%s' "${ENABLED[*]/#/+}" > "$ROOT/cgroup.subtree_control"
  printf '%s' "${ENABLED[*]/#/+}" > "$ROOT/sandbox/cgroup.subtree_control"
  printf '%s' "${ENABLED[*]/#/+}" > "$ROOT/sandbox/sb/cgroup.subtree_control"
fi

has() { case " ${ENABLED[*]} " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }
if has cpu; then
  wr core/cpu.weight 400
  wr sandbox/cpu.max "$SB_QUOTA $PERIOD"
  wr sandbox/cpu.weight 100
  wr sandbox/ctl/cpu.weight 300
  wr sandbox/plan/cpu.max "$PLAN_QUOTA $PERIOD"
  wr sandbox/plan/cpu.weight 50
  wr sandbox/sb/cpu.max "$SB_SESS_QUOTA $PERIOD"
fi
if has memory; then
  wr core/memory.min "$(to_bytes "${AWR_CG_CORE_MEM_MIN:-450M}")"
  wr sandbox/memory.high "$(( SB_HIGH_MB * 1048576 ))"
  wr sandbox/memory.max "$(( SB_MAX_MB * 1048576 ))"
  wr sandbox/ctl/memory.min "$(to_bytes "${AWR_CG_CTL_MEM_MIN:-150M}")"
  wr sandbox/plan/memory.max "$(to_bytes "${AWR_CG_PLAN_MEM_MAX:-250M}")"
fi

if [ -z "${AWR_SANDBOX_MEM_MB:-}" ]; then
  AWR_SANDBOX_MEM_MB=$(( SB_HIGH_MB - CTL_PLANE_MB ))
  export AWR_SANDBOX_MEM_MB
fi

W=""
for w in "${WARN[@]+"${WARN[@]}"}"; do W="$W${W:+, }$(jstr "$w")"; done
LVL=INFO
[ ${#WARN[@]} -eq 0 ] || LVL=WARN
emit "$LVL" "cgroup.layout" "{\"root\": $(jstr "$ROOT"), \"delegated\": true, \"controllers\": $(jstr "${ENABLED[*]}"), \"q_cores\": ${Q:-null}, \"q_effective\": $Q_EFF, \"sandbox_cpu_max_cores\": $SB_CORES, \"sessions_cpu_max_cores\": $SB_SESS_CORES, \"sandbox_memory_high_mb\": $SB_HIGH_MB, \"sandbox_memory_max_mb\": $SB_MAX_MB, \"unit_memory_high_mb\": $MH_MB, \"sandbox_mem_mb\": $AWR_SANDBOX_MEM_MB, \"failed_writes\": [$W]}"
exec "$@"
