# Python SDK

`awr_sandbox` is the single-file Python SDK of the sandbox interface (version 1.1.0, interface `/api/sandbox/v1`). It does not depend on this
repository's `awr` package and can be copied and used on its own; it handles queueing, proof of work, renewal, resume after power saving and
decoding of realtime binary frames.

## Installation

Python 3.9 to 3.13 are supported; the dependencies are httpx, websockets and msgpack:

```sh
pip install "httpx>=0.24" "websockets>=12" "msgpack>=1.0"
curl -O https://drone4d.agentnetwork.org.cn/sdk/awr_sandbox.py
```

Inside the repository the file is `tools/sdk/awr_sandbox.py`, and the `.venv` created by `make setup` already contains the dependencies (see
{doc}`../intro/quickstart`).

## Minimal example

```python
from awr_sandbox import Sandbox

sb = Sandbox("https://drone4d.agentnetwork.org.cn", timeout_s=30)
with sb.create("synthcity", "t5_area_guard") as s:   # queues when full; solves the proof of work when required
    print(s.state()["state"])
    s.clock("speed", rate=5)                          # returns the granted rate when limited
    for msg in s.stream(["sandbox/session"], seconds=3):
        print(msg.topic, msg.data)
```

The session ends when the `with` block is left; before that the SDK exports a configuration snapshot and keeps it on the `Session` object.

## Object model

| Object | Obtained from | Role |
|---|---|---|
| `Sandbox(base_url, *, timeout_s=10.0, auto_keepalive=True, verify_tls=True, principal_hint=None)` | Constructor | Connects to one deployment; reads `GET /meta` and checks the API and contract major versions |
| `Session` | `Sandbox.create()`, `Sandbox.attach(sid, token)` | One sandbox session; a context manager |
| `Session.vehicles`, `targets`, `nests`, `tasks`, `models` | Attributes | Resource collections with `list`, `get`, `create`, `update` and `remove` |
| `Session.env`, `Session.tasking` | Attributes | Environment and external allocator |
| `Lease` | `Session.acquire(vid)` | A drone lease; a context manager that releases on exit |
| `Msg(topic, t_sim_ns, data)` | Items of `Session.stream()` | One realtime message |

## Call reference

