#!/usr/bin/env bash
# d3-registry（私有镜像仓库）与受邀访问的部署、演练与接入（AWR-05 §5.6.7、§5.6.8、ADR-183；AWR-17 §18.23；D3-AC-59）。
# 三个容器均在 CPU 池（不挂 GPU）、--network host、--cgroup-parent d3.slice、只读根文件系统、--cap-drop ALL（nginx 保留切换用户所需能力）：
#
#   容器                 进程                                               绑定
#   d3-registry          CNCF distribution v3（$REG_IMAGE，不鉴权）         127.0.0.1:$REG_BACKEND_PORT
#   d3-registry-authz    authz.py（BenchAuth.authorize_registry，逐请求）   127.0.0.1:$REG_AUTHZ_PORT
#   d3-registry-front    nginx（TLS，auth_request，不缓存鉴权结果）          $REG_BIND:$REG_PORT（ZeroTier 地址）
#
# 用法（本机仓库根目录）：tools/deploy/dmax/registry/regctl.sh <动作>（缺省 up）
#   load               本机拉取 $REG_IMAGE 并 docker save 同步到 dmax（dmax 已有同 ID 镜像时跳过）
#   sync               dmax 上建立部署根 $REG_ROOT（data、conf、out、drill），同步 config.yml、authz.py、regctl.py 与 nginx 配置
#   up | down | status 启停与状态（up 先删除同名容器；数据在 $REG_ROOT/data，不随容器删除）
#   drill              d3-test-b25-drill 容器内执行 regctl.py drill（临时 token，结束即撤销），结果 $REG_ROOT/out/drill-<UTC>.json；
#                      本机经 ZeroTier 扫描 dmax 地址的端口，并以 ztmember.py check 求值受邀成员网络策略（$OUT/zt-policy-check.json）
#   materialize        宿主执行 regctl.py materialize --once（DATA_ROOT 缺省 $LAB_DATA；PRUNE=1 时清理终态提交拉取的镜像）
#   materialize-drill  演练链路"推送 → 接入 → 扫描"：临时数据根内登记一条引用演练镜像的 RECEIVED 提交 → materialize 拉取 →
#                      d3-test-b25-scan 容器内以 subworker.scan_image（inspect + Trivy 离线扫描）判定 → 删除拉取的镜像与演练清单
#   gc                 停止 d3-registry → 一次性容器执行 garbage-collect --delete-untagged → 启动（管理员维护，不在运行中回收）
#   fetch              复制 dmax $REG_ROOT/out 下的记录（JSON，不含密钥）到 $OUT（缺省 .cache/deploy/dmax/registry）
#
# 受邀成员的 ZeroTier 授权与网络策略（只放行 lab-web、bench-api 与 d3-registry）由管理员以 zerotier/ztmember.py plan|apply 执行；
# 本脚本不修改 ZeroTier 网络配置。受邀成员的 bench token 由管理员签发：
#   docker exec -it d3-bench python -m awr.bench.ctl token issue --principal <name> --role invited --projects <team>
# 成员推送：BN-82 取推送令牌 → docker login $REG_ORIGIN -u <name> --password-stdin → docker push $REG_ORIGIN/<团队命名空间>/<仓库>:<标签>；
# 成员需信任 $LAB_ROOT/tls/cert.pem（/etc/docker/certs.d/$REG_ORIGIN/ca.crt）。
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../../.." && pwd)
# shellcheck source=registry.env
. "$HERE/registry.env"
ACTION=${1:-up}
OUT=${OUT:-$ROOT/.cache/deploy/dmax/registry}
NAMES="d3-registry-front d3-registry-authz d3-registry"
COMMON="--network host --restart unless-stopped --cgroup-parent d3.slice --read-only --security-opt no-new-privileges \
 --label d3.component=registry --label d3.profile=d3 --log-opt max-size=20m --log-opt max-file=3"
TESTC="--rm --network host --cgroup-parent d3.slice --read-only --tmpfs /tmp:rw,size=256m --security-opt no-new-privileges --cap-drop ALL \
 --label d3.component=test"

