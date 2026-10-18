"""6 种参考策略（r6 §12.3，M26-FR-081，D3-AC-18）：登记表与 r6 实测值、run_spec 派生与契约、补齐的 3 个参考行
（`hover1_plaza` → `hover_plaza`、`track3_nocomm` → `track_solo`、`track5_share` → 5 机 `track_share`）的确定性与行为
（合成世界、运动学开发档）。"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE.parents[0] / "world"))
sys.path.insert(0, str(HERE.parents[0] / "events"))
sys.path.insert(0, str(ROOT / "tools" / "sdk" / "drone4d" / "src"))

from drone4d.baselines import BASELINES, POLICIES, REFERENCE6, check_reference_spec, reference_run_spec  # noqa: E402

BASE_SPEC = ROOT / "configs/bench.d3/crop-handoff-001.l1-3uav.run_spec.json"
R6_ORDER = ["hover1_plaza", "hover3_anchors", "lawn3_cover", "track3_nocomm", "track3_share", "track5_share"]


# ------------------------------------------------------------------ 登记表（无仿真依赖）
def test_registry_matches_r6_table() -> None:
    assert list(REFERENCE6) == R6_ORDER
    assert {r.policy for r in REFERENCE6.values()} == set(POLICIES)            # 5 个策略类都有参考行
    assert [(r.policy, r.n_uav, r.comm) for r in REFERENCE6.values()] == [
        ("hover_plaza", 1, True), ("static_guard", 3, True), ("coverage_scan", 3, True), ("track_solo", 3, False), ("track_share", 3, True),
        ("track_share", 5, True)]
    for r in REFERENCE6.values():
        assert BASELINES[r.name] is POLICIES[r.policy]
        pe = r.r6["per_event"]
        assert abs(sum(pe.values()) / len(pe) - r.r6["ec"]) <= 0.01, r.name   # r6 逐事件捕获率均值 = EC（转录核对）
    assert REFERENCE6["lawn3_cover"].r6["ao_real"] == 0.0 and REFERENCE6["lawn3_cover"].r6["ao_pos"] == 0.92


def test_run_spec_policy_enum_covers_registry() -> None:
    d = json.loads((ROOT / "packages/contracts/bench/run_spec.v1.schema.json").read_text(encoding="utf-8"))
    assert set(BASELINES) <= set(d["properties"]["policy"]["properties"]["name"]["enum"])


def test_reference_run_spec_derivation() -> None:
    base = json.loads(BASE_SPEC.read_text(encoding="utf-8"))
    snap = copy.deepcopy(base)
    for name in R6_ORDER:
        rs = reference_run_spec(base, name)
        ref = REFERENCE6[name]
        assert check_reference_spec(rs) == []
        assert rs["policy"] == {"kind": "reference", "name": name}
        assert len(rs["fleet"]) == ref.n_uav and rs["comm"]["enabled"] is ref.comm
        ids = [f["uav_id"] for f in rs["fleet"]]
        assert len(set(ids)) == len(ids) and ids[: min(3, ref.n_uav)] == [f["uav_id"] for f in base["fleet"]][: ref.n_uav]
        assert {k: v for k, v in rs.items() if k not in ("fleet", "policy", "comm")} == {k: v for k, v in base.items() if k not in ("fleet", "policy", "comm")}
    assert base == snap                                                          # 不修改基础规格
    five = reference_run_spec(base, "track5_share")
    assert [f["start"]["pos_enu_m"] for f in five["fleet"]] == [[76.0, -78.0, 0.0], [76.0, -82.0, 0.0], [76.0, -86.0, 0.0], [76.0, -90.0, 0.0],
                                                                 [76.0, -94.0, 0.0]]   # 沿基础机队间隔延长
    assert [f["uav_id"] for f in five["fleet"]] == ["uav-01", "uav-02", "uav-03", "uav-04", "uav-05"]
    assert all(f["vehicle_ref"] == "x500" and f["start"]["yaw_deg"] == 90.0 for f in five["fleet"])
    pads = [[10.0, 0.0, 0.0]]
    assert reference_run_spec(base, "hover1_plaza", pads=pads)["fleet"][0]["start"]["pos_enu_m"] == pads[0]
    with pytest.raises(ValueError):
        reference_run_spec(base, "track5_share", pads=pads)
    bad = {**five, "fleet": five["fleet"][:3], "comm": {**five["comm"], "enabled": False}}
    assert len(check_reference_spec(bad)) == 2
    assert check_reference_spec({**five, "policy": {"kind": "image", "name": "track5_share"}}) != []
    assert check_reference_spec(base) == []                                      # 基础策略名不受行约束


def test_reference_run_spec_contract_valid() -> None:
    pytest.importorskip("jsonschema")
    from awr.agentsdk.schemas import bench_validator
    from awr.bench.spec_rules import check

    base = json.loads(BASE_SPEC.read_text(encoding="utf-8"))
    v = bench_validator("run_spec")
    base_viol = sorted(x.rule for x in check(base))
    for name in R6_ORDER:
        rs = reference_run_spec(base, name)
        assert not [e.message for e in v.iter_errors(rs)], name
        assert sorted(x.rule for x in check(rs)) == base_viol, name              # x-rules 组合矩阵与基础规格同结论


# ------------------------------------------------------------------ 合成世界运行（运动学开发档）
@pytest.fixture(scope="module")
def synth_event(tmp_path_factory):
    pytest.importorskip("open3d")
    pytest.importorskip("zstandard")
    from synthworld import build_synth_world
    from test_compile import EVENT, OVERLAY

    from awr.sim.events.compile import CompileOptions, compile_event

    tmp = tmp_path_factory.mktemp("ref6")
    mp = pytest.MonkeyPatch()
    w = build_synth_world(tmp, mp)
    ev = copy.deepcopy(EVENT)
    ev["annotations"] = [
        {"id": "meet_ab", "type": "proximity_encounter", "participants": ["person_a", "person_b"], "window": [5, 25]},
        {"id": "board_bc", "type": "board_vehicle", "participants": ["person_b", "vehicle_c"], "window": [70, 90]},
        {"id": "enter_c", "type": "enter_building", "participants": ["person_c"], "window": [35, 45], "params": {"anchor": "ev_door"}}]
    ev["brief"] = {"public_anchors": ["ev_plaza", "ev_door"],
                   "targets": [{"actor": "person_a", "description_en": "Blue top", "description_zh": "蓝色上衣"}]}
    d = tmp / "ev"
    d.mkdir()
    (d / "anchors.json").write_text(json.dumps(OVERLAY))
    (d / "event.json").write_text(json.dumps(ev))
    r = compile_event(d / "event.json", w, opts=CompileOptions(out=d / "v1", cache_dir=tmp / "cache"))
    assert r.status == "compiled", r.as_dict()
    yield w, d / "v1"
    mp.undo()


def _fleet(n: int):
    from awr.agentsdk.env.spec import FleetEntry

    return tuple(FleetEntry(f"uav-{i + 1:02d}", (-20.0 + 10.0 * i, 50.0, 0.0)) for i in range(n))


def _run(synth_event, out: Path, name: str, seed: int = 5, dur: float = 30.0) -> tuple[dict, dict]:
    """按参考行 `name` 的机数与链路开关运行一回合 → (manifest, {uav: agent})。"""
    from awr.agentsdk.env.core import BenchEnv
    from awr.agentsdk.env.link import CommCfg
    from awr.agentsdk.env.spec import EnvSpec

    w, ev = synth_event
    ref = REFERENCE6[name]
    spec = EnvSpec(f"t-{seed}", seed, w, ev, _fleet(ref.n_uav), physics="kinematic", duration_s=dur, out_dir=out,
                   comm=CommCfg(enabled=ref.comm), policy={"kind": "reference", "name": name})
    env = BenchEnv(spec)
    obs, infos = env.reset()
    ags = {u: BASELINES[name]() for u in env.possible_agents}
    for u, a in ags.items():
        a.reset(infos[u]["config"])
    while env.agents:
        obs, *_ = env.step({u: ags[u].step(o["obs"], o["inbox"]) for u, o in obs.items()})
    return env.close(), ags


def _acts(out: Path, u: str) -> list[dict]:
    return [json.loads(x) for x in (out / "agents" / u / "acts.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("name", ["hover1_plaza", "track3_nocomm", "track5_share"])
def test_added_references_deterministic(synth_event, tmp_path: Path, name: str) -> None:
    from awr.agentsdk.schemas import bench_validator

    m1, _ = _run(synth_event, tmp_path / "a", name)
    m2, _ = _run(synth_event, tmp_path / "b", name)
    uavs = [f"uav-{i + 1:02d}" for i in range(REFERENCE6[name].n_uav)]
    va = bench_validator("act")
    for u in uavs:
        a = (tmp_path / "a/agents" / u / "acts.jsonl").read_bytes()
        assert a == (tmp_path / "b/agents" / u / "acts.jsonl").read_bytes()               # 同一种子：动作流逐字节一致
        assert (tmp_path / "a/agents" / u / "obs.sha256").read_bytes() == (tmp_path / "b/agents" / u / "obs.sha256").read_bytes()
        for line in a.decode().splitlines():
            assert not list(va.iter_errors(json.loads(line)["act"]))
    assert m1["pose_sha256"] == m2["pose_sha256"] and m1["n_ticks"] == 300
    m3, _ = _run(synth_event, tmp_path / "c", name, seed=6)
    assert m3["pose_sha256"] != m1["pose_sha256"] or (tmp_path / "c/agents/uav-01/obs.sha256").read_bytes() != \
        (tmp_path / "a/agents/uav-01/obs.sha256").read_bytes()                             # 种子变化则观测或位姿变化


def test_hover_plaza_holds_zone_anchor(synth_event, tmp_path: Path) -> None:
    _, ags = _run(synth_event, tmp_path / "h", "hover1_plaza", dur=60.0)
    a = ags["uav-01"]
    zone = next(x for x in a.anchors if x["kind"] == "zone")                               # 合成事件的 ev_plaza（中心 (0, 60)）
    assert a.post == tuple(zone["pos_enu_m"][:2])
    assert a.phase == "mission" and abs(a.pos[0] - a.stand[0]) < 3.0 and abs(a.pos[1] - a.stand[1]) < 3.0
    looks = {tuple(r["act"]["gimbal"]["target_enu_m"][:2]) for r in _acts(tmp_path / "h", "uav-01") if r["act"].get("gimbal")}
    assert (a.post[0], a.post[1]) in looks and len(looks) >= 3                             # 以广场为中心扫视


def test_track_solo_never_communicates(synth_event, tmp_path: Path) -> None:
    _, ags = _run(synth_event, tmp_path / "n", "track3_nocomm", dur=60.0)
    for u, a in ags.items():
        rows = _acts(tmp_path / "n", u)
        assert not any(r["act"].get("msgs") for r in rows)
        assert not any(k["item"] == "msg" for r in rows for k in r["acks"])
        assert all(src == u for (_t, _xy, _v, src) in a.shared.values()) and a.claims == {}
