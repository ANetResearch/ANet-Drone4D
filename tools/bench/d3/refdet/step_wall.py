#!/usr/bin/env python3
"""tools/bench/d3/refdet/step_wall.py：L2 基线在容器 `std` 资源档内的每步墙钟（D3-AC-58；M26-FR-126、FR-121）。

正式评测的 L2 每步墙钟预算为 `max(0.5 s, τ × wall_ratio)`（τ = 0 时 0.5 s），自 OBS 帧最后一个字节写出起计（AWR-17 §18.9.6）。
本工具把"一次真实 L2 运行的线协议帧"录下来，再以同一字节流驱动 `std` 档隔离容器中的 `anet4d.serve`，逐步测量 OBS 写完到
ACT 收齐的墙钟；Agent 的计算与观测内容无关于 Env 的物理档与渲染耗时，录放分离后可在无 GPU 的 Agent 容器中重复测量。

子命令：
- `capture`（d3-sensorsvc 镜像，EGL 渲染需 GPU）：以 run_spec 运行一次 L2 Env，Agent 进程内运行；每架机的 CONFIG 与逐步 OBS 按宿主侧
  `awr.agentsdk.wire.encode_frame` 编码后落盘，Agent 收到的是这些帧的解码结果（与容器内逐字节同一输入），其 ACT 按 Agent 侧
  `anet4d.wire.encode_frame` 编码落盘作为期望值。输出 `<out>/<uav>/{config,obs,act}.bin` 与 `<out>/capture.json`。
- `replay`（d3-agent-py 镜像，只需 msgpack 与标准库）：每架机一个监听套接字（`<sock-root>/<uav>/agent.sock`），接受容器 Agent 的
  HELLO，下发录制的 CONFIG，然后各机以屏障锁步逐步下发 OBS（与 `run_remote_episode` 同序：同一步内各机并发计算），记录每步墙钟并比对
  ACT 字节与录制值；结束发 CLOSE。输出 `bench/step_wall.v1` 的单次重复记录。
- `infer`（d3-agent-py 镜像，`std` 档）：只测参考检测器的单帧推理（`decode_small` + `RefDet.detect`，有可识别框时加 `decode_full`）。
- `summarize`：合并各次重复（ADR-033：3 次取中位），给出逐机与合并的分位数、超预算步数与预算判定。

容器参数与 Env 侧启动器一致（`awr.agentsdk.env.containers.AgentContainers.argv`，M26 §8.1 资源档 `std`：`--cpus 2`、`--memory 8g`、
`--memory-swap 8g`、`--pids-limit 256`、`--ulimit nofile=1024:1024`、无 GPU），由同目录 run_step_wall.sh 编排。

用法：
    python step_wall.py capture --spec configs/bench.d3/crop-handoff-001.l2-3uav.run_spec.json --policy track_share --seed 1 \\
        --worlds-root worlds --events-root d3data/events --physics kinematic --out out/a05/trace
    python step_wall.py replay --trace out/a05/trace --sock-root out/a05/sock --out out/a05/rep-1.json [--budget-s 0.5]
    python step_wall.py infer --trace out/a05/trace --uav uav-01 --out out/a05/infer.json
    python step_wall.py summarize out/a05/rep-*.json --infer out/a05/infer.json --out out/a05/step_wall.json
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import platform
import socket
import struct
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SDK = ROOT / "tools" / "sdk" / "anet4d" / "src"

SCHEMA = "drone4d.bench.step_wall.v1"
HDR = struct.Struct("<IBBH")
T_HELLO, T_CONFIG, T_READY, T_OBS, T_ACT, T_CLOSE, T_BYE, T_ERROR = 0x01, 0x02, 0x03, 0x10, 0x11, 0x20, 0x21, 0x30
PROTOCOL = "drone4d.bench.wire.v1"
L2_BUDGET_S = 0.5                       # M26-FR-126：正式评测 L2 分轨每步墙钟预算 max(0.5 s, τ × wall_ratio)
QUANTILES = (0.5, 0.9, 0.95, 0.99)


# ---------------------------------------------------------------------------------------------- 帧文件
def iter_frames(path: Path) -> Iterator[tuple[int, bytes]]:
    """连续拼接的线协议帧文件 → (类型, 整帧字节)。"""
    with open(path, "rb") as f:
        while True:
            h = f.read(HDR.size)
            if not h:
                return
            if len(h) != HDR.size:
                raise ValueError(f"{path}: truncated frame header")
            n, t, _flags, _ver = HDR.unpack(h)
            b = f.read(n)
            if len(b) != n:
                raise ValueError(f"{path}: truncated frame body")
            yield t, h + b


def quantile(xs: list[float], q: float) -> float:
    """线性插值分位数（同 numpy 缺省 `linear`）；空表为 nan。"""
    if not xs:
        return math.nan
    s = sorted(xs)
    pos = (len(s) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def stats(xs: list[float], budget_s: float | None = None) -> dict:
    d = {"n": len(xs), "mean_s": round(sum(xs) / len(xs), 6) if xs else None, "max_s": round(max(xs), 6) if xs else None}
    for q in QUANTILES:
        d[f"p{round(q * 100):d}_s"] = round(quantile(xs, q), 6) if xs else None
    if budget_s is not None:
        d["over_budget"] = sum(1 for x in xs if x > budget_s)
    return d


# ---------------------------------------------------------------------------------------------- capture
def capture(a: argparse.Namespace) -> int:
    sys.path[:0] = [str(ROOT / "python"), str(SDK)]
    import msgpack
    from anet4d.agent import DroneAgent
    from anet4d.baselines import BASELINES
    from anet4d.runner import run_episode
    from anet4d.wire import T as AT
    from anet4d.wire import encode_frame as agent_frame

    from awr.agentsdk.env.core import BenchEnv
    from awr.agentsdk.env.spec import EnvSpec
    from awr.agentsdk.wire import T as HT
    from awr.agentsdk.wire import encode_frame as host_frame

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    walls: dict[str, list[float]] = {}

    class Recorder(DroneAgent):
        """进程内 Agent 的录制外壳：输入取宿主侧帧的解码结果（与容器内同一输入），输出按 Agent 侧编码落盘。"""

        def __init__(self, uav: str) -> None:
            self.uav, self.inner = uav, BASELINES[a.policy]()
            d = out / uav
            d.mkdir(parents=True, exist_ok=True)
            self.f_obs, self.f_act = open(d / "obs.bin", "wb"), open(d / "act.bin", "wb")  # noqa: SIM115
            self.d = d
            walls[uav] = []

        def reset(self, config: dict) -> None:
            fr = host_frame(HT.CONFIG, config)
            (self.d / "config.bin").write_bytes(fr)
            self.inner.reset(msgpack.unpackb(fr[HDR.size:], raw=False))

        def step(self, obs: dict, inbox: list[dict]) -> dict:
            fr = host_frame(HT.OBS, {"obs": obs, "inbox": inbox})
            self.f_obs.write(fr)
            body = msgpack.unpackb(fr[HDR.size:], raw=False)
            t0 = time.perf_counter()
            act = self.inner.step(body["obs"], body["inbox"])
            walls[self.uav].append(time.perf_counter() - t0)
            self.f_act.write(agent_frame(AT.ACT, act))
            return act

        def close(self) -> None:
            self.f_obs.close()
            self.f_act.close()

    rs = json.loads(Path(a.spec).read_text(encoding="utf-8"))
    rs = {**rs, "policy": {"kind": "reference", "name": a.policy}}
    if a.duration_s:
        rs["duration_s"] = float(a.duration_s)
    run_id = f"a05-capture-{a.policy}-s{a.seed}"
    spec = EnvSpec.from_run_spec(rs, run_id=run_id, seed=a.seed, worlds_root=Path(a.worlds_root), events_root=Path(a.events_root),
                                 out_dir=out / "run", physics=a.physics, gz={})
    recs: dict[str, Recorder] = {}

    def make(u: str) -> Recorder:
        recs[u] = Recorder(u)
        return recs[u]

    t0 = time.time()
    try:
        manifest = run_episode(BenchEnv(spec), make)
    finally:
        for r in recs.values():
            r.close()
    doc = {"schema": "drone4d.bench.step_wall_trace.v1", "run_id": run_id, "policy": a.policy, "seed": a.seed, "physics": a.physics,
           "obs_level": rs.get("obs_level"), "duration_s": rs.get("duration_s"), "spec": str(a.spec), "wall_s": round(time.time() - t0, 1),
           "pose_sha256": manifest.get("pose_sha256"), "python": platform.python_version(), "uavs": {}}
    for u in sorted(recs):
        d = out / u
        doc["uavs"][u] = {"steps": len(walls[u]), "obs_bytes": (d / "obs.bin").stat().st_size, "obs_sha256": _sha256(d / "obs.bin"),
                          "act_sha256": _sha256(d / "act.bin"), "inproc_step_wall": stats(walls[u])}
    (out / "capture.json").write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({u: v["inproc_step_wall"] for u, v in doc["uavs"].items()}), flush=True)
    return 0


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


# ---------------------------------------------------------------------------------------------- replay
class _Conn:
    def __init__(self, sock: socket.socket) -> None:
        self.s, self.buf = sock, bytearray()

    def recv_frame(self) -> tuple[int, bytes]:
        """→ (类型, 帧体字节)。"""
        while True:
            if len(self.buf) >= HDR.size:
                n, t, _f, _v = HDR.unpack_from(self.buf)
                if len(self.buf) >= HDR.size + n:
                    body = bytes(self.buf[HDR.size:HDR.size + n])
                    del self.buf[:HDR.size + n]
                    return t, body
            chunk = self.s.recv(1 << 16)
            if not chunk:
                raise ConnectionError("agent closed the connection")
            self.buf += chunk


def _encode(t: int, body) -> bytes:
    import msgpack

    b = msgpack.packb(body, use_bin_type=True)
    return HDR.pack(len(b), t, 0, 1) + b


def replay_uav(conn: _Conn, trace: Path, barrier: threading.Barrier | None, budget_s: float, res: dict) -> None:
    """一架机的锁步回放：CONFIG → READY → 逐步 OBS/ACT（每步前后过屏障）→ CLOSE。结果写入 res。"""
    import msgpack

    t, body = conn.recv_frame()
    hello = msgpack.unpackb(body, raw=False)
    if t != T_HELLO or hello.get("protocol") != PROTOCOL:
        raise RuntimeError(f"unexpected first frame {t:#x} {hello!r}")
    res["sdk_version"] = hello.get("sdk_version")
    conn.s.sendall((trace / "config.bin").read_bytes())
    t0 = time.perf_counter()
    t, _ = conn.recv_frame()
    if t != T_READY:
        raise RuntimeError(f"expected READY, got {t:#x}")
    res["config_to_ready_s"] = round(time.perf_counter() - t0, 3)
    wall, xfer, mism = [], [], []
    for k, ((to, obs), (ta, act)) in enumerate(zip(iter_frames(trace / "obs.bin"), iter_frames(trace / "act.bin"), strict=True)):
        assert to == T_OBS and ta == T_ACT
        if barrier is not None:
            barrier.wait()
        t_start = time.perf_counter()
        conn.s.sendall(obs)
        t_sent = time.perf_counter()
        t, body = conn.recv_frame()
        t_done = time.perf_counter()
        if t != T_ACT:
            raise RuntimeError(f"step {k}: expected ACT, got {t:#x}: {body[:200]!r}")
        wall.append(t_done - t_sent)
        xfer.append(t_sent - t_start)
        if body != act[HDR.size:]:
            mism.append(k)
    if barrier is not None:
        barrier.wait()
    conn.s.sendall(_encode(T_CLOSE, {"reason": "run_ended"}))
    with contextlib.suppress(ConnectionError, OSError):
        conn.recv_frame()                                    # 可选的 BYE
    res.update({"steps": len(wall), "wall_s": wall, "send_s": xfer, "act_mismatch_steps": mism[:50], "act_mismatches": len(mism),
                "step_wall": stats(wall, budget_s), "obs_send": stats(xfer)})


def replay(a: argparse.Namespace) -> int:
    trace, root = Path(a.trace), Path(a.sock_root)
    cap = json.loads((trace / "capture.json").read_text(encoding="utf-8"))
    uavs = a.uavs.split(",") if a.uavs else sorted(cap["uavs"])
    listen = {}
    for u in uavs:
        d = root / u
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o755)
        p, tmp = d / "agent.sock", d / "agent.sock.tmp"
        for x in (p, tmp):
            if x.exists():
                x.unlink()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(tmp))
        os.chmod(tmp, 0o666)                                 # 容器用户 65534
        s.listen(1)
        os.replace(tmp, p)                                   # 监听就绪后才出现正式路径（Agent 见到路径即可连接）
        s.settimeout(a.hello_timeout_s)
        listen[u] = s
    print(f"[replay] listening for {len(uavs)} agents under {root}", flush=True)
    conns = {}
    for u, s in listen.items():
        c, _ = s.accept()
        c.settimeout(None)
        conns[u] = _Conn(c)
    barrier = threading.Barrier(len(uavs)) if len(uavs) > 1 else None
    res: dict[str, dict] = {u: {} for u in uavs}
    errs: list[BaseException] = []

    def run(u: str) -> None:
        try:
            replay_uav(conns[u], trace / u, barrier, a.budget_s, res[u])
        except BaseException as e:                           # 线程内异常汇总到主线程
            errs.append(e)
            if barrier is not None:
                barrier.abort()

    t0 = time.time()
    th = [threading.Thread(target=run, args=(u,), name=f"replay-{u}") for u in uavs]
    for x in th:
        x.start()
    for x in th:
        x.join()
    for c in conns.values():
        c.s.close()
    for s in listen.values():
        s.close()
    if errs:
        raise errs[0]
    pooled = [w for u in uavs for w in res[u]["wall_s"]]
    doc = {"schema": SCHEMA, "kind": "replay", "trace": {"run_id": cap["run_id"], "policy": cap["policy"], "seed": cap["seed"],
                                                          "physics": cap["physics"], "obs_level": cap["obs_level"]},
           "budget_s": a.budget_s, "label": a.label, "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0)),
           "wall_s": round(time.time() - t0, 1), "loadavg": _loadavg(), "uavs": {}, "pooled": stats(pooled, a.budget_s)}
    for u in uavs:
        r = res[u]
        doc["uavs"][u] = {k: r[k] for k in ("sdk_version", "config_to_ready_s", "steps", "step_wall", "obs_send", "act_mismatches",
                                             "act_mismatch_steps")}
        doc["uavs"][u]["act_matches_capture"] = r["act_mismatches"] == 0
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    if a.raw:
        Path(a.raw).write_text(json.dumps({u: res[u]["wall_s"] for u in uavs}) + "\n", encoding="utf-8")
    print(json.dumps({"pooled": doc["pooled"], "mismatches": {u: res[u]["act_mismatches"] for u in uavs}}), flush=True)
    return 0


def _loadavg() -> list[float] | None:
    try:
        return [round(x, 2) for x in os.getloadavg()]
    except OSError:
        return None


# ---------------------------------------------------------------------------------------------- infer
def infer(a: argparse.Namespace) -> int:
    """单帧推理墙钟：逐帧 decode_small + detect；检测框高 ≥ IDENT_PX（交付分辨率）时加一次 decode_full（外观取色）。"""
    sys.path.insert(0, str(SDK))
    import msgpack
    from anet4d.perception import ref_det as R

    det = R.RefDet()
    rows, n_det = [], 0
    trace = Path(a.trace) / a.uav
    for k, (_t, fr) in enumerate(iter_frames(trace / "obs.bin")):
        if a.max_frames and k >= a.max_frames:
            break
        obs = msgpack.unpackb(fr[HDR.size:], raw=False)["obs"]
        for f in obs["sensors"].get("eo", []):
            data = f["rgb"]["data"]
            t0 = time.perf_counter()
            small, (fw, _fh) = R.decode_small(data)
            t1 = time.perf_counter()
            dets = det.detect(small)
            t2 = time.perf_counter()
            if any(h * fw / R.W_IN >= R.IDENT_PX for *_c, h in dets):
                R.decode_full(data)
            t3 = time.perf_counter()
            n_det += len(dets)
            rows.append((t1 - t0, t2 - t1, t3 - t2, t3 - t0))
    doc = {"schema": SCHEMA, "kind": "infer", "uav": a.uav, "frames": len(rows), "detections": n_det, "loadavg": _loadavg(),
           "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "decode_small": stats([r[0] for r in rows]), "detect": stats([r[1] for r in rows]), "decode_full": stats([r[2] for r in rows]),
           "frame_total": stats([r[3] for r in rows])}
    Path(a.out).write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(doc["frame_total"]), flush=True)
    return 0


# ---------------------------------------------------------------------------------------------- summarize
def summarize_docs(reps: list[dict], infer_doc: dict | None = None, *, env: dict | None = None) -> dict:
    """各次重复 → 汇总：逐机与合并的分位数取各次重复的中位数（ADR-033：3 次取中位），超预算步数取最大值；
    `passed`：合并 p99 与最大单步均 ≤ 预算、所有重复 ACT 与录制逐字节一致。"""
    if not reps:
        raise ValueError("no replay records")
    budget = reps[0]["budget_s"]
    if any(r["budget_s"] != budget for r in reps):
        raise ValueError("replays use different budgets")

    def med(vals: list) -> float | None:
        v = [x for x in vals if x is not None]
        return round(quantile(v, 0.5), 6) if v else None

    def merge(blocks: list[dict]) -> dict:
        keys = [k for k in blocks[0] if k.endswith("_s")]
        d = {k: med([b[k] for b in blocks]) for k in keys}
        d["n"] = blocks[0]["n"]
        if "over_budget" in blocks[0]:
            d["over_budget_max"] = max(b["over_budget"] for b in blocks)
        return d

    uavs = sorted(reps[0]["uavs"])
    out = {"schema": SCHEMA, "kind": "summary", "budget_s": budget, "repeats": len(reps), "trace": reps[0]["trace"],
           "runs": [{"label": r.get("label"), "utc": r["utc"], "loadavg": r.get("loadavg"), "pooled": r["pooled"]} for r in reps],
           "uavs": {u: merge([r["uavs"][u]["step_wall"] for r in reps]) for u in uavs},
           "pooled": merge([r["pooled"] for r in reps]),
           "act_mismatches": sum(r["uavs"][u]["act_mismatches"] for r in reps for u in uavs)}
    p = out["pooled"]
    worst = max(r["pooled"]["max_s"] for r in reps)
    out["worst_step_s"] = worst
    out["passed"] = bool(p["p99_s"] is not None and p["p99_s"] <= budget and worst <= budget and out["act_mismatches"] == 0)
    if infer_doc is not None:
        out["infer"] = {k: infer_doc[k] for k in ("uav", "frames", "detections", "decode_small", "detect", "decode_full", "frame_total")}
    if env is not None:
        out["env"] = env
    return out


def summarize(a: argparse.Namespace) -> int:
    reps = [json.loads(Path(p).read_text(encoding="utf-8")) for p in a.replays]
    inf = json.loads(Path(a.infer).read_text(encoding="utf-8")) if a.infer else None
    env = json.loads(Path(a.env).read_text(encoding="utf-8")) if a.env else None
    doc = summarize_docs(reps, inf, env=env)
    Path(a.out).write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"pooled": doc["pooled"], "worst_step_s": doc["worst_step_s"], "passed": doc["passed"]}), flush=True)
    return 0 if doc["passed"] else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="step_wall.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("--spec", type=Path, required=True)
    c.add_argument("--policy", default="track_share")
    c.add_argument("--seed", type=int, default=1)
    c.add_argument("--worlds-root", default="worlds")
    c.add_argument("--events-root", default="d3data/events")
    c.add_argument("--physics", choices=("realistic", "kinematic"), default="kinematic")
    c.add_argument("--duration-s", type=float)
    c.add_argument("--out", required=True)
    r = sub.add_parser("replay")
    r.add_argument("--trace", required=True)
    r.add_argument("--sock-root", required=True)
    r.add_argument("--uavs")
    r.add_argument("--budget-s", type=float, default=L2_BUDGET_S)
    r.add_argument("--hello-timeout-s", type=float, default=120.0)
    r.add_argument("--label")
    r.add_argument("--raw", help="also write the per-step wall times")
    r.add_argument("--out", required=True)
    i = sub.add_parser("infer")
    i.add_argument("--trace", required=True)
    i.add_argument("--uav", default="uav-01")
    i.add_argument("--max-frames", type=int, default=0)
    i.add_argument("--out", required=True)
    s = sub.add_parser("summarize")
    s.add_argument("replays", nargs="+")
    s.add_argument("--infer")
    s.add_argument("--env", help="JSON with container flags, host and image facts")
    s.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    return {"capture": capture, "replay": replay, "infer": infer, "summarize": summarize}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
