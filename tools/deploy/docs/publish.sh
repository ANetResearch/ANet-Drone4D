#!/usr/bin/env bash
# tools/deploy/docs/publish.sh（D2.2-D1）：准备 ANet4D 文档站的待发布目录与旧路径跳转目录，并打印上传与校验命令。本脚本不上传、
# 不连接任何服务器。
#
#   tools/deploy/docs/publish.sh [--host <ssh 主机>] [--out DIR] [--build DIR]
#
# 输入：make docs-site 的产物 docs-site/_build/html/（index.html 根跳转页、en/latest/、zh-cn/latest/）。
# 输出（缺省 .cache/deploy/docs/anet4d/）：待发布目录的完整副本，去掉构建缓存文件（.buildinfo、_sources/），附 MANIFEST.sha256；
# 同级的 drone4d/ 为旧路径跳转目录：待发布目录中的每个 HTML 页面在同一相对路径上生成一个静态跳转页（meta refresh 加
# rel=canonical，浏览器执行脚本时保留查询串与锚点），指向 https://docs.agentnetwork.org.cn/anet4d/ 下的同一页面，附 MANIFEST.sha256。
# 目标：文档服务器上的 /var/www/docs.agentnetwork/public/anet4d/ 与 /var/www/docs.agentnetwork/public/drone4d/（属主 www-data）。
# docs.agentnetwork.org.cn 是同一根目录下的静态站点；同步只作用于这两个子目录，不删除、不改动该根目录中其他任何内容，也不需要
# 修改 nginx 配置。旧路径 /drone4d/ 下的非 HTML 文件（样式、脚本、图片、搜索索引）不再提供。
# 复核：三个入口页存在；单文件不超过 5 MB；不含本机部署口令（若本机存在口令文件，只做不回显的包含检查）。
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
BUILD="$ROOT/docs-site/_build/html"
OUT="$ROOT/.cache/deploy/docs/anet4d"
HOST="emax"
REMOTE_ROOT="/var/www/docs.agentnetwork/public"
REMOTE_DIR="$REMOTE_ROOT/anet4d"
LEGACY_REMOTE_DIR="$REMOTE_ROOT/drone4d"          # 旧路径：只放跳转页
SITE_URL="https://docs.agentnetwork.org.cn/anet4d"
LEGACY_URL="https://docs.agentnetwork.org.cn/drone4d"
MAX_BYTES=$((5 * 1024 * 1024))

usage() { sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

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
  */anet4d) ;;
  *) fail "--out 必须以 /anet4d 结尾（与线上目录同名，避免同步到错误的目录）：$OUT" ;;
esac
LEGACY_OUT="$(dirname "$OUT")/drone4d"

# ---- 2 准备待发布目录 ---------------------------------------------------------
rm -rf "$OUT"
mkdir -p "$OUT"
cp -a "$BUILD/." "$OUT/"
find "$OUT" -name .buildinfo -type f -delete
find "$OUT" -type d -name _sources -prune -exec rm -rf {} +
find "$OUT" -type d -exec chmod 0755 {} +
find "$OUT" -type f -exec chmod 0644 {} +

# ---- 3 旧路径跳转目录（/drone4d/ → /anet4d/，同一相对路径） -----------------------
rm -rf "$LEGACY_OUT"
mkdir -p "$LEGACY_OUT"
python3 - "$OUT" "$LEGACY_OUT" "$SITE_URL" <<'PY'
import html
import json
import sys
from pathlib import Path

src, dst, base = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3].rstrip("/")
n = 0
for f in sorted(src.rglob("*.html")):
    rel = f.relative_to(src).as_posix()
    path = rel[: -len("index.html")] if rel == "index.html" or rel.endswith("/index.html") else rel
    target = f"{base}/{path}"
    t = html.escape(target, quote=True)
    page = (
        '<!doctype html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        "<title>ANet4D 文档已迁移 / ANet4D documentation has moved</title>\n"
        f'<link rel="canonical" href="{t}">\n<meta name="robots" content="noindex">\n'
        f'<meta http-equiv="refresh" content="0; url={t}">\n'
        f"<script>location.replace({json.dumps(target)} + location.search + location.hash);</script>\n"
        "</head>\n<body>\n"
        f'<p>ANet4D 文档已迁移到 <a href="{t}">{t}</a>。</p>\n'
        f'<p>The ANet4D documentation has moved to <a href="{t}">{t}</a>.</p>\n'
        "</body>\n</html>\n"
    )
    out = dst / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    n += 1
if n == 0:
    sys.exit("没有可生成跳转页的 HTML 页面")
print(f"旧路径跳转页：{n} 个")
PY
find "$LEGACY_OUT" -type d -exec chmod 0755 {} +
find "$LEGACY_OUT" -type f -exec chmod 0644 {} +

