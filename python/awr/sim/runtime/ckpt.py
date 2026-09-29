"""checkpoint 接线（D1-ext，M08-FR-083；ADR-019；M11-R-to-M08 第 5 条）。

内容：FleetState 全部字段与全部状态块、PathBuffer、调用表（SoA + 调用元数据）、staged 队列、幂等表、roster、租约与席位、
SimClock（tick、state、rate）、RNG 流状态、幽灵机游标。主循环内只做 numpy 拷贝（`CheckpointStore.save` 双缓冲，两份缓冲都忙
时跳过），序列化与写盘在 M11 CheckpointStore 的后台线程；每 1 s【仿真】一次（慢任务），SIGTERM 时 `close(final=True)`。
恢复：supervisor 重启 sim-core 时（StateRing 复用）读取最新有效代恢复，发 `sim.restarted`；恢复后 5 s 内再次崩溃则把该代标记
为毒性，连续 3 代中毒时从剧本起点重开（M11-FR-017）。
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np

from awr.contracts import LAYOUT_ID
from awr.contracts.enums import Lifecycle

__all__ = ["SimCheckpointer", "attach_checkpoint", "capture", "restore"]

_CT = ("live", "slot", "op", "lane", "status", "applied", "fine_pending", "paused", "t_accept", "t_running", "deadline",
       "hold_since", "goal", "tol", "alt", "aux", "seen_landed", "stop_seen", "stall_ref", "stall_t", "max_dev", "by_slot")


def _big_out(x: Any) -> Any:
    """msgpack 只支持 64 位整数：PCG64 的 128 位状态以 `{"__bigint__": "<十进制>"}` 保存。"""
    if isinstance(x, dict):
        return {k: _big_out(v) for k, v in x.items()}
    if isinstance(x, int) and not isinstance(x, bool) and not -(1 << 63) <= x < (1 << 64):
        return {"__bigint__": str(x)}
    return x


def _big_in(x: Any) -> Any:
    if isinstance(x, dict):
        if set(x) == {"__bigint__"}:
            return int(x["__bigint__"])
        return {k: _big_in(v) for k, v in x.items()}
    return x


def capture(core: Any) -> tuple[dict[str, np.ndarray], dict]:
    """主线程：收集 checkpoint 数组与小对象（调用方负责拷贝语义；CheckpointStore.save 会拷贝）。"""
    f, eng, tb = core.fleet, core.engine, core.engine.table
    arrays = dict(f.checkpoint_arrays())
    for k in _CT:
        arrays[f"ct.{k}"] = getattr(tb, k)
    arrays["lease.owner"] = core.lease._owner
    arrays["lease.susp"] = core.lease._suspended
    calls = []
    for r in tb.rows():
        c = tb.meta[int(r)]
        if c is None:
            continue
        calls.append({"row": int(r), "cid": c.cid, "op": c.op, "slot": c.slot, "uav": c.uav, "principal_id": c.principal_id,
                      "role": c.role, "source": c.source, "args": c.args, "t_accept_ns": c.t_accept_ns,
                      "wall_accept_ns": c.wall_accept_ns, "lane": c.lane, "batch_id": c.batch_id, "status": c.status,
                      "applied": c.applied, "provider": c.provider, "effect": c.effect})
    staged = [{"cid": s.cid, "slots": [int(x) for x in s.slots], "op": s.op, "provider": s.provider, "args": s.args,
               "source": s.source, "seq": s.seq, "apply_tick": s.apply_tick, "request_tick": s.request_tick,
               "rows": [int(x) for x in (s.rows if s.rows is not None else [])]} for s in eng.staged]
    ros = core.roster
    roster = [{"slot": e.slot, "id": e.id, "agent_no": e.agent_no, "kind": e.kind, "model": e.model,
               "profile_id": e.profile_id, "limits_profile": e.limits_profile, "home_enu_m": list(e.home_enu_m),
               "yaw_rad": e.yaw_rad, "initial_soc": e.initial_soc, "backend": e.backend, "lifecycle": e.lifecycle,
               "lc_t_ns": e.lc_t_ns} for e in ros.by_slot.values()]
    leases = {str(s): {"owner": L.owner, "holder": L.holder, "stack": [list(x) for x in L.stack]}
              for s, L in core.lease._lease.items()}
    clk = core.clock
    meta = {"fleet": f.checkpoint_meta(), "calls": calls, "staged": staged, "seq": eng._seq,
            "idem": [[k, v.adm, [c.cid for c in v.calls]] for k, v in eng.idem.items()],
            "roster": roster, "roster_version": ros.roster_version, "next_no": ros._next_no,
            "model_seq": dict(ros._model_seq), "seat": core.lease.seat.to_json(), "leases": leases,
            "clock": {"tick": clk.tick, "state": clk.state, "rate": clk.rate},
            "rng": {k: _big_out(g.bit_generator.state) for k, g in core.ctx.rng.items()},
            "epoch": core.epoch, "segment": core.segment, "removing": {str(k): v for k, v in core._removing.items()},
            "stopped": list(core._stopped)}
    return arrays, meta


def restore(core: Any, arrays: dict[str, np.ndarray], meta: dict) -> None:
    """把 checkpoint 装回已 start 的 SimCore（机群、调用、租约、时钟、RNG）；不改 epoch/segment（由调用方决定）。"""
    from ..core.calls import Call
    from ..core.command import StagedCmd, _Idem
    from ..core.roster import RosterEntry

    f, eng, tb = core.fleet, core.engine, core.engine.table
    for s in list(core.roster.by_slot):
        f.remove(s)
    core.roster.by_slot.clear()
    core.roster.by_id.clear()
    f.restore({k: v for k, v in arrays.items() if not k.startswith(("ct.", "lease."))}, meta["fleet"])
    for e in meta["roster"]:
        re = RosterEntry(int(e["slot"]), e["id"], int(e["agent_no"]), e["kind"], e["model"], e["profile_id"],
                         e["limits_profile"], tuple(e["home_enu_m"]), float(e["yaw_rad"]), float(e["initial_soc"]),
                         backend=e["backend"], producer=core.roster.producer, lifecycle=int(e["lifecycle"]),
                         lc_t_ns=int(e["lc_t_ns"]))
        core.roster.by_slot[re.slot] = re
        core.roster.by_id[re.id] = re
    core.roster.roster_version = int(meta["roster_version"]) + 1
    core.roster._next_no = int(meta["next_no"])
    core.roster._model_seq = {k: int(v) for k, v in meta["model_seq"].items()}
    core.agent_slot = {x.agent_no: x.slot for x in core.roster.by_slot.values()}
    tb.clear()
    for k in _CT:
        np.copyto(getattr(tb, k), arrays[f"ct.{k}"])
    tb.meta = [None] * tb.capacity
    for c in meta["calls"]:
        call = Call(c["cid"], c["op"], int(c["slot"]), c["uav"], c["principal_id"], c["role"], c["source"], c["args"],
                    int(c["t_accept_ns"]), int(c["wall_accept_ns"]), row=int(c["row"]), lane=int(c["lane"]),
                    batch_id=c["batch_id"], status=c["status"], applied=bool(c["applied"]), provider=c["provider"],
                    effect=c["effect"])
        tb.meta[call.row] = call
    tb.free = [r for r in range(tb.capacity - 1, -1, -1) if not tb.live[r]]
    eng.staged = [StagedCmd(s["cid"], np.asarray(s["slots"], np.int32), s["op"], s["provider"], s["args"], s["source"],
                            int(s["seq"]), int(s["apply_tick"]), int(s["request_tick"]), np.asarray(s["rows"], np.int32))
                  for s in meta["staged"]]
    eng._seq = int(meta["seq"])
    by_cid = {c.cid: c for c in tb.meta if c is not None}
    eng.idem.clear()
    for k, adm, cids in meta["idem"]:
        eng.idem[k] = _Idem(core.clock.wall_mono_ns(), adm, [by_cid[c] for c in cids if c in by_cid])
    np.copyto(core.lease._owner, arrays["lease.owner"])
    np.copyto(core.lease._suspended, arrays["lease.susp"])
    core.lease._lease.clear()
    for s, L in meta["leases"].items():
        ls = core.lease.lease(int(s))
        ls.owner, ls.holder, ls.stack = int(L["owner"]), L["holder"], [tuple(x) for x in L["stack"]]
    seat = meta["seat"]
    core.lease.seat.state, core.lease.seat.holder = seat["state"], seat["holder"]
    clk = core.clock
    clk.tick = int(meta["clock"]["tick"])
    clk.rate = float(meta["clock"]["rate"])
    clk._anchor(clk.wall_mono_ns())
    core.ctx.tick = clk.tick
    core.ctx.t_ns = clk.t_ns
    for k, st in (meta.get("rng") or {}).items():
        if k in core.ctx.rng:
            core.ctx.rng[k].bit_generator.state = _big_in(st)
    core._removing = {int(k): v for k, v in (meta.get("removing") or {}).items()}
    core._stopped = list(meta.get("stopped") or [])
    for e in core.roster.by_slot.values():
        f.S.lifecycle[e.slot] = e.lifecycle


class SimCheckpointer:
    def __init__(self, store: Any, *, period_sim_ns: int = 1_000_000_000) -> None:
        self.store = store
        self.period = period_sim_ns
        self.last_t = -(1 << 62)
        self.copy_ms: list[float] = []

    def maybe_save(self, core: Any) -> Any:
        if core.clock.t_ns - self.last_t < self.period:
            return False
        return self.save(core)

    def save(self, core: Any, *, force: bool = False) -> bool:
        t0 = time.perf_counter()
        arrays, meta = capture(core)
        ok = self.store.save(core.clock.t_ns, core.epoch, core.segment, arrays, meta)
        self.copy_ms.append((time.perf_counter() - t0) * 1e3)
        if ok:
            self.last_t = core.clock.t_ns
        return ok

    def close(self, *, final: bool = True) -> None:
        self.store.close(final=final)


def attach_checkpoint(core: Any, ctx: Any) -> None:
    """supervisor 形态：CheckpointStore 在 `run_dir/ckpt`（tmpfs），镜像到 `persist_dir/ckpt`；崩溃重启（StateRing 复用、
    `restart_count > 0`）时按 M11 `restore_for_restart` 的策略恢复（5 s 内再崩 → 该代 poison 改用上一代；连续 3 代中毒 →
    从剧本起点重开），恢复后发 `sim.restarted`。"""
    from awr.runtime.checkpoint import CheckpointStore

    if not getattr(ctx, "supervised", False):
        return
    store = CheckpointStore(ctx.run_dir / "ckpt", layout_id=LAYOUT_ID, mirror=ctx.persist_dir / "ckpt")
    core.checkpointer = SimCheckpointer(store)
    restart_count = int(getattr(ctx, "restart_count", 0))
    if not core.reused:
        restart_count = 0
    ck = store.restore_for_restart(restart_count)
    if ck is None:
        return
    orig_start = core.start

    def start_and_restore() -> None:
        orig_start()
        restore(core, ck.arrays, ck.meta)
        lost_ms = 0
        core.events.emit("sim.restarted", t_sim_ns=ck.t_sim_ns, severity=2, epoch=core.epoch,
                         restored_t_sim_ns=int(ck.t_sim_ns), lost_ms=lost_ms, restart_count=restart_count)
        for e in core.roster.by_slot.values():
            if e.lifecycle == int(Lifecycle.PENDING):
                e.lifecycle = int(Lifecycle.STARTING)
        core.events.flush()

    core.start = start_and_restore  # type: ignore[method-assign]
