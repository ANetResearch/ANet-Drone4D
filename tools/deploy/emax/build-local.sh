#!/usr/bin/env bash
# tools/deploy/emax/build-local.sh：在本机构建 ANet Drone4D 公开演示站 V0.2-demo 的部署包（T4，AWR-19 §3.7；ADR-083、ADR-085、
# ADR-109、ADR-118）。
#
#   tools/deploy/emax/build-local.sh [--worlds all|none|<id,id,…>] [--with-world] [--out DIR] [--skip-web]
#
# 产物（缺省在 .cache/deploy/emax/）：
#   anet-drone4d-<release>.tar.gz   代码与配置：python/、packages/contracts/、configs/、scenarios/、vehicles/、
#                                   apps/web/dist/（VITE_AWR_DEMO=public 演示构建，含 build-info.json）、tools/deploy/、
#                                   tools/sdk/（nginx /sdk/ 直出）、pyproject.toml、requirements.lock、许可与通知文件；
#                                   顶层 RELEASE（构建信息，服务端经容量接口与 /api/health 公开）与 MANIFEST.sha256
#   worlds/                         世界包目录（--worlds，缺省 all = public profile 的七个世界：synthcity 与 UrbanScene3D
#                                   六城，后者经数据集权利方授权使用，ADR-109）：每个世界一个世界包与一个派生缓存包（含 Grid25），
#                                   另有共享环境资产包，索引 WORLDS.json 与 WORLDS.sha256（tools/deploy/emax/worldpack.py）。
#                                   包名含内容版本与派生键，未变化的包直接复用；上传用 rsync 只传新增的包
# 部署包本身绝不包含：refs/、.cache/、data/、runs/、worlds/（世界只经世界包目录上传）、apps/web/public/bench（flight60 夹具，
# WP-13 评估前不随演示构建发布，ADR-109 第 5 条）、本机凭据与部署口令。打包后逐项复核，违反即失败。
# --with-world 为 D1 的写法，等同 --worlds synthcity。
# 构建环境（ADR-118，AWR-04 §15.4）：AWR_BUILD_CHANNEL=demo、AWR_BUILD_SHA、AWR_BUILD_DIRTY、AWR_BUILD_TIME、AWR_RELEASE
# 导出给 vite 的 buildInfoPlugin()；设置了 SOURCE_DATE_EPOCH 时构建时刻取该值（可复现构建）。
# 不执行任何 git 写操作；只读 `git rev-parse` 与 `git status` 取提交（没有 git 时为 nogit）。
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
OUT="$ROOT/.cache/deploy/emax"
WORLDS=all
SKIP_WEB=0
WORLDS_DIR=${AWR_WORLDS_DIR:-$ROOT/worlds}
PY="$ROOT/.venv/bin/python"

die() { echo "build-local: $*" >&2; exit 1; }
step() { echo "==> $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --worlds) WORLDS=$2; shift ;;
    --with-world) WORLDS=synthcity ;;
    --skip-web) SKIP_WEB=1 ;;
    --out) OUT=$2; shift ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) die "未知参数 $1（--worlds all|none|<id,…> | --with-world | --skip-web | --out DIR）" ;;
  esac
  shift
done
case "$WORLDS" in
  all|none) ;;
  *) [[ "$WORLDS" =~ ^[a-z0-9][a-z0-9_-]*(,[a-z0-9][a-z0-9_-]*)*$ ]] || die "--worlds 取 all、none 或逗号分隔的世界 id" ;;
esac

command -v node >/dev/null || die "需要 Node 22（npm ci 之后的 apps/web 构建环境）"
command -v rsync >/dev/null || die "需要 rsync"
[ -x "$PY" ] || die "需要仓库 .venv（make setup）"
[ -z "${VITE_AWR_TEST_SWITCHES:-}" ] || die "VITE_AWR_TEST_SWITCHES 已设置：演示包中不得有测试开关，请在干净的环境中运行"

