from awr.sim.activities.life import VERSION


def ring_population(n=12):
    nodes, edges, facilities, actors = {}, {}, {}, []
    # Separate purposeful routes isolate scheduling fairness from traffic congestion.
    for i in range(n):
        prefix = f"a{i:03d}"
        for j, p in enumerate(([0, i * 12, 0], [40, i * 12, 0], [40, i * 12 + 6, 0], [0, i * 12 + 6, 0])):
            key = f"{prefix}-{j}"
            nodes[key] = {"pos": p}
            facilities[key] = {
                "node": key,
                "label": key,
                "profiles": ["person"],
                "capacity": 1,
                "indoor": j == 1,
                "portal_verified": True,
            }
        for j in range(4):
            a, b = f"{prefix}-{j}", f"{prefix}-{(j + 1) % 4}"
            edges[a] = {
                "from": a,
                "to": b,
                "points": [nodes[a]["pos"], nodes[b]["pos"]],
                "verified": True,
                "profiles": ["person"],
                "clearance_m": 1,
                "speed_mps": 2,
            }
        actors.append(
            {
                "id": prefix,
                "start": f"{prefix}-0",
                "profile": "person",
                "radius_m": 0.4,
                "speed_mps": 1.5,
                "itinerary": [
                    {"facility": f"{prefix}-1", "purpose": "shopping", "dwell_s": 5},
                    {"facility": f"{prefix}-3", "purpose": "rest", "dwell_s": 2},
                ],
            }
        )
    return {
        "version": VERSION,
        "status": "verified",
        "seed": 7,
        "nodes": nodes,
        "edges": edges,
        "facilities": facilities,
        "actors": actors,
    }
