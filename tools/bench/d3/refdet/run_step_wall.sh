#!/usr/bin/env bash
# 在 dmax 上测量 L2 基线在容器 `std` 资源档内的每步墙钟（D3-AC-58；M26-FR-126）与 ref_det_v1 定稿权重的 torch/numpy 比对（step_wall.py、parity.py）。
# 用法（本机仓库根目录）：tools/bench/d3/refdet/run_step_wall.sh <阶段…>，阶段依次为
#   sync      同步代码到 $W（python/、契约、机型传感器模型取 git HEAD；anet4d SDK 与本目录取工作树），不触碰他人目录；
#   build     以 tools/deploy/dmax/images/agent/Dockerfile 在 $W/build-agent 构建 d3-agent-py:$TAG（不改动共享的 d3-agent-py:dev）；
#   capture   d3-sensorsvc 镜像（GPU=$GPU，EGL 渲染）录制一次 L2 运行的线协议帧（POLICY、SEED、PHYSICS、DURATION 可覆盖）；
#   parity    pytorch 镜像（CPU）计算 torch 参考输出，再在 d3-agent-py:$TAG 中以 numpy 推理逐元素比对；
#   replay    REPEATS 次：回放驱动容器 + 每机一个 std 档 Agent 容器（参数同 AgentContainers.argv，--cpus 2、--memory 8g）；
#   infer     std 档容器内的单帧推理墙钟；
#   summarize 合并为 $W/out/step_wall.json（3 次取中位，ADR-033）。
# 容器名 d3-a05-*，结束即删除；Agent 容器不挂载 GPU；日志与结果在 $W/out/。
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../../.." && pwd)
D=/data/anet-drone4d-d3/d3dev
W=${W:-$D/a05}
TAG=${TAG:-a05}
IMG=d3-agent-py:$TAG
GPU=${GPU:-3}
POLICY=${POLICY:-track_share}
SEED=${SEED:-1}
PHYSICS=${PHYSICS:-kinematic}
DURATION=${DURATION:-}
REPEATS=${REPEATS:-3}
SPEC=${SPEC:-configs/bench.d3/crop-handoff-001.l2-3uav.run_spec.json}
case "$GPU" in 1|2|3) ;; *) echo "GPU must be 1, 2 or 3 on dmax" >&2; exit 2;; esac
STD="--network none --read-only --tmpfs /tmp:rw,size=256m --cap-drop ALL --security-opt no-new-privileges --pids-limit 256 \
--cpus 2 --memory 8g --memory-swap 8g --ulimit nofile=1024:1024 --user 65534:65534"
GIT=(git)
if [ -n "${GIT_DIR:-}" ]; then GIT=(git --git-dir="$GIT_DIR" --work-tree="$ROOT"); fi

stage_sync() {
  local tmp
  tmp=$(mktemp -d)
  "${GIT[@]}" -C "$ROOT" archive HEAD python packages/contracts vehicles/sensor_models configs/bench.d3 | tar -x -C "$tmp"
  ssh dmax "mkdir -p $W/out $W/cache $W/worlds $W/d3data"
  rsync -a --delete --exclude __pycache__ "$tmp/python/" "dmax:$W/python/"
  rsync -a --delete "$tmp/packages/contracts/" "dmax:$W/contracts/"
  rsync -a --mkpath --delete "$tmp/vehicles/sensor_models/" "dmax:$W/vehicles/sensor_models/"
  rsync -a --mkpath --delete "$tmp/configs/bench.d3/" "dmax:$W/configs/bench.d3/"
  rm -rf "$tmp"
  rsync -a --mkpath --delete --exclude __pycache__ "$ROOT/tools/sdk/anet4d/src/" "dmax:$W/tools/sdk/anet4d/src/"
  rsync -a --mkpath --delete --exclude __pycache__ "$ROOT/tools/bench/d3/refdet/" "dmax:$W/tools/bench/d3/refdet/"
}

stage_build() {
  ssh dmax "mkdir -p $W/build-agent/wheels && rm -f $W/build-agent/wheels/*.whl && cp $D/wheels-gz/msgpack-*cp312*.whl \
    $D/wheels-agent/numpy-*cp312*.whl $D/wheels-agent/pillow-*cp312*.whl $W/build-agent/wheels/"
  rsync -a --mkpath --delete --exclude __pycache__ --exclude /anet4d/bench "$ROOT/tools/sdk/anet4d/src/" "dmax:$W/build-agent/sdk/"
  rsync -a "$ROOT/tools/deploy/dmax/images/agent/Dockerfile" "dmax:$W/build-agent/"
  ssh dmax "cd $W/build-agent && docker build -q -t $IMG . >/dev/null && docker image inspect $IMG --format '{{.Id}} {{.Size}}'"
}

stage_capture() {
  local dur=""
  [ -n "$DURATION" ] && dur="--duration-s $DURATION"
  ssh dmax "rm -rf $W/out/trace && mkdir -p $W/out/trace && docker rm -f d3-a05-capture >/dev/null 2>&1; \
    docker run --rm --name d3-a05-capture --network none --cpus 8 --memory 32g --user \$(id -u):\$(id -g) -e HOME=/tmp \
    --gpus device=$GPU -e NVIDIA_DRIVER_CAPABILITIES=all -e OMP_NUM_THREADS=1 -e OPENBLAS_NUM_THREADS=1 -e MKL_NUM_THREADS=1 \
    -e PYTHONPATH=/repo/python -e AWR_CONTRACTS_DIR=/repo/contracts -v $W:/repo -v $D/worlds:/repo/worlds:ro -v $D/d3data:/repo/d3data:ro \
    -v $W/vehicles:/opt/awr/vehicles:ro -w /repo d3-sensorsvc:dev \
    python3 tools/bench/d3/refdet/step_wall.py capture --spec $SPEC --policy $POLICY --seed $SEED --physics $PHYSICS $dur \
      --worlds-root worlds --events-root d3data/events --out out/trace > $W/out/capture.log 2>&1; tail -3 $W/out/capture.log"
}

