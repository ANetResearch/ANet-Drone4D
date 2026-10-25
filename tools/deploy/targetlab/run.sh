#!/usr/bin/env bash
# Private target-engine review sandbox. Run from an independent checkout, behind SSH only.
set -euo pipefail
LAB_ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
cd "$LAB_ROOT"
export PATH="$LAB_ROOT/.venv/bin:$PATH"
export PYTHONPATH="$LAB_ROOT/python"
export AWR_PORT_OFFSET=27
export AWR_PROFILE=targetlab
export AWR_WORLD=synthcity
export AWR_SCENARIO=s0-synthcity-showcase
export AWR_WORLDS_DIR="$LAB_ROOT/worlds"
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
export AWR_BUILD_CHANNEL=local
export AWR_TARGET_LIFE_ENABLE=1
export AWR_TARGET_LIFE_DIR="$LAB_ROOT/var/target-life"
export AWR_LIFE_PROVIDER_CONFIG="$LAB_ROOT/var/private/gravitex.json"
.venv/bin/python tools/deploy/targetlab/config.py
exec .venv/bin/python -m awr.runtime.supervisor --profile targetlab --config .cache/targetlab/runtime.yaml \
  --set 'defaults.standby=false' --set 'cpu.pin=off' \
  --set "run.shm_root=/dev/shm/awr-target-lab-${UID}" \
  --set 'net.access_mode=loopback' --set 'net.worlds_allow=[synthcity]' \
  --set 'sandbox.worlds_allow=[synthcity]' --set 'sandbox.max_sessions=2' \
  --set 'sandbox.cpu_quota_cores=4.0' --set 'sandbox.mem_sandbox_mb=4096' \
  --set 'power.sandbox_idle_s=28800' --set 'power.sandbox_idle_busy_s=28800' \
  --set 'sandbox.park.idle_disconnected_s=28800' --set 'sandbox.park.idle_disconnected_busy_s=28800' \
  --set 'power.reap=false'
