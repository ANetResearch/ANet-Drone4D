#!/usr/bin/env bash
# 在 dmax 上构建 d3-sensorsvc 镜像：本机按 requirements-nvjpeg.lock、requirements-warp.lock 的哈希下载 GPU JPEG 栈与 Warp wheel
# （缓存于 var/wheels/sensorsvc-{nvjpeg,warp}，不入库），与 Dockerfile、锁文件、10_nvidia.json 一并同步到
# /data/anet-drone4d-d3/build/sensorsvc 后 docker build（构建内离线安装）。
# 用法（本机仓库根目录）：tools/deploy/dmax/images/sensorsvc/build.sh [tag]
# PY_BASE：amd64 的 Python 3.12 镜像（dmax 共享标签 python:3.12-slim 被改指其他架构时，以 BuildKit 命名上下文替换 FROM，不改共享标签）
set -euo pipefail
TAG=${1:-dev}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../../../.." && pwd)
CTX=/data/anet-drone4d-d3/build/sensorsvc
PY=${PY:-$ROOT/.venv/bin/python}
for L in nvjpeg warp; do                                 # 离线 wheel 层：var/wheels/sensorsvc-<层> ← requirements-<层>.lock
  WH=$ROOT/var/wheels/sensorsvc-$L
  mkdir -p "$WH"
  "$PY" -m pip download -q --no-deps --only-binary=:all: --python-version 3.12 --implementation cp \
    --platform manylinux_2_28_x86_64 --platform manylinux_2_27_x86_64 --platform manylinux_2_24_x86_64 \
    --platform manylinux_2_17_x86_64 --platform manylinux2014_x86_64 \
    --require-hashes -r "$HERE/requirements-$L.lock" -d "$WH"
done
ssh dmax "mkdir -p $CTX/wheels-nvjpeg $CTX/wheels-warp"
rsync -a "$HERE/Dockerfile" "$HERE/10_nvidia.json" "$HERE/requirements-gpu.lock" "$HERE/requirements-nvjpeg.lock" "$HERE/requirements-warp.lock" \
  "$ROOT/requirements.lock" "$ROOT/requirements-d3.lock" "dmax:$CTX/"
rsync -a --delete "$ROOT/var/wheels/sensorsvc-nvjpeg/" "dmax:$CTX/wheels-nvjpeg/"
rsync -a --delete "$ROOT/var/wheels/sensorsvc-warp/" "dmax:$CTX/wheels-warp/"
ssh dmax "cd $CTX && docker build -q ${PY_BASE:+--build-context python:3.12-slim=docker-image://$PY_BASE} -t d3-sensorsvc:$TAG . && docker images d3-sensorsvc:$TAG --format '{{.Repository}}:{{.Tag}} {{.Size}}'"
