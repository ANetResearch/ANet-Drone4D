"""车道图上的车辆运动（D3-AC-33 运动条款；M23-AC-031、M23-AC-040 的生成与验证部分；M23 §15.4、M23-FR-056、FR-075）。

合成路网（世界帧 m，直接构造 `OsmRoads` 经 M22 `build_lane_graph` 生成车道图，右侧通行、缺省每向 1 车道）：
- 十字路口：东西向 primary（主路，13.9 m/s）与南北向 residential（次路，8.3 m/s）交于 n-2；
- 同级路口：两条 residential 相交（先到先行）；
- 停车让行：同级路口的北行方向在 n-6 有 `highway=stop`（`stop_sign`）；
- 直路：单条 residential（跟驰）；
- 锐角路口：两条道路以约 11° 相交（车道端部重叠，路口单元向车道延伸）。

验证：IDM 参数与缺省值；自由流限速与弯道限速；跟驰最小车距 ≥ 2 m 与静止车距；次路让主路（准入时主路车到达时间 > 4 s）；
同级先到先行与 idx 决胜；停车让行先停稳；泊车与 `engine_on`；关键事件 `depart`、`turn{intersection}`、`stop`、`park` 由磁带
复算并与生成一致；验证器对越出车道、越出走廊、车距、穿越、冲突单元同占与准入记录篡改的检出；同一种子字节相同的确定性与
流 19 通道登记；crop 实数据（有数据时）：车辆沿车道图运动、100% 在道路走廊内、最小车距 ≥ 2 m、无穿越、路口无冲突。
"""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import numpy as np
import pytest

from awr.sim.events import vehicle as V
from awr.sim.sensors import cbrng
from awr.world.glb.derive import lanes as L

ROOT = Path(__file__).resolve().parents[3]
CROP = ROOT / "worlds" / "oryahovo-crop1"
CROP_OSM = ROOT / "data" / "raw" / "d3-glb" / "osm" / "oryahovo_map.osm"


def _zfn(xy: np.ndarray) -> np.ndarray:
    return 0.01 * xy[:, 0] + 1.0


def _graph(nodes: dict, ways: list, node_tags: dict | None = None) -> L.LaneGraph:
    roads = L.OsmRoads(nodes={k: (float(x), float(y)) for k, (x, y) in nodes.items()}, ways=ways, node_tags=dict(node_tags or {}))
    return L.LaneGraph(L.build_lane_graph(roads, z_fn=_zfn).doc)


@pytest.fixture(scope="module")
def cross() -> tuple[L.LaneGraph, V.Network]:
    g = _graph({1: (-300, 0), 2: (0, 0), 3: (300, 0), 4: (0, -200), 5: (0, 200)},
               [(100, {"highway": "primary"}, [1, 2, 3]), (101, {"highway": "residential"}, [4, 2, 5])])
    return g, V.Network(g)


@pytest.fixture(scope="module")
def equal() -> tuple[L.LaneGraph, V.Network]:
    g = _graph({1: (-200, 0), 2: (0, 0), 3: (200, 0), 4: (0, -200), 5: (0, 200)},
               [(100, {"highway": "residential"}, [1, 2, 3]), (101, {"highway": "residential"}, [4, 2, 5])])
    return g, V.Network(g)


def _route(g: L.LaneGraph, *pts) -> L.LaneRoute:
    r = V.chain_route(g, list(pts))
    assert r is not None
    return r[0]


def _sim(g, net, specs, ms):
    sim = V.simulate(g, specs, ms, net=net)
    au = V.audit(g, sim.routes, sim.tape(), log=sim.log, ids=sim.ids, net=net)
    return sim, au


def _ok(au: dict) -> None:
    assert au["ok"], (au["counts"], {k: v[:3] for k, v in au["violations"].items() if v})


# ---------------------------------------------------------------- 参数


