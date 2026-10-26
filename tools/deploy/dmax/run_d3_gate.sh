#!/usr/bin/env bash
# D3 回归门重载部分在 dmax 运行（D3-AC-01；AWR-05 §4.8；封板任务 G05、K14）：D2 Python 测试全集（不含 tests/d3，由 I01 承担）、零影响对拍（relay_batch 1 h ×
# 两种开关条件）、resim 帧摘要对拍、D2 性能子集（G04）与 import 方向，在 dmax CPU 池容器（d3-agent-py:dev，Python 3.12，不挂 GPU，d3.slice）中
# 以 D2 锁文件的离线 venv 执行 tools/bench/d3/regress_gate.py。轻量部分（契约、前端单测、lint、构建与产物扫描）由托管 CI 与
# 本机 make gate-d3-light 承担；两部分的报告以同一 tree id 为键合并（make gate-d3-check GATE_TIER=full）。
#
# 被检查的内容只取提交（git archive REF），不取工作树：证据须对应一个可复现的提交。
# 产物：dmax $D/gate/out/<tag>/heavy/ 与本机 $GATE_D3_ROOT/<tag>/heavy/（缺省 runs/perf/d3-gate，不入库）：report.json
# （awr.d3.regress_gate.report.v1）、meta.json（awr.d3.regress_gate.meta.v1，runner = dmax）、gate.log、setup.log、各步骤日志。
# <tag> = 提交的 git tree id（12 位），与 mk/d3.mk 的本机报告相同。
#
# 用法（本机仓库根目录）：tools/deploy/dmax/run_d3_gate.sh（make gate-d3-dmax、make gate-d3-nightly 调用）
# 环境变量：
#   REF=HEAD          被检查的提交（任意 git 版本表达式，如 origin/main）
#   STEPS=pytest,zero-impact,resim,perf,imports    gate.json 的步骤 id（逗号分隔）
#   TASK_ID=main      实例名：容器 d3-test-gate-<id>、快照 $D/gate/src-<id>（小写字母、数字、点、下划线、连字符）
#   CPUS=16 MEM=48g   容器资源上限；TIMEOUT=21600 容器内总时长上限（秒）
#   NOFILE=1048576    容器打开文件数上限（--ulimit nofile；缺省上限下 D2 全集会话收尾报 Too many open files，F-114）
#   IMAGE=d3-agent-py:dev  容器镜像（只取 Python 3.12 解释器，依赖为 D2 锁文件的离线 venv；perf 基线的主机指纹含 Python 版本）
#   SHM=2g            容器 /dev/shm 大小（D2 doctor 用例 DOC-10 要求 ≥ 1 GiB；Docker 缺省 64 MB）
#   SYNC=1            同步源码快照、D2 wheels 与 Node；0 复用 dmax 上已有的快照与环境（只重新运行）
#   WAIT=1            等待容器结束并回传证据；0 只启动容器（后台运行）即返回，之后以 COLLECT=1（同一 REF 与 TASK_ID）回传
#   COLLECT=0         1：不同步、不启动，只回传 dmax 上该 tag 的证据并写 meta.json
#   WORLDS=1          增量同步本机 worlds/ 到 $D/d3tests/worlds（与 run_d3_tests.sh 共用，只增不删，含 .status 世界状态：缺少时目录
#                     服务把六城判为 stale；.locks、.staging、.trash 只建空目录：Publisher 在只读卷上不能再创建它们），以只读卷挂到 /repo/worlds
#   NODE_DIR          本机 Node 22 目录（缺省 ~/.local/node；部分 D2 用例调用 node）
#   GATE_D3_ROOT      本机证据根（缺省 runs/perf/d3-gate）
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
D=/data/anet-drone4d-d3/d3dev
REF=${REF:-HEAD}
STEPS=${STEPS:-pytest,zero-impact,resim,perf,imports}
TASK_ID=$(printf '%s' "${TASK_ID:-main}" | tr 'A-Z' 'a-z')
CPUS=${CPUS:-16}
MEM=${MEM:-48g}
TIMEOUT=${TIMEOUT:-21600}
NOFILE=${NOFILE:-1048576}
SHM=${SHM:-2g}
IMAGE=${IMAGE:-d3-agent-py:dev}
SYNC=${SYNC:-1}
WAIT=${WAIT:-1}
COLLECT=${COLLECT:-0}
WORLDS=${WORLDS:-1}
NODE_DIR=${NODE_DIR:-$HOME/.local/node}
GATE_D3_ROOT=${GATE_D3_ROOT:-$ROOT/runs/perf/d3-gate}
case "$TASK_ID" in ""|*[!a-z0-9._-]*) echo "TASK_ID must match [a-z0-9._-]+ (got '$TASK_ID')" >&2; exit 2;; esac
case "$STEPS" in ""|*[!a-z0-9,-]*) echo "STEPS must be comma separated gate step ids (got '$STEPS')" >&2; exit 2;; esac
case "$NOFILE" in ""|*[!0-9]*) echo "NOFILE must be a positive integer (got '$NOFILE')" >&2; exit 2;; esac
case "$SHM" in ""|*[!0-9a-z]*) echo "SHM must be a docker size such as 2g (got '$SHM')" >&2; exit 2;; esac
cd "$ROOT"
if [ ! -e .git ]; then export GIT_DIR=${GIT_DIR:-/data/projs/.anet-drone4d.git} GIT_WORK_TREE=${GIT_WORK_TREE:-$ROOT}; fi
export GIT_OPTIONAL_LOCKS=0

