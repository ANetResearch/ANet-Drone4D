"""合成录制生成器（M12 §9.1 `synth.py`；测试夹具与基准，§9.3 "数据合成改用 awr.recorder.synth"）。

用与 recorder 进程完全相同的 `RecorderCore` 写出一段真实的 awr 录制（MCAP、`.ovw`、`.evx`、`meta.json`），只是帧与总线
消息由确定性的合成机群直接喂入（不经 StateRing 与 zenoh）：N 架无人机绕各自圆心匀速转弯（位置、速度、姿态与
机体角速度解析给出），tap 频率按倍率 `max(8 ms, rate × 4 ms)`，`state_ext` 2 Hz、环境心跳 1 Hz、事件按给定速率，
可注入 checkpoint 回滚（生产者纪元 + 1、时间回退到 restored_t）与剧本重置（仿真段 + 1、时间归零）。
每个仿真秒 flush 一次 chunk 并等待 writer 排空（无丢弃），同一参数两次生成的消息序列逐字节相同（M12-AC-032）。

用法：`python -m awr.recorder.synth --out runs/r20260929-000000-0001 --n 1000 --sim-s 600`
"""

from __future__ import annotations

import argparse
import json
import math
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import msgpack
import numpy as np

from awr.contracts import CONTRACTS_VERSION, LAYOUT_ID
from awr.contracts.enums import FlightFlags, FlightState
from awr.contracts.layouts import DRONE_STATE64, SWARM_LITE32
from awr.runtime.bus import LocalBus

from .app import RecorderCore
from .config import RecorderCfg
from .meta import MetaFile

__all__ = ["SynthFleet", "SynthFrame", "synthesize"]

TICK_NS = 4_000_000


def _pack(x: Any) -> bytes:
    return msgpack.packb(x, use_bin_type=True)


class SynthFleet:
    def __init__(self, n: int, seed: int = 7, *, id_prefix: str = "sim") -> None:
        rng = np.random.default_rng(seed)
        self.n = n
        self.ids = [f"{id_prefix}-{i:04d}" for i in range(n)]
        self.ctr = rng.uniform(-400, 400, (n, 2))
        self.rad = rng.uniform(30, 120, n)
        self.w = rng.uniform(0.05, 0.2, n) * rng.choice([-1, 1], n)
        self.alt = rng.uniform(40, 120, n)
        self.ph0 = rng.uniform(0, 2 * math.pi, n)
        self.lite = np.zeros(n, SWARM_LITE32)
        self.full = np.zeros(n, DRONE_STATE64)
        self.lite["agent_no"] = np.arange(n)
        self.full["agent_no"] = np.arange(n)
        fs = int(FlightState.FLYING)
        fl = int(FlightFlags.ARMED | FlightFlags.IN_AIR | FlightFlags.LOC_OK)
        self.lite["flight_state"] = fs
        self.full["flight_state"] = fs
        self.lite["flags"] = fl
        self.full["flags"] = fl
        self.full["mission_item"] = 0xFFFF

    def state(self, t_ns: int) -> tuple[np.ndarray, ...]:
        t = t_ns * 1e-9
        ph = self.ph0 + self.w * t
        x = self.ctr[:, 0] + self.rad * np.cos(ph)
        y = self.ctr[:, 1] + self.rad * np.sin(ph)
        z = self.alt + 3 * np.sin(0.1 * t + self.ph0)
        vx = -self.rad * self.w * np.sin(ph)
        vy = self.rad * self.w * np.cos(ph)
        vz = 0.3 * np.cos(0.1 * t + self.ph0)
        yaw = ph + np.sign(self.w) * math.pi / 2
        return x, y, z, vx, vy, vz, yaw

    def battery(self, t_ns: int) -> int:
        return int(max(0, min(100, 100 - t_ns * 1e-9 / 20)))

    def lite_bytes(self, t_ns: int) -> bytes:
        x, y, z, vx, vy, vz, yaw = self.state(t_ns)
        L = self.lite
        L["pos"][:, 0], L["pos"][:, 1], L["pos"][:, 2] = x, y, z
        L["vel_cms"][:, 0] = np.clip(np.round(vx * 100), -32767, 32767)
        L["vel_cms"][:, 1] = np.clip(np.round(vy * 100), -32767, 32767)
        L["vel_cms"][:, 2] = np.clip(np.round(vz * 100), -32767, 32767)
        L["q_snorm"][:, 0] = 0
        L["q_snorm"][:, 1] = 0
        L["q_snorm"][:, 2] = np.round(np.sin(yaw / 2) * 32767)
        L["q_snorm"][:, 3] = np.round(np.cos(yaw / 2) * 32767)
        L["battery_pct"] = self.battery(t_ns)
        return L.tobytes()

    def full_bytes(self, t_ns: int) -> bytes:
        x, y, z, vx, vy, vz, yaw = self.state(t_ns)
        F = self.full
        F["pos"][:, 0], F["pos"][:, 1], F["pos"][:, 2] = x, y, z
        F["vel"][:, 0], F["vel"][:, 1], F["vel"][:, 2] = vx, vy, vz
        F["q"][:, 2] = np.sin(yaw / 2)
        F["q"][:, 3] = np.cos(yaw / 2)
        F["omega"][:, 2] = self.w
        F["battery_pct"] = self.battery(t_ns)
        return F.tobytes()

    def roster(self, version: int = 1) -> dict[str, Any]:
        return {"v": 1, "producer": "sim-core", "roster_version": version, "id_base": 0, "id_count": 1024,
                "entries": [{"agent_no": i, "id": vid, "model": "p600", "kind": "uav", "producer": "sim-core", "simulated": True,
                             "lifecycle": "READY"} for i, vid in enumerate(self.ids)]}

    def ext(self, t_ns: int) -> bytes:
        bat = self.battery(t_ns)
        return _pack([[i, {"lifecycle": "READY", "mode_text": "AUTO.MISSION", "voltage_v": round(22.0 + bat / 50, 2),
                           "gps": {"fix": 3, "sats": 14}}] for i in range(self.n)])

    def env(self, t_ns: int, version: int) -> bytes:
        ts = t_ns * 1e-9
        return _pack({"schema": "awr.env.keyframe.v1", "version": version, "epoch": 1, "t_ns": t_ns, "mode": "smooth",
                      "anchors": {"t_ns": t_ns, "s_m": round(ts * 6.0, 9), "d_enu_m": [round(ts * 4.2, 9), round(-ts * 4.2, 9), 0.0],
                                  "fall_rain_m": 0.0, "fall_snow_m": 0.0, "wetness": 0.1, "puddle": 0.02},
                      "events": [], "config": {"presets_sha256": "0" * 64}})


