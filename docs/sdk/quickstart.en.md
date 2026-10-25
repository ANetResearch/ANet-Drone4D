# ANet4D Python SDK quickstart

In about 15 minutes you install the `anet4d` SDK, write an example agent, run a 60 s L1 episode with 3 drones on the high-fidelity physics tier (`physics = realistic`, Gazebo Sim lockstep), confirm that the action streams are byte-identical whether the agent connects over the wire protocol or runs inside the Env process, and finally repeat the episode on the fast simulation tier (`physics = fast`) without code changes. Every output shown below comes from a real run; section 0 lists the measurement conditions. 中文版：[quickstart.zh-CN.md](quickstart.zh-CN.md).

The SDK was named `drone4d` up to V0.3.1. In V0.3.2, `drone4d` remains a deprecated compatibility alias that re-exports `anet4d` and emits a `DeprecationWarning`, and the command `drone4d-agent` still works with a notice on standard error; both will be removed in the next release. Replace `drone4d` with `anet4d` in imports and commands. The wire protocol `drone4d.bench.wire.v1`, the socket path `/run/drone4d/agent.sock` and the variable `DRONE4D_AGENT_SOCK` are legacy identifiers and keep their names.

## 0. Measured runs

| Steps | Environment | Measured |
|---|---|---|
| Steps 2 to 4 (install, write, run) | dmax, a fresh `python:3.11-slim` container (amd64, `--network none`, `--cpus 2`, `--memory 8g`), offline wheel directory | From container start to the end of the 3-drone 60 s high-fidelity episode: 49.4 s wall clock, of which the Env episode took 38.6 s |
| Section 4.3 (in-process run) | dmax, d3-gz image (Python 3.12, `--cpus 8`) | The same episode took 36.7 s wall clock; the per-drone action stream sha256 equals section 4.2 |
| Section 5 (fast simulation tier) | The researcher-side container from section 4.2; the platform side runs FleetSim | From container start to the end of the 3-drone 60 s fast simulation episode: 47.6 s wall clock, of which the Env episode took 39.2 s |
| Clean-host drill (ADR-202) | dmax, a fresh `python:3.12-slim` container (amd64, 2026-10-19) | 45.0 to 51.2 s from container start (limit 900 s); recorded in `docs/impl/D3-SDK-上手演练记录.md` |

Measured on 2026-10-18 (UTC), with the package under its former name `drone4d`; the package name and version in the installation output are shown for the renamed package `anet4d` 0.3.2, whose modules are unchanged apart from the name. The `--help` output in section 2 and the build in section 8 were rerun with `anet4d` 0.3.2 on 2026-10-25 (UTC). The platform side (section 4.1) was started first and waited for the agents. The formal clean-host drill runs in a fresh `python:3.12-slim` container on dmax under ADR-202 and is recorded in `docs/impl/D3-SDK-上手演练记录.md`.

## 1. Prerequisites

- Researcher host: Linux x86_64 with Python 3.11, 3.12 or 3.13. The agent depends only on `anet4d` and its dependencies; no repository code is needed.
- Platform side (runs the Env): the repository environment (`PYTHONPATH` with `python` and `tools/sdk/anet4d/src`), the world package `oryahovo-crop1` and the compiled event `crop-handoff-001`; the high-fidelity tier also needs the d3-gz image (Gazebo Sim with the native Gazebo plugins of this repository). Internal workspace members can use the world package and event already in place under `/data/anet-drone4d-d3/d3dev` on dmax.
- The agent and the Env exchange frames over a local Unix socket (wire protocol `drone4d.bench.wire.v1`): both run on the same host, or the agent runs in a container on that host with the socket directory bind-mounted.

## 2. Install the SDK

`anet4d` is not published on PyPI. The wheel ships as a release attachment, or you can build it from source as described in section 8. Put the wheel and its dependencies in one directory and install:

```bash
python3 -m venv ~/d4d
. ~/d4d/bin/activate
pip install --no-index --find-links /wheels "anet4d[l2]"
```

```text
Successfully installed anyio-4.15.1 certifi-2026.7.22 anet4d-0.3.2 h11-0.16.0 httpcore-1.0.9 httpx-0.28.1 idna-3.20 msgpack-1.2.2 numpy-2.4.6 pillow-11.3.0 typing_extensions-4.16.0
```

