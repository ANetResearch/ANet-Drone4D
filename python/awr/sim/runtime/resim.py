"""输入日志重仿真 `python -m awr.sim.runtime --resim runs/<run>`（D1-ext，M08-FR-084；ADR-049）。

以批处理方式运行（不受墙钟约束）：按头中的 FleetConfig、世界与布设重建 sim-core（LocalBus + LocalRing，不验签），逐 tick
推进并在日志记录的 apply_tick 注入外部输入（`cmd`、`clock`、`lease`、`roster`）；每次 StateRing 发布记录 Full64 字节的
sha256，结束时输出帧摘要序列（同一内核、同一版本、同一输入序列下与原运行逐位一致，M08-AC-036）。
头中的内核、版本、FleetConfig、世界或 world_seed 与当前环境任一不一致即拒绝（`355 RESIM_INCOMPATIBLE`，AWR-17 §8.4）。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import msgpack

from awr.contracts import LAYOUT_ID
from awr.contracts.reasons import Reason

from .inputlog import _version, read_inputlog

__all__ = ["ResimIncompatible", "check_compat", "resim", "resim_main"]


class ResimIncompatible(RuntimeError):
    code = int(Reason.RESIM_INCOMPATIBLE)


def check_compat(header: dict, *, kernel: str | None = None) -> None:
    from ..fleet.fleet import FleetConfig, resolve_kernel

    sim = header.get("sim") or {}
    want = sim.get("kernel", "numba")
    got, _ = resolve_kernel(kernel or want)
    problems = []
    if got != want:
        problems.append(f"kernel {want} != {got}")
    if sim.get("version") not in (None, _version()):
        problems.append(f"version {sim.get('version')} != {_version()}")
    fc = sim.get("fleet_config") or {}
    known = set(FleetConfig().to_json())
    if set(fc) - known:
        problems.append(f"fleet_config keys {sorted(set(fc) - known)}")
    if problems:
        raise ResimIncompatible("; ".join(problems))


def resim(run_dir: Path, *, kernel: str | None = None, extra_ticks: int = 0, frames: list | None = None,
          times: list | None = None) -> list[str]:
    """重放 `run_dir/inputs.msgpack`；返回每次发布的 Full64 sha256 列表（frames、times 非 None 时另收集原始字节与
    帧的 t_sim_ns）。"""
    from awr.runtime.bus import LocalBus
    from awr.runtime.statering import LocalRing

    from ..fleet.fleet import FleetConfig
    from .config import SimConfig
    from .main import SimCore

    header, entries = read_inputlog(Path(run_dir) / "inputs.msgpack")
    check_compat(header, kernel=kernel)
    sim = header["sim"]
    fc = FleetConfig(**{k: v for k, v in (sim.get("fleet_config") or {}).items()})
    cfg = SimConfig(world_id=sim.get("world_id", "shenzhen"), run_id="resim" + secrets.token_hex(3), fleet=fc,
                    n_vehicles=int(sim.get("n_vehicles", 1)), profile_id=sim.get("profile_id", "p600_mid360"),
                    spawn_xy=tuple(sim["spawn_xy"]) if sim.get("spawn_xy") else None,
                    spawn_spacing_m=float(sim.get("spawn_spacing_m", SimConfig.spawn_spacing_m)),
                    limits_profile=sim.get("limits_profile"), autoplay=bool(sim.get("autoplay", True)),
                    world_seed=int(header.get("world_seed", 0)), load_world=bool(sim.get("load_world", False)))
    path = f"/resim/{cfg.run_id}/state.sim-core"
    ring, _ = LocalRing.open_or_create(path, capacity=fc.capacity, slots=32, layout_id=LAYOUT_ID, id_base=0, id_count=1024)
    bus = LocalBus.open("sim-core", namespace=f"awr/resim/{cfg.run_id}")
    W = [1_000_000_000]
    core = SimCore(cfg, bus, ring, secret=None, wall_ns=lambda: W[0])
    by_tick: dict[int, list[dict]] = defaultdict(list)
    for e in entries:
        by_tick[int(e["tick"])].append(e)
    digests: list[str] = []
    try:
        core.start()
        last = max(by_tick) if by_tick else 0
        tap = core.fleet.tap
        pub0 = tap.published
        for t in range(1, last + 1 + int(extra_ticks)):
            for e in by_tick.get(t, []):
                _inject(core, e)
            core.ctx.tick = core.clock.tick
            core.fleet.step(core.ctx, 1)
            core.clock.tick = core.ctx.tick
            W[0] += 4_000_000
            core.events.flush()
            if tap.published != pub0:
                pub0 = tap.published
                f = ring.read_latest(0) if hasattr(ring, "read_latest") else None
                raw = bytes(f.full) if f is not None else b""
                digests.append(hashlib.sha256(raw).hexdigest())
                if frames is not None:
                    frames.append(raw)
                if times is not None:
                    times.append(int(f.t_sim_ns) if f is not None else -1)
    finally:
        core.stop()
        bus.close()
        LocalRing.remove(path)
    return digests


def _inject(core: Any, e: dict) -> None:
    msg = msgpack.unpackb(e["payload"], raw=False, strict_map_key=False)
    kind = e["kind"]
    if kind == "cmd":
        core.engine.handle(msg)
    elif kind == "clock":
        op = msg.get("op")
        if op in ("speed",):
            core.clock.apply(op, msg.get("args") or {})
    elif kind == "lease":
        if "principal" in msg:
            core._lease_op(msg)
        elif msg.get("op") in ("acquire", "release"):
            core._lease_op({"v": 1, "cid": "resim", "op": msg["op"], "uav": msg.get("uav"),
                            "principal": {"principal_id": msg.get("pid"), "role": "operator", "entry": "api", "seat": True}})
    elif kind == "roster":
        if msg.get("op") == "add" and core.roster.resolve(str(msg.get("id"))) is None:
            core.add_vehicle(msg["profile_id"], tuple(msg["home_enu_m"]), 0.0, vehicle_id=msg["id"],
                             backend=msg.get("backend", "mock"))
        elif msg.get("op") in ("fleet/add", "fleet/remove"):
            core.engine.handle(msg)


def resim_main(run_dir: Path, out: Path | None) -> int:
    try:
        d = resim(run_dir)
    except ResimIncompatible as e:
        print(json.dumps({"code": e.code, "reason": "RESIM_INCOMPATIBLE", "detail": str(e)}), file=sys.stderr)
        return 5
    summary = {"frames": len(d), "sha256": hashlib.sha256("".join(d).encode()).hexdigest()}
    if out is not None:
        out.write_text(json.dumps({"summary": summary, "frames": d}, indent=1))
    print(json.dumps(summary))
    return 0
