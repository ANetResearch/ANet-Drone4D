#!/usr/bin/env python3
"""tools/bench/sandbox/relay_batch.py：7×24 h 接力的批处理验收（WP-13，M16；D2-AC-19、D2-AC-20、D2-AC-35 的批处理部分、D2-AC-32；
M20-AC-019 至 024、031、033；AWR-04 §9、ADR-105、ADR-106）。

每个用例在独立子进程中以 M08 批处理（`awr.sim.runtime.batch.run_batch`，sandbox100，D1 + D2 全部插件 + 本目录的 `bench_probe`）运行，
结束后读取 `relay_stats.json`（M20 `awr.relay.stats.v1` + ext）、`events.jsonl` 与运行中探测的 dry-run 与可持续性，按 AWR-04 §13
的判据逐项判定并与事件流对拍。

| 用例 | 内容（synthcity；识别物用类别缺省模板，人敏感体 300 m） | 判据 |
|---|---|---|
| `a` | 静止的人；单机巢（机巢箱，4 个 6S 通道、6 块电池 = 机上 3 + 备用 3）、3 架 P600（挂 `nir_ill_l`）；`relay_watch` | D2-AC-19 (a) |
| `b` | 随机游走的人（1.2 m/s，60 m 见方区域），其余同 a | D2-AC-19 (b) |
| `c` | physical 模式的狗（夜间不可行），3 架 P600 只配不挂照明器 | D2-AC-19 (c) |
| `t5` | 区域值守与接力：`scenarios/sandbox/<world>/t5_area_guard.json`（D2-AC-35：synthcity 24 h、sanfrancisco 2 h） | D2-AC-35 批处理部分 |

日历：缺省从当地 12:00 开始（24 h 内含日落与日出两次昼夜切换）；晴（`clear`，MOR 30 km ≥ 10 km），a、b 在 21:00–24:00 注入
3 km 霾（`bench_probe` 的 `env/set`）。`--hours 168` 即 D2-AC-20 的 7×24 h；`--chargers 3` 为"人为减少一个充电通道"的对照
（可持续性评估应给出瓶颈 `charger`，批处理出现 `battery` 原因的派遣延误）。

D2-AC-19 (a)(b) 判据：间隙 0（豁免：`keepout_priority` ≤ 3 次且单次 ≤ 30 s；`illum_transition` 每次昼夜切换 ≤ 1 次且 ≤ 30 s），
被发现 0、进入配置体 0、能量 RTL 0、碰撞 0、交接重叠 ≥ 15 s、无 `battery` 原因的派遣延误；统计与事件流一致。
(c)：dry-run 不可持续（可持续性"否"或 581）并给出不可行时段；被发现 0；间隙全部为 `infeasible`，总时长与预测偏差 ≤ 5%。
D2-AC-20：(a)(b) 7 天同样通过，且 dry-run 的可持续性为"是"（与批处理结果一致）。

用法：
    python tools/bench/sandbox/relay_batch.py --ac 19                  # a、b、c 各 24 h（约 3 h 墙钟；并行 3）
    python tools/bench/sandbox/relay_batch.py --ac 20                  # a、b 各 168 h（约 20 h 墙钟，夜间任务）
    python tools/bench/sandbox/relay_batch.py --ac 35                  # T5：synthcity 24 h、sanfrancisco 2 h
    python tools/bench/sandbox/relay_batch.py --case a --hours 1 --no-lock --no-load-wait   # 功能冒烟
    python tools/bench/sandbox/relay_batch.py --case a --chargers 3 --hours 24              # D2-AC-20 瓶颈对照
输出 `runs/perf/relay-<UTC>/<用例>/`（`session.json`、`relay_stats.json`、`events.jsonl`、`summary.json`、`probe.json`）与
`report.json`（`awr.bench.relay.v1`）；退出码 0 通过、1 不通过、14 环境不满足。按 ADR-033 运行协议（排他锁、负载 ≤ 4）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "tools" / "sandbox"))
sys.path.insert(0, str(ROOT / "tools" / "bench" / "ipc"))
import _common as PERF  # noqa: E402
import sbcommon as C  # noqa: E402

SCHEMA = "awr.bench.relay.v1"
EXIT_OK, EXIT_FAIL, EXIT_ENV = 0, 1, 14

# synthcity 的接力几何：机巢取锚点 nest_box（槽位高度已校验），识别物在南侧开阔带，距机巢约 460 m（> 禁入半径 320 m，夜间
# nir_ill_l 窗口 320–455 m 内有视线可见的站位；搜索过程见 WP-13 报告 §3）
RELAY_TARGET = (360.0, -520.0)
RELAY_WALK = [[330.0, -550.0], [390.0, -550.0], [390.0, -490.0], [330.0, -490.0]]
HAZE_PATCH = {"atmosphere": {"mor_bg_m": 3000}}
CLEAR_PATCH = {"atmosphere": {"mor_bg_m": 30000}}
GAP_EXEMPT_KEEPOUT_N, GAP_EXEMPT_KEEPOUT_S = 3, 30.0
GAP_EXEMPT_ILLUM_S = 30.0
OVERLAP_MIN_S = 15.0
INFEASIBLE_TOL = 0.05


# ---------------------------------------------------------------- 配置
def calendar_local(day: str, start_local: str, tz: str) -> dict[str, str]:
    y, m, d = (int(x) for x in day.split("-"))
    hh, mm = (int(x) for x in start_local.split(":"))
    t = datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(tz))
    return {"start_utc": t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "tz": tz}


def relay_config(case: str, *, chargers: int = 4, packs_total: int = 6, n_p600: int = 3, seed: int = 11, day: str = "2026-10-05",
                 start_local: str = "12:00") -> dict[str, Any]:
    """a、b、c 的会话配置（`awr.sandbox.snapshot.v1`）。"""
    a = C.load_json(C.ANCHORS / "synthcity.json")
    nest = a["nest_box"]
    assert C.dist(nest, RELAY_TARGET) >= 420.0, "relay target too close to the nest"
    nir = case in ("a", "b")
    vehicles = []
    for k in range(1, n_p600 + 1):
        v: dict[str, Any] = {"id": f"p600-0{k}", "model_id": "p600_mid360", "nest_id": "nest-a", "label": f"P600 {k}"}
        if nir:
            v["sensors"] = [{"name": "nir_ill", "enabled": True}]
        vehicles.append(v)
    if case == "a":
        tgt = {"id": "person-01", "class": "person", "label": "静止的人", "pos": [*RELAY_TARGET, None], "yaw_rad": 1.571,
               "motion": {"mode": "static"}}
    elif case == "b":
        tgt = {"id": "person-01", "class": "person", "label": "游走的人", "pos": [*RELAY_TARGET, None],
               "motion": {"mode": "random_walk", "speed_mps": 1.2, "region": RELAY_WALK}}
    elif case == "c":
        tgt = {"id": "dog-01", "class": "dog", "label": "游走的狗", "pos": [*RELAY_TARGET, None], "criterion_mode": "physical",
               "motion": {"mode": "random_walk", "region": RELAY_WALK}}
    else:
        raise ValueError(case)
    return {
        "schema": "awr.sandbox.snapshot.v1", "contracts": C.CONTRACTS_VERSION, "world_id": "synthcity", "template_id": f"relay-{case}",
        "calendar": calendar_local(day, start_local, "Asia/Shanghai"), "acoustic_bg": "suburban", "seed": seed,
        "env": {"preset": "clear", "overrides": {}},
        "nests": [{"id": "nest-a", "name": "机巢箱 A", "pos": list(nest), "kind": "box", "slots": max(4, n_p600),
                   "energy": {"mode": "swap", "swap_s": 180, "packs": [{"cells": 6, "count": max(packs_total - n_p600, 0), "capacity_ah": 10}]},
                   "chargers": [{"model": "c1_xr", "count": chargers, "channels": 1, "i_max_a": 10, "cells_max": 6}],
                   "turnaround_s": 60, "launch_interval_s": 30, "link_range_m": 3500}],
        "vehicles": vehicles, "targets": [tgt],
        "tasks": [{"type": "relay_watch", "label": "接力监控", "params": {"target": tgt["id"], "level_req": "R"},
                   "common": {"priority": "high", "on_detect": "notify"}}],
    }


def t5_config(world: str, *, seed: int = 15, day: str = "2026-10-05", start_local: str = "12:00") -> dict[str, Any]:
    from awr.sandbox.templates import build_session_config

    t = C.load_json(C.SANDBOX / world / f"{C.T5}.json")
    cfg = build_session_config(world, template_id=C.T5, template=t, snapshot=None, calendar=calendar_local(day, start_local, C.TZ[world]),
                               acoustic_bg=None, seed=seed, limits={"vehicles_max": 12, "targets_max": 30, "nests_max": 4,
                                                                     "tasks_active_max": 6}, tz=C.TZ[world])
    cfg["contracts"] = C.CONTRACTS_VERSION
    cfg.pop("limits", None)
    return cfg


def haze_schedule(start_local: str, haze_from: str = "21:00", haze_to: str = "24:00") -> list[dict]:
    """当地时刻 → 自开始起的仿真秒（跨午夜按次日计）。"""
    def sec(hm: str) -> float:
        h, m = (int(x) for x in hm.split(":"))
        return h * 3600.0 + m * 60.0

    s0 = sec(start_local)
    t0, t1 = (sec(haze_from) - s0) % 86400.0, (sec(haze_to) - s0) % 86400.0 or 86400.0
    return [{"at_s": t0, "patch": HAZE_PATCH, "mode": "smooth", "duration_s": 600.0},
            {"at_s": t1, "patch": CLEAR_PATCH, "mode": "smooth", "duration_s": 600.0}]


# ---------------------------------------------------------------- 运行
def worker(cfg_path: Path, until_s: float, out: Path) -> int:
    """子进程入口：批处理运行（插件登记表与父进程隔离）。"""
    sys.path.insert(0, str(HERE))
    from awr.sim.runtime.batch import run_batch

    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    s = run_batch(until_s, cfg, seed=int(cfg.get("seed", 0)), out=out, clock="sandbox100", world=cfg.get("world_id"),
                  plugins=(*C.SANDBOX_PLUGINS, "bench_probe"), progress_s=3600.0)
    return int(s.get("exit_code", 1))


def run_case(name: str, cfg: dict, until_s: float, out: Path, *, schedule: list | None = None, probe_at_s: float = 30.0,
             horizon_h: int = 24, timeout_s: float | None = None) -> dict[str, Any]:
    d = out / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "session.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    env = dict(os.environ, AWR_CLOCK_PROFILE="sandbox100", PYTHONUNBUFFERED="1",
               PYTHONPATH=os.pathsep.join([str(HERE), str(ROOT / "python"), os.environ.get("PYTHONPATH", "")]),
               AWR_BENCH_ENV_SCHEDULE=json.dumps(schedule or []), AWR_BENCH_PROBE_AT_S=str(probe_at_s),
               AWR_BENCH_PROBE_OUT=str(d / "probe.json"), AWR_BENCH_PROBE_HORIZON_H=str(horizon_h))
    for k in ("AWR_SCENARIO", "AWR_SESSION_CONFIG", "AWR_SUPERVISOR_PID", "AWR_PLUGINS"):
        env.pop(k, None)
    t0 = time.monotonic()
    cmd = [sys.executable, str(Path(__file__).resolve()), "--worker", str(d / "session.json"), "--until", str(until_s), "--out", str(d)]
    p = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout_s or max(600.0, until_s / 2.0))
    (d / "worker.log").write_text((p.stdout + p.stderr)[-500_000:], encoding="utf-8")
    res: dict[str, Any] = {"case": name, "rc": p.returncode, "wall_s": round(time.monotonic() - t0, 1), "dir": str(d)}
    try:
        res["summary"] = json.loads((d / "summary.json").read_text(encoding="utf-8"))
        res["relay_stats"] = json.loads((d / "relay_stats.json").read_text(encoding="utf-8"))
        res["events"] = [json.loads(x) for x in (d / "events.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    except (OSError, ValueError) as e:
        res["error"] = f"batch outputs missing: {e}; {p.stderr[-800:]}"
    try:
        res["probe"] = json.loads((d / "probe.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        res["probe"] = None
    return res


# ---------------------------------------------------------------- 判定（纯函数，单测覆盖）
def _ev(events: list[dict], kind: str) -> list[dict]:
    return [e for e in events if e.get("kind") == kind]


def band_switches(events: list[dict]) -> int:
    """照度档（day、low、night）在昼与夜之间的切换次数（M07 关键帧的 `config.sun.band`）。"""
    seq = []
    for e in _ev(events, "env.keyframe"):
        b = (((e.get("data") or {}).get("config") or {}).get("sun") or {}).get("band")
        if b in ("day", "night") and (not seq or seq[-1] != b):
            seq.append(b)
    return max(0, len(seq) - 1)


def check(name: str, ok: bool, value: Any = None, expect: Any = None) -> dict:
    return {"check": name, "ok": bool(ok), "value": value, "expect": expect}


def consistency(item: dict, events: list[dict], tid: str) -> list[dict]:
    """统计与事件流对拍：间隙（按原因）、交接次数、被发现次数。"""
    gaps = [e for e in _ev(events, "relay.gap") if (e.get("data") or {}).get("tid") == tid]
    by: dict[str, int] = {}
    for g in gaps:
        c = str((g.get("data") or {}).get("cause"))
        by[c] = by.get(c, 0) + 1
    st = {k: v for k, v in ((item.get("gaps") or {}).get("by_cause") or {}).items() if v}
    hov = [e for e in _ev(events, "relay.handover") if (e.get("data") or {}).get("tid") == tid]
    disc = [e for e in _ev(events, "target.discovered") if (e.get("data") or {}).get("target") == item.get("target")
            and (e.get("data") or {}).get("uav")]
    return [check("stats_vs_events.gaps_by_cause", by == st, by, st),
            check("stats_vs_events.handovers", len(hov) == int((item.get("handovers") or {}).get("count", 0)), len(hov),
                  (item.get("handovers") or {}).get("count")),
            check("stats_vs_events.discovered", len(disc) == int(item.get("discovered", 0)), len(disc), item.get("discovered"))]


def evaluate_ab(res: dict, *, expect_sustainable: bool | None = None) -> list[dict]:
    """D2-AC-19 (a)(b)（D2-AC-20 同一判据，另比较 dry-run 的可持续性）。"""
    out: list[dict] = []
    items = (res.get("relay_stats") or {}).get("items") or []
    if not items:
        return [check("relay_stats.items", False, 0, ">= 1")]
    item = items[0]
    ext = item.get("ext") or {}
    events = res.get("events") or []
    tid = item["tid"]
    gaps = [e.get("data") or {} for e in _ev(events, "relay.gap") if (e.get("data") or {}).get("tid") == tid]
    ko = [g for g in gaps if g.get("cause") == "keepout_priority"]
    il = [g for g in gaps if g.get("cause") == "illum_transition"]
    other = [g for g in gaps if g.get("cause") not in ("keepout_priority", "illum_transition")]
    n_sw = band_switches(events)
    out += [check("summary.exit_code", (res.get("summary") or {}).get("exit_code") == 0, (res.get("summary") or {}).get("exit_code"), 0),
            check("summary.config_problems", not (res.get("summary") or {}).get("config_problems"),
                  (res.get("summary") or {}).get("config_problems"), []),
            check("first_capture", ext.get("first_capture_s") is not None, ext.get("first_capture_s"), "not null"),
            check("gaps.non_exempt", not other, [(g.get("cause"), g.get("duration_s")) for g in other], []),
            check("gaps.keepout_priority", len(ko) <= GAP_EXEMPT_KEEPOUT_N and all(float(g.get("duration_s", 0)) <= GAP_EXEMPT_KEEPOUT_S for g in ko),
                  [g.get("duration_s") for g in ko], f"<= {GAP_EXEMPT_KEEPOUT_N} x <= {GAP_EXEMPT_KEEPOUT_S} s"),
            check("gaps.illum_transition", len(il) <= max(n_sw, 0) and all(float(g.get("duration_s", 0)) <= GAP_EXEMPT_ILLUM_S for g in il),
                  [g.get("duration_s") for g in il], f"<= {n_sw} (one per day/night switch) x <= {GAP_EXEMPT_ILLUM_S} s"),
            check("discovered", int(item.get("discovered", 0)) == 0, item.get("discovered"), 0),
            check("config_entries", int((item.get("config_entries") or {}).get("count", 0)) == 0, item.get("config_entries"), 0),
            check("energy_rtl", int(item.get("energy_rtl", 0)) == 0, item.get("energy_rtl"), 0),
            check("collisions", not [e for e in events if e.get("kind") == "sim.contact.collision"],
                  len([e for e in events if e.get("kind") == "sim.contact.collision"]), 0),
            check("handover.overlap_min", (item.get("handovers") or {}).get("count", 0) == 0
                  or float((item.get("handovers") or {}).get("min_overlap_s") or 0.0) >= OVERLAP_MIN_S,
                  (item.get("handovers") or {}).get("min_overlap_s"), f">= {OVERLAP_MIN_S} s"),
            check("dispatch_delays.battery", int((item.get("dispatch_delays") or {}).get("battery", 0)) == 0,
                  (item.get("dispatch_delays") or {}).get("battery"), 0),
            check("day_night_switches", n_sw >= 2 or (res.get("summary") or {}).get("t_sim_s", 0) < 86400.0, n_sw, ">= 2 in 24 h")]
    out += consistency(item, events, tid)
    if expect_sustainable is not None:
        v = ((res.get("probe") or {}).get("sustainability") or {}).get("verdict")
        out.append(check("dry_run.sustainability", (v == "yes") == expect_sustainable, v, "yes" if expect_sustainable else "no"))
    return out


def predicted_infeasible_s(probe: dict | None, calendar: dict, t_end_s: float) -> float | None:
    """可持续性评估逐小时窗口中不可行部分在 [0, t_end] 内的总时长（仿真秒）。"""
    s = (probe or {}).get("sustainability") or {}
    wins = s.get("windows")
    if not wins:
        return None
    t0 = datetime.strptime(calendar["start_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
    tot = 0.0
    for w in wins:
        if w.get("feasible"):
            continue
        a = int(w["t_from_unix_ns"]) / 1e9 - t0
        b = int(w["t_to_unix_ns"]) / 1e9 - t0
        tot += max(0.0, min(b, t_end_s) - max(a, 0.0))
    return tot


def evaluate_c(res: dict, calendar: dict) -> list[dict]:
    """D2-AC-19 (c)：dry-run 不可持续并给出不可行时段；被发现 0；间隙全部 infeasible，总时长与预测偏差 ≤ 5%。"""
    out: list[dict] = []
    items = (res.get("relay_stats") or {}).get("items") or []
    probe = res.get("probe") or {}
    sus = probe.get("sustainability") or {}
    dry = probe.get("dry_run") or {}
    codes = [x.get("code") for x in ((dry.get("item") or {}).get("infeasible") or [])]
    out.append(check("dry_run.not_sustainable", sus.get("verdict") == "no" or 581 in codes, {"verdict": sus.get("verdict"), "codes": codes},
                     "no or 581"))
    out.append(check("dry_run.infeasible_windows", any(not w.get("feasible") for w in sus.get("windows") or []),
                     sum(1 for w in sus.get("windows") or [] if not w.get("feasible")), ">= 1"))
    if not items:
        out.append(check("relay_stats.items", True, 0, "task rejected at creation (581) is acceptable"))
        return out
    item = items[0]
    events = res.get("events") or []
    out.append(check("discovered", int(item.get("discovered", 0)) == 0, item.get("discovered"), 0))
    by = (item.get("gaps") or {}).get("by_cause") or {}
    out.append(check("gaps.only_infeasible", all(v == 0 for k, v in by.items() if k != "infeasible"), by, "infeasible only"))
    t_end = float((res.get("summary") or {}).get("t_sim_s") or 0.0)
    pred = predicted_infeasible_s(probe, calendar, t_end)
    got = float(((item.get("gaps") or {}).get("total_s_by_cause") or {}).get("infeasible", 0.0))
    ok = pred is not None and (abs(got - pred) <= INFEASIBLE_TOL * pred if pred > 0 else got == 0.0)
    out.append(check("infeasible_total_vs_prediction", ok, {"batch_s": round(got, 1), "predicted_s": None if pred is None else round(pred, 1)},
                     f"|delta| <= {INFEASIBLE_TOL:.0%}"))
    out += consistency(item, events, item["tid"])
    return out


def evaluate_t5(res: dict) -> list[dict]:
    """D2-AC-35 批处理部分：被发现 0、间隙按 D2-AC-19 口径；巡查发现后自动转入接力（存在接力子任务的统计）。"""
    items = (res.get("relay_stats") or {}).get("items") or []
    out = [check("relay_spawned", bool(items), len(items), ">= 1 relay child task")]
    for it in items:
        sub = evaluate_ab({**res, "relay_stats": {"items": [it]}})
        out += [dict(c, check=f"{it['target']}.{c['check']}") for c in sub if not c["check"].startswith("first_capture")]
    disc = [e for e in _ev(res.get("events") or [], "target.discovered") if (e.get("data") or {}).get("uav")]
    out.append(check("discovered_any", not disc, len(disc), 0))
    return out


# ---------------------------------------------------------------- 入口
def plan(args: argparse.Namespace) -> list[tuple[str, dict, float, dict]]:
    """[(名称, 配置, 仿真秒, 选项)]"""
    out = []
    hours = args.hours
    sched = haze_schedule(args.start_local)
    for c in args.case:
        if c in ("a", "b", "c"):
            cfg = relay_config(c, chargers=args.chargers, packs_total=args.packs, seed=args.seed, day=args.day, start_local=args.start_local)
            opt = {"schedule": sched if c in ("a", "b") else [], "horizon_h": int(min(max(hours, 24), 168)), "kind": c}
            suffix = f"-ch{args.chargers}" if args.chargers != 4 else ""
            out.append((f"relay-{c}{suffix}-{hours:g}h", cfg, hours * 3600.0, opt))
        elif c == "t5":
            for w in args.world or ["synthcity"]:
                cfg = t5_config(w, seed=args.seed, day=args.day, start_local=args.start_local)
                out.append((f"t5-{w}-{hours:g}h", cfg, hours * 3600.0, {"schedule": [], "horizon_h": 24, "kind": "t5"}))
    return out


def preset(ac: str) -> list[list[str]]:
    return {"19": [["--case", "a", "--case", "b", "--case", "c", "--hours", "24"]],
            "20": [["--case", "a", "--case", "b", "--hours", "168"], ["--case", "a", "--chargers", "3", "--hours", "24"]],
            "35": [["--case", "t5", "--world", "synthcity", "--hours", "24"], ["--case", "t5", "--world", "sanfrancisco", "--hours", "2"]]}[ac]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tools/bench/sandbox/relay_batch.py")
    ap.add_argument("--worker", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--until", type=float, default=None, help=argparse.SUPPRESS)
    ap.add_argument("--ac", choices=("19", "20", "35"))
    ap.add_argument("--case", action="append", choices=("a", "b", "c", "t5"))
    ap.add_argument("--world", action="append", choices=C.WORLDS)
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--chargers", type=int, default=4)
    ap.add_argument("--packs", type=int, default=6)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--day", default="2026-10-05")
    ap.add_argument("--start-local", default="12:00")
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--out", default=None)
    ap.add_argument("--determinism", action="store_true", help="每个用例运行两次，比较 relay_stats.json 与 events.jsonl 逐字节一致（D2-AC-32）")
    ap.add_argument("--no-lock", action="store_true")
    ap.add_argument("--no-load-wait", action="store_true")
    a = ap.parse_args(argv)
    if a.worker:
        return worker(Path(a.worker), float(a.until), Path(a.out))
    if a.ac:
        rc = EXIT_OK
        for extra in preset(a.ac):
            rc = max(rc, main([*extra, *(["--out", a.out] if a.out else []), *(["--no-lock"] if a.no_lock else []),
                               *(["--no-load-wait"] if a.no_load_wait else []), "--jobs", str(a.jobs)]))
        return rc
    if not a.case:
        ap.error("--case or --ac required")
    if not (ROOT / "worlds" / "synthcity" / "world.json").exists():
        print("worlds/synthcity not built", file=sys.stderr)
        return EXIT_ENV
    lock = PERF.acquire_perf_lock(a.no_lock)
    if not PERF.wait_load(a.no_load_wait):
        return EXIT_ENV
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = Path(a.out) if a.out else ROOT / "runs" / "perf" / f"relay-{ts}"
    out.mkdir(parents=True, exist_ok=True)
    jobs = plan(a)
    if a.determinism:
        jobs = [(f"{n}-r{k}", c, u, o) for n, c, u, o in jobs for k in (1, 2)]
    sampler = PERF.LoadSampler()
    with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as ex:
        results = list(ex.map(lambda j: (j, run_case(j[0], j[1], j[2], out, schedule=j[3]["schedule"], horizon_h=j[3]["horizon_h"])), jobs))
    load = sampler.stop()
    report: dict[str, Any] = {"schema": SCHEMA, "run_id": f"relay-{ts}", "git_sha": PERF.git_sha(), "host": PERF.host_info(), "load": load,
                              "cases": []}
    ok_all = True
    for (name, cfg, until_s, opt), res in results:
        if res.get("error"):
            checks = [check("batch", False, res["error"], "outputs")]
        elif opt["kind"] in ("a", "b"):
            expect = None if until_s < 24 * 3600 else (a.chargers >= 4)
            checks = evaluate_ab(res, expect_sustainable=expect)
            if a.chargers < 4:
                bn = [b.get("code") for b in ((res.get("probe") or {}).get("sustainability") or {}).get("bottlenecks") or []]
                checks.append(check("dry_run.bottleneck_charger", "charger" in bn, bn, "charger"))
                checks = [c for c in checks if c["check"] != "dispatch_delays.battery"] + [
                    check("dispatch_delays.battery_present", any((it.get("dispatch_delays") or {}).get("battery", 0) > 0
                                                                 for it in (res.get("relay_stats") or {}).get("items") or []),
                          None, "> 0")]
        elif opt["kind"] == "c":
            checks = evaluate_c(res, cfg["calendar"])
        else:
            checks = evaluate_t5(res)
        ok = all(c["ok"] for c in checks)
        ok_all = ok_all and ok
        s = res.get("summary") or {}
        report["cases"].append({"case": name, "ok": ok, "t_sim_s": s.get("t_sim_s"), "wall_s": res.get("wall_s"), "rtf": s.get("rtf_avg"),
                                "checks": checks, "relay_stats": (res.get("relay_stats") or {}).get("items"), "probe": res.get("probe"),
                                "dir": res.get("dir")})
        print(f"{name:28s} {'PASS' if ok else 'FAIL'}  t_sim {s.get('t_sim_s')} s  wall {res.get('wall_s')} s  rtf {s.get('rtf_avg')}")
        for c in checks:
            if not c["ok"]:
                print(f"    FAIL {c['check']}: {json.dumps(c['value'], ensure_ascii=False, default=str)[:200]} (expect {c['expect']})")
    if a.determinism:
        for name, _c, _u, _o in jobs:
            if not name.endswith("-r1"):
                continue
            d1, d2 = out / name, out / (name[:-3] + "-r2")
            same = all((d1 / f).exists() and (d1 / f).read_bytes() == (d2 / f).read_bytes() for f in ("relay_stats.json", "events.jsonl"))
            report["cases"].append({"case": name[:-3] + "-determinism", "ok": same,
                                    "checks": [check("byte_identical", same, None, "relay_stats.json, events.jsonl")]})
            ok_all = ok_all and same
            print(f"{name[:-3] + '-determinism':28s} {'PASS' if same else 'FAIL'}")
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    print(f"report: {out / 'report.json'}")
    del lock
    return EXIT_OK if ok_all else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
