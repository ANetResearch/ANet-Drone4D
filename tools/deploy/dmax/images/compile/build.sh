#!/usr/bin/env bash
# 在 dmax 上构建 d3-compile 镜像（世界包构建与事件编译，Detour 后端；见同目录 Dockerfile）：把本机 Node 22.12 可执行文件
# （NODE_DIR/bin/node，缺省 ~/.local/node）与 tools/navsvc/node_modules（`npm ci --prefix tools/navsvc`）同步到
# /data/anet-drone4d-d3/build/compile 后 docker build（构建内不访问网络；版本与 WASM sha256 在构建内核对）。
# 用法（本机仓库根目录）：tools/deploy/dmax/images/compile/build.sh [tag]；BASE 缺省 d3-sensorsvc:dev（基础镜像重建后重新运行本脚本）。
set -euo pipefail
TAG=${1:-dev}
BASE=${BASE:-d3-sensorsvc:dev}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../../../.." && pwd)
NODE_DIR=${NODE_DIR:-$HOME/.local/node}
CTX=/data/anet-drone4d-d3/build/compile
NAVSVC=$ROOT/tools/navsvc
NODE_VERSION=$("$NODE_DIR/bin/node" --version)
case "$NODE_VERSION" in v22.1[2-9].*|v22.[2-9][0-9].*) ;; *) echo "Node 22.12+ required at $NODE_DIR (got $NODE_VERSION)" >&2; exit 2;; esac
RECAST_VERSION=$(cat "$ROOT/tools/worldglb/RECAST_VERSION")
WASM_SHA256=$(cat "$NAVSVC/WASM_SHA256")
test -d "$NAVSVC/node_modules/@recast-navigation/core" || { echo "run: npm ci --prefix tools/navsvc" >&2; exit 2; }
ssh dmax "mkdir -p $CTX/node_modules"
rsync -a "$HERE/Dockerfile" "dmax:$CTX/"
rsync -a "$NODE_DIR/bin/node" "dmax:$CTX/node"
rsync -a --delete "$NAVSVC/node_modules/" "dmax:$CTX/node_modules/"
ssh dmax "cd $CTX && docker build -q --build-arg BASE=$BASE --build-arg NODE_VERSION=$NODE_VERSION --build-arg RECAST_VERSION=$RECAST_VERSION \
  --build-arg WASM_SHA256=$WASM_SHA256 -t d3-compile:$TAG . >/dev/null && docker image inspect d3-compile:$TAG --format '{{.Id}} {{.Size}}'"
