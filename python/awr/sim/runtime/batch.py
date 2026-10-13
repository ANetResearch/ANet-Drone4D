"""批处理自由运行（M08-FR-221、FR-222；ADR-106；AWR-04 §4.10 第 3 条；M08 §15.5"批处理主循环"）。

`python -m awr.sim.runtime --batch --until <t_sim_s> --config <session.json> [--seed n] [--out <dir>] [--clock sandbox100]
[--world <id>]`：

- 不按墙钟节拍：每轮执行一批到期 tick（`SLICE_S` 仿真秒），墙钟取"仿真时间 + 常数"的假时钟（墙钟周期的慢任务按 ×1 的节奏
  执行，结果与机器速度无关）；总线为进程内 LocalBus；不写 StateRing 槽（tap 关闭，只写头部时钟组，1 Hz【仿真】）；不写
  checkpoint、不发布 state_ext 与 perf；
- 会话配置：`--config` 为 `awr.sandbox.snapshot.v1`（或场景模板的同构对象），启动后经 `SessionConfig.apply` 原地装配；
- 规划作业：`ctx.batch`（`BatchCtl`）给出固定延迟 `lat_ticks(kind) = ⌈budget_ms·tick_hz/1000⌉`（分配 200 ms、站位 500 ms、
  扫描 2000 ms、转场 50 ms，其余取 M10 `BUDGET_MS`）；M10 的 PlanPoolClient 照常在规划进程执行，结果暂存到
  `apply_tick = request_tick + lat_ticks` 的步边界生效，到期未返回时阻塞等待（不按结果到达的墙钟时刻生效，D2-AC-32）；M20 的作业
  在提交 tick 同步执行、同样按固定延迟生效；墙钟等待超过 `WAIT_LIMIT_S`（60 s）由 `BatchCtl.fail()` 报告，批处理以退出码 3 结束
  （M08-FR-221）；
- 事件中的墙钟诊断量（`WALL_FIELDS`：`geo.ready.load_ms`、`plan.*.t_ms`）与 `t_wall_ns` 一样在输出中置 null；
- 输出（`--out`，缺省 `runs/batch/<id>/`）：`events.jsonl`（事件流，`t_wall_ns` 置 null 以便逐字节对拍，FR-222）、`summary.json`
  （墙钟用时、平均 RTF、各 stage 耗时、tick 数、会话配置问题）与登记的结果文件（M20 `relay_stats.json` 等；没有登记时写
  `{"v": 1, "items": []}`）；退出码 0。

确定性（FR-222）：同一内核、版本、会话配置与种子下，事件序列、识别物轨迹与结果文件逐字节一致——RNG 为计数器式流（ADR-049），
实体表按 target_no 升序，慢任务的预算不受墙钟影响（每轮全部到期任务都执行），规划结果按固定延迟生效。
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

import msgpack

from . import clock_profile as CP

__all__ = ["BATCH_LAT_MS", "WAIT_LIMIT_S", "WALL_FIELDS", "BatchCtl", "batch_main", "run_batch"]

log = logging.getLogger("awr.sim.runtime.batch")

ROOT = Path(__file__).resolve().parents[4]
SLICE_S = 0.05  # 每轮推进的仿真时长（慢任务的执行节奏）
WAIT_LIMIT_S = 60.0  # 规划结果的墙钟等待上限（M08 §15.4.4）
BATCH_LAT_MS = {"allocate": 200, "allocation": 200, "station": 500, "scan": 2000, "transit": 50}
KEEP_SLOW = ("estimate", "query", "fine_check", "cleanup", "geo", "audit")
EXIT_OK, EXIT_CONFIG, EXIT_PLAN_WAIT = 0, 2, 3


class BatchCtl:
    """批处理控制（`ctx.batch`）：规划作业的固定延迟与墙钟等待上限（M08-FR-221）。"""

    def __init__(self, profile: CP.ClockProfile) -> None:
        self.profile = profile
        self.wait_limit_s = WAIT_LIMIT_S
        self.failed: str | None = None
        self.waits: dict[str, list[float]] = {}  # 作业类别 -> 墙钟等待（s），summary 中按类别给出分布

    def lat_ticks(self, kind: str, budget_ms: float | None = None) -> int:
        """`⌈budget_ms · tick_hz / 1000⌉`；budget_ms 缺省按 BATCH_LAT_MS，其次 M10 的 `BUDGET_MS`（调用方传入），否则 1000 ms。"""
        ms = BATCH_LAT_MS.get(kind, budget_ms if budget_ms is not None else 1000.0)
        return max(1, math.ceil(float(ms) * self.profile.tick_hz / 1000.0))

    def record_wait(self, kind: str, wall_s: float) -> None:
        self.waits.setdefault(kind, []).append(float(wall_s))

    def fail(self, why: str) -> None:
        """规划结果等待超过上限（或其他不可恢复的批处理错误）：结束批处理，退出码 3。"""
        self.failed = str(why)


# 事件中的墙钟诊断量（不参与仿真，但值随机器负载变化）：批处理输出中置 null，使事件流可以逐字节对拍（D2-AC-32 第 1 轮）。
# 键为事件 kind，值为 data 中的字段名
WALL_FIELDS: dict[str, tuple[str, ...]] = {"geo.ready": ("load_ms",), "plan.ready": ("t_ms",), "plan.warm": ("t_ms",),
                                           "plan.failed": ("t_ms",), "plan.degraded": ("t_ms",)}


def _strip(ev: Any) -> Any:
    if isinstance(ev, dict) and "t_wall_ns" in ev:
        ev = dict(ev)
        ev["t_wall_ns"] = None
        keys = WALL_FIELDS.get(str(ev.get("kind")))
        data = ev.get("data")
        if keys and isinstance(data, dict) and any(k in data for k in keys):
            ev["data"] = {k: (None if k in keys else v) for k, v in data.items()}
    return ev


def _event_lines(raws: list[bytes]) -> list[bytes]:
    """事件批（msgpack 数组）→ events.jsonl 的行（键排序、紧凑分隔，`t_wall_ns` 置 null）。"""
    return [json.dumps(_strip(e), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode() + b"\n"
            for raw in raws for e in msgpack.unpackb(raw, raw=False, strict_map_key=False)]


def run_batch(until_s: float, config: dict, *, seed: int | None = None, out: Path | None = None,
              clock: str | None = None, world: str | None = None, plugins: tuple[str, ...] | None = None,
              kernel: str | None = None, load_world: bool = True, progress_s: float = 3600.0) -> dict:
    """运行一次批处理；返回 summary（含 `exit_code`）。结果文件写到 `out`（None 时不写文件，测试用）。"""
    from awr.contracts import LAYOUT_ID, bus_keys
    from awr.runtime.bus import LocalBus
    from awr.runtime.statering import LocalRing

    from ..fleet.fleet import FleetConfig
    from .config import SimConfig
    from .main import SimCore

    prof = CP.get(clock or os.environ.get("AWR_CLOCK_PROFILE") or "sandbox100")
    world_id = str(world or config.get("world_id") or "synthcity")
    seed_v = int(seed if seed is not None else config.get("seed", 0) or 0)
    fc = FleetConfig.from_env(clock_profile=prof.name)
    if kernel:
        from dataclasses import replace

        fc = replace(fc, kernel=kernel)
    run_id = "batch" + secrets.token_hex(3)
    over: dict[str, Any] = {"world_id": world_id, "run_id": run_id, "n_vehicles": 0, "autoplay": False, "fleet": fc,
                            "world_seed": seed_v, "scenario": None, "load_world": bool(load_world)}
    if plugins is not None:
        over["plugins"] = tuple(plugins)
    cfg = SimConfig.from_env(**over)
    path = f"/batch/{run_id}/state.sim-core"
    ring, _ = LocalRing.open_or_create(path, capacity=fc.capacity, slots=16, layout_id=LAYOUT_ID, id_base=0, id_count=1024)
    bus = LocalBus.open("sim-core", namespace=f"awr/batch/{run_id}")
    W = [1_000_000_000]
    raw_events: list[bytes] = []  # PY-CB-01：回调只入队，结束时统一解码为 events.jsonl 的行
    events: list[bytes] = []
    for cat in bus_keys.EVENT_CATEGORIES:
        bus.subscribe(bus_keys.evt("sim-core", cat), lambda k, raw: raw_events.append(raw))
    core = SimCore(cfg, bus, ring, secret=None, wall_ns=lambda: W[0])
    ctl = BatchCtl(prof)
    core.batch = ctl
    core.ctx.batch = ctl  # 启动与会话装配期间提交的规划作业同样按固定延迟生效（M10 PlanPoolClient.batch_of）
    wall0 = time.monotonic()
    cpu0 = time.process_time()
    summary: dict[str, Any] = {"schema": "awr.sim.batch_summary.v1", "clock_profile": prof.name, "world_id": world_id,
                               "seed": seed_v, "until_s": float(until_s)}
    try:
        core.start()
        core.ctx.batch = ctl
        if core.fleet.tap is not None:
            core.fleet.tap.enabled = False  # 不写 StateRing 槽（只写头部时钟组）
        core.slow.tasks = [t for t in core.slow.tasks if t.owner == "plugin" or t.name in KEEP_SLOW]
        core.slow.cap_us = 1e9
        probs = core.session.apply(dict(config, seed=seed_v))  # --seed 覆盖配置中的种子
        core.clock.apply("play")
        clk = core.clock
        tick_ns = clk.tick_ns
        end_tick = math.ceil(float(until_s) * 1e9 / tick_ns)
        k_slice = max(1, round(SLICE_S * 1e9 / tick_ns))
        next_hb = 0
        next_prog = progress_s * 1e9
        while clk.tick < end_tick and ctl.failed is None:
            core._drain()
            n = min(k_slice, end_tick - clk.tick)
            core.ctx.tick = clk.tick
            core.fleet.step(core.ctx, n)
            clk.tick = core.ctx.tick
            W[0] += n * tick_ns
            core.events.flush()
            core.slow.run(1e9, now_wall=W[0], now_sim=clk.t_ns)
            core.events.flush()
            if clk.t_ns >= next_hb:
                ring.heartbeat(clk.t_ns, clk.state, clk.rate_milli, step_seq=clk.tick)
                next_hb = clk.t_ns + 1_000_000_000
            if clk.t_ns >= next_prog:
                log.info("batch progress", extra={"kv": {"t_sim_h": round(clk.t_ns / 3.6e12, 2),
                                                          "wall_s": round(time.monotonic() - wall0, 1)}})
                next_prog += progress_s * 1e9
        wall = time.monotonic() - wall0
        # 结果文件先于事件解码：登记的结果函数可在结束时闭合进行中的统计并发出事件（M20 relay.gap，D2-FX-R2）
        outputs: dict[str, Any] = {}
        for name, fn in core.reg.batch_outputs.items():
            try:
                outputs[name] = fn(core.ctx)
            except Exception as e:
                log.exception("batch output failed", extra={"kv": {"output": name}})
                outputs[name] = {"error": f"{type(e).__name__}: {e}"}
        outputs.setdefault("relay_stats", {"v": 1, "items": []})
        core.events.flush()
        events = _event_lines(raw_events)
        pl = core.fleet.pipeline
        t_sim_s = clk.t_ns / 1e9
        stage_ms = {}
        for name, st in pl.stats.items():
            stage_ms[name] = round(st.ns / 1e6 / max(t_sim_s, 1e-9), 4)
        summary.update({"exit_code": EXIT_PLAN_WAIT if ctl.failed else EXIT_OK, "failed": ctl.failed,
                        "t_sim_s": round(t_sim_s, 3), "ticks": int(clk.tick), "wall_s": round(wall, 3),
                        "cpu_s": round(time.process_time() - cpu0, 3), "rtf_avg": round(t_sim_s / max(wall, 1e-9), 3),
                        "stage_ms_per_sim_s": stage_ms, "config_problems": probs, "n_events": len(events),
                        "n_uav": len(core.roster.by_slot),
                        "n_target": len(core.entities["target"]) if core.entities is not None else 0,
                        "plan_wait_s": {k: {"n": len(v), "max": round(max(v), 3)} for k, v in ctl.waits.items() if v}})
        if out is not None:
            out.mkdir(parents=True, exist_ok=True)
            (out / "events.jsonl").write_bytes(b"".join(events))
            for name, val in outputs.items():
                (out / f"{name}.json").write_text(json.dumps(val, ensure_ascii=False, sort_keys=True, indent=1) + "\n",
                                                  encoding="utf-8")
            (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        summary["_events"] = events
        summary["_outputs"] = outputs
        summary["_core"] = core
        return summary
    finally:
        core.stop("batch")
        bus.close()
        LocalRing.remove(path)


def batch_main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m awr.sim.runtime --batch")
    ap.add_argument("--batch", action="store_true")
    ap.add_argument("--until", type=float, required=True, help="仿真秒")
    ap.add_argument("--config", required=True, help="session.json（awr.sandbox.snapshot.v1）")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--clock", default=None, choices=sorted(CP.PROFILES))
    ap.add_argument("--world", default=None)
    ap.add_argument("--no-world", action="store_true", help="不加载世界几何（水平地面，测试用）")
    a = ap.parse_args(argv)
    try:
        cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(json.dumps({"code": 2, "reason": "CONFIG_UNREADABLE", "detail": str(e)}, ensure_ascii=False), file=sys.stderr)
        return EXIT_CONFIG
    out = Path(a.out) if a.out else ROOT / "runs" / "batch" / time.strftime("%Y%m%dT%H%M%S")
    s = run_batch(a.until, cfg, seed=a.seed, out=out, clock=a.clock, world=a.world, load_world=not a.no_world)
    print(json.dumps({k: v for k, v in s.items() if not k.startswith("_") and k != "stage_ms_per_sim_s"},
                     ensure_ascii=False))
    return int(s["exit_code"])
