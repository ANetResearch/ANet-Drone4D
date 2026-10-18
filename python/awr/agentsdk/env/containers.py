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
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from awr.agentsdk.env.remote import SocketAgentHost, run_remote_episode
from awr.agentsdk.wire import WireError
from awr.phys.gz.instance import host_path

__all__ = ["ALLOWED_GPUS", "COMPONENTS", "D3_SLICE", "RULES", "AgentContainers", "Component", "Finding", "Launch", "check_launch",
           "check_launches", "component_of", "host_path", "launch_from_inspect", "parse_docker_run", "run_container_episode"]

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


class AgentContainers:
    def __init__(self, run_dir: Path, run_id: str, uavs: Sequence[str], image: str, *, cmd: Sequence[str] | None = None,
                 env: dict[str, str] | None = None, cpus: float = 1.0, mem_gb: float = 2.0, docker: str = "docker",
                 hello_timeout_s: float = 60.0, ready_timeout_s: float = 30.0) -> None:
        for k in env or {}:
            if not _ENV_KEY.match(k) or k.startswith("DRONE4D_"):
                raise ValueError(f"entry.env key {k!r} is not allowed")
        self.run_dir, self.run_id, self.uavs, self.image = Path(run_dir), run_id, list(uavs), image
        self.cmd, self.env, self.cpus, self.mem_gb, self.docker = list(cmd or []), dict(env or {}), cpus, mem_gb, docker
        self.hello_timeout_s, self.ready_timeout_s = hello_timeout_s, ready_timeout_s
        self.hosts: dict[str, SocketAgentHost] = {}
        self.names: dict[str, str] = {}

    def name(self, uav: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]", "-", f"d3-agent-{self.run_id}-{uav}")[:120]

    def argv(self, uav: str, sock_dir: Path) -> list[str]:
        """该机 Agent 容器的完整 `docker run` 参数表（CPU 池：不挂载 GPU；设 --cpus、--memory；命名 d3-agent-<run>-<uav>）。"""
        argv = [self.docker, "run", "-d", "--name", self.name(uav), "--label", f"d3.run={self.run_id}", "--label", "d3.component=agent",
                "--network", "none", "--read-only", "--tmpfs", "/tmp:rw,size=256m", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--pids-limit", "256", "--cpus", f"{self.cpus:g}", "--memory", f"{self.mem_gb:g}g", "--user", "65534:65534",
                "-v", f"{host_path(sock_dir)}:/run/drone4d", "-e", "DRONE4D_AGENT_SOCK=/run/drone4d/agent.sock"]
        for k, v in sorted(self.env.items()):
            argv += ["-e", f"{k}={v}"]
        return [*argv, self.image, *self.cmd]

    def start(self) -> None:
        root = os.environ.get("AWR_AGENT_SOCK_ROOT")
        tag = hashlib.sha256(f"{self.run_dir.resolve()}|{self.run_id}".encode()).hexdigest()[:10]
        for u in self.uavs:
            d = Path(root) / f"{tag}-{u}" if root else self.run_dir / "agent_sock" / u
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o755)
            self.hosts[u] = SocketAgentHost(u, d / "agent.sock", hello_timeout_s=self.hello_timeout_s, ready_timeout_s=self.ready_timeout_s)
            name = self.name(u)
            argv = self.argv(u, d)
            errs = [x for x in check_launch(parse_docker_run(argv, "agent", component="agent", run=self.run_id)) if x.level == "error"]
            if errs:
                self.stop()
                raise WireError(664, "container_manifest: " + "; ".join(f"{x.rule} {x.message}" for x in errs))
            subprocess.run([self.docker, "rm", "-f", name], capture_output=True, check=False)
            r = subprocess.run(argv, capture_output=True, text=True, check=False)
            if r.returncode != 0:
                self.stop()
                raise WireError(664, f"container_start: {r.stderr.strip()[-300:]}")
            self.names[u] = name

    def accept(self) -> None:
        try:
            for h in self.hosts.values():
                h.accept()
        except WireError:
            self.stop()
            raise

    def stop(self) -> dict[str, int | None]:
        """CLOSE 之后 Agent 应自行退出：等待 ≤ 10 s，否则强制结束；→ 每机容器退出码。"""
        for h in self.hosts.values():
            h.close("run_failed")
        codes: dict[str, int | None] = {}
        for u, name in self.names.items():
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
        self.names.clear()
        return codes


def run_container_episode(env, run_dir: Path, image: str, *, cmd: Sequence[str] | None = None, entry_env: dict[str, str] | None = None,
                          cpus: float = 1.0, mem_gb: float = 2.0, step_budget_s: float | None = None, docker: str = "docker",
                          hello_timeout_s: float = 60.0) -> tuple[dict, dict]:
    """→ (运行清单, 每机线协议统计与容器退出码)。容器在 Env reset 之前启动并完成 HELLO（Env 预滚期间 Agent 已就绪）。"""
    ac = AgentContainers(run_dir, env.spec.run_id, env.possible_agents, image, cmd=cmd, env=entry_env, cpus=cpus, mem_gb=mem_gb, docker=docker,
                         hello_timeout_s=hello_timeout_s)
    ac.start()
    try:
        ac.accept()
        manifest, stats = run_remote_episode(env, ac.hosts, step_budget_s=step_budget_s)
    finally:
        codes = ac.stop()
    return manifest, {u: {**stats.get(u, {}), "exit_code": codes.get(u)} for u in env.possible_agents}
