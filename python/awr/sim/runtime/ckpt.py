"""checkpoint 接线（D1-ext，M08-FR-083；ADR-019；M11-R-to-M08 第 5 条）。

内容：FleetState 全部字段与全部状态块、PathBuffer、调用表（SoA + 调用元数据）、staged 队列、幂等表、roster、租约与席位、
SimClock（tick、state、rate）、RNG 流状态、幽灵机游标。主循环内只做 numpy 拷贝（`CheckpointStore.save` 双缓冲，两份缓冲都忙
时跳过），序列化与写盘在 M11 CheckpointStore 的后台线程；每 1 s【仿真】一次（慢任务），SIGTERM 时 `close(final=True)`。
恢复：supervisor 重启 sim-core 时（StateRing 复用）读取最新有效代恢复，发 `sim.restarted`；恢复后 5 s 内再次崩溃则把该代标记
为毒性，连续 3 代中毒时从剧本起点重开（M11-FR-017）。
"""

from __future__ import annotations

import contextlib
import logging
import sys
import time
from typing import Any

import numpy as np

from awr.contracts import LAYOUT_ID
from awr.contracts.enums import Lifecycle
from awr.runtime.checkpoint import yield_point

__all__ = ["SimCheckpointer", "attach_checkpoint", "capture", "capture_ext", "restore", "restore_ext"]

log = logging.getLogger("awr.sim.runtime.ckpt")

_CT = ("live", "slot", "op", "lane", "status", "applied", "fine_pending", "paused", "t_accept", "t_running", "deadline",
       "hold_since", "goal", "tol", "alt", "aux", "seen_landed", "stop_seen", "stall_ref", "stall_t", "max_dev", "by_slot",
       "stall_rref", "stall_rmove")


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


_YIELD_EVERY = 64  # 后台编码每批条目数：批间让出（M11 `yield_point`：缺省 `time.sleep(0)`，有 gate 时等主循环休眠）


class _CallsMeta(list):
    """调用元数据列表（元素同 `Call.ck_entry`），附带调用对象以缓存不变字段字典的编码（`Call.ck_raw`）。

    `ck_pack_parts` 由 CheckpointStore 的后台写线程调用（M11 `pack_meta` 的约定）：输出与对列表本身 `packb` 逐字节相同，
    不变字段字典每个调用只编码一次（N = 1000 架在途 orbit 调用时每代编码此前约 9 ms；FX2-R3-sim，ADR-070）。"""

    __slots__ = ("calls",)

    def ck_pack_parts(self, pk: Any, parts: list[bytes]) -> None:
        parts.append(pk.pack_array_header(len(self)))
        pack = pk.pack
        for i, (c, e) in enumerate(zip(self.calls, self, strict=True)):
            tail = e[1:]
            prev = c.ck_tail
            if prev is not None and e[0] is c.ck and prev[0] == tail:
                parts.append(prev[1])  # 可变字段与上一代相同（稳态的在途调用）：整条复用
            else:
                raw = c.ck_raw
                if raw is None or e[0] is not c.ck:
                    raw = pack(e[0])
                    if e[0] is c.ck:
                        c.ck_raw = raw
                # 8 元素数组 = 头 0x98 + 不变字典的编码 + 其余 7 个元素的编码；后者取 7 元素数组编码去掉 1 字节的 fixarray 头
                # 0x97。effect 字典在主线程按引用交出，这里存一份浅拷贝作比较基准
                b = b"\x98" + raw + pack(tail)[1:]
                if e[0] is c.ck:
                    c.ck_tail = ([*tail[:6], dict(tail[6]) if isinstance(tail[6], dict) else tail[6]], b)
                parts.append(b)
            if i % _YIELD_EVERY == _YIELD_EVERY - 1:
                yield_point()


class _CachedList(list):
    """内容在缓存键不变期间不改动的列表（roster）：首次编码后缓存字节，此后各代直接拼接（ADR-070）。"""

    __slots__ = ("raw",)

    def ck_pack_parts(self, pk: Any, parts: list[bytes]) -> None:
        raw = getattr(self, "raw", None)
        if raw is None:
            from awr.runtime.checkpoint import pack_meta

            raw = self.raw = pack_meta(list(self))
        parts.append(raw)


