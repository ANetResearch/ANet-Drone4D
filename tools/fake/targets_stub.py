"""M18 target state block and read-only views: frozen field layout, types and a deterministic fake (WP-01, D2-MS1; AWR-04 §7;
M08 §15.3.4; M18-M19 PRD §6.2-§6.5, §7.1).

Frozen here (tests/contracts/test_d2_stubs.py compares with `awr.sim.targets` once M18 delivers):
- the entity block `targets` (EntityTables["target"], capacity 32): M08 owns active, target_no, roster_flags; M18 the tgt_* motion,
  awareness and keepout columns; M19 the best-level and capture columns; M20 the relay column (one writer per column);
- TargetConfig, TargetState1, TargetView, Volume and VehicleSignature (read-only views used by M19 and M20);
- critical_dim and keepout_volumes signatures.

FakeTargets drives targets with the contract shapes (TargetLite32 rows, awr.target.detail.v1, roster entries) for fake_gw, fixtures and
tests; it is not the M18 model (no discovery criterion, no LOS, simplified random walk). Pure Python + numpy, no awr.sim imports.
"""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from awr.contracts import enums, layouts

ROOT = Path(__file__).resolve().parents[2]
CLASS_DEFAULTS = json.loads((ROOT / "packages/contracts/target/class_defaults.json").read_text(encoding="utf-8"))
CAPACITY = 32
LIMIT = 30

# ---------------------------------------------------------------- entity block `targets` (register_entity_block field specs)
F64, F32, U8, U16, U32, I16 = "<f8", "<f4", "u1", "<u2", "<u4", "<i2"
M08_FIELDS: dict[str, tuple[str, tuple[int, ...]]] = {"active": (U8, ()), "target_no": (U16, ()), "roster_flags": (U8, ())}
M18_FIELDS: dict[str, tuple[str, tuple[int, ...]]] = {
    "tgt_cls": (U8, ()), "tgt_pos": (F64, (3,)), "tgt_vel": (F64, (3,)), "tgt_yaw": (F64, ()), "tgt_speed": (F64, ()),
    "tgt_size": (F32, (3,)), "tgt_occl": (F32, ()), "tgt_aware": (U8, ()), "tgt_alert": (F32, ()), "tgt_crit": (U8, ()),
    "tgt_enabled": (U8, ()), "tgt_moving": (U8, ()), "tgt_occupied": (U8, ()), "tgt_sound_on": (U8, ()), "tgt_hidden": (U8, ()),
    "tgt_disc_recent": (U8, ()), "tgt_disc_n": (U16, ()), "tgt_episode": (U16, ()), "tgt_n_vol": (U8, ()), "tgt_keepout_r": (F32, ()),
    "tgt_cfg_rev": (U32, ()), "tgt_path_idx": (I16, ()), "tgt_sound_phase": (F32, ()),
}
M19_FIELDS: dict[str, tuple[str, tuple[int, ...]]] = {"tgt_best_level": (U8, ()), "tgt_captured": (U8, ()), "tgt_best_sensor": (U8, ()),
                                                      "tgt_capture_uav_no": (U16, ())}
M20_FIELDS: dict[str, tuple[str, tuple[int, ...]]] = {"tgt_relay": (U8, ())}
BLOCK_FIELDS = {"M08": M08_FIELDS, "M18": M18_FIELDS, "M19": M19_FIELDS, "M20": M20_FIELDS}


def new_block(capacity: int = CAPACITY) -> dict[str, np.ndarray]:
    """Allocate the SoA block as M08 would (shape (capacity, *shape))."""
    out = {}
    for spec in BLOCK_FIELDS.values():
        for name, (dt, shape) in spec.items():
            out[name] = np.zeros((capacity, *shape), np.dtype(dt))
    out["tgt_capture_uav_no"][:] = 0xFFFF
    out["tgt_path_idx"][:] = -1
    return out


# ---------------------------------------------------------------- views
@dataclass(frozen=True)
class TargetConfig:
    """Merged target configuration (class template + overrides), awr.target.v1 fields."""
    id: str
    cls: str
    raw: dict = field(repr=False, default_factory=dict)

    def __getattr__(self, name: str) -> Any:          # attribute access to the awr.target.v1 fields
        raw = object.__getattribute__(self, "raw")
        if name in raw:
            return raw[name]
        raise AttributeError(name)

    @classmethod
    def from_dict(cls, d: dict, tid: str | None = None) -> TargetConfig:
        c = d["class"]
        merged = copy.deepcopy(CLASS_DEFAULTS["classes"][c])
        for k, v in d.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k] = {**merged[k], **v}
            else:
                merged[k] = copy.deepcopy(v)
        merged.setdefault("critical_dim_m", None)
        merged.setdefault("n_req_px", None)
        return cls(tid or d.get("id") or f"{c}-01", c, merged)

    def to_dict(self) -> dict:
        return {"id": self.id, **copy.deepcopy(self.raw)}


