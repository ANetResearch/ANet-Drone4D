#!/usr/bin/env bash
# First use time (PRD-NFR-027; PERF-AC-063; AWR-18 §8.9; M16-FR-061, P1): a clean copy of the repository -> make setup ->
# make run -> the Shenzhen first screen reachable through the forwarded port; every step is timed; the total must stay
# <= 30 min (six-city build <= 6 min). Uses the local data/raw (no download) and a fresh worlds directory.
#   bash tests/e2e/first_use_time.sh [WORKDIR]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="${1:-$(mktemp -d /tmp/awr-first-use-XXXXXX)}"
LOG="$WORK/first_use_time.log"
mkdir -p "$WORK"
t() { date +%s; }
T0=$(t)
step() { local name="$1"; shift; local s; s=$(t); echo "[first-use] $name ..." | tee -a "$LOG"; "$@" >>"$LOG" 2>&1; echo "[first-use] $name $(( $(t) - s )) s" | tee -a "$LOG"; }
step "copy (clean clone)" rsync -a --exclude node_modules --exclude .venv --exclude worlds --exclude runs --exclude .cache \
  --exclude 'apps/web/dist' --exclude data "$ROOT/" "$WORK/repo/"
ln -s "$ROOT/data" "$WORK/repo/data"
cd "$WORK/repo"
step "make setup" make setup OFFLINE="${OFFLINE:-}"
step "make worlds (six cities)" make worlds
step "make build" make build
export AWR_PORT_OFFSET="${AWR_PORT_OFFSET:-8}"
PORT=$((8000 + 10 * AWR_PORT_OFFSET))
( make run >"$WORK/run.log" 2>&1 & echo $! >"$WORK/run.pid" )
S=$(t)
until curl -sf "http://127.0.0.1:$PORT/api/health/ready" >/dev/null; do sleep 1; [ $(( $(t) - S )) -lt 300 ] || { echo "run not ready" | tee -a "$LOG"; exit 1; }; done
curl -sf "http://127.0.0.1:$PORT/world/shenzhen" >/dev/null
echo "[first-use] make run -> ready $(( $(t) - S )) s" | tee -a "$LOG"
TOTAL=$(( $(t) - T0 ))
echo "[first-use] total ${TOTAL} s (<= 1800 s required)" | tee -a "$LOG"
kill -TERM "$(cat "$WORK/run.pid")" 2>/dev/null || true
[ "$TOTAL" -le 1800 ]
