#!/usr/bin/env python3
"""tools/deploy/emax/observe.py：公开演示站的线上观测（D2-AC-27，P1；AWR-04 §13 第 27 行、§4.4.1、§4.4.6；ADR-094）。

D2-AC-27 只能在 emax 上测量：部署后 72 h 内 OOM 0、非预期重启 0、共享展示 RTF ≥ 0.99；在 emax 实测 §4.4.1 的各项成本与世界成本表
（再由文档总编以 ADR 冻结 N_max）；S7 连续 7 天的线上统计（被发现 0、间隙按原因分列）。本脚本是部署套件的一部分，与服务并排运行，
只读取、不改动任何服务状态；除读取 StateRing 外只用标准库。请用发布的 .venv 执行（/opt/anet-drone4d/current/.venv/bin/python）。

  run      采样到 OUT 目录（可中断后用同一 OUT 续跑；以 root 执行：读取 0600 的 EnvironmentFile 后降权为 awr 读取 supervisor 状态，
           经 sbctl.sh 读取沙盒池状态）：
             samples.jsonl  每 --interval 秒：systemd 单元（InvocationID、NRestarts、MainPID、状态）、当前发布、cgroup 统计（单元根、
                            core/、sandbox/、sandbox/{sb,plan,ctl} 与每会话叶子组的 memory.events、memory.current/peak、
                            memory.stat 的 anon 与 shmem、cpu.stat 的 usage_usec 与 nr_throttled）、sandbox-api 健康；
                            该间隔内 StateRing 读数的汇总（`ring`）；每 --status-every 秒：`awr status --json`（supervisor 进程表的
                            restarts 与环头部）；每 --pool-every 秒：`sbctl.sh --json status`（N_max、降级级别、各会话世界与 epoch）
             events.jsonl   每 --events-every 秒增量拉取 show-api `/api/events`（viewer token，经回环 127.0.0.1:18640 并带站点 Host）
                            中的 target.discovered、relay.*、capture.*、sandbox.* 与 sim.* 事件
             每 --kernel-every 秒：内核日志中提到本单元的 OOM 行（journalctl -k）
             state.json     续跑所需的游标（事件 seq、开始时刻）；maintenance.jsonl 维护窗口（mark 子命令写入）
  mark     登记维护窗口（例如手工重启、install.sh 之外的变更）：窗口内的服务重启与 RTF 不计入判据
  report   由 OUT 计算 report.json（`awr.deploy.observe.v1`）并打印结论；run 结束时也会写一次

判据（report）：
  ① OOM：单元根 memory.events 的 oom_kill 增量（cgroup v2 的 memory.events 为层级计数，含 core/ 与全部沙盒叶子组）为 0，
     且内核日志中没有提到本单元的 OOM 行；
  ② 非预期重启：单元 InvocationID 变化中既不伴随发布切换（current 指向变化）也不在维护窗口内的次数，加上同一 InvocationID 内 supervisor
     进程表 restarts 计数（退避重启：sim-core、show-api、sandbox-pool、sandbox-api 等）的增量，合计为 0；沙盒会话的 epoch 增加只记录
     （驻留恢复也会增加 epoch）；沙盒会话的崩溃、挂死与看门狗重启按池状态行的 `restarts` 增量计入（D2-INT 起提供）；
  ③ 共享展示 RTF ≥ 0.99：后台线程每 --ring-every 秒（缺省 0.5 s）在进程内读取共享展示 StateRing 的头部（需用发布的 .venv 运行；
     只映射、不登记读者游标），取 sim-core 逐秒自报 rtf（`rtf_milli`，1 s 窗口）的最小值与相邻样本间 Δt_sim / Δt_wall 的最小值，
     心跳超过 2 s 未更新的读数按 0 计；时钟在 STOPPED/PAUSED/STEPPING/ENDED/RESTARTING、倍速非 ×1、维护窗口内、重启或 epoch 变化后
     120 s 内的读数与窗口不计。不能导入 awr 时退回 `awr status` 的稀疏采样（≥ 300 s 窗口），结论最多为"未完成"；
  ④ 时长与覆盖：观测时长 ≥ --hours（缺省 72 h）且采样覆盖率 ≥ 0.95，否则结论为"未完成"（退出码 3）；
  ⑤ S7（观测时长 ≥ 168 h 时给出"7 天"结论）：带我方机体的 target.discovered 为 0；relay.gap 按 cause 分列次数、合计与最长时长；
     事件游标过期（410）记为事件缺口，结论为"未完成"。
  成本（§4.4.1 的输入，不参与判据）：core/ 与 sandbox/ 的核数均值与 p95、按世界的每会话核数与匿名页、规划与控制组核数、单元
  memory.peak、降级级别时长分布、生效 N_max。N_max 的冻结由文档总编以 ADR 完成，本脚本不改配置。

用法（emax，root）：
  PY=/opt/anet-drone4d/current/.venv/bin/python
  sudo $PY /opt/anet-drone4d/current/tools/deploy/emax/observe.py run --hours 168 \\
       --out /opt/anet-drone4d/var/observe/$(date -u +%Y%m%dT%H%MZ)          # 7 天，前 72 h 同时给出服务判据
  sudo $PY …/observe.py mark /opt/anet-drone4d/var/observe/<dir> --minutes 20 --note "手工重启 nginx"
  $PY …/observe.py report /opt/anet-drone4d/var/observe/<dir> --hours 72
退出码：0 通过；1 不通过；2 参数错误；3 未完成（时长、覆盖或事件不足）。观测进程自身的 CPU 记入报告 `observer`。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import resource
import secrets
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

SCHEMA_SAMPLE = "awr.deploy.observe.sample.v1"
SCHEMA_REPORT = "awr.deploy.observe.v1"
EXIT_OK, EXIT_FAIL, EXIT_ARGS, EXIT_INCOMPLETE = 0, 1, 2, 3
RTF_MIN = 0.99
COVERAGE_MIN = 0.95
S7_HOURS = 168.0
EVENT_PREFIXES = ("target.discovered", "relay.", "capture.", "sandbox.", "sim.")
CGROUP_GROUPS = ("", "core", "sandbox", "sandbox/sb", "sandbox/plan", "sandbox/ctl")
SID_RE = re.compile(r"^sb-[a-z2-7]{10}$")
# 时钟状态（awr.contracts.enums.TIMESTATE_NAMES）：由操作或剧本决定、不反映仿真能力的状态不计 RTF；STALLED、FAILED 等照常计入
CLOCK_EXCLUDED = frozenset({"STOPPED", "PAUSED", "STEPPING", "ENDED", "RESTARTING"})
STATUS_PY = "-m awr.runtime.cli status --json"
STALE_MS = 2000.0          # StateRing 心跳超过 2 s 未更新视为 sim-core 停顿（该次读取按 RTF 0 计）
RTF_FALLBACK_WINDOW_S = 300.0


@dataclass
class Env:
    """部署布局与可替换的外部调用（测试注入假的 systemctl、runuser 与 cgroupfs）。"""

    prefix: Path = Path("/opt/anet-drone4d")
    env_file: Path = Path("/etc/anet-drone4d/env")
    svc: str = "anet-drone4d"
    user: str = "awr"
    show_base: str = "http://127.0.0.1:18640"
    sb_base: str = "http://127.0.0.1:18641"
    host: str | None = "drone4d.agentnetwork.org.cn"
    cgroup_fs: Path = Path(os.environ.get("AWR_CGROUP_FS", "/sys/fs/cgroup"))
    status_cmd: list[str] | None = None      # 缺省按 prefix 组装（见 status_call）
    pool_cmd: list[str] | None = None
    ring: Any = None                          # 有 read() -> dict | None 的对象；None 时用 RingReader（需要发布的 .venv）
    # (argv, timeout, env, user) -> (rc, stdout)；env 为 None 时继承本进程环境，user 为 None 时不降权
    run: Callable[..., tuple[int, str]] = field(default=None)  # type: ignore[assignment]
    now: Callable[[], float] = time.time
    mono: Callable[[], float] = time.monotonic
    is_root: bool = field(default_factory=lambda: os.geteuid() == 0)

    def __post_init__(self) -> None:
        if self.run is None:
            self.run = run_cmd


def run_cmd(argv: list[str], timeout: float, env: dict[str, str] | None = None, user: str | None = None) -> tuple[int, str]:
    """子进程；`user` 只在 root 下生效（subprocess 的 user/group 降权，环境经 env= 传递而不出现在命令行上）。"""
    kw: dict[str, Any] = {}
    if user is not None:
        kw.update(user=user, group=user, extra_groups=[])
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False, env=env, **kw)
    except (OSError, subprocess.TimeoutExpired, KeyError, ValueError) as e:
        return 127, f"{type(e).__name__}: {e}"
    return p.returncode, p.stdout


def _read_env_file(p: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        k = k.strip()
        if re.fullmatch(r"[A-Z_][A-Z0-9_]*", k):
            out[k] = v.strip().strip('"').strip("'")
    return out


def status_call(env: Env) -> tuple[list[str], dict[str, str] | None, str | None]:
    """`awr status --json` 的命令、环境与降权用户：与单元相同的 EnvironmentFile（public profile、runs 目录等；该文件 0600，
    因此观测以 root 执行），root 下降权为 awr。环境经 env= 传递，不出现在任何进程的命令行上。"""
    if env.status_cmd:
        return list(env.status_cmd), None, None
    py = str(env.prefix / "current" / ".venv" / "bin" / "python")
    child = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"} | _read_env_file(env.env_file)
    # 单线程导入：本机实测单次 CPU 由 1.8 s 降到 0.79 s（numpy 与 BLAS 线程池），每 60 s 一次约 0.013 核
    child |= {"HOME": str(env.prefix), "PYTHONDONTWRITEBYTECODE": "1", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
              "MKL_NUM_THREADS": "1", "NUMBA_NUM_THREADS": "1"}
    return [py, *shlex.split(STATUS_PY)], child, (env.user if env.is_root else None)


def pool_argv(env: Env) -> list[str]:
    if env.pool_cmd:
        return list(env.pool_cmd)
    return ["bash", str(env.prefix / "current" / "tools" / "deploy" / "emax" / "sbctl.sh"), "--json", "status"]


# ---------------------------------------------------------------- 探针


def probe_systemd(env: Env) -> dict[str, Any]:
    props = ("InvocationID", "NRestarts", "MainPID", "ActiveState", "SubState", "ControlGroup")
    rc, out = env.run(["systemctl", "show", env.svc, "--no-pager", *[a for p in props for a in ("-p", p)]], 10.0)
    if rc != 0:
        return {"error": f"systemctl rc={rc}"}
    d: dict[str, Any] = {}
    for ln in out.splitlines():
        if "=" in ln:
            k, v = ln.split("=", 1)
            d[k] = v
    for k in ("NRestarts", "MainPID"):
        if str(d.get(k, "")).isdigit():
            d[k] = int(d[k])
    return d


def probe_kernel_oom(env: Env, since_t: float) -> list[str]:
    """内核日志中自 since_t 起提到本单元的 OOM 行（与 memory.events 互为印证；journalctl 不可用时为空）。"""
    rc, out = env.run(["journalctl", "-k", "--no-pager", "-q", "-o", "short-unix", f"--since=@{int(since_t)}"], 30.0)
    if rc != 0:
        return []
    pat = re.compile(r"oom-kill|Out of memory|Memory cgroup out of memory", re.IGNORECASE)
    return [ln.strip() for ln in out.splitlines() if pat.search(ln) and env.svc in ln][:100]


def probe_release(env: Env) -> str | None:
    try:
        return Path(os.readlink(env.prefix / "current")).name
    except OSError:
        return None


def _kv(p: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        for ln in p.read_text(encoding="ascii").splitlines():
            parts = ln.split()
            if len(parts) == 2 and parts[1].lstrip("-").isdigit():
                out[parts[0]] = int(parts[1])
    except OSError:
        pass
    return out


def _int(p: Path) -> int | None:
    try:
        s = p.read_text(encoding="ascii").strip()
    except OSError:
        return None
    return int(s) if s.isdigit() else None


def _group_stats(d: Path, *, full: bool) -> dict[str, Any] | None:
    if not d.is_dir():
        return None
    cpu = _kv(d / "cpu.stat")
    out: dict[str, Any] = {"usage_usec": cpu.get("usage_usec"), "mem": _int(d / "memory.current")}
    if full:
        ev = _kv(d / "memory.events")
        ms = _kv(d / "memory.stat")
        out.update({"nr_throttled": cpu.get("nr_throttled"), "throttled_usec": cpu.get("throttled_usec"),
                    "peak": _int(d / "memory.peak"), "anon_shmem": (ms.get("anon", 0) + ms.get("shmem", 0)) if ms else None,
                    "oom": ev.get("oom"), "oom_kill": ev.get("oom_kill"), "mem_high": ev.get("high"), "mem_max": ev.get("max")})
    return out


def probe_cgroup(env: Env, control_group: str | None) -> dict[str, Any] | None:
    if not control_group:
        return None
    root = env.cgroup_fs / control_group.lstrip("/")
    if not root.is_dir():
        return None
    groups = {g or ".": s for g in CGROUP_GROUPS if (s := _group_stats(root / g if g else root, full=True)) is not None}
    leaves: dict[str, Any] = {}
    sb = root / "sandbox" / "sb"
    if sb.is_dir():
        for c in sorted(sb.iterdir()):
            if c.is_dir() and SID_RE.match(c.name) and (s := _group_stats(c, full=False)) is not None:
                leaves[c.name] = s
    return {"groups": groups, "leaves": leaves}


def _json_out(rc: int, out: str) -> dict | None:
    if rc != 0:
        return None
    try:
        d = json.loads(out)
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def probe_status(env: Env) -> dict[str, Any] | None:
    argv, child, user = status_call(env)
    d = _json_out(*env.run(argv, 30.0, child, user))
    if d is None:
        return None
    ring = d.get("ring") or {}
    procs = [{k: p.get(k) for k in ("name", "state", "pid", "restarts", "last_exit")} for p in d.get("procs") or [] if isinstance(p, dict)]
    return {"run_id": d.get("run_id"), "supervisor_pid": d.get("supervisor_pid"),
            "ring": {k: ring.get(k) for k in ("t_sim_s", "state", "rate", "epoch", "writer_pid", "rtf", "step_p99_us")},
            "procs": procs}


def runs_dir(env: Env) -> Path:
    return Path(_read_env_file(env.env_file).get("AWR_RUNS_DIR") or env.prefix / "var" / "runs")


class RingReader:
    """共享展示 StateRing 头部的进程内读取（RTF 的主口径）。sim-core 每 1 s 写一次 `rtf_milli`（`_write_step_stats`），以
    --ring-every（缺省 0.5 s）读取时每个 1 s 窗口至少读到一次；心跳超过 STALE_MS 未更新视为停顿。需要发布的 .venv
    （`awr.runtime.statering`）；只映射并读头部，不登记读者游标（不占用 8 个游标行）；sim-core 或 supervisor 重启后环文件换新，
    按路径与 inode 重新映射。环文件在共享内存目录：`<run.shm_root>/<run_id>/state.sim-core`（run_id 取 `<runs>/current` 的指向，
    shm_root 取该运行的 effective-config.yaml，缺省 /dev/shm/awr，与 `awr status` 的 CurrentRun.run_dir 相同）。"""

    def __init__(self, runs: Path) -> None:
        from awr.contracts import LAYOUT_ID
        from awr.contracts.enums import TIMESTATE_NAMES
        from awr.runtime.statering import StateRing

        self._cls, self._layout, self._names = StateRing, LAYOUT_ID, TIMESTATE_NAMES
        self.runs = runs
        self.ring: Any = None
        self.key: tuple[str, int] | None = None
        self.errors = 0
        self._shm: tuple[str, Path] | None = None   # (run_id, 环目录)

    @staticmethod
    def available() -> bool:
        try:
            import awr.runtime.statering  # noqa: F401
        except Exception:
            return False
        return True

    def close(self) -> None:
        if self.ring is not None:
            with contextlib.suppress(Exception):
                self.ring.close()
        self.ring, self.key = None, None

    def ring_path(self) -> Path:
        run = (self.runs / "current").resolve()
        if self._shm is None or self._shm[0] != run.name:
            shm = "/dev/shm/awr"
            try:
                import yaml  # 发布的 .venv 有 pyyaml（requirements.lock）

                eff = yaml.safe_load((run / "effective-config.yaml").read_text(encoding="utf-8")) or {}
                shm = str((eff.get("run") or {}).get("shm_root") or shm)
            except Exception:
                pass
            self._shm = (run.name, Path(shm) / run.name)
        return self._shm[1] / "state.sim-core"

    def read(self) -> dict[str, Any] | None:
        try:
            p = self.ring_path()
            key = (str(p), p.stat().st_ino)
            if self.ring is None or self.key != key:
                self.close()
                self.ring, self.key = self._cls.attach(p, expect_layout_id=self._layout), key
            h = self.ring.header()
            return {"t_sim_s": h.t_sim_ns / 1e9, "epoch": int(h.epoch), "state": self._names.get(h.clock_state & 0x0F),
                    "rate": h.rate_milli / 1000.0, "rtf": h.rtf_milli / 1000.0, "age_ms": float(self.ring.writer_age_ms())}
        except Exception:
            self.errors += 1
            self.close()
            return None


class RingAcc:
    """一个采样间隔内的 StateRing 读数汇总（线程安全）：最后一次读数、逐秒 rtf 的最小值、心跳年龄最大值、出现过的 epoch 与状态。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self._reset()

    def _reset(self) -> None:
        self.n = 0
        self.last: dict | None = None
        self.rtf_min: float | None = None
        self.age_max = 0.0
        self.epochs: set[int] = set()
        self.states: set[str] = set()
        self.rates: set[float] = set()

    def add(self, r: dict | None, mono: float) -> None:
        if r is None:
            return
        with self.lock:
            self.n += 1
            self.last = r | {"mono": round(mono, 3)}
            self.epochs.add(int(r["epoch"]))
            self.states.add(str(r["state"]))
            self.rates.add(float(r["rate"]))
            age = float(r["age_ms"]) if math.isfinite(float(r["age_ms"])) else 1e9
            self.age_max = max(self.age_max, age)
            if r["state"] not in CLOCK_EXCLUDED:
                x = float(r["rtf"]) if age <= STALE_MS else 0.0
                self.rtf_min = x if self.rtf_min is None else min(self.rtf_min, x)

    def flush(self) -> dict[str, Any]:
        with self.lock:
            last = self.last or {}
            d = {"reads": self.n, "t_sim_s": None if not last else round(float(last["t_sim_s"]), 4), "mono": last.get("mono"),
                 "epoch": last.get("epoch"), "state": last.get("state"), "rate": last.get("rate"),
                 "rtf_min": None if self.rtf_min is None else round(self.rtf_min, 4), "age_max_ms": round(self.age_max, 1),
                 "epochs": sorted(self.epochs), "states": sorted(self.states), "rates": sorted(self.rates)}
            self._reset()
            return d


