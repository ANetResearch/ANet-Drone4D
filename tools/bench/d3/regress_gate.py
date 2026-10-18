#!/usr/bin/env python3
"""tools/bench/d3/regress_gate.py：D3 回归门（AWR-05 §4.8、D3-AC-01；WP-D3-00）。每个 D3 工作包合入 `d3-dev` 之前运行。

步骤与命令登记在 `tests/d3/regress_gate/gate.json`（第 1–6 条）：
  1. D2 Python 测试全集（`pytest -m "not perf"`）、契约测试与前端单测；
  2. 零影响对拍（builtin `zero-impact`）：`AWR_D3_ENABLED = 0` 与 `= 1`（不加载事件）两种条件下各运行一次
     `relay_batch.py --case b --hours 1 --determinism`，`relay_stats.json` 与 `events.jsonl` 的 sha256 两两相同，且与
     `tests/d3/regress_gate/baseline.json`（D2 基线，`--record-baseline` 在 `d2` 分支上生成）相同；两种条件下
     `tests/targets/test_determinism.py` 都通过；relay_batch 只以运行性检查（`RELAY_RUN_CHECKS`）与 determinism 为门槛，
     间隙等场景质量检查随字节对拍一并比较；
     resim 帧摘要对拍（builtin `resim`）：两种条件下各在独立子进程中按 `gate.json` 的固定剧本录制一次输入日志（假墙钟单步
     驱动，确定性），再以 `awr.sim.runtime.resim` 重仿真，帧摘要序列（每次 StateRing 发布的 Full64 sha256）与输入条目序列
     两两相同，且与 `baseline.json` 的 `resim` 节（D2 基线，d2@928c57b）相同；
  3. D2 性能用例子集（builtin `perf`，缺省不运行：需要独占主机，ADR-033 运行协议）；
  4. `make lint`（含 lint-m06 与 import 方向规则）；5. import 方向；6. 公开演示构建与 K9 生产包扫描；K9 的 D3 扩展扫描
     （builtin `k9-d3`）：D2 公开演示构建产物的文本文件中 D3 入口字符串（`/lab/`、`/api/bench/v1/runs|events|session`，以及
     bench 客户端拼接路径所用的基址 `/api/bench/v1`）出现 0 次；报告附产物的 build-info（提交与构建时刻，判断是否陈旧）。

用法：
    python tools/bench/d3/regress_gate.py                       # 缺省步骤
    python tools/bench/d3/regress_gate.py --steps zero-impact   # 只跑指定步骤（逗号分隔）
    python tools/bench/d3/regress_gate.py --steps zero-impact --record-baseline   # 在 d2 分支上记录基线
    PYTHONPATH=<d2 检出>/python python tools/bench/d3/regress_gate.py --steps resim --record-baseline
                                                                # 以 d2 检出的 awr 包记录 resim 基线（本脚本不随 d2 发布；
                                                                # d2 检出 = git archive 928c57b python packages vehicles configs，
                                                                # worlds/ 链接到本检出；--source-label 写入 source.tree）
输出 `runs/perf/d3-gate-<UTC>/report.json`（`awr.d3.regress_gate.report.v1`）；退出码 0 通过、1 不通过、2 参数错误。
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
GATE = ROOT / "tests" / "d3" / "regress_gate" / "gate.json"
BASELINE = ROOT / "tests" / "d3" / "regress_gate" / "baseline.json"
PY = sys.executable


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def load_gate(path: Path = GATE) -> dict:
    g = json.loads(path.read_text(encoding="utf-8"))
    ids = [s["id"] for s in g["steps"]]
    if len(ids) != len(set(ids)):
        raise ValueError("gate.json: duplicate step ids")
    return g


def select_steps(g: dict, only: list[str] | None) -> list[dict]:
    if only:
        known = {s["id"] for s in g["steps"]}
        bad = [x for x in only if x not in known]
        if bad:
            raise ValueError(f"unknown steps {bad}; known {sorted(known)}")
        return [s for s in g["steps"] if s["id"] in only]
    return [s for s in g["steps"] if s.get("default", True)]


def compare_digests(runs: list[dict[str, str]], baseline: dict[str, str] | None) -> list[str]:
    """runs: 每种开关条件下 {文件名: sha256}；返回不一致的说明（空表示一致）。"""
    out = []
    if not runs:
        return ["no runs"]
    ref = runs[0]
    for i, r in enumerate(runs[1:], 1):
        out.extend(f"{k}: flags #{i} {r.get(k)} != flags #0 {ref[k]}" for k in ref if r.get(k) != ref[k])
    if baseline is not None:
        out.extend(f"{k}: {ref.get(k)} != D2 baseline {v}" for k, v in baseline.items() if ref.get(k) != v)
    return out


# relay_batch 用例检查中的运行性检查；其余（间隙、交接等场景质量检查）由字节对拍覆盖，不单独作为门槛
RELAY_RUN_CHECKS = ("summary.exit_code", "summary.config_problems")


def relay_verdict(report: dict, case: str, hours: float) -> tuple[str, list[str]]:
    """从 relay_batch 的 report.json 取 r1 用例目录名与不通过项（运行性检查与 determinism 逐字节一致）。"""
    name = f"relay-{case}-{hours:g}h"
    cases = {c["case"]: c for c in report.get("cases", [])}
    problems = []
    for r in ("-r1", "-r2"):
        c = cases.get(name + r)
        if c is None:
            problems.append(f"{name}{r}: missing")
            continue
        problems += [f"{name}{r}: {k['check']}" for k in c.get("checks", []) if k["check"] in RELAY_RUN_CHECKS and not k["ok"]]
    det = cases.get(name + "-determinism")
    if det is None or not det.get("ok"):
        problems.append(f"{name}-determinism: {'missing' if det is None else 'not byte identical'}")
    return name + "-r1", problems


def _run(argv: list[str], cwd: Path, timeout_s: float, env: dict | None = None, log: Path | None = None) -> tuple[int, float]:
    t0 = time.monotonic()
    e = {**os.environ, **(env or {})}
    with open(log, "w", encoding="utf-8") if log else open(os.devnull, "w") as f:
        try:
            r = subprocess.run(argv, cwd=cwd, env=e, stdout=f, stderr=subprocess.STDOUT, timeout=timeout_s, check=False)
            code = r.returncode
        except subprocess.TimeoutExpired:
            code = 124
    return code, round(time.monotonic() - t0, 1)


def step_cmd(s: dict, out: Path) -> dict:
    argv = [PY if a == "{py}" else a for a in s["argv"]]
    code, dt = _run(argv, ROOT / s.get("cwd", "."), float(s.get("timeout_s", 3600)), log=out / f"{s['id']}.log")
    return {"pass": code == 0, "exit": code, "seconds": dt, "argv": argv}


def step_zero_impact(g: dict, out: Path, record: bool) -> dict:
    z = g["zero_impact"]
    rb = z["relay_batch"]
    runs, detail = [], []
    for k, flags in enumerate(z["flags"]):
        d = out / f"zero-{k}"
        argv = [PY, "tools/bench/sandbox/relay_batch.py", "--case", rb["case"], "--hours", str(rb["hours"]), "--seed", str(rb["seed"]),
                "--no-lock", "--no-load-wait", "--determinism", "--out", str(d)]
        code, dt = _run(argv, ROOT, 7200, env=flags, log=out / f"zero-{k}.log")
        rep = json.loads((d / "report.json").read_text(encoding="utf-8")) if (d / "report.json").exists() else {}
        case_name, problems = relay_verdict(rep, rb["case"], float(rb["hours"]))
        case_dir = d / case_name
        digests = {f: sha256(case_dir / f) for f in z["files"] if (case_dir / f).exists()}
        pcode, pdt = _run([PY, "-m", "pytest", "-q", *z["pytest"]], ROOT, 1800, env=flags, log=out / f"zero-{k}-pytest.log")
        runs.append(digests)
        detail.append({"flags": flags, "relay_batch_exit": code, "relay_problems": problems, "seconds": dt, "digests": digests,
                       "pytest_exit": pcode,
                       "pytest_seconds": pdt})
    missing = [f for f in z["files"] for r in runs if f not in r]
    if record:
        write_baseline("relay_batch", rb)
        write_baseline("digests", runs[0])
    base = load_baseline().get("digests")
    diffs = compare_digests(runs, base)
    ok = not missing and not diffs and all(not d["relay_problems"] and d["pytest_exit"] == 0 for d in detail)
    return {"pass": ok, "runs": detail, "diffs": diffs, "missing": missing, "baseline": str(BASELINE.relative_to(ROOT)) if base else None}


# ---------------------------------------------------------------- resim 帧摘要（AWR-05 §4.8 第 2 条）
# 录制与重仿真只用 D1 起稳定的 awr 公共接口（SimCore、InputLog、resim），以便同一段代码在 d2 检出上记录基线
RESIM_KEYS = ("frames", "frames_sha256", "inputs", "inputs_sha256")
RESIM_CHUNK = 500


def _resim_principal(pid: str = "p-gate") -> dict:
    """录制用操作员主体（sim-core 无会话密钥时不验签；签名字段本来也不写入输入日志）。"""
    from awr.runtime.principal import Principal

    return Principal(pid, "operator", "api", "c-gate", True).fields()


def resim_record(run_dir: Path, spec: dict) -> int:
    """按 spec 的固定剧本录制 `run_dir/inputs.msgpack`（假墙钟单步驱动：命令在固定仿真时刻提交，apply_tick 确定）；
    返回录制结束时的 tick。命令未被受理即抛出 RuntimeError（剧本与当前代码不匹配，不能作为对拍输入）。"""
    import secrets

    from awr.contracts import LAYOUT_ID
    from awr.runtime.bus import LocalBus
    from awr.runtime.statering import LocalRing
    from awr.sim.fleet.fleet import FleetConfig
    from awr.sim.runtime.config import SimConfig
    from awr.sim.runtime.inputlog import InputLog, inputlog_header
    from awr.sim.runtime.main import SimCore

    tag = secrets.token_hex(3)
    fc = FleetConfig(kernel=spec.get("kernel", "numba"), path_capacity=int(spec.get("path_capacity", 65536)))
    cfg = SimConfig(run_id=str(spec.get("run_id", "gate-resim")), n_vehicles=int(spec["n_vehicles"]),
                    load_world=bool(spec.get("load_world", False)), world_id=str(spec.get("world_id", "shenzhen")),
                    autoplay=True, profile_id=str(spec.get("profile_id", "p600_mid360")),
                    spawn_xy=tuple(spec.get("spawn_xy", (0.0, 0.0))), spawn_spacing_m=float(spec.get("spawn_spacing_m", 15.0)),
                    fleet=fc, world_seed=int(spec.get("world_seed", 0)))
    path = f"/d3gate/{tag}/state.sim-core"
    ring, _ = LocalRing.open_or_create(path, capacity=fc.capacity, slots=32, layout_id=LAYOUT_ID, id_base=0, id_count=1024)
    bus = LocalBus.open("sim-core", namespace=f"awr/d3gate/{tag}")
    W = [1_000_000_000]
    core = SimCore(cfg, bus, ring, secret=None, wall_ns=lambda: W[0])
    il = None

    def advance_to(t_s: float) -> None:
        end = round(t_s * 1e9)
        step = 8 * core.clock.tick_ns  # 每次迭代墙钟前进 8 tick（同 M08 单步驱动夹具）
        guard = 0
        while core.clock.t_ns < end and guard < 10_000_000:
            W[0] += step
            core.iterate()
            guard += 1

    try:
        core.start()
        il = InputLog(Path(run_dir) / "inputs.msgpack", inputlog_header(core, cfg.run_id))
        core.inputlog = il
        core.engine.inputlog = il
        r = core._lease_op({"v": 1, "cid": "seat-gate", "op": "seat_claim", "principal": _resim_principal()})
        if r.get("code"):
            raise RuntimeError(f"seat_claim rejected: {r}")
        ids = [core.roster.by_slot[s].id for s in core.roster.slots_in_order()]
        for k, c in enumerate(sorted(spec["script"], key=lambda c: float(c["t_s"]))):
            advance_to(float(c["t_s"]))
            cid = f"gate-{k:03d}-{c['op']}"
            uav = c.get("uav", "*")
            msg = {"v": 1, "cid": cid, "op": c["op"], "uav": ids[uav] if isinstance(uav, int) else uav, "args": c.get("args") or {},
                   "principal": _resim_principal(), "lease": None, "t_wall_ns": 0, "epoch_seen": 1, "batch_id": None}
            adm = core.engine.handle(msg)
            if adm.get("status") != "accepted":
                raise RuntimeError(f"{cid}: {adm}")
        advance_to(float(spec["end_s"]))
        return int(core.clock.tick)
    finally:
        if il is not None:
            il.close()
        core.stop()
        bus.close()
        LocalRing.remove(path)


def resim_summary(digests: list[str], entries: list[dict], end_tick: int) -> dict:
    """帧摘要序列与输入条目序列的摘要：`frames_sha256` = sha256(各帧 sha256 十六进制串顺序拼接)（同 `resim_main`）；
    `inputs_sha256` 覆盖每条输入的 (tick, kind, cid, payload_sha256)，不含日志头（头中有版本与 FleetConfig 全集）；
    `chunks` 为每 500 帧一段的摘要，用于定位首个分歧段；`distinct` 为不同帧数（机体在运动的旁证）。"""
    inputs = "\n".join(f"{int(e['tick'])} {e['kind']} {e.get('cid')} {e['payload_sha256']}" for e in entries)
    return {"frames": len(digests), "distinct": len(set(digests)),
            "frames_sha256": hashlib.sha256("".join(digests).encode()).hexdigest(),
            "inputs": len(entries), "inputs_sha256": hashlib.sha256(inputs.encode()).hexdigest(), "end_tick": end_tick,
            "chunks": [hashlib.sha256("".join(digests[i:i + RESIM_CHUNK]).encode()).hexdigest()[:16]
                       for i in range(0, len(digests), RESIM_CHUNK)]}


def resim_worker(run_dir: Path, spec: dict) -> dict:
    """子进程：录制输入日志并重仿真，返回 `resim_summary`（附所用内核与 awr 版本）。"""
    from awr.sim.fleet.fleet import resolve_kernel
    from awr.sim.runtime.inputlog import _version, read_inputlog
    from awr.sim.runtime.resim import resim

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "inputs.msgpack").unlink(missing_ok=True)  # InputLog 以追加方式打开，残留的旧日志会混入第二个头
    end_tick = resim_record(run_dir, spec)
    _, entries = read_inputlog(run_dir / "inputs.msgpack")
    last = max((int(e["tick"]) for e in entries), default=0)
    digests = resim(run_dir, extra_ticks=max(0, end_tick - last))
    s = resim_summary(digests, entries, end_tick)
    s["kernel"] = resolve_kernel(spec.get("kernel", "numba"))[0]
    s["awr_version"] = _version()
    import awr

    s["awr_path"] = str(Path(awr.__file__).resolve().parent)
    return s


SOURCE_LABEL: list[str | None] = [None]  # --source-label：记录基线时写入 source.tree（例如 "d2@928c57b"）


def _rel_root(p: str | None) -> str | None:
    """awr 包路径：位于本检出内时写相对路径，否则只写 "external"（不把本机临时目录写进入库文件）。"""
    if not p:
        return None
    with contextlib.suppress(ValueError):
        return Path(p).resolve().relative_to(ROOT).as_posix()
    return "external"


def resim_digests(s: dict) -> dict[str, str]:
    return {k: str(s[k]) for k in RESIM_KEYS if k in s}


def step_resim(g: dict, out: Path, record: bool) -> dict:
    spec = g["resim"]
    runs, detail = [], []
    for k, flags in enumerate(g["zero_impact"]["flags"]):
        d = out / f"resim-{k}"
        argv = [PY, str(Path(__file__).resolve()), "--resim-worker", str(d)]
        code, dt = _run(argv, ROOT, float(spec.get("timeout_s", 1800)), env=flags, log=out / f"resim-{k}.log")
        s = json.loads((d / "resim.json").read_text(encoding="utf-8")) if code == 0 and (d / "resim.json").exists() else {}
        runs.append(resim_digests(s))
        detail.append({"flags": flags, "exit": code, "seconds": dt, "summary": s})
    if record and runs[0]:
        s0 = detail[0]["summary"]
        write_baseline("resim", {"spec_sha256": spec_sha256(spec), **runs[0], "chunks": s0.get("chunks", []),
                                 "source": {"tree": SOURCE_LABEL[0] or _rel_root(s0.get("awr_path")), "kernel": s0.get("kernel"),
                                            "awr_version": s0.get("awr_version"), "flags": detail[0]["flags"],
                                            "recorded_utc": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}})
    base = load_baseline().get("resim")
    problems = [f"flags #{k}: exit {d['exit']}" for k, d in enumerate(detail) if d["exit"] != 0]
    if base is not None and base.get("spec_sha256") != spec_sha256(spec):
        problems.append("gate.json resim spec changed since the D2 baseline was recorded; re-record on d2")
    diffs = compare_digests(runs, {k: base[k] for k in RESIM_KEYS} if base else None) if not problems else []
    if base is None:
        problems.append(f"no resim baseline in {BASELINE.relative_to(ROOT)}")
    return {"pass": not problems and not diffs, "runs": detail, "diffs": diffs, "problems": problems,
            "first_diverging_chunk": first_diverging_chunk(detail, base)}


def first_diverging_chunk(detail: list[dict], base: dict | None) -> int | None:
    """首个与基线（无基线时与条件 #0）不同的 500 帧段序号；全部一致为 None。"""
    ref = (base or {}).get("chunks") or (detail[0]["summary"].get("chunks") if detail else None) or []
    for d in detail:
        got = d["summary"].get("chunks") or []
        for i in range(max(len(ref), len(got))):
            if i >= len(ref) or i >= len(got) or ref[i] != got[i]:
                return i
    return None