case "$ACTION" in
  load)
    docker image inspect "$REG_IMAGE" >/dev/null 2>&1 || docker pull -q "$REG_IMAGE" >/dev/null
    WANT=$(docker image inspect "$REG_IMAGE" --format '{{.Id}}')
    if ssh dmax "docker image inspect $REG_IMAGE >/dev/null 2>&1"; then
      echo "dmax already has $REG_IMAGE ($(ssh dmax "docker image inspect $REG_IMAGE --format '{{.Id}}'")); local $WANT"
    else
      docker save "$REG_IMAGE" | gzip -1 | ssh dmax "gunzip | docker load -q"
    fi
    ;;
  sync)
    ssh dmax "mkdir -p $REG_ROOT/data $REG_ROOT/conf/nginx $REG_ROOT/out $REG_ROOT/drill && chown $LAB_UID:$LAB_UID $REG_ROOT/data $REG_ROOT/out \
&& chmod 0750 $REG_ROOT/data"
    rsync -a "$HERE/config.yml" "$HERE/authz.py" "$HERE/regctl.py" "dmax:$REG_ROOT/conf/"
    rsync -a "$HERE/nginx/nginx.conf" "$HERE/nginx/registry.conf.template" "dmax:$REG_ROOT/conf/nginx/"
    ssh dmax "chmod 0644 $REG_ROOT/conf/*.py $REG_ROOT/conf/config.yml $REG_ROOT/conf/nginx/*; ls -l $REG_ROOT/conf $REG_ROOT/conf/nginx | tail -n +1"
    ;;
  up)
    ssh dmax "mkdir -p $REG_ROOT/data $REG_ROOT/out && docker rm -f $NAMES >/dev/null 2>&1; \
docker run -d --name d3-registry --cpus 1 --memory 1g $COMMON --cap-drop ALL --tmpfs /tmp:rw,size=64m --user $LAB_UID:$LAB_UID \
  -v $REG_ROOT/data:/var/lib/registry -v $REG_ROOT/conf/config.yml:/etc/distribution/config.yml:ro \
  -e REGISTRY_HTTP_ADDR=127.0.0.1:$REG_BACKEND_PORT -e OTEL_TRACES_EXPORTER=none $REG_IMAGE && \
docker run -d --name d3-registry-authz --cpus 1 --memory 512m $COMMON --cap-drop ALL --tmpfs /tmp:rw,size=64m --user $LAB_UID:$LAB_UID \
  -v $LAB_DATA:$LAB_DATA -v $REG_ROOT/conf/authz.py:/opt/awr/registry/authz.py:ro -e AWR_BENCH_DATA_ROOT=$LAB_DATA \
  $REG_AUTHZ_IMAGE python /opt/awr/registry/authz.py --host 127.0.0.1 --port $REG_AUTHZ_PORT --data-root $LAB_DATA && \
docker run -d --name d3-registry-front --cpus 1 --memory 512m $COMMON --cap-drop ALL --cap-add CHOWN --cap-add SETUID --cap-add SETGID \
  --tmpfs /tmp:rw,size=1g --tmpfs /run:rw,size=8m --tmpfs /var/cache/nginx:rw,size=64m --tmpfs /etc/nginx/conf.d:rw,size=1m \
  -v $LAB_ROOT/tls:/etc/nginx/tls:ro -v $REG_ROOT/conf/nginx/nginx.conf:/etc/nginx/nginx.conf:ro \
  -v $REG_ROOT/conf/nginx/registry.conf.template:/etc/nginx/templates/registry.conf.template:ro \
  -e NGINX_ENVSUBST_FILTER='^(REG|LAB)_' -e REG_BIND=$REG_BIND -e REG_PORT=$REG_PORT -e REG_BACKEND_PORT=$REG_BACKEND_PORT \
  -e REG_AUTHZ_PORT=$REG_AUTHZ_PORT -e LAB_SERVER_NAME=$LAB_SERVER_NAME $REG_NGINX_IMAGE"
    ;;
  down)
    ssh dmax "docker rm -f $NAMES"
    ;;
  status)
    ssh dmax "docker ps -a --filter label=d3.component=registry --format '{{.Names}}\t{{.Status}}\t{{.Image}}'; \
ss -ltnH | awk '{print \$4}' | grep -E ':($REG_PORT|$REG_BACKEND_PORT|$REG_AUTHZ_PORT)\$' | sort; du -sh $REG_ROOT/data | cut -f1"
    ;;
  drill)
    T=$(date -u +%Y%m%dT%H%M%SZ)
    mkdir -p "$OUT"
    ssh dmax "docker rm -f d3-test-b25-drill >/dev/null 2>&1; docker run --name d3-test-b25-drill $TESTC --cpus 1 --memory 1g \
  --user $LAB_UID:$LAB_UID -v $LAB_DATA:$LAB_DATA -v $REG_ROOT/out:$REG_ROOT/out -v $LAB_ROOT/tls/cert.pem:/opt/lab-cert.pem:ro \
  -v $REG_ROOT/conf/regctl.py:/opt/awr/registry/regctl.py:ro -e AWR_BENCH_DATA_ROOT=$LAB_DATA $REG_AUTHZ_IMAGE \
  python /opt/awr/registry/regctl.py drill --lab $LAB_ORIGIN --registry https://$REG_ORIGIN --data-root $LAB_DATA --cafile /opt/lab-cert.pem \
  --tag $REG_DRILL_TAG --trials ${TRIALS:-5} --out $REG_ROOT/out/drill-$T.json" || rc=$?
    python3 "$HERE/../lab/labctl.py" scan --host "$REG_BIND" --label zerotier-b25 \
      --ports "22,80,443,8000-8030,8600,8765,8888,9993,18088,18640-18680,19000-19010,19443,25118,28202,28216-28218,28650,62003,62006" \
      --lab-ports "$LAB_PORT,$REG_PORT" --out "$OUT/scan-zerotier-b25.json"
    printf '{"members": [{"node": "00000000a1", "team": "%s-drill-a", "expires": "2099-01-01"}, {"node": "00000000b2", "team": "%s-drill-b", "expires": "2099-01-01"}, {"node": "00000000c3", "team": "%s-expired", "expires": "2000-01-01"}]}\n' \
      "$REG_DRILL_TAG" "$REG_DRILL_TAG" "$REG_DRILL_TAG" > "$OUT/zt-members-drill.json"
    python3 "$HERE/zerotier/ztmember.py" render --members "$OUT/zt-members-drill.json" --out "$OUT/zt-fragment-drill.rules"
    python3 "$HERE/zerotier/ztmember.py" check --members "$OUT/zt-members-drill.json" --scan "$OUT/scan-zerotier-b25.json" \
      --out "$OUT/zt-policy-check.json" || rc=1
    echo "$REG_ROOT/out/drill-$T.json"
    exit "${rc:-0}"
    ;;
  materialize)
    ssh dmax "python3 $REG_ROOT/conf/regctl.py materialize --data-root ${DATA_ROOT:-$LAB_DATA} --registry 127.0.0.1:$REG_BACKEND_PORT --once \
${PRUNE:+--prune} --out $REG_ROOT/out/materialize-\$(date -u +%Y%m%dT%H%M%SZ).json"
    ;;
  materialize-drill)
    T=$(date -u +%Y%m%dT%H%M%SZ)
    DR=$REG_ROOT/drill/$T
    # 演练镜像的摘要取自最近一次 drill 记录（层的 gzip 字节随 zlib 版本可能不同，不在本机重算）
    read -r CFG MAN REPO < <(ssh dmax "J=\$(ls -t $REG_ROOT/out/drill-*.json | head -1); python3 -c 'import json, sys; \
d = json.load(open(sys.argv[1]))[\"image\"]; print(d[\"config_digest\"], d[\"manifest_digest\"], d[\"repository\"])' \$J")
    ssh dmax "set -e; mkdir -p $DR/submissions $DR/scan; chown -R $LAB_UID:$LAB_UID $DR
