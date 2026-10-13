#!/usr/bin/env python3
"""tools/bench/sandbox/capacity.py：并发容量（WP-13，M16；D2-AC-04，并提供 D2-AC-02、05、07 的观测；AWR-04 §4.4、ADR-091）。

cgroup 模拟（单元 CPUQuota 250%（Q-D2-01 维持 200% 时 `--cpu-quota 200`）、MemoryHigh 1.8 GB、MemoryMax 2 GB、4 核 cpuset、
§4.4.4 子组，见 `_sbbench.Stack`）下运行三个变体，各 `--minutes`（缺省 30）分钟：

| 变体 | 负载 | 期望 |
|---|---|---|
| A | 共享展示（`--show-scenario`，S7 接线后为 `s7-synthcity-relay`）+ N_max 个 synthcity 沙盒（各 10 架飞行 + 30 识别物 + 1 个接力，每 60 s 触发一次规划 dry-run，×1）+ `ws_max_show` 个 viewer + 每沙盒 2 个 operator 客户端（实时流 + REST 按配额 50%） | 下列全部判据 |
| B | sanfrancisco 与 shanghai 两个沙盒，再请求第 3 个（chicago）；世界内存额度为测试配置（`variant_b_budget`，D2-FX-R2：实测成本表下按生产额度第 3 个本应准入） | 前两个准入，第 3 个 508（世界加权准入），回复给出可立即开始的世界 |
| C | N_max − 1 个会话 ×1，另 1 个会话申请 ×5 | 授予满足治理器，全部会话 RTF ≥ 0.99（×5 会话按授予值） |
| D（D2.1，WP-13b） | A + 全部 viewer 订阅声学场与感知体并读取一个识别物的逐对读数 + 每个沙盒的 operator 订阅同样的场与读数 + 2 个驻留会话（先建后驻留，不占槽位）+ 每 2 min 一次的 viewer 节能与恢复循环（错开断开 30 s 再连）+ 容量接口 10 次/s（测时延）+ 可选内部档会话（`--internal`，预留槽位） | D2-AC-55：A 的全部判据 + 下列 |

判据（D2-AC-04）：全部会话 RTF ≥ 0.99（10 s 窗口）；沙盒单步 p99 ≤ 8 ms；共享展示单步 p99 ≤ 3 ms 且 `core/cpu.stat` 的
`nr_throttled` 增量 0；sandbox-api 事件循环延迟 p99 ≤ 20 ms（hub `lag_ms`）；每个 api 进程 CPU ≤ 0.7 核；匿名页 + shmem 峰值
≤ 1.25 GB 且 `memory.current` ≤ MemoryHigh；OOM 与非预期重启 0。

D2-AC-55 的增量判据（变体 D）：容量接口 p95 ≤ 50 ms 且所致 sandbox-api CPU ≤ 0.01 核（10 次/s 与空载对照各 60 s 的差，
本机直连 sandbox-api，无 nginx 微缓存，偏保守）；驻留会话不在进程表中、容量 `parked` = 2、驻留目录合计 ≤ 16 MB；内部档会话占用预留槽位期间
降级级别 ≥ L2 的连续时长 ≤ 10 s；场与读数在每个会话 ≤ 2 ms/仿真秒、共享展示 ≤ 3 ms/仿真秒（读 sim-core 的 perf 自报
`slow_ms_per_s.m19.field`；自报缺该项时记为不可判定）。

用法：
    python tools/bench/sandbox/capacity.py --variant A                 # 30 min（排他锁、负载 ≤ 4）
    python tools/bench/sandbox/capacity.py --variant D --internal      # D2-AC-55（D2.1）
    python tools/bench/sandbox/capacity.py --variant all --minutes 30
    python tools/bench/sandbox/capacity.py --variant A --minutes 1 --sessions 1 --viewers 2 --smoke   # 功能自检（不判定）
输出 `runs/perf/capacity-<UTC>/<变体>/`（`samples.jsonl`、`summary.json`）与 `report.json`（`awr.bench.capacity.v1`）；
退出码 0 通过、1 不通过、14 环境不满足。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import itertools
import json
import random
import sys
import threading
import time
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE.parents[2] / "tools" / "bench" / "ipc", _HERE.parents[2] / "tools" / "sandbox"):
    sys.path.insert(0, str(_p))
import _common as PERF  # noqa: E402
import _sbbench as B  # noqa: E402
import sbcommon as C  # noqa: E402

SCHEMA = "awr.bench.capacity.v1"
EXIT_OK, EXIT_FAIL, EXIT_ENV = 0, 1, 14
GATES = {"rtf_min": 0.99, "sbx_step_p99_us": 8000, "show_step_p99_us": 3000, "api_cpu": 0.7, "lag_p99_ms": 20.0,
         "anon_shmem_mb": 1250.0, "mem_high_mb": 1800.0}


# ---------------------------------------------------------------- 负载
def capacity_config(world: str = "synthcity", *, seed: int = 21) -> dict[str, Any]:
    """满载会话（AWR-04 §4.4.1"每会话 12 架 + 30 识别物"的 D2-AC-04 口径：10 架飞行 + 30 识别物 + 1 个接力）。"""
    a = C.load_json(C.ANCHORS / f"{world}.json")
    rnd = random.Random(seed)
    sens = [{"kind": "visual", "shape": "sector", "radius_max_m": 60, "z_min_m": 0, "z_max_m": 60, "half_angle_deg": 60,
             "azimuth_offset_deg": 0, "gain_back_db": 0, "enabled": True},
            {"kind": "acoustic", "shape": "hemisphere", "radius_max_m": 40, "z_min_m": 0, "z_max_m": 40, "half_angle_deg": 180,
             "azimuth_offset_deg": 0, "gain_back_db": 0, "enabled": True}]
    targets = [{"id": "person-01", "class": "person", "pos": [*a["target_static"]["pos"], None], "yaw_rad": a["target_static"]["yaw_rad"],
                "motion": {"mode": "static"}, "sensitivity": sens}]
    regs = a["walk_regions"]
    for k in range(2, 31):
        reg = regs[k % len(regs)]
        x0, y0 = min(p[0] for p in reg), min(p[1] for p in reg)
        x1, y1 = max(p[0] for p in reg), max(p[1] for p in reg)
        targets.append({"id": f"tg-{k:02d}", "class": "person" if k % 3 else "dog",
                        "pos": [round(rnd.uniform(x0 + 5, x1 - 5), 1), round(rnd.uniform(y0 + 5, y1 - 5), 1), None],
                        "motion": {"mode": "random_walk", "region": reg}, "sensitivity": sens})
    nest = {"id": "nest-a", "name": "机巢箱 A", "pos": list(a["nest_box"]), "kind": "box", "slots": 12,
            "energy": {"mode": "swap", "swap_s": 180, "packs": [{"cells": 6, "count": 6, "capacity_ah": 10}]},
            "chargers": [{"model": "c1_xr", "count": 6, "channels": 1, "i_max_a": 10, "cells_max": 6}], "link_range_m": 3500}
    return {"schema": "awr.sandbox.snapshot.v1", "contracts": C.CONTRACTS_VERSION, "world_id": world, "template_id": None,
            "calendar": {"start_utc": "2026-10-05T02:00:00Z", "tz": C.TZ[world]}, "seed": seed, "acoustic_bg": "urban",
            "nests": [nest], "vehicles": [{"id": f"p600-{k:02d}", "model_id": "p600_mid360", "nest_id": "nest-a"} for k in range(1, 11)],
            "targets": targets,
            "tasks": [{"type": "area_patrol", "label": "巡查", "params": {"polygon": a["aoi_guard"], "n_vehicles": 6, "target_class": "person",
                                                                       "level_req": "D", "revisit_s": 300}, "common": {"on_detect": "notify"}},
                      {"type": "relay_watch", "label": "接力", "params": {"target": "person-01", "level_req": "R"}, "common": {}},
                      {"type": "polyline_patrol", "label": "巡线", "params": {"waypoints": a["polyline"], "mode": "loop", "alt_m": 70}, "common": {}}]}


class Operators:
    """每沙盒 2 个 operator 客户端：实时流（会话、机体、识别物、感知 topic）与 REST 轮询（配额 50%：5 次/s）+ 每 60 s 一次规划 dry-run。"""

    def __init__(self, sessions: list[Any], rest_rps: float = 5.0) -> None:
        self.sessions = sessions
        self.rest_rps = rest_rps
        self.stop_ev = threading.Event()
        self.counts: dict[str, int] = {"rest": 0, "rest_err": 0, "stream_msgs": 0, "dry_runs": 0}
        self.threads: list[threading.Thread] = []

    def start(self) -> Operators:
        for s in self.sessions:
            for fn in (self._stream, self._rest):
                t = threading.Thread(target=fn, args=(s,), daemon=True)
                t.start()
                self.threads.append(t)
        return self

    def _stream(self, s: Any) -> None:
        topics = ["sandbox/session", "swarm/target/state", "uav/p600-01/perception", "nest/nest-a/status"]
        while not self.stop_ev.is_set():
            try:
                for _m in s.stream(topics, seconds=20):
                    self.counts["stream_msgs"] += 1
                    if self.stop_ev.is_set():
                        return
            except Exception:
                time.sleep(1.0)

    def _rest(self, s: Any) -> None:
        last_plan = 0.0
        a = C.load_json(C.ANCHORS / f"{s.world or 'synthcity'}.json")
        paths = [s.path("vehicles"), s.path("targets"), s.path("tasks"), s.path("vehicles", "p600-01", "state"), s.path()]
        k = 0
        while not self.stop_ev.is_set():
            t0 = time.monotonic()
            try:
                s.sb.http.get(paths[k % len(paths)], token=s.token)
                self.counts["rest"] += 1
            except Exception:
                self.counts["rest_err"] += 1
            k += 1
            if t0 - last_plan >= 60.0:
                last_plan = t0
                with contextlib.suppress(Exception):
                    s.tasks.create({"type": "area_scan_gsd", "params": {"polygon": a["aoi_scan"], "gsd_cm": 3.0}}, dry_run=True)
                    self.counts["dry_runs"] += 1
            self.stop_ev.wait(max(0.0, 1.0 / self.rest_rps - (time.monotonic() - t0)))

    def stop(self) -> dict[str, int]:
        self.stop_ev.set()
        for t in self.threads:
            t.join(25)
        return dict(self.counts)


# 共享展示页默认视图中真实浏览器的订阅集（D2-FX-R2-sandbox 第 2 次执行，ADR-174）：测试构建打开 `/world/synthcity`（S7、未选中机体、
# 未打开面板），揭开 20 s 后经 R58 `/api/rt/inspect` 采集，共 13 条；来源 `viewport/layers/drones.tsx`（event、perf/server、sys/procs、
# fleet/roster、swarm/state、env/state）、`stores/targets.ts`、`stores/tasking.ts`、`stores/mission.ts`。swarm/uav/state 按 Tier：
# B 与 A 为 20 Hz（桌面，AWR-04 §15.7.8 的"桌面 Tier B"），S 为 10 Hz；默认视图的关注集为空（机体 r_px < 8）。
BROWSER_SUBS: tuple[tuple[str, int, str], ...] = (
    ("nest/*/status", 1, "latest"), ("task/*/status", 1, "latest"), ("relay/*/stats", 1, "latest"),
    ("swarm/target/state", 10, "latest"), ("target/*/detail", 1, "latest"), ("event", 0, "all"), ("perf/server", 1, "latest"),
    ("sys/procs", 1, "latest"), ("fleet/roster", 10, "latest"), ("swarm/uav/state", 20, "latest"), ("env/state", 10, "latest"),
    ("mission/*/status", 2, "latest"), ("uav/*/path", 2, "latest"))
# viewer 口径：hello = 只握手不订阅（TIME 与事件，D2-AC-04 / 39 / 55 沿用）；browser = 桌面 Tier B 浏览器；browser-s = 本机 Tier S 浏览器
VIEWER_PROFILES = {"hello": None, "browser": 20, "browser-s": 10}


def browser_subs(swarm_hz: int) -> list[dict[str, Any]]:
    return [{"id": i + 1, "topic": t, "rate": swarm_hz if t == "swarm/uav/state" else r, "mode": m} for i, (t, r, m) in enumerate(BROWSER_SUBS)]


class Viewers:
    """共享展示的 viewer（`awr.rt.v1`，show-api `/api/rt`）。`profile`（`VIEWER_PROFILES`）：hello 由 rtprobe 收帧并逐帧 ack；browser 与
    browser-s 订阅 `BROWSER_SUBS` 并按网页客户端的节律收发（`apps/web/src/net/rt/session.ts`：BATCH 每 3 帧或距上次 ack ≥ 34 ms 时 ack，
    ping 每 500 ms，clientStats 每 1 s；只解析帧头，记录在浏览器 worker 内解码，与服务端成本无关）。`batches` 只计 BATCH 帧。"""

    def __init__(self, stack: B.Stack, n: int, profile: str = "hello") -> None:
        if profile not in VIEWER_PROFILES:
            raise ValueError(f"viewer profile {profile!r} (expect one of {sorted(VIEWER_PROFILES)})")
        self.stack, self.n, self.profile = stack, n, profile
        self.loop = asyncio.new_event_loop()
        self.frames = 0
        self.batches = 0
        self.errors = 0
        self._stop = False
        self.t = threading.Thread(target=self._run, daemon=True)

    def start(self) -> Viewers:
        self.t.start()
        return self

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(asyncio.gather(*(self._one(k) for k in range(self.n)), return_exceptions=True))

    async def _one(self, k: int) -> None:
        try:
            from rtprobe import Probe  # tests/chaos/rtprobe.py（M16 的最小 awr.rt.v1 客户端）
        except ImportError:
            self.errors += 1
            return
        await asyncio.sleep(0.2 * k)
        swarm_hz = VIEWER_PROFILES[self.profile]
        while not self._stop:
            p = None
            try:
                p = await Probe.open(self.stack.show_base, self.stack.viewer_token(k))
                if swarm_hz is not None:
                    await self._browser(p, swarm_hz)
                while not self._stop:
                    kind, _ = await p.recv(10.0)
                    self.frames += 1
                    self.batches += kind == "batch"
            except Exception:
                if not self._stop:
                    self.errors += 1
                    await asyncio.sleep(2.0)
            finally:
                if p is not None:
                    with contextlib.suppress(Exception):
                        await p.ws.close()

    async def _browser(self, p: Any, swarm_hz: int) -> None:
        from awr.contracts import frame as F

        await p.send({"op": "subscribe", "subs": browser_subs(swarm_hz)})
        last_ack = 0
        t_ack = t_ping = t_stats = time.monotonic()
        while not self._stop:
            m = await asyncio.wait_for(p.ws.recv(), 10.0)
            now = time.monotonic()
            self.frames += 1
            if isinstance(m, (bytes, bytearray)) and m[0] == F.OP_BATCH:
                self.batches += 1
                seq = F.decode_batch_header(m).frame_seq
                if seq > last_ack and (seq - last_ack >= 3 or now - t_ack >= 0.034):
                    await p.send({"op": "ack", "frame": seq, "fps": 60.0, "decodeMs": 0.05})
                    last_ack, t_ack = seq, now
            if now - t_ping >= 0.5:
                await p.send({"op": "ping", "t": round(now * 1e3, 3), "srttMs": 1.0})
                t_ping = now
            if now - t_stats >= 1.0:
                await p.send({"op": "clientStats", "fps": 60.0, "decodeMs": 0.05, "tier": "B" if swarm_hz >= 20 else "S",
                              "deviceClass": "bench"})
                t_stats = now

    def stop(self) -> dict[str, int]:
        self._stop = True
        self.t.join(15)
        return {"frames": self.frames, "batches": self.batches, "errors": self.errors}


# ---------------------------------------------------------------- 变体
def sandbox(stack: B.Stack, k: int) -> Any:
    import awr_sandbox as SDK

    hint = ("MSIXCAPACITY" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[k % 26] + "AAAAAAAAAAAAAAA")[:24]
    return SDK.Sandbox(stack.base, principal_hint=hint, auto_keepalive=True, timeout_s=30.0)   # 长轮询 wait_s 10 s（见 d2-WP-13-to-M17）


def variant_a(stack: B.Stack, out: Path, minutes: float, n_sessions: int | None, viewers: int) -> dict[str, Any]:
    n_max = int(stack_health(stack).get("n_max") or 1)
    n = n_sessions or n_max
    sessions = []
    t_create = []
    for k in range(n):
        t0 = time.monotonic()
        s = sandbox(stack, k).create("synthcity", snapshot=capacity_config(seed=21 + k))
        s.wait_ready(120)
        t_create.append(round(time.monotonic() - t0, 2))
        s.clock("speed", rate=1)
        sessions.append(s)
    smp = B.Sampler(stack, out).start()
    ops = Operators(sessions).start()
    vw = Viewers(stack, viewers).start()
    time.sleep(minutes * 60.0)
    o, v = ops.stop(), vw.stop()
    samples = smp.stop()
    for s in sessions:
        with contextlib.suppress(Exception):
            s.close()
    summ = B.summarize(samples)
    return {"n_max": n_max, "n_sessions": n, "create_ready_s": t_create, "operators": o, "viewers": v, "summary": summ}


B_PAIR = ("sanfrancisco", "shanghai")
B_THIRD = "chicago"


def variant_b_budget(table_path: Path | None = None, m_sb_prod: float = 1100.0) -> dict[str, Any]:
    """变体 B 的世界内存额度（D2-FX-R2）。AWR-04 §4.4.2 的组合表（sanfrancisco + shanghai 准入、第 3 个 508）基于初值成本表
    （ws 25–350 MB）；入库的实测表（`configs/sandbox/world_costs.json`，D2-AC-28）中 L 级世界的文件页工作集只有 5–13 MB，按生产额度
    1100 MB 第 3 个会话本应被准入。为了在实测表上仍检验"世界加权准入拒绝超额世界并给出可立即开始的世界"，以测试配置的额度
    `AWR_SANDBOX_MEM_MB` 运行：前两个会话之和加上"最小世界与第 3 个世界成本的中点"，使第 3 个（chicago）超额而较小的世界仍可开始；
    生产额度下的结论另行记录（`natural_third_admitted`）。"""
    import json as _json

    p = table_path or (B.ROOT / "configs" / "sandbox" / "world_costs.json")
    rows = {w["world"]: w for w in _json.loads(p.read_text(encoding="utf-8")).get("worlds") or []}

    def cost(w: str) -> float:
        r = rows.get(w) or {}
        return float(r.get("anon_mb", 155.0)) + float(r.get("ws_mb", 350.0))

    two = sum(cost(w) for w in B_PAIR)
    third = cost(B_THIRD)
    others = [cost(w) for w in rows if w not in (*B_PAIR, B_THIRD)]
    lo = min(others) if others else third / 2.0
    natural = two + third <= m_sb_prod
    m_test = round(two + ((lo + third) / 2.0 if lo < third else third / 2.0), 1) if natural else m_sb_prod
    return {"m_sb_prod": m_sb_prod, "m_sb_test": m_test, "pair_mb": round(two, 1), "third_mb": round(third, 1),
            "smallest_other_mb": round(lo, 1), "natural_third_admitted": natural, "table": str(p)}


def variant_b(stack: B.Stack, out: Path, budget: dict[str, Any] | None = None) -> dict[str, Any]:
    import awr_sandbox as SDK

    got = []
    res: dict[str, Any] = {"budget": budget}
    for k, w in enumerate(B_PAIR):
        try:
            s = sandbox(stack, 10 + k).create(w, C.T5)
            got.append(s)
        except SDK.AwrError as e:
            res[f"create_{w}"] = {"code": e.code, "detail": e.detail}
    try:
        s3 = sandbox(stack, 12).create(B_THIRD, C.T5, queue=False)
        got.append(s3)
        res["third"] = {"code": 0}
    except SDK.AwrError as e:
        res["third"] = {"code": e.code, "detail": e.detail}
    for s in got:
        with contextlib.suppress(Exception):
            s.close()
    return res


def variant_c(stack: B.Stack, out: Path, minutes: float) -> dict[str, Any]:
    n_max = int(stack_health(stack).get("n_max") or 1)
    sessions = []
    for k in range(max(1, n_max)):
        last = k == n_max - 1
        s = sandbox(stack, 20 + k).create("synthcity", C.T2 if last else None, snapshot=None if last else capacity_config(seed=40 + k))
        s.wait_ready(120)
        sessions.append(s)
    grants = [s.clock("speed", rate=1) for s in sessions[:-1]]
    grants.append(sessions[-1].clock("speed", rate=5))
    # D2.1（ADR-121）：无连接且无调用的会话 60 s 后进入 IDLE、再 120 s 驻留；变体 C 没有 operator 客户端，测量期间每 20 s 对每个会话发一次
    # 会话内 REST 调用保持活动（D2-ACC-1 工具层最小修复：此前会话在测量开始约 3 min 后被驻留，变体 C 的结果无效）
    stop_ka = threading.Event()

    def keep_active() -> None:
        while not stop_ka.wait(20.0):
            for s in sessions:
                with contextlib.suppress(Exception):
                    s.sb.http.get(s.path("vehicles"), token=s.token)

    ka = threading.Thread(target=keep_active, daemon=True)
    ka.start()
    smp = B.Sampler(stack, out).start()
    time.sleep(minutes * 60.0)
    samples = smp.stop()
    stop_ka.set()
    ka.join(30)
    for s in sessions:
        with contextlib.suppress(Exception):
            s.close()
    return {"n_max": n_max, "grants": grants, "summary": B.summarize(samples)}


# ---------------------------------------------------------------- 变体 D（D2.1，D2-AC-55）
FIELD_TOPICS = ["field/acoustic", "field/volumes", "target/person-01/quant"]


class FieldViewers(Viewers):
    """共享展示的 viewer：订阅声学场、感知体与一个识别物的逐对读数；每 `cycle_s` 一次节能与恢复（断开 `off_s` 后重连，各 viewer 错开）。"""

    def __init__(self, stack: B.Stack, n: int, *, cycle_s: float = 120.0, off_s: float = 30.0) -> None:
        super().__init__(stack, n)
        self.cycle_s, self.off_s = cycle_s, off_s
        self.cycles = 0
        self.field_msgs = 0

    async def _one(self, k: int) -> None:
        try:
            from rtprobe import Probe
        except ImportError:
            self.errors += 1
            return
        await asyncio.sleep(0.2 * k)
        phase = (k / max(1, self.n)) * self.cycle_s
        t_next_save = time.monotonic() + phase + self.cycle_s - self.off_s
        while not self._stop:
            p = None
            try:
                p = await Probe.open(self.stack.show_base, self.stack.viewer_token(k))
                await p.send({"op": "subscribe", "subs": [{"id": 900 + i, "topic": t, "rate": 1} for i, t in enumerate(FIELD_TOPICS)]})
                while not self._stop and time.monotonic() < t_next_save:
                    kind, x = await p.recv(10.0)
                    self.frames += 1
                    if kind == "json" and str((x or {}).get("topic", "")).startswith(("field/", "target/")):
                        self.field_msgs += 1
            except Exception:
                if not self._stop:
                    self.errors += 1
                    await asyncio.sleep(2.0)
                    continue
            finally:
                if p is not None:
                    with contextlib.suppress(Exception):
                        await p.ws.close(1000)
            if self._stop:
                return
            self.cycles += 1                                      # 节能：断开 off_s，交互后重连
            await asyncio.sleep(self.off_s)
            t_next_save = time.monotonic() + self.cycle_s - self.off_s

    def stop(self) -> dict[str, int]:
        r = super().stop()
        return {**r, "power_cycles": self.cycles, "field_msgs": self.field_msgs}


class CapacityProbe:
    """公开容量接口按固定频率请求（D2-AC-55：10 次/s），记录状态码与时延。"""

    def __init__(self, base: str, hz: float = 10.0) -> None:
        self.base, self.hz = base, hz
        self.lat_ms: list[float] = []
        self.bad = 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def start(self) -> CapacityProbe:
        self._t.start()
        return self

    def _run(self) -> None:
        import httpx

        with httpx.Client(timeout=5.0) as c:
            while not self._stop.is_set():
                t0 = time.monotonic()
                try:
                    r = c.get(self.base + "/api/sandbox/v1/capacity")
                    if r.status_code == 200:
                        self.lat_ms.append((time.monotonic() - t0) * 1e3)
                    else:
                        self.bad += 1
                except httpx.HTTPError:
                    self.bad += 1
                self._stop.wait(max(0.0, 1.0 / self.hz - (time.monotonic() - t0)))

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        self._t.join(10)
        lat = sorted(self.lat_ms)
        p95 = lat[min(len(lat) - 1, int(0.95 * len(lat)))] if lat else None
        return {"n": len(lat), "bad": self.bad, "p95_ms": None if p95 is None else round(p95, 2),
                "p50_ms": round(lat[len(lat) // 2], 2) if lat else None}


def proc_cpu_window(stack: B.Stack, name: str, seconds: float) -> float | None:
    """进程 `name`（supervisor 进程表）在 seconds 内的平均 CPU（核）。"""
    try:
        pid = next(int(p["pid"]) for p in stack.procs() if p.get("name") == name and p.get("pid"))
    except (StopIteration, Exception):
        return None
    c0, t0 = B.proc_cpu_s(pid), time.monotonic()
    time.sleep(seconds)
    c1, t1 = B.proc_cpu_s(pid), time.monotonic()
    if c0 is None or c1 is None:
        return None
    return round((c1 - c0) / (t1 - t0), 4)


def variant_d(stack: B.Stack, out: Path, minutes: float, n_sessions: int | None, viewers: int, *, internal: str | None = None,
              cap_window_s: float = 60.0) -> dict[str, Any]:
    import httpx

    res: dict[str, Any] = {}
    # 1 两个驻留会话（先建后驻留：驻留不占槽位，D2-AC-43 口径）
    parked = []
    for k in range(2):
        s = sandbox(stack, 30 + k).create("synthcity", C.T1, wait_ready=True)
        s.rt()
        time.sleep(2.0)
        s.park()
        parked.append(s)
    end = time.monotonic() + 30.0
    while time.monotonic() < end and int(httpx.get(stack.base + "/api/sandbox/v1/capacity", timeout=5).json()["sandbox"].get("parked") or 0) < 2:
        time.sleep(0.5)
    # 2 容量接口所致 CPU：空载与 10 次/s 各一个窗口
    idle = proc_cpu_window(stack, "sandbox-api", cap_window_s)
    cp0 = CapacityProbe(stack.base).start()
    busy = proc_cpu_window(stack, "sandbox-api", cap_window_s)
    pre = cp0.stop()
    res["capacity_cpu"] = {"idle": idle, "busy_10hz": busy, "delta": None if idle is None or busy is None else round(busy - idle, 4), "probe": pre}
    # 3 A 的满载 + 场与读数订阅 + viewer 节能循环 + 容量接口 10 次/s
    n_max = int(stack_health(stack).get("n_max") or 1)
    n = n_sessions or n_max
    sessions = []
    for k in range(n):
        s = sandbox(stack, k).create("synthcity", snapshot=capacity_config(seed=21 + k))
        s.wait_ready(120)
        s.clock("speed", rate=1)
        sessions.append(s)
    smp = B.Sampler(stack, out).start()
    ops = Operators(sessions).start()
    fops = []
    for s in sessions:                                           # operator 的场与读数订阅
        def run(s=s) -> None:
            with contextlib.suppress(Exception):
                for _m in s.stream(FIELD_TOPICS, seconds=minutes * 60.0):
                    pass
        t = threading.Thread(target=run, daemon=True)
        t.start()
        fops.append(t)
    vw = FieldViewers(stack, viewers).start()
    cp = CapacityProbe(stack.base).start()
    internal_s = None
    levels: list[tuple[float, str]] = []
    t_end = time.monotonic() + minutes * 60.0
    t_internal = time.monotonic() + minutes * 60.0 / 3.0
    while time.monotonic() < t_end:
        if internal and internal_s is None and time.monotonic() >= t_internal:
            with contextlib.suppress(Exception):
                internal_s = sandbox(stack, 40).create("synthcity", C.T6, identity_token=internal, queue=False)
        with contextlib.suppress(Exception):
            levels.append((time.monotonic(), str(httpx.get(stack.base + "/api/sandbox/v1/capacity", timeout=5).json().get("degrade_level"))))
        time.sleep(1.0)
    capr = cp.stop()
    o, v = ops.stop(), vw.stop()
    samples = smp.stop()
    # 驻留会话：不在进程表中、目录合计
    tags = B.session_pids(stack.tag)
    sbx = (stack.tmp / "sbx") if stack.tmp else None
    disk = sum(f.stat().st_size for f in (sbx / "parked").rglob("*") if f.is_file()) if sbx and (sbx / "parked").exists() else 0
    res["parked"] = {"sids": [s.sid for s in parked], "running": [s.sid for s in parked if s.sid in tags], "disk_bytes": disk}
    for s in [*sessions, *parked] + ([internal_s] if internal_s else []):
        with contextlib.suppress(Exception):
            s.close()
    # L ≥ 2 的连续时长（内部档占用预留槽位期间）
    run_s, worst = 0.0, 0.0
    for (t0, lv), (t1, _lv) in itertools.pairwise(levels):
        if lv in ("L2", "L3", "L4"):
            run_s += t1 - t0
            worst = max(worst, run_s)
        else:
            run_s = 0.0
    res.update({"n_max": n_max, "n_sessions": n, "operators": o, "viewers": v, "capacity": capr, "internal": bool(internal_s),
                "degrade_l2_run_s_max": round(worst, 1), "summary": B.summarize(samples), "field_cost": field_cost(samples)})
    return res


def field_cost(samples: list[dict]) -> dict[str, Any]:
    """sim-core perf 自报中的 M19 场服务成本（ms/仿真秒）；自报缺该项时为空（判定记为不可判定）。"""
    out: dict[str, list[float]] = {}
    for s in samples:
        for sid, d in (s.get("sessions") or {}).items():
            v = ((d.get("perf") or {}).get("slow_ms_per_s") or {}).get("m19.field")
            if v is not None:
                out.setdefault(sid, []).append(float(v))
    return {sid: round(max(v), 3) for sid, v in out.items()}


def judge_d(r: dict, *, cpu_valid: bool, cgroup_mode: str) -> list[dict]:
    out = judge_a(r, cpu_valid=cpu_valid, cgroup_mode=cgroup_mode)
    cap = r.get("capacity") or {}
    out.append(check("capacity.p95_ms", (cap.get("p95_ms") or 1e9) <= 50.0 if cpu_valid else None, cap.get("p95_ms"), "<= 50"))
    out.append(check("capacity.errors", cap.get("bad", 0) == 0, cap.get("bad"), 0))
    cc = r.get("capacity_cpu") or {}
    out.append(check("capacity.cpu_delta", (cc.get("delta") if cc.get("delta") is not None else 1.0) <= 0.01 if cpu_valid else None,
                     cc.get("delta"), "<= 0.01 core"))
    pk = r.get("parked") or {}
    out.append(check("parked.not_running", not pk.get("running"), pk.get("running"), "[]"))
    out.append(check("parked.disk_mb", (pk.get("disk_bytes") or 0) <= 16 * 1048576, round((pk.get("disk_bytes") or 0) / 1048576, 2), "<= 16"))
    if r.get("internal"):
        out.append(check("internal.l2_run_s", (r.get("degrade_l2_run_s_max") or 0) <= 10.0 if cpu_valid else None,
                         r.get("degrade_l2_run_s_max"), "<= 10 s"))
    vw = r.get("viewers") or {}
    out.append(check("viewers.power_cycles", (vw.get("power_cycles") or 0) > 0, vw.get("power_cycles"), "> 0"))
    fc = r.get("field_cost") or {}
    out.append(check("field_cost_ms_per_sim_s", (max(fc.values()) <= 2.0 if fc else None) if cpu_valid else None, fc or None,
                     "<= 2 per session (perf self-report m19.field)"))
    return out


def stack_health(stack: B.Stack) -> dict:
    import httpx

    return httpx.get(stack.base + "/api/sandbox/v1/health", timeout=5).json()


# ---------------------------------------------------------------- 判定
def check(name: str, ok: bool | None, value: Any, expect: Any) -> dict:
    return {"check": name, "ok": ok, "value": value, "expect": expect}


def judge_a(r: dict, *, cpu_valid: bool, cgroup_mode: str) -> list[dict]:
    s = r.get("summary") or {}
    out = []
    for sid, e in (s.get("sessions") or {}).items():
        out.append(check(f"{sid}.rtf_min", e["rtf_min"] >= GATES["rtf_min"] if cpu_valid else None, e["rtf_min"], f">= {GATES['rtf_min']}"))
        out.append(check(f"{sid}.step_p99_us", e["step_p99_us_max"] <= GATES["sbx_step_p99_us"] if cpu_valid else None,
                         e["step_p99_us_max"], f"<= {GATES['sbx_step_p99_us']}"))
        out.append(check(f"{sid}.epoch_stable", len(e["epochs"]) <= 1, e["epochs"], "single epoch (no restart)"))
    sh = s.get("show") or {}
    out.append(check("show.rtf_min", sh.get("rtf_min", 0) >= GATES["rtf_min"] if cpu_valid else None, sh.get("rtf_min"), ">= 0.99"))
    out.append(check("show.step_p99_us", sh.get("step_p99_us_max", 0) <= GATES["show_step_p99_us"] if cpu_valid else None,
                     sh.get("step_p99_us_max"), "<= 3000"))
    for name in ("api", "sandbox-api"):
        p = (s.get("procs") or {}).get(name) or {}
        out.append(check(f"{name}.cpu_mean", (p.get("cpu_mean") or 0) <= GATES["api_cpu"] if cpu_valid else None, p.get("cpu_mean"), "<= 0.7"))
    out.append(check("sandbox_api.lag_p99_ms", (s.get("sandbox_api_lag_ms_p99") or 0) <= GATES["lag_p99_ms"] if cpu_valid else None,
                     s.get("sandbox_api_lag_ms_p99"), "<= 20"))
    restarts = {n: p.get("restarts") for n, p in (s.get("procs") or {}).items() if p.get("restarts")}
    out.append(check("unexpected_restarts", not restarts, restarts, 0))
    cg = s.get("cgroup")
    if cg is None:
        out.append(check("cgroup", None, cgroup_mode, "systemd-scope (D2-AC-04 needs the cgroup simulation)"))
    else:
        out.append(check("core.nr_throttled_delta", cg["core_nr_throttled_delta"] == 0, cg["core_nr_throttled_delta"], 0))
        out.append(check("anon_shmem_mb_max", cg["anon_shmem_mb_max"] <= GATES["anon_shmem_mb"], cg["anon_shmem_mb_max"], "<= 1250"))
        out.append(check("memory_current_mb_max", cg["unit_memory_current_mb_max"] <= GATES["mem_high_mb"], cg["unit_memory_current_mb_max"],
                         "<= MemoryHigh 1800"))
        out.append(check("oom_kill", cg["oom_kill"] == 0, cg["oom_kill"], 0))
    return out


def judge_b(r: dict) -> list[dict]:
    third = r.get("third") or {}
    alt = ((third.get("detail") or {}) if isinstance(third.get("detail"), dict) else {}).get("available_now")
    pair_ok = not any(f"create_{w}" in r for w in B_PAIR)
    return [check("pair_admitted", pair_ok, [r.get(f"create_{w}") for w in B_PAIR], "sanfrancisco + shanghai admitted"),
            check("third_world_admission_508", third.get("code") == 508, third.get("code"), 508),
            check("508_lists_available_worlds", bool(alt) if third.get("code") == 508 else None, alt, "non-empty")]


def judge_c(r: dict, *, cpu_valid: bool) -> list[dict]:
    out = []
    g = r.get("grants") or []
    if g:
        last = g[-1]
        out.append(check("ff_grant_reported", "granted_rate" in last, last.get("granted_rate"), "granted_rate"))
    for sid, e in ((r.get("summary") or {}).get("sessions") or {}).items():
        out.append(check(f"{sid}.rtf_min", e["rtf_min"] >= GATES["rtf_min"] if cpu_valid else None, e["rtf_min"], ">= 0.99 of the grant"))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tools/bench/sandbox/capacity.py")
    ap.add_argument("--variant", choices=("A", "B", "C", "D", "all"), default="A")
    ap.add_argument("--minutes", type=float, default=30.0)
    ap.add_argument("--cpu-quota", type=int, default=250, help="单元 CPUQuota（%%）")
    ap.add_argument("--sessions", type=int, default=None, help="A：会话数（缺省 N_max）")
    ap.add_argument("--viewers", type=int, default=24, help="ws_max_show")
    ap.add_argument("--show-scenario", default=None, help="共享展示剧本（S7 接线后 s7-synthcity-relay）")
    ap.add_argument("--no-cgroup", action="store_true")
    ap.add_argument("--internal", action="store_true", help="D：以运行时随机口令的临时密钥文件启用内部身份，运行三分之一后创建内部档会话")
    ap.add_argument("--cap-window", type=float, default=60.0, help="D：容量接口 CPU 对照窗口（s）")
    ap.add_argument("--smoke", action="store_true", help="功能自检：不取锁、不等负载、不判定")
    ap.add_argument("--no-lock", action="store_true")
    ap.add_argument("--no-load-wait", action="store_true")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    if not (B.ROOT / "worlds" / "synthcity" / "world.json").exists():
        print("worlds/synthcity not built", file=sys.stderr)
        return EXIT_ENV
    lock = None if a.smoke else PERF.acquire_perf_lock(a.no_lock)
    if not a.smoke and not PERF.wait_load(a.no_load_wait):
        return EXIT_ENV
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = Path(a.out) if a.out else B.ROOT / "runs" / "perf" / f"capacity-{ts}"
    out.mkdir(parents=True, exist_ok=True)
    variants = ("A", "B", "C", "D") if a.variant == "all" else (a.variant,)
    report: dict[str, Any] = {"schema": SCHEMA, "run_id": f"capacity-{ts}", "git_sha": PERF.git_sha(), "host": PERF.host_info(),
                              "gates": GATES, "smoke": a.smoke, "variants": []}
    ok_all = True
    for v in variants:
        d = out / v
        d.mkdir(parents=True, exist_ok=True)
        env: dict[str, str] = {}
        ident = None
        if v == "D" and a.internal:
            sys.path.insert(0, str(B.ROOT / "tests" / "fixtures"))
            import identity as idfx  # 运行时随机口令（tests/fixtures/identity.py）

            sf = idfx.secret_file(d / "identity")
            env.update(sf.env())
            ident = idfx.issue_token(sf)[0]
        bud = None
        if v == "B":
            bud = variant_b_budget()
            env["AWR_SANDBOX_MEM_MB"] = str(bud["m_sb_test"])     # 测试配置的世界内存额度（见 variant_b_budget）
        stack = B.Stack(cgroup=not a.no_cgroup, cpu_quota_pct=a.cpu_quota, show_scenario=a.show_scenario, ws_max_show=max(a.viewers, 1),
                        env=env, max_sessions="auto" if v != "D" or a.sessions is None else max(a.sessions, 1))
        load = PERF.LoadSampler()
        try:
            stack.start()
            if v == "A":
                r = variant_a(stack, d, a.minutes, a.sessions, a.viewers)
            elif v == "B":
                r = variant_b(stack, d, bud)
            elif v == "C":
                r = variant_c(stack, d, a.minutes)
            else:
                r = variant_d(stack, d, a.minutes, a.sessions, a.viewers, internal=ident, cap_window_s=a.cap_window)
        finally:
            stack.stop()
            if ident is not None:
                import shutil

                shutil.rmtree(d / "identity", ignore_errors=True)
        ld = load.stop()
        cpu_valid = ld["mean"] < 6.0 and not a.smoke
        checks = [] if a.smoke else (judge_a(r, cpu_valid=cpu_valid, cgroup_mode=stack.cgroup_mode) if v == "A" else
                                     judge_b(r) if v == "B" else judge_c(r, cpu_valid=cpu_valid) if v == "C" else
                                     judge_d(r, cpu_valid=cpu_valid, cgroup_mode=stack.cgroup_mode))
        ok = all(c["ok"] is not False for c in checks)
        ok_all = ok_all and ok
        (d / "summary.json").write_text(json.dumps(r, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
        report["variants"].append({"variant": v, "ok": ok, "cgroup": stack.cgroup_mode, "load": ld, "result": r, "checks": checks})
        print(f"variant {v}: {'PASS' if ok else 'FAIL'} (cgroup {stack.cgroup_mode}, load mean {ld['mean']})")
        for c in checks:
            if c["ok"] is False:
                print(f"    FAIL {c['check']}: {c['value']} (expect {c['expect']})")
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    print(f"report: {out / 'report.json'}")
    del lock
    return EXIT_OK if ok_all else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
