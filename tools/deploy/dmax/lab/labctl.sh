#!/usr/bin/env bash
# 内部工作区（dmax `d3` profile）的构建、初始化与核查（AWR-05 §7.11、ADR-166；D3-AC-51、D3-AC-25）。容器启停见 profile.sh。
#
#   build   本机构建 apps/web 内部版（VITE_AWR_EXT_ROUTES 含 lab 组，产物放临时目录，同步后删除），与 python/awr、契约、
#           labctl.py、离线 wheel 一并同步到 dmax /data/anet-drone4d-d3/build/lab/{py,web}，构建 d3-lab-py 与 d3-labweb，
#           标签为提交号与 $LAB_TAG（缺省 current）
#   init    dmax 上建立部署根：数据根（首次从 $LAB_SEED 复制榜单、提交与 Env 运行；采集运行与世界包以符号链接接入）、TLS 自签证书
#           （SAN 含 ZeroTier 地址与 $LAB_SERVER_NAME，私钥 0600，已存在时不重建）、属主 $LAB_UID
#   sample  在 d3-bench 容器内以 public 档（只含观测层）导出一个采集运行（缺省 cap-crop-3uav-demo，30 s、3 机）并注册到分发
#           catalog（D3-AC-25"内部工作区经 catalog 按需取数"的链路核查）
#   scan    TCP 连接扫描（labctl.py scan）：本机对 dmax 公网地址与 ZeroTier 地址，dmax 宿主对自身每个非回环地址；结果写
#           $OUT/scan-{public,zerotier,host-<网卡>}.json
#   smoke   本机经 ZeroTier 做无令牌核查，并在 d3-bench 容器内（临时评测员 token，结束即撤销）做登录、会话、实时网关与 Rerun 核查；
#           结果写 $OUT/smoke-{zerotier,inside}.json
#   fetch   把 dmax $LAB_ROOT/out 下的核查记录（JSON，不含密钥）复制到 $OUT
#
# 用法（本机仓库根目录）：tools/deploy/dmax/lab/labctl.sh <build|init|sample|scan|smoke|fetch>；OUT 缺省 .cache/deploy/dmax/lab。
# 不输出任何令牌、Cookie 与私钥；bench token 由管理员在 dmax 上以 `docker exec -it d3-bench python -m awr.bench.ctl token issue …` 签发。
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../../.." && pwd)
# shellcheck source=lab.env
. "$HERE/lab.env"
ACTION=${1:-}
OUT=${OUT:-$ROOT/.cache/deploy/dmax/lab}
CTX=/data/anet-drone4d-d3/build/lab
WHEELS="/data/anet-drone4d-d3/d3dev/wheels-d2-aa5c1253550e /data/anet-drone4d-d3/d3dev/wheels-rerun /data/anet-drone4d-d3/d3dev/wheels-catalog"
PYLOCAL=${PYLOCAL:-$ROOT/.venv-d3/bin/python}
SAMPLE_RUN=${SAMPLE_RUN:-cap-crop-3uav-demo}

case "$ACTION" in
  build)
    SHA=$(git -C "$ROOT" rev-parse --short=10 HEAD 2>/dev/null || echo nogit)
    if [ -n "$(git -C "$ROOT" status --porcelain -- python packages/contracts apps/web tools/deploy/dmax/lab 2>/dev/null)" ]; then SHA="$SHA-dirty"; fi
    STAGE=$(mktemp -d "${TMPDIR:-/tmp}/d3-lab-build.XXXXXX")
    trap 'rm -rf "$STAGE"' EXIT
    (cd "$ROOT/apps/web" && VITE_AWR_EXT_ROUTES=lab,lab-login,lab-board,lab-submissions,lab-run AWR_BUILD_SHA="${SHA%-dirty}" \
      AWR_BUILD_CHANNEL=local npx vite build --logLevel warn --outDir "$STAGE/site" --emptyOutDir)
    ssh dmax "mkdir -p $CTX/py/wheels $CTX/web && rm -f $CTX/py/wheels/*.whl && for d in $WHEELS; do cp \$d/*.whl $CTX/py/wheels/; done"
    rsync -a --delete --exclude __pycache__ --exclude '*.egg-info' "$ROOT/python/" "dmax:$CTX/py/python/"
    rsync -a --delete "$ROOT/packages/contracts/" "dmax:$CTX/py/contracts/"
    rsync -a "$HERE/images/py/Dockerfile" "$HERE/requirements-lab.txt" "$HERE/labctl.py" "dmax:$CTX/py/"
    rsync -a --delete "$STAGE/site/" "dmax:$CTX/web/site/"
    rsync -a "$HERE/images/web/Dockerfile" "$HERE/nginx/nginx.conf" "$HERE/nginx/lab-headers.conf" "$HERE/nginx/lab.conf.template" "dmax:$CTX/web/"
    ssh dmax "cd $CTX/py && docker build -q -t d3-lab-py:$SHA -t d3-lab-py:$LAB_TAG . >/dev/null && \
cd $CTX/web && docker build -q -t d3-labweb:$SHA -t d3-labweb:$LAB_TAG . >/dev/null && \
docker images --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}' | grep -E '^d3-lab(web|-py):($SHA|$LAB_TAG) '"
    ;;
  init)
    ssh dmax "set -e; mkdir -p $LAB_DATA/dataset $LAB_DATA/rerun $LAB_ROOT/tls $LAB_ROOT/out
