#!/usr/bin/env bash
# dmax `d3` profile：内部工作区六个容器（AWR-05 §7.11、ADR-166；AWR-17 §19；D3-AC-51、D3-AC-25）。全部 --network host，服务只绑定回环，
# 只有 d3-labweb（nginx，TLS）监听 ZeroTier 地址 $LAB_BIND:$LAB_PORT；CPU 池（不挂 GPU）、--cgroup-parent d3.slice、只读根文件系统。
#
#   容器                    进程                                         绑定
#   d3-bench                bench-api   python -m awr.api.bench_main     127.0.0.1:18650
#   d3-lab-gateway          lab-gateway python -m awr.bench.api.gateway  127.0.0.1:18651
#   d3-rerun-proxy          rerun-proxy python -m awr.bench.api.rerun_proxy 127.0.0.1:18652
#   d3-bench-catalog-dist   分发 catalog python -m awr.bench.catalog serve  127.0.0.1:18653
#   d3-bench-catalog-eval   评测 catalog                                  127.0.0.1:18654
#   d3-labweb               lab-web 与鉴权反向代理（nginx）               $LAB_BIND:$LAB_PORT（TLS）
#
# 用法（本机仓库根目录）：tools/deploy/dmax/lab/profile.sh [up|down|status|probe-cookie]（缺省 up；先 labctl.sh build 与 init）。
# probe-cookie：后台容器 d3-test-f05-cookie 执行 12 h 会话 Cookie 实测，结果写 $LAB_ROOT/out/cookie-probe-<UTC>.json。
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=lab.env
. "$HERE/lab.env"
ACTION=${1:-up}
IMG=d3-lab-py:$LAB_TAG
WEB=d3-labweb:$LAB_TAG
NAMES="d3-labweb d3-lab-gateway d3-rerun-proxy d3-bench-catalog-dist d3-bench-catalog-eval d3-bench"
COMMON="--network host --restart unless-stopped --cgroup-parent d3.slice --read-only --tmpfs /tmp:rw,size=256m --security-opt no-new-privileges \
 --label d3.profile=d3 --log-opt max-size=20m --log-opt max-file=3"
PY="$COMMON --user $LAB_UID:$LAB_UID -v $LAB_DATA:$LAB_DATA -v $LAB_CAPTURE:$LAB_CAPTURE:ro -v $LAB_WORLDS:$LAB_WORLDS:ro -e AWR_BENCH_DATA_ROOT=$LAB_DATA \
 -v $LAB_ROOT/tls/cert.pem:/opt/lab-cert.pem:ro -e AWR_BENCH_ALLOWED_ORIGINS=$LAB_ORIGINS"
case "$ACTION" in
  up)
    ssh dmax "mkdir -p $LAB_DATA/rerun $LAB_ROOT/tls $LAB_ROOT/out && docker rm -f $NAMES >/dev/null 2>&1; \
docker run -d --name d3-bench --label d3.component=bench --cpus 2 --memory 4g $PY -e AWR_RERUN_PROXY_BASE=$LAB_ORIGIN \
  $IMG python -m awr.api.bench_main --host 127.0.0.1 --port 18650 && \
docker run -d --name d3-bench-catalog-dist --label d3.component=bench --cpus 2 --memory 4g $PY \
  $IMG python -m awr.bench.catalog serve --catalog dist --host 127.0.0.1 --port 18653 && \
docker run -d --name d3-bench-catalog-eval --label d3.component=bench --cpus 2 --memory 4g $PY \
  $IMG python -m awr.bench.catalog serve --catalog eval --host 127.0.0.1 --port 18654 && \
docker run -d --name d3-rerun-proxy --label d3.component=rerun-proxy --cpus 1 --memory 1g $PY \
  $IMG python -m awr.bench.api.rerun_proxy --host 127.0.0.1 --port 18652 && \
docker run -d --name d3-lab-gateway --label d3.component=lab-gateway --cpus 1 --memory 1g $PY \
  $IMG python -m awr.bench.api.gateway --host 127.0.0.1 --port 18651 && \
docker run -d --name d3-labweb --label d3.component=labweb --cpus 1 --memory 512m $COMMON \
  --tmpfs /run:rw,size=8m --tmpfs /var/cache/nginx:rw,size=64m --tmpfs /etc/nginx/conf.d:rw,size=1m \
  -v $LAB_ROOT/tls:/etc/nginx/tls:ro -e LAB_BIND=$LAB_BIND -e LAB_PORT=$LAB_PORT -e LAB_SERVER_NAME=$LAB_SERVER_NAME $WEB"
    ;;
  down)
    ssh dmax "docker rm -f $NAMES"
    ;;
  status)
    ssh dmax "docker ps -a --filter label=d3.profile=d3 --format '{{.Names}}\t{{.Status}}\t{{.Image}}'; \
ss -ltnH | awk '{print \$4}' | grep -E ':(1865[0-4]|$LAB_PORT)\$' | sort"
    ;;
  probe-cookie)
    T=$(date -u +%Y%m%dT%H%M%SZ)
    ssh dmax "docker rm -f d3-test-f05-cookie >/dev/null 2>&1; docker run -d --rm --name d3-test-f05-cookie --label d3.component=test \
 --network host --cgroup-parent d3.slice --read-only --tmpfs /tmp:rw,size=64m --security-opt no-new-privileges --cpus 0.5 --memory 512m \
 --user $LAB_UID:$LAB_UID -v $LAB_DATA:$LAB_DATA -v $LAB_ROOT/out:$LAB_ROOT/out -v $LAB_ROOT/tls/cert.pem:/opt/lab-cert.pem:ro \
 -e AWR_BENCH_DATA_ROOT=$LAB_DATA $IMG python /opt/awr/tools/labctl.py cookie-probe --base $LAB_ORIGIN --cafile /opt/lab-cert.pem \
 --out $LAB_ROOT/out/cookie-probe-$T.json"
    echo "$LAB_ROOT/out/cookie-probe-$T.json"
    ;;
  *)
    echo "usage: $0 [up|down|status|probe-cookie]" >&2
    exit 2
    ;;
esac
