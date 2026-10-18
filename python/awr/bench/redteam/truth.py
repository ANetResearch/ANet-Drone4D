"""红队的真值侧（M26 §8.2、§8.3；D3-AC-16）：运行期间由 `ProbedEnv` 逐 tick 记下本机真值，运行后由 `TruthSet` 汇总全部
保密对象，供结构化检索（R1）与高熵字节检索（R2）按时间窗取样比对。真值只在红队宿主内存与运行真值层中，不进入报告正文。

保密对象（§8.2 不属于泄漏的信息之外的全部）：

| 类 | 内容 | 来源 |
|---|---|---|
| 本机真值位姿 | 位置、姿态（四元数、横滚俯仰航向，弧度与度） | 物理档样本（tick 末与 250 Hz 历史） |
| 真值外参 | 各传感器真值原点（`pos + R_wb · t_bs`，相机另取航向增稳挂载）、相机真值旋转（矩阵元素与四元数） | 同上 + 标称挂载 + 云台真值 |
| 真值云台角 | 俯仰、偏航（度与弧度） | Env 云台状态（每 tick） |
| 演员 | 全部演员（含背景）的位置；演员 id、磁带演员序号 | 磁带帧（10 Hz 网格，毫米整数）与 `actor_states` 插值 |
| 标识 | `run_seed`、SplitMix64 第一级与流级派生键、`run_id`、事件 id、划分名、磁带与事件哈希 | 运行规格、磁带、事件目录 |

`ProbedEnv` 只覆写 `_log_fleet`（reset 与每个 `_advance` 末调用）追加记录，不改变观测、动作与随机数，运行结果与 `BenchEnv`
逐字节相同（红队用例校验 obs.sha256 一致）。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from awr.agentsdk.env.core import TICK_NS, BenchEnv
from awr.sim.sensors import cbrng

__all__ = ["SPLIT_LABELS", "STREAMS_DERIVED", "ProbedEnv", "Secrets", "TruthSet", "derived_keys"]

SPLIT_LABELS = ("dev", "test")                # M27 划分名（runs.split）
STREAMS_DERIVED = range(64)                   # 流级派生键 sm(sm(seed) ^ stream)：覆盖 rt/rng_streams.json 已登记与预留的流号
NS = 1_000_000_000


def derived_keys(seed: int) -> dict[str, int]:
    """SplitMix64 第一级派生键 `k1 = sm(seed)` 与流级键 `sm(k1 ^ stream)`（cbrng.key 的前两级）。"""
    k1 = cbrng.sm(cbrng.u64(seed))
    out = {"k1": int(k1)}
    for s in STREAMS_DERIVED:
        out[f"stream{s}"] = int(cbrng.sm(k1 ^ cbrng.u64(s)))
    return out


def _q_of_R(R: np.ndarray) -> np.ndarray:
    """旋转矩阵 (n, 3, 3) → 四元数 xyzw (n, 4)（w ≥ 0）。"""
    R = np.asarray(R, np.float64).reshape(-1, 3, 3)
    out = np.empty((len(R), 4))
    for i, m in enumerate(R):
        tr = m[0, 0] + m[1, 1] + m[2, 2]
        if tr > 0:
            s = math.sqrt(tr + 1.0) * 2
            q = ((m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s, 0.25 * s)
        elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
            s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
            q = (0.25 * s, (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s, (m[2, 1] - m[1, 2]) / s)
        elif m[1, 1] > m[2, 2]:
            s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
            q = ((m[0, 1] + m[1, 0]) / s, 0.25 * s, (m[1, 2] + m[2, 1]) / s, (m[0, 2] - m[2, 0]) / s)
        else:
            s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
            q = ((m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s, 0.25 * s, (m[1, 0] - m[0, 1]) / s)
        out[i] = q if q[3] >= 0 else tuple(-x for x in q)
    return out


def _rpy(q: np.ndarray) -> np.ndarray:
    x, y, z, w = (q[:, i] for i in range(4))
    roll = np.arctan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    pitch = np.arcsin(np.clip(2 * (w * y - z * x), -1.0, 1.0))
    yaw = np.arctan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
    return np.stack([roll, pitch, yaw], 1)


class ProbedEnv(BenchEnv):
    """`BenchEnv` + 真值探针：每 tick 末记 tick 时刻、真值行（16 列）、云台真值角，以及 (t_{k−1}, t_k] 的物理档样本。"""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.probe_t: list[int] = []
        self.probe_rows: dict[str, list[np.ndarray]] = {}
        self.probe_gim: dict[str, list[tuple[float, float]]] = {}
        self.probe_hist: dict[str, dict[int, np.ndarray]] = {}

    def _log_fleet(self) -> None:
        super()._log_fleet()
        t = self.k * TICK_NS
        self.probe_t.append(t)
        for u in self.uavs:
            self.probe_rows.setdefault(u, []).append(self._row(u))
            g = self.gim[u]
            self.probe_gim.setdefault(u, []).append((float(g.pitch_deg), float(g.yaw_deg)))
            h = self.probe_hist.setdefault(u, {})
            try:
                sl = self.tier.history.slice(u, max(t - TICK_NS, 0), t)
                for ti, st in zip(sl.t_ns.tolist(), sl.state, strict=True):
                    h.setdefault(int(ti), np.asarray(st[0:7], np.float64).copy())
            except LookupError:                               # 首个 tick 前无历史：只用 tick 末样本
                h.setdefault(t, self._row(u)[0:7].copy())


@dataclass
class Secrets:
    """标识类保密对象（R1 字符串与整数、R2 令牌）。"""

    run_seed: int
    run_id: str
    event_id: str
    actor_ids: list[str]
    hashes: dict[str, str]                                    # 名称 → 64 位十六进制 sha256
    derived: dict[str, int] = field(default_factory=dict)
    split_labels: tuple[str, ...] = SPLIT_LABELS

    def strings(self) -> dict[str, str]:
        """R1 精确比对的字符串 → 类别。"""
        out: dict[str, str] = {}
        for a in self.actor_ids:
            out[a] = "actor_id"
        out[self.event_id] = "event_id"
        for s in self.split_labels:
            out[s] = "split"
        out[self.run_id] = "run_id"
        for h in self.hashes.values():
            out[h] = out[h.upper()] = "hash"
        for v in (self.run_seed, *self.derived.values()):
            out[str(v)] = out[f"{v:x}"] = out[f"{v:016x}"] = out[f"{v:X}"] = out[f"0x{v:x}"] = "seed"
        return out

    def integers(self) -> dict[int, str]:
        """R1 整数字段精确比对（u64 与 i64 两种解释）。"""
        out: dict[int, str] = {}
        for v in (self.run_seed, *self.derived.values()):
            u = int(v) & ((1 << 64) - 1)
            out[u] = "seed"
            out[u - (1 << 64) if u >= 1 << 63 else u] = "seed"
        return out

    def tokens(self, min_len: int = 6) -> tuple[dict[bytes, str], dict[str, int]]:
        """R2 字节令牌 → 类别；短于 `min_len` 的令牌不做全字节检索（只在 R1 字符串中精确比对），第二个返回值为其按类别的个数
        （不含取值，可写入报告）。"""
        tok: dict[bytes, str] = {}
        short: dict[str, int] = {}

        def add(b: bytes, cls: str, name: str) -> None:
            if len(b) >= min_len:
                tok[b] = cls
            elif b not in tok:
                short[cls] = short.get(cls, 0) + 1

        for name, v in (("run_seed", self.run_seed), *self.derived.items()):
            u = int(v) & ((1 << 64) - 1)
            cls = "seed" if name == "run_seed" else "derived_key"
            add(u.to_bytes(8, "little"), cls, f"{name}/u64le")
            add(u.to_bytes(8, "big"), cls, f"{name}/u64be")
            for txt in (str(int(v)), f"{u:x}", f"{u:X}", f"{u:016x}"):
                add(txt.encode(), cls, f"{name}/text")
        for name, h in self.hashes.items():
            add(bytes.fromhex(h), "hash", f"{name}/raw")
            add(h.encode(), "hash", f"{name}/hex")
            add(h.upper().encode(), "hash", f"{name}/HEX")
        add(self.run_id.encode(), "run_id", "run_id")
        add(self.event_id.encode(), "event_id", "event_id")
        for s in self.split_labels:
            add(s.encode(), "split", s)
        for a in self.actor_ids:
            add(a.encode(), "actor_id", a)
        return tok, dict(sorted(short.items()))


@dataclass
class _Own:
    tick_t: np.ndarray                                        # (K,) i64
    rows: np.ndarray                                          # (K, 16)
    gim_deg: np.ndarray                                       # (K, 2) pitch、yaw
    hist_t: np.ndarray                                        # (H,) i64
    hist: np.ndarray                                          # (H, 7) pos、q
    mounts: dict[str, np.ndarray]                             # sensor_id → t_bs (3,)
    camera: str | None


class TruthSet:
    """一个红队运行的全部保密对象与按时刻取样的真值量。"""

    def __init__(self, *, secrets: Secrets, own: dict[str, _Own], tape, actor_mm: np.ndarray, actor_state: np.ndarray,
                 det_actor: dict[str, int], gimbal_sigma_deg: float | None, duration_s: float, obs_level: str, noise: str) -> None:
        self.secrets, self.own, self.tape = secrets, own, tape
        self.actor_mm, self.actor_state = actor_mm, actor_state     # (T, n, 3) i64 毫米、(T, n) u8
        self.actor_m = actor_mm.astype(np.float64) / 1000.0
        self.det_actor = det_actor                                    # det_id → 磁带演员序号
        self.gimbal_sigma_deg = gimbal_sigma_deg
        self.duration_s, self.obs_level, self.noise = duration_s, obs_level, noise
        self.n_actors = actor_mm.shape[1]
        self.tape_t_max_ns = (actor_mm.shape[0] - 1) * TICK_NS

    # ------------------------------------------------------------------ 构造
    @classmethod
    def from_env(cls, env: ProbedEnv) -> TruthSet:
        spec, tape = env.spec, env.tape
        ev_dir = Path(spec.event_dir)
        hashes = {f"tape.{k}": v for k, v in tape.hashes().items()}
        hashes["tape.scripted_sha256"] = tape.scripted_sha256()
        for p in sorted(ev_dir.iterdir()):
            if p.is_file():
                hashes[f"file.{p.name}"] = hashlib.sha256(p.read_bytes()).hexdigest()
        sec = Secrets(run_seed=int(spec.seed), run_id=str(spec.run_id), event_id=str(tape.header["event_id"]),
                      actor_ids=[str(a["id"]) for a in tape.actors], hashes=hashes, derived=derived_keys(int(spec.seed)))
        frames = np.concatenate([tape.block_frames(bi) for bi in range(len(tape.blocks))])     # (T, n) 记录
        actor_mm = np.stack([frames["x_mm"], frames["y_mm"], frames["z_mm"]], -1).astype(np.int64)
        own = {}
        for u in env.uavs:
            h = env.probe_hist.get(u, {})
            ht = np.array(sorted(h), np.int64)
            sensors = env.configs[u]["sensors"]
            mounts = {s["sensor_id"]: np.asarray(s["mount_nominal"]["t_bs_m"], np.float64) for s in sensors if "mount_nominal" in s}
            cam = next((s["sensor_id"] for s in sensors if s.get("kind") == "camera"), None)
            own[u] = _Own(np.asarray(env.probe_t, np.int64), np.asarray(env.probe_rows[u]), np.asarray(env.probe_gim[u], np.float64),
                          ht, np.asarray([h[t] for t in ht.tolist()]).reshape(-1, 7), mounts, cam)
        det_actor = {}
        idx = {str(a["id"]): int(a["idx"]) for a in tape.actors}
        p = Path(spec.out_dir) / "truth" / "l1.jsonl" if spec.out_dir else None
        if p is not None and p.is_file():
            for line in p.read_text(encoding="utf-8").splitlines():
                r = json.loads(line)
                det_actor[r["det_id"]] = idx[r["actor"]]
        prof = getattr(getattr(env, "l2", None), "prof", None)
        sig = getattr(prof, "gimbal_sigma_deg", None)
        return cls(secrets=sec, own=own, tape=tape, actor_mm=actor_mm, actor_state=np.asarray(frames["state"]), det_actor=det_actor,
                   gimbal_sigma_deg=float(sig) if sig is not None else None, duration_s=float(env.duration_s), obs_level=spec.obs_level,
                   noise=spec.noise)

    # ------------------------------------------------------------------ 取样
    def own_range_ns(self, uav: str) -> tuple[int, int]:
        o = self.own[uav]
        return int(o.tick_t[0]), int(o.tick_t[-1])

    def own_samples(self, uav: str, times_ns: np.ndarray) -> dict[str, np.ndarray]:
        """→ 真值量名 → (S, d)。位置与姿态按 250 Hz 历史插值（姿态归一化线性插值），云台角在 tick 之间线性插值（同 L2 `gimbal_at`）。"""
        o = self.own[uav]
        t = np.asarray(times_ns, np.int64)
        if not len(t):
            return {}
        pos = np.stack([np.interp(t, o.hist_t, o.hist[:, j]) for j in range(3)], 1)
        i1 = np.clip(np.searchsorted(o.hist_t, t), 1, max(len(o.hist_t) - 1, 1))
        i0 = i1 - 1
        if len(o.hist_t) > 1:
            span = (o.hist_t[i1] - o.hist_t[i0]).astype(np.float64)
            a = np.clip((t - o.hist_t[i0]) / np.where(span > 0, span, 1.0), 0.0, 1.0)[:, None]
            q0, q1 = o.hist[i0, 3:7], o.hist[i1, 3:7]
            q1 = np.where((np.sum(q0 * q1, 1) < 0)[:, None], -q1, q1)
            q = q0 * (1 - a) + q1 * a
        else:
            q = np.repeat(o.hist[:1, 3:7], len(t), 0)
        q = q / np.linalg.norm(q, axis=1, keepdims=True)
        rpy = _rpy(q)
        g = np.stack([np.interp(t, o.tick_t, o.gim_deg[:, j]) for j in range(2)], 1)     # (S, 2) pitch、yaw（度），tick 间线性
        from awr.sim.sensors.frames import gimbal_R, quat_to_R

        Rwb = quat_to_R(q)
        yaw = rpy[:, 2]
        cz, sz = np.cos(yaw), np.sin(yaw)
        Rz = np.zeros((len(t), 3, 3))
        Rz[:, 0, 0], Rz[:, 0, 1], Rz[:, 1, 0], Rz[:, 1, 1], Rz[:, 2, 2] = cz, -sz, sz, cz, 1.0
        out = {"own_pose/position": pos, "own_pose/attitude_q": q, "own_pose/rpy_rad": rpy, "own_pose/rpy_deg": np.degrees(rpy),
               "gimbal/deg": g, "gimbal/rad": np.radians(g)}
        for sid, tb in o.mounts.items():
            out[f"extrinsics/{sid}/origin"] = pos + np.einsum("nij,j->ni", Rwb, tb)
            if sid == o.camera:
                out[f"extrinsics/{sid}/origin_stab"] = pos + np.einsum("nij,j->ni", Rz, tb)
        if o.camera is not None:
            Rc = np.einsum("nij,njk->nik", Rz, gimbal_R(np.radians(g[:, 1]), np.radians(g[:, 0])).reshape(-1, 3, 3))
            out[f"extrinsics/{o.camera}/R"] = Rc.reshape(-1, 9)
            out[f"extrinsics/{o.camera}/q"] = _q_of_R(Rc)
        return out

    def actor_samples(self, times_ns: np.ndarray) -> np.ndarray:
        """→ (S, n, 3) 全部演员位置（m）；网格时刻取磁带毫米值，其余按 `actor_states` 插值；超出磁带范围的时刻为 NaN。"""
        t = np.asarray(times_ns, np.int64)
        out = np.full((len(t), self.n_actors, 3), np.nan)
        for i, ti in enumerate(t.tolist()):
            if ti < 0 or ti > self.tape_t_max_ns:
                continue
            k, rem = divmod(ti, TICK_NS)
            out[i] = self.actor_m[k] if rem == 0 else self.tape.actor_states(ti).pos_enu_m
        return out
