#!/usr/bin/env bash
# 在 dmax 上构建示例提交镜像 d3-example-close-track:<tag>（需先 tools/deploy/dmax/images/agent/build.sh）；输出镜像摘要，即内部提交的
# image_digest。BASE 为基础镜像（缺省 d3-agent-py:dev）；WHEELS 为本机离线 wheel 目录（可选，覆盖安装到镜像，只在基础镜像尚未修复
# 已有修复版本的漏洞时使用）。构建后以 tools/deploy/dmax/images/agent/scan.sh d3-example-close-track:<tag> 过提交扫描门。
set -euo pipefail
TAG=${1:-dev}
BASE=${BASE:-d3-agent-py:dev}
HERE=$(cd "$(dirname "$0")" && pwd)
CTX=/data/anet-drone4d-d3/build/example-close-track
ssh dmax "mkdir -p $CTX && rm -rf $CTX/wheels && mkdir -p $CTX/wheels"
rsync -a --delete --exclude __pycache__ "$HERE/close_track" "$HERE/Dockerfile" "dmax:$CTX/"
if [ -n "${WHEELS:-}" ]; then rsync -a "$WHEELS"/*.whl "dmax:$CTX/wheels/"; fi
ssh dmax "cd $CTX && docker build -q --build-arg BASE=$BASE -t d3-example-close-track:$TAG . >/dev/null && docker image inspect d3-example-close-track:$TAG --format '{{.Id}}'"
