#!/usr/bin/env python3
"""tools/deploy/emax/d3-public/drill.py：公开样例发布线的本机 cgroup 模拟演练（D3-AC-21 上线前演练；ADR-194 (4)、(5) ②）。

封板以上线后的 emax 实测判定（ADR-194）；本演练只证明发布线与回滚流程可执行、阈值在模拟环境下成立，不计入封板。

模拟环境：
- D2 栈：`tools/bench/sandbox/_sbbench.Stack`（supervisor `sandbox` profile，`systemd-run --user --scope` 单元 CPUQuota 250%、
  MemoryHigh 1.8G、MemoryMax 2G，`taskset 0-3` 模拟 emax 的 4 核，单元内 `cgroup-exec.sh` 建立 core/ 与 sandbox/ 子组），负载为
  D2-AC-04 变体 A：N_max 个 synthcity 满载沙盒（各 10 架 + 30 识别物 + 接力，operator 客户端 REST 50% 配额）+ 共享展示 viewer；
- nginx：一次性 Ubuntu 24.04 容器（nginx 1.24，`--network host`，与 D2 栈同一 cpuset 0-3，不限 CPU，与 emax 上 nginx 不受 D2 单元
  配额约束相同），站点文件 = `labsite.render(D2 站点文件)` 经 `localize` 改为回环端口 + 自签证书（TLS、HTTP/2），location 与限速原样；
- 发布目录：labctl.py index → push（本机 rsync --link-dest）→ activate，两个版本（第 2 版只改切片），各含 4 个 48 MiB 视频；
- 客户端不与模拟的 emax 抢核：WS 探针独占 4 号核，本进程（operator、直连 viewer、采样）在 5-7 号核，下载进程在 6-7 号核。

步骤与判据：
1. verify.py 全部站点检查（bench404、lab、static、rate、conn_ip、conn_all、nmax）；
2. WS 推送时延：8 个 viewer 经 nginx（wss，2 个源地址，每地址 4 条）订阅共享展示；推送时延 = 收到 TIME 帧的单调时刻 −（gw_t0 +
   t_srv_ns），gw_t0 由最小往返的 pong（server_ns）估计（客户端与 api 同机，CLOCK_MONOTONIC 相同）。无下载（基线）与 16 路并发
   下载（16 个源地址，各自循环整段下载，每段前 8 MiB 不限、之后 2 MiB/s）交替 `--repeats` 轮，各 `--phase-s` 秒；
   判据：下载阶段 p95 相对基线 p95 的增幅 ≤ 10 %；
3. D2-AC-04：负载期间容量接口的 N_max 与启动时相同（不变）；变体 A 判据（RTF、单步 p99、api CPU、内存、OOM、重启）以本次时长
   计算（短时版，正式 30 min 复测见 capacity.py 与 H05）；
4. 回滚演练：labctl rollback（上一版本）、withdraw（恢复 D2 站点文件并 reload）、再上线（安装 /lab/ 站点文件并重新 activate），
   每步从命令开始到 verify 通过的耗时 ≤ 10 min。

用法：
    .venv/bin/python tools/deploy/emax/d3-public/drill.py [--out DIR] [--phase-s 45] [--repeats 3] [--sessions N] [--image IMG]
报告 `<out>/drill.json`（`awr.deploy.d3_public.drill.v1`）；退出码 0 通过、1 不通过、14 环境不满足（docker、镜像、世界、lab 构建）。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
for _p in (HERE, ROOT / "python", ROOT / "tools" / "bench" / "sandbox", ROOT / "tools" / "bench" / "ipc", ROOT / "tools" / "sandbox",
           ROOT / "tools" / "sdk", ROOT / "tests" / "e2e", ROOT / "tests" / "chaos"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import labctl  # noqa: E402
import labsite  # noqa: E402
import verify  # noqa: E402

SCHEMA = "awr.deploy.d3_public.drill.v1"
DOMAIN = labsite.DOMAIN
EXIT_OK, EXIT_FAIL, EXIT_ENV = 0, 1, 14
MIB = 1 << 20
WS_P95_GATE = 0.10                    # D3-AC-21：16 路并发下载期间 WS 推送时延 p95 增幅 ≤ 10 %
ROLLBACK_GATE_S = 600.0               # 回滚演练 ≤ 10 min
DL_SOURCES = "127.0.0.20-127.0.0.35"  # 16 路下载各自的源地址（各自的地址前缀）
WS_SOURCES = ("127.0.0.8", "127.0.0.9")
VERIFY_SOURCES = "127.0.0.40-127.0.0.50"
CLIENT_CPUS = "4-7"                   # 客户端核：4 号核只给 WS 探针，5-7 号核给本进程（operator、viewer、采样）与下载进程
PROBE_CPUS = "4"
MAIN_CPUS = "5-7"
DL_CPUS = "6-7"
VIDEO_MIB = 48
N_VIDEOS = 4
STAGGER_S = 0.125


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    v = sorted(xs)
    k = (len(v) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 3)


def parse_cpus(spec: str) -> set[int]:
    """taskset 写法的核列表（"5-7"、"0,2-3"）→ 核号集合。"""
    out: set[int] = set()
    for part in spec.split(","):
        lo, _, hi = part.strip().partition("-")
        out |= set(range(int(lo), int(hi or lo) + 1))
    return out


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------------------------------------------------
# 发布目录


def blob_file(p: Path, n: int, seed: str) -> None:
    """n 字节的确定内容（不可压缩，避免 gzip 与页缓存造成的偏差）。"""
    import hashlib

    p.parent.mkdir(parents=True, exist_ok=True)
    block = b"".join(hashlib.sha256(f"{seed}:{k}".encode()).digest() for k in range(4096))   # 128 KiB
    with open(p, "wb") as f:
        for i in range(n // len(block)):
            f.write(block[i % 7:] + block[: i % 7])
        f.write(block[: n % len(block)])


def build_release(work: Path, version: str, release: str, *, slice_seed: str) -> Path:
    v = work / "src" / version
    rid, eid = "run-abcdefghij", "crop-handoff-001"
    (v / "LICENSE").parent.mkdir(parents=True, exist_ok=True)
    (v / "LICENSE").write_text("CC-BY-4.0（演练用合成制品）\n", encoding="utf-8")
    (v / "NOTICE").write_text("OpenStreetMap contributors（ODbL-1.0）\n", encoding="utf-8")
    blob_file(v / f"events/{eid}/tape.bin", 256 * 1024, "tape")
    blob_file(v / f"runs/{rid}/slices/encounter_ab.rrd", 6 * MIB, slice_seed)
    blob_file(v / f"runs/{rid}/slices/basemap.rrd", 5 * MIB, "basemap")
    videos = []
    for k in range(N_VIDEOS):
        rel = f"runs/{rid}/video/uav-0{k + 1}_cam0.mp4"
        blob_file(v / rel, VIDEO_MIB * MIB, f"video-{k}")
        videos.append({"uav": f"uav-0{k + 1}", "cam": "cam0", "kind": "rgb", "path": rel})
    draft = {"license": {"files": ["LICENSE", "NOTICE"], "attribution": ["OpenStreetMap contributors（ODbL-1.0）"]},
             "worlds": [{"id": "oryahovo-crop1"}],
             "events": [{"id": eid, "version": 1, "world_id": "oryahovo-crop1", "tape": f"events/{eid}/tape.bin",
                         "runs": [{"run_id": rid, "policy": "track_share", "n_uav": 4, "obs_level": "L2", "videos": videos,
                                   "slices": [{"event_id": "encounter_ab", "t_from_s": 17.5, "t_to_s": 27.5,
                                               "rrd": f"runs/{rid}/slices/encounter_ab.rrd"}],
                                   "basemap": f"runs/{rid}/slices/basemap.rrd"}]}]}
    labctl.build_index(v, draft, lab_build=release)
    return v


def lab_dist_copy(work: Path, src: Path, release: str) -> Path:
    """真实 /lab/ 构建（make lab-build 的产物）复制一份并改写 build-info.json 的 release（两个构建名，用于构建回滚）。"""
    d = work / "src" / f"lab-{release}"
    shutil.copytree(src, d)
    info = json.loads((d / "build-info.json").read_text(encoding="utf-8"))
    info["release"] = release
    (d / "build-info.json").write_text(json.dumps(info, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return d


# ---------------------------------------------------------------------------------------------------------------------
# nginx 容器


class NginxBox:
    def __init__(self, work: Path, image: str, show_port: int, sandbox_port: int) -> None:
        self.work, self.image = work, image
        # 容器命名与资源参数按 D3 容器清单规则（tools/deploy/dmax/audit_manifests.py 登记本启动点，组件 test，CPU 池）
        self.name = f"d3-test-f08-drill-{os.getpid()}"
        self.port = free_port()
        self.show_port, self.sandbox_port = show_port, sandbox_port
        self.conf = work / "nginx" / "nginx.conf"
        self.site_file = work / "nginx" / "site.conf"
        self.d2_site = work / "nginx" / "d2-site.conf"
        self.cert = (work / "nginx" / "fullchain.pem", work / "nginx" / "privkey.pem")

    def localize(self, text: str) -> str:
        return labsite.localize(text, root=self.work / "srv", show_port=self.show_port, sandbox_port=self.sandbox_port,
                                listen=f"127.0.0.1:{self.port}", opt_root=self.work / "opt", micro_dir=self.work / "nginx" / "micro",
                                cert=self.cert, http2=True)

    def exec_cmd(self, *args: str) -> str:
        return " ".join(["docker", "exec", self.name, "nginx", *args, "-p", str(self.work / "nginx"), "-c", str(self.conf)])

    @property
    def test_cmd(self) -> str:
        return self.exec_cmd("-t", "-q")

    @property
    def reload_cmd(self) -> str:
        return self.exec_cmd("-s", "reload")

    def start(self) -> None:
        n = self.work / "nginx"
        (n / "logs").mkdir(parents=True, exist_ok=True)
        d2 = labsite.D2_SITE.read_text(encoding="utf-8")
        self.d2_site.write_text(self.localize(d2), encoding="utf-8")
        self.site_file.write_text(self.localize(labsite.render(d2)), encoding="utf-8")
        self.conf.write_text(f"""user root;
