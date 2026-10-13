"""`uav/{id}/payload` 与 `uav/{id}/sensor/nav` 数据产品的打包与发布（M13-FR-209–211；M13 §15.3.2、§15.3.3；AWR-04 §11.7）。

- 慢任务 `m13.payload_pub`（2 Hz【仿真】）：`awr.uav.payload.v1`，对每架 D2 机体（挂载掩码非 0）与兴趣集（detail 并 marks）中的
  D1 机体生成，以全集一条消息发布到 `state/sim-core/payload`：msgpack `{v: 1, t_sim_ns, items: {<uav id>: payload}}`（M11 网关
  按键拆到 `uav/{id}/payload`，d2-WP-03-to-M13 第 1 条）。D2 机体超过 `PAYLOAD_ALL_MAX`（12，17 §17.7.2 的"全部机体 ≤ 12"）
  时只为兴趣集生成；消息按 `PAYLOAD_SLICE` 架一轮分片构建（D2-FX-R2-other，见 `PayloadPub`）。
- 慢任务 `m13.nav_pub`（10 Hz【仿真】）：`awr.sensor.nav.v1`，只为被订阅的机体生成（M11 兴趣下推的 `streams.nav`，每会话
  ≤ 4 架，超出的按 agent_no 升序截断并计数 `nav_dropped`），发布到 `state/sim-core/nav`：msgpack `{v: 1, t_sim_ns, items: {<uav id>: nav}}`。
  IMU 为飞控 IMU 的一个 200 Hz 样本（D1 模型，比力与角速度，FLU）；GNSS 为真值加 D1 GM 误差与白噪声（位置），速度白噪声按
  fix 档（本文设定，D）；气压高度为 ISA 气压加 D1 气压计 GM 与白噪声；`loc_mode` 按 M13-FR-211（P1，度量 `tasking.rtk_cover`）。

计数器 RNG（规则 ①，与订阅者无关）：GNSS 位置白噪声沿用通道 0–2，气压计通道 24–25，速度白噪声通道 32–34（`cbrng.CHANNELS`）。
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import numpy as np

from . import cbrng
from .baro import alt_from_pressure, isa_pressure
from .enums import GnssFix, SensorKind

if TYPE_CHECKING:
    from .runtime import SensorRuntime

__all__ = ["FIX_WIRE", "NAV_MAX", "VEL_SIGMA", "NavPub", "PayloadPub"]

PAYLOAD_PERIOD_NS = 500_000_000
# 不受兴趣集控制的 D2 机体数上限（17 §17.7.2 "全部机体 ≤ 12"，单会话机群上限 AWR-04 §4.4）；超过时只为兴趣集生成
PAYLOAD_ALL_MAX = 12
PAYLOAD_SLICE = 4  # 每轮构建的消息数（逐机约 50 µs，N = 1000 实测；每轮 ≤ 约 0.3 ms，M13 登记的慢任务预算 300 µs）
NAV_PERIOD_NS = 100_000_000
NAV_MAX = 4  # 每会话 nav 订阅上限（M13 §15.4）
FIX_WIRE = {GnssFix.NO_FIX: "none", GnssFix.SINGLE: "3d", GnssFix.DGPS: "3d", GnssFix.RTK_FLOAT: "rtk_float",
            GnssFix.RTK_FIXED: "rtk_fixed"}
# GNSS 速度白噪声 1sigma（m/s，水平、垂直）按 fix 档；本文设定（D），与位置白噪声同量级比例
VEL_SIGMA = {GnssFix.NO_FIX: (float("nan"), float("nan")), GnssFix.SINGLE: (0.10, 0.15), GnssFix.DGPS: (0.05, 0.08),
             GnssFix.RTK_FLOAT: (0.03, 0.05), GnssFix.RTK_FIXED: (0.01, 0.02)}
CH_VEL = np.arange(32, 35)
CH_BARO = np.arange(24, 26)
METRIC_RTK = "tasking.rtk_cover"  # M20 登记：metric(x_m, y_m) -> 1.0 有带 rtk_base 的机巢覆盖、0.0 无（度量为标量）


def pack_items(t_ns: int, items: list[dict]) -> bytes:
    """`{v: 1, t_sim_ns, items: {<uav id>: 载荷}}`（M11 网关的总线载荷形状）。"""
    import msgpack

    return msgpack.packb({"v": 1, "t_sim_ns": int(t_ns), "items": {str(it["uav"]): it for it in items}}, use_bin_type=True)


def _r(x: float, nd: int) -> float | None:
    v = float(x)
    return None if not math.isfinite(v) else round(v, nd)


def _bus_put(ctx: Any, holder: Any, key: str, payload: bytes) -> bool:
    ev = getattr(ctx, "events", None)
    bus = getattr(ev, "bus", None)
    if bus is None:
        return False
    try:
        if holder._pub is None or holder._bus is not bus:
            holder._bus = bus
            holder._pub = bus.publisher(key)
        holder._pub.put(payload)
        return True
    except Exception:
        holder._pub = None
        return False


class PayloadPub:
    """`m13.payload_pub`：每 500 ms【仿真】一个周期。

    生成集合（17 §17.7.2 Topic 表：`uav/{id}/payload` 不受兴趣集控制的前提是"全部机体 ≤ 12"，即单会话机群上限，AWR-04 §4.4）：
    D2 机体（挂载掩码非 0）不超过 PAYLOAD_ALL_MAX 架时为全部 D2 机体并兴趣集；超过时只为兴趣集（detail 并 marks，≤ 80）生成。
    D2 起 P600 为对象形式（D2-INT），共享展示与 D1 阶梯的 P600 全部算作 D2 机体，此前 N = 1000 时每周期在同一轮内为 1000 架
    逐机构建消息（约 50 µs/架），单步出现约 50 ms 的周期尖峰，RTF 0.86（D2 验收第 1 轮 D2-AC-26 → D1-AC-07、08、27）。
    分片：周期开始时取集合，之后每轮至多构建 PAYLOAD_SLICE 架（逐机消息带构建时刻的 `t_sim_ns`），全部构建完成后以周期
    开始时刻为头部 `t_sim_ns` 一次发布（每周期一条全集消息，M11 网关按键拆分）。仿真时间回退（重置、恢复）时放弃在途周期。"""

    def __init__(self, rt: SensorRuntime) -> None:
        self.rt = rt
        self.next_due = 0
        self._pub = None
        self._bus = None
        self.sink: Any = None  # 测试钩子：替代总线发布，参数为 bytes
        self.last: list[dict] = []
        self.stats = {"rounds": 0, "items": 0, "puts": 0, "slices": 0, "interest_only": 0}
        self._queue: list[int] | None = None  # 在途周期的 slot（升序）
        self._pos = 0
        self._t0 = 0
        self._items: list[dict] = []

    def slots(self, ctx: Any) -> np.ndarray:
        rt = self.rt
        S, P = rt.S, rt.pblk
        if S is None or P is None:
            return np.zeros(0, np.int64)
        act = S.active_idx()
        d2 = act[P["mnt"][act] != 0]
        interest = rt.interest_slots(ctx)
        if d2.size > PAYLOAD_ALL_MAX:
            self.stats["interest_only"] += 1
            return np.asarray(interest, np.int64)
        return np.union1d(d2, interest).astype(np.int64)

    def run_slow(self, ctx: Any) -> Any:
        rt = self.rt
        if rt.S is None or rt.pblk is None:
            return False
        t = int(getattr(ctx, "t_ns", 0))
        if self._queue is not None and t < self._t0:
            self._queue = None  # 仿真时间回退：放弃在途周期
        if self.next_due - t > PAYLOAD_PERIOD_NS:
            self.next_due = 0
        if self._queue is None:
            if t < self.next_due:
                return False
            self.next_due = (t // PAYLOAD_PERIOD_NS + 1) * PAYLOAD_PERIOD_NS
            sl = self.slots(ctx)
            self.stats["rounds"] += 1
            if sl.size == 0:
                self.last = []
                return None
            self._queue, self._pos, self._t0, self._items = sl.tolist(), 0, t, []
        q = self._queue
        chunk = q[self._pos:self._pos + PAYLOAD_SLICE]
        self._pos += len(chunk)
        self.stats["slices"] += 1
        for s in chunk:
            m = rt.payload.message(int(s), t)
            if m is not None:
                self._items.append(m)
        if self._pos < len(q):
            return None
        items, t0 = self._items, self._t0
        self._queue, self._items = None, []
        self.last = items
        if not items:
            return None
        payload = pack_items(t0, items)
        if self.sink is not None:
            self.sink(payload)
            self.stats["puts"] += 1
        elif _bus_put(ctx, self, _key("STATE_PAYLOAD"), payload):
            self.stats["puts"] += 1
        self.stats["items"] += len(items)
        return None


def _key(name: str) -> str:
    from awr.contracts import bus_keys

    return str(getattr(bus_keys, name))


class NavPub:
    def __init__(self, rt: SensorRuntime) -> None:
        self.rt = rt
        self.next_due = 0
        self._pub = None
        self._bus = None
        self.sink: Any = None
        self.override: list[int] | None = None  # 测试或直接调用的订阅集（agent_no）
        self.last: list[dict] = []
        self.stats = {"rounds": 0, "items": 0, "puts": 0, "nav_dropped": 0}

    def wanted(self, ctx: Any) -> list[int]:
        """被订阅的 agent_no（升序，≤ NAV_MAX）：`stream_interest` 覆盖 > `ctx.streams["nav"]` > `ctx.interest_streams["nav"]`。"""
        src = self.override
        if src is None:
            src = _stream(ctx, "nav")
        want = sorted({int(a) for a in (src or [])})
        if len(want) > NAV_MAX:
            self.stats["nav_dropped"] += len(want) - NAV_MAX
            want = want[:NAV_MAX]
        return want

    def run_slow(self, ctx: Any) -> Any:
        rt = self.rt
        S = rt.S
        if S is None or rt.blk is None:
            return False
        t = int(getattr(ctx, "t_ns", 0))
        if self.next_due - t > NAV_PERIOD_NS:
            self.next_due = 0                   # 仿真时间回退：重新按 10 Hz 对齐（同 PayloadPub，d2-FX-R2-other-to-M13 第 5 条）
        if t < self.next_due:
            return False
        self.next_due = (t // NAV_PERIOD_NS + 1) * NAV_PERIOD_NS
        want = self.wanted(ctx)
        self.stats["rounds"] += 1
        if not want:
            return None
        act = S.active_idx()
        sl = act[np.isin(S.agent_no[act], np.asarray(want))]
        sl = sl[rt.blk["init"][sl] == S.agent_no[sl] + 1]
        if sl.size == 0:
            return None
        items = self.messages(sl, ctx, t)
        self.last = items
        payload = pack_items(t, items)
        if self.sink is not None:
            self.sink(payload)
            self.stats["puts"] += 1
        elif _bus_put(ctx, self, _key("STATE_NAV"), payload):
            self.stats["puts"] += 1
        self.stats["items"] += len(items)
        return None

    def messages(self, slots: np.ndarray, ctx: Any, t_ns: int) -> list[dict]:
        rt = self.rt
        S, b = rt.S, rt.blk
        slots = np.asarray(slots, np.int64)
        tick = int(getattr(ctx, "tick", 0))
        agent = S.agent_no[slots].astype(np.int64)
        PQ = S.enu.pose_enu_flu(slots)
        vel = np.asarray(S.vel_enu_view()[slots], np.float64)
        out: list[dict] = []
        imu_rows = slots[(b["has"][slots] & 8) != 0]
        imu = {}
        if imu_rows.size:
            acc, gyro, _bias = rt.imu.sample(S, imu_rows, S.agent_no[imu_rows].astype(np.int64), tick)
            imu = {int(s): (acc[i], gyro[i]) for i, s in enumerate(imu_rows.tolist())}
        gn_rows = slots[(b["has"][slots] & 4) != 0]
        wg = cbrng.normal(rt.seed, 3, S.agent_no[gn_rows].astype(np.int64), tick, np.arange(0, 3)) if gn_rows.size else None
        fix_eff: dict[int, GnssFix] = {}
        errs: dict[int, np.ndarray] = {}
        for i, s in enumerate(gn_rows.tolist()):
            f = GnssFix(int(b["gn_fix"][s]))
            p = PQ[int(np.flatnonzero(slots == s)[0]), :3]
            if f >= GnssFix.RTK_FLOAT and rtk_covered(float(p[0]), float(p[1])) is False:
                f = GnssFix.SINGLE  # 无 RTK 基站覆盖：按单点档（M13-FR-211，P1）
            fix_eff[s] = f
            rig = rt.rig_of(s)
            gn = None if rig is None else rig.by_kind(SensorKind.GNSS)
            if gn is None or f == GnssFix.NO_FIX:
                continue
            from .gnss import gnss_table

            tb = gnss_table(gn)
            z = b["gn_z"][s]
            errs[s] = np.array([tb.sigma_h[f] * z[0] + tb.white_h[f] * wg[i, 0], tb.sigma_h[f] * z[1] + tb.white_h[f] * wg[i, 1],
                                tb.sigma_v[f] * z[2] + tb.white_v[f] * wg[i, 2]])
        wv = cbrng.normal(rt.seed, 3, agent, tick, CH_VEL)
        wb = cbrng.normal(rt.seed, 3, agent, tick, CH_BARO)
        for i, s in enumerate(slots.tolist()):
            rig = rt.rig_of(s)
            uav = rt.uav_id(S, s)
            a, w = imu.get(s, (np.full(3, np.nan), np.full(3, np.nan)))
            f = fix_eff.get(s, GnssFix(int(b["gn_fix"][s])))
            gn = None if rig is None else rig.by_kind(SensorKind.GNSS)
            pos = PQ[i, :3].copy()
            if gn is not None:
                from .frames import quat_to_R

                pos = pos + quat_to_R(PQ[i:i + 1, 3:7])[0] @ np.asarray(gn.mount_t, np.float64)
            e = errs.get(s)
            p_meas = pos + (e if e is not None else np.zeros(3))
            sh, sv = VEL_SIGMA[f]
            v_meas = vel[i] + np.array([sh * wv[i, 0], sh * wv[i, 1], sv * wv[i, 2]])
            eph, epv = (float("nan"), float("nan"))
            if gn is not None:
                from .gnss import gnss_table

                tb = gnss_table(gn)
                eph, epv = float(tb.eph[int(f)]), float(tb.epv[int(f)])
            p_true = float(isa_pressure(PQ[i, 2]))
            p_b = p_true * (1.0 + 3.65e-4 * float(b["ba_z"][s])) + 2.7 * float(wb[i, 0])
            gnss: dict[str, Any] = {"pos_enu_m": [_r(x, 3) for x in p_meas] if f != GnssFix.NO_FIX else [None, None, None],
                                    "vel_mps": [_r(x, 3) for x in v_meas] if f != GnssFix.NO_FIX else [None, None, None],
                                    "fix": FIX_WIRE[f], "sats": int(b["gn_sats"][s])}
            if math.isfinite(eph):
                gnss["hacc_m"] = round(eph / math.sqrt(2.0), 4)
            if math.isfinite(epv):
                gnss["vacc_m"] = round(epv, 4)
            if f != GnssFix.NO_FIX:
                gnss["hdop"] = round(float(b["gn_hdop"][s]), 3)
            if gnss["pos_enu_m"][0] is None:  # NO_FIX：schema 要求三元数组，退化为最近真值不可得时给 0 并标 none
                gnss["pos_enu_m"] = [0.0, 0.0, 0.0]
                gnss["vel_mps"] = [0.0, 0.0, 0.0]
            out.append({"uav": uav, "t_sim_ns": int(t_ns),
                        "imu": {"f_mps2": [_r(x, 4) if math.isfinite(x) else 0.0 for x in a],
                                "w_rad_s": [_r(x, 6) if math.isfinite(x) else 0.0 for x in w]},
                        "gnss": gnss, "baro": {"alt_m": round(float(alt_from_pressure(p_b)), 3), "p_pa": round(p_b, 2)},
                        "loc_mode": self.loc_mode(s, ctx)})
        return out

    def loc_mode(self, s: int, ctx: Any) -> str:
        """定位档（M13-FR-211，P1）：机巢 RTK 覆盖时 rtk，否则装有 MID-360 的为 slam，其余 single。

        RTK 覆盖经 M08 度量注册表读取 M20 登记的度量 `tasking.rtk_cover(x_m, y_m)`（1 覆盖、0 不覆盖；不 import M20）；
        度量未登记时退回 GNSS 配置：fix 为 RTK 档即视为有基站（D1 剧本行为）。"""
        rt = self.rt
        S, b = rt.S, rt.blk
        rig = rt.rig_of(s)
        p = S.enu.pose_enu_flu(np.array([s]))[0, :3]
        cov = rtk_covered(float(p[0]), float(p[1]))
        rtk = int(b["gn_fix"][s]) >= GnssFix.RTK_FLOAT if cov is None else cov
        if rtk:
            return "rtk"
        lid = None if rig is None else rig.by_kind(SensorKind.LIDAR)
        if lid is not None and b["has"][s] & lid.bit:
            return "slam"
        return "single"


def rtk_covered(x_m: float, y_m: float) -> bool | None:
    """该点是否在带 `rtk_base` 的机巢链路覆盖内；度量未登记时为 None。"""
    try:
        from awr.sim.core import metrics as MET

        v = MET.metric(METRIC_RTK, x_m=x_m, y_m=y_m)
    except KeyError:
        return None
    except Exception:
        return None
    return bool(v >= 0.5)


def _stream(ctx: Any, name: str) -> list[int] | None:
    for attr in ("streams", "interest_streams"):
        st = getattr(ctx, attr, None)
        if st is None:
            continue
        try:
            v = st.get(name) if hasattr(st, "get") else getattr(st, name, None)
        except Exception:
            v = None
        if v is not None:
            return [int(x) for x in np.asarray(v).reshape(-1).tolist()]
    return None
