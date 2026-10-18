#!/usr/bin/env bash
# 在 dmax 的 d3-gz 容器内运行 Realistic 档锁步测试（宿主与 gz 同容器，launcher = local；无网络、本用户 uid）。
# 用法（本机仓库根目录）：tools/deploy/dmax/run_gz_tests.sh [pytest 参数]；需先 tools/deploy/dmax/images/gz/build.sh。
# 宿主侧依赖装在 $D/pylib-gz（cp312 离线 wheel：pyzmq、protobuf、pytest、jsonschema、msgpack，版本同 requirements-d3.lock）。
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
D=/data/anet-drone4d-d3/d3dev
rsync -a --delete --exclude __pycache__ "$ROOT/python/" "dmax:$D/python/"
rsync -a --mkpath --delete --exclude __pycache__ "$ROOT/tests/d3/phys/" "dmax:$D/tests/d3/phys/"
rsync -a --mkpath "$ROOT/native/gz/proto/" "dmax:$D/native/gz/proto/"
ssh dmax "docker run --rm --name d3-gz-test --network none --cpus 4 --user \$(id -u):\$(id -g) -e HOME=/tmp \
  -e PYTHONPATH=/repo/pylib-gz:/repo/python -e AWR_CONTRACTS_DIR=/repo/contracts -v $D:/repo -w /repo d3-gz:dev \
  python3 -m pytest -q -p no:cacheprovider -p no:warnings ${*:-/repo/tests/d3/phys/test_gz_lockstep.py /repo/tests/d3/phys/test_gz_protocol.py}"
