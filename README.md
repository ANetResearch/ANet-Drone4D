<div align="center">

<img src="docs/media/drone4d-banner.jpg" alt="ANet-Drone4D: the 4D world runtime for multi-agent drones" width="100%" />

<h3>Reality in, swarms out.</h3>

Real-world 4D simulation for multi-agent drones: city-scale point-cloud digital twins, fleet simulation,
swarm control and target perception on one simulation clock, rendered live in any browser. No server GPU required.

[![Release](https://img.shields.io/badge/release-v0.2.0-1f1f1f)](https://github.com/ANetResearch/ANet-Drone4D/releases/tag/v0.2.0)
[![CI](https://github.com/ANetResearch/ANet-Drone4D/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/ANetResearch/ANet-Drone4D/actions/workflows/ci.yml)
[![Docs](https://img.shields.io/badge/docs-Read%20the%20Docs%20layout-1f1f1f)](https://docs.agentnetwork.org.cn/drone4d/en/latest/)
[![Live demo](https://img.shields.io/badge/live%20demo-drone4d.agentnetwork.org.cn-e0322d)](https://drone4d.agentnetwork.org.cn)
[![License](https://img.shields.io/badge/license-modified%20Apache--2.0-1f1f1f)](LICENSE)

**[Live demo](https://drone4d.agentnetwork.org.cn)** · **[Documentation](https://docs.agentnetwork.org.cn/drone4d/en/latest/)** ·
[Quick start](https://docs.agentnetwork.org.cn/drone4d/en/latest/intro/quickstart.html) ·
[Open API](https://docs.agentnetwork.org.cn/drone4d/en/latest/api/rest.html) ·
[Releases](https://github.com/ANetResearch/ANet-Drone4D/releases) · [ANet](https://github.com/ANetResearch/ANet)

**English** · [简体中文](README.zh-CN.md)

</div>

<br/>

<img src="docs/media/demo-flight.webp" alt="Scenario S0 in ANet Synthetic City: drones spiral around the ANet Tower, cover city blocks and fly a V formation along the river" width="100%" />

<sub>Scenario S0 in ANet Synthetic City, captured from the V0.1 build on a machine without a GPU and played back at twice real time.</sub>

## Highlights

- **Point-cloud digital twin.** Seven worlds, ANet Synthetic City and six UrbanScene3D cities, stream into the browser as
  octree point clouds; density follows the viewpoint and the frame budget.
- **Drone swarm simulation and swarm control.** FleetSim steps 1 to 1,000 vehicles on a shared 250 Hz clock with a
  PX4-style flight model, mission generators, geofences and fleet separation in the same loop.
- **Targets, perception and missions.** Six target classes with Johnson/TTPF detection for EO, NIR, LWIR, radar and
  acoustic sensors; nests, greedy task allocation and 7x24 relay watch outside each target's sensitivity range.
- **An interactive sandbox per visitor.** Each session runs an isolated simulation core, operated from the browser,
  from phones and tablets, or through REST v1 and a Python SDK.
- **Built for the next step.** The D3 Event Lab adds Gazebo Sim lockstep physics, mesh cameras and LiDAR, FAST-LIVO2
  reconstruction comparisons and event-level benchmarks for multi-agent observation policies.

## Capabilities

| | Capability | Status |
|---|---|---|
| Digital Twin | City-scale point clouds, progressive loading, automatic density | Available |
| Drone Swarm | FleetSim, up to 1,000 vehicles on one clock | Available |
| PX4 | PX4-style position and attitude cascade (PX4-lite), checked against PX4 SIH flights | Available |
| Swarm Control | Formations, area coverage, geofences, separation, 14-state flight state machine | Available |
| Perception | Six target classes, EO, NIR, LWIR, radar and acoustic sensors | Available |
| Agent Swarm | Capability discovery and task delegation over Mock ANet (not enabled on the public site) | Available |
| Gazebo | Gazebo Sim lockstep as the high-fidelity physics tier | In development |
| LiDAR and cameras | Mesh LiDAR (Livox MID-360 pattern) and photorealistic mesh cameras | In development |
| 4D Reconstruction | FAST-LIVO2 on simulated data, compared with mesh ground truth | In development |
| Rerun | Recordings exported for inspection in Rerun | In development |
| 3DGS | Gaussian-splatting appearance layer over the mesh geometry | Research |
| PX4 SITL | Software-in-the-loop flight-controller validation with Gazebo | Research |

Statuses follow the [v0.2.0 release](https://github.com/ANetResearch/ANet-Drone4D/releases/tag/v0.2.0) and the
[D3 design baseline](docs/05-D3-设计增补与决策记录.md).

## Get started

Open the **[live demo](https://drone4d.agentnetwork.org.cn)**: no registration, a read-only shared show, and one
isolated sandbox per visitor. To run it locally on Linux:

```sh
git clone https://github.com/ANetResearch/ANet-Drone4D.git && cd ANet-Drone4D
make setup        # Python and Node dependencies
make demo-world   # generate ANet Synthetic City
make run          # then open http://127.0.0.1:8000/world/synthcity
```

The [Quick start](https://docs.agentnetwork.org.cn/drone4d/en/latest/intro/quickstart.html) walks through every step
with real output, including a sandbox session driven from the Python SDK.

## Gallery

<table>
<tr>
<td width="50%"><img src="docs/media/landing-desktop.jpg" alt="The ANet Drone4D public demo landing page" /></td>
<td width="50%"><img src="docs/media/d2-mobile.jpg" alt="The shared show on two phones with the bottom tab bar" /></td>
</tr>
<tr>
<td><sub>Public demo landing page.</sub></td>
<td><sub>Phone layout with window switching.</sub></td>
</tr>
<tr>
<td><img src="docs/media/city-shanghai.jpg" alt="UrbanScene3D Shanghai point cloud" /></td>
<td><img src="docs/media/city-newyork.jpg" alt="UrbanScene3D New York point cloud" /></td>
</tr>
<tr>
<td><img src="docs/media/city-shenzhen.jpg" alt="UrbanScene3D Shenzhen point cloud" /></td>
<td><img src="docs/media/city-sanfrancisco.jpg" alt="UrbanScene3D San Francisco point cloud" /></td>
</tr>
<tr>
<td><img src="docs/media/city-chicago.jpg" alt="UrbanScene3D Chicago point cloud" /></td>
<td><img src="docs/media/city-suzhou.jpg" alt="UrbanScene3D Suzhou point cloud" /></td>
</tr>
<tr>
<td colspan="2"><sub>Six UrbanScene3D cities on the public demo, rendered in the browser.</sub></td>
</tr>
</table>

<details>
<summary><b>How it works</b></summary>

<br/>

- **World.** A world is a package of an octree point cloud, a terrain model, semantics and derived caches, in one
  metric East-North-Up frame. The browser streams octree nodes by HTTP range requests and draws them in a single call
  through a point pool sized to the frame budget.
- **Simulation.** `sim-core` advances the fleet, the environment (wind, visibility, weather), targets, perception and
  tasking on one integer-nanosecond clock. The flight model ports the PX4 position and attitude cascade and runs in
  numba kernels.
- **Realtime protocol.** `awr.rt.v1` carries a JSON control plane and a binary batch layer with credit-based
  backpressure, so slow clients never stall the server.
- **Sandbox.** `sandbox-pool` runs one simulation core per session under cgroup quotas, with admission control, a
  visible queue and an energy-saving mode that releases idle sessions.
- **Browser.** React with an imperative three.js engine; every render path is tested under the WebGL2 specification
  minimum limits so that integrated and mobile GPUs work.

Details: [Architecture](https://docs.agentnetwork.org.cn/drone4d/en/latest/topics/architecture.html) ·
[Worlds and data](https://docs.agentnetwork.org.cn/drone4d/en/latest/topics/worlds.html) ·
[Design documents](docs/).

</details>

<details>
<summary><b>Open API and Python SDK</b></summary>

<br/>

Every sandbox operation is available through REST v1 at `/api/sandbox/v1`, with the same permissions as the interface.
The single-file SDK [`tools/sdk/awr_sandbox.py`](tools/sdk/awr_sandbox.py) wraps it:

```python
from awr_sandbox import Sandbox

sb = Sandbox("http://127.0.0.1:8002", timeout_s=30)
with sb.create("synthcity", "t5_area_guard") as s:
    v = s.vehicles.add("p600_mid360", nest_id="nest-b", label="docs-1")
    print(s.vehicles.state(v["id"])["state"]["flight_state"])
```

Endpoint reference: [REST API](https://docs.agentnetwork.org.cn/drone4d/en/latest/api/rest.html) ·
[Python SDK](https://docs.agentnetwork.org.cn/drone4d/en/latest/api/sdk.html).

</details>

<details>
<summary><b>Roadmap</b></summary>

<br/>

| Version | Scope | Status |
|---|---|---|
| V0.1 | Point-cloud digital twin, progressive loading, FleetSim with PX4-lite, realtime protocol, Mock ANet | Released |
| V0.2.0 | Interactive sandbox, targets and quantified perception, nests and relay watch, open API, phones and tablets | Released |
| V0.3 | D3 Event Lab: GLB worlds, event tapes, Gazebo Sim lockstep, mesh cameras and LiDAR, FAST-LIVO2, benchmark | In development |
| P3 and V1.0 | 3DGS appearance layer, PX4 SITL, real ANet with a self-hosted ANetHub | Planned |

See [Roadmap](https://docs.agentnetwork.org.cn/drone4d/en/latest/project/roadmap.html) and the
[D3 design](docs/05-D3-设计增补与决策记录.md).

</details>

<details>
<summary><b>V0.2.0 acceptance</b></summary>

<br/>

V0.2.0 is accepted against 55 criteria, D2-AC-01 to D2-AC-55. Round 2 (2026-10-12 to 10-13):

| Items | Pass | Fail | Not measured |
|---|---|---|---|
| P0 (50) | 41 | 9 | 0 |
| P1 (5) | 3 | 0 | 2 |

The 24 h soak ran its full duration with no restarts and memory growth of 0.48 MB/h. The remaining items are listed as
known issues in the [release notes](https://github.com/ANetResearch/ANet-Drone4D/releases/tag/v0.2.0); the full report
is [docs/impl/D2-验收报告-第2轮.md](docs/impl/D2-验收报告-第2轮.md).

</details>

<details>
<summary><b>Technology</b></summary>

<br/>

Python 3.12, numba, FastAPI and zenoh on the server; React 19, TypeScript and three.js in the browser; shadcn/ui
components, lieflat charts and morphicons icons; Potree-style octree containers; Sphinx documentation in the Read the
Docs layout. The server needs no GPU. Third-party components and their licenses are listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

</details>

<details>
<summary><b>Relation to ANet</b></summary>

<br/>

ANet-Drone4D is part of the ANet family from Agent Network Research, alongside
[ANet](https://github.com/ANetResearch/ANet), [ANetHub](https://github.com/ANetResearch/ANetHub) and
[ANetCore](https://github.com/ANetResearch/ANetCore). A drone is the first kind of physical agent: it publishes what
it can do, other agents discover it and delegate tasks, and outcomes are checked against receipts. ANet is the
collaboration plane, not the control plane: a delegated task becomes ordinary commands that pass the same admission
and safety checks as any operator's.

</details>

<details>
<summary><b>Data and citation</b></summary>

<br/>

The six city worlds are built from UrbanScene3D, used on the public demo with the authorization of the dataset's
rights holders (经数据集权利方授权使用). This repository does not include the dataset; see [NOTICE](NOTICE). Please
cite UrbanScene3D (ECCV 2022) when you use these worlds:

```bibtex
@inproceedings{UrbanScene3D,
  title     = {Capturing, Reconstructing, and Simulating: the UrbanScene3D Dataset},
  author    = {Liqiang Lin and Yilin Liu and Yue Hu and Xingguang Yan and Ke Xie and Hui Huang},
  booktitle = {ECCV},
  year      = {2022}
}
```

To cite ANet-Drone4D, use GitHub's "Cite this repository" ([CITATION.cff](CITATION.cff)) or:

```bibtex
@software{anet_drone4d_2026,
  title   = {ANet-Drone4D: A Real-World Grounded 4D World Runtime for Autonomous Multi-Agent Drones},
  author  = {{Agent Network Research}},
  year    = {2026},
  version = {0.2.0},
  url     = {https://github.com/ANetResearch/ANet-Drone4D}
}
```

</details>

## Contributing

Issues and pull requests are welcome in English or Chinese; see [CONTRIBUTING.md](CONTRIBUTING.md). Changes to a
contract (file formats, `awr.rt.v1`, the REST API, reason codes) start as an issue, and `make ci` is the merge gate.
Security reports go to hi@anet0.com ([SECURITY.md](SECURITY.md)).

## License

Released under the **ANet Open Source License (ANet-Drone4D)**, a modified Apache License 2.0 that permits commercial
use and adds two conditions: operating a multi-tenant hosted simulation or digital twin service for unrelated parties
requires written authorization, and the ANet logo and copyright notices stay in place. See [LICENSE](LICENSE).

<div align="center">
<br/>
<img src="docs/media/anet-avatar.png" alt="Agent Network Research" width="48" />
<br/>
<sub>Agent Network Research</sub>
</div>
