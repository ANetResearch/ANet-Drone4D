#!/usr/bin/env bash
# 在 dmax 上运行内部提交流水线（`python -m awr.bench.subworker`，d3-gz 容器，宿主与 gz 同容器，Agent 为隔离容器）。
# 用法（本机仓库根目录）：tools/deploy/dmax/run_subworker.sh [--once] [其它 subworker 参数]；后台运行，日志 $D/<BENCH_ROOT>/subworker.log。
# 数据根 $D/<BENCH_ROOT>（缺省 benchroot）须已发布榜单（bench_ctl.sh board publish … --current）。Agent 套接字放在 $D/run/as（AF_UNIX 路径上限）。
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
D=/data/anet-drone4d-d3/d3dev
BR=${BENCH_ROOT:-benchroot}
CPUS=${CPUS:-8}
rsync -a --delete --exclude __pycache__ "$ROOT/python/" "dmax:$D/python/"
rsync -a --mkpath --delete "$ROOT/packages/contracts/" "dmax:$D/contracts/"
rsync -a --mkpath "$ROOT/native/gz/proto/" "dmax:$D/native/gz/proto/"
ssh dmax "mkdir -p $D/$BR $D/run/as && docker rm -f d3-subworker >/dev/null 2>&1; docker run -d --rm --name d3-subworker --network none --cpus $CPUS \
  --user \$(id -u):\$(id -g) -e HOME=/tmp -e PYTHONPATH=/repo/pylib-gz:/repo/python -e AWR_CONTRACTS_DIR=/repo/contracts \
  -e AWR_HOST_PATH_MAP=/repo=$D -e AWR_AGENT_SOCK_ROOT=/repo/run/as \
  -v /var/run/docker.sock:/var/run/docker.sock -v /usr/bin/docker:/usr/bin/docker:ro -v $D:/repo -w /repo d3-gz:dev \
  sh -c 'python3 -m awr.bench.subworker --data-root /repo/$BR $* >> /repo/$BR/subworker.log 2>&1'"
