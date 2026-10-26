# Versions and roadmap

ANet4D first builds one complete chain, then covers every layer with mocks, and then replaces the layers with real back ends one by
one without changing the interfaces. Delivery advances in stages numbered "D": D1 corresponds to V0.1, D2 (with D2.1) to V0.2-demo, D3 to V0.3 (the event
simulation and active observation benchmark, called the Event Lab), and D4 is the forward direction of ANet4D: a cross-entity
collaborative experimentation platform. D1 to D4 remain the internal phase labels after the rename to ANet4D.

## Released

### V0.1 (D1): a World Runtime with every layer in place

Six UrbanScene3D cities and the synthetic demo city, city-scale point cloud streaming, FleetSim with 1 to 1,000 drones, safety and missions,
L0/L1 environment, recording and replay, and an in-process Mock ANet. 36 of the 38 acceptance items passed, including all 25 P0 sub-items; the
remaining 2 P1 items are listed as known issues. See the
{gh}`D1 acceptance report (round 6) </blob/main/docs/impl/D1-验收报告-第6轮.md>`.

### V0.2-demo (D2 and D2.1): interactive sandbox and open interface

Released as 0.2.0 on 2026-10-13. Main content:

- one isolated sandbox per visitor, with a concurrency limit and queueing; seven base worlds and scenario templates T1 to T6;
- target management and perception quantification (detection, recognition and identification levels and definite capture);
- nests and greedy tasking, complete scans at a given GSD, 7x24 relay monitoring and sustainability assessment;
- the open interface (sandbox REST v1, `awr.rt.v1`, OpenAPI 3.1) and a single-file Python SDK;
- an airframe catalog including fixed-wing and VTOL;
- D2.1: a startup screen with version and progress, queues for viewer seats and sandbox slots, power saving, phone and tablet layouts, the
  internal identity, and acoustic, visual, thermal and radar field views.

Of the 55 acceptance items, 44 passed, 9 failed and 2 await production measurement; the version was released with known issues, see
{doc}`acceptance`. V0.2-demo is a demo and open interface slice in parallel with the V0.2 theme and does not change the original exit criteria
of V0.2.

### V0.3 (D3): Event Lab

Sealed with the tag v0.3.0 on 2026-10-25 (UTC), followed by 0.3.1 on the same day, which made the Event Lab samples reachable from the public landing
page. The seal covers phases P-1, P0a and P0b of the D3 plan below: mesh worlds, event tapes, the gz-sim lockstep physics tier, mesh cameras
(including thermal), mesh LiDAR and IMU, the Python SDK with three baseline policies, the evaluator and an internal leaderboard, and
read-only public samples. Of the 41 acceptance items in the seal scope, 20 passed and 21 passed under recorded ADR decisions, with none
failed; the open follow-ups are registered for the first D3.x release. See
{gh}`the D3 delivery summary </blob/main/docs/impl/D3-交付总结.md>`.

### V0.3.2: ANet4D

