# Sandbox

A sandbox is an isolated session owned by one visitor: it has its own sim-core process, and you can place drones, targets and nests, issue
tasks, take manual control and switch sensor views. The interface and the open interface have the same rights; every command passes the
same admission and safety checks.

## Opening a sandbox

| Entry | How |
|---|---|
| Public instance | "Open my sandbox" on the landing page: choose a world and a scenario template (default ANet Synthetic City with T5 area guard), confirm, and enter `/sandbox/<sid>` |
| Open interface | `POST /api/sandbox/v1/sessions`, or `Sandbox.create(world, template)` in the SDK (see {doc}`../api/sdk`) |
| Local | `AWR_PROFILE=sandbox make run` starts the sandbox control plane; the SDK connects to `http://127.0.0.1:8002` (see {doc}`../intro/quickstart`) |

When the server is full, the request queues: the interface shows a non-modal queue card (position, estimated wait, leave the queue), and
the SDK waits with long polls and claims the slot when its turn comes. You can keep watching the shared show while queued. The public
instance requires a proof of work when needed (solved automatically by the browser and the SDK) and limits the number of sessions and queue
tickets per address prefix.

## Scenario templates

Each world has a set of verified scenario templates; nest positions, task areas and target positions are checked in advance against that
world's geometry and lines of sight:

| Template | Content | First feedback |
|---|---|---|
| T1 point watch `t1_point_watch` | 1 nest box, 2 P600s hovering at a point with the gimbal locked on 1 stationary person | Definite capture in about 40 s |
| T2 polyline patrol `t2_polyline` | 1 P600 looping over four waypoints | Flight legs and FPV |
| T3 multi-drone area patrol `t3_area_patrol` | 2 nest boxes, 3 drones (P600 and AWR-Q1) patrolling sub-areas, 3 wandering people in the area | Sub-areas and detections |
| T4 GSD complete scan `t4_scan_gsd` | 1 nest, 4 P600s; preview the allocation, then run a complete scan at the required GSD | Drone count and completion curve |
| T5 area guard and relay `t5_area_guard` (default) | A nest box and a VTOL pad, P600 and AWR-V1, 2 targets; `area_guard` hands discoveries over to relay | First definite capture within about 60 s of the session start |
| T6 manual flight and sensor views `t6_manual` | 1 airborne P600, 1 target, optional night calendar | Takeover prompt |

"Load scenario" in the session bar reloads any template within the session. On the first visit, a 4-step guide introduces the session bar
and lifetime, the target panel and sensitivity ranges, the task panel and allocation preview, and capture prompts and FPV; it can be skipped.

## Interface

The sandbox view keeps the floating layout of the shared show, with the role badge "My sandbox · operator". The **session bar** below the
top bar shows the world, the remaining lifetime ("Renew" when renewal is possible), the rate (with the reachable rate and the reason when
limited), counts of drones, targets and nests, and "Load scenario", "Reset" and "End session". Four management panels:

| Panel | Content |
|---|---|
| Fleet | Airframe catalog and parameter tables, clone and edit, add, modify and remove instances, sensor payloads and parameters (with bounds checking and instant hints for GSD, maximum recognition range and feasible window) |
| Targets | Add from class templates; place by clicking the map, drag, draw paths and wander areas; attribute forms; 3D display of sensitivity ranges; perception state and discovery count |
| Nests | Placement, type, inventory, energy mode, charging channels and battery queue, launch and landing queue |
| Tasks | Draw points, polylines and polygons with the viewport toolbar; type and parameters; "Preview allocation"; start, pause and cancel; relay statistics |

Panels support URL deep links `?panel=<id>` for sharing and bookmarking.

## Session limits

| Item | Limit |
|---|---|
| Drones | 12 |
| Targets | 30 (up to 4 sensitivity ranges each, at most 64 enabled per session) |
| Nests | 4 |
| Concurrent tasks | 6 |
| Cloned airframes | 8 |
| Configuration snapshot | 512 KiB |
| Realtime connections | 4 per session |

Task inputs have their own quotas (area up to 1 km², up to 64 vertices, up to 200 strips, up to 50 work items, among others); larger inputs
are rejected with reason code 585.