def test_drive_params_defaults_profiles_and_idm() -> None:
    p = V.drive_params(None)
    assert (p.T_s, p.s0_m, p.a_mps2, p.b_mps2, p.v0_scale, p.v0_mps) == (1.5, 2.0, 1.5, 2.0, 1.0, None)    # M23 §15.4 缺省
    assert V.drive_params({"profile": "cautious"}).T_s > p.T_s > V.drive_params({"profile": "aggressive"}).T_s
    q = V.drive_params({"profile": "urban", "idm": {"v0_mps": 9.0, "T_s": 1.2, "s0_m": 2.5}})
    assert (q.v0_mps, q.T_s, q.s0_m, q.a_mps2) == (9.0, 1.2, 2.5, 1.5)
    for bad in ({"idm": {"s0_m": 1.5}}, {"profile": "racing"}, {"idm": {"T_s": 0}}, {"idm": {"jerk": 1}}):
        with pytest.raises(ValueError):
            V.drive_params(bad)
    assert V.idm_accel(10.0, 10.0, None, 0.0, p) == 0.0                       # 自由流在 v0 处平衡
    assert V.idm_accel(0.0, 10.0, 2.0, 0.0, p) == 0.0                         # 静止于 s0 处平衡
    assert V.idm_accel(0.0, 10.0, None, 0.0, p) == p.a_mps2
    assert V.idm_accel(10.0, 10.0, 5.0, 10.0, p) == -9.0                      # 下限
    s_eq = p.s0_m + 8.0 * p.T_s                                                # 稳态跟驰间距 s0 + vT 处 a ≈ −a(v/v0)^4
    assert V.idm_accel(8.0, 13.9, s_eq, 0.0, p) == pytest.approx(-1.5 * (8.0 / 13.9) ** 4, abs=1e-12)


# ---------------------------------------------------------------- 限速与弯道


def test_free_flow_speed_limits_and_curve_limit(cross) -> None:
    g, net = cross
    r = _route(g, (-280.0, -1.6), (1.6, 150.0))                              # 东行后左转北行
    assert any(g.segs[k].props.get("turn") == "left" for k in r.segments)
    specs = [V.VehicleSpec("a", r), V.VehicleSpec("b", r, 40_000, V.drive_params({"idm": {"v0_mps": 8.0}})),
             V.VehicleSpec("c", r, 80_000, V.drive_params({"profile": "cautious"}))]
    sim, au = _sim(g, net, specs, 140_000)
    _ok(au)
    P = V._Path(g, r, net)
    on_primary = [(sim.u[:, i] > 0) & (sim.u[:, i] < P.U[1] - 60.0) for i in range(3)]
    assert sim.v[on_primary[0], 0].max() == pytest.approx(13.9, abs=0.05)
    assert sim.v[:, 0].max() <= g.segs[r.segments[0]].speed + 1e-9
    assert sim.v[:, 1].max() <= 8.0 + 1e-9 and sim.v[on_primary[1], 1].max() > 7.9
    assert sim.v[:, 2].max() <= 0.9 * 13.9 + 1e-9
    c = r.segments[1]
    vlim = math.sqrt(V.A_LAT * g.segs[c].props["radius_min_m"])
    on_c = (sim.u[:, 0] > P.U[1]) & (sim.u[:, 0] <= P.U[1] + P.lens[1])
    assert on_c.any() and sim.v[on_c, 0].max() <= vlim + 1e-9 and au["stats"]["curve_speed_ratio_max"] <= 1.0 + 0.2 / vlim
    assert (sim.state[-1] == V.STATE_GONE).all()                             # 到达终点后消失
    assert sim.log["max_decel_mps2"] <= 3.0


# ---------------------------------------------------------------- 跟驰


