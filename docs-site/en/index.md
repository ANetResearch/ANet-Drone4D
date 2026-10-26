# ANet4D Documentation

**ANet4D: simulation infrastructure for collaborative embodied agents.** ANet4D aims to be for collaborative embodied-agent research
what NS-3 is for network research: a shared experimental base in which teams of embodied and software agents are studied in one world,
on one clock and under one evaluation protocol. The intended scope covers multi-UAV fleets, unmanned ground vehicles (UGV), unmanned
surface vessels (USV), ground robots, fixed sensors and software agents without a body.

The current foundation is what V0.1 to V0.3 delivered:

- **Real-world 4D digital twins.** Captured or reconstructed city point clouds and photogrammetric meshes become versioned Worlds that serve
  physics, queries and display, with a 4D environment field (wind, visibility, precipitation) on the same simulation clock.
- **Interactive Sandbox (V0.2).** One isolated simulation core per visitor with fleet simulation, targets and perception quantification,
  nests and tasking, a REST interface and a Python SDK; the server needs no GPU and the scene renders in the browser.
- **Event Lab (V0.3).** Event tapes compiled from declarative event specifications, Gazebo Sim lockstep as the high-fidelity physics tier,
  mesh-rendered camera, thermal and LiDAR sensors, and a benchmark runtime with an agent SDK and baseline policies; sensor generation
  requires an NVIDIA GPU on the server.

The delivered entity type today is the drone (multirotor, fixed-wing and VTOL airframes). The forward direction is a cross-entity
collaborative experimentation platform with declarative experiment specifications, replaceable collaboration algorithms and comparable
evidence; see {doc}`project/roadmap`.

This documentation covers **0.3.4** (V0.3.4 · ANet4D). The public instance runs at
[anet4d.agentnetwork.org.cn](https://anet4d.agentnetwork.org.cn), and the source code is on {gh}`GitHub </>`. ANet4D was
previously published as ANet Drone4D; the former site and documentation addresses redirect to the new ones, and a small set of legacy
identifiers keeps the former name (see {doc}`project/faq`).

## Getting help

When you run into a problem, look in this order:

- Read the {doc}`project/faq` and the troubleshooting section of {doc}`intro/quickstart`.
- Search or open an issue on {gh}`GitHub Issues </issues>`, in English or Chinese.
- For changes that affect a contract (file formats, `awr.rt.v1`, the REST interface, reason codes), open an issue for discussion first; see
  {gh}`CONTRIBUTING.md </blob/main/CONTRIBUTING.md>`.
- Report security issues to hi@anet0.com; see {gh}`SECURITY.md </blob/main/SECURITY.md>`.

## First steps

```{toctree}
:caption: First steps
:hidden:

intro/overview
intro/install
intro/quickstart
```

{doc}`intro/overview`
: Positioning, core capabilities and the delivery status of each (delivered, in development, research).

{doc}`intro/install`
: Server and browser requirements, ports, directories, environment variables and run profiles.

{doc}`intro/quickstart`
: From `git clone` to a local shared show and a sandbox session, then create a session, add a drone, read its state and end the
  session with the Python SDK.

## User guide

```{toctree}
:caption: User guide
:hidden:

guide/showcase
guide/sandbox
guide/targets
guide/tasking
guide/mobile
guide/identity
guide/sdk-quickstart
```

{doc}`guide/showcase`
: The read-only shared show: scenario S7, cameras and sensor views, targets and relay statistics; each of the other six cities plays
  back its own recorded monitoring demonstration.

{doc}`guide/sandbox`
: One isolated sandbox per visitor: worlds and scenario templates, management panels, clock and rate, manual control, snapshots and
  world switching, lifetime and power saving.

{doc}`guide/targets`
: Target classes, attributes and sensitivity ranges, the discovery criterion, the detection, recognition and identification levels, and
  definite capture.

{doc}`guide/tasking`
: Six task types and area guard, nests and batteries, GSD scan planning, 7x24 relay monitoring and sustainability assessment.

{doc}`guide/mobile`
: Device classes, layouts, touch gestures and quality settings on phones and tablets.

{doc}`guide/identity`
: The identity entry for internal staff: where it is and what it is for.

{doc}`guide/sdk-quickstart`
: The `anet4d` Python SDK of the Event Lab (V0.3): install it, write an example agent and run a 3-drone episode on the high-fidelity and fast simulation tiers.

## Open interface

```{toctree}
:caption: Open interface
:hidden:

api/rest
api/sdk
```

{doc}`api/rest`
: Endpoint tables of the sandbox REST v1 and the local runtime API (generated from the OpenAPI snapshots), authentication, reason
  codes, quotas and the realtime protocol.

{doc}`api/sdk`
: The single-file Python SDK `awr_sandbox`: installation, object model, call reference, error handling and examples.

## System and data

```{toctree}
:caption: System and data
:hidden:

topics/architecture
topics/worlds
topics/deployment
```

{doc}`topics/architecture`
: Process topology, data flow, clocks and the sandbox layer; conventions for World ENU, body FLU, the render frame and the PX4 boundary.

{doc}`topics/worlds`
: World Package v1, the seven worlds, the synthetic demo city, and the authorization and citation of UrbanScene3D.

{doc}`topics/deployment`
: The deployment kit of the public demo, resource limits, path routing and how this documentation site is published.

## All the rest

```{toctree}
:caption: All the rest
:hidden:

project/roadmap
project/acceptance
project/faq
project/citation
```

{doc}`project/roadmap`
: What V0.1 to V0.3 delivered, the scope of V0.3.2 and V0.3.3, and the forward direction of ANet4D (D4).

{doc}`project/acceptance`
: Results of the second D2 acceptance round, root causes of the failed items, and links to the full reports.

{doc}`project/faq`
: Frequently asked questions.

{doc}`project/citation`
: Citing ANet4D and UrbanScene3D, license and third-party notices.
