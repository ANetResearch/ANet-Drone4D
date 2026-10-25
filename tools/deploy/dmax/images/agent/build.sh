#!/usr/bin/env bash
# 在 dmax 上构建 d3-agent-py 镜像（容器 Agent 基础镜像与参考基线）：同步 anet4d SDK（含参考检测器权重与兼容别名包 drone4d）与离线 wheel
# （msgpack 在 $D/wheels-gz，numpy、pillow 在 $D/wheels-agent，cp312 manylinux_2_28）后 docker build。msgpack 钉在 MSGPACK_VERSION
# （缺省 1.2.2，同 requirements.lock；wheels-gz 中并存多个版本时 pip 无法求解），numpy 版本同 requirements-d3.lock；pillow 单独
# 钉在 PILLOW_VERSION（缺省 12.3.0）：基础镜像须通过提交扫描门（Trivy HIGH 及以上、上游已有修复的漏洞即拒绝，ADR-204、D3-AC-59），
# 11.3.0 有 13 个可修复的 HIGH 漏洞（B21 遗留 7，K18）。wheel 须先放入 $D/wheels-agent，并与 PyPI 公布的 sha256 一致（构建前校验）。
# 构建后以 scan.sh 过扫描门：tools/deploy/dmax/images/agent/scan.sh d3-agent-py:<tag>。
# 用法（本机仓库根目录）：tools/deploy/dmax/images/agent/build.sh [tag]；输出镜像摘要（内部提交与运行规格以摘要引用）。
set -euo pipefail
TAG=${1:-dev}
PILLOW_VERSION=${PILLOW_VERSION:-12.3.0}
MSGPACK_VERSION=${MSGPACK_VERSION:-1.2.2}
# PyPI 公布的 pillow-12.3.0-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl sha256（2026-10-22 核对）
PILLOW_SHA256=${PILLOW_SHA256:-78cb2c6865a35ab8ff8b75fd122f6033b92a62c82801110e48ddd6c936a45d91}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../../../.." && pwd)
CTX=/data/anet-drone4d-d3/build/agent
W=/data/anet-drone4d-d3/d3dev/wheels-agent
ssh dmax "set -e; mkdir -p $CTX/wheels && rm -f $CTX/wheels/*.whl
  pw=\$(ls $W/pillow-$PILLOW_VERSION-cp312-*manylinux*x86_64*.whl)
  echo \"$PILLOW_SHA256  \$pw\" | sha256sum -c --quiet -
  cp /data/anet-drone4d-d3/d3dev/wheels-gz/msgpack-$MSGPACK_VERSION-cp312*.whl $W/numpy-*cp312*.whl \"\$pw\" $CTX/wheels/"
rsync -a --mkpath --delete --exclude __pycache__ --exclude /anet4d/bench "$ROOT/tools/sdk/anet4d/src/" "dmax:$CTX/sdk/"
rsync -a "$HERE/Dockerfile" "dmax:$CTX/"
ssh dmax "cd $CTX && docker build -q -t d3-agent-py:$TAG . >/dev/null && docker image inspect d3-agent-py:$TAG --format '{{.Id}} {{.Size}}'"