class _CachedDict(dict):
    """内容在缓存键不变期间不改动的字典（租约表）：首次编码后缓存字节（ADR-070）。"""

    __slots__ = ("raw",)

    def ck_pack_parts(self, pk: Any, parts: list[bytes]) -> None:
        raw = getattr(self, "raw", None)
        if raw is None:
            raw = self.raw = pk.pack(dict(self))
        parts.append(raw)


class _IdemMeta(list):
    """幂等表行（元素为 [cid, 准入回执, 调用 cid 列表]），附带缓存队列的条目以逐行缓存编码（条目创建后不再改动，ADR-070）。"""

    __slots__ = ("ents",)

    def ck_pack_parts(self, pk: Any, parts: list[bytes]) -> None:
        parts.append(pk.pack_array_header(len(self)))
        for i, ent in enumerate(self.ents):
            raw = ent[3]
            if raw is None:
                raw = ent[3] = pk.pack(ent[2])
            parts.append(raw)
            if i % _YIELD_EVERY == _YIELD_EVERY - 1:
                yield_point()


def _calls_meta(core: Any, tb: Any) -> list[list]:
    """调用元数据：每条 [不变字段字典, row, lane, batch_id, status, applied, provider, effect]（`Call.ck_entry`）。不变字段
    字典在准入时生成并随调用对象复用，每代只拼可变字段（N = 1000 架批量命令后首代此前为 1000 条调用逐个建字典，数毫秒，
    ADR-065）；其编码在后台线程首次编码时缓存（`_CallsMeta`，ADR-070）。字典与列表交出后只读（后台线程编码）。"""
    meta = tb.meta
    pairs = [(c, r) for r in tb.rows().tolist() if (c := meta[r]) is not None]
    out = _CallsMeta(c.ck_entry(r) for c, r in pairs)
    out.calls = [c for c, _r in pairs]
    return out


def _roster_meta(core: Any) -> list[dict]:
    """roster 条目列表，按 (roster_version, lc_gen) 缓存：增删与生命周期转移都会推进其中之一（M08 roster）。"""
    ros = core.roster
    key = (id(ros), ros.roster_version, ros.lc_gen, len(ros.by_slot))
    hit = getattr(core, "_ck_roster", None)
    if hit is not None and hit[0] == key:
        return hit[1]
    roster = [{"slot": e.slot, "id": e.id, "agent_no": e.agent_no, "kind": e.kind, "model": e.model,
               "profile_id": e.profile_id, "limits_profile": e.limits_profile, "home_enu_m": list(e.home_enu_m),
               "yaw_rad": e.yaw_rad, "initial_soc": e.initial_soc, "backend": e.backend, "lifecycle": e.lifecycle,
               "lc_t_ns": e.lc_t_ns, "sensors": [dict(x) for x in e.sensors]} for e in ros.by_slot.values()]
    roster = _CachedList(roster)  # 键不变的各代复用同一对象，编码只做一次（ADR-070）
    core._ck_roster = (key, roster)
    return roster


def _leases_meta(core: Any) -> dict:
    """逐机租约表，按 `LeaseManager.gen`（acquire、release、孤儿化）与条目数缓存；N = 1000 架各有 operator 租约时此前每代重建
    约 2 ms。"""
    lm = core.lease
    key = (id(lm), id(lm._lease), getattr(lm, "gen", None), len(lm._lease))
    hit = getattr(core, "_ck_leases", None)
    if hit is not None and hit[0] == key and key[2] is not None:
        return hit[1]
    leases = _CachedDict({str(s): {"owner": L.owner, "holder": L.holder, "stack": [list(x) for x in L.stack]}
                          for s, L in lm._lease.items()})
    core._ck_leases = (key, leases)
    return leases


