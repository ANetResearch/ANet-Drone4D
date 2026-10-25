#!/usr/bin/env bash
# D3 测试统一入口（D3 封板 I01；docs/05 §11 证据规则）：在 dmax 的 d3-test 镜像内（d3-sensorsvc 的依赖 + Node 22.12 与 navsvc +
# rsync、git、make；tools/deploy/dmax/images/test，K14、F-113）绑定一张 GPU 运行 tests/d3 全量，另在 d3-gz 镜像内（CPU 池，不挂 GPU）
# 运行需要 gz-sim 的用例（tests/d3/phys 中名称含 gz 者：锁步、协议、导出检查、故障注入）；依赖本仓库 git 目录、docker 守护进程或
# 本机部署工具的用例（HOST_TESTS：tests/d3/deploy、submit/test_redteam.py）在本机 .venv-d3 中作为主机阶段运行（主阶段 --ignore 它们），
# 按用例合并计数与跳过分层
# （tests/d3/conftest.py 写各阶段摘要，tools/deploy/dmax/d3_tests_summary.py 合并）。此后各工作包“done”以本入口
# counts.json 的计数为证据（通过、失败、跳过分层、缺失依赖）。
#
# 产物：dmax $D/out/tests/<tag>/ 与本机副本 var/tests/<tag>/（var/ 不入库）。<tag> 为 HEAD 的 12 位 sha；工作树有改动时为
# <sha>-dirty-<改动摘要 8 位>。文件：counts.json（合并计数）、meta.json、files.txt（快照文件清单）；各阶段 junit<后缀>.xml、
# summary<后缀>.json、pytest<后缀>.log、rc<后缀>.txt（主阶段后缀为空，gz 阶段为 -gz，主机阶段为 -host）。
# 源码快照：已跟踪与未忽略的未跟踪文件（git ls-files -co --exclude-standard）同步到 $D/d3tests/src-<id>（本入口独占，清单外文件
# 删除）；世界包：本机 worlds/ 同步到 $D/d3tests/worlds（只增量，不删除），以只读卷挂到 /repo/worlds。
#
# 用法（本机仓库根目录）：tools/deploy/dmax/run_d3_tests.sh [pytest 参数，缺省 tests/d3]
# 环境变量：
#   GPU=2            绑定的卡（1、2、3 之一；dmax 只用 GPU 2、3，GPU 1 空闲时可用）
#   TASK_ID=main     实例名：容器 d3-test-<id>[-gz|-sum]、快照 $D/d3tests/src-<id>（小写字母、数字、点、下划线、连字符）
#   REQUIRE=dep,gpu  严格层（AWR_D3_REQUIRE）：完整镜像与 GPU 上不允许以缺依赖或缺 GPU 为由跳过；置空则只记录
#   GZ=1             是否运行 gz 锁步阶段；给出 pytest 参数时缺省 0
#   HOST=1           是否运行主机阶段（本机 .venv-d3，HOST_TESTS）；给出 pytest 参数时缺省 0
#   IMAGE=d3-test:dev GZ_IMAGE=d3-gz:dev   主阶段与 gz 阶段镜像（镜像构建见 tools/deploy/dmax/images/test/build.sh、gz/build.sh）
#   PASS_ENV         额外传入主阶段容器的环境变量名（空格分隔，仅在本机已设置时传入；缺省 AWR_LIDAR_GPU_REPORT AWR_AC61_REPORT）
#   NOFILE=1048576   容器打开文件数上限（--ulimit nofile；容器缺省上限下长会话收尾会报 Too many open files，F-114）
#   WORLDS=1         是否同步本机 worlds/（首次约 4 GB，之后增量；含 .status 世界状态）
#   CPUS=8 MEM=32g   主阶段容器资源上限（gz 阶段 4 核、8 GB）；TIMEOUT=5400 单阶段秒数上限（容器内 timeout）
#   OUT_LOCAL        本机副本目录（缺省 var/tests/<tag>）
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
D=/data/anet-drone4d-d3/d3dev
GPU=${GPU:-2}
TASK_ID=$(printf '%s' "${TASK_ID:-main}" | tr 'A-Z' 'a-z')
REQUIRE=${REQUIRE-dep,gpu}
CPUS=${CPUS:-8}
MEM=${MEM:-32g}
TIMEOUT=${TIMEOUT:-5400}
WORLDS=${WORLDS:-1}
IMAGE=${IMAGE:-d3-test:dev}
GZ_IMAGE=${GZ_IMAGE:-d3-gz:dev}
NOFILE=${NOFILE:-1048576}
PASS_ENV=${PASS_ENV-AWR_LIDAR_GPU_REPORT AWR_AC61_REPORT}
NODE_DIR=${NODE_DIR:-$HOME/.local/node}
HOST_PY=${HOST_PY:-$ROOT/.venv-d3/bin/python}
HOST_TESTS="tests/d3/deploy tests/d3/submit/test_redteam.py"
case "$GPU" in 1|2|3) ;; *) echo "GPU must be exactly one of 1, 2, 3 on dmax (got '$GPU')" >&2; exit 2;; esac
case "$TASK_ID" in ""|*[!a-z0-9._-]*) echo "TASK_ID must match [a-z0-9._-]+ (got '$TASK_ID')" >&2; exit 2;; esac
case "$NOFILE" in ""|*[!0-9]*) echo "NOFILE must be a positive integer (got '$NOFILE')" >&2; exit 2;; esac
if [ $# -gt 0 ]; then GZ=${GZ:-0} HOST=${HOST:-0}; else GZ=${GZ:-1} HOST=${HOST:-1}; set -- tests/d3; fi
if [ "$HOST" = 1 ]; then                                # 主机阶段的用例不在主阶段运行（合并取最差，容器内必然失败）
  for t in $HOST_TESTS; do set -- "$@" "--ignore=$t"; done
fi
cd "$ROOT"
if [ ! -e .git ]; then export GIT_DIR=${GIT_DIR:-/data/projs/.anet-drone4d.git} GIT_WORK_TREE=${GIT_WORK_TREE:-$ROOT}; fi

# ---------------------------------------------------------------- 标签与快照文件清单
LIST=$(mktemp)
FILT=$(mktemp)
trap 'rm -f "$LIST" "$FILT"' EXIT
if command -v git >/dev/null 2>&1 && git rev-parse --verify -q HEAD >/dev/null 2>&1; then
  SHA=$(git rev-parse --short=12 HEAD)
  TAG=$SHA DIRTY=0
  git ls-files -co --exclude-standard -z | while IFS= read -r -d '' f; do if [ -f "$f" ]; then printf '%s\n' "$f"; fi; done | LC_ALL=C sort -u > "$LIST"
  if [ -n "$(git status --porcelain --untracked-files=normal)" ]; then
    DIRTY=1
    # 未跟踪项只摘要普通文件：工作树副本里以符号链接接入的目录（如 tools/navsvc/node_modules）不是文件，sha256sum 会报错退出；
    # 循环体用 if（不用 &&），最后一项不是普通文件时循环的退出码仍为 0（set -e 与 pipefail）
    TAG=$SHA-dirty-$({ git diff HEAD --binary; git ls-files -o --exclude-standard -z \
      | while IFS= read -r -d '' f; do if [ -f "$f" ]; then printf '%s\0' "$f"; fi; done | xargs -0 -r sha256sum; } | sha256sum | cut -c1-8)
  fi
else                                                    # 无 git（如容器内的清单审计）：按目录清单，标签不可作证据
  SHA=nogit TAG=nogit-$(date -u +%Y%m%dT%H%M%SZ) DIRTY=1
  find python tests tools packages vehicles native configs scenarios pyproject.toml -type f -not -path '*/__pycache__/*' 2>/dev/null \
    | LC_ALL=C sort -u > "$LIST"
fi
# rsync 过滤表：清单内文件及其各级父目录纳入，其余排除并在快照中删除（--delete-excluded）
awk '{ p = $0; gsub(/[][*?\\]/, "\\\\&", p); n = split(p, a, "/"); d = ""; for (i = 1; i < n; i++) { d = d "/" a[i]; if (!(d in seen)) { seen[d] = 1; print "+ " d "/" } } print "+ /" p } END { print "- *" }' \
  "$LIST" > "$FILT"
SRC=$D/d3tests/src-$TASK_ID
OUT=$D/out/tests/$TAG
OUT_LOCAL=${OUT_LOCAL:-$ROOT/var/tests/$TAG}
N=d3-test-$TASK_ID
echo "[d3-tests] tag $TAG  gpu $GPU  require ${REQUIRE:--}  image $IMAGE  gz $GZ ($GZ_IMAGE)  host $HOST  worlds $WORLDS  args: $*"
ssh dmax "mkdir -p $SRC $OUT $D/d3tests/worlds; cd $OUT && rm -f rc*.txt summary*.json junit*.xml pytest*.log counts.json meta.json || true"
rsync -a --delete --delete-excluded --chown=1000:1000 --filter="merge $FILT" "$ROOT/" "dmax:$SRC/"
rsync -a "$LIST" "dmax:$OUT/files.txt"
if [ "$WORLDS" = 1 ] && [ -d "$ROOT/worlds" ]; then
  rsync -a --chown=1000:1000 --exclude '/.locks/*' --exclude '/.staging/*' --exclude '/.trash/*' "$ROOT/worlds/" "dmax:$D/d3tests/worlds/"
fi

PYFLAGS="-q -rs -p no:cacheprovider -p no:warnings --continue-on-collection-errors -o junit_family=xunit2"

# ---------------------------------------------------------------- 本机：主机阶段（.venv-d3，与快照同一份文件清单；结果并入同一标签目录）
STAGES="=$IMAGE"
if [ "$HOST" = 1 ]; then
  STAGES="$STAGES,-host=host:$(hostname -s 2>/dev/null || echo local)"
  HOUT=$(mktemp -d)
  echo "[d3-tests] host stage: $HOST_TESTS"
  set +e
  PATH="$NODE_DIR/bin:$PATH" PYTHONPATH="$ROOT/python" AWR_D3_SUMMARY="$HOUT/summary-host.json" AWR_D3_REQUIRE="$REQUIRE" \
    timeout "$TIMEOUT" "$HOST_PY" -m pytest $PYFLAGS --junitxml="$HOUT/junit-host.xml" $HOST_TESTS > "$HOUT/pytest-host.log" 2>&1
  echo $? > "$HOUT/rc-host.txt"
  set -e
  tail -n 4 "$HOUT/pytest-host.log"
  rsync -a "$HOUT/" "dmax:$OUT/"
  rm -rf "$HOUT"
fi

# ---------------------------------------------------------------- 远端：主阶段（GPU）、gz 阶段（CPU 池）、合并
ARGS=$(printf '%q ' "$@")
COMMON="--rm --network none --user 1000:1000 --ulimit nofile=$NOFILE:$NOFILE -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 \
  -e AWR_CONTRACTS_DIR=/repo/packages/contracts -e AWR_HOST_PATH_MAP=/repo=$SRC -e AWR_WORLDS_DIR=/repo/worlds -e AWR_D3_REQUIRE=$REQUIRE \
  -v $SRC:/repo -v $D/d3tests/worlds:/repo/worlds:ro -v $OUT:/out -w /repo"
XENV=""
for v in $PASS_ENV; do
  case "$v" in ""|*[!A-Z0-9_]*) echo "PASS_ENV: bad variable name '$v'" >&2; exit 2;; esac
  if [ -n "${!v:-}" ]; then XENV="$XENV -e $v=$(printf '%q' "${!v}")"; fi
