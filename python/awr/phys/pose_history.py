"""PoseHistory 真值通道（M24 §6.3.1、§6.3.2；AWR-05 §8.1；ADR-160；契约 sensor/v2/pose_history.v1）。

每个物理步末一行无损状态（16 个 f64：`px py pz qx qy qz qw vx vy vz wx wy wz fx fy fz`，ENU / WORLD←FLU / FLU；与 gz 控制协议
`StepDone.samples` 同布局）。`PoseHistoryBuffer` 每机一个环形 SoA，容量 ⌈capacity_s × hz⌉：

- 写入：`append_block(t_ns (n,), state (n, n_uav, 16))`（Realistic，每 tick 25 步）或 `append(uav, t_ns, row)`（Fast，逐步）；
  时刻须严格递增且落在网格 `t_0 + k·dt_ns` 上（635）；写入位置等于未释放的最早样本时抛 634，不覆盖。
- 释放：`release_before(t_ns)` 把水位推进到 t 之前（保留 t 之前最近一个样本，供跨水位的插值）。
- 查询：`slice(uav, t_from, t_to)` 返回含两端外侧相邻样本的拷贝；`t_to` 晚于最新样本时抛 `PoseHistoryRangeError`。
- 云台：`set_gimbal(uav, sensor_no, t_ns, az, el, zoom)` 只在状态变化时追加（契约 `gimbal` 块，按时刻升序）。
- 存档：`drain_message(uav)` 把上次存档后的样本编码为 `drone4d.pose_history.v1` 消息（"D3PH" 容器：与 camera_gt 同一
  命名块布局，块头 JSON 的 `ref` 指向同一消息内的块）。vel、omega、f_b 按契约存 f32，t、pos、q 存 i64 / f64。
"""

from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass

import numpy as np

from awr.phys import PhysError

__all__ = [
    "COLS",
    "G_W",
    "STATE_N",
    "GimbalSample",
    "PoseHistoryBuffer",
    "PoseHistoryRangeError",
    "PoseSample",
    "PoseSlice",
    "pack_pose_history",
    "unpack_pose_history",
]

G_W = np.array([0.0, 0.0, -9.80665])
STATE_N = 16
COLS = {"pos": slice(0, 3), "q": slice(3, 7), "vel": slice(7, 10), "omega": slice(10, 13), "f_b": slice(13, 16)}
MAGIC = b"D3PH"


class PoseHistoryRangeError(LookupError):
    """请求晚于最新样本或早于已释放水位。"""


@dataclass(frozen=True)
class PoseSample:
    t_ns: int
    pos_enu_m: np.ndarray
    q_ws_xyzw: np.ndarray
    vel_enu_mps: np.ndarray
    omega_b_rps: np.ndarray
    f_b_mps2: np.ndarray

    @classmethod
    def from_row(cls, t_ns: int, row: np.ndarray) -> PoseSample:
        r = np.asarray(row, np.float64)
        return cls(int(t_ns), r[0:3].copy(), r[3:7].copy(), r[7:10].copy(), r[10:13].copy(), r[13:16].copy())


@dataclass(frozen=True)
class GimbalSample:
    t_ns: int
    sensor_no: int
    az_rad: float
    el_rad: float
    zoom_f_mm: float = 0.0


@dataclass(frozen=True)
class PoseSlice:
    uav: str
    t_ns: np.ndarray            # (n,) i64
    state: np.ndarray           # (n, 16) f64

    def sample(self, i: int) -> PoseSample:
        return PoseSample.from_row(int(self.t_ns[i]), self.state[i])


class _Ring:
    def __init__(self, cap: int) -> None:
        self.t = np.zeros(cap, np.int64)
        self.s = np.zeros((cap, STATE_N), np.float64)
        self.head = 0           # 最早未释放样本的绝对序号
        self.n = 0              # 已写入样本总数（绝对序号上界）
        self.archived = 0       # 已存档的绝对序号上界
        self.gimbal: list[GimbalSample] = []
        self.g_archived = 0