def _idem_meta(core: Any, eng: Any) -> list:
    """幂等表（cid → 准入回执与调用 cid 列表）。幂等表只在尾部插入、从头部淘汰（`IDEM_MAX` 与 TTL），条目创建后不再改动：
    缓存上一代的行队列，头部弹出已淘汰的条目、尾部补上新条目，只为新条目建行（N = 1000 架各一条命令时此前每代重建
    2000 行约 3 ms，ADR-065）。条目数对不上（例如中间被替换）时整表重建。"""
    from collections import deque

    idem = eng.idem
    st = getattr(core, "_ck_idem", None)
    rows = st[1] if st is not None and st[0] is idem else None
    if rows is not None:
        while rows and idem.get(rows[0][0]) is not rows[0][1]:
            rows.popleft()
        last = rows[-1] if rows else None
        new = []
        for k in reversed(idem):
            v = idem[k]
            if last is not None and k == last[0] and v is last[1]:
                break
            new.append([k, v, [k, v.adm, [c.cid for c in v.calls]], None])
        rows.extend(reversed(new))
        if len(rows) != len(idem):
            rows = None
    if rows is None:
        rows = deque([k, v, [k, v.adm, [c.cid for c in v.calls]], None] for k, v in idem.items())
        core._ck_idem = (idem, rows)
    out = _IdemMeta(r[2] for r in rows)
    out.ents = list(rows)  # 快照：后台线程编码期间主线程可能增删队列
    return out


def capture(core: Any) -> tuple[dict[str, np.ndarray], dict]:
    """主线程：收集 checkpoint 数组与小对象（数组不拷贝：CheckpointStore.save 拷贝进空闲缓冲；元数据字典交出后只读）。"""
    f, eng, tb = core.fleet, core.engine, core.engine.table
    arrays = dict(f.checkpoint_arrays(copy=False))
    live = np.flatnonzero(tb.live)
    hi = int(live[-1]) + 1 if live.size else 0  # 调用表只存活动行高水位以下（容量 8192 行约 1.5 MB；其余行均已释放）
    for k in _CT:
        a = getattr(tb, k)
        arrays[f"ct.{k}"] = a if k == "by_slot" else a[:hi]
    arrays["lease.owner"] = core.lease._owner
    arrays["lease.susp"] = core.lease._suspended
    calls = _calls_meta(core, tb)
    staged = [{"cid": s.cid, "slots": [int(x) for x in s.slots], "op": s.op, "provider": s.provider, "args": s.args,
               "source": s.source, "seq": s.seq, "apply_tick": s.apply_tick, "request_tick": s.request_tick,
               "rows": [int(x) for x in (s.rows if s.rows is not None else [])]} for s in eng.staged]
    ros = core.roster
    roster = _roster_meta(core)
    leases = _leases_meta(core)
    clk = core.clock
    meta = {"fleet": f.checkpoint_meta(), "calls": calls, "staged": staged, "seq": eng._seq,
            "idem": _idem_meta(core, eng),
            "roster": roster, "roster_version": ros.roster_version, "next_no": ros._next_no,
            "model_seq": dict(ros._model_seq), "seat": core.lease.seat.to_json(), "leases": leases,
            "clock": {"tick": clk.tick, "state": clk.state, "rate": clk.rate},
            "rng": {k: _big_out(g.bit_generator.state) for k, g in core.ctx.rng.items()},
            "epoch": core.epoch, "segment": core.segment, "removing": {str(k): v for k, v in core._removing.items()},
            "stopped": list(core._stopped), "ext": capture_ext(core)}
    return arrays, meta


def capture_ext(core: Any) -> dict[str, Any]:
    """扩展段（ADR-019；M09-to-M08 第 6 条、M07-to-M08 第 4 条）：各插件以字节串自行序列化的内部状态。

    - `safety`：M09 `SafetyHooks.checkpoint()`（故障登记、链路源累计量、间距最小值与事件计数；状态块已随 FleetState）；
    - `env`：M07 `EnvironmentService.checkpoint()`（关键帧、锚点、待生效操作、阵风调度器与 RNG、Dryden 状态）；
    - `metrics`：外部度量的最新值（ADR-058）。
    某段取不到（插件未装配或抛异常）时省略，恢复时对应插件保持冷启动状态。"""
    from ..core import metrics as MET

    out: dict[str, Any] = {}
    hooks = getattr(core.reg, "hooks", None)
    fn = getattr(hooks, "checkpoint", None)
    if callable(fn):
        try:
            out["safety"] = bytes(fn())
        except Exception:
            log.exception("safety checkpoint segment failed")
    env = getattr(core.ctx, "env", None)
    fn = getattr(env, "checkpoint", None)
    if callable(fn):
        try:
            out["env"] = bytes(fn())
        except Exception:
            log.exception("env checkpoint segment failed")
    snap = MET.external_snapshot()
    if snap:
        out["metrics"] = snap
    return out