printf '%s\n' '{\"submission_id\": \"sub-$T-b25drill\", \"state\": \"RECEIVED\", \"image_digest\": \"$CFG\", \"_meta\": {\"team\": \"$REG_DRILL_TAG-drill-a\", \"role\": \"invited\"}}' > $DR/submissions/sub-$T-b25drill.json
printf '%s\n' '{\"submission_id\": \"sub-$T-b25other\", \"state\": \"RECEIVED\", \"image_digest\": \"$CFG\", \"_meta\": {\"team\": \"$REG_DRILL_TAG-drill-b\", \"role\": \"invited\"}}' > $DR/submissions/sub-$T-b25other.json
docker image inspect $CFG >/dev/null 2>&1 && echo 'drill image already on the host before materialize' || true
python3 $REG_ROOT/conf/regctl.py materialize --data-root $DR --registry 127.0.0.1:$REG_BACKEND_PORT --once --out $REG_ROOT/out/materialize-drill-$T.json || true
docker run --name d3-test-b25-scan $TESTC --cpus 2 --memory 4g --user $LAB_UID:$LAB_UID --group-add \$(stat -c %g /var/run/docker.sock) \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /usr/bin/docker:/usr/bin/docker:ro -v $DR:$DR -v $REG_ROOT/out:$REG_ROOT/out -v $TRIVY_CACHE:$TRIVY_CACHE:ro \
  -v $REG_ROOT/conf/regctl.py:/opt/awr/registry/regctl.py:ro $REG_AUTHZ_IMAGE \
  python /opt/awr/registry/regctl.py scan --digest $CFG --cache $TRIVY_CACHE --work $DR/scan --name d3-subworker-scan-b25drill \
  --out $REG_ROOT/out/scan-drill-$T.json || echo 'scan reported a failure'