class PoseHistoryBuffer:
    def __init__(self, uavs: list[str] | tuple[str, ...], hz: int = 250, *, capacity_s: float = 2.0, t0_ns: int = 0) -> None:
        self.uavs = tuple(uavs)
        self.hz = int(hz)
        self.dt_ns = 1_000_000_000 // self.hz
        if self.dt_ns * self.hz != 1_000_000_000:
            raise PhysError(637, f"pose_history_hz {hz} does not divide 1 s")
        self.cap = max(2, math.ceil(capacity_s * self.hz))
        self.t0_ns = int(t0_ns)
        self._r = {u: _Ring(self.cap) for u in self.uavs}

    # ------------------------------------------------------------ 写入
    def _put(self, r: _Ring, uav: str, t: int, row: np.ndarray) -> None:
        if r.n > 0:
            last = int(r.t[(r.n - 1) % self.cap])
            if t != last + self.dt_ns:
                raise PhysError(635, f"{uav}: sample at {t} ns does not follow {last} ns on the {self.dt_ns} ns grid")
        elif (t - self.t0_ns) % self.dt_ns:
            raise PhysError(635, f"{uav}: first sample {t} ns is off the grid")
        if r.n - r.head >= self.cap:
            raise PhysError(634, f"{uav}: pose history full ({self.cap} samples unreleased)")
        i = r.n % self.cap
        r.t[i] = t
        r.s[i] = row
        r.n += 1

    def append(self, uav: str, t_ns: int, row) -> None:
        self._put(self._r[uav], uav, int(t_ns), np.asarray(row, np.float64))

    def append_block(self, t_ns, state, uavs: tuple[str, ...] | None = None) -> None:
        """t_ns (n,)、state (n, n_uav, 16)；uavs 缺省为构造时的顺序（uav_index）。"""
        t = np.asarray(t_ns, np.int64)
        s = np.asarray(state, np.float64)
        order = uavs or self.uavs
        if s.shape != (len(t), len(order), STATE_N):
            raise PhysError(635, f"state block shape {s.shape} != ({len(t)}, {len(order)}, {STATE_N})")
        if len(t) > 1 and np.any(np.diff(t) != self.dt_ns):
            raise PhysError(635, "block times are not on the step grid")
        for u in order:                                       # 整块写入或不写：先检查容量与衔接
            r = self._r[u]
            if r.n - r.head + len(t) > self.cap:
                raise PhysError(634, f"{u}: pose history full ({self.cap} samples unreleased)")
            if r.n and len(t) and int(t[0]) != int(r.t[(r.n - 1) % self.cap]) + self.dt_ns:
                raise PhysError(635, f"{u}: block at {int(t[0])} ns does not follow the last sample")
        for j, u in enumerate(order):
            r = self._r[u]
            for k in range(len(t)):
                self._put(r, u, int(t[k]), s[k, j])

    def set_gimbal(self, uav: str, sensor_no: int, t_ns: int, az_rad: float, el_rad: float, zoom_f_mm: float = 0.0) -> None:
        g = self._r[uav].gimbal
        last = next((x for x in reversed(g) if x.sensor_no == sensor_no), None)
        if last is not None and (last.az_rad, last.el_rad, last.zoom_f_mm) == (az_rad, el_rad, zoom_f_mm):
            return
        if g and int(t_ns) < g[-1].t_ns:
            raise PhysError(635, f"{uav}: gimbal sample goes back in time")
        g.append(GimbalSample(int(t_ns), int(sensor_no), float(az_rad), float(el_rad), float(zoom_f_mm)))

    # ------------------------------------------------------------ 释放与查询
    def latest_t(self, uav: str) -> int | None:
        r = self._r[uav]
        return int(r.t[(r.n - 1) % self.cap]) if r.n else None

    def release_before(self, t_ns: int) -> None:
        """释放 t 之前的样本，但保留 t 之前最近一个（跨水位插值用）。存档（drain_message）须先于释放，由调用方保证。"""
        for r in self._r.values():
            while r.n - r.head > 1 and int(r.t[(r.head + 1) % self.cap]) <= t_ns:
                r.head += 1

    def slice(self, uav: str, t_from: int, t_to: int) -> PoseSlice:
        r = self._r[uav]
        if r.n == 0:
            raise PoseHistoryRangeError(f"{uav}: no samples")
        idx = np.arange(r.head, r.n) % self.cap
        t = r.t[idx]
        if t_to > t[-1]:
            raise PoseHistoryRangeError(f"{uav}: t_to {t_to} after latest sample {int(t[-1])}")
        if t_from < t[0]:
            raise PoseHistoryRangeError(f"{uav}: t_from {t_from} before released watermark {int(t[0])}")
        a = max(int(np.searchsorted(t, t_from, side="right")) - 1, 0)
        b = min(int(np.searchsorted(t, t_to, side="left")) + 1, len(t))
        sel = idx[a:b]
        return PoseSlice(uav, r.t[sel].copy(), r.s[sel].copy())

    def last_sample(self, uav: str) -> PoseSample:
        r = self._r[uav]
        if r.n == 0:
            raise PoseHistoryRangeError(f"{uav}: no samples")
        i = (r.n - 1) % self.cap
        return PoseSample.from_row(int(r.t[i]), r.s[i])

    def gimbal_samples(self, uav: str) -> list[GimbalSample]:
        return list(self._r[uav].gimbal)

    # ------------------------------------------------------------ 存档
    def drain_message(self, uav: str) -> bytes | None:
        """上次存档之后的样本（与云台变化）→ `drone4d.pose_history.v1` 消息；没有新样本时 None。存档前不得释放（634 保护）。"""
        r = self._r[uav]
        a = max(r.archived, r.head)
        if a >= r.n:
            return None
        idx = np.arange(a, r.n) % self.cap
        g = r.gimbal[r.g_archived:]
        r.archived, r.g_archived = r.n, len(r.gimbal)
        return pack_pose_history(uav, self.hz, r.t[idx], r.s[idx], g)


