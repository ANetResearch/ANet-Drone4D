"""BenchEnv（M26 §6.6.1；P0a）：lockstep 主循环、观测投影（L1 检测级；L2、L2-depth 渲染级见 `env.l2`）、决策时延与动作注入，
PettingZoo Parallel 风格接口。

    env = BenchEnv(spec)
    obs, infos = env.reset()                      # {uav: {"obs": Observation, "inbox": [Message]}}，只含本 tick 应调用的 Agent
    while env.agents:
        acts = {u: agent[u].step(o["obs"], o["inbox"]) for u, o in obs.items()}
        obs, rewards, terms, truncs, infos = env.step(acts)
    result = env.close()

时序：控制步 100 ms；tick k 的观测取 t_k 的真值与导航解，L1 检测取 (t_last, t_k] 内的相机帧（10 Hz，帧序号 = tick）；动作在
`apply_tick = k_obs + max(1, ⌈τ / 100 ms⌉)` 生效（固定延迟生效、未到即阻塞），在途期间不再调用该 Agent；物理档 `advance(k)` 推进
(t_k, t_{k+1}]。L2 的传感器帧在 tick 末按物理档样本渲染，Agent 未被调用的 tick 的帧累积到下一次观测（按 seq 升序）。
goto 目标解释在本机导航系，每个控制步按当前导航误差换算为真值系目标（变化 ≥ 5 cm 时更新跟踪器）。
未返回动作的 Agent 计超时（回执 `timeout`），沿用上一导航指令。回合在事件时长到达或全部机体终止时结束。
"""

from __future__ import annotations

import math
import time
from pathlib import Path

import numpy as np

from awr.agentsdk.env.camera import CameraModel, Gimbal, cam_axes
from awr.agentsdk.env.guard import ActionGuard, Limits
from awr.agentsdk.env.l1 import L1Model
from awr.agentsdk.env.link import LinkModel
from awr.agentsdk.env.nav import NavModel
from awr.agentsdk.env.recorder import EnvRecorder
from awr.agentsdk.env.spec import EnvSpec
from awr.agentsdk.task import brief_for_agent, task_from_event
from awr.phys.adapter import TierCommand, VehicleStatus

__all__ = ["TICK_MS", "TICK_NS", "BenchEnv"]

TICK_MS = 100
TICK_NS = TICK_MS * 1_000_000
_MODE = {VehicleStatus.GROUNDED: "landed", VehicleStatus.LANDING: "landing", VehicleStatus.AIRBORNE: "airborne",
         VehicleStatus.CRASHED: "failsafe", VehicleStatus.LOST: "failsafe"}
_NAV_CMD = {"velocity": "velocity", "goto": "goto", "hover": "hover", "takeoff": "takeoff", "land": "land", "kill": "hover"}


def _yaw(q) -> float:
    x, y, z, w = (float(a) for a in q)
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


class _Agent:
    def __init__(self, uav: str, no: int) -> None:
        self.uav, self.no = uav, no
        self.next_k = 0
        self.k_obs: int | None = None
        self.acks: list[dict] = []
        self.inbox: list[dict] = []
        self.dets: list[dict] = []
        self.frames: dict[str, list[dict]] = {}
        self.cmd_kind = "hover"
        self.goto_nav: np.ndarray | None = None
        self.goto_true: np.ndarray | None = None
        self.goto_speed = 8.0
        self.done = False
        self.term = {"terminated": False, "truncated": False, "reason": "none"}
        self.timeouts = 0
        self.flight_s = 0.0
        self.wall_s = 0.0


