#!/usr/bin/env bash
# 获取 Trivy 与离线漏洞库并同步到 dmax（ADR-204；D3-AC-59）。dmax 容器无 DNS，扫描只用离线库：
#   1. 本机下载 Trivy v0.75.0 静态二进制（GitHub 发布页），按发布的 checksums 文件校验 sha256；
#   2. 本机以该二进制下载 trivy-db 与 trivy-java-db（`--download-db-only`、`--download-java-db-only`），连同 metadata.json；
#   3. 二进制同步到 dmax `$D/tools/trivy/`，库同步到 `$D/trivy-cache/{db,java-db}/`（扫描容器只读挂载）。
# 刷新：每周一次，以及每个榜单版本正式评测前一次（`--db-only` 只刷新库）；库龄 > 7 天告警，> 30 天 subworker 拒绝扫描
# （`awr.bench.subworker.TrivyScanner`）。下载暂存在 $STAGE（缺省 var/trivy，不入库）。
# 用法（本机仓库根目录）：tools/deploy/dmax/images/trivy/fetch.sh [--db-only] [--no-sync]；之后 build.sh 构建 d3-trivy 镜像。
set -euo pipefail
VER=0.75.0
TGZ=trivy_${VER}_Linux-64bit.tar.gz
SHA=c6e65abddb348e25f10549df887045629cf28cc72453cd1c63acb717316b3f3f    # 发布页 trivy_0.75.0_checksums.txt 对应行（2026-10-18 核对）
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../../../.." && pwd)
STAGE=${STAGE:-$ROOT/var/trivy}
D=/data/anet-drone4d-d3/d3dev
DB_ONLY=0
SYNC=1
for a in "$@"; do
  case $a in
    --db-only) DB_ONLY=1 ;;
    --no-sync) SYNC=0 ;;
    *) echo "unknown argument: $a" >&2; exit 2 ;;
  esac
done
mkdir -p "$STAGE/bin" "$STAGE/cache"
if [ "$DB_ONLY" = 0 ] || [ ! -x "$STAGE/bin/trivy" ]; then
  URL=https://github.com/aquasecurity/trivy/releases/download/v$VER
  curl -fsSL -o "$STAGE/$TGZ" "$URL/$TGZ"
  curl -fsSL -o "$STAGE/checksums.txt" "$URL/trivy_${VER}_checksums.txt"
  grep -q "^$SHA  $TGZ\$" "$STAGE/checksums.txt" || { echo "checksums.txt does not list the pinned sha256 for $TGZ" >&2; exit 1; }
  echo "$SHA  $STAGE/$TGZ" | sha256sum -c --quiet -
  tar -xzf "$STAGE/$TGZ" -C "$STAGE/bin" trivy
fi
"$STAGE/bin/trivy" --version | grep -q "Version: $VER" || { echo "trivy binary is not v$VER" >&2; exit 1; }
rm -rf "$STAGE/cache/db" "$STAGE/cache/java-db"
"$STAGE/bin/trivy" image --download-db-only --cache-dir "$STAGE/cache" --quiet
"$STAGE/bin/trivy" image --download-java-db-only --cache-dir "$STAGE/cache" --quiet
cat "$STAGE/cache/db/metadata.json"; echo
cat "$STAGE/cache/java-db/metadata.json"; echo
if [ "$SYNC" = 1 ]; then
  ssh dmax "mkdir -p $D/tools/trivy $D/trivy-cache"
  if [ "$DB_ONLY" = 0 ]; then
    rsync -a --no-o --no-g "$STAGE/bin/trivy" "dmax:$D/tools/trivy/trivy"
    ssh dmax "echo 'v$VER $SHA $TGZ' > $D/tools/trivy/VERSION && sha256sum $D/tools/trivy/trivy >> $D/tools/trivy/VERSION && $D/tools/trivy/trivy --version | head -1"
  fi
  # 库目录只属于本项目：先写入临时目录再整体替换，扫描进行中不会读到半新半旧的库
  rsync -a --no-o --no-g "$STAGE/cache/db/" "dmax:$D/trivy-cache/db.new/"
  rsync -a --no-o --no-g "$STAGE/cache/java-db/" "dmax:$D/trivy-cache/java-db.new/"
  ssh dmax "cd $D/trivy-cache && rm -rf db.old java-db.old && { [ ! -d db ] || mv db db.old; } && mv db.new db \
    && { [ ! -d java-db ] || mv java-db java-db.old; } && mv java-db.new java-db && rm -rf db.old java-db.old && chmod -R a+rX . && cat db/metadata.json"
fi
