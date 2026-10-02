"""SensorRuntime：sim-core 进程内的 M13 运行期状态（传感器组、状态块视图、各 Bank、慢任务、对外 API）。

- 装配：sensors stage 每次调用先 `sync()`：对活动 slot 比较块字段 `init` 与 `agent_no + 1`，发现新 spawn 或 slot 复用即按
  机型传感器组初始化（`has`、`act`、`det_en`、云台缺省、GNSS warm start、IMU 上电偏置；随机量全部取计数器 RNG，与抽样子集
  无关）；非活动但 `init ≠ 0` 的 slot 清空（remove）。派生的 Python 侧数组（每 slot 传感器组下标）按 `init` 戳重建，
  因此 checkpoint 恢复后自动一致。
- 传感器组来自 `ctx.profiles`（M08 ProfileTable）的 `source` 目录；剧本 `vehicles[].sensors` 与 `caps` 经
  `configure_vehicle()` 给出（缺省为机型全部传感器、能力集由传感器合成，M13-FR-041）。
- sim-core 内部 API（M10、M14 经 M08 estimate）：`set_mode`、`set_gimbal`、`set_default_for`、`apply_item_gimbal`、
  `set_active`、`spawn_target`、`sensor_world_pose`、`state_ext_fields`。M08 在 slot 装配前调用这些 API 时，操作延迟到装配后执行。
"""

from __future__ import annotations

import contextlib
import math
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from . import cbrng
from .block import BLOCK, N_GIMBAL, TARGET_BLOCK
from .detector import Detector
from .enums import GIMBAL_MODE_NAMES, GimbalMode, GnssFix, SensorKind, SensorState
from .gimbal import COL_OF_KIND, KIND_OF_COL, GimbalBank, SensorError
from .gnss import GnssBank, gnss_table
from .imu import ImuBank
from .spec import Rig, SensorSpec, load_rig, rig_for_model

__all__ = ["SensorRuntime"]

_GIMBAL_NAME = {int(m): GIMBAL_MODE_NAMES[m] for m in GimbalMode}  # state_ext 热路径：按整数取云台模式名

ALL_KINDS = tuple(SensorKind)


