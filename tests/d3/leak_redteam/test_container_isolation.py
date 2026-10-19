"""Agent 容器隔离实测（D3-AC-16"Agent 容器对 dmax 内网、公网、catalog、沙盒 API 与 bench-api 的连接全部失败，挂载路径、主机名与
环境变量不含运行标识"；M26 §8.1 容器规格、FR-071、FR-072、§8.2 L-18、L-20、§8.3 R4 A11；M26-AC-010 的 A11 探测部分）。

以真实 `d3-agent-py` 镜像（`AWR_A08_AGENT_IMAGE`，缺省 `d3-agent-py:dev`）经生产启动器 `AgentContainers`（`run_container_episode`，
与 bench runner、subworker 相同的 `docker run` 参数表）在排名配置下运行一个回合。容器入口为探测 Agent：先在容器内执行 A11
探测，再以 `drone4d.serve --baseline static_guard` 完成整个回合（HELLO、CONFIG、逐步 OBS/ACT、CLOSE），探测结果经容器日志
（启动器回收为 `agents/<uav>/container.log`）带回宿主。

- 网络（L-18）：宿主侧在 dmax 本机全部 IPv4 地址（回环、ZeroTier、docker 网桥、局域网与公网地址）上设诱饵监听，并在部署
  登记的端口上启动真实服务或诱饵：bench-api（真实 `awr.api.bench_main` 应用，127.0.0.1:18650，17 §18.15）、lab-gateway 18651、
  rerun-proxy 18652、分发 catalog（真实 Rerun OSS catalog 服务，内网地址；另有 9876、51234 两个 Rerun 缺省端口）、D2 沙盒
  sandbox-api 8002 与公开档 18641、show-api 18640；公网取 1.1.1.1、8.8.8.8、223.5.5.5、114.114.114.114 与 IPv6 地址，域名解析
  取 pypi.org、modelscope.cn、github.com；另有 UDP 数据报、抽象 Unix 套接字诱饵与 docker、containerd 套接字。判据：探测 Agent
  的每个目标都失败（TCP 连接、UDP 往返（诱饵逐个应答）、域名解析、Unix 连接），网卡只有 `lo`、无路由，诱饵在探测期间收到的连接与数据报为 0。
  正向对照：同一镜像、同一探测脚本在 `--network host` 的对照容器中能连上诱饵、真实 bench-api（HTTP 401）与 catalog，证明
  目标有效；对照容器只连本机地址与公网，不连其他主机。
- 元数据（L-20）：主机名（`/etc/hostname`、`gethostname`）、全部环境变量（含 `/proc/1/environ`）、`/proc/self/mountinfo`
  （宿主挂载源路径，F15）、`/proc/1/cmdline`、cgroup、`/etc/hosts`、挂载目录清单中不出现运行标识：run_id、提交 id、事件 id、
  事件目录名、世界 id、划分名、运行种子与派生键、种子承诺值、磁带与事件文件哈希、演员 id（≥ 6 字符按子串，较短的按分词
  精确匹配）；挂载目录内只有 `agent.sock`（另有空 tmpfs `/tmp`）；不挂载 docker 套接字。
- 生产配置（`AWR_AGENT_SOCK_ROOT` 短根目录，run_subworker.sh 与 run_mbl_e2e.sh 同）判通过；启动器缺省配置（套接字目录在运行
  目录内）的挂载源路径带事件目录与 run_id，作为已知缺陷以严格 xfail 跟踪（KNOWN_FINDINGS）。
- 同一回合的全部下发帧经 A06 录制并做 R1/R2 检索（容器路径、真实镜像），除 A06 已登记的已知缺陷外命中 0。

docker 不可用或镜像不存在时跳过（platform 层，正式口径在 dmax 执行，命令见 results-B03/A08.md）；分析逻辑、启动参数静态核对与
探测脚本本机自检不需要 docker。`AWR_A08_OUT` 给出时写 `isolation_report.json`（目标逐项结果、元数据检出、镜像 id、UTC 时刻）。
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from pathlib import Path

import pytest

from awr.agentsdk.env.containers import AgentContainers, check_launch, parse_docker_run

ROOT = Path(__file__).resolve().parents[3]
IMAGE = os.environ.get("AWR_A08_AGENT_IMAGE", "d3-agent-py:dev")
DOCKER = os.environ.get("AWR_A08_DOCKER", "docker")
OUT = os.environ.get("AWR_A08_OUT")
MARK = "A08PROBE "

# 已知缺陷：判据名 → 原因（修复后对应的严格 xfail 用例转为 XPASS 而失败，届时从此处移除）
KNOWN_FINDINGS = {
    "launcher.default_sock_dir": "AgentContainers 缺省把套接字目录放在运行目录内（run_dir/agent_sock/<uav>），挂载源路径经 "
                                 "/proc/self/mountinfo 暴露事件目录与 run_id；应恒用中性暂存目录（M26-FR-072、F15）",
    "fr072.neutral_metadata": "未按 M26 §8.1 设 --hostname agent；暂存令牌为 sha256(run_dir|run_id) 前 10 位而非 128 位随机数；"
                              "环境变量为 DRONE4D_AGENT_SOCK 而非 DRONE4D_SOCK/DRONE4D_PROTOCOL",
}

# 部署登记的服务端口（17 §18.15；configs/runtime.yaml；M27 §8.4）→ 名称
SERVICE_PORTS = {18650: "bench-api", 18651: "lab-gateway", 18652: "rerun-proxy", 8002: "sandbox-api", 18641: "sandbox-api-public",
                 18640: "show-api", 9876: "rerun-grpc-default", 51234: "rerun-catalog-default"}
PUBLIC_TCP = (("1.1.1.1", 443), ("8.8.8.8", 53), ("223.5.5.5", 53), ("114.114.114.114", 53), ("2606:4700:4700::1111", 443),
              ("2400:3200::1", 53))
PUBLIC_DNS = ("pypi.org", "modelscope.cn", "github.com")
DNS_QUERY = "abcd01000001000000000000037777770667697468756203636f6d0000010001"     # www.github.com A 记录查询
ENGINE_SOCKS = ("/var/run/docker.sock", "/run/docker.sock", "/run/containerd/containerd.sock", "/var/run/docker/containerd/containerd.sock")


# ====================================================================== 容器内探测脚本（只用标准库）

PROBE = r'''
import base64, errno, json, os, socket, sys, zlib
TARGETS = json.loads(@TARGETS@)
def rd(p, n=262144):
    try:
        with open(p, "rb") as f:
            return f.read(n).decode("utf-8", "replace")
    except OSError as e:
        return "!" + errno.errorcode.get(e.errno or 0, str(e.errno))
def err(e):
    if isinstance(e, socket.gaierror):
        return "gaierror:%s" % e.args[0]
    if isinstance(e, TimeoutError):
        return "timeout"
    return errno.errorcode.get(getattr(e, "errno", None) or 0, type(e).__name__)
def attempt(t):
    k, h = t["kind"], t["host"]
    try:
        if k == "dns":
            socket.getaddrinfo(h, 443)
            return {"ok": True}
        if k == "unix":
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(1.5)
            s.connect("\0" + h[1:] if h.startswith("@") else h)
            s.close()
            return {"ok": True}
        fam = socket.AF_INET6 if ":" in h else socket.AF_INET
        if k == "udp":
            s = socket.socket(fam, socket.SOCK_DGRAM)
            s.settimeout(1.5)
            s.connect((h, t["port"]))
            s.send(bytes.fromhex(t.get("payload") or "00"))
            try:
                s.recv(512)
            except OSError as e:
                return {"ok": False, "err": err(e), "sent": True}
            return {"ok": True, "reply": True}
        s = socket.create_connection((h, t["port"]), timeout=1.5)
        out = {"ok": True}
        if k == "http":
            s.settimeout(3.0)
            s.sendall(("GET %s HTTP/1.0\r\nHost: %s\r\n\r\n" % (t.get("path", "/"), h)).encode())
            out["status"] = s.recv(64).decode("latin-1").split("\r\n")[0]
        s.close()
        return out
    except Exception as e:
        return {"ok": False, "err": err(e)}
def mounts(mi):
    std = ("/proc", "/sys", "/dev")
    out = {}
    for line in mi.splitlines():
        f = line.split(" ")
        if len(f) > 5 and not f[4].startswith(std) and f[4] not in ("/", "/etc/hostname", "/etc/hosts", "/etc/resolv.conf"):
            try:
                out[f[4]] = sorted(os.listdir(f[4]))[:50]
            except OSError as e:
                out[f[4]] = "!" + errno.errorcode.get(e.errno or 0, "")
    return out
def probe():
    mi = rd("/proc/self/mountinfo")
    try:
        ifaces = sorted(os.listdir("/sys/class/net"))
    except OSError:
        ifaces = None
    st = rd("/proc/self/status")
    return {"probe": 1, "uid": os.getuid(), "gid": os.getgid(), "groups": os.getgroups(), "hostname": socket.gethostname(),
            "etc_hostname": rd("/etc/hostname"), "nodename": os.uname().nodename, "env": dict(os.environ),
            "environ1": rd("/proc/1/environ").split("\0"), "cmdline1": rd("/proc/1/cmdline").split("\0"), "mountinfo": mi,
            "cgroup": rd("/proc/self/cgroup"), "hosts": rd("/etc/hosts"), "resolv": rd("/etc/resolv.conf"), "cwd": os.getcwd(),
            "mounts": mounts(mi), "ifaces": ifaces, "net_dev": rd("/proc/net/dev"), "route": rd("/proc/net/route"),
            "route6": rd("/proc/net/ipv6_route"), "net_unix": rd("/proc/net/unix"),
            "status": {k: v.strip() for k, _, v in (x.partition(":") for x in st.splitlines()) if k in ("CapEff", "CapPrm", "NoNewPrivs", "Seccomp")},
            "engine_socks": {p: os.path.exists(p) for p in @ENGINE@},
            "results": [dict(t, **attempt(t)) for t in TARGETS]}
def emit(doc):
    sys.stdout.write("A08PROBE " + base64.b64encode(zlib.compress(json.dumps(doc, sort_keys=True).encode(), 9)).decode() + "\n")
    sys.stdout.flush()
doc = probe()
emit(doc)
if "--serve" in sys.argv:
    import runpy
    sys.argv = ["drone4d.serve", "--baseline", "static_guard"]
    try:
        runpy.run_module("drone4d.serve", run_name="__main__", alter_sys=True)
    except SystemExit:
        pass
    finally:
        emit(doc)
'''


def probe_code(targets: list[dict]) -> str:
    return PROBE.replace("@TARGETS@", repr(json.dumps(targets, sort_keys=True))).replace("@ENGINE@", repr(list(ENGINE_SOCKS)))


def parse_probe(text: str) -> dict | None:
    """容器日志 → 第一份探测结果（`A08PROBE <base64(zlib(json))>` 行）。"""
    for line in text.splitlines():
        if line.startswith(MARK):
            return json.loads(zlib.decompress(base64.b64decode(line[len(MARK):].strip())))
    return None


# ====================================================================== 运行标识与元数据检出

_SPLIT = re.compile(r"[\s/=:,;.\"'\[\]{}()\x00|@#]+")


def identifiers(truth, *, world_id: str, extra: dict[str, str]) -> dict[str, str]:
    """运行标识 → 类别：A06 `Secrets` 的字符串（演员 id、事件 id、划分名、run_id、哈希、种子与派生键文本）+ 世界 id 与 `extra`
    （提交 id、事件目录名、种子承诺值等）。"""
    out = dict(truth.secrets.strings())
    out.setdefault(world_id, "world_id")
    for k, v in extra.items():                                                          # 与已有标识同值时保留原类别
        out.setdefault(k, v)
    return {k: v for k, v in out.items() if k}


def scan_text(text: str, idents: dict[str, str]) -> list[tuple[str, str]]:
    """→ [(类别, 命中方式)]：≥ 6 字符的标识按子串（不区分大小写）检出，较短的（如划分名 dev、test）只按分词精确匹配。"""
    low = text.lower()
    toks = set(_SPLIT.split(low))
    hits = []
    for s, cls in idents.items():
        k = s.lower()
        if len(k) >= 6 and k in low:
            hits.append((cls, "substring"))
        elif len(k) < 6 and k in toks:
            hits.append((cls, "token"))
    return sorted(set(hits))


_SYS_MOUNTS = ("/proc", "/sys", "/dev")


def mount_view(mountinfo: str) -> str:
    """mountinfo 的结构化视图（分词匹配用）：每行只取挂载源在其文件系统内的路径（第 4 列，F15 所指的宿主路径）与非系统挂载点；
    系统挂载点（/proc、/sys、/dev）与设备名、文件系统类型、选项不参与分词，避免 `/dev` 被当作划分名 dev。"""
    out = []
    for line in mountinfo.splitlines():
        f = line.split(" ")
        if len(f) < 5:
            continue
        out.append(f[3])
        if not f[4].startswith(_SYS_MOUNTS):
            out.append(f[4])
    return "\n".join(out)


METADATA_FIELDS = ("hostname", "etc_hostname", "nodename", "env", "environ1", "mountinfo", "cmdline1", "cgroup", "hosts", "resolv",
                   "cwd", "mounts")


def scan_metadata(doc: dict, idents: dict[str, str], *, ignore: tuple[str, ...] = ()) -> dict[str, list[tuple[str, str]]]:
    """探测结果中的元数据字段逐项检出运行标识 → {字段: 命中}（无命中的字段不列出）。长标识在原文上按子串检出；短标识在
    mountinfo 上只对 `mount_view` 分词。`cmdline1` 去掉探测脚本本身（`-c` 之后的代码文本由测试生成，只含目标地址）；`ignore`
    为与运行无关的公共取值（如 UAV 名）。"""
    out = {}
    for f in METADATA_FIELDS:
        v = doc.get(f)
        if f == "cmdline1" and isinstance(v, list) and "-c" in v:
            v = [x for i, x in enumerate(v) if i == 0 or v[i - 1] != "-c"]
        text = v if isinstance(v, str) else json.dumps(v, sort_keys=True, ensure_ascii=False)
        view = mount_view(text) if f == "mountinfo" else text
        for s in ignore:
            text, view = text.replace(s, " "), view.replace(s, " ")
        hits = sorted({*scan_text(text, {k: c for k, c in idents.items() if len(k) >= 6}),
                       *scan_text(view, {k: c for k, c in idents.items() if len(k) < 6})})
        if hits:
            out[f] = hits
    return out


def failed_connections(doc: dict) -> tuple[list[dict], list[dict]]:
    """→ (失败的目标, 成功的目标)。"""
    res = doc.get("results") or []
    return [r for r in res if not r.get("ok")], [r for r in res if r.get("ok")]


def isolation_problems(doc: dict) -> list[str]:
    """隔离判据（L-18、A11）：全部目标失败、网卡只有 lo、无 IPv4/IPv6 路由、无引擎套接字、非 root、能力集为空、no_new_privs。"""
    p = []
    _fail, ok = failed_connections(doc)
    p += [f"reachable {r['label']} {r['kind']} {r['host']}:{r.get('port', '')}" for r in ok]
    if doc.get("ifaces") is not None and doc["ifaces"] != ["lo"]:
        p.append(f"interfaces {doc['ifaces']}")
    routes = [x for x in str(doc.get("route", "")).splitlines()[1:] if x.strip()]
    if routes:
        p.append(f"{len(routes)} IPv4 routes")
    r6 = [x for x in str(doc.get("route6", "")).splitlines() if x.strip() and not x.rstrip().endswith(" lo")]
    if r6:
        p.append(f"{len(r6)} IPv6 routes outside lo")
    p += [f"engine socket {k} present" for k, v in (doc.get("engine_socks") or {}).items() if v]
    if doc.get("uid") == 0:
        p.append("runs as root")
    st = doc.get("status") or {}
    if st.get("CapEff", "0").strip("0"):
        p.append(f"CapEff {st.get('CapEff')}")
    if st.get("NoNewPrivs") != "1":
        p.append(f"NoNewPrivs {st.get('NoNewPrivs')}")
    return p


# ====================================================================== 宿主侧目标与诱饵

SIOCGIFADDR = 0x8915


def host_ipv4() -> dict[str, str]:
    """本进程网络命名空间内的全部 IPv4 地址 → 类别（loopback、zerotier、docker、lan、public）。"""
    out = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _i, name in socket.if_nameindex():
            try:
                raw = fcntl.ioctl(s.fileno(), SIOCGIFADDR, struct.pack("256s", name[:15].encode()))
            except OSError:
                continue
            ip = socket.inet_ntoa(raw[20:24])
            a, b = (int(x) for x in ip.split(".")[:2])
            if name == "lo" or a == 127:
                cls = "loopback"
            elif name.startswith("zt"):
                cls = "zerotier"
            elif name.startswith(("docker", "br-")):
                cls = "docker"
            elif a == 10 or (a == 172 and 16 <= b < 32) or (a == 192 and b == 168) or (a == 100 and 64 <= b < 128):
                cls = "lan"
            else:
                cls = "public"
            out[ip] = cls
    finally:
        s.close()
    return out


def default_gateway() -> str | None:
    try:
        for line in Path("/proc/net/route").read_text().splitlines()[1:]:
            f = line.split()
            if len(f) > 2 and f[1] == "00000000":
                return socket.inet_ntoa(struct.pack("<I", int(f[2], 16)))
    except OSError:
        pass
    return None


class Decoys:
    """宿主侧诱饵：TCP、UDP 监听与抽象 Unix 套接字；逐个计数收到的连接与数据报（`reset` 清零）。"""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self._socks: list[socket.socket] = []
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def _serve(self, key: str, s: socket.socket, dgram: bool) -> None:
        s.settimeout(0.2)
        while not self._stop.is_set():
            try:
                if dgram:
                    _d, peer = s.recvfrom(2048)
                    s.sendto(b"A08", peer)                                           # 应答：对端以收到应答判定可达
                else:
                    c, _ = s.accept()
                    c.close()
            except (TimeoutError, OSError):
                continue
            with self._lock:
                self.counts[key] = self.counts.get(key, 0) + 1

    def _start(self, key: str, s: socket.socket, dgram: bool = False) -> None:
        self.counts[key] = 0
        self._socks.append(s)
        threading.Thread(target=self._serve, args=(key, s, dgram), daemon=True).start()

    def tcp(self, host: str, port: int = 0) -> int | None:
        s = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            s.close()
            return None
        s.listen(64)
        p = s.getsockname()[1]
        self._start(f"tcp {host}:{p}", s)
        return p

    def udp(self, host: str, port: int = 0) -> int | None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind((host, port))
        except OSError:
            s.close()
            return None
        p = s.getsockname()[1]
        self._start(f"udp {host}:{p}", s, dgram=True)
        return p

    def unix_abstract(self, name: str) -> str:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind("\0" + name)
        s.listen(8)
        self._start(f"unix @{name}", s)
        return "@" + name

    def reset(self) -> None:
        with self._lock:
            for k in self.counts:
                self.counts[k] = 0

    def total(self) -> int:
        with self._lock:
            return sum(self.counts.values())

    def close(self) -> None:
        self._stop.set()
        for s in self._socks:
            with contextlib.suppress(OSError):
                s.close()


def _start_bench_api(host: str, port: int, data_root: Path):
    """真实 bench-api 应用（`awr.api.bench_main.create_app`）以 uvicorn 在线程中监听；依赖缺失或端口占用时返回 None。"""
    try:
        import uvicorn

        from awr.api.bench_main import create_app
        from awr.bench.settings import BenchSettings
    except ImportError:
        return None
    probe = socket.socket()
    try:
        probe.bind((host, port))
    except OSError:
        return None
    finally:
        probe.close()
    srv = uvicorn.Server(uvicorn.Config(create_app(BenchSettings(data_root=data_root)), host=host, port=port, log_level="warning",
                                        lifespan="off"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    t0 = time.monotonic()
    while not srv.started and time.monotonic() - t0 < 15:
        time.sleep(0.05)
    return srv if srv.started else None


def _start_catalog(host: str, root: Path):
    """真实分发 catalog 服务（Rerun OSS catalog，`awr.bench.catalog.service.CatalogService`）；依赖缺失时返回 None。"""
    try:
        from awr.bench.catalog.service import CatalogService

        s = socket.socket()
        s.bind((host, 0))
        port = s.getsockname()[1]
        s.close()
        return CatalogService(root, "dist", host=host, port=port).start()
    except Exception:                                    # rerun / datafusion 缺失或启动失败时退回诱饵
        return None


@pytest.fixture(scope="module")
def targets(tmp_path_factory):
    """宿主侧目标表与诱饵：→ {agent: 探测 Agent 的目标, control: 对照容器的目标（只含本机与公网）, decoys, services}。"""
    dec = Decoys()
    addrs = host_ipv4()
    internal = {ip: c for ip, c in addrs.items() if c != "public"}
    agent_t: list[dict] = []
    services: dict[str, str] = {}

    def add(label: str, kind: str, host: str, port: int | None = None, **kw) -> None:
        agent_t.append({"label": label, "kind": kind, "host": host, **({"port": port} if port is not None else {}), **kw})

    # 本机全部地址：诱饵（不绑定公网地址）与 sshd
    for ip, cls in sorted(addrs.items()):                              # 公网地址上不开诱饵端口，只探测 sshd
        if cls != "public":
            p = dec.tcp(ip)
            if p:
                add(f"dmax-{cls}-decoy", "tcp", ip, p)
        add(f"dmax-{cls}-ssh", "tcp", ip, 22)
    up = dec.udp("127.0.0.1")
    if up:
        add("dmax-loopback-udp-decoy", "udp", "127.0.0.1", up)
    for ip, cls in sorted(internal.items()):
        if cls in ("zerotier", "docker") and (u := dec.udp(ip)):
            add(f"dmax-{cls}-udp-decoy", "udp", ip, u)
    add("dmax-abstract-unix-decoy", "unix", dec.unix_abstract("d3-a08-" + os.urandom(8).hex()))
    gw = default_gateway()
    agent_only = []
    if gw:
        agent_only.append({"label": "dmax-lan-gateway", "kind": "tcp", "host": gw, "port": 22})
    # 部署登记的服务端口：真实 bench-api 与分发 catalog，其余为诱饵；回环与内网地址各一份
    work = tmp_path_factory.mktemp("svc")
    srv = _start_bench_api("127.0.0.1", 18650, work / "bench")
    if srv is not None:
        services["bench-api"] = "real awr.api.bench_main on 127.0.0.1:18650"
        add("bench-api", "http", "127.0.0.1", 18650, path="/api/bench/v1/meta")
    zt = sorted(ip for ip, c in internal.items() if c == "zerotier") or sorted(ip for ip, c in internal.items() if c == "lan")
    cat_host = zt[0] if zt else "127.0.0.1"
    cat = _start_catalog(cat_host, work / "rerun")
    if cat is not None:
        host, port = cat.url.split("://", 1)[1].rsplit(":", 1)
        services["catalog-dist"] = f"real Rerun catalog on {host}:{port}"
        add("catalog-dist", "tcp", host.strip("[]"), int(port))
    for port, name in SERVICE_PORTS.items():
        for ip in ["127.0.0.1", *zt]:
            if name == "bench-api" and ip == "127.0.0.1" and srv is not None:
                continue
            got = dec.tcp(ip, port)
            if got is None:
                services.setdefault(f"{name}@{ip}", "port already bound by another process (probed as is)")
            add(name, "tcp", ip, port)
    for p in ENGINE_SOCKS:
        add("docker-engine", "unix", p)
    control = list(agent_t)
    for h, p in PUBLIC_TCP:
        add("public-internet", "tcp", h, p)
    add("public-internet", "udp", "8.8.8.8", 53, payload=DNS_QUERY)
    for name in PUBLIC_DNS:
        add("public-dns", "dns", name)
    control += [t for t in agent_t if t["label"].startswith("public")]
    yield {"agent": agent_t + agent_only, "control": control, "decoys": dec, "services": services, "addrs": addrs}
    dec.close()
    if srv is not None:
        srv.should_exit = True
    if cat is not None:
        cat.shutdown()


# ====================================================================== docker 与运行

def _docker_unavailable() -> str | None:
    if shutil.which(DOCKER) is None:
        return "docker CLI not found"
    try:
        r = subprocess.run([DOCKER, "image", "inspect", "--format", "{{.Id}}", IMAGE], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"docker unavailable: {e}"
    return None if r.returncode == 0 else f"docker image {IMAGE} unavailable: {r.stderr.strip()[-200:]}"


_NO_DOCKER = _docker_unavailable()
needs_docker = pytest.mark.skipif(_NO_DOCKER is not None, reason=f"[platform] {_NO_DOCKER} (formal run on dmax)")


def _image_id() -> str:
    r = subprocess.run([DOCKER, "image", "inspect", "--format", "{{.Id}}", IMAGE], capture_output=True, text=True, check=False)
    return r.stdout.strip()


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _open3d_error() -> str | None:
    try:
        import open3d  # noqa: F401
    except (ImportError, OSError) as e:
        return f"{type(e).__name__}: {e}"
    return None


_O3D = _open3d_error()
if _O3D is None:
    synth_event = _load("d3_env_conftest_for_isolation", ROOT / "tests" / "d3" / "env" / "conftest.py").synth_event
else:
    @pytest.fixture(scope="module")
    def synth_event():
        pytest.skip(f"[dep] open3d unavailable (Env synthetic world): {_O3D}")


def _ranked(case: str) -> tuple[int, str, str]:
    """→ (63 位种子, 提交 id, 种子承诺值)：按用例名由带密钥 BLAKE2b 派生（与 CSPRNG 同分布，可复现）。"""
    d = hashlib.blake2b(case.encode(), key=b"d3-redteam-a08-iso", digest_size=32).digest()
    seed = int.from_bytes(d[:8], "little") >> 1
    return seed, f"sub-{d[8:14].hex()}", "hmac-sha256:" + hashlib.sha256(d[14:] + seed.to_bytes(8, "little")).hexdigest()


def _run_root(tmp_path: Path) -> Path:
    """运行数据根（短路径，缺省配置下套接字路径在运行目录内，须 ≤ 100 字节）：在容器中驱动兄弟容器时须为宿主可见路径
    （`AWR_A08_RUN_ROOT`，经 AWR_HOST_PATH_MAP 换算），缺省为 /tmp 下的新目录。"""
    base = Path(os.environ.get("AWR_A08_RUN_ROOT") or tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="a08-", dir=base))


def _episode(synth_event, data_root: Path, case: str, image_cmd: list[str], *, sock_root: str | None):
    """排名配置 L1 回合（2 机 × 8 s，运动学开发档）经 `record_container_episode`（生产启动器 + A06 录制）在真实镜像中运行；
    `sock_root` 为 None 时不设 AWR_AGENT_SOCK_ROOT（启动器缺省配置）。→ (RedteamRun, run_dir, 标识表)。"""
    from awr.agentsdk.env.spec import EnvSpec, FleetEntry
    from awr.bench.redteam import ProbedEnv, TruthSet, record_container_episode

    w, ev = synth_event
    seed, sid, commit = _ranked(case)
    rid = f"{sid}-r0"
    ev_dir_name = "synth-handoff"
    run_dir = data_root / "dataset" / ev_dir_name / "runs" / rid                       # subworker 的数据集布局
    fleet = (FleetEntry("uav-01", (-10.0, 50.0, 0.0)), FleetEntry("uav-02", (10.0, 50.0, 0.0)))
    spec = EnvSpec(rid, seed, w, ev, fleet, obs_level="L1", physics="kinematic", duration_s=8.0, out_dir=run_dir, ranked=True,
                   seed_commit=commit)
    env = ProbedEnv(spec)
    old = os.environ.get("AWR_AGENT_SOCK_ROOT")
    try:
        if sock_root is None:
            os.environ.pop("AWR_AGENT_SOCK_ROOT", None)
        else:
            os.environ["AWR_AGENT_SOCK_ROOT"] = sock_root
        run = record_container_episode(env, run_dir, IMAGE, cmd=image_cmd, cpus=2.0, mem_gb=8.0, hello_timeout_s=120.0)
    finally:
        if old is None:
            os.environ.pop("AWR_AGENT_SOCK_ROOT", None)
        else:
            os.environ["AWR_AGENT_SOCK_ROOT"] = old
    world_id = json.loads((Path(w) / "world.json").read_text(encoding="utf-8")).get("id", Path(w).name)
    truth = TruthSet.from_env(env)
    idents = identifiers(truth, world_id=world_id, extra={sid: "submission_id", rid: "run_id", ev_dir_name: "event_dir", commit: "seed_commit",
                                                          commit.split(":", 1)[1]: "seed_commit", str(seed): "seed"})
    return run, run_dir, idents


def _agent_docs(run_dir: Path, uavs) -> dict[str, dict]:
    out = {}
    for u in uavs:
        log = run_dir / "agents" / u / "container.log"
        assert log.is_file(), f"{u}: container.log missing"
        doc = parse_probe(log.read_text(encoding="utf-8", errors="replace"))
        assert doc is not None, f"{u}: probe output missing from container.log"
        out[u] = doc
    return out


def _control(code: str) -> dict:
    """正向对照：同一镜像与探测脚本，`--network host`（其余加固参数相同），前台运行并读取 stdout。"""
    name = f"d3-a08-ctl-{os.getpid()}"                                           # dmax 约定：d3-<任务 id>-*
    subprocess.run([DOCKER, "rm", "-f", name], capture_output=True, check=False)
    r = subprocess.run([DOCKER, "run", "--rm", "--name", name, "--network", "host", "--read-only", "--cap-drop", "ALL", "--security-opt",
                        "no-new-privileges", "--user", "65534:65534", "--cpus", "1", "--memory", "1g", "--pids-limit", "64", IMAGE,
                        "python", "-c", code], capture_output=True, text=True, timeout=600, check=False)
    doc = parse_probe(r.stdout)
    assert doc is not None, f"control probe failed: rc={r.returncode} {r.stderr[-500:]}"
    return doc


_REPORT: dict = {}


def _write_report() -> None:
    if OUT and _REPORT:
        p = Path(OUT)
        p.mkdir(parents=True, exist_ok=True)
        (p / "isolation_report.json").write_text(json.dumps(_REPORT, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def _summ(doc: dict) -> list[dict]:
    return [{k: r.get(k) for k in ("label", "kind", "host", "port", "ok", "err", "status", "reply")} for r in doc.get("results") or []]


# ====================================================================== 不需要 docker 的用例


def test_scan_detects_identifiers_in_metadata() -> None:
    """检出逻辑的正向与反向对照：标识出现在挂载源路径、环境变量、主机名、cmdline 时检出；短标识只按分词；中性取值不检出。"""
    idents = {"sub-4f1a2b3c4d5e-r0": "run_id", "sub-4f1a2b3c4d5e": "submission_id", "handoff-07": "event_id", "test": "split",
              "dev": "split", "synth-glb": "world_id", "1234567890123456789": "seed"}
    clean = {"hostname": "3f2a1b0c9d8e", "etc_hostname": "3f2a1b0c9d8e\n", "nodename": "3f2a1b0c9d8e",
             "env": {"PATH": "/usr/local/bin:/usr/bin", "DRONE4D_AGENT_SOCK": "/run/drone4d/agent.sock", "HOSTNAME": "3f2a1b0c9d8e"},
             "environ1": ["PATH=/usr/local/bin"], "cmdline1": ["python", "-c", "code with test and sub-4f1a2b3c4d5e-r0", "--serve"],
             "mountinfo": "812 790 0:52 /anet/d3dev/run/as/9a8b7c6d5e-uav-01 /run/drone4d rw,relatime - xfs /dev/sdb1 rw\n"
                          "813 790 0:21 / /dev rw - tmpfs tmpfs rw\n",
             "cgroup": "0::/\n", "hosts": "127.0.0.1\tlocalhost\n", "resolv": "", "cwd": "/tmp", "mounts": {"/run/drone4d": ["agent.sock"]}}
    assert scan_metadata(clean, idents) == {}                                           # /dev 不是划分名 dev；-c 代码文本不计入
    leaky = json.loads(json.dumps(clean))
    leaky["mountinfo"] += "814 790 0:52 /d3/dataset/handoff-07/runs/sub-4f1a2b3c4d5e-r0/agent_sock/uav-01 /run/drone4d rw - xfs x rw\n"
    leaky["env"]["RUN"] = "split=test"
    leaky["hostname"] = "synth-glb-host"
    leaky["cmdline1"] = ["python", "agent.py", "--seed", "1234567890123456789"]
    hits = scan_metadata(leaky, idents)
    assert {c for c, _ in hits["mountinfo"]} == {"event_id", "run_id", "submission_id"}
    assert ("split", "token") in hits["env"] and ("world_id", "substring") in hits["hostname"] and ("seed", "substring") in hits["cmdline1"]


def test_isolation_problems_logic() -> None:
    ok = {"results": [{"label": "x", "kind": "tcp", "host": "10.0.0.1", "port": 22, "ok": False, "err": "ENETUNREACH"}], "ifaces": ["lo"],
          "route": "Iface\tDestination\n", "route6": "00000000000000000000000000000001 80 0000 00 00000000 00000001 00000000 lo\n",
          "engine_socks": {"/var/run/docker.sock": False}, "uid": 65534, "status": {"CapEff": "0000000000000000", "NoNewPrivs": "1"}}
    assert isolation_problems(ok) == []
    bad = json.loads(json.dumps(ok))
    bad["results"].append({"label": "bench-api", "kind": "http", "host": "127.0.0.1", "port": 18650, "ok": True})
    bad["ifaces"] = ["eth0", "lo"]
    bad["route"] += "eth0\t00000000\t0100A8C0\n"
    bad["engine_socks"]["/var/run/docker.sock"] = True
    bad["status"]["CapEff"] = "00000000a80425fb"
    assert len(isolation_problems(bad)) == 5


def test_probe_script_runs_on_host(tmp_path: Path) -> None:
    """探测脚本自检（本机、宿主网络）：输出可解析，诱饵 TCP、UDP 与抽象 Unix 套接字可连上并被计数，不存在的目标失败。"""
    dec = Decoys()
    try:
        tp, up = dec.tcp("127.0.0.1"), dec.udp("127.0.0.1")
        un = dec.unix_abstract("d3-a08-self-" + os.urandom(4).hex())
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        dead_port = dead.getsockname()[1]
        dead.close()
        ts = [{"label": "decoy", "kind": "tcp", "host": "127.0.0.1", "port": tp}, {"label": "udp", "kind": "udp", "host": "127.0.0.1", "port": up},
              {"label": "unix", "kind": "unix", "host": un}, {"label": "closed", "kind": "tcp", "host": "127.0.0.1", "port": dead_port},
              {"label": "nosock", "kind": "unix", "host": str(tmp_path / "none.sock")},
              {"label": "udp-closed", "kind": "udp", "host": "127.0.0.1", "port": dead_port}]
        r = subprocess.run([sys.executable, "-I", "-c", probe_code(ts)], capture_output=True, text=True, timeout=60, check=False)
        doc = parse_probe(r.stdout)
        assert doc is not None, r.stderr[-500:]
        res = {x["label"]: x for x in doc["results"]}
        assert res["decoy"]["ok"] and res["udp"]["ok"] and res["unix"]["ok"]
        assert not res["closed"]["ok"] and res["closed"]["err"] == "ECONNREFUSED" and not res["nosock"]["ok"]
        assert res["udp"]["reply"] and not res["udp-closed"]["ok"]                     # UDP 只以收到诱饵应答判可达
        t0 = time.monotonic()
        while dec.total() < 3 and time.monotonic() - t0 < 5:
            time.sleep(0.05)
        assert dec.total() == 3, dec.counts
        assert doc["mountinfo"] and doc["env"] and isinstance(doc["cmdline1"], list)
    finally:
        dec.close()


def test_launcher_argv_isolation_flags(tmp_path: Path) -> None:
    """生产启动器参数表（静态）：`--network none`、`--cap-drop ALL`、no-new-privileges、只读根、非 root、无 GPU 与设备、无特权、
    只有套接字目录一处挂载且不挂 docker 套接字、`-e` 只有 DRONE4D_*；运行标识只出现在宿主侧容器名与标签中。"""
    run_dir = tmp_path / "dataset" / "handoff-07" / "runs" / "sub-4f1a2b3c4d5e-r0"
    ac = AgentContainers(run_dir, "sub-4f1a2b3c4d5e-r0", ["uav-01"], IMAGE, cpus=2.0, mem_gb=8.0)
    argv = ac.argv("uav-01", Path("/run/d3/agents/0123456789abcdef0123456789abcdef/sock"))
    lc = parse_docker_run(argv, "agent", component="agent", run="sub-4f1a2b3c4d5e-r0")
    assert [x for x in check_launch(lc) if x.level == "error"] == []
    a = " ".join(argv)
    for flag in ("--network none", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges", "--user 65534:65534",
                 "--pids-limit 256", "--cpus 2", "--memory 8g"):
        assert flag in a, flag
    assert not lc.gpus and not lc.devices and lc.runtime is None and "--privileged" not in argv and "--network host" not in a
    vols = [argv[i + 1] for i, x in enumerate(argv) if x in ("-v", "--volume")]
    assert vols == ["/run/d3/agents/0123456789abcdef0123456789abcdef/sock:/run/drone4d"]
    assert not any("docker.sock" in v or "containerd" in v for v in vols)
    assert set(lc.env) <= {"DRONE4D_AGENT_SOCK", "DRONE4D_SOCK", "DRONE4D_PROTOCOL"}
    host_only = [x for x in (lc.name, *(f"{k}={v}" for k, v in lc.labels.items())) if "sub-4f1a2b3c4d5e" in str(x)]
    assert host_only                                                                     # 容器名与标签只在宿主可见（M26 §8.1）
    inside = [x for i, x in enumerate(argv) if x not in ("--name", "--label") and (i == 0 or argv[i - 1] not in ("--name", "--label"))]
    assert not [x for x in inside if "sub-4f1a2b3c4d5e" in x]


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=KNOWN_FINDINGS["fr072.neutral_metadata"])
def test_launcher_metadata_matches_m26_spec(tmp_path: Path, monkeypatch) -> None:
    """严格 xfail（M26-FR-072、§8.1）：`--hostname agent`；挂载源为 128 位随机令牌目录（同一运行两次启动不同，不能由运行标识
    推算）；环境变量为 DRONE4D_SOCK 与 DRONE4D_PROTOCOL。"""
    monkeypatch.setenv("AWR_AGENT_SOCK_ROOT", str(tmp_path / "as"))
    run_dir = tmp_path / "dataset" / "handoff-07" / "runs" / "sub-4f1a2b3c4d5e-r0"
    srcs = []
    for _ in range(2):
        ac = AgentContainers(run_dir, "sub-4f1a2b3c4d5e-r0", ["uav-01"], IMAGE, docker="true")
        ac.start()
        argv = ac.argv("uav-01", ac.hosts["uav-01"].path.parent)
        srcs.append(argv[argv.index("-v") + 1].split(":", 1)[0])
        for h in ac.hosts.values():
            h.lsock.close()
        a = " ".join(argv)
        assert "--hostname agent" in a
        assert "DRONE4D_SOCK=/run/drone4d/agent.sock" in a and "DRONE4D_PROTOCOL=drone4d.bench.wire.v1" in a
    tok = [re.search(r"([0-9a-f]{32})", s) for s in srcs]
    assert all(tok) and srcs[0] != srcs[1]
    digest = hashlib.sha256(f"{run_dir.resolve()}|sub-4f1a2b3c4d5e-r0".encode()).hexdigest()
    assert not any(digest[:10] in s for s in srcs)


# ====================================================================== dmax：真实镜像


@pytest.fixture(scope="module")
def prod_run(synth_event, targets, tmp_path_factory):
    """生产配置（AWR_AGENT_SOCK_ROOT 短根目录）下的探测回合：→ {run, run_dir, idents, docs, control, decoy_hits}。"""
    if _NO_DOCKER is not None:
        pytest.skip(f"[platform] {_NO_DOCKER} (formal run on dmax)")
    tmp = tmp_path_factory.mktemp("a08")
    data_root = _run_root(tmp)
    sock_root = os.environ.get("AWR_A08_SOCK_ROOT") or str(data_root / "as")
    dec: Decoys = targets["decoys"]
    dec.reset()
    code = probe_code(targets["agent"])
    t0 = time.time()
    run, run_dir, idents = _episode(synth_event, data_root, "isolation-prod", ["python", "-c", code, "--serve"], sock_root=sock_root)
    time.sleep(0.5)
    hits = dict(dec.counts)
    docs = _agent_docs(run_dir, run.env.uavs)
    dec.reset()
    ctl = _control(probe_code(targets["control"]))
    time.sleep(0.5)
    ctl_hits = dict(dec.counts)
    _REPORT.update({"schema": "drone4d.bench.isolation_report.v1", "image": IMAGE, "image_id": _image_id(),
                    "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0)), "services": targets["services"],
                    "host_addresses": targets["addrs"], "agent": {u: _summ(d) for u, d in docs.items()}, "control": _summ(ctl),
                    "decoy_hits_during_agent": sum(hits.values()), "decoy_hits_during_control": sum(ctl_hits.values()),
                    "n_targets": {"agent": len(targets["agent"]), "control": len(targets["control"])}})
    _write_report()
    return {"run": run, "run_dir": run_dir, "idents": idents, "docs": docs, "control": ctl, "decoy_hits": hits, "ctl_hits": ctl_hits}


@needs_docker
def test_agent_container_network_isolated(prod_run, targets) -> None:
    """D3-AC-16、L-18、A11：探测 Agent 对 dmax 本机全部地址、内网服务端口（bench-api、catalog、沙盒 API、lab-gateway、rerun-proxy）、
    公网地址与域名、UDP、抽象 Unix 与引擎套接字的连接全部失败；网卡只有 lo、无路由；诱饵在探测期间收到 0 个连接与数据报。"""
    labels = {t["label"] for t in targets["agent"]}
    assert {"bench-api", "sandbox-api", "lab-gateway", "public-internet", "public-dns", "docker-engine", "dmax-abstract-unix-decoy"} <= labels
    assert "catalog-dist" in labels or "rerun-catalog-default" in labels
    assert any(lb.startswith(("dmax-zerotier", "dmax-lan")) for lb in labels)
    for u, doc in prod_run["docs"].items():
        assert doc["uid"] == 65534, u
        assert len(doc["results"]) == len(targets["agent"])
        assert isolation_problems(doc) == [], (u, isolation_problems(doc))
        dns = [r for r in doc["results"] if r["kind"] == "dns"]
        assert dns and all(r["err"].startswith("gaierror") for r in dns)
    assert sum(prod_run["decoy_hits"].values()) == 0, {k: v for k, v in prod_run["decoy_hits"].items() if v}


@needs_docker
def test_control_container_reaches_targets(prod_run, targets) -> None:
    """正向对照：`--network host` 的对照容器能连上本机诱饵、抽象 Unix 诱饵、真实 bench-api（HTTP 401 AUTH_REQUIRED）与真实 catalog，
    诱饵计数 > 0（目标有效，失败来自隔离而非探测脚本或目标表）。"""
    res = prod_run["control"]["results"]
    ok = {(r["label"], r["host"], r.get("port")) for r in res if r["ok"]}
    decoys = [r for r in res if r["label"].endswith("-decoy") and r["kind"] == "tcp"]
    assert decoys and all(r["ok"] for r in decoys), [r for r in decoys if not r["ok"]]
    assert any(r["label"] == "dmax-abstract-unix-decoy" and r["ok"] for r in res)
    assert any(lb == "lab-gateway" for lb, _h, _p in ok) and any(lb == "sandbox-api" for lb, _h, _p in ok)
    if "bench-api" in targets["services"]:
        b = [r for r in res if r["label"] == "bench-api"]
        assert b and b[0]["ok"] and " 401" in b[0]["status"], b
    if "catalog-dist" in targets["services"]:
        assert any(lb == "catalog-dist" for lb, _h, _p in ok)
    assert sum(prod_run["ctl_hits"].values()) > 0
    assert prod_run["control"]["ifaces"] != ["lo"]


@needs_docker
def test_agent_container_metadata_has_no_run_identifiers(prod_run) -> None:
    """D3-AC-16、L-20、F15：主机名、环境变量、挂载信息（宿主挂载源路径）、/proc/1/cmdline、cgroup 与 /etc/hosts 中运行标识 0；
    挂载目录内只有 agent.sock，/tmp 为空 tmpfs；不挂载 docker 套接字；标识表非空且覆盖 run_id、事件、世界、划分、种子、哈希。"""
    idents = prod_run["idents"]
    assert {"run_id", "event_id", "world_id", "split", "seed", "hash", "submission_id", "seed_commit"} <= set(idents.values())
    uavs = list(prod_run["docs"])
    report = {}
    for u, doc in prod_run["docs"].items():
        hits = scan_metadata(doc, idents, ignore=tuple(uavs))
        report[u] = hits
        assert hits == {}, (u, hits)
        assert doc["mounts"] == {"/run/drone4d": ["agent.sock"], "/tmp": []}, doc["mounts"]           # /tmp 为空 tmpfs
        tmp_fs = [x.split(" - ", 1)[1].split(" ")[0] for x in doc["mountinfo"].splitlines() if x.split(" ")[4:5] == ["/tmp"]]
        assert tmp_fs == ["tmpfs"], tmp_fs
        assert not any(doc["engine_socks"].values())
        assert doc["env"].get("DRONE4D_AGENT_SOCK") == "/run/drone4d/agent.sock"
    _REPORT["metadata_hits_prod"] = report
    _write_report()


@needs_docker
def test_container_episode_leak_audit(prod_run) -> None:
    """同一回合（真实镜像、生产启动器）下发的全部帧经 A06 R1/R2 检索：除 A06 已登记的已知缺陷外命中 0；CLOSE 正常、退出码 0。"""
    from awr.bench.redteam import leak_report

    sk = _load("d3_leak_search_for_isolation", ROOT / "tests" / "d3" / "leak_redteam" / "test_search.py")
    run = prod_run["run"]
    rep = leak_report(run, config={"path": "container", "image": IMAGE})
    unexpected = [k for k in rep["failed"] if k not in sk.KNOWN_FINDINGS]
    assert unexpected == [], {k: rep["verdict"][k] for k in unexpected}
    assert sum(rep["config"]["frames"].values()) > 0 and run.capture.summary()["schema_violations"] == 0
    assert all(s.get("exit_code") == 0 for s in run.stats.values()), run.stats
    _REPORT["leak_audit"] = {"failed": rep["failed"], "frames": rep["config"]["frames"], "audit": rep["audit"], "known": sorted(sk.KNOWN_FINDINGS)}
    _write_report()


@needs_docker
@pytest.mark.xfail(strict=True, raises=AssertionError, reason=KNOWN_FINDINGS["launcher.default_sock_dir"])
def test_default_launcher_mount_source_has_no_run_identifiers(synth_event, tmp_path) -> None:
    """严格 xfail：不设 AWR_AGENT_SOCK_ROOT 时（启动器缺省），套接字目录在 `<data_root>/dataset/<事件>/runs/<run_id>/agent_sock`，
    容器内 /proc/self/mountinfo 显示该宿主路径，事件目录名与 run_id 被检出。修复后（恒用中性暂存目录）本用例 XPASS 失败提示移除。"""
    data_root = _run_root(tmp_path)                                                       # 短路径：套接字路径 ≤ 100 字节
    run, run_dir, idents = _episode(synth_event, data_root, "isolation-default", ["python", "-c", probe_code([]), "--serve"], sock_root=None)
    docs = _agent_docs(run_dir, run.env.uavs)
    hits = {u: scan_metadata(d, idents, ignore=tuple(docs)) for u, d in docs.items()}
    _REPORT["metadata_hits_default_launcher"] = hits
    _write_report()
    assert all(h == {} for h in hits.values()), hits


# ====================================================================== dmax：正式矩阵（显式开启）

FORMAL = os.environ.get("AWR_A08_FORMAL") == "1"
FORMAL_BASELINE = os.environ.get("AWR_A08_FORMAL_BASELINE", "track_share")
FORMAL_LEVELS = ("L1", "L2", "L2-depth")
FORMAL_NOISES = ("realistic", "harsh")
FORMAL_REPS = (0, 1)


def formal_durations(text: str | None = None) -> dict[str, float]:
    """`AWR_A08_FORMAL_DUR`（如 `L1=360,L2=360,L2-depth=60`）→ 各观测档的回合时长；缺省全部 360 s（A06 后续 2 的正式规模）。"""
    out = dict.fromkeys(FORMAL_LEVELS, 360.0)
    for part in filter(None, (text if text is not None else os.environ.get("AWR_A08_FORMAL_DUR", "")).split(",")):
        k, _, v = part.partition("=")
        if k.strip() not in out:
            raise ValueError(f"AWR_A08_FORMAL_DUR: unknown level {k!r}")
        out[k.strip()] = float(v)
    return out


# 正式规模下的孤立偶然吻合（results-B03/A08.md §4）：带噪导航解与 GNSS 位置（GM 误差约 1 m）与本机真值位置、各传感器外参原点
# 在 1 mm 容差内两个分量同时吻合的概率每样本约 4e-7，±1 s 窗口 × 分量对 × 位置类真值量 × 3 机 × 3600 帧下每回合期望约 1 次；
# |R|、|q| 元素与小速度值同理。L2 的 nav 与 gnss0 位置取同一误差、逐帧相同，一次吻合在两个字段各计一次。泄漏逐帧重复，偶然吻合
# 只在个别帧出现：限于带噪导航与 GNSS 字段、每个字段每帧至多一次、每回合命中帧合计 ≤ CHANCE_MAX 时判为偶然，逐条列入摘要。
# 统计口径的正式判据（对照标称噪声的 Poisson 检验，同 r1.gimbal）由 redteam 检索实现（A06 后续）。
CHANCE_GROUPS = {"r1.own_position": "own_position:", "r1.extrinsics": "extrinsics:"}
CHANCE_FIELDS = ("obs/nav/pos_enu_m", "obs/nav/vel_enu_mps", "obs/sensors/gnss0/*/pos_enu_m", "obs/sensors/gnss0/*/vel_enu_mps")
CHANCE_MAX = 6


def formal_verdict(rep: dict, known) -> tuple[list[str], list[dict]]:
    """leak_audit_report → (未解释的失败判据, 判为孤立偶然吻合的命中清单)。已知缺陷（`known`）不计入失败。"""
    vec = rep["r1"]["vector"]
    unexplained, chance = [], []
    for k in rep["failed"]:
        if k in known:
            continue
        pre = CHANCE_GROUPS.get(k)
        cls = [c for c in vec["by_class"] if pre and c.startswith(pre)]
        ok = bool(cls) and all(set(vec["paths"][c]) <= set(CHANCE_FIELDS) and max(vec["paths"][c].values()) <= vec["frames"][c] for c in cls)
        if ok:
            chance += [{"criterion": k, "class": c, "hits": vec["by_class"][c], "frames": vec["frames"][c], "paths": vec["paths"][c]} for c in cls]
        else:
            unexplained.append(k)
    if sum(x["frames"] for x in chance) > CHANCE_MAX:
        unexplained += sorted({x["criterion"] for x in chance})
    return sorted(set(unexplained)), chance


def test_formal_verdict_isolated_chance_only() -> None:
    """偶然吻合判定：导航字段上不重复的孤立命中判偶然；重复命中、非导航字段、超过上限或其他判据仍判失败；已知缺陷不计。"""
    def rep(by_class, frames, paths, failed):
        return {"failed": failed, "r1": {"vector": {"by_class": by_class, "frames": frames, "paths": paths}}}

    iso = rep({"extrinsics:extrinsics/eo/R": 1, "extrinsics:extrinsics/eo/origin": 1}, {"extrinsics:extrinsics/eo/R": 1, "extrinsics:extrinsics/eo/origin": 1},
              {"extrinsics:extrinsics/eo/R": {"obs/nav/vel_enu_mps": 1}, "extrinsics:extrinsics/eo/origin": {"obs/nav/pos_enu_m": 1}},
              ["r1.extrinsics", "r1.own_attitude"])
    bad, ch = formal_verdict(iso, {"r1.own_attitude"})
    assert bad == [] and len(ch) == 2
    dup = rep({"own_position:own_pose/position": 2}, {"own_position:own_pose/position": 1},
              {"own_position:own_pose/position": {"obs/nav/pos_enu_m": 1, "obs/sensors/gnss0/*/pos_enu_m": 1}}, ["r1.own_position"])
    assert formal_verdict(dup, ())[0] == []                                          # nav 与 gnss0 同值：一次吻合计两个字段
    rep_ = rep({"own_position:own_pose/position": 40}, {"own_position:own_pose/position": 40},
               {"own_position:own_pose/position": {"obs/nav/pos_enu_m": 40}}, ["r1.own_position"])
    assert formal_verdict(rep_, ())[0] == ["r1.own_position"]                       # 逐帧重复 → 超上限
    rep_ = rep({"own_position:own_pose/position": 2}, {"own_position:own_pose/position": 1},
               {"own_position:own_pose/position": {"obs/nav/pos_enu_m": 2}}, ["r1.own_position"])
    assert formal_verdict(rep_, ())[0] == ["r1.own_position"]                       # 同一字段同一帧重复
    rep_ = rep({"extrinsics:extrinsics/eo/origin": 1}, {"extrinsics:extrinsics/eo/origin": 1},
               {"extrinsics:extrinsics/eo/origin": {"obs/sensors/eo/mount_nominal/t_bs_m": 1}}, ["r1.extrinsics"])
    assert formal_verdict(rep_, ())[0] == ["r1.extrinsics"]                         # 非导航字段
    assert formal_verdict(rep({}, {}, {}, ["r2.own_position", "r1.strings"]), ())[0] == ["r1.strings", "r2.own_position"]


def _formal_fleet(level: str):
    from awr.agentsdk.env.spec import FleetEntry

    air = level != "L1"
    # 起点取非整米值：运动学档下未受指令的分量逐 tick 恒等于起点，整米起点（如 (0, 40, 30)）的毫米定点分量对与 LiDAR
    # offset_time_ns 的 5 µs 公开时序网格偶然逐字节相同（R2 mm_i32 假阳性，见 results-B03/A08.md）；起点本身是公开先验
    pts = ((-10.437, 50.861, 30.613), (10.291, 50.374, 30.282), (0.553, 40.719, 30.947))
    return tuple(FleetEntry(f"uav-0{i + 1}", (x, y, z if air else 0.0), 0.0, airborne=air) for i, (x, y, z) in enumerate(pts))


def test_formal_durations_parse() -> None:
    assert formal_durations("") == {"L1": 360.0, "L2": 360.0, "L2-depth": 360.0}
    assert formal_durations("L2-depth=60, L2=120") == {"L1": 360.0, "L2": 120.0, "L2-depth": 60.0}
    with pytest.raises(ValueError):
        formal_durations("L3=10")


@needs_docker
@pytest.mark.skipif(not FORMAL, reason="opt-in formal matrix: set AWR_A08_FORMAL=1 (dmax, results-B03/A08.md)")
@pytest.mark.parametrize("rep", FORMAL_REPS)
@pytest.mark.parametrize("noise", FORMAL_NOISES)
@pytest.mark.parametrize("level", FORMAL_LEVELS)
def test_formal_container_leak_matrix(synth_event, level: str, noise: str, rep: int) -> None:
    """A06 后续 2（D3-AC-16 正式口径的容器路径）：L1、L2、L2-depth × realistic、harsh × 3 机 × `AWR_A08_FORMAL_DUR` × 2 个种子，
    真实 d3-agent-py 镜像（缺省 track_share 基线）经生产启动器（AWR_AGENT_SOCK_ROOT 短根目录）运行排名配置，全部下发帧落盘录制
    （`keep=False`，长运行不占内存）后做 A06 R1/R2 检索并写 `leak_audit_report.json`；判据：obs.v1 校验 0 违规、未知字段 0、JPEG
    段与 LiDAR tag 违规 0、除 A06 已登记的已知缺陷与 `formal_verdict` 判定的孤立偶然吻合（逐条列入摘要）外命中 0、每机 CLOSE
    正常且退出码 0。L2 类以 CPU 替身渲染（dmax CPU 池；真实
    EGL 渲染的同一检索在独占卡运行中复核）。"""
    from awr.agentsdk.env.spec import EnvSpec
    from awr.bench.redteam import ProbedEnv, leak_report, read_capture, record_container_episode, write_report

    sk = _load("d3_leak_search_for_isolation", ROOT / "tests" / "d3" / "leak_redteam" / "test_search.py")
    if level != "L1":
        pytest.importorskip("PIL")
    w, ev = synth_event
    dur = formal_durations()[level]
    seed, sid, commit = _ranked(f"formal-{level}-{noise}-{rep}")
    rid = f"{sid}-r{rep}"
    data_root = _run_root(Path(tempfile.gettempdir()))
    run_dir = data_root / "dataset" / "synth-handoff" / "runs" / rid
    sock_root = data_root / "as"
    spec = EnvSpec(rid, seed, w, ev, _formal_fleet(level), obs_level=level, noise=noise, physics="kinematic", duration_s=dur, out_dir=run_dir,
                   ranked=True, seed_commit=commit)
    env = ProbedEnv(spec, l2=sk._l2_factory() if level != "L1" else None)
    cap_dir = data_root / "capture"
    old = os.environ.get("AWR_AGENT_SOCK_ROOT")
    os.environ["AWR_AGENT_SOCK_ROOT"] = str(sock_root)
    t0 = time.monotonic()
    try:
        run = record_container_episode(env, run_dir, IMAGE, capture_dir=cap_dir, keep=False, cpus=2.0, mem_gb=8.0, hello_timeout_s=120.0,
                                       cmd=["python", "-m", "drone4d.serve", "--baseline", FORMAL_BASELINE])
    finally:
        if old is None:
            os.environ.pop("AWR_AGENT_SOCK_ROOT", None)
        else:
            os.environ["AWR_AGENT_SOCK_ROOT"] = old
    wall_episode = time.monotonic() - t0
    rep_doc = leak_report(run, frames=read_capture(cap_dir), config={"path": "container", "image": IMAGE, "image_id": _image_id(),
                                                                       "baseline": FORMAL_BASELINE, "noise": noise, "obs_level": level,
                                                                       "duration_s": dur, "n_uav": len(env.possible_agents)})
    wall_audit = time.monotonic() - t0 - wall_episode
    path = write_report(rep_doc, run_dir)
    row = {"level": level, "noise": noise, "rep": rep, "duration_s": dur, "failed": rep_doc["failed"], "frames": rep_doc["config"]["frames"],
           "audit": rep_doc["audit"], "capture_bytes": run.capture.n_bytes, "exit_codes": {u: s.get("exit_code") for u, s in run.stats.items()},
           "wall_episode_s": round(wall_episode, 1), "wall_audit_s": round(wall_audit, 1)}
    row["unexplained"], row["chance_isolated"] = formal_verdict(rep_doc, sk.KNOWN_FINDINGS)
    if OUT:
        p = Path(OUT) / "formal"
        p.mkdir(parents=True, exist_ok=True)
        tag = f"{level}-{noise}-{rep}"
        (p / f"{tag}.leak_audit_report.json").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        (p / f"{tag}.summary.json").write_text(json.dumps(row, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    if os.environ.get("AWR_A08_KEEP_CAPTURE") != "1":                                   # 落盘录制只用于本次检索（L2-depth 每机数 GB）
        shutil.rmtree(cap_dir, ignore_errors=True)
    elif OUT:
        (Path(OUT) / "formal" / f"{level}-{noise}-{rep}.capture_dir").write_text(str(cap_dir) + "\n", encoding="utf-8")
    unexpected = row["unexplained"]
    assert unexpected == [], {k: rep_doc["verdict"][k] for k in unexpected}
    a = rep_doc["audit"]
    assert a["schema_violations"] == 0 and a["unknown_fields"] == 0 and a["jpeg_segment_violations"] == 0 and a["lidar_tag_violations"] == 0, a
    n_obs = rep_doc["config"]["frames"].get("obs", 0)
    assert n_obs >= 0.9 * len(env.possible_agents) * dur * 10 and a["schema_checked"] == run.capture.n_frames
    assert all(s.get("exit_code") == 0 and s.get("protocol_errors", 0) == 0 for s in run.stats.values()), run.stats


def test_known_findings_registered() -> None:
    """已知缺陷均有对应的严格 xfail 用例（修复后 XPASS 失败提示移除登记）。"""
    marks = {}
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            for m in getattr(fn, "pytestmark", []):
                if m.name == "xfail":
                    marks[name] = m.kwargs.get("reason")
    assert set(marks.values()) == set(KNOWN_FINDINGS.values())
