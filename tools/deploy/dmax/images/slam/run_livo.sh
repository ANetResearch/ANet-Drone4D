#!/usr/bin/env bash
# d3-livo 容器入口（M27-FR-101、FR-105；r1 §3.1 run_livo_bag.sh 的产品化版本）：启动 FAST-LIVO2（无 rviz），按倍速回放 bag，
# 采样节点 RSS 与 CPU，等待处理队列排空，SIGINT 触发存图，写出 status.json。
# 环境变量：CFG CAM BAG（必填）、RATE（缺省 4）、START、DURATION、TOPICS、COMPRESSED（缺省 true）、OUT（缺省 /run）、
#           QUIET_S（日志静默多少秒视为排空，缺省 10）、DRAIN_MAX_S（缺省 900）、SAVE_MAX_S（缺省 900）。
# 退出码：0 正常且轨迹非空；3 节点未启动；4 回放期间节点退出；5 轨迹为空；6 存图超时。作业层把非零退出、OOM 记为 684。
source /ws/devel/setup.bash                   # ROS 环境脚本引用未定义变量，须在 set -u 之前加载
set -u
# --network none 下容器主机名不可解析，roscore 与节点一律走回环
export ROS_HOSTNAME=localhost ROS_MASTER_URI=http://localhost:11311
OUT=${OUT:-/run}
LOG=/ws/src/FAST-LIVO2/Log
mkdir -p "$OUT" $LOG/pcd $LOG/result $LOG/image $LOG/Colmap/sparse/0 $LOG/Colmap/images
CLK_TCK=$(getconf CLK_TCK)
now() { date +%s.%N; }
# 节点存活（不含僵尸：节点被转交给本脚本（PID 1）后，退出到被回收之前 kill -0 仍成功）
alive() { [ -r "/proc/$1/status" ] && ! grep -q '^State:[[:space:]]*Z' "/proc/$1/status"; }
oom_kills() { awk '$1 == "oom_kill" {print $2}' /sys/fs/cgroup/memory.events 2>/dev/null || echo 0; }
cgroup_peak() {
  for f in /sys/fs/cgroup/memory.peak /sys/fs/cgroup/memory/memory.max_usage_in_bytes; do   # cgroup v2 / v1；均不可读时为 0
    [ -r "$f" ] && { cat "$f"; return; }
  done
  echo 0
}
write_status() {   # $1 exit_code  $2 why
  python3 - "$OUT/status.json" "$1" "$2" <<PY
import json, sys, os
p, code, why = sys.argv[1], int(sys.argv[2]), sys.argv[3]
res = "$LOG/result"
trajs = [f for f in os.listdir(res) if f.endswith(".txt")] if os.path.isdir(res) else []
n = 0
for f in trajs:
    with open(os.path.join(res, f)) as fh:
        n += sum(1 for ln in fh if ln.strip())
f = lambda v: float(v) if v not in ("", None) else None
json.dump({"exit_code": code, "why": why or None, "node_started": "${NPID:-}" != "",
           "play_t0": f("${T0:-}"), "play_t1": f("${T1:-}"), "play_s": (f("${T1:-}") - f("${T0:-}")) if "${T1:-}" and "${T0:-}" else None,
           "drain_s": (f("${T2:-}") - f("${T1:-}")) if "${T2:-}" and "${T1:-}" else None,
           "save_s": (f("${T3:-}") - f("${T2:-}")) if "${T3:-}" and "${T2:-}" else None,
           "cpu_ticks": int("${CPU_TICKS:-0}") or None, "clk_tck": int("$CLK_TCK"), "cgroup_mem_peak_bytes": int("$(cgroup_peak)" or 0),
           "oom_kill_events": int("$(oom_kills)" or 0),
           "rate": float("${RATE:-4}"), "start_s": f("${START:-}"), "duration_s": f("${DURATION:-}"),
           "trajectory_files": trajs, "trajectory_poses": n, "result_ok": n > 0 and code == 0,
           "pcd": sorted(os.listdir("$LOG/pcd")) if os.path.isdir("$LOG/pcd") else []}, open(p, "w"), indent=1)
PY
}
finish() { write_status "$1" "$2"; exit "$1"; }

roslaunch fast_livo run_bag.launch cfg:="$CFG" cam:="$CAM" compressed:="${COMPRESSED:-true}" > "$OUT/livo.log" 2>&1 &
LPID=$!
NPID=""
for i in $(seq 1 120); do NPID=$(pidof fastlivo_mapping || true); [ -n "$NPID" ] && break; sleep 0.5; done
[ -z "$NPID" ] && { kill -INT $LPID 2>/dev/null; wait $LPID; finish 3 node_not_started; }
sleep 3
echo "t_wall rss_kb cpu_pct" > "$OUT/res.txt"
( while alive "$NPID"; do ps -o rss=,pcpu= -p "$NPID" | awk -v t="$(now)" '{print t, $1, $2}' >> "$OUT/res.txt"; sleep 2; done ) &
T0=$(now)
rosbag play -q ${START:+-s $START} ${DURATION:+-u $DURATION} -r "${RATE:-4}" "$BAG" ${TOPICS:+--topics $TOPICS} > "$OUT/play.log" 2>&1
T1=$(now)
alive "$NPID" || { wait $LPID; finish 4 node_exited_during_play; }
# 排空：日志 QUIET_S 秒内不再增长视为处理完毕（倍速回放时队列在回放结束后继续处理）
prev=-1; quiet=0; waited=0
while [ "$waited" -lt "${DRAIN_MAX_S:-900}" ]; do
  cur=$(stat -c %s "$OUT/livo.log")
  if [ "$cur" = "$prev" ]; then quiet=$((quiet + 1)); else quiet=0; fi
  [ "$quiet" -ge "${QUIET_S:-10}" ] && break
  prev=$cur; sleep 1; waited=$((waited + 1))
done
CPU_TICKS=$(awk '{print $14 + $15}' /proc/$NPID/stat 2>/dev/null || echo 0)
T2=$(now)
kill -INT $LPID
( sleep "${SAVE_MAX_S:-900}"; kill -KILL $LPID 2>/dev/null ) & KPID=$!
wait $LPID
for i in $(seq 1 "${SAVE_MAX_S:-900}"); do alive "$NPID" || break; sleep 1; done
T3=$(now)
kill $KPID 2>/dev/null
alive "$NPID" && finish 6 save_timeout
N=$(cat $LOG/result/*.txt 2>/dev/null | grep -c . || true)
[ "${N:-0}" -gt 0 ] || finish 5 empty_trajectory
finish 0 ""
