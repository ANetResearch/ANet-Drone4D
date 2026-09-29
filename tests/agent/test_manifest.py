"""M14-AC-008：能力清单（S3 四个成员与协调者通过 schema；sha 稳定；describe 往返逐字节相同；元能力齐全）。"""

from __future__ import annotations

import asyncio
import json

from fakes.s3 import build_s3, start
from fakes.schemas import errors

from awr.agent.anet_mock import identity as ID
from awr.agent.capabilities.catalog import META_CAPS
from awr.agent.capabilities.manifest import build_manifest, coordinator_manifest, manifest_sha256, vehicle_data
from awr.agent.runtime.evidence import canonical_json

MEMBERS = [("p600-a1", ["rgb.zoom"]), ("p600-b1", ["thermal.imaging"]), ("p600-b2", ["thermal.imaging"]),
           ("p600-b3", ["thermal.imaging"])]


def test_manifests_valid_and_stable() -> None:
    for vid, caps in MEMBERS:
        aid = ID.aid("newyork", vid)
        m1 = build_manifest(aid=aid, vehicle_id=vid, world_id="newyork", profile_id="p600_mid360", capabilities=caps)
        m2 = build_manifest(aid=aid, vehicle_id=vid, world_id="newyork", profile_id="p600_mid360", capabilities=caps)
        assert errors("agent/manifest.schema.json", m1) == []
        assert m1["manifest_sha256"] == m2["manifest_sha256"] == manifest_sha256(m1)
        ids = [s["id"] for s in m1["skills"]]
        assert set(META_CAPS) <= set(ids) and caps[0] in ids
    cm = coordinator_manifest(aid=ID.coordinator_aid("newyork"), world_id="newyork")
    assert errors("agent/manifest.schema.json", cm) == []
    assert {"blackboard.add", "blackboard.snapshot", "blackboard.conclude"} <= {s["id"] for s in cm["skills"]}


def test_physical_from_sensor_yaml() -> None:
    vd = vehicle_data("p600_mid360")
    assert vd is not None and vd.has_battery
    m = build_manifest(aid="bafyreix", vehicle_id="p600-b1", world_id="newyork", profile_id="p600_mid360",
                       capabilities=["thermal.imaging", "rgb.zoom"])
    th = next(s for s in m["skills"] if s["id"] == "thermal.imaging")
    sen = th["physical"]["sensor"]
    assert abs(sen["hfov_deg"] - 50.0) < 0.1  # 2·atan(320/686.3)
    assert sen["res_px"] == [640, 512] and sen["p0"] == 0.95 and sen["r_fp_m"] == 150
    assert th["physical"]["limits"]["wind_mps"] == 12.0  # min(目录 12, 机型 13.8)
    rgb = next(s for s in m["skills"] if s["id"] == "rgb.zoom")
    assert abs(rgb["physical"]["sensor"]["hfov_deg"] - 60.0) < 0.1 and rgb["physical"]["sensor"]["r_fp_m"] == 90
    x = vehicle_data("x500_sih")
    assert x is not None and not x.has_battery


def test_describe_roundtrip_bytes() -> None:
    sched, fake, core = build_s3()

    async def main() -> str:
        await start(core, fake)
        b1 = core.by_vehicle["p600-b1"]
        ix = await core.net.delegate(core.coord_aid, b1, "agent.describe", {}, task_id="T-probe")
        fut = asyncio.ensure_future(core.net.result(core.coord_aid, ix, 5.0))
        while not fut.done():
            fake.step_to(sched.now_ns() + 20_000_000)
            await sched.advance_to(sched.now_ns() + 20_000_000)
        res = fut.result()
        assert res.receipt_ok
        return res.effect.observed_state

    got = asyncio.run(main())
    man = core.agents[core.by_vehicle["p600-b1"]].describe()
    assert canonical_json(json.loads(got)) == canonical_json(man)
