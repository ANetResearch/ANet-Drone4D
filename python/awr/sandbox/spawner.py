"""拉起、就绪判定、健康检查、停止与清理沙盒 sim-core（M17-FR-003、FR-007、FR-008、FR-025、FR-026；AWR-04 §4.6、§4.9；
M17 PRD §6.6.9）。

`ProcessLauncher`（生产实现）：
1. 目录：`var/sandbox/runs/<sid>/`（0700，含 `logs/`）与 `/dev/shm/awr/<sid>/`（0700）；per-run secret
   `runs/<sid>/secret`（32 B，0600；崩溃重启与切换底图保持不变）；`session.json`（0600）；有汇合点时渲染会话 zenoh 配置
   （namespace `awr/<world>/<sid>`，`procutil.render_child_zenoh`）；
2. 叶子组 `sandbox/sb/<sid>/`（无委派时跳过，以 `nice +5` 运行）；`procutil.spawn_logged`：新会话（setsid）、stdin 为
   /dev/null、stdout 与 stderr 追加写 `runs/<sid>/logs/sim-core.log`（不经管道，池退出不连带）、在 exec 之前写入叶子组；
3. 环境：`AWR_RUN = sid`、`AWR_WORLD`、`AWR_RUN_DIR`、`AWR_PERSIST_DIR`、`AWR_SECRET_FILE`、`AWR_ZENOH_CONFIG`、
   `AWR_PROC = sim-core`、`AWR_DETACHED = 1`、`AWR_SESSION_CONFIG`、`AWR_CLOCK_PROFILE = sandbox100`、`AWR_PLAN_POOL = remote`、
   `AWR_PLUGINS`、`AWR_SANDBOX_LIMITS`、`AWR_SIM_AUTOPLAY = 0`（时钟在会话 ACTIVE 时由池以 `sim/play` 启动）、`AWR_SIM_N = 0`、
   `AWR_RESTART_COUNT`、`MALLOC_ARENA_MAX = 2`（D2-FX-R2）；去掉 `AWR_SUPERVISOR_PID`（脱离模式不核对监管者）；
4. 就绪：StateRing 可附着（布局一致）且心跳新于本次拉起、年龄 ≤ `stale_s`；超时 `spawn.timeout_s`（30 s）或进程退出即失败；
5. 停止：SIGTERM，5 s 未退出 SIGKILL（被 SIGSTOP 的进程补 SIGCONT）；挂死先 SIGUSR1 转储栈再 SIGKILL；
6. 清理：删除 `/dev/shm/awr/<sid>`、`var/sandbox/runs/<sid>` 与叶子组（快照另存 `var/sandbox/snapshots/`）；日志超过
   `disk.log_mb` 时截断。

D2.1 驻留与恢复（`park.py`）用到的四个接口：`ring_meta`（驻留时记录环的尺寸、布局、epoch、segment 与仿真时刻）、
`latest_checkpoint`（停止进程后取最新一代 checkpoint）、`stage_checkpoint`（恢复前把驻留的 checkpoint 放回 tmpfs 并按记录预建
StateRing，使 sim-core 以 `AWR_RESTART_COUNT = 1` 走崩溃重启的恢复路径）、`ring_t_sim`（恢复后核对）；`launch` 接受额外环境
（`AWR_RESUME_FROM`）。驻留目录 `parked/` 另有磁盘上限，不计入 `disk.total_mb`。
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import logging
import os
import shutil
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from awr.contracts import LAYOUT_ID
from awr.runtime.binding import RunBinding
from awr.runtime.procutil import (
    atomic_write,
    pid_alive,
    render_child_zenoh,
    ring_age_ms,
    spawn_logged,
    starttime,
    write_private,
)
from awr.runtime.statering import LayoutMismatch, RingNotReady, StateRing

from .cgroups import CgroupManager
from .config import PoolSettings
from .model import SessionRecord

__all__ = ["D1_PLUGINS", "D2_PLUGINS", "LaunchHandle", "Launcher", "ProcessLauncher", "default_plugins"]

log = logging.getLogger("awr.sandbox.spawner")

D1_PLUGINS = ("awr.environment.stage", "awr.sim.safety", "awr.sim.mission", "awr.sim.sensors.plugin")
# `.plugin` 入口：awr.sim.catalog 与 awr.sim.fixedwing 两个包导入无副作用（只有 .plugin 登记功率模型与目录），写包名会使沙盒会话
# 装不出固定翼与垂起机（110 NO_POWER_MODEL；d2-WP-13-to-M17 第 1 条，D2-INT 修正）；M18 至 M20 的包名与 .plugin 等价
D2_PLUGINS = ("awr.sim.catalog.plugin", "awr.sim.fixedwing.plugin", "awr.sim.targets.plugin", "awr.sim.perception.plugin",
              "awr.sim.tasking.plugin")
# 池自身（supervisor 注入给共享展示 run 的）环境中不得带入沙盒 sim-core 的项：监管者、叶子组、热备用、共享展示的剧本与生成参数
STRIP_ENV = ("AWR_SUPERVISOR_PID", "AWR_CGROUP", "AWR_STANDBY", "AWR_LOG_FILE", "AWR_ID_RANGE", "AWR_SCENARIO",
             "AWR_SCENARIO_PROFILE", "AWR_LAST_EXIT", "AWR_SIM_SPAWN", "AWR_SIM_PROFILE", "AWR_SIM_AUX_CPUS")


def default_plugins() -> tuple[str, ...]:
    """D1 公开剧本插件 + 已交付的 D2 插件（M17 PRD §6.6.9 `SANDBOX_PLUGINS`；未安装的包跳过，不使拉起失败）。"""
    out = []
    for m in (*D1_PLUGINS, *D2_PLUGINS):
        try:
            if importlib.util.find_spec(m) is not None:
                out.append(m)
        except (ImportError, ValueError):
            continue
    return tuple(out)


@dataclass
class LaunchHandle:
    pid: int
    starttime: int | None
    proc: asyncio.subprocess.Process | None
    spawn_mono_ns: int


class Launcher:
    """拉起器接口（测试以进程内替身实现）。"""

    async def launch(self, rec: SessionRecord, b: RunBinding, cfg_doc: dict | None, *, restart: bool,
                     extra_env: dict[str, str] | None = None) -> LaunchHandle:
        raise NotImplementedError

    def ring_meta(self, b: RunBinding) -> dict | None:
        return None

    def ring_t_sim(self, b: RunBinding) -> int | None:
        return None

    def latest_checkpoint(self, rec: SessionRecord, b: RunBinding, dst: Path) -> int | None:
        return None

    def stage_checkpoint(self, rec: SessionRecord, b: RunBinding, src: Path, meta: dict) -> bool:
        return False

    async def wait_ready(self, rec: SessionRecord, b: RunBinding, h: LaunchHandle, timeout: float) -> str | None:
        raise NotImplementedError

    def health(self, b: RunBinding, spawn_mono_ns: int) -> tuple[float | None, Any]:
        return None, None

    async def terminate(self, pid: int, *, grace_s: float = 5.0, dump: bool = False) -> bool:
        raise NotImplementedError

    def kill(self, pid: int, *, dump: bool = False) -> None:
        raise NotImplementedError

    def write_config(self, rec: SessionRecord, cfg_doc: dict) -> None: ...

    def cleanup(self, rec: SessionRecord, b: RunBinding, *, keep_run_dir: bool = False) -> None: ...

    def run_dir_bytes(self) -> int:
        return 0


def _mkdir_private(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(p, 0o700)


def _du(p: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(p, onerror=lambda e: None):
        for f in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, f)).st_size
    return total


class ProcessLauncher(Launcher):
    def __init__(self, settings: PoolSettings, cgroups: CgroupManager, *, ring_cls: type = StateRing) -> None:
        self.s = settings
        self.cg = cgroups
        self.ring_cls = ring_cls
        self.rings: dict[str, Any] = {}
        argv = settings.simcore_argv or (sys.executable, "-m", "awr.sim.runtime")
        self.argv = list(argv)
        self.plugins = settings.plugins or default_plugins()

    # ------------------------------------------------------------ 目录与配置
    def run_dir(self, sid: str) -> Path:
        return self.s.runs_dir / sid

    def write_config(self, rec: SessionRecord, cfg_doc: dict) -> None:
        rd = self.run_dir(rec.sid)
        _mkdir_private(rd)
        atomic_write(rd / "session.json", json.dumps(cfg_doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), 0o600)

    def _env(self, rec: SessionRecord, b: RunBinding, *, restart: bool, zc: Path | None,
             extra_env: dict[str, str] | None = None) -> dict[str, str]:
        rd = self.run_dir(rec.sid)
        env = {k: v for k, v in (self.s.env or os.environ).items() if k not in STRIP_ENV}
        lim = self.s.limits_obj()
        env.update({"AWR_RUN": rec.sid, "AWR_WORLD": rec.world, "AWR_RUN_DIR": str(b.run_dir), "AWR_PERSIST_DIR": str(rd),
                    "AWR_RUNS_DIR": str(self.s.runs_dir), "AWR_SECRET_FILE": str(rd / "secret"), "AWR_PROC": "sim-core",
                    "AWR_DETACHED": "1", "AWR_SESSION_CONFIG": str(rd / "session.json"), "AWR_CLOCK_PROFILE": "sandbox100",
                    "AWR_PLAN_POOL": "remote", "AWR_PLUGINS": ",".join(self.plugins), "AWR_SIM_AUTOPLAY": "0", "AWR_SIM_N": "0",
                    "AWR_ID_BASE": "0", "AWR_ID_COUNT": "1024", "AWR_RESTART_COUNT": str(rec.restarts if restart else 0),
                    "AWR_SANDBOX_LIMITS": json.dumps({"vehicles": lim["vehicles_max"], "targets": lim["targets_max"],
                                                      "clones": lim["cloned_models_max"]}),
                    "AWR_PROFILE": self.s.profile, "AWR_WORLDS_DIR": str(self.s.worlds_dir)})
        # glibc 每个线程最多可建一个 malloc arena（zenoh I/O、checkpoint 写线程、规划客户端），碎片使匿名页高出数 MB；
        # 会话 sim-core 限 2 个 arena（D2-FX-R2，D2-AC-28 的 S 级 ≤ 150 MB；环境已显式给出时不覆盖）
        env.setdefault("MALLOC_ARENA_MAX", "2")
        if not self.s.allow_derive:
            env["AWR_GEO_ALLOW_DERIVE"] = "0"
        if zc is not None:
            env["AWR_ZENOH_CONFIG"] = str(zc)
        else:
            env.pop("AWR_ZENOH_CONFIG", None)
        env.pop("AWR_RESUME_FROM", None)
        env.update(extra_env or {})
        return env

    # ------------------------------------------------------------ 拉起与就绪
    async def launch(self, rec: SessionRecord, b: RunBinding, cfg_doc: dict | None, *, restart: bool,
                     extra_env: dict[str, str] | None = None) -> LaunchHandle:
        rd = self.run_dir(rec.sid)
        _mkdir_private(rd)
        _mkdir_private(rd / "logs")
        _mkdir_private(b.run_dir)
        sec = rd / "secret"
        if not sec.exists():
            write_private(sec, os.urandom(32))
        if cfg_doc is not None:
            self.write_config(rec, cfg_doc)
        zc = None
        if self.s.rendezvous:
            zc = render_child_zenoh(rd / "zenoh.json5", namespace=b.namespace, rendezvous=self.s.rendezvous,
                                    lease_ms=self.s.lease_ms)
        leaf = self.cg.create(rec.sid, rec.world_class, spawning=True)   # 拉起加权，就绪后由池 settle（D2-FX-R2）
        self.rings.pop(str(b.ring_path), None)          # 新实例：重新附着环（重启后的环可能是新文件）
        log_path = rd / "logs" / "sim-core.log"
        self._truncate_log(log_path)
        t0 = time.monotonic_ns()
        proc = await spawn_logged(self.argv, env=self._env(rec, b, restart=restart, zc=zc, extra_env=extra_env), log_path=log_path,
                                  cgroup_dir=leaf, nice=None if leaf is not None else int(self.s.sb.spawn.nice))
        return LaunchHandle(proc.pid, starttime(proc.pid), proc, t0)

    def _truncate_log(self, p: Path) -> None:
        try:
            if p.stat().st_size > self.s.sb.disk.log_mb * (1 << 20):
                with open(p, "r+b") as fh:
                    fh.truncate(0)
        except OSError:
            pass

    def health(self, b: RunBinding, spawn_mono_ns: int) -> tuple[float | None, Any]:
        """(心跳年龄 ms 或 None, 环头部或 None)。"""
        key = str(b.ring_path)
        ring = self.rings.get(key)
        if self.ring_cls is StateRing:
            age, ring = ring_age_ms(ring, b.ring_path, spawn_mono_ns)
        else:
            age = None
            if ring is not None:
                try:
                    if ring.identity()[0] != ring.mapped_ino:
                        ring = None
                except Exception:
                    ring = None
            if ring is None:
                with contextlib.suppress(RingNotReady, LayoutMismatch, OSError):
                    ring = self.ring_cls.attach(b.ring_path, expect_layout_id=LAYOUT_ID)
            if ring is not None:
                hb = ring.header().heartbeat_ns
                age = (time.monotonic_ns() - hb) / 1e6 if hb > spawn_mono_ns else None
        if ring is None:
            self.rings.pop(key, None)
            return None, None
        self.rings[key] = ring
        try:
            return age, ring.header()
        except Exception:
            return age, None

    async def wait_ready(self, rec: SessionRecord, b: RunBinding, h: LaunchHandle, timeout: float) -> str | None:
        end = time.monotonic() + timeout
        stale_ms = self.s.sb.spawn.stale_s * 1000
        while time.monotonic() < end:
            if h.proc is not None and h.proc.returncode is not None:
                return f"exit:{h.proc.returncode}"
            if h.proc is None and not pid_alive(h.pid):
                return "exit"
            age, _ = self.health(b, h.spawn_mono_ns)
            if age is not None and age <= stale_ms:
                return None
            await asyncio.sleep(0.05)
        return "timeout"

    # ------------------------------------------------------------ 停止与清理
    async def terminate(self, pid: int, *, grace_s: float = 5.0, dump: bool = False) -> bool:
        if not pid_alive(pid):
            return True
        if dump:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGUSR1)
            await asyncio.sleep(0.1)
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
        end = time.monotonic() + grace_s
        while time.monotonic() < end:
            if not pid_alive(pid) or _zombie(pid):
                return True
            await asyncio.sleep(0.05)
        self.kill(pid)
        end = time.monotonic() + 5.0
        while time.monotonic() < end:
            if not pid_alive(pid) or _zombie(pid):
                return True
            await asyncio.sleep(0.05)
        return False

    def kill(self, pid: int, *, dump: bool = False) -> None:
        """SIGKILL（补 SIGCONT）；dump 时先 SIGUSR1 转储栈，0.1 s 后再 SIGKILL（不阻塞事件循环）。"""

        def hard() -> None:
            for sig in (signal.SIGKILL, signal.SIGCONT):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, sig)

        if dump:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGUSR1)
            t = threading.Timer(0.1, hard)
            t.daemon = True
            t.start()
            return
        hard()

    def cleanup(self, rec: SessionRecord, b: RunBinding, *, keep_run_dir: bool = False) -> None:
        ring = self.rings.pop(str(b.ring_path), None)
        if ring is not None:
            with contextlib.suppress(Exception):
                ring.close()
        shutil.rmtree(b.run_dir, ignore_errors=True)
        if not keep_run_dir:
            shutil.rmtree(self.run_dir(rec.sid), ignore_errors=True)
        for _ in range(3):
            if self.cg.remove(rec.sid):
                break
            time.sleep(0.05)

    def run_dir_bytes(self) -> int:
        parked = self.s.parked_dir
        total = 0
        with contextlib.suppress(OSError):
            for p in self.s.state_dir.iterdir():
                if p == parked:
                    continue
                if p.is_dir() and not p.is_symlink():
                    total += _du(p)
                else:
                    with contextlib.suppress(OSError):
                        total += p.lstat().st_size
        return total

    # ------------------------------------------------------------ 驻留与恢复（D2.1）
    def ring_meta(self, b: RunBinding) -> dict | None:
        _age, hdr = self.health(b, 0)
        if hdr is None:
            return None
        return {"capacity": int(hdr.capacity), "slots": int(hdr.slot_count), "layout_id": int(hdr.layout_id),
                "epoch": int(hdr.epoch), "segment": int(hdr.segment), "t_sim_ns": int(hdr.t_sim_ns),
                "id_base": int(hdr.id_base), "id_count": int(hdr.id_count)}

    def ring_t_sim(self, b: RunBinding) -> int | None:
        _age, hdr = self.health(b, 0)
        return int(hdr.t_sim_ns) if hdr is not None else None

    def latest_checkpoint(self, rec: SessionRecord, b: RunBinding, dst: Path) -> int | None:
        """tmpfs 与持久镜像中最新的有效代（不含 `.poison`、`.stale`）复制到 dst；返回该代的仿真时刻或 None。"""
        best: tuple[int, Path] | None = None
        for d in (Path(b.run_dir) / "ckpt", self.run_dir(rec.sid) / "ckpt"):
            with contextlib.suppress(OSError):
                for p in d.iterdir():
                    if p.suffix != ".bin" or not p.stem.isdigit():
                        continue
                    if p.with_suffix(".poison").exists() or p.with_suffix(".stale").exists():
                        continue
                    t = int(p.stem)
                    if best is None or t > best[0]:
                        best = (t, p)
        if best is None:
            return None
        try:
            tmp = dst.with_name(dst.name + ".tmp")
            shutil.copyfile(best[1], tmp)
            os.chmod(tmp, 0o600)
            os.replace(tmp, dst)
        except OSError:
            return None
        return best[0]

    def stage_checkpoint(self, rec: SessionRecord, b: RunBinding, src: Path, meta: dict) -> bool:
        """恢复前：把驻留的 checkpoint 放回 `<run_dir>/ckpt/`，并按驻留时的环尺寸、布局、epoch 与 segment 预建 StateRing。"""
        ring = meta.get("ring") if isinstance(meta.get("ring"), dict) else None
        if ring is None or int(ring.get("layout_id") or -1) != int(LAYOUT_ID):
            return False
        t = int(meta.get("t_sim_ns") or ring.get("t_sim_ns") or 0)
        rd = Path(b.run_dir)
        try:
            shutil.rmtree(rd, ignore_errors=True)
            _mkdir_private(rd / "ckpt")
            shutil.copyfile(src, rd / "ckpt" / f"{t:020d}.bin")
            r = StateRing.create(Path(b.ring_path), capacity=int(ring["capacity"]), slots=int(ring["slots"]),
                                 layout_id=int(LAYOUT_ID), epoch=int(ring.get("epoch") or 1),
                                 segment=int(ring.get("segment") or 0), id_base=int(ring.get("id_base") or 0),
                                 id_count=int(ring.get("id_count") or 1024))
            r.close()
        except (OSError, ValueError, KeyError):
            log.warning("checkpoint staging failed", exc_info=True, extra={"kv": {"sid": rec.sid}})
            shutil.rmtree(rd, ignore_errors=True)
            return False
        return True


def _zombie(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.rsplit(")", 1)[1].split()[0] == "Z"
    except (OSError, IndexError):
        return False
