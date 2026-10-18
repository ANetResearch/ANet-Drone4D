"""M26 Env（P0a）：契约、确定性、决策时延、动作守卫回执、L1 匿名性与链路（合成世界、运动学开发档）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from awr.agentsdk.env.core import BenchEnv
from awr.agentsdk.env.spec import DecisionCfg, EnvSpec, FleetEntry
from awr.agentsdk.schemas import bench_validator

FLEET = (FleetEntry("uav-01", (-10.0, 50.0, 0.0)), FleetEntry("uav-02", (10.0, 50.0, 0.0)))


def _spec(synth_event, out: Path | None, seed: int = 5, dur: float = 30.0, **kw) -> EnvSpec:
    w, ev = synth_event
    return EnvSpec(f"t-{seed}", seed, w, ev, kw.pop("fleet", FLEET), physics="kinematic", duration_s=dur, out_dir=out, **kw)


def _run(spec: EnvSpec, make_agent, check=None) -> dict:
    env = BenchEnv(spec)
    obs, infos = env.reset()
    ags = {u: make_agent(u) for u in env.possible_agents}
    for u, a in ags.items():
        a.reset(infos[u]["config"])
        if check:
            check("config", u, infos[u]["config"])
    while env.agents:
        acts = {}
        for u, o in obs.items():
            if check:
                check("obs", u, o)
            acts[u] = ags[u].step(o["obs"], o["inbox"])
        obs, *_ = env.step(acts)
    m = env.close()
    m["_env"] = env
    return m


def _valid(kind: str, _u: str, doc: dict) -> None:
    errs = list(bench_validator({"obs": "obs", "config": "agent_config"}[kind]).iter_errors(doc))
    assert not errs, (kind, [e.message for e in errs[:3]])


@pytest.mark.parametrize("policy", ["static_guard", "coverage_scan", "track_share"])
def test_baselines_contracts_and_determinism(synth_event, tmp_path: Path, policy: str) -> None:
    from drone4d.baselines import BASELINES

    m1 = _run(_spec(synth_event, tmp_path / "a"), lambda u: BASELINES[policy](), _valid)
    m2 = _run(_spec(synth_event, tmp_path / "b"), lambda u: BASELINES[policy]())
    env = m1["_env"]
    assert not list(bench_validator("task").iter_errors(env.task))
    for u in ("uav-01", "uav-02"):
        a = (tmp_path / "a/agents" / u / "acts.jsonl").read_bytes()
        assert a == (tmp_path / "b/agents" / u / "acts.jsonl").read_bytes()          # 同一种子：动作流逐字节一致
        assert (tmp_path / "a/agents" / u / "obs.sha256").read_bytes() == (tmp_path / "b/agents" / u / "obs.sha256").read_bytes()
        for line in a.decode().splitlines():
            assert not list(bench_validator("act").iter_errors(json.loads(line)["act"]))
    assert m1["pose_sha256"] == m2["pose_sha256"] and m1["n_ticks"] == 300
    m3 = _run(_spec(synth_event, tmp_path / "c", seed=6), lambda u: BASELINES[policy]())
    assert (tmp_path / "c/agents/uav-01/obs.sha256").read_bytes() != (tmp_path / "a/agents/uav-01/obs.sha256").read_bytes() or m3 != m1


class _Probe:
    def __init__(self, script=None) -> None:
        self.ks, self.acks, self.inbox, self.obs = [], [], [], []
        self.script = script or (lambda o: {"schema": "drone4d.bench.act.v1", "k": o["k"]})

    def reset(self, config: dict) -> None:
        self.config = config

    def step(self, obs: dict, inbox: list[dict]) -> dict:
        self.ks.append(obs["k"])
        self.acks += obs["acks"]
        self.inbox += inbox
        self.obs.append(obs)
        return self.script(obs)


def test_decision_latency_fixed_tau(synth_event, tmp_path: Path) -> None:
    probes = {}

    def mk(u):
        probes[u] = _Probe(lambda o: {"schema": "drone4d.bench.act.v1", "k": o["k"], "nav": {"mode": "takeoff", "alt_agl_m": 40.0}})
        return probes[u]

    _run(_spec(synth_event, tmp_path, dur=3.0, decision=DecisionCfg(tau_ms=250)), mk)
    assert probes["uav-01"].ks[:4] == [0, 3, 6, 9]                                      # apply_tick = k + ⌈250 / 100⌉，在途不调用
    rows = [json.loads(x) for x in (tmp_path / "agents/uav-01/acts.jsonl").read_text().splitlines()]
    assert [(r["k"], r["apply_tick"]) for r in rows[:3]] == [(0, 3), (3, 6), (6, 9)]


def test_guard_acks(synth_event, tmp_path: Path) -> None:
    def script(o):
        k = o["k"]
        if k == 0:
            return {"schema": "drone4d.bench.act.v1", "k": 0, "nav": {"mode": "takeoff", "alt_agl_m": 500.0}}
        if k == 1:
            return {"schema": "drone4d.bench.act.v1", "k": 1, "nav": {"mode": "orbit", "center_enu_m": [0, 0], "alt_m": 50, "radius_m": 20,
                                                                    "speed_mps": 5, "cw": True},
                    "msgs": [{"dst": "uav-09", "topic": "x", "payload": b"1"}, {"dst": "*", "topic": "x", "payload": b"\0" * 5000}],
                    "report": [{"type": "dance", "participants": [{"track_id": "a", "evidence": [{"uav": "uav-01", "sensor": "eo", "frame_seq": 0,
                                                                                                   "det_id": "0" * 16}]}],
                                "window": {"t_start_ns": 0, "t_end_ns": 1}},
                               {"type": "meet", "participants": [{"track_id": "a", "evidence": [{"uav": "uav-01", "sensor": "eo", "frame_seq": 0,
                                                                                                  "det_id": "0" * 16}]}],
                                "window": {"t_start_ns": 0, "t_end_ns": 1}}]}
        if k == 2:
            return {"schema": "drone4d.bench.act.v1", "k": 7}
        if k == 3:
            return {"schema": "drone4d.bench.act.v1", "k": 3, "nav": {"mode": "velocity", "vel_mps": [30.0, None, 0.0], "frame": "world"}}
        return {"schema": "drone4d.bench.act.v1", "k": k, "bogus": 1}

    probes = {}

    def mk(u):
        probes[u] = _Probe(script)
        return probes[u]

    _run(_spec(synth_event, tmp_path, dur=1.0), mk)
    got = {(a["k"], a["item"], a["index"]): (a["result"], a["code"], a.get("why")) for a in probes["uav-01"].acks}
    assert got[(0, "nav", 0)] == ("clamped", 0, "limit")
    assert got[(1, "nav", 0)] == ("rejected", 663, "unsupported")
    assert got[(1, "msg", 0)] == ("rejected", 667, "dst") and got[(1, "msg", 1)] == ("rejected", 667, "too_large")
    assert got[(1, "report", 0)] == ("rejected", 663, "report_type")
    assert got[(1, "report", 1)] == ("rejected", 663, "evidence_unknown")
    assert got[(2, "act", 0)] == ("rejected", 666, "k_mismatch")
    assert got[(3, "nav", 0)] == ("clamped", 0, "limit")
    assert got[(4, "act", 0)] == ("rejected", 666, "schema")


def test_l1_anonymous_and_link(synth_event, tmp_path: Path) -> None:
    from drone4d.agent import act, gimbal_angles, nav_takeoff, out_msg

    def script(o):
        if o["k"] == 0:
            return act(0, nav=nav_takeoff(60.0), gimbal=gimbal_angles("eo", -60.0, -90.0))
        return act(o["k"], msgs=[out_msg("*", "ping", {"k": o["k"]})] if o["k"] % 10 == 0 else None)

    probes = {}

    def mk(u):
        probes[u] = _Probe(script)
        return probes[u]

    _run(_spec(synth_event, tmp_path, dur=40.0), mk)
    dets = [d for o in probes["uav-01"].obs + probes["uav-02"].obs for d in o["sensors"]["detections"]]
    assert dets, "the synthetic event should be visible from 60 m"
    blob = json.dumps([o for p in probes.values() for o in p.obs], default=str)
    assert "person_a" not in blob and "vehicle_c" not in blob and "bg0_" not in blob     # 观测不含演员身份
    for o in probes["uav-01"].obs:
        ids = [d["det_id"] for d in o["sensors"]["detections"]]
        assert ids == sorted(ids)
    truth = [json.loads(x) for x in (tmp_path / "truth/l1.jsonl").read_text().splitlines()]
    assert {t["det_id"] for t in truth} >= {d["det_id"] for d in dets}
    by_actor: dict[str, set] = {}
    for t in truth:
        by_actor.setdefault(t["actor"], set()).add(t["det_id"])
    assert max(len(v) for v in by_actor.values()) > 1                                     # 同一演员每帧 det_id 不同
    rx = probes["uav-02"].inbox
    assert rx and all(m["src"] == "uav-01" or m["src"] == "uav-02" for m in rx)
    assert all(m["t_recv_ns"] > m["t_sent_ns"] for m in rx)
    assert (tmp_path / "net/records.jsonl").read_text().count('"ok":true') >= len(rx)