def test_platoon_min_gap_standstill_and_dwell_stop() -> None:
    g = _graph({1: (-400, 0), 2: (400, 0)}, [(100, {"highway": "residential"}, [1, 2])])
    net = V.Network(g)
    r = _route(g, (-390.0, -1.6), (390.0, -1.6))
    specs = [V.VehicleSpec("lead", r, 0, stops=((250.0, 30_000),))] + [V.VehicleSpec(f"f{i}", r, 2000 * i) for i in range(1, 6)]
    sim, au = _sim(g, net, specs, 150_000)
    _ok(au)
    assert au["stats"]["min_gap_m"] >= V.MIN_GAP_M and au["stats"]["overlaps"] == 0
    lg = sim.log
    assert lg["gap_clamps"] == 0 and lg["safety_stops"] == 0 and lg["max_decel_mps2"] <= 4.0
    assert [s["actor"] for s in lg["stops"]] == ["lead"] and abs(lg["stops"][0]["u_m"] - 250.0) < 1e-9
    k_stop = lg["stops"][0]["t_ms"] // 100
    assert 250.0 - V.STOP_TOL_M <= sim.u[k_stop, 0] <= 250.0
    # 车队在领车停车期间全部停住，静止车距 ∈ [2, 3] m
    k = k_stop + 250
    assert (sim.v[k] == 0).all()
    gaps = [sim.u[k, i] - 4.5 - sim.u[k, i + 1] for i in range(5)]
    assert all(2.0 <= x <= 3.0 for x in gaps), gaps
    ev = V.key_events(g, sim.routes, sim.tape(), ids=sim.ids, net=net)
    stops = {e["actor"]: e for e in ev if e["type"] == "stop"}
    assert stops["lead"]["params"]["cause"] == "dwell" and stops["lead"]["interval_ms"][0] == lg["stops"][0]["t_ms"]
    assert stops["lead"]["interval_ms"][1] - stops["lead"]["interval_ms"][0] >= 30_000 - 100
    assert all(stops[f"f{i}"]["params"]["cause"] == "queue" for i in range(1, 6))
    assert sorted(e["actor"] for e in ev if e["type"] == "depart") == sorted(sim.ids)


# ---------------------------------------------------------------- 路口让行


def test_minor_yields_to_major_stream(cross) -> None:
    g, net = cross
    east = _route(g, (-280.0, -1.6), (280.0, -1.6))
    north = _route(g, (1.6, -180.0), (1.6, 180.0))
    majors = [V.VehicleSpec(f"M{i}", east, 10_000 * i, v_init=13.9) for i in range(7)]
    minors = [V.VehicleSpec(f"m{i}", north, 4000 + 1000 * i) for i in range(3)]
    sim, au = _sim(g, net, majors + minors, 120_000)
    _ok(au)
    assert au["stats"]["junction_traversals"] == 10 and au["stats"]["order_checked"]
    # 主路车不为次路车减速
    for i in range(7):
        vis = sim.state[:, i] == V.STATE_MOVING
        assert sim.v[vis, i].min() > 13.0
    # 次路车准入时，主路车到达本单元冲突区的时间 > 4 s（生成侧精确量）
    paths = [V._Path(g, s.route, net) for s in sim.specs]
    for e in sim.log["commits"]:
        if not e["actor"].startswith("m"):
            continue
        k = e["t_ms"] // 100
        for i in range(7):
            if sim.state[k, i] != V.STATE_MOVING:
                continue
            for j2, c2, u_in, _uo in paths[i].units:
                if c2 in net.conflicts[e["conn"]] and paths[i].U[j2] >= sim.u[k, i]:
                    assert (u_in - sim.u[k, i]) / sim.v[k, i] > V.GAP_S
    waits = [e["t_ms"] - e["arr_ms"] for e in sim.log["commits"] if e["actor"].startswith("m")]
    assert max(waits) > 0 and sim.log["denied"].get("gap", 0) > 0


def test_equal_junction_first_come_and_idx_tiebreak(equal) -> None:
    g, net = equal
    north_near = _route(g, (1.6, -40.0), (1.6, 150.0))
    east_far = _route(g, (-70.0, -1.6), (150.0, -1.6))
    sim, au = _sim(g, net, [V.VehicleSpec("B", east_far), V.VehicleSpec("A", north_near)], 60_000)
    _ok(au)
    c = [e for e in sim.log["commits"]]
    assert [e["actor"] for e in c] == ["A", "B"] and c[0]["arr_ms"] < c[1]["arr_ms"]
    assert c[0]["conn"] in net.conflicts[c[1]["conn"]]
    # 对称到达（同一帧）：按 idx
    north = _route(g, (1.6, -60.0), (1.6, 150.0))
    east = _route(g, (-60.0, -1.6), (150.0, -1.6))
    for order in (("E", "N"), ("N", "E")):
        specs = [V.VehicleSpec(x, east if x == "E" else north) for x in order]
        sim, au = _sim(g, net, specs, 60_000)
        _ok(au)
        c = sim.log["commits"]
        assert c[0]["arr_ms"] == c[1]["arr_ms"] and [e["actor"] for e in c] == list(order)


