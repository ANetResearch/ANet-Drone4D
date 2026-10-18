#!/usr/bin/env bash
# 在 dmax 上构建 d3-sensorsvc 镜像：本机按 requirements-nvjpeg.lock 的哈希下载 GPU JPEG 栈 wheel（缓存于 var/wheels/sensorsvc-nvjpeg，
# 不入库），与 Dockerfile、锁文件、10_nvidia.json 一并同步到 /data/anet-drone4d-d3/build/sensorsvc 后 docker build（构建内离线安装）。
# 用法（本机仓库根目录）：tools/deploy/dmax/images/sensorsvc/build.sh [tag]
set -euo pipefail
TAG=${1:-dev}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../../../.." && pwd)
CTX=/data/anet-drone4d-d3/build/sensorsvc
WH=$ROOT/var/wheels/sensorsvc-nvjpeg
PY=${PY:-$ROOT/.venv/bin/python}
mkdir -p "$WH"
"$PY" -m pip download -q --no-deps --only-binary=:all: --python-version 3.12 --implementation cp \
  --platform manylinux_2_28_x86_64 --platform manylinux_2_27_x86_64 --platform manylinux_2_24_x86_64 \
  --platform manylinux_2_17_x86_64 --platform manylinux2014_x86_64 \
  --require-hashes -r "$HERE/requirements-nvjpeg.lock" -d "$WH"
ssh dmax "mkdir -p $CTX/wheels-nvjpeg"
rsync -a "$HERE/Dockerfile" "$HERE/10_nvidia.json" "$HERE/requirements-gpu.lock" "$HERE/requirements-nvjpeg.lock" "$ROOT/requirements.lock" \
  "$ROOT/requirements-d3.lock" "dmax:$CTX/"
rsync -a --delete "$WH/" "dmax:$CTX/wheels-nvjpeg/"
ssh dmax "cd $CTX && docker build -q -t d3-sensorsvc:$TAG . && docker images d3-sensorsvc:$TAG --format '{{.Repository}}:{{.Tag}} {{.Size}}'"