# ---- 0 构建信息（ADR-118：产品版本单一来源 pyproject.toml 与 apps/web/package.json，二者必须相等） ------------------------
node "$ROOT/tools/lint/check-version.mjs" >/dev/null || die "产品版本不一致（node tools/lint/check-version.mjs 查看）"
VERSION=$("$PY" -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["project"]["version"])' "$ROOT/pyproject.toml")
SHA=$(git -C "$ROOT" rev-parse --short=10 HEAD 2>/dev/null || echo nogit)
DIRTY=false
if [ "$SHA" != nogit ] && [ -n "$(git -C "$ROOT" status --porcelain 2>/dev/null | head -1)" ]; then DIRTY=true; fi
EPOCH=${SOURCE_DATE_EPOCH:-$(date +%s)}
BUILT_AT=$(date -u -d "@$EPOCH" +%Y-%m-%dT%H:%M:%SZ)
REL="$(date -u -d "@$EPOCH" +%Y%m%d-%H%M%S)-${SHA}$([ "$DIRTY" = true ] && echo -dirty || true)"
if [ "$SKIP_WEB" -eq 1 ]; then  # 复用上一次的演示构建：发布 id 与构建信息取自它的 build-info.json，前后端保持一致
  BI="$ROOT/.cache/deploy/emax/web-dist/build-info.json"
  [ -f "$BI" ] || die "--skip-web 需要已有的 .cache/deploy/emax/web-dist（含 build-info.json）"
  read -r REL SHA DIRTY BUILT_AT V2 < <("$PY" -c 'import json, sys; b = json.load(open(sys.argv[1], encoding="utf-8")); print(b["release"], b["commit"], str(b["dirty"]).lower(), b["built_at"], b["version"])' "$BI")
  [ "$V2" = "$VERSION" ] || die "web-dist 的产品版本 $V2 与 pyproject.toml 的 $VERSION 不同：去掉 --skip-web 重新构建"
fi
NAME="anet-drone4d-$REL"
STAGE="$OUT/stage/$NAME"
rm -rf "$OUT/stage"
mkdir -p "$STAGE"
export AWR_BUILD_CHANNEL=demo AWR_BUILD_SHA="$SHA" AWR_BUILD_DIRTY="$DIRTY" AWR_BUILD_TIME="$BUILT_AT" AWR_RELEASE="$REL"
echo "发布 $REL：V$VERSION-demo · ${SHA:0:7}$([ "$DIRTY" = true ] && echo -dirty || true) · ${BUILT_AT:0:10}"

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
  mkdir -p "$STAGE/apps/web" && cp -a "$ROOT/.cache/deploy/emax/web-dist" "$WEB_OUT"
fi
step "生产包扫描（M06-AC-010：无测试开关）与构建信息"
node "$ROOT/apps/web/tests/m06/lint/prod-bundle-scan.mjs" "$WEB_OUT"
grep -q 'data-landing' "$WEB_OUT/index.html" || die "dist/index.html 不是演示构建（缺少落地页标记）"
[ ! -e "$WEB_OUT/bench" ] || die "演示构建不得包含 dist/bench（UrbanScene3D 城市的 flight60 夹具，ADR-109 第 5 条待评估）"
[ -f "$WEB_OUT/demo/drone4d-banner.jpg" ] && [ -f "$WEB_OUT/demo/demo-flight.webp" ] || die "落地页媒体缺失（dist/demo）"
"$PY" - "$WEB_OUT/build-info.json" "$REL" "$VERSION" <<'PY' || die "dist/build-info.json 缺失或与本次构建不符（buildInfoPlugin，ADR-118）"
import json, sys
b = json.load(open(sys.argv[1], encoding="utf-8"))
assert b["release"] == sys.argv[2] and b["version"] == sys.argv[3] and b["channel"] == "demo", b
PY
grep -q "name=\"awr-build\"" "$WEB_OUT/index.html" || die "dist/index.html 缺少 <meta name=\"awr-build\">（buildInfoPlugin）"
if [ "$SKIP_WEB" -eq 0 ]; then mkdir -p "$OUT" && rm -rf "$OUT/web-dist" && cp -a "$WEB_OUT" "$OUT/web-dist"; fi

# ---- 2 运行所需文件 ------------------------------------------------------------------------------------------
step "复制代码与配置"
RS=(rsync -a --delete --exclude '__pycache__/' --exclude '*.pyc' --exclude '*.egg-info/' --exclude 'node_modules/')
mkdir -p "$STAGE/packages" "$STAGE/tools/deploy" "$STAGE/tools/sdk"
"${RS[@]}" "$ROOT/python/" "$STAGE/python/"
"${RS[@]}" "$ROOT/packages/contracts/" "$STAGE/packages/contracts/"
"${RS[@]}" "$ROOT/configs/" "$STAGE/configs/"
"${RS[@]}" "$ROOT/scenarios/" "$STAGE/scenarios/"
"${RS[@]}" "$ROOT/vehicles/" "$STAGE/vehicles/"
"${RS[@]}" "$ROOT/tools/deploy/" "$STAGE/tools/deploy/"
"${RS[@]}" "$ROOT/tools/sdk/" "$STAGE/tools/sdk/"
for f in pyproject.toml requirements.lock requirements.in LICENSE NOTICE THIRD_PARTY_NOTICES.md README.md README.zh-CN.md; do
  [ -f "$ROOT/$f" ] && cp -a "$ROOT/$f" "$STAGE/$f"
