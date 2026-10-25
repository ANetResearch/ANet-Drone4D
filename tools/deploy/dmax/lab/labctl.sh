#!/usr/bin/env bash
# 内部工作区（dmax `d3` profile）的构建、初始化与核查（AWR-05 §7.11、ADR-166；D3-AC-51、D3-AC-25）。容器启停见 profile.sh。
#
#   build   以已提交的快照构建（K12）：`git archive $LAB_REF`（缺省 HEAD）解出 python、契约、apps/web 与本目录到临时目录，
#           不读工作树（并行改动与未入库文件不进入镜像）；可另给 LAB_PATCH=<统一 diff 文件> 在快照上打补丁（标签加 -p<sha256 前 8 位>，
#           只用于把尚未提交的本任务改动随同部署，补丁与提交号写入构建记录）。在快照内构建 apps/web 内部版（VITE_AWR_EXT_ROUTES 含
#           lab 组；node_modules 以符号链接接入本机依赖，@awr/* 指回快照），与 python/awr、契约、labctl.py、离线 wheel 一并同步到 dmax
#           /data/anet-drone4d-d3/build/lab/{py,web}，构建 d3-lab-py 与 d3-labweb，标签为 <提交号>[-p<补丁>] 与 $LAB_TAG（缺省
#           current），镜像标注 org.opencontainers.image.revision；构建记录写 $OUT/build-<标签>.json
#   snapshot <目录>  只做 build 的快照一步（解出源码、打补丁、接入 node_modules、写 <目录>/BUILD.json），供复测在同一快照上运行
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
# 用法（本机仓库根目录）：tools/deploy/dmax/lab/labctl.sh <build|snapshot|init|sample|scan|smoke|fetch>；OUT 缺省 .cache/deploy/dmax/lab。
# git 目录在仓库外时先导出 GIT_DIR 与 GIT_WORK_TREE（build 与 snapshot 需要；取不到提交时拒绝构建，不再回退到工作树）。
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
LAB_REF=${LAB_REF:-HEAD}
LAB_PATCH=${LAB_PATCH:-}
EXT_ROUTES=lab,lab-login,lab-board,lab-submissions,lab-run
# 快照包含的路径：镜像所需（python、契约、apps/web、本目录）与在同一快照上复测所需（tests/e2e、tools/lint）
SNAP_PATHS="package.json python packages/contracts apps/web tools/deploy/dmax/lab tests/e2e tools/lint"

