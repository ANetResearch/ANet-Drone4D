#!/usr/bin/env bash
# tools/deploy/emax/install.sh：在 emax（Ubuntu 24.04，x86-64，Python 3.12.3）上安装或更新 ANet Drone4D 公开演示站
# （T4，AWR-19 §3.7；ADR-085）。幂等：重复执行只补齐缺失的部分；以 root 执行。
#
#   sudo bash install.sh --bundle DIR [--world-pack TAR] [--admin-secret] [--no-restart]   安装或更新（缺省 bundle 为本脚本所在包）
#   sudo bash install.sh --rollback [RELEASE]                                               切回上一个（或指定）发布并重启
#   sudo bash install.sh --status                                                           发布、服务与健康检查
#
# 做什么：创建系统用户 awr 与目录（/opt/anet-drone4d、/etc/anet-drone4d）；把包复制到 releases/<release>（root 所有，awr
# 只读）；按 requirements.lock 用 uv 建该发布的 .venv（服务器 Python 3.12）；预编译字节码与 numba 缓存；生成合成城市
# synthcity（或解开 build-local.sh --with-world 打的世界包，含共享环境资产与构建状态）；首次安装时由 env.example 生成
# /etc/anet-drone4d/env（0600）；安装 systemd 单元，切换 current 并重启服务，等待健康检查通过；保留最近 3 个发布。
# 不做什么：不 reload、不修改 nginx，也不申请证书：这两步以打印的命令交给人工执行（只新增本站点，不碰其他站点）；
# 不打开防火墙端口（api 只监听 127.0.0.1:18640）；不执行任何 git 操作。
set -euo pipefail

PREFIX=/opt/anet-drone4d
ETC=/etc/anet-drone4d
SVC=anet-drone4d
SVC_USER=awr
DOMAIN=drone4d.agentnetwork.org.cn
PORT=18640
UV_VERSION=0.12.19
PY=${PYTHON:-/usr/bin/python3.12}
KEEP=3
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
BUNDLE=$(cd "$HERE/../../.." && pwd)
WORLD_PACK=""
ADMIN_SECRET=0
RESTART=1
MODE=install
ROLLBACK_TO=""

die() { echo "install: $*" >&2; exit 1; }
step() { echo "==> $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --bundle) BUNDLE=$(cd "$2" && pwd); shift ;;
    --world-pack) WORLD_PACK=$(readlink -f "$2"); shift ;;
    --admin-secret) ADMIN_SECRET=1 ;;
    --no-restart) RESTART=0 ;;
    --rollback) MODE=rollback; if [ $# -gt 1 ] && [ "${2#--}" = "$2" ]; then ROLLBACK_TO=$2; shift; fi ;;
    --status) MODE=status ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) die "未知参数 $1" ;;
  esac
  shift
done

[ "$(id -u)" -eq 0 ] || die "请以 root 执行（sudo bash $0 ...）"
[ "$(uname -m)" = x86_64 ] || die "只支持 x86-64（StateRing 依赖 TSO，AWR-19 OPS-NFR-011）"
for c in systemctl runuser rsync curl sha256sum; do command -v "$c" >/dev/null || die "缺少命令 $c（apt install $c）"; done

as_awr() { runuser -u "$SVC_USER" -- env HOME="$PREFIX" NUMBA_CACHE_DIR="$PREFIX/var/cache/numba" PYTHONDONTWRITEBYTECODE=1 "$@"; }

health() {  # 等待 api 就绪（经回环、带站点 Host；ready 需要 sim-core 环与世界都已就绪）
  local i
  for i in $(seq 1 90); do
    if curl -fsS -o /dev/null -H "Host: $DOMAIN" "http://127.0.0.1:$PORT/api/health/ready"; then
      echo "    api 就绪（${i} s）：http://127.0.0.1:$PORT（Host: $DOMAIN）"
      return 0
    fi
    sleep 1
  done
  echo "    90 s 内未就绪：journalctl -u $SVC -n 100；ls $PREFIX/var/runs/current/logs/" >&2
  return 1
}

releases() { find "$PREFIX/releases" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null | sort; }
current_release() { basename "$(readlink -f "$PREFIX/current" 2>/dev/null || echo none)"; }

switch_to() {  # 原子切换 current 符号链接
  ln -sfn "releases/$1" "$PREFIX/.current.tmp"
  mv -Tf "$PREFIX/.current.tmp" "$PREFIX/current"
}

