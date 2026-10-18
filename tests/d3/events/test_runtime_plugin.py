"""M23 运行期接入（§6.12）：会话段装配、磁带运动源驱动 M18 timeline 识别物（磁带 tick 上逐毫米一致，M23-AC-021）、
event_clock 按 (t, t + dt] 投递环境轨、状态查询与批处理输出、614/615 拒绝。"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("zstandard")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "targets"))

from awr.sim.events import tape as tp
from awr.sim.events.gen import compile_env_track
from awr.sim.events.runtime import TapeMotionSource, target_id

H64 = "0" * 64
N_T = 300


def _tape(d: Path, content_version: str = "cv-1") -> str:
    """person_A：t = 1 s 出现于 (5, 0)，沿 +x 以 1 m/s 行走，t = 20 s 进入建筑（state 4）；vehicle_C：全程等待于 (40, 10)。"""
    f = np.zeros((N_T, 2), tp.RECORD)
    f["container"] = tp.NO_CONTAINER
    f["state"][10:200, 0] = 1
    f["state"][200:, 0] = 4
    f["x_mm"][:, 0] = 5000 + np.clip(np.arange(N_T) - 10, 0, 190) * 100
    f["vx_cmps"][10:200, 0] = 100
    f["state"][:, 1] = 2
    f["x_mm"][:, 1], f["y_mm"][:, 1], f["yaw_cdeg"][:, 1] = 40000, 10000, 9000
    env = {"interpolation": "smooth", "keyframes": [
        {"t_ms": 0, "wind_mps": 2.0, "wind_dir_deg": 270.0, "gust_sigma_mps": 0.5, "rain_mm_h": 0.0, "visibility_m": 20000.0, "fog": 0.0, "cloud": 0.1},
        {"t_ms": 10_000, "wind_mps": 6.0, "wind_dir_deg": 300.0, "gust_sigma_mps": 1.0, "rain_mm_h": 0.0, "visibility_m": 9000.0, "fog": 0.0, "cloud": 0.3}]}
    hd = {"schema": "drone4d.tape.v1", "event_id": "ev-rt", "event_version": 2, "event_sha256": H64, "mode": "exogenous", "seed": 0,
          "tick_hz": 10, "n_ticks": N_T, "duration_ms": N_T * 100, "record": tp.record_spec(),
          "codec": {"name": "col-delta-zstd@1", "block_ticks": 100},
          "actors": [{"idx": 0, "id": "person_A", "class": "person", "scripted": True, "profile": "person", "size_mm": [500, 300, 1750],
                      "appearance": {}},
                     {"idx": 1, "id": "vehicle_C", "class": "vehicle", "scripted": True, "profile": "vehicle", "size_mm": [4500, 1800, 1500],
                      "appearance": {}}],
          "world": {"id": "w1", "content_version": content_version, "raycast_sha256": H64, "navmesh_sha256": {"person": H64},
                    "anchors_sha256": H64},
          "environment": env, "env_track": compile_env_track(env), "comms": {}, "anim": None, "realized": None}
    d.mkdir(parents=True, exist_ok=True)
    h = tp.write_tape(d / "tape.bin", hd, f, n_scripted=2)
    (d / "tape.json").write_text(json.dumps({"content_sha256": h["content_sha256"]}))
    return h["content_sha256"]


@dataclass
class _World:
    content_version: str = "cv-1"


class _Calls:
    def __init__(self) -> None:
        self.sent: list[tuple[int, dict]] = []
        self.t_ns = 0

    def submit_internal(self, cmd: dict, principal: dict) -> dict:
        assert principal["principal_id"] == "event:ev-rt" and cmd["op"] == "env/set"
        self.sent.append((self.t_ns, cmd))
        return {"status": "accepted", "code": 0}


class _Api:
    def __init__(self, bench, world) -> None:
        self.b, self.world = bench, world
        self.principals: list[str] = []

    def submit(self, op: str, args: dict, *, uav: str | None = None, principal: str = "session") -> dict:
        self.principals.append(principal)
        return self.b.cmd(op, args)


def test_target_ids() -> None:
    taken: set[str] = set()
    assert [target_id(a, taken) for a in ("person_A", "Person-a", "_x")] == ["person-a", "person-a-2", "x"]


@pytest.fixture
def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from tlib import Bench, FakeWorld

    monkeypatch.setenv("AWR_D3_EVENTS_DIR", str(tmp_path / "events"))
    sha = _tape(tmp_path / "events" / "ev-rt" / "v2")
    with Bench(1, FakeWorld()) as b:
        from awr.sim.events import plugin

        rt = plugin.install()
        yield b, rt, sha, plugin
        plugin.uninstall()


def _section(sha: str, **kw) -> dict:
    return {"event_id": "ev-rt", "version": 2, "tape_uri": "ev-rt/v2", "tape_content_sha256": sha, "mode": "exogenous", **kw}


def _run(b, rt, calls: _Calls, seconds: float) -> None:
    stage = next(s for s in b.reg.stages if s.name == "event_clock")
    assert (stage.order, stage.every, stage.owner) == (199, 1, "M23")
    b.ctx.calls = calls
    for _ in range(round(seconds * 10)):
        b.ctx.tick += 10
        b.ctx.t_ns += 100_000_000
        calls.t_ns = b.ctx.t_ns
        stage.fn(b.S, b.ctx)
        b.rt.stage(b.S, b.ctx)


def test_session_load_drives_m18_timeline_targets(bench) -> None:
    b, rt, sha, _plugin = bench
    api = _Api(b, _World())
    loader = b.reg.session_loaders["event"]
    assert loader.order == 45 and loader.fn(_section(sha), api) == []
    assert set(rt.tids.values()) == {"person-a", "vehicle-c"} and set(api.principals) == {"event:ev-rt"}
    assert b.rt.motion_source is rt.source
    T = b.T("person-a")
    assert T.cfg.raw["motion"]["mode"] == "timeline" and T.cfg.raw["reaction_policy"] == "exogenous"
    calls = _Calls()
    tape = tp.open_tape(rt.reader.path.parent)
    for k in range(1, 260):
        _run(b, rt, calls, 0.1)
        rec = tape.frame(k)
        for idx, tid in ((0, "person-a"), (1, "vehicle-c")):
            p = b.T(tid).ms.pos
            assert np.array_equal(np.round(p[:2] * 1000.0), [rec["x_mm"][idx], rec["y_mm"][idx]])          # 磁带 tick 上逐毫米一致
    assert b.T("person-a").ms.speed == 0.0 and "person-a" in b.rt._src_off                              # 进入建筑：不判定
    assert "vehicle-c" not in b.rt._src_off
    out = b.reg.batch_outputs["event_run"](b.ctx)
    assert out["track_ticks"] == 259 and len(out["scripted_track_sha256"]) == 64 and out["targets"]["person_A"] == "person-a"
    st = b.reg.queries["event/status"].fn({"op": "event/status", "args": {}}, b.ctx)
    assert st["event_id"] == "ev-rt" and st["t_ms"] == 25_900 and st["env"]["wind_speed_ref_mps"] == pytest.approx(6.0)


def test_event_clock_delivers_env_ops_one_step_ahead(bench) -> None:
    b, rt, sha, _plugin = bench
    assert b.reg.session_loaders["event"].fn(_section(sha), _Api(b, _World())) == []
    calls = _Calls()
    _run(b, rt, calls, 12.0)
    ops = [(t, c["args"]) for t, c in calls.sent]
    assert [c["cid"] for _, c in calls.sent] == ["m23e:0", "m23e:1"]
    assert ops[0][0] == 100_000_000 and ops[0][1]["mode"] == "step" and "kind" not in ops[0][1]
    assert ops[1][0] == 100_000_000 and ops[1][1]["mode"] == "smooth" and ops[1][1]["duration_s"] == 10.0     # t_ms 0 的平滑段
    assert [a["t_ms"] for a in rt.applied] == [0, 0]


def test_offset_shifts_event_time(bench) -> None:
    b, rt, sha, _plugin = bench
    assert b.reg.session_loaders["event"].fn(_section(sha, offset_s=15.0), _Api(b, _World())) == []
    src = rt.source
    assert isinstance(src, TapeMotionSource)
    pos, _yaw, vel, enabled, hidden = src.pose("person-a", 0)
    assert pos[0] == pytest.approx(5.0 + 14.0) and vel[0] == pytest.approx(1.0) and enabled and not hidden
    assert not src.pose("person-a", 6_000_000_000)[3] and src.pose("nobody", 0) is None
    calls = _Calls()
    _run(b, rt, calls, 0.1)
    assert len(calls.sent) == 2                                                                        # 已过的操作在首个 tick 补投


def test_rejections(bench) -> None:
    b, _rt, sha, _plugin = bench
    load = b.reg.session_loaders["event"].fn
    assert load(_section("f" * 64), _Api(b, _World()))[0]["code"] == 614
    assert load(_section(sha), _Api(b, _World("other")))[0]["code"] == 615
    assert load(_section(sha, mode="interactive"), _Api(b, _World()))[0]["code"] == 611
    probs = load(_section(sha), _Api(b, object()))
    assert [p["code"] for p in probs] == ["EVENT_WORLD_UNCHECKED"]
    assert load({}, _Api(b, _World())) == []
