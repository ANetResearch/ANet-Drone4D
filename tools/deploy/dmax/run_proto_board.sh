#!/usr/bin/env bash
# 在 dmax 的 d3-gz 容器内运行 P0a 原型榜（tools/bench/d3/proto_board.py，Realistic 档，宿主与 gz 同容器，launcher = local）。
# 用法（本机仓库根目录）：tools/deploy/dmax/run_proto_board.sh <输出名> [proto_board.py 参数]；后台运行，日志在 $D/out/<输出名>/board.log。
# 前提：tools/deploy/dmax/images/gz/build.sh 已构建镜像；$D/worlds/<world> 与 $D/d3data/events/<event>/v<N> 已就位；
# 宿主侧依赖在 $D/pylib-gz（离线 wheel：pyzmq、protobuf、jsonschema、msgpack、scipy、open3d-cpu 及其依赖）。
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
D=/data/anet-drone4d-d3/d3dev
NAME=${1:?output name}; shift
CPUS=${CPUS:-12}
rsync -a --delete --exclude __pycache__ "$ROOT/python/" "dmax:$D/python/"
rsync -a --mkpath --delete --exclude __pycache__ "$ROOT/tools/sdk/drone4d/src/" "dmax:$D/tools/sdk/drone4d/src/"
rsync -a --mkpath --delete --exclude __pycache__ "$ROOT/tools/bench/d3/" "dmax:$D/tools/bench/d3/"
rsync -a --mkpath --delete "$ROOT/configs/bench.d3/" "dmax:$D/configs/bench.d3/"
rsync -a --mkpath --delete "$ROOT/packages/contracts/" "dmax:$D/contracts/"
rsync -a --mkpath "$ROOT/native/gz/proto/" "dmax:$D/native/gz/proto/"
ssh dmax "mkdir -p $D/out/$NAME && docker run -d --rm --name d3-board-$NAME --network none --cpus $CPUS --user \$(id -u):\$(id -g) -e HOME=/tmp \
  -e PYTHONPATH=/repo/pylib-gz:/repo/python -e AWR_CONTRACTS_DIR=/repo/contracts -v $D:/repo -w /repo d3-gz:dev \
  sh -c 'python3 tools/bench/d3/proto_board.py --spec configs/bench.d3/crop-handoff-001.l1-3uav.run_spec.json \
    --events-root d3data/events --worlds-root worlds --out out/$NAME --gz-launcher local $* > out/$NAME/board.log 2>&1'"
