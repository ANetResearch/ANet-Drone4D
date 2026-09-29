"""CommandEngine：生产者本地准入 ④–⑩ 与调用生命周期（M08-FR-054 至 FR-061、FR-086、FR-089；M08 §6.10；AWR-17 §7.2–§7.5）。

`handle(q)` 在主循环步顶 drain inbox 时调用（zenoh 回调线程只入队），返回 Admission（bus/command.schema.json#/$defs/admission），
由调用方 `req.reply_msg(adm)`。线上状态：accepted（准入通过且同 tick 的准入矩阵确认，`native_ack = true`，effect V1）→
running（下一个 cmd_watch 周期规范态读回符合期望，V2）→ succeeded（AWR-12 §5.4 完成判据，Mock 为 V4 + `simulated`，必带
metrics）；另有 rejected、failed、canceled、timeout。生命周期以事件 `cmd.*`（data `{op, code, effect}`）发出。

准入顺序即原因码优先级（AWR-12 §5.1.2）：④ 状态（108 生命周期 → 117 时钟 → 114 锁 → 106/101/105 准入矩阵 → 104 未解锁 →
113 定位 → 登记的第 4 步检查，例如 M09 预检 103）→ ⑤ 租约（115、116、100）→ ⑥ 参数边界（110）→ ⑦ 后端能力（109）→
⑧ 围栏粗校验（登记的第 8 步检查，102；`needs_fine` 时细校验）→ ⑨ 分发（staged，apply_tick = 当前 tick + 1）→ ⑩ 审计。
`uav` 为列表或 `"*"` 时（批量，只允许 rtl、land、hover、safety_stop、pause、resume、takeoff）逐机向量化准入，回复
`per_uav{accepted, rejected}`（M08-FR-059）。

命令集：takeoff、land（here、home、{pos}）、goto、follow_path（原生 TOPP-lite PATH，或 M10 运动提供者）、orbit、hover（别名
hold）、rtl、velocity、velocity_stop、safety_stop、pause、resume、arm、disarm、cancel、kill（ext，需确认令牌）；机群增删
`fleet/add`、`fleet/remove`（委托 `fleet_ops`）；`scenario/metric`（度量注册表只读）。运动提供者（M10）经
`register_motion_provider` 接管其 `ops`（例如 `goto:route!=direct`），apply 时置 TRAJ 并调用 `provider.start()`。

幂等：cid 表保留 60 s【墙钟】、最多 4096 条，重复调用返回 `status = duplicate` 与 `call_state`。本模块不读墙钟：墙钟时刻经
注入的 SimClock（`wall_mono_ns()`）取得（ADR-045、ADR-049）。坐标：准入边界做 ENU → NED 换算（M08 §6.5.1）。
"""

from __future__ import annotations

import contextlib
import math
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from awr.contracts.enums import FLIGHTSTATE_NAMES, FlightFlags, FlightState, Lifecycle, sub_name, sub_value
from awr.contracts.reasons import Reason
from awr.contracts.reasons import info as reason_info
from awr.runtime.principal import Principal, verify_principal
from awr.world.georef.frames import enu_to_ned, yaw_ned_from_enu

from ..fleet import actions as ACT
from ..fleet import kernels_l1 as K
from ..fleet import params_px4 as P
from ..fleet.path import MAX_PATH_LEN_M, MAX_PATH_POINTS
from ..fleet.setpoint import p_stop_enu as _p_stop_enu
from ..fleet.state import CtrlMode
from . import admission as A
from . import metrics as MET
from . import state_model as SM
from .calls import ST_ACCEPTED, ST_FINAL, ST_RUNNING, Call, CallTable
from .fuser import flags_bytes
from .interfaces import CallRef, MotionProvider

__all__ = ["BATCH_OPS", "Call", "CommandEngine", "StagedCmd", "motion_providers", "register_fine_checker",
           "register_motion_provider", "reset_motion_providers"]

FS = FlightState
IDEM_TTL_NS = 60_000_000_000
IDEM_MAX = 4096
PROGRESS_MIN_NS = 500_000_000  # progress ≤ 2 Hz【墙钟】
VEHICLE_OPS = frozenset({"takeoff", "land", "goto", "follow_path", "orbit", "hover", "rtl", "velocity", "velocity_stop",
                         "safety_stop", "pause", "resume", "arm", "disarm", "cancel", "kill", "escalate"})
BATCH_OPS = frozenset({"rtl", "land", "hover", "safety_stop", "pause", "resume", "takeoff"})
NAV_OPS = frozenset({"goto", "follow_path", "orbit", "velocity"})
SAFETY_OPS = frozenset({"land", "hover", "rtl", "safety_stop"})
LANE1 = frozenset({"pause"})
INSTANT_OPS = frozenset({"cancel", "resume", "velocity_stop", "kill"})
LOCK_EXEMPT_OPS = frozenset({"land", "hover", "rtl", "safety_stop", "kill", "escalate", "resume", "cancel"})
NEED_LOC = frozenset({"takeoff", "goto", "follow_path", "orbit", "velocity"})
SEVERITY = {"accepted": 0, "running": 0, "succeeded": 0, "failed": 2, "canceled": 1, "timeout": 2, "rejected": 1}
R_SAFE_SPAWN_M = 1.5  # AWR-12 §5.14.1：出生点间距 ≥ 2√2·R_safe
_RUN_FS: dict[str, tuple[int, ...]] = {
    "takeoff": (FS.TAKING_OFF, FS.FLYING), "land": (FS.LANDING, FS.LANDED, FS.DISARMED),
    "goto": (FS.FLYING,), "follow_path": (FS.FLYING,), "orbit": (FS.FLYING,), "hover": (FS.FLYING, FS.HOLD),
    "rtl": (FS.RTL, FS.LANDING, FS.LANDED, FS.DISARMED), "velocity": (FS.FLYING,), "safety_stop": (FS.HOLD,),
    "pause": (FS.FLYING, FS.HOLD), "arm": (FS.READY, FS.TAKING_OFF), "disarm": (FS.DISARMED,),
}

_PROVIDERS: dict[str, MotionProvider] = {}
_FINE: list[Callable[..., Any]] = []


def register_motion_provider(provider: MotionProvider) -> None:
    """M10 登记接管某些命令运动的提供者（`ops` 例如 `("follow_path", "orbit", "goto:route!=direct")`）；同一 op 只允许一个。"""
    for op in provider.ops:
        if op in _PROVIDERS:
            raise ValueError(f"运动提供者重复登记：{op!r}（已由 {_PROVIDERS[op].name} 登记）")
    for m in ("start", "cancel"):
        if not callable(getattr(provider, m, None)):
            raise TypeError(f"MotionProvider 缺少方法 {m}")
    for op in provider.ops:
        _PROVIDERS[op] = provider


def motion_providers() -> dict[str, MotionProvider]:
    return dict(_PROVIDERS)


def reset_motion_providers() -> None:
    _PROVIDERS.clear()


def register_fine_checker(fn: Callable[..., Any]) -> None:
    """plan-pool（M10）登记细粒度 `path_valid` 执行者：`fn(cid, polyline_enu_m, engine)`，完成后调用
    `engine.fine_result(cid, ok, code)`（结果在下一步边界生效，FR-061）。未登记时 sim-core 在慢任务中用 M04 `path_valid` 执行。"""
    _FINE.append(fn)


def _provider_for(op: str, args: dict) -> MotionProvider | None:
    p = _PROVIDERS.get(op)
    if p is not None:
        return p
    if op == "goto" and args.get("route", "auto") != "direct":
        return _PROVIDERS.get("goto:route!=direct")
    return None


@dataclass
class StagedCmd:
    """准入通过后进入 staged 队列，由 ingest 在 apply_tick 生效（M08 §6.3.4）。"""

    cid: str
    slots: np.ndarray
    op: str
    provider: str | None
    args: dict
    source: str
    seq: int
    apply_tick: int
    request_tick: int
    rows: np.ndarray | None = None


@dataclass
class _Idem:
    wall_ns: int
    adm: dict
    calls: list[Call]

    @property
    def call(self) -> Call | None:
        """单机调用（批量时为第一条）。"""
        return self.calls[0] if self.calls else None


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