worker_processes 4;
pid {n}/nginx.pid;
error_log {n}/error.log warn;
events {{ worker_connections 4096; }}
http {{
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    sendfile on;
    tcp_nopush on;
    access_log off;
    client_body_temp_path {n}/body;
    proxy_temp_path {n}/proxy;
    fastcgi_temp_path {n}/fastcgi;
    uwsgi_temp_path {n}/uwsgi;
    scgi_temp_path {n}/scgi;
    include {self.site_file};
}}
""", encoding="utf-8")
        script = (f"set -e; command -v nginx >/dev/null || (apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
                  f"--no-install-recommends nginx openssl >/dev/null); "
                  f"openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:secp384r1 -nodes -days 2 -subj /CN={DOMAIN} "
                  f"-keyout {self.cert[1]} -out {self.cert[0]} >/dev/null 2>&1; "
                  f"nginx -v 2> {n}/nginx-version.txt; exec nginx -g 'daemon off;' -p {n} -c {self.conf}")
        subprocess.run(self.run_argv(script), check=True, capture_output=True, text=True)
        end = time.monotonic() + 180
        while time.monotonic() < end:
            with contextlib.suppress(OSError):
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return
            r = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", self.name], capture_output=True, text=True)
            if r.stdout.strip() != "true":
                logs = subprocess.run(["docker", "logs", self.name], capture_output=True, text=True)
                raise RuntimeError(f"nginx 容器退出：{logs.stdout[-800:]}{logs.stderr[-800:]}")
            time.sleep(0.3)
        raise TimeoutError("nginx 容器未在 180 s 内监听")

    def run_argv(self, script: str) -> list[str]:
        """nginx 容器：与 D2 栈同一 cpuset 0-3（--cpus 4 即整组 4 核，不另加配额：emax 上 nginx 不受 D2 单元的 CPUQuota 约束）。"""
        return ["docker", "run", "-d", "--rm", "--name", self.name, "--network", "host", "--cpuset-cpus", "0-3", "--cpus", "4",
                "--memory", "512m", "-v", f"{self.work}:{self.work}", self.image, "bash", "-c", script]

    def chown_argv(self) -> list[str]:
        """收尾：容器以 root 写出的临时目录与日志交回当前用户（一次性容器）。"""
        return ["docker", "run", "--rm", "--name", f"{self.name}-chown", "--network", "none", "--cpus", "1", "--memory", "128m",
                "-v", f"{self.work}:{self.work}", self.image, "chown", "-R", f"{os.getuid()}:{os.getgid()}", str(self.work / "nginx")]

    def version(self) -> str | None:
        p = self.work / "nginx" / "nginx-version.txt"
        return p.read_text(encoding="utf-8").strip() if p.exists() else None

    def stop(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)
        subprocess.run(self.chown_argv(), capture_output=True)


# ---------------------------------------------------------------------------------------------------------------------
# 子进程：WS 探针与下载（钉在客户端核上）


def _tls() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def _ws_viewer(k: int, src: str, port: int, stop_at: float, rec: dict) -> None:
    import capacity as CAP
    import websockets

    from awr.contracts import frame as F

    site = verify.Site(f"https://{DOMAIN}:{port}", connect="127.0.0.1", insecure=True)
    st, _, body = site.request("POST", "/api/auth/token", body=json.dumps({"role": "viewer"}).encode(),
                               headers={"Content-Type": "application/json"}, source=src)
    if st != 200:
        rec["errors"].append(f"viewer {k}: token {st}")
        return
    tok = json.loads(body)["token"]
    sock = socket.create_connection(("127.0.0.1", port), timeout=10, source_address=(src, 0))
    async with websockets.connect(f"wss://{DOMAIN}:{port}/api/rt", sock=sock, ssl=_tls(), server_hostname=DOMAIN,
                                  subprotocols=["awr.rt.v1", f"bearer.{tok}"], max_size=None, open_timeout=20,
                                  ping_interval=None) as ws:
        last_ack, t_ack, t_ping = 0, time.monotonic(), 0.0
        subscribed = False
        while time.monotonic() < stop_at:
            try:
                m = await asyncio.wait_for(ws.recv(), 2.0)
            except TimeoutError:
                continue
            now_ns = time.monotonic_ns()
            if isinstance(m, str):
                d = json.loads(m)
                op = d.get("op")
                if op == "serverInfo":
                    await ws.send(json.dumps({"op": "hello", "client": "d3-public-drill/1", "contracts": d.get("contracts"),
                                              "tier": "S", "deviceClass": "software"}))
                elif op == "advertise" and not subscribed:
                    subscribed = True
                    await ws.send(json.dumps({"op": "subscribe", "subs": CAP.browser_subs(CAP.VIEWER_PROFILES["browser-s"])}))
                elif op == "pong":
                    t = d.get("t")
                    if isinstance(t, (int, float)):
                        rec["pongs"].append((int(t), now_ns, int(d.get("server_ns") or 0)))
            else:
                if m[0] == F.OP_TIME:
                    tf = F.decode_time(m)
                    if F.time_advances(tf):
                        rec["times"].append((k, now_ns, tf.t_srv_ns))
                elif m[0] == F.OP_BATCH:
                    rec["batches"] += 1
                    seq = F.decode_batch_header(m).frame_seq
                    if seq > last_ack and (seq - last_ack >= 3 or time.monotonic() - t_ack >= 0.034):
                        await ws.send(json.dumps({"op": "ack", "frame": seq, "fps": 30.0}))
                        last_ack, t_ack = seq, time.monotonic()
            if time.monotonic() - t_ping >= 0.5:
                t_ping = time.monotonic()
                await ws.send(json.dumps({"op": "ping", "t": time.monotonic_ns(), "srttMs": 1.0}))


def run_wsprobe(port: int, seconds: float, out: Path, viewers: int) -> None:
    rec: dict[str, Any] = {"times": [], "pongs": [], "batches": 0, "errors": []}
    stop_at = time.monotonic() + seconds

    async def main() -> None:
        tasks = [_ws_viewer(k, WS_SOURCES[k % len(WS_SOURCES)], port, stop_at, rec) for k in range(viewers)]
        res = await asyncio.gather(*tasks, return_exceptions=True)
        rec["errors"] += [f"{type(r).__name__}: {r}" for r in res if isinstance(r, BaseException)]

    asyncio.run(main())
    out.write_text(json.dumps(rec), encoding="utf-8")


def run_download(port: int, paths: list[str], out: Path) -> None:
    """16 路并发：每个源地址一个线程，循环整段下载（每段前 8 MiB 不限，之后 2 MiB/s），SIGTERM 时写出统计。"""
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    srcs = verify.parse_ips(DL_SOURCES)
    stats = {"bytes": [0] * len(srcs), "requests": [0] * len(srcs), "status": {}, "errors": 0}
    t0 = time.monotonic()

    def worker(i: int, src: str) -> None:
        site = verify.Site(f"https://{DOMAIN}:{port}", connect="127.0.0.1", insecure=True, timeout=30)
        k = i
        stop.wait(i * STAGGER_S)                 # 16 个访客在 2 s 内陆续开始（同一时刻起跑的 16 段 8 MiB 突发不代表真实访问）
        while not stop.is_set():
            path = paths[k % len(paths)]
            k += 1
            try:
                c = site.conn(src)
                c.request("GET", path, headers={"Host": DOMAIN})
                r = c.getresponse()
                stats["status"][str(r.status)] = stats["status"].get(str(r.status), 0) + 1
                stats["requests"][i] += 1
                while not stop.is_set():
                    b = r.read(256 * 1024)
                    if not b:
                        break
                    stats["bytes"][i] += len(b)
                c.close()
            except Exception:
                stats["errors"] += 1
                stop.wait(0.5)

    ts = [threading.Thread(target=worker, args=(i, s), daemon=True) for i, s in enumerate(srcs)]
    for t in ts:
        t.start()
    while not stop.is_set():
        stop.wait(0.2)
    for t in ts:
        t.join(5)
    dt = time.monotonic() - t0
    out.write_text(json.dumps({**stats, "seconds": round(dt, 3), "clients": len(srcs),
                               "mib_s_total": round(sum(stats["bytes"]) / MIB / dt, 3),
                               "mib_s_per_client_max": round(max(stats["bytes"]) / MIB / dt, 3)}), encoding="utf-8")


def _spawn(sub: str, *args: str, cpus: str = CLIENT_CPUS) -> subprocess.Popen:
    cmd = ["taskset", "-c", cpus, sys.executable, str(Path(__file__).resolve()), sub, *args]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "python"), os.environ.get("PYTHONPATH", "")]))
    return subprocess.Popen(cmd, env=env)


# ---------------------------------------------------------------------------------------------------------------------
# WS 时延分析


def ws_latency(rec: dict, phases: list[dict]) -> dict:
    """TIME 帧推送时延（ms）按阶段切分：offset 取最小往返 pong 的估计（gw_t0 = 往返中点 − server_ns）。"""
    pongs = rec.get("pongs") or []
    if not pongs or not rec.get("times"):
        return {"ok": False, "error": "没有 TIME 帧或 pong", "errors": rec.get("errors")}
    c0, c1, srv = min(pongs, key=lambda p: p[1] - p[0])
    off = (c0 + c1) // 2 - srv
    rtt_min_ms = (c1 - c0) / 1e6
    out: dict[str, Any] = {"offset_rtt_ms": round(rtt_min_ms, 3), "phases": []}
    agg: dict[str, list[float]] = {"base": [], "load": []}
    rtt: dict[str, list[float]] = {"base": [], "load": []}
    for ph in phases:
        lat = [(r - off - t) / 1e6 for _, r, t in rec["times"] if ph["t0_ns"] <= r < ph["t1_ns"]]
        rt = [(b - a) / 1e6 for a, b, _ in pongs if ph["t0_ns"] <= b < ph["t1_ns"]]
        agg[ph["kind"]] += lat
        rtt[ph["kind"]] += rt
        out["phases"].append({"kind": ph["kind"], "n": len(lat), "p50_ms": pct(lat, 0.5), "p95_ms": pct(lat, 0.95), "p99_ms": pct(lat, 0.99),
                              "rtt_p95_ms": pct(rt, 0.95), "download": ph.get("download")})
    b95, l95 = pct(agg["base"], 0.95), pct(agg["load"], 0.95)
    inc = round(l95 / b95 - 1, 4) if b95 and l95 is not None else None
    # 每轮（相邻的基线与下载阶段）各自的增幅：只作稳健性参考（同机其他负载的波动会落在单轮上），判据仍为合并后的 p95
    rounds = []
    for ph_b, ph_l in zip(out["phases"][0::2], out["phases"][1::2], strict=False):
        if ph_b["kind"] == "base" and ph_l["kind"] == "load" and ph_b["p95_ms"] and ph_l["p95_ms"] is not None:
            rounds.append(round(ph_l["p95_ms"] / ph_b["p95_ms"] - 1, 4))
    out.update({"base": {"n": len(agg["base"]), "p50_ms": pct(agg["base"], 0.5), "p95_ms": b95, "p99_ms": pct(agg["base"], 0.99),
                         "rtt_p95_ms": pct(rtt["base"], 0.95)},
                "load": {"n": len(agg["load"]), "p50_ms": pct(agg["load"], 0.5), "p95_ms": l95, "p99_ms": pct(agg["load"], 0.99),
                         "rtt_p95_ms": pct(rtt["load"], 0.95)},
                "p95_increase": inc, "round_increase": rounds, "gate": WS_P95_GATE, "batches": rec.get("batches"), "errors": rec.get("errors")[:10],
                "ok": inc is not None and inc <= WS_P95_GATE and len(agg["base"]) > 100 and len(agg["load"]) > 100})
    return out


def d2_by_phase(samples: list[dict], phases: list[dict]) -> dict:
    """沙盒会话的 RTF 与单步 p99 按阶段切分（Sampler 的 t 与阶段边界同为单调时钟）：下载阶段相对基线有无回退（参考项）。

    变体 A 判据（judge_a）按整段时长判定，同机其他负载造成的 RTF 低谷会落在任何阶段；按阶段对比可以区分"下载造成的回退"与
    "主机本身不满足变体 A"。回退判定：下载阶段单步 p99 中位数 ≤ 基线 × 1.1，且 RTF 均值不低于基线 0.01 以上。"""
    import statistics

    rows: list[dict] = []
    agg: dict[str, dict[str, list[float]]] = {"base": {"rtf": [], "p99": []}, "load": {"rtf": [], "p99": []}}
    for ph in phases:
        rtf: list[float] = []
        p99: list[float] = []
        for smp in samples:
            t = smp.get("t")
            if t is None or not ph["t0_ns"] <= t * 1e9 < ph["t1_ns"]:
                continue
            for e in (smp.get("sessions") or {}).values():
                ring = e.get("ring") or {}
                if ring.get("rtf") is not None and ring.get("step_p99_us") is not None:
                    rtf.append(float(ring["rtf"]))
                    p99.append(float(ring["step_p99_us"]))
        agg[ph["kind"]]["rtf"] += rtf
        agg[ph["kind"]]["p99"] += p99
        rows.append({"kind": ph["kind"], "n": len(rtf), "rtf_mean": round(statistics.fmean(rtf), 4) if rtf else None,
                     "rtf_min": min(rtf) if rtf else None, "step_p99_us_median": statistics.median(p99) if p99 else None})
    out: dict[str, Any] = {"phases": rows}
    for k, v in agg.items():
        out[k] = {"rtf_mean": round(statistics.fmean(v["rtf"]), 4) if v["rtf"] else None,
                  "step_p99_us_median": statistics.median(v["p99"]) if v["p99"] else None}
    b, ld = out["base"], out["load"]
    out["no_regression"] = (None if None in (b["rtf_mean"], ld["rtf_mean"], b["step_p99_us_median"], ld["step_p99_us_median"]) else
                            ld["step_p99_us_median"] <= b["step_p99_us_median"] * 1.1 and ld["rtf_mean"] >= b["rtf_mean"] - 0.01)
    return out


# ---------------------------------------------------------------------------------------------------------------------
# 主流程


def env_problems(image: str, lab_src: Path) -> list[str]:
    probs = []
    if shutil.which("docker") is None:
        probs.append("需要 docker")
    elif subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode != 0:
        probs.append(f"本地没有镜像 {image}")
    if not (ROOT / "worlds" / "synthcity" / "world.json").exists():
        probs.append("worlds/synthcity 未构建")
    if not (lab_src / "index.html").is_file():
        probs.append(f"{lab_src} 不是 /lab/ 构建（先 make lab-build）")
    if os.cpu_count() and os.cpu_count() < 8:
        probs.append("需要 ≥ 8 核（0-3 模拟 emax，4-7 跑客户端）")
    return probs


def timed(fn, check) -> dict:
    t0 = time.monotonic()
    out = fn()
    rep = check()
    end = time.monotonic() + 30
    while not rep["ok"] and time.monotonic() < end:           # reload 后旧 worker 处理完已有连接才退出
        time.sleep(0.25)
        rep = check()
    dt = round(time.monotonic() - t0, 3)
    return {"seconds": dt, "ok": rep["ok"] and dt <= ROLLBACK_GATE_S, "verify": [{k: c[k] for k in ("name", "ok", "problems")}
                                                                               for c in rep["checks"]], "result": out}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="drill.py", description="公开样例发布线的本机 cgroup 模拟演练（D3-AC-21）")
    sub = ap.add_subparsers(dest="sub")
    w = sub.add_parser("_wsprobe")
    w.add_argument("--port", type=int, required=True)
    w.add_argument("--seconds", type=float, required=True)
    w.add_argument("--viewers", type=int, default=8)
    w.add_argument("--out", type=Path, required=True)
    d = sub.add_parser("_download")
    d.add_argument("--port", type=int, required=True)
    d.add_argument("--paths", required=True)
    d.add_argument("--out", type=Path, required=True)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--phase-s", type=float, default=45.0)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--warmup-s", type=float, default=20.0)
    ap.add_argument("--sessions", type=int, default=None, help="满载沙盒数（缺省 N_max；0 表示只有共享展示）")
    ap.add_argument("--viewers", type=int, default=16, help="直连 show-api 的共享展示 viewer（另有 8 个经 nginx 的探针）")
    ap.add_argument("--image", default=os.environ.get("NGINX_CHECK_IMAGE", "ubuntu:24.04"))
    ap.add_argument("--lab-dist", type=Path, default=ROOT / ".cache" / "deploy" / "emax" / "lab-dist")
    ap.add_argument("--no-lock", action="store_true")
    a = ap.parse_args(argv)
    if a.sub == "_wsprobe":
        run_wsprobe(a.port, a.seconds, a.out, a.viewers)
        return 0
    if a.sub == "_download":
        run_download(a.port, a.paths.split(","), a.out)
        return 0

    import _common as PERF
    import _sbbench as B
    import capacity as CAP

    probs = env_problems(a.image, a.lab_dist)
    if probs:
        print("drill: 环境不满足：" + "；".join(probs), file=sys.stderr)
        return EXIT_ENV
    lock = PERF.acquire_perf_lock(a.no_lock)
    # 本进程（operator、直连 viewer、采样线程）不得落在模拟 emax 的 0-3 号核上，也不与 WS 探针争核（探针收到帧的时刻即测量值）；
    # D2 栈由 Stack 以 taskset 0-3 启动，nginx 容器以 --cpuset-cpus 0-3 启动，不继承本进程的亲和性
    with contextlib.suppress(AttributeError, OSError):
        os.sched_setaffinity(0, parse_cpus(MAIN_CPUS))
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = a.out or ROOT / ".cache" / "deploy" / "emax" / "d3-public" / "drill" / ts
    out.mkdir(parents=True, exist_ok=True)
    work = Path(os.environ.get("TMPDIR", "/tmp")) / f"d3-public-drill-{ts}"
    work.mkdir(parents=True)
    rep: dict[str, Any] = {"schema": SCHEMA, "t_start": utc(), "ac": ["D3-AC-21", "D2-AC-04"], "formal": False,
                           "note": "上线前演练（ADR-194）：封板以上线后的 emax 实测判定", "host": PERF.host_info(), "git_sha": PERF.git_sha(),
                           "params": {"phase_s": a.phase_s, "repeats": a.repeats, "warmup_s": a.warmup_s, "viewers": a.viewers,
                                      "ws_probes": 8, "downloads": 16, "video_mib": VIDEO_MIB, "cpuset_emax": "0-3",
                                      "cpuset_clients": CLIENT_CPUS, "cpuset_probe": PROBE_CPUS, "cpuset_main": MAIN_CPUS,
                                      "cpuset_downloads": DL_CPUS, "cpu_quota": "250%", "memory_high": "1.8G", "memory_max": "2G"}}
    load = PERF.LoadSampler()
    stack = B.Stack(cgroup=True, cpu_quota_pct=250, ws_max_show=max(a.viewers + 8, 24), env={"AWR_ALLOWED_HOSTS": DOMAIN})
    box: NginxBox | None = None
    sessions: list[Any] = []
    ok = True
    try:
        # 1. 发布目录：两个版本、两个构建
        root = work / "srv"
        rels = ("20261019-060000-aaaaaaaaaa", "20261020-060000-bbbbbbbbbb")
        pub = []
        for ver, rel, seed in (("2026.10.19-1", rels[0], "s1"), ("2026.10.20-1", rels[1], "s2")):
            v = build_release(work, ver, rel, slice_seed=seed)
            dist = lab_dist_copy(work, a.lab_dist, rel)
            pub.append({"push": labctl.push(v, dist, str(root)), "activate": labctl.activate(labctl.Layout(root), ver)})
        lay = labctl.Layout(root)
        rep["publish"] = [{"version": p["activate"]["version"], "build": p["activate"]["build"], "activate_s": p["activate"]["seconds"],
                           "link_dest": p["push"]["link_dest"]} for p in pub]
        rep["budget"] = labctl.budget_report(lay)
        # 2. D2 栈与 nginx
        stack.start()
        n_max0 = int(CAP.stack_health(stack).get("n_max") or 0)
        box = NginxBox(work, a.image, int(stack.show_base.rsplit(":", 1)[1]), int(stack.base.rsplit(":", 1)[1]))
        box.start()
        rep["nginx"] = box.version()
        site = verify.Site(f"https://{DOMAIN}:{box.port}", connect="127.0.0.1", insecure=True)
        v1 = verify.run(site, source_ips=verify.parse_ips(VERIFY_SOURCES), expect_nmax=n_max0)
        rep["verify"] = v1
        print(f"drill: verify {'通过' if v1['ok'] else '不通过'}（{v1['seconds']} s）")
        # 3. D2-AC-04 变体 A 负载
        n_sess = n_max0 if a.sessions is None else a.sessions
        for k in range(n_sess):
            s = CAP.sandbox(stack, k).create("synthcity", snapshot=CAP.capacity_config(seed=21 + k))
            s.wait_ready(120)
            s.clock("speed", rate=1)
            sessions.append(s)
        smp = B.Sampler(stack, out).start()
        ops = CAP.Operators(sessions).start()
        vw = CAP.Viewers(stack, a.viewers).start()
        # 4. WS 探针 + 交替下载
        total = a.warmup_s + a.repeats * 2 * (a.phase_s + 5.0) + 10.0
        probe_out = out / "wsprobe.json"
        probe = _spawn("_wsprobe", "--port", str(box.port), "--seconds", str(total), "--out", str(probe_out), cpus=PROBE_CPUS)
        time.sleep(a.warmup_s)
        idx = json.loads(lay.index.read_text(encoding="utf-8"))
        paths = [f"/lab-static/v/{idx['version']}/{x['path']}" for x in idx["events"][0]["runs"][0]["videos"]]
        phases: list[dict] = []
        nmax_seen: list[int | None] = []
        for r in range(a.repeats):
            for kind in ("base", "load"):
                t0 = time.monotonic_ns()
                dl, dl_out = None, out / f"download-{r}.json"
                if kind == "load":
                    dl = _spawn("_download", "--port", str(box.port), "--paths", ",".join(paths), "--out", str(dl_out), cpus=DL_CPUS)
                time.sleep(a.phase_s / 2)
                nmax_seen.append(verify.check_nmax(site, n_max0)["detail"].get("n_max"))
                time.sleep(a.phase_s / 2)
                t1 = time.monotonic_ns()
                info = None
                if dl is not None:
                    dl.send_signal(signal.SIGTERM)
                    dl.wait(30)
                    with contextlib.suppress(OSError, ValueError):
                        info = json.loads(dl_out.read_text(encoding="utf-8"))
                phases.append({"kind": kind, "t0_ns": t0, "t1_ns": t1, "download": info})
                print(f"drill: 阶段 {r + 1}/{a.repeats} {kind} 完成" + (f"（下载 {info['mib_s_total']} MiB/s）" if info else ""))
                time.sleep(5.0)
        probe.wait(total + 60)
        rec = json.loads(probe_out.read_text(encoding="utf-8"))
        rep["ws"] = ws_latency(rec, phases)
        rep["ws"]["phase_plan"] = [{"kind": p["kind"], "seconds": round((p["t1_ns"] - p["t0_ns"]) / 1e9, 1)} for p in phases]
        o, vv = ops.stop(), vw.stop()
        samples = smp.stop()
        summ = B.summarize(samples, skip_s=10.0)
        ld = load.stop()
        cpu_valid = ld["mean"] < 6.0
        # 负载前置条件（与 capacity.py 同一口径：采样期间 1 min 平均负载的均值 < 6）不满足时，时延与变体 A 的 CPU 类判据只作参考（judge_a 记为 None）
        rep["ws"]["load_valid"] = cpu_valid
        rep["load_valid"] = cpu_valid
        checks = CAP.judge_a({"summary": summ}, cpu_valid=cpu_valid, cgroup_mode=stack.cgroup_mode)
        rep["d2_ac04"] = {"n_max_start": n_max0, "n_max_seen": nmax_seen, "n_max_unchanged": all(x == n_max0 for x in nmax_seen),
                          "sessions": n_sess, "operators": o, "viewers": vv, "cgroup": stack.cgroup_mode, "load": ld,
                          "cpu_valid": cpu_valid, "checks": checks, "minutes": round(len(samples) / 60.0, 1),
                          "by_phase": d2_by_phase(samples, phases),
                          "ok": all(c["ok"] is not False for c in checks) and all(x == n_max0 for x in nmax_seen)}
        for s in sessions:
            with contextlib.suppress(Exception):
                s.close()
        sessions = []
        # 5. 回滚演练
        rb: dict[str, Any] = {}
        rb["rollback_version"] = timed(lambda: labctl.rollback(lay),
                                       lambda: _expect_version(site, "2026.10.19-1"))
        rb["withdraw_to_d2"] = timed(lambda: labctl.withdraw(lay, box.site_file, d2_site=box.d2_site, nginx_test=box.test_cmd,
                                                             nginx_reload=box.reload_cmd),
                                     lambda: verify.run(site, expect="d2"))
        rb["reenable_lab"] = timed(lambda: (labctl.install_site(box.site_file, box.localize(labsite.render(labsite.D2_SITE.read_text(
            encoding="utf-8"))), nginx_test=box.test_cmd, nginx_reload=box.reload_cmd), labctl.activate(lay, "2026.10.20-1")),
            lambda: _expect_version(site, "2026.10.20-1"))
        rb["ok"] = all(x["ok"] for x in rb.values() if isinstance(x, dict))
        rb["gate_s"] = ROLLBACK_GATE_S
        rep["rollback"] = rb
        rep["history"] = [json.loads(x) for x in lay.history.read_text(encoding="utf-8").splitlines()]
        ok = v1["ok"] and rep["ws"]["ok"] and rep["d2_ac04"]["ok"] and rb["ok"] and rep["budget"]["ok"]
    except Exception as e:
        rep["error"] = f"{type(e).__name__}: {e}"
        ok = False
    finally:
        for s in sessions:
            with contextlib.suppress(Exception):
                s.close()
        if box is not None:
            with contextlib.suppress(Exception):
                (out / "nginx-error.log").write_bytes((work / "nginx" / "error.log").read_bytes()[-200_000:])
            box.stop()
        stack.stop()
        shutil.rmtree(work, ignore_errors=True)
        del lock
    rep["t_end"] = utc()
    rep["ok"] = ok
    (out / "drill.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    ws = rep.get("ws") or {}
    print(f"drill: {'通过' if ok else '不通过'}；WS p95 基线 {(ws.get('base') or {}).get('p95_ms')} ms → 下载 {(ws.get('load') or {}).get('p95_ms')} ms"
          f"（增幅 {ws.get('p95_increase')}）；N_max {(rep.get('d2_ac04') or {}).get('n_max_seen')}"
          f"{'' if rep.get('load_valid', True) else '；负载前置条件不满足（同机平均负载 ≥ 6），时延与 CPU 类判据只作参考'}；报告 {out / 'drill.json'}")
    if rep.get("error"):
        print(f"drill: 错误 {rep['error']}", file=sys.stderr)
    return EXIT_OK if ok else EXIT_FAIL


def _expect_version(site: verify.Site, ver: str) -> dict:
    idx, _, err = verify.fetch_index(site)
    ok = idx is not None and idx.get("version") == ver
    lab = verify.check_lab(site)
    return {"ok": ok and lab["ok"], "checks": [{"name": "index_version", "ok": ok, "problems": [] if ok else [err or f"版本 {idx and idx.get('version')}"]},
                                              lab]}


if __name__ == "__main__":
    sys.exit(main())