The core dependencies are `msgpack` (wire protocol) and `httpx` (bench API client). The `l2` extra adds `numpy` and `pillow` for the reference detector `ref_det_v1` under L2 observations; install `anet4d` without extras if you only run L1. When the host can reach a package index, drop `--no-index` to fetch the dependencies online.

Check the container entry point:

```bash
anet4d-agent --help
```

```text
usage: python -m anet4d.serve [-h] [--baseline BASELINE] [--sock SOCK]
                              [factory]

positional arguments:
  factory              pkg.module:Factory

options:
  -h, --help           show this help message and exit
  --baseline BASELINE  built-in baseline policy name
  --sock SOCK          default $DRONE4D_AGENT_SOCK or /run/drone4d/agent.sock
```

`anet4d-agent` is equivalent to `python -m anet4d.serve`.

## 3. Write an agent

An agent subclasses `anet4d.agent.DroneAgent`, with one instance per drone, and implements two methods:

- `reset(config)` is called once at the start of an episode; `config` is a `bench/agent_config.v1` document (task brief, vehicle limits, sensors, decision latency and communication parameters).
- `step(obs, inbox)` is called at every control step (10 Hz); `obs` is a `bench/obs.v1` observation, `inbox` holds the inter-drone messages received in this step, and the method returns a `bench/act.v1` action.

Observations and actions are plain dicts; `anet4d.agent` provides action builders such as `act`, `nav_takeoff`, `nav_goto`, `gimbal_look_at`, `out_msg` and `report`. Save the following file as `plaza_watch.py` in your working directory (it is identical to `docs/sdk/examples/plaza_watch.py` in the repository):

```python
"""快速上手示例 Agent `PlazaWatch`（docs/sdk/quickstart.zh-CN.md、quickstart.en.md）。

每架机起飞到各自的高度层，飞往分配给本机的公开锚点斜上方站位，云台持续对准锚点，并每 10 s 向标准错误输出累计检测数。
L1 直接读取观测中的检测列表；L2 由参考检测器 `ref_det_v1`（`anet4d.perception.ref_det.L2Perception`，需要 extras `l2`）
从相机图像得到同形的检测。示例不发送机间消息、不提交 report；report 与协同的写法见基线策略 `anet4d.baselines`。

同一份代码不改动地运行于各物理档（`physics = realistic`、`fast`）与观测档（L1、L2）；策略内无随机数、无墙钟依赖，
同一种子两次运行的动作流逐字节一致。
"""

from __future__ import annotations

import sys

from anet4d.agent import DroneAgent, act, gimbal_look_at, nav_goto, nav_takeoff

STANDOFF_M = 30.0                 # 站位相对锚点向西、向南各偏移 30 m（斜视）
LAYER_M = 8.0                     # 机间高度分层间隔：同高度交叉航线会机间碰撞
CRUISE_MPS = 6.0
LOG_EVERY = 100                   # 每 100 步（10 s）输出一次累计检测数


class PlazaWatch(DroneAgent):
    def reset(self, config: dict) -> None:
        super().reset(config)
        self.uav = config["uav_id"]
        brief = config["brief"]
        fleet = sorted([self.uav, *config["peers"]])
        i = fleet.index(self.uav)
        lo, hi = brief["area"]["alt_agl_range_m"]
        self.alt = min(max(0.5 * (lo + hi) + LAYER_M * (i - 0.5 * (len(fleet) - 1)), lo + 2.0), hi - 2.0)
        anchor = brief["anchors"][i % len(brief["anchors"])]["pos_enu_m"]
        self.anchor = (float(anchor[0]), float(anchor[1]))
        self.station = (self.anchor[0] - STANDOFF_M, self.anchor[1] - STANDOFF_M)
        self.cam = next(s["sensor_id"] for s in config["sensors"] if s["kind"] == "camera")
        self.perception = None
        if config["obs_level"] != "L1":
            from anet4d.perception.ref_det import L2Perception

            self.perception = L2Perception(config)
        self.ground: float | None = None
        self.in_mission = config["start"]["state"] == "airborne"     # 空中出生时不起飞，直接飞往站位
        self.seen = {"person": 0, "vehicle": 0}

    def step(self, obs: dict, inbox: list[dict]) -> dict:
        k = obs["k"]
        if self.ground is None:
            self.ground = float(obs["ground_hint"]["dtm_m"])
        dets = self.perception.detect(obs) if self.perception is not None else obs["sensors"].get("detections", [])
        for d in dets:
            self.seen[d["class"]] = self.seen.get(d["class"], 0) + 1
        if k % LOG_EVERY == 0 and k > 0:
            print(f"{self.uav} k={k} detections person={self.seen['person']} vehicle={self.seen['vehicle']}", file=sys.stderr, flush=True)
        if not self.in_mission:
            if k == 0:
                return act(k, nav=nav_takeoff(self.alt))
            climbed = obs["nav"]["pos_enu_m"][2] - self.ground >= self.alt - 3.0
            if obs["status"]["mode"] != "airborne" or not climbed:
                return act(k)                         # 爬升期间不下发新指令，沿用起飞指令
            self.in_mission = True
        z = self.ground + self.alt
        return act(k, nav=nav_goto((*self.station, z), CRUISE_MPS), gimbal=gimbal_look_at(self.cam, (*self.anchor, self.ground)))
```

