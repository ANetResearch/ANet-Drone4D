#!/usr/bin/env bash
# 在 dmax 上一键执行最小基准闭环（tools/bench/d3/mbl_e2e.py，D3-AC-56）：世界包检查 → strict 编译 → gz 锁步预检 → L1 + L2 各 3 个基线 ×
# 秘密种子 × 重复 → 聚合评测 → 发布原型榜 → 触发 1 个内部提交 → 报告 $D/out/<输出名>/mbl_e2e.json。
# 用法（本机仓库根目录）：GPU=2 tools/deploy/dmax/run_mbl_e2e.sh <输出名> [mbl_e2e.py 参数]
#   同一输出名再次执行即断点续跑（已完成的阶段与已 COMPLETED 的 Run 跳过，配置指纹不同时拒绝并以 3 退出）。
#   缺省配置 CONFIG=configs/bench.d3/mbl-formal.yaml（不存在时用 mbl_e2e.py 的内置缺省，即正式口径）；数据根 $D/<BENCH_ROOT>（缺省 benchroot）。
#   SUBMIT_IMAGE_BUILD=1 时先构建示例内部提交镜像 d3-example-close-track:<SUBMIT_TAG>（tools/bench/d3/examples/close_track/build.sh）。
#   WAIT=1 时前台等待容器结束并以其退出码退出；缺省后台运行，日志在 $D/out/<输出名>/e2e.log（阶段与 Run）与 console.log（全部输出）。
# 执行方式：宿主进程在 d3-sensorsvc 容器内绑定一张卡（L2 的 EGL 渲染），gz 实例与 Agent 隔离容器经挂载的 docker 套接字作为兄弟容器启动
# （--gz-launcher docker 的挂载约定与 run_proto_board.sh 相同，路径按 AWR_HOST_PATH_MAP 换算）。dmax 只用 GPU 2、3；容器名 d3-mbl-<输出名>。
# 前提：镜像 d3-sensorsvc:dev、d3-gz:dev、d3-agent-py:dev（images/*/build.sh；d3-agent-py 须含 numpy 与 pillow）；$D/worlds/<world> 已就位。
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
D=/data/anet-drone4d-d3/d3dev
NAME=${1:?output name}; shift
case "$NAME" in *[!A-Za-z0-9._-]*) echo "output name must use [A-Za-z0-9._-]" >&2; exit 2;; esac
GPU=${GPU:-2}
case "$GPU" in 2|3) ;; *) echo "GPU must be 2 or 3 on dmax" >&2; exit 2;; esac
CPUS=${CPUS:-16}
MEM=${MEM:-64g}
BR=${BENCH_ROOT:-benchroot}
CONFIG=${CONFIG:-configs/bench.d3/mbl-formal.yaml}
CNAME=d3-mbl-$NAME
CFG_ARG=""
if [ -f "$ROOT/$CONFIG" ]; then CFG_ARG="--config $CONFIG"; else echo "note: $CONFIG not found, using the built-in formal defaults" >&2; fi
if ssh dmax "docker ps -q --filter name=^/$CNAME\$" | grep -q .; then echo "$CNAME is already running" >&2; exit 3; fi
rsync -a --delete --exclude __pycache__ "$ROOT/python/" "dmax:$D/python/"
rsync -a --mkpath --delete --exclude __pycache__ "$ROOT/tools/sdk/drone4d/src/" "dmax:$D/tools/sdk/drone4d/src/"
rsync -a --mkpath --delete --exclude __pycache__ "$ROOT/tools/bench/d3/" "dmax:$D/tools/bench/d3/"
rsync -a --mkpath --delete "$ROOT/configs/bench.d3/" "dmax:$D/configs/bench.d3/"
rsync -a --mkpath --delete "$ROOT/configs/events.d3/" "dmax:$D/configs/events.d3/"
rsync -a --mkpath --delete "$ROOT/packages/contracts/" "dmax:$D/contracts/"
rsync -a --mkpath "$ROOT/native/gz/proto/" "dmax:$D/native/gz/proto/"
rsync -a --mkpath --exclude __pycache__ "$ROOT/vehicles/" "dmax:$D/vehicles/"
if [ "${SUBMIT_IMAGE_BUILD:-0}" = 1 ]; then "$ROOT/tools/bench/d3/examples/close_track/build.sh" "${SUBMIT_TAG:-dev}"; fi
args=$(printf '%q ' "$@")
ssh dmax "mkdir -p $D/out/$NAME $D/$BR $D/run/as && docker run -d --rm --name $CNAME --network none --cpus $CPUS --memory $MEM \
  --user \$(id -u):\$(id -g) -e HOME=/tmp --gpus device=$GPU -e NVIDIA_DRIVER_CAPABILITIES=all -e OMP_NUM_THREADS=8 -e OPENBLAS_NUM_THREADS=8 \
  -e PYTHONPATH=/repo/python -e AWR_CONTRACTS_DIR=/repo/contracts -e AWR_HOST_PATH_MAP=/repo=$D -e AWR_AGENT_SOCK_ROOT=/repo/run/as \
  -v $D/vehicles:/opt/awr/vehicles:ro -v /var/run/docker.sock:/var/run/docker.sock -v /usr/bin/docker:/usr/bin/docker:ro -v $D:/repo -w /repo \
  d3-sensorsvc:dev sh -c 'python3 tools/bench/d3/mbl_e2e.py $CFG_ARG --out out/$NAME --data-root $BR --worlds-root worlds $args \
    >> out/$NAME/console.log 2>&1' >/dev/null"
echo "$CNAME started on GPU $GPU: dmax:$D/out/$NAME/{e2e.log,console.log,mbl_e2e.json}"
if [ "${WAIT:-0}" = 1 ]; then
  rc=$(ssh dmax "docker wait $CNAME" || echo 1)
  ssh dmax "tail -n 14 $D/out/$NAME/e2e.log" || true
  exit "$rc"
fi