@dataclass
class SynthFrame:
    """与 `awr.runtime.statering.Frame` 同名字段；lite/full 在首次访问时生成（recorder 只在需要时读取）。"""

    fleet: SynthFleet
    frame_seq: int
    t_sim_ns: int
    epoch: int
    roster_version: int = 1
    flags: int = 0

    @property
    def t_pub_ns(self) -> int:
        return self.t_sim_ns

    @property
    def n_rows(self) -> int:
        return self.fleet.n

    @property
    def lite(self) -> bytes:
        return self.fleet.lite_bytes(self.t_sim_ns)

    @property
    def full(self) -> bytes:
        return self.fleet.full_bytes(self.t_sim_ns)


def synthesize(persist_dir: Path, *, run_id: str | None = None, n: int = 20, sim_s: float = 20.0, seed: int = 7,
               rate: float | Callable[[int], float] = 1.0, events_per_s: float = 5.0, critical_every_s: float = 0.0,
               lineage: tuple[int, int] | None = None, resets: int = 0, marked: list[str] | None = None,
               world_id: str = "shenzhen", content_version: str = "synth", coordinate_sha256: str = "0" * 64,
               stop: bool = True, queue_mb: int = 64) -> dict[str, Any]:
    """写出一段合成录制到 persist_dir（= runs/<run>/）；返回摘要。

    lineage = (t_crash_ns, t_restore_ns)：到 t_crash 后生产者纪元 + 1，时间回退到 t_restore 继续（只作用于第一个仿真段）；
    resets：剧本重置次数，每个仿真段 sim_s 秒，重置时仿真段 + 1、时间归零、纪元 + 1（得到 resets + 1 个文件段）。
    """
    persist_dir = Path(persist_dir)
    persist_dir.mkdir(parents=True, exist_ok=True)
    run_id = run_id or persist_dir.name
    binding = {"world_id": world_id, "content_version": content_version, "coordinate_sha256": coordinate_sha256,
               "layout_id": LAYOUT_ID, "contracts_version": CONTRACTS_VERSION}
    meta = MetaFile(persist_dir, run_id, binding)
    meta.set_binding(binding)
    meta.save()
    fleet = SynthFleet(n, seed)
    rate_fn: Callable[[int], float] = rate if callable(rate) else (lambda _t, r=float(rate): r)
    bus = LocalBus.open("recorder", namespace=f"awr/{world_id}/synth-{secrets.token_hex(4)}")
    scen = {"record": True, "vehicles": [{"vehicle_id": v, "marked": True} for v in (marked or [])]}
    core = RecorderCore(bus=bus, ring_path=persist_dir / "no-ring", persist_dir=persist_dir, run_id=run_id, world_id=world_id,
                        cfg=RecorderCfg(queue_mb=queue_mb, flush_wall_s=3600.0), scenario=scen, autostart=False)
    core.feed_roster(fleet.roster())
    epoch = 1
    core.set_sim_segment(0, epoch)
    core.feed_state("env", fleet.env(0, 0))
    core.start("synth")
    seq_ev = 0
    frame_seq = 0
    env_version = 0
    t_end = int(sim_s * 1e9)
    ev_period = int(1e9 / events_per_s) if events_per_s > 0 else 0
    crit_period = int(critical_every_s * 1e9) if critical_every_s > 0 else 0
    t0_wall = time.perf_counter()

    def event(kind: str, sev: int, t_ns: int, uav: str | None, data: dict) -> None:
        nonlocal seq_ev
        seq_ev += 1
        core.feed_events("sim-core", [{"seq": seq_ev, "epoch": epoch, "producer": "sim-core", "kind": kind, "severity": sev,
                                       "t_sim_ns": t_ns, "t_wall_ns": 0, "uav": uav, "cid": None, "data": data}])

    for segment in range(resets + 1):
        if segment:
            epoch += 1
            core.set_sim_segment(segment, epoch)
        t = 0
        next_ext, next_env, next_flush = 0, 1_000_000_000, 1_000_000_000
        next_ev = ev_period if ev_period else t_end + 1
        next_crit = crit_period if crit_period else t_end + 1
        crashed = segment > 0 or lineage is None
        while t <= t_end:
            r = rate_fn(t)
            if not crashed and lineage is not None and t >= lineage[0]:
                crashed = True
                epoch += 1
                event("sim.restarted", 2, lineage[1], None, {"restored_t_sim_ns": lineage[1]})
                t = lineage[1]
                next_ext = t
                next_ev = t + ev_period if ev_period else t_end + 1
                next_crit = t + crit_period if crit_period else t_end + 1
            frame_seq += 1
            core.feed_frame(SynthFrame(fleet, frame_seq, t, epoch), r)
            if t >= next_ext:
                core.feed_state("ext", fleet.ext(t))
                next_ext += 500_000_000
            if t >= next_env:
                env_version += 1
                core.feed_state("env", fleet.env(t, env_version))
                next_env += 1_000_000_000
            while t >= next_ev:
                event("mission.item_reached", 0, next_ev, fleet.ids[seq_ev % n], {"item": seq_ev % 97})
                next_ev += ev_period
            while t >= next_crit:
                event("safety.geofence", 3, next_crit, fleet.ids[0], {"zone": "z1"})
                next_crit += crit_period
            if t >= next_flush:
                core.writer.flush()
                core.writer.drain(30)
                next_flush += 1_000_000_000
            t += max(8_000_000, round(r) * TICK_NS)
    if stop:
        core.stop("stop")
    core.close()
    bus.close()
    return {"run_id": run_id, "dir": str(persist_dir), "segments": MetaFile(persist_dir, run_id).segments(),
            "wall_s": time.perf_counter() - t0_wall, "frames": frame_seq, "events": seq_ev, "fleet": fleet.ids}


def main() -> None:
    ap = argparse.ArgumentParser(description="合成 awr 录制（M12 synth）")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--sim-s", type=float, default=60.0)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--rate", type=float, default=1.0)
    ap.add_argument("--events-per-s", type=float, default=5.0)
    ap.add_argument("--marked", type=int, default=16)
    a = ap.parse_args()
    ids = [f"sim-{i:04d}" for i in range(min(a.marked, a.n))]
    out = synthesize(a.out, n=a.n, sim_s=a.sim_s, seed=a.seed, rate=a.rate, events_per_s=a.events_per_s, marked=ids)
    print(json.dumps({k: v for k, v in out.items() if k != "fleet"}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