def test_stop_sign_requires_full_stop() -> None:
    g = _graph({1: (-200, 0), 2: (0, 0), 3: (200, 0), 4: (0, -200), 6: (0, -30), 5: (0, 200)},
               [(100, {"highway": "residential"}, [1, 2, 3]), (101, {"highway": "residential"}, [4, 6, 2, 5])],
               {6: {"highway": "stop", "direction": "forward"}})
    net = V.Network(g)
    assert net.stop_sign
    r = _route(g, (1.6, -150.0), (1.6, 150.0))
    assert any(c in net.stop_sign for c in r.segments)
    sim, au = _sim(g, net, [V.VehicleSpec("a", r)], 60_000)
    _ok(au)
    P = V._Path(g, r, net)
    _j, _c, u_in, _ = next(x for x in P.units if x[1] in net.stop_sign)
    k_in = int(np.flatnonzero(sim.u[:, 0] > u_in)[0])
    before = (sim.u[:k_in, 0] >= u_in - V.STOP_TOL_M) & (sim.v[:k_in, 0] == 0.0)
    assert before.any()                                                        # 停止线前 0.5 m 内停稳后才驶入
    assert sim.log["commits"][0]["t_ms"] >= int(np.flatnonzero(before)[0]) * 100


# ---------------------------------------------------------------- 泊车、标志与关键事件


def test_park_wait_engine_flags_and_key_events(cross) -> None:
    g, net = cross
    p_route = _route(g, (-250.0, -1.6), (1.6, 120.0))                        # 东行左转北行，终点泊车
    w_route = _route(g, (-1.6, 150.0), (-120.0, 1.6))                         # 南行右转西行，终点停住
    specs = [V.VehicleSpec("P", p_route, end="park"), V.VehicleSpec("W", w_route, 30_000, end="wait")]
    sim, au = _sim(g, net, specs, 260_000)
    _ok(au)
    (pk,) = sim.log["parks"]
    kp = pk["t_ms"] // 100
    f = sim.flags & V.FLAG_ENGINE
    assert (f[sim.state[:, 0] == V.STATE_MOVING, 0] == 1).all() and (f[kp:, 0] == 0).all() and f[kp - 1, 0] == 1
    assert (sim.state[kp:, 0] == V.STATE_WAITING).all()
    # W 在终点停住：停车后 120 s 内 engine_on = 1，之后为 0
    ks = int(np.flatnonzero((sim.v[:, 1] == 0) & (sim.u[:, 1] >= sim.u[-1, 1] - 1e-9) & (sim.state[:, 1] == V.STATE_WAITING))[0])
    t_last_move = (ks - 1) * 100
    k_off = int(np.flatnonzero(f[ks:, 1] == 0)[0]) + ks
    assert k_off * 100 - t_last_move == V.ENGINE_IDLE_MS + 100
    ev = V.key_events(g, sim.routes, sim.tape(), ids=sim.ids, net=net)
    by = {(e["type"], e["actor"]): e for e in ev}
    assert by[("park", "P")]["interval_ms"] == [pk["t_ms"], pk["t_ms"]]
    assert ("park", "W") not in by and by[("stop", "W")]["params"]["cause"] == "dwell"
    assert by[("stop", "W")]["interval_ms"][1] == 259_900
    tp, tw = by[("turn", "P")]["params"], by[("turn", "W")]["params"]
    assert (tp["turn"], tw["turn"]) == ("left", "right") and tp["intersection"] == tw["intersection"] == "j-2"
    assert 80.0 < tp["heading_change_deg"] < 100.0 and -100.0 < tw["heading_change_deg"] < -80.0
    assert by[("depart", "P")]["interval_ms"][0] == 0 and by[("depart", "W")]["interval_ms"][0] == 30_000
    tr = sim.track(0)
    assert set(tr) == {"xy", "vxy", "state", "container", "z_hint", "flags"} and tr["container"].dtype == np.int64
    vis = sim.state[:, 0] > 0
    assert np.allclose(tr["z_hint"][vis], _zfn(tr["xy"][vis]), atol=0.05)    # 高度取车道图逐顶点高度
    sim2 = V.simulate(g, specs, 20_000, net=net, z_fn=lambda xy: np.full(len(xy), 7.0))
    assert (sim2.z == 7.0).all()


