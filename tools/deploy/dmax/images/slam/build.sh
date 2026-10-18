#!/usr/bin/env bash
# 在 dmax 上构建 d3-livo 镜像：把本目录（Dockerfile、启动文件、入口与 RTK 抽取脚本）同步到 /data/anet-drone4d-d3/build/slam 后
# docker build（基础镜像 d3-r1-fast-livo2:noetic 已在 dmax，本层不联网）。构建号取当前提交（无 git 时为 dev）。
# 用法（本机仓库根目录）：tools/deploy/dmax/images/slam/build.sh [tag]
set -euo pipefail
TAG=${1:-dev}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../../../.." && pwd)
CTX=/data/anet-drone4d-d3/build/slam
BUILD=$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo dev)
ssh dmax "mkdir -p $CTX"
rsync -a "$HERE/Dockerfile" "$HERE/run_bag.launch" "$HERE/run_livo.sh" "$HERE/extract_rtk.py" "dmax:$CTX/"
ssh dmax "cd $CTX && docker build -q --build-arg D3_LIVO_BUILD=$BUILD -t d3-livo:$TAG . && docker images d3-livo:$TAG --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}'"