V0.3.2 renames the product to ANet4D and repositions it as simulation infrastructure for collaborative embodied agents. It also includes
the experimental scene-target foundations (target appearance, activity and population bindings as additive optional contract fields),
which are disabled by default and not offered on the public instance. The public site moves to
[anet4d.agentnetwork.org.cn](https://anet4d.agentnetwork.org.cn) and this documentation to `docs.agentnetwork.org.cn/anet4d/`; the former
addresses redirect permanently. The Python SDK is now `anet4d`; `drone4d` remains for one release as a deprecated alias.

### V0.3.3: Event Lab samples

V0.3.3 extends the public samples of the Event Lab (`/lab/`) to seven runs over five dev events: three baseline policies on
`crop-handoff-001` (`track_share`, `coverage_scan`, `static_guard`) and one run each on `crop-handoff-002`, `crop-reid-001`,
`crop-search-001` and `crop-vehicle-001`. Each run offers the event replay, the RGB and thermal private views of every UAV and Rerun data
inspection; the samples are single-seed single runs, not ranked results. Sample media moves to the ModelScope dataset
`inksong/ANet4D-samples`, and the landing page of the public site shows a sample gallery right after the first screen. The repository is
renamed to `ANetResearch/ANet4D`; development happens on `main` only, and earlier releases are checked out by tag. V0.3.4 is a patch release: the images of the Event
Lab section on the landing page load only near the viewport, so they no longer compete with the sandbox for the outbound bandwidth of the
public host.

## D3 design record: event simulation and active observation benchmark

D3 is defined as *A World-Grounded Event Simulation and Active Observation Benchmark for Collaborative Aerial Agents*: under the same world
and the same events, it compares the observations obtained by different fleet policies, and studies how multiple autonomous agents decide
together where, when and how to observe a dynamic world under partial observability. The D3 design
({gh}`docs/05 </blob/main/docs/05-D3-设计增补与决策记录.md>`) was implemented through phase P0b and sealed in V0.3; phases P1 to P3 were moved
by ADR to D3.x releases or to D4.

### Design highlights

| Aspect | Highlights |
|---|---|
| Relation to D2 | D2 remains unchanged as the public interactive sandbox; D3 only adds modules, joins D2 through registered extension points, and a regression gate guards D2 behavior |
| Four authorities | World, events, physics and agents never overstep each other; the sensor generation service is a pure function of poses and the event tape, shared by all physics tiers |
| World asset import (M22) | Photogrammetric triangle meshes go through mandatory metric scale, true north and leveling calibration, and derive geometry, navigation and quality layers |
| Event runtime (M23) | `drone4d.event.v1` (a legacy identifier kept for compatibility) compiles into an event tape that is byte-identical across machines; the first four event types are identity keeping across occlusion, multi-person meeting and handover, vehicle tracking, and search for a target at an unknown position |
| Physics back ends (M24) | The gz-sim lockstep tier is the default physics tier and the only source of official scores; FleetSim stays as the fast tier (development, training and large searches); the SITL tier only validates flight control |
| Sensor generation (M25) | Mesh rasterization cameras output RGB, depth, instance and semantics in one pass, with rolling shutter, multi-drone 1080p output and thermal imaging; GPU per-ray LiDAR with per-point timing; IMU synthesized from the specific force of the physics tier |
| Agent observation (M26) | The only observation outlet for agents: observation whitelist, lockstep and decision latency, link model, isolated containers; the Python SDK and 3 baseline policies ship in the first milestone |
| Benchmark runtime (M27) | MCAP as the single authoritative frame store; tracks by event type, each reporting four metrics with 95% confidence intervals: event capture rate, event understanding, active observation efficiency and cooperation efficiency |
| Public presentation | The leaderboard is shown only in the internal workspace; the public site shows read-only samples (event replays, private views of several drones, a data inspection panel), with no score comparisons and no submissions |

### Phases

| Phase | Relative weeks | Content | Exit |
|---|---|---|---|
| P-1 preparation | Weeks 1 to 3 | D2 release and copy review, extension points and regression gate, contract 1.3.0 draft, measurements of key paths | Regression gate passing, contract review |
| P0a minimal benchmark loop | Weeks 4 to 20 | First metric world, 1 fixed event, gz-sim lockstep, multi-drone cameras and LiDAR, Python SDK and 3 baseline policies, evaluation and an internal leaderboard prototype | Sample package and prototype leaderboard |
| P0b benchmark v1 | Weeks 19 to 34 | Four event types, thermal imaging, leaderboard v1 with invited submissions, reconstruction comparison, public samples | P0 exit |
| P1 | Weeks 35 to 42 | Event editor, large-model command layer, private maps and cooperation, gz cross-validation, standalone camera package | — |
| P2 | Weeks 43 to 48 | SITL tier, interactive mode, calibration, mesh tiles of whole worlds, cooperative exploration demo | — |
| P3 | Weeks 43 to 53 (in parallel with P2) | 3DGS appearance layer (entry decided by the G-3DGS evaluation gate), Gaussian asset tool chain, event families, extended export and dataset release | — |

The exit criteria of every phase are: all blocking acceptance items of the phase pass, at least 80% of the deliverable items pass (with
waivers recorded for the rest), and the D2 regression gate stays green. D3 has 68 effective acceptance items (52 blocking, 16 deliverable);
the V0.3 seal evaluated the 41 items of phases P-1, P0a and P0b.

## Forward direction: D4 collaborative experimentation platform

D4 extends ANet4D from drone fleets to collaborative experiments across entity types: multirotor and fixed-wing UAVs, UGVs, USVs, ground
robots, fixed sensors, edge nodes and software agents without a body. Its purpose is to let researchers develop and compare collaboration
and coordination algorithms (task allocation, cooperative search, dynamic teaming, consensus and information fusion, multi-agent path
finding, communication topology, cooperative tracking, multi-agent reinforcement learning and LLM multi-agent systems) in experiments that
others can rerun, audit and replace part by part.

| Public asset | Content |
|---|---|
| Experiment API | One declarative experiment specification and one command; per-agent policies, centralized policies, solvers and a PettingZoo adapter share one set of data structures |
| Scenario library | A versioned library of suites, problem families and instances with objective difficulty tiers and scale tiers |
| Algorithm baselines | Classic baselines for each suite, with sources and notes on reproduction differences |
| Evaluation protocol | Metric specifications, paired comparisons with multiple-comparison correction, and intervention controls |
| Reproducible traces | A unified trace, run manifests, declared determinism levels, input replay and offline recomputation |

The interaction network inside an experiment has interchangeable tiers (ideal, abstract radio models and an optional NS-3 bridge that is not
used for ranking). The collaboration leaderboard is a first-class output whose role is to turn "algorithm A outperforms algorithm B under the
declared conditions" into verifiable evidence rather than a single rank.

D4 is at the research and concept design stage
({gh}`docs/06 </blob/main/docs/06-D4-协同实验平台概念设计.md>`); its ADRs are proposals, and it makes no implementation commitment.
Implementation starts after the D3 seal; no D4 capability is released.

## Original version themes (design baseline)

The table below is the version plan of the original design baseline (docs/03). D3 and the D4 direction above take precedence where they
differ.

| Version | Theme | Main content |
|---|---|---|
| V0.2 | Real flight stack in the loop | PX4 SIH through MAVSDK (up to 8 drones), Prometheus back end, virtual MID-360, import of arbitrary PLY or LAS point clouds, Python `awr.rt.v1` client |
| V0.3 | Physical environment and planning | L2 mass-conserving wind field library, GPU wind sampling, high environment tier, B-spline and ESDF planning |
| V0.4 | Environment acting on sensors and dynamics | Camera and LiDAR degradation, L2 aerodynamic moments, PX4 SITL lockstep, rewind and what-if branches, P600 parameter identification |
| V0.5 | Real-world fusion | GPU reconstruction, capture sessions with RTK, relocalization, a real P600 through Prometheus, COPC and 3D Tiles export |
| V0.6 | Swarms at scale | Three-layer collision avoidance (4D reservations, ORCA-3D), energy-constrained coverage routing, OpenFOAM wind fields, HITL |
| V0.8 | Visual World and high-fidelity sensors | 3DGS LOD streaming in the same frame as the point cloud, 3D Tiles terrain, Isaac sensor back end, dynamic objects |
| V1.0 | Physical Multi-Agent World | Real ANet (ANetHub plus one daemon per drone), CBBA under disconnection, large models and MCP behind trusted guards, heterogeneous agents |

Parts of D3 (the gz-sim physics tier, the SITL tier, mesh sensors) bring forward the corresponding items of the table above; docs/05 is
authoritative on how the two map onto each other.