restart_service() {
  systemctl daemon-reload
  systemctl enable "$SVC" >/dev/null 2>&1 || true
  if [ "$RESTART" -eq 1 ]; then
    step "重启 $SVC"
    systemctl restart "$SVC"
    health
  else
    echo "    --no-restart：稍后执行 systemctl restart $SVC"
  fi
}

if [ -f "$ETC/env" ]; then P=$(sed -n 's/^AWR_PORT=//p' "$ETC/env" | tail -1); PORT=${P:-$PORT}; fi

# ---- --status ---------------------------------------------------------------------------------------------------
if [ "$MODE" = status ]; then
  echo "current:  $(current_release)"
  echo "releases: $(releases | tr '\n' ' ')"
  systemctl --no-pager --lines=0 status "$SVC" || true
  curl -sS -H "Host: $DOMAIN" "http://127.0.0.1:$PORT/api/health/ready" || true
  echo
  exit 0
fi

# ---- --rollback -------------------------------------------------------------------------------------------------
if [ "$MODE" = rollback ]; then
  CUR=$(current_release)
  if [ -z "$ROLLBACK_TO" ]; then  # 当前发布之前的那一个（发布名以时间戳开头，字典序即时间序）
    ROLLBACK_TO=$(releases | awk -v c="$CUR" '$0 < c' | tail -1)
  fi
  [ -n "$ROLLBACK_TO" ] && [ -d "$PREFIX/releases/$ROLLBACK_TO" ] || die "没有可回滚的发布（现有：$(releases | tr '\n' ' ')）"
  [ -x "$PREFIX/releases/$ROLLBACK_TO/.venv/bin/python" ] || die "发布 $ROLLBACK_TO 的 .venv 不完整"
  step "回滚：$CUR → $ROLLBACK_TO"
  switch_to "$ROLLBACK_TO"
  install -m 0644 "$PREFIX/releases/$ROLLBACK_TO/tools/deploy/emax/anet-drone4d.service" "/etc/systemd/system/$SVC.service"
  restart_service
  exit 0
fi

# ---- 安装或更新 ----------------------------------------------------------------------------------------------------
[ -f "$BUNDLE/RELEASE" ] && [ -f "$BUNDLE/requirements.lock" ] && [ -f "$BUNDLE/apps/web/dist/index.html" ] \
  || die "$BUNDLE 不是 build-local.sh 生成的部署包（缺少 RELEASE、requirements.lock 或 apps/web/dist）"
REL=$(sed -n 's/^release=//p' "$BUNDLE/RELEASE")
[ -n "$REL" ] || die "RELEASE 中没有 release="
grep -q 'data-landing' "$BUNDLE/apps/web/dist/index.html" || die "前端不是 VITE_AWR_DEMO=public 演示构建"
[ ! -e "$BUNDLE/apps/web/dist/bench" ] || die "前端构建含 dist/bench（UrbanScene3D 夹具），拒绝安装"
(cd "$BUNDLE" && sha256sum --quiet -c MANIFEST.sha256) || die "MANIFEST.sha256 校验失败（包不完整或被改动）"
[ -x "$PY" ] || die "找不到 $PY（apt install python3.12 python3.12-venv）"
"$PY" -c 'import sys; assert sys.version_info[:2] == (3, 12)' || die "$PY 不是 Python 3.12"
"$PY" -c 'import ensurepip, venv' 2>/dev/null || die "缺少 venv 模块：apt install python3.12-venv"
echo "发布 $REL（来自 $BUNDLE）"

step "用户与目录"
getent group "$SVC_USER" >/dev/null || groupadd --system "$SVC_USER"
getent passwd "$SVC_USER" >/dev/null || useradd --system --gid "$SVC_USER" --home-dir "$PREFIX" --no-create-home \
  --shell /usr/sbin/nologin --comment "ANet Drone4D public demo" "$SVC_USER"
install -d -o root -g root -m 0755 "$PREFIX" "$PREFIX/releases" "$PREFIX/tools"
install -d -o root -g root -m 0700 "$PREFIX/.uv-cache"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 0750 "$PREFIX/var" "$PREFIX/var/worlds" "$PREFIX/var/runs" "$PREFIX/var/cache" \
  "$PREFIX/var/cache/numba"
install -d -o root -g "$SVC_USER" -m 0750 "$ETC"