if [ ! -e $LAB_DATA/.seeded ]; then
  for d in boards submissions dataset events worlds; do [ -e $LAB_SEED/\$d ] && cp -a $LAB_SEED/\$d/. $LAB_DATA/\$d/ 2>/dev/null || mkdir -p $LAB_DATA/\$d; done
  for d in events worlds; do [ -d $LAB_CAPTURE/\$d ] && cp -an $LAB_CAPTURE/\$d/. $LAB_DATA/\$d/ || true; done
  date -u +%FT%TZ > $LAB_DATA/.seeded
fi
for r in $LAB_CAPTURE/dataset/*/runs/*; do
  ev=\$(basename \$(dirname \$(dirname \$r))); mkdir -p $LAB_DATA/dataset/\$ev/runs
  [ -e $LAB_DATA/dataset/\$ev/runs/\$(basename \$r) ] || ln -s \$r $LAB_DATA/dataset/\$ev/runs/\$(basename \$r)
done
for w in $LAB_WORLDS/*/; do w=\${w%/}; [ -f \$w/world.json ] && [ ! -e $LAB_DATA/worlds/\$(basename \$w) ] && ln -s \$w $LAB_DATA/worlds/\$(basename \$w); done
if [ ! -s $LAB_ROOT/tls/key.pem ]; then
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 825 -subj '/CN=$LAB_SERVER_NAME/O=ANet Drone4D D3 lab' \
    -addext 'subjectAltName=IP:$LAB_BIND,DNS:$LAB_SERVER_NAME' -addext 'basicConstraints=critical,CA:TRUE' \
    -keyout $LAB_ROOT/tls/key.pem -out $LAB_ROOT/tls/cert.pem 2>/dev/null
fi
chmod 0600 $LAB_ROOT/tls/key.pem; chmod 0644 $LAB_ROOT/tls/cert.pem; chmod 0755 $LAB_ROOT/tls
chown -R $LAB_UID:$LAB_UID $LAB_DATA $LAB_ROOT/out; chmod 0750 $LAB_DATA
openssl x509 -in $LAB_ROOT/tls/cert.pem -noout -subject -enddate -ext subjectAltName | tr '\n' ' '; echo
du -sh $LAB_DATA | cut -f1"
    ;;
  sample)
    ssh dmax "set -e; R=\$(ls -d $LAB_DATA/dataset/*/runs/$SAMPLE_RUN | head -1); O=$LAB_DATA/derived/$SAMPLE_RUN/public
docker exec d3-bench sh -c \"mkdir -p \$O && python -m awr.bench.recorder.rerun_export --from-mcap \$R --out \$O --profile public --verify\" | tail -3
docker exec d3-bench python -m awr.bench.catalog register --catalog dist --run \$O | tail -3"
    ;;
  scan)
    mkdir -p "$OUT"
    PUB=${LAB_PUBLIC_IP:-$(ssh dmax "ip -4 route get 1.1.1.1" | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}')}
    python3 "$HERE/labctl.py" scan --host "$PUB" --label public --out "$OUT/scan-public.json"
    python3 "$HERE/labctl.py" scan --host "$LAB_BIND" --label zerotier --out "$OUT/scan-zerotier.json"
    for ia in $(ssh dmax "ip -4 -o addr show | awk '\$2 != \"lo\" {split(\$4, a, \"/\"); print \$2 \"=\" a[1]}'"); do
      ssh dmax "python3 - scan --host ${ia#*=} --label dmax-host:${ia%%=*} --ports 22,18640-18670" < "$HERE/labctl.py" > "$OUT/scan-host-${ia%%=*}.json"
    done
    ;;
  smoke)
    mkdir -p "$OUT"
    scp -q "dmax:$LAB_ROOT/tls/cert.pem" "$OUT/lab-cert.pem"
    rc=0
    "$PYLOCAL" "$HERE/labctl.py" smoke --base "$LAB_ORIGIN" --cafile "$OUT/lab-cert.pem" --out "$OUT/smoke-zerotier.json" || rc=1
    ssh dmax "docker exec d3-bench python /opt/awr/tools/labctl.py smoke --base $LAB_ORIGIN --cafile /opt/lab-cert.pem --issue \
      --data-root $LAB_DATA ${RERUN_RUN:+--rerun-run $RERUN_RUN}" < /dev/null > "$OUT/smoke-inside.json" || rc=1
    exit $rc
    ;;
  fetch)
    mkdir -p "$OUT"
    rsync -a --include '*.json' --exclude '*' "dmax:$LAB_ROOT/out/" "$OUT/"
    ;;
  *)
    echo "usage: $0 <build|init|sample|scan|smoke|fetch>" >&2
    exit 2
    ;;
esac
