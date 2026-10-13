# Targets and perception

A target is a dynamic entity on the World: something the drones need to detect, recognize and keep observing. Each target has size,
motion, appearance, reflectance, thermal signature, radar cross-section and a periodic sound source, and carries visual and acoustic
sensitivity ranges: a drone that enters a sensitivity range may be "discovered" by the target. All perception criteria are computed on the
server; the sensor views in the browser are visual approximations only.

## Classes

Six class templates (`packages/contracts/target/class_defaults.json`) provide the default attributes; a new class only needs a new template
entry. All values are simulated reference values.

| Class | Size l × w × h (m) | Default motion | Default sensitivity ranges | Behavior after discovery |
|---|---|---|---|---|
| Person `person` | 0.5 × 0.3 × 1.75 | Random walk 1.4 m/s | Visual sector, half angle 60°, radius 300 m; acoustic hemisphere 300 m | Stops and looks for 10 s, then moves away at 2.5 m/s |
| Drone `uav` (third-party small multirotor) | 0.35 × 0.35 × 0.10 | Path or hover, 5 m/s | Visual cone, half angle 40°, 800 m | Moves away and returns home |
| Machine `machine` (generator and similar) | 1.0 × 0.7 × 0.8 | Stationary | None | — |
| Dog `dog` | 0.9 × 0.3 × 0.6 | Random walk 1.5 m/s, running 8 m/s | Visual sector, half angle 90°, 200 m; acoustic hemisphere 250 m | Barks and approaches to 20 m |
| Vehicle `vehicle` (passenger car) | 4.5 × 1.8 × 1.5 | Along a path, 10 m/s | Visual forward sector, half angle 60°, 150 m; acoustic hemisphere 100 m | Keeps driving |
| Object `object` (bag or box) | 0.6 × 0.4 × 0.4 | Stationary | None | — |

A sensitivity range can be a sphere, hemisphere, cylinder or horizontal sector prism (directional), with a height interval, an azimuth
offset relative to the heading and a back attenuation. They can be edited directly in the target panel, or set with "Adopt suggested
radius", the value that the physical model suggests for the selected airframe.

## Discovery criterion

Each target selects one criterion mode (field `criterion_mode`):

| Mode | Criterion | Use |
|---|---|---|
| `geometric` (default) | The drone enters any enabled sensitivity range and stays for `t_dwell` | The range the visitor draws is exactly the boundary of discovery and of relay avoidance |
| `physical` (optional) | The drone is inside a sensitivity range and meets the visual criterion (line of sight, angular size, contrast and illuminance) or the acoustic criterion (margin of the arriving sound pressure per 1/3 octave band over the hearing threshold and background noise), staying for `t_dwell` | Study differences in airframe noise and visibility |

The dwell time is `t_dwell = 3·(1.2 − a)` s, where a is the target's alertness (0.6 s at a = 1). Meeting the criterion emits the event
`target.discovered`, which the interface shows as an alert with an acknowledge action. Background noise follows day and night profiles of
the session's acoustic environment (rural, suburban, urban) and the simulation calendar, and rises with precipitation. Relay monitoring
treats "zero discoveries" as a hard requirement.

## Sensors

| Sensor | Key facts |
|---|---|
| Zoom electro-optical `eo_gx40` | 1/2.8 inch CMOS, native 3840 × 2160, focal length 4.8 to 48 mm (10× optical); analysis resolution 1080P by default |
| Night vision `nir` | Passive low-light imaging, plus active 850 nm illumination (built-in illuminator or the external long-range illuminator `nir_ill_l`) |
| Thermal `lwir640` | Uncooled 640 × 512, NETD 50 mK, lenses of 13, 25, 75 and 100 mm; white-hot and black-hot palettes |
| Millimeter-wave radar `mmw77` | 77 GHz, reference range 180 m (RCS 1 m²), outputs tracks |
| Acoustic array `mic8` | 8 microphones, outputs bearing and per-band signal-to-noise ratio |
| LiDAR `mid360` | Range depends on reflectance, cut off at 100 m |

## Detection, recognition and identification

Perception by imaging sensors unifies three levels through the Johnson criteria and the target transfer probability function (TTPF): the
number of pixels across the critical dimension `N = d_c·f / (R·p')`, multiplied by factors for line of sight, contrast, illuminance and
motion blur, gives the effective pixels `N_eff`.

| Level | Meaning | Pixels for P ≥ 0.9 |
|---|---|---|
| D detection | Something is there | 3.5 |
| R recognition | Class identified (person, dog, vehicle, drone, object) | 14.0 |
| I identification | Individual features identified | 22.4 |

**Definite capture**: the same imaging sensor, for the same target, simultaneously has the target in its frustum, at least two thirds of the
lines of sight visible, and `N_eff ≥ N_req`, held for 1.0 s of simulation time. By default `N_req` is the pixel count for P ≥ 0.9 at the
required level (`level_req` of the target or task, R by default). The capture is lost after the conditions fail for 2 s. Radar, the acoustic
array and LiDAR only produce detections and cueing (turning the gimbal towards the detection bearing) and cannot constitute a definite
capture on their own.

The HUD of the FPV view shows the pixel progress towards the nearest target in the frustum (for example 9.6 / 14 px) and the current
limiting factor (frustum, line of sight, pixels, illuminance, contrast or blur), which explains why a capture has not happened yet.

## Perception quantification and field views

For each (target, drone) pair, the perception readings give the arriving sound pressure, threshold and margin, angular size, equivalent
pixels, contrast, and per-modality and combined display probabilities of detection (`GET /sessions/{sid}/perception/quant`). The criteria
themselves do not change; the display probability is calibrated so that the criterion boundary equals 0.5.

The field view in the viewport toolbar switches among none, acoustic, visual, thermal and radar: each drone shows a spherical scatter field
decreasing with distance (five gray-level bands and iso-shells), overlaid with the threshold shell of the selected target, which shows where a
drone would be heard or seen; the target's perception volume uses transparency for probability and point shape and gray level for the
modality. Physical quantities are computed on the server as profile tables; the browser only samples them deterministically and draws.

## Related endpoints

| Purpose | Endpoint |
|---|---|
| Manage targets | `GET/POST /sessions/{sid}/targets`, `GET/PATCH/DELETE /sessions/{sid}/targets/{id}` |
| Perception state and effective sensitivity volume | `GET /sessions/{sid}/targets/{id}/state` |
| Suggested radius | `GET /sessions/{sid}/targets/{id}/suggested_radius` |
| Per-drone perception and detection history | `GET /sessions/{sid}/vehicles/{vid}/perception`, `GET /sessions/{sid}/detections` |
| Target tracks (ground truth not exposed) | `GET /sessions/{sid}/tracks` |

The full endpoint table is in {doc}`../api/rest`; the example `tools/sdk/examples/04_targets_capture.py` places a target and waits for a
definite capture.
