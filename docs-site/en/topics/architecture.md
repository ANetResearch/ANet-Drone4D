# Architecture and coordinate frames

## Overall structure

The server "computes the world": physics, safety, missions, perception and environment run on a Linux host without a GPU. The browser "views
the world": it streams the point cloud and draws drones, trails, zones, frusta and weather. All processes are supervised (heartbeats, backoff
restarts and circuit breakers), exchange commands, events and queries over the zenoh bus, and exchange high-rate state through a
shared-memory state ring.

```text
World Package (octree, DSM and DTM, zones and semantics)
  │
  ↓
Server (no GPU)
  sim-core: 250 Hz clock; FleetSim, Safety, Mission, Environment, Sensors
    ├──→ StateRing (shared memory, 125 Hz) ──→ recorder, replay-worker (MCAP)
    └──→ zenoh bus (commands, events, queries) ←──→ agent-runtime
  api / Gateway: reads StateRing (60 Hz), serves REST, static files and awr.rt.v1, sends commands over the bus
  sandbox-pool ──→ one sim-core per session (sandbox100)
  sandbox-api: one Gateway per session, same interface and protocol
  │
  ├── HTTP Range: point cloud and world files
  └── WebSocket: state streams and commands
  ↓
Browser
  PointCloudEngine (APH selector, CAS controller) ──→ viewport and render backend (WebGL2)
  rt.worker (awr.rt.v1 client) ──→ viewport and UI shell (React 19, shadcn)
```

| Process | Role |
|---|---|
| sim-core | Simulation kernel: the FleetSim fleet, flight state machine and safety, mission execution, environment field, sensors and perception; runs registered pipeline stages every tick and publishes state to the StateRing |
| api (show-api) | Gateway: REST, frontend and world static files, `awr.rt.v1` realtime connections; sends state according to each client's interest set and credit |
| recorder, replay-worker | MCAP recording and replay (replayed frames are byte-identical to the recording) |
| agent-runtime | Agent runtime (an in-process Mock ANet in V0.1), cooperating with sim-core over the bus |
| job-worker | Background jobs such as world builds and mock reconstruction |
| sandbox-pool | Admission, queueing, parking and reclamation of sandbox sessions; governs the rate of each session; runs the shared planning service |
| sandbox-api | Instantiates a Gateway per session with the same interface and realtime protocol |

## Data flow of one interaction

1. **Open.** Visiting `/world/<id>` loads the world manifest and octree hierarchy; one HTTP Range request per octree root fetches the
   first-screen levels, and a Web Worker unpacks the ANET_Q16 points into the GPU point pool.
2. **Stream.** Every frame, the APH selector picks octree nodes within the point budget; the CAS controller adjusts the budget from the
   measured frame time and switches among 7 quality tiers.
3. **Simulate.** sim-core advances in 4 ms ticks, stepping all drones with one numba kernel (L1 control every other tick), runs safety,
   missions, environment and sensors as registered stages, and publishes state to the StateRing.
4. **Serve.** The Gateway reads the state ring at 60 Hz and sends binary data per client interest set and credit: the whole fleet as 32-byte
   Lite32 records and the focused drone as 64-byte Full64 records.
5. **Draw.** The browser interpolates at a single render time and draws drones, trails, zones, frusta and weather over the point cloud.
6. **Operate.** Clicking the ground runs a ray-hit query on the DSM; a go-to command passes admission (lease, parameters, capability,
   fence) and returns accepted, running and succeeded in turn.

## Clocks

| Quantity | Shared show (d1_250) | Sandbox (sandbox100) |
|---|---|---|
| Main clock | 250 Hz (4 ms) | 100 Hz (10 ms) |
| Multirotor control and physics | 125 Hz | 100 Hz |
| Fixed-wing and VTOL kinematics | — | 50 Hz |
| Environment and fast guard | 50 Hz | 50 Hz |
| State ring publication | 125 Hz | 50 Hz |
| Target motion and sensitivity checks | — | 10 Hz |
| Perception | — | 5 Hz |
| Tasking and relay | — | 1 Hz (heavy computation goes to the shared planning service) |

