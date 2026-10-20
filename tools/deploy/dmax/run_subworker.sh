#!/usr/bin/env bash
# 在 dmax 上运行内部提交流水线（`python -m awr.bench.subworker`，d3-gz 容器，宿主与 gz 同容器，Agent 为隔离容器）。
# 用法（本机仓库根目录）：tools/deploy/dmax/run_subworker.sh [--once] [其它 subworker 参数]；后台运行，日志 <数据根>/subworker.log。
# 数据根（B25 遗留：与 bench-api、d3-registry 鉴权统一）：缺省为内部工作区的 $LAB_DATA（lab/lab.env），受邀与内部成员经 lab-web 登记的
#   提交、bench token 表与审计都在此；以相同绝对路径挂载，进程以 $LAB_UID 运行（与 d3 profile 容器同一属主，另加入 docker 套接字所属组）。
#   BENCH_ROOT=<名> 时改用旧开发数据根 $D/<名>（如 benchroot，以 ssh 用户运行）。数据根须已发布榜单（bench_ctl.sh board publish … --current）。
# 镜像接入（D3-AC-59）：MATERIALIZE=1（缺省）时另启动伴随容器 d3-subworker-materialize（--network host，只访问回环上的 d3-registry 后端
#   127.0.0.1:$REG_BACKEND_PORT 与宿主 docker），每 ${MATERIALIZE_INTERVAL:-5} s 执行 registry/regctl.py materialize，把推送到 d3-registry 的
#   镜像拉到宿主并写 submissions/<id>/registry.json；subworker 以 --registry-gate all 运行（没有 pulled 或 present 接入记录的提交不领取，
#   受邀成员的他队镜像 692 foreign_image）。MATERIALIZE=0 时不启动伴随容器，gate 为 invited（只约束受邀成员）。
# Agent 套接字放在 $D/run/as-<uid>（AF_UNIX 路径上限）。
# 漏洞扫描（D3-AC-59，ADR-204）：离线库 $TRIVY_CACHE 只读挂载，扫描镜像 d3-trivy:dev（images/trivy/fetch.sh 与 build.sh）。
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
# shellcheck source=registry/registry.env
. "$HERE/registry/registry.env"
D=/data/anet-drone4d-d3/d3dev
CPUS=${CPUS:-8}
MEM=${MEM:-32g}
MATERIALIZE=${MATERIALIZE:-1}
if [ -n "${BENCH_ROOT:-}" ]; then
  DR=$D/$BENCH_ROOT
  RUN_AS='$(id -u):$(id -g)'
  SOCK=$D/run/as
else
  DR=$LAB_DATA
  RUN_AS="$LAB_UID:$LAB_UID --group-add \$(stat -c %g /var/run/docker.sock)"
  SOCK=$D/run/as-$LAB_UID
fi
GATE=invited
[ "$MATERIALIZE" = 1 ] && GATE=all
rsync -a --delete --exclude __pycache__ "$ROOT/python/" "dmax:$D/python/"
rsync -a --mkpath --delete "$ROOT/packages/contracts/" "dmax:$D/contracts/"
rsync -a --mkpath "$ROOT/native/gz/proto/" "dmax:$D/native/gz/proto/"
rsync -a --mkpath "$HERE/registry/regctl.py" "dmax:$REG_ROOT/conf/"
ssh dmax "mkdir -p $DR $SOCK && { [ -n '${BENCH_ROOT:-}' ] || chown $LAB_UID:$LAB_UID $SOCK; } && docker rm -f d3-subworker d3-subworker-materialize >/dev/null 2>&1; \
if [ $MATERIALIZE = 1 ]; then docker run -d --rm --name d3-subworker-materialize --label d3.component=subworker --network host --cpus 0.5 --memory 512m \
  --cgroup-parent d3.slice --read-only --tmpfs /tmp:rw,size=64m --cap-drop ALL --security-opt no-new-privileges --user $RUN_AS \
  -v /var/run/docker.sock:/var/run/docker.sock -v /usr/bin/docker:/usr/bin/docker:ro -v $DR:$DR -v $REG_ROOT/conf/regctl.py:/opt/awr/registry/regctl.py:ro \
  $REG_AUTHZ_IMAGE python /opt/awr/registry/regctl.py materialize --data-root $DR --registry 127.0.0.1:$REG_BACKEND_PORT \
  --interval ${MATERIALIZE_INTERVAL:-5} --out $DR/registry-materialize.json; fi && \
docker run -d --rm --name d3-subworker --network none --cpus $CPUS --memory $MEM --cgroup-parent d3.slice \
  --user $RUN_AS -e HOME=/tmp -e PYTHONPATH=/repo/pylib-gz:/repo/python -e AWR_CONTRACTS_DIR=/repo/contracts \
  -e AWR_HOST_PATH_MAP=/repo=$D -e AWR_AGENT_SOCK_ROOT=$SOCK -e AWR_TRIVY_CACHE=/repo/trivy-cache -e AWR_REGISTRY_GATE=$GATE \
  -v /var/run/docker.sock:/var/run/docker.sock -v /usr/bin/docker:/usr/bin/docker:ro -v $D:/repo -v $DR:$DR -v $SOCK:$SOCK \
  -v $TRIVY_CACHE:/repo/trivy-cache:ro -w /repo d3-gz:dev \
  sh -c 'python3 -m awr.bench.subworker --data-root $DR $* >> $DR/subworker.log 2>&1'"