def probe_pool(env: Env) -> dict[str, Any] | None:
    d = _json_out(*env.run(pool_argv(env), 30.0))
    if d is None:
        return None
    sess = [{k: s.get(k) for k in ("sid", "state", "world", "epoch", "restarts", "granted_rate", "anon_mb")} for s in d.get("sessions") or []
            if isinstance(s, dict)]
    return {k: d.get(k) for k in ("n_max", "q_cores", "delegated", "accepting", "degrade_level", "queue_len")} | {"sessions": sess}


def _http(env: Env, base: str, path: str, *, method: str = "GET", body: dict | None = None, token: str | None = None,
          timeout: float = 10.0) -> tuple[int, Any]:
    headers = {"User-Agent": "anet-drone4d-observe", "Accept": "application/json"}
    if env.host:
        headers["Host"] = env.host
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (OSError, ValueError):
        return 0, {}


def probe_sandbox_health(env: Env) -> dict[str, Any] | None:
    st, d = _http(env, env.sb_base, "/api/sandbox/v1/health")
    if st == 0 or not isinstance(d, dict):
        return None
    return {"http": st} | {k: d.get(k) for k in ("ok", "pool", "sessions", "n_max", "uptime_s")}


class EventTail:
    """show-api `/api/events` 增量拉取：viewer token（401 时重新签发），`types` 前缀过滤，按 seq 去重写入 events.jsonl。"""

    def __init__(self, env: Env, out: Path, since: int = 0) -> None:
        self.env, self.out, self.since = env, out, since
        self.token: str | None = None
        self.lost: list[dict] = []

    def _token(self) -> str | None:
        hint = "OBSERVE" + secrets.token_hex(8).upper()
        st, d = _http(self.env, self.env.show_base, "/api/auth/token", method="POST", body={"role": "viewer", "principal_hint": hint})
        return str(d.get("token")) if st == 200 and isinstance(d, dict) and d.get("token") else None

    def poll(self) -> int:
        n = 0
        for _ in range(50):
            if self.token is None:
                self.token = self._token()
                if self.token is None:
                    return n
            # 不带 types：带前缀过滤时 next_since 只推进到最后一个匹配项，游标会落后并在事件环滚动后误报 410
            st, d = _http(self.env, self.env.show_base, f"/api/events?since={self.since}&limit=1000", token=self.token)
            if st == 401:
                self.token = None
                continue
            if st == 410:
                oldest = int(((d or {}).get("detail") or {}).get("oldest_seq") or (d or {}).get("oldest_seq") or 0)
                self.lost.append({"t": self.env.now(), "since": self.since, "oldest_seq": oldest})
                with (self.out / "events.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"_lost": {"since": self.since, "oldest_seq": oldest}, "t_obs": self.env.now()}) + "\n")
                self.since = max(oldest - 1, 0)
                continue
            if st != 200 or not isinstance(d, dict):
                return n
            items = [e for e in d.get("items") or [] if isinstance(e, dict)]
            keep = [e for e in items if _kind(e).startswith(EVENT_PREFIXES)]
            if keep:
                with (self.out / "events.jsonl").open("a", encoding="utf-8") as f:
                    for e in keep:
                        f.write(json.dumps(e | {"t_obs": self.env.now()}, ensure_ascii=False) + "\n")
                n += len(keep)
            self.since = max(self.since, int(d.get("next_since") or self.since))
            if len(items) < 1000:
                return n
        return n


# ---------------------------------------------------------------- 采样循环


def _load_state(out: Path) -> dict:
    try:
        d = json.loads((out / "state.json").read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(out: Path, st: dict) -> None:
    tmp = out / f".state.json.tmp-{os.getpid()}"
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, out / "state.json")


def sample_once(env: Env, *, with_status: bool, with_pool: bool) -> dict[str, Any]:
    sd = probe_systemd(env)
    s: dict[str, Any] = {"schema": SCHEMA_SAMPLE, "t": round(env.now(), 3), "mono": round(env.mono(), 3), "systemd": sd,
                         "release": probe_release(env), "cgroup": probe_cgroup(env, sd.get("ControlGroup")),
                         "sandbox_api": probe_sandbox_health(env)}
    if with_status:
        t0 = env.mono()
        s["status"] = probe_status(env)
        s["status_mono"] = round((t0 + env.mono()) / 2, 3)   # 子进程读取 StateRing 的时刻取调用区间中点
    if with_pool:
        s["pool"] = probe_pool(env)
    return s


def run(env: Env, out: Path, *, hours: float, interval: float, status_every: float, pool_every: float, events_every: float,
        kernel_every: float = 600.0, ring_every: float = 0.5, threaded: bool = True, max_samples: int | None = None,
        sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] = print) -> int:
    out.mkdir(parents=True, exist_ok=True)
    st = _load_state(out)
    st.setdefault("started_t", env.now())
    st["hours"] = hours
    st.setdefault("interval_s", interval)
    st.setdefault("env", {"prefix": str(env.prefix), "svc": env.svc, "show_base": env.show_base, "sb_base": env.sb_base})
    ring = env.ring
    if ring is None and RingReader.available():
        ring = RingReader(runs_dir(env))
    st["rtf_source"] = "ring" if ring is not None else "status"
    _save_state(out, st)
    tail = EventTail(env, out, since=int(st.get("events_since") or 0))
    deadline = float(st["started_t"]) + hours * 3600.0
    last_status = last_pool = last_events = last_kernel = -math.inf
    acc = RingAcc()
    stop = threading.Event()
    reader: threading.Thread | None = None
    if ring is not None and threaded:
        def loop() -> None:
            while not stop.is_set():
                acc.add(ring.read(), env.mono())
                stop.wait(ring_every)

        reader = threading.Thread(target=loop, name="observe-ring", daemon=True)
        reader.start()
    n = 0
    next_t = env.mono()
    if ring is None:
        log("observe: 未能导入 awr（请用发布的 .venv/bin/python 运行）：RTF 只有 awr status 的稀疏采样，结论将为未完成")
    log(f"observe: 写入 {out}，至 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(deadline))}（{hours:g} h，间隔 {interval:g} s，"
        f"RTF 来源 {st['rtf_source']}）")
    try:
        while env.now() < deadline and (max_samples is None or n < max_samples):
            if ring is not None and not threaded:
                acc.add(ring.read(), env.mono())
            t0 = env.mono()
            if t0 + 1e-9 < next_t:
                sleep(min(ring_every if (ring is not None and not threaded) else next_t - t0, next_t - t0))
                continue
            next_t = max(next_t + interval, t0)
            do_status, do_pool = t0 - last_status >= status_every, t0 - last_pool >= pool_every
            s = sample_once(env, with_status=do_status, with_pool=do_pool)
            if ring is not None:
                s["ring"] = acc.flush()
            last_status = t0 if do_status else last_status
            last_pool = t0 if do_pool else last_pool
            if t0 - last_events >= events_every:
                s["events_new"] = tail.poll()
                last_events = t0
                st["events_since"] = tail.since
                _save_state(out, st)
            if t0 - last_kernel >= kernel_every:
                t_now = env.now()
                s["kernel_oom"] = probe_kernel_oom(env, float(st.get("kernel_since") or st["started_t"]))
                st["kernel_since"] = t_now
                last_kernel = t0
                _save_state(out, st)
            with (out / "samples.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(s, ensure_ascii=False, separators=(",", ":")) + "\n")
            n += 1
    finally:
        stop.set()
        if reader is not None:
            reader.join(timeout=5)
        if isinstance(ring, RingReader):
            ring.close()
        ru_self, ru_child = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
        st["observer_cpu_s"] = round(float(st.get("observer_cpu_s") or 0.0) + ru_self.ru_utime + ru_self.ru_stime
                                     + ru_child.ru_utime + ru_child.ru_stime, 3)
        _save_state(out, st)
    rep = write_report(out, required_hours=hours)
    print_report(rep, log)
    return rep["exit_code"]


def mark(out: Path, minutes: float, note: str, *, start: float | None = None, now: Callable[[], float] = time.time) -> dict:
    t0 = now() if start is None else start
    rec = {"start": round(t0, 3), "end": round(t0 + minutes * 60.0, 3), "note": note}
    out.mkdir(parents=True, exist_ok=True)
    with (out / "maintenance.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


# ---------------------------------------------------------------- 报告


def _jsonl(p: Path) -> list[dict]:
    out: list[dict] = []
    try:
        with open(p, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try:
                        d = json.loads(ln)
                    except ValueError:
                        continue
                    if isinstance(d, dict):
                        out.append(d)
    except OSError:
        pass
    return out


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    return round(ys[min(len(ys) - 1, max(0, math.ceil(q * len(ys)) - 1))], 4)


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 4) if xs else None


def _in_windows(t: float, wins: Iterable[dict], pad: float = 0.0) -> bool:
    return any(float(w["start"]) - pad <= t <= float(w["end"]) + pad for w in wins)


def _data(e: dict) -> dict:
    d = e.get("data")
    return d if isinstance(d, dict) else e


def _kind(e: dict) -> str:
    return str(e.get("type") or e.get("kind") or "")


def build_report(out: Path, *, required_hours: float, warmup_s: float = 120.0) -> dict[str, Any]:
    samples = sorted(_jsonl(out / "samples.jsonl"), key=lambda s: float(s.get("t") or 0))
    events = _jsonl(out / "events.jsonl")
    wins = _jsonl(out / "maintenance.jsonl")
    st = _load_state(out)
    interval = float(st.get("interval_s") or 10.0)
    rep: dict[str, Any] = {"schema": SCHEMA_REPORT, "ac": "D2-AC-27", "out": str(out), "samples": len(samples),
                           "maintenance": wins, "required_hours": required_hours, "interval_s": interval}
    if len(samples) < 2:
        rep.update({"verdict": "incomplete", "exit_code": EXIT_INCOMPLETE, "reasons": ["采样不足 2 次"]})
        return rep
    t_first, t_last = float(samples[0]["t"]), float(samples[-1]["t"])
    covered = sum(min(b["t"] - a["t"], 3 * interval) for a, b in pairwise(samples)
                  if b["t"] - a["t"] <= 3 * interval)
    hours = (t_last - t_first + interval) / 3600.0   # 每个样本代表其后一个采样间隔
    rep["duration_h"] = round(hours, 3)
    rep["coverage"] = round(covered / max(t_last - t_first, 1e-9), 4)

    # ① OOM（单元根的层级计数；单元重启后计数器随新 cgroup 归零，按 InvocationID 分段求增量）
    oom_kill = oom = 0
    prev: dict[str, Any] | None = None
    for s in samples:
        g = ((s.get("cgroup") or {}).get("groups") or {}).get(".") or {}
        inv = (s.get("systemd") or {}).get("InvocationID")
        cur = {"inv": inv, "oom_kill": g.get("oom_kill"), "oom": g.get("oom")}
        if prev is not None and cur["oom_kill"] is not None and prev["oom_kill"] is not None:
            if cur["inv"] == prev["inv"]:
                oom_kill += max(0, int(cur["oom_kill"]) - int(prev["oom_kill"]))
                oom += max(0, int(cur["oom"] or 0) - int(prev["oom"] or 0))
            else:
                oom_kill += int(cur["oom_kill"])  # 新 cgroup 自创建起的计数
                oom += int(cur["oom"] or 0)
        if cur["oom_kill"] is not None:
            prev = cur
    kernel = [ln for s in samples for ln in s.get("kernel_oom") or []]
    rep["oom"] = {"oom_kill": oom_kill, "oom": oom, "kernel_lines": kernel[:20], "kernel_count": len(kernel),
                  "ok": oom_kill == 0 and not kernel}

    # ② 非预期重启
    unit_restarts: list[dict] = []
    child_restarts: list[dict] = []
    restart_ts: list[float] = []
    last_inv = last_rel = None
    last_procs: dict[str, int] = {}
    last_proc_inv = None
    for s in samples:
        sd = s.get("systemd") or {}
        inv, rel, t = sd.get("InvocationID"), s.get("release"), float(s["t"])
        if inv and last_inv and inv != last_inv:
            why = "release" if rel != last_rel else ("maintenance" if _in_windows(t, wins, pad=interval * 2) else None)
            unit_restarts.append({"t": t, "from": last_inv, "to": inv, "release": rel, "expected": why})
            restart_ts.append(t)
        if inv:
            last_inv = inv
        last_rel = rel if rel else last_rel
        stt = s.get("status")
        if stt and inv:
            procs = {str(p["name"]): int(p.get("restarts") or 0) for p in stt.get("procs") or [] if p.get("name") is not None}
            if inv == last_proc_inv:
                for name, r in procs.items():
                    d = r - last_procs.get(name, r)
                    if d > 0:
                        child_restarts.append({"t": t, "proc": name, "count": d,
                                               "expected": "maintenance" if _in_windows(t, wins, pad=interval * 2) else None})
                        restart_ts.append(t)
            last_procs, last_proc_inv = procs, inv
    # 沙盒会话的崩溃、挂死与看门狗重启（池状态行的 restarts，D2-INT 起提供；驻留恢复只增加 epoch、不计入；d2-FX-R2-deploy-to-M17 第 1 条）
    session_restarts: list[dict[str, Any]] = []
    last_sess: dict[str, int] = {}
    for s in samples:
        for x in (s.get("pool") or {}).get("sessions") or []:
            sid, r = x.get("sid"), x.get("restarts")
            if sid is None or r is None:
                continue
            d = int(r) - last_sess.get(str(sid), int(r))
            if d > 0:
                session_restarts.append({"t": float(s["t"]), "sid": str(sid), "count": d,
                                         "expected": "maintenance" if _in_windows(float(s["t"]), wins, pad=interval * 2) else None})
            last_sess[str(sid)] = int(r)
    unexpected = (sum(1 for r in unit_restarts if not r["expected"]) + sum(r["count"] for r in child_restarts if not r["expected"])
                  + sum(r["count"] for r in session_restarts if not r["expected"]))
    rep["restarts"] = {"unit": unit_restarts, "children": child_restarts, "sessions": session_restarts, "unexpected": unexpected,
                       "ok": unexpected == 0}

    # ③ 共享展示 RTF
    rep["show_rtf"] = _show_rtf(samples, wins, restart_ts, interval, warmup_s)
    rtf_ok_windows = rep["show_rtf"]["windows"] > 0

    # ⑤ S7
    discovered = [e for e in events if _kind(e) == "target.discovered" and _data(e).get("uav")]
    gaps: dict[str, dict[str, float]] = {}
    for e in events:
        if _kind(e) == "relay.gap":
            d = _data(e)
            c = str(d.get("cause") or "unknown")
            g = gaps.setdefault(c, {"count": 0, "total_s": 0.0, "max_s": 0.0})
            dur = float(d.get("duration_s") or 0.0)
            g["count"] += 1
            g["total_s"] = round(g["total_s"] + dur, 2)
            g["max_s"] = max(g["max_s"], dur)
    lost = [e["_lost"] for e in events if "_lost" in e]
    kinds: dict[str, int] = {}
    for e in events:
        if "_lost" not in e:
            kinds[_kind(e)] = kinds.get(_kind(e), 0) + 1
    s7_complete = hours >= S7_HOURS and not lost
    rep["s7"] = {"hours": round(hours, 3), "complete_7d": s7_complete, "discovered": len(discovered),
                 "discovered_first": discovered[:10], "gaps_by_cause": gaps, "events_lost": lost, "event_counts": kinds,
                 "ok": len(discovered) == 0}

    # 成本（§4.4.1 的输入）
    rep["costs"] = _costs(samples)
    rep["observer"] = {"cpu_s": st.get("observer_cpu_s"),
                       "cores": round(float(st.get("observer_cpu_s") or 0.0) / max(t_last - t_first, 1.0), 5)}

    reasons: list[str] = []
    if hours + 1e-9 < required_hours:
        reasons.append(f"观测 {hours:.2f} h < {required_hours:g} h")
    if rep["coverage"] < COVERAGE_MIN:
        reasons.append(f"采样覆盖率 {rep['coverage']:.3f} < {COVERAGE_MIN}")
    if not rtf_ok_windows:
        reasons.append("没有可用的共享展示 RTF 窗口（StateRing 与 awr status 都不可读？）")
    elif rep["show_rtf"]["source"] != "ring":
        reasons.append("RTF 只有 awr status 的稀疏采样：用发布的 .venv/bin/python 运行 observe.py 才能逐秒读取 StateRing")
    if lost:
        reasons.append(f"事件游标过期 {len(lost)} 次（S7 统计不完整）")
    failed = [k for k in ("oom", "restarts", "show_rtf", "s7") if not rep[k]["ok"] and not (k == "show_rtf" and not rtf_ok_windows)]
    if failed:
        verdict, code = "fail", EXIT_FAIL
    elif reasons:
        verdict, code = "incomplete", EXIT_INCOMPLETE
    else:
        verdict, code = "pass", EXIT_OK
    rep.update({"verdict": verdict, "exit_code": code, "failed": failed, "reasons": reasons})
    return rep


def _show_rtf(samples: list[dict], wins: list[dict], restart_ts: list[float], interval: float, warmup_s: float) -> dict[str, Any]:
    """共享展示 RTF：主口径为进程内 StateRing 读数（逐秒自报 rtf 的最小值，加上相邻样本间的 Δt_sim / Δt_wall）；没有 StateRing 读数
    时退回 awr status 的稀疏采样（≥ 300 s 窗口的 Δt_sim / Δt_wall 与自报 rtf，结论为未完成）。"""
    ring_s = [s for s in samples if (s.get("ring") or {}).get("reads")]
    if ring_s:
        rows = [(s, s["ring"]) for s in ring_s]
        epoch_ts = [float(b["t"]) for (_, ra), (b, rb) in pairwise(rows) if rb["epochs"] != [ra.get("epoch")]]
        quiet = (*restart_ts, *epoch_ts)

        def skip(s: dict, r: dict) -> bool:
            t = float(s["t"])
            return (_in_windows(t, wins) or _in_windows(t - interval, wins) or any(0 <= t - x <= warmup_s for x in quiet)
                    or any(x != 1.0 for x in r["rates"]))

        sec: list[float] = []
        win: list[float] = []
        low: list[dict] = []
        stale = 0
        for s, r in rows:
            if skip(s, r) or r["rtf_min"] is None:
                continue
            sec.append(float(r["rtf_min"]))
            stale += int(r["age_max_ms"] > STALE_MS)
            if r["rtf_min"] < RTF_MIN:
                low.append({"t": float(s["t"]), "kind": "1s", "rtf": r["rtf_min"], "age_max_ms": r["age_max_ms"]})
        for (sa, ra), (sb, rb) in pairwise(rows):
            if (skip(sb, rb) or (sa.get("systemd") or {}).get("InvocationID") != (sb.get("systemd") or {}).get("InvocationID")
                    or rb["epochs"] != [ra.get("epoch")] or set(rb["states"]) & CLOCK_EXCLUDED or ra.get("state") in CLOCK_EXCLUDED
                    or ra.get("t_sim_s") is None or rb.get("t_sim_s") is None or float(rb["mono"]) - float(ra["mono"]) <= 0):
                continue
            dt = float(rb["mono"]) - float(ra["mono"])
            x = (float(rb["t_sim_s"]) - float(ra["t_sim_s"])) / dt
            win.append(x)
            if x < RTF_MIN:
                low.append({"t": float(sb["t"]), "kind": "window", "rtf": round(x, 4), "window_s": round(dt, 2)})
        lo = min([*sec, *win]) if (sec or win) else None
        return {"source": "ring", "windows": len(win), "seconds_samples": len(sec), "min_1s": _r4(min(sec)) if sec else None,
                "p01_1s": _pct(sec, 0.01), "min_window": _r4(min(win)) if win else None, "mean_window": _mean(win),
                "stale_intervals": stale, "below": sorted(low, key=lambda x: x["t"])[:50], "below_count": len(low),
                "min": _r4(lo) if lo is not None else None, "ok": lo is not None and lo >= RTF_MIN}
    # 退回：awr status（约每 60 s 一次，子进程读环的时刻有约 0.1 s 的不确定，因此只用 ≥ 300 s 的窗口）
    st_s = [s for s in samples if (s.get("status") or {}).get("ring")]
    win, self_rtf, low = [], [], []
    i = 0
    while i < len(st_s) - 1:
        a = st_s[i]
        j = next((k for k in range(i + 1, len(st_s)) if float(st_s[k]["status_mono"]) - float(a["status_mono"]) >= RTF_FALLBACK_WINDOW_S),
                 None)
        if j is None:
            break
        b = st_s[j]
        seg = st_s[i:j + 1]
        ra, rb = a["status"]["ring"], b["status"]["ring"]
        if (len({x["status"]["ring"].get("epoch") for x in seg}) == 1 and not any(x["status"]["ring"].get("state") in CLOCK_EXCLUDED for x in seg)
                and all(float(x["status"]["ring"].get("rate") or 0) == 1.0 for x in seg)
                and len({(x.get("systemd") or {}).get("InvocationID") for x in seg}) == 1
                and not any(_in_windows(float(x["t"]), wins) or any(0 <= float(x["t"]) - r <= warmup_s for r in restart_ts) for x in seg)):
            dt = float(b["status_mono"]) - float(a["status_mono"])
            x = (float(rb["t_sim_s"]) - float(ra["t_sim_s"])) / dt
            win.append(x)
            if x < RTF_MIN:
                low.append({"t": float(b["t"]), "kind": "window", "rtf": round(x, 4), "window_s": round(dt, 1)})
            self_rtf += [float(x["status"]["ring"]["rtf"]) for x in seg[1:] if isinstance(x["status"]["ring"].get("rtf"), (int, float))]
        i = j
    lo = min(win) if win else None
    return {"source": "status", "windows": len(win), "min_window": _r4(lo) if lo is not None else None, "mean_window": _mean(win),
            "self_min": _r4(min(self_rtf)) if self_rtf else None, "below": low[:50], "below_count": len(low),
            "min": _r4(lo) if lo is not None else None, "ok": lo is not None and lo >= RTF_MIN}


def _r4(x: float) -> float:
    return round(float(x), 4)


def _rate_series(samples: list[dict], get: Callable[[dict], Any]) -> list[float]:
    """计数器（usage_usec）→ 相邻样本间的核数；跨单元重启或计数回退的区间丢弃。"""
    out: list[float] = []
    for a, b in pairwise(samples):
        ua, ub = get(a), get(b)
        if ua is None or ub is None or (a.get("systemd") or {}).get("InvocationID") != (b.get("systemd") or {}).get("InvocationID"):
            continue
        dt = float(b["mono"]) - float(a["mono"])
        if dt > 0 and ub >= ua:
            out.append((ub - ua) / 1e6 / dt)
    return out


def _costs(samples: list[dict]) -> dict[str, Any]:
    def grp(name: str, key: str) -> Callable[[dict], Any]:
        return lambda s: (((s.get("cgroup") or {}).get("groups") or {}).get(name) or {}).get(key)

    res: dict[str, Any] = {}
    for name in ("core", "sandbox", "sandbox/plan", "sandbox/ctl", "."):
        xs = _rate_series(samples, grp(name, "usage_usec"))
        res["unit" if name == "." else name] = {"cores_mean": _mean(xs), "cores_p95": _pct(xs, 0.95)}
    peaks = [v for s in samples if (v := grp(".", "peak")(s)) is not None]
    anon = [v for s in samples if (v := grp(".", "anon_shmem")(s)) is not None]
    res["unit"].update({"memory_peak_mb": round(max(peaks) / 2**20, 1) if peaks else None,
                        "anon_shmem_max_mb": round(max(anon) / 2**20, 1) if anon else None})
    thr = [v for s in samples if (v := grp("core", "nr_throttled")(s)) is not None]
    res["core"]["nr_throttled_delta"] = (thr[-1] - thr[0]) if len(thr) >= 2 and thr[-1] >= thr[0] else None
    # 每会话：叶子组核数按世界归类（世界取最近一次 sbctl 状态中的会话表）
    world_of: dict[str, str] = {}
    levels: dict[str, float] = {}
    n_max = set()
    prev_t = None
    prev_level = None
    for s in samples:
        p = s.get("pool")
        if p:
            for x in p.get("sessions") or []:
                if x.get("sid") and x.get("world"):
                    world_of[str(x["sid"])] = str(x["world"])
            if p.get("n_max") is not None:
                n_max.add(p["n_max"])
            if prev_t is not None and prev_level is not None:
                levels[prev_level] = round(levels.get(prev_level, 0.0) + float(s["t"]) - prev_t, 1)
            prev_t, prev_level = float(s["t"]), str(p.get("degrade_level"))
    per_world: dict[str, list[float]] = {}
    sids = {sid for s in samples for sid in (((s.get("cgroup") or {}).get("leaves")) or {})}
    for sid in sorted(sids):
        xs = _rate_series(samples, lambda s, sid=sid: ((((s.get("cgroup") or {}).get("leaves")) or {}).get(sid) or {}).get("usage_usec"))
        if xs:
            per_world.setdefault(world_of.get(sid, "unknown"), []).extend(xs)
    res["sessions_by_world"] = {w: {"samples": len(xs), "cores_mean": _mean(xs), "cores_p95": _pct(xs, 0.95)}
                                for w, xs in sorted(per_world.items())}
    res["degrade_level_s"] = levels
    res["n_max_seen"] = sorted(n_max, key=str)
    epochs: dict[str, list[int]] = {}
    for s in samples:
        for x in (s.get("pool") or {}).get("sessions") or []:
            if x.get("sid") is not None and x.get("epoch") is not None:
                epochs.setdefault(str(x["sid"]), []).append(int(x["epoch"]))
    res["sandbox_epoch_bumps"] = sum(max(0, b - a) for es in epochs.values() for a, b in pairwise(es))
    return res


def write_report(out: Path, *, required_hours: float | None = None) -> dict[str, Any]:
    st = _load_state(out)
    rep = build_report(out, required_hours=float(required_hours if required_hours is not None else st.get("hours") or 72.0))
    tmp = out / f".report.json.tmp-{os.getpid()}"
    tmp.write_text(json.dumps(rep, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, out / "report.json")
    return rep


def print_report(rep: dict[str, Any], log: Callable[[str], None] = print) -> None:
    word = {"pass": "通过", "fail": "不通过", "incomplete": "未完成"}[rep["verdict"]]
    log(f"D2-AC-27 线上观测：{word}（{rep.get('duration_h', 0):.2f} h，覆盖率 {rep.get('coverage', 0):.3f}，样本 {rep['samples']}）")
    if "oom" in rep:
        log(f"  OOM：oom_kill {rep['oom']['oom_kill']}（oom {rep['oom']['oom']}）")
        r = rep["restarts"]
        log(f"  非预期重启：{r['unexpected']}（单元重启 {len(r['unit'])}，子进程退避重启 {sum(x['count'] for x in r['children'])}）")
        f = rep["show_rtf"]
        if f.get("source") == "ring":
            log(f"  共享展示 RTF（StateRing）：逐秒最小 {f['min_1s']}（1 % 分位 {f['p01_1s']}），{f['windows']} 个 {rep.get('interval_s', '')} 窗口最小 "
                f"{f['min_window']}，心跳停顿区间 {f['stale_intervals']}，低于 0.99 {f['below_count']} 处")
        else:
            log(f"  共享展示 RTF（awr status，稀疏）：{f['windows']} 个 ≥ 300 s 窗口最小 {f['min_window']}，自报最小 {f.get('self_min')}，"
                f"低于 0.99 {f['below_count']} 处")
        s7 = rep["s7"]
        gaps = "、".join(f"{c} {g['count']} 次 {g['total_s']:.0f} s（最长 {g['max_s']:.0f} s）" for c, g in s7["gaps_by_cause"].items()) or "无"
        log(f"  S7：被发现 {s7['discovered']}；间隙 {gaps}；7 天{'已' if s7['complete_7d'] else '未'}覆盖")
        c = rep["costs"]
        log(f"  成本：core/ {c['core']['cores_mean']} 核（p95 {c['core']['cores_p95']}），sandbox/ {c['sandbox']['cores_mean']} 核，"
            f"单元峰值 {c['unit']['memory_peak_mb']} MB；按世界 {json.dumps(c['sessions_by_world'], ensure_ascii=False)}")
    for x in rep.get("reasons") or []:
        log(f"  原因：{x}")


# ---------------------------------------------------------------- 入口


def _env_from_args(a: argparse.Namespace) -> Env:
    return Env(prefix=Path(a.prefix), env_file=Path(a.env_file), svc=a.svc, user=a.user, show_base=a.show_base, sb_base=a.sb_base,
               host=a.host or None, cgroup_fs=Path(a.cgroup_fs),
               status_cmd=shlex.split(a.status_cmd) if a.status_cmd else None,
               pool_cmd=shlex.split(a.pool_cmd) if a.pool_cmd else None)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="observe.py", description="公开站线上观测（D2-AC-27）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="采样到 OUT（可续跑）")
    p.add_argument("--out", required=True)
    p.add_argument("--hours", type=float, default=72.0)
    p.add_argument("--interval", type=float, default=10.0, help="systemd、cgroup 与 sandbox-api 采样间隔（s）")
    p.add_argument("--status-every", type=float, default=60.0, help="awr status（共享展示 StateRing 与进程表）间隔（s）")
    p.add_argument("--pool-every", type=float, default=120.0, help="sbctl.sh --json status 间隔（s；非 root 时跳过）")
    p.add_argument("--events-every", type=float, default=30.0, help="/api/events 拉取间隔（s）")
    p.add_argument("--kernel-every", type=float, default=600.0, help="内核日志 OOM 扫描间隔（s）")
    p.add_argument("--ring-every", type=float, default=0.5, help="StateRing 头部读取间隔（s；sim-core 每 1 s 更新一次 rtf）")
    p.add_argument("--max-samples", type=int, default=None, help=argparse.SUPPRESS)
    for q in (p,):
        q.add_argument("--prefix", default="/opt/anet-drone4d")
        q.add_argument("--env-file", default="/etc/anet-drone4d/env")
        q.add_argument("--svc", default="anet-drone4d")
        q.add_argument("--user", default="awr")
        q.add_argument("--show-base", default="http://127.0.0.1:18640")
        q.add_argument("--sb-base", default="http://127.0.0.1:18641")
        q.add_argument("--host", default="drone4d.agentnetwork.org.cn", help="Host 头（公开模式只认站点域名；空串不发送）")
        q.add_argument("--cgroup-fs", default=os.environ.get("AWR_CGROUP_FS", "/sys/fs/cgroup"))
        q.add_argument("--status-cmd", default=None, help="替代 awr status --json 的命令（调试用）")
        q.add_argument("--pool-cmd", default=None, help="替代 sbctl.sh --json status 的命令（调试用）")
    p = sub.add_parser("mark", help="登记维护窗口")
    p.add_argument("out")
    p.add_argument("--minutes", type=float, required=True)
    p.add_argument("--note", default="")
    p = sub.add_parser("report", help="由 OUT 计算 report.json")
    p.add_argument("out")
    p.add_argument("--hours", type=float, default=None, help="判据要求的时长（缺省取 run 时的 --hours）")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "mark":
        if a.minutes <= 0:
            ap.error("--minutes 必须为正")
        rec = mark(Path(a.out), a.minutes, a.note)
        print(json.dumps(rec, ensure_ascii=False))
        return EXIT_OK
    if a.cmd == "report":
        out = Path(a.out)
        if not (out / "samples.jsonl").is_file():
            print(f"observe: {out} 没有 samples.jsonl", file=sys.stderr)
            return EXIT_ARGS
        rep = write_report(out, required_hours=a.hours)
        if a.json:
            print(json.dumps(rep, ensure_ascii=False, indent=1))
        else:
            print_report(rep)
        return int(rep["exit_code"])
    if a.hours <= 0 or a.interval <= 0:
        ap.error("--hours 与 --interval 必须为正")
    env = _env_from_args(a)
    pool_every = a.pool_every if (env.is_root or env.pool_cmd) else math.inf
    try:
        return run(env, Path(a.out), hours=a.hours, interval=a.interval, status_every=a.status_every, pool_every=pool_every,
                   events_every=a.events_every, kernel_every=a.kernel_every, ring_every=a.ring_every, max_samples=a.max_samples)
    except KeyboardInterrupt:
        rep = write_report(Path(a.out), required_hours=a.hours)
        print_report(rep)
        return int(rep["exit_code"])


if __name__ == "__main__":
    sys.dont_write_bytecode = True   # 以 root 运行时不在发布目录（root 所有、awr 只读）里写 __pycache__
    sys.exit(main())