Notes: navigation commands use the navigation solution delivered by the Env (a noisy estimate), never ground truth; a step without a new command keeps the previous one; the Env-side action guard validates every action against the schema and the vehicle limits and writes the receipts to `agents/<uav>/acts.jsonl` in the run directory.

## 4. Run the example agent

### 4.1 Platform side: start the environment and wait for agents

Run from the repository root (inside the d3-gz image for the high-fidelity tier). `run_quickstart.py` builds a local development Env (not ranked) with `anet4d.bench.make(..., physics="realistic")` and listens on one socket per drone:

```bash
PYTHONPATH=python:tools/sdk/anet4d/src python docs/sdk/examples/run_quickstart.py \
  --spec configs/bench.d3/crop-handoff-001.l1-3uav.run_spec.json \
  --worlds-root worlds --events-root d3data/events --physics realistic --gz-launcher local \
  --gz-export-root var/gz-export --duration-s 60 --seed 1 --out runs/quickstart-1 --sock-root /tmp/d4d-sock
```

```text
uav-01: waiting for an agent on /tmp/d4d-sock/uav-01/agent.sock
uav-02: waiting for an agent on /tmp/d4d-sock/uav-02/agent.sock
uav-03: waiting for an agent on /tmp/d4d-sock/uav-03/agent.sock
```

The default wait is 300 s (`--hello-timeout-s`). With `--gz-launcher docker`, Gazebo Sim runs as a sibling d3-gz container and the host does not need Gazebo Sim installed.

### 4.2 Researcher side: connect the agents

In the environment from step 2, in the directory that holds `plaza_watch.py`, start one process per drone:

```bash
for u in uav-01 uav-02 uav-03; do
  python -m anet4d.serve plaza_watch:PlazaWatch --sock /tmp/d4d-sock/$u/agent.sock 2> $u.log &
done
wait
```

`uav-01.log`:

```text
uav-01 k=100 detections person=119 vehicle=0
uav-01 k=200 detections person=701 vehicle=0
uav-01 k=300 detections person=1398 vehicle=0
uav-01 k=400 detections person=2082 vehicle=0
uav-01 k=500 detections person=2886 vehicle=0
```

When the episode ends, the Env sends CLOSE and the agent processes exit with status 0; the platform side prints a one-line summary on standard output:

```text
{"acts_sha256": {"uav-01": "dccb13582cb385756127c40e8eddc8fba6297bd8311dc8ee910ff91380e1f703", "uav-02": "8bfa3f7133cbbb89be1d7217feacceb6284bf25587407be6014862e0fd191fa4", "uav-03": "687b45b8bb0821efcd455e6dac0f405c7638e3d4d92a0c8526f475cb01c2f112"}, "duration_s": 60.0, "event": "crop-handoff-001", "n_ticks": 600, "obs_level": "L1", "physics": "realistic", "reports": 0, "run_id": "quickstart-1", "schema": "drone4d.quickstart.summary.v1", "tier": "realistic", "timeouts": {"uav-01": 0, "uav-02": 0, "uav-03": 0}, "wall_s": 38.63, "wire": {"uav-01": {"exited": null, "protocol_errors": 0}, "uav-02": {"exited": null, "protocol_errors": 0}, "uav-03": {"exited": null, "protocol_errors": 0}}, "world": "oryahovo-crop1"}
```

| Field | Meaning |
|---|---|
| `n_ticks` | Number of control steps (10 Hz × 60 s) |
| `timeouts` | Per-drone steps that exceeded the per-step wall-clock budget; a timed-out step keeps the previous command |
| `wire` | Per-drone wire protocol errors and abnormal exits (`null` means normal) |
| `acts_sha256` | sha256 of each drone's action stream `agents/<uav>/acts.jsonl`, for byte-level comparison |
| `reports` | Number of accepted reports (the example submits none) |

