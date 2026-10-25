"""容器 Agent 的启动与回收（AWR-17 §18.9.1；M26 §7.2；ADR-161）：每架机一个隔离容器，经只挂载进该容器的套接字目录与 Env 通信。

容器参数：`--network none`、只读根文件系统（`/tmp` 为 tmpfs）、`--cap-drop ALL`、`no-new-privileges`、非 root 用户（65534）、
CPU、内存与进程数上限；入口为镜像缺省 CMD 或提交的 `entry.cmd`，`entry.env` 以 `-e` 传入（键须为大写标识符）。
运行器本身在容器中（dmax 的 d3-gz）时，`docker -v` 的源路径须为宿主路径：`AWR_HOST_PATH_MAP="/repo=/data/…/d3dev"` 按前缀换算。
Unix 套接字路径上限约 108 字节：运行目录较深时以 `AWR_AGENT_SOCK_ROOT` 指定短根目录（每机子目录 `<run 摘要>-<uav>`）。
回收时把各容器日志尾部（64 KiB）写入 `agents/<uav>/container.log`。

D3 容器清单规则（AWR-05 §4.6、§4.7、ADR-151、ADR-178；M24-FR-071；D3-AC-22）：`parse_docker_run` 把 `docker run` 参数表
（或 `docker inspect` 记录，`launch_from_inspect`）归一为 `Launch`，`check_launch` 逐项断言 CM-01 至 CM-08，`check_launches`
另做跨实例的分区与命名唯一性检查。规则表 `RULES`、组件表 `COMPONENTS` 与 `tools/deploy/dmax/audit_manifests.py` 共用；
`AgentContainers.start()` 在启动前对自身参数表执行同一检查，不满足时以 664 拒绝启动。

容器加固与运行期审计（§5.6.7 容器加固、ADR-164；M26 §8.1；D3-AC-59 负向用例）：
- seccomp 配置 `d3-agent-v1`（`seccomp_profile`，经 `--security-opt seccomp=<文件>` 显式指定，不依赖 dockerd 内置缺省档）：以
  moby 缺省档中不依赖能力的放行集合为基础，缺省动作 EPERM；`clone` 只放行不带命名空间标志的调用，`clone3` 返回 ENOSYS（glibc
  回退到 `clone`）；逃逸类系统调用（`SECCOMP_KILL`：挂载、命名空间、模块、kexec、ptrace 与跨进程内存、句柄打开、重启与交换区、
  端口 I/O）为 KILL_PROCESS：进程以 SIGSYS 终止（容器主进程即退出码 159），内核同时记录 seccomp 审计事件；子进程被终止时
  启动器侧只能经主进程的后续行为间接观察，完整记录在宿主内核日志（读取需 CAP_SYSLOG，不在本模块范围）。
- 运行期审计（`harden_audit`，缺省取 `AWR_AGENT_HARDEN_AUDIT=1`；不提权、不改宿主配置）：`HardeningMonitor` 每机一个，证据来源
  ——越权系统调用：容器退出码 159（SIGSYS）；联网：Agent 之前先启动监视容器（`<容器名>-mon`，`--network none`、非 root、
  `--cap-drop ALL`、只读根），Agent 以 `--network container:<监视容器>` 加入其网络命名空间（同样只有 `lo`、无路由），监视容器在
  Agent 整个生命周期内（含 Agent 退出之后）读取 `/proc/net/snmp`、`/proc/net/snmp6` 的 `OutNoRoutes` 计数（对外 TCP、UDP 一律
  无路由失败并计数；容器内无有效 DNS 服务器，域名解析不发包即失败）；fork 炸弹：Engine API 统计的进程数峰值达到 `--pids-limit`；写满 tmpfs：Engine API 统计的
  cgroup `shmem` 峰值 ≥ 0.95 × tmpfs 容量；读取 `/data/d3/hidden`：该路径只挂载一个只读诱饵目录（真实隐藏存储从不挂载），宿主侧
  以 inotify 记录对诱饵的打开与读取。每个触发的向量写一条审计记录 `{t_unix_ns, action: deny, code: 691, why: hardening, vector,
  run_id, uav, container, evidence}` 到 `agents/<uav>/hardening.jsonl`，并交给 `audit_sink`（与 bench `AuditLog.write` 同签名）。
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import http.client
import itertools
import json
import os
import re
import select
import socket
import struct
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from awr.agentsdk.env.remote import SocketAgentHost, run_remote_episode
from awr.agentsdk.wire import WireError
from awr.phys.gz.instance import host_path

__all__ = ["ALLOWED_GPUS", "COMPONENTS", "D3_SLICE", "HARDEN_CODE", "HARDEN_VECTORS", "HIDDEN_PATH", "PIDS_LIMIT", "RULES", "SECCOMP_KILL",
           "SECCOMP_NAME", "SIGSYS_EXIT", "TMPFS_MB", "AgentContainers", "Component", "Finding", "HardeningMonitor", "Launch", "check_launch",
           "check_launches", "component_of", "engine_stats", "hardening_findings", "host_path", "launch_from_inspect", "make_decoy",
           "net_noroutes", "parse_docker_run", "run_container_episode", "seccomp_path", "seccomp_profile", "write_seccomp"]

_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
LOG_TAIL_B = 64 * 1024


# ==================================================================== D3 容器清单规则（D3-AC-22）

ALLOWED_GPUS = frozenset({1, 2, 3})        # dmax 上 D3 可绑定的卡：GPU 2、3（§4.7），GPU 1 空闲时可用（R-D3-63）；其余属于其他服务
D3_SLICE = "d3.slice"                      # CPU 池 cgroup（§4.6：≤ 40 核、≤ 384 GB）

RULES: dict[str, str] = {
    "CM-01": "每个 D3 容器设 --memory（§4.6 隔离与命名、ADR-151）",
    "CM-02": "每个 D3 容器设 --cpus（ADR-178 每实例预算；M24-FR-071）",
    "CM-03": "GPU 只经 --gpus device=<N> 绑定一张卡且 N 属于 D3 可用卡；不得 all、计数、多卡、直通 /dev/nvidia* 或 nvidia 运行时",
    "CM-04": "CPU 池容器（含 geosvc、gz physics、Agent、Env 与写入器、FAST-LIVO2、bench）不挂载 GPU",
    "CM-05": "GPU Worker 容器恰好绑定一张卡",
    "CM-06": "容器命名 d3-<组件>[-<实例>]，组件属于组件表；d3-gz-* 只用于 gz 实例；不同 Run 的实例名不同",
    "CM-07": "gz 实例设独立 GZ_PARTITION（每 Run 唯一）与 GZ_IP=127.0.0.1；非 gz 容器不在容器层设 GZ_PARTITION",
    "CM-08": "CPU 池容器进入 d3.slice（--cgroup-parent d3.slice；告警级，待 dmax 配置 d3.slice 配额后升为错误）",
}


@dataclass(frozen=True)
class Component:
    pool: str                              # cpu：CPU 池，不挂载 GPU；gpu：GPU Worker，恰好一张卡；any：随运行档位，至多一张卡
    note: str = ""


COMPONENTS: dict[str, Component] = {
    "sensorsvc": Component("gpu", "M25 Sensor Service Worker（每 GPU 一个）"),
    "render": Component("gpu", "render_offline"),
    "train": Component("gpu", "3DGS 训练与高斯发布 GPU 步骤（P3）"),
    "gz": Component("cpu", "gz-sim physics 导出档；xval 档以标签 d3.profile=xval 声明并绑定一张卡（P1）"),
    "px4gz": Component("cpu", "PX4 SITL 导出档（P2，无相机）"),
    "geosvc": Component("cpu", "共享几何服务（每活跃世界一个，1 核、0.7 GB）"),
    "agent": Component("cpu", "Agent 隔离容器（std 每机 2 核、8 GB）"),
    "env": Component("cpu", "Env 与写入器（每 Run 2 核、4 GB）"),
    "livo": Component("cpu", "FAST-LIVO2"),
    "eval": Component("cpu", "Evaluator"),
    "worldbuild": Component("cpu", "世界构建、NavMesh 与磁带编译"),
    "bench": Component("cpu", "bench-api 与 bench 控制命令"),
    "subworker": Component("cpu", "内部提交流水线（宿主与 gz 同容器，Agent 为兄弟容器）"),
    "labweb": Component("cpu", "内部工作区 lab-web"),
    "lab-gateway": Component("cpu", "内部工作区网关"),
    "rerun-proxy": Component("cpu", "Rerun 代理"),
    "registry": Component("cpu", "私有镜像仓库 d3-registry"),
    "infer-gw": Component("cpu", "容器内推理网关（P1）"),
    "board": Component("any", "原型榜宿主：L1 在 CPU 池，L2 以 d3-sensorsvc 镜像绑定一张卡"),
    "test": Component("any", "dmax 测试容器（gz 锁步测试在 CPU 池，GPU 测试绑定一张卡）"),
}

_NAME = re.compile(r"^d3-[a-z0-9]+(?:-[A-Za-z0-9_.-]+)?$")
_MEM = re.compile(r"^(\d+(?:\.\d+)?)([bkmg]?)$", re.IGNORECASE)
_GPU_DEV = re.compile(r"^device=(\d+)$")
_NV_DEV = re.compile(r"^/dev/nvidia")
_BOOL_LONG = frozenset({"--detach", "--rm", "--interactive", "--tty", "--init", "--read-only", "--privileged", "--publish-all",
                        "--oom-kill-disable", "--no-healthcheck", "--sig-proxy", "--disable-content-trust", "--quiet", "--help"})
_BOOL_SHORT = frozenset("ditPq")
_SHORT = {"-e": "--env", "-m": "--memory", "-l": "--label", "-v": "--volume", "-u": "--user", "-w": "--workdir", "-c": "--cpu-shares",
          "-p": "--publish", "-h": "--hostname", "-a": "--attach"}


@dataclass
class Launch:
    """一次容器启动的归一化参数（来自 `docker run` 参数表或 `docker inspect`）。`env` 中值为 None 表示从调用方环境透传。"""
    site: str
    name: str | None = None
    image: str | None = None
    command: list[str] = field(default_factory=list)
    memory: str | None = None
    cpus: str | None = None
    gpus: list[str] = field(default_factory=list)                  # 每个 --gpus 值（inspect 时由 DeviceRequests 换算）
    devices: list[str] = field(default_factory=list)
    runtime: str | None = None
    env: dict[str, str | None] = field(default_factory=dict)
    labels: dict[str, str | None] = field(default_factory=dict)
    cgroup_parent: str | None = None
    component: str | None = None                                   # 声明的组件；缺省按标签 d3.component 或容器名推断
    run: str | None = None                                         # 渲染时的 Run 标识（跨实例唯一性检查）
    argv: list[str] | None = None


@dataclass(frozen=True)
class Finding:
    rule: str
    level: str                                                     # error | warn
    site: str
    name: str | None
    message: str


def _kv(v: str) -> tuple[str, str | None]:
    k, eq, val = v.partition("=")
    return k, (val if eq else None)


def parse_docker_run(argv: Sequence[str], site: str = "", *, component: str | None = None, run: str | None = None) -> Launch:
    """`[docker, (container,) run, 选项…, 镜像, 命令…]` → Launch。值选项支持 `--x v`、`--x=v`、`-e v`、`-ev`；未知长选项按带值处理。"""
    a = list(argv)
    i = 1
    if i < len(a) and a[i] == "container":
        i += 1
    if i >= len(a) or a[i] != "run":
        raise ValueError(f"not a docker run command: {' '.join(a[:3])}")
    i += 1
    lc = Launch(site, component=component, run=run, argv=a)
    while i < len(a):
        t = a[i]
        if t == "--" or not t.startswith("-") or t == "-":
            break
        if t.startswith("--"):
            flag, eq, val = t.partition("=")
            if not eq:
                if flag in _BOOL_LONG:
                    i += 1
                    continue
                val = a[i + 1] if i + 1 < len(a) else ""
                i += 1
        elif t in _SHORT:
            flag, val = _SHORT[t], (a[i + 1] if i + 1 < len(a) else "")
            i += 1
        elif all(c in _BOOL_SHORT for c in t[1:]):
            i += 1
            continue
        elif t[:2] in _SHORT:
            flag, val = _SHORT[t[:2]], t[2:]
        else:
            flag, val = t, (a[i + 1] if i + 1 < len(a) else "")
            i += 1
        i += 1
        if flag == "--name":
            lc.name = val
        elif flag == "--memory":
            lc.memory = val
        elif flag == "--cpus":
            lc.cpus = val
        elif flag == "--gpus":
            lc.gpus.append(val)
        elif flag == "--device":
            lc.devices.append(val.split(":", 1)[0])
        elif flag == "--runtime":
            lc.runtime = val
        elif flag == "--env":
            k, v = _kv(val)
            lc.env[k] = v
        elif flag == "--label":
            k, v = _kv(val)
            lc.labels[k] = v
        elif flag == "--cgroup-parent":
            lc.cgroup_parent = val
        elif flag == "--env-file":
            lc.env.setdefault("__env_file__", val)
    if i < len(a) and a[i] == "--":
        i += 1
    if i < len(a):
        lc.image, lc.command = a[i], a[i + 1:]
    return lc


def launch_from_inspect(rec: dict, site: str = "inspect") -> Launch:
    """`docker inspect` 的一条记录 → Launch（Memory 0 视为未设；DeviceRequests 中 gpu 请求换算为 --gpus 写法）。"""
    hc, cfg = rec.get("HostConfig") or {}, rec.get("Config") or {}
    gpus = []
    for r in hc.get("DeviceRequests") or []:
        if not any("gpu" in caps for caps in (r.get("Capabilities") or [])) and r.get("Driver") not in ("nvidia",):
            continue
        ids = r.get("DeviceIDs") or []
        gpus.append(f"device={','.join(ids)}" if ids else ("all" if int(r.get("Count") or 0) < 0 else f"count={r.get('Count')}"))
    nano = int(hc.get("NanoCpus") or 0)
    quota, period = int(hc.get("CpuQuota") or 0), int(hc.get("CpuPeriod") or 0) or 100_000
    cpus = f"{nano / 1e9:g}" if nano > 0 else (f"{quota / period:g}" if quota > 0 else None)
    mem = int(hc.get("Memory") or 0)
    runtime = hc.get("Runtime")
    env = dict(_kv(e) for e in cfg.get("Env") or [])
    if runtime != "nvidia":                                        # runc 下镜像自带的 NVIDIA_* 变量不暴露设备
        env.pop("NVIDIA_VISIBLE_DEVICES", None)
    return Launch(site, name=str(rec.get("Name") or "").lstrip("/") or None, image=cfg.get("Image"),
                  command=[*(cfg.get("Entrypoint") or []), *(cfg.get("Cmd") or [])], memory=str(mem) if mem > 0 else None, cpus=cpus,
                  gpus=gpus, devices=[d.get("PathOnHost", "") for d in hc.get("Devices") or []], runtime=runtime if runtime != "runc" else None,
                  env=env, labels=dict(cfg.get("Labels") or {}), cgroup_parent=hc.get("CgroupParent") or None)


def component_of(lc: Launch) -> str | None:
    """声明的组件 → 标签 d3.component → 容器名 `d3-<组件>` 的最长前缀匹配；都不成立时为 None。"""
    if lc.component:
        return lc.component
    if lc.labels.get("d3.component"):
        return lc.labels["d3.component"]
    n = lc.name or ""
    best = None
    for k in COMPONENTS:
        if (n == f"d3-{k}" or n.startswith(f"d3-{k}-")) and (best is None or len(k) > len(best)):
            best = k
    return best


def _mem_bytes(v: str) -> float:
    m = _MEM.match(v.strip())
    if not m:
        return -1.0
    return float(m.group(1)) * {"": 1, "b": 1, "k": 1 << 10, "m": 1 << 20, "g": 1 << 30}[m.group(2).lower()]


def _pos_float(v: str | None) -> bool:
    try:
        return v is not None and float(v) > 0
    except ValueError:
        return False


def _gpu_binding(lc: Launch) -> tuple[list[int], list[str]]:
    """→ (绑定的卡号, CM-03 违规说明)。"""
    cards, bad = [], []
    if len(lc.gpus) > 1:
        bad.append(f"--gpus given {len(lc.gpus)} times")
    for g in lc.gpus:
        v = g.strip().strip("'\"")
        m = _GPU_DEV.match(v)
        if not m:
            bad.append(f"--gpus {g!r} does not bind exactly one card (use device=<N>)")
            continue
        n = int(m.group(1))
        cards.append(n)
        if n not in ALLOWED_GPUS:
            bad.append(f"GPU {n} is not assigned to D3 (allowed: {sorted(ALLOWED_GPUS)})")
    bad += [f"--device {d} passes an NVIDIA device through (use --gpus device=<N>)" for d in lc.devices if _NV_DEV.match(d)]
    if lc.runtime == "nvidia":
        bad.append("--runtime nvidia exposes devices via NVIDIA_VISIBLE_DEVICES (use --gpus device=<N>)")
    nv = lc.env.get("NVIDIA_VISIBLE_DEVICES", "void")
    if nv not in ("void", "none", "") and "NVIDIA_VISIBLE_DEVICES" in lc.env:
        bad.append(f"NVIDIA_VISIBLE_DEVICES={nv} set on the container (use --gpus device=<N>)")
    return cards, bad


def _is_gz(lc: Launch, comp: str | None) -> bool:
    return comp == "gz" or lc.command[:2] == ["gz", "sim"]


def check_launch(lc: Launch) -> list[Finding]:
    """单个容器启动参数逐项断言 CM-01 至 CM-08。"""
    out: list[Finding] = []

    def f(rule: str, msg: str, level: str = "error") -> None:
        out.append(Finding(rule, level, lc.site, lc.name, msg))

    if lc.memory is None:
        f("CM-01", "--memory is not set")
    elif _mem_bytes(lc.memory) <= 0:
        f("CM-01", f"--memory {lc.memory!r} is not a positive size")
    if not _pos_float(lc.cpus):
        f("CM-02", "--cpus is not set" if lc.cpus is None else f"--cpus {lc.cpus!r} is not positive")
    comp = component_of(lc)
    cards, bad = _gpu_binding(lc)
    for b in bad:
        f("CM-03", b)
    mounted = bool(lc.gpus or bad)
    pool = COMPONENTS[comp].pool if comp in COMPONENTS else None
    if comp == "gz" and lc.labels.get("d3.profile") == "xval":
        pool = "gpu"
    if pool == "cpu" and mounted:
        f("CM-04", f"CPU-pool component {comp!r} must not mount a GPU")
    if pool == "gpu" and len(cards) != 1:
        f("CM-05", f"GPU worker {comp!r} binds {len(cards)} cards (exactly one required)")
    if not lc.name:
        f("CM-06", "--name is not set")
    elif not _NAME.match(lc.name):
        f("CM-06", f"name {lc.name!r} does not follow d3-<component>[-<instance>]")
    elif comp is None or comp not in COMPONENTS:
        f("CM-06", f"name {lc.name!r} does not start with a known component (d3-{{{','.join(sorted(COMPONENTS))}}})")
    elif lc.name.startswith("d3-gz-") and not _is_gz(lc, comp):
        f("CM-06", f"name {lc.name!r} uses the d3-gz-* namespace reserved for gz instances")
    elif not (lc.name == f"d3-{comp}" or lc.name.startswith(f"d3-{comp}-")):
        f("CM-06", f"name {lc.name!r} does not match declared component {comp!r}")
    if _is_gz(lc, comp):
        if not lc.env.get("GZ_PARTITION"):
            f("CM-07", "gz instance has no GZ_PARTITION")
        if lc.env.get("GZ_IP") != "127.0.0.1":
            f("CM-07", f"gz instance GZ_IP={lc.env.get('GZ_IP')!r} (127.0.0.1 required)")
    elif "GZ_PARTITION" in lc.env:
        f("CM-07", "non-gz container sets GZ_PARTITION at container level (gz instances inside would share one partition)")
    if (pool == "cpu" or (pool == "any" and not mounted)) and lc.cgroup_parent != D3_SLICE:
        f("CM-08", f"CPU-pool container is not under {D3_SLICE} (cgroup parent {lc.cgroup_parent!r})", "warn")
    return out


def check_launches(launches: Iterable[Launch]) -> list[Finding]:
    """逐个 `check_launch`，另检查不同 Run 渲染出的 gz 实例：GZ_PARTITION 与容器名两两不同（CM-07、CM-06）。"""
    ls = list(launches)
    out = [x for lc in ls for x in check_launch(lc)]
    seen_p: dict[str, Launch] = {}
    seen_n: dict[str, Launch] = {}
    for lc in ls:
        if lc.run is None:
            continue
        for key, seen, rule in ((lc.env.get("GZ_PARTITION") if _is_gz(lc, component_of(lc)) else None, seen_p, "CM-07"), (lc.name, seen_n, "CM-06")):
            if not key:
                continue
            o = seen.get(key)
            if o is not None and o.run != lc.run:
                what = "GZ_PARTITION" if rule == "CM-07" else "container name"
                out.append(Finding(rule, "error", lc.site, lc.name, f"{what} {key!r} shared by runs {o.run!r} and {lc.run!r}"))
            seen.setdefault(key, lc)
    return out


# ==================================================================== 容器加固：seccomp 与运行期审计（D3-AC-59）

SECCOMP_NAME = "d3-agent-v1"
PIDS_LIMIT = 256                           # --pids-limit（§5.6.7）
TMPFS_MB = 256                             # /tmp tmpfs 容量（§5.6.7：≤ 1 GB）
HIDDEN_PATH = "/data/d3/hidden"            # 隐藏存储在宿主的路径；容器内同名路径只挂载只读诱饵（审计用）
SIGSYS_EXIT = 128 + 31                     # 主进程被 seccomp KILL_PROCESS 终止时的容器退出码
HARDEN_CODE = 691
HARDEN_VECTORS = ("syscall", "network", "fork_bomb", "tmpfs_fill", "hidden_read")
TMPFS_FULL_FRAC = 0.95
MONITOR_IMAGE = "d3-agent-py:dev"          # 监视容器镜像（受信基础镜像，非提交镜像；AWR_AGENT_MONITOR_IMAGE 覆盖）

# moby 缺省档（moby/profiles seccomp/default.json，2026-10 版）中不依赖能力的无条件放行集合；ptrace 与跨进程内存另列为 KILL
_SECCOMP_ALLOW_TEXT = """
accept accept4 access adjtimex alarm bind brk cachestat capget capset chdir chmod chown chown32 clock_adjtime clock_adjtime64
clock_getres clock_getres_time64 clock_gettime clock_gettime64 clock_nanosleep clock_nanosleep_time64 close close_range connect
copy_file_range creat dup dup2 dup3 epoll_create epoll_create1 epoll_ctl epoll_ctl_old epoll_pwait epoll_pwait2 epoll_wait
epoll_wait_old eventfd eventfd2 execve execveat exit exit_group faccessat faccessat2 fadvise64 fadvise64_64 fallocate fanotify_mark
fchdir fchmod fchmodat fchmodat2 fchown fchown32 fchownat fcntl fcntl64 fdatasync fgetxattr flistxattr flock fork fremovexattr fsetxattr
fstat fstat64 fstatat64 fstatfs fstatfs64 fsync ftruncate ftruncate64 futex futex_requeue futex_time64 futex_wait futex_waitv futex_wake
futimesat getcpu getcwd getdents getdents64 getegid getegid32 geteuid geteuid32 getgid getgid32 getgroups getgroups32 getitimer
getpeername getpgid getpgrp getpid getppid getpriority getrandom getresgid getresgid32 getresuid getresuid32 getrlimit get_robust_list
getrusage getsid getsockname getsockopt get_thread_area gettid gettimeofday getuid getuid32 getxattr getxattrat inotify_add_watch
inotify_init inotify_init1 inotify_rm_watch io_cancel ioctl io_destroy io_getevents io_pgetevents io_pgetevents_time64 ioprio_get
ioprio_set io_setup io_submit ipc kill landlock_add_rule landlock_create_ruleset landlock_restrict_self lchown lchown32 lgetxattr link
linkat listen listmount listxattr listxattrat llistxattr _llseek lremovexattr lseek lsetxattr lstat lstat64 madvise map_shadow_stack
membarrier memfd_create memfd_secret mincore mkdir mkdirat mknod mknodat mlock mlock2 mlockall mmap mmap2 mprotect mq_getsetattr
mq_notify mq_open mq_timedreceive mq_timedreceive_time64 mq_timedsend mq_timedsend_time64 mq_unlink mremap mseal msgctl msgget msgrcv
msgsnd msync munlock munlockall munmap name_to_handle_at nanosleep newfstatat _newselect open openat openat2 pause pidfd_open
pidfd_send_signal pipe pipe2 pkey_alloc pkey_free pkey_mprotect poll ppoll ppoll_time64 prctl pread64 preadv preadv2 prlimit64
process_mrelease pselect6 pselect6_time64 pwrite64 pwritev pwritev2 read readahead readlink readlinkat readv recv recvfrom recvmmsg
recvmmsg_time64 recvmsg remap_file_pages removexattr removexattrat rename renameat renameat2 restart_syscall riscv_hwprobe rmdir rseq
rt_sigaction rt_sigpending rt_sigprocmask rt_sigqueueinfo rt_sigreturn rt_sigsuspend rt_sigtimedwait rt_sigtimedwait_time64
rt_tgsigqueueinfo sched_getaffinity sched_getattr sched_getparam sched_get_priority_max sched_get_priority_min sched_getscheduler
sched_rr_get_interval sched_rr_get_interval_time64 sched_setaffinity sched_setattr sched_setparam sched_setscheduler sched_yield seccomp
select semctl semget semop semtimedop semtimedop_time64 send sendfile sendfile64 sendmmsg sendmsg sendto setfsgid setfsgid32 setfsuid
setfsuid32 setgid setgid32 setgroups setgroups32 setitimer setpgid setpriority setregid setregid32 setresgid setresgid32 setresuid
setresuid32 setreuid setreuid32 setrlimit set_robust_list setsid setsockopt set_thread_area set_tid_address setuid setuid32 setxattr
setxattrat shmat shmctl shmdt shmget shutdown sigaltstack signalfd signalfd4 sigprocmask sigreturn socketcall socketpair splice stat
stat64 statfs statfs64 statmount statx symlink symlinkat sync sync_file_range syncfs sysinfo tee tgkill time timer_create timer_delete
timer_getoverrun timer_gettime timer_gettime64 timer_settime timer_settime64 timerfd_create timerfd_gettime timerfd_gettime64
timerfd_settime timerfd_settime64 times tkill truncate truncate64 ugetrlimit umask uname unlink unlinkat uretprobe utime utimensat
utimensat_time64 utimes vfork vmsplice wait4 waitid waitpid write writev
"""
_SECCOMP_ALLOW = tuple(_SECCOMP_ALLOW_TEXT.split())

# 逃逸类：Agent 无正当用途，尝试即终止进程（KILL_PROCESS，内核记录审计事件）
SECCOMP_KILL = ("mount", "umount", "umount2", "mount_setattr", "move_mount", "open_tree", "fsopen", "fsconfig", "fsmount", "fspick",
                "pivot_root", "chroot", "unshare", "setns", "init_module", "finit_module", "delete_module", "kexec_load", "kexec_file_load",
                "ptrace", "process_vm_readv", "process_vm_writev", "pidfd_getfd", "open_by_handle_at", "reboot", "swapon", "swapoff",
                "iopl", "ioperm")
_SOCKET_DOMAINS = (7, 8, 9, 10, 13, 14, 15, 16, 17, 18, 21, 22, 24, 26, 27, 28, 29, 30, 31, 32, 33, 35, 36, 39, 41, 42, 43, 44, 45)
_PERSONALITY = (0, 8, 131072, 131080, 4294967295)
_CLONE_NS_MASK = 0x7E020000                # CLONE_NEW{NS,CGROUP,UTS,IPC,USER,PID,NET}


def seccomp_profile() -> dict:
    """seccomp 配置 `d3-agent-v1`（Docker JSON 格式）。缺省 EPERM；放行集合见 `_SECCOMP_ALLOW`；`SECCOMP_KILL` 为 KILL_PROCESS。"""
    kill = set(SECCOMP_KILL)
    allow = sorted(n for n in _SECCOMP_ALLOW if n not in kill)
    rules: list[dict] = [{"names": allow, "action": "SCMP_ACT_ALLOW"}]
    rules.append({"names": ["socket"], "action": "SCMP_ACT_ALLOW", "args": [{"index": 0, "value": 3, "valueTwo": 0, "op": "SCMP_CMP_LT"}]})
    rules += [{"names": ["socket"], "action": "SCMP_ACT_ALLOW", "args": [{"index": 0, "value": d, "valueTwo": 0, "op": "SCMP_CMP_EQ"}]}
              for d in _SOCKET_DOMAINS]
    rules += [{"names": ["personality"], "action": "SCMP_ACT_ALLOW", "args": [{"index": 0, "value": v, "valueTwo": 0, "op": "SCMP_CMP_EQ"}]}
              for v in _PERSONALITY]
    rules += [{"names": ["arch_prctl"], "action": "SCMP_ACT_ALLOW", "includes": {"arches": ["amd64", "x32"]}},
              {"names": ["modify_ldt"], "action": "SCMP_ACT_ALLOW", "includes": {"arches": ["amd64", "x32", "x86"]}},
              {"names": ["arm_fadvise64_64", "arm_sync_file_range", "sync_file_range2", "breakpoint", "cacheflush", "set_tls"],
               "action": "SCMP_ACT_ALLOW", "includes": {"arches": ["arm", "arm64"]}},
              {"names": ["clone"], "action": "SCMP_ACT_ALLOW", "args": [{"index": 0, "value": _CLONE_NS_MASK, "valueTwo": 0, "op": "SCMP_CMP_MASKED_EQ"}]},
              {"names": ["clone3"], "action": "SCMP_ACT_ERRNO", "errnoRet": 38},
              {"names": sorted(kill), "action": "SCMP_ACT_KILL_PROCESS"}]
    return {"defaultAction": "SCMP_ACT_ERRNO", "defaultErrnoRet": 1,
            "archMap": [{"architecture": "SCMP_ARCH_X86_64", "subArchitectures": ["SCMP_ARCH_X86", "SCMP_ARCH_X32"]},
                        {"architecture": "SCMP_ARCH_AARCH64", "subArchitectures": ["SCMP_ARCH_ARM"]}],
            "syscalls": rules}


def _seccomp_bytes() -> bytes:
    return json.dumps(seccomp_profile(), sort_keys=True, separators=(",", ":")).encode()


def seccomp_path() -> Path:
    """配置文件路径（docker CLI 在客户端读取，故为启动器本地路径）：按内容寻址，不含运行标识（M26-FR-072）。"""
    return Path(tempfile.gettempdir()) / f"d3-seccomp-{os.getuid()}" / f"{SECCOMP_NAME}-{hashlib.sha256(_seccomp_bytes()).hexdigest()[:12]}.json"


def write_seccomp() -> Path:
    p = seccomp_path()
    b = _seccomp_bytes()
    if not (p.exists() and p.read_bytes() == b):
        p.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.{os.getpid()}")
        tmp.write_bytes(b)
        os.replace(tmp, p)
    return p


class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.unix_path)
        self.sock = s


def engine_stats(name: str, *, timeout: float = 2.0) -> dict | None:
    """Docker Engine API 单次统计（`GET /containers/<名>/stats?stream=false&one-shot=true`）；`DOCKER_HOST` 须为 unix 套接字。"""
    host = os.environ.get("DOCKER_HOST") or "unix:///var/run/docker.sock"
    if not host.startswith("unix://"):
        return None
    c = _UnixHTTP(host[len("unix://"):], timeout)
    try:
        c.request("GET", f"/containers/{name}/stats?stream=false&one-shot=true")
        r = c.getresponse()
        body = r.read()
        return json.loads(body) if r.status == 200 else None
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        c.close()


def net_noroutes(text: str) -> dict[str, int]:
    """`/proc/net/snmp` 与 `/proc/net/snmp6` 文本 → `{ip_out_no_routes, ip6_out_no_routes}`（缺项不出现）。"""
    out: dict[str, int] = {}
    lines = text.splitlines()
    for a, b in itertools.pairwise(lines):
        ha, hb = a.split(), b.split()
        if ha[:1] == ["Ip:"] and hb[:1] == ["Ip:"] and "OutNoRoutes" in ha and len(hb) == len(ha) and hb[1].lstrip("-").isdigit():
            out["ip_out_no_routes"] = int(hb[ha.index("OutNoRoutes")])
    m = re.search(r"^Ip6OutNoRoutes\s+(\d+)", text, re.M)
    if m:
        out["ip6_out_no_routes"] = int(m.group(1))
    return out


_IN_ACCESS, _IN_OPEN, _IN_ISDIR = 0x1, 0x20, 0x40000000
_IN_EVENT = struct.Struct("iIII")


class _Inotify:
    """目录的 inotify 监视（只记打开与读取；inotify 按 inode 生效，经其他容器的绑定挂载访问同样产生事件）。"""

    def __init__(self, path: Path) -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        self.fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")
        if libc.inotify_add_watch(self.fd, os.fsencode(path), _IN_ACCESS | _IN_OPEN) < 0:
            e = ctypes.get_errno()
            os.close(self.fd)
            raise OSError(e, f"inotify_add_watch {path}")

    def poll(self, timeout_s: float) -> list[dict]:
        r, _, _ = select.select([self.fd], [], [], timeout_s)
        if not r:
            return []
        try:
            buf = os.read(self.fd, 65536)
        except BlockingIOError:
            return []
        out, i = [], 0
        while i + _IN_EVENT.size <= len(buf):
            _, mask, _, n = _IN_EVENT.unpack_from(buf, i)
            nm = buf[i + _IN_EVENT.size:i + _IN_EVENT.size + n].rstrip(b"\0").decode("utf-8", "replace")
            i += _IN_EVENT.size + n
            out.append({"name": nm or ".", "op": "open" if mask & _IN_OPEN else "read", "dir": bool(mask & _IN_ISDIR)})
        return out

    def close(self) -> None:
        with contextlib.suppress(OSError):
            os.close(self.fd)


def make_decoy(d: Path) -> Path:
    """隐藏存储诱饵目录（只读挂载到容器内 `HIDDEN_PATH`）：一个空清单文件，不含任何真实数据。"""
    d.mkdir(parents=True, exist_ok=True)
    for f in d.iterdir():
        if f.is_file():
            f.unlink()
    (d / "index.json").write_text("{}\n")
    os.chmod(d / "index.json", 0o444)
    os.chmod(d, 0o755)
    return d


class HardeningMonitor:
    """单机运行期加固审计（不提权、不改宿主配置；证据来源见模块说明）。`watch()` 在 Agent 容器启动前调用（诱饵监视与网络
    命名空间宿主，失败时返回 False），`attach()` 在启动后调用（统计采样），`finish(exit_code)` 在 Agent 容器删除后调用 → 证据字典。"""

    def __init__(self, name: str, *, docker: str = "docker", image: str | None = None, decoy: Path | None = None, labels: Sequence[str] = (),
                 interval_s: float = 0.25, stats: Callable[[str], dict | None] | None = None) -> None:
        self.name, self.docker, self.decoy, self.interval_s = name, docker, decoy, interval_s
        self.image = image or os.environ.get("AWR_AGENT_MONITOR_IMAGE") or MONITOR_IMAGE
        self.labels = list(labels)
        self.stats = stats or engine_stats
        self.mon_name = f"{name}-mon"
        self.pids_peak = self.shmem_peak = self.samples = 0
        self.net: dict[str, int] | None = None
        self.hidden: list[dict] = []
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._ino: _Inotify | None = None
        self._mon_up = self._mon_tried = self._attached = False

    def monitor_argv(self) -> list[str]:
        """监视容器：持有 Agent 的网络命名空间（`--network none`，只读取计数），非 root、无能力、只读根、最小资源。"""
        argv = [self.docker, "run", "-d", "--name", self.mon_name, "--label", "d3.component=agent", "--label", "d3.role=hardening-monitor"]
        for lab in self.labels:
            argv += ["--label", lab]
        return [*argv, "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--pids-limit", "8", "--cpus", "0.1", "--memory", "32m", "--user", "65534:65534", self.image, "sleep", "2147483647"]

    def _spawn(self, fn: Callable[[], None]) -> None:
        t = threading.Thread(target=fn, daemon=True)
        t.start()
        self._threads.append(t)

    @property
    def network(self) -> str:
        """Agent 容器的 `--network` 取值。"""
        return f"container:{self.mon_name}"

    def watch(self) -> bool:
        subprocess.run([self.docker, "rm", "-f", self.mon_name], capture_output=True, check=False)
        self._mon_tried = True
        r = subprocess.run(self.monitor_argv(), capture_output=True, text=True, check=False)
        self._mon_up = r.returncode == 0
        if not self._mon_up:
            self.errors.append(f"monitor: {r.stderr.strip()[-200:]}")
            return False
        if self.decoy is not None:
            try:
                self._ino = _Inotify(self.decoy)
            except OSError as e:
                self.errors.append(f"inotify: {e}")
                return False
            ino = self._ino

            def loop() -> None:
                while not self._stop.is_set():
                    self.hidden += ino.poll(0.2)
                self.hidden += ino.poll(0.0)
            self._spawn(loop)
        return True

    def sample(self) -> None:
        st = self.stats(self.name)
        if not st:
            return
        self.samples += 1
        self.pids_peak = max(self.pids_peak, int((st.get("pids_stats") or {}).get("current") or 0))
        self.shmem_peak = max(self.shmem_peak, int(((st.get("memory_stats") or {}).get("stats") or {}).get("shmem") or 0))

    def attach(self) -> None:
        self._attached = True

        def loop() -> None:
            while not self._stop.is_set():
                self.sample()
                self._stop.wait(self.interval_s)
        self._spawn(loop)

    def read_net(self) -> None:
        if not self._mon_up:
            return
        r = subprocess.run([self.docker, "exec", self.mon_name, "cat", "/proc/net/snmp", "/proc/net/snmp6"], capture_output=True, text=True,
                           timeout=10, check=False)
        if r.returncode != 0:
            return
        cur = net_noroutes(r.stdout)
        self.net = {k: max(v, (self.net or {}).get(k, 0)) for k, v in cur.items()}

    def finish(self, exit_code: int | None) -> dict:
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.read_net()                                      # 监视容器持有该网络命名空间：计数在 Agent 退出后仍可读
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5)
        if self._ino is not None:
            self._ino.close()
        if self._mon_tried:                                      # 启动失败时 docker 仍可能留下 Created 状态的容器
            subprocess.run([self.docker, "rm", "-f", self.mon_name], capture_output=True, check=False)
        if self._attached and self.samples == 0:                 # Engine API 不可达（如 DOCKER_HOST 非 unix 套接字）：进程数与 tmpfs 无证据
            self.errors.append("stats: no samples")
        return {"exit_code": exit_code, "pids_peak": self.pids_peak, "shmem_peak_b": self.shmem_peak, "samples": self.samples,
                "net": self.net, "hidden": self.hidden[:64], "hidden_events": len(self.hidden), "errors": self.errors}


def hardening_findings(ev: dict, *, pids_limit: int = PIDS_LIMIT, tmpfs_b: int = TMPFS_MB << 20) -> list[tuple[str, dict]]:
    """证据字典（`HardeningMonitor.finish`）→ 触发的加固向量 `[(vector, evidence)]`，向量顺序同 `HARDEN_VECTORS`。"""
    out: list[tuple[str, dict]] = []
    if ev.get("exit_code") == SIGSYS_EXIT:
        out.append(("syscall", {"exit_code": SIGSYS_EXIT, "signal": "SIGSYS", "seccomp": SECCOMP_NAME}))
    net = ev.get("net") or {}
    if any(v > 0 for v in net.values()):
        out.append(("network", {"network": "none", **net}))
    if int(ev.get("pids_peak") or 0) >= pids_limit:
        out.append(("fork_bomb", {"pids_peak": int(ev["pids_peak"]), "pids_limit": pids_limit}))
    if int(ev.get("shmem_peak_b") or 0) >= TMPFS_FULL_FRAC * tmpfs_b:
        out.append(("tmpfs_fill", {"shmem_peak_b": int(ev["shmem_peak_b"]), "tmpfs_b": tmpfs_b}))
    if ev.get("hidden"):
        out.append(("hidden_read", {"path": HIDDEN_PATH, "mounted": "decoy", "events": ev["hidden"][:16], "n_events": ev.get("hidden_events")}))
    return out


class AgentContainers:
    def __init__(self, run_dir: Path, run_id: str, uavs: Sequence[str], image: str, *, cmd: Sequence[str] | None = None,
                 env: dict[str, str] | None = None, cpus: float = 1.0, mem_gb: float = 2.0, docker: str = "docker",
                 hello_timeout_s: float = 60.0, ready_timeout_s: float = 30.0, harden_audit: bool | None = None,
                 audit_sink: Callable[..., object] | None = None, monitor_image: str | None = None) -> None:
        for k in env or {}:
            if not _ENV_KEY.match(k) or k.startswith("DRONE4D_"):
                raise ValueError(f"entry.env key {k!r} is not allowed")
        self.run_dir, self.run_id, self.uavs, self.image = Path(run_dir), run_id, list(uavs), image
        self.cmd, self.env, self.cpus, self.mem_gb, self.docker = list(cmd or []), dict(env or {}), cpus, mem_gb, docker
        self.hello_timeout_s, self.ready_timeout_s = hello_timeout_s, ready_timeout_s
        self.harden_audit = os.environ.get("AWR_AGENT_HARDEN_AUDIT") == "1" if harden_audit is None else harden_audit
        self.audit_sink, self.monitor_image = audit_sink, monitor_image
        self.hosts: dict[str, SocketAgentHost] = {}
        self.names: dict[str, str] = {}
        self.monitors: dict[str, HardeningMonitor] = {}
        self.hardening: dict[str, list[dict]] = {}

    def name(self, uav: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]", "-", f"d3-agent-{self.run_id}-{uav}")[:120]

    def argv(self, uav: str, sock_dir: Path, *, decoy: Path | None = None, network: str = "none") -> list[str]:
        """该机 Agent 容器的完整 `docker run` 参数表（CPU 池：不挂载 GPU；设 --cpus、--memory；命名 d3-agent-<run>-<uav>；
        seccomp 配置 `d3-agent-v1`；审计开启时 `decoy` 为只读挂载到 `HIDDEN_PATH` 的诱饵目录，`network` 为监视容器的网络命名空间）。"""
        if network != "none" and not network.startswith("container:d3-agent-"):
            raise ValueError(f"agent network {network!r} must be none or a hardening monitor namespace")
        argv = [self.docker, "run", "-d", "--name", self.name(uav), "--label", f"d3.run={self.run_id}", "--label", "d3.component=agent",
                "--network", network, "--read-only", "--tmpfs", f"/tmp:rw,size={TMPFS_MB}m", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--security-opt", f"seccomp={seccomp_path()}", "--pids-limit", str(PIDS_LIMIT), "--cpus", f"{self.cpus:g}", "--memory",
                f"{self.mem_gb:g}g", "--user", "65534:65534", "-v", f"{host_path(sock_dir)}:/run/drone4d"]
        if decoy is not None:
            argv += ["-v", f"{host_path(decoy)}:{HIDDEN_PATH}:ro"]
        argv += ["-e", "DRONE4D_AGENT_SOCK=/run/drone4d/agent.sock"]
        for k, v in sorted(self.env.items()):
            argv += ["-e", f"{k}={v}"]
        return [*argv, self.image, *self.cmd]

    def start(self) -> None:
        root = os.environ.get("AWR_AGENT_SOCK_ROOT")
        tag = hashlib.sha256(f"{self.run_dir.resolve()}|{self.run_id}".encode()).hexdigest()[:10]
        write_seccomp()
        for u in self.uavs:
            d = Path(root) / f"{tag}-{u}" if root else self.run_dir / "agent_sock" / u
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o755)
            self.hosts[u] = SocketAgentHost(u, d / "agent.sock", hello_timeout_s=self.hello_timeout_s, ready_timeout_s=self.ready_timeout_s)
            name = self.name(u)
            decoy = make_decoy(d.with_name(f"{d.name}-hid")) if self.harden_audit else None
            mon = HardeningMonitor(name, docker=self.docker, image=self.monitor_image, decoy=decoy, labels=[f"d3.run={self.run_id}"]) \
                if self.harden_audit else None
            argv = self.argv(u, d, decoy=decoy, network=mon.network if mon is not None else "none")
            errs = [x for x in check_launch(parse_docker_run(argv, "agent", component="agent", run=self.run_id)) if x.level == "error"]
            if errs:
                self.stop()
                raise WireError(664, "container_manifest: " + "; ".join(f"{x.rule} {x.message}" for x in errs))
            if mon is not None:                                  # 审计开启而监视不可用：不启动（宁可拒绝，不做无审计运行）
                self.monitors[u] = mon
                if not mon.watch():
                    self.stop()
                    raise WireError(664, f"hardening_monitor: {'; '.join(mon.errors)[-300:]}")
            subprocess.run([self.docker, "rm", "-f", name], capture_output=True, check=False)
            r = subprocess.run(argv, capture_output=True, text=True, check=False)
            if r.returncode != 0:
                self.stop()
                raise WireError(664, f"container_start: {r.stderr.strip()[-300:]}")
            self.names[u] = name
            if mon is not None:
                mon.attach()

    def accept(self) -> None:
        try:
            for h in self.hosts.values():
                h.accept()
        except WireError:
            self.stop()
            raise

    def stop(self) -> dict[str, int | None]:
        """CLOSE 之后 Agent 应自行退出：等待 ≤ 10 s，否则强制结束；→ 每机容器退出码。启用运行期审计时另写
        `agents/<uav>/hardening.jsonl`（每个触发的加固向量一条 691 记录，同时交给 `audit_sink`），记录存于 `self.hardening`。"""
        for h in self.hosts.values():
            h.close("run_failed")
        codes: dict[str, int | None] = {}
        for u, name in self.names.items():
            mon = self.monitors.pop(u, None)
            if mon is not None:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    mon.read_net()
            try:
                subprocess.run([self.docker, "wait", name], capture_output=True, timeout=10, check=False)
            except subprocess.TimeoutExpired:
                subprocess.run([self.docker, "kill", name], capture_output=True, check=False)
            ins = subprocess.run([self.docker, "inspect", "-f", "{{.State.ExitCode}}", name], capture_output=True, text=True, check=False)
            code = ins.stdout.strip()
            codes[u] = int(code) if ins.returncode == 0 and code.lstrip("-").isdigit() else None
            logs = subprocess.run([self.docker, "logs", name], capture_output=True, check=False)
            out = self.run_dir / "agents" / u
            out.mkdir(parents=True, exist_ok=True)
            (out / "container.log").write_bytes((logs.stdout + logs.stderr)[-LOG_TAIL_B:])
            subprocess.run([self.docker, "rm", "-f", name], capture_output=True, check=False)
            if mon is not None:                                  # Agent 删除之后再回收监视容器（Agent 依赖其网络命名空间）
                self._audit(u, name, mon.finish(codes[u]), out)
        for mon in self.monitors.values():                       # 启动失败的机：只回收监视资源
            mon.finish(None)
        self.monitors.clear()
        self.names.clear()
        return codes

    def _audit(self, uav: str, name: str, ev: dict, out: Path) -> None:
        recs = []
        for vector, evidence in hardening_findings(ev):
            rec = {"t_unix_ns": str(time.time_ns()), "action": "deny", "code": HARDEN_CODE, "why": "hardening", "vector": vector,
                   "run_id": self.run_id, "uav": uav, "container": name, "evidence": evidence}
            recs.append(rec)
            if self.audit_sink is not None:
                self.audit_sink("deny", **{k: v for k, v in rec.items() if k not in ("t_unix_ns", "action")})
        self.hardening[uav] = recs
        with open(out / "hardening.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"kind": "evidence", "run_id": self.run_id, "uav": uav, "container": name, **ev}, ensure_ascii=False,
                               sort_keys=True) + "\n")
            for rec in recs:
                f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")


def run_container_episode(env, run_dir: Path, image: str, *, cmd: Sequence[str] | None = None, entry_env: dict[str, str] | None = None,
                          cpus: float = 1.0, mem_gb: float = 2.0, step_budget_s: float | None = None, docker: str = "docker",
                          hello_timeout_s: float = 60.0, harden_audit: bool | None = None,
                          audit_sink: Callable[..., object] | None = None) -> tuple[dict, dict]:
    """→ (运行清单, 每机线协议统计与容器退出码)。容器在 Env reset 之前启动并完成 HELLO（Env 预滚期间 Agent 已就绪）。
    启用运行期审计时每机统计另含 `hardening`（触发的加固向量，691）。"""
    ac = AgentContainers(run_dir, env.spec.run_id, env.possible_agents, image, cmd=cmd, env=entry_env, cpus=cpus, mem_gb=mem_gb, docker=docker,
                         hello_timeout_s=hello_timeout_s, harden_audit=harden_audit, audit_sink=audit_sink)
    ac.start()
    try:
        ac.accept()
        manifest, stats = run_remote_episode(env, ac.hosts, step_budget_s=step_budget_s)
    finally:
        codes = ac.stop()
    extra = {u: {"hardening": [r["vector"] for r in ac.hardening.get(u, [])]} for u in env.possible_agents} if ac.harden_audit else {}
    return manifest, {u: {**stats.get(u, {}), "exit_code": codes.get(u), **extra.get(u, {})} for u in env.possible_agents}
