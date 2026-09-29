#!/usr/bin/env bash
# Remote access smoke (D1-AC-33; PERF-AC-058; AWR-18 §8.9; M16-FR-061): through an `ssh -N -L` forward the page, the World
# Package with a single Range request, token issuance and the awr.rt.v1 WebSocket handshake all work, and the forwarded
# localhost Origin is accepted.
#   bash tests/e2e/remote_smoke.sh [BASE] [--ssh user@host]
#     BASE        api of a running backend (default: a ci-profile supervisor started for the smoke)
#     --ssh HOST  forward through a real ssh session to HOST (run on the client machine); without it the forward goes
#                 through `ssh localhost` when key login works, otherwise through a local TCP relay (reported as such)
# Exit 0 pass, 1 failure, 6 world missing.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="${AWR_PY:-$ROOT/.venv/bin/python}"
exec "$PY" -P "$ROOT/tests/e2e/remote_smoke_check.py" "$@"
