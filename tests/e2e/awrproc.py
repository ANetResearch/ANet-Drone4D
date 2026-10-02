"""M16 端到端与混沌测试的后端辅助：以真实 supervisor 进程启动 sim-core + api（ci profile、空闲端口、临时 runs 目录），
签发 token、轮询 `/api/events`、读取 `/api/sys/procs`（M16 §9.2 `scenario_run`；SK-E2E 报告 §4.1 推荐写法）。

不依赖前端代码与其他模块的测试辅助；`tests/chaos` 经 sys.path 垫片复用本模块。
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SHM = Path("/dev/shm/awr")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def world_ready(world: str) -> bool:
    base = Path(os.environ.get("AWR_WORLDS_DIR") or ROOT / "worlds")
    return (base / world / "world.json").exists()


_NUMBA_WARMED: list[bool] = []


def numba_warm(env: dict[str, str] | None = None) -> None:
    """启动 supervisor 前预热 sim-core 的全部 numba 核（与 `make run` 前置 `numba-warm` 同一入口，每个测试进程一次）。
    冷缓存时首次编译约 60 s，若留给 sim-core 在启动宽限（15 s）内完成会被反复判 startup_timeout（D1 验收第 1 轮 4.1）。"""
    if _NUMBA_WARMED:
        return
    _NUMBA_WARMED.append(True)
    e = dict(env or os.environ)
    if e.get("AWR_KERNEL", "").strip().lower() == "numpy":
        return
    with contextlib.suppress(Exception):
        subprocess.run([sys.executable, "-m", "awr.sim.runtime.warm"], cwd=ROOT, env=e, capture_output=True, timeout=900)


def hint_of(name: str) -> str:
    """固定 principal_hint（16–64 位 base32，SK-E2E §4.3）：同一测试重跑保持同一 principal。"""
    s = "".join(ch for ch in name.upper() if ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
    return (s + "MSIXTEENEETESTHINTAAAAAAA")[:24]


@dataclass
class Backend:
    """一个 supervisor 运行。`start()` 读到 READY 横幅后返回；`stop()` 以 SIGTERM 停止整个进程组并删除临时目录。"""

    world: str = "shenzhen"
    scenario: str | None = None
    scenario_profile: str | None = None
    profile: str = "ci"
    only: str | None = "sim-core,api"
    scenarios_dir: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    ready_timeout_s: float = 120.0
    # 运行态
    proc: subprocess.Popen | None = None
    base: str = ""
    run_id: str = ""
    tmp: Path | None = None
    log: list[str] = field(default_factory=list)
    t_ready_s: float = 0.0
    _tokens: dict[tuple[str, str], str] = field(default_factory=dict)

    def start(self) -> Backend:
        self.tmp = Path(tempfile.mkdtemp(prefix="awr-m16-"))
        port, bus = free_port(), free_port()
        cmd = [sys.executable, "-m", "awr.runtime.supervisor", "--profile", self.profile,
               "--set", "net.port_offset=0", "--set", f"net.port={port}", "--set", f"bus.rendezvous=tcp/127.0.0.1:{bus}",
               "--set", "run.keep_run_dir=false"]
        if self.only:
            cmd += ["--only", self.only]
        env = dict(os.environ, AWR_RUNS_DIR=str(self.tmp / "runs"), PYTHONUNBUFFERED="1", AWR_WORLD=self.world,
                   **self.env)
        env.pop("AWR_SUPERVISOR_PID", None)
        env.pop("AWR_PORT_OFFSET", None)
        if self.scenario:
            env["AWR_SCENARIO"] = self.scenario
        if self.scenario_profile:
            env["AWR_SCENARIO_PROFILE"] = self.scenario_profile
        if self.scenarios_dir:
            env["AWR_SCENARIOS_DIR"] = str(self.scenarios_dir)
        numba_warm(env)
        t0 = time.monotonic()
        self.proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     start_new_session=True)
        ready = threading.Event()

        def pump() -> None:
            assert self.proc is not None and self.proc.stdout is not None
            for line in self.proc.stdout:
                self.log.append(line.rstrip())
                if line.startswith("READY") and not ready.is_set():
                    self.run_id = next((x.split("=", 1)[1] for x in line.split() if x.startswith("run=")), "")
                    ready.set()

        threading.Thread(target=pump, daemon=True).start()
        if not ready.wait(self.ready_timeout_s) or self.proc.poll() is not None:
            tail = "\n".join(self.log[-40:])
            self.stop()
            raise RuntimeError(f"supervisor did not print READY within {self.ready_timeout_s} s:\n{tail}")
        self.t_ready_s = time.monotonic() - t0
        self.base = f"http://127.0.0.1:{port}"
        try:
            self.wait_http("/api/health/ready", 60.0)
        except BaseException:
            self.stop()
            raise
        return self

    # ------------------------------------------------------------ HTTP
    def wait_http(self, path: str, timeout_s: float) -> None:
        import httpx

        end = time.monotonic() + timeout_s
        last = ""
        while time.monotonic() < end:
            try:
                r = httpx.get(self.base + path, timeout=2.0)
                if r.status_code == 200:
                    return
                last = f"{r.status_code} {r.text[:200]}"
            except httpx.HTTPError as e:
                last = str(e)
            time.sleep(0.25)
        raise TimeoutError(f"{path} not ready: {last}")

    def admin_secret(self) -> str:
        assert self.tmp is not None
        return (self.tmp / "runs" / self.run_id / "admin.token").read_text().strip()

    def token(self, role: str = "viewer", name: str = "m16viewer") -> str:
        import httpx

        key = (role, name)
        if key not in self._tokens:
            body: dict[str, Any] = {"role": role, "principal_hint": hint_of(name)}
            if role == "admin":
                body["admin_secret"] = self.admin_secret()
            r = httpx.post(self.base + "/api/auth/token", json=body, timeout=10)
            r.raise_for_status()
            self._tokens[key] = r.json()["token"]
        return self._tokens[key]

    def get(self, path: str, role: str = "viewer", **params: Any) -> Any:
        import httpx

        r = httpx.get(self.base + path, params=params or None, timeout=10,
                      headers={"Authorization": "Bearer " + self.token(role, f"m16{role}")})
        r.raise_for_status()
        return r.json()

    def procs(self) -> list[dict]:
        return list(self.get("/api/sys/procs", role="admin").get("items") or [])

    def pid_of(self, name: str) -> int:
        return int(next(p["pid"] for p in self.procs() if p.get("name") == name))

    # ------------------------------------------------------------ 事件
    def events(self, since: int = 0, types: str | None = None) -> tuple[list[dict], int]:
        kw: dict[str, Any] = {"since": since, "limit": 1000}
        if types:
            kw["types"] = types
        d = self.get("/api/events", **kw)
        return list(d.get("items") or []), int(d.get("next_since") or since)

    def proc_log(self, name: str = "sim-core") -> str:
        """子进程日志（`runs/<run>/logs/<name>.log`）；sim-core 在 api 连接之前发出的事件（如 scenario.invalid）只能从这里读到。"""
        if self.tmp is None:
            return ""
        p = self.tmp / "runs" / self.run_id / "logs" / f"{name}.log"
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def scenario_invalid(self) -> str | None:
        for line in self.proc_log().splitlines():
            if '"scenario invalid"' in line:
                return line[-400:]
        return None

    def wait_event(self, pred: Any, timeout_s: float, types: str | None = None, poll_s: float = 0.5,
                   collect: list[dict] | None = None) -> dict:
        """轮询 `/api/events` 直到 pred(event) 为真；collect 收集途经的全部事件（按 seq 递增）。
        sim-core 日志中出现 "scenario invalid"（121，事件早于 api 连接、事件环中看不到）时立即失败。"""
        since, end = 0, time.monotonic() + timeout_s
        while time.monotonic() < end:
            if self.proc is not None and self.proc.poll() is not None:
                raise RuntimeError("supervisor exited:\n" + "\n".join(self.log[-40:]))
            bad = self.scenario_invalid()
            if bad:
                raise RuntimeError(f"scenario invalid (sim-core log): {bad}")
            items, since = self.events(since, types)
            for e in items:
                if collect is not None:
                    collect.append(e)
                if pred(e):
                    return e
            time.sleep(poll_s)
        raise TimeoutError(f"event not seen within {timeout_s} s")

    # ------------------------------------------------------------ 停止
    def stop(self) -> None:
        p = self.proc
        if p is not None and p.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(30)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(p.pid, signal.SIGKILL)
                p.wait(10)
        if self.tmp is not None:
            shutil.rmtree(self.tmp, ignore_errors=True)
        if self.run_id:
            shutil.rmtree(SHM / self.run_id, ignore_errors=True)

    def __enter__(self) -> Backend:
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()


def write_variant(tmp: Path, doc: dict) -> Path:
    """把剧本变体写入临时剧本目录（`AWR_SCENARIOS_DIR`），其余内置剧本照常可用。"""
    d = tmp / "scenarios"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{doc['scenario_id']}.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return d
