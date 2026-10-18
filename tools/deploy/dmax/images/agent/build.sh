#!/usr/bin/env bash
# 在 dmax 上构建 d3-agent-py 镜像（容器 Agent 基础镜像与参考基线）：同步 drone4d SDK（含参考检测器权重）与离线 wheel
# （msgpack；numpy、pillow 在 $D/wheels-agent，cp312 manylinux_2_28，版本同 requirements-d3.lock）后 docker build。
# 用法（本机仓库根目录）：tools/deploy/dmax/images/agent/build.sh [tag]；输出镜像摘要（内部提交与运行规格以摘要引用）。
set -euo pipefail
TAG=${1:-dev}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../../../.." && pwd)
CTX=/data/anet-drone4d-d3/build/agent
ssh dmax "mkdir -p $CTX/wheels && rm -f $CTX/wheels/*.whl && cp /data/anet-drone4d-d3/d3dev/wheels-gz/msgpack-*cp312*.whl \
  /data/anet-drone4d-d3/d3dev/wheels-agent/numpy-*cp312*.whl /data/anet-drone4d-d3/d3dev/wheels-agent/pillow-*cp312*.whl $CTX/wheels/"
rsync -a --delete --exclude __pycache__ --exclude bench "$ROOT/tools/sdk/drone4d/src/drone4d/" "dmax:$CTX/drone4d/"
rsync -a "$HERE/Dockerfile" "dmax:$CTX/"
ssh dmax "cd $CTX && docker build -q -t d3-agent-py:$TAG . >/dev/null && docker image inspect d3-agent-py:$TAG --format '{{.Id}} {{.Size}}'"