# 解出 $LAB_REF 的快照到 $1（须为空目录或不存在），设置 COMMIT、TAG、WEB_DIRTY 并写 $1/BUILD.json
snapshot_src() {
  local dst=$1 psha="" pfiles=""
  if ! COMMIT=$(git -C "$ROOT" rev-parse --verify -q "$LAB_REF^{commit}"); then
    echo "labctl: $LAB_REF is not a commit here (export GIT_DIR and GIT_WORK_TREE when the git dir lives outside the work tree)" >&2
    exit 2
  fi
  TAG=${COMMIT:0:10}
  WEB_DIRTY=false
  mkdir -p "$dst"
  if [ -n "$(ls -A "$dst")" ]; then echo "labctl: snapshot target $dst is not empty" >&2; exit 2; fi
  # shellcheck disable=SC2086
  git -C "$ROOT" archive --format=tar "$COMMIT" -- $SNAP_PATHS | tar -x -C "$dst"
  if [ -n "$LAB_PATCH" ]; then
    LAB_PATCH=$(realpath "$LAB_PATCH")
    psha=$(sha256sum "$LAB_PATCH" | cut -d' ' -f1)
    pfiles=$(env -u GIT_DIR -u GIT_WORK_TREE git apply --numstat "$LAB_PATCH" | cut -f3 | sort -u | tr '\n' ' ')
    (cd "$dst" && env -u GIT_DIR -u GIT_WORK_TREE GIT_CEILING_DIRECTORIES="$(dirname "$dst")" git apply --whitespace=nowarn "$LAB_PATCH")
    TAG=$TAG-p${psha:0:8}
    case " $pfiles" in *" apps/web/"*) WEB_DIRTY=true ;; esac
  fi
  mkdir -p "$dst/node_modules/@awr"
  for e in "$ROOT"/node_modules/* "$ROOT"/node_modules/.bin; do
    [ "${e##*/}" = @awr ] || ln -s "$e" "$dst/node_modules/${e##*/}"
  done
  ln -s ../../packages/contracts "$dst/node_modules/@awr/contracts"
  ln -s ../../apps/web "$dst/node_modules/@awr/web"
  python3 - "$dst/BUILD.json" "$LAB_REF" "$COMMIT" "$TAG" "$psha" "$pfiles" "$WEB_DIRTY" <<'PY'
import json, sys, time
out, ref, commit, tag, psha, pfiles, web_dirty = sys.argv[1:]
doc = {"schema": "drone4d.lab.build.v1", "ref": ref, "commit": commit, "tag": tag, "patch_sha256": psha or None,
       "patch_files": pfiles.split() or [], "web_dirty": web_dirty == "true", "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
open(out, "w", encoding="utf-8").write(json.dumps(doc, indent=1) + "\n")
PY
}

case "$ACTION" in
  snapshot)
    [ -n "${2:-}" ] || { echo "usage: $0 snapshot <dir>" >&2; exit 2; }
    snapshot_src "$2"
    cat "$2/BUILD.json"
    ;;
  build)
    STAGE=$(mktemp -d "${TMPDIR:-/tmp}/d3-lab-build.XXXXXX")
    trap 'rm -rf "$STAGE"' EXIT
    snapshot_src "$STAGE/src"
    S=$STAGE/src
    L=$S/tools/deploy/dmax/lab
    (cd "$S/apps/web" && env -u GIT_DIR -u GIT_WORK_TREE VITE_AWR_EXT_ROUTES=$EXT_ROUTES AWR_BUILD_SHA="${COMMIT:0:10}" \
      AWR_BUILD_DIRTY=$WEB_DIRTY AWR_BUILD_CHANNEL=local npx vite build --logLevel warn --outDir "$STAGE/site" --emptyOutDir)
    ssh dmax "mkdir -p $CTX/py/wheels $CTX/web && rm -f $CTX/py/wheels/*.whl && for d in $WHEELS; do cp \$d/*.whl $CTX/py/wheels/; done"
    rsync -a --delete --exclude __pycache__ --exclude '*.egg-info' "$S/python/" "dmax:$CTX/py/python/"
    rsync -a --delete "$S/packages/contracts/" "dmax:$CTX/py/contracts/"
    rsync -a "$L/images/py/Dockerfile" "$L/requirements-lab.txt" "$L/labctl.py" "dmax:$CTX/py/"
    rsync -a --delete "$STAGE/site/" "dmax:$CTX/web/site/"
    rsync -a "$L/images/web/Dockerfile" "$L/nginx/nginx.conf" "$L/nginx/lab-headers.conf" "$L/nginx/lab.conf.template" "$L/nginx/lab-next.js" "dmax:$CTX/web/"
    LBL="--label org.opencontainers.image.revision=$COMMIT --label d3.build.tag=$TAG"
    ssh dmax "cd $CTX/py && docker build -q $LBL -t d3-lab-py:$TAG -t d3-lab-py:$LAB_TAG . >/dev/null && \
cd $CTX/web && docker build -q $LBL -t d3-labweb:$TAG -t d3-labweb:$LAB_TAG . >/dev/null && \
docker images --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}' | grep -E '^d3-lab(web|-py):($TAG|$LAB_TAG) '" | tee "$STAGE/images.txt"
    mkdir -p "$OUT"
    python3 - "$S/BUILD.json" "$STAGE/images.txt" "$OUT/build-$TAG.json" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1], encoding="utf-8"))
doc["images"] = [dict(zip(("ref", "id", "size"), ln.split(maxsplit=2))) for ln in open(sys.argv[2], encoding="utf-8").read().splitlines() if ln.strip()]
open(sys.argv[3], "w", encoding="utf-8").write(json.dumps(doc, indent=1) + "\n")
PY
    echo "build record: $OUT/build-$TAG.json"
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
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 825 -subj '/CN=$LAB_SERVER_NAME/O=ANet4D D3 lab' \
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
    echo "usage: $0 <build|snapshot <dir>|init|sample|scan|smoke|fetch>" >&2
    exit 2
    ;;
esac
