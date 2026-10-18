#!/usr/bin/env bash
# 在 dmax 的 d3-gz 容器内执行 `python -m awr.bench.ctl …`（数据根 $D/<BENCH_ROOT>，缺省 benchroot）。
# 例：tools/deploy/dmax/bench_ctl.sh board publish /repo/out/proto-2 --current
#     tools/deploy/dmax/bench_ctl.sh submission create --team d3 --owner alice --track std3-l1 --digest sha256:… --cmd "python -m drone4d.serve pkg:Agent"
# 不要用于 token issue（明文令牌会经 ssh 输出到本机终端）。
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
D=/data/anet-drone4d-d3/d3dev
BR=${BENCH_ROOT:-benchroot}
rsync -a --delete --exclude __pycache__ "$ROOT/python/" "dmax:$D/python/"
rsync -a --mkpath --delete "$ROOT/packages/contracts/" "dmax:$D/contracts/"
args=$(printf '%q ' "$@")
ssh dmax "mkdir -p $D/$BR && docker run --rm --network none --user \$(id -u):\$(id -g) -e HOME=/tmp -e PYTHONPATH=/repo/pylib-gz:/repo/python \
  -e AWR_CONTRACTS_DIR=/repo/contracts -e AWR_BENCH_DATA_ROOT=/repo/$BR -v $D:/repo -w /repo d3-gz:dev python3 -m awr.bench.ctl $args"
