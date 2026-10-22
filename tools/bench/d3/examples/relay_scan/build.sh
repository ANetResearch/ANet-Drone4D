#!/usr/bin/env bash
# 在 dmax 上构建示例提交镜像 d3-example-relay-scan:<tag>（需先 tools/deploy/dmax/images/agent/build.sh）；输出镜像摘要，即内部提交的
# image_digest。BASE 为基础镜像（缺省 d3-agent-py:dev；排行榜 v1 正式评测用快照标签 d3-agent-py:b21）；WHEELS 为本机离线 wheel 目录
# （可选，覆盖安装到镜像，用于修复基础镜像中已有修复版本的漏洞，例如 pillow 12.3.0 的 cp312 manylinux wheel）。
set -euo pipefail
TAG=${1:-dev}
BASE=${BASE:-d3-agent-py:dev}
HERE=$(cd "$(dirname "$0")" && pwd)
CTX=/data/anet-drone4d-d3/build/example-relay-scan
ssh dmax "mkdir -p $CTX && rm -rf $CTX/wheels && mkdir -p $CTX/wheels"
rsync -a --delete --exclude __pycache__ "$HERE/relay_scan" "$HERE/Dockerfile" "dmax:$CTX/"
if [ -n "${WHEELS:-}" ]; then rsync -a "$WHEELS"/*.whl "dmax:$CTX/wheels/"; fi
ssh dmax "cd $CTX && docker build -q --build-arg BASE=$BASE -t d3-example-relay-scan:$TAG . >/dev/null && docker image inspect d3-example-relay-scan:$TAG --format '{{.Id}}'"