# ---- 4 复核 -------------------------------------------------------------------
big=$(find "$OUT" -type f -size +"${MAX_BYTES}"c -printf '%P\n' | head -5)
[ -z "$big" ] || fail "存在超过 5 MB 的文件：$big"
for secret in "${AWR_ADMIN_SECRET_FILE:-}" "$HOME/.config/awr/demo-admin.secret" "$HOME/.config/awr/admin.secret"; do
  [ -n "$secret" ] && [ -s "$secret" ] || continue
  [ "$(head -n 1 "$secret" | wc -c)" -gt 1 ] || continue
  if grep -rqsF -f <(head -n 1 "$secret") "$OUT" "$LEGACY_OUT"; then
    fail "待发布目录中出现部署口令文件的内容（$secret）；内容不在此回显" "检查 docs-site 源文件，删除口令后重新 make docs-site"
  fi
done

for d in "$OUT" "$LEGACY_OUT"; do
  ( cd "$d" && find . -type f ! -name MANIFEST.sha256 -printf '%P\n' | LC_ALL=C sort | xargs -d '\n' sha256sum > MANIFEST.sha256 )
done
files=$(wc -l < "$OUT/MANIFEST.sha256")
lfiles=$(wc -l < "$LEGACY_OUT/MANIFEST.sha256")
size=$(du -sh "$OUT" | cut -f1)
stamp=$(date -u +%Y%m%d-%H%M%S)

# ---- 5 打印上传与校验命令（不执行） ---------------------------------------------
cat <<EOF
待发布目录已就绪：$OUT（$files 个文件，$size）
旧路径跳转目录已就绪：$LEGACY_OUT（$lfiles 个文件）

以下命令供有权限的运维人员执行（本脚本不执行任何上传）。目标只限 $REMOTE_DIR/ 与 $LEGACY_REMOTE_DIR/：
--delete 只作用于这两个子目录，$REMOTE_ROOT 下其他内容不受影响。先发布新路径并验证，再把旧路径换成跳转页。

  # 1 上传到服务器的临时目录
  rsync -rlptz --delete --chmod=D0755,F0644 "$OUT/" $HOST:/tmp/anet4d-docs-$stamp/
  rsync -rlptz --delete --chmod=D0755,F0644 "$LEGACY_OUT/" $HOST:/tmp/anet4d-docs-legacy-$stamp/

  # 2 在服务器上校验并同步新路径（属主 www-data），然后验证
  ssh -t $HOST 'set -e; cd /tmp/anet4d-docs-$stamp && sha256sum --quiet -c MANIFEST.sha256 && \\
    sudo install -d -o www-data -g www-data -m 0755 $REMOTE_DIR && \\
    sudo rsync -rlpt --delete --chown=www-data:www-data /tmp/anet4d-docs-$stamp/ $REMOTE_DIR/ && \\
    rm -rf /tmp/anet4d-docs-$stamp'
  curl -sI $SITE_URL/ | head -1
  curl -sI $SITE_URL/en/latest/ | head -1
  curl -sI $SITE_URL/zh-cn/latest/intro/quickstart.html | head -1

  # 3 新路径正常后：把旧目录备份到站点根目录之外（回滚用），再换成跳转页
  ssh -t $HOST 'set -e; cd /tmp/anet4d-docs-legacy-$stamp && sha256sum --quiet -c MANIFEST.sha256 && \\
    sudo install -d -m 0700 /var/www/docs.agentnetwork/backup && \\
    { [ ! -d $LEGACY_REMOTE_DIR ] || sudo cp -a $LEGACY_REMOTE_DIR /var/www/docs.agentnetwork/backup/drone4d-$stamp; } && \\
    sudo install -d -o www-data -g www-data -m 0755 $LEGACY_REMOTE_DIR && \\
    sudo rsync -rlpt --delete --chown=www-data:www-data /tmp/anet4d-docs-legacy-$stamp/ $LEGACY_REMOTE_DIR/ && \\
    rm -rf /tmp/anet4d-docs-legacy-$stamp'

  # 4 验证旧路径：返回跳转页，canonical 指向新路径的同一页面
  curl -s $LEGACY_URL/zh-cn/latest/intro/quickstart.html | grep -o 'rel="canonical" href="[^"]*"'
  curl -s $LEGACY_URL/ | grep -o 'rel="canonical" href="[^"]*"'

  # 回滚旧路径（恢复更名前的目录）：
  #   ssh -t $HOST 'sudo rsync -a --delete /var/www/docs.agentnetwork/backup/drone4d-$stamp/ $LEGACY_REMOTE_DIR/'
EOF
