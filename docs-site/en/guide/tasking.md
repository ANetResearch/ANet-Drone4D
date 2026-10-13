# Tasks, nests and relay

A task states what to observe and at what quality. The tasking service breaks a task into work items and allocates them to nests and
airframes under capability and energy constraints. Nests provide launch, landing, battery swapping or charging. Relay monitoring keeps
observing a discovered target from outside its sensitivity ranges and dispatches replacements ahead of time based on the day and night
forecast.

## Task types

| Type | Main parameters | Decomposition | Completion |
|---|---|---|---|
| `point_watch` point watch | Point, altitude, dwell time or "until cancelled", sensor pointing | 1 work item | Dwell time reached |
| `polyline_patrol` polyline patrol | At least 2 waypoints, ping-pong or loop, laps or duration, speed, altitude | Split into legs when longer than one drone can fly | Laps or duration reached |
| `area_patrol` multi-drone area patrol | Polygon, drone count (automatic or fixed), target class and level, revisit period | The area is split into n balanced sub-areas on an equal-time basis | Duration reached; last visit time per cell reported |
| `area_scan_gsd` complete scan at GSD | Polygon, GSD (cm/px), side overlap, off-axis limit, time limit, altitude limit | Strips derived from the GSD and grouped by drone count | 100% of the visible cells covered |
| `perimeter_patrol` perimeter patrol | Polygon, inner or outer offset, direction, revisit period | The perimeter is shared by n drones at equal phase | Duration reached |
| `relay_watch` relay monitoring | Target, required level, duration or "continuous", allowed airframes | Station work items generated on a rolling basis by endurance | Duration reached or cancelled |
| `area_guard` area guard (composite) | Polygon, target class and level, revisit period, allowed nests and airframes | Creates one area patrol and one perimeter patrol automatically | Continuous until cancelled |

Common parameters are priority, start time, allowed nests, allowed airframes and `on_detect` (the action of patrol and scan tasks after
discovering a target: `notify`, `relay` or `ignore`; `area_guard` defaults to `relay`, so the first detection at the required level creates a
relay monitoring task).

Patrol and scan tasks derive the required GSD from the target class and level (the top-view critical dimension divided by the required
pixels; at most 2.7 cm for a person at level R). Every flight leg passes the targets' keep-out volumes (sensitivity range plus 20 m and the
holding error) together with the no-fly zones to the 2.5D A* planner, so transits and legs always avoid them; where a strip is cut by a
keep-out volume, the tasking service inserts an oblique observation point outside the volume for each "hole", so that targets in the hole can
still be discovered.

## Allocation preview

"Preview allocation" in the task panel (`POST /sessions/{sid}/tasks?dry_run=true` in the API) draws sub-areas, strips, fill-in waypoints, legs
and stations in the viewport, and reports the drone count and completion curve, the reasoning behind the drone count and the sustainability
verdict; when infeasible it lists each reason (for example an empty altitude interval, an empty feasible window or a missing night sensor).
Create the task after confirming, then control it with start, pause, resume and cancel.

## GSD scan planning

A complete scan at a given GSD derives altitude and strips from the required ground sampling distance:

1. Cells in the area taller than the flight altitude minus the clearance become keep-out volumes and are removed from the coverage
   denominator; the lowest altitude is the P99 height above ground of the remaining cells plus the clearance (20 m by default), and the
   highest altitude is 120 m AGL by default.
2. For a nadir view of flat ground the GSD equals `H·p'/f` everywhere, which gives the focal length and altitude; leaving the focal range or
   the altitude interval makes the task infeasible.
3. The strip width is the smaller of output pixel width times GSD and the off-axis limit, and the strip spacing shrinks with the side
   overlap; speed is limited by cruise speed, exposure and frame-to-frame overlap.
4. When one sortie or one distant viewpoint cannot cover the area within the time limit, more drones are dispatched automatically, and the
   scan continues across sorties with battery swaps (`allow_multi_sortie`).

## Nests

| Type | Accepted airframes | Notes |
|---|---|---|
| `box` nest box | Multirotors | Automatic launch and landing, battery swap or charging |
| `vtol_pad` VTOL pad | VTOL airframes and multirotors | Automatic landing and battery swap |
| `launcher` launch and recovery station | Fixed-wing | Catapult launch and parachute recovery, needs ground crew for reloading (`crewed`), not used in unattended 7x24 tasking |