# ---------------------------------------------------------------- 被检查的提交与 tag
if SHA=$(git rev-parse --verify -q "$REF^{commit}" 2>/dev/null); then
  TAG=$(git rev-parse "$SHA^{tree}" | cut -c1-12)
else
  echo "cannot resolve REF=$REF" >&2
  exit 2
fi
LOCK_SHA=$(sha256sum requirements.lock | cut -c1-12)
WH=$D/wheels-d2-$LOCK_SHA
NODE_VER=$( [ -x "$NODE_DIR/bin/node" ] && "$NODE_DIR/bin/node" --version || echo none)
NODE=$D/gate/node-$NODE_VER
SRC=$D/gate/src-$TASK_ID
OUT=$D/gate/out/$TAG/heavy
OUT_LOCAL=$GATE_D3_ROOT/$TAG/heavy
N=d3-test-gate-$TASK_ID                                       # 组件 test（dmax 测试容器，CM-06 组件表），CPU 池
T0=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "[d3-gate-dmax] ref $REF ($SHA)  tag $TAG  steps $STEPS  cpus $CPUS  mem $MEM  sync $SYNC  worlds $WORLDS"

# ---------------------------------------------------------------- 同步：源码快照（git archive）、D2 wheels、Node、世界包
if [ "$SYNC" = 1 ] && [ "$COLLECT" != 1 ]; then
  EXP=$ROOT/.cache/d3-gate/dmax-src-$TASK_ID
  rm -rf "$EXP"; mkdir -p "$EXP"
  git archive --format=tar "$SHA" | tar -x -C "$EXP"
  WH_LOCAL=$ROOT/.cache/d3-gate/wheels-d2-$LOCK_SHA          # requirements.lock 的 cp312 manylinux wheels（逐个校验哈希）
  if [ ! -f "$WH_LOCAL/.complete" ]; then
    mkdir -p "$WH_LOCAL"
    "$ROOT/.venv/bin/python" -m pip download -q --no-deps --require-hashes -r requirements.lock --only-binary=:all: \
      --python-version 3.12 --implementation cp --abi cp312 --abi abi3 --abi none \
      $(for p in manylinux_2_28 manylinux_2_27 manylinux_2_26 manylinux_2_17 manylinux2014 manylinux_2_12 manylinux2010 manylinux_2_5 \
          manylinux1 linux; do printf -- '--platform %s_x86_64 ' "$p"; done) -d "$WH_LOCAL"
    touch "$WH_LOCAL/.complete"
  fi
  ssh dmax "mkdir -p $SRC $WH $NODE $D/gate/out $D/d3tests/worlds"
  rsync -a --checksum --delete --chown=1000:1000 "$EXP/" "dmax:$SRC/"
  rsync -a --chown=1000:1000 "$WH_LOCAL/" "dmax:$WH/"
  if [ "$NODE_VER" != none ]; then rsync -a --chown=1000:1000 "$NODE_DIR/" "dmax:$NODE/"; fi
  if [ "$WORLDS" = 1 ] && [ -d "$ROOT/worlds" ]; then
    rsync -a --chown=1000:1000 --exclude '/.locks/*' --exclude '/.staging/*' --exclude '/.trash/*' "$ROOT/worlds/" "dmax:$D/d3tests/worlds/"
  fi
  rm -rf "$EXP"
