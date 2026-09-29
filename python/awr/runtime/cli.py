"""运维 CLI `awr`（入口 `awr = "awr.runtime.cli:main"`；AWR-19 §7.6、§14.1、§16.2；M11-FR-018）。

子命令：run、doctor、config print、status、procs、stop、logs、restart（同 `sys restart`）、ring dump、bus ls、
runs ls|keep|unkeep|gc|verify、token。`awr data *` 由 M03 以插件注册（模块 `awr.<pkg>.awr_cli` 暴露 `register(sub)`），
`awr backup` 属 M00。退出码按 AWR-19 §16.2：0 OK、1 GENERIC、2 CONFIG_INVALID、3 PORT_IN_USE、4 DATA_MISSING、
5 DATA_CORRUPT、6 WORLD_INVALID、7 DISK_LOW、8 DEP_MISSING、9 SHM_UNAVAILABLE、10 STATE_INCOMPATIBLE、11 BUSY、
12 NOT_RUNNING、13 ARCH_UNSUPPORTED、14 PERF_ENV_NOT_READY、15 SERVICE_CHECK_FAILED、20 CORE_PROC_FAILED。
失败时最后一行打印可直接复制执行的修复命令。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import importlib
import json
import os
import platform
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .config import DEFAULT_CONFIG_PATH, ROOT, ConfigError, RuntimeConfig, load_runtime_config

__all__ = ["main"]

EXIT = {"OK": 0, "GENERIC": 1, "CONFIG_INVALID": 2, "PORT_IN_USE": 3, "DATA_MISSING": 4, "DATA_CORRUPT": 5,
        "WORLD_INVALID": 6, "DISK_LOW": 7, "DEP_MISSING": 8, "SHM_UNAVAILABLE": 9, "STATE_INCOMPATIBLE": 10, "BUSY": 11,
        "NOT_RUNNING": 12, "ARCH_UNSUPPORTED": 13, "PERF_ENV_NOT_READY": 14, "SERVICE_CHECK_FAILED": 15}
_PLUGIN_MODULES = ("awr.world.awr_cli", "awr.datasets.awr_cli", "awr.recorder.awr_cli", "awr.jobs.awr_cli")
GIB = 1 << 30


def _fail(code: int, msg: str, fix: str) -> int:
    sys.stderr.write(f"错误（退出码 {code}）：{msg}\n修复：{fix}\n")
    return code


def _load_cfg(a: argparse.Namespace) -> RuntimeConfig:
    return load_runtime_config(getattr(a, "config", None) or DEFAULT_CONFIG_PATH, profile=getattr(a, "profile", None))


def _runs_root(cfg: RuntimeConfig) -> Path:
    p = Path(cfg.run.persist_root)
    return p if p.is_absolute() else ROOT / p


@dataclass
class CurrentRun:
    run_id: str
    persist_dir: Path
    eff: dict
    supervisor_pid: int

    @property
    def alive(self) -> bool:
        return _supervisor_alive(self.supervisor_pid)

    @property
    def run_dir(self) -> Path:
        shm = (self.eff.get("run") or {}).get("shm_root", "/dev/shm/awr")
        return Path(shm) / self.run_id

    @property
    def namespace(self) -> str:
        return f"awr/{(self.eff.get('run') or {}).get('world', 'shenzhen')}/{self.run_id}"

    @property
    def rendezvous(self) -> str:
        return (self.eff.get("derived") or {}).get("rendezvous_effective", "tcp/127.0.0.1:7447")


def _supervisor_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return False
    return "awr.runtime.supervisor" in cmd


def _current_run(cfg: RuntimeConfig) -> CurrentRun | None:
    cur = _runs_root(cfg) / "current"
    if not cur.exists():
        return None
    d = cur.resolve()
    try:
        eff = yaml.safe_load((d / "effective-config.yaml").read_text(encoding="utf-8")) or {}
    except OSError:
        eff = {}
    try:
        pid = int((d / "supervisor.pid").read_text().strip())
    except (OSError, ValueError):
        pid = 0
    return CurrentRun(d.name, d, eff, pid)


def _open_cli_bus(run: CurrentRun):
    from .bus import ZenohBus

    return ZenohBus.open("cli", namespace=run.namespace, listen=["tcp/127.0.0.1:0"], connect=[run.rendezvous], announce=False)


def _call(run: CurrentRun, key: str, msg: dict, timeout: float = 2.0) -> Any:
    bus = _open_cli_bus(run)
    try:
        time.sleep(0.15)  # 等待 gossip 建立到服务方的路由

        async def go() -> Any:
            return await bus.call(key, msg, timeout=timeout, retries=1)

        return asyncio.run(go())
    finally:
        bus.close()


# ---------------------------------------------------------------- run
def cmd_run(a: argparse.Namespace) -> int:
    argv = [sys.executable, "-m", "awr.runtime.supervisor", "-c", str(a.config or DEFAULT_CONFIG_PATH)]
    if a.profile:
        argv += ["--profile", a.profile]
    for s in a.set or []:
        argv += ["--set", s]
    os.execv(sys.executable, argv)
    return 0  # pragma: no cover


# ---------------------------------------------------------------- doctor
@dataclass
class Check:
    id: str
    title: str
    level: str  # OK、WARN、ERROR、SKIP
    code: int
    detail: str
    fix: str = ""


def _port_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _node_version() -> str | None:
    cands = [shutil.which("node"), str(Path.home() / ".local" / "node" / "bin" / "node")]
    for c in cands:
        if c and Path(c).exists():
            try:
                out = subprocess.run([c, "--version"], capture_output=True, text=True, timeout=3).stdout.strip()
                return out.lstrip("v")
            except (OSError, subprocess.SubprocessError):
                continue
    return None


def _pinned_versions() -> dict[str, str]:
    pins: dict[str, str] = {}
    try:
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return pins
    for m in re.finditer(r'"([A-Za-z0-9_.-]+)==([0-9][^"]*)"', text):
        pins[m.group(1).lower()] = m.group(2)
    return pins


def _doctor_checks(cfg: RuntimeConfig | None, cfg_err: ConfigError | None, *, quick: bool, deep: bool) -> list[Check]:
    from importlib.metadata import PackageNotFoundError, version

    out: list[Check] = []
    m = platform.machine().lower()
    out.append(Check("DOC-01", "CPU 架构", "OK" if m in ("x86_64", "amd64") else "ERROR", 13, m, "更换 x86-64 主机（OPS-NFR-011）"))
    pv = ".".join(map(str, sys.version_info[:3]))
    out.append(Check("DOC-02", "Python 3.12", "OK" if sys.version_info[:2] == (3, 12) else "ERROR", 8, pv,
                     "python3.12 -m venv .venv && make setup"))
    nv = _node_version()
    ok = nv is not None and re.fullmatch(r"22\.(1[2-9]|[2-9]\d)\.\d+", nv) is not None
    out.append(Check("DOC-04", "Node 22.12.x", "OK" if ok else "ERROR", 8, nv or "未找到 node", "nvm install 22.12.0（.nvmrc）"))
    # 依赖版本（M11-FR-018：Python 依赖与 zenoh 版本；DOC-08）
    pins = _pinned_versions()
    bad = []
    for pkg in ("eclipse-zenoh", "numpy", "msgpack", "uvicorn", "websockets", "fastapi", "pyyaml", "pydantic"):
        want = pins.get(pkg)
        try:
            have = version(pkg)
        except PackageNotFoundError:
            have = None
        if want and have != want:
            bad.append(f"{pkg} {have} != {want}")
    out.append(Check("DOC-08", "依赖版本（含 zenoh 1.10.1）", "ERROR" if bad else "OK", 8, "; ".join(bad) or "与 pyproject 锁定一致",
                     "make setup"))
    # 配置与访问模式（DOC-22）
    if cfg_err is not None:
        out.append(Check("DOC-22", "配置与访问模式", "ERROR", 2, str(cfg_err), "按键路径修正 configs/runtime.yaml 或 AWR_* 环境变量"))
        return out
    assert cfg is not None
    out.append(Check("DOC-22", "配置与访问模式", "OK", 2, f"profile={cfg.profile} access={cfg.access_mode} bind={cfg.net.bind}"))
    # 端口（DOC-09）
    ports = [cfg.port_effective, cfg.rendezvous_port] + ([cfg.vite_port_effective] if "dev" in cfg.procs_enable else [])
    busy = [p for p in ports if not _port_free(p)]
    out.append(Check("DOC-09", "端口空闲", "ERROR" if busy else "OK", 3,
                     f"被占用：{busy}" if busy else f"{ports} 空闲",
                     f"ss -ltnp | grep -E ':({'|'.join(map(str, busy))})'；或设置 AWR_PORT_OFFSET=<k>" if busy else ""))
    # tmpfs（DOC-10）
    shm = Path(cfg.run.shm_root)
    shm_base = shm if shm.exists() else shm.parent
    try:
        free = shutil.disk_usage(shm_base).free
        writable = os.access(shm_base, os.W_OK)
    except OSError:
        free, writable = 0, False
    lvl = "OK" if writable and free >= GIB else "ERROR"
    out.append(Check("DOC-10", "/dev/shm 可写且 ≥ 1 GiB", lvl, 9, f"{shm_base} free={free / GIB:.1f} GiB writable={writable}",
                     "容器设 shm_size: 1g；清理 /dev/shm/awr 残留"))
    # 残留运行目录（DOC-11）
    stale = []
    if shm.exists():
        for d in shm.iterdir():
            pidf = d / "supervisor.pid"
            with contextlib.suppress(OSError, ValueError):
                if pidf.exists() and not _supervisor_alive(int(pidf.read_text().strip())):
                    stale.append(d.name)
    out.append(Check("DOC-11", "残留运行目录", "WARN" if stale else "OK", 0, f"{stale}" if stale else "无",
                     "启动时自动清理" if stale else ""))
    # 磁盘余量（DOC-12）
    from .quota import disk_status

    ds = disk_status(_runs_root(cfg), cfg.quota.disk_warn_gb, cfg.quota.disk_min_gb)
    out.append(Check("DOC-12", "磁盘余量", "ERROR" if ds.low else ("WARN" if ds.warn else "OK"), 7 if ds.low else 0,
                     f"free={ds.free_gb} GB", "清理 runs/ 或数据盘（make gc-runs）"))
    # 原始数据与世界（DOC-14、DOC-15；--quick 下只告警：make run 在 doctor 之后构建世界）
    raw = Path(os.environ.get("AWR_DATA_DIR", ROOT / "data" / "raw")) / "urbanscene3d"
    plys = sorted(raw.glob("*.ply")) if raw.exists() else []
    out.append(Check("DOC-14", "原始数据", "OK" if plys else "WARN", 0 if plys else 4, f"{len(plys)} 个 PLY（{raw}）",
                     "make fetch-data" if not plys else ""))
    wd = Path(os.environ.get("AWR_WORLDS_DIR", ROOT / "worlds")) / cfg.run.world / "world.json"
    out.append(Check("DOC-15", "默认世界", "OK" if wd.exists() else "WARN", 0 if wd.exists() else 6,
                     f"{wd} {'存在' if wd.exists() else '尚未构建（make run 会自动构建）'}", "make worlds" if not wd.exists() else ""))
    # M11：StateRing 自测与 zenoh 自环（M11-FR-018）
    out.append(_check_ring(shm_base))
    out.append(_check_zenoh())
    if quick:
        return out
    # 非 quick 项
    uvv = None
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        uvv = subprocess.run([str(ROOT / ".venv" / "bin" / "uv"), "--version"], capture_output=True, text=True,
                             timeout=5).stdout.strip()
    out.append(Check("DOC-03", "uv 版本", "OK" if uvv and "0.12.19" in uvv else "ERROR", 8, uvv or "未找到 uv",
                     ".venv/bin/pip install uv==0.12.19"))
    nc = Path(os.environ.get("NUMBA_CACHE_DIR", Path.home() / ".cache" / "awr" / "numba"))
    try:
        nc.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=nc):
            pass
        ok = True
    except OSError:
        ok = False
    out.append(Check("DOC-07", "numba 缓存可写", "OK" if ok else "WARN", 0, str(nc), f"chmod u+w {nc}"))
    from .quota import du, list_runs

    used = sum(du(d) for d in list_runs(_runs_root(cfg)))
    frac = used / (cfg.quota.runs_gb * GIB)
    out.append(Check("DOC-13", "runs/ 配额", "WARN" if frac >= 0.8 else "OK", 0, f"{used / GIB:.2f} / {cfg.quota.runs_gb} GB",
                     "make gc-runs"))
    dist = ROOT / "apps" / "web" / "dist" / "index.html"
    out.append(Check("DOC-16", "前端构建", "OK" if dist.exists() else "WARN", 0, str(dist), "make build"))
    ncpu = os.cpu_count() or 1
    out.append(Check("DOC-17", "CPU 与优先级能力", "OK" if ncpu >= 8 else "WARN", 0, f"{ncpu} 核", "系统级 systemd 单元（19 §4.4）"))
    chrome = Path(os.environ.get("PW_CHROME", Path.home() / ".cache/ms-playwright/chromium-1234/chrome-linux64/chrome"))
    out.append(Check("DOC-19", "测试浏览器", "OK" if chrome.exists() else "WARN", 8, str(chrome), "设置 PW_CHROME"))
    cur = _current_run(cfg)
    if cur is not None:
        bad_perm = []
        for f, want in ((cur.persist_dir, 0o700), (cur.persist_dir / "secret", 0o600), (cur.persist_dir / "admin.token", 0o600)):
            with contextlib.suppress(OSError):
                if f.exists() and (f.stat().st_mode & 0o777) != want:
                    bad_perm.append(f"{f.name}:{oct(f.stat().st_mode & 0o777)}")
        out.append(Check("DOC-20", "秘密与目录权限", "WARN" if bad_perm else "OK", 2, ", ".join(bad_perm) or "0700/0600",
                         f"chmod 700 {cur.persist_dir}; chmod 600 {cur.persist_dir}/secret {cur.persist_dir}/admin.token"))
    if deep:
        rc = subprocess.run([sys.executable, str(ROOT / "tools" / "contracts" / "gen.py"), "--check"], capture_output=True,
                            text=True, timeout=120).returncode if (ROOT / "tools/contracts/gen.py").exists() else 0
        out.append(Check("DOC-06", "契约生成物一致", "OK" if rc == 0 else "ERROR", 8, f"gen.py --check rc={rc}", "make contracts"))
    return out


def _check_ring(shm_base: Path) -> Check:
    try:
        import numpy as np

        from awr.contracts import LAYOUT_ID
        from awr.contracts.layouts import DRONE_STATE64, SWARM_LITE32

        from .statering import StateRing

        d = Path(tempfile.mkdtemp(prefix="awr-doctor-", dir=str(shm_base)))
        try:
            w = StateRing.create(d / "state.selftest", capacity=64, slots=16, layout_id=LAYOUT_ID)
            full, lite = np.zeros(64, DRONE_STATE64), np.zeros(64, SWARM_LITE32)
            full["agent_no"] = np.arange(64)
            w.heartbeat(123, 9)
            fs = w.publish(full, lite, 123, 1)
            r = StateRing.attach(d / "state.selftest", expect_layout_id=LAYOUT_ID)
            r.register(name="doctor")
            f = r.read_latest()
            h = r.header()
            ok = f is not None and f.frame_seq == fs and f.n_rows == 64 and h.t_sim_ns == 123 and h.clock_state == 9
            r.close()
            w.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)
        return Check("M11-R1", "StateRing 自测", "OK" if ok else "ERROR", 9, "publish/attach/read_latest/header",
                     "检查 /dev/shm 权限与容量")
    except Exception as e:  # PlatformUnsupported 等
        code = 13 if type(e).__name__ == "PlatformUnsupported" else 9
        return Check("M11-R1", "StateRing 自测", "ERROR", code, f"{type(e).__name__}: {e}", "检查 /dev/shm 权限与平台")


def _check_zenoh() -> Check:
    try:
        from .bus import ZenohBus

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        ep = f"tcp/127.0.0.1:{port}"
        a = ZenohBus.open("doctor-a", namespace="awr/doctor/selftest", listen=[ep], connect=[], announce=False)
        b = ZenohBus.open("doctor-b", namespace="awr/doctor/selftest", listen=["tcp/127.0.0.1:0"], connect=[ep],
                          announce=False)
        try:
            from awr.contracts import bus_keys

            inbox: queue.SimpleQueue = queue.SimpleQueue()
            a.serve(bus_keys.SYS_PROCS, inbox.put)  # 回调只入队，本线程回复
            t0 = time.perf_counter()
            rep: Any = None
            for _ in range(20):
                time.sleep(0.02)
                box: list = []
                b.call_cb(bus_keys.SYS_PROCS, {}, lambda r, e, box=box: box.append((r, e)), timeout=0.3, retries=0)
                with contextlib.suppress(queue.Empty):
                    inbox.get(timeout=0.3).reply_msg({"ok": True})
                t_end = time.monotonic() + 0.5
                while not box and time.monotonic() < t_end:
                    time.sleep(0.005)
                if box and box[0][0]:
                    rep = box[0][0]
                    break
            ms = (time.perf_counter() - t0) * 1e3
        finally:
            b.close()
            a.close()
        ok = isinstance(rep, dict) and rep.get("ok") is True
        return Check("M11-R2", "zenoh 自环", "OK" if ok else "ERROR", 8, f"query/reply {'成功' if ok else '失败'}（{ms:.0f} ms）",
                     "make setup（检查 eclipse-zenoh 1.10.1）")
    except Exception as e:
        return Check("M11-R2", "zenoh 自环", "ERROR", 8, f"{type(e).__name__}: {e}", "make setup（检查 eclipse-zenoh 1.10.1）")


def cmd_doctor(a: argparse.Namespace) -> int:
    t0 = time.perf_counter()
    cfg, err = None, None
    try:
        cfg = _load_cfg(a)
    except ConfigError as e:
        err = e
    checks = _doctor_checks(cfg, err, quick=a.quick, deep=a.deep)
    first_err = next((c for c in checks if c.level == "ERROR"), None)
    rc = first_err.code if first_err else 0
    if a.json:
        print(json.dumps({"ok": first_err is None, "exit_code": rc, "elapsed_ms": round((time.perf_counter() - t0) * 1e3),
                          "checks": [c.__dict__ for c in checks]}, ensure_ascii=False, indent=1))
    else:
        for c in checks:
            print(f"{c.id:<7} {c.level:<5} {c.title}：{c.detail}")
            if c.level in ("ERROR", "WARN") and c.fix:
                print(f"        修复：{c.fix}")
        print(f"awr doctor{' --quick' if a.quick else ''}: {'通过' if rc == 0 else '失败'}（{(time.perf_counter() - t0):.1f} s）")
        if first_err is not None:
            sys.stderr.write(f"错误（退出码 {rc}）：{first_err.id} {first_err.title}\n修复：{first_err.fix}\n")
    return rc


# ---------------------------------------------------------------- config print
def cmd_config_print(a: argparse.Namespace) -> int:
    try:
        cfg = _load_cfg(a)
    except ConfigError as e:
        return _fail(2, f"配置无效：{e}", "按键路径修正 configs/runtime.yaml 或对应的 AWR_* 环境变量")
    sys.stdout.write(yaml.safe_dump(cfg.effective_dict(), allow_unicode=True, sort_keys=False))
    return 0


# ---------------------------------------------------------------- status / procs
def _fmt_table(rows: list[list[str]]) -> str:
    w = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(str(c).ljust(w[i]) for i, c in enumerate(r)) for r in rows)


def _ring_summary(run: CurrentRun) -> dict | None:
    from awr.contracts import LAYOUT_ID
    from awr.contracts.enums import TIMESTATE_NAMES

    from .statering import StateRing

    p = run.run_dir / "state.sim-core"
    if not p.exists():
        return None
    try:
        r = StateRing.attach(p, expect_layout_id=LAYOUT_ID)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    try:
        h = r.header()
        return {"head": h.head, "t_sim_s": round(h.t_sim_ns / 1e9, 3), "state": TIMESTATE_NAMES.get(h.clock_state & 0x0F),
                "rate": h.rate_milli / 1000, "epoch": h.epoch, "segment": h.segment, "writer_pid": h.writer_pid,
                "writer_age_ms": round(r.writer_age_ms(), 1), "step_seq": h.step_seq, "step_p99_us": h.step_p99_us,
                "rtf": h.rtf_milli / 1000, "roster_version": h.roster_version}
    finally:
        r.close()


def cmd_status(a: argparse.Namespace) -> int:
    cfg = _load_cfg(a)
    run = _current_run(cfg)
    if run is None or not run.alive:
        return _fail(12, "没有活动运行", "make run")
    out: dict[str, Any] = {"run_id": run.run_id, "supervisor_pid": run.supervisor_pid, "ring": _ring_summary(run)}
    if a.offline:
        from .heartbeat import Heartbeat

        out["heartbeats"] = {f.name[3:]: Heartbeat.age_ms(f) for f in sorted(run.run_dir.glob("hb.*"))}
    else:
        from awr.contracts import bus_keys

        try:
            rep = _call(run, bus_keys.SYS_PROCS, {"v": 1})
            out["procs"] = rep.get("items", [])
        except Exception as e:
            out["procs_error"] = str(e)
    if getattr(a, "ws", False):
        out["ws"] = _ws_clients(run)
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    print(f"run {run.run_id}  supervisor pid {run.supervisor_pid}")
    if out.get("ring"):
        print("ring  " + "  ".join(f"{k}={v}" for k, v in out["ring"].items()))
    if "procs" in out:
        rows = [["NAME", "STATE", "PID", "RESTARTS", "UPTIME_S", "HB_AGE_MS", "CPU%", "RSS_MB"]]
        rows += [[i["name"], i["state"], i["pid"] or "-", i["restarts"], i.get("uptime_s") or "-", i.get("hb_age_ms") or "-",
                  i.get("cpu_pct") or "-", i.get("rss_mb") or "-"] for i in out["procs"]]
        print(_fmt_table(rows))
    if "heartbeats" in out:
        for k, v in out["heartbeats"].items():
            print(f"hb.{k}  age_ms={None if v is None else round(v, 1)}")
    if isinstance(out.get("ws"), list):
        rows = [["CONN", "ROLE", "CLIENT", "SUBS", "WINDOW", "ACKED/SEQ", "CREDIT_SKIPS", "SRTT_MS", "KBPS"]]
        rows += [[c.get("conn_id"), c.get("role"), c.get("client") or "-", len(c.get("subs") or []), c.get("window"),
                  f"{c.get('acked')}/{c.get('frame_seq')}", c.get("credit_skips"), c.get("srtt_ms"), c.get("kbps")]
                 for c in out["ws"]]
        print(_fmt_table(rows))
    elif "ws" in out:
        print(f"ws  {out['ws']}")
    if "procs_error" in out:
        return _fail(12, f"sys/procs 无回复：{out['procs_error']}", "awr status --offline")
    return 0


def _ws_clients(run: CurrentRun) -> list[dict] | str:
    """`awr status --ws`：经 api 的 `GET /api/rt/inspect`（R58）列出各 WS 会话（viewer token；demo profile 需要 admin，
    这里不签 admin token，以免占用席位）。"""
    import urllib.error
    import urllib.request

    port = (run.eff.get("derived") or {}).get("port_effective") or (run.eff.get("net") or {}).get("port", 8000)
    base = f"http://127.0.0.1:{port}"
    try:
        req = urllib.request.Request(f"{base}/api/auth/token", data=b'{"role": "viewer", "client": "awr-cli"}',
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=3) as r:
            tok = json.loads(r.read())["token"]
        req = urllib.request.Request(f"{base}/api/rt/inspect", headers={"Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(req, timeout=3) as r:
            return list(json.loads(r.read()).get("clients") or [])
    except urllib.error.HTTPError as e:
        return f"api 返回 {e.code}（demo profile 的 /api/rt/inspect 需要 admin）"
    except (OSError, ValueError, KeyError) as e:
        return f"api 不可达：{e}"


def cmd_procs(a: argparse.Namespace) -> int:
    a.offline = False
    return cmd_status(a)


# ---------------------------------------------------------------- stop
def _children_of_run(run_id: str) -> list[int]:
    pids = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            env = (d / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        if f"AWR_RUN={run_id}".encode() in env:
            pids.append(int(d.name))
    return pids


def cmd_stop(a: argparse.Namespace) -> int:
    cfg = _load_cfg(a)
    run = _current_run(cfg)
    if run is None or run.supervisor_pid <= 0:
        return _fail(12, "没有活动运行", "无需停止")
    if not run.alive:
        if not a.force:
            return _fail(12, f"supervisor（pid {run.supervisor_pid}）已不在运行", "make stop FORCE=1（回收失联运行）")
        pids = [p for p in _children_of_run(run.run_id) if p != os.getpid()]
        print(f"回收失联运行 {run.run_id} 的子进程：{pids}")
        if pids and not a.yes and sys.stdin.isatty() and input("确认 SIGKILL 以上进程？[y/N] ").strip().lower() != "y":
            return 1
        for p in pids:
            with contextlib.suppress(ProcessLookupError):
                os.kill(p, signal.SIGKILL)
        return 0
    os.kill(run.supervisor_pid, signal.SIGTERM)
    t_end = time.monotonic() + a.timeout
    while time.monotonic() < t_end:
        if not _supervisor_alive(run.supervisor_pid):
            print(f"已停止 {run.run_id}")
            return 0
        time.sleep(0.2)
    if a.force:
        with contextlib.suppress(ProcessLookupError):
            os.kill(run.supervisor_pid, signal.SIGKILL)
        for p in _children_of_run(run.run_id):
            with contextlib.suppress(ProcessLookupError):
                os.kill(p, signal.SIGKILL)
        return 0
    return _fail(11, f"supervisor 在 {a.timeout} s 内未退出", "make stop FORCE=1")


# ---------------------------------------------------------------- logs
def cmd_logs(a: argparse.Namespace) -> int:
    cfg = _load_cfg(a)
    run = _current_run(cfg)
    if run is None:
        return _fail(12, "没有运行目录", "make run")
    path = run.persist_dir / "logs" / f"{a.proc}.log"
    if not path.exists():
        return _fail(1, f"日志不存在：{path}", f"ls {run.persist_dir / 'logs'}")
    flt: Callable[[str], bool] = lambda line: True  # noqa: E731
    if a.grep:
        k, _, v = a.grep.partition("=")

        def flt(line: str) -> bool:
            try:
                o = json.loads(line)
            except ValueError:
                return v in line
            val = o.get(k, (o.get("kv") or {}).get(k))
            return str(val) == v

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in [x for x in lines if flt(x)][-a.tail:]:
        print(line)
    if not a.follow:
        return 0
    ino, pos = path.stat().st_ino, path.stat().st_size
    try:
        while True:
            time.sleep(0.2)
            try:
                st = path.stat()
            except FileNotFoundError:
                continue
            if st.st_ino != ino or st.st_size < pos:
                ino, pos = st.st_ino, 0  # 轮转
            if st.st_size > pos:
                with open(path, encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    data = f.read()
                    pos = f.tell()
                for line in data.splitlines():
                    if flt(line):
                        print(line, flush=True)
    except KeyboardInterrupt:
        return 0


# ---------------------------------------------------------------- restart
def cmd_restart(a: argparse.Namespace) -> int:
    from awr.contracts import bus_keys

    cfg = _load_cfg(a)
    run = _current_run(cfg)
    if run is None or not run.alive:
        return _fail(12, "没有活动运行", "make run")
    tok = run.persist_dir / "admin.token"
    try:
        tok.read_text()
    except OSError:
        return _fail(2, f"无法读取管理口令 {tok}", f"以运行 supervisor 的用户执行，或 chmod 600 {tok}")
    try:
        rep = _call(run, bus_keys.SYS_RESTART, {"v": 1, "cid": f"cli-{os.getpid()}", "name": a.proc,
                                                "reset_breaker": not a.no_reset_breaker})
    except Exception as e:
        return _fail(12, f"sys/restart 无回复：{e}", "awr status --offline")
    print(json.dumps(rep, ensure_ascii=False))
    return 0 if rep.get("status") == "accepted" else 1


# ---------------------------------------------------------------- ring dump
def cmd_ring_dump(a: argparse.Namespace) -> int:
    import numpy as np

    from awr.contracts import LAYOUT_ID
    from awr.contracts.layouts import DRONE_STATE64

    from .statering import LayoutMismatch, RingNotReady, StateRing

    try:
        r = StateRing.attach(Path(a.path), expect_layout_id=LAYOUT_ID)
    except LayoutMismatch as e:
        return _fail(10, f"布局不一致：{e}", "全量重启（make stop && make run）")
    except RingNotReady as e:
        return _fail(12, str(e), "确认 sim-core 正在运行")
    try:
        h = r.header()
        f = r.read_latest()
        out: dict[str, Any] = {"path": a.path, "header": h._asdict(), "writer_age_ms": round(r.writer_age_ms(), 1),
                               "cursors": [c for c in r.reader_cursors() if c["mode"] != 0]}
        if f is not None:
            full = np.frombuffer(f.full, DRONE_STATE64)
            out["latest"] = {"frame_seq": f.frame_seq, "t_sim_ns": f.t_sim_ns, "t_pub_ns": f.t_pub_ns, "epoch": f.epoch,
                             "roster_version": f.roster_version, "n_rows": f.n_rows, "flags": f.flags,
                             "first_rows": [{"agent_no": int(x["agent_no"]), "pos": [round(float(v), 3) for v in x["pos"]]}
                                            for x in full[:3]]}
    finally:
        r.close()
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


# ---------------------------------------------------------------- bus ls
def cmd_bus_ls(a: argparse.Namespace) -> int:
    cfg = _load_cfg(a)
    run = _current_run(cfg)
    if run is None or not run.alive:
        return _fail(12, "没有活动运行", "make run")
    bus = _open_cli_bus(run)
    try:
        time.sleep(0.2)
        keys = bus.alive("proc/**", timeout=a.timeout)
    finally:
        bus.close()
    print(f"namespace {run.namespace}")
    for k in keys:
        print(k)
    return 0


# ---------------------------------------------------------------- runs
def cmd_runs(a: argparse.Namespace) -> int:
    from .quota import GIB as _G
    from .quota import du, gc_runs, list_runs

    cfg = _load_cfg(a)
    root = _runs_root(cfg)
    cur = _current_run(cfg)
    if a.runs_cmd == "ls":
        rows = [["RUN", "SIZE_MB", "KEEP", "CURRENT"]]
        rows.extend([d.name, f"{du(d) / (1 << 20):.1f}", "yes" if (d / "keep").exists() else "",
                     "*" if cur and cur.run_id == d.name else ""] for d in list_runs(root))
        print(_fmt_table(rows))
        return 0
    if a.runs_cmd in ("keep", "unkeep"):
        d = root / a.run
        if not d.is_dir():
            return _fail(1, f"运行不存在：{a.run}", "awr runs ls")
        if a.runs_cmd == "keep":
            (d / "keep").touch()
        else:
            (d / "keep").unlink(missing_ok=True)
        return 0
    if a.runs_cmd == "gc":
        res = gc_runs(root, cfg.quota.runs_gb, cur.run_id if cur and cur.alive else None)
        print(json.dumps({"evicted": res.evicted, "used_gb": round(res.used_bytes / _G, 3), "quota_gb": cfg.quota.runs_gb,
                          "over_quota_keep": res.over_quota_keep}, ensure_ascii=False))
        return 0
    if a.runs_cmd == "verify":
        d = root / a.run
        sums = d / "SHA256SUMS"
        if not sums.exists():
            return _fail(1, f"{sums} 不存在（运行尚未正常关闭）", "等待运行结束后重试")
        bad = 0
        for line in sums.read_text().splitlines():
            if not line.strip():
                continue
            h, _, rel = line.partition("  ")
            f = d / rel
            try:
                ok = hashlib.sha256(f.read_bytes()).hexdigest() == h
            except OSError:
                ok = False
            if not ok:
                bad += 1
                print(f"FAILED {rel}")
        print(f"{'OK' if not bad else f'{bad} 个文件不一致'}")
        return 0 if not bad else 5
    return 1


# ---------------------------------------------------------------- token
def cmd_token(a: argparse.Namespace) -> int:
    from .principal import derive_key, encode_token, make_token_payload

    cfg = _load_cfg(a)
    run = _current_run(cfg)
    if run is None:
        return _fail(12, "没有运行目录", "make run")
    try:
        secret = (run.persist_dir / "secret").read_bytes()
        if a.role == "admin":
            (run.persist_dir / "admin.token").read_text()
    except OSError:
        return _fail(2, "无法读取运行秘密或管理口令", "以运行 supervisor 的用户执行")
    mode = (run.eff.get("derived") or {}).get("access_mode", "loopback")
    payload = make_token_payload(a.principal or f"p-cli{os.getuid()}", a.role, run.run_id, mode, now_s=int(time.time()))
    print(encode_token(payload, derive_key(secret, run.run_id, "auth")))
    if a.role != "viewer":
        sys.stderr.write("提示：CLI 签发的 token 不经 api 登记席位（operator、admin 的席位由 POST /api/auth/token 登记）\n")
    return 0


# ---------------------------------------------------------------- 未交付的外部子命令
def _not_delivered(owner: str, what: str) -> Callable[[argparse.Namespace], int]:
    def f(a: argparse.Namespace) -> int:
        return _fail(8, f"{what} 由 {owner} 提供，尚未注册", f"等待 {owner} 交付后 make setup")

    return f


# ---------------------------------------------------------------- 入口
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="awr", description="AWR 运维 CLI（AWR-19 §7.6）")
    ap.add_argument("-c", "--config", default=None, help="configs/runtime.yaml（默认本仓库）")
    ap.add_argument("--profile", default=None, help="dev | demo | ci | perf（默认 AWR_PROFILE 或 dev）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="启动 supervisor（等价于 python -m awr.runtime.supervisor）")
    p.add_argument("--set", action="append", default=[])
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("doctor", help="环境诊断（19 §14.1）")
    p.add_argument("--quick", action="store_true")
    p.add_argument("--deep", action="store_true")
    p.add_argument("--upgrade-check", action="store_true", help="V0.1 暂不支持，等同默认检查")
    p.add_argument("--field", action="store_true", help="V0.5")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("config", help="配置")
    cs = p.add_subparsers(dest="config_cmd", required=True)
    pp = cs.add_parser("print", help="打印生效配置（秘密掩码）")
    pp.add_argument("--profile", dest="profile_override", default=None)
    pp.set_defaults(fn=lambda a: cmd_config_print(_apply_profile(a)))

    for name, fn, hlp in (("status", cmd_status, "进程表、环头部与心跳"), ("procs", cmd_procs, "进程表（sys/procs）")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("--offline", action="store_true")
        p.add_argument("--ws", action="store_true", help="按会话列出 WS 客户端字段（经 GET /api/rt/inspect）")
        p.add_argument("--json", action="store_true")
        p.set_defaults(fn=fn)

    p = sub.add_parser("stop", help="停止当前运行")
    p.add_argument("--force", action="store_true")
    p.add_argument("--yes", action="store_true", help="--force 时不询问")
    p.add_argument("--timeout", type=float, default=30.0)
    p.set_defaults(fn=cmd_stop)

    p = sub.add_parser("logs", help="查看或跟随日志")
    p.add_argument("proc")
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("--grep", default=None, help="key=value（按 JSON 字段或 kv 过滤）")
    p.add_argument("--tail", type=int, default=200)
    p.set_defaults(fn=cmd_logs)

    p = sub.add_parser("restart", help="重启进程（默认带 reset_breaker，需要管理口令文件可读）")
    p.add_argument("proc")
    p.add_argument("--no-reset-breaker", action="store_true")
    p.set_defaults(fn=cmd_restart)

    p = sub.add_parser("sys", help="sys 子命令")
    ss = p.add_subparsers(dest="sys_cmd", required=True)
    pr = ss.add_parser("restart")
    pr.add_argument("proc")
    pr.add_argument("--no-reset-breaker", action="store_true")
    pr.set_defaults(fn=cmd_restart)
    pl = ss.add_parser("loglevel", help="P1，api 交付后提供")
    pl.add_argument("proc")
    pl.add_argument("logger")
    pl.add_argument("lvl")
    pl.set_defaults(fn=_not_delivered("M11 api", "awr sys loglevel"))

    p = sub.add_parser("ring", help="StateRing 工具")
    rs = p.add_subparsers(dest="ring_cmd", required=True)
    pd = rs.add_parser("dump", help="头部与最新槽摘要")
    pd.add_argument("path")
    pd.set_defaults(fn=cmd_ring_dump)

    p = sub.add_parser("bus", help="总线工具")
    bs = p.add_subparsers(dest="bus_cmd", required=True)
    pb = bs.add_parser("ls", help="liveliness 探测")
    pb.add_argument("--timeout", type=float, default=0.5)
    pb.set_defaults(fn=cmd_bus_ls)

    p = sub.add_parser("runs", help="运行目录")
    rs2 = p.add_subparsers(dest="runs_cmd", required=True)
    rs2.add_parser("ls").set_defaults(fn=cmd_runs)
    for n in ("keep", "unkeep", "verify"):
        q = rs2.add_parser(n)
        q.add_argument("run")
        q.set_defaults(fn=cmd_runs)
    rs2.add_parser("gc").set_defaults(fn=cmd_runs)

    p = sub.add_parser("token", help="签发 token（脚本与排查用）")
    p.add_argument("role", choices=("viewer", "operator", "admin"))
    p.add_argument("--principal", default=None)
    p.set_defaults(fn=cmd_token)

    p = sub.add_parser("backup", help="备份（M00）")
    p.add_argument("--target", default=None)
    p.set_defaults(fn=_not_delivered("M00", "awr backup"))

    for mod in _PLUGIN_MODULES:
        try:
            m = importlib.import_module(mod)
        except ImportError:
            continue
        reg = getattr(m, "register", None)
        if callable(reg):
            reg(sub)
    if "data" not in sub.choices:
        p = sub.add_parser("data", help="数据（M03）")
        p.add_argument("rest", nargs=argparse.REMAINDER)
        p.set_defaults(fn=_not_delivered("M03", "awr data"))
    return ap


def _apply_profile(a: argparse.Namespace) -> argparse.Namespace:
    if getattr(a, "profile_override", None):
        a.profile = a.profile_override
    return a


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    try:
        return int(a.fn(a) or 0)
    except ConfigError as e:
        return _fail(2, f"配置无效：{e}", "按键路径修正 configs/runtime.yaml 或对应的 AWR_* 环境变量")
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