@dataclass(frozen=True)
class TargetState1:
    id: str
    target_no: int
    pos: tuple[float, float, float]
    yaw_rad: float
    vel: tuple[float, float, float]
    speed_mps: float
    moving: bool
    awareness: str
    alertness: float


@dataclass(frozen=True)
class TargetView:
    """Read-only view for M19 and M20 (arrays are slices of the targets block, target_no order)."""
    n: int
    target_no: NDArray
    cls: NDArray
    pos: NDArray
    yaw_rad: NDArray
    vel: NDArray
    size_m: NDArray
    occlusion: NDArray
    cfg: tuple[TargetConfig, ...]
    rev: int


@dataclass(frozen=True)
class Volume:
    """Keepout or sensitivity volume in world coordinates (target/keepout.schema.json fields)."""
    shape: str                       # sphere, hemisphere, cylinder, sector
    center: tuple[float, float, float]
    r_m: float
    z_min_m: float
    z_max_m: float
    yaw_rad: float = 0.0
    half_angle_rad: float = math.pi
    vol_idx: int = 0
    kind: str = "visual"

    def to_wire(self, vid: str) -> dict:
        d = {"id": vid, "shape": self.shape, "center_enu_m": list(self.center), "r_m": self.r_m, "z_min_m": self.z_min_m, "z_max_m": self.z_max_m,
             "source": {"kind": "target", "id": vid.split("/")[0], "vol_idx": self.vol_idx}}
        if self.shape == "sector":
            d.update(yaw_rad=self.yaw_rad, half_angle_rad=self.half_angle_rad)
        return d


@dataclass(frozen=True)
class VehicleSignature:
    """Subset of the model signature used by targets and perception (M21 provides the real one)."""
    model_id: str
    hold_sigma_h_m: float = 0.02     # RTK tier
    lwa_hover_dba: float | None = 88.0
    lwa_cruise_dba: float | None = None
    area_m2: tuple[float, float, float] = (0.36, 0.12, 0.12)   # top, side, front
    contrast_sky: float = -0.8
    lights_nav: bool = False


P600 = VehicleSignature("p600_mid360", 0.02, 88.0, None, (0.36, 0.12, 0.12), -0.8, False)


def critical_dim(size_m: NDArray, eps_rad: float | NDArray, beta_rad: float | NDArray, *, worst: bool = False,
                 override_m: float | NDArray | None = None) -> float | NDArray:
    """d_c = sqrt(|sin e| l w + |cos e| (|cos b| w h + |sin b| l h)); worst takes min(w h, l h) for the side term (AWR-04 §2.2)."""
    if override_m is not None:
        return override_m
    if isinstance(size_m, dict):
        size_m = [size_m["l"], size_m["w"], size_m["h"]]
    s = np.asarray(size_m, float)
    ln, w, h = s[..., 0], s[..., 1], s[..., 2]
    e = np.asarray(eps_rad, float)
    b = np.asarray(beta_rad, float)
    side = np.minimum(w * h, ln * h) if worst else np.abs(np.cos(b)) * w * h + np.abs(np.sin(b)) * ln * h
    out = np.sqrt(np.abs(np.sin(e)) * ln * w + np.abs(np.cos(e)) * side)
    return float(out) if np.ndim(out) == 0 else out


def keepout_volumes(t: TargetConfig, state: TargetState1, mode: str, margin_m: float, *, conservative: bool = True,
                    moving_as_circle: bool = True) -> list[Volume]:
    """Enabled configured volumes enlarged by margin_m (geometric). Stub: physical mode uses the configured radius too (M18 adds
    min(radius_max, r_eff,cons)). Moving targets turn sectors into cylinders."""
    del conservative
    out = []
    x, y, z = state.pos
    for i, b in enumerate(t.sensitivity):
        if not b.get("enabled", True) or b["kind"] not in ("visual", "acoustic"):
            continue
        shape = b["shape"]
        if shape == "sector" and moving_as_circle and state.moving:
            shape = "cylinder"
        r = float(b["radius_max_m"]) + margin_m
        zmin, zmax = z + float(b.get("z_min_m", 0.0)) - margin_m, z + float(b.get("z_max_m", 300.0)) + margin_m
        out.append(Volume(shape, (x, y, z), r, zmin if shape not in ("sphere", "hemisphere") else z, zmax,
                          state.yaw_rad + math.radians(b.get("azimuth_offset_deg", 0.0)), math.radians(b.get("half_angle_deg", 180.0)), i, b["kind"]))
    return out


