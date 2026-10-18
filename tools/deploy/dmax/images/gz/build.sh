#!/usr/bin/env bash
# 在 dmax 上构建 d3-gz 镜像：把 native/gz 与 Dockerfile 同步到 /data/anet-drone4d-d3/build/gz 后 docker build。
# 用法（本机仓库根目录）：tools/deploy/dmax/images/gz/build.sh [tag]；插件构建号取当前提交（无 git 时为 dev）。
set -euo pipefail
TAG=${1:-dev}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../../../.." && pwd)
CTX=/data/anet-drone4d-d3/build/gz
BUILD=$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo dev)
ssh dmax "mkdir -p $CTX"
rsync -a --delete "$ROOT/native/gz/" "dmax:$CTX/gz/"
rsync -a "$HERE/Dockerfile" "dmax:$CTX/"
ssh dmax "cd $CTX && docker build -q --build-arg D4D_PLUGIN_BUILD=$BUILD -t d3-gz:$TAG . && docker images d3-gz:$TAG --format '{{.Repository}}:{{.Tag}} {{.Size}}'"
