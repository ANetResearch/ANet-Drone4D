#!/usr/bin/env python3
"""tools/bench/sandbox/power.py：节能模式的服务端节省实测（WP-13b，M16；D2-AC-46（P1），并给出 D2-AC-45 的"沙盒组内存回落到
创建前 + 10 MB 以内"；AWR-04 §15.7.8 预期节省）。

cgroup 模拟（与 `capacity.py` 相同的 `_sbbench.Stack`：单元 CPUQuota 250%、MemoryHigh 1.8 GB、4 核 cpuset、§4.4.4 子组）下：

| 段 | 做法 | 判据 |
|---|---|---|
| 沙盒驻留 | 等控制面启动期分配稳定（`settle`）后记录 `sandbox` 子组的基线（无会话）→ 建 1 个满载会话（`capacity_config`：10 架 + 30 识别物 + 3 个任务，×1）预热 `--warm` s 后测 `--seconds` s → SB-69 驻留，PARKED 后测 `--seconds` s | 沙盒组 CPU 下降 ≥ 0.12 核；匿名页 + shmem 下降 ≥ 120 MB；驻留后 ≤ 基线 + 10 MB（D2-AC-45） |
| viewer 节能 | 共享展示（`--show-scenario`，缺省 S7，与 emax 的 public profile 一致）无 viewer 时测 `--seconds` s（基线）→ `--viewers` 个实时观看连接（`--viewer-profile`，缺省 browser：共享展示页默认视图的真实浏览器订阅集与收发节律，桌面 Tier B，见 `capacity.BROWSER_SUBS`）测 `--seconds` s → 全部以 1000 断开（节能）后测 `--seconds` s | ADR-174：节能后 show-api CPU ≤ 基线 + 0.01 核；下降量 ≥ 90% 的 viewer 增量（连接时 − 基线）；每 viewer 增量 ≤ 0.02 核（§4.4.1 预算）；测量有效性：每个 viewer 收到的 BATCH ≥ 0.75 × swarm 订阅频率 |

viewer 段的第 1 轮口径（只握手不订阅，共享展示不是 S7，判据"下降 ≥ 0.3 核"）低估了真实 viewer 的成本，判据取自预算"每 viewer 0.02 核"，
本机不可达；ADR-174（D2-FX-R2-sandbox 第 2 次执行）改为上述口径与判据，下降量与每 viewer 成本的绝对值照常记录，emax 实测在 MS5 记入交付总结。
`--viewer-profile hello` 复现第 1 轮口径（只作对照，按 ADR-174 判为测量无效）。

浏览器一侧（节能 60 s 内渲染进程主线程 ≤ 1% 单核）在 `apps/web/perf/d21/power-live.spec.ts`（`M16_PERF=1`）以 Chromium 性能指标测量。

用法：
    python tools/bench/sandbox/power.py                      # 排他锁、负载 ≤ 4
    python tools/bench/sandbox/power.py --smoke --seconds 10 --warm 10 --viewers 2   # 功能自检（不判定）
    python tools/bench/sandbox/power.py --part viewers       # 只测 viewer 段（--part sandbox 只测驻留段）
输出 `runs/perf/power-<UTC>/report.json`（`awr.bench.power.v1`）；退出码 0 通过、1 不通过、14 环境不满足。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE.parents[2] / "tools" / "bench" / "ipc", _HERE.parents[2] / "tools" / "sandbox"):
    sys.path.insert(0, str(_p))
import _common as PERF  # noqa: E402
import _sbbench as B  # noqa: E402
import capacity as CAP  # noqa: E402

SCHEMA = "awr.bench.power.v1"
EXIT_OK, EXIT_FAIL, EXIT_ENV = 0, 1, 14
# viewer 段（ADR-174）：节能后的残余（相对无 viewer 基线，核）、释放比例（下降量 / viewer 增量）、每 viewer 增量上界（§4.4.1 预算）、
# 测量有效性（每个 viewer 每秒收到的 BATCH 不少于 swarm 订阅频率的该比例，否则 viewer 实际没有订阅，结果不代表真实观看）
GATES = {"sandbox_cpu_drop": 0.12, "sandbox_mem_drop_mb": 120.0, "sandbox_mem_vs_baseline_mb": 10.0,
         "viewer_residual_core": 0.01, "viewer_release_frac": 0.9, "viewer_cost_core": 0.02, "viewer_batch_frac": 0.75}
SHOW_SCENARIO = "s7-synthcity-relay"


def cg_window(stack: B.Stack, seconds: float) -> dict[str, Any] | None:
    """`sandbox` 子组在窗口内的平均 CPU（核）与窗口末的匿名页 + shmem（MB）；无 cgroup 布局时 None。"""
    pid = stack.supervisor_pid()
    root = B.scope_root_of(int(pid)) if pid else None
    if root is None or not (root / "sandbox").exists():
        return None
    a = B.read_cg(root / "sandbox")
    t0 = time.monotonic()
    time.sleep(seconds)
    b = B.read_cg(root / "sandbox")
    dt = time.monotonic() - t0
    cpu = None
    if a.get("usage_usec") is not None and b.get("usage_usec") is not None:
        cpu = round((b["usage_usec"] - a["usage_usec"]) / 1e6 / dt, 4)
    return {"cpu": cpu, "anon_shmem_mb": round((b.get("anon_mb") or 0) + (b.get("shmem_mb") or 0), 2),
            "memory_current_mb": b.get("memory_current_mb")}


def ctl_mem_mb(stack: B.Stack) -> dict[str, float]:
    """沙盒控制面各进程（sandbox-pool、sandbox-api、共享规划 worker）的匿名页 + shmem（MB）：基线与驻留后的逐进程分解。"""
    out: dict[str, float] = {}
    try:
        procs = {p.get("name"): p.get("pid") for p in stack.procs() if p.get("pid")}
    except Exception:
        return out
    sess = set(B.session_pids(stack.tag).values())
    for name in ("sandbox-pool", "sandbox-api"):
        pid = procs.get(name)
        if pid:
            m = B.proc_mem_mb(int(pid))
            out[name] = round((m.get("RssAnon") or 0) + (m.get("RssShmem") or 0), 2)
    pool = procs.get("sandbox-pool")
    if pool:
        for d in Path("/proc").iterdir():
            if not d.name.isdigit() or int(d.name) in sess:
                continue
            with contextlib.suppress(OSError, ValueError, IndexError):
                if int((d / "stat").read_text().rsplit(")", 1)[1].split()[1]) == int(pool):
                    m = B.proc_mem_mb(int(d.name))
                    out[f"worker:{d.name}"] = round((m.get("RssAnon") or 0) + (m.get("RssShmem") or 0), 2)
    return out


def settle(stack: B.Stack, max_s: float = 60.0, tol_mb: float = 0.3, hold_s: int = 5) -> float:
    """等沙盒控制面完成启动期的一次性分配（共享规划 worker 的核预热与模块导入、池与 sandbox-api 的预热；D2-FX-R2）：各进程匿名页 + shmem
    之和连续 `hold_s` 秒变化 ≤ `tol_mb` 即返回（最长 `max_s`）。基线应是空闲稳态，而不是仍在初始化的进程。"""
    t0 = time.monotonic()
    prev, calm = None, 0
    while time.monotonic() - t0 < max_s:
        tot = sum(ctl_mem_mb(stack).values())
        calm = calm + 1 if prev is not None and abs(tot - prev) <= tol_mb else 0
        if calm >= hold_s:
            break
        prev = tot
        time.sleep(1.0)
    return round(time.monotonic() - t0, 1)


def sandbox_part(stack: B.Stack, seconds: float, warm: float) -> dict[str, Any]:
    import httpx

    settle_s = settle(stack)
    ctl_base = ctl_mem_mb(stack)
    base = cg_window(stack, min(seconds, 10.0))
    s = CAP.sandbox(stack, 50).create("synthcity", snapshot=CAP.capacity_config(seed=77))
    s.wait_ready(120)
    s.clock("speed", rate=1)
    rt = s.rt()
    time.sleep(warm)
    on = cg_window(stack, seconds)
    rt.close()
    s._rt = None
    t0 = time.monotonic()
    s.park()
    state = ""
    while time.monotonic() - t0 < 60.0:
        state = str(httpx.get(stack.base + f"/api/sandbox/v1/sessions/{s.sid}", headers={"authorization": "Bearer " + s.token},
                              timeout=10).json().get("state"))
        if state == "PARKED":
            break
        time.sleep(0.25)
    t_park = round(time.monotonic() - t0, 2)
    off = cg_window(stack, seconds)
    ctl_parked = ctl_mem_mb(stack)
    pd = (stack.tmp / "sbx" / "parked" / s.sid) if stack.tmp else None
    disk = sum(f.stat().st_size for f in pd.rglob("*") if f.is_file()) if pd and pd.exists() else None
    with contextlib.suppress(Exception):
        s.close()
    return {"baseline": base, "active": on, "parked": off, "park_s": t_park, "state": state, "park_dir_bytes": disk,
            "settle_s": settle_s, "ctl_baseline_mb": ctl_base, "ctl_parked_mb": ctl_parked}


def viewer_part(stack: B.Stack, seconds: float, n: int, profile: str = "browser") -> dict[str, Any]:
    """无 viewer 的基线 → n 个 viewer（`profile`）连接 → 全部以 1000 断开（节能，D2-AC-44 的客户端行为）后，各测 show-api CPU。"""
    none = CAP.proc_cpu_window(stack, "api", seconds)
    vw = CAP.Viewers(stack, n, profile).start()
    time.sleep(max(5.0, 0.2 * n + 5.0))
    b0, t0 = vw.batches, time.monotonic()
    on = CAP.proc_cpu_window(stack, "api", seconds)
    batch_hz = round((vw.batches - b0) / max(1e-6, time.monotonic() - t0) / max(1, n), 2)
    r = vw.stop()
    time.sleep(3.0)
    off = CAP.proc_cpu_window(stack, "api", seconds)
    out: dict[str, Any] = {"viewers": n, "profile": profile, "swarm_hz": CAP.VIEWER_PROFILES[profile], "connected": r,
                           "batch_hz_per_viewer": batch_hz, "api_cpu_none": none, "api_cpu_on": on, "api_cpu_off": off,
                           "drop": None if on is None or off is None else round(on - off, 4)}
    if none is not None and on is not None and off is not None:
        inc = on - none
        out.update(increment=round(inc, 4), residual=round(off - none, 4), per_viewer=round(inc / max(1, n), 5),
                   release_frac=round((on - off) / inc, 3) if inc > 0 else None)
    return out


def check(name: str, ok: bool | None, value: Any, expect: Any) -> dict:
    return {"check": name, "ok": ok, "value": value, "expect": expect}


def judge(r: dict, *, cpu_valid: bool) -> list[dict]:
    """`r` 中缺的段（`--part`）不判定。"""
    out: list[dict] = []
    if "sandbox" in r:
        out.extend(judge_sandbox(r.get("sandbox") or {}, cpu_valid=cpu_valid))
    if "viewers" in r:
        out.extend(judge_viewers(r.get("viewers") or {}, cpu_valid=cpu_valid))
    return out


def judge_sandbox(sb: dict, *, cpu_valid: bool) -> list[dict]:
    out: list[dict] = []
    on, off, base = sb.get("active"), sb.get("parked"), sb.get("baseline")
    out.append(check("sandbox.parked", sb.get("state") == "PARKED", sb.get("state"), "PARKED"))
    if on and off and on.get("cpu") is not None and off.get("cpu") is not None:
        drop = round(on["cpu"] - off["cpu"], 4)
        out.append(check("sandbox.cpu_drop", drop >= GATES["sandbox_cpu_drop"] if cpu_valid else None, drop, ">= 0.12 core"))
        mdrop = round(on["anon_shmem_mb"] - off["anon_shmem_mb"], 2)
        out.append(check("sandbox.mem_drop_mb", mdrop >= GATES["sandbox_mem_drop_mb"], mdrop, ">= 120 MB"))
        if base:
            over = round(off["anon_shmem_mb"] - base["anon_shmem_mb"], 2)
            out.append(check("sandbox.mem_vs_baseline_mb", over <= GATES["sandbox_mem_vs_baseline_mb"], over, "<= +10 MB (D2-AC-45)"))
    else:
        out.append(check("sandbox.cgroup", None, None, "cgroup simulation needed (systemd user scope with delegation)"))
    if sb.get("park_dir_bytes") is not None:
        out.append(check("sandbox.park_dir_mb", sb["park_dir_bytes"] <= 8 * 1048576, round(sb["park_dir_bytes"] / 1048576, 2), "<= 8 MB"))
    return out


def judge_viewers(vw: dict, *, cpu_valid: bool) -> list[dict]:
    """viewer 段（ADR-174）。缺无 viewer 基线时不判定；viewer 没有订阅（hello 口径，只作对照）时测量无效、判为不通过。"""
    if vw.get("increment") is None:
        return [check("viewers.baseline", None, vw.get("drop"), "no-viewer baseline needed (ADR-174)")]
    out: list[dict] = []
    swarm = vw.get("swarm_hz")
    if swarm is None:
        out.append(check("viewers.subscribed", False, vw.get("profile"), "browser or browser-s profile (ADR-174)"))
        return out
    need = GATES["viewer_batch_frac"] * swarm
    valid = (vw.get("batch_hz_per_viewer") or 0.0) >= need and not (vw.get("connected") or {}).get("errors")
    out.append(check("viewers.subscribed", valid, vw.get("batch_hz_per_viewer"), f">= {need:.1f} BATCH/s per viewer, 0 errors"))
    judged = cpu_valid and valid
    out.append(check("viewers.residual_core", vw["residual"] <= GATES["viewer_residual_core"] if judged else None, vw["residual"],
                     f"<= {GATES['viewer_residual_core']} core over the no-viewer baseline"))
    rf = vw.get("release_frac")
    out.append(check("viewers.release_frac", (rf is not None and rf >= GATES["viewer_release_frac"]) if judged else None, rf,
                     f">= {GATES['viewer_release_frac']} of the viewer increment"))
    out.append(check("viewers.cost_per_viewer", vw["per_viewer"] <= GATES["viewer_cost_core"] if judged else None, vw["per_viewer"],
                     f"<= {GATES['viewer_cost_core']} core (AWR-04 4.4.1 budget)"))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tools/bench/sandbox/power.py")
    ap.add_argument("--seconds", type=float, default=60.0, help="每个测量窗口（s）")
    ap.add_argument("--warm", type=float, default=60.0, help="满载会话预热（s）")
    ap.add_argument("--viewers", type=int, default=24)
    ap.add_argument("--viewer-profile", default="browser", choices=sorted(CAP.VIEWER_PROFILES),
                    help="browser：桌面 Tier B 浏览器的订阅集与收发节律（ADR-174）；browser-s：Tier S；hello：第 1 轮口径（只作对照）")
    ap.add_argument("--show-scenario", default=SHOW_SCENARIO, help="共享展示剧本（emax 的 public profile 为 S7）")
    ap.add_argument("--part", default="all", choices=("all", "sandbox", "viewers"))
    ap.add_argument("--cpu-quota", type=int, default=250)
    ap.add_argument("--no-cgroup", action="store_true")
    ap.add_argument("--smoke", action="store_true", help="功能自检：不取锁、不等负载、不判定")
    ap.add_argument("--no-lock", action="store_true")
    ap.add_argument("--no-load-wait", action="store_true")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    if not (B.ROOT / "worlds" / "synthcity" / "world.json").exists():
        print("worlds/synthcity not built", file=sys.stderr)
        return EXIT_ENV
    lock = None if a.smoke else PERF.acquire_perf_lock(a.no_lock)
    if not a.smoke and not PERF.wait_load(a.no_load_wait):
        return EXIT_ENV
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = Path(a.out) if a.out else B.ROOT / "runs" / "perf" / f"power-{ts}"
    out.mkdir(parents=True, exist_ok=True)
    stack = B.Stack(cgroup=not a.no_cgroup, cpu_quota_pct=a.cpu_quota, ws_max_show=max(a.viewers, 1), max_sessions=1,
                    show_scenario=a.show_scenario or None)
    load = PERF.LoadSampler()
    r: dict[str, Any] = {"show_scenario": a.show_scenario or None}
    try:
        stack.start()
        if a.part in ("all", "sandbox"):
            r["sandbox"] = sandbox_part(stack, a.seconds, a.warm)
        if a.part in ("all", "viewers"):
            if a.part == "viewers":
                settle(stack)
            r["viewers"] = viewer_part(stack, a.seconds, a.viewers, a.viewer_profile)
    finally:
        stack.stop()
    ld = load.stop()
    cpu_valid = ld["mean"] < 6.0 and not a.smoke
    checks = [] if a.smoke else judge(r, cpu_valid=cpu_valid)
    ok = all(c["ok"] is not False for c in checks)
    report = {"schema": SCHEMA, "run_id": f"power-{ts}", "git_sha": PERF.git_sha(), "host": PERF.host_info(), "gates": GATES,
              "smoke": a.smoke, "cgroup": stack.cgroup_mode, "load": ld, "result": r, "checks": checks, "ok": ok}
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    print(f"power: {'PASS' if ok else 'FAIL'} (cgroup {stack.cgroup_mode}, load mean {ld['mean']})")
    for c in checks:
        if c["ok"] is False:
            print(f"    FAIL {c['check']}: {c['value']} (expect {c['expect']})")
    print(f"report: {out / 'report.json'}")
    del lock
    return EXIT_OK if ok else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
