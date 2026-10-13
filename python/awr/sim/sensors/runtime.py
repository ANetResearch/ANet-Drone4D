"""SensorRuntime：sim-core 进程内的 M13 运行期状态（传感器组、状态块视图、各 Bank、慢任务、对外 API）。

- 装配：sensors stage 每次调用先 `sync()`：对活动 slot 比较块字段 `init` 与 `agent_no + 1`，发现新 spawn 或 slot 复用即按
  机型传感器组初始化（`has`、`act`、`det_en`、云台缺省、GNSS warm start、IMU 上电偏置；随机量全部取计数器 RNG，与抽样子集
  无关）；非活动但 `init ≠ 0` 的 slot 清空（remove）。派生的 Python 侧数组（每 slot 传感器组下标）按 `init` 戳重建，
  因此 checkpoint 恢复后自动一致。
- 传感器组来自 `ctx.profiles`（M08 ProfileTable）的 `source` 目录；剧本 `vehicles[].sensors` 与 `caps` 经
  `configure_vehicle()` 给出（缺省为机型全部传感器、能力集由传感器合成，M13-FR-041）。
- sim-core 内部 API（M10、M14 经 M08 estimate）：`set_mode`、`set_gimbal`、`set_default_for`、`apply_item_gimbal`、
  `set_active`、`spawn_target`、`sensor_world_pose`、`state_ext_fields`。M08 在 slot 装配前调用这些 API 时，操作延迟到装配后执行。
- D2（M13 §15.3.4）：挂载 `configure_mounts`（实例 `sensors[]` 覆盖，M13-FR-202）与有效传感器组（`payload.mnt` 掩码，0 为 D1
  完整组）；`payload_view`、`imaging_specs`（M19 只读）；`cue`、`cue_point`（M19 引导）；`set_tasking_pointing`（M20）；
  `scan_interest`、`stream_interest`（M11 兴趣下推）；`set_light_band`（测试与 M07 推送）；`sensors_rest`（M08 的 REST 路由）。
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
from .enums import D1_KINDS, GIMBAL_MODE_NAMES, GimbalMode, GnssFix, SensorKind, SensorState
from .gimbal import COL_OF_KIND, GimbalBank, SensorError
from .gnss import GnssBank, gnss_table
from .imu import ImuBank
from .payload import PAYLOAD_BLOCK, PayloadBank, PayloadView, light_band_of
from .spec import Rig, SensorSpec, load_rig, rig_for_model

__all__ = ["SensorRuntime"]

_GIMBAL_NAME = {int(m): GIMBAL_MODE_NAMES[m] for m in GimbalMode}  # state_ext 热路径：按整数取云台模式名

ALL_KINDS = D1_KINDS  # `sensors.state` 只有 D1 七类的列（D2 的照明器与声阵列启停记在 payload 块）


class SensorRuntime:
    def __init__(self) -> None:
        self.S: Any = None
        self.blk: dict[str, np.ndarray] | None = None
        self.pblk: dict[str, np.ndarray] | None = None
        self.ctx: Any = None
        self.seed = 0
        self.tick = 0
        self.rig_list: list[Rig] = []
        self._swp1: dict[tuple, tuple] = {}   # (id(rig), 传感器名) -> (rig, 位姿所用 spec, 云台列)；`sensor_world_pose1`
        self._rig_by_dir: dict[str, int] = {}
        self._rig_by_pidx: dict[int, int] = {}
        self.slot_rig = np.zeros(0, np.int32)
        self.dstamp = np.zeros(0, np.int32)
        self.dmask = np.zeros(0, np.int64)  # 上次派生时的挂载键（mnt 与镜头），与块不一致即重新派生
        self._variants: dict[tuple, int] = {}
        self._parts: dict[tuple[int, int], list[np.ndarray]] = {}
        self._roster_ver = 0
        self._rig_groups: dict[tuple, list[tuple[int, np.ndarray]]] = {}  # group_by_rig 的按版本缓存
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
        self.payload = PayloadBank(self)
        from .lidar.scan_rt import LidarScanner
        from .nav_pub import NavPub, PayloadPub

        self.payload_pub = PayloadPub(self)
        self.nav = NavPub(self)
        self.scanner = LidarScanner(self)
        self.pending_cmds: list[tuple[int, int, Callable[[], None]]] = []  # (apply_tick, seq, fn)：D2 机体命令
        self._cmd_seq = 0
        self.sandbox: bool | None = None  # None：按 ctx 判定（时钟档 sandbox* 或 cfg.sandbox）
        self.d2_live = False  # 有 D2 挂载或非缺省指向源的机体：sensors stage 才推进指向源、成像模式与变焦（D1 大机群零开销）
        self.target_resolver: Callable[[str], tuple[int, Any] | None] | None = None  # M18：id -> (target_no, pos)
        self.ev_seen = 0
        self.stats = {"calls": 0, "spawned": 0, "removed": 0, "bridged_targets": 0}

    # ------------------------------------------------------------ 绑定与同步
    def bind(self, S: Any, ctx: Any = None) -> None:
        if self.S is not S:
            self.S = S
            self.blk = S.blocks[BLOCK]
            self.pblk = S.blocks.get(PAYLOAD_BLOCK)
            tb = S.blocks.get(TARGET_BLOCK)
            if tb is not None:
                self.detector.targets.bind(tb)
            n = S.capacity
            self.slot_rig = np.full(n, -1, np.int32)
            self.dstamp = np.zeros(n, np.int32)
            self.dmask = np.zeros(n, np.int64)
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
        P = self.pblk
        key = (S.active.tobytes(), S.agent_no.tobytes(), b["init"].tobytes(), self.dstamp.tobytes(),
               b"" if P is None else P["mnt"].tobytes() + P["lens"].tobytes())
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
        mk = self._mount_keys()
        bad = np.flatnonzero((self.dstamp != b["init"]) | (self.dmask != mk))
        for s in bad:
            self._derive(int(s))
        if self._deferred:
            todo, self._deferred = self._deferred, []
            for fn in todo:
                with contextlib.suppress(SensorError):
                    fn()
        self.d2_live = P is not None and bool(P["mnt"].any() or P["src"].any())
        self._sync_key = (S.active.tobytes(), S.agent_no.tobytes(), b["init"].tobytes(), self.dstamp.tobytes(),
                          b"" if P is None else P["mnt"].tobytes() + P["lens"].tobytes())

    def _mount_keys(self) -> np.ndarray:
        """每 slot 的挂载键：`mnt` 掩码与云台列镜头覆盖（f32 位）合成的 i64；无 payload 块时全 0。"""
        P = self.pblk
        if P is None:
            return np.zeros(self.dstamp.size, np.int64)
        k = P["mnt"].astype(np.int64)
        ln = np.nan_to_num(P["lens"], nan=0.0).view(np.uint32).astype(np.int64)
        for c in range(ln.shape[1]):
            k = k ^ (ln[:, c] << (32 + c))
        return k

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
        except Exception as e:  # 机型的传感器文件无效：该 slot 无传感器并记日志（启动前应已由 validate_profiles 拒绝）
            import logging

            logging.getLogger("awr.sim.sensors").warning("sensor rig of profile index %s not loaded: %s", pidx, e)
            self.stats["rig_errors"] = self.stats.get("rig_errors", 0) + 1
            rig = Rig("unknown", ())
        i = len(self.rig_list)
        self.rig_list.append(rig)
        self._rig_by_dir[key] = i
        self._rig_by_pidx[pidx] = i
        return i

    @staticmethod
    def validate_profiles(T: Any) -> dict[str, list[str]]:
        """机型表中每个机型的传感器组能否装配（M13-FR-201：规格、spec_ref 与 SN 规则失败即拒绝该机型）。返回
        {profile_id: problems}（空为全部通过）；M08 在 ProfileTable 加载或 sim-core 启动时调用，非空时以 353 拒绝。"""
        from .spec import SensorSpecError

        out: dict[str, list[str]] = {}
        seen: dict[str, list[str]] = {}
        for pid in list(getattr(T, "ids", [])):
            try:
                src = getattr(T.get(pid), "source", "")
            except Exception:
                continue
            if not src:
                continue
            d = str(Path(src).parent)
            if d not in seen:
                try:
                    load_rig(Path(d))
                    seen[d] = []
                except SensorSpecError as e:
                    seen[d] = [f"{e.where}: {x}" for x in e.problems]
            if seen[d]:
                out[str(pid)] = seen[d]
        return out

    def _derive(self, s: int) -> None:
        b = self.blk
        if b["init"][s] == 0:
            self.slot_rig[s] = -1
        else:
            base = self._rig_index_for_pidx(int(self.S.profile_id[s]))
            self.slot_rig[s] = self._variant_index(base, s)
        self.dstamp[s] = b["init"][s]
        self.dmask[s] = self._mount_keys()[s] if self.pblk is not None else 0
        if self.pblk is not None and int(self.pblk["mnt"][s]) != 0:
            self.d2_live = True
        self._roster_ver += 1

    def _variant_index(self, base: int, s: int) -> int:
        """有效传感器组：`mnt = 0` 为完整组（D1），否则按掩码与镜头覆盖取子集（同一 (组, 掩码, 镜头) 只建一次）。"""
        P = self.pblk
        if P is None or int(P["mnt"][s]) == 0:
            return base
        rig = self.rig_list[base]
        mask = int(P["mnt"][s])
        lens: list[float] = []
        for k in range(N_GIMBAL):
            f = float(P["lens"][s, k])
            sp = rig.subset(mask).col_specs[k]
            if np.isfinite(f) and sp is not None and sp.lenses:
                lens += [float(sp.sensor_no), f]
        key = (base, mask, tuple(lens))
        i = self._variants.get(key)
        if i is None:
            i = len(self.rig_list)
            self.rig_list.append(rig.subset(mask, tuple(lens)))
            self._variants[key] = i
        return i

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
            # D2-INT：对象形式（v2）映射的机型在 spawn 之后改剧本子集时按新子集换装（与 configure_mounts 同一路径）；
            # P600 profile 2.0.0 切换为对象形式后，D1 剧本的 `vehicles[].sensors` 子集照常生效
            if sensors is not None and self.pblk is not None and not (self.vcfg[str(vehicle_id)].get("instance")):
                base = self.rig_list[self._rig_index_for_pidx(int(self.S.profile_id[s]))]
                if base.d2:
                    from types import SimpleNamespace

                    self._remount(s, base, SimpleNamespace(mask=base.mask_of(list(sensors)), lens={}))
                    return
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
        sel = list(rig.specs) if rig.mask else self._selected(rig, cfg)
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
        P = self.pblk
        if P is not None:
            for f in P.values():
                f[s] = 0
        agent = int(S.agent_no[s])
        b["init"][s] = agent + 1
        if P is not None:
            base = self.rig_list[self._rig_index_for_pidx(int(S.profile_id[s]))]
            try:
                P["mnt"][s], lens = self._mount_for(base, self.vcfg.get(self.uav_id(S, s)), ctx, int(S.profile_id[s]))
            except SensorError:
                P["mnt"][s], lens = (base.default_mask(sandbox=self._is_sandbox(ctx)) if base.d2 else 0), {}
            P["lens"][s] = np.nan
            for k, f in lens.items():
                P["lens"][s, k] = f
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
        if P is not None:
            self.payload.on_spawn(s, rig, ctx)
            cfg = self.vcfg.get(self.uav_id(S, s)) or {}
            for k, a in (cfg.get("analysis") or {}).items():
                P["res"][s, k] = 1 if a == "4k" else 0
        self.stats["spawned"] += 1

    def _remove(self, s: int) -> None:
        self.gate_caps.pop(s, None)
        for f in self.blk.values():
            f[s] = 0
        if self.pblk is not None:
            for f in self.pblk.values():
                f[s] = 0
        self.scanner.forget(s)
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
        # slot_rig 只在 _derive 与移除时改写，两处都推进 _roster_ver：同一版本、同一组 slot 的分组不变（按版本缓存；
        # D2-AC-08：GNSS、IMU 分片每次调用一次，np.unique 在主循环中约 30 µs）。返回值只读，调用方不改写
        key = (self._roster_ver, slots.dtype.str, slots.tobytes())
        cache = self._rig_groups
        got = cache.get(key)
        if got is not None:
            return got
        if len(cache) > 64:
            cache.clear()
        r = self.slot_rig[slots]
        u = np.unique(r)
        if u.size == 1:
            out = [] if u[0] < 0 else [(int(u[0]), np.arange(slots.size))]
        else:
            out = [(int(i), np.flatnonzero(r == i)) for i in u if i >= 0]
        for _i, sel in out:
            sel.flags.writeable = False
        cache[key] = out
        return out

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
            k = rig.col_of(sensor) if rig.mask else COL_OF_KIND.get(sp.kind)
            az = None if k is None else self.blk["g_az"][s:s + 1, k]
            el = None if k is None else self.blk["g_el"][s:s + 1, k]
            if k is not None and sp.gimbal is None and sp.on_gimbal_of is not None:
                gs = rig.col_specs[k]
                sp = gs if gs is not None else sp
            p, r = world_pose(PQ[i:i + 1], sp, az, el)
            pos[i], R[i] = p[0], r[0]
        return pos, R

    def sensor_world_pose1(self, s: int, sensor: str, pq: np.ndarray, Rb: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray] | None:
        """单个 slot 的传感器原点 (3,) 与 WORLD←SENSOR (3, 3)，机体位姿取调用方给出的 1×7 行（`S.enu.pose_enu_flu` 的一行）；
        slot 没有该传感器时为 None。与 `sensor_world_pose` 的同一行逐位相同（同一 `world_pose`、同一云台列与跟随规则），免去
        NaN 数组与第二次位姿拷贝（M19 每次评估一个平台调用，D2-FX-R2-sim 第 2 次）。"""
        from .camera import world_pose

        rig = self.rig_of(int(s))
        if rig is None:
            return None
        ck = (id(rig), sensor)
        c = self._swp1.get(ck)
        if c is None or c[0] is not rig:
            sp = rig.by_name(sensor)
            k = None
            if sp is not None:
                k = rig.col_of(sensor) if rig.mask else COL_OF_KIND.get(sp.kind)
                if k is not None and sp.gimbal is None and sp.on_gimbal_of is not None:
                    gs = rig.col_specs[k]
                    sp = gs if gs is not None else sp
            if len(self._swp1) > 1024:
                self._swp1.clear()
            c = self._swp1[ck] = (rig, sp, k)
        sp, k = c[1], c[2]
        if sp is None:
            return None
        az = None if k is None else self.blk["g_az"][s:s + 1, k]
        el = None if k is None else self.blk["g_el"][s:s + 1, k]
        p, r = world_pose(pq, sp, az, el, Rb=Rb)
        return p[0], r[0]

    # ------------------------------------------------------------ D2：挂载（M13-FR-202）
    def _is_sandbox(self, ctx: Any = None) -> bool:
        """沙盒实例的缺省挂载规则（只装对象形式且 default 的槽位）：显式设置 > `cfg.sandbox` > 时钟档以 sandbox 开头。"""
        if self.sandbox is not None:
            return bool(self.sandbox)
        c = ctx if ctx is not None else self.ctx
        cfg = getattr(c, "cfg", None)
        if cfg is not None and getattr(cfg, "sandbox", None) is not None:
            return bool(cfg.sandbox)
        prof = getattr(c, "profile", None)
        name = getattr(prof, "name", prof) if prof is not None else os.environ.get("AWR_CLOCK_PROFILE", "")
        return str(name or "").startswith("sandbox")

    def _caps_for_pidx(self, pidx: int) -> list[str]:
        T = getattr(self.ctx, "profiles", None)
        try:
            doc = T.get(T.ids[pidx]).doc or {}
        except Exception:
            return []
        return [str(c) for c in (doc.get("capabilities") or [])]

    def _mount_for(self, base: Rig, cfg: dict | None, ctx: Any, pidx: int = -1) -> tuple[int, dict[int, float]]:
        """spawn 时的挂载掩码与镜头覆盖（列 -> mm）：实例配置 > 纯 D1 组（0）> 剧本子集 > 缺省挂载。"""
        from .mounts import resolve_mounts

        if cfg and (cfg.get("instance") or cfg.get("mounts") is not None):  # 机队实例（configure_mounts，含 None = 缺省挂载）
            if not base.d2:
                return 0, {}  # 纯 D1 映射的机型：完整组（过渡期，D1 行为）
            caps = cfg.get("capabilities") or (self._caps_for_pidx(pidx) if pidx >= 0 else [])
            r = resolve_mounts(base, cfg["mounts"], caps, allow_legacy=False)
            sub = base.subset(r.mask)
            lens = {}
            for no, f in r.lens.items():
                sp = sub.by_no(no)
                k = None if sp is None else sub.col_of(sp.name)
                if k is not None:
                    lens[k] = f
            cfg["analysis"] = {k: r.analysis[sp.sensor_no] for k in range(N_GIMBAL)
                               if (sp := sub.col_specs[k]) is not None and sp.sensor_no in r.analysis}
            return r.mask, lens
        if not base.d2:
            return 0, {}
        if cfg and cfg.get("sensors") is not None:
            return base.mask_of(list(cfg["sensors"])), {}
        return base.default_mask(sandbox=self._is_sandbox(ctx)), {}

    def configure_mounts(self, vehicle_id: str, mounts: Any, *, capabilities: list[str] | None = None,
                         profile_id: str | None = None) -> dict:
        """机队实例的挂载与参数覆盖（M13-FR-202；M08 `fleet/update`、`POST /vehicles` 在 LANDED 且上锁时调用）。

        在 spawn 之前或之后调用均可；之后调用时立即换装（云台与成像列重新初始化）。返回挂载汇总（质量、功率、逐槽位）；
        非法时抛 SensorError（560、561、522）。`capabilities` 缺省取机型 `params.yaml` 的 `capabilities[]`。"""
        from .mounts import mount_summary, resolve_mounts

        vid = str(vehicle_id)
        s = self.slot_of(vid)
        base: Rig | None = None
        if s >= 0 and self.blk is not None and self.blk["init"][s] != 0:
            base = self.rig_list[self._rig_index_for_pidx(int(self.S.profile_id[s]))]
            if capabilities is None:
                capabilities = self._caps_for_pidx(int(self.S.profile_id[s]))
        elif profile_id is not None:
            T = getattr(self.ctx, "profiles", None)
            try:
                base = self.rig_list[self._rig_index_for_pidx(int(T.ids.index(profile_id)))]
                if capabilities is None:
                    capabilities = self._caps_for_pidx(int(T.ids.index(profile_id)))
            except Exception:
                base = None
            if base is None:  # 尚未绑定 ctx（首个 stage 之前）：按机型目录取传感器组
                from .describe import dir_for_profile

                d = dir_for_profile(str(profile_id).split("@")[0])
                base = load_rig(d) if d is not None else None
        cfg = self.vcfg.setdefault(vid, {"sensors": None, "caps": None, "delegated": None})
        cfg["mounts"] = mounts
        cfg["instance"] = True  # 机队实例：缺省挂载按沙盒规则（不装 D1 槽位），与时钟档无关
        cfg["capabilities"] = list(capabilities or [])
        if base is None:
            return {"deferred": True}
        if not base.d2:  # 纯 D1 映射的机型（过渡期）：实例为完整组（挂载掩码 0），只校验槽位名
            from .mounts import normalize_mounts

            bad = [n for n in normalize_mounts(mounts) if base.by_name(n) is None]
            if bad:
                raise SensorError(560, "SLOT_UNKNOWN", sensor=bad[0], slots=[sp.name for sp in base.specs])
            return mount_summary(base, base.all_mask)
        r = resolve_mounts(base, mounts, cfg["capabilities"], allow_legacy=False)
        if s >= 0 and self.pblk is not None and self.blk["init"][s] != 0:
            self._remount(s, base, r)
        return mount_summary(base.subset(r.mask), r.mask)

    def _remount(self, s: int, base: Rig, r: Any) -> None:
        P = self.pblk
        old = self.rig_of(s)
        P["mnt"][s] = r.mask
        P["off"][s] = 0
        P["lens"][s] = np.nan
        sub = base.subset(r.mask)
        for no, f in r.lens.items():
            sp = sub.by_no(no)
            k = None if sp is None else sub.col_of(sp.name)
            if k is not None:
                P["lens"][s, k] = f
        self._derive(s)
        rig = self.rig_of(s)
        self._apply_cfg(s)
        b = self.blk
        for k in range(N_GIMBAL):  # 列所有者变化的列重新初始化（云台缺省、指向源、变焦与成像）
            a = None if old is None else old.col_specs[k]
            c = rig.col_specs[k]
            if a is not None and c is not None and a.sensor_no == c.sensor_no and a.lens_mm == c.lens_mm:
                continue
            for name in ("src", "op_on", "tk_on", "cu_on", "im_cmd", "im_mode", "res", "pal", "cue"):
                P[name][s, k] = 0
            P["ill_cfg"][s, k] = 0
            P["ill_on"][s, k] = 0
            P["f"][s, k] = np.nan
            P["f_cmd"][s, k] = np.nan
            if c is not None and c.gimbal is not None:
                g = c.gimbal
                b["g_mode"][s, k] = GimbalMode.FIXED
                b["g_paz"][s, k] = b["g_az"][s, k] = g.default_az_rad
                b["g_pel"][s, k] = b["g_el"][s, k] = g.default_el_rad
                b["g_tgt"][s, k] = np.nan
                b["g_dyn"][s, k] = False
                P["df_mode"][s, k] = 0
                P["df_a"][s, k] = (g.default_az_rad, g.default_el_rad)
            if c is not None and c.zoom is not None:
                P["f"][s, k] = P["f_cmd"][s, k] = c.zoom.f_min_mm
                band = self.payload.light_band(self.ctx)
                P["band"][s] = int(band)
                P["im_mode"][s, k] = int(self.payload._auto_mode(rig, k, band))
        for k in range(N_GIMBAL):
            c = rig.col_specs[k]
            if c is not None and c.zoom is not None:
                no = c.sensor_no
                P["res"][s, k] = 1 if r.analysis.get(no) == "4k" else 0
                self.payload._apply_illum(s, rig, k)

    def mount_summary(self, slot: int) -> dict:
        from .mounts import mount_summary

        rig = self.rig_of(slot)
        return {} if rig is None else mount_summary(rig, rig.mask or None)

    def mounted_names(self, vehicle_id: str) -> list[str] | None:
        """已装配机体当前挂载的槽位名（roster `sensors[]` 的 names 参数，M08 传给 `register_sensor_rig` 的钩子）；未装配为 None。"""
        s = self.slot_of(str(vehicle_id))
        rig = self.rig_of(s) if s >= 0 else None
        if rig is None:
            return None
        if rig.mask:
            return [sp.name for sp in rig.specs]
        b = self.blk
        return [sp.name for sp in rig.specs if (b["has"][s] & sp.bit) or not sp.bit]

    def set_zoom(self, slot: int, sensor: str, f_mm: float) -> bool:
        """sim-core 内部变焦（M20 站位与跟随用 `feasible_view` 给出的焦距；当步生效、不写日志）：钳到镜头范围，无光学变焦返回 False。"""
        s = int(slot)
        rig = self.rig_of(s)
        sp = None if rig is None else rig.by_name(str(sensor))
        k = None if sp is None else rig.col_of(sp.name)
        if sp is None or sp.zoom is None or k is None or self.pblk is None:
            return False
        z = sp.zoom
        self.payload.zoom_to(s, k, min(max(float(f_mm), z.f_min_mm), z.f_max_mm))
        return True

    # ------------------------------------------------------------ D2：M19、M20 的读取与引导（M13 §15.3.4）
    def tick_ns(self) -> int:
        return int(getattr(self.ctx, "t_ns", 0))

    def payload_view(self, slot: int) -> PayloadView | None:
        return self.payload.view(int(slot))

    def payload_view_key(self, slot: int) -> tuple | None:
        """`payload_view(slot)` 除云台角外的取值键（`PayloadBank.view_key`；M19 按键复用成像状态）。"""
        return self.payload.view_key(int(slot))

    def imaging_specs(self, slot: int) -> list:
        return self.payload.imaging_states(int(slot))

    def col_for(self, slot: int, gimbal_id: Any = None) -> int | None:
        """云台列：None 为主云台；`g0`、`g1`、整数列号或传感器名。"""
        if gimbal_id is None:
            return self.gimbal.primary_col(int(slot))
        if isinstance(gimbal_id, (int, np.integer)):
            return int(gimbal_id) if 0 <= int(gimbal_id) < N_GIMBAL else None
        try:
            cols = self.gimbal._cols(int(slot), str(gimbal_id))
        except SensorError:
            return None
        return cols[0] if cols else None

    def cue(self, slot: int, az_enu_deg: float, el_deg: float, range_m: float | None, source_sensor: str | None = None) -> bool:
        """M19 引导（M13-FR-207）：方位 az（World ENU，东为 0、逆时针为正，度）、俯仰 el（向上为正，度）；有距离时 LOOK_AT
        `p_sensor + range·u`，无距离时取方位射线与 DSM 的交点（无交点取 300 m 处）。operator、tasking 生效或该列 cue = off 时
        只记录不执行，返回 False。"""
        from .gimbal import CUE_DEFAULT_RANGE_M

        s = int(slot)
        k = self.col_for(s)
        if k is None or not self._ready(s):
            return False
        P = self.pblk
        if P is not None and int(P["cue"][s, k]) == 2:
            self.stats["cue_off"] = self.stats.get("cue_off", 0) + 1
            return False
        rig = self.rig_of(s)
        sp = rig.col_specs[k]
        PQ = self.S.enu.pose_enu_flu(np.array([s]))
        from .frames import quat_to_R

        Rb = quat_to_R(PQ[:, 3:7])[0]
        p0 = PQ[0, :3] + Rb @ sp.mount_t
        a, e = math.radians(float(az_enu_deg)), math.radians(float(el_deg))
        u = np.array([math.cos(e) * math.cos(a), math.cos(e) * math.sin(a), math.sin(e)])
        if range_m is not None and math.isfinite(float(range_m)) and float(range_m) > 0:
            p = p0 + float(range_m) * u
        else:
            p = p0 + CUE_DEFAULT_RANGE_M * u
            w = getattr(self.ctx, "world", None)
            fn = getattr(w, "ray_hit", None)
            if callable(fn):
                try:
                    h = fn(p0, u, 5000.0)
                    if getattr(h, "hit", False) and h.point_enu_m is not None:
                        p = np.asarray(h.point_enu_m, np.float64)
                except Exception:
                    pass
        return self.gimbal.cue_point(s, k, p, self.tick_ns())

    def cue_point(self, slot: int, p_enu: Any, gimbal_id: Any = None) -> bool:
        """M19 以点引导（M18-M19 PRD §6.12 的 look_at 形式）。"""
        k = self.col_for(int(slot), gimbal_id)
        if k is None or not self._ready(int(slot)):
            return False
        P = self.pblk
        if P is not None and int(P["cue"][int(slot), k]) == 2:
            return False
        return self.gimbal.cue_point(int(slot), k, p_enu, self.tick_ns())

    def set_tasking_pointing(self, slot: int, gimbal_id: Any, look_at_enu_m: Any | None) -> None:
        """M20 的架次项与接力站位 LOOK_AT（None 释放）；sim-core 内部调用，当步生效，不写日志。"""
        s = int(slot)

        def go() -> None:
            k = self.col_for(s, gimbal_id)
            if k is not None and self.pblk is not None:
                self.gimbal.set_tasking(s, k, look_at_enu_m, self.tick_ns())

        self._call(s, go)

    def stream_interest(self, stream: str, agent_nos: Any | None) -> None:
        """按机体生成的数据流订阅（M11 兴趣下推 `streams{}` 的直接入口；None 恢复读取 `ctx.streams`）：`nav`（≤ 4 架）、
        `lidar`（每会话 1 架，列表中最后一个为最新订阅者）。"""
        v = None if agent_nos is None else [int(a) for a in np.asarray(agent_nos).reshape(-1).tolist()]
        if stream == "nav":
            self.nav.override = v
        elif stream == "lidar":
            self.scanner.override = v
        else:
            raise ValueError(f"unknown stream {stream!r}")

    def scan_interest(self, slots: Any) -> None:
        """M13 §15.3.4：MID-360 扫描兴趣（slot 列表，最后一个为最新订阅者；空列表停止）。"""
        S = self.S
        sl = [int(x) for x in np.asarray(slots).reshape(-1).tolist()]
        self.scanner.override = [int(S.agent_no[x]) for x in sl] if S is not None else []

    def set_light_band(self, band: Any | None) -> None:
        """照度档覆盖（测试与无 M07 D2 部分时的推送入口）；None 恢复读取 M07。"""
        self.payload.band_override = light_band_of(band) if band is not None else None

    # ------------------------------------------------------------ D2：识别物（M18 实体表，只读）
    def _targets_block(self) -> dict | None:
        ents = getattr(self.ctx, "entities", None)
        tb = None
        if ents is not None:
            try:
                tb = ents["target"]
            except Exception:
                tb = getattr(ents, "target", None)
        if tb is None and self.S is not None:
            tb = self.S.blocks.get("targets")
        blocks = getattr(tb, "blocks", None)
        if isinstance(blocks, dict):  # EntityTables 的表对象：合并各模块登记的列
            merged: dict = {}
            for v in blocks.values():
                merged.update(v)
            tb = merged
        return tb if isinstance(tb, dict) else None

    def target_pos(self, target_no: int) -> np.ndarray | None:
        if target_no < 0:
            return None
        tb = self._targets_block()
        if tb is None or "tgt_pos" not in tb or "target_no" not in tb:
            return None
        act = tb.get("active")
        idx = np.flatnonzero((tb["target_no"] == target_no) & ((act != 0) if act is not None else True))
        return None if idx.size == 0 else np.asarray(tb["tgt_pos"][int(idx[0])], np.float64).copy()

    def resolve_target(self, target_id: str) -> tuple[int, np.ndarray] | None:
        """识别物 id -> (target_no, 位置)：M18 登记的解析函数 `target_resolver`，否则 roster 的 kind = target 条目。"""
        fn = self.target_resolver
        if callable(fn):
            try:
                r = fn(str(target_id))
            except Exception:
                r = None
            if r is not None:
                no, pos = r
                p = self.target_pos(int(no)) if pos is None else np.asarray(pos, np.float64)
                return (int(no), p) if p is not None else None
            return None
        ro = getattr(self.ctx, "roster", None)
        get = getattr(ro, "target_no_of", None)
        if callable(get):
            try:
                no = get(str(target_id))
            except Exception:
                no = None
            if no is not None:
                p = self.target_pos(int(no))
                return (int(no), p) if p is not None else None
        return None

    def target_id_of(self, target_no: int) -> str | None:
        ro = getattr(self.ctx, "roster", None)
        fn = getattr(ro, "target_id_of", None)
        if callable(fn) and target_no >= 0:
            try:
                v = fn(int(target_no))
                return None if v is None else str(v)
            except Exception:
                return None
        return None

    # ------------------------------------------------------------ D2：机体命令的排程（apply_tick 生效）
    def schedule(self, apply_tick: int, fn: Callable[[], None]) -> None:
        import bisect

        self._cmd_seq += 1
        bisect.insort(self.pending_cmds, (int(apply_tick), self._cmd_seq, fn), key=lambda x: (x[0], x[1]))

    def apply_pending_cmds(self, tick: int) -> int:
        n = 0
        while self.pending_cmds and self.pending_cmds[0][0] <= tick:
            _t, _q, fn = self.pending_cmds.pop(0)
            with contextlib.suppress(SensorError):
                fn()
            n += 1
        return n

    def sensors_rest(self, vehicle_id: str) -> dict | None:
        """`GET .../vehicles/{vid}/sensors`（M08 路由调用）：载荷状态与逐传感器挂载、状态、模式。"""
        s = self.slot_of(str(vehicle_id))
        if s < 0 or not self._ready(s):
            return None
        return self.payload.rest_sensors(s, self.tick_ns())

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
                                         if (sp := rig.col_specs[k]) is not None and sp.gimbal is not None]
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
                if _state_ext_payload() and self.pblk is not None and int(self.pblk["mnt"][int(sl[i])]) != 0:
                    m = self.payload.message(int(sl[i]), int(t_sim_ns))  # M13-FR-209：选中机（兴趣集）2 Hz
                    if m is not None:
                        sens["payload"] = m
            if sens:
                d["sens"] = sens

    def ext_fields_for(self, slot: int) -> dict:
        d: dict[str, Any] = {}
        self.state_ext_fields(np.array([slot]), 0, [d])
        return d


_SEP: list[bool] = []


def _state_ext_payload() -> bool:
    """`uav_state_ext.schema.json` 是否已登记 `sens.payload`（M13-FR-209，已提请 M00）；登记前不写该字段。"""
    if not _SEP:
        try:
            import json

            from awr.contracts._paths import contracts_root

            d = json.loads((contracts_root() / "rt" / "payloads" / "uav_state_ext.schema.json").read_text(encoding="utf-8"))
            _SEP.append("payload" in ((d.get("properties") or {}).get("sens") or {}).get("properties", {}))
        except Exception:
            _SEP.append(False)
    return _SEP[0]


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
