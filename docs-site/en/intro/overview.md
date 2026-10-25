# Overview

## Positioning

ANet4D is simulation infrastructure for collaborative embodied agents. It is built to study how teams of agents decide together where,
when and how to sense and act in a dynamic real-world place: multi-UAV fleets today, and in the forward direction also unmanned ground
vehicles (UGV), unmanned surface vessels (USV), ground robots, fixed sensors and software agents in one experiment. Experiments are meant
to be reproducible and auditable, so that one team can rerun, inspect and replace each part of another team's setup.

Robot simulators usually start from the vehicle and give it a hand-built map; digital twins usually show a place but cannot run
missions in it. ANet4D starts from the place: a captured or reconstructed point cloud becomes a versioned **World**, the
environment field, the drone fleet and their agents all run on that World under one simulation clock, and a browser watches and
operates it from anywhere. The whole chain is:

```text
Reality → Reconstruction → World → Environment → Simulation → Agent
```

"4D" means three spatial dimensions plus time: drones, weather and the timeline share one simulation clock, so any moment can be
paused, stepped, recorded and replayed.

Four design principles:

- **The World is the core.** Capture, reconstruction and simulation converge on one World Package: geometry for physics and queries,
  a point cloud for display, semantics and no-fly zones, anchored to a real place.
- **The browser views the world, the server computes it.** Physics, safety, planning, perception and environment run on a Linux server
  without a GPU; the browser only streams and draws.
- **The environment is a 4D field.** Wind, turbulence, visibility and precipitation form one field E(x, y, z, t); the same numbers act
  on the airframes and paint the sky.
- **An entity is a Physical Agent.** Each drone (the entity type delivered today) publishes what it can do; other agents discover it, request a quote and delegate tasks.
  Every flight command still passes the simulator's admission and safety checks.

```{image} ../../../docs/media/screenshot-hero.jpg
:alt: The ANet4D sandbox in the synthetic demo city ANet Synthetic City: world, layer and weather panels, fleet list, timeline and performance HUD
:width: 100%
```

A real screenshot of V0.1 (D1) in the synthetic demo city ANet Synthetic City (an 8-core machine without a GPU, software rendering).

## Current version