done
PYTEST="python3 -m pytest $PYFLAGS"
R="chown 1000:1000 $OUT; \
docker rm -f $N >/dev/null 2>&1; \
docker run --name $N $COMMON --cpus $CPUS --memory $MEM --gpus device=$GPU -e NVIDIA_DRIVER_CAPABILITIES=all \
  -e PYTHONPATH=/repo/python -e AWR_D3_SUMMARY=/out/summary.json$XENV -v $D/vehicles:/opt/awr/vehicles:ro \
  $IMAGE sh -c 'timeout $TIMEOUT $PYTEST --junitxml=/out/junit.xml $ARGS > /out/pytest.log 2>&1'; echo \$? > $OUT/rc.txt; \
tail -n 8 $OUT/pytest.log;"
if [ "$GZ" = 1 ]; then
  STAGES="$STAGES,-gz=$GZ_IMAGE"
  R="$R docker rm -f $N-gz >/dev/null 2>&1; \
docker run --name $N-gz $COMMON --cpus 4 --memory 8g -e PYTHONPATH=/pylib-gz:/repo/python -e AWR_D3_SUMMARY=/out/summary-gz.json \
  -v $D/pylib-gz:/pylib-gz:ro $GZ_IMAGE sh -c 'timeout $TIMEOUT $PYTEST --junitxml=/out/junit-gz.xml \
  tests/d3/phys -k gz > /out/pytest-gz.log 2>&1'; echo \$? > $OUT/rc-gz.txt; \
tail -n 6 $OUT/pytest-gz.log;"
fi
R="$R G=\$(nvidia-smi -i $GPU --query-gpu=name,driver_version --format=csv,noheader 2>/dev/null); \
I=\$(docker image inspect -f '{{.Id}}' $IMAGE 2>/dev/null); J=\$(docker image inspect -f '{{.Id}}' $GZ_IMAGE 2>/dev/null); \
docker rm -f $N-sum >/dev/null 2>&1; \
docker run --name $N-sum --rm --network none --user 1000:1000 --cpus 1 --memory 1g -v $SRC:/repo:ro -v $OUT:/out \
  -e D3T_TAG=$TAG -e D3T_SHA=$SHA -e D3T_DIRTY=$DIRTY -e D3T_GPU=$GPU -e D3T_GPU_NAME=\"\$G\" -e D3T_IMAGE=$IMAGE -e D3T_IMAGE_SENSORSVC=\"\$I\" \
  -e D3T_IMAGE_GZ=\"\$J\" -e D3T_REQUIRED_TIERS=$REQUIRE -e D3T_PYTEST_ARGS=\"$*\" -e D3T_STARTED_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ) \
  $IMAGE python3 /repo/tools/deploy/dmax/d3_tests_summary.py --out /out --stages=$STAGES"
set +e
ssh dmax "$R"
RC=$?
set -e
rsync -a --mkpath "dmax:$OUT/" "$OUT_LOCAL/"
echo "[d3-tests] results: dmax:$OUT  local: $OUT_LOCAL"
exit $RC
