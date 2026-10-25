"""D3-AC-08 Exogenous 隔离（M23-AC-021、G-M23-3；M23-FR-047、FR-050）：同一磁带、6 种机队策略（悬停于演员出发点 10 m 内、
盘旋、跟踪、扫描、远离，另加不起飞的对照）下，sim-core（M18 timeline 识别物）中三名脚本演员在磁带 tick 上的毫米定点实现轨迹与
磁带逐 tick 一致，`event_run.scripted_track_sha256` 与由磁带独立复算的哈希相同且与策略无关；M18"被发现"照常计数，并与
`target.discovered` 事件、`tgt_disc_n` 列一起写入 `event_run` 的 `discovered` 与 `discovered_count`；会话段 `event` 与
`targets` 的重复 id 被拒绝。`reaction_policy` 缺省值下 D2 行为逐位不变由 D3-AC-01 覆盖，本文件只断言无运动源时不记事件度量。

磁带为构造用例（不经编译器；世界为地面高 1 m 的平地，person_A 的 z 沿坡升高以覆盖 z 分量）：person_A 出现后行走、等待、再行走后进入建筑（state 4）；person_B 等待、行走后上车（state 3，
容器为 vehicle_C）；vehicle_C 等待后以 8 m/s 驶离；另有 2 名背景演员（不进入 sim-core）。机队 3 架（FakeS），每 tick 先按策略
给出机位、再推进 event_clock 与 targets stage（与 sim-core 中 order 170、199 同一 tick 的取值相同）。
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("zstandard")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "targets"))

from awr.sim.events import tape as tp
from awr.sim.events.gen import compile_env_track

H64 = "0" * 64
N_T = 1100                     # 110 s @ 10 Hz
TICKS = N_T - 1                # 运行 tick 1..1099（t = 0.1 s .. 109.9 s）
SCRIPTED = ("person_A", "person_B", "vehicle_C")
TIDS = ("person-a", "person-b", "vehicle-c")
IN_BUILDING_T_MS = 90_000      # person_A 自 tick 900 起 state 4（不判定）


# ------------------------------------------------------------------------------------------------ 构造磁带
def _track(keys: list[tuple[int, float, float]]) -> np.ndarray:
    """关键帧 (tick, x_m, y_m) 之间逐 tick 线性插值，返回 (N_T, 2) 米；末关键帧之后保持。"""
    k = np.array([a for a, _, _ in keys], float)
    t = np.arange(N_T, dtype=float)
    return np.stack([np.interp(t, k, [x for _, x, _ in keys]), np.interp(t, k, [y for _, _, y in keys])], 1)


def _states(spans: list[tuple[int, int]]) -> np.ndarray:
    """(起始 tick, state) 分段，返回 (N_T,) u8。"""
    s = np.zeros(N_T, np.uint8)
    for (k0, st), (k1, _) in zip(spans, [*spans[1:], (N_T, 0)], strict=True):
        s[k0:k1] = st
    return s


def _fill(f: np.ndarray, i: int, xy: np.ndarray, z: np.ndarray, state: np.ndarray, container: np.ndarray | None = None) -> None:
    """写入第 i 名演员：位置取整到毫米；移动态速度为前向差分（cm/s），航向取运动方向（静止时保持上一航向）。"""
    mm = np.round(np.column_stack([xy, z]) * 1000.0).astype(np.int64)
    f["x_mm"][:, i], f["y_mm"][:, i], f["z_mm"][:, i] = mm[:, 0], mm[:, 1], mm[:, 2]
    v = np.zeros((N_T, 3))
    v[:-1] = (mm[1:] - mm[:-1]) / 1000.0 / 0.1
    v[state != 1] = 0.0
    f["vx_cmps"][:, i], f["vy_cmps"][:, i], f["vz_cmps"][:, i] = (np.round(v * 100.0).astype(np.int16).T)
    yaw, last = np.zeros(N_T), 0.0
    for k in range(N_T):
        if math.hypot(v[k, 0], v[k, 1]) > 1e-6:
            last = math.degrees(math.atan2(v[k, 1], v[k, 0]))
        yaw[k] = last
    f["yaw_cdeg"][:, i] = tp.fold_yaw(np.round(yaw * 100.0))
    f["state"][:, i] = state
    f["container"][:, i] = tp.NO_CONTAINER if container is None else container


def _tape(d: Path) -> tuple[str, list[tuple[float, float, float]]]:
    """写出 tape.bin 与 tape.json，返回 (content_sha256, 三名脚本演员的出发点)。"""
    f = np.zeros((N_T, 5), tp.RECORD)
    a = _track([(0, 0.0, 0.0), (10, 0.0, 0.0), (400, 54.6, 0.0), (600, 54.6, 0.0), (900, 54.6, 42.0)])
    _fill(f, 0, a, 1.0 + 0.02 * a[:, 0], _states([(0, 0), (10, 1), (400, 2), (600, 1), (900, 4)]))       # 坡地：z 随 x 变化
    c = _track([(0, -30.0, 40.0), (720, -30.0, 40.0), (1000, -30.0, 264.0)])
    cst = _states([(0, 2), (720, 1), (1000, 2)])
    b = _track([(0, 60.0, 40.0), (150, 60.0, 40.0), (450, 10.0, 40.0), (500, 10.0, 40.0), (700, -30.0, 40.0)])
    b[700:] = c[700:]                                                                                    # 车内：随车
    cont = np.full(N_T, tp.NO_CONTAINER, np.uint8)
    cont[700:] = 2
    _fill(f, 1, b, np.full(N_T, 1.0), _states([(0, 2), (150, 1), (450, 2), (500, 1), (700, 3)]), cont)
    _fill(f, 2, c, np.full(N_T, 1.0), cst)
    for i, (x0, y0, vx) in ((3, (120.0, -50.0, -1.2)), (4, (-90.0, -20.0, 1.0))):
        xy = np.column_stack([x0 + vx * 0.1 * np.arange(N_T), np.full(N_T, y0)])
        _fill(f, i, xy, np.zeros(N_T), np.ones(N_T, np.uint8))
    env = {"interpolation": "step", "keyframes": [
        {"t_ms": 0, "wind_mps": 3.0, "wind_dir_deg": 270.0, "gust_sigma_mps": 0.5, "rain_mm_h": 0.0, "visibility_m": 20000.0, "fog": 0.0,
         "cloud": 0.1}]}
    size = {"person": [500, 300, 1750], "vehicle": [4500, 1800, 1500]}
    actors = [{"idx": i, "id": aid, "class": cls, "scripted": i < 3, "profile": cls, "size_mm": size[cls], "appearance": {}}
              for i, (aid, cls) in enumerate((("person_A", "person"), ("person_B", "person"), ("vehicle_C", "vehicle"),
                                              ("bg_0", "person"), ("bg_1", "person")))]
    hd = {"schema": "drone4d.tape.v1", "event_id": "ev-exo", "event_version": 1, "event_sha256": H64, "mode": "exogenous", "seed": 0,
          "tick_hz": 10, "n_ticks": N_T, "duration_ms": N_T * 100, "record": tp.record_spec(),
          "codec": {"name": "col-delta-zstd@1", "block_ticks": 100}, "actors": actors,
          "world": {"id": "w1", "content_version": "cv-1", "raycast_sha256": H64, "navmesh_sha256": {"person": H64}, "anchors_sha256": H64},
          "environment": env, "env_track": compile_env_track(env), "comms": {}, "anim": None, "realized": None}
    d.mkdir(parents=True, exist_ok=True)
    h = tp.write_tape(d / "tape.bin", hd, f, n_scripted=3)
    (d / "tape.json").write_text(json.dumps({"content_sha256": h["content_sha256"]}))
    pos0 = [(float(f["x_mm"][0, i]) / 1000, float(f["y_mm"][0, i]) / 1000, float(f["z_mm"][0, i]) / 1000) for i in range(3)]
    return h["content_sha256"], pos0


@pytest.fixture(scope="module")
def tape_dir(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str, list]:
    root = tmp_path_factory.mktemp("exo") / "events"
    sha, pos0 = _tape(root / "ev-exo" / "v1")
    return root, sha, pos0


def _tape_track(root: Path, ticks: range) -> tuple[np.ndarray, str]:
    """由磁带独立复算：tick k 上三名脚本演员 (x, y, z) 毫米（按 idx 拼接，"<i8"）及其 SHA-256（与 sim-core 侧同一字节口径）。"""
    r = tp.open_tape(root / "ev-exo" / "v1")
    rows = []
    for k in ticks:
        fr = r.frame(k, background=False)
        rows.append(np.stack([fr["x_mm"], fr["y_mm"], fr["z_mm"]], 1).astype("<i8"))
    arr = np.stack(rows)
    return arr, hashlib.sha256(b"".join(a.tobytes() for a in arr)).hexdigest()


# ------------------------------------------------------------------------------------------------ 机队策略
@dataclass
class Ctx:
    """策略的输入：仿真时刻、三名演员的出发点与 M18 在上一 tick 的实现位置（跟踪策略读取，即观察者对演员的反馈）。"""

    t_s: float
    pos0: list
    seen: list


Policy = Callable[[Ctx], list | None]
HOVER_OFF = np.array([3.0, 4.0, 8.0])                         # 与出发点的距离 √89 ≈ 9.43 m


def hover(c: Ctx) -> list:
    """悬停：第 i 架机悬停于演员 i 出发点上方 10 m 内。"""
    return [np.asarray(p) + HOVER_OFF for p in c.pos0]


def orbit(c: Ctx) -> list:
    """盘旋：绕场景中心半径 40 m、高 30 m、角速度 0.1 rad/s，三机相位相隔 120°。"""
    return [np.array([15.0 + 40.0 * math.cos(0.1 * c.t_s + 2.0944 * i), 30.0 + 40.0 * math.sin(0.1 * c.t_s + 2.0944 * i), 30.0])
            for i in range(3)]


def track(c: Ctx) -> list:
    """跟踪：第 i 架机位于 M18 给出的演员 i 上一 tick 实现位置正上方 15 m。"""
    return [np.asarray(p) + np.array([0.0, 0.0, 15.0]) for p in c.seen]


def scan(c: Ctx) -> list:
    """扫描：三机分带往返扫描 [−100, 100] m，带距 60 m，高 60 m，地速 5 m/s。"""
    out = []
    for i in range(3):
        u = (5.0 * c.t_s) % 400.0
        x = -100.0 + (u if u < 200.0 else 400.0 - u)
        out.append(np.array([x, -60.0 + 60.0 * i, 60.0]))
    return out


def away(c: Ctx) -> list:
    """远离：三机以 6 m/s 自场景中心向外飞离（自 400 m 起），始终在敏感体（≤ 300 m）之外。"""
    r = 400.0 + 6.0 * c.t_s
    return [np.array([15.0 + r * math.cos(2.0944 * i), 30.0 + r * math.sin(2.0944 * i), 100.0]) for i in range(3)]


def grounded(c: Ctx) -> None:
    """对照：不起飞（无判据参与者）。"""
    return None


POLICIES: dict[str, Policy] = {"hover": hover, "orbit": orbit, "track": track, "scan": scan, "away": away, "grounded": grounded}


# ------------------------------------------------------------------------------------------------ 台架
@dataclass
class _World:
    content_version: str = "cv-1"


class _Calls:
    def submit_internal(self, cmd: dict, principal: dict) -> dict:
        assert principal["principal_id"] == "event:ev-exo" and cmd["op"] == "env/set"
        return {"status": "accepted", "code": 0}


class _Api:
    def __init__(self, bench, world) -> None:
        self.b, self.world, self.ctx = bench, world, bench.ctx

    def submit(self, op: str, args: dict, *, uav: str | None = None, principal: str = "session") -> dict:
        return self.b.cmd(op, args)


def _section(sha: str, **kw) -> dict:
    return {"event_id": "ev-exo", "version": 1, "tape_uri": "ev-exo/v1", "tape_content_sha256": sha, "mode": "exogenous", **kw}


@dataclass
class Result:
    run: dict
    realized: np.ndarray            # (TICKS, 3, 3) 毫米：M18 timeline_track 逐 tick 解包
    min_d0: list                    # 各演员出发点与最近机体的最小三维距离（m）
    discovered_ev: list             # target.discovered 事件（M18 事件流）
    disc_n: dict                    # 识别物 id -> TState.disc_n
    col_disc_n: dict                # 识别物 id -> tgt_disc_n 列
    states: list                    # target.state 事件
    file_sha: str


def _run_policy(name: str, tape_dir, monkeypatch: pytest.MonkeyPatch) -> Result:
    from tlib import Bench, FakeWorld

    root, sha, pos0 = tape_dir
    monkeypatch.setenv("AWR_D3_EVENTS_DIR", str(root))
    with Bench(3, FakeWorld(ground=1.0)) as b:
        from awr.sim.events import plugin

        rt = plugin.install()
        try:
            assert b.reg.session_loaders["event"].fn(_section(sha), _Api(b, _World())) == []
            assert set(rt.tids.values()) == set(TIDS)
            for tid in TIDS:
                raw = b.T(tid).cfg.raw
                assert raw["motion"]["mode"] == "timeline" and raw["reaction_policy"] == "exogenous"
            stage = next(s for s in b.reg.stages if s.name == "event_clock")
            b.ctx.calls = _Calls()
            pol = POLICIES[name]
            min_d0 = [math.inf] * 3
            for _ in range(TICKS):
                b.ctx.tick += 10
                b.ctx.t_ns += 100_000_000
                seen = [b.T(tid).ms.pos.copy() for tid in TIDS]
                uav = pol(Ctx(b.ctx.t_ns * 1e-9, pos0, seen))
                for i in range(3):
                    if uav is None:
                        b.S.land(i)
                    else:
                        b.S.fly(i, uav[i])
                if uav is not None:
                    U = np.asarray(uav)
                    for j, p in enumerate(pos0):
                        min_d0[j] = min(min_d0[j], float(np.min(np.linalg.norm(U - np.asarray(p), axis=1))))
                stage.fn(b.S, b.ctx)
                b.rt.stage(b.S, b.ctx)
            run = b.reg.batch_outputs["event_run"](b.ctx)
            tr = b.rt.timeline_track(list(TIDS))
            assert [t for t, _ in tr] == [k * 100_000_000 for k in range(1, N_T)]
            realized = np.stack([np.frombuffer(x, "<i8").reshape(3, 3) for _, x in tr])
            return Result(run, realized, min_d0, b.events.of("target.discovered"), {t: int(b.T(t).disc_n) for t in TIDS},
                          {t: int(b.col("tgt_disc_n", t)) for t in TIDS}, b.events.of("target.state"),
                          hashlib.sha256((root / "ev-exo" / "v1" / "tape.bin").read_bytes()).hexdigest())
        finally:
            plugin.uninstall()


@pytest.fixture(scope="module")
def results(tape_dir) -> dict[str, Result]:
    mp = pytest.MonkeyPatch()
    try:
        return {name: _run_policy(name, tape_dir, mp) for name in POLICIES}
    finally:
        mp.undo()


# ------------------------------------------------------------------------------------------------ 用例
def test_at_least_five_policies_and_hover_within_10m(results) -> None:
    flying = [n for n in POLICIES if n != "grounded"]
    assert len(flying) >= 5 and {"hover", "orbit", "track"} <= set(flying)
    assert max(results["hover"].min_d0) <= 10.0                     # 悬停于三名演员出发点 10 m 内
    assert min(results["away"].min_d0) > 300.0


def test_realized_tracks_equal_the_tape_under_every_policy(results, tape_dir) -> None:
    root, sha, _pos0 = tape_dir
    want, want_sha = _tape_track(root, range(1, N_T))
    assert not np.array_equal(want[0], want[-1])                    # 构造轨迹确有运动
    for name, r in results.items():
        assert np.array_equal(r.realized, want), name               # 逐 tick 逐毫米（含 z）一致
        run = r.run
        assert run["tape_content_sha256"] == sha and run["mode"] == "exogenous"
        assert run["scripted_track_sha256"] == want_sha == run["tape_track_sha256"], name
        assert run["realized_ticks"] == run["track_ticks"] == TICKS
        assert r.file_sha == results["grounded"].file_sha           # 磁带文件未被改写
    assert len({r.run["scripted_track_sha256"] for r in results.values()}) == 1
    tp.open_tape(root / "ev-exo" / "v1").verify(expect_content_sha256=sha)


def test_discovery_is_counted_and_written_to_event_run(results) -> None:
    actor = dict(zip(TIDS, SCRIPTED, strict=True))
    for name, r in results.items():
        disc = r.run["discovered"]
        # event_run 的被发现记录与 M18 事件流、TState 计数与 tgt_disc_n 列一一对应
        assert [(d["actor"], d["uav"], d["t_ms"]) for d in disc] == [
            (actor[e["data"]["target"]], e["uav"], e["t_sim_ns"] // 1_000_000) for e in r.discovered_ev], name
        assert all(d["criterion"] == "geometric" and d["kind"] in ("visual", "acoustic") for d in disc)
        assert r.run["discovered_count"] == {actor[t]: r.disc_n[t] for t in TIDS} == {actor[t]: r.col_disc_n[t] for t in TIDS}, name
        assert sum(r.run["discovered_count"].values()) == len(disc)
        assert all(not (d["actor"] == "person_A" and d["t_ms"] >= IN_BUILDING_T_MS) for d in disc)   # 建筑内不判定
        assert "broadcast" not in [e["data"]["reason"] for e in r.states]                            # exogenous 不广播
    for name in ("hover", "orbit", "track", "scan"):
        cnt = results[name].run["discovered_count"]
        assert cnt["person_A"] >= 1 and cnt["person_B"] >= 1, (name, cnt)                           # 照常计数
        assert any(e["data"]["to"] in ("REACTING", "ALERTED") for e in results[name].states)          # 状态机照跑
    assert sum(results["hover"].run["discovered_count"].values()) > 0
    for name in ("away", "grounded"):
        assert results[name].run["discovered"] == [] and set(results[name].run["discovered_count"].values()) == {0}
    tr = results["track"].run["discovered"]
    assert any(d["actor"] == "vehicle_C" for d in tr)                # 跟踪车辆的机体进入车辆敏感体
    assert len({json.dumps(r.run["discovered_count"], sort_keys=True) for r in results.values()}) > 1   # 度量随策略变化，轨迹不变


def test_rerun_is_bitwise_identical(tape_dir, results, monkeypatch: pytest.MonkeyPatch) -> None:
    r = _run_policy("track", tape_dir, monkeypatch)
    assert r.run == results["track"].run and np.array_equal(r.realized, results["track"].realized)


def test_duplicate_target_id_between_event_and_targets_is_rejected(tape_dir, monkeypatch: pytest.MonkeyPatch) -> None:
    from tlib import Bench, FakeWorld

    root, sha, _pos0 = tape_dir
    monkeypatch.setenv("AWR_D3_EVENTS_DIR", str(root))
    with Bench(1, FakeWorld(ground=1.0)) as b:
        from awr.sim.events import plugin

        plugin.install()
        try:
            api = _Api(b, _World())
            loaders = b.reg.session_loaders
            assert loaders["event"].order < loaders["targets"].order
            assert loaders["event"].fn(_section(sha), api) == []
            probs = loaders["targets"].fn([{"id": "person-a", "class": "person", "pos": [5.0, 5.0, None]}], api)
            assert len(probs) == 1 and "TARGET_ID_DUP" in json.dumps(probs)
            assert b.T("person-a").cfg.raw["motion"]["mode"] == "timeline"   # 事件识别物保持不变
        finally:
            plugin.uninstall()


def test_no_motion_source_records_nothing() -> None:
    """D2 缺省（无运动源、无 timeline 识别物）：不记事件度量（D2 逐位不变的完整覆盖见 D3-AC-01）。"""
    from tlib import Bench, FakeWorld, static_person

    with Bench(1, FakeWorld()) as b:
        b.add(static_person(sensitivity=[{"kind": "visual", "shape": "sphere", "radius_max_m": 120.0}]))
        b.S.fly(0, [0.0, 60.0, 40.0])
        b.run(10.0)
        assert b.rt.stats["discovered"] >= 1
        assert b.rt.timeline_track(["x"]) == [] and b.rt.timeline_discovered() == []
