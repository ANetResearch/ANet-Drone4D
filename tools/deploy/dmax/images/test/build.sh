#!/usr/bin/env bash
# 在 dmax 上构建 d3-test 镜像（D3 测试统一入口主阶段；见同目录 Dockerfile）。缺省按依赖顺序重建三层：
#   1. d3-sensorsvc:<tag>  按 requirements.lock、requirements-d3.lock 与 GPU 层锁文件重建（images/sensorsvc/build.sh）；
#   2. d3-compile:<tag>    以新的 d3-sensorsvc 为基础，加 Node 22.12 与 navsvc 依赖（images/compile/build.sh）；
#   3. d3-test:<tag>       加 rsync、git、make。
# 基础镜像 python:3.12-slim 须为 amd64：dmax 上该共享标签可能被他人改指其他架构（K14 实测为 arm64），PY_BASE 给出 amd64 的
# Python 3.12 镜像时以 BuildKit 命名上下文替换 FROM（不改共享标签）；缺省核对共享标签的架构，不是 amd64 时报错退出。
# 用法（本机仓库根目录）：tools/deploy/dmax/images/test/build.sh [tag]；LAYERS=test 只重建第 3 层（缺省 sensorsvc,compile,test）。
set -euo pipefail
TAG=${1:-dev}
LAYERS=${LAYERS:-sensorsvc,compile,test}
PY_BASE=${PY_BASE:-}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../../../.." && pwd)
IMG=$ROOT/tools/deploy/dmax/images
CTX=/data/anet-drone4d-d3/build/test
case ",$LAYERS," in
  *,sensorsvc,*)
    if [ -z "$PY_BASE" ]; then
      A=$(ssh dmax "docker image inspect python:3.12-slim -f '{{.Architecture}}' 2>/dev/null || echo missing")
      [ "$A" = amd64 ] || { echo "python:3.12-slim on dmax is '$A', not amd64: set PY_BASE=<amd64 python 3.12 image>" >&2; exit 2; }
    fi
    PY_BASE=$PY_BASE bash "$IMG/sensorsvc/build.sh" "$TAG";;
esac
case ",$LAYERS," in *,compile,*) BASE=d3-sensorsvc:$TAG bash "$IMG/compile/build.sh" "$TAG";; esac
case ",$LAYERS," in
  *,test,*)
    ssh dmax "mkdir -p $CTX"
    rsync -a "$HERE/Dockerfile" "dmax:$CTX/"
    ssh dmax "cd $CTX && docker build -q --build-arg BASE=d3-compile:$TAG -t d3-test:$TAG . >/dev/null \
      && docker image inspect d3-test:$TAG --format '{{.Id}} {{.Architecture}} {{.Size}}'";;
esac
