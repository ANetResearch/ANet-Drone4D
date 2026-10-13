# ANet Drone4D Documentation

ANet Drone4D is a real-world grounded 4D World Runtime for autonomous multi-agent drones. It turns captured or reconstructed city
point clouds into versioned Worlds, runs the environment field, the drone fleet and their agents on one server clock, and streams the
result to any browser with WebGL2. The server needs no GPU.

This documentation covers **0.2.0** (V0.2-demo, that is D2 and D2.1), released on 2026-10-13. The public instance runs at
[drone4d.agentnetwork.org.cn](https://drone4d.agentnetwork.org.cn), and the source code is on
[GitHub](https://github.com/ANetResearch/ANet-Drone4D).

## Getting help

When you run into a problem, look in this order:

- Read the {doc}`project/faq` and the troubleshooting section of {doc}`intro/quickstart`.
- Search or open an issue on [GitHub Issues](https://github.com/ANetResearch/ANet-Drone4D/issues), in English or Chinese.
- For changes that affect a contract (file formats, `awr.rt.v1`, the REST interface, reason codes), open an issue for discussion first; see
  [CONTRIBUTING.md](https://github.com/ANetResearch/ANet-Drone4D/blob/main/CONTRIBUTING.md).
- Report security issues to hi@anet0.com; see [SECURITY.md](https://github.com/ANetResearch/ANet-Drone4D/blob/main/SECURITY.md).

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
: What D1 and D2 delivered, the design and phases of D3, and the themes of later versions.

{doc}`project/acceptance`
: Results of the second D2 acceptance round, root causes of the failed items, and links to the full reports.

{doc}`project/faq`
: Frequently asked questions.

{doc}`project/citation`
: Citing ANet Drone4D and UrbanScene3D, license and third-party notices.
