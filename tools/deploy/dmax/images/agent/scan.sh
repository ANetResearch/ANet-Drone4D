#!/usr/bin/env bash
# 提交扫描门复核（ADR-204、D3-AC-59；K18）：在 dmax 上以 subworker 的同一实现（`awr.bench.subworker.scan_image` + `TrivyScanner`：
# `docker save` 导出 → 隔离的 d3-trivy 容器以只读离线库扫描；HIGH 及以上、上游已有修复版本的漏洞即拒绝，体积 > 4 GB 拒绝）扫描
# 本机构建的镜像，用于基础镜像与示例提交镜像重建后的放行核对。扫描在 d3-k18-scangate 容器中执行（--network none，只挂载 docker
# 套接字、只读代码快照与只读离线库）；代码快照取本机工作树的 python/ 与 packages/contracts/。
# 用法（本机仓库根目录）：tools/deploy/dmax/images/agent/scan.sh <镜像> [<镜像> ...]；每个镜像输出一行 JSON：
# {"image", "id", "passed", "reason", "inspect", "trivy": {fixable_count, unfixed_count, by_severity, ...}}；任一镜像未通过时退出码 1。
set -euo pipefail
[ $# -ge 1 ] || { echo "usage: $0 <image> [<image> ...]" >&2; exit 2; }
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../../../.." && pwd)
D=/data/anet-drone4d-d3/d3dev
R=$D/scangate/src
W=$D/scangate/work
RUNNER=${SCAN_RUNNER_IMAGE:-d3-compile:b21}
ssh dmax "mkdir -p $R $W && chmod 1777 $W"
rsync -a --delete --exclude __pycache__ "$ROOT/python/" "dmax:$R/python/"
rsync -a --delete "$ROOT/packages/contracts/" "dmax:$R/contracts/"
args=$(printf '%q ' "$@")
ssh dmax "docker run --rm --name d3-k18-scangate --network none --cpus 4 --memory 8g --group-add \$(stat -c %g /var/run/docker.sock) \
  -e PYTHONPATH=/repo/python -e AWR_CONTRACTS_DIR=/repo/contracts -e AWR_HOST_PATH_MAP=/repo=$R,/work=$W \
  -v /var/run/docker.sock:/var/run/docker.sock -v /usr/bin/docker:/usr/bin/docker:ro -v $R:/repo:ro -v $W:/work \
  -v $D/trivy-cache:$D/trivy-cache:ro $RUNNER python3 -c '
import json, subprocess, sys
from pathlib import Path
from awr.bench.subworker import TrivyScanner, scan_image
t = TrivyScanner(cache_dir=Path(\"$D/trivy-cache\"), work_dir=Path(\"/work\"))
db = t.ready()
print(json.dumps({\"trivy_db\": db}, sort_keys=True))
bad = 0
for i, img in enumerate(sys.argv[1:]):
    iid = subprocess.run([\"docker\", \"image\", \"inspect\", img, \"--format\", \"{{.Id}}\"], capture_output=True, text=True).stdout.strip()
    rec = {}
    reason = scan_image(img, trivy=t, name=f\"d3-subworker-scan-k18-{i}\", record=rec)
    bad += reason is not None
    print(json.dumps({\"image\": img, \"id\": iid, \"passed\": reason is None, \"reason\": reason, **rec}, sort_keys=True))
sys.exit(1 if bad else 0)
' $args"