done
chmod 0755 "$STAGE"/tools/deploy/emax/*.sh
ls "$STAGE"/vehicles/p600/model/*.glb >/dev/null 2>&1 || echo "警告：vehicles/p600/model/*.glb 不存在（make setup 生成）；前端会退回 dist/models 的副本" >&2

# ---- 3 合规复核（发布集合同口径，ADR-079 REL-02、REL-03、REL-04） ------------------------------------------------------
step "合规复核"
for d in worlds data refs .cache runs apps/web/public node_modules; do
  [ ! -e "$STAGE/$d" ] || die "包内不得出现 $d/（世界只经世界包目录上传）"
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
# 部署口令（内部身份，ADR-123）：只比对、不打印；口令经管道交给 grep -f，不出现在命令行参数中（AWR-04 §15.9.5 第 2 条）
for sf in "$HOME/.config/awr/admin.secret" "${AWR_ADMIN_SECRET_FILE:-}"; do
  [ -n "$sf" ] && [ -f "$sf" ] && [ -r "$sf" ] || continue
  [ "$(head -n 1 "$sf" | tr -d '\r\n' | wc -c)" -ge 6 ] || continue
  if grep -rqF -f <(head -n 1 "$sf" | tr -d '\r\n'; echo) "$STAGE" 2>/dev/null; then
    die "包内出现部署口令（$sf 的内容）；从源文件中删除后重新打包"
  fi
done

# ---- 4 构建信息、清单与打包 ----------------------------------------------------------------------------------------
step "清单与打包"
{
  echo "release=$REL"
  echo "version=$VERSION"
  echo "channel=demo"
  echo "commit=$SHA"
  echo "dirty=$DIRTY"
  echo "built_at=$BUILT_AT"
  echo "git=$SHA$([ "$DIRTY" = true ] && echo -dirty || true)"
  echo "web=VITE_AWR_DEMO=public"
} > "$STAGE/RELEASE"
(cd "$STAGE" && find . -type f -print0 | sort -z | xargs -0 sha256sum > "$OUT/MANIFEST.tmp" && mv "$OUT/MANIFEST.tmp" MANIFEST.sha256)
TAR="$OUT/$NAME.tar.gz"
tar -C "$OUT/stage" -czf "$TAR" "$NAME"
echo "    $(du -h "$TAR" | cut -f1)  $TAR"

# ---- 5 世界包（七个世界与派生缓存，AWR-04 §4.4.7） ------------------------------------------------------------------
WPACK="$OUT/worlds"
if [ "$WORLDS" != none ]; then
  step "世界包（$WORLDS）→ $WPACK"
  "$PY" "$HERE/worldpack.py" pack --worlds-dir "$WORLDS_DIR" --out "$WPACK" --ids "$WORLDS" \
    || die "世界包失败（世界须已构建且派生缓存可用：make worlds、make demo-world、make geo-warm）"
  "$PY" "$HERE/worldpack.py" verify --src "$WPACK" >/dev/null || die "世界包校验失败"
  echo "    $(du -sh "$WPACK" | cut -f1)  $WPACK"
fi

cat <<EOF

完成：$NAME
上传与安装（在本机执行前两条，在 emax 上执行第三条；install.sh 不会改动 nginx，最后会打印 nginx 与 certbot 的命令）：
  rsync -avP $TAR emax:/tmp/
EOF
if [ "$WORLDS" != none ]; then
  cat <<EOF
  rsync -avP --delete $WPACK/ emax:anet-drone4d-worlds/        # 只传新增或变化的包；服务器上保留该目录以便下次增量上传
  ssh emax 'tar -C /tmp -xzf /tmp/$NAME.tar.gz'
  ssh -t emax 'sudo bash /tmp/$NAME/tools/deploy/emax/install.sh --bundle /tmp/$NAME --worlds "\$HOME/anet-drone4d-worlds"'
EOF
else
  cat <<EOF
  ssh emax 'tar -C /tmp -xzf /tmp/$NAME.tar.gz'
  ssh -t emax 'sudo bash /tmp/$NAME/tools/deploy/emax/install.sh --bundle /tmp/$NAME'
EOF
fi
