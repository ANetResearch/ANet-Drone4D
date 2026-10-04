#!/usr/bin/env bash
# tools/deploy/emax/build-local.sh：在本机构建 ANet Drone4D 公开演示站的部署包（T4，AWR-19 §3.7；ADR-083、ADR-085）。
#
#   tools/deploy/emax/build-local.sh [--with-world] [--out DIR] [--skip-web]
#
# 产物（缺省在 .cache/deploy/emax/）：
#   anet-drone4d-<release>.tar.gz   代码与配置：python/、packages/contracts/、configs/、scenarios/、vehicles/、
#                                   apps/web/dist/（VITE_AWR_DEMO=public 演示构建）、tools/deploy/、pyproject.toml、
#                                   requirements.lock、许可与通知文件；顶层 RELEASE 与 MANIFEST.sha256
#   synthcity-world-<cv>.tar.gz     （--with-world）合成城市 synthcity 的 World Package 与共享环境资产 _shared/env，
#                                   服务器上就不必再花约 25 s 与 1.3 GB 内存生成
# 绝不打包：refs/、.cache/、data/、runs/、worlds/ 中 synthcity 以外的任何世界（UrbanScene3D 条款禁止再分发，ADR-034），
# 以及 apps/web/public/bench（UrbanScene3D 城市的 flight60 夹具；演示构建本就不复制它）。打包后逐项复核，违反即失败。
# 不执行任何 git 写操作；只读 `git rev-parse` 取版本号（没有 git 时为 nogit）。
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
OUT="$ROOT/.cache/deploy/emax"
WITH_WORLD=0
SKIP_WEB=0
WORLDS_DIR=${AWR_WORLDS_DIR:-$ROOT/worlds}

die() { echo "build-local: $*" >&2; exit 1; }
step() { echo "==> $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --with-world) WITH_WORLD=1 ;;
    --skip-web) SKIP_WEB=1 ;;
    --out) OUT=$2; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) die "未知参数 $1（--with-world | --skip-web | --out DIR）" ;;
  esac
  shift
done

command -v node >/dev/null || die "需要 Node 22（npm ci 之后的 apps/web 构建环境）"
command -v rsync >/dev/null || die "需要 rsync"
[ -z "${VITE_AWR_TEST_SWITCHES:-}" ] || die "VITE_AWR_TEST_SWITCHES 已设置：演示包中不得有测试开关，请在干净的环境中运行"

SHA=$(git -C "$ROOT" rev-parse --short=10 HEAD 2>/dev/null || echo nogit)
DIRTY=""
if [ "$SHA" != nogit ] && [ -n "$(git -C "$ROOT" status --porcelain 2>/dev/null | head -1)" ]; then DIRTY="-dirty"; fi
REL="$(date +%Y%m%d-%H%M%S)-${SHA}${DIRTY}"
NAME="anet-drone4d-$REL"
STAGE="$OUT/stage/$NAME"
rm -rf "$OUT/stage"
mkdir -p "$STAGE"

# ---- 1 前端演示构建（VITE_AWR_DEMO=public；持全局性能锁的共享锁，不打扰正在进行的性能用例，AWR-19 §7.5） ----------
WEB_OUT="$STAGE/apps/web/dist"
if [ "$SKIP_WEB" -eq 0 ]; then
  step "前端演示构建 → $WEB_OUT"
  build() { (cd "$ROOT/apps/web" && VITE_AWR_DEMO=public npx vite build --outDir "$WEB_OUT" --emptyOutDir --logLevel warn); }
  LOCK="${AWR_PERF_LOCK:-$ROOT/runs/.perf.lock}"
  if command -v flock >/dev/null && [ -z "${AWR_PERF_LOCK_HELD:-}" ]; then
    mkdir -p "$(dirname "$LOCK")"
    export -f build; export ROOT WEB_OUT
    AWR_PERF_LOCK_HELD="sh" flock -s -w 1800 "$LOCK" bash -c build
  else
    build
  fi
else
  [ -d "$ROOT/.cache/deploy/emax/web-dist" ] || die "--skip-web 需要已有的 .cache/deploy/emax/web-dist"
  mkdir -p "$STAGE/apps/web" && cp -a "$ROOT/.cache/deploy/emax/web-dist" "$WEB_OUT"
fi
step "生产包扫描（M06-AC-010：无测试开关）"
node "$ROOT/apps/web/tests/m06/lint/prod-bundle-scan.mjs" "$WEB_OUT"
grep -q 'data-landing' "$WEB_OUT/index.html" || die "dist/index.html 不是演示构建（缺少落地页标记）"
[ ! -e "$WEB_OUT/bench" ] || die "演示构建不得包含 dist/bench（UrbanScene3D 城市的 flight60 夹具）"
[ -f "$WEB_OUT/demo/drone4d-banner.jpg" ] && [ -f "$WEB_OUT/demo/demo-flight.webp" ] || die "落地页媒体缺失（dist/demo）"
mkdir -p "$OUT" && rm -rf "$OUT/web-dist" && cp -a "$WEB_OUT" "$OUT/web-dist"