stage_parity() {
  ssh dmax "mkdir -p $W/out/parity && docker rm -f d3-a05-parity-ref d3-a05-parity >/dev/null 2>&1; \
    docker run --rm --name d3-a05-parity-ref --network none --cpus 8 --memory 16g --user \$(id -u):\$(id -g) -e HOME=/tmp \
      -v $W:/repo -v $D/out/refdet:/refdet:ro -w /repo pytorch/pytorch:2.4.1-cuda12.1-cudnn9-runtime \
      python tools/bench/d3/refdet/parity.py ref --data /refdet/data --pt /refdet/model/ref_det_v1.pt --frames ${FRAMES:-64} --out out/parity && \
    chmod 777 $W/out/parity && docker run --rm --name d3-a05-parity $STD -v $W:/repo:ro -v $W/out/parity:/out -w /tmp $IMG \
      python /repo/tools/bench/d3/refdet/parity.py check --ref /out/parity-ref.npz --out /out/parity.json"
}

stage_replay() {
  local uavs
  uavs=$(ssh dmax "python3 -c 'import json;print(\" \".join(sorted(json.load(open(\"$W/out/trace/capture.json\"))[\"uavs\"])))'")
  for r in $(seq 1 "$REPEATS"); do
    ssh dmax "set -e; S=$W/out/sock; rm -rf \$S; mkdir -p \$S; chmod 755 \$S; \
      for n in \$(docker ps -a --format '{{.Names}}' | grep '^d3-a05-'); do docker rm -f \$n >/dev/null; done; \
      docker run -d --name d3-a05-driver --network none --cpus 2 --memory 4g --user \$(id -u):\$(id -g) \
        -v $W:/repo:ro -v $W/out:/out -v \$S:/sock -w /tmp $IMG \
        python /repo/tools/bench/d3/refdet/step_wall.py replay --trace /repo/out/trace --sock-root /sock --label rep-$r \
          --out /out/rep-$r.json --raw /out/rep-$r.raw.json >/dev/null; \
      for u in $uavs; do for i in \$(seq 1 100); do [ -S \$S/\$u/agent.sock ] && break; sleep 0.1; done; done; \
      for u in $uavs; do docker run -d --name d3-a05-agent-\$u --label d3.component=agent $STD -v \$S/\$u:/run/drone4d \
        -e DRONE4D_AGENT_SOCK=/run/drone4d/agent.sock $IMG python -m anet4d.serve --baseline $POLICY >/dev/null; done; \
      code=\$(docker wait d3-a05-driver); docker logs d3-a05-driver > $W/out/rep-$r.log 2>&1; \
      for u in $uavs; do docker wait d3-a05-agent-\$u >/dev/null || true; docker logs d3-a05-agent-\$u > $W/out/rep-$r-\$u.log 2>&1; \
        docker inspect d3-a05-agent-\$u --format '{{json .HostConfig}}' > $W/out/rep-$r-\$u.hostconfig.json; done; \
      for n in \$(docker ps -a --format '{{.Names}}' | grep '^d3-a05-'); do docker rm -f \$n >/dev/null; done; \
      tail -1 $W/out/rep-$r.log; exit \$code"
  done
}

stage_infer() {
  ssh dmax "mkdir -p $W/out/infer && chmod 777 $W/out/infer && docker rm -f d3-a05-infer >/dev/null 2>&1; \
    docker run --rm --name d3-a05-infer $STD -v $W:/repo:ro -v $W/out/infer:/out -w /tmp $IMG \
    python /repo/tools/bench/d3/refdet/step_wall.py infer --trace /repo/out/trace --uav ${UAV:-uav-01} --out /out/infer.json"
}

stage_summarize() {
  ssh dmax "cd $W/out && python3 - <<EOF
import json, os, platform, subprocess
img = subprocess.run(['docker', 'image', 'inspect', '$IMG', '--format', '{{.Id}}'], capture_output=True, text=True).stdout.strip()
cpu = next((l.split(':', 1)[1].strip() for l in open('/proc/cpuinfo') if l.startswith('model name')), None)
hc = json.load(open(sorted(f for f in os.listdir('.') if f.endswith('.hostconfig.json'))[0]))
env = {'host': 'dmax', 'cpu_model': cpu, 'host_cpus': os.cpu_count(), 'image': '$IMG', 'image_id': img, 'gpu': None,
       'agent_container': {'NanoCpus': hc.get('NanoCpus'), 'Memory': hc.get('Memory'), 'MemorySwap': hc.get('MemorySwap'),
                           'PidsLimit': hc.get('PidsLimit'), 'ReadonlyRootfs': hc.get('ReadonlyRootfs'), 'NetworkMode': hc.get('NetworkMode'),
                           'CapDrop': hc.get('CapDrop'), 'DeviceRequests': hc.get('DeviceRequests'), 'Ulimits': hc.get('Ulimits')},
       'blas_threads': 1, 'capture': json.load(open('trace/capture.json'))}
for u in env['capture']['uavs'].values():
    u.pop('inproc_step_wall', None)
json.dump(env, open('env.json', 'w'), indent=1)
EOF
    python3 $W/tools/bench/d3/refdet/step_wall.py summarize \$(ls | grep -E '^rep-[0-9]+[.]json\$' | sort) --infer infer/infer.json --env env.json --out step_wall.json"
}

[ $# -gt 0 ] || { echo "usage: $0 sync|build|capture|parity|replay|infer|summarize ..." >&2; exit 2; }
for s in "$@"; do "stage_$s"; done
