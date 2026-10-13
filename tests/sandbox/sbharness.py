"""tests/sandbox 的替身与栈（M17 PRD §10.1）。

- `FakeLauncher`：ProcessLauncher 的子类。每个会话拉起一个真实的轻量进程（`python -c` 睡眠循环，环境含 `AWR_RUN=<sid>`，
  因此 pid、starttime、environ 核对、pidfd 退出监视、SIGTERM/SIGKILL 与 kill -9 都是真实的），同时在进程内起一个 tests/rt 的
  FakeSim（LocalRing + LocalBus，命名空间 `awr/<world>/<sid>`）承担 StateRing 心跳、`ctl/sim-core/clock`、`state/sim-core/perf`
  与 `ctl/sim-core/snapshot`。可配置：拉起失败（不就绪）、心跳暂停（挂死）。
- `PoolThread`：在独立线程的事件循环中运行真实的 `awr.sandbox.pool.Pool`（LocalBus、FakeLauncher、无规划服务）。
- `SbxStack`：PoolThread + tests/rt 的 SbStack（真实 sandbox-api 应用 + uvicorn 线程），两者共用状态目录（token.key、pool.secret）。

D2.1 驻留与恢复（`park.py`）：FakeSim 在暂停时不推进仿真时刻；`ctl/sim-core/checkpoint{to: disk}`（正式接口，`formal_ckpt`
为 True 时提供）把 `{t_sim_ns, epoch, vehicles}` 写入给定路径；未提供时 `clock{op: checkpoint}` 在 `<run_dir>/ckpt/` 写一代（过渡
路径）。以 `AWR_RESUME_FROM` 拉起时从该文件恢复仿真时刻与机体（epoch + 1），`fail_restore` 中的 sid 模拟 checkpoint 不兼容
（冷启动）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import shutil
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

HERE = Path(__file__).resolve().parent
RT = HERE.parent / "rt"
for p in (str(HERE), str(RT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import fakesim  # noqa: E402

from awr.contracts import bus_keys  # noqa: E402
from awr.contracts.enums import TimeState  # noqa: E402
from awr.runtime.binding import RunBinding  # noqa: E402
from awr.runtime.bus import LocalBus  # noqa: E402
from awr.runtime.config import SandboxCfg  # noqa: E402
from awr.runtime.procutil import environ_of  # noqa: E402
from awr.runtime.statering import LocalRing  # noqa: E402
from awr.sandbox.cgroups import CgroupManager  # noqa: E402
from awr.sandbox.config import PoolSettings  # noqa: E402
from awr.sandbox.model import SessionRecord, WorldCost  # noqa: E402
from awr.sandbox.park import CTL_CHECKPOINT  # noqa: E402
from awr.sandbox.pool import Pool  # noqa: E402
from awr.sandbox.spawner import LaunchHandle, ProcessLauncher  # noqa: E402
from awr.sandbox.templates import TemplateCatalog  # noqa: E402
from awr.sandbox.worlds import WorldTable  # noqa: E402

SLEEPER = [sys.executable, "-c", "import time\nwhile True: time.sleep(3600)"]
WORLDS = ["synthcity", "shenzhen", "suzhou", "newyork", "chicago", "shanghai", "sanfrancisco"]
COSTS = {"synthcity": WorldCost("synthcity", "S", 140, 25, 0.15), "shenzhen": WorldCost("shenzhen", "S", 140, 30, 0.15),
         "suzhou": WorldCost("suzhou", "S", 140, 30, 0.15), "newyork": WorldCost("newyork", "S", 140, 65, 0.15),
         "chicago": WorldCost("chicago", "L", 155, 220, 0.15), "shanghai": WorldCost("shanghai", "L", 155, 310, 0.15),
         "sanfrancisco": WorldCost("sanfrancisco", "L", 155, 350, 0.15)}


def make_sb(**over: Any) -> SandboxCfg:
    base: dict[str, Any] = {"max_sessions": 2, "batch": {"enabled": True}}
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = {**base[k], **v}
        else:
            base[k] = v
    return SandboxCfg.model_validate(base)


def make_worlds(missing: tuple[str, ...] = ()) -> WorldTable:
    return WorldTable(Path("/nonexistent/worlds"), dict(COSTS), ids=list(WORLDS),
                      probe=lambda _d, w: (w not in missing, "derived_cache_missing" if w in missing else None))


def make_settings(tmp: Path, sb: SandboxCfg | None = None, **kw: Any) -> PoolSettings:
    shm = Path("/dev/shm/awr") / f"t-sbx-{secrets.token_hex(4)}"
    s = PoolSettings(sb=sb or make_sb(), state_dir=tmp / "state", shm_root=shm, q_cores=2.5, delegated=False,
                     access_mode="loopback", allow_derive=True, profile="ci", identity_dir=tmp / "identity")
    for k, v in kw.items():
        setattr(s, k, v)
    return s


class FakeLauncher(ProcessLauncher):
    def __init__(self, settings: PoolSettings) -> None:
        settings.simcore_argv = tuple(SLEEPER)
        super().__init__(settings, CgroupManager(settings.sb, delegated=False), ring_cls=LocalRing)
        self.sims: dict[str, fakesim.FakeSim] = {}
        self.fail_ready: set[str] = set()          # sid：拉起后不出现环（就绪超时）
        self.fail_worlds: set[str] = set()         # 这些世界拉起即不就绪（切换底图失败）
        self.snapshots: dict[str, dict] = {}
        self.launches: list[tuple[str, str, bool]] = []
        self.n_vehicles = 0                         # FakeSim 的机体数（SDK 用例的速度控制需要 1 架）
        self.pids: list[tuple[int, str]] = []
        self.lock = threading.Lock()
        self.formal_ckpt = True                      # 提供 ctl/sim-core/checkpoint{to: disk}（M08 正式接口）
        self.fail_restore: set[str] = set()          # sid：以 AWR_RESUME_FROM 拉起但不恢复（模拟不兼容）
        self.fail_ckpt: set[str] = set()             # sid：两条 checkpoint 路径都不产出文件（超时或失败）
        self.envs: dict[str, dict[str, str]] = {}    # sid -> 最近一次拉起的额外环境

    def sim(self, sid: str) -> fakesim.FakeSim | None:
        return self.sims.get(sid)

    async def launch(self, rec: SessionRecord, b: RunBinding, cfg_doc: dict | None, *, restart: bool,
                     extra_env: dict[str, str] | None = None) -> LaunchHandle:
        h = await super().launch(rec, b, cfg_doc, restart=restart, extra_env=extra_env)
        self.envs[rec.sid] = dict(extra_env or {})
        self.launches.append((rec.sid, rec.world, restart))
        self.pids.append((h.pid, rec.sid))
        old = self.sims.pop(rec.sid, None)
        if old is not None:
            with contextlib.suppress(RuntimeError):   # 崩溃的旧池事件循环留下的在途请求（只出现在进程内模拟中）
                await asyncio.to_thread(old.close)
        if rec.sid in self.fail_ready or rec.world in self.fail_worlds:
            return h
        secret = Path(b.secret_path).read_bytes()
        st = SimpleNamespace(secret=secret, run_id=b.run_id, ring_path=b.ring_path, namespace=b.namespace, run_dir=b.run_dir)
        sim = fakesim.FakeSim(st, n=self.n_vehicles, hz=50.0)
        sid, world = rec.sid, rec.world

        def snap(req: Any) -> None:
            req.reply_msg({"v": 1, "code": 0, "snapshot": self.snapshots.get(sid) or {
                "schema": "awr.sandbox.snapshot.v1", "contracts": "1.1.0", "world_id": world, "template_id": None,
                "calendar": {"start_utc": "2026-10-05T02:00:00Z", "tz": "Asia/Shanghai"}, "seed": 7, "nests": [],
                "vehicles": [], "targets": [], "tasks": []}})

        sim.handles.append(sim.bus.serve(bus_keys.CTL_SNAPSHOT, snap))
        self._park_hooks(rec.sid, sim, b, extra_env or {})
        self.sims[rec.sid] = sim
        return h

    # ------------------------------------------------------------ D2.1 驻留与恢复的替身
    def _park_hooks(self, sid: str, sim: fakesim.FakeSim, b: RunBinding, env: dict[str, str]) -> None:
        orig_step, orig_clock = sim.step, sim._clock
        hold = {int(TimeState.PAUSED)}
        if env:                                   # 恢复拉起：与 AWR_SIM_AUTOPLAY=0 的 sim-core 一致，播放前不推进
            hold.add(int(TimeState.STOPPED))
            sim.state = int(TimeState.STOPPED)

        def step() -> None:                       # 暂停时仿真时刻不推进（与 sim-core 一致）
            t = sim.t_sim_ns
            orig_step()
            if sim.state in hold:
                sim.t_sim_ns = t

        def state_doc() -> bytes:
            return json.dumps({"t_sim_ns": sim.t_sim_ns, "epoch": sim.epoch,
                               "vehicles": [v["id"] for v in sim.vehicles]}).encode()

        def clock(m: dict) -> dict:
            if m.get("op") == "checkpoint" and sid in self.fail_ckpt:
                return {"v": 1, "cid": m.get("cid"), "status": "rejected", "code": 211, "clock": {}}
            if m.get("op") == "checkpoint":       # 过渡路径：强制写一代到 tmpfs
                d = Path(b.run_dir) / "ckpt"
                d.mkdir(parents=True, exist_ok=True)
                (d / f"{sim.t_sim_ns:020d}.bin").write_bytes(state_doc())
                return {"v": 1, "cid": m.get("cid"), "status": "accepted", "code": 0, "clock": {}}
            return orig_clock(m)

        def ckpt(req: Any) -> None:
            m = req.msg() or {}
            if sid in self.fail_ckpt:
                req.reply_msg({"v": 1, "cid": m.get("cid"), "ok": False, "code": 211})
                return
            with sim.lock:
                data = state_doc()
                Path(str(m.get("path"))).write_bytes(data)
                req.reply_msg({"v": 1, "cid": m.get("cid"), "ok": True, "bytes": len(data), "t_sim_ns": sim.t_sim_ns,
                               "layout_hashes": []})

        sim.step, sim._clock = step, clock      # type: ignore[method-assign]
        if self.formal_ckpt:
            sim.handles.append(sim.bus.serve(CTL_CHECKPOINT, ckpt))
        src = env.get("AWR_RESUME_FROM")
        if src and sid not in self.fail_restore:
            try:
                st = json.loads(Path(src).read_bytes())
            except (OSError, ValueError):
                return
            with sim.lock:
                sim.t_sim_ns = int(st["t_sim_ns"])
                sim.state = int(TimeState.PAUSED)
                ids = list(st.get("vehicles") or [])
                sim.vehicles = [dict(sim.vehicles[0] if sim.vehicles else {"kind": "uav", "producer": "sim-core",
                                                                          "sensors": []}, agent_no=i, id=v)
                                for i, v in enumerate(ids)]
                sim.roster_version += 1
                sim.epoch = max(sim.epoch, int(st.get("epoch") or 1)) + 1
                sim.ring.set_epoch(sim.epoch)
                sim.events.set_epoch(sim.epoch)

    def stage_checkpoint(self, rec: SessionRecord, b: RunBinding, src: Path, meta: dict) -> bool:
        return Path(src).exists()                 # LocalRing 由 FakeSim 自建；恢复在 launch 中按 AWR_RESUME_FROM 完成

    async def terminate(self, pid: int, *, grace_s: float = 5.0, dump: bool = False) -> bool:
        ok = await super().terminate(pid, grace_s=grace_s, dump=dump)
        return ok

    def cleanup(self, rec: SessionRecord, b: RunBinding, *, keep_run_dir: bool = False) -> None:
        sim = self.sims.pop(rec.sid, None)
        if sim is not None:
            with contextlib.suppress(RuntimeError):
                sim.close()
        super().cleanup(rec, b, keep_run_dir=keep_run_dir)

    def close(self) -> None:
        for sid in list(self.sims):
            with contextlib.suppress(RuntimeError):
                self.sims.pop(sid).close()
        for pid, sid in self.pids:                  # 用例失败时残留的睡眠进程（按环境核对，不误杀复用的 pid）
            env = environ_of(pid, ("AWR_RUN",))
            if env is not None and env.get("AWR_RUN") == sid:
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)


class PoolThread:
    """真实 Pool 在独立线程的事件循环中运行（LocalBus、FakeLauncher）。"""

    def __init__(self, settings: PoolSettings, *, namespace: str | None = None, worlds: WorldTable | None = None,
                 templates: TemplateCatalog | None = None, clock: Any = time.time, launcher: FakeLauncher | None = None) -> None:
        self.settings = settings
        self.namespace = namespace or f"{bus_keys.POOL_NAMESPACE}-{secrets.token_hex(3)}"
        self.state_dir = settings.state_dir
        self.worlds = worlds or make_worlds()
        self.templates = templates or TemplateCatalog(HERE / "fixtures" / "templates")
        self.launcher = launcher or FakeLauncher(settings)
        self.clock = clock
        self.loop: asyncio.AbstractEventLoop | None = None
        self.pool: Pool | None = None
        self.thread: threading.Thread | None = None
        self.start()

    def start(self) -> None:
        ready = threading.Event()
        box: dict[str, Any] = {}

        def run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.loop = loop
            bus = LocalBus.open("sandbox-pool", namespace=self.namespace, announce=False, loop=loop)
            pool = Pool(self.settings, worlds=self.worlds, templates=self.templates, launcher=self.launcher, planhost=False,
                        pool_bus=bus, bus_kind="local", clock=self.clock, ring_cls=LocalRing,
                        bus_factory=lambda ns: LocalBus.open("sandbox-pool", namespace=ns, announce=False, loop=loop))
            pool._own_pool_bus = True
            self.pool = pool
            try:
                loop.run_until_complete(pool.start())
            except BaseException as e:  # pragma: no cover
                box["e"] = e
                ready.set()
                return
            ready.set()
            loop.run_forever()
            loop.close()

        self.thread = threading.Thread(target=run, name="sbx-pool", daemon=True)
        self.thread.start()
        if not ready.wait(20):
            raise RuntimeError("pool did not start")
        if "e" in box:
            raise box["e"]

    def call(self, fn: Any, *args: Any, timeout: float = 10.0) -> Any:
        """在池的事件循环中执行 fn(*args)（同步函数或协程函数）。"""
        assert self.loop is not None

        async def run() -> Any:
            r = fn(*args)
            if asyncio.iscoroutine(r):
                r = await r
            return r

        return asyncio.run_coroutine_threadsafe(run(), self.loop).result(timeout)

    def op(self, op: str, args: dict, timeout: float = 10.0) -> dict:
        assert self.pool is not None
        return self.call(self.pool.dispatch, op, args, timeout=timeout)

    @property
    def broker(self) -> Any:
        assert self.pool is not None
        return self.pool.broker

    def wait(self, pred: Any, timeout: float = 10.0, what: str = "条件") -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.call(pred):
                return
            time.sleep(0.05)
        raise AssertionError(f"等待超时：{what}")

    def state(self, sid: str) -> str | None:
        return self.call(lambda: (r.state if (r := self.broker.sessions.get(sid)) else None))

    def stop(self, *, maintenance: bool = False) -> None:
        if self.loop is None or self.pool is None:
            return
        fut = asyncio.run_coroutine_threadsafe(self.pool.stop(maintenance=maintenance, grace_s=8.0), self.loop)
        fut.result(20)
        self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread is not None:
            self.thread.join(10)
        self.loop = None

    def crash(self) -> None:
        """模拟池被 kill -9：不做任何清理地停掉事件循环（沙盒进程与 FakeSim 继续运行）。"""
        assert self.loop is not None and self.pool is not None
        pool = self.pool
        loop = self.loop

        def halt() -> None:
            for t in list(pool._loops) + list(pool.tasks):
                t.cancel()
            for h in pool._handles:
                h.close()
            if pool.exitw is not None:
                for sid in list(pool.exitw.watching):
                    pool.exitw._drop(sid)
            for sb in list(pool.sbus.values()):
                sb.closed = True
                for h in sb.handles:
                    h.close()
            pool.pool_bus.close()
            loop.call_later(0.05, loop.stop)       # 让被取消的任务走完取消（不落盘：落盘任务已取消）

        self.loop.call_soon_threadsafe(halt)
        if self.thread is not None:
            self.thread.join(10)
        self.loop = None


class SbxStack:
    """真实池（PoolThread）+ 真实 sandbox-api（tests/rt SbStack 的 uvicorn 线程）。"""

    def __init__(self, *, sb: SandboxCfg | None = None, sb_api: dict | None = None, clock: Any = time.time,
                 missing: tuple[str, ...] = (), **settings_kw: Any) -> None:
        from fakepool import SbStack

        self.tmp = Path(tempfile.mkdtemp(prefix="awr-sbx-pool-"))
        self.settings = make_settings(self.tmp, sb, **settings_kw)
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        self.pt = PoolThread(self.settings, worlds=make_worlds(missing), clock=clock)
        adapter = SimpleNamespace(namespace=self.pt.namespace, close=lambda: None, state_dir=self.settings.state_dir)
        sbc = self.settings.sb
        api_over = {"max_sessions": sbc.max_sessions, "quota_profile": sbc.quota_profile,
                    "batch": sbc.batch.model_dump(), **(sb_api or {})}
        self.api = SbStack(pool=adapter, state_dir=self.settings.state_dir, sb_over=api_over)
        self.base = self.api.base
        self.origin = self.api.origin

    @property
    def broker(self) -> Any:
        return self.pt.broker

    def close(self) -> None:
        self.api.stop_api()
        self.pt.stop()
        self.pt.launcher.close()
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.settings.shm_root, ignore_errors=True)
