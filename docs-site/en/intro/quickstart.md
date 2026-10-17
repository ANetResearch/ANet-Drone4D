# Quick start

> Clone, install, generate a world, run the shared show and the sandbox, and drive a session with the Python SDK

This page starts from "the source is not on the machine yet" and ends at "I ran the shared show locally, and created, operated and
ended a sandbox session from code". The end state is three concrete things: a running World Runtime on this machine (the shared
show, reachable from a browser), a local sandbox control plane (sandbox-pool and sandbox-api), and a Python program that creates a
session, adds a drone, reads its state and ends the session through the open interface.

Every output below comes from a real run on 2026-10-13 that followed this page from `git clone` onwards, on an 8-core x86-64 development
machine (Ubuntu, Python 3.12.3, Node 22.12.0, no GPU). For readability the repository path is written as `~/ANet-Drone4D` and ports are
written with their default values (8000, 8002, 7447); run IDs, process IDs, session IDs and timings differ between machines.

:::{note}
Prefer not to install anything? The [public instance](https://drone4d.agentnetwork.org.cn) offers the shared show and the sandbox of
the same version; see {doc}`../guide/showcase`. Requirements, ports and configuration are described in full in {doc}`install`.
:::

---

## Prerequisites

| Item | Requirement |
|---|---|
| System | Linux x86-64 (Ubuntu 22.04 or 24.04) |
| Tools | Python 3.12 (with venv), Node 22.12.x, make, git, curl |
| Resources | About 20 GB free disk, at least 1 GiB free in `/dev/shm`; no GPU required |
| Network | The first installation needs GitHub, PyPI and the npm registry |

## Get the source: `git clone`

```sh
cd ~
git clone https://github.com/ANetResearch/ANet-Drone4D.git
cd ANet-Drone4D
```

```text
Cloning into 'ANet-Drone4D'...
Updating files: 100% (3245/3245), done.
```

In this run the working tree is about 126 MB and the clone took 5.8 s. The repository contains no point clouds, world packages or run
artifacts: `worlds/`, `data/raw/` and `runs/` are in `.gitignore` and are produced locally by the following steps.

`main` is the maintained branch; to match the released version byte for byte, check out the version tag:

```sh
git checkout v0.2.0
```

## Install dependencies: `make setup`

```sh
make setup
```

```text
创建 .venv（python3.12）
自举 uv 0.12.19
cd ~/ANet-Drone4D && ~/ANet-Drone4D/.venv/bin/uv pip sync requirements.lock --python ~/ANet-Drone4D/.venv/bin/python
Resolved 70 packages in 757ms
Uninstalled 1 package in 118ms
Installed 69 packages in 174ms
 + annotated-doc==0.0.5
 + annotated-types==0.8.0
 ...
 + zstandard==0.25.0
cd ~/ANet-Drone4D && ~/ANet-Drone4D/.venv/bin/pip install --quiet -e . --no-deps

added 829 packages in 36s
gen.py: up to date (layout_id 0x009600AE, contracts 1.2.0)
gen_golden.py: up to date
gen_frames_golden.py: 4106 cases, up to date
gen_env_golden.py: 10261 cases, up to date
gen_fixtures.py: up to date
gen.mjs: up to date (layout_id 0x009600AE, contracts 1.2.0)
DOC-01  OK    CPU 架构：x86_64
DOC-02  OK    Python 3.12：3.12.3
DOC-04  OK    Node 22.12.x：22.12.0
DOC-08  OK    依赖版本（含 zenoh 1.10.1）：与 pyproject 锁定一致
DOC-22  OK    配置与访问模式：profile=dev access=loopback bind=127.0.0.1
DOC-09  OK    端口空闲：[8000, 7447, 5173] 空闲
DOC-10  OK    /dev/shm 可写且 ≥ 1 GiB：/dev/shm/awr free=31.4 GiB writable=True
DOC-11  OK    残留运行目录：无
DOC-12  OK    磁盘余量：free=18.7 GB
DOC-14  WARN  原始数据：0 个 PLY（~/ANet-Drone4D/data/raw/urbanscene3d）
        修复：make fetch-data
DOC-15  WARN  默认世界：~/ANet-Drone4D/worlds/shenzhen/world.json 尚未构建（make run 会自动构建）
        修复：make worlds
M11-R1  OK    StateRing 自测：publish/attach/read_latest/header
M11-R2  OK    zenoh 自环：query/reply 成功（35 ms）
awr doctor --quick: 通过（0.2 s）
make setup 完成
```

The build tooling prints its messages in Chinese; the output is reproduced exactly as printed. `make setup` is idempotent and does four
things in order:

1. **Python dependencies.** Creates `.venv` with `python3.12`, bootstraps a pinned uv, installs the 70 packages of the lock file (with
   hashes) through `uv pip sync requirements.lock`, then installs this repository's `awr` package in editable mode. The 69-line install
   list is elided above.
2. **Frontend dependencies.** `npm ci` installs from `package-lock.json`; it is skipped when the lock file is unchanged and the previous
   install succeeded (`FORCE=1` forces a reinstall).
3. **Contract artifact check.** The Python and TypeScript types, binary layouts and golden cases generated from 75 JSON Schemas must match
   the versions in the repository (`up to date`); a mismatch means the working tree was modified, and `make contracts` regenerates them.
4. **Quick diagnosis.** `awr doctor --quick` checks the environment item by item: `WARN` does not block, `ERROR` fails with a non-zero
   exit code and a fix command.

This run took 73 s with the local uv and npm caches already in place; a first installation on a new machine downloads the dependencies and
usually takes 3 to 8 min.

### Warnings in the output

The two `WARN` items are expected on a machine without UrbanScene3D data: DOC-14 ("原始数据", raw data) reports that `data/raw/`
holds none of the six cities, and DOC-15 (默认世界, default world) reports that the default world Shenzhen is not built. The synthetic demo
city generated in the next step becomes the fallback world.

The full output also contains two kinds of warnings unrelated to function, omitted above: npm `EBADENGINE` warnings for four development
tool packages such as `react-doctor` and `eslint-scope` (they declare Node 22.13 or later and take no part in the build or runtime), and a
`Duplicate Operation ID` notice from FastAPI while it generates the OpenAPI schema, for the one REST route that serves both `GET` and `HEAD`
(`segment_file`, run segment downloads). The generated id ends in `_get` or `_head` depending on the interpreter's hash seed, so the exact
suffix in your output can differ from a previous run (`segment_file_api_runs__run__segments__name__get` or `..._head`); either form is
expected and harmless, and the committed OpenAPI snapshot is not affected (`tools/contracts/gen_openapi.py` normalizes it).

## Generate the synthetic demo city: `make demo-world`

```sh
make demo-world
```

```text
[worldpkg] synthcity: generate (missing; synthetic, ADR-077)
[worldpkg] 1-minute loadavg 7.2 > 4: --jobs reduced to 1 (M03 R-2)
[synthcity] ingest    done  13.56 s points=3924500
[synthcity] dsm       done   3.30 s
[synthcity] tile      done   5.30 s
[synthcity] tile summary roots=1 nodes=439 depth=5 first_screen_g=190507/2286084
[synthcity] zones     done   0.15 s
[synthcity] hash      done   0.73 s
[synthcity] validate  done   1.85 s
[synthcity] published 9e05c2364f58 in 25.3 s
OK   synthcity     exit=0 cv=9e05c2364f58 built 25.3 s
```

ANet Synthetic City is generated from a fixed seed: 1.2 km × 1.2 km, about 3.9 million points, with roads, a river, parks and two
300-meter-class towers, and no third-party data. It goes through the same pipeline as the real cities:

| Stage | Product |
|---|---|
| ingest | Points normalized to World ENU (meters, right-handed, +Z up) with per-point attributes |
| dsm | 2 m digital surface model and 10 m digital terrain model |
| tile | Potree 2.0 compatible octree (ANET_Q16 point encoding) and the byte ranges of the first-screen levels |
| zones | Zones and no-fly areas used by scenarios (`zones.geojson`) |
| hash | The content version `cv` (here `9e05c2364f58`), used for caching and recording compatibility |
| validate | Semantic and structural validation; the world is published only with zero errors |

The second line reports that the 1-minute load average was above 4 during the build, so the builder reduced its parallelism to 1 to leave
room for other work on the machine; it does not appear on an idle machine. The `--missing` semantics make reruns rebuild only missing or
invalid worlds.

## Start the shared show: `make run`

```sh
make run
```

```text
DOC-01  OK    CPU 架构：x86_64
...
awr doctor --quick: 通过（0.2 s）
~/ANet-Drone4D/.venv/bin/python -m awr.sim.runtime.warm
numba warmup s plugins=2.04 m08=0.34 m10_plan=0.06 m20_cover=0.01 m04_geom=0.06
m10 numba warmup s 1.77
[worldpkg] chicago: 原始数据缺失（~/ANet-Drone4D/data/raw/urbanscene3d/Chicago_sampled_5m.ply）；修复：make fetch-data
...
[worldpkg] synthcity: up to date
FAIL chicago       exit=4 0.0 s chicago: 原始数据缺失（~/ANet-Drone4D/data/raw/urbanscene3d/Chicago_sampled_5m.ply）；修复：make fetch-data
...
OK   synthcity     exit=0 cv=9e05c2364f58 0.0 s
提示：原始数据缺失，运行 make fetch-data（无数据试用：make demo-world 生成合成演示城市 synthcity，ADR-077）
提示：有城市缺少原始数据，运行 make fetch-data
提示：默认世界为合成演示城市 synthcity（/world/synthcity，剧本 s0-synthcity-showcase）
警告：部分世界构建失败（退出码 4），其余照常；详情见 worlds/.status/
[synthcity] geo warm hit 0.02 s derive_sha8=d98119e8 empty=0.01633 pits_filled=1.0 mem_mib=6.5 grid25=[301, 301]
cd ~/ANet-Drone4D && AWR_PROFILE=${AWR_PROFILE:-demo} exec ~/ANet-Drone4D/.venv/bin/python -m awr.runtime.supervisor
提示：默认世界 shenzhen 未构建，回退到合成演示城市 synthcity（剧本 s0-synthcity-showcase；ADR-077）；有 UrbanScene3D 数据时运行 make fetch-data && make worlds
READY run=r20261013-143216-2988 profile=demo world=synthcity access=loopback
  api      http://127.0.0.1:8000/world/synthcity
  bus      tcp/127.0.0.1:7447（namespace awr/synthcity/r20261013-143216-2988）
  logs     ~/ANet-Drone4D/runs/r20261013-143216-2988/logs
  ssh      ssh -N -L 8000:127.0.0.1:8000 <user>@<host>
  admin    ~/ANet-Drone4D/runs/r20261013-143216-2988/admin.token
```

`make run` stays in the foreground; stop it with Ctrl+C or with `make stop` in another terminal. Before starting it:

1. **Preflight.** Runs the quick diagnosis again; an `ERROR` such as a busy port stops here with exit code 3 (see
   {ref}`en-qs-troubleshooting`).
2. **numba warm-up.** Compiles the flight, planning, coverage and geometry kernels into `~/.cache/awr/numba`. The output above is the second
   start (cache hit, about 4 s in total); the first start on a machine compiles everything, in this run `plugins=103.73 m08=57.76`
   (seconds) on a busy 8-core machine.
3. **World build.** Runs a `--missing` build for all seven worlds; the six cities without raw data are marked failed (exit code 4,
   "原始数据缺失" means raw data missing) without blocking, as long as the default world is available. `geo warm` produces derived caches
   such as the 25 m planning grid. The hints ("提示") state that the default world falls back to synthcity with scenario
   `s0-synthcity-showcase`.
4. **Production build.** When `apps/web/dist` is missing or older than the sources, runs `npm run build -w apps/web` (vite took 5.8 s in
   the first start of this run), then `prod-bundle-scan` checks that no deployment secret ended up in the bundle. The second start above
   skipped this step.
5. **Supervisor.** Starts sim-core, api, recorder, agent-runtime and job-worker for profile `demo` and prints `READY` once all are ready.

The five lines of the `READY` block are: the browser entry point, the inter-process bus and its namespace, the log directory of this run,
the SSH forwarding command for remote access, and the admin token file (used by the operator interface and the protected endpoints of
`/api`; mode 0600, do not share it).

The supervisor's structured log (one JSON object per line, for example `proc state RUNNING`) is interleaved with this output and omitted
here. When sim-core lacks `CAP_SYS_NICE` it prints `nice not permitted, using 0`, which does not affect operation.

## Confirm the service is ready: `make status`

In another terminal:

```sh
make status
```

```text
~/ANet-Drone4D/.venv/bin/awr status
run r20261013-143216-2988  supervisor pid 2305695
ring  head=3231  t_sim_s=25.84  state=PLAYING  rate=1.0  epoch=1  segment=0  writer_pid=2305735  writer_age_ms=3.9  step_seq=6460  step_p99_us=3199  rtf=0.997  roster_version=9
NAME           STATE    PID      RESTARTS  UPTIME_S  HB_AGE_MS  CPU%  RSS_MB
sim-core       RUNNING  2305735  0         31.4      7.2        17.9  245.1
api            RUNNING  2305831  0         28.5      10.6       3.0   93.2
recorder       RUNNING  2305833  0         28.5      10.3       7.0   57.9
replay-worker  STOPPED  -        0         -         -          -     -
agent-runtime  RUNNING  2305839  0         28.5      79.4       4.0   54.8
job-worker     RUNNING  2305841  0         28.4      323.5      -     54.9
```

The `ring` line is read from the header of the shared-memory state ring: `t_sim_s` is simulation time, `state=PLAYING` means the clock is
running, `rtf` is the real-time factor (1.0 runs at wall-clock speed), and `step_p99_us` is the p99 step duration over the recent window.
`RESTARTS` should be 0 in the process table; `replay-worker` starts on demand and is normally `STOPPED`.

The readiness probe and the health endpoint need no token:

```sh
curl -s http://127.0.0.1:8000/api/health/ready
curl -s http://127.0.0.1:8000/api/health
```

```text
{"status":"ready","sim":"ok","ring_attached":true,"world_loaded":true,"run_id":"r20261013-143216-2988"}
{"status":"ok","build":{"version":"0.2.0","channel":"local","release":"local-06731a8f76"}}
```

`06731a8f76` in `release` is the short commit hash at build time and matches the version line on the browser's startup screen.

## Open it in a browser

Open **http://localhost:8000/world/synthcity**. Without UrbanScene3D data, the synthetic demo city is the default world and runs scenario
S0: seven P600s scan the 318 m ANet Tower in two spiral sections, two cover city blocks, three cruise along the river in a V formation,
and the weather turns from clear to light rain and then fog (about 5 min of simulation time).

When the server is a remote machine, run the SSH forwarding command from the `READY` block on your own computer and open the same address.
How to use the interface is described in {doc}`../guide/showcase`.

## Stop: `make stop`

```sh
make stop
```

```text
~/ANet-Drone4D/.venv/bin/awr stop
已停止 r20261013-143216-2988
```

`awr stop` ("已停止" means stopped) stops all processes gracefully through the supervisor's control channel (5 s grace period per process)
and removes the run directory under `/dev/shm/awr/<run>`; logs and recordings in `runs/<run>/` are kept. The foreground `make run` exits.

## Start the sandbox service: `AWR_PROFILE=sandbox make run`

```sh
AWR_PROFILE=sandbox make run
```

```text
...
cd ~/ANet-Drone4D && AWR_PROFILE=${AWR_PROFILE:-demo} exec ~/ANet-Drone4D/.venv/bin/python -m awr.runtime.supervisor
提示：默认世界 shenzhen 未构建，回退到合成演示城市 synthcity（剧本 s0-synthcity-showcase；ADR-077）；有 UrbanScene3D 数据时运行 make fetch-data && make worlds
READY run=r20261013-144754-296c profile=sandbox world=synthcity access=loopback
  api      http://127.0.0.1:8000/world/synthcity
  bus      tcp/127.0.0.1:7447（namespace awr/synthcity/r20261013-144754-296c）
  logs     ~/ANet-Drone4D/runs/r20261013-144754-296c/logs
  ssh      ssh -N -L 8000:127.0.0.1:8000 <user>@<host>
  admin    ~/ANet-Drone4D/runs/r20261013-144754-296c/admin.token
```

Profile `sandbox` starts the sandbox control plane next to the shared show; the process table of `make status` gains three rows:

```text
sandbox-pool   RUNNING  2320617  0         17.3      67.2       6.0   140.7
sandbox-api    RUNNING  2320754  0         15.6      18.4       6.9   92.9
vite           RUNNING  2320756  0         15.6      -          -     73.3
```

- **sandbox-pool** handles admission, queueing, parking and reclamation of sessions, and starts a separate sim-core for each session
  (sandbox clock profile sandbox100).
- **sandbox-api** serves the sandbox REST v1 and realtime connections at `http://127.0.0.1:8002/api/sandbox/v1`, the SDK's default address.
- **vite** is the development server, started with the dev process group of this profile.

The local quota profile is `local`: no address-prefix quota, no proof of work, and the batch endpoints are open.

## Query the sandbox interface

```sh
curl -s http://127.0.0.1:8002/api/sandbox/v1/meta
```

```text
{"access_mode":"loopback","active_sessions":0,"api_version":1,"build":{"channel":"local","release":"local-06731a8f76","version":"0.2.0"},"contracts":"1.2.0","features":["queue","batch"],"max_sessions":1,"pow":{"bits":20,"required_now":false},"queue_len":0,"sdk":{"min_version":"1.0.0"}}
```

`max_sessions` is the number of sandbox sessions this machine can run at the same time, computed by the capacity model from the CPU budget
(hard limit 6); here it is 1, so a second session would queue. `pow.required_now=false` means no proof of work is required right now.
All endpoints are listed in {doc}`../api/rest`.

## Drive a session with the Python SDK

The SDK is the single file `tools/sdk/awr_sandbox.py` and depends on httpx, websockets and msgpack, all present in `.venv`. Create
`sdk_quickstart.py` at the repository root:

```python
"""Quick start: create a sandbox session, add a drone, read its state, end the session."""
import sys
import time

sys.path.insert(0, "tools/sdk")  # single-file SDK shipped in the repository

from awr_sandbox import Sandbox

sb = Sandbox("http://127.0.0.1:8002", timeout_s=30)
meta = sb.meta()
print("server:", meta["build"]["version"], "api", meta["api_version"], "max_sessions", meta["max_sessions"])
print("worlds:", [w["world_id"] for w in sb.worlds() if w["available"]])

with sb.create("synthcity", "t5_area_guard") as s:
    info = s.state()
    print("session:", s.sid, info["state"], "expires_in_s", info["expires_in_s"])
    print("template:", info["template_id"], "vehicles", len(s.vehicles.list()["items"]),
          "targets", len(s.targets.list()["items"]), "nests", len(s.nests.list()["items"]))

    v = s.vehicles.add("p600_mid360", nest_id="nest-b", label="docs-1")
    print("added:", v)

    for _ in range(20):  # the new airframe boots within a few seconds
        snap = s.vehicles.state(v["id"])
        st, ext = snap["state"], snap.get("state_ext") or {}
        if ext.get("battery"):
            break
        time.sleep(0.5)
    print("state:", st["lifecycle"], st["flight_state"], "pos_enu_m", st["pos_enu_m"],
          "soc_pct", ext["battery"]["soc_pct"])

print("closed:", s.sid)
```

```sh
.venv/bin/python sdk_quickstart.py
```

```text
server: 0.2.0 api 1 max_sessions 1
worlds: ['synthcity']
session: sb-da5ygnnr3z READY expires_in_s 1795
template: t5_area_guard vehicles 7 targets 2 nests 2
added: {'id': 'p600-07', 'agent_no': 7, 'lifecycle': 'STARTING', 'model_id': 'p600_mid360', 'nest_id': 'nest-b'}
state: BOOTED DISARMED pos_enu_m [-120.9, -339.7, 0.0] soc_pct 100.0
closed: sb-da5ygnnr3z
```

The whole run took 6 s. Line by line:

1. **`Sandbox(...)`** reads `GET /meta` on connect and checks the major versions of the API and the contracts, failing immediately when
   they are incompatible. `timeout_s=30` sets the per-request timeout to 30 s: when the server is full, creation queues and the SDK waits
   for a slot with long polls of up to 25 s each, so the timeout must exceed that (see {ref}`en-qs-troubleshooting`).
2. **`worlds()`** returns the seven worlds and their availability. Only the synthetic demo city is built on this machine, so only
   `synthcity` is available.
3. **`create("synthcity", "t5_area_guard")`** creates a session and waits until it is ready, solving the proof of work when required and
   queueing and claiming a slot when the server is full. T5 area guard and relay is the default scenario template: 2 nests (a nest box and
   a VTOL pad), 7 drones (P600 and AWR-V1) and 2 targets. The initial session lifetime is 30 min (`expires_in_s` about 1,800).
4. **`vehicles.add("p600_mid360", nest_id="nest-b")`** adds a P600 in the MID-360 configuration to the VTOL pad `nest-b`. Exactly one of
   `nest_id` and `home_enu_m` (World ENU coordinates) must be given. The returned `lifecycle` is `STARTING`; the airframe finishes booting
   within a few seconds.
5. **`vehicles.state(vid)`** returns the full state: `BOOTED DISARMED` means booted, not armed and parked in the nest; `pos_enu_m` is the
   World ENU position in meters, and `state_ext.battery.soc_pct` is the state of charge in percent.
6. **Leaving the `with` block** makes the SDK export a configuration snapshot once (still retrievable for 10 min after the end), then end
   the session with `DELETE /sessions/{sid}` and release the slot.

The browser interface of a local sandbox needs the `/api/sandbox/` path routed to port 8002 (done by nginx on the public instance, see
{doc}`../topics/deployment`); the steps on this page use only the open interface.

## Stop the sandbox service

```sh
make stop
```

```text
~/ANet-Drone4D/.venv/bin/awr stop
已停止 r20261013-144754-296c
```

When sandbox-pool stops, it first stops accepting new sessions, then exports a configuration snapshot of every running session and ends it
as `maintenance`, all within a 12 s grace period.

---

(en-qs-troubleshooting)=
## Troubleshooting

### Startup fails with a busy port

```text
错误（退出码 3）：DOC-09 端口空闲
修复：ss -ltnp | grep -E ':(7447)'；或设置 AWR_PORT_OFFSET=<k>
...
awr doctor --quick: 失败（0.2 s）
make[1]: *** [Makefile:83: _need-supervisor] Error 3
make: *** [Makefile:68: run] Error 2
```

The preflight found a port in use ("错误（退出码 3）" is error, exit code 3; "修复" is the fix). In this run it happened when `make run` was
started immediately after `make stop`: the bus port of the previous run had not been released yet, and a retry a few seconds later
passed. When another instance really holds the port, inspect it as suggested, or shift all ports of this instance by `10·k` with
`AWR_PORT_OFFSET=<k>`.

### sim-core is repeatedly reported as hung

The supervisor log shows, again and again:

```text
{"lvl":"WARN","proc":"supervisor","logger":"awr.runtime.supervisor","msg":"proc hung","kv":{"name":"sim-core","heartbeat_age_ms":2027}}
{"lvl":"INFO","proc":"supervisor","logger":"awr.runtime.supervisor","msg":"standby promoted","kv":{"name":"sim-core","pid":2302046,"restarts":1}}
```

(Timestamps and other fields removed for readability.) The sim-core heartbeat was older than 2 s, so the supervisor declared it hung and
promoted the hot standby; the sixth restart within the window trips the circuit breaker. In this run the cause was another instance on the
same machine that had also pinned its sim-core to core 1, so two 250 Hz main loops competed for one core. Pinning is on by default on
machines with 8 or more cores; turn it off on a machine shared with other workloads:

```sh
AWR_CPU_PIN=off make run
```

You can also write `AWR_CPU_PIN=off` into `.env.local` at the repository root so that it applies to every later `make`.

### Adding a drone returns 110 PARAM_OUT_OF_RANGE

```text
awr_sandbox.AwrError: 110 PARAM_OUT_OF_RANGE: 参数越界或超过上限
```

`vehicles.add()` was called with neither `nest_id` nor `home_enu_m` (or with both). The error `detail` is
`{"field": "nest_id|home_enu_m", "why": "EXACTLY_ONE"}`. Give exactly one of them.

### Adding a drone returns 525 NEST_INCOMPATIBLE

```text
awr_sandbox.AwrError: 525 NEST_INCOMPATIBLE: 机巢类型、充电器节数或槽位不兼容
```

The `detail` is `{"why": "slots", "slots": 6}`: the nest box `nest-a` of template T5 already holds 6 drones. Use `nest-b`, or check the
slots and inventory of each nest with `s.nests.list()` first. A nest box accepts multirotors only; a VTOL pad accepts VTOL airframes and
multirotors.

### Creating a session returns 409 SANDBOX_EXISTS

```text
awr_sandbox.AwrError: 409 SANDBOX_EXISTS: an active session already exists for this principal and address prefix; attach() with its token or create(force_new=True)
```

The same principal and address prefix already have a session that has not ended (for example, a previous script exited with an error
before leaving the `with` block, and the session is still running or parked). If you hold its token, reattach with
`sb.attach(sid, token)`; otherwise create a new one with `create(..., force_new=True)`, or wait until the old session is parked for being
idle without a connection and expires. The SDK keeps its principal hint in `~/.config/awr_sandbox/principal`; it is used only for
idempotency and quota aggregation and is not a credential.

### httpx.ReadTimeout while queueing

```text
httpx.ReadTimeout: timed out
```

The server is full and creation is queued; SDK 1.1.0 and earlier wait with long polls of up to 25 s each but keep the default
per-request timeout of `Sandbox()`, 10 s. Upgrade to SDK 1.1.1 (the long polls use their own read timeout), or pass `timeout_s=30` when
creating `Sandbox` (as in the example on this page).

### Messages about the six cities failing to build

The six lines such as `FAIL chicago exit=4` and the warning that some worlds failed to build (exit code 4) printed by `make run` are
expected without UrbanScene3D data and do not affect the synthetic demo city. For the six cities run `make fetch-data && make worlds`; see
{doc}`install`.

More questions are answered in {doc}`../project/faq`.

## What you have after this step, and what you do not

**You have:** a local installation with locked dependencies; a freely shareable synthetic world `worlds/synthcity`; a World Runtime that
can be watched in a browser and inspected with `make status`; a local sandbox control plane; and a program that runs the full life cycle
of a session through the open interface. The logs and effective configuration of every run are under `runs/<run>/`:

```text
~/ANet-Drone4D/runs/r20261013-144754-296c/
├── logs/                    # one log per process: sim-core.log, api.log, sandbox-pool.log and others
├── effective-config.yaml    # effective configuration (secrets masked)
├── admin.token              # admin token (0600)
├── rec-000.mcap             # recording of the shared show (MCAP), replayable locally
├── inputs.msgpack           # input log for deterministic re-simulation
├── audit.jsonl              # audit records
└── ckpt/                    # checkpoints for crash recovery
```

**You do not have yet:** the six UrbanScene3D cities (they require downloading the data and accepting its terms); the browser interface of
the local sandbox (it requires the path routing of the deployment kit); and capabilities of later versions such as GPU reconstruction and
a real flight stack. Next you can:

- Read {doc}`../guide/sandbox` for scenario templates, management panels and manual control.
- Read {doc}`../api/sdk` and use the examples in `tools/sdk/examples/` for manual flight, sensors, target capture and GSD scan previews.
- Read {doc}`../topics/architecture` for the process topology and the coordinate conventions.