def test_key_events_match_generator(cross) -> None:
    g, net = cross
    specs = V.background(g, 30, 11, depart_window_ms=60_000, net=net)
    sim, au = _sim(g, net, specs, 150_000)
    _ok(au)
    ev = V.key_events(g, sim.routes, sim.tape(), ids=sim.ids, net=net)
    got = {(e["actor"], e["params"]["connector"]): e["interval_ms"] for e in ev if e["type"] == "turn"}
    want = {}
    for i, s in enumerate(sim.specs):
        P = V._Path(g, s.route, net)
        for j, c in enumerate(P.route):
            if g.segs[c].props.get("turn") in ("left", "right", "uturn") and net.junction_of.get(c):
                u = sim.u[:, i]
                ins, outs = np.flatnonzero(u > P.U[j] + 1e-6), np.flatnonzero(u >= P.U[j] + P.lens[j] - 1e-6)
                if len(ins) and len(outs):
                    want[(s.id, c)] = [int(ins[0]) * 100, int(outs[0]) * 100]
    assert got.keys() == want.keys() and len(want) >= 10
    assert all(abs(got[k][0] - want[k][0]) <= 100 and abs(got[k][1] - want[k][1]) <= 100 for k in want)
    dep = {e["actor"]: e["interval_ms"][0] for e in ev if e["type"] == "depart"}
    assert dep == {s["actor"]: s["t_ms"] for s in sim.log["spawns"]}           # 静止出生，下一帧开始移动


# ---------------------------------------------------------------- 确定性


def test_determinism_and_rng_channels(cross) -> None:
    g, net = cross
    assert cbrng.CHANNELS[(V.STREAM, "vehicle")] == (24, 27) and cbrng.check_channels() == []
    a = V.background(g, 20, 5, net=net)
    b = V.background(g, 20, 5, net=net)
    c = V.background(g, 20, 6, net=net)
    key = [(s.id, s.depart_ms, s.route.segments, s.route.s_start) for s in a]
    assert key == [(s.id, s.depart_ms, s.route.segments, s.route.s_start) for s in b]
    assert key != [(s.id, s.depart_ms, s.route.segments, s.route.s_start) for s in c]
    assert all(s.depart_ms % 100 == 0 for s in a) and len(a) == 20
    s1 = V.simulate(g, a, 90_000, net=net)
    s2 = V.simulate(g, b, 90_000, net=V.Network(g))
    assert s1.digest() == s2.digest() and np.array_equal(s1.xy, s2.xy) and s1.log == s2.log
    s3 = V.simulate(g, c, 90_000, net=net)
    assert s3.digest() != s1.digest()
    a2 = copy.copy(a)
    a2[0] = V.VehicleSpec(a[0].id, a[0].route, a[0].depart_ms + 100)
    assert V.simulate(g, a2, 90_000, net=net).digest() != s1.digest()


# ---------------------------------------------------------------- 验证器检出