| Capability | Call | Endpoints |
|---|---|---|
| Worlds, templates, overview, public capacity | `Sandbox.worlds()`, `templates(world)`, `meta()`, `capacity()` | SB-00 to SB-02, SB-71 |
| Create (proof of work, queue long polling and claim) | `Sandbox.create(world, template, *, snapshot=None, calendar=None, seed=None, queue=True, force_new=False, on_queue=None)` | SB-03 to SB-06, SB-08 |
| Reattach an existing session | `Sandbox.attach(sid, token)` | SB-08 |
| State and waiting for readiness | `Session.state()`, `wait_ready()` | SB-08 |
| Renew, end | `Session.keepalive()`, `close()` | SB-10, SB-09 |
| Snapshot, restore, template, world switch | `Session.snapshot()`, `restore(snapshot)`, `load_template(t)`, `switch_world(world, template=None)` | SB-11 to SB-14 |
| Clock | `Session.clock(op, rate=None, ticks=None)` | SB-15 |
| Fetch missed events | `Session.events(since_seq=0, limit=1000, types=None)` | SB-16 |
| Fleet | `vehicles.add(model_id, **kw)`, `state(vid)`, `sensors(vid)`, `perception(vid)`, `radar_tracks(vid)`, `acoustic_bearings(vid)` | SB-28 to SB-38 |
| Targets | `targets.create(body)`, `state(tid)`, `suggested_radius(tid, model_id)` | SB-40 to SB-46 |
| Nests | `nests.list()`, `create(body)`, `update(id, patch)`, `remove(id)` | SB-47 to SB-51 |
| Tasks | `tasks.create(body, dry_run=False)`, `start`, `pause`, `resume`, `cancel`, `sustainability(tid)`, `relay_stats(tid)` | SB-52 to SB-56, SB-59 |
| Environment | `env.state()`, `preset(name, duration_s)`, `calendar(start_utc, tz)`, `query(points, fields)` | SB-60 to SB-63 |
| External allocator | `tasking.snapshot()`, `tasking.submit(plan)` | SB-57, SB-58 |
| Airframe catalog | `Sandbox.catalog.models()`, `model(model_id)`, `sensors()` | SB-21 to SB-23 |
| Commands and leases | `Session.command(vid, name, **args)`, `with Session.acquire(vid):` | SB-35 to SB-37 |
| Velocity control | `Session.velocity(vid, vx, vy, vz, yaw_rate=0.0)`, `velocity_stop(vid)` (up to 50 Hz) | WebSocket `call` and client data channel |
| Realtime stream | `for m in Session.stream([topic, ...], seconds=None):` | SB-64 |
| Power-save parking and resume | `Session.park(reason)`, `resume(wait=True)` | SB-69, SB-70 |
| Internal-tier session | `Sandbox.create(..., identity_token=...)` (token issued by the deployment's identity entry) | SB-04 |

Endpoint numbers map to paths in {doc}`rest`. `Session.command()` uses the WebSocket `call` when a realtime connection is open and
`POST .../commands` otherwise, retrying idempotently with the same call ID.

## Error handling

All errors are `AwrError(code, name, message, detail, remedy, status, retry_after_s)`, and `str(e)` reads `"<code> <NAME>: <message>"`.
When the session has ended, the subclass `SandboxGone` is raised, with the last exported configuration snapshot in its `snapshot` attribute.

```python
from awr_sandbox import AwrError, Sandbox, SandboxGone

try:
    s.vehicles.add("p600_mid360", nest_id="nest-a")
except AwrError as e:
    print(e.code, e.name, e.detail, e.remedy)   # for example 525 NEST_INCOMPATIBLE {'why': 'slots', 'slots': 6}
```

- On rate limiting (429, reason code 111) and temporary unavailability the SDK retries automatically after `retry_after_s` (from
  `retry_after_ms` or `Retry-After`), with jitter scaled to the wait.
- A REST-only session that stays idle for long is parked for having no connection; when the next call that needs sim-core receives 409 with
  reason code 515, the SDK resumes the session once automatically.

## Queueing, renewal and power saving

- **Queueing.** When `create()` receives 202 it queues and waits with long polls of up to 25 s each; the callback `on_queue(kind, view)`
  receives the position and estimated wait. Because each long poll may take 25 s, set `timeout_s` above 25 s (for example 30).
  With `queue=False`, creation fails immediately when full.
- **Renewal.** With `auto_keepalive=True` a background thread renews shortly before the lifetime ends and swaps in the reissued token.
  Renewal does **not** issue calls to keep the session running and does not wake a parked session.
- **Power saving.** A session without a realtime connection and without any session call for 120 s (60 s when the sandbox queue is not
  empty; see `idle_ttl_s` in `GET /sessions/{sid}`) is parked. To keep the simulation advancing during long inactive periods, keep one
  `stream()` connection open, or call `Session.state()` at an interval shorter than `idle_ttl_s`. The next session request after parking
  resumes automatically (and may queue when full).
- **Principal hint.** `principal_hint` is stored in `~/.config/awr_sandbox/principal` (0600); it is used only for idempotency and quota
  aggregation and is not a credential.

## Manual control example

```python
with sb.create("synthcity", "t6_manual") as s:
    v = s.vehicles.add("p600_mid360", home_enu_m=[0.0, 0.0, None], label="manual")
    vid = v["id"]
    s.command(vid, "arm")
    s.command(vid, "takeoff", alt_m=30.0)
    with s.acquire(vid, priority="normal"):
        t_end = time.monotonic() + 5.0
        while time.monotonic() < t_end:
            s.velocity(vid, 3.0, 0.0, 0.0)   # 3 m/s east (World ENU)
            time.sleep(0.04)                  # 25 Hz
        s.velocity_stop(vid)
    s.command(vid, "land")
```

Velocity control is protected by the server-side soft fence: the velocity component towards a target's sensitivity range is clamped to 0 and
the warning `KEEPOUT_CLAMPED` is returned (see {doc}`../guide/sandbox`).

## Example programs

The repository's `tools/sdk/examples/` contains the following examples, all using only the public interface; the address is the first
command-line argument or the environment variable `AWR_SANDBOX_URL`, defaulting to `http://127.0.0.1:8002`:

| Example | Content |
|---|---|
| `01_quickstart.py` | Create a sandbox, read the session state, request a rate, subscribe to the session state for 5 s, export a snapshot, end |
| `02_manual_control.py` | Add a P600, take off, acquire a lease and fly east for 5 s with velocity commands, stop, release, land |
| `03_sensors_status.py` | Discover every data product per airframe and sensor, and fetch one sample over REST and one over WebSocket |
| `04_targets_capture.py` | Place a person, send a drone to watch it, and wait for the `perception.capture` event |
| `05_gsd_scan_dryrun.py` | Given an area and the required GSD, print altitude, strips, drone count and the completion time curve |
| `06_external_allocator.py` | Read the allocation snapshot, assign work items greedily to the nearest idle drone, submit an external plan |
| `scenario2_e2e.py` | End-to-end acceptance script for area guard and relay |

```sh
python tools/sdk/examples/01_quickstart.py https://drone4d.agentnetwork.org.cn
```