_BLOCKS = (("t_ns", "i64", 1), ("pos_enu_m", "f64", 3), ("q_ws_xyzw", "f64", 4), ("vel_enu_mps", "f32", 3), ("omega_b_rps", "f32", 3),
           ("f_b_mps2", "f32", 3))
_DT = {"i64": "<i8", "f64": "<f8", "f32": "<f4", "u16": "<u2"}


def pack_pose_history(uav: str, hz: int, t_ns: np.ndarray, state: np.ndarray, gimbal: list[GimbalSample] | None = None) -> bytes:
    t = np.asarray(t_ns, np.int64)
    s = np.asarray(state, np.float64)
    cols = {"t_ns": t[:, None], "pos_enu_m": s[:, COLS["pos"]], "q_ws_xyzw": s[:, COLS["q"]], "vel_enu_mps": s[:, COLS["vel"]],
            "omega_b_rps": s[:, COLS["omega"]], "f_b_mps2": s[:, COLS["f_b"]]}
    hdr: dict = {"schema": "drone4d.pose_history.v1", "uav": uav, "hz": int(hz), "frame": "ENU<-FLU", "n": len(t)}
    blocks: dict[str, bytes] = {}
    for name, dt, nc in _BLOCKS:
        hdr[name] = {"dtype": dt, "cols": nc, "ref": name}
        blocks[name] = np.ascontiguousarray(cols[name], _DT[dt]).tobytes()
    g = gimbal or []
    if g:
        gc = {"t_ns": ("i64", [x.t_ns for x in g]), "sensor_no": ("u16", [x.sensor_no for x in g]), "az_rad": ("f32", [x.az_rad for x in g]),
              "el_rad": ("f32", [x.el_rad for x in g]), "zoom_f_mm": ("f32", [x.zoom_f_mm for x in g])}
        hdr["gimbal"] = {"m": len(g)}
        for k, (dt, v) in gc.items():
            hdr["gimbal"][k] = {"dtype": dt, "cols": 1, "ref": f"gimbal.{k}"}
            blocks[f"gimbal.{k}"] = np.asarray(v, _DT[dt]).tobytes()
    js = json.dumps(hdr, separators=(",", ":"), sort_keys=True).encode()
    parts = [MAGIC, struct.pack("<HHI", 1, len(blocks), len(js)), js]
    for name in sorted(blocks):
        nb = name.encode()
        parts += [struct.pack("<H", len(nb)), nb, struct.pack("<I", len(blocks[name])), blocks[name]]
    return b"".join(parts)


def unpack_pose_history(b: bytes) -> tuple[dict, dict[str, np.ndarray]]:
    """→ (块头, {块名: 数组})；数组形状 (n, cols)，云台列为 (m,)。"""
    if b[:4] != MAGIC:
        raise ValueError("not a drone4d.pose_history.v1 message")
    ver, nb, jl = struct.unpack_from("<HHI", b, 4)
    if ver != 1:
        raise ValueError(f"pose_history container version {ver}")
    hdr = json.loads(b[12:12 + jl])
    p = 12 + jl
    raw: dict[str, bytes] = {}
    for _ in range(nb):
        (nl,) = struct.unpack_from("<H", b, p)
        name = b[p + 2:p + 2 + nl].decode()
        p += 2 + nl
        (ln,) = struct.unpack_from("<I", b, p)
        raw[name] = b[p + 4:p + 4 + ln]
        p += 4 + ln
    out: dict[str, np.ndarray] = {}
    for name, dt, nc in _BLOCKS:
        spec = hdr[name]
        out[name] = np.frombuffer(raw[spec["ref"]], _DT[dt]).reshape(-1, nc) if nc > 1 else np.frombuffer(raw[spec["ref"]], _DT[dt])
    for k, spec in (hdr.get("gimbal") or {}).items():
        if k != "m":
            out[f"gimbal.{k}"] = np.frombuffer(raw[spec["ref"]], _DT[spec["dtype"]])
    return hdr, out