| Item | Content |
|---|---|
| Version | 0.3.2 (V0.3.2 · ANet4D): the V0.3 Event Lab sealed with the tag v0.3.0 on 2026-10-25, the rename to ANet4D and the experimental scene-target foundations; each GitHub Release lists its known issues |
| Public instance | [anet4d.agentnetwork.org.cn](https://anet4d.agentnetwork.org.cn): the shared show, one sandbox per visitor and read-only Event Lab samples, no installation or sign-in |
| Server | Interactive Sandbox: Linux x86-64, Python 3.12, no GPU required; the public instance runs on a 4-core server without a GPU. Event Lab: Linux x86-64 with an NVIDIA GPU for sensor generation |
| Browser | Any desktop, tablet or phone browser with WebGL2; image quality and frame rate depend on the visitor's GPU |
| License | ANet Open Source License (a modified Apache-2.0), see {doc}`../project/citation` |

## Capabilities and delivery status

Status labels:

- [Delivered]{.anet4d-status}: released in a version and accepted, or listed as a known issue.
- [In development]{.anet4d-status}: design finalized and scheduled in the current phase, with interfaces and acceptance criteria defined; not yet released.
- [Research]{.anet4d-status}: under evaluation or prototyping; whether it enters the product depends on the evaluation.

| Capability | Description | Status |
|---|---|---|
| World Package v1 | Point cloud normalization (units, up axis, leveling, north, origin, normals, ground model), Potree 2.0 compatible octree with ANET_Q16 encoding, DSM and DTM, semantics and zones; `validate --deep` | [Delivered]{.anet4d-status} V0.1 |
| Seven worlds | The synthetic demo city ANet Synthetic City and six UrbanScene3D cities (authorized by the dataset rights holders for this project's deployments) | [Delivered]{.anet4d-status} V0.1 / V0.2 |
| City-scale point cloud streaming | One Range request per octree root for the first screen; the APH selector and CAS controller regulate the point budget across 7 quality tiers in a closed loop | [Delivered]{.anet4d-status} V0.1 |
| 4D environment field | Wind (height profile and turbulence), visibility and precipitation, 12 weather presets; simulation calendar, sun position, illuminance and wavelength-dependent extinction | [Delivered]{.anet4d-status} V0.1 / V0.2 |
| Fleet on one clock (FleetSim) | 1 to 1,000 drones on a shared 250 Hz clock; PX4-style cascade compiled with numba; 1,000 drones stay real time on about half a CPU core | [Delivered]{.anet4d-status} V0.1 |
| Safety and missions | 14-state flight FSM, geofence, energy-aware return to home, separation guard; 8 mission generators | [Delivered]{.anet4d-status} V0.1 |
| Timeline, recording and replay | Pause, step, rate ×0.25 to ×10; MCAP recording with byte-identical replay (local runs; not offered on the public instance) | [Delivered]{.anet4d-status} V0.1 |
| Agent collaboration | In-process Mock ANet: capability discovery, quotes, delegation and receipt verification (search and rescue scenario S3) | [Delivered]{.anet4d-status} V0.1 |
| Interactive sandbox | One isolated session and sim-core per visitor; admission, queueing, renewal, snapshots and world switching | [Delivered]{.anet4d-status} V0.2 |
| Targets and perception quantification | Six target classes with sensitivity ranges; detection, recognition and identification unified by the Johnson criteria and TTPF; per-pair perception readings and field views | [Delivered]{.anet4d-status} V0.2 |
| Nests, tasking and relay | Six task types and area guard, greedy allocator behind the Allocator interface, GSD scan planning, 7x24 relay monitoring and sustainability assessment | [Delivered]{.anet4d-status} V0.2 (short daytime relay gaps are a known issue) |
| Open interface and Python SDK | Sandbox REST v1, the `awr.rt.v1` realtime protocol, OpenAPI 3.1 and a single-file SDK | [Delivered]{.anet4d-status} V0.2 |
| Airframe catalog | The P600 digital twin (MID-360 configuration) and four generic reference airframes, including fixed-wing and VTOL | [Delivered]{.anet4d-status} V0.2 |
| Startup, capacity and devices | Startup screen with version and progress, queues for viewer seats and sandbox slots, power saving, phone and tablet layouts | [Delivered]{.anet4d-status} V0.2 |
| Mesh worlds | Photogrammetric GLB meshes with metric scale, true north and leveling calibration, and derived geometry, navigation and quality layers | [Delivered]{.anet4d-status} V0.3 |
| Event simulation and active observation benchmark | Compare the observations of different fleet policies under the same world and event; event DSL and event tape, evaluation and an internal leaderboard | [Delivered]{.anet4d-status} V0.3 |
| High-fidelity physics tier | The gz-sim lockstep tier as the physics tier for official scores; FleetSim stays as the fast tier | [Delivered]{.anet4d-status} V0.3 |
| Sensor generation service | Mesh rasterization cameras (RGB, depth, instance, semantics, rolling shutter, thermal), mesh LiDAR with the Livox MID-360 scan pattern, and IMU synthesis | [Delivered]{.anet4d-status} V0.3 |
| Agent SDK and baselines | The `anet4d` Python SDK, the agent wire protocol over Unix sockets, isolated agent containers and three baseline policies | [Delivered]{.anet4d-status} V0.3 |
| Scene-target foundations | Target appearance, activity and population bindings as additive optional contract fields; disabled by default and not offered on the public instance | [In development]{.anet4d-status} V0.3.2 |
| Large-model command layer | A large-model command layer through an inference gateway, and ANet multi-agent delegation (moved out of the V0.3 seal) | [Research]{.anet4d-status} D3.x / D4 |
| 3DGS appearance layer | Gaussian appearance layer from mesh distillation and real aerial assets; the G-3DGS evaluation gate decides product entry | [Research]{.anet4d-status} D3.x / D4 |
| Cross-entity collaborative experimentation | Heterogeneous entities (UAV, UGV, USV, ground robots, fixed sensors, software agents) in one experiment; declarative experiment specifications, replaceable collaboration algorithms and a collaboration leaderboard | [Research]{.anet4d-status} D4 |
| GPU reconstruction | Reconstruction engines such as LingBot-Map, DA3-Streaming and MapAnything (the mock engine pipeline already runs end to end) | [Research]{.anet4d-status} V0.5 |
| Real ANet network | A self-hosted ANetHub, one daemon per drone, CBBA allocation under disconnection | [Research]{.anet4d-status} V1.0 |

## Documentation map

| You want to | Start here |
|---|---|
| Try it without installing | The [public instance](https://anet4d.agentnetwork.org.cn), with {doc}`../guide/showcase` and {doc}`../guide/sandbox` |
| Run it locally | {doc}`install` and {doc}`quickstart` |
| Control a sandbox from code | {doc}`../api/sdk` and {doc}`../api/rest` |
| Understand the internals | {doc}`../topics/architecture` and {doc}`../topics/worlds` |
| Deploy your own instance | {doc}`../topics/deployment` |
| Follow progress and quality | {doc}`../project/roadmap` and {doc}`../project/acceptance` |

The design documents are written in Chinese and live in the repository under
{gh}`docs/ </tree/main/docs>`: the design baseline with 85 ADRs (docs/03), the D2 design supplement
(docs/04), the D3 design supplement (docs/05), the module PRDs (docs/modules/) and the implementation and acceptance reports (docs/impl/).
This site is written for users and summarizes the parts relevant to using the system; where the two differ, the design documents prevail.