The run directory `runs/quickstart-1/` holds the run manifest `manifest.json`, the actions and receipts in `agents/<uav>/acts.jsonl`, the recording `raw.mcap` and the physics tier records in `phys/`.

### 4.3 In-process run

For development and debugging you can run the same episode with an in-process agent in the repository environment, without sockets:

```bash
PYTHONPATH=python:tools/sdk/anet4d/src:docs/sdk/examples python docs/sdk/examples/run_quickstart.py \
  --spec configs/bench.d3/crop-handoff-001.l1-3uav.run_spec.json \
  --worlds-root worlds --events-root d3data/events --physics realistic --gz-launcher local \
  --gz-export-root var/gz-export --duration-s 60 --seed 1 --out runs/quickstart-1-inproc --agent plaza_watch:PlazaWatch
```

```text
{"acts_sha256": {"uav-01": "dccb13582cb385756127c40e8eddc8fba6297bd8311dc8ee910ff91380e1f703", "uav-02": "8bfa3f7133cbbb89be1d7217feacceb6284bf25587407be6014862e0fd191fa4", "uav-03": "687b45b8bb0821efcd455e6dac0f405c7638e3d4d92a0c8526f475cb01c2f112"}, "duration_s": 60.0, "event": "crop-handoff-001", "n_ticks": 600, "obs_level": "L1", "physics": "realistic", "reports": 0, "run_id": "quickstart-1", "schema": "drone4d.quickstart.summary.v1", "tier": "realistic", "timeouts": {"uav-01": 0, "uav-02": 0, "uav-03": 0}, "wall_s": 36.7, "world": "oryahovo-crop1"}
```

The `acts_sha256` values of all three drones equal those of section 4.2: the action streams are byte-identical whether the agent runs in a separate environment over the wire protocol (a Python 3.11 container in section 0) or inside the Env process. The equivalent Python code:

```python
import json

from anet4d.bench import make
from anet4d.runner import run_episode
from plaza_watch import PlazaWatch

with open("configs/bench.d3/crop-handoff-001.l1-3uav.run_spec.json", encoding="utf-8") as f:
    run_spec = json.load(f)
run_spec.update(ranked=False, duration_s=60)
env = make(run_spec, run_id="dev-1", seed=1, worlds_root="worlds", events_root="d3data/events", out_dir="runs/dev-1",
           physics="realistic", gz={"launcher": "local", "export_root": "var/gz-export"})
manifest = run_episode(env, lambda uav: PlazaWatch())
print(manifest["n_ticks"], manifest["pose_sha256"])
```

## 5. Switch the physics tier and the observation level

The agent code stays unchanged; only the run parameters differ:

| Parameter | Physics tier or observation level | Platform requirement |
|---|---|---|
| `--physics realistic` | High-fidelity tier (Gazebo Sim lockstep; the only source of official results) | d3-gz image |
| `--physics fast` | Fast simulation tier (FleetSim; development, training and large-scale search; not ranked) | The repository environment and the vehicle directory `vehicles/` (or `AWR_VEHICLES_DIR`) |
| `--physics kinematic` | Kinematic development tier (development and tests only; not ranked) | The repository environment |
| `--spec configs/bench.d3/crop-handoff-001.l2-3uav.run_spec.json` | L2 camera-in-the-loop observations; detections come from `ref_det_v1` on the images | d3-sensorsvc image and a GPU (EGL rendering); the `l2` extra on the agent side |

To run the episode from section 4 on the fast simulation tier, change only `--physics` on the platform side; the researcher-side commands are the same as in section 4.2:

```bash
PYTHONPATH=python:tools/sdk/anet4d/src python docs/sdk/examples/run_quickstart.py \
  --spec configs/bench.d3/crop-handoff-001.l1-3uav.run_spec.json \
  --worlds-root worlds --events-root d3data/events --physics fast \
  --duration-s 60 --seed 1 --out runs/quickstart-1-fast --sock-root /tmp/d4d-sock
```