def inside(v: Volume, p: NDArray) -> bool:
    d = np.asarray(p, float) - np.asarray(v.center)
    rh, r3 = math.hypot(d[0], d[1]), float(np.linalg.norm(d))
    if v.shape == "sphere":
        return r3 <= v.r_m
    if v.shape == "hemisphere":
        return r3 <= v.r_m and d[2] >= 0
    if not (rh <= v.r_m and v.z_min_m <= p[2] <= v.z_max_m):
        return False
    if v.shape == "cylinder":
        return True
    a = (math.atan2(d[1], d[0]) - v.yaw_rad + math.pi) % (2 * math.pi) - math.pi
    return abs(a) <= v.half_angle_rad


def predict_entry(p: NDArray, v: NDArray, vols: list[Volume], horizon_s: float = 3.0) -> float | None:
    """Seconds until the straight path p + v t enters any volume within the horizon (0.1 s steps), None otherwise."""
    p, v = np.asarray(p, float), np.asarray(v, float)
    for k in range(int(horizon_s * 10) + 1):
        q = p + v * (k * 0.1)
        if any(inside(vol, q) for vol in vols):
            return k * 0.1
    return None


def view_of(cfgs: list[TargetConfig], states: list[TargetState1], rev: int = 0) -> TargetView:
    n = len(cfgs)
    return TargetView(n, np.array([s.target_no for s in states], np.uint16),
                      np.array([enums.TargetClass[c.cls.upper()] for c in cfgs], np.uint8),
                      np.array([s.pos for s in states], float).reshape(n, 3), np.array([s.yaw_rad for s in states], float),
                      np.array([s.vel for s in states], float).reshape(n, 3),
                      np.array([[c.size_m["l"], c.size_m["w"], c.size_m["h"]] for c in cfgs], float).reshape(n, 3),
                      np.array([c.occlusion for c in cfgs], float), tuple(cfgs), rev)


# ---------------------------------------------------------------- deterministic fake
def _unit(seed: int, *keys: int) -> float:
    """Counter-based uniform in [0, 1) (stand-in for cbrng stream 17 targets_motion)."""
    h = seed & 0xFFFFFFFF
    for k in keys:
        h = (h ^ (k & 0xFFFFFFFF)) * 0x9E3779B1 & 0xFFFFFFFF
        h ^= h >> 15
    return (h * 0x85EBCA6B & 0xFFFFFFFF) / 2**32


