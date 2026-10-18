#!/usr/bin/env bash
# 在 dmax 上构建示例提交镜像 d3-example-close-track:<tag>（需先 tools/deploy/dmax/images/agent/build.sh）；输出镜像摘要，
# 即内部提交的 image_digest。
set -euo pipefail
TAG=${1:-dev}
HERE=$(cd "$(dirname "$0")" && pwd)
CTX=/data/anet-drone4d-d3/build/example-close-track
ssh dmax "mkdir -p $CTX"
rsync -a --delete --exclude __pycache__ "$HERE/close_track" "$HERE/Dockerfile" "dmax:$CTX/"
ssh dmax "cd $CTX && docker build -q -t d3-example-close-track:$TAG . >/dev/null && docker image inspect d3-example-close-track:$TAG --format '{{.Id}}'"
