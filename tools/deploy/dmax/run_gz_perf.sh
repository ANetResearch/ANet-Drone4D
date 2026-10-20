#!/usr/bin/env bash
# 在 dmax CPU 池测量 gz `physics` 实例的性能条款（D3-AC-34；ADR-195 决策 (6)：不挂 GPU，工具持主机排他锁 $D/.perf.lock）。
# 用法（本机仓库根目录）：tools/deploy/dmax/run_gz_perf.sh <输出名> [阶段...]；阶段缺省为 startup gzcpu，日志在 $D/perf/<输出名>/logs/。
#   startup：gz_lockstep.py startup --n ${N:-40}（每次新容器冷启动至 HelloAck，不推进仿真）；
#   gzcpu：  gz_lockstep.py run --kinds physics --repeats 3（3 机 x500 360 s，约 12 min）；同一输出目录的汇总并入 startup 样本。
# 代码以快照同步到 $D/perf/<输出名>/src（不影响其他任务的目录）；宿主进程在 d3-sensorsvc 容器内，gz 为兄弟容器（经挂载的 docker 套接字），
# 容器名前缀 d3-gzperf-；世界与事件取 $D/worlds、$D/d3data/events。前台运行，结束后删除残留容器。
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
D=/data/anet-drone4d-d3/d3dev
NAME=${1:?output name}; shift
STAGES=${*:-startup gzcpu}
K=$D/perf/$NAME
SRC=$K/src
IMG=${IMG:-d3-sensorsvc:dev}
ssh dmax "mkdir -p $SRC/tools $SRC/native/gz $SRC/packages $K/logs"
rsync -a --delete --exclude __pycache__ "$ROOT/python" "$ROOT/configs" "$ROOT/vehicles" "$ROOT/pyproject.toml" "dmax:$SRC/"
rsync -a --delete --exclude __pycache__ "$ROOT/tools/bench" "$ROOT/tools/sdk" "dmax:$SRC/tools/"
rsync -a --delete "$ROOT/native/gz/models" "dmax:$SRC/native/gz/"
rsync -a --delete "$ROOT/packages/contracts" "dmax:$SRC/packages/"
ssh dmax "bash -s" <<EOF
set -uo pipefail
mkdir -p $SRC/var/perf
COMMON=(--network none --user 0:0 -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1
  -e AWR_CONTRACTS_DIR=/repo/packages/contracts -e "AWR_HOST_PATH_MAP=/repo/worlds=$D/worlds,/repo/d3data=$D/d3data,/repo=$SRC"
  -e AWR_WORLDS_DIR=/repo/worlds -e AWR_GEO_CACHE_DIR=/repo/var/perf/geo-cache -e AWR_D3_EVENTS_ROOT=/repo/d3data/events
  -e PYTHONPATH=/repo/python -e OMP_NUM_THREADS=8 -e OPENBLAS_NUM_THREADS=8
  -v "$SRC:/repo" -v "$D/worlds:/repo/worlds:ro" -v "$D/d3data:/repo/d3data:ro" -v "$D/vehicles:/opt/awr/vehicles:ro"
  -v /sys/fs/cgroup:/host/sys/fs/cgroup:ro -v /var/run/docker.sock:/var/run/docker.sock -v /usr/bin/docker:/usr/bin/docker:ro
  -v "$D/.perf.lock:/lock/.perf.lock" -w /repo)
TOOL=(python3 tools/bench/d3/gz_lockstep.py)
ARGS=(--worlds-root worlds --events-root d3data/events --out var/perf/$NAME --host-cgroup-root /host/sys/fs/cgroup
  --lock /lock/.perf.lock --load-max ${LOADMAX:-32})
clean() { docker ps -a --format '{{.Names}}' | grep '^d3-gzperf-' | xargs -r docker rm -f >/dev/null; }
clean
for st in $STAGES; do
  case \$st in
    startup) docker run --rm --name d3-gzperf-host "\${COMMON[@]}" --cpus 16 --memory 32g $IMG "\${TOOL[@]}" startup "\${ARGS[@]}" \
               --name-prefix d3-gzperf-st --n ${N:-40} --gap-s 1 > $K/logs/startup.log 2>&1 ;;
    gzcpu)   docker run --rm --name d3-gzperf-host "\${COMMON[@]}" --cpus 16 --memory 48g $IMG "\${TOOL[@]}" run "\${ARGS[@]}" \
               --name-prefix d3-gzperf-gc --kinds physics --repeats 3 > $K/logs/gzcpu.log 2>&1 ;;
    *) echo "unknown stage \$st" >&2; clean; exit 2 ;;
  esac
  rc=\$?
  echo "[gz-perf] \$(date -u +%FT%TZ) \$st rc=\$rc" | tee -a $K/logs/run.log
  clean
done
EOF