class BenchEnv:
    def __init__(self, spec: EnvSpec, *, world=None, tape=None, tier=None, cam: CameraModel | None = None, limits: Limits | None = None,
                 l2=None) -> None:
        """`l2`：L2 传感器工厂 `(env) -> L2Sensors`（测试注入 CPU 渲染替身）；缺省按 `spec.l2` 构造 EGL 光栅相机。"""
        from awr.sim.events.tape import open_tape

        if spec.obs_level not in ("L1", "L2", "L2-depth"):
            raise ValueError(f"obs_level {spec.obs_level!r}: ranked Env runs are L1, L2 or L2-depth")
        self.spec = spec
        self.uavs = [f.uav for f in spec.fleet]
        self.tape = tape or open_tape(Path(spec.event_dir))
        self.world = world or _open_world(Path(spec.world_dir))
        self.cam = cam or CameraModel()
        self.lim = limits or Limits()
        self.task = task_from_event(Path(spec.event_dir))
        self.duration_s = float(spec.duration_s or self.task["brief"]["duration_s"])
        self.n_ticks = round(self.duration_s * 1000 / TICK_MS)
        self.nav = NavModel(spec.seed, spec.noise)
        self.l1 = L1Model(spec.seed, self.world, self.tape, self.cam)
        self.link = LinkModel(spec.seed, tuple(self.uavs), self.world, self.tape, spec.comm)
        self.rec = EnvRecorder(spec.out_dir)
        self._tier = tier
        self._l2_factory = l2
        self.l2 = None
        self.possible_agents = list(self.uavs)
        self.agents: list[str] = []
        self.k = 0

    # ------------------------------------------------------------------ 组装
    def _make_l2(self):
        import json as _json

        from awr.agentsdk.env.l2 import L2Config, L2Sensors

        wd = Path(self.spec.world_dir)
        meta = _json.loads((wd / "world.json").read_text(encoding="utf-8")) if (wd / "world.json").is_file() else {}
        cfg = L2Config(**{k: v for k, v in (self.spec.l2 or {}).items() if k in ("camera_profile", "jpeg_quality", "lidar", "imu", "gnss")})
        return L2Sensors(world_dir=wd, tape=self.tape, uavs=self.uavs, seed=self.spec.seed, noise=self.spec.noise, obs_level=self.spec.obs_level,
                         nav=self.nav, mount_t_bs_m=self.cam.mount_t_bs_m, hfov_deg=self.cam.hfov_deg, cfg=cfg, world_meta=meta)

    def _feed_l2(self, k_prev: int) -> None:
        """物理档 (t_{k_prev}, t_k] 的样本与 t_k 的云台角交给 L2 传感器。"""
        if self.l2 is None:
            return
        t = self.k * TICK_NS
        for u in self.uavs:
            sl = self.tier.history.slice(u, k_prev * TICK_NS, t)
            self.l2.ingest(u, sl.t_ns, sl.state)
            g = self.gim[u]
            self.l2.gimbal(u, t, g.yaw_deg, g.pitch_deg)

    def _make_tier(self):
        if self._tier is not None:
            return self._tier
        spawn = {f.uav: (f.pos_enu_m, f.yaw_rad) for f in self.spec.fleet}
        grounded = [f.uav for f in self.spec.fleet if not f.airborne]
        if self.spec.physics == "kinematic":
            from awr.phys.kinematic import KinematicTier

            return KinematicTier(self.uavs, spawn, lambda xy: self.world.height_dsm(np.asarray(xy, np.float64)), grounded=grounded)
        if self.spec.physics == "realistic":
            return _GzTier.create(self.spec, spawn, grounded, self.world)
        raise ValueError(f"physics {self.spec.physics!r} is not available in P0a")

    def agent_config(self, uav: str) -> dict:
        nominal = {"gnss_pos_std_m": self.nav.pos_std_m, "l1_ground_sigma_m": [self.l1.p.sig0_m, self.l1.p.sig_per_m]}
        brief = brief_for_agent(self.task, Path(self.spec.event_dir), nominal_noise=nominal)
        for a in brief["anchors"]:
            a["pos_enu_m"][2] = float(self.world.ground_dtm(np.array([a["pos_enu_m"][:2]]))[0])
        f = next(x for x in self.spec.fleet if x.uav == uav)
        e = self.nav.error(self.uavs.index(uav), 0)
        return {"schema": "drone4d.bench.agent_config.v1", "uav_id": uav, "kind": "uav", "peers": [u for u in self.uavs if u != uav],
                "vehicle": {"ref": f.vehicle_ref, "limits": self.lim.config(), "endurance_s": self.nav.endurance_s},
                "sensors": self.l2.configs(self.cam.config()) if self.l2 is not None else [self.cam.config()],
                "start": {"kind": "pose", "state": "airborne" if f.airborne else "landed",
                          "nav_pos_enu_m": [float(x) for x in np.asarray(f.pos_enu_m) + e]},
                "obs_level": self.spec.obs_level, "control_hz": 10, "decision": self.spec.decision.config(),
                "comm": {"enabled": self.spec.comm.enabled, "bucket_Bps": self.spec.comm.bucket_Bps, "burst_B": self.spec.comm.burst_B,
                         "msg_max_B": self.spec.comm.msg_max_B, "model": self.spec.comm.model},
                "episode": {"duration_s": self.duration_s},
                "world": {"id": Path(self.spec.world_dir).name, "content_version": self.tape.header["world"]["content_version"],
                          "origin": {"lat_deg": 0.0, "lon_deg": 0.0, "alt_m": 0.0}},
                "brief": brief, "protocol": "drone4d.bench.wire.v1"}

    # ------------------------------------------------------------------ PettingZoo 风格接口
    def reset(self, seed: int | None = None, options: dict | None = None):
        if seed is not None and seed != self.spec.seed:
            raise ValueError("the run seed is fixed by the run specification")
        self.tier = self._make_tier()
        self.tier.open()
        if self.spec.obs_level != "L1":
            self.l2 = self._l2_factory(self) if self._l2_factory is not None else self._make_l2()
        self.st = {u: _Agent(u, i) for i, u in enumerate(self.uavs)}
        self.gim = {u: Gimbal(cam=self.cam) for u in self.uavs}
        self.guard = {u: ActionGuard(u, [p for p in self.uavs if p != u], self.lim, self.task["report_types"], self.spec.comm.enabled,
                                     self.spec.comm.msg_max_B, self.cam) for u in self.uavs}
        self.pending_gim: list[tuple[int, str, tuple[float, float]]] = []
        self.pending_tx: list[tuple[int, str, list[dict]]] = []
        self._fleet_t, self._fleet = [], {"pos": [], "vel": [], "yaw": [], "nav": [], "status": []}
        self.k = 0
        self.agents = list(self.uavs)
        self.status = dict(self.tier.status())
        self.rec.write_json("task.json", self.task)
        self.rec.write_json("truth/run_seed.json", {"run_seed": self.spec.seed})
        self.configs = {u: self.agent_config(u) for u in self.uavs}
        for u, c in self.configs.items():
            self.rec.write_json(f"agents/{u}/config.json", c)
        self._t0 = time.perf_counter()
        self._feed_l2(0)
        self._sense(0)
        self._log_fleet()
        return self._observe(), {u: {"config": self.configs[u]} for u in self.uavs}

    def step(self, actions: dict[str, dict]):
        for u in list(actions):
            if u not in self.st or self.st[u].k_obs is None:
                raise KeyError(f"{u} has no observation in flight")
        for u, a in self.st.items():
            if a.k_obs is None or a.done:
                continue
            if u in actions:
                self._settle(a, actions[u])
            else:
                a.timeouts += 1
                a.acks.append({"k": a.k_obs, "item": "act", "index": 0, "result": "timeout", "code": 661})
                a.next_k = a.k_obs + 1
            a.k_obs = None
        while True:
            self._advance()
            if self.k >= self.n_ticks or all(a.done for a in self.st.values()):
                for a in self.st.values():
                    if not a.done:
                        a.term = {"terminated": False, "truncated": True, "reason": "time_limit"}
                break
            if any(not a.done and a.next_k <= self.k for a in self.st.values()):
                break
        obs = self._observe(final=self.k >= self.n_ticks)
        terms = {u: o["obs"]["term"]["terminated"] for u, o in obs.items()}
        truncs = {u: o["obs"]["term"]["truncated"] for u, o in obs.items()}
        for u in obs:
            if terms[u] or truncs[u]:
                self.st[u].done = True
        self.agents = [u for u in self.uavs if not self.st[u].done]
        return obs, dict.fromkeys(obs, 0.0), terms, truncs, {u: {} for u in obs}

    # ------------------------------------------------------------------ 内部
    def _row(self, u: str) -> np.ndarray:
        s = self.tier.history.last_sample(u)
        r = np.zeros(16)
        r[0:3], r[3:7], r[7:10], r[10:13] = s.pos_enu_m, s.q_ws_xyzw, s.vel_enu_mps, s.omega_b_rps
        return r

    def _settle(self, a: _Agent, act: dict) -> None:
        t_wall = time.perf_counter()
        apply_tick = a.k_obs + self.spec.decision.lat_ticks(TICK_MS)
        t_apply = apply_tick * TICK_NS
        row = self._row(a.uav)
        e = self.nav.error(a.no, t_apply)
        nav_pos = row[0:3] + self.nav.error(a.no, a.k_obs * TICK_NS)
        gz = float(self.world.ground_dtm(np.array([nav_pos[:2]]))[0])
        r = self.guard[a.uav].check(act, a.k_obs, nav_pos, _yaw(row[3:7]), e, gz)
        if r.nav is not None:
            n = r.nav
            kind = n["kind"]
            a.cmd_kind = kind
            a.goto_nav = a.goto_true = None
            if kind == "velocity":
                cmd = TierCommand(a.uav, "velocity", apply_tick, vel_enu_mps=n["vel_enu"], yaw_rate_rps=n["yaw_rate"])
            elif kind == "goto":
                a.goto_nav, a.goto_true, a.goto_speed = np.array(n["target_enu_nav"]), np.array(n["target_enu_true"]), n["speed"]
                cmd = TierCommand(a.uav, "goto", apply_tick, target_enu_m=n["target_enu_true"], speed_mps=n["speed"])
            elif kind == "takeoff":
                cmd = TierCommand(a.uav, "takeoff", apply_tick, alt_m=n["alt_m"])
            else:
                cmd = TierCommand(a.uav, kind, apply_tick)
            res = self.tier.submit([cmd])[0]
            if not res.ok:
                r.acks = [x for x in r.acks if x["item"] != "nav"] + [{"k": a.k_obs, "item": "nav", "index": 0, "result": "rejected",
                                                                     "code": res.code, "why": "tier"}]
        if r.gimbal is not None:
            self.pending_gim.append((apply_tick, a.uav, r.gimbal))
        if r.msgs:
            self.pending_tx.append((apply_tick, a.uav, r.msgs))
            self.link.stats[a.uav]["rejected"] += sum(1 for x in r.acks if x["item"] == "msg" and x["result"] == "rejected")
        for rep in r.reports:
            self.rec.on_report(a.uav, a.k_obs, apply_tick, t_apply, rep)
        self.rec.on_act(a.uav, a.k_obs, act, r.acks, apply_tick)
        a.acks.extend(r.acks)
        a.next_k = apply_tick
        a.wall_s += time.perf_counter() - t_wall

    def _advance(self) -> None:
        k = self.k
        for a in self.st.values():                          # 估计系目标随导航误差更新（≥ 5 cm）
            if a.cmd_kind == "goto" and a.goto_nav is not None and not a.done:
                tgt = a.goto_nav - self.nav.error(a.no, k * TICK_NS)
                if float(np.linalg.norm(tgt - a.goto_true)) >= 0.05:
                    a.goto_true = tgt
                    self.tier.submit([TierCommand(a.uav, "goto", k, target_enu_m=tuple(float(x) for x in tgt), speed_mps=a.goto_speed)])
        r = self.tier.advance(k)
        for c in r.contacts:
            self.rec.on_contact(c)
        self.status = dict(r.status)
        self.k = k + 1
        t = self.k * TICK_NS
        for g in self.gim.values():
            g.slew(TICK_MS / 1000.0)
        for item in [p for p in self.pending_gim if p[0] <= self.k]:
            self.gim[item[1]].command(*item[2])
        self.pending_gim = [p for p in self.pending_gim if p[0] > self.k]
        self._feed_l2(k)
        self.link.refill(TICK_MS / 1000.0)
        pos = {u: self._row(u)[0:3] for u in self.uavs}
        for apply_tick, u, msgs in [p for p in self.pending_tx if p[0] <= self.k]:
            self.link.transmit(u, msgs, apply_tick * TICK_NS, pos)
        self.pending_tx = [p for p in self.pending_tx if p[0] > self.k]
        for u, msgs in self.link.deliver_due(t).items():
            self.st[u].inbox.extend(msgs)
        for u, a in self.st.items():
            if self.status[u] in (VehicleStatus.AIRBORNE, VehicleStatus.LANDING):
                a.flight_s += TICK_MS / 1000.0
            if self.status[u] == VehicleStatus.CRASHED and not a.term["terminated"]:
                a.term = {"terminated": True, "truncated": False, "reason": "contact"}
                a.next_k = self.k
        self._sense(self.k)
        self._log_fleet()

    def _sense(self, k: int) -> None:
        t = k * TICK_NS
        if self.l2 is not None:
            live = [(u, a.no) for u, a in self.st.items() if not a.done and self.status[u] != VehicleStatus.CRASHED]
            for u, (frames, truth) in self.l2.sense_all(k, live).items():
                a = self.st[u]
                for sid, fs in frames.items():
                    a.frames.setdefault(sid, []).extend(fs)
                for tr in truth:
                    self.rec.on_vis(tr)
            return
        env = self.tape.env_at(t)
        for u, a in self.st.items():
            if a.done or self.status[u] == VehicleStatus.CRASHED:
                continue
            row = self._row(u)
            yaw = _yaw(row[3:7])
            g = self.gim[u]
            c, s = math.cos(yaw), math.sin(yaw)
            mt = self.cam.mount_t_bs_m
            o = row[0:3] + np.array([c * mt[0] - s * mt[1], s * mt[0] + c * mt[1], mt[2]])
            dets, truth = self.l1.frame(a.no, k, t, o, cam_axes(yaw, g.pitch_deg, g.yaw_deg), self.nav.error(a.no, t), env)
            for d, tr in zip(dets, truth, strict=True):
                self.rec.on_l1(u, k, t, d, tr)
            a.dets.extend(dets)

    def _observe(self, final: bool = False) -> dict:
        out = {}
        t = self.k * TICK_NS
        for u, a in self.st.items():
            if a.done or (a.next_k > self.k and not final and not a.term["terminated"]):
                continue
            row = self._row(u)
            nav, _e = self.nav.solution(a.no, self.k, t, row, a.flight_s)
            st = self.status[u]
            xy = np.array([nav["pos_enu_m"][:2]])
            reached = a.goto_nav is not None and float(np.linalg.norm(np.asarray(nav["pos_enu_m"]) - a.goto_nav)) < 1.0
            obs = {"schema": "drone4d.bench.obs.v1", "k": self.k, "t_ns": t, "nav": nav,
                   "status": {"mode": _MODE[st], "nav_cmd": _NAV_CMD.get(self.tier.tracker.kind(u), "hover"), "goto_reached": bool(reached),
                              "contact": st in (VehicleStatus.GROUNDED, VehicleStatus.CRASHED),
                              "safety": "low_battery" if nav["battery_soc"] < 0.2 else "normal", "locked_sensors": []},
                   "ground_hint": {"dsm_m": float(self.world.height_dsm(xy)[0]), "dtm_m": float(self.world.ground_dtm(xy)[0])},
                   "sensors": a.frames if self.l2 is not None else {"detections": a.dets}, "acks": a.acks, "comm": self.link.status(u), "dropped": {},
                   "term": a.term if (a.term["terminated"] or a.term["truncated"]) else {"terminated": False, "truncated": False, "reason": "none"}}
            msg = {"obs": obs, "inbox": a.inbox}
            self.guard[u].saw(a.dets)
            self.rec.on_obs(u, self.k, msg)
            out[u] = msg
            a.dets, a.acks, a.inbox, a.frames = [], [], [], {}
            a.k_obs = self.k
        return out

    def _log_fleet(self) -> None:
        self._fleet_t.append(self.k * TICK_NS)
        rows = [self._row(u) for u in self.uavs]
        self._fleet["pos"].append([r[0:3] for r in rows])
        self._fleet["vel"].append([r[7:10] for r in rows])
        self._fleet["yaw"].append([_yaw(r[3:7]) for r in rows])
        self._fleet["nav"].append([r[0:3] + self.nav.error(i, self.k * TICK_NS) for i, r in enumerate(rows)])
        self._fleet["status"].append([str(self.status[u]) for u in self.uavs])

    def close(self) -> dict:
        info = self.tier.close() if hasattr(self, "tier") else {}
        if self.l2 is not None:
            self.l2.close()
        wall = time.perf_counter() - self._t0 if hasattr(self, "_t0") else 0.0
        self.rec.net(self.link.records)
        self.rec.fleet(self.uavs, self._fleet_t, self._fleet["pos"], self._fleet["vel"], self._fleet["yaw"], self._fleet["nav"],
                       self._fleet["status"])
        stats = {u: {"timeouts": a.timeouts, "flight_s": round(a.flight_s, 1), "agent_wall_s": round(a.wall_s, 3),
                     "comm": self.link.stats[u], "term": a.term} for u, a in self.st.items()}
        manifest = {"schema": "drone4d.bench.env_run.v1", "run_id": self.spec.run_id, "world": Path(self.spec.world_dir).name,
                    "event": {"id": self.tape.header["event_id"], "version": self.tape.header["event_version"],
                              "content_sha256": self.tape.hashes()["content_sha256"]},
                    "task_id": self.task["task_id"], "fleet": [{"uav": f.uav, "vehicle_ref": f.vehicle_ref, "pos_enu_m": list(f.pos_enu_m),
                                                                "yaw_rad": f.yaw_rad} for f in self.spec.fleet],
                    "obs_level": self.spec.obs_level, "noise": self.spec.noise, "physics": self.spec.physics, "tier": getattr(self.tier, "name", None),
                    "ranked": self.spec.ranked, "policy": self.spec.policy, "decision": self.spec.decision.config(),
                    "comm": {"enabled": self.spec.comm.enabled, "bucket_Bps": self.spec.comm.bucket_Bps},
                    "duration_s": self.duration_s, "n_ticks": self.k, "pose_sha256": info.get("pose_sha256"), "tier_info": info,
                    "wall_s": round(wall, 2), "l1_rays": self.l1.n_rays, "agents": stats}
        self.rec.write_json("manifest.json", manifest)
        self.rec.close()
        return manifest