fi

# ---------------------------------------------------------------- 远端：离线 venv（D2 锁文件）+ regress_gate.py
INNER="python -m venv /tmp/venv && /tmp/venv/bin/pip install -q --no-index --find-links /wheels --require-hashes -r requirements.lock \
&& /tmp/venv/bin/pip install -q --no-index --no-build-isolation --no-deps -e . && /tmp/venv/bin/python -V && /tmp/venv/bin/pip freeze; \
} > /out/setup.log 2>&1 && timeout $TIMEOUT /tmp/venv/bin/python tools/bench/d3/regress_gate.py --steps $STEPS --out /out > /out/gate.log 2>&1; \
rc=\$?; echo \$rc > /out/rc.txt; exit \$rc"
if [ "$COLLECT" != 1 ]; then
  R="rm -rf $OUT; mkdir -p $OUT; chown 1000:1000 $OUT; echo $T0 2>/dev/null > $OUT/start.txt; docker rm -f $N >/dev/null 2>&1; \
docker run -d --name $N --rm --network none --user 1000:1000 --cpus $CPUS --memory $MEM --cgroup-parent d3.slice \
  --ulimit nofile=$NOFILE:$NOFILE --shm-size $SHM \
  -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 -e NUMBA_CACHE_DIR=/tmp/numba -e PYTHONPATH=/repo/python -e AWR_WORLDS_DIR=/repo/worlds \
  -e AWR_CONTRACTS_DIR=/repo/packages/contracts -e AWR_HOST_PATH_MAP=/repo=$SRC -e PATH=/opt/node/bin:/usr/local/bin:/usr/bin:/bin \
  -v $SRC:/repo -v $D/d3tests/worlds:/repo/worlds:ro -v $WH:/wheels:ro -v $NODE:/opt/node:ro -v $OUT:/out -w /repo \
  --entrypoint sh $IMAGE -c '{ $INNER' >/dev/null || exit 1; docker image inspect -f '{{.Id}}' $IMAGE 2>/dev/null > $OUT/image.txt; exit 0"
  ssh dmax "$R"
  if [ "$WAIT" != 1 ]; then
    echo "[d3-gate-dmax] started $N on dmax (tag $TAG); collect with: REF=$SHA COLLECT=1 TASK_ID=$TASK_ID $0"
    exit 0
  fi
  set +e
  ssh dmax "docker wait $N >/dev/null 2>&1; tail -n 12 $OUT/gate.log 2>/dev/null"
  set -e
fi

# ---------------------------------------------------------------- 回传证据与 meta.json
mkdir -p "$OUT_LOCAL"
set +e
rsync -a "dmax:$OUT/" "$OUT_LOCAL/"
RC=$?
set -e
GRC=$(cat "$OUT_LOCAL/rc.txt" 2>/dev/null || echo 1)          # 没有 rc.txt：容器未跑完或回传失败（按不通过计）
[ "$RC" = 0 ] || GRC=$RC
M_TAG=$TAG M_REF=$REF M_SHA=$SHA M_STEPS=$STEPS M_T0="$(cat "$OUT_LOCAL/start.txt" 2>/dev/null || echo "$T0")" M_RC=$GRC M_SRC="dmax:$SRC" \
M_IMG="$(cat "$OUT_LOCAL/image.txt" 2>/dev/null || true)" M_RES="cpus $CPUS, memory $MEM, no GPU, d3.slice, nofile $NOFILE, shm $SHM" "$ROOT/.venv/bin/python" -c '
import datetime, json, os, sys
e = os.environ
m = {"schema": "awr.d3.regress_gate.meta.v1", "tier": "heavy", "tag": e["M_TAG"], "ref": e["M_REF"], "commit": e["M_SHA"], "dirty": False,
     "tree_changed": False, "tag_after": e["M_TAG"], "steps": e["M_STEPS"].split(","), "started_utc": e["M_T0"].strip(),
     "finished_utc": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "exit": int(e["M_RC"]), "host": "dmax",
     "runner": "dmax", "image": e["M_IMG"].strip(), "resources": e["M_RES"], "src": e["M_SRC"]}
open(sys.argv[1], "w", encoding="utf-8").write(json.dumps(m, indent=1, ensure_ascii=False) + "\n")' "$OUT_LOCAL/meta.json"
echo "[d3-gate-dmax] exit $GRC  evidence dmax:$OUT  local: $OUT_LOCAL"
exit "$GRC"
