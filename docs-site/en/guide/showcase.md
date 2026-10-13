# Shared show

The shared show is a read-only session: all visitors watch the same continuously running simulation, and watching does not take a
sandbox slot. On the public instance, "Enter shared show" on the landing page opens the shared show at `/world/synthcity`; locally,
`make run` also starts the shared show (profile `demo`).

## What runs

| Deployment | World and scenario | Content |
|---|---|---|
| Public instance | ANet Synthetic City, scenario S7 | Area guard: 2 nests (a nest box and a VTOL pad), 6 P600s and 2 AWR-V1s running an `area_guard` task; 2 pedestrians and 1 car in the area, handed over to 7x24 relay monitoring once discovered. The simulation calendar follows real time in the Asia/Shanghai time zone, so day and night change accordingly |
| Local, without the six cities | ANet Synthetic City, scenario S0 | Panorama: seven P600s spiral-scan the 318 m ANet Tower, two cover city blocks, three cruise along the river in a V formation, and the weather turns from clear to light rain and then fog |
| Local, with the six cities built | Shenzhen, scenario S1 | Two P600s spiral down the facade of the tallest tower in a 6 m/s southeast wind |

Locally, `AWR_WORLD` and `AWR_SCENARIO` select the world and scenario, for example
`AWR_WORLD=synthcity AWR_SCENARIO=s0-synthcity-showcase make run`. The other built-in scenarios are S2 Shanghai formation, S3 New York
search and rescue (agent collaboration), S4 Chicago lakeshore, S5 San Francisco terrain following and S6 Suzhou corridor.

## Available interactions

Interactions in the shared show run in the browser or use read-only subscriptions, and remain available while the visitor is queued:

| Interaction | Description |
|---|---|
| Camera | Free, follow, third-person and FPV views; selecting a drone follows it, and the focused drone uses a low-latency channel |
| Sensor views | FPV of any drone, plus daylight, night vision and thermal sensor views; the HUD shows the pixel progress `N_eff / N_req` towards the nearest target in the frustum and the current limiting factor |
| Targets | List and details (read-only); the sensitivity range can be shown as the configured volume or as the effective volume for the selected airframe |
| Relay statistics | Coverage, monitoring gaps, discovery count and handovers |
| Airframe catalog | Airframe cards and parameter tables; reference airframes are labeled as simulated reference values |
| GSD calculator | Choose an airframe, sensor, focal length, target class and distance to get the ground sampling distance, pixels on the critical dimension, the level probabilities and the feasible window |
| Field views | The viewport toolbar switches between acoustic, visual, thermal and radar emission fields (see {doc}`targets`) |
| Timeline | The clock of the shared show is driven by the server; visitors cannot pause it or change its rate |

The role badge reads "Public demo · read-only". To place drones or targets or issue tasks, open your own sandbox ({doc}`sandbox`).

## The other six worlds: each city's own simulated monitoring demonstration

Opening `/world/<id>` for one of the six UrbanScene3D cities (for example `/world/shanghai`) plays back that city's own simulated drone
monitoring demonstration: its own fleet, nests, targets and tasking, generated and verified against that city's geometry, recorded once
and then replayed client-side on a continuous loop. No realtime connection is opened for it; the role badge reads "Simulated replay ·
read-only" with a reminder that it is an offline recording, and you still open your own sandbox to issue commands in that city. A world
with no recorded demonstration of its own (for instance a world package built locally without the packaged recording) falls back to
**static browsing**: only the point cloud and terrain load. The ids of the seven worlds are `synthcity`, `shenzhen`, `shanghai`,
`newyork`, `sanfrancisco`, `suzhou` and `chicago`.

## Viewer seats and queueing

Live viewing connections are limited by a seat cap (24 on the public instance); when full, visitors join the viewer queue and see their
position and the estimated wait. Static browsing, a city's own recorded replay, the landing page and visitors in power saving take no
seat. After 5 min without
interaction (2 min when the seats are full and someone is queued) or 60 s hidden, the page enters power saving: it closes the realtime
connection, keeps the last frame and releases the seat; any interaction reconnects.

## Rendering and quality

The point cloud, weather and interface are rendered in the browser. The quality controller adjusts the point budget from the measured frame
time and switches among 7 quality tiers, so the same world runs on a software renderer as well as on a desktop GPU; sharpness and frame
rate depend on the visitor's device.