docker image rm 127.0.0.1:$REG_BACKEND_PORT/$REPO@$MAN >/dev/null && echo 'pulled drill image removed'
curl -s -o /dev/null -w 'drill manifest delete %{http_code}\n' -X DELETE http://127.0.0.1:$REG_BACKEND_PORT/v2/$REPO/manifests/$MAN
docker stop -t 10 d3-registry >/dev/null && docker run --rm --name d3-registry-gc --label d3.component=registry --network none --cpus 1 --memory 1g \
  --cgroup-parent d3.slice --read-only --cap-drop ALL --security-opt no-new-privileges --user $LAB_UID:$LAB_UID -v $REG_ROOT/data:/var/lib/registry \
  -v $REG_ROOT/conf/config.yml:/etc/distribution/config.yml:ro -e OTEL_TRACES_EXPORTER=none $REG_IMAGE \
  garbage-collect --delete-untagged /etc/distribution/config.yml 2>&1 | tail -1; docker start d3-registry >/dev/null
rm -rf $DR/scan; ls $DR/submissions/*/registry.json"
    ;;
  gc)
    # 垃圾回收须在仓库停止时执行（运行中的 inmemory 描述符缓存会把已删除的 Blob 报告为存在）：停止 → 一次性容器回收 → 启动
    ssh dmax "docker stop -t 10 d3-registry >/dev/null && docker run --rm --name d3-registry-gc --label d3.component=registry --network none \
  --cpus 1 --memory 1g --cgroup-parent d3.slice --read-only --cap-drop ALL --security-opt no-new-privileges --user $LAB_UID:$LAB_UID \
  -v $REG_ROOT/data:/var/lib/registry -v $REG_ROOT/conf/config.yml:/etc/distribution/config.yml:ro -e OTEL_TRACES_EXPORTER=none $REG_IMAGE \
  garbage-collect --delete-untagged /etc/distribution/config.yml 2>&1 | tail -2; docker start d3-registry >/dev/null; du -sh $REG_ROOT/data | cut -f1"
    ;;
  fetch)
    mkdir -p "$OUT"
    rsync -a --include '*.json' --exclude '*' "dmax:$REG_ROOT/out/" "$OUT/"
    ;;
  *)
    echo "usage: $0 <load|sync|up|down|status|drill|materialize|materialize-drill|gc|fetch>" >&2
    exit 2
    ;;
esac
