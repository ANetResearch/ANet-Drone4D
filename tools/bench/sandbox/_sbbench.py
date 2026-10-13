"""tools/bench/sandbox 公共部分（WP-13，M16）：cgroup 模拟栈、1 Hz 采样器、指标汇总、负载生成（D2-AC-04、28、39）。

**cgroup 模拟**（AWR-04 §13"本机（cgroup 模拟）"：以本机 4 核 cpuset 与 §4.4.4 的单元与子组限值模拟 emax）：
`systemd-run --user --scope -p Delegate=yes -p CPUQuota=<Q> -p MemoryHigh=<H> -p MemoryMax=<M>` 建立临时单元，`taskset -c 0-3`
限定 4 核，单元内以 M00 的 `tools/deploy/emax/cgroup-exec.sh` 建立 `core/`、`sandbox/{ctl,plan,sb}` 布局与限值（与 emax 的
systemd 单元同一脚本），再 exec supervisor（`sandbox` profile：共享展示 sim-core、show-api、sandbox-pool、sandbox-api）。
用户 systemd 不支持时（或 `--no-cgroup`）退化为无限值的同一进程组（报告注明 `cgroup: none`，结果不能作为 D2-AC-04 判据）。

**采样**（1 Hz）：每个会话与共享展示的 StateRing 头部（`rtf_milli`、`step_p99_us`、`t_sim_ns`、`epoch`）、各进程 CPU（utime + stime
增量）与内存（RssAnon、RssFile、RssShmem）、sandbox-api `/_inspect`（hub 指标与 `lag_ms`）与 `/health`、单元与子组 cgroup 文件
（`cpu.stat`、`memory.current`、`memory.stat` 的 anon 与 shmem、`memory.events` 的 oom_kill）、supervisor 进程表（重启次数）。
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for p in (ROOT / "python", ROOT / "tools" / "sandbox", ROOT / "tools" / "bench" / "ipc", ROOT / "tools" / "sdk", ROOT / "tests" / "e2e",
          ROOT / "tests" / "chaos"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

SHM = Path("/dev/shm/awr")
CG_FS = Path("/sys/fs/cgroup")
PAGE = os.sysconf("SC_PAGE_SIZE")
TICK = os.sysconf("SC_CLK_TCK")


# ---------------------------------------------------------------- /proc
def proc_cpu_s(pid: int) -> float | None:
    try:
        f = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return (int(f[11]) + int(f[12])) / TICK
    except (OSError, IndexError, ValueError):
        return None


def proc_mem_mb(pid: int) -> dict[str, float]:
    out: dict[str, float] = {}
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            k, _, v = line.partition(":")
            if k in ("RssAnon", "RssFile", "RssShmem", "VmRSS"):
                out[k] = round(int(v.split()[0]) / 1024.0, 2)
    except (OSError, ValueError, IndexError):
        pass
    return out


def proc_env(pid: int) -> dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return {}
    return {k.decode(errors="replace"): v.decode(errors="replace") for k, _, v in (x.partition(b"=") for x in raw if b"=" in x)}


def session_pids(tag: str) -> dict[str, int]:
    """池拉起的会话 sim-core：`{sid: pid}`（环境 `AWR_DETACHED=1` 且 `AWR_SESSION_CONFIG` 位于 tag 目录下）。"""
    out = {}
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        env = proc_env(int(d.name))
        if env.get("AWR_DETACHED") == "1" and tag in env.get("AWR_SESSION_CONFIG", "") and env.get("AWR_RUN"):
            out[env["AWR_RUN"]] = int(d.name)
    return out


def ring_header(path: Path) -> dict[str, Any] | None:
    from awr.contracts import LAYOUT_ID
    from awr.runtime.statering import StateRing

    try:
        with StateRing.attach(path, expect_layout_id=LAYOUT_ID) as r:
            h = r.header()
    except Exception:
        return None
    return {"epoch": h.epoch, "t_sim_ns": h.t_sim_ns, "rtf": h.rtf_milli / 1000.0, "step_p99_us": h.step_p99_us,
            "step_max_us": h.step_max_us, "step_seq": h.step_seq, "rate": h.rate, "clock_state": h.clock_state}


# ---------------------------------------------------------------- cgroup
def read_cg(dirpath: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}

    def kv(name: str) -> dict[str, int]:
        d = {}
        with contextlib.suppress(OSError, ValueError):
            for line in (dirpath / name).read_text().splitlines():
                k, _, v = line.partition(" ")
                d[k] = int(v.split()[0]) if v else 0
        return d

    with contextlib.suppress(OSError, ValueError):
        out["memory_current_mb"] = round(int((dirpath / "memory.current").read_text()) / 1048576, 2)
    ms = kv("memory.stat")
    if ms:
        out["anon_mb"] = round(ms.get("anon", 0) / 1048576, 2)
        out["shmem_mb"] = round(ms.get("shmem", 0) / 1048576, 2)
        out["file_mb"] = round(ms.get("file", 0) / 1048576, 2)
    cs = kv("cpu.stat")
    if cs:
        out["usage_usec"] = cs.get("usage_usec")
        out["nr_throttled"] = cs.get("nr_throttled")
        out["throttled_usec"] = cs.get("throttled_usec")
    ev = kv("memory.events")
    if ev:
        out["oom_kill"] = ev.get("oom_kill", 0)
        out["high"] = ev.get("high", 0)
    return out


def scope_root_of(pid: int) -> Path | None:
    """进程所在单元根（cgroup-exec 布局下去掉末尾的 core 或 sandbox/…）。"""
    try:
        line = next(x for x in Path(f"/proc/{pid}/cgroup").read_text().splitlines() if x.startswith("0::"))
    except (OSError, StopIteration):
        return None
    p = line[3:].strip()
    for tail in ("/core", "/sandbox/ctl", "/sandbox/plan"):
        if p.endswith(tail):
            p = p[: -len(tail)]
    if "/sandbox/sb/" in p:
        p = p.split("/sandbox/sb/")[0]
    return CG_FS / p.lstrip("/")


def systemd_scope_ok() -> bool:
    if shutil.which("systemd-run") is None:
        return False
    try:
        r = subprocess.run(["systemd-run", "--user", "--scope", "-q", "-p", "Delegate=yes", "/bin/true"], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


# ---------------------------------------------------------------- 栈
@dataclass
class Stack:
    """supervisor `sandbox` profile 的栈（可选 cgroup 模拟包装）。"""

    world: str = "synthcity"
    max_sessions: int | str = "auto"
    cgroup: bool = True
    cpu_quota_pct: int = 250
    mem_high: str = "1.8G"          # 与 systemd 单元（anet-drone4d.service）同一写法（D2-FX-R2）
    mem_max: str = "2G"
    cpus: str = "0-3"
    show_scenario: str | None = None
    quota_profile: str = "local"
    ws_max_show: int = 24
    extra_set: list[str] = field(default_factory=list)
    emax_like: bool = True              # 资源配置同 emax 的 public profile（无热备用、不钉核），见 start()
    env: dict[str, str] = field(default_factory=dict)
    proc: subprocess.Popen | None = None
    tmp: Path | None = None
    base: str = ""
    show_base: str = ""
    run_id: str = ""
    log: list[str] = field(default_factory=list)
    cgroup_mode: str = "none"

    def start(self, ready_timeout_s: float = 180.0) -> Stack:
        import httpx
        from awrproc import free_port, numba_warm

        self.tmp = Path(tempfile.mkdtemp(prefix="awr-sbbench-"))
        port, sport, bus = free_port(), free_port(), free_port()
        sup = [sys.executable, "-m", "awr.runtime.supervisor", "--profile", "sandbox", "--set", "net.port_offset=0",
               "--set", f"net.port={port}", "--set", f"bus.rendezvous=tcp/127.0.0.1:{bus}", "--set", f"sandbox.port={sport}",
               "--set", f"sandbox.state_dir={self.tmp / 'sbx'}", "--set", f"sandbox.max_sessions={self.max_sessions}",
               "--set", f"sandbox.quota_profile={self.quota_profile}", "--set", f"net.ws_max={self.ws_max_show}",
               "--set", f"sandbox.cpu_quota_cores={self.cpu_quota_pct / 100.0}", "--set", "run.keep_run_dir=false",
               "--only", "sim-core,api,sandbox-pool,sandbox-api"]
        if self.show_scenario:
            sup += ["--set", f"run.scenario={self.show_scenario}"]
        if self.emax_like:
            # 与 emax 的 public profile 同一资源配置（D2-FX-R2）：不起热备用 sim-core（public profile `defaults.standby: false`，
            # 第 1 轮 core 组多出约 100 MB 匿名页，D2-AC-04/55 的 1.25 GB）、不钉核（emax 4 核时 `cpu.pin: auto` 即关闭；本机 8 核
            # 时 auto 会把共享展示 sim-core 钉在 core1、热备用钉在 taskset 之外的 core7）
            sup += ["--set", "defaults.standby=false", "--set", "cpu.pin=off"]
        for kv in self.extra_set:
            sup += ["--set", kv]
        cmd = sup
        if self.cgroup and systemd_scope_ok():
            cmd = ["systemd-run", "--user", "--scope", "-q", "-p", "Delegate=yes", "-p", f"CPUQuota={self.cpu_quota_pct}%",
                   "-p", f"MemoryHigh={self.mem_high}", "-p", f"MemoryMax={self.mem_max}", "--",
                   "taskset", "-c", self.cpus, str(ROOT / "tools" / "deploy" / "emax" / "cgroup-exec.sh"), *sup]
            self.cgroup_mode = "systemd-scope"
        elif self.cgroup:
            cmd = ["taskset", "-c", self.cpus, *sup]
            self.cgroup_mode = "taskset-only"
        env = dict(os.environ, AWR_RUNS_DIR=str(self.tmp / "runs"), PYTHONUNBUFFERED="1", AWR_WORLD=self.world, **self.env)
        for k in ("AWR_SUPERVISOR_PID", "AWR_PORT_OFFSET", "AWR_SCENARIO", "AWR_PLUGINS", "AWR_SESSION_CONFIG"):
            env.pop(k, None)
        numba_warm(env)
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
        if not ready.wait(ready_timeout_s) or self.proc.poll() is not None:
            tail = "\n".join(self.log[-40:])
            self.stop()
            raise RuntimeError(f"supervisor did not print READY:\n{tail}")
        self.base, self.show_base = f"http://127.0.0.1:{sport}", f"http://127.0.0.1:{port}"
        end = time.monotonic() + 60.0
        while time.monotonic() < end:
            with contextlib.suppress(httpx.HTTPError, ValueError):
                if httpx.get(self.base + "/api/sandbox/v1/health", timeout=2.0).json().get("pool") == "up":
                    return self
            time.sleep(0.25)
        self.stop()
        raise TimeoutError("sandbox-api not healthy")

    @property
    def tag(self) -> str:
        return str(self.tmp)

    def supervisor_pid(self) -> int | None:
        """systemd-run 以 exec 方式运行包装链，进程组首进程最终就是 supervisor。"""
        return self.proc.pid if self.proc is not None else None

    def admin_token(self) -> str:
        import httpx

        assert self.tmp is not None
        sec = (self.tmp / "runs" / self.run_id / "admin.token").read_text().strip()
        r = httpx.post(self.show_base + "/api/auth/token", json={"role": "admin", "principal_hint": "MSIXBENCHADMINHINTAAAAAA",
                                                                 "admin_secret": sec}, timeout=10)
        r.raise_for_status()
        return str(r.json()["token"])

    def viewer_token(self, k: int) -> str:
        import httpx

        hint = ("MSIXBENCHVIEWER" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[k % 26] + "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[(k // 26) % 26] + "AAAAAAAAA")[:24]
        r = httpx.post(self.show_base + "/api/auth/token", json={"role": "viewer", "principal_hint": hint}, timeout=10)
        r.raise_for_status()
        return str(r.json()["token"])

    def procs(self, token: str | None = None) -> list[dict]:
        import httpx

        r = httpx.get(self.show_base + "/api/sys/procs", headers={"Authorization": "Bearer " + (token or self.admin_token())}, timeout=10)
        r.raise_for_status()
        return list(r.json().get("items") or [])

    def stop(self) -> None:
        p = self.proc
        if p is not None and p.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(40)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(p.pid, signal.SIGKILL)
                p.wait(10)
        for pid in session_pids(self.tag).values():
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        if self.tmp is not None:
            shutil.rmtree(self.tmp, ignore_errors=True)
        if self.run_id:
            shutil.rmtree(SHM / self.run_id, ignore_errors=True)


# ---------------------------------------------------------------- 采样
def slim_sample(s: dict) -> dict:
    """内存中保留的精简采样（`summarize`、`rss_slope_mb_per_h` 与窗口 RTF 用到的字段；完整采样照常写 JSONL）。24 h 浸泡 1 Hz
    共 86 400 个采样，完整字典约 9 KB/个（D2-FX-R2-other）。"""
    if "error" in s:
        return s
    keep_p = ("pid", "restarts", "cpu", "VmRSS", "RssAnon", "RssShmem")
    out: dict[str, Any] = {"t": s.get("t"),
                           "procs": {n: {k: d.get(k) for k in keep_p if k in d} for n, d in (s.get("procs") or {}).items()},
                           "sessions": s.get("sessions") or {}}
    r = s.get("show_ring")
    if r:
        out["show_ring"] = {k: r.get(k) for k in ("epoch", "t_sim_ns", "rtf", "step_p99_us")}
    if s.get("sbx_inspect"):
        out["sbx_inspect"] = {"lag_ms": (s["sbx_inspect"] or {}).get("lag_ms")}
    cg = s.get("cg")
    if cg:
        out["cg"] = {"unit": {k: (cg.get("unit") or {}).get(k) for k in ("memory_current_mb", "anon_mb", "shmem_mb", "oom_kill")},
                     "core": {"nr_throttled": (cg.get("core") or {}).get("nr_throttled")},
                     "sandbox": {"oom_kill": (cg.get("sandbox") or {}).get("oom_kill")},
                     "sb": {k: (cg.get("sb") or {}).get(k) for k in ("anon_mb", "shmem_mb")}}
    return out


class Sampler:
    """1 Hz 采样到内存与 JSONL（`slim=True` 时内存中只保留汇总所需字段）。"""

    def __init__(self, stack: Stack, out: Path, period_s: float = 1.0, *, slim: bool = False) -> None:
        self.stack = stack
        self.out = out
        self.period = period_s
        self.slim = slim
        self.samples: list[dict] = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._cpu_prev: dict[int, tuple[float, float]] = {}
        self._token: str | None = None

    def start(self) -> Sampler:
        self._t.start()
        return self

    def _cpu(self, pid: int, now: float) -> float | None:
        c = proc_cpu_s(pid)
        if c is None:
            return None
        prev = self._cpu_prev.get(pid)
        self._cpu_prev[pid] = (now, c)
        if prev is None or now <= prev[0]:
            return None
        return round((c - prev[1]) / (now - prev[0]), 4)

    def one(self) -> dict:
        import httpx

        st = self.stack
        now = time.monotonic()
        s: dict[str, Any] = {"t": round(now, 3)}
        try:
            self._token = self._token or st.admin_token()
            procs = st.procs(self._token)
        except Exception:
            procs = []
        s["procs"] = {}
        for p in procs:
            pid = p.get("pid")
            if pid:
                s["procs"][p["name"]] = {"pid": pid, "restarts": p.get("restarts"), "state": p.get("state"), "cpu": self._cpu(int(pid), now),
                                         **proc_mem_mb(int(pid))}
        s["sessions"] = {}
        for sid, pid in session_pids(st.tag).items():
            s["sessions"][sid] = {"pid": pid, "cpu": self._cpu(pid, now), **proc_mem_mb(pid), "ring": ring_header(SHM / sid / "state.sim-core")}
        if st.run_id:
            s["show_ring"] = ring_header(SHM / st.run_id / "state.sim-core")
        with contextlib.suppress(Exception):
            s["sbx_inspect"] = httpx.get(st.base + "/api/sandbox/v1/_inspect", timeout=2.0).json()
        with contextlib.suppress(Exception):
            s["sbx_health"] = httpx.get(st.base + "/api/sandbox/v1/health", timeout=2.0).json()
        pid0 = (s["procs"].get("sandbox-pool") or {}).get("pid") or st.supervisor_pid()
        root = scope_root_of(int(pid0)) if pid0 else None
        if root is not None and root.exists():
            s["cg"] = {"unit": read_cg(root), "core": read_cg(root / "core"), "sandbox": read_cg(root / "sandbox"),
                       "sb": read_cg(root / "sandbox" / "sb")}
        return s

    def _run(self) -> None:
        with (self.out / "samples.jsonl").open("a", encoding="utf-8") as f:
            while not self._stop.is_set():
                t0 = time.monotonic()
                try:
                    s = self.one()
                except Exception as e:
                    s = {"t": round(t0, 3), "error": f"{type(e).__name__}: {e}"}
                self.samples.append(slim_sample(s) if self.slim else s)
                f.write(json.dumps(s, ensure_ascii=False, default=str) + "\n")
                f.flush()
                self._stop.wait(max(0.0, self.period - (time.monotonic() - t0)))

    def stop(self) -> list[dict]:
        self._stop.set()
        self._t.join(10)
        return self.samples


def p99(xs: list[float]) -> float | None:
    v = [x for x in xs if x is not None]
    return round(float(np.percentile(v, 99)), 4) if v else None


def summarize(samples: list[dict], *, skip_s: float = 30.0) -> dict[str, Any]:
    """采样 → 判据所需的指标（前 skip_s 秒为预热，不计）。"""
    if not samples:
        return {}
    t0 = samples[0]["t"]
    S = [s for s in samples if s["t"] - t0 >= skip_s and "error" not in s] or samples
    out: dict[str, Any] = {"n_samples": len(S)}
    sess: dict[str, dict[str, list]] = {}
    for s in S:
        for sid, d in (s.get("sessions") or {}).items():
            e = sess.setdefault(sid, {"rtf": [], "p99_us": [], "cpu": [], "anon": [], "epoch": []})
            r = d.get("ring") or {}
            e["rtf"].append(r.get("rtf"))
            e["p99_us"].append(r.get("step_p99_us"))
            e["cpu"].append(d.get("cpu"))
            e["anon"].append((d.get("RssAnon") or 0) + (d.get("RssShmem") or 0))
            e["epoch"].append(r.get("epoch"))
    out["sessions"] = {sid: {"rtf_min": min([x for x in e["rtf"] if x is not None] or [0.0]),
                             "rtf_mean": round(float(np.mean([x for x in e["rtf"] if x is not None] or [0.0])), 4),
                             "step_p99_us_max": max([x for x in e["p99_us"] if x is not None] or [0]),
                             "cpu_mean": round(float(np.mean([x for x in e["cpu"] if x is not None] or [0.0])), 4),
                             "anon_shmem_mb_max": max(e["anon"] or [0.0]),
                             "epochs": sorted({x for x in e["epoch"] if x is not None})} for sid, e in sess.items()}
    show = [s.get("show_ring") or {} for s in S]
    out["show"] = {"rtf_min": min([r.get("rtf") for r in show if r.get("rtf") is not None] or [0.0]),
                   "step_p99_us_max": max([r.get("step_p99_us") or 0 for r in show] or [0]),
                   "epochs": sorted({r.get("epoch") for r in show if r.get("epoch") is not None})}
    procs: dict[str, dict[str, list]] = {}
    for s in S:
        for name, d in (s.get("procs") or {}).items():
            e = procs.setdefault(name, {"cpu": [], "rss": [], "restarts": []})
            e["cpu"].append(d.get("cpu"))
            e["rss"].append(d.get("VmRSS"))
            e["restarts"].append(d.get("restarts") or 0)
    out["procs"] = {n: {"cpu_mean": round(float(np.mean([x for x in e["cpu"] if x is not None] or [0.0])), 4),
                        "cpu_p99": p99(e["cpu"]), "rss_mb_max": max([x for x in e["rss"] if x is not None] or [0.0]),
                        "restarts": max(e["restarts"]) - min(e["restarts"])} for n, e in procs.items()}
    lag = [((s.get("sbx_inspect") or {}).get("lag_ms")) for s in S]
    out["sandbox_api_lag_ms_p99"] = p99(lag)
    cg = [s.get("cg") for s in S if s.get("cg")]
    if cg:
        out["cgroup"] = {
            "core_nr_throttled_delta": (cg[-1]["core"].get("nr_throttled") or 0) - (cg[0]["core"].get("nr_throttled") or 0),
            "unit_memory_current_mb_max": max(c["unit"].get("memory_current_mb") or 0 for c in cg),
            "anon_shmem_mb_max": max((c["unit"].get("anon_mb") or 0) + (c["unit"].get("shmem_mb") or 0) for c in cg),
            "sb_anon_shmem_mb_max": max((c["sb"].get("anon_mb") or 0) + (c["sb"].get("shmem_mb") or 0) for c in cg),
            "oom_kill": max(c["unit"].get("oom_kill") or 0 for c in cg) + max(c["sandbox"].get("oom_kill") or 0 for c in cg)}
    return out


def rss_slope_mb_per_h(samples: list[dict], proc: str, *, skip_s: float = 1800.0) -> float | None:
    """进程 RSS 的线性回归斜率（MB/h；前 skip_s 秒为预热）。"""
    if not samples:
        return None
    t0 = samples[0]["t"]
    pts = [(s["t"] - t0, ((s.get("procs") or {}).get(proc) or {}).get("VmRSS")) for s in samples if s["t"] - t0 >= skip_s]
    pts = [(t, v) for t, v in pts if v is not None]
    if len(pts) < 10:
        return None
    t, v = np.array([p[0] for p in pts]), np.array([p[1] for p in pts])
    return round(float(np.polyfit(t / 3600.0, v, 1)[0]), 3)
