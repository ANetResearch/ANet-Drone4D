#!/usr/bin/env bash
# Fetch the ANet brand assets once and host them locally (M15-FR-109; ADR-032; AWR-15 §4.1). The runtime never requests
# GitHub (BRAND-03); rerun only when the brand owner changes the avatar, then commit the files and the regenerated lock.
# Usage: tools/shadcn/brand/fetch-brand.sh   (network needed once)
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
OUT="$ROOT/apps/web/public/brand"
mkdir -p "$OUT"
cp "$ROOT/refs/design/ANet/docs/media/anet-logo.svg" "$OUT/anet-logo.svg"
AVATAR="https://avatars.githubusercontent.com/u/305781773"
curl -fsSL -o "$OUT/avatar-96.png" "$AVATAR?s=96&v=4"
curl -fsSL -o "$OUT/avatar-460.png" "$AVATAR?s=460&v=4"
python3 "$ROOT/tools/shadcn/brand/make-favicons.py"
python3 "$ROOT/tools/shadcn/brand/brand-lock.py"
