#!/usr/bin/env bash
# 在 dmax 上一键执行最小基准闭环（tools/bench/d3/mbl_e2e.py，D3-AC-56）：世界包检查 → strict 编译 → gz 锁步预检 → L1 + L2 各 3 个基线 ×
# 秘密种子 × 重复 → 聚合评测 → 发布原型榜 → 触发 1 个内部提交 → 报告 $D/out/<输出名>/mbl_e2e.json。
# 用法（本机仓库根目录）：GPU=2 tools/deploy/dmax/run_mbl_e2e.sh <输出名> [mbl_e2e.py 参数]
#   同一输出名再次执行即断点续跑（已完成的阶段与已 COMPLETED 的 Run 跳过，配置指纹不同时拒绝并以 3 退出）。
#   缺省配置 CONFIG=configs/bench.d3/mbl-formal.yaml（不存在时用 mbl_e2e.py 的内置缺省，即正式口径）；数据根 $D/<BENCH_ROOT>（缺省 benchroot）。
#   SUBMIT_IMAGE_BUILD=1 时先构建示例内部提交镜像 d3-example-close-track:<SUBMIT_TAG>（tools/bench/d3/examples/close_track/build.sh）。
#   IMAGE 为宿主容器镜像，缺省 d3-compile:dev（d3-sensorsvc + Node 与 navsvc 依赖；strict 编译只接受 Detour 后端，AWR_EVENT_NAV=detour，
#   ADR-196、W12/K06）。REPO_DIR 为 dmax 上的代码与输出根目录（缺省 $D）；设为独立子目录（如 $D/b10）时，本轮代码不受其他任务同步
#   $D/python 的影响，世界包仍取 $D/worlds（以同一绝对路径挂载）。
#   WAIT=1 时前台等待容器结束并以其退出码退出；缺省后台运行，日志在 $D/out/<输出名>/e2e.log（阶段与 Run）与 console.log（全部输出）。
# 执行方式：宿主进程在 $IMAGE 容器内绑定一张卡（L2 的 EGL 渲染），gz 实例与 Agent 隔离容器经挂载的 docker 套接字作为兄弟容器启动
# （--gz-launcher docker 的挂载约定与 run_proto_board.sh 相同，路径按 AWR_HOST_PATH_MAP 换算）。dmax 只用 GPU 2、3；容器名 d3-board-mbl-<输出名>（组件 board：L1 在 CPU 池、L2 绑定一张卡，D3-AC-22 CM-06）。
# 前提：镜像 d3-compile:dev（images/compile/build.sh）、d3-gz:dev、d3-agent-py:dev（images/*/build.sh；d3-agent-py 须含 numpy 与 pillow）；$D/worlds/<world> 已就位。
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
CNAME=d3-board-mbl-$NAME
IMAGE=${IMAGE:-d3-compile:dev}
R=${REPO_DIR:-$D}
WR=worlds
WMOUNT=""
if [ "$R" != "$D" ]; then WR=$D/worlds; WMOUNT="-v $D/worlds:$D/worlds"; fi   # 同一绝对路径挂载（gz 导出与几何缓存写在世界包旁，与缺省方式相同）
CFG_ARG=""
if [ -f "$ROOT/$CONFIG" ]; then CFG_ARG="--config $CONFIG"; else echo "note: $CONFIG not found, using the built-in formal defaults" >&2; fi
if ssh dmax "docker ps -q --filter name=^/$CNAME\$" | grep -q .; then echo "$CNAME is already running" >&2; exit 3; fi
ssh dmax "mkdir -p $R"
rsync -a --delete --exclude __pycache__ "$ROOT/python/" "dmax:$R/python/"
rsync -a --mkpath --delete --exclude __pycache__ "$ROOT/tools/sdk/anet4d/src/" "dmax:$R/tools/sdk/anet4d/src/"
rsync -a --mkpath --delete --exclude __pycache__ "$ROOT/tools/bench/d3/" "dmax:$R/tools/bench/d3/"
rsync -a --mkpath --delete "$ROOT/configs/bench.d3/" "dmax:$R/configs/bench.d3/"
rsync -a --mkpath --delete "$ROOT/configs/events.d3/" "dmax:$R/configs/events.d3/"
rsync -a --mkpath --delete --exclude node_modules "$ROOT/tools/navsvc/" "dmax:$R/tools/navsvc/"
rsync -a --mkpath "$ROOT/tools/worldglb/RECAST_VERSION" "dmax:$R/tools/worldglb/"
rsync -a --mkpath --delete "$ROOT/packages/contracts/" "dmax:$R/contracts/"
rsync -a --mkpath "$ROOT/native/gz/proto/" "dmax:$R/native/gz/proto/"
rsync -a --mkpath "$ROOT/native/gz/models/x500_d4d/" "dmax:$R/native/gz/models/x500_d4d/"   # 机型映射的派生模型以此为基础（awr.phys.gz.vehicles）
rsync -a --mkpath --exclude __pycache__ "$ROOT/vehicles/" "dmax:$R/vehicles/"
if [ "${SUBMIT_IMAGE_BUILD:-0}" = 1 ]; then "$ROOT/tools/bench/d3/examples/close_track/build.sh" "${SUBMIT_TAG:-dev}"; fi
args=$(printf '%q ' "$@")
ssh dmax "mkdir -p $R/out/$NAME $R/$BR $R/run/as && docker run -d --rm --name $CNAME --network none --cpus $CPUS --memory $MEM \
  --user \$(id -u):\$(id -g) -e HOME=/tmp --gpus device=$GPU -e NVIDIA_DRIVER_CAPABILITIES=all -e OMP_NUM_THREADS=8 -e OPENBLAS_NUM_THREADS=8 \
  -e PYTHONPATH=/repo/python -e AWR_CONTRACTS_DIR=/repo/contracts -e AWR_HOST_PATH_MAP=/repo=$R -e AWR_AGENT_SOCK_ROOT=/repo/run/as -e AWR_EVENT_NAV=detour \
  -v $R/vehicles:/opt/awr/vehicles:ro -v /var/run/docker.sock:/var/run/docker.sock -v /usr/bin/docker:/usr/bin/docker:ro -v $R:/repo $WMOUNT -w /repo \
  $IMAGE sh -c 'python3 tools/bench/d3/mbl_e2e.py $CFG_ARG --out out/$NAME --data-root $BR --worlds-root $WR $args \
    >> out/$NAME/console.log 2>&1' >/dev/null"
echo "$CNAME started on GPU $GPU ($IMAGE): dmax:$R/out/$NAME/{e2e.log,console.log,mbl_e2e.json}"
if [ "${WAIT:-0}" = 1 ]; then
  rc=$(ssh dmax "docker wait $CNAME" || echo 1)
  ssh dmax "tail -n 14 $R/out/$NAME/e2e.log" || true
  exit "$rc"
fi