def spec_sha256(spec: dict) -> str:
    """resim 剧本的规范化摘要（剧本改动后基线失效，须在 d2 上重录）。"""
    keep = {k: v for k, v in spec.items() if k not in ("doc", "timeout_s")}
    return hashlib.sha256(json.dumps(keep, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# ---------------------------------------------------------------- K9 的 D3 扩展扫描（AWR-05 §4.8 第 6 条）
def scan_k9_d3(dist: Path, forbidden: list[str], text_ext: list[str]) -> dict:
    """D2 公开演示构建产物中 D3 入口字符串的出现次数：`counts` 逐字符串计数，`hits` 为 [文件, 字符串, 上下文]（每文件每串
    至多 3 条）；产物缺失（无 index.html）时 `missing` 为真。"""
    if not (dist / "index.html").is_file():
        return {"missing": True, "files": 0, "counts": dict.fromkeys(forbidden, 0), "hits": []}
    exts = tuple(e.lower() for e in text_ext)
    counts = dict.fromkeys(forbidden, 0)
    hits: list[list[str]] = []
    n = 0
    for p in sorted(dist.rglob("*")):
        if not p.is_file() or not p.name.lower().endswith(exts):
            continue
        n += 1
        text = p.read_text(encoding="utf-8", errors="replace")
        rel = p.relative_to(dist).as_posix()
        for s in forbidden:
            c = text.count(s)
            if not c:
                continue
            counts[s] += c
            i = -1
            for _ in range(min(c, 3)):
                i = text.find(s, i + 1)
                hits.append([rel, s, " ".join(text[max(0, i - 50):i + len(s) + 30].split())])
    return {"missing": False, "files": n, "counts": counts, "hits": hits}


def step_k9_d3(g: dict, out: Path) -> dict:
    k = g["k9_d3"]
    dist = (ROOT / k["dist"]).resolve()
    r = scan_k9_d3(dist, list(k["forbidden"]), list(k["text_ext"]))
    with open(out / "k9-d3.log", "w", encoding="utf-8") as f:
        for h in r["hits"]:
            f.write(f"{h[0]} {h[1]} ...{h[2]}...\n")
        f.write(f"k9-d3: {dist} files {r['files']} counts {json.dumps(r['counts'])}\n")
    bi = dist / "build-info.json"
    build = json.loads(bi.read_text(encoding="utf-8")) if bi.is_file() else None  # 产物对应的提交与构建时刻（判断是否陈旧）
    return {"pass": not r["missing"] and not any(r["counts"].values()), "dist": str(dist), "build_info": build, **r}


# ---------------------------------------------------------------- 基线文件
def load_baseline() -> dict:
    return json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}


def write_baseline(section: str, value: dict) -> None:
    """只改写 baseline.json 的一节（`digests` 为零影响对拍，`resim` 为 resim 帧摘要），其余节与来源说明保留。"""
    b = load_baseline() or {"schema": "awr.d3.regress_gate.baseline.v1"}
    b[section] = value
    BASELINE.write_text(json.dumps(b, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def step_perf(g: dict, out: Path) -> dict:
    p = g["perf"]
    code, dt = _run([PY, "-m", "pytest", "-q", "-m", "perf", *p["pytest"]], ROOT, 3600, log=out / "perf.log")
    return {"pass": code == 0, "exit": code, "seconds": dt}


BUILTIN = {"zero-impact": step_zero_impact, "resim": step_resim, "perf": lambda g, out, _rec: step_perf(g, out),
           "k9-d3": lambda g, out, _rec: step_k9_d3(g, out)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--steps", default=None, help="comma separated step ids (default: all default steps)")
    ap.add_argument("--record-baseline", action="store_true", help="write the zero-impact digests as the D2 baseline")
    ap.add_argument("--out", default=None)
    ap.add_argument("--gate", default=None, help="alternative gate.json (default tests/d3/regress_gate/gate.json)")
    ap.add_argument("--source-label", default=None, help="with --record-baseline: label of the recorded tree (e.g. d2@928c57b)")
    ap.add_argument("--resim-worker", default=None, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    SOURCE_LABEL[0] = a.source_label
    try:
        g = load_gate(Path(a.gate) if a.gate else GATE)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    if a.resim_worker:
        d = Path(a.resim_worker)
        s = resim_worker(d, g["resim"])
        (d / "resim.json").write_text(json.dumps(s, indent=1) + "\n", encoding="utf-8")
        print(json.dumps({k: s[k] for k in ("frames", "frames_sha256", "inputs", "inputs_sha256", "kernel", "awr_path")}))
        return 0
    try:
        steps = select_steps(g, a.steps.split(",") if a.steps else None)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    out = Path(a.out) if a.out else ROOT / "runs" / "perf" / f"d3-gate-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    res = {}
    for s in steps:
        print(f"[d3-gate] {s['id']} ...", flush=True)
        r = BUILTIN[s["id"]](g, out, a.record_baseline) if s["kind"] == "builtin" else step_cmd(s, out)
        r["item"] = s["item"]
        res[s["id"]] = r
        print(f"[d3-gate] {s['id']}: {'PASS' if r['pass'] else 'FAIL'}", flush=True)
    rep = {"schema": "awr.d3.regress_gate.report.v1", "utc": stamp, "steps": res, "pass": all(r["pass"] for r in res.values())}
    (out / "report.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[d3-gate] report {out / 'report.json'}: {'PASS' if rep['pass'] else 'FAIL'}")
    return 0 if rep["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
