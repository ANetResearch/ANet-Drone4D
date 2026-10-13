#!/usr/bin/env bash
# tools/deploy/emax/nginx-check.sh：在一次性 Ubuntu 24.04 容器（与 emax 相同的 nginx 1.24）中检查站点文件（M00，D2 WP-16、D2-FX-R2-deploy）。
#
#   tools/deploy/emax/nginx-check.sh [--out DIR] [-- <pytest 参数>]
#
#   需要 docker 与本地 ubuntu:24.04 镜像（NGINX_CHECK_IMAGE 可换成已装 nginx 的镜像，此时不再 apt 安装）；容器内 apt 安装 nginx、
#   openssl、python3。仓库以只读方式挂载，只有证据目录可写；容器退出即删除，不改动本机（本机不需要安装 nginx）。
#
# 步骤：自签证书放到 /etc/letsencrypt/live/<域名>/，依次以引导站点与正式站点 `nginx -t`；再以仓库 .venv 在容器内执行
# tests/ops/test_nginx_static_parity.py（/worlds/ 直出与 show-api 静态路由对拍）与 tests/ops/test_nginx_site_e2e.py（路径分流、
# 创建限流；容量接口微缓存 D2-AC-42：100 个并发客户端每秒到达 sandbox-api ≤ 1 次，上游 503、500、慢、卡住与不可达五种故障）。
# 证据写到 --out（缺省 .cache/deploy/emax/nginx-check/<UTC 时刻>/）：nginx-version.txt、nginx-t.log、pytest.log、junit.xml、
# capacity-<用例>.json（awr.deploy.nginx-capacity.v1）与汇总 nginx-check.json（awr.deploy.nginx-check.v1）。
# 验收入口：M16 harness 用例 d21.nginx-capacity（apps/web/perf/d21/cases.mjs）以 --out {runDir}/nginx 调用本脚本。
# 发版前改动站点文件时执行；emax 上则直接 `sudo nginx -t` 并执行同两个用例。
# 退出码（19 §16.2）：0 通过；1 用例失败；2 nginx -t 失败或参数错误；8 缺少 docker、镜像、.venv 或容器内安装失败。
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
IMAGE=${NGINX_CHECK_IMAGE:-ubuntu:24.04}
OUT=""
PYTEST_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --out) [ $# -ge 2 ] || { echo "nginx-check: --out 需要目录" >&2; exit 2; }; OUT=$2; shift 2 ;;
    --) shift; PYTEST_ARGS=("$@"); break ;;
    -h|--help) sed -n '2,17p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "nginx-check: 未知参数 $1" >&2; exit 2 ;;
  esac
done
OUT=${OUT:-$ROOT/.cache/deploy/emax/nginx-check/$(date -u +%Y%m%dT%H%M%SZ)}
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)

command -v docker >/dev/null || { echo "nginx-check: 需要 docker（修复：安装 docker 并把当前用户加入 docker 组）" >&2; exit 8; }
[ -x "$ROOT/.venv/bin/python" ] || { echo "nginx-check: 需要仓库 .venv（修复：make setup）" >&2; exit 8; }
docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "nginx-check: 本地没有镜像 $IMAGE（修复：docker pull $IMAGE）" >&2; exit 8; }

set +e
docker run --rm -i -v "$ROOT:$ROOT:ro" -v "$OUT:$OUT" -e ROOT="$ROOT" -e OUT="$OUT" -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
  "$IMAGE" bash -s -- "${PYTEST_ARGS[@]}" <<'IN'
set -uo pipefail
trap 'chown -R "$HOST_UID:$HOST_GID" "$OUT" 2>/dev/null || true' EXIT
export DEBIAN_FRONTEND=noninteractive
if ! command -v nginx >/dev/null; then
  { apt-get update && apt-get install -y --no-install-recommends nginx openssl python3 ca-certificates; } >"$OUT/apt.log" 2>&1 \
    || { echo "nginx-check: 容器内 apt 安装 nginx 失败（见 $OUT/apt.log；需要能访问 Ubuntu 软件源）" >&2; exit 8; }
fi
nginx -v 2>&1 | tee "$OUT/nginx-version.txt"
D=drone4d.agentnetwork.org.cn
mkdir -p "/etc/letsencrypt/live/$D" /var/www/certbot
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:secp384r1 -nodes -days 2 -subj "/CN=$D" \
  -keyout "/etc/letsencrypt/live/$D/privkey.pem" -out "/etc/letsencrypt/live/$D/fullchain.pem" >/dev/null 2>&1 || exit 8
: >"$OUT/nginx-t.log"
for site in bootstrap site; do
  f="$ROOT/tools/deploy/emax/$D.conf"; [ "$site" = bootstrap ] && f="$ROOT/tools/deploy/emax/$D.bootstrap.conf"
  echo "==> nginx -t：$site" | tee -a "$OUT/nginx-t.log"
  cp "$f" "/etc/nginx/sites-enabled/$D.conf"
  nginx -t 2>&1 | tee -a "$OUT/nginx-t.log"
  [ "${PIPESTATUS[0]}" -eq 0 ] || exit 2
done
echo "==> 静态对拍与站点端到端"
cd "$ROOT"
PYTHONDONTWRITEBYTECODE=1 AWR_NGINX_EVIDENCE_DIR="$OUT" "$ROOT/.venv/bin/python" -m pytest -q -p no:cacheprovider \
  --junitxml="$OUT/junit.xml" tests/ops/test_nginx_static_parity.py tests/ops/test_nginx_site_e2e.py "$@" 2>&1 | tee "$OUT/pytest.log"
[ "${PIPESTATUS[0]}" -eq 0 ] || exit 1
IN
rc=$?
set -e

"$ROOT/.venv/bin/python" - "$OUT" "$rc" "$IMAGE" <<'PY'
import json, sys, time
import xml.etree.ElementTree as ET
from pathlib import Path

out, rc, image = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
doc = {"schema": "awr.deploy.nginx-check.v1", "t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "exit": rc,
       "pass": rc == 0, "image": image, "ac": ["D2-AC-42", "M17-AC-020", "M11-AC-211", "API-AC-063"]}
v = out / "nginx-version.txt"
doc["nginx"] = v.read_text(encoding="utf-8").strip() if v.exists() else None
j = out / "junit.xml"
if j.exists():
    root = ET.parse(j).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    doc["tests"] = {k: int(suite.get(k, 0)) for k in ("tests", "failures", "errors", "skipped")}
cap = {}
for f in sorted(out.glob("capacity-*.json")):
    d = json.loads(f.read_text(encoding="utf-8"))
    cap[d["case"]] = {k: d.get(k) for k in ("clients", "requests", "req_per_s", "status", "upstream_arrivals",
                                            "arrivals_max_per_second", "arrivals_min_gap_s", "copy_max_age_s")}
doc["capacity"] = cap
(out / "nginx-check.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
c = cap.get("100-clients")
line = f"nginx-check: {'通过' if rc == 0 else '失败'}（退出码 {rc}）；{doc['nginx']}"
if c:
    line += (f"；100 客户端 {c['requests']} 次请求、到达 sandbox-api {c['upstream_arrivals']} 次、"
             f"每秒最多 {c['arrivals_max_per_second']} 次")
print(line)
print(f"nginx-check: 证据 {out}")
PY
exit "$rc"