class CommandEngine:
    def __init__(self, S, profiles, roster, lease, *, events, clock, caps, entry_key: bytes | None = None,
                 audit: Callable[[dict], None] | None = None, world=None, reg=None, supervisor=None,
                 epoch_fn: Callable[[], int] = lambda: 1, segment_fn: Callable[[], int] = lambda: 0,
                 energy=None, hooks=None, PB=None, fleet_ops: Any = None, stop_motion: bool = True,
                 fallback_fsm: bool = True, replay_caps=None, inputlog: Any = None) -> None:
        self.S = S
        self.T = profiles
        self.roster = roster
        self.lease = lease
        self.events = events
        self.clock = clock
        self.caps = caps
        self.replay_caps = replay_caps
        self.entry_key = entry_key
        self.audit = audit
        self.world = world
        self.reg = reg
        self.supervisor = supervisor
        self.epoch_fn = epoch_fn
        self.segment_fn = segment_fn
        self.energy = energy
        self.hooks = hooks
        self.PB = PB
        self.fleet_ops = fleet_ops
        self.stop_motion = stop_motion
        self.fallback_fsm = fallback_fsm
        self.inputlog = inputlog
        self.ctx: Any = None  # StageCtx（组合根注入；登记的命令处理者经此取 env、world 等）
        self.active_zone_ids: list[str] | None = None  # 当前剧本的 zones.active（M10 导演设置；细校验透传给 M04）
        self.idem: OrderedDict[str, _Idem] = OrderedDict()
        self.staged: list[StagedCmd] = []
        self.table = CallTable(8192, S.capacity)
        self.fine_queue: list[tuple[str, np.ndarray]] = []
        self.fine_done: list[tuple[str, bool, int]] = []
        self._seq = 0
        self._subs: list[tuple[str, Callable[[Call], None]]] = []
        self._matrix_cache: dict[tuple, int] = {}
        self.stats = {"admitted": 0, "rejected": 0, "duplicates": 0, "succeeded": 0, "failed": 0, "canceled": 0}
        self.admission_us: list[float] = []

    # ------------------------------------------------------------ 兼容视图
    @property
    def active(self) -> dict[str, Call]:
        return {c.cid: c for c in self.table.calls()}

    # ------------------------------------------------------------ 入口
    def handle(self, q: Any) -> dict:
        """准入一条 Command（`awr.runtime.bus.Request`、带 msg() 的对象或 dict）；返回 Admission。"""
        try:
            msg = q.msg() if hasattr(q, "msg") else q
        except Exception:
            msg = None
        if not isinstance(msg, dict):
            return self._reject_raw("", int(Reason.BAD_REQUEST), detail={"field": "body"})
        cid = str(msg.get("cid") or "")
        now_w = self.clock.wall_mono_ns()
        self._expire_idem(now_w)
        ent = self.idem.get(cid)
        if ent is not None:
            self.stats["duplicates"] += 1
            return self._duplicate(ent)
        adm, calls = self._admit(msg)
        if cid:
            self.idem[cid] = _Idem(now_w, adm, calls)
            while len(self.idem) > IDEM_MAX:
                self.idem.popitem(last=False)
        if self.audit is not None:
            p = msg.get("principal") if isinstance(msg.get("principal"), dict) else {}
            self.audit({"kind": "cmd.admission", "t_sim_ns": self.S.t_ns, "principal_id": p.get("principal_id"),
                        "role": p.get("role"), "entry": p.get("entry"), "cid": cid, "uav": msg.get("uav"),
                        "code": adm["code"], "detail": {"op": msg.get("op"), "status": adm["status"],
                                                        "apply_tick": adm.get("apply_tick"),
                                                        "n_accepted": len(calls)}})
        return adm

    def submit_internal(self, cmd: dict, principal: dict) -> dict:
        """M09、M10 进程内调用（FR-089）：同一准入与生命周期；principal 由组合根构造（不验签）。"""
        m = dict(cmd)
        m.setdefault("v", 1)
        m.setdefault("args", {})
        m["principal"] = dict(principal) | {"_internal": True}
        return self.handle(m)

    def subscribe_results(self, owner: str, cb: Callable[[Call], None]) -> None:
        """在途调用终态回调（按 cid 前缀 owner 过滤；空串为全部）。"""
        self._subs.append((owner, cb))

    def schedule_fine_check(self, cid: str, polyline_enu_m: np.ndarray) -> None:
        """细粒度 `path_valid` 交 plan-pool（M10 登记的执行者；未登记时进入本引擎的慢任务队列，M04 `path_valid`）。"""
        pl = np.asarray(polyline_enu_m, np.float64).reshape(-1, 3)
        for r in self._rows_of(cid):
            self.table.fine_pending[r] = True
        if _FINE:
            for fn in _FINE:
                fn(cid, pl, self)
        else:
            self.fine_queue.append((cid, pl))

    def fine_result(self, cid: str, ok: bool, code: int = int(Reason.GEOFENCE_REJECT)) -> None:
        """细校验结果（在下一个步边界生效，FR-061）。"""
        self.fine_done.append((cid, bool(ok), int(code)))

    def run_fine_checks(self, budget_calls: int = 1) -> None:
        """慢任务：执行排队的细校验（M04 `path_valid`，每次至多 budget_calls 条）。"""
        for _ in range(min(budget_calls, len(self.fine_queue))):
            cid, pl = self.fine_queue.pop(0)
            ok, code = True, 0
            if self.world is not None and len(pl) >= 2:
                try:
                    r = self.world.path_valid(pl, buffer_m=1.0, active_zone_ids=self.active_zone_ids)
                    ok = bool(getattr(r, "ok", getattr(r, "valid", True)))
                    code = int(Reason.GEOFENCE_REJECT)
                except Exception:
                    ok = True
            self.fine_result(cid, ok, code)

    def _apply_fine_results(self) -> None:
        if not self.fine_done:
            return
        done, self.fine_done = self.fine_done, []
        for cid, ok, code in done:
            for r in self._rows_of(cid):
                self.table.fine_pending[r] = False
                if not ok:
                    call = self.table.meta[r]
                    if call is not None:
                        ACT.begin_hold(self.S, np.array([call.slot]), self.S.t_ns * 1e-9)
                        self._finish(call, "failed", code, {"status": "FAILED", "verify_trust": 2,
                                                            "observed_state": self._obs(call.slot), "message": "fine check"})

    def _rows_of(self, cid: str) -> list[int]:
        ent = self.idem.get(cid)
        if ent is None:
            return []
        return [c.row for c in ent.calls if c.row >= 0 and not c.final]

    # ------------------------------------------------------------ 准入结果
    def _base_adm(self, cid: str) -> dict:
        return {"v": 1, "cid": cid, "t_sim_ns": int(self.S.t_ns), "epoch": int(self.epoch_fn()),
                "segment": int(self.segment_fn())}

    def _reject_raw(self, cid: str, code: int, *, detail: Any = None, dup_of: str | None = None) -> dict:
        ri = reason_info(code)
        # INT-1（M09-to-M08 第 3 条）：登记检查（M09 等）在 detail.remedy 中给出的具体补救优先于原因码通用文案
        remedy = detail.get("remedy") if isinstance(detail, dict) and isinstance(detail.get("remedy"), str) and \
            detail.get("remedy") else ri.remedy_zh
        adm = self._base_adm(cid) | {"status": "rejected", "code": int(code), "reason": ri.name, "detail": detail,
                                      "remedy": remedy, "apply_tick": None}
        if dup_of is not None:
            adm["dup_of"] = dup_of
        self.stats["rejected"] += 1
        return adm

    def _reject(self, msg: dict, code: int, *, detail: Any = None, dup_of: str | None = None) -> tuple[dict, list[Call]]:
        cid = str(msg.get("cid") or "")
        adm = self._reject_raw(cid, code, detail=detail, dup_of=dup_of)
        uav = msg.get("uav") if isinstance(msg.get("uav"), str) else None
        self.events.emit("cmd.rejected", t_sim_ns=self.S.t_ns, severity=SEVERITY["rejected"], uav=uav, cid=cid or None,
                         op=str(msg.get("op")), code=int(code),
                         effect={"status": "UNAVAILABLE", "verify_trust": 0, "message": reason_info(code).message_zh})
        return adm, []

    def _duplicate(self, ent: _Idem) -> dict:
        adm = dict(ent.adm)
        adm["status"] = "duplicate"
        adm["code"] = 0
        if len(ent.calls) == 1:
            adm["call_state"] = ent.calls[0].state()
        elif ent.calls:
            adm["call_state"] = {"status": "batch", "final": all(c.final for c in ent.calls),
                                 "counts": _counts(ent.calls)}
        else:
            adm["call_state"] = {"status": ent.adm["status"], "code": ent.adm["code"], "final": True}
        adm["t_sim_ns"] = int(self.S.t_ns)
        return adm

    def _verify(self, msg: dict) -> bool:
        p = msg.get("principal")
        if not isinstance(p, dict):
            return False
        if p.get("_internal"):
            return True
        if self.entry_key is None:  # 独立运行（无 AWR_SECRET_FILE）时不验签
            return True
        sig = p.get("sig")
        if isinstance(sig, str):
            sig = sig.encode("latin-1")
        if not isinstance(sig, (bytes, bytearray)):
            return False
        try:
            pr = Principal(str(p["principal_id"]), p["role"], p["entry"], p.get("conn_id"), bool(p.get("seat", False)))
        except (KeyError, TypeError):
            return False
        return verify_principal(pr, str(msg.get("cid") or ""), bytes(sig), self.entry_key)

    # ------------------------------------------------------------ ④–⑧
    def _admit(self, msg: dict) -> tuple[dict, list[Call]]:
        cid = str(msg.get("cid") or "")
        op = str(msg.get("op") or "")
        if op == "hold":
            op = "hover"
        if msg.get("v") != 1 or not cid:
            return self._reject(msg, int(Reason.BAD_REQUEST), detail={"field": "v" if msg.get("v") != 1 else "cid"})
        if not self._verify(msg):
            return self._reject(msg, int(Reason.ROLE_FORBIDDEN), detail={"field": "principal.sig"})
        principal = msg["principal"]
        args = msg.get("args") if isinstance(msg.get("args"), dict) else {}
        if op in ("fleet/add", "fleet/remove"):
            return self._fleet_op(msg, op, args, principal)
        if op == "scenario/metric":
            return self._metric_op(msg, args)
        force_batch = op.startswith("fleet/cmd/")  # 网关批量：`fleet/cmd/<op>`，uav 为 id 列表或 "*"（17 §7.5）
        if force_batch:
            op = op[len("fleet/cmd/"):]
            if isinstance(msg.get("uav"), str) and msg.get("uav") != "*":
                msg = dict(msg, uav=[msg["uav"]])
        elif self.reg is not None and op in getattr(self.reg, "commands", {}):
            return self._plugin_op(msg, op, principal)
        if op not in VEHICLE_OPS:
            return self._reject(msg, int(Reason.BACKEND_UNSUPPORTED), detail={"op": op, "why": "UNKNOWN_OP"})
        uav = msg.get("uav")
        batch = force_batch or not isinstance(uav, str) or uav == "*"
        if batch:
            if op not in BATCH_OPS:
                return self._reject(msg, int(Reason.PARAM_OUT_OF_RANGE), detail={"op": op, "why": "BATCH_OP"})
            if uav == "*":
                entries = [self.roster.by_slot[s] for s in self.roster.slots_in_order()]
            elif isinstance(uav, list) and 1 <= len(uav) <= 1000 and all(isinstance(u, str) for u in uav):
                entries = [self.roster.resolve(u) for u in uav]
            else:
                return self._reject(msg, int(Reason.PARAM_OUT_OF_RANGE), detail={"field": "uav"})
        else:
            entries = [self.roster.resolve(uav)]
        ids = [(e.id if e is not None else (uav if not batch else None)) for e in entries]
        if not batch and entries[0] is None:
            return self._reject(msg, int(Reason.NO_VEHICLE), detail={"uav": uav})
        known = [e for e in entries if e is not None]
        unknown_ids = [u for u, e in zip(uav if isinstance(uav, list) else [], entries, strict=False) if e is None]
        slots = np.array([e.slot for e in known], np.int64)
        codes, details = self._admit_slots(op, args, principal, slots, known, cid)
        ok = codes == 0
        if not ok.any():
            if not batch:
                c = int(codes[0])
                dup = None
                if c == int(Reason.DUPLICATE):
                    cur = self.table.current(int(slots[0]))
                    dup = cur.cid if cur is not None else None
                return self._reject(msg, c, detail=details[0], dup_of=dup)
            rej = [[e.id, int(c)] for e, c in zip(known, codes, strict=True)] + [[u, int(Reason.NO_VEHICLE)] for u in unknown_ids]
            adm, _ = self._reject(msg, int(codes[0]) if codes.size else int(Reason.NO_VEHICLE), detail=None)
            adm["per_uav"] = {"accepted": [], "rejected": rej}
            return adm, []
        acc_slots = slots[ok]
        acc_entries = [e for e, o in zip(known, ok, strict=True) if o]
        adm, calls = self._dispatch(msg, cid, op, args, principal, acc_slots, acc_entries, batch)
        if batch:
            rej = [[e.id, int(c)] for e, c in zip(known, codes, strict=True) if c != 0] + \
                  [[u, int(Reason.NO_VEHICLE)] for u in unknown_ids]
            adm["per_uav"] = {"accepted": [e.id for e in acc_entries], "rejected": rej}
        _ = ids
        return adm, calls

    def _matrix(self, op: str, fs: np.ndarray, sub: np.ndarray, flags: np.ndarray, agl: np.ndarray,
                has_task: np.ndarray) -> np.ndarray:
        out = np.zeros(fs.size, np.int64)
        fsafe = (flags & int(FlightFlags.FAILSAFE)) != 0
        agl2 = agl >= 2.0
        for k in range(fs.size):
            key = (op, int(fs[k]), int(sub[k]), bool(fsafe[k]), bool(has_task[k]), bool(agl2[k]), bool(flags[k] & 1))
            c = self._matrix_cache.get(key)
            if c is None:
                name = SM.admit(op, SM.FS(int(fs[k])), int(sub[k]), int(flags[k]), has_task=bool(has_task[k]),
                                agl=3.0 if agl2[k] else 0.0)
                c = 0 if name is None else A.ADMIT_NAME_CODE.get(name, int(Reason.STATE))
                self._matrix_cache[key] = c
            out[k] = c
        return out

    def _has_task(self, op: str, slots: np.ndarray) -> np.ndarray:
        tb = self.table
        cur = tb.by_slot[slots, 0]
        live = cur >= 0
        opn = np.where(live, tb.op[np.maximum(cur, 0)], 255)
        nav = np.isin(opn, [2, 3, 4])  # goto、follow_path、orbit
        if op == "resume":
            return live & tb.paused[np.maximum(cur, 0)]
        if op == "pause":
            return live & nav & ~tb.paused[np.maximum(cur, 0)]
        return live & nav

    def _admit_slots(self, op: str, args: dict, principal: dict, slots: np.ndarray, entries: list, cid: str
                     ) -> tuple[np.ndarray, list[Any]]:
        S = self.S
        n = slots.size
        codes = np.zeros(n, np.int64)
        details: list[Any] = [None] * n
        if n == 0:
            return codes, details
        sb = S.blocks["safety"]

        def put(mask: np.ndarray, code: int, detail: Any = None) -> None:
            m = mask & (codes == 0)
            codes[m] = code
            for k in np.flatnonzero(m):
                details[k] = detail(k) if callable(detail) else detail

        # ④ 状态
        lc = S.lifecycle[slots]
        put(lc != int(Lifecycle.READY), int(Reason.LINK_ERROR), lambda k: {"lifecycle": int(lc[k])})
        # Replay 幽灵机（L0）没有 FSM，准入矩阵不适用：任何命令直接 109（M08-AC-031；与 ⑦ 同码，提前判定）
        ghost = (S.fidelity[slots] & 8) != 0
        put(ghost, int(Reason.BACKEND_UNSUPPORTED), {"op": op, "backend": "replay"})
        if op == "velocity" and self.clock.rate != 1.0:
            put(np.ones(n, bool), int(Reason.CLOCK_CONSTRAINT), {"rate": self.clock.rate})
        fs = sb["fs"][slots]
        sub = sb["sub"][slots]
        flags = flags_bytes(S, slots.astype(np.int32))
        if op not in ("cancel", "velocity_stop", "escalate"):
            mc = self._matrix(op, fs, sub, flags, S.agl[slots], self._has_task(op, slots))
            locked = sb["locked"][slots].astype(bool)
            # INT-1（M09-to-M08 第 2 条，12 §4.4.3 优先级 114 → 106 → 101 → 105）：锁定期间的非安全类命令一律 114
            if op not in LOCK_EXEMPT_OPS:
                mc = np.where(locked, int(Reason.LOCKED), mc)
            mc = np.where((mc == int(Reason.SAFETY_ACTIVE)) & locked & (op != "resume"), int(Reason.LOCKED), mc)
            for code in (int(Reason.LOCKED), int(Reason.DUPLICATE), int(Reason.SAFETY_ACTIVE), int(Reason.STATE),
                         int(Reason.BACKEND_UNSUPPORTED)):
                put(mc == code, code, lambda k: {"flight_state": FLIGHTSTATE_NAMES[int(fs[k])],
                                                 "sub": sub_name(int(fs[k]), int(sub[k]))})
        elif op == "velocity_stop":
            cur = self.table.by_slot[slots, 0]
            ok = (cur >= 0) & (self.table.op[np.maximum(cur, 0)] == 7)
            put(~ok, int(Reason.STATE), {"why": "NO_VELOCITY_SESSION"})
        if op == "takeoff" and args.get("auto_arm", True) is False:
            put(fs == int(FS.DISARMED), int(Reason.NOT_ARMED))
        if op in NEED_LOC:
            put(~sb["flag_loc_ok"][slots].astype(bool), int(Reason.LOC_NOT_READY))
        if op == "cancel":
            tgt = self.idem.get(str(args.get("call_id") or ""))
            tc = [c for c in (tgt.calls if tgt else []) if not c.final]
            for k in range(n):
                if not any(c.slot == int(slots[k]) for c in tc):
                    put(np.arange(n) == k, int(Reason.STATE), {"call_id": args.get("call_id")})
        if self.reg is not None and self.reg.admission_checks(4):
            for k in np.flatnonzero(codes == 0):
                s = int(slots[k])
                req = A.AdmitReq(cid, op, s, entries[k].id, args, principal, int(fs[k]), int(sub[k]), int(flags[k]))
                r = A.run_registered(self.reg.admission_checks(4), req, A.AdmitCtx(S.tick, S.t_ns, S, self.world, self.T))
                if r.code:
                    codes[k] = r.code
                    details[k] = r.detail
        # ⑤ 租约
        for k in np.flatnonzero(codes == 0):
            s = int(slots[k])
            c = self.lease.check(s, op, principal, uav=entries[k].id, t_ns=S.t_ns, emit=self.events.emit, commit=False)
            if c:
                codes[k] = c
                details[k] = {"lease": self.lease.lease_json(s)}
        # ⑥ 参数边界
        if (codes == 0).any():
            bad = self._check_params(op, args, slots[codes == 0])
            if bad is not None:
                put(np.ones(n, bool), int(Reason.PARAM_OUT_OF_RANGE), bad)
        # ⑦ 后端能力
        if op in A.MOTION_OPS and self.caps.cmd_impl(op) == "none":
            put(np.ones(n, bool), int(Reason.BACKEND_UNSUPPORTED), {"op": op, "backend": self.caps.backend})
        if op in ("arm", "disarm") and args.get("force"):
            put(np.ones(n, bool), int(Reason.BACKEND_UNSUPPORTED), {"field": "args.force"})
        if op == "escalate":
            put(np.ones(n, bool), int(Reason.BACKEND_UNSUPPORTED), {"op": op, "why": "D1_EXT"})
        if op == "kill" and not args.get("confirm_token"):
            put(np.ones(n, bool), int(Reason.CONFIRM_REQUIRED))
        # ⑧ 围栏粗校验（M09 登记）
        self._fine_polys: dict[int, np.ndarray] = {}
        if self.reg is not None and self.reg.admission_checks(8):
            for k in np.flatnonzero(codes == 0):
                s = int(slots[k])
                req = A.AdmitReq(cid, op, s, entries[k].id, args, principal, int(fs[k]), int(sub[k]), int(flags[k]),
                                 polyline_enu_m=self._polyline(op, args, s))
                r = A.run_registered(self.reg.admission_checks(8), req, A.AdmitCtx(S.tick, S.t_ns, S, self.world, self.T))
                if r.code:
                    codes[k] = r.code
                    details[k] = r.detail
                elif r.needs_fine and req.polyline_enu_m is not None:
                    self._fine_polys[s] = req.polyline_enu_m
        return codes, details

    def _polyline(self, op: str, args: dict, slot: int) -> np.ndarray | None:
        """⑧ 围栏粗校验折线（ENU）：goto `[p, p_stop, goal]`、follow_path 航点、orbit 入圈与圆周采样、rtl 返航折线。"""
        S = self.S
        p = S.enu.pos[slot].copy()
        if op == "goto" and A.finite_vec(args.get("pos"), 3):
            ps = _p_stop_enu(S, self.T.LT, np.array([slot]))[0]
            return np.array([p, ps, args["pos"]], np.float64)
        if op == "follow_path" and isinstance(args.get("waypoints"), list):
            try:
                return np.vstack([p[None], np.asarray(args["waypoints"], np.float64)])
            except ValueError:
                return None
        if op == "orbit" and A.finite_vec(args.get("center"), 3) and _num(args.get("radius_m")):
            c = np.asarray(args["center"], np.float64)
            r = float(args["radius_m"])
            th = np.linspace(0, 2 * np.pi, 17)
            ring = np.stack([c[0] + r * np.cos(th), c[1] + r * np.sin(th), np.full(17, c[2])], 1)
            return np.vstack([p[None], ring])
        if op == "rtl":
            h = S.enu.home[slot]
            z = max(p[2], h[2] + P.RTL_RETURN_ALT)
            return np.array([p, [p[0], p[1], z], [h[0], h[1], z], h], np.float64)
        return None

    def _check_params(self, op: str, args: dict, slots: np.ndarray) -> dict | None:
        """⑥ 参数边界（AWR-12 §5.3；commands.json args_schema）。"""
        if op == "goto":
            if not A.finite_vec(args.get("pos"), 3):
                return {"field": "args.pos"}
            sp = args.get("speed_mps")
            if sp is not None and (not _num(sp) or not 0 < sp <= 12):
                return {"field": "args.speed_mps", "value": sp, "range": [0, 12]}
            tol = args.get("tol_m", 0.5)
            if not _num(tol) or not 0.2 <= tol <= 10:
                return {"field": "args.tol_m", "value": tol, "range": [0.2, 10]}
            y = args.get("yaw_rad")
            if y is not None and (not _num(y) or not -math.pi <= y <= math.pi):
                return {"field": "args.yaw_rad", "value": y, "range": [-math.pi, math.pi]}
            if args.get("route", "auto") not in ("auto", "direct", "safe_transit"):
                return {"field": "args.route"}
            b = self._bounds_bad(args["pos"])
            if b is not None:
                return b
        elif op == "takeoff":
            alt = args.get("alt_m", P.MIS_TAKEOFF_ALT)
            if not _num(alt) or not 0.5 <= alt <= 120:
                return {"field": "args.alt_m", "value": alt, "range": [0.5, 120], "why": "ALT_RANGE"}
        elif op == "rtl":
            alt = args.get("alt_m")
            if alt is not None and (not _num(alt) or not 10 <= alt <= 500):
                return {"field": "args.alt_m", "value": alt, "range": [10, 500], "why": "ALT_RANGE"}
        elif op == "land":
            at = args.get("at", "here")
            if not (at in ("here", "home") or (isinstance(at, dict) and A.finite_vec(at.get("pos"), 3))):
                return {"field": "args.at"}
        elif op == "follow_path":
            wp = args.get("waypoints")
            if not isinstance(wp, list) or not 2 <= len(wp) <= MAX_PATH_POINTS or not all(A.finite_vec(w, 3) for w in wp):
                n = len(wp) if isinstance(wp, list) else None
                return {"field": "args.waypoints", "value": n, "range": [2, MAX_PATH_POINTS], "why": "PATH_TOO_LONG"}
            w = np.asarray(wp, np.float64)
            if float(np.linalg.norm(np.diff(w, axis=0), axis=1).sum()) > MAX_PATH_LEN_M:
                return {"field": "args.waypoints", "why": "PATH_TOO_LONG", "range": [0, MAX_PATH_LEN_M]}
            sp = args.get("speed_mps")
            if sp is not None and (not _num(sp) or not 0 < sp <= 12):
                return {"field": "args.speed_mps", "value": sp, "range": [0, 12]}
            if "bspline" in args:
                bs = args["bspline"]
                if not isinstance(bs, dict) or bs.get("order") != 3 or not _num(bs.get("ts_s")) or bs["ts_s"] <= 0 or \
                        not isinstance(bs.get("ctrl_pts"), list) or len(bs["ctrl_pts"]) < 4:
                    return {"field": "args.bspline"}
            if self.PB is not None and self.PB.free_rows() < self.PB.rows_for(len(wp) + 1) and _provider_for(op, args) is None:
                return {"field": "args.waypoints", "why": "PATH_BUFFER_FULL"}
        elif op == "orbit":
            if not A.finite_vec(args.get("center"), 3):
                return {"field": "args.center"}
            r = args.get("radius_m")
            if not _num(r) or not 1 <= r <= 1000:
                return {"field": "args.radius_m", "value": r, "range": [1, 1000], "why": "ORBIT_RADIUS"}
            sp = args.get("speed_mps")
            if sp is not None and (not _num(sp) or not 0 < sp <= 12):
                return {"field": "args.speed_mps", "value": sp, "range": [0, 12]}
            if sp is not None and sp * sp / r > P.MPC_ACC_HOR + 1e-9:
                return {"field": "args.speed_mps", "value": sp, "why": "ORBIT_RADIUS",
                        "range": [0, round(math.sqrt(P.MPC_ACC_HOR * r), 3)]}
            tr = args.get("turns", 0)
            if not _num(tr) or not 0 <= tr <= 100:
                return {"field": "args.turns", "value": tr, "range": [0, 100]}
            if args.get("yaw_behavior", "center") not in ("center", "tangent", "fixed"):
                return {"field": "args.yaw_behavior"}
            b = self._bounds_bad(args["center"])
            if b is not None:
                return b
        elif op == "velocity":
            if args.get("frame", "world") not in ("world", "body"):
                return {"field": "args.frame"}
            vm = args.get("vmax_mps")
            if vm is not None:
                lim = float(np.min(self.T.LT[self.S.limits_id[slots], K.L_VXY])) if slots.size else 12.0
                if not _num(vm) or vm <= 0 or vm > lim + 1e-9:
                    return {"field": "args.vmax_mps", "value": vm, "range": [0, lim]}
        elif op == "cancel":
            if not isinstance(args.get("call_id"), str) or not args["call_id"]:
                return {"field": "args.call_id"}
        return None

    def _bounds_bad(self, pos) -> dict | None:
        if self.world is None:
            return None
        lo, hi = self.world.bounds_m
        x, y = pos[0], pos[1]
        if not (lo[0] <= x <= hi[0] and lo[1] <= y <= hi[1]):
            return {"field": "args.pos", "value": list(pos), "range": [list(map(float, lo[:2])), list(map(float, hi[:2]))]}
        return None

    # ------------------------------------------------------------ ⑨ 分发
    def _source(self, p: dict) -> str:
        role = str(p.get("role", "internal"))
        if role in ("operator", "admin"):
            return "OPERATOR"
        return str(p.get("source") or role).upper()

    def _dispatch(self, msg: dict, cid: str, op: str, args: dict, p: dict, slots: np.ndarray, entries: list,
                  batch: bool) -> tuple[dict, list[Call]]:
        S = self.S
        now_w = self.clock.wall_mono_ns()
        source = self._source(p)
        prov = _provider_for(op, args)
        lane = 1 if op in LANE1 else 0
        warnings: list[str] = []
        args_n = dict(args)
        if op in ("goto", "follow_path", "orbit") and _num(args.get("speed_mps")):
            lim = S.limits_id[slots]
            vmax = self.T.LT[lim, K.L_VXY]
            if (float(args["speed_mps"]) > vmax + 1e-9).any():
                warnings.append("SPEED_CLAMPED")
        calls: list[Call] = []
        self._seq += 1
        apply_tick = S.tick + 1
        rows = np.zeros(slots.size, np.int32)
        pid = p.get("principal_id")
        for k, (s, e) in enumerate(zip(slots, entries, strict=True)):
            s = int(s)
            # ⑤ 的隐式 acquire 在全部检查通过后才提交（被拒命令不改变租约）
            self.lease.check(s, op, p, uav=e.id, t_ns=S.t_ns, emit=self.events.emit, commit=True)
            if op not in INSTANT_OPS and op != "cancel":
                old = self.table.current(s, lane)
                if old is not None and not old.final:
                    self._cancel_provider(old)
                    self._finish(old, "canceled", int(Reason.SUPERSEDED),
                                 {"status": "UNVERIFIED", "verify_trust": 2 if old.status == "running" else 1,
                                  "message": "superseded"})
                if lane == 0 and op in SAFETY_OPS:
                    old1 = self.table.current(s, 1)
                    if old1 is not None and not old1.final:
                        self._finish(old1, "canceled", int(Reason.SUPERSEDED),
                                     {"status": "UNVERIFIED", "verify_trust": 1, "message": "superseded"})
            call = Call(cid if not batch else f"{cid}:{e.id}", op, s, e.id, pid, p.get("role"), source, args_n, S.t_ns,
                        now_w, lane=lane, batch_id=cid if batch else msg.get("batch_id"),
                        provider=getattr(prov, "name", None), warnings=list(warnings))
            call.effect = {"status": "UNVERIFIED", "verify_trust": 1, "native_ack": True, "protocol": "inproc",
                           "requested": self._requested(op, args)}
            r = self.table.alloc(call, t_ns=S.t_ns, deadline_ns=S.t_ns + self._deadline_ns(op, args, s))
            self._init_row(r, op, args, s)
            rows[k] = r
            calls.append(call)
            # goto(route != direct) 由 M10 提供者接管：它以 safe_transit 规划绕障航线并自行请求细校验（或粗校验已证明
            # 直线安全），第⑧步对直线 [p, p_stop, goal] 的细校验不适用（INT-1：起飞后点选 GoTo 被误判 102）
            if s in getattr(self, "_fine_polys", {}) and not (op == "goto" and prov is not None):
                self.table.fine_pending[r] = True
                if _FINE:
                    for fn in _FINE:
                        fn(call.cid, self._fine_polys[s], self)
                else:
                    self.fine_queue.append((call.cid, self._fine_polys[s]))
        self.staged.append(StagedCmd(cid, slots.astype(np.int32), op, getattr(prov, "name", None), args_n, source,
                                     self._seq, apply_tick, S.tick, rows))
        if self.inputlog is not None:
            self.inputlog.append("cmd", apply_tick, msg)
        self.stats["admitted"] += len(calls)
        for c in calls:
            eff = dict(c.effect)
            if c.warnings:
                self._event("cmd.accepted", c, 0, eff, warnings=c.warnings)
            else:
                self._event("cmd.accepted", c, 0, eff)
        adm = self._base_adm(cid) | {"status": "accepted", "code": 0, "reason": None,
                                      "detail": {"warnings": warnings} if warnings else None, "remedy": None,
                                      "apply_tick": apply_tick}
        return adm, calls

    def _init_row(self, r: int, op: str, args: dict, slot: int) -> None:
        tb, S = self.table, self.S
        if op == "goto":
            tb.goal[r] = np.asarray(args["pos"], np.float64)  # 调用表内一律 ENU（M08-NFR-019）
            tb.tol[r] = float(args.get("tol_m", 0.5))
        elif op == "takeoff":
            tb.alt[r] = float(args.get("alt_m", P.MIS_TAKEOFF_ALT))
        elif op == "follow_path":
            w = np.asarray(args["waypoints"], np.float64)
            tb.goal[r] = w[-1]
            tb.aux[r, 0] = float(np.linalg.norm(np.diff(np.vstack([S.enu.pos[slot], w]), axis=0), axis=1).sum())
        elif op == "orbit":
            tb.goal[r] = np.asarray(args["center"], np.float64)
            lim = int(S.limits_id[slot])
            v = min(float(args.get("speed_mps") or self.T.LT[lim, K.L_CRUISE]), float(self.T.LT[lim, K.L_VXY]))
            R = float(args["radius_m"])
            tb.aux[r] = (R, min(v, math.sqrt(float(self.T.LT[lim, K.L_ACC]) * R)), float(args.get("turns", 0) or 0), 0.0)
        tb.stall_ref[r] = S.enu.pos[slot]

    def _requested(self, op: str, args: dict) -> str:
        if op == "goto":
            x, y, z = args["pos"]
            sp = args.get("speed_mps")
            return f"goto enu=({x:g},{y:g},{z:g})" + (f" v={sp:g}" if sp is not None else "")
        if op == "takeoff":
            return f"takeoff alt={args.get('alt_m', P.MIS_TAKEOFF_ALT):g}"
        if op == "cancel":
            return f"cancel {args.get('call_id')}"
        if op == "orbit":
            return f"orbit r={args.get('radius_m')} turns={args.get('turns', 0)}"
        if op == "follow_path":
            return f"follow_path n={len(args.get('waypoints') or [])}"
        return op

    def _deadline_ns(self, op: str, args: dict, slot: int) -> int:
        """完成截止时间【仿真】（AWR-12 §5.4）。"""
        S = self.S
        lim = int(S.limits_id[slot])
        cruise, vmax, acc = (float(self.T.LT[lim, K.L_CRUISE]), float(self.T.LT[lim, K.L_VXY]),
                             float(self.T.LT[lim, K.L_ACC]))
        if op == "takeoff":
            alt = float(args.get("alt_m", P.MIS_TAKEOFF_ALT))
            return int((alt / 1.5 * 1.5 + 10.0 + 2.0 + P.COM_SPOOLUP_TIME + P.MPC_TKO_RAMP_T) * 1e9)
        if op == "goto":
            p = S.enu.pos[slot]
            d = float(np.linalg.norm(np.asarray(args["pos"], np.float64) - p))
            v = min(float(args.get("speed_mps") or cruise), vmax)
            vz = abs(float(args["pos"][2]) - float(p[2]))
            eta = max(d / max(v, 0.1) + v / acc, vz / P.MPC_Z_V_AUTO_DN)
            return int((1.5 * eta + 10.0) * 1e9)
        if op == "follow_path":
            w = np.vstack([S.enu.pos[slot], np.asarray(args["waypoints"], np.float64)])
            L = float(np.linalg.norm(np.diff(w, axis=0), axis=1).sum())
            v = min(float(args.get("speed_mps") or cruise), vmax)
            n = len(w)
            eta = L / max(v, 0.1) + n * (v / acc + 2.0)
            return int((1.5 * eta + 10.0) * 1e9)
        if op == "orbit":
            c = np.asarray(args["center"], np.float64)
            R = float(args["radius_m"])
            v = min(float(args.get("speed_mps") or cruise), vmax, math.sqrt(acc * R))
            p = S.enu.pos[slot]
            d_in = abs(float(np.linalg.norm(p[:2] - c[:2])) - R) + abs(float(p[2] - c[2]))
            t_in = 1.5 * (d_in / max(min(v, cruise), 0.1) + v / acc) + 10.0
            turns = float(args.get("turns", 0) or 0)
            t_orb = 1.5 * (2 * math.pi * R * turns / max(v, 0.1) + v / (acc / R) / R) + 5.0 if turns > 0 else 3.0 + 10.0
            return int((t_in + t_orb) * 1e9)
        if op == "land":
            alt = max(float(S.agl[slot]), 0.0)
            return int((alt / 0.7 * 1.5 + 10.0 + P.COM_DISARM_LAND + 2.0) * 1e9)
        if op == "rtl":
            return int((1.5 * self._t_rtl(slot, args) + 20.0) * 1e9)
        if op in ("hover", "pause"):
            return int(10e9)
        if op == "safety_stop":
            return int(5e9 + 10e9 * float(np.linalg.norm(S.enu.vel[slot])) / max(acc, 0.1))
        if op in ("arm", "disarm"):
            return int(5e9)
        if op == "velocity":
            return int(1e18)
        return int(10e9)

    def _t_rtl(self, slot: int, args: dict) -> float:
        S = self.S
        if self.energy is not None:
            try:
                return float(self.energy.rtl_plan(slot).t_rtl_s)
            except Exception:
                pass
        home = S.enu.home[slot]
        p = S.enu.pos[slot]
        dxy = float(np.linalg.norm(p[:2] - home[:2]))
        z_now = float(p[2])
        z_home = float(home[2])
        alt = args.get("alt_m")
        z_rtl = max(z_now, z_home + (float(alt) if alt is not None else P.RTL_RETURN_ALT))
        lim = int(S.limits_id[slot])
        v_c = max(1.0, min(5.0, float(self.T.LT[lim, K.L_VXY])))
        return dxy / v_c + max(0.0, z_rtl - z_now) / 2.0 + max(0.0, z_rtl - z_home - 10.0) / 1.5 + 10.0 / 0.7 + 5.0

    # ------------------------------------------------------------ ⑨ apply（ingest，apply_tick）
    def apply_staged(self, ctx) -> None:
        self._apply_fine_results()
        if not self.staged:
            return
        due = [c for c in self.staged if c.apply_tick <= ctx.tick]
        if not due:
            return
        self.staged = [c for c in self.staged if c.apply_tick > ctx.tick]
        for sc in sorted(due, key=lambda c: c.seq):
            self._apply_one(sc, ctx)

    def _apply_one(self, sc: StagedCmd, ctx) -> None:
        S, tb = self.S, self.table
        rows = sc.rows if sc.rows is not None else np.zeros(0, np.int32)
        live = [(int(s), int(r)) for s, r in zip(sc.slots, rows, strict=True)
                if tb.live[r] and tb.meta[r] is not None and not tb.meta[r].final]
        if not live:
            return
        keep: list[tuple[int, int]] = []
        for s, r in live:
            call = tb.meta[r]
            if not S.active[s]:
                self._finish(call, "failed", int(Reason.VEHICLE_LOST), {"status": "FAILED", "verify_trust": 2})
                continue
            keep.append((s, r))
        if not keep:
            return
        slots = np.array([s for s, _ in keep], np.int64)
        rws = [r for _, r in keep]
        # apply 时复核（FR-060）：状态已升级为禁止该命令时 failed 204，不改变运动模式
        if sc.op not in ("cancel", "velocity_stop", "resume"):
            sb = S.blocks["safety"]
            fs, sub = sb["fs"][slots], sb["sub"][slots]
            flags = flags_bytes(S, slots.astype(np.int32))
            mc = self._matrix(sc.op, fs, sub, flags, S.agl[slots], self._has_task(sc.op, slots) | (sc.op == "pause"))
            bad = (mc == int(Reason.SAFETY_ACTIVE)) | (mc == int(Reason.LOCKED))
            if self.hooks is not None and hasattr(self.hooks, "matrix_verdict"):
                try:
                    hv = np.asarray(self.hooks.matrix_verdict(slots.astype(np.int32), sc.op))
                    bad |= hv != 0
                except Exception:
                    pass
            for k in np.flatnonzero(bad):
                call = tb.meta[rws[k]]
                self._finish(call, "failed", int(Reason.PREEMPTED_BY_SAFETY),
                             {"status": "FAILED", "verify_trust": 2, "observed_state": self._obs(int(slots[k]))})
            ok = ~bad
            slots = slots[ok]
            rws = [r for r, o in zip(rws, ok, strict=True) if o]
            if slots.size == 0:
                return
        t_s = ctx.t_ns * 1e-9
        for r in rws:
            tb.applied[r] = True
            tb.meta[r].applied = True
        if self.hooks is not None and sc.op in SAFETY_OPS:
            for s, r in zip(slots, rws, strict=True):
                with contextlib.suppress(Exception):
                    self.hooks.apply_operator(int(s), tb.meta[r], ctx.t_ns)
        self._execute(sc, slots, rws, t_s, ctx)
        S.touch()

    def _execute(self, sc: StagedCmd, slots: np.ndarray, rws: list[int], t_s: float, ctx) -> None:
        S, tb, op, args = self.S, self.table, sc.op, sc.args
        if sc.provider is not None:
            prov = _PROVIDERS.get(op) or _PROVIDERS.get("goto:route!=direct")
            if prov is not None:
                ACT.release_path(S, self.PB, slots)
                ACT.begin_traj(S, slots, t_s)
                for s, r in zip(slots, rws, strict=True):
                    c = tb.meta[r]
                    prov.start(CallRef(c.cid, op, c.source), np.array([s], np.int32), args, sc.apply_tick)
                return
        if op == "takeoff":
            ACT.begin_takeoff(S, slots, tb.alt[rws], t_s)
        elif op == "goto":
            sp = args.get("speed_mps")
            lim = S.limits_id[slots]
            speed = np.nan if sp is None else np.minimum(float(sp), self.T.LT[lim, K.L_VXY])
            yaw = None if args.get("yaw_rad") is None else float(yaw_ned_from_enu(float(args["yaw_rad"])))
            ACT.begin_goto(S, slots, enu_to_ned(tb.goal[rws]), speed, yaw, t_s, stop_motion=self.stop_motion)
        elif op == "follow_path":
            w = enu_to_ned(np.asarray(args["waypoints"], np.float64))
            sp = args.get("speed_mps")
            for s, r in zip(slots, rws, strict=True):
                T = ACT.begin_path(S, self.PB, self.T, int(s), w, sp if sp is not None else float("nan"), None, t_s)
                if T is None:
                    self._finish(tb.meta[r], "failed", int(Reason.PARAM_OUT_OF_RANGE),
                                 {"status": "FAILED", "verify_trust": 2, "message": "PATH_BUFFER_FULL"})
                else:
                    tb.deadline[r] = max(tb.deadline[r], S.t_ns + int((1.5 * T + 10.0) * 1e9))
        elif op == "orbit":
            c = enu_to_ned(np.asarray(args["center"], np.float64))
            yb = args.get("yaw_behavior", "center")
            yf = None  # fixed：保持当前偏航设定（ACT.begin_orbit 读取 yaw_sp）
            for s, r in zip(slots, rws, strict=True):
                ACT.begin_orbit(S, np.array([s]), c, float(args["radius_m"]), float(tb.aux[r, 1]),
                                bool(args.get("cw", True)), float(args.get("turns", 0) or 0), yb, yf, t_s)
        elif op in ("hover", "safety_stop"):
            ACT.release_path(S, self.PB, slots)
            ACT.begin_hold(S, slots, t_s)
            if op == "safety_stop" and self.fallback_fsm:
                S.blocks["safety"]["locked"][slots] = True
            if op == "safety_stop":
                self.lease.suspend(slots)
        elif op == "land":
            at = args.get("at", "here")
            if at == "home":
                ACT.begin_land_home(S, slots, t_s)
            else:
                xy = enu_to_ned(np.asarray(at["pos"], np.float64))[:2] if isinstance(at, dict) else None
                ACT.begin_land(S, slots, xy, t_s)
        elif op == "rtl":
            z, v = self._rtl_params(slots, args)
            ACT.begin_rtl(S, slots, z, v, t_s)
        elif op == "velocity":
            vm = args.get("vmax_mps")
            ACT.begin_velocity(S, slots, args.get("frame", "world") == "body", float(vm) if _num(vm) else math.inf,
                               bool(args.get("hold_alt", True)), t_s)
            S.sp_wall_ns[slots] = self.clock.wall_mono_ns()
            self._sp_paused = getattr(self, "_sp_paused", {})
            for s in slots:
                self._sp_paused[int(s)] = self.clock.paused_total_ns()
        elif op == "velocity_stop":
            for s, r in zip(slots, rws, strict=True):
                cur = tb.current(int(s), 0)
                if cur is not None and cur.op == "velocity" and not cur.final:
                    tb.stop_seen[cur.row] = True
                    tb.hold_since[cur.row] = -1
                S.vel_sess[s] = False
                ACT.begin_hold(S, np.array([s]), t_s)
                self._succeed_now(tb.meta[r])
        elif op == "pause":
            for s in slots:
                nav = tb.current(int(s), 0)
                if nav is not None and not nav.final:
                    tb.paused[nav.row] = True
                    self._cancel_provider(nav)
            ACT.begin_hold(S, slots, t_s)
        elif op == "resume":
            for s, r in zip(slots, rws, strict=True):
                s = int(s)
                sb = S.blocks["safety"]
                if sb["locked"][s]:
                    if self.fallback_fsm:
                        sb["locked"][s] = False
                    self.lease.resume(np.array([s]))
                if self.fallback_fsm and "hold_reason" in sb:
                    sb["hold_reason"][s] = 0
                nav = tb.current(s, 0)
                if nav is not None and not nav.final and tb.paused[nav.row]:
                    tb.paused[nav.row] = False
                    self._resume_nav(nav, t_s)
                pz = tb.current(s, 1)
                if pz is not None and not pz.final:
                    self._succeed_now(pz)
                call = tb.meta[r]  # INT-1（M09-to-M08 第 1 条）：_succeed_now 归还行并清空 meta，先取出调用再交给 M09
                self._succeed_now(call)
                if self.hooks is not None:
                    with contextlib.suppress(Exception):
                        self.hooks.apply_operator(s, call, ctx.t_ns)
        elif op == "arm":
            ACT.arm(S, slots, t_s)
        elif op == "disarm":
            ACT.disarm(S, slots, t_s)
        elif op == "kill":
            ACT.release_path(S, self.PB, slots)
            ACT.begin_kill(S, slots, t_s)
            for r in rws:
                self._succeed_now(tb.meta[r])
        elif op == "cancel":
            target = self.idem.get(str(args.get("call_id")))
            for s, r in zip(slots, rws, strict=True):
                ACT.begin_hold(S, np.array([s]), t_s)
                for tc in (target.calls if target else []):
                    if tc.slot == int(s) and not tc.final:
                        self._cancel_provider(tc)
                        self._finish(tc, "canceled", int(Reason.CANCELLED),
                                     {"status": "UNVERIFIED", "verify_trust": 2 if tc.status == "running" else 1,
                                      "message": "canceled"})
                self._succeed_now(tb.meta[r])

    def _rtl_params(self, slots: np.ndarray, args: dict) -> tuple[np.ndarray, np.ndarray]:
        S = self.S
        z_now = S.enu.pos[slots, 2].copy()
        z_home = S.enu.home[slots, 2].copy()
        alt = args.get("alt_m")
        z = np.maximum(z_now, z_home + (float(alt) if alt is not None else P.RTL_RETURN_ALT))
        lim = S.limits_id[slots]
        v = np.maximum(1.0, np.minimum(5.0, self.T.LT[lim, K.L_VXY]))
        if self.energy is not None:
            for k, s in enumerate(slots):
                try:
                    plan = self.energy.rtl_plan(int(s))
                    z[k] = max(z[k], float(plan.z_rtl_m))
                    v[k] = float(plan.v_c_mps)
                except Exception:
                    continue
        return z, v

    def _resume_nav(self, call: Call, t_s: float) -> None:
        S, tb = self.S, self.table
        s = call.slot
        args = call.args
        if call.provider is not None:
            prov = _PROVIDERS.get(call.op) or _PROVIDERS.get("goto:route!=direct")
            if prov is not None:
                ACT.begin_traj(S, np.array([s]), t_s)
                prov.start(CallRef(call.cid, call.op, call.source), np.array([s], np.int32), args, S.tick)
                return
        if call.op == "goto":
            sp = args.get("speed_mps")
            ACT.begin_goto(S, np.array([s]), enu_to_ned(tb.goal[call.row]), np.nan if sp is None else float(sp), None, t_s)
        elif call.op == "orbit":
            ACT.begin_orbit(S, np.array([s]), enu_to_ned(tb.goal[call.row]), float(tb.aux[call.row, 0]), float(tb.aux[call.row, 1]),
                            bool(args.get("cw", True)), float(args.get("turns", 0) or 0),
                            args.get("yaw_behavior", "center"), None, t_s)
        elif call.op == "follow_path":
            w = np.asarray(args["waypoints"], np.float64)
            p = S.enu.pos[s]
            k = int(np.argmin(np.linalg.norm(w - p, axis=1)))
            rem = w[k:] if np.linalg.norm(w[k] - p) > 0.5 else w[min(k + 1, len(w) - 1):]
            if len(rem) == 0:
                rem = w[-1:]
            ACT.begin_path(S, self.PB, self.T, s, enu_to_ned(rem), args.get("speed_mps") or float("nan"), None, t_s)

    # ------------------------------------------------------------ cmd_watch（50 Hz，AWR-12 §5.4，向量化）
    def watch_tick(self, S, ctx) -> None:
        tb = self.table
        rows = tb.rows()
        if rows.size == 0:
            return
        t = int(S.t_ns)
        now_w = self.clock.wall_mono_ns()
        sb = S.blocks["safety"]
        slots = tb.slot[rows]
        valid = slots >= 0
        rows, slots = rows[valid], slots[valid]
        if rows.size == 0:
            return
        applied = tb.applied[rows]
        # 未 apply：只判截止
        na = rows[~applied & (t > tb.deadline[rows])]
        for r in na:
            self._finish(tb.meta[r], "failed", int(Reason.PROGRESS_TIMEOUT), {"status": "FAILED", "verify_trust": 2})
        rows, slots = rows[applied], slots[applied]
        if rows.size == 0:
            return
        fs = sb["fs"][slots]
        sub = sb["sub"][slots]
        gone = ~S.active[slots] | (S.lifecycle[slots] == int(Lifecycle.LOST))
        crashed = fs == int(FS.CRASHED)
        for r, s, g, c in zip(rows[gone | crashed], slots[gone | crashed], gone[gone | crashed], crashed[gone | crashed],
                              strict=True):
            if g:
                self._finish(tb.meta[r], "failed", int(Reason.VEHICLE_LOST), {"status": "FAILED", "verify_trust": 2})
            elif c:
                self._finish(tb.meta[r], "failed", int(Reason.CRASHED),
                             {"status": "FAILED", "verify_trust": 2, "observed_state": self._obs(int(s))})
        keep = ~(gone | crashed)
        rows, slots, fs, sub = rows[keep], slots[keep], fs[keep], sub[keep]
        if rows.size == 0:
            return
        opc = tb.op[rows]
        # 严重度升级（ELAND、FAILSAFE）→ 204（M09 登记时由其 resolve_calls 给出，先到者为准）
        esc = np.isin(fs, (int(FS.ELAND), int(FS.FAILSAFE)))
        for r, s in zip(rows[esc], slots[esc], strict=True):
            self._finish(tb.meta[r], "failed", int(Reason.PREEMPTED_BY_SAFETY),
                         {"status": "FAILED", "verify_trust": 2, "observed_state": self._obs(int(s))})
        rows, slots, fs, sub, opc = rows[~esc], slots[~esc], fs[~esc], sub[~esc], opc[~esc]
        # running 读回
        acc = (tb.status[rows] == ST_ACCEPTED) & ~tb.fine_pending[rows]
        for r, s, f in zip(rows[acc], slots[acc], fs[acc], strict=True):
            call = tb.meta[r]
            if int(f) in _RUN_FS.get(call.op, ()):
                tb.status[r] = ST_RUNNING
                tb.t_running[r] = t
                tb.stall_ref[r] = S.enu.pos[s]
                tb.stall_t[r] = t
                call.status = "running"
                call.effect = {"status": "UNVERIFIED", "verify_trust": 2, "observed_state": self._obs(int(s))}
                self._event("cmd.running", call, 0, call.effect)
        run = tb.status[rows] == ST_RUNNING
        r_rows, r_slots, r_fs = rows[run], slots[run], fs[run]
        if r_rows.size:
            self._done_vector(r_rows, r_slots, r_fs, t)
        # 停滞 203（导航与返航巡航段 5 s 内前进 < 0.2 m）与截止 202
        for r, s in zip(rows, slots, strict=True):
            call = tb.meta[r]
            if call is None or call.final:
                continue
            if tb.paused[r]:
                tb.deadline[r] += int(ctx.dt_tick * 5 * 1e9) if ctx is not None else 0
                continue
            if tb.status[r] == ST_RUNNING and call.op in ("goto", "follow_path", "orbit"):
                # 停滞：自上次前进 ≥ 0.2 m 起 5 s【仿真】内未再前进 0.2 m（滑动判据）
                pe = S.enu.pos[s]
                if float(np.linalg.norm(pe - tb.stall_ref[r])) >= 0.2:
                    tb.stall_ref[r] = pe
                    tb.stall_t[r] = t
                elif t - tb.stall_t[r] >= 5_000_000_000:
                    far = float(np.linalg.norm(pe - tb.goal[r])) > max(1.0, tb.tol[r] * 2) if call.op != "orbit" else True
                    if far and S.ctrl_mode[s] not in (CtrlMode.HOLD,):
                        self._finish(call, "failed", int(Reason.STALLED), {"status": "FAILED", "verify_trust": 2,
                                                                          "observed_state": self._obs(int(s))})
                        continue
                    tb.stall_ref[r] = pe
                    tb.stall_t[r] = t
            if t > tb.deadline[r]:
                self._finish(call, "failed", int(Reason.PROGRESS_TIMEOUT), {"status": "FAILED", "verify_trust": 2,
                                                                           "observed_state": self._obs(int(s))})
                continue
            if call.batch_id is None and tb.status[r] == ST_RUNNING and now_w - tb.prog_wall[r] >= PROGRESS_MIN_NS:
                tb.prog_wall[r] = now_w
                prog = self._progress(call, r)
                if prog is not None:
                    self.events.emit("cmd.progress", t_sim_ns=t, severity=0, uav=call.uav, cid=call.cid, op=call.op,
                                     progress=prog)
        E = S.enu
        tb.max_dev[rows] = np.maximum(tb.max_dev[rows], np.linalg.norm(E.pos_ref[slots] - E.pos[slots], axis=1))

    def _hold(self, r: int, cond: bool, t: int, need_s: float) -> bool:
        tb = self.table
        if not cond:
            tb.hold_since[r] = -1
            return False
        if tb.hold_since[r] < 0:
            tb.hold_since[r] = t
        return t - tb.hold_since[r] >= int(need_s * 1e9) - 1

    def _done_vector(self, rows: np.ndarray, slots: np.ndarray, fs: np.ndarray, t: int) -> None:
        S, tb = self.S, self.table
        E = S.enu
        vn = np.linalg.norm(E.vel[slots], axis=1)
        for r, s, f, sp in zip(rows, slots, fs, vn, strict=True):
            call = tb.meta[r]
            if call is None or call.final or tb.paused[r]:
                continue
            s = int(s)
            f = int(f)
            op = call.op
            t_exec = round((t - int(tb.t_accept[r])) * 1e-9, 3)
            done, m = False, {"t_exec_s": t_exec}
            if op == "takeoff":
                z_t = float(E.ground_up(s)) + float(tb.alt[r])
                err = abs(float(E.pos[s, 2]) - z_t)
                ok = f == FS.FLYING and err < max(0.3, 0.05 * float(tb.alt[r])) and abs(float(E.vel[s, 2])) < 0.3
                done, m = self._hold(r, ok, t, 1.0), {"alt_err_m": round(err, 3), "t_exec_s": t_exec}
            elif op == "goto":
                dist = float(np.linalg.norm(E.pos[s] - tb.goal[r]))
                done = self._hold(r, dist < tb.tol[r] and sp < 0.5 and S.ctrl_mode[s] != CtrlMode.TRAJ, t, 1.0)
                m = {"dist_err_m": round(dist, 3), "t_exec_s": t_exec}
            elif op == "follow_path":
                dist = float(np.linalg.norm(E.pos[s] - tb.goal[r]))
                arrived = S.ctrl_mode[s] not in (CtrlMode.PATH, CtrlMode.TRAJ)  # 原生 PATH 或提供者交回 HOLD 后才判完成
                done = self._hold(r, arrived and dist < 0.5 and sp < 0.5, t, 1.0)
                m = {"path_len_m": round(float(tb.aux[r, 0]), 2), "max_dev_m": round(float(tb.max_dev[r]), 3),
                     "dist_err_m": round(dist, 3), "t_exec_s": t_exec}
            elif op == "orbit":
                R, v, turns = float(tb.aux[r, 0]), float(tb.aux[r, 1]), float(tb.aux[r, 2])
                c = tb.goal[r]
                if turns > 0:
                    back = S.ctrl_mode[s] not in (CtrlMode.ORBIT, CtrlMode.TRAJ)
                    done = back and (call.provider is not None or float(S.orb[s, K.O_TURN]) >= turns - 1e-6)
                    m = {"turns": round(float(S.orb[s, K.O_TURN]), 3), "t_exec_s": t_exec}
                else:
                    rr = float(np.linalg.norm(E.pos[s, :2] - c[:2]))
                    ok = abs(rr - R) < 1.0 and abs(sp - v) < 0.5
                    done = self._hold(r, ok, t, 3.0)
                    m = {"radius_err_m": round(abs(rr - R), 3), "t_exec_s": t_exec}
            elif op == "hover":
                done = self._hold(r, sp < 0.3, t, 1.0)
            elif op == "land":
                tb.seen_landed[r] |= f == FS.LANDED
                done = bool(tb.seen_landed[r]) and f == FS.DISARMED
                m = {"pos_err_m": round(float(np.linalg.norm(E.pos[s, :2] - E.land_xy(s))), 3), "t_exec_s": t_exec}
            elif op == "rtl":
                err = float(np.linalg.norm(E.pos[s, :2] - E.home[s, :2]))
                if call.args.get("land", True):
                    tb.seen_landed[r] |= f == FS.LANDED
                    done = f == FS.DISARMED and err < 2.0 and bool(tb.seen_landed[r])
                else:
                    done = err < 2.0 and abs(float(E.pos[s, 2]) - float(S.z_rtl[s])) < 1.0
                m = {"pos_err_m": round(err, 3), "t_exec_s": t_exec}
            elif op == "velocity":
                done = bool(tb.stop_seen[r]) and self._hold(r, sp < 0.3, t, 1.0)
            elif op == "safety_stop":
                done = self._hold(r, sp < 0.3, t, 0.5)
            elif op == "pause":
                done = self._hold(r, sp < 0.3, t, 1.0)
            elif op == "arm":
                done = f == FS.READY
            elif op == "disarm":
                done = f == FS.DISARMED
            if done:
                call.metrics = m
                if op == "velocity":
                    S.vel_sess[s] = False
                self._finish(call, "succeeded", 0, {"status": "OK", "verify_trust": 4, "simulated": True, "metrics": m,
                                                    "latency_ms": self._latency_ms(call)})
                if op in ("rtl", "land") and self.PB is not None:
                    ACT.release_path(S, self.PB, np.array([s]))

    def _progress(self, call: Call, r: int) -> dict | None:
        S, tb = self.S, self.table
        s = call.slot
        if call.op in ("goto", "follow_path"):
            d = float(np.linalg.norm(tb.goal[r] - S.enu.pos[s]))
            v = max(float(np.linalg.norm(S.enu.vel[s])), 0.5)
            return {"phase": "paused" if tb.paused[r] else "executing", "dist_m": round(d, 2), "eta_s": round(d / v, 1)}
        if call.op == "takeoff":
            z_t = float(S.enu.ground_up(s)) + float(tb.alt[r])
            d = abs(float(S.enu.pos[s, 2]) - z_t)
            return {"phase": "executing", "dist_m": round(d, 2), "eta_s": round(d / 1.5, 1)}
        if call.op == "orbit":
            return {"phase": "paused" if tb.paused[r] else "executing", "turns": round(float(S.orb[s, K.O_TURN]), 3)}
        if call.op in ("land", "rtl"):
            return {"phase": "executing", "dist_m": round(float(S.agl[s]), 2)}
        return None

    def _obs(self, slot: int) -> str:
        sb = self.S.blocks["safety"]
        fs, s = int(sb["fs"][slot]), int(sb["sub"][slot])
        return f"{FLIGHTSTATE_NAMES[fs]}/{sub_name(fs, s) or s}"

    def _latency_ms(self, call: Call) -> int:
        return max(0, int((self.clock.wall_mono_ns() - call.wall_accept_ns) // 1_000_000))

    # ------------------------------------------------------------ 终态与回调
    def _event(self, kind: str, call: Call, code: int, effect: dict, **extra) -> None:
        self.events.emit(kind, t_sim_ns=self.S.t_ns, severity=SEVERITY.get(kind.split(".", 1)[1], 0), uav=call.uav,
                         cid=call.cid, batch_id=call.batch_id, op=call.op, code=int(code), effect=effect, **extra)

    def _succeed_now(self, call: Call | None) -> None:
        if call is None:
            return
        self._finish(call, "succeeded", 0, {"status": "OK", "verify_trust": 4, "simulated": True,
                                            "metrics": {"t_exec_s": round((self.S.t_ns - call.t_accept_ns) * 1e-9, 3)},
                                            "latency_ms": self._latency_ms(call)})

    def _finish(self, call: Call | None, status: str, code: int, effect: dict) -> None:
        if call is None or call.final:
            return
        call.status, call.code, call.final, call.effect = status, int(code), True, effect
        key = "succeeded" if status == "succeeded" else "failed" if status in ("failed", "timeout") else "canceled"
        self.stats[key] += 1
        if call.op == "velocity" and status != "succeeded" and call.slot >= 0:
            self.S.vel_sess[call.slot] = False
        self._event(f"cmd.{status}", call, code, effect)
        if call.row >= 0:
            self.table.status[call.row] = ST_FINAL
            self.table.release(call.row)
        for owner, cb in self._subs:
            if not owner or call.cid.startswith(owner):
                with contextlib.suppress(Exception):
                    cb(call)

    def _cancel_provider(self, call: Call) -> None:
        if call.provider is None:
            return
        prov = _provider_for(call.op, call.args)
        if prov is not None:
            prov.cancel(call.cid, np.array([call.slot], np.int32))

    def resolve_calls(self, slots: np.ndarray, status: str, code: int, reason: str) -> None:
        """M09 回调（204、208、209、安全类取代的 206）：同一调用只结束一次，先到者为准（FR-057）。"""
        tb = self.table
        for s in np.atleast_1d(np.asarray(slots, np.int64)):
            for lane in (0, 1):
                call = tb.current(int(s), lane)
                if call is not None and not call.final:
                    self._cancel_provider(call)
                    self._finish(call, status, code, {"status": "FAILED" if status == "failed" else "UNVERIFIED",
                                                      "verify_trust": 2, "message": reason})

    def cancel_slot(self, slot: int, code: int, message: str) -> None:
        self.resolve_calls(np.array([slot]), "canceled", code, message)

    def cancel_all(self, code: int, message: str) -> None:
        """剧本重置等：在途调用一律 `canceled code`。"""
        for call in self.table.calls():
            if not call.final:
                self._cancel_provider(call)
                self._finish(call, "canceled", code, {"status": "UNVERIFIED", "verify_trust": 1, "message": message})
        self.table.clear()
        self.staged.clear()
        self.fine_queue.clear()
        self.fine_done.clear()

    def on_rate_change(self, rate: float) -> None:
        """倍率 ≠ 1 时取消 Velocity 会话（`canceled 209`），机体转悬停（AWR-12 §5.13；C06）。"""
        if rate == 1.0:
            return
        for call in self.table.calls():
            if call.op == "velocity" and not call.final:
                ACT.begin_hold(self.S, np.array([call.slot]), self.S.t_ns * 1e-9)
                self._finish(call, "canceled", int(Reason.WATCHDOG),
                             {"status": "UNVERIFIED", "verify_trust": 2, "message": "rate != 1"})

    def on_watchdog(self, slots: np.ndarray) -> None:
        """Velocity 看门狗（M09 未登记 SafetyHooks 时的兜底：HOLD 并 `canceled 209`，M08-FR-029）。"""
        for s in slots:
            s = int(s)
            ACT.begin_hold(self.S, np.array([s]), self.S.t_ns * 1e-9)
            sb = self.S.blocks["safety"]
            if self.fallback_fsm and "hold_reason" in sb:
                sb["hold_reason"][s] = sub_value(FS.HOLD, "LINK_LOSS") + 1
            call = self.table.current(s, 0)
            if call is not None and call.op == "velocity" and not call.final:
                self._finish(call, "canceled", int(Reason.WATCHDOG),
                             {"status": "UNVERIFIED", "verify_trust": 2, "message": "stream watchdog"})
            self.S.vel_sess[s] = False

    def _expire_idem(self, now_w: int) -> None:
        while self.idem:
            _k, ent = next(iter(self.idem.items()))
            if now_w - ent.wall_ns <= IDEM_TTL_NS:
                break
            self.idem.popitem(last=False)

    # ------------------------------------------------------------ 机群增删与度量
    def _fleet_op(self, msg: dict, op: str, args: dict, p: dict) -> tuple[dict, list[Call]]:
        role = p.get("role")
        internal = bool(p.get("_internal"))
        if not internal:
            if role not in ("operator", "admin"):
                return self._reject(msg, int(Reason.ROLE_FORBIDDEN))
            if not self.lease.is_seat_holder(str(p.get("principal_id"))):
                return self._reject(msg, int(Reason.SEAT_TAKEN))
        if self.fleet_ops is None:
            return self._reject(msg, int(Reason.SERVICE_UNAVAILABLE))
        cid = str(msg.get("cid") or "")
        if op == "fleet/add":
            code, detail = self.fleet_ops.admit_add(args)
        else:
            uav = msg.get("uav") if isinstance(msg.get("uav"), str) else args.get("id")
            if not isinstance(uav, str) or self.roster.resolve(uav) is None:
                return self._reject(msg, int(Reason.NO_VEHICLE), detail={"uav": uav})
            if args.get("force") and not args.get("confirm_token"):
                return self._reject(msg, int(Reason.CONFIRM_REQUIRED))
            code, detail = self.fleet_ops.remove(uav, bool(args.get("force")))
        if code:
            return self._reject(msg, code, detail=detail)
        if self.inputlog is not None:
            self.inputlog.append("roster", self.S.tick + 1, msg)
        adm = self._base_adm(cid) | {"status": "accepted", "code": 0, "reason": None, "detail": detail, "remedy": None,
                                      "apply_tick": self.S.tick + 1}
        return adm, []

    def extend_deadline(self, cid: str, until_t_ns: int) -> bool:
        """把在途调用的截止时间推迟到不早于 until_t_ns（仿真 ns）；返回是否找到在途调用。INT-1（M10-to-M08 第 2 条）：
        M10 暂停任务与长轨迹经此公开接口延长截止，不再直接写 `table.deadline`。"""
        ent = self.idem.get(cid)
        if ent is None:
            return False
        hit = False
        for c in ent.calls:
            if c.row >= 0 and not c.final:
                self.table.deadline[c.row] = max(int(self.table.deadline[c.row]), int(until_t_ns))
                hit = True
        return hit

    def _plugin_op(self, msg: dict, op: str, p: dict) -> tuple[dict, list[Call]]:
        """登记的非机体命令（`register_command_handler`）：验签后按角色与席位放行，同步调用处理者。"""
        spec = self.reg.commands[op]
        if not p.get("_internal"):
            if p.get("role") not in ("operator", "admin"):
                return self._reject(msg, int(Reason.ROLE_FORBIDDEN))
            if spec.need_seat and not self.lease.is_seat_holder(str(p.get("principal_id"))):
                return self._reject(msg, int(Reason.SEAT_TAKEN))
        apply_tick = self.S.tick + 1
        try:
            r = spec.fn(msg, apply_tick, self.ctx)
        except Exception as e:  # 插件异常不拖垮主循环
            return self._reject(msg, int(Reason.SERVICE_UNAVAILABLE), detail={"op": op, "error": type(e).__name__})
        r = r if isinstance(r, dict) else {}
        code = int(r.get("code") or 0)
        if r.get("status", "accepted" if code == 0 else "rejected") != "accepted" or code:
            return self._reject(msg, code or int(Reason.STATE), detail=r.get("detail"))
        if self.inputlog is not None:
            self.inputlog.append("cmd", apply_tick, msg)
        det = {k: r[k] for k in ("result", "detail") if r.get(k) is not None}
        if r.get("warnings"):
            det["warnings"] = list(r["warnings"])
        adm = self._base_adm(str(msg.get("cid") or "")) | {"status": "accepted", "code": 0, "reason": None,
                                                              "detail": det or None, "remedy": None,
                                                              "apply_tick": apply_tick}
        # INT-1（M07-to-M08 第 1 条）：插件命令由处理者同步完成，准入后立即给出终态，网关的调用与 UI 草稿据此结束
        cid = str(msg.get("cid") or "")
        if cid:
            extra = {"result": r["result"]} if r.get("result") is not None else {}
            self.events.emit("cmd.succeeded", t_sim_ns=self.S.t_ns, severity=SEVERITY.get("succeeded", 0), uav=None,
                             cid=cid, batch_id=msg.get("batch_id"), op=op, code=0,
                             effect={"status": "OK", "verify_trust": 4, "simulated": True}, **extra)
        return adm, []

    def _metric_op(self, msg: dict, args: dict) -> tuple[dict, list[Call]]:
        name = str(args.get("name") or "")
        try:
            val = MET.metric(name, **(args.get("kw") or {}))
        except KeyError:
            return self._reject(msg, int(Reason.NOT_FOUND), detail={"name": name})
        adm = self._base_adm(str(msg.get("cid") or "")) | {"status": "accepted", "code": 0, "reason": None,
                                                              "detail": {"name": name, "value": val}, "remedy": None,
                                                              "apply_tick": None}
        return adm, []


def _counts(calls: list[Call]) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in calls:
        out[c.status] = out.get(c.status, 0) + 1
    return out
