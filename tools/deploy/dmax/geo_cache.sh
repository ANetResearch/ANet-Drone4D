#!/usr/bin/env bash
# dmax 世界派生缓存 $D/worlds/.geo-cache 的核对与预生成（K07；A10 遗留 F-78 (b)）。缓存目录属 root、对运行容器只读：正式运行不在线
# 派生，世界包重建（contentVersion 变化）后须先在此预生成；未预生成时 Env 打开网格查询失败，正式运行按 K07 拒绝退回 DSM。
# 用法（本机仓库根目录）：tools/deploy/dmax/geo_cache.sh check | warm [world_id]（缺省全部世界）
#   check  只读探测全部世界（python -m awr.world.geometry.cli check，缺条目退出码 4）
#   warm   以 root 在 d3-gz 容器内预生成（缺省全部世界）；之后复核 check，并以 MeshWorldQuery 打开（正式运行同一路径）
# 代码缺省取 $D/python（run_proto_board.sh 同步；PYSRC=/repo/<子目录>/python 改用其他副本）；依赖取 $D/pylib-gz（images/gz/pylib.sh）。
set -euo pipefail
D=/data/anet-drone4d-d3/d3dev
CMD=${1:?check | warm}; shift || true
RUN="docker run --rm --name d3-worldbuild-geocache-\$\$ --network none --cpus 8 --memory 16g --cgroup-parent d3.slice -e PYTHONPATH=/repo/pylib-gz:${PYSRC:-/repo/python} \
  -e AWR_CONTRACTS_DIR=/repo/contracts -e AWR_GEO_CACHE_DIR=/repo/worlds/.geo-cache -v $D:/repo:ro -v $D/worlds/.geo-cache:/repo/worlds/.geo-cache \
  -w /repo --entrypoint python3 d3-gz:dev"
IDS="--all"
[ -n "${1:-}" ] && IDS="--world $1"
case "$CMD" in
  check) ssh dmax "$RUN -m awr.world.geometry.cli check --worlds /repo/worlds $IDS" ;;
  warm)
    ssh dmax "mkdir -p $D/worlds/.geo-cache && $RUN -m awr.world.geometry.cli warm --strict --worlds /repo/worlds $IDS && \
      $RUN -m awr.world.geometry.cli check --worlds /repo/worlds $IDS && \
      $RUN -c 'import sys; from pathlib import Path; from awr.agentsdk.env.core import open_world_query
for w in sorted(p for p in Path(\"/repo/worlds\").iterdir() if (p / \"world.json\").is_file() and (len(sys.argv) < 2 or p.name == sys.argv[1])):
    q, info = open_world_query(w, formal=True); print(w.name, q.content_version, info)' ${1:-}"
    ;;
  *) echo "unknown command $CMD" >&2; exit 2;;
esac