## Clock and rate

The clock starts at ×1 once the session is ready. The rate steps are ×0.25, ×0.5, ×1, ×2, ×5 and ×10; a requested rate is granted only
within the CPU budget, and when it is limited the response carries the granted rate and the reason (for example the capacity ladder or the
step duration). Clock operations are play, pause, rate and single step, through `POST /sessions/{sid}/clock` or `Session.clock()` in the
SDK. The clock is locked to ×1 while manual control is active.

## Manual control

Select a drone and "Take over" to acquire its lease: the tasking lease on that drone is preempted and its work items are reallocated.

| Input | Action |
|---|---|
| W / S | Forward, backward |
| A / D | Left, right |
| Q / E | Down, up |
| Left and right arrow keys | Yaw |
| C | Switch speed band (slow, normal, fast) |
| Esc | Leave takeover |
| Shift+R, Shift+L, Shift+X | Return to home, land, safe stop (global safety shortcuts, active during takeover) |

Touch screens and mice can use virtual twin sticks; releasing them sends a stop. For fixed-wing airframes the inputs map to airspeed, turn
rate and climb rate. Velocity setpoints are sent at up to 50 Hz.

Keep-out volumes are always shown during manual flight, and a warning appears when the drone is predicted to enter any sensitivity range
within 3 s. The soft fence is enforced on the server: at the velocity setpoint entry, sim-core clamps the velocity component towards a
sensitivity range to 0 and returns the warning `KEEPOUT_CLAMPED`. Velocity control from the interface, the SDK and REST is protected in the
same way; the session parameter `keepout_guard` is on by default and can be turned off in the settings, after which entering a sensitivity
range counts as a discovery under the criterion. After releasing the takeover, the drone returns to the available pool, or you can choose
"Return to nest".

## Snapshots, reset and world switching

- **Configuration snapshot**: `GET /sessions/{sid}/snapshot` exports the current configuration of nests, fleet, targets and tasks (JSON, up
  to 512 KiB), still retrievable for 10 min after the session ends; `POST /sessions/{sid}/restore` restores it in the **same world**.
- **Reset**: reloads the current template.
- **Switch world**: `POST /sessions/{sid}/world` restarts sim-core in the new world and loads the target world's template of the same name.
  The sid stays the same; realtime connections close with code 1012 and reconnect to the new world. Targets, nests, tasks and view settings of
  the previous world are not carried over to the new world (coordinates and geometry differ between worlds); export a snapshot first if you
  need to keep them.

Known issue: in the 0.2.0 release currently serving the public instance, this isolation does not fully hold on every switching path. Several
client-side caches (targets, perception and field readings, tasking, the fleet roster, the timeline) were not reset per world, so entities and
view settings from the previous world could remain visible after switching, including when switching worlds inside a sandbox session. The
defect has been fixed in the development line (every such cache is now gated and reset by a world-scoped data layer) and ships with the next
release; until the public instance is redeployed, you may still observe it there.

## Lifetime, renewal and power saving

| Item | Rule |
|---|---|
| Initial lifetime | 30 min |
| Renewal | 30 min per renewal, up to 120 min; renewal pauses while visitors are queued |
| Power-save parking | After 10 min without interaction (5 min when the sandbox queue is not empty) or with the page hidden: the checkpoint and configuration snapshot are written to disk, sim-core stops and the slot is released; simulation time and lifetime are frozen |
| Idle without connection | A REST-only session without a realtime connection is parked after 120 s (60 s when the queue is not empty) without any session call |
| Resume | Any interaction or session request resumes from the checkpoint (with a resume ticket in the queue when full); parked sessions are kept for 60 min |
| End | "End session", closing the page, lifetime expiry or reclamation; a configuration snapshot is exported before the end |

## Capacity

The number of concurrent sandboxes is computed by the capacity model from measured session cost and the CPU budget, with a hard limit of 6;
the public instance currently allows 2, and further visitors queue. The landing page and startup screen show live viewers, sandbox slots,
queue length and estimated wait (public endpoint `GET /api/sandbox/v1/capacity`).