```text
{"acts_sha256": {"uav-01": "5ecd1215126d4e78eaa5f394778721f6f7c42ddf83790e1a99efc7807d0238ab", "uav-02": "c9a4b1250911d9beb540ea16f3d3c860c2087f94e4dbeb73ec61969ca3eb9c97", "uav-03": "9aaa44f0ff0256b65a98d4f1634c066b45085be0253bab794ac606b9d13f7937"}, "duration_s": 60.0, "event": "crop-handoff-001", "n_ticks": 600, "obs_level": "L1", "physics": "fast", "reports": 0, "run_id": "quickstart-1", "schema": "drone4d.quickstart.summary.v1", "tier": "fast", "timeouts": {"uav-01": 0, "uav-02": 0, "uav-03": 0}, "wall_s": 39.23, "wire": {"uav-01": {"exited": null, "protocol_errors": 0}, "uav-02": {"exited": null, "protocol_errors": 0}, "uav-03": {"exited": null, "protocol_errors": 0}}, "world": "oryahovo-crop1"}
```

The fast and high-fidelity tiers use different dynamics models, so action streams are not expected to match across tiers; within a tier, two runs with the same seed are byte-identical. `tests/d3/agentsdk_pkg/` installs the wheel in a temporary venv and runs the same `plaza_watch.py` on L1 episodes of the kinematic development tier and the fast simulation tier, on an L2 episode of the kinematic development tier (a CPU raster stand-in and the reference detector weights from the wheel), and on a high-fidelity episode inside the d3-gz image.

## 6. Baseline policies and the reference detector

Three baseline policies ship with the SDK as starting points for research. The sources live under `tools/sdk/anet4d/` in the repository, and `src/anet4d/` in the sdist `anet4d-<version>.tar.gz` is byte-identical to them:

| Name | Behavior | Source |
|---|---|---|
| `static_guard` | One observation post per drone at the public anchors and the task area center; once in place, the gimbal sweeps a 3 × 3 grid | `src/anet4d/baselines/static_guard.py` |
| `coverage_scan` | The task area is split into one strip per drone and covered with lawnmower passes at 70% camera footprint overlap; the gimbal looks almost straight down; no communication | `src/anet4d/baselines/coverage_scan.py` |
| `track_share` | Switches to tracking when a detection matches the brief appearance, broadcasts sightings and tracking claims every second, and hands a lost target to the nearest idle drone | `src/anet4d/baselines/track_share.py` |

The shared part (takeoff and positioning, multi-target association, report rules and evidence) is in `src/anet4d/baselines/common.py`; the r6 reference rows are registered in `src/anet4d/baselines/reference.py`. All three baselines run on L1 and L2, two runs with the same seed produce byte-identical action streams, and every report carries evidence and passes `bench/act.v1` validation. To connect a baseline instead of the example to the Env from section 4.1:

```bash
for u in uav-01 uav-02 uav-03; do
  python -m anet4d.serve --baseline track_share --sock /tmp/d4d-sock/$u/agent.sock 2> $u.log &
done
wait
```

The reference detector `ref_det_v1` inference and the L2 perception adapter are in `src/anet4d/perception/ref_det.py`; the frozen weights `src/anet4d/perception/ref_det_v1.npz` (sha256 `da68c6cb3e91e51bd389aefd4b25e482301fa5633a0c6ecb6ebf7d5e16b584ce`, verified on load) and the model card `model_card.json` ship in the wheel. The detector runs on the CPU only. The official L2 per-step wall-clock budget is 0.5 s (`STEP_BUDGET_S`); replaying a 360 s run of the L2 baseline in a `std` resource profile container (2 cores, 8 GB, no GPU, single-threaded BLAS) measured a worst step of 0.181 s (`STD_STEP_WALL_S`), which a custom agent can use to estimate its compute headroom.

## 7. Package the agent as an image

In an official evaluation, each drone's agent runs in its own isolated container (`--network none`, read-only root file system, non-root user, `std` resource profile with 2 cores, 8 GB and no GPU) and talks to the Env through the mounted `/run/drone4d/agent.sock`. An internal submission starts from the platform agent base image and adds its own package:

```dockerfile
FROM d3-agent-py:dev
COPY plaza_watch.py /opt/agent/plaza_watch.py
ENV PYTHONPATH=/opt/drone4d:/opt/agent
CMD ["python", "-m", "anet4d.serve", "plaza_watch:PlazaWatch"]
```

See `tools/bench/d3/examples/close_track/` for a complete example. The `anet4d` version inside the container comes from the base image; ranked runs accept images referenced by digest only.

## 8. Build the wheel and the sdist from source

Run from the repository root (the output goes to `var/sdk/dist/` and no build directory is left in the source tree; setuptools 77 or later is required):