The timeline runs on the simulation clock: pause, step, rate ×0.25 to ×10, recording and replay. On the wire and in storage, time is always
the int64 `t_sim_ns` counted from the start of the run; the wall-clock `t_wall_ns` is used only for display and auditing, and intervals are
computed with monotonic clocks only.

## Coordinate frames

`world` is the only metric frame of the system: the ENU tangent plane at the anchor, X east, Y north, Z up, in meters, right-handed.
Geometry, physics, planning, the environment field, drone state and realtime payloads all use it.

| Frame | Definition | Where it appears |
|---|---|---|
| `world` | ENU at the anchor (meters, right-handed, +Z up) | All metric computation and interface payloads, for example `pos_enu_m`, `home_enu_m`, `vel_enu_mps` |
| `earth` | ECEF (WGS84) | `T_ecef_world` in `coordinate.json` (derived, used only for validation and export, never on the GPU) |
| `source` | Raw frame of the dataset | `source.T_world_source` in `coordinate.json` |
| `uavNN/base_link` | Body FLU (forward, left, up; REP-103) | Attitude, angular rate, sensor mounts, for example `omega_flu_rad_s` |
| `uavNN/<sensor>_optical` | Camera optical frame RDF | Sensor simulation and intrinsic projection |
| `three` | three.js render frame, Y up | Only in the browser's render layer; mapped as `(x, y, z) = (E, U, −N)` |
| `ned`, `frd` | PX4 NED and FRD | Only at the PX4 adapter boundary (FleetSim internals are the one exception, converted on publication) |
| `uavNN/local` | Local NED frame of an external flight controller, origin at that drone's EKF origin | PX4 and Prometheus adapter boundary; `T_world_local` is written to `state_ext.frames` |

Conventions:

| Item | Convention |
|---|---|
| Matrices | `T_<to>_<from>` satisfies `p_to = T · [p_from; 1]`; nested row-major 4×4 in JSON |
| Quaternions | `[x, y, z, w]` (Hamilton, unit norm), meaning WORLD←BODY(FLU), in fields such as `q_xyzw` |
| Yaw | `yaw_rad` on the wire is the ENU yaw: 0 at east, counterclockwise positive, radians |
| Heading display | Degrees, 0 at north, clockwise: `heading_deg = (90° − ψ_enu) mod 360` |
| Wind direction | Meteorological convention: the direction the wind comes from |
| Unit suffixes | Field names carry unit suffixes: `_m`, `_mps`, `_rad`, `_ns`, `_ms` (`_ms` only means milliseconds) and others |
| Precision | Only relative coordinates on the GPU; world coordinates are never stored as float16 |

**Anchors and latitude and longitude.** Real captures use an RTK base or a surveyed control point as the origin. Synthetic and dataset
worlds (including the synthetic demo city and the six UrbanScene3D cities) put the origin at the bounding box center and the median ground
height, with `anchor.kind = "synthetic"`; latitude and longitude derived from landmarks are illustrative only: they drive the sun position,
sky and interface display, must not be used for real navigation, and the interface labels them as illustrative coordinates. The confidence of
north is recorded in `trueNorth.confidence` (`exact`, `verified`, `assumed` or `unknown`); with `unknown` the interface shows no north arrow.

Conversions have exactly one implementation each: `awr/world/georef/frames.py` and `awr/environment/conventions.py` in Python,
`apps/web/src/engine/geo/frames.ts` and `engine/environment/state/conventions.ts` in TypeScript, cross-checked with golden cases.

## Contracts

Interfaces and file formats are contract first: 75 JSON Schemas in `packages/contracts` generate the Python and TypeScript types, and binary
layouts, reason codes and the unit dictionary are registered there too, with golden files keeping both sides consistent. The OpenAPI
snapshots of the REST interfaces (`packages/contracts/rest/*.snapshot.json`) are generated by the contract tooling as well; the endpoint
tables of this documentation come from those snapshots. Any change to a contract (file formats, `awr.rt.v1`, the REST interface, reason
codes) is specified in the design documents before the code changes.
