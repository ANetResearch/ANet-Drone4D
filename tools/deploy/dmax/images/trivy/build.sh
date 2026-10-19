#!/usr/bin/env bash
# 在 dmax 上构建 d3-trivy 镜像（提交镜像漏洞扫描器，ADR-204）：以 dmax `$D/tools/trivy/trivy`（fetch.sh 同步并校验）为唯一内容，
# FROM scratch，构建不访问网络。用法（本机仓库根目录）：tools/deploy/dmax/images/trivy/build.sh [tag]；输出镜像 ID 与大小。
set -euo pipefail
TAG=${1:-dev}
HERE=$(cd "$(dirname "$0")" && pwd)
D=/data/anet-drone4d-d3/d3dev
CTX=/data/anet-drone4d-d3/build/trivy
ssh dmax "mkdir -p $CTX && cp $D/tools/trivy/trivy $CTX/trivy"
rsync -a "$HERE/Dockerfile" "dmax:$CTX/"
ssh dmax "docker build -q -t d3-trivy:$TAG $CTX >/dev/null && docker image inspect d3-trivy:$TAG --format '{{.Id}} {{.Size}}' \
  && docker run --rm --network none --read-only --tmpfs /tmp --cpus 1 --memory 512m --name d3-subworker-trivy-version d3-trivy:$TAG --version | head -1"
