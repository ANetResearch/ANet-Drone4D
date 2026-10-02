"""supervisor：exec 启动、waitpid、心跳巡检、退避、熔断、sys/* 服务、日志、配额与停止顺序（M11-AC-005；M11-FR-012 至 FR-014）。

用假子进程 tests/runtime/fake_child.py（经 init_child 启动）。进程内用例用 LocalBus 与 Supervisor 通信；
子进程用例（supervisor 被杀、zenoh 集成）以 `python -m awr.runtime.supervisor` 启动。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
import rtlib
import yaml

from awr.contracts import bus_keys as K
from awr.runtime.bus import BusTimeout, LocalBus, ZenohBus
from awr.runtime.config import load_runtime_config
from awr.runtime.supervisor import LogSink, Supervisor

FC = str(rtlib.FAKE_CHILD)
_SHM_DIRS: list[Path] = []


@pytest.fixture(autouse=True)
def _cleanup_shm():
    yield
    import shutil

    while _SHM_DIRS:
        shutil.rmtree(_SHM_DIRS.pop(), ignore_errors=True)


def proc(name: str, *args: str, hb: bool = True, stale_s: float = 0.6, grace: float = 5.0, **kw) -> dict:
    d = {"name": name, "cmd": ["python", FC, *args], "liveness": ({"heartbeat": f"hb.{name}", "stale_s": stale_s,
                                                                    "startup_grace_s": grace} if hb else {"mode": "exit_only"})}
    d.update(kw)
    return d


def make_cfg(tmp: Path, procs: list[dict], *, profile: str = "dev", keep: bool = False, backoff=(0.05, 0.05, 0.05, 0.05, 0.05),
             max_count: int = 5, grace_s: float = 2.0, rendezvous: str | None = None, runs_gb: float = 20) -> Path:
    shm = Path(tempfile.mkdtemp(prefix="awr-test-sup-", dir="/dev/shm"))
    _SHM_DIRS.append(shm)
    cfg = {"version": 1,
           "run": {"shm_root": str(shm), "persist_root": str(tmp / "runs"), "world": "shenzhen", "keep_run_dir": keep},
           "net": {"port": 18000},
           "quota": {"runs_gb": runs_gb},
           "cpu": {"pin": "off"},
           "defaults": {"restart": {"policy": "always", "backoff_s": list(backoff), "max": {"count": max_count, "window_s": 60}},
                        "stop": {"grace_s": grace_s}},
           "procs": procs,
           "profiles": {"dev": {"procs_enable": ["core", "ext"]}, "ci": {"procs_enable": ["core", "ext"]}}}
    if rendezvous:
        cfg["bus"] = {"rendezvous": rendezvous}
    p = tmp / "runtime.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return p


class Harness:
    """在当前事件循环中运行 Supervisor（LocalBus），并提供 sys/* 调用。"""

    def __init__(self, cfg_path: Path, profile: str = "dev") -> None:
        self.cfg = load_runtime_config(cfg_path, profile=profile, env={})
        self.sup = Supervisor(self.cfg, bus_kind="local", out=open(os.devnull, "w"))  # noqa: SIM115
        self.task: asyncio.Task | None = None
        self.bus: LocalBus | None = None

    async def __aenter__(self) -> Harness:
        self.task = asyncio.ensure_future(self.sup.run())
        for _ in range(500):
            if self.sup.bus is not None and all(p.state != "STOPPED" or p.spec.on_demand for p in self.sup.procs.values()) \
                    and self.sup.procs:
                break
            await asyncio.sleep(0.01)
        self.bus = LocalBus.open("tester", namespace=self.sup.namespace, loop=asyncio.get_running_loop(), announce=False)
        return self

    async def __aexit__(self, *exc) -> None:
        if self.bus is not None:
            self.bus.close()
        if self.sup.stop_event is not None:
            self.sup.stop_event.set()
        if self.task is not None:
            await asyncio.wait_for(self.task, 30)
        import shutil

        shutil.rmtree(self.sup.shm_root, ignore_errors=True)

    async def call(self, key: str, msg: dict, **kw) -> dict:
        return await self.bus.call(key, msg, **kw)

    async def wait_state(self, name: str, state: str, timeout: float = 10.0) -> float:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if self.sup.procs[name].state == state:
                return time.monotonic() - t0
            await asyncio.sleep(0.002)
        raise AssertionError(f"{name} 未进入 {state}（当前 {self.sup.procs[name].state}）")


def run(coro):
    return asyncio.run(coro)


def test_startup_layout_procs_and_ordered_stop(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, [proc("sim-core", "--mode", "ring", hb=False) | {"liveness": {"ring": "state.sim-core",
                                                                                              "stale_s": 1.0}},
                              proc("api", start_after=["sim-core"]), proc("recorder", layer="ext")])

    async def main() -> None:
        async with Harness(cfg) as h:
            s = h.sup
            for name in ("sim-core", "api", "recorder"):
                await h.wait_state(name, "RUNNING", 10)
            pd = s.persist_dir
            assert stat.S_IMODE(pd.stat().st_mode) == 0o700 and stat.S_IMODE(s.run_dir.stat().st_mode) == 0o700
            for f in ("secret", "admin.token"):
                assert stat.S_IMODE((pd / f).stat().st_mode) == 0o600
            assert len((pd / "secret").read_bytes()) == 32
            assert (pd.parent / "current").resolve() == pd.resolve()
            assert int((pd / "supervisor.pid").read_text()) == os.getpid()
            eff = yaml.safe_load((pd / "effective-config.yaml").read_text())
            assert eff["run"]["id"] == s.run_id and eff["derived"]["enabled_procs"] == ["sim-core", "api", "recorder"]
            meta = json.loads((pd / "meta.json").read_text())
            assert rtlib.schema_errors("rec/meta.schema.json", meta) == []
            rep = await h.call(K.SYS_PROCS, {"v": 1})
            items = {i["name"]: i for i in rep["items"]}
            assert rtlib.schema_errors("rt/payloads/sys_procs.schema.json", {"items": rep["items"]}) == []
            assert all(items[n]["state"] == "RUNNING" and items[n]["pid"] for n in items)
            assert items["api"]["hb_age_ms"] is not None and items["sim-core"]["hb_age_ms"] is not None
            rep = await h.call(K.SYS_PROCS, {"log_tail": True})
            assert any("child started" in line for line in rep["items"][1]["log_tail"])
        # 停止：api → sim-core → 其余；tmpfs 目录删除；SHA256SUMS 生成
        t_api = int((pd / "stopped.api").read_text())
        t_sim = int((pd / "stopped.sim-core").read_text())
        t_rec = int((pd / "stopped.recorder").read_text())
        assert t_api < t_sim < t_rec
        assert not s.run_dir.exists() and (pd / "SHA256SUMS").exists()
        assert all(p.state == "STOPPED" for p in s.procs.values())

    run(main())


def test_kill9_detected_fast_and_restarted_with_backoff(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, [proc("api")], backoff=(0.3, 1, 2, 4, 8))

    async def main() -> None:
        async with Harness(cfg) as h:
            await h.wait_state("api", "RUNNING")
            p = h.sup.procs["api"]
            os.kill(p.pid, signal.SIGKILL)
            dt = await h.wait_state("api", "BACKOFF", 2)
            assert dt <= 0.1  # waitpid：kill -9 ≤ 100 ms 检出（M11-AC-005）
            t0 = time.monotonic()
            await h.wait_state("api", "STARTING", 3)
            assert 0.25 <= time.monotonic() - t0 < 2.0  # 第一次退避 0.3 s（上界留出负载余量）
            await h.wait_state("api", "RUNNING", 5)
            assert p.restarts == 1 and p.last_exit == -9
            log = (h.sup.logs_dir / "api.log").read_text()
            started = [json.loads(x) for x in log.splitlines() if '"child started"' in x]
            assert started[-1]["kv"]["restart_count"] == 1 and started[-1]["kv"]["last_exit"] == -9
            assert list(h.sup.crash_dir.glob("api-*.txt"))

    run(main())


def test_hang_detected_dumped_and_restarted(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, [proc("hangy", "--mode", "hang", "--after", "0.3", stale_s=0.5)])

    async def main() -> None:
        async with Harness(cfg) as h:
            await h.wait_state("hangy", "RUNNING")
            t_run = time.monotonic()
            await h.wait_state("hangy", "BACKOFF", 6)
            assert time.monotonic() - t_run < 5.0  # 挂死 0.5 s 阈值 + 5 Hz 巡检 + SIGTERM 后 1 s SIGKILL（留负载余量）
            p = h.sup.procs["hangy"]
            assert p.last_reason == "hung" and p.last_exit == -signal.SIGKILL
            crash = next(h.sup.crash_dir.glob("hangy-*.txt")).read_text()
            assert "reason=hung" in crash
            assert "Thread" in crash or "File" in crash  # SIGUSR1 触发的 faulthandler 栈

    run(main())


def test_standby_promoted_on_kill_and_hang(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """热备用（`standby`，AWR-19 §4.2，ADR-070）：主进程 RUNNING 后起备用进程；主进程被 kill -9 时由已就绪的备用进程
    接替（pid 即备用进程，环境中的重启计数与上次退出码为接替时的值），随后再起新的备用进程；挂死判定后同样接替；停止时
    备用进程一并退出。"""
    import awr.runtime.supervisor as SUP

    monkeypatch.setattr(SUP, "STANDBY_DELAY_S", 0.05)
    cfg = make_cfg(tmp_path, [proc("api", "--mode", "run", standby=True)], backoff=(0.05, 0.05, 0.05, 0.05, 0.05))

    async def wait_spare(p, timeout: float = 10.0) -> int:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if p.spare is not None and p.spare.ready:
                return p.spare.pid
            await asyncio.sleep(0.01)
        raise AssertionError("备用进程未就绪")

    async def main() -> None:
        async with Harness(cfg) as h:
            p = h.sup.procs["api"]
            await h.wait_state("api", "RUNNING")
            spare = await wait_spare(p)
            assert spare != p.pid
            os.kill(p.pid, signal.SIGKILL)
            await h.wait_state("api", "BACKOFF", 2)
            await h.wait_state("api", "RUNNING", 5)
            assert p.pid == spare and p.restarts == 1 and p.last_exit == -9
            log = (h.sup.logs_dir / "api.log").read_text()
            started = [json.loads(x) for x in log.splitlines() if '"child started"' in x]
            assert started[-1]["kv"]["restart_count"] == 1 and started[-1]["kv"]["last_exit"] == -9
            spare2 = await wait_spare(p)
            assert spare2 not in (spare, None)
            os.kill(p.pid, signal.SIGSTOP)  # 挂死：判定后 SIGKILL，备用进程接替
            await h.wait_state("api", "BACKOFF", 5)
            await h.wait_state("api", "RUNNING", 5)
            assert p.pid == spare2 and p.last_reason == "hung"
            last = await wait_spare(p)
        assert not _alive(last) and not _alive(spare2) and p.spare is None

    run(main())


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            return f.read().rsplit(b")", 1)[1].split()[0] != b"Z"
    except OSError:
        return False


def test_descendants_reaped_after_child_killed(tmp_path: Path) -> None:
    """子进程被 SIGKILL 或判挂死后，其同组后代被回收（FX2-R3-gateway；D1 验收第 2 轮 4.1b：sim-core 的 plan-pool 孤儿）；
    忽略 SIGTERM 的后代在 REAP_GRACE_S 后被 SIGKILL；正常停止时 supervisor 等回收完成再退出。"""
    g1, g2, g3 = (tmp_path / f"g{i}.pid" for i in (1, 2, 3))
    cfg = make_cfg(tmp_path, [proc("api", "--grandchild", str(g1), "--grandchild-ignore-term"),
                              proc("hangy", "--mode", "hang", "--after", "0.5", "--grandchild", str(g2), stale_s=0.5),
                              proc("calm", "--grandchild", str(g3), "--grandchild-ignore-term")], backoff=(5, 5, 5, 5, 5))

    async def wait_pid(f: Path) -> int:
        for _ in range(500):
            if f.exists() and f.read_text().strip():
                return int(f.read_text())
            await asyncio.sleep(0.01)
        raise AssertionError(f"{f} 未写出")

    async def gone(pid: int, timeout: float) -> float:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if not _alive(pid):
                return time.monotonic() - t0
            await asyncio.sleep(0.02)
        raise AssertionError(f"后代 {pid} 仍存活")

    async def main() -> None:
        async with Harness(cfg) as h:
            await h.wait_state("api", "RUNNING")
            p1, p2, p3 = await wait_pid(g1), await wait_pid(g2), await wait_pid(g3)
            assert _alive(p1) and _alive(p2) and _alive(p3)
            os.kill(h.sup.procs["api"].pid, signal.SIGKILL)
            # 退出检测不依赖管道关闭：后代仍持有 stdout 管道（且忽略 SIGTERM）时也在 ≤ 100 ms 内检出（上界留负载余量）
            assert await h.wait_state("api", "BACKOFF", 2) <= 0.3
            assert _alive(p1)
            assert await gone(p1, 4.0) <= 3.0  # 忽略 SIGTERM：REAP_GRACE_S 后 SIGKILL
            await h.wait_state("hangy", "BACKOFF", 8)
            assert h.sup.procs["hangy"].last_reason == "hung"
            assert await gone(p2, 3.0) < 1.0  # 不忽略 SIGTERM：立即结束
            assert h.sup.reaped_groups >= 2
        assert not _alive(p3)  # 正常停止：忽略 SIGTERM 的后代也在 supervisor 退出前被回收

    run(main())


def test_breaker_failed_after_6th_and_manual_reset(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, [proc("crashy", "--mode", "crash", "--after", "0.05")])

    async def main() -> None:
        async with Harness(cfg) as h:
            await h.wait_state("crashy", "FAILED", 20)
            p = h.sup.procs["crashy"]
            assert p.restarts == 5 and len(p.window) == 5  # 首次启动 + 5 次退避重启，第 6 次异常退出熔断
            rep = await h.call(K.SYS_RESTART, {"name": "crashy"})
            assert rep["status"] == "rejected" and rep["code"] == 105
            rep = await h.call(K.SYS_RESTART, {"name": "crashy", "reset_breaker": True, "cid": "c1"})
            assert rep["status"] == "accepted"
            assert p.state in ("STARTING", "RUNNING", "BACKOFF") and len(p.window) <= 1
            rep = await h.call(K.SYS_RESTART, {"name": "nope"})
            assert rep["code"] == 110
            audit = (h.sup.persist_dir / "audit.jsonl").read_text()
            assert '"proc.failed"' in audit and '"sys.restart"' in audit

    run(main())


def test_on_failure_policy_and_startup_timeout(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, [proc("oneshot", "--mode", "exit0", "--after", "0.2", restart={"policy": "on-failure"}),
                              proc("silent", "--mode", "ring", grace=0.6)])

    async def main() -> None:
        async with Harness(cfg) as h:
            await h.wait_state("oneshot", "STOPPED", 10)
            assert h.sup.procs["oneshot"].restarts == 0 and h.sup.procs["oneshot"].last_exit == 0
            await h.wait_state("silent", "BACKOFF", 10)  # 从不写 hb.silent：启动宽限到期视为启动失败
            assert h.sup.procs["silent"].last_reason == "startup_timeout"

    run(main())


def test_module_missing_fails_without_restart_loop(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, [{"name": "ghost", "cmd": ["python", "-m", "awr.not_delivered_yet"],
                               "liveness": {"heartbeat": "hb.ghost", "stale_s": 1}}])

    async def main() -> None:
        async with Harness(cfg) as h:
            await h.wait_state("ghost", "FAILED", 5)
            assert h.sup.procs["ghost"].restarts == 0
            assert h.sup.procs["ghost"].last_reason == "module_missing:awr.not_delivered_yet"

    run(main())


def test_on_demand_start_stop_and_sigterm_grace(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, [proc("replay-worker", on_demand=True, restart={"policy": "on-failure"}),
                              proc("stubborn", "--mode", "ignore-term")], grace_s=0.5)

    async def main() -> None:
        async with Harness(cfg) as h:
            assert h.sup.procs["replay-worker"].state == "STOPPED"
            rep = await h.call(K.SYS_START, {"name": "replay-worker", "args": {"run": "r20260928-143200-a3f1", "segment": 2}})
            assert rep["status"] == "accepted" and rep["pid"]
            await h.wait_state("replay-worker", "RUNNING")
            await asyncio.sleep(0.3)
            assert "args=r20260928-143200-a3f1,2" in (h.sup.logs_dir / "replay-worker.log").read_text()
            rep = await h.call(K.SYS_STOP, {"name": "replay-worker"})
            assert rep["status"] == "accepted" and h.sup.procs["replay-worker"].state == "STOPPED"
            rep = await h.call(K.SYS_START, {"name": "stubborn"})
            assert rep["code"] == 110  # 只接受 on_demand 进程
            with pytest.raises(BusTimeout):  # dev profile 不注册 sys/inject
                await h.call(K.SYS_INJECT, {"name": "stubborn", "fault": "kill"}, timeout=0.2, retries=0)
            await h.wait_state("stubborn", "RUNNING")
            p = h.sup.procs["stubborn"]
            t0 = time.monotonic()
            await h.sup.stop_proc(p)  # 忽略 SIGTERM：grace 0.5 s 后 SIGKILL
            assert 0.4 <= time.monotonic() - t0 < 3 and p.last_exit == -signal.SIGKILL and p.state == "STOPPED"

    run(main())


def test_ci_profile_inject_hooks(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, [proc("api", stale_s=0.5)], profile="ci", backoff=(0.1, 0.1, 0.1, 0.1, 0.1))

    async def main() -> None:
        async with Harness(cfg, profile="ci") as h:
            await h.wait_state("api", "RUNNING")
            rep = await h.call(K.SYS_INJECT, {"name": "api", "fault": "stop_heartbeat"})
            assert rep["status"] == "accepted"
            await h.wait_state("api", "BACKOFF", 5)
            assert h.sup.procs["api"].last_reason == "hung"
            await h.wait_state("api", "RUNNING", 5)
            rep = await h.call(K.SYS_INJECT, {"name": "api", "fault": "hang"})  # SIGSTOP：心跳停止
            await h.wait_state("api", "BACKOFF", 5)
            await h.wait_state("api", "RUNNING", 5)
            rep = await h.call(K.SYS_INJECT, {"name": "api", "fault": "kill"})
            await h.wait_state("api", "BACKOFF", 2)
            assert h.sup.procs["api"].last_exit == -signal.SIGKILL

    run(main())


def test_quota_gc_at_startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from awr.runtime import quota as Q

    monkeypatch.setattr(Q, "GIB", 1 << 20)  # 配额下限 1 GB：测试中把 1 "GB" 缩为 1 MiB
    runs = tmp_path / "runs"
    old = runs / "r20200101-000000-abcd"
    (old / "logs").mkdir(parents=True)
    (old / "logs" / "x.log").write_bytes(b"\0" * (3 << 20))
    kept = runs / "r20200102-000000-abcd"
    (kept / "logs").mkdir(parents=True)
    (kept / "keep").touch()
    cfg = make_cfg(tmp_path, [proc("api")], runs_gb=1)

    async def main() -> None:
        async with Harness(cfg) as h:
            assert not old.exists() and kept.exists()
            assert "runs.evicted" in (h.sup.persist_dir / "audit.jsonl").read_text()

    run(main())


def test_log_sink_rotation_and_tail(tmp_path: Path) -> None:
    sink = LogSink(tmp_path / "x.log", max_bytes=1000, backups=3, tail_lines=200)
    for i in range(500):
        sink.write_line(f'{{"i":{i},"pad":"{"y" * 40}"}}'.encode())
    sink.close()
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["x.log", "x.log.1", "x.log.2", "x.log.3"]
    assert all(p.stat().st_size <= 1000 for p in tmp_path.iterdir())
    assert len(sink.tail) == 200 and json.loads(sink.tail[-1])["i"] == 499


def _children_of(run_id: str) -> list[int]:
    out = []
    for d in Path("/proc").iterdir():
        if d.name.isdigit():
            try:
                if f"AWR_RUN={run_id}".encode() in (d / "environ").read_bytes().split(b"\0"):
                    out.append(int(d.name))
            except OSError:
                pass
    return out


def _start_sup_subprocess(cfg: Path, *extra: str) -> tuple[subprocess.Popen, str]:
    p = subprocess.Popen([sys.executable, "-m", "awr.runtime.supervisor", "-c", str(cfg), *extra],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=rtlib.child_env())
    line = p.stdout.readline()
    assert line.startswith("READY"), line
    run_id = line.split("run=")[1].split()[0]
    return p, run_id


def test_children_exit_within_1s_when_supervisor_killed(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, [proc("sim-core"), proc("api")])
    sup, run_id = _start_sup_subprocess(cfg, "--bus", "local")
    try:
        assert rtlib.wait_until(lambda: len(_children_of(run_id)) == 2, 10)
        kids = _children_of(run_id)
        os.kill(sup.pid, signal.SIGKILL)
        sup.wait(5)
        t0 = time.monotonic()
        alive = lambda: [k for k in kids if Path(f"/proc/{k}").exists() and "Z" not in  # noqa: E731
                         Path(f"/proc/{k}/stat").read_text().split(")")[1].split()[0]]
        assert rtlib.wait_until(lambda: not alive(), 3)
        assert time.monotonic() - t0 <= 1.0
    finally:
        if sup.poll() is None:
            sup.kill()
        for k in _children_of(run_id):
            with contextlib.suppress(ProcessLookupError):
                os.kill(k, signal.SIGKILL)


def test_zenoh_integration_sys_procs_ready_and_cli(tmp_path: Path) -> None:
    port = rtlib.free_port()
    cfg = make_cfg(tmp_path, [proc("sim-core", "--mode", "ring", "--bus", hb=False) | {
        "liveness": {"ring": "state.sim-core", "stale_s": 2.0}}, proc("api", "--bus", start_after=["sim-core"])],
        rendezvous=f"tcp/127.0.0.1:{port}")
    sup, run_id = _start_sup_subprocess(cfg)
    try:
        ns = f"awr/shenzhen/{run_id}"

        async def main() -> None:
            bus = ZenohBus.open("tester", namespace=ns, listen=["tcp/127.0.0.1:0"], connect=[f"tcp/127.0.0.1:{port}"],
                                announce=False, loop=asyncio.get_running_loop())
            try:
                await asyncio.sleep(0.5)
                items = {}
                for _ in range(50):
                    rep = await bus.call(K.SYS_PROCS, {"v": 1})
                    items = {i["name"]: i for i in rep["items"]}
                    if all(i["state"] == "RUNNING" for i in items.values()):
                        break
                    await asyncio.sleep(0.1)
                assert items["sim-core"]["state"] == "RUNNING" and items["api"]["state"] == "RUNNING"
                alive = bus.alive("proc/**", timeout=1.0)
                assert {K.proc_ready("sim-core"), K.proc_ready("api"), K.proc_alive("supervisor")} <= set(alive)
            finally:
                bus.close()

        asyncio.run(main())
        zc = Path(yaml.safe_load((tmp_path / "runtime.yaml").read_text())["run"]["shm_root"]) / run_id / "zenoh.json5"
        z = json.loads(zc.read_text())
        assert z["namespace"] == ns and z["connect"]["endpoints"] == [f"tcp/127.0.0.1:{port}"]
        r = subprocess.run([sys.executable, "-m", "awr.runtime.cli", "-c", str(cfg), "status", "--json"], capture_output=True,
                           text=True, timeout=60, env=rtlib.child_env())
        assert r.returncode == 0, r.stderr
        st = json.loads(r.stdout)
        assert st["run_id"] == run_id and {i["name"] for i in st["procs"]} == {"sim-core", "api"}
        assert st["ring"]["head"] >= 0 and st["ring"]["writer_age_ms"] < 2000
        r = subprocess.run([sys.executable, "-m", "awr.runtime.cli", "-c", str(cfg), "stop"], capture_output=True, text=True,
                           timeout=60, env=rtlib.child_env())
        assert r.returncode == 0, r.stderr
        sup.wait(20)
        assert sup.returncode == 0
    finally:
        if sup.poll() is None:
            sup.kill()
