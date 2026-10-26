# Frequently asked questions

## Naming

**What is the relation between ANet4D and ANet Drone4D?**
They are the same project. From 0.3.2 the product is called ANet4D, simulation infrastructure for collaborative embodied agents; the
former name ANet Drone4D is retired. The public site moved from `drone4d.agentnetwork.org.cn` to
[anet4d.agentnetwork.org.cn](https://anet4d.agentnetwork.org.cn) and this documentation from `docs.agentnetwork.org.cn/drone4d/` to
`docs.agentnetwork.org.cn/anet4d/`; the former addresses redirect permanently. The GitHub repository is {gh}`github.com/ANetResearch/ANet4D </>`; GitHub redirects the former URL
(`ANetResearch/ANet-Drone4D`) to it.

**Why do some identifiers still contain `drone4d`?**
Identifiers that are part of a protocol, a data contract or recorded data keep their former spelling, so that existing recordings, event
tapes, submissions and installations stay valid and replays stay byte-identical. These legacy identifiers include the event specification
`drone4d.event.v1`, the agent wire protocol `drone4d.bench.wire.v1`, schema identifiers inside the contracts package, channel names in
recorded MCAP files, the native Gazebo plugin names and the server installation paths and unit names (for example the systemd unit
`anet-drone4d`). They are identifiers, not product names.

**I use the `drone4d` Python package. What changes?**
The SDK package is now `anet4d` (wheel `anet4d-<version>-py3-none-any.whl`, `import anet4d`, `python -m anet4d.serve`). For one release `drone4d` remains a
deprecated compatibility alias that re-exports `anet4d` and emits a `DeprecationWarning`; replace `drone4d` with `anet4d` in imports and
commands. See {doc}`../guide/sdk-quickstart`.

## Runtime environment

**Does the server need a GPU?**
Not for the Interactive Sandbox: physics, safety, missions, perception and environment are computed on the CPU, and the point cloud, weather
and interface are rendered in the visitor's browser. The public instance runs on a 4-core server without a GPU. The Event Lab needs an
NVIDIA GPU on the server for sensor generation (mesh cameras, thermal imaging and LiDAR); the public instance shows pre-rendered Event Lab
samples only.

**Which operating systems are supported?**
The server supports x86-64 Linux only (verified on Ubuntu 22.04 and 24.04): the shared-memory state ring relies on the x86 memory order.
Other systems can run it in a virtual machine or on a remote Linux server and open it in a local browser through SSH port forwarding. The
browser side works on any operating system with WebGL2.

**Can one machine run several instances?**
Yes. Give each instance a different `AWR_PORT_OFFSET=k` (0 to 31) so that all its ports shift by `10·k`; on a machine shared with other
workloads also set `AWR_CPU_PIN=off`. Both can go into `.env.local` at the root of each repository. See {doc}`../intro/install`.

**Why is the first `make run` slow?**
The first start compiles the flight, planning, coverage and geometry kernels into the numba cache and runs a frontend production build; later
starts use the cache and are usually ready within 30 s.

## Worlds and data

**Why does `make run` report that six cities failed to build?**
That is expected without the UrbanScene3D raw data (exit code 4) and does not affect the synthetic demo city. For the six cities run
`make fetch-data && make worlds`, after reading the dataset terms.

**Can the six cities be used commercially or redistributed?**
No. The authors of UrbanScene3D allow non-commercial use only and prohibit redistribution of the raw data or any modified version. The
authorization this project holds for its own deployments is not passed on to users. The synthetic demo city contains no third-party data and
can be shared freely. See {doc}`../topics/worlds`.

**Can I import my own point cloud?**
Point-cloud ingest targets the built-in dataset and the synthetic city; import of arbitrary PLY or LAS point clouds is part of the V0.2
theme of the original design baseline. Since V0.3, photogrammetric GLB meshes can be imported as mesh worlds with metric scale, true north
and leveling calibration. See {doc}`roadmap`.

## Shared show and sandbox

**Why is there a queue on the public instance?**
The number of concurrent sandboxes is computed by the capacity model from the CPU budget, and the public instance currently allows 2; when
full, visitors queue and see their position and the estimated wait. You can keep watching the shared show while queued.

**After switching the world, where did my targets and tasks go?**
Switching the world restarts the simulation in the new world and loads that world's template of the same name; targets, nests, tasks and
view settings of the previous world are not carried over, because coordinates and geometry differ between worlds. To keep a configuration,
export a configuration snapshot first; it can be restored later in the original world. In 0.2.0 this isolation did not hold on every
switching path; the defect is fixed since 0.2.1, see {doc}`../guide/sandbox`.

**The image is sparse or the frame rate is low. What can I do?**
The quality controller lowers the point budget from the measured frame time to hold the frame rate. Without a hardware GPU, or with an
integrated GPU, the image is sparser; closing other GPU-heavy pages, using a desktop browser or shrinking the window all raise the frame
rate.

**Can I use the browser interface of a local sandbox?**
`AWR_PROFILE=sandbox make run` provides the full sandbox interface locally, which the SDK can drive. The browser interface needs the
`/api/sandbox/` path routed to sandbox-api (port 8002), as in the nginx configuration of the public instance; see {doc}`../topics/deployment`.

**Why was my session parked automatically?**
After a period without interaction (10 min for a sandbox, 5 min when the queue is not empty), or when a REST-only client stays idle for long,
the session enters power-save parking: it is saved to disk and its slot released, with simulation time and lifetime frozen. Calls to the
session API from external clients such as the SDK or scripts count as activity: while they continue, an open page does not enter power saving
and does not park the session, even when the page is hidden. Any interaction or session request resumes a parked session, and parked sessions
are kept for 60 min.

**How do simulation time and time zones work?**
The sandbox and the shared show have their own simulation calendars (start time and time zone), which determine the sun position,
illuminance and day and night; the shared show of the public instance follows real time in the Asia/Shanghai time zone. Times in the
interface are always simulation nanoseconds `t_sim_ns`, and calendars are given as a UTC instant plus a time zone.

## Interface and SDK

**The SDK raises `httpx.ReadTimeout` while queueing.**
In SDK 1.1.0 and earlier, the queue long polls (up to 25 s each) used the default per-request timeout of `Sandbox()`, 10 s. From 1.1.1
the long polls use their own read timeout (the wait plus 10 s), so the defaults queue correctly; with an older SDK pass `timeout_s=30`
when creating it. See the troubleshooting section of {doc}`../intro/quickstart`.

**Why does creating a session return 409 `SANDBOX_EXISTS`?**
The same principal and address prefix already have a session that has not ended. Reattach with `attach()` if you hold its token, or create a
new one with `force_new=True`.

**Can I plug in my own agent or allocation algorithm?**
Yes. An external program can read the allocation snapshot and submit allocation plans (`tasking.snapshot()` and `tasking.submit()`), or
drive drones directly through velocity control and commands. All commands pass the same admission and safety checks as the interface. The
real ANet network is planned for V1.0.

**Can PX4 or a real drone be connected?**
The Interactive Sandbox uses the built-in FleetSim (a PX4-style control cascade checked against PX4 SIH flight logs). The Event Lab adds the
Gazebo Sim lockstep tier as the high-fidelity physics tier (V0.3). A PX4 SITL tier and a real P600 through Prometheus are not part of the
released versions; see {doc}`roadmap`.

## Feedback

**How do I report a problem?**
Open an issue on {gh}`GitHub Issues </issues>` with the version line from the startup screen or
`GET /api/health`, your browser and device, the steps to reproduce and the `request_id` of any error response. Report security issues to
hi@anet0.com.