def test_audit_detects_violations(cross) -> None:
    g, net = cross
    east = _route(g, (-150.0, -1.6), (150.0, -1.6))
    north = _route(g, (1.6, -100.0), (1.6, 100.0))
    b = V.simulate(g, [V.VehicleSpec("m", north, 0)], 40_000, net=net)
    for dep in range(0, 20_000, 100):                                          # 主路车出发时刻：不交互的单车生成拼接后两车身相交
        specs = [V.VehicleSpec("M", east, dep, v_init=13.9), V.VehicleSpec("m", north, 0)]
        a = V.simulate(g, specs[:1], 40_000, net=net)
        tape = {"xy": np.concatenate([a.tape()["xy"], b.tape()["xy"]], 1), "state": np.concatenate([a.state, b.state], 1)}
        bad = V.audit(g, [east, north], tape, ids=["M", "m"], net=net)
        if bad["counts"]["overlap"] > 0:
            break
    else:
        pytest.fail("no meeting departure")
    # 不交互的拼接：两车同时占用冲突单元并穿越；无准入记录时不做次序检查
    assert not bad["ok"] and bad["counts"]["conflict"] > 0 and not bad["stats"]["order_checked"]
    sim, au = _sim(g, net, specs, 40_000)                                      # 交互生成：同样的出发时刻下让行无冲突
    _ok(au)
    # 越出车道与越出走廊
    t = sim.tape()
    t1 = {**t, "xy": t["xy"].copy()}
    t1["xy"][100:110, 0, 1] += 0.5
    r1 = V.audit(g, sim.routes, t1, ids=sim.ids, net=net)
    assert r1["counts"]["off_lane"] == 1 and r1["counts"]["corridor"] == 0
    t2 = {**t, "xy": t["xy"].copy()}
    t2["xy"][100:110, 0, 1] += 30.0
    assert V.audit(g, sim.routes, t2, ids=sim.ids, net=net)["counts"]["corridor"] == 1
    # 准入记录篡改：删除准入、把次路车准入时刻提前到主路车驶近时
    lg = copy.deepcopy(sim.log)
    lg["commits"] = [e for e in lg["commits"] if e["actor"] != "m"]
    assert V.audit(g, sim.routes, t, log=lg, ids=sim.ids, net=net)["counts"]["uncommitted"] == 1
    lg = copy.deepcopy(sim.log)
    em = next(e for e in lg["commits"] if e["actor"] == "m")
    _j, _c, u_in, u_out = V._Path(g, east, net).units[0]
    k_occ = int(np.flatnonzero((sim.u[:, 0] > u_in) & (sim.u[:, 0] < u_out))[0])
    em["t_ms"] = em["arr_ms"] = k_occ * 100                                     # 主路车占用冲突单元时次路车准入
    r3 = V.audit(g, sim.routes, t, log=lg, ids=sim.ids, net=net)
    assert r3["counts"]["order"] >= 1 and "occupied" in {x["why"] for x in r3["violations"]["order"]}
    # 主路车未准入、距冲突单元 ETA < 3.5 s 时次路车准入：可接受间隙违例
    k_gap = int(np.flatnonzero((sim.u[:, 0] > u_in - 3.0 * 13.9) & (sim.state[:, 0] == 1))[0])
    lg = copy.deepcopy(sim.log)
    lg["commits"] = [e for e in lg["commits"] if e["actor"] != "M"]
    em = next(e for e in lg["commits"] if e["actor"] == "m")
    em["t_ms"] = em["arr_ms"] = k_gap * 100
    r4 = V.audit(g, sim.routes, t, log=lg, ids=sim.ids, net=net)
    assert "gap" in {x["why"] for x in r4["violations"]["order"]} and r4["counts"]["uncommitted"] == 1


def test_audit_detects_gap_violation() -> None:
    g = _graph({1: (-300, 0), 2: (300, 0)}, [(100, {"highway": "residential"}, [1, 2])])
    net = V.Network(g)
    r = _route(g, (-290.0, -1.6), (290.0, -1.6))
    specs = [V.VehicleSpec("lead", r, 0, stops=((150.0, 10_000),)), V.VehicleSpec("f", r, 3000)]
    sim, au = _sim(g, net, specs, 80_000)
    _ok(au)
    t = sim.tape()
    k = sim.log["stops"][0]["t_ms"] // 100 + 60
    assert (sim.v[k] == 0).all()
    t["xy"] = t["xy"].copy()
    t["xy"][k, 1, 0] += 1.0                                                    # 后车前移 1 m：车距 < 2 m
    r1 = V.audit(g, sim.routes, t, ids=sim.ids, net=net)
    assert r1["counts"]["gap"] == 1 and r1["violations"]["gap"][0]["gap_m"] < 2.0 - 0.9
    t["xy"][k, 1, 0] += 2.5                                                    # 再前移 2.5 m：车身相交
    r2 = V.audit(g, sim.routes, t, ids=sim.ids, net=net)
    assert r2["counts"]["overlap"] >= 1