class FakeTargets:
    """Targets placed by class with contract-shaped outputs. step(t_sim_ns) is a pure function of time (replayable)."""

    def __init__(self, items: list[dict], seed: int = 7, ground_z: float = 0.0):
        if len(items) > LIMIT:
            raise ValueError("at most 30 targets (505)")
        self.seed = seed
        self.cfgs: list[TargetConfig] = []
        counters: dict[str, int] = {}
        for d in items:
            counters[d["class"]] = counters.get(d["class"], 0) + 1
            tid = d.get("id") or f"{d['class']}-{counters[d['class']]:02d}"
            pos = list(d["pos"]) + [ground_z] * (3 - len(d["pos"]))
            self.cfgs.append(TargetConfig.from_dict({**d, "pos": [pos[0], pos[1], pos[2] if pos[2] is not None else ground_z]}, tid))
        self.block = new_block()
        self.n = len(self.cfgs)
        self.cfg_rev = [1] * self.n
        self.t_sim_ns = 0
        b = self.block
        for i, c in enumerate(self.cfgs):
            b["active"][i], b["target_no"][i] = 1, i
            b["tgt_cls"][i] = enums.TargetClass[c.cls.upper()]
            b["tgt_size"][i] = (c.size_m["l"], c.size_m["w"], c.size_m["h"])
            b["tgt_enabled"][i] = 1 if c.enabled else 0
            b["tgt_crit"][i] = 1 if c.criterion_mode == "physical" else 0
            b["tgt_alert"][i] = c.alertness
            b["tgt_n_vol"][i] = sum(1 for v in c.sensitivity if v.get("enabled", True))
            b["tgt_keepout_r"][i] = max([float(v["radius_max_m"]) for v in c.sensitivity if v.get("enabled", True)] or [0.0])
        self.step(0)

    def step(self, t_sim_ns: int) -> None:
        self.t_sim_ns = t_sim_ns
        t = t_sim_ns * 1e-9
        b = self.block
        for i, c in enumerate(self.cfgs):
            x0, y0, z0 = c.pos
            m = c.motion
            mode = m.get("mode", "static")
            if mode == "random_walk" and c.enabled:
                # smooth closed loop (radius 15-40 m) with a slow per-target phase: bounded, replayable, continuous
                v = float(m.get("speed_mps", 1.0))
                rad = 15.0 + 25.0 * _unit(self.seed, i, 1)
                w = v / rad
                ph = 2 * math.pi * _unit(self.seed, i, 2) + w * t
                b["tgt_pos"][i] = (x0 + rad * math.cos(ph) - rad * math.cos(ph - w * t), y0 + rad * math.sin(ph) - rad * math.sin(ph - w * t), z0)
                b["tgt_vel"][i] = (-v * math.sin(ph), v * math.cos(ph), 0.0)
                b["tgt_yaw"][i] = (ph + math.pi / 2 + math.pi) % (2 * math.pi) - math.pi
                b["tgt_speed"][i] = v
                b["tgt_moving"][i] = 1
            else:
                b["tgt_pos"][i] = (x0, y0, z0 if c.cls != "uav" else z0 + float(c.raw.get("alt_agl_m", 50.0)))
                b["tgt_vel"][i] = (0.0, 0.0, 0.0)
                b["tgt_yaw"][i] = float(c.raw.get("yaw_rad", 0.0))
                b["tgt_speed"][i] = 0.0
                b["tgt_moving"][i] = 0
            s = c.sound
            if s:
                ph = (t - s.get("phase_s", 0.0)) % s["period_s"]
                b["tgt_sound_phase"][i] = ph
                b["tgt_sound_on"][i] = 1 if ph < s["duty"] * s["period_s"] else 0
            else:
                b["tgt_sound_on"][i] = 0

    def states(self) -> list[TargetState1]:
        b = self.block
        return [TargetState1(c.id, i, tuple(map(float, b["tgt_pos"][i])), float(b["tgt_yaw"][i]), tuple(map(float, b["tgt_vel"][i])),
                             float(b["tgt_speed"][i]), bool(b["tgt_moving"][i]), enums.Awareness(int(b["tgt_aware"][i])).name,
                             float(b["tgt_alert"][i])) for i, c in enumerate(self.cfgs)]

    def view(self) -> TargetView:
        return view_of(self.cfgs, self.states(), sum(self.cfg_rev))

    def lite_rows(self) -> np.ndarray:
        """awr.TargetLite32.v1 rows in target_no order (what M08 tap_targets publishes on state/sim-core/targets)."""
        b, n = self.block, self.n
        a = np.zeros(n, layouts.TARGET_LITE32)
        B = layouts.TARGET_LITE32_BITS
        for i in range(n):
            a["agent_no"][i] = b["target_no"][i]
            a["class"][i] = b["tgt_cls"][i]
            a["state"][i] = layouts.pack_bits(B["state"], awareness=int(b["tgt_aware"][i]), criterion=int(b["tgt_crit"][i]),
                                              moving=int(b["tgt_moving"][i]), occupied=int(b["tgt_occupied"][i]),
                                              sound_on=int(b["tgt_sound_on"][i]), enabled=int(b["tgt_enabled"][i]))
            a["pos"][i] = b["tgt_pos"][i]
            a["heading_rad"][i] = b["tgt_yaw"][i]
            a["speed_cms"][i] = round(min(float(b["tgt_speed"][i]) * 100.0, 32767.0))
            a["best"][i] = layouts.pack_bits(B["best"], level=int(b["tgt_best_level"][i]), captured=int(b["tgt_captured"][i]),
                                             relay=int(b["tgt_relay"][i]), sensor=int(b["tgt_best_sensor"][i]))
            a["alertness_u8"][i] = round(float(b["tgt_alert"][i]) * 255)
            a["discovered_n"][i] = b["tgt_disc_n"][i]
            a["n_vol"][i] = b["tgt_n_vol"][i]
            a["flags"][i] = layouts.pack_bits(B["flags"], hidden=int(b["tgt_hidden"][i]), discovered_recent=int(b["tgt_disc_recent"][i]),
                                              cfg_rev=self.cfg_rev[i] % 64)
            a["keepout_r_m"][i] = b["tgt_keepout_r"][i]
        return a

    def bus_rows(self, roster_version: int = 1) -> dict:
        """state/sim-core/targets payload (msgpack object, M08 §15.3.4)."""
        return {"v": 1, "t_sim_ns": self.t_sim_ns, "roster_version": roster_version, "rows": self.lite_rows().tobytes()}

    def roster_entries(self) -> list[dict]:
        return [{"agent_no": i, "id": c.id, "kind": "target", "model": c.cls, "label": c.raw.get("label") or f"{CLASS_DEFAULTS['label_zh'][c.cls]}-{i + 1:02d}"}
                for i, c in enumerate(self.cfgs)]

    def detail(self, i: int, models: tuple[str, ...] = ("p600_mid360",), with_cfg: bool = False) -> dict:
        """awr.target.detail.v1 for target i."""
        c, b = self.cfgs[i], self.block
        vols = []
        for k, v in enumerate(c.sensitivity):
            vols.append({"idx": k, "kind": v["kind"], "shape": v["shape"], "radius_cfg_m": float(v["radius_max_m"]),
                         "radius_eff_m": {m: float(v["radius_max_m"]) for m in models}, "z_min_m": float(v.get("z_min_m", 0.0)),
                         "z_max_m": float(v.get("z_max_m", 300.0)), "half_angle_deg": float(v.get("half_angle_deg", 180.0)),
                         "azimuth_offset_deg": float(v.get("azimuth_offset_deg", 0.0)), "enabled": bool(v.get("enabled", True)), "occupied_by": []})
        d = {"id": c.id, "label": c.raw.get("label") or c.id, "class": c.cls, "t_sim_ns": self.t_sim_ns,
             "awareness": enums.Awareness(int(b["tgt_aware"][i])).name, "alertness": round(float(b["tgt_alert"][i]), 4),
             "criterion_mode": c.criterion_mode, "level_req": c.level_req, "discovered_count": int(b["tgt_disc_n"][i]), "episode": int(b["tgt_episode"][i]),
             "last_discovered": None, "dwell": None, "behavior": None, "sensitivity": vols, "inside_n": 0,
             "keepout": {"radius_m": float(b["tgt_keepout_r"][i]), "margin_m": 20.06, "full_circle": bool(b["tgt_moving"][i])},
             "sound": ({"on": bool(b["tgt_sound_on"][i]), "phase_s": round(float(b["tgt_sound_phase"][i]), 3), "lw_dba_now": float(c.sound["lw_dba"])}
                       if c.sound else None),
             "motion": {"mode": c.motion.get("mode", "static"), "speed_mps": round(float(b["tgt_speed"][i]), 3), "vz_mps": 0.0, "path_idx": None},
             "capture": {"best_level": ("none", "D", "R", "I")[int(b["tgt_best_level"][i])], "captured": bool(b["tgt_captured"][i]),
                         "sensor": None, "uav": None, "n_eff": None, "n_req": None, "since_t_sim_ns": None},
             "cfg_rev": self.cfg_rev[i]}
        if with_cfg:
            d["cfg"] = c.to_dict()
        return d

    def set_perception(self, i: int, level: int, captured: bool, sensor: int, relay: bool = False) -> None:
        """M19 and M20 columns (one writer each in the real block)."""
        b = self.block
        b["tgt_best_level"][i], b["tgt_captured"][i], b["tgt_best_sensor"][i], b["tgt_relay"][i] = level, int(captured), sensor, int(relay)

    def set_awareness(self, i: int, awareness: str, discovered: bool = False) -> None:
        b = self.block
        b["tgt_aware"][i] = enums.Awareness[awareness]
        b["tgt_disc_recent"][i] = 1 if awareness in ("REACTING", "ALERTED", "CALMING") else 0
        if discovered:
            b["tgt_disc_n"][i] = min(int(b["tgt_disc_n"][i]) + 1, 65535)
            b["tgt_episode"][i] += 1


TEMPLATE_T5 = [{"class": "person", "pos": [150.0, -40.0, 0.0]}, {"class": "dog", "pos": [-80.0, 120.0, 0.0]}]
