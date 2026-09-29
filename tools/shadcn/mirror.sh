#!/usr/bin/env bash
# Mirror shadcn registry items for one style into ./registry-mirror/r for offline installs (M15-FR-085; d04 §3.9).
# Pruned on purpose (tools/shadcn/VENDOR.md): the eight forbidden components (M15 §6.1), demo blocks and
# r/colors/index.json are not mirrored; the CLI only fetches r/colors/<baseColor>.json, and the pruned files carry hex
# colours and emoji that D1-AC-20 forbids in the repository scan scope.
set -euo pipefail
STYLE=${STYLE:-base-mira}
BASE=${BASE_URL:-https://ui.shadcn.com}
OUT=${OUT:-registry-mirror}
mkdir -p "$OUT/r/styles/$STYLE"
curl -fsS "$BASE/r/index.json" -o "$OUT/r/index.json"
mkdir -p "$OUT/r/colors"
for c in neutral zinc stone gray; do curl -fsS "$BASE/r/colors/$c.json" -o "$OUT/r/colors/$c.json" || echo "miss color $c"; done
curl -fsS "$BASE/r/styles/index.json" -o "$OUT/r/styles/index.json" || true
curl -fsS "$BASE/r/registries.json" -o "$OUT/r/registries.json" || true
ITEMS=$(python3 -c "import json;print(' '.join(i['name'] for i in json.load(open('$OUT/r/index.json'))))")
EXTRA="utils use-mobile font-inter font-jetbrains-mono"
FORBIDDEN=" chart sonner drawer carousel calendar navigation-menu pagination input-otp "
for n in $ITEMS $EXTRA; do
  case "$FORBIDDEN" in *" $n "*) continue ;; esac
  curl -fsS "$BASE/r/styles/$STYLE/$n.json" -o "$OUT/r/styles/$STYLE/$n.json" || echo "miss $n"
done
# registry:base payload served at /init?... ; a static server ignores the query string.
curl -fsS "$BASE/init?base=base&style=mira&baseColor=neutral&theme=neutral&iconLibrary=lucide&font=inter&rtl=false&menuAccent=subtle&menuColor=default&radius=small" -o "$OUT/init"
