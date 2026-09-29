"""ReplayBackend（L0 运动学回放，M08-FR-068；M08 §6.11.4；ADR-020 L0；能力声明 `packages/contracts/rt/caps/replay.json`）。

数据源：①`awr.traj.v1`：CSV 或 NPZ，列 `t_s, e_m, n_m, u_m, qx, qy, qz, qw`（可选 `ve, vn, vu`），World ENU；②PX4 SIH 黄金 CSV
（`LOCAL_POSITION_NED` + `ATTITUDE`，约 21 Hz）：local NED 的原点为该实例 EKF 原点，按 `offset_enu_m` 放到 World ENU；
插值切向取位置的中心差分（EKF 速度与位置差分不自洽，见 `load_sih_csv`）
（NED → ENU 轴置换与 M02 同式，姿态由 roll/pitch/yaw 构造 FRD→NED 四元数再换算为 WORLD←FLU）。
插值在 `stages/kinematic.py`：位置三次 Hermite、姿态 slerp，时刻为世界时钟 `t_sim − t0`（随暂停、单步、倍速一致变化）；
超出末端保持最后一帧并置 EVT_ARRIVED。命令一律 `109 BACKEND_UNSUPPORTED`；幽灵机 `fidelity = L0`（8），contact、guard、
电量不处理，`pose_src = KINEMATIC`。每实例规模 ≤ 100（本文设定）。
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import msgpack
import numpy as np

from awr.contracts.enums import LIFECYCLE_NAMES
from awr.contracts.reasons import Reason

from ..fleet.fleet import FleetSim
from ..fleet.kernels_tap import INV_SQRT2
from ..fleet.pipeline import StageCtx
from ..fleet.stages.kinematic import Track
from ..fleet.stages.registry import Fidelity
from .base import BackendCaps, DispatchResult, EntitySpec, Kind, Pose, load_caps

__all__ = ["MAX_GHOSTS", "ReplayBackend", "ReplayDroneView", "load_sih_csv", "load_traj", "ned_rpy_to_enu_xyzw"]

MAX_GHOSTS = 100


def ned_rpy_to_enu_xyzw(rpy: np.ndarray) -> np.ndarray:
    """PX4 ATTITUDE（roll, pitch, yaw；FRD→NED，ZYX）→ WORLD←FLU 四元数 [x, y, z, w]（M02 对合公式）。"""
    r, p, y = rpy[:, 0] / 2, rpy[:, 1] / 2, rpy[:, 2] / 2
    cr, sr, cp, sp, cy, sy = np.cos(r), np.sin(r), np.cos(p), np.sin(p), np.cos(y), np.sin(y)
    w = cr * cp * cy + sr * sp * sy
    x = sr * cp * cy - cr * sp * sy
    yq = cr * sp * cy + sr * cp * sy
    z = cr * cp * sy - sr * sp * cy
    return np.stack([(x + yq) * INV_SQRT2, (x - yq) * INV_SQRT2, (w - z) * INV_SQRT2, (w + z) * INV_SQRT2], 1)


def load_traj(path: Path) -> Track:
    """`awr.traj.v1`（CSV 带表头或 NPZ）。"""
    path = Path(path)
    if path.suffix == ".npz":
        d = np.load(path)
        cols = {k: np.asarray(d[k], np.float64) for k in d.files}
    else:
        with open(path, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        cols = {k: np.array([float(r[k]) for r in rows]) for k in rows[0]}
    t = cols["t_s"]
    pos = np.stack([cols["e_m"], cols["n_m"], cols["u_m"]], 1)
    q = np.stack([cols["qx"], cols["qy"], cols["qz"], cols["qw"]], 1) if "qw" in cols else None
    vel = np.stack([cols["ve"], cols["vn"], cols["vu"]], 1) if "ve" in cols else None
    return Track.build(t, pos, q, vel)


def load_sih_csv(path: Path, *, t0_wall: float | None = None, offset_enu_m=(0.0, 0.0, 0.0),
                 duration_s: float | None = None) -> Track:
    """PX4 SIH 黄金 CSV（`lpos` 与 `att` 行）；`t0_wall` 给出时从该墙钟时刻起截取（marks.csv），否则取全段。"""
    lp, at = [], []
    with open(path, encoding="utf-8") as fh:
        for r in csv.reader(fh):
            (lp if r[0] == "lpos" else at).append([float(x) for x in r[1:]])
    lp_a, at_a = np.array(lp), np.array(at)
    if t0_wall is not None:
        lp_a = lp_a[lp_a[:, 0] >= t0_wall]
        at_a = at_a[at_a[:, 0] >= t0_wall]
    tb0 = lp_a[:, 1].min()
    t = (lp_a[:, 1] - tb0) / 1000.0
    ned = lp_a[:, 2:5]
    vned = lp_a[:, 5:8]
    off = np.asarray(offset_enu_m, np.float64)
    pos = np.stack([ned[:, 1], ned[:, 0], -ned[:, 2]], 1) + off
    vel = np.stack([vned[:, 1], vned[:, 0], -vned[:, 2]], 1)
    ta = (at_a[:, 1] - tb0) / 1000.0
    q_at = ned_rpy_to_enu_xyzw(at_a[:, 2:5])
    # 姿态按位置采样时刻取最近的 ATTITUDE（两路 21 Hz 流异步）
    k = np.clip(np.searchsorted(ta, t), 0, len(ta) - 1)
    k0 = np.clip(k - 1, 0, len(ta) - 1)
    k = np.where(np.abs(ta[k0] - t) < np.abs(ta[k] - t), k0, k)
    q = q_at[k]
    if duration_s is not None:
        m = t <= duration_s
        t, pos, vel, q = t[m], pos[m], vel[m], q[m]
    # LOCAL_POSITION_NED 的速度与位置差分不自洽（EKF 输出时间对齐差，inst1 最大 0.8 m/s）：以其为 Hermite 切向会在
    # 段内产生 ~90 m/s² 的伪加速度；插值切向改用中心差分（位置在采样时刻仍精确），速度列只用于核对。
    _ = vel
    return Track.build(t, pos, q, None)


class ReplayDroneView:
    kind = Kind.UAV

    def __init__(self, fleet: FleetSim, slot: int, entity_id: str, agent_no: int) -> None:
        self._f = fleet
        self.slot = slot
        self.id = entity_id
        self.agent_no = agent_no

    def lifecycle(self) -> str:
        return LIFECYCLE_NAMES[int(self._f.S.lifecycle[self.slot])]

    def pose(self) -> Pose:
        S = self._f.S
        e = S.enu
        return Pose(S.t_ns, e.pos[self.slot].copy(), e.q_xyzw[self.slot].copy(), e.vel[self.slot].copy(),
                    e.omega_flu[self.slot].copy())

    def capabilities(self) -> list[str]:
        return []

    def derive_state(self) -> dict[str, Any]:
        return {"fs": 5, "sub": 7, "pose_src": "KINEMATIC", "native": None}  # 缺省 FLYING/EXTERNAL

    def frames(self) -> Any:
        return None


class ReplayBackend:
    name = "replay"

    def __init__(self, fleet: FleetSim, *, ctx: StageCtx | None = None) -> None:
        self.fleet = fleet
        self.caps: BackendCaps = load_caps("replay")
        self.ctx = ctx or StageCtx(profiles=fleet.T, paths=fleet.PB, events=fleet.events)
        self.views: dict[str, ReplayDroneView] = {}
        self._next = 0

    def attach(self, world: Any, clock: Any, bus: Any, ring: Any) -> None:
        self.world, self.clock = world, clock

    @staticmethod
    def track_from_source(src: dict) -> Track:
        if "track" in src:
            return src["track"]
        if "traj" in src:
            return load_traj(Path(src["traj"]))
        if "sih_csv" in src:
            return load_sih_csv(Path(src["sih_csv"]), t0_wall=src.get("t0_wall"), offset_enu_m=src.get("offset_enu_m", (0, 0, 0)),
                                duration_s=src.get("duration_s"))
        raise ValueError("ReplayBackend source 需要 traj、sih_csv 或 track")

    def spawn(self, spec: EntitySpec, *, slot: int | None = None, agent_no: int | None = None) -> ReplayDroneView:
        if len(self.views) >= MAX_GHOSTS:
            raise OverflowError("CAPACITY")
        track = self.track_from_source(spec.source or {})
        S = self.fleet.S
        s = int(np.flatnonzero(~S.active)[0]) if slot is None else slot
        no = self._next if agent_no is None else agent_no
        self._next = max(self._next, no + 1)
        eid = spec.entity_id or f"ghost-{no:02d}"
        p0 = track.pos[0]
        spec2 = EntitySpec(eid, Kind.UAV, spec.profile_id, spec.limits_profile, (float(p0[0]), float(p0[1]), float(p0[2])),
                           spec.yaw_rad)
        self.fleet.add(spec2, slot=s, agent_no=no, entity_id=eid, t_s=self.ctx.t_ns * 1e-9, fidelity=Fidelity.L0)
        self.fleet.kinematic.attach(s, track, self.ctx.t_ns * 1e-9)
        S.lifecycle[s] = 4
        v = ReplayDroneView(self.fleet, s, eid, no)
        self.views[eid] = v
        return v

    def despawn(self, entity_id: str) -> None:
        v = self.views.pop(entity_id, None)
        if v is not None:
            self.fleet.remove(v.slot)

    def dispatch_batch(self, cmds: Sequence[Any]) -> list[DispatchResult]:
        out = []
        S = self.fleet.S
        for c in cmds:
            slots = c.slots if hasattr(c, "slots") else c["slots"]
            out += [DispatchResult(S.ids[int(s)] or "", False, int(Reason.BACKEND_UNSUPPORTED), False, "L0_REPLAY")
                    for s in slots]
        return out

    def step(self, tick: int) -> None:
        n = int(tick) - int(self.ctx.tick)
        if n > 0:
            self.fleet.step(self.ctx, n)

    def snapshot(self) -> bytes:
        k = self.fleet.kinematic
        return msgpack.packb({"t0": {str(s): v for s, v in k.t0.items()}, "tick": int(self.ctx.tick)}, use_bin_type=True)

    def restore(self, blob: bytes) -> None:
        d = msgpack.unpackb(blob, raw=False, strict_map_key=False)
        for s, v in d["t0"].items():
            if int(s) in self.fleet.kinematic.tracks:
                self.fleet.kinematic.t0[int(s)] = float(v)
        self.fleet.kinematic._dirty = True
        self.ctx.tick = int(d["tick"])
        self.ctx.t_ns = self.ctx.tick * 4_000_000
