#!/usr/bin/env bash
# 在 dmax 上构建 d3-sensorsvc 镜像：把 Dockerfile、锁文件与 10_nvidia.json 同步到 /data/anet-drone4d-d3/build/sensorsvc 后 docker build。
# 用法（本机仓库根目录）：tools/deploy/dmax/images/sensorsvc/build.sh [tag]
set -euo pipefail
TAG=${1:-dev}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../../../.." && pwd)
CTX=/data/anet-drone4d-d3/build/sensorsvc
ssh dmax "mkdir -p $CTX"
rsync -a "$HERE/Dockerfile" "$HERE/10_nvidia.json" "$HERE/requirements-gpu.lock" "$ROOT/requirements.lock" "$ROOT/requirements-d3.lock" "dmax:$CTX/"
ssh dmax "cd $CTX && docker build -q -t d3-sensorsvc:$TAG . && docker images d3-sensorsvc:$TAG --format '{{.Repository}}:{{.Tag}} {{.Size}}'"
