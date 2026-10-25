#!/usr/bin/env bash
# tools/deploy/emax/d3-public/nginx-check.sh：在一次性 Ubuntu 24.04 容器（与 emax 相同的 nginx 1.24）中检查带 /lab/ 的站点文件（D3-AC-21）。
#
#   tools/deploy/emax/d3-public/nginx-check.sh [--out DIR] [-- <pytest 参数>]
#
#   需要 docker 与本地镜像：NGINX_CHECK_IMAGE（缺省 ubuntu:24.04；已装 nginx 的镜像不再 apt 安装）。仓库只读挂载，只有证据目录可写，
#   容器退出即删除。步骤：labsite.py render 生成"D2 + /lab/"站点文件，与 D2 站点文件一起以自签证书 `nginx -t`；再以仓库 .venv 在
#   容器内执行 tests/d3/deploy/test_d3_public_nginx_e2e.py（verify.py 全部站点检查、回滚与撤下 /lab/）与 test_d3_public_site.py。
#   证据写到 --out（缺省 .cache/deploy/emax/d3-public/nginx-check/<UTC 时刻>/）：nginx-version.txt、nginx-t.log、pytest.log、junit.xml、
#   d3-public-*.json 与汇总 nginx-check.json（awr.deploy.d3_public.nginx-check.v1）。
# 退出码：0 通过；1 用例失败；2 nginx -t 失败或参数错误；8 缺少 docker、镜像、.venv 或容器内安装失败。
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$HERE/../../../.." && pwd)
IMAGE=${NGINX_CHECK_IMAGE:-ubuntu:24.04}
OUT=""
PYTEST_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --out) [ $# -ge 2 ] || { echo "nginx-check: --out 需要目录" >&2; exit 2; }; OUT=$2; shift 2 ;;
    --) shift; PYTEST_ARGS=("$@"); break ;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "nginx-check: 未知参数 $1" >&2; exit 2 ;;
  esac
done
OUT=${OUT:-$ROOT/.cache/deploy/emax/d3-public/nginx-check/$(date -u +%Y%m%dT%H%M%SZ)}
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)

command -v docker >/dev/null || { echo "nginx-check: 需要 docker" >&2; exit 8; }
[ -x "$ROOT/.venv/bin/python" ] || { echo "nginx-check: 需要仓库 .venv（make setup）" >&2; exit 8; }
docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "nginx-check: 本地没有镜像 $IMAGE（docker pull $IMAGE）" >&2; exit 8; }
"$ROOT/.venv/bin/python" "$HERE/labsite.py" render --out "$OUT/site.conf" >/dev/null

set +e
docker run --rm -i --name "d3-f08-nginx-check-$$" -v "$ROOT:$ROOT:ro" -v "$OUT:$OUT" -e ROOT="$ROOT" -e OUT="$OUT" \
  -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" "$IMAGE" bash -s -- "${PYTEST_ARGS[@]}" <<'IN'
set -uo pipefail
trap 'chown -R "$HOST_UID:$HOST_GID" "$OUT" 2>/dev/null || true' EXIT
export DEBIAN_FRONTEND=noninteractive
if ! command -v nginx >/dev/null || ! command -v rsync >/dev/null; then
  { apt-get update && apt-get install -y --no-install-recommends nginx openssl python3 ca-certificates rsync; } >"$OUT/apt.log" 2>&1 \
    || { echo "nginx-check: 容器内 apt 安装失败（见 $OUT/apt.log）" >&2; exit 8; }
fi
nginx -v 2>&1 | tee "$OUT/nginx-version.txt"
D=drone4d.agentnetwork.org.cn
mkdir -p "/etc/letsencrypt/live/$D" /var/www/certbot
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:secp384r1 -nodes -days 2 -subj "/CN=$D" \
  -keyout "/etc/letsencrypt/live/$D/privkey.pem" -out "/etc/letsencrypt/live/$D/fullchain.pem" >/dev/null 2>&1 || exit 8
: >"$OUT/nginx-t.log"
for f in "$ROOT/tools/deploy/emax/$D.conf" "$OUT/site.conf"; do
  echo "==> nginx -t：$f" | tee -a "$OUT/nginx-t.log"
  cp "$f" "/etc/nginx/sites-enabled/$D.conf"
  nginx -t 2>&1 | tee -a "$OUT/nginx-t.log"
  [ "${PIPESTATUS[0]}" -eq 0 ] || exit 2
done
cd "$ROOT"
PYTHONDONTWRITEBYTECODE=1 AWR_NGINX_EVIDENCE_DIR="$OUT" "$ROOT/.venv/bin/python" -m pytest -q -p no:cacheprovider -p no:warnings \
  --junitxml="$OUT/junit.xml" tests/d3/deploy/test_d3_public_site.py tests/d3/deploy/test_d3_public_nginx_e2e.py "$@" 2>&1 | tee "$OUT/pytest.log"
[ "${PIPESTATUS[0]}" -eq 0 ] || exit 1
IN
rc=$?
set -e

"$ROOT/.venv/bin/python" - "$OUT" "$rc" "$IMAGE" <<'PY'
import json, sys, time
import xml.etree.ElementTree as ET
from pathlib import Path

out, rc, image = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
doc = {"schema": "awr.deploy.d3_public.nginx-check.v1", "t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "exit": rc,
       "pass": rc == 0, "image": image, "ac": ["D3-AC-21"]}
v = out / "nginx-version.txt"
doc["nginx"] = v.read_text(encoding="utf-8").strip() if v.exists() else None
j = out / "junit.xml"
if j.exists():
    root = ET.parse(j).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    doc["tests"] = {k: int(suite.get(k, 0)) for k in ("tests", "failures", "errors", "skipped")}
for name in ("verify-lab", "verify-d2", "rollback-timing"):
    f = out / f"d3-public-{name}.json"
    if f.exists():
        d = json.loads(f.read_text(encoding="utf-8"))
        doc[name] = {c["name"]: c["ok"] for c in d["checks"]} if "checks" in d else d
rate = out / "d3-public-verify-lab.json"
if rate.exists():
    d = json.loads(rate.read_text(encoding="utf-8"))
    doc["rate"] = next((c["detail"] for c in d["checks"] if c["name"] == "rate"), None)
(out / "nginx-check.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
print(f"nginx-check: {'通过' if rc == 0 else '失败'}（退出码 {rc}）；{doc['nginx']}；证据 {out}")
PY
exit "$rc"