def _open_world(world_dir: Path):
    try:
        from awr.world.mesh.query import open_mesh_world_query

        return open_mesh_world_query(world_dir)
    except Exception:
        from awr.world.geometry.query import open_dsm_world_query

        return open_dsm_world_query(world_dir)


SPAWN_FOOT = np.array([[dx, dy] for dx in (-0.7, -0.35, 0.0, 0.35, 0.7) for dy in (-0.7, -0.35, 0.0, 0.35, 0.7)])
SPAWN_LEVEL_M = 0.15


class _GzTier:
    """Realistic 档：每 Run 一个 gz 实例（GzInstance + compose_run + GzSimBackend），关闭时停止实例。"""

    @staticmethod
    def create(spec: EnvSpec, spawn: dict, grounded: list[str], world):
        from awr.agentsdk.env.geom import surface_z
        from awr.phys.gz.backend import GzSimBackend
        from awr.phys.gz.compose import UavSpawn, compose_run
        from awr.phys.gz.instance import GzInstance
        from awr.phys.kinematic import GEAR_Z_M
        from awr.world.export.gz import export_world

        o = spec.gz
        exp = export_world(Path(spec.world_dir), out_root=o.get("export_root"), check_sdf=o.get("check_sdf"))
        run_dir = Path(o.get("run_dir") or (Path(spec.out_dir) / "phys" if spec.out_dir else Path("/tmp") / f"d4d-{spec.run_id}"))
        inst = GzInstance(run_dir, o.get("name", f"d3-gz-{spec.run_id}"), image=o.get("image", "d3-gz:dev"), launcher=o.get("launcher", "docker"),
                          world_export=exp.root)
        spawns = []
        for u, (p, yaw) in spawn.items():
            z = float(p[2])
            if u in grounded:
                foot = np.asarray(p[:2], np.float64) + SPAWN_FOOT
                zs = surface_z(world, foot)
                if float(np.ptp(zs)) > SPAWN_LEVEL_M:            # 机体跨在台阶或坑上：起飞失败或侧向碰撞，运行无效
                    raise ValueError(f"{u}: spawn surface not level ({np.ptp(zs):.2f} m over the airframe footprint)")
                z = float(zs.max()) + GEAR_Z_M + 0.01
            spawns.append(UavSpawn(u, (float(p[0]), float(p[1]), z), yaw))
        inst.start(compose_run(spawns, world_static_uri=exp.uri, endpoint=inst.gz_endpoint, spherical=exp.spherical))
        try:
            inst.wait_socket(float(o.get("ready_timeout_s", 120)))
            be = GzSimBackend(list(spawn), inst.endpoint, grounded=grounded, run_id=spec.run_id, timeout_s=float(o.get("step_timeout_s", 30)))
        except Exception:
            inst.stop()
            raise
        return _GzHandle(be, inst)


class _GzHandle:
    def __init__(self, be, inst) -> None:
        self.be, self.inst = be, inst
        self.name = be.name

    def __getattr__(self, k):
        return getattr(self.be, k)

    def close(self) -> dict:
        try:
            return self.be.close()
        finally:
            self.inst.stop()