class SensorRuntime:
    def __init__(self) -> None:
        self.S: Any = None
        self.blk: dict[str, np.ndarray] | None = None
        self.ctx: Any = None
        self.seed = 0
        self.tick = 0
        self.rig_list: list[Rig] = []
        self._rig_by_dir: dict[str, int] = {}
        self._rig_by_pidx: dict[int, int] = {}
        self.slot_rig = np.zeros(0, np.int32)
        self.dstamp = np.zeros(0, np.int32)
        self._parts: dict[tuple[int, int], list[np.ndarray]] = {}
        self._roster_ver = 0
        self._parts_ver = -1
        self.vcfg: dict[str, dict] = {}
        self.gate_caps: dict[int, frozenset[str]] = {}  # slot -> 只在 AGENT 租约下参与检测的能力（R-12）
        self._deferred: list[Callable[[], None]] = []
        self._sync_key: tuple | None = None  # sync() 收敛后的输入字节快照
        self.gimbal = GimbalBank(self)
        self.gnss = GnssBank(self)
        self.imu = ImuBank(self)
        self.detector = Detector(self)
        from .ext_fields import NoisyObs
        from .pose_pack import SensorPosePacker

        self.packer = SensorPosePacker(self)
        self.obs = NoisyObs(self)
        self.ev_seen = 0
        self.stats = {"calls": 0, "spawned": 0, "removed": 0, "bridged_targets": 0}

    # ------------------------------------------------------------ 绑定与同步
    def bind(self, S: Any, ctx: Any = None) -> None:
        if self.S is not S:
            self.S = S
            self.blk = S.blocks[BLOCK]
            tb = S.blocks.get(TARGET_BLOCK)
            if tb is not None:
                self.detector.targets.bind(tb)
            n = S.capacity
            self.slot_rig = np.full(n, -1, np.int32)
            self.dstamp = np.zeros(n, np.int32)
            self._roster_ver += 1
        if ctx is not None:
            self.ctx = ctx
            self.seed = world_seed_of(ctx)
            self.tick = int(getattr(ctx, "tick", self.tick))

    def rng3(self, ctx: Any) -> np.random.Generator:
        r = getattr(ctx, "rng", None) or {}
        g = r.get("sensor_noise") if hasattr(r, "get") else None
        if g is None:
            from awr.contracts.rng_streams import Stream, rng

            if not hasattr(self, "_rng3"):
                self._rng3 = rng(self.seed, int(Stream.SENSOR_NOISE))
            g = self._rng3
        return g

    def sync(self, S: Any, ctx: Any) -> None:
        self.bind(S, ctx)
        b = self.blk
        assert b is not None
        # 活动集、agent_no、装配标记与派生戳与上次同步完成后逐字节相同且无待执行操作时，下面三组集合都为空（同步是幂等的
        # 收敛过程），直接返回（N = 1000、50 Hz，此前约 0.1 ms/次，FX2-R3）
        key = (S.active.tobytes(), S.agent_no.tobytes(), b["init"].tobytes(), self.dstamp.tobytes())
        if key == self._sync_key and not self._deferred:
            return
        act = S.active_idx()
        want = (S.agent_no[act] + 1).astype(np.int32)
        stale = act[b["init"][act] != want]
        gone = np.flatnonzero((b["init"] != 0) & ~S.active)
        for s in gone:
            self._remove(int(s))
        for s in stale:
            self._spawn(int(s), ctx)
        bad = np.flatnonzero(self.dstamp != b["init"])
        for s in bad:
            self._derive(int(s))
        if self._deferred:
            todo, self._deferred = self._deferred, []
            for fn in todo:
                with contextlib.suppress(SensorError):
                    fn()
        self._sync_key = (S.active.tobytes(), S.agent_no.tobytes(), b["init"].tobytes(), self.dstamp.tobytes())

    def _rig_index_for_pidx(self, pidx: int) -> int:
        i = self._rig_by_pidx.get(pidx)
        if i is not None:
            return i
        T = getattr(self.ctx, "profiles", None)
        rig: Rig | None = None
        key = f"pidx:{pidx}"
        try:
            pid = T.ids[pidx]
            p = T.get(pid)
            if getattr(p, "source", ""):
                d = Path(p.source).parent
                key = str(d)
                if key in self._rig_by_dir:
                    self._rig_by_pidx[pidx] = self._rig_by_dir[key]
                    return self._rig_by_dir[key]
                rig = load_rig(d)
            else:
                key = f"model:{p.model}"
                rig = rig_for_model(p.model)
        except Exception:
            rig = Rig("unknown", ())
        i = len(self.rig_list)
        self.rig_list.append(rig)
        self._rig_by_dir[key] = i
        self._rig_by_pidx[pidx] = i
        return i

    def _derive(self, s: int) -> None:
        b = self.blk
        if b["init"][s] == 0:
            self.slot_rig[s] = -1
        else:
            self.slot_rig[s] = self._rig_index_for_pidx(int(self.S.profile_id[s]))
        self.dstamp[s] = b["init"][s]
        self._roster_ver += 1

    def rig_of(self, slot: int) -> Rig | None:
        if self.blk is None or slot < 0 or slot >= self.slot_rig.size:
            return None
        i = int(self.slot_rig[slot])
        return self.rig_list[i] if i >= 0 else None

    def uav_id(self, S: Any, slot: int) -> str:
        ids = getattr(S, "ids", None)
        v = ids[slot] if ids is not None and slot < len(ids) else None
        return str(v) if v is not None else f"agent-{int(S.agent_no[slot])}"

    def slot_of(self, vehicle_id: str) -> int:
        S = self.S
        if S is None:
            return -1
        for s in S.active_idx():
            if S.ids[int(s)] == vehicle_id:
                return int(s)
        return -1

    # ------------------------------------------------------------ spawn / remove（M13 §6.7.1）
    def configure_vehicle(self, vehicle_id: str, *, sensors: list[str] | None = None, caps: list[str] | None = None,
                          delegated: list[str] | None = None) -> None:
        """剧本 `vehicles[].sensors`（子集）与 `caps`（有效能力集）；在 fleet/add 之前或之后调用均可（之后调用时重算 has、det_en）。

        `delegated`：只在委派执行期间参与检测的能力（R-12，M14-to-M13 第 2 条）：机体持有 AGENT 租约时该能力的检测器
        视为 ACTIVE，其余时间（例如 MISSION 待命航线上的复核候选）视为 STANDBY；由确定性租约状态派生，不写输入日志。"""
        self.vcfg[str(vehicle_id)] = {"sensors": None if sensors is None else list(sensors),
                                      "caps": None if caps is None else list(caps),
                                      "delegated": None if delegated is None else list(delegated)}
        s = self.slot_of(str(vehicle_id))
        if s >= 0 and self.blk is not None and self.blk["init"][s] != 0:
            self._apply_cfg(s)

    def _selected(self, rig: Rig, cfg: dict | None) -> list[SensorSpec]:
        names = None if cfg is None else cfg.get("sensors")
        return [sp for sp in rig.specs if names is None or sp.name in names]

    def _apply_cfg(self, s: int) -> None:
        b = self.blk
        rig = self.rig_of(s)
        if rig is None:
            return
        cfg = self.vcfg.get(self.uav_id(self.S, s))
        sel = self._selected(rig, cfg)
        dg = None if cfg is None else cfg.get("delegated")
        if dg:
            self.gate_caps[s] = frozenset(dg)
        else:
            self.gate_caps.pop(s, None)
        has = 0
        for sp in sel:
            has |= sp.bit
            if sp.kind == SensorKind.IMU and isinstance(sp.doc.get("baro"), dict):
                has |= 32
        caps = None if cfg is None else cfg.get("caps")
        if caps is None:
            caps = [sp.detector.capability for sp in sel if sp.detector is not None]
        det = 0
        for sp in sel:
            if sp.detector is not None and sp.detector.capability in caps:
                det |= 1 << COL_OF_KIND.get(sp.kind, 7)
        old_has, old_act = int(b["has"][s]), int(b["act"][s])
        b["has"][s] = has
        b["act"][s] = has & (old_act | (has & ~old_has))  # 新增的传感器为 ACTIVE，已在 STANDBY 的保持
        b["det_en"][s] = det & 0x3
        for k in ALL_KINDS:
            present = any(sp.kind == k for sp in sel)
            if not present:
                b["state"][s, int(k)] = SensorState.OFF
            elif b["state"][s, int(k)] == SensorState.OFF:
                b["state"][s, int(k)] = SensorState.ACTIVE

    def _spawn(self, s: int, ctx: Any) -> None:
        S = self.S
        b = self.blk
        for f in b.values():
            f[s] = 0
        agent = int(S.agent_no[s])
        b["init"][s] = agent + 1
        self._derive(s)
        rig = self.rig_of(s)
        if rig is None:
            return
        self._apply_cfg(s)
        t_ns = int(getattr(ctx, "t_ns", 0))
        tick = int(getattr(ctx, "tick", 0))
        b["g_tgt"][s] = np.nan
        for k in range(N_GIMBAL):
            sp = self.gimbal.spec_of(s, k)
            if sp is None or sp.gimbal is None:
                continue
            g = sp.gimbal
            b["g_mode"][s, k] = GimbalMode.FIXED
            b["g_paz"][s, k] = g.default_az_rad
            b["g_pel"][s, k] = g.default_el_rad
            b["g_az"][s, k] = g.default_az_rad
            b["g_el"][s, k] = g.default_el_rad
        gn = rig.by_kind(SensorKind.GNSS)
        if gn is not None and b["has"][s] & gn.bit:
            self.gnss.on_spawn(s, gn, t_ns)
            b["gn_z"][s] = cbrng.normal(self.seed, 3, np.array([agent]), tick, np.arange(15, 18))[0]
        im = rig.by_kind(SensorKind.IMU)
        if im is not None and b["has"][s] & im.bit:
            self.imu.on_spawn(s, im, agent, tick)
        self.stats["spawned"] += 1

    def _remove(self, s: int) -> None:
        self.gate_caps.pop(s, None)
        for f in self.blk.values():
            f[s] = 0
        self.slot_rig[s] = -1
        self.dstamp[s] = 0
        self._roster_ver += 1
        self.stats["removed"] += 1

    def lease_gated(self, slot: int, capability: str, ctx: Any = None) -> bool:
        """R-12：该能力只在委派执行期间检测，且机体当前不持有 AGENT 租约时为真（检测器据此把传感器视为 STANDBY）。"""
        caps = self.gate_caps.get(int(slot))
        if not caps or capability not in caps:
            return False
        lease = getattr(ctx if ctx is not None else self.ctx, "lease", None)
        fn = getattr(lease, "owner_codes", None)
        if not callable(fn):
            return True
        from awr.contracts.enums import Owner

        try:
            return int(fn()[int(slot)]) != int(Owner.AGENT)
        except Exception:
            return True

    def _ready(self, slot: int) -> bool:
        S, b = self.S, self.blk
        return S is not None and b is not None and bool(S.active[slot]) and b["init"][slot] == S.agent_no[slot] + 1

    def _call(self, slot: int, fn: Callable[[], None]) -> None:
        """slot 已装配时立即执行，否则延迟到下一次 sync 装配之后（M10 在同一 tick 先于 sensors stage 设置云台时）。"""
        if self._ready(int(slot)):
            fn()
        else:
            self._deferred.append(fn)

    # ------------------------------------------------------------ 分片（按 slot 取模，只取决于 slot 编号，M13 §6.5.1）
    def part_slots(self, kind: SensorKind, part: int, mod: int) -> np.ndarray:
        key = (int(kind), mod)
        if self._parts_ver != self._roster_ver:
            self._parts.clear()
            self._parts_ver = self._roster_ver
        lst = self._parts.get(key)
        if lst is None:
            b = self.blk
            bit = {SensorKind.GNSS: 4, SensorKind.IMU: 8}.get(kind, 0)
            idx = np.flatnonzero((b["init"] != 0) & ((b["has"] & bit) != 0))
            lst = [idx[idx % mod == p] for p in range(mod)]
            self._parts[key] = lst
        return lst[part]

    def group_by_rig(self, slots: np.ndarray) -> list[tuple[int, np.ndarray]]:
        r = self.slot_rig[slots]
        u = np.unique(r)
        if u.size == 1:
            return [] if u[0] < 0 else [(int(u[0]), np.arange(slots.size))]
        return [(int(i), np.flatnonzero(r == i)) for i in u if i >= 0]

    def interest_slots(self, ctx: Any) -> np.ndarray:
        """兴趣集 并 标记集（ctx.interest 为 agent_no，≤ 80）中已装配的活动 slot，按 slot 升序。"""
        S, b = self.S, self.blk
        if S is None or b is None:
            return np.zeros(0, np.int64)
        want = np.asarray(getattr(ctx, "interest", np.zeros(0)), np.int64)
        if want.size == 0:
            return np.zeros(0, np.int64)
        act = S.active_idx()
        m = np.isin(S.agent_no[act], want) & (b["init"][act] == S.agent_no[act] + 1)
        return act[m]

    # ------------------------------------------------------------ 环境与世界
    def ground_z(self, x: float, y: float, c: Any = None) -> float:
        w = getattr(self.ctx, "world", None)
        if w is not None and callable(getattr(w, "ground_dtm", None)):
            try:
                return float(np.asarray(w.ground_dtm(np.array([[x, y]])))[0])
            except Exception:
                pass
        if c is not None and len(c) > 2 and c[2] is not None and math.isfinite(float(c[2])):
            return float(c[2])
        return 0.0

    # ------------------------------------------------------------ sim-core 内部 API（M10、M14、M08）
    def set_mode(self, slot: int, sensor: str | None, mode: GimbalMode | int | str, args: dict | None = None, *,
                 apply_tick: int | None = None, external: bool = False) -> None:
        self._call(slot, lambda: self.gimbal.set_mode(slot, sensor, mode, args, apply_tick=apply_tick, external=external))

    def set_gimbal(self, slot: int, sensor: str | None = None, *, pitch_rad: float | None = None, look_at: str | None = None,
                   mission_ctx: Any = None, apply_tick: int | None = None) -> None:
        self._call(slot, lambda: self.gimbal.set_gimbal(slot, sensor, pitch_rad=pitch_rad, look_at=look_at,
                                                        mission_ctx=mission_ctx, apply_tick=apply_tick))

    def set_default_for(self, slot: int, generator: str, params: dict | None = None, *, apply_tick: int | None = None,
                        sensor: str | None = None) -> None:
        self._call(slot, lambda: self.gimbal.set_default_for(slot, generator, params, apply_tick=apply_tick, sensor=sensor))

    def apply_item_gimbal(self, slot: int, gimbal: dict, sensor: str | None = None) -> None:
        self._call(slot, lambda: self.gimbal.apply_item_gimbal(slot, gimbal, sensor))

    def set_active(self, slot: int, sensor: str, on: bool, *, apply_tick: int | None = None, external: bool = False) -> None:
        self._call(slot, lambda: self.gimbal.set_active(slot, sensor, on, apply_tick=apply_tick, external=external))

    def spawn_target(self, target_id: str, pos_enu_m: Any, kind: str = "generic", *, conf_first: float | None = None,
                     conf_confirm: float | None = None, apply_tick: int | None = None) -> int:
        return self.detector.spawn(target_id, pos_enu_m, kind, conf_first=conf_first, conf_confirm=conf_confirm,
                                   apply_tick=apply_tick)

    def sensor_world_pose(self, slots: np.ndarray, sensor: str = "camera") -> tuple[np.ndarray, np.ndarray]:
        """(n,3) 传感器原点与 (n,3,3) WORLD←SENSOR；slot 没有该传感器时该行为 NaN（供 M10 覆盖率与检测器）。"""
        from .camera import world_pose

        S = self.S
        slots = np.asarray(slots, np.int64).reshape(-1)
        pos = np.full((slots.size, 3), np.nan)
        R = np.full((slots.size, 3, 3), np.nan)
        if S is None or slots.size == 0:
            return pos, R
        PQ = S.enu.pose_enu_flu(slots)
        for i, s in enumerate(slots):
            rig = self.rig_of(int(s))
            sp = None if rig is None else rig.by_name(sensor)
            if sp is None:
                continue
            k = COL_OF_KIND.get(sp.kind)
            az = None if k is None else self.blk["g_az"][s:s + 1, k]
            el = None if k is None else self.blk["g_el"][s:s + 1, k]
            p, r = world_pose(PQ[i:i + 1], sp, az, el)
            pos[i], R[i] = p[0], r[0]
        return pos, R

    def gimbal_rows(self, slot: int) -> list[dict]:
        b = self.blk
        out = []
        for k in range(N_GIMBAL):
            sp = self.gimbal.spec_of(slot, k)
            if sp is None or sp.gimbal is None or not (b["has"][slot] & sp.bit):
                continue
            out.append({"sensor_no": sp.sensor_no, "mode": GIMBAL_MODE_NAMES[GimbalMode(int(b["g_mode"][slot, k]))],
                        "az_rad": round(float(b["g_az"][slot, k]), 6), "el_rad": round(float(b["g_el"][slot, k]), 6),
                        "limited": bool(b["g_lim"][slot, k])})
        return out

    def state_ext_fields(self, slots: np.ndarray, t_sim_ns: int, out: list[dict]) -> None:
        """M08 state_ext 分片打包调用（M13-FR-034、§6.5.6）：全机拷贝 `loc.gnss_fix/sats/eph_m/epv_m/hdop` 与
        `sens.gimbal`（无 RNG）；属于白噪声侧缓冲（detail 并 marks）的机体再合入 `loc.err_enu_m` 与 `sens.imu`。
        `out[i]` 为第 i 个 slot 正在装配的 state_ext 对象（原地合并）。逐机标量按片向量化取出，字段与取值同逐机实现
        （`gnss.summary`、`gimbal_rows`；ADR-051 全机 2 Hz 分片编码的成本，D1 验收第 1 轮）。"""
        if self.blk is None or self.S is None:
            return
        S, b = self.S, self.blk
        sl = np.asarray(slots, np.int64).reshape(-1)
        if sl.size == 0:
            return
        ready = (S.active[sl] & (b["init"][sl].astype(np.int64) == S.agent_no[sl].astype(np.int64) + 1)).tolist()
        rig_i = self.slot_rig[sl].tolist() if sl.size and self.slot_rig.size else [-1] * sl.size
        has = b["has"][sl].tolist()
        fix = b["gn_fix"][sl].tolist()
        sats = b["gn_sats"][sl].tolist()
        # 舍入按片向量化（np.round；Python round(x, n) 每次约 1 µs，是逐行开销的主体）
        hdop = np.round(b["gn_hdop"][sl].astype(np.float64), 3).tolist()
        g_mode = b["g_mode"][sl].tolist()
        g_az = np.round(b["g_az"][sl].astype(np.float64), 6).tolist()
        g_el = np.round(b["g_el"][sl].astype(np.float64), 6).tolist()
        g_lim = b["g_lim"][sl].tolist()
        agent = S.agent_no[sl].tolist()
        side_d = self.obs.side
        gn_cache: dict[int, Any] = {}
        gb_cache: dict[int, list] = {}
        for i in range(sl.size):
            if not ready[i]:
                continue
            ri = int(rig_i[i])
            rig = self.rig_list[ri] if ri >= 0 else None
            d = out[i]
            loc = d.setdefault("loc", {"status": "TRACKING"})
            # GNSS 查表部分（同 GnssBank.summary）
            if rig is not None:
                tb = gn_cache.get(ri, 0)
                if tb == 0:
                    sp = rig.by_kind(SensorKind.GNSS)
                    if sp is None:
                        tb = None
                    else:
                        t_ = gnss_table(sp)
                        ep = [None if not math.isfinite(float(x)) else round(float(x), 4) for x in t_.eph]
                        ev = [None if not math.isfinite(float(x)) else round(float(x), 4) for x in t_.epv]
                        tb = (ep, ev)
                    gn_cache[ri] = tb
                if tb is not None and has[i] & 4:
                    f = int(fix[i])
                    loc.update({"gnss_fix": f, "sats": int(sats[i]), "eph_m": tb[0][f], "epv_m": tb[1][f],
                                "hdop": None if f == GnssFix.NO_FIX else hdop[i]})
            sens: dict[str, Any] = {}
            # 云台（同 gimbal_rows）
            if rig is not None:
                gb = gb_cache.get(ri)
                if gb is None:
                    gb = gb_cache[ri] = [(k, sp) for k in range(N_GIMBAL)
                                         if (sp := rig.by_kind(KIND_OF_COL[k])) is not None and sp.gimbal is not None]
                gr = [{"sensor_no": sp.sensor_no, "mode": _GIMBAL_NAME[int(g_mode[i][k])],
                       "az_rad": g_az[i][k], "el_rad": g_el[i][k], "limited": bool(g_lim[i][k])}
                      for k, sp in gb if has[i] & sp.bit]
                if gr:
                    sens["gimbal"] = gr
            side = side_d.get(int(agent[i]))
            if side is not None:
                if "err_enu_m" in side:
                    loc["err_enu_m"] = side["err_enu_m"]
                if "imu" in side:
                    sens["imu"] = side["imu"]
            if sens:
                d["sens"] = sens

    def ext_fields_for(self, slot: int) -> dict:
        d: dict[str, Any] = {}
        self.state_ext_fields(np.array([slot]), 0, [d])
        return d


def world_seed_of(ctx: Any) -> int:
    """world_seed：优先取 ctx.rng 流的 SeedSequence 熵（[world_seed, stream_id]），其次 ctx.cfg.world_seed、AWR_WORLD_SEED。"""
    r = getattr(ctx, "rng", None)
    try:
        g = r.get("sensor_noise") if r is not None else None
        if g is not None:
            ent = g.bit_generator.seed_seq.entropy
            if isinstance(ent, (list, tuple)) and ent:
                return int(ent[0])
    except Exception:
        pass
    cfg = getattr(ctx, "cfg", None)
    if cfg is not None and getattr(cfg, "world_seed", None) is not None:
        return int(cfg.world_seed)
    return int(os.environ.get("AWR_WORLD_SEED") or 0)