# ---------------------------------------------------------------- 路线与锐角路口


def test_chain_route_waypoints(cross) -> None:
    g, _net = cross
    r, us = V.chain_route(g, [(-250.0, -1.6), (-100.0, -1.6), (1.6, 100.0)])
    assert len(us) == 3 and us[0] == 0.0 < us[1] < us[2] == pytest.approx(r.length_m)
    for a, b in zip(r.segments[:-1], r.segments[1:], strict=True):
        assert b in g.segs[a].succ
    assert us[1] == pytest.approx(150.0, abs=1e-6)
    with pytest.raises(ValueError):
        V.chain_route(g, [(-250.0, -1.6)])


def test_acute_junction_units_extend_into_lanes() -> None:
    g = _graph({1: (-250, 0), 2: (0, 0), 3: (250, 0), 4: (-250, -50), 7: (0, 200)},
               [(100, {"highway": "primary"}, [1, 2, 3]), (101, {"highway": "secondary"}, [4, 2, 7])])
    net = V.Network(g)
    assert max(net.ext_in.values()) > 0.0                                     # 锐角路口：车道端部重叠，单元向车道延伸
    specs = V.background(g, 20, 3, depart_window_ms=60_000, net=net)
    sim, au = _sim(g, net, specs, 180_000)
    _ok(au)
    assert sim.log["safety_stops"] == 0 and sim.log["gap_clamps"] == 0
    assert all(x["since_ms"] >= 180_000 - 60_000 for x in sim.log["pending_at_end"])     # 无长时间等待（无死锁）
    assert (sim.state[-1] == V.STATE_GONE).sum() >= 10 and au["stats"]["junction_traversals"] >= 20


# ---------------------------------------------------------------- crop 实数据


@pytest.mark.skipif(not (CROP / "world.json").is_file() or not CROP_OSM.is_file(), reason="data: worlds/oryahovo-crop1 and the crop OSM extract")
def test_crop_traffic_on_lane_graph() -> None:
    """D3-AC-33：crop 车道图上 60 辆背景车 180 s：沿车道图运动、100% 在道路走廊内、最小车距 ≥ 2 m、无穿越、路口无冲突同占与
    让行违例；同一种子两次生成字节相同。"""
    from awr.world.georef.types import Anchor
    from awr.world.package.validate import coverage_mask
    from awr.world.terrain.grids import Grid, read_grid

    world = json.loads((CROP / "world.json").read_text())
    pub = next((x for x in world["layers"] if x.get("format") == L.FORMAT), None)
    if pub is not None:
        g = L.LaneGraph.load(CROP / pub["href"])
    else:
        coord = json.loads((CROP / "coordinate.json").read_text())
        sc, a = read_grid(CROP / "geometry" / "terrain" / "dsm_2m.json")
        dsm = Grid(a, tuple(sc["originXY"]), sc["cellM"])
        scn, cnt = read_grid(CROP / "geometry" / "terrain" / "dsm_2m_n.json")
        roads = L.read_osm(CROP_OSM, Anchor.from_coordinate(coord))
        g = L.LaneGraph(L.build_lane_graph(roads, inside=L.cover_fn(coverage_mask(cnt, 2), scn["originXY"], scn["cellM"]),
                                           z_fn=lambda xy: dsm.bilinear(xy[:, 0], xy[:, 1])).doc)
    net = V.Network(g)
    specs = V.background(g, 60, 33, depart_window_ms=60_000, net=net)
    assert len(specs) == 60
    sim, au = _sim(g, net, specs, 180_000)
    _ok(au)
    st = au["stats"]
    assert st["corridor_samples_frac"] == 1.0 and st["lat_max_m"] <= 0.002 and st["min_gap_m"] >= V.MIN_GAP_M and st["overlaps"] == 0
    assert st["junction_traversals"] >= 100 and sim.log["safety_stops"] == 0
    assert V.simulate(g, specs, 180_000, net=net).digest() == sim.digest()
    ev = V.key_events(g, sim.routes, sim.tape(), ids=sim.ids, net=net)
    assert sum(e["type"] == "turn" for e in ev) >= 50 and sum(e["type"] == "depart" for e in ev) == 60