A nest's configuration includes slots, inventory, energy mode (`swap` or `charge`), battery packs, chargers and channels, inspection
turnaround, launch interval, wind limit and link range. In swap mode each charging channel charges one battery at a time; when no charged
battery is available, dispatch is delayed and the reason `battery` is recorded. The nest state is published at 1 Hz on `nest/{id}/status`.

The steady-state conditions for continuous relay are: charging channels `n_ch ≥ n_t·ceil(t_charge / t_on)` and total batteries
`n_pack ≥ n_t·ceil((t_on + 2·t_tr + t_swap + t_charge) / t_on)`, where `n_t` is the number of targets under relay at the same time, `t_on` the
time on station per battery and `t_tr` the one-way transit time. For a P600 with the nest 1 km from the target, each target needs 4 channels
and 5 batteries.

## Allocators

The default is a greedy allocator: it assigns work items to available nests and airframes under capability (sensors, endurance, wind limit)
and energy constraints. It sits behind the `Allocator` interface, which is reserved for model-based, large-model and agent allocators; an
external program can read the allocation snapshot (`GET /sessions/{sid}/tasking/snapshot`) and submit its own allocation plan
(`POST /sessions/{sid}/tasking/plans`); see `tools/sdk/examples/06_external_allocator.py`.

## 7x24 relay monitoring

### Trigger and stations

With `on_detect = relay`, the first detection of a target at the required level by a patrol, scan or area guard task creates a relay
monitoring task; it can also be started manually from the target details. A feasibility window check runs first: the feasible station
interval expressed as horizontal distance, whose lower bound is the expanded keep-out distance and whose upper bound is the largest distance
with `N_eff ≥ N_req`. When infeasible, no drone takes off; the response carries reason code 581 with the causes (current illuminance band,
missing sensor, keep-out radius larger than the recognition range and so on) and suggested airframes. Relay uses only the tracks produced by
perception, never the target ground truth.

Station candidates are generated around the predicted target position in 25 m rings, 15° azimuth steps and 5 altitudes, filtered by hard
constraints on line of sight, pixels, clearance, link range and wind, and scored. In urban areas relay relies mainly on hovering multirotor
stations; fixed-wing airframes use loiter circles and suit open areas.

### Early dispatch and handover

The tasking service continuously estimates the remaining usable time of the drone on station and dispatches a replacement before "the
replacement arrives, captures, overlaps for 15 s, plus a margin" runs out. Every 60 s it forecasts, from the sun position and weather
keyframes, whether the current station and sensor stay feasible over the next 45 min, and dispatches an airframe with suitable sensors before
the illuminance band changes (for example at nightfall). At handover the replacement reaches its station and achieves a definite capture;
after both drones have captured simultaneously for at least 15 s, the drone on station returns along a path outside the keep-out volumes.

Staying out of sensitivity ranges takes priority over keeping the capture: when a target moves towards the drone on station, the drone
backs off first, accepting a gap rather than a discovery; such gaps are counted separately.

### Statistics and sustainability assessment

Each relay task publishes statistics at 1 Hz (`GET /sessions/{sid}/relay/{tid}/stats`): monitoring time, capture coverage, gap count and
longest gap (by cause `keepout_priority`, `illum_transition`, `infeasible`, `battery`, `other`), discovery count, handovers and mean overlap,
minimum keep-out margin, sorties, flight hours, energy and battery inventory.

The preview of relay and area guard tasks and `GET /sessions/{sid}/tasks/{tid}/sustainability` give an analytical sustainability
assessment; the task panel shows "Sustainable 7x24: yes or no" with the bottleneck:

| Check | Bottleneck code |
|---|---|
| A non-empty feasible window and a station with line of sight in every illuminance band (day, low light, night) and weather band | `night_sensor`, `los`, `weather` |
| Airframes needed for rotation do not exceed the available airframes of that type | `airframe` |
| Charging channels and total batteries meet the steady-state conditions | `charger`, `battery` |
| Link range and wind limit of nests and stations | `link_range`, `wind` |

Known issues in 0.2.0: randomly walking targets briefly leave the line of sight of the drone on station during the day, producing a number
of short monitoring gaps in 24 h batches; template T5 on the synthetic demo city has `battery` gaps caused by the battery-swap rhythm. See
{doc}`../project/acceptance`.

The example `tools/sdk/examples/05_gsd_scan_dryrun.py` previews a GSD scan, and `scenario2_e2e.py` is the end-to-end acceptance script for
area guard and relay.
