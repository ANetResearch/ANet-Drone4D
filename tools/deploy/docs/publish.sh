#!/usr/bin/env bash
# tools/deploy/docs/publish.sh（D2.2-D1）：准备 Drone4D 文档站的待发布目录，并打印上传与校验命令。本脚本不上传、不连接任何服务器。
#
#   tools/deploy/docs/publish.sh [--host <ssh 主机>] [--out DIR] [--build DIR]
#
# 输入：make docs-site 的产物 docs-site/_build/html/（index.html 根跳转页、en/latest/、zh-cn/latest/）。
# 输出（缺省 .cache/deploy/docs/drone4d/）：待发布目录的完整副本，去掉构建缓存文件（.buildinfo、_sources/），附 MANIFEST.sha256。
# 目标：文档服务器上的 /var/www/docs.agentnetwork/public/drone4d/（属主 www-data）。docs.agentnetwork.org.cn 是同一根目录下的
# 静态站点；同步只作用于 drone4d/ 子目录，不删除、不改动该根目录中 drone4d 之外的任何内容，也不需要修改 nginx 配置。
# 复核：三个入口页存在；单文件不超过 5 MB；不含本机部署口令（若本机存在口令文件，只做不回显的包含检查）。
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
BUILD="$ROOT/docs-site/_build/html"
OUT="$ROOT/.cache/deploy/docs/drone4d"
HOST="emax"
REMOTE_ROOT="/var/www/docs.agentnetwork/public"
REMOTE_DIR="$REMOTE_ROOT/drone4d"
SITE_URL="https://docs.agentnetwork.org.cn/drone4d"
MAX_BYTES=$((5 * 1024 * 1024))

usage() { sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --build) BUILD="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数：$1" >&2; usage >&2; exit 2 ;;
  esac
done

fail() { echo "错误：$1" >&2; [ -n "${2:-}" ] && echo "修复：$2" >&2; exit 1; }

# ---- 1 输入检查 ---------------------------------------------------------------
for f in index.html en/latest/index.html zh-cn/latest/index.html en/latest/search.html zh-cn/latest/search.html; do
  [ -f "$BUILD/$f" ] || fail "缺少 $BUILD/$f" "make docs-site"
done
case "$OUT" in
  */drone4d) ;;
  *) fail "--out 必须以 /drone4d 结尾（与线上目录同名，避免同步到错误的目录）：$OUT" ;;
esac

# ---- 2 准备待发布目录 ---------------------------------------------------------
rm -rf "$OUT"
mkdir -p "$OUT"
cp -a "$BUILD/." "$OUT/"
find "$OUT" -name .buildinfo -type f -delete
find "$OUT" -type d -name _sources -prune -exec rm -rf {} +
find "$OUT" -type d -exec chmod 0755 {} +
find "$OUT" -type f -exec chmod 0644 {} +

# ---- 3 复核 -------------------------------------------------------------------
big=$(find "$OUT" -type f -size +"${MAX_BYTES}"c -printf '%P\n' | head -5)
[ -z "$big" ] || fail "存在超过 5 MB 的文件：$big"
for secret in "${AWR_ADMIN_SECRET_FILE:-}" "$HOME/.config/awr/demo-admin.secret" "$HOME/.config/awr/admin.secret"; do
  [ -n "$secret" ] && [ -s "$secret" ] || continue
  [ "$(head -n 1 "$secret" | wc -c)" -gt 1 ] || continue
  if grep -rqsF -f <(head -n 1 "$secret") "$OUT"; then
    fail "待发布目录中出现部署口令文件的内容（$secret）；内容不在此回显" "检查 docs-site 源文件，删除口令后重新 make docs-site"
  fi
done

( cd "$OUT" && find . -type f ! -name MANIFEST.sha256 -printf '%P\n' | LC_ALL=C sort | xargs -d '\n' sha256sum > MANIFEST.sha256 )
files=$(wc -l < "$OUT/MANIFEST.sha256")
size=$(du -sh "$OUT" | cut -f1)
stamp=$(date -u +%Y%m%d-%H%M%S)

# ---- 4 打印上传与校验命令（不执行） ---------------------------------------------
cat <<EOF
待发布目录已就绪：$OUT（$files 个文件，$size）

以下命令供有权限的运维人员执行（本脚本不执行任何上传）。目标只限 $REMOTE_DIR/：
--delete 只作用于该子目录，$REMOTE_ROOT 下 drone4d 之外的内容不受影响。

  # 1 上传到服务器的临时目录
  rsync -rlptz --delete --chmod=D0755,F0644 "$OUT/" $HOST:/tmp/drone4d-docs-$stamp/

  # 2 在服务器上校验并同步到站点目录（属主 www-data）
  ssh -t $HOST 'set -e; cd /tmp/drone4d-docs-$stamp && sha256sum --quiet -c MANIFEST.sha256 && \\
    sudo install -d -o www-data -g www-data -m 0755 $REMOTE_DIR && \\
    sudo rsync -rlpt --delete --chown=www-data:www-data /tmp/drone4d-docs-$stamp/ $REMOTE_DIR/ && \\
    rm -rf /tmp/drone4d-docs-$stamp'

  # 3 验证
  curl -sI $SITE_URL/ | head -1
  curl -sI $SITE_URL/en/latest/ | head -1
  curl -sI $SITE_URL/zh-cn/latest/intro/quickstart.html | head -1
EOF