The wheel bytes depend on the build-time `umask` and on the permission bits of the source files, so set `umask 022` and normalise the source permissions first; the sha256 below reproduces under these conditions (verified on 2026-10-25 for `anet4d` 0.3.2).

```bash
umask 022
rm -rf var/sdk && mkdir -p var/sdk && cp -r tools/sdk/anet4d var/sdk/src
chmod -R u=rwX,go=rX var/sdk/src
SOURCE_DATE_EPOCH=1760745600 python -m pip wheel --no-deps --no-build-isolation -w var/sdk/dist var/sdk/src
(cd var/sdk/src && SOURCE_DATE_EPOCH=1760745600 python -c "import setuptools.build_meta as b; b.build_sdist('../dist')")
ls var/sdk/dist
sha256sum var/sdk/dist/*.whl
```

```text
anet4d-0.3.2-py3-none-any.whl
anet4d-0.3.2.tar.gz
7a5fe21c338a0846aecd1a56b1fc8efdeee93582763f46ac595ea86754e53eb9  var/sdk/dist/anet4d-0.3.2-py3-none-any.whl
```

The wheel is pure Python (`py3-none-any`) and contains all modules, the reference detector weights and the model card; with a fixed `SOURCE_DATE_EPOCH`, two builds produce byte-identical wheels. The sdist keeps file timestamps in its tar members, so its sha256 changes with the copy time and is not compared byte by byte. A wheel directory for offline installation needs `anet4d` and the following dependencies (numpy 2.4.6 for Python 3.11, since numpy 2.5 no longer supports Python 3.11):

| Package | Version | Notes |
|---|---|---|
| `httpx`, `httpcore`, `h11`, `anyio`, `idna`, `certifi`, `typing_extensions` | 0.28.1, 1.0.9, 0.16.0, 4.15.1, 3.20, 2026.7.22, 4.16.0 | Pure Python |
| `msgpack` | 1.2.2 | Per interpreter version (cp311, cp312, cp313) |
| `numpy`, `pillow` | 2.5.3 (2.4.6 for cp311), 11.3.0 | The `l2` extra, per interpreter version |

## 9. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `RuntimeError: anet4d.bench.make needs the ANet4D repository environment (awr package)` | The Env was built on a host that has only `anet4d`. The Env belongs to the platform side and runs in the repository environment as in section 4.1; the researcher host runs only the agent processes from section 4.2 |
| `ModuleNotFoundError: No module named 'numpy'` (when an L2 episode starts) | The `l2` extra is missing. Run `pip install "anet4d[l2]"` |
| `ModuleNotFoundError: No module named 'httpx'` (`from anet4d.bench import make` in a platform image) | The platform image lacks the bench API client dependency. `run_quickstart.py` uses the equivalent `anet4d.local.make` and is not affected |
| `ERROR: Could not find a version that satisfies the requirement msgpack>=1.0` | The wheel directory has no binary wheel for the interpreter version or the processor architecture. Check `python -V` and `uname -m` and add the matching cp311, cp312 or cp313 x86_64 wheels |
| `awr.agentsdk.wire.WireError: 665 hello_timeout` | Not every agent connected within `--hello-timeout-s`. Check that the processes from section 4.2 are running and use the same socket paths; a container image built for another architecture runs under emulation and slows down considerably, so use an image that matches the host architecture |
| `ValueError: agent socket path is … bytes (AF_UNIX limit ~108)` | `--sock-root` is too long; Unix socket paths are limited to about 108 bytes. Use a short path such as `/tmp/d4d-sock` |
| `awr.phys.PhysError: 630: vehicle 'x500' has no FleetSim profile` | The fast simulation tier cannot find the vehicle directory. Run from the repository root, or point `AWR_VEHICLES_DIR` at a vehicle directory that contains `x500` |
| The gz export fails when the world directory is mounted read-only | Point `--gz-export-root` at a writable directory (section 4.1 already uses `var/gz-export`) |

## 10. After this guide

You now have:

- an installable `anet4d` environment and a reusable agent file;
- completed 3-drone L1 episodes on the high-fidelity tier (Gazebo Sim lockstep) and the fast simulation tier (FleetSim), with their run directories;
- a way to check that wire-protocol and in-process runs are byte-identical;
- the source locations of the three baseline policies and the reference detector, ready to modify.

Not yet covered:

- ranked results: official evaluations run as benchmark jobs in isolated containers on dmax and reach the prototype board through internal submissions;
- the platform-side environment for L2 camera-in-the-loop observations (d3-sensorsvc image and a GPU).