step "uv $UV_VERSION"
UV="$PREFIX/tools/uv/bin/uv"
if [ ! -x "$UV" ] || [ "$("$UV" --version 2>/dev/null | cut -d' ' -f2)" != "$UV_VERSION" ]; then
  rm -rf "$PREFIX/tools/uv"
  "$PY" -m venv "$PREFIX/tools/uv"
  "$PREFIX/tools/uv/bin/pip" install --quiet --disable-pip-version-check "uv==$UV_VERSION" \
    || die "uv 自举失败（检查到 PyPI 的网络；可设 PIP_INDEX_URL 与 UV_INDEX_URL 指向镜像）"
fi

DEST="$PREFIX/releases/$REL"
step "发布目录 $DEST"
if [ -f "$DEST/MANIFEST.sha256" ] && cmp -s "$DEST/MANIFEST.sha256" "$BUNDLE/MANIFEST.sha256" && [ -x "$DEST/.venv/bin/python" ]; then
  echo "    已存在且内容一致，复用"
else
  install -d -o root -g root -m 0755 "$DEST"
  rsync -a --delete --exclude '/.venv/' --chown=root:root "$BUNDLE/" "$DEST/"
  chmod -R u+rwX,go+rX,go-w "$DEST"
fi

step ".venv（uv pip sync requirements.lock）"
export UV_CACHE_DIR="$PREFIX/.uv-cache"
if [ ! -x "$DEST/.venv/bin/python" ]; then
  "$UV" venv --quiet --python "$PY" "$DEST/.venv"
fi
"$UV" pip sync --quiet --python "$DEST/.venv/bin/python" "$DEST/requirements.lock" \
  || die "依赖安装失败（hash 锁定；网络受限时设 UV_INDEX_URL 指向 PyPI 镜像后重试）"
