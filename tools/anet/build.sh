#!/usr/bin/env bash
# tools/anet/build.sh（V1.0；M14 §6.15、§9.1）：按 versions.lock 构建 anet daemon 与 ANetHub。
# 约束：不编译 -tags shell；身份目录 runs/.anet/<world>/<vehicle>/（0700），绝不使用 ~/.anet；只允许自建 hub。
# D1 不使用真 ANet（M14 §2.1），本脚本只做版本检查与提示。
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCK="${HERE}/versions.lock"
if [[ ! -f "${LOCK}" ]]; then
  echo "versions.lock missing: ${LOCK}" >&2
  exit 2
fi
grep -E '^(wire|anet|anethub|anetcore) *=' "${LOCK}"
echo "anet build is a V1.0 task (M14-FR-069); nothing to build in D1." >&2
exit 0
