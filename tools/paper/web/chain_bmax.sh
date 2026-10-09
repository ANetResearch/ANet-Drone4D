#!/usr/bin/env bash
# Paper campaign tail on bmax, run in order so the timing-sensitive steps (browser frame pacing, micro-benchmarks)
# get an otherwise idle host: delegation sweep, streaming on SwiftShader and on the A100, micro-benchmarks, then the
# remaining seeds of the overall campaign.
set -u
cd /root/anet-drone
PY=".venv/bin/python"
export PYTHONPATH=python:tools/paper
while pgrep -f "sweep.py rtl --procs 56 --run-id sens" > /dev/null; do sleep 20; done
$PY tools/paper/sweep.py deleg --procs 48 --run-id full seeds=5 > /tmp/sweep_deleg.log 2>&1
(cd apps/web && node ../../tools/paper/web/stream_bench.mjs --world shenzhen --device bmax_swiftshader \
  --out /root/anet-drone/runs/paper/stream/full --reps 3 > /tmp/stream_sw.log 2>&1)
(cd apps/web && node ../../tools/paper/web/stream_bench.mjs --world shenzhen --device bmax_a100 --gpu angle-vulkan \
  --out /root/anet-drone/runs/paper/stream/full --reps 3 > /tmp/stream_a100.log 2>&1)
taskset -c 8 $PY tools/paper/sweep.py micro --procs 1 --run-id full reps=3 > /tmp/sweep_micro.log 2>&1
$PY tools/paper/sweep.py rtl --procs 56 --run-id overall2 seeds=10 \
  policies=coupled,px4,px4_time,no_geometry,no_detour,no_wind,no_shared,no_energy,no_precheck > /tmp/sweep_overall2b.log 2>&1
echo CHAIN_DONE > /tmp/chain.done