# ---- 2 运行所需文件 ------------------------------------------------------------------------------------------
step "复制代码与配置"
RS=(rsync -a --delete --exclude '__pycache__/' --exclude '*.pyc' --exclude '*.egg-info/' --exclude 'node_modules/')
mkdir -p "$STAGE/packages" "$STAGE/tools/deploy"
"${RS[@]}" "$ROOT/python/" "$STAGE/python/"
"${RS[@]}" "$ROOT/packages/contracts/" "$STAGE/packages/contracts/"
"${RS[@]}" "$ROOT/configs/" "$STAGE/configs/"
"${RS[@]}" "$ROOT/scenarios/" "$STAGE/scenarios/"
"${RS[@]}" "$ROOT/vehicles/" "$STAGE/vehicles/"
"${RS[@]}" "$ROOT/tools/deploy/" "$STAGE/tools/deploy/"
for f in pyproject.toml requirements.lock requirements.in LICENSE NOTICE THIRD_PARTY_NOTICES.md README.md README.zh-CN.md; do
  [ -f "$ROOT/$f" ] && cp -a "$ROOT/$f" "$STAGE/$f"
done
ls "$STAGE"/vehicles/p600/model/*.glb >/dev/null 2>&1 || echo "警告：vehicles/p600/model/*.glb 不存在（make setup 生成）；前端会退回 dist/models 的副本" >&2

# ---- 3 合规复核（发布集合同口径，ADR-079 REL-02） --------------------------------------------------------------------
step "合规复核"
for d in worlds data refs .cache runs apps/web/public node_modules; do
  [ ! -e "$STAGE/$d" ] || die "包内不得出现 $d/"
done
BAD=$(find "$STAGE" -type f \( -iname '*.ply' -o -iname '*.las' -o -iname '*.laz' -o -iname '*.e57' -o -iname '*.pcd' \
      -o -name 'octree.bin' -o -name 'hierarchy.bin' \) | head -5)
[ -z "$BAD" ] || die "包内出现点云或世界数据：$BAD"
BIG=$(find "$STAGE" -type f -size +5000000c | head -5)
[ -z "$BIG" ] || die "包内有超过 5 MB 的文件：$BIG"
if [ -d "$HOME/.config/awr" ]; then
  for envf in "$HOME"/.config/awr/*.env; do
    [ -f "$envf" ] || continue
    while IFS='=' read -r k v; do
      v=${v%\"}; v=${v#\"}
      [ ${#v} -ge 12 ] || continue
      if grep -rqF -- "$v" "$STAGE" 2>/dev/null; then die "包内出现本机凭据（$(basename "$envf") 的 $k）"; fi
    done < <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$envf" || true)
  done
fi

# ---- 4 版本、清单与打包 ----------------------------------------------------------------------------------------------
step "清单与打包"
{
  echo "release=$REL"
  echo "git=$SHA$DIRTY"
  echo "built_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "web=VITE_AWR_DEMO=public"
} > "$STAGE/RELEASE"
(cd "$STAGE" && find . -type f -print0 | sort -z | xargs -0 sha256sum > "$OUT/MANIFEST.tmp" && mv "$OUT/MANIFEST.tmp" MANIFEST.sha256)
TAR="$OUT/$NAME.tar.gz"
tar -C "$OUT/stage" -czf "$TAR" "$NAME"
echo "    $(du -h "$TAR" | cut -f1)  $TAR"

# ---- 5 可选：synthcity 世界包 -----------------------------------------------------------------------------------------
WORLD_TAR=""
if [ "$WITH_WORLD" -eq 1 ]; then
  step "synthcity 世界包"
  WJ="$WORLDS_DIR/synthcity/world.json"
  [ -f "$WJ" ] || die "$WJ 不存在：先 make demo-world"
  CV=$(python3 - "$WJ" <<'PY'
import json, sys
w = json.load(open(sys.argv[1], encoding="utf-8"))
assert w.get("id") == "synthcity", "world.json id 不是 synthcity"
assert (w.get("dataset") or {}).get("redistribution") is True, "synthcity 未声明可再分发（dataset.redistribution）"
print(w["contentVersion"])
PY
) || die "synthcity 世界包校验失败"
  [ -d "$WORLDS_DIR/_shared/env" ] || die "$WORLDS_DIR/_shared/env 不存在：先 make env-assets"
  # 构建状态记录（.status/synthcity.json）一并带上：目录服务只有在它与 world.json 的 contentVersion 一致时才报 ready（否则
  # 世界列表显示"待重建"、打开按钮禁用）；服务器上的 `worldpkg build synthcity --missing` 据此判定无需重建
  ST="$WORLDS_DIR/.status/synthcity.json"
  python3 - "$ST" "$CV" <<'PY' || die "$ST 不存在或不是 ready（先 make demo-world）"
import json, sys
st = json.load(open(sys.argv[1], encoding="utf-8"))
assert st.get("status") == "ready" and st.get("content_version") == sys.argv[2][:len(st.get("content_version") or "")]
PY
  WORLD_TAR="$OUT/synthcity-world-${CV:0:12}.tar.gz"
  tar -C "$WORLDS_DIR" -czf "$WORLD_TAR" synthcity _shared/env .status/synthcity.json
  echo "    $(du -h "$WORLD_TAR" | cut -f1)  $WORLD_TAR"
fi

cat <<EOF

完成：$NAME
上传与安装（在本机执行前两条，在 emax 上执行第三条；install.sh 不会改动 nginx，最后会打印 nginx 与 certbot 的命令）：
  rsync -avP $TAR${WORLD_TAR:+ $WORLD_TAR} emax:/tmp/
  ssh emax 'tar -C /tmp -xzf /tmp/$NAME.tar.gz'
  ssh -t emax 'sudo bash /tmp/$NAME/tools/deploy/emax/install.sh --bundle /tmp/$NAME${WORLD_TAR:+ --world-pack /tmp/$(basename "$WORLD_TAR")}'
EOF
