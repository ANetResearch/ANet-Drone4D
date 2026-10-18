#!/usr/bin/env bash
# 在 dmax 上运行 P0a 原型榜（tools/bench/d3/proto_board.py，Realistic 档）。L1 缺省在 d3-gz 容器内运行（宿主与 gz 同容器，launcher = local）。
# 用法（本机仓库根目录）：tools/deploy/dmax/run_proto_board.sh <输出名> [proto_board.py 参数]；后台运行，日志在 $D/out/<输出名>/board.log。
# 前提：tools/deploy/dmax/images/gz/build.sh 已构建镜像；$D/worlds/<world> 与 $D/d3data/events/<event>/v<N> 已就位；
# 宿主侧依赖在 $D/pylib-gz（离线 wheel：pyzmq、protobuf、jsonschema、msgpack、scipy、open3d-cpu 及其依赖）。
# `--agents container` 时运行器经挂载的 docker 套接字为每架机启动隔离 Agent 容器（镜像 d3-agent-py，images/agent/build.sh），
# 套接字目录按 AWR_HOST_PATH_MAP 换算为宿主路径。
# L2（相机在环）：`GPU=2 SPEC=configs/bench.d3/crop-handoff-001.l2-3uav.run_spec.json` 以 d3-sensorsvc 镜像（EGL）为宿主、绑定该卡，
# gz 改为兄弟容器（--gz-launcher docker）。dmax 只用 GPU 2、3。
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
D=/data/anet-drone4d-d3/d3dev
NAME=${1:?output name}; shift
CPUS=${CPUS:-12}
SPEC=${SPEC:-configs/bench.d3/crop-handoff-001.l1-3uav.run_spec.json}
GPU=${GPU:-}
case "$GPU" in ""|2|3) ;; *) echo "GPU must be 2 or 3 on dmax" >&2; exit 2;; esac
rsync -a --delete --exclude __pycache__ "$ROOT/python/" "dmax:$D/python/"
rsync -a --mkpath --delete --exclude __pycache__ "$ROOT/tools/sdk/drone4d/src/" "dmax:$D/tools/sdk/drone4d/src/"
rsync -a --mkpath --delete --exclude __pycache__ "$ROOT/tools/bench/d3/" "dmax:$D/tools/bench/d3/"
rsync -a --mkpath --delete "$ROOT/configs/bench.d3/" "dmax:$D/configs/bench.d3/"
rsync -a --mkpath --delete "$ROOT/packages/contracts/" "dmax:$D/contracts/"
rsync -a --mkpath "$ROOT/native/gz/proto/" "dmax:$D/native/gz/proto/"
rsync -a --mkpath "$ROOT/vehicles/sensor_models/" "dmax:$D/vehicles/sensor_models/"
if [ -n "$GPU" ]; then
  HOST="--gpus device=$GPU -e NVIDIA_DRIVER_CAPABILITIES=all -e OMP_NUM_THREADS=8 -e OPENBLAS_NUM_THREADS=8 -e PYTHONPATH=/repo/python d3-sensorsvc:dev"
  LAUNCH="--gz-launcher docker"
else
  HOST="-e PYTHONPATH=/repo/pylib-gz:/repo/python d3-gz:dev"
  LAUNCH="--gz-launcher local"
fi
ssh dmax "mkdir -p $D/out/$NAME && docker run -d --rm --name d3-board-$NAME --network none --cpus $CPUS --user \$(id -u):\$(id -g) -e HOME=/tmp \
  -e AWR_CONTRACTS_DIR=/repo/contracts -e AWR_HOST_PATH_MAP=/repo=$D \
  -v /var/run/docker.sock:/var/run/docker.sock -v /usr/bin/docker:/usr/bin/docker:ro -v $D:/repo -w /repo $HOST \
  sh -c 'python3 tools/bench/d3/proto_board.py --spec $SPEC \
    --events-root d3data/events --worlds-root worlds --out out/$NAME $LAUNCH $* > out/$NAME/board.log 2>&1'"