def restore_ext(core: Any, ext: dict[str, Any] | None) -> list[str]:
    """按段恢复（`capture_ext` 的逆操作）；返回已恢复的段名。env 在恢复前确保服务已构造（M07 `ensure_service`）。"""
    from ..core import metrics as MET

    done: list[str] = []
    ext = ext or {}
    blob = ext.get("safety")
    hooks = getattr(core.reg, "hooks", None)
    if blob and callable(getattr(hooks, "restore", None)):
        try:
            hooks.restore(bytes(blob))
            done.append("safety")
        except Exception:
            log.exception("safety checkpoint restore failed")
    blob = ext.get("env")
    if blob:
        env = getattr(core.ctx, "env", None)
        if env is None:
            mod = sys.modules.get("awr.environment.stage")
            ensure = getattr(mod, "ensure_service", None) if mod is not None else None
            if callable(ensure):
                with contextlib.suppress(Exception):
                    env = ensure(core.ctx)
        if env is not None and callable(getattr(env, "restore", None)):
            try:
                env.restore(bytes(blob))
                done.append("env")
            except Exception:
                log.exception("env checkpoint restore failed")
    MET.external_restore(ext.get("metrics"))
    if ext.get("metrics"):
        done.append("metrics")
    return done


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
                         lc_t_ns=int(e["lc_t_ns"]), sensors=[dict(x) for x in (e.get("sensors") or [])])
        core.roster.by_slot[re.slot] = re
        core.roster.by_id[re.id] = re
    core.roster._free_lo = 0  # free_slot 的扫描下界随 roster 整体替换而复位
    core.roster.roster_version = int(meta["roster_version"]) + 1
    core.roster._next_no = int(meta["next_no"])
    core.roster._model_seq = {k: int(v) for k, v in meta["model_seq"].items()}
    core.agent_slot = {x.agent_no: x.slot for x in core.roster.by_slot.values()}
    tb.clear()
    for k in _CT:
        if f"ct.{k}" in arrays:  # 旧版本的 checkpoint 没有后追加的数组（stall_rref、stall_rmove），保持清零
            a, dst = arrays[f"ct.{k}"], getattr(tb, k)
            if a.shape == dst.shape:
                np.copyto(dst, a)
            else:  # 只存了活动行高水位以下（其余行已释放，alloc 时整行重置）
                dst[:len(a)] = a
    tb.meta = [None] * tb.capacity
    for c in meta["calls"]:
        if not isinstance(c, dict):  # [不变字段, row, lane, batch_id, status, applied, provider, effect]
            st, row, lane, batch_id, status, applied, provider, effect = c
            c = dict(st, row=row, lane=lane, batch_id=batch_id, status=status, applied=applied, provider=provider,
                     effect=effect)
        call = Call(c["cid"], c["op"], int(c["slot"]), c["uav"], c["principal_id"], c["role"], c["source"], c["args"],
                    int(c["t_accept_ns"]), int(c["wall_accept_ns"]), row=int(c["row"]), lane=int(c["lane"]),
                    batch_id=c["batch_id"], status=c["status"], applied=bool(c["applied"]), provider=c["provider"],
                    effect=c["effect"])
        tb.meta[call.row] = call
        tb.sync_flags(call.row)
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
    core.lease.gen = getattr(core.lease, "gen", 0) + 1
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
    core.restored_ext = restore_ext(core, meta.get("ext"))


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
    store = CheckpointStore(ctx.run_dir / "ckpt", layout_id=LAYOUT_ID, mirror=ctx.persist_dir / "ckpt",
                            gate=getattr(core, "idle_gate", None))
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
                         restored_t_sim_ns=int(ck.t_sim_ns), lost_ms=lost_ms, restart_count=restart_count,
                         ext=list(getattr(core, "restored_ext", []) or []))
        for e in core.roster.by_slot.values():
            if e.lifecycle == int(Lifecycle.PENDING):
                e.lifecycle = int(Lifecycle.STARTING)
        core.roster.touch()
        core.events.flush()

    core.start = start_and_restore  # type: ignore[method-assign]