# awr 本身以源码路径加入 sys.path（等价于可编辑安装，但不在只读的源码树里写 egg-info）：ROOT 由源码位置推导（AWR-03 §4.1）
SITE=$("$DEST/.venv/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
echo "$DEST/python" > "$SITE/awr-src.pth"
"$DEST/.venv/bin/python" -c 'import awr.runtime.supervisor, awr.api.main' || die "awr 无法导入"
"$DEST/.venv/bin/python" -m compileall -q -j 0 "$DEST/python" >/dev/null || true
chmod -R go+rX,go-w "$DEST/.venv"

step "numba 预热（写入 $PREFIX/var/cache/numba；sim-core 启动时只读缓存）"
as_awr "$DEST/.venv/bin/python" -m awr.sim.runtime.warm
as_awr "$DEST/.venv/bin/python" -c "from awr.sim.planning import kernels_track as K, smooth as S, astar25 as A; K.warmup(); S.warmup(); A.warmup()"

step "世界：synthcity（与世界包里的共享环境资产）"
W="$PREFIX/var/worlds"
if [ -n "$WORLD_PACK" ]; then
  [ -f "$WORLD_PACK" ] || die "世界包 $WORLD_PACK 不存在"
  BAD=$(tar -tzf "$WORLD_PACK" | grep -vE '^(\./)?(synthcity|_shared/env)(/|$)|^(\./)?\.status/(synthcity\.json)?$' | head -3 || true)
  [ -z "$BAD" ] || die "世界包只能包含 synthcity、_shared/env 与 .status/synthcity.json（发现：$BAD）"
  TMPW="$W/.unpack-$$"
  install -d -o "$SVC_USER" -g "$SVC_USER" "$TMPW"
  as_awr tar -C "$TMPW" -xzf "$WORLD_PACK"
  if [ -d "$TMPW/synthcity" ]; then rm -rf "$W/synthcity.old"; [ ! -d "$W/synthcity" ] || mv "$W/synthcity" "$W/synthcity.old"; mv "$TMPW/synthcity" "$W/synthcity"; rm -rf "$W/synthcity.old"; fi
  if [ -d "$TMPW/_shared/env" ]; then as_awr mkdir -p "$W/_shared"; rm -rf "$W/_shared/env"; mv "$TMPW/_shared/env" "$W/_shared/env"; fi
  if [ -f "$TMPW/.status/synthcity.json" ]; then as_awr mkdir -p "$W/.status"; mv -f "$TMPW/.status/synthcity.json" "$W/.status/synthcity.json"; fi
  rm -rf "$TMPW"
fi
as_awr env AWR_WORLDS_DIR="$W" "$DEST/.venv/bin/python" -m awr.world.package.cli build synthcity --missing \
  || die "synthcity 生成失败（约需 1.3 GB 内存与 30 s；也可在本机 build-local.sh --with-world 后用 --world-pack 上传）"
# 共享环境资产（_shared/env 的湍流盒与天气图）由世界包带来；没有时 sim-core 首次启动按自己的种子生成（约 1 s，AWR-16 §10）
for other in "$W"/*/; do
  name=$(basename "$other")
  case "$name" in synthcity|_shared) ;; *) echo "警告：$W 中有 synthcity 以外的世界 $name（公开站不得提供，api 世界白名单会 404，但请删除）" >&2 ;; esac
done

step "配置 $ETC/env"
if [ ! -f "$ETC/env" ]; then
  install -o root -g root -m 0600 "$DEST/tools/deploy/emax/env.example" "$ETC/env"
  echo "    已由 env.example 生成；以后的安装不会覆盖它"
else
  chmod 0600 "$ETC/env"
  if ! diff -q <(grep -E '^[A-Z_]+=' "$DEST/tools/deploy/emax/env.example" | cut -d= -f1 | sort) \
               <(grep -E '^#?[A-Z_]+=' "$ETC/env" | tr -d '#' | cut -d= -f1 | sort -u) >/dev/null; then
    echo "    提示：env.example 有新增或删除的键，对照后手工合并：diff $DEST/tools/deploy/emax/env.example $ETC/env"
  fi
fi
if [ "$ADMIN_SECRET" -eq 1 ]; then
  if [ ! -s "$ETC/admin.secret" ]; then
    umask 077
    head -c 24 /dev/urandom | base64 | tr -d '/+=\n' > "$ETC/admin.secret"
    chown "$SVC_USER:$SVC_USER" "$ETC/admin.secret"
    chmod 0600 "$ETC/admin.secret"
    echo "    已生成管理口令 $ETC/admin.secret（不打印；查看：sudo cat $ETC/admin.secret）"
  fi
  grep -q '^AWR_ADMIN_SECRET_FILE=' "$ETC/env" || echo "AWR_ADMIN_SECRET_FILE=$ETC/admin.secret" >> "$ETC/env"
fi

P=$(sed -n 's/^AWR_PORT=//p' "$ETC/env" | tail -1)
PORT=${P:-$PORT}

step "systemd 单元与切换"
PREV=$(current_release)
install -m 0644 "$DEST/tools/deploy/emax/anet-drone4d.service" "/etc/systemd/system/$SVC.service"
switch_to "$REL"
echo "    current：$PREV → $REL"
if ! restart_service; then
  if [ "$PREV" != none ] && [ "$PREV" != "$REL" ] && [ -d "$PREFIX/releases/$PREV" ]; then
    echo "    新发布未就绪，自动回滚到 $PREV" >&2
    switch_to "$PREV"
    systemctl restart "$SVC"
    health || true
  fi
  die "新发布 $REL 启动失败（journalctl -u $SVC -n 200）"
fi

step "清理旧发布（保留最近 $KEEP 个）"
mapfile -t ALL < <(releases)
N=${#ALL[@]}
if [ "$N" -gt "$KEEP" ]; then
  for r in "${ALL[@]:0:$((N - KEEP))}"; do
    [ "$r" = "$REL" ] || [ "$r" = "$PREV" ] || { rm -rf "${PREFIX:?}/releases/$r"; echo "    删除 $r"; }
  done
fi

cat <<EOF

完成：$SVC 运行发布 $REL，api 只监听 127.0.0.1:$PORT。
以下 nginx 与证书步骤本脚本不会执行，请人工逐条执行（只新增本站点，不修改其他站点；详见 README.md）：

  # 首次部署（尚无证书）：先启用只含 80 端口的引导站点，申请 ECDSA 证书
  sudo install -m 0644 $DEST/tools/deploy/emax/drone4d.agentnetwork.org.cn.bootstrap.conf /etc/nginx/sites-available/$DOMAIN.conf
  sudo ln -sfn /etc/nginx/sites-available/$DOMAIN.conf /etc/nginx/sites-enabled/$DOMAIN.conf
  sudo nginx -t && sudo systemctl reload nginx
  sudo certbot certonly --webroot -w /var/www/certbot -d $DOMAIN --key-type ecdsa --elliptic-curve secp384r1

  # 证书就绪后（以及以后每次站点文件有变化时）：换成正式站点文件
  sudo install -m 0644 $DEST/tools/deploy/emax/drone4d.agentnetwork.org.cn.conf /etc/nginx/sites-available/$DOMAIN.conf
  sudo nginx -t && sudo systemctl reload nginx

  # 验证
  curl -sI https://$DOMAIN/ | head -1
  curl -s -o /dev/null -w '%{http_code}\n' https://$DOMAIN/worlds/shenzhen/world.json    # 404
EOF
