#!/usr/bin/env python3
"""tools/bench/d3/mbl_e2e.py：最小基准闭环一键脚本（D3-AC-56；WP-D3-30；M27 §6.7.9、§14.5；ADR-161、ADR-175、ADR-182、ADR-191）。

一条命令依次执行 7 个阶段，每个阶段的结果写入检查点 `<out>/e2e_state.json`，同一 `--out` 再次执行即断点续跑：

1. `world`：世界包检查（`awr.world.package.validate.validate_world`，D2 规则 + V-GLB 规则组），error 为 0 才继续；
2. `compile`：事件 strict 编译（`awr.sim.events.compile.compile_event`，`strict = True`；排名运行另按 `ranked` 口径），三道门均不为
   fail 且状态为 compiled；编译产物写入本轮目录 `<out>/events/<event>/v<N>`（不改动共享事件目录），记录磁带 `content_sha256`；
3. `lockstep`：Realistic 档 gz 锁步预检（参考基线、公开冒烟种子 0、`lockstep.preflight_s` 秒、隔离容器 Agent）：物理档为 realistic，
   gz 迭代数 = 每 tick 物理步数 × tick 数，白名单审计通过；
4. `runs`：各观测档组（缺省 L1、L2）× 参考基线（缺省 3 个）× 秘密种子（缺省 10 个）× 重复（缺省 2 次）的正式 Run，经
   `awr.bench.runner.run_job`（隔离容器 + 线协议 + `bench/obs.v1` 逐帧审计）运行并评测；每个 Run 记录 `wall_s`（Env 构造至关闭，
   含 gz 与 Agent 容器启动）、Env 主循环墙钟、仿真时长、墙钟比、锁步核对与 `manifest.json.obs_audit`，另写
   `run_manifest.json`（`bench/run_manifest.v1`，排名运行只含 `seed_commit`）；平台原因失败自动重跑 `platform_retries` 次；
5. `board`：聚合评测并发布原型榜：每个观测档组一个榜单版本 `<board.version>-<l1|l2>`（`config.json` 与 `rows.jsonl` 写入
   `<data_root>/boards/<ver>/`，`awr.bench.boardstore.publish` 重算 `board.json`、`details.json`），3 个基线为参考行；
   `board.current` 所指组设为当前版本（开发榜除外）；
6. `submit`：触发 1 个内部提交：解析镜像摘要，按 `bench/submission.v1` 登记到所选组的榜单版本并设为上榜，再由
   `awr.bench.subworker.process_one` 逐阶段处理（扫描 → 冒烟 → 正式评测 → 评分），DONE 后核对条目出现在 `board.json`；
   `submission.worker = external` 时只登记并等待常驻流水线处理；
7. `report`：汇总报告 `<out>/mbl_e2e.json`（`drone4d.bench.mbl_e2e.v1`）并逐项判定（每次执行都重算）。

秘密种子（M27-FR-005、FR-166）：首次执行以 `secrets.randbits(63)` 生成，与评测密钥 `K_eval` 一起只写入 `<out>/evalstore/secrets.json`
（权限 0600）与服务端榜单配置 `boards/<ver>/config.json`（内部提交流水线读取）；Run 标识、运行目录、日志、检查点、评测行、榜单与
报告中只出现种子序号 k；`seed_commit = HMAC-SHA256(K_eval, run_id ‖ ":" ‖ seed)`，种子集承诺
`seed_set_commit = HMAC-SHA256(K_eval, "seed_set:" ‖ event ‖ ":" ‖ 逗号连接的种子)`。报告阶段在本轮全部文本产物中检索种子的
十进制与十六进制表示，命中须为 0。

断点续跑：检查点记录本轮配置指纹（配置、run_spec、事件源文件的 sha256），指纹不同时拒绝续跑（退出码 3，须换 `--out`）；
已完成的阶段跳过（`--force` 指定的阶段除外，`runs` 与 `report` 每次都执行）；`runs` 阶段逐 Run 续跑：记录为 COMPLETED 且
`evaluation.json` 的 sha256 与记录一致的 Run 跳过，其余（含上次失败的）删除运行目录后重跑；运行前核对本轮事件磁带的
`content_sha256` 与 `compile` 阶段记录一致。已发布榜单版本中内部提交的评测行在重新发布时保留。

配置（YAML 或 JSON，`--config`；缺省值即正式口径，B10 的 `configs/bench.d3/mbl-formal.yaml` 只需写出与缺省不同的键）：

    schema: drone4d.bench.mbl_e2e_config.v1
    round: mbl-p0a                    # 本轮标识：Run 标识前缀（gz 与 Agent 容器名由此派生）、榜单配置中的轮次记录
    world: {id: oryahovo-crop1, deep: false}
    event: {source: configs/events.d3/crop-handoff-001/event.yaml}
    groups:                           # 观测档组：run_spec 与并行 Run 数（jobs 不进入指纹）
      - {obs_level: L1, run_spec: configs/bench.d3/crop-handoff-001.l1-3uav.run_spec.json, jobs: 3}
      - {obs_level: L2, run_spec: configs/bench.d3/crop-handoff-001.l2-3uav.run_spec.json, jobs: 1}
    fleet_vehicle: null               # 非空时覆盖全部 fleet[].vehicle_ref（如 p600_bench）
    baselines: [static_guard, coverage_scan, track_share]
    seeds: 10                         # 秘密种子个数
    repeats: 2
    duration_s: null                  # 非空时覆盖 Run 时长（冒烟；不计正式口径）
    physics: realistic                # kinematic 为开发自检（开发榜，不得设为当前版本）
    split: dev                        # 原型榜单事件、不设 test 划分（§5.6.9）
    agent_image: d3-agent-py:dev
    gz: {launcher: docker, image: d3-gz:dev, export_root: null}
    lockstep: {preflight_s: 20, policy: track_share, group: L1}
    wall_ratio_max: 3.0               # 单个正式 Run 墙钟 ≤ 仿真时长 × 3（共享卡，原型）
    platform_retries: 1
    board: {version: mbl-p0a, current: L2, n_boot: 10000}
    submission: {image: d3-example-close-track:dev, cmd: [python, -m, drone4d.serve, close_track.agent:Agent], env: {},
                 group: L2, team: d3-internal, owner: mbl-e2e, notes: "", smoke_s: 20, worker: inline, timeout_s: 21600}

用法：
    python tools/bench/d3/mbl_e2e.py --config configs/bench.d3/mbl-formal.yaml --out out/mbl-formal-1 --data-root benchroot \\
        [--worlds-root worlds] [--stages world,compile,lockstep,runs,board,submit,report] [--force board,submit] \\
        [--seeds N] [--repeats R] [--duration-s S] [--baselines a,b] [--groups L1,L2] [--physics kinematic] [--plan]
dmax 上经 `tools/deploy/dmax/run_mbl_e2e.sh` 在 d3-sensorsvc 容器内执行（绑定一张卡，gz 与 Agent 为兄弟容器）。
退出码：0 全部阶段完成且全部判定通过；1 有阶段失败或判定不通过；2 配置或参数错误；3 检查点与本次配置不一致（指纹不同）。
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import hmac
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
for _p in (ROOT / "tools/sdk/drone4d/src", ROOT / "python"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

CONFIG_SCHEMA = "drone4d.bench.mbl_e2e_config.v1"
REPORT_SCHEMA = "drone4d.bench.mbl_e2e.v1"
STATE_SCHEMA = "drone4d.bench.mbl_e2e_state.v1"
STAGES = ("world", "compile", "lockstep", "runs", "board", "submit", "report")
ALWAYS = ("runs", "report")                     # 每次执行（逐 Run 续跑；报告重算）
BASELINES = ("static_guard", "coverage_scan", "track_share")
DISPLAY = {"static_guard": "Static guard", "coverage_scan": "Coverage scan", "track_share": "Track and share"}
METRICS = ("ec", "eu", "ao", "ce")
AUDIT_COUNTS = ("schema_checked", "schema_valid", "schema_violations", "unknown_fields", "jpeg_segment_violations", "lidar_tag_violations",
                "decode_violations")
VOLATILE = ("jobs",)                            # 不进入指纹的组内键
TERMINAL = ("DONE", "REJECTED", "FAILED", "WITHDRAWN")

DEFAULTS: dict[str, Any] = {
    "schema": CONFIG_SCHEMA, "round": "mbl-p0a",
    "world": {"id": "oryahovo-crop1", "deep": False},
    "event": {"source": "configs/events.d3/crop-handoff-001/event.yaml"},
    "groups": [{"obs_level": "L1", "run_spec": "configs/bench.d3/crop-handoff-001.l1-3uav.run_spec.json", "jobs": 3},
               {"obs_level": "L2", "run_spec": "configs/bench.d3/crop-handoff-001.l2-3uav.run_spec.json", "jobs": 1}],
    "fleet_vehicle": None, "baselines": list(BASELINES), "seeds": 10, "repeats": 2, "duration_s": None, "physics": "realistic", "split": "dev",
    "agent_image": "d3-agent-py:dev", "gz": {"launcher": "docker", "image": "d3-gz:dev", "export_root": None},
    "lockstep": {"preflight_s": 20.0, "policy": "track_share", "group": "L1"},
    "wall_ratio_max": 3.0, "platform_retries": 1, "lidar_backend": "cpu",
    "board": {"version": "mbl-p0a", "current": "L2", "n_boot": 10_000},
    "submission": {"image": "d3-example-close-track:dev", "cmd": ["python", "-m", "drone4d.serve", "close_track.agent:Agent"], "env": {},
                   "group": "L2", "team": "d3-internal", "owner": "mbl-e2e", "notes": "", "smoke_s": 20.0, "worker": "inline",
                   "timeout_s": 21_600.0, "poll_s": 15.0},
}
_SUBMISSION_VOLATILE = ("worker", "timeout_s", "poll_s")


class E2EError(Exception):
    """阶段失败（写入检查点，后续阶段不执行）。"""


class ConfigError(Exception):
    """配置或参数错误（退出码 2）。"""


# ------------------------------------------------------------------------------------------------------------------ 工具
def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _jcs(x: Any) -> bytes:
    return json.dumps(x, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(p: Path, doc: Any, *, mode: int | None = None) -> None:
    """原子写入（临时文件 + rename）；`mode` 给出时以该权限创建（密钥文件 0600）。"""
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp-{os.getpid()}")
    data = (json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644 if mode is None else mode)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, p)


def read_json(p: Path) -> Any:
    return json.loads(Path(p).read_text(encoding="utf-8"))


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def redact(text: str, seeds: list[int]) -> str:
    """异常与日志文本中的种子明文（十进制、十六进制）替换为 `<seed>`；公开冒烟种子 0 不是秘密，不替换（否则文本中的每个 0 都被改写）。"""
    for s in seeds:
        if not s:
            continue
        for pat in (str(s), f"{s:x}", f"{s:016x}", f"{s:X}"):
            text = text.replace(pat, "<seed>")
    return text


# ------------------------------------------------------------------------------------------------------------------ 配置
def load_config(path: Path | None, overrides: dict | None = None) -> dict:
    """读入配置（YAML 或 JSON）并按缺省值补全、应用命令行覆盖、校验；相对路径按当前目录解析。"""
    doc: dict = {}
    if path is not None:
        txt = Path(path).read_text(encoding="utf-8")
        if Path(path).suffix in (".yaml", ".yml"):
            import yaml

            doc = yaml.safe_load(txt) or {}
        else:
            doc = json.loads(txt)
        if not isinstance(doc, dict):
            raise ConfigError(f"{path}: the configuration must be a mapping")
    groups = doc.pop("groups", None)
    cfg = _merge(DEFAULTS, doc)
    if groups is not None:
        cfg["groups"] = copy.deepcopy(groups)
    for k, v in (overrides or {}).items():
        if v is None:
            continue
        if k == "groups":
            want = [x.upper() for x in v]
            cfg["groups"] = [g for g in cfg["groups"] if g["obs_level"].upper() in want]
            lv = [g["obs_level"] for g in cfg["groups"]]
            if lv:                                                       # 引用了被去掉的组：改指保留的组（锁步取首组，榜单与提交取末组）
                if cfg["lockstep"]["group"] not in lv:
                    cfg["lockstep"]["group"] = lv[0]
                if cfg["board"].get("current") not in (None, *lv):
                    cfg["board"]["current"] = lv[-1]
                if cfg.get("submission") and cfg["submission"]["group"] not in lv:
                    cfg["submission"]["group"] = lv[-1]
        else:
            cfg[k] = v
    _validate(cfg)
    return cfg


def _validate(cfg: dict) -> None:
    if cfg.get("schema") != CONFIG_SCHEMA:
        raise ConfigError(f"schema must be {CONFIG_SCHEMA}")
    if not cfg["groups"]:
        raise ConfigError("no observation-level group selected")
    levels = [g["obs_level"] for g in cfg["groups"]]
    if len(set(levels)) != len(levels):
        raise ConfigError(f"duplicate observation-level groups: {levels}")
    for g in cfg["groups"]:
        if g["obs_level"] not in ("L1", "L2", "L2-depth") or not g.get("run_spec"):
            raise ConfigError(f"group {g}: obs_level must be L1, L2 or L2-depth and run_spec is required")
    bad = [b for b in cfg["baselines"] if b not in BASELINES]
    if bad or not cfg["baselines"]:
        raise ConfigError(f"unknown baselines {bad}; available: {list(BASELINES)}")
    if int(cfg["seeds"]) < 1 or int(cfg["repeats"]) < 1:
        raise ConfigError("seeds and repeats must be >= 1")
    if cfg["physics"] not in ("realistic", "kinematic"):
        raise ConfigError("physics must be realistic or kinematic")
    if cfg["split"] not in ("dev", "test"):
        raise ConfigError("split must be dev or test")
    if cfg["lidar_backend"] not in ("cpu", "warp"):
        raise ConfigError("lidar_backend must be cpu or warp")
    v = str(cfg["round"])
    if not v or not all(c.isalnum() or c in "-." for c in v) or len(v) > 40:
        raise ConfigError("round must be 1-40 characters of [A-Za-z0-9.-]")
    v = str(cfg["board"]["version"])
    if not v or not all(c.isalnum() or c in "-._" for c in v) or len(v) > 56:
        raise ConfigError("board.version must be 1-56 characters of [A-Za-z0-9._-]")
    if cfg["board"].get("current") not in (None, *levels):
        raise ConfigError(f"board.current must name one of the groups {levels}")
    if cfg["lockstep"]["group"] not in levels:
        raise ConfigError(f"lockstep.group must name one of the groups {levels}")
    sub = cfg.get("submission")
    if sub:
        if sub["group"] not in levels:
            raise ConfigError(f"submission.group must name one of the groups {levels}")
        if sub["worker"] not in ("inline", "external"):
            raise ConfigError("submission.worker must be inline or external")
        if not sub.get("cmd"):
            raise ConfigError("submission.cmd is required")


def fingerprint(cfg: dict) -> str:
    """本轮配置指纹：配置（去掉只影响执行方式的键）、各组 run_spec 内容与事件源目录全部文件的 sha256。"""
    c = copy.deepcopy(cfg)
    for g in c["groups"]:
        for k in VOLATILE:
            g.pop(k, None)
        g["run_spec_sha256"] = sha256_file(Path(g["run_spec"]))
    if c.get("submission"):
        for k in _SUBMISSION_VOLATILE:
            c["submission"].pop(k, None)
    src = Path(c["event"]["source"])
    c["event"]["files"] = {p.name: sha256_file(p) for p in sorted(src.parent.iterdir()) if p.is_file()} if src.exists() else {}
    return sha256_bytes(_jcs(c))


def is_formal(cfg: dict) -> bool:
    """正式口径（D3-AC-56）：Realistic 档、L1 与 L2 两组、3 个基线、≥ 10 个秘密种子 × ≥ 2 次重复、不覆盖时长、有内部提交。"""
    return (cfg["physics"] == "realistic" and {g["obs_level"] for g in cfg["groups"]} >= {"L1", "L2"} and set(cfg["baselines"]) == set(BASELINES)
            and int(cfg["seeds"]) >= 10 and int(cfg["repeats"]) >= 2 and cfg["duration_s"] is None and bool(cfg.get("submission")))


# ------------------------------------------------------------------------------------------------------------------ 秘密种子
class EvalStore:
    """本轮的评测密钥与秘密种子集（只存于 `<dir>/secrets.json`，0600）。"""

    def __init__(self, d: Path) -> None:
        self.path = Path(d) / "secrets.json"

    def ensure(self, event: str, n: int) -> None:
        if self.path.exists():
            doc = read_json(self.path)
            if len(doc["seeds"].get(event, [])) != n:
                raise E2EError(f"evalstore holds {len(doc['seeds'].get(event, []))} seeds for {event}, the round needs {n}")
            return
        seeds: list[int] = []
        while len(seeds) < n:                                            # CSPRNG（M27-FR-005），不重复、不为 0（0 为公开冒烟种子）
            s = secrets.randbits(63)
            if s and s not in seeds:
                seeds.append(s)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        write_json(self.path, {"schema": "drone4d.bench.evalstore.v1", "k_eval": secrets.token_hex(32), "seeds": {event: seeds},
                               "created_utc": utc_now()}, mode=0o600)

    def _doc(self) -> dict:
        return read_json(self.path)

    def seeds(self, event: str) -> list[int]:
        return [int(s) for s in self._doc()["seeds"][event]]

    def _k(self) -> bytes:
        return bytes.fromhex(self._doc()["k_eval"])

    def seed_commit(self, run_id: str, seed: int) -> str:
        return hmac.new(self._k(), f"{run_id}:{int(seed)}".encode(), hashlib.sha256).hexdigest()

    def seed_set_commit(self, event: str) -> str:
        s = ",".join(str(x) for x in self.seeds(event))
        return hmac.new(self._k(), f"seed_set:{event}:{s}".encode(), hashlib.sha256).hexdigest()


# ------------------------------------------------------------------------------------------------------------------ 单个 Run
def _hz(rs: dict) -> tuple[int, int]:
    """→ (Env 决策 tick 频率, 物理步频)：tick 固定 100 ms（M26 §6.6.1），物理步频取 `clock_profile.physics_hz`（d1_250 为 250 Hz）。"""
    from awr.agentsdk.env.spec import resolve_clock_profile
    from awr.bench.eval.evaluator import TICK_S

    cp = resolve_clock_profile(rs.get("clock_profile"))
    return round(1.0 / TICK_S), int(cp["physics_hz"])


def _audit(man: dict, r: dict | None) -> dict:
    a = man.get("obs_audit") or (r or {}).get("obs_audit") or {}
    out = {k: int(a.get(k, 0) or 0) for k in AUDIT_COUNTS if k in a}
    out["passed"] = bool(a.get("passed", False)) if a else False
    out["present"] = bool(a)
    return out


def lockstep_check(man: dict, main_hz: int, physics_hz: int) -> dict:
    """gz 锁步核对：物理档 realistic，gz 自报迭代数 = 每 tick 物理步数 × 档内 tick 数（M24-AC-026 的步进不变量；`main_hz` 为 Env tick 频率）。
    档内 tick 数 = 回合 tick 数 + Env 预滚 tick 数（事件零点之前的 `[−t_pre, 0)`，`tier_info.preroll.n_ticks`，M26-FR-004；B10）。"""
    if man.get("physics") != "realistic":
        return {"applicable": False, "physics": man.get("physics")}
    ti = man.get("tier_info") or {}
    n = int(man.get("n_ticks", 0))
    pre = int((ti.get("preroll") or {}).get("n_ticks") or 0)
    steps = physics_hz // main_hz
    it = ti.get("iterations")
    bt = ti.get("ticks")
    ok = (man.get("tier") == "realistic" and it is not None and int(it) == steps * (n + pre) and n > 0
          and (bt is None or int(bt) == n + pre))
    return {"applicable": True, "ok": bool(ok), "tier": man.get("tier"), "n_ticks": n, "preroll_ticks": pre, "steps_per_tick": steps,
            "iterations": it, "backend_ticks": bt, "restarts": int(ti.get("restarts", 0) or 0), "gz_rtf_mean": ti.get("gz_rtf_mean")}


def execute_run(job: dict, run_job: Callable | None = None, log: Callable[[str], None] | None = None) -> dict:
    """执行一个 Run（含平台原因重跑）并写记录 `job["record"]`；→ 记录。记录与日志不含种子明文。"""
    seeds = [int(job["seed"])]
    log = log or (lambda m: print(m, flush=True))
    if run_job is None:
        from awr.bench.runner import run_job
    run_dir = Path(job["run_dir"])
    rec: dict[str, Any] = {k: job[k] for k in ("key", "run_id", "kind", "group", "obs_level", "policy", "seed_index", "rep")}
    rec["queued_utc"] = utc_now()
    t_q = time.time_ns()
    r, errs = None, []
    for attempt in range(1, int(job.get("retries", 0)) + 2):
        shutil.rmtree(run_dir, ignore_errors=True)
        t0 = time.time_ns()
        try:
            r = run_job(job["rs"], policy=job["policy_doc"], seed=int(job["seed"]), run_id=job["run_id"], run_dir=run_dir,
                        worlds_root=Path(job["worlds_root"]), events_root=Path(job["events_root"]), physics=job["physics"],
                        gz=dict(job["gz"]) or None, agent_image=job["agent_image"], docker=job.get("docker", "docker"))
            break
        except Exception as e:                                           # 平台原因：按 retries 重跑
            msg = redact(f"{type(e).__name__}: {e}", seeds)[:600]
            errs.append({"attempt": attempt, "error": msg, "code": getattr(e, "code", None),
                         "trace": redact(traceback.format_exc()[-2000:], seeds)})
            log(f"{job['key']}: attempt {attempt} failed: {msg}")
    t1 = time.time_ns()
    rec.update(attempts=len(errs) + (r is not None), errors=errs, started_unix_ns=t0, ended_unix_ns=t1, ended_utc=utc_now())
    if r is None:
        rec["status"] = "FAILED"
        write_json(Path(job["record"]), rec)
        return rec
    ev_path = run_dir / "evaluation.json"
    try:
        man, ev = read_json(run_dir / "manifest.json"), read_json(ev_path)
    except (OSError, ValueError) as e:                                   # run_job 返回但产物缺失：按失败记录，续跑时重做
        rec.update(status="FAILED", errors=[*errs, {"attempt": rec["attempts"], "error": redact(f"outputs: {type(e).__name__}: {e}", seeds)[:600]}])
        write_json(Path(job["record"]), rec)
        log(f"{job['key']}: FAILED (run outputs missing)")
        return rec
    main_hz, physics_hz = job["hz"]
    n = int(man.get("n_ticks", 0))
    sim_s = round(n / main_hz, 3)
    wall = float(r.get("run_wall_s", man.get("wall_s", 0.0)))
    rec.update(status="COMPLETED", run_dir=str(run_dir), wall_s=wall, env_wall_s=float(man.get("wall_s", 0.0)), eval_wall_s=r.get("eval_wall_s"),
               sim_s=sim_s, wall_ratio=round(wall / sim_s, 4) if sim_s > 0 else None, n_ticks=n, physics=man.get("physics"),
               lockstep=lockstep_check(man, main_hz, physics_hz), obs_audit=_audit(man, r), pose_sha256=man.get("pose_sha256"),
               tape_content_sha256=(man.get("event") or {}).get("content_sha256"), components=ev.get("components"),
               ranked=bool(ev.get("ranked", False)), ranked_valid=bool(ev.get("ranked_valid")), invalid_reasons=list(ev.get("invalid_reasons", [])),
               evaluation_sha256=sha256_file(ev_path), lidar_backend=(man.get("l2") or {}).get("lidar_backend"))
    try:                                                                 # 运行清单（M27 §7.3）：排名运行只含 seed_commit
        from awr.agentsdk.env.spec import EnvSpec
        from awr.bench.eval.evaluator import run_manifest, write_run_manifest

        rs = {**job["rs"], "policy": {k: v for k, v in job["policy_doc"].items() if k in ("kind", "name", "image_digest", "params")}}
        spec = EnvSpec.from_run_spec(rs, run_id=job["run_id"], seed=int(job["seed"]), worlds_root=Path(job["worlds_root"]),
                                     events_root=Path(job["events_root"]), out_dir=run_dir, physics=job["physics"], gz=dict(job["gz"]) or None,
                                     images=job.get("images") or None, seed_commit=job.get("seed_commit"))
        doc = run_manifest(spec, run_dir, ev, experiment_id=job["round"], attempt=rec["attempts"], restart_count=rec["attempts"] - 1,
                           split=job.get("split", "dev"), timing={"queued_unix_ns": t_q, "started_unix_ns": t0, "ended_unix_ns": t1})
        write_run_manifest(run_dir, doc)
        rec["run_manifest"] = {"ok": True, "seed_commit": doc["seed_commit"], "run_hash": doc["run_hash"], "config_sha256": doc["config_sha256"]}
    except Exception as e:                                               # 清单缺陷不改变 Run 状态，由报告判定
        rec["run_manifest"] = {"ok": False, "error": redact(f"{type(e).__name__}: {e}", seeds)[:600]}
    write_json(Path(job["record"]), rec)
    c = rec["components"] or {}
    log(f"{job['key']}: COMPLETED wall {wall:.1f} s / sim {sim_s:.1f} s (x{rec['wall_ratio']}) EC {c.get('ec')} EU {c.get('eu')} AO {c.get('ao')} "
        f"CE {c.get('ce')} unknown_fields {rec['obs_audit'].get('unknown_fields')}")
    return rec


def _pool_run(job: dict) -> dict:
    """进程池入口（缺省 run_job；日志追加到本轮日志文件）。"""
    lp = job.get("log_path")

    def log(m: str) -> None:
        line = f"{utc_now()} {m}"
        print(line, flush=True)
        if lp:
            with open(lp, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    return execute_run(job, None, log)


# ------------------------------------------------------------------------------------------------------------------ 本轮
@dataclass
class Hooks:
    """可替换的平台调用（测试注入替身；缺省为真实实现）。`run_job` 为 None 时可用进程池并行。"""

    validate_world: Callable | None = None
    compile_event: Callable | None = None
    run_job: Callable | None = None
    process_one: Callable | None = None
    docker: str = "docker"


class Round:
    def __init__(self, cfg: dict, out: Path, *, worlds_root: Path, data_root: Path, hooks: Hooks | None = None) -> None:
        self.cfg, self.out = cfg, Path(out)
        self.worlds_root, self.data_root = Path(worlds_root), Path(data_root)
        self.hooks = hooks or Hooks()
        self.events_root = self.out / "events"
        self.state_path = self.out / "e2e_state.json"
        self.log_path = self.out / "e2e.log"
        self.store = EvalStore(self.out / "evalstore")
        self.fp = fingerprint(cfg)
        # L2 网格 LiDAR 后端（`awr.agentsdk.env.l2.lidar_backend`）：经 Env 专用变量传给本进程与进程池中的 Run（基线与内部提交同一后端）
        os.environ["AWR_ENV_LIDAR_BACKEND"] = str(cfg["lidar_backend"])
        self.specs = {g["obs_level"]: self._spec(g) for g in cfg["groups"]}
        ev = {(rs["event"]["id"], int(rs["event"].get("version", 1))) for rs in self.specs.values()}
        if len(ev) != 1:
            raise ConfigError(f"all groups must use one event, got {sorted(ev)}")
        self.event_id, self.event_version = next(iter(ev))
        worlds = {rs["world"]["id"] for rs in self.specs.values()}
        if worlds != {cfg["world"]["id"]}:
            raise ConfigError(f"run specs refer to worlds {sorted(worlds)}, the round checks {cfg['world']['id']}")
        self.state: dict = {}

    # -------------------------------------------------------------- 规格与计划
    def _spec(self, g: dict) -> dict:
        rs = read_json(Path(g["run_spec"]))
        if rs.get("obs_level") != g["obs_level"]:
            raise ConfigError(f"{g['run_spec']}: obs_level {rs.get('obs_level')} != group {g['obs_level']}")
        rs = {k: v for k, v in rs.items() if k not in ("seeds", "seed_count", "seed")}
        rs["physics"] = self.cfg["physics"]
        if self.cfg.get("fleet_vehicle"):
            rs["fleet"] = [{**f, "vehicle_ref": self.cfg["fleet_vehicle"]} for f in rs["fleet"]]
        if self.cfg.get("duration_s") is not None:
            rs["duration_s"] = float(self.cfg["duration_s"])
        return rs

    @staticmethod
    def track_id(rs: dict) -> str:
        return f"std{len(rs['fleet'])}-{rs['obs_level'].lower()}"

    def board_version(self, level: str) -> str:
        return f"{self.cfg['board']['version']}-{level.lower()}"

    def plan(self) -> list[dict]:
        """正式 Run 清单（不含种子）：组 × 基线 × 种子序号 × 重复。"""
        out = []
        for g in self.cfg["groups"]:
            lv = g["obs_level"]
            for p in self.cfg["baselines"]:
                for k in range(int(self.cfg["seeds"])):
                    for rep in range(1, int(self.cfg["repeats"]) + 1):
                        key = f"{lv.lower()}-{p}-k{k:02d}-p{rep}"
                        out.append({"key": key, "run_id": f"{self.cfg['round']}-{key}", "kind": "formal", "group": lv, "obs_level": lv,
                                    "policy": p, "seed_index": k, "rep": rep})
        return out

    def _job(self, item: dict, seed: int, *, seed_commit: str | None, run_dir: Path, record: Path, rs: dict, retries: int) -> dict:
        main_hz, physics_hz = _hz(rs)
        gz = {k: v for k, v in (self.cfg.get("gz") or {}).items() if v is not None} if self.cfg["physics"] == "realistic" else {}
        return {**item, "seed": int(seed), "seed_commit": seed_commit, "rs": rs, "policy_doc": {"kind": "reference", "name": item["policy"]},
                "run_dir": str(run_dir), "record": str(record), "worlds_root": str(self.worlds_root.resolve()),
                "events_root": str(self.events_root.resolve()), "physics": self.cfg["physics"], "gz": gz, "agent_image": self.cfg["agent_image"],
                "docker": self.hooks.docker, "retries": retries, "round": self.cfg["round"], "split": self.cfg["split"], "hz": (main_hz, physics_hz),
                "images": self.state.get("images") or {}, "log_path": str(self.log_path)}

    # -------------------------------------------------------------- 检查点与日志
    def log(self, m: str) -> None:
        line = f"{utc_now()} {m}"
        print(line, flush=True)
        self.out.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _load_state(self) -> None:
        if self.state_path.exists():
            st = read_json(self.state_path)
            if st.get("fingerprint") != self.fp:
                raise _Mismatch(f"{self.state_path}: the checkpoint belongs to another configuration (fingerprint {st.get('fingerprint', '')[:12]} "
                                f"!= {self.fp[:12]}); use a new --out")
            self.state = st
        else:
            self.state = {"schema": STATE_SCHEMA, "round": self.cfg["round"], "fingerprint": self.fp, "created_utc": utc_now(), "stages": {}}
            write_json(self.out / "config.effective.json", self.cfg)
        self.state["formal"] = is_formal(self.cfg)
        self._save()

    def _save(self) -> None:
        write_json(self.state_path, self.state)

    def stage(self, name: str) -> dict:
        return self.state["stages"].get(name) or {}

    # -------------------------------------------------------------- 执行
    def run(self, stages: list[str] | None = None, force: set[str] | None = None) -> int:
        self.out.mkdir(parents=True, exist_ok=True)
        self._load_state()
        force = set(force or ())
        todo = [s for s in STAGES if stages is None or s in stages]
        self.log(f"round {self.cfg['round']} fingerprint {self.fp[:12]} formal={self.state['formal']} stages {','.join(todo)} out {self.out}")
        failed = None
        for s in todo:
            if s == "report":
                continue
            prev = self.stage(s)
            if prev.get("status") == "done" and s not in force and s not in ALWAYS:
                self.log(f"[{s}] done at {prev.get('ended_utc')} (resume: skipped)")
                continue
            need = {"compile": ("world",), "lockstep": ("compile",), "runs": ("compile",), "board": ("runs",), "submit": ("board",)}.get(s, ())
            missing = [x for x in need if self.stage(x).get("status") != "done"]
            if missing:
                failed = s
                self.state["stages"][s] = {"status": "failed", "error": f"requires stage(s) {missing} to be done", "ended_utc": utc_now()}
                self._save()
                self.log(f"[{s}] FAILED: requires {missing}")
                break
            t0 = time.time()
            self.state["stages"][s] = {"status": "running", "started_utc": utc_now()}
            self._save()
            self.log(f"[{s}] start")
            try:
                res = getattr(self, f"st_{s}")()
                st = {"status": "done", "result": res}
            except E2EError as e:
                st = {"status": "failed", "error": str(e)}
            except Exception as e:                                       # 意外异常同样记入检查点
                st = {"status": "failed", "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-3000:]}
            if self.store.path.exists():
                for k in ("error", "trace"):
                    if k in st:
                        st[k] = redact(st[k], self.store.seeds(self.event_id))
            st.update(started_utc=self.state["stages"][s]["started_utc"], ended_utc=utc_now(), wall_s=round(time.time() - t0, 1))
            self.state["stages"][s] = st
            self._save()
            self.log(f"[{s}] {st['status']} in {st['wall_s']} s" + (f": {st['error']}" if st["status"] == "failed" else ""))
            if st["status"] != "done":
                failed = s
                break
        rep = None
        if stages is None or "report" in stages:
            try:
                rep = self.st_report()
            except E2EError as e:
                self.log(f"[report] FAILED: {e}")
                return 1
        if rep is not None:
            self.log(f"[report] passed={rep['passed']} formal={rep['formal']} formal_passed={rep['formal_passed']} -> {self.out / 'mbl_e2e.json'}")
            for c in rep["checks"]:
                self.log(f"  {'PASS' if c['passed'] else 'FAIL'} {c['id']}: {c['summary']}")
        if failed is not None:
            return 1
        return 0 if rep is None or rep["passed"] else 1

    # -------------------------------------------------------------- 1 世界包检查
    def st_world(self) -> dict:
        fn = self.hooks.validate_world
        if fn is None:
            from awr.world.package.validate import validate_world as fn
        wd = self.worlds_root / self.cfg["world"]["id"]
        if not (wd / "world.json").is_file():
            raise E2EError(f"{wd}: world package missing (world.json)")
        rep = fn(wd, deep=bool(self.cfg["world"].get("deep", False)))
        d = rep.to_json() if hasattr(rep, "to_json") else dict(rep)
        res = {"world_dir": str(wd), "world_id": d.get("world_id"), "ok": bool(d.get("ok")), "deep": bool(d.get("deep")),
               "errors": len(d.get("errors", [])), "warnings": len(d.get("warnings", [])), "rules_checked": d.get("info", {}).get("rules_checked"),
               "first_errors": d.get("errors", [])[:20], "seconds": d.get("seconds")}
        write_json(self.out / "checks" / "world_validate.json", d)
        if not res["ok"]:
            raise E2EError(f"world package check failed: {res['errors']} error(s), first {[e.get('rule') for e in res['first_errors'][:5]]}")
        with contextlib.suppress(OSError, ValueError):
            res["content_version"] = read_json(wd / "world.json").get("contentVersion")
        return res

    # -------------------------------------------------------------- 2 strict 编译
    def st_compile(self) -> dict:
        fn = self.hooks.compile_event
        from awr.sim.events.compile import CompileOptions

        if fn is None:
            from awr.sim.events.compile import compile_event as fn
        src = Path(self.cfg["event"]["source"])
        out = self.events_root / self.event_id / f"v{self.event_version}"
        ranked = any(bool(rs.get("ranked")) for rs in self.specs.values())
        r = fn(src, self.worlds_root / self.cfg["world"]["id"], opts=CompileOptions(strict=True, ranked=ranked, out=out, cache_dir=self.out / "cache" / "compile"))
        d = r.as_dict() if hasattr(r, "as_dict") else dict(r)
        write_json(self.out / "checks" / "compile_report.json", d)
        gates = [{"gate": g["gate"], "status": g["status"], "errors": len(g.get("errors", [])), "warnings": len(g.get("warnings", []))}
                 for g in d.get("gates", [])]
        res = {"status": d.get("status"), "strict": d.get("strict", True), "ranked": ranked, "event_id": d.get("event_id"), "version": d.get("version"),
               "gates": gates, "reason": d.get("reason"), "out": str(out)}
        if d.get("status") != "compiled" or not d.get("strict", True) or [g["gate"] for g in gates] != [1, 2, 3] or any(g["status"] == "fail" for g in gates):
            raise E2EError(f"strict compile failed: status {d.get('status')}, gates {gates}, reason {d.get('reason')}")
        if (d.get("event_id"), d.get("version")) != (self.event_id, self.event_version):
            raise E2EError(f"compiled {d.get('event_id')} v{d.get('version')}, run specs need {self.event_id} v{self.event_version}")
        tape = read_json(out / "tape.json")
        plan = read_json(out / "plan.json")
        res.update(tape_content_sha256=tape["content_sha256"], event_type=plan["event_type"], n_ticks=(d.get("tape") or {}).get("n_ticks"),
                   n_actors=(d.get("tape") or {}).get("n_actors"))
        self.state["images"] = self._images()
        return res

    def _images(self) -> dict:
        """平台镜像摘要（进入运行清单 versions）；docker 不可用时留空。"""
        out = {}
        for k, ref in (("agent", self.cfg["agent_image"]), ("gz", (self.cfg.get("gz") or {}).get("image") if self.cfg["physics"] == "realistic" else None)):
            if ref:
                d = self._inspect(ref)
                if d:
                    out[k] = d
        return out

    def _inspect(self, ref: str) -> str | None:
        try:
            r = subprocess.run([self.hooks.docker, "image", "inspect", ref, "--format", "{{json .}}"], capture_output=True, text=True, check=False,
                               timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if r.returncode != 0:
            return None
        try:
            info = json.loads(r.stdout)
        except ValueError:
            return None
        info = info[0] if isinstance(info, list) and info else info
        return str(info.get("Id")) if isinstance(info, dict) and info.get("Id") else None

    def _tape_guard(self) -> None:
        want = self.stage("compile").get("result", {}).get("tape_content_sha256")
        p = self.events_root / self.event_id / f"v{self.event_version}" / "tape.json"
        if not p.exists() or read_json(p).get("content_sha256") != want:
            raise E2EError(f"{p}: event tape differs from the one compiled for this round (expected content_sha256 {str(want)[:12]})")

    # -------------------------------------------------------------- 3 gz 锁步预检
    def st_lockstep(self) -> dict:
        self._tape_guard()
        lc = self.cfg["lockstep"]
        rs = {**self.specs[lc["group"]], "ranked": False, "duration_s": float(lc["preflight_s"])}
        item = {"key": "preflight-lockstep", "run_id": f"{self.cfg['round']}-preflight", "kind": "preflight", "group": lc["group"],
                "obs_level": lc["group"], "policy": lc["policy"], "seed_index": None, "rep": None}
        job = self._job(item, 0, seed_commit=None, run_dir=self.out / "preflight" / "lockstep", record=self.out / "records" / "preflight-lockstep.json",
                        rs=rs, retries=0)
        rec = execute_run(job, self.hooks.run_job, self.log)
        if rec["status"] != "COMPLETED":
            raise E2EError(f"lockstep preflight failed: {rec['errors'][-1]['error'] if rec['errors'] else 'unknown'}")
        ls, au = rec["lockstep"], rec["obs_audit"]
        res = {"physics": rec["physics"], "lockstep": ls, "obs_audit": au, "wall_s": rec["wall_s"], "sim_s": rec["sim_s"], "wall_ratio": rec["wall_ratio"],
               "dev": self.cfg["physics"] != "realistic"}
        if self.cfg["physics"] == "realistic" and not ls.get("ok"):
            raise E2EError(f"gz lockstep invariant violated: {ls}")
        if not au.get("passed") or au.get("unknown_fields", 0) or au.get("schema_violations", 0):
            raise E2EError(f"obs.v1 audit failed in the preflight: {au}")
        return res

    # -------------------------------------------------------------- 4 正式 Run
    def st_runs(self) -> dict:
        self._tape_guard()
        self.store.ensure(self.event_id, int(self.cfg["seeds"]))
        seeds = self.store.seeds(self.event_id)
        jobs_by_group: dict[str, list[dict]] = {}
        skipped = 0
        for it in self.plan():
            rec_p = self.out / "records" / f"{it['key']}.json"
            run_dir = self.out / "runs" / it["key"]
            if rec_p.exists():
                rec = read_json(rec_p)
                ev = run_dir / "evaluation.json"
                if rec.get("status") == "COMPLETED" and ev.exists() and sha256_file(ev) == rec.get("evaluation_sha256"):
                    skipped += 1
                    continue
            seed = seeds[it["seed_index"]]
            job = self._job(it, seed, seed_commit=self.store.seed_commit(it["run_id"], seed), run_dir=run_dir, record=rec_p,
                            rs=self.specs[it["group"]], retries=int(self.cfg["platform_retries"]))
            jobs_by_group.setdefault(it["group"], []).append(job)
        n_todo = sum(len(v) for v in jobs_by_group.values())
        self.log(f"[runs] {len(self.plan())} planned, {skipped} completed earlier (skipped), {n_todo} to run")
        for g in self.cfg["groups"]:
            jobs = jobs_by_group.get(g["obs_level"], [])
            if not jobs:
                continue
            n_par = max(1, int(g.get("jobs", 1)))
            if self.hooks.run_job is not None or n_par == 1:
                for j in jobs:
                    execute_run(j, self.hooks.run_job, self.log)
            else:
                with ProcessPoolExecutor(max_workers=n_par) as ex:
                    futs = {ex.submit(_pool_run, j): j for j in jobs}
                    for f in as_completed(futs):
                        j = futs[f]
                        try:
                            f.result()
                        except Exception as e:                           # 进程级故障（OOM 等）：记为失败，续跑时重做
                            write_json(Path(j["record"]), {**{k: j[k] for k in ("key", "run_id", "kind", "group", "obs_level", "policy", "seed_index",
                                                                                  "rep")}, "status": "FAILED", "attempts": 0,
                                                            "errors": [{"attempt": 0, "error": redact(f"{type(e).__name__}: {e}", seeds)[:600]}]})
                            self.log(f"{j['key']}: worker process failed: {type(e).__name__}")
        recs = self.records()
        bad = [r["key"] for r in recs if r.get("status") != "COMPLETED"]
        res = {"planned": len(self.plan()), "completed": len(recs) - len(bad), "failed": bad, "skipped_resume": skipped,
               "seed_set_commit": self.store.seed_set_commit(self.event_id)}
        if bad:
            raise E2EError(f"{len(bad)} run(s) not COMPLETED: {bad[:10]}")
        return res

    def records(self) -> list[dict]:
        out = []
        for it in self.plan():
            p = self.out / "records" / f"{it['key']}.json"
            out.append(read_json(p) if p.exists() else {**it, "status": "PENDING"})
        return out

    # -------------------------------------------------------------- 5 原型榜
    def st_board(self) -> dict:
        from awr.bench.boardstore import BoardVersionError, load_rows, publish, slim_evaluation
        from awr.bench.eval.thresholds import SCORE_VERSION
        from awr.bench.submissions import SubmissionStore

        plan = read_json(self.events_root / self.event_id / f"v{self.event_version}" / "plan.json")
        seeds = self.store.seeds(self.event_id)
        dev = self.cfg["physics"] != "realistic"
        subs = SubmissionStore(self.data_root / "submissions")
        out = {}
        recs = self.records()
        for g in self.cfg["groups"]:
            lv, rs = g["obs_level"], self.specs[g["obs_level"]]
            ver, tr = self.board_version(lv), self.track_id(rs)
            rows = []
            for r in sorted((x for x in recs if x["group"] == lv), key=lambda x: (x["policy"], x["seed_index"], x["rep"])):
                ev = read_json(Path(r["run_dir"]) / "evaluation.json")
                rows.append({"entry_id": f"baseline/{r['policy']}", "team": "D3 reference", "display_name": DISPLAY.get(r["policy"], r["policy"]),
                             "baseline": True, "track": tr, "lane": plan["event_type"], "event_type": plan["event_type"], "event": self.event_id,
                             "seed": r["seed_index"], "rep": r["rep"], "run_id": r["run_id"], "evaluation": slim_evaluation(ev)})
            vehicles = sorted({f.get("vehicle_ref", "x500") for f in rs["fleet"]})
            gz = {k: v for k, v in (self.cfg.get("gz") or {}).items() if v is not None} if not dev else {}
            cfg = {"run_spec": rs, "seeds": seeds, "repeats": int(self.cfg["repeats"]), "events_root": str(self.events_root.resolve()),
                   "worlds_root": str(self.worlds_root.resolve()), "physics": self.cfg["physics"], "gz": gz, "agent_image": self.cfg["agent_image"],
                   "score_version": SCORE_VERSION,
                   "track": {"fleet_ref": f"{len(rs['fleet'])}x-{'+'.join(vehicles)}", "obs_level": lv, "dev": dev, "prototype": True,
                             "gpu_class": "shared"},
                   "track_id": tr, "lane": plan["event_type"], "event_type": plan["event_type"], "event": self.event_id,
                   "seed_set_commit": self.store.seed_set_commit(self.event_id),
                   "mbl_round": {"round": self.cfg["round"], "fingerprint": self.fp, "out": str(self.out.resolve())}}
            local = self.out / "board" / ver
            write_json(local / "config.json", cfg, mode=0o600)
            (local / "rows.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False, sort_keys=True) + "\n" for x in rows), encoding="utf-8")
            dst = self.data_root / "boards" / ver
            keep = []
            if (dst / "config.json").exists():
                old = read_json(dst / "config.json")
                if (old.get("mbl_round") or {}).get("fingerprint") != self.fp:
                    raise E2EError(f"{dst}: board version {ver} is already published by another round; choose another board.version")
                keep = [x for x in load_rows(dst) if not x.get("baseline")]   # 内部提交的评测行在重新发布时保留
            dst.mkdir(parents=True, exist_ok=True)
            write_json(dst / "config.json", cfg, mode=0o600)
            (dst / "rows.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False, sort_keys=True) + "\n" for x in rows + keep), encoding="utf-8")
            try:
                board = publish(dst, subs.board_view(), n_boot=int(self.cfg["board"]["n_boot"]))
            except BoardVersionError as e:
                raise E2EError(str(e)) from e
            current = self.cfg["board"].get("current") == lv and not dev
            if current:
                (self.data_root / "boards" / "current").write_text(ver + "\n", encoding="utf-8")
            out[lv] = {"board_version": ver, "board_dir": str(dst), "track": tr, "lane": plan["event_type"], "rows": len(rows),
                       "kept_submission_rows": len(keep), "entries": len(board["entries"]), "current": current, "dev": dev}
            self.log(f"[board] {ver}: {len(board['entries'])} entries, {len(rows)} baseline rows, current={current}")
        return out

    # -------------------------------------------------------------- 6 内部提交
    def st_submit(self) -> dict:
        from awr.agentsdk.schemas import bench_validator
        from awr.bench.submissions import SubmissionError, SubmissionStore

        sc = self.cfg.get("submission")
        if not sc:
            raise E2EError("no submission configured")
        lv = sc["group"]
        bv = self.stage("board")["result"][lv]["board_version"]
        bdir = self.data_root / "boards" / bv
        st = SubmissionStore(self.data_root / "submissions")
        prev = self.stage("submit").get("result") or self.state.get("submission") or {}
        sid = prev.get("submission_id")
        if sid:
            d = st.get(sid)
            if d["state"] in ("REJECTED", "FAILED", "WITHDRAWN"):
                sid = None                                               # --force submit：上次终结于失败，重新登记
        if not sid:
            digest = sc["image"] if str(sc["image"]).startswith("sha256:") and len(sc["image"]) == 71 else self._inspect(sc["image"])
            if not digest:
                raise E2EError(f"submission image {sc['image']} not found (docker image inspect)")
            req = {"track": self.track_id(self.specs[lv]), "image_digest": digest,
                   "entry": {"cmd": [str(x) for x in sc["cmd"]], **({"env": {str(k): str(v) for k, v in sc["env"].items()}} if sc.get("env") else {})},
                   "notes": sc.get("notes") or f"mbl_e2e {self.cfg['round']}"}
            errs = [e.message for e in bench_validator("submission").iter_errors({"schema": "drone4d.bench.submission.v1", **req})]
            if errs:
                raise E2EError(f"submission request invalid: {errs[:3]}")
            b = read_json(bdir / "board.json")
            try:
                d = st.create(req, owner=sc["owner"], team=sc["team"], board_version=bv, tracks={t["track"] for t in b["tracks"]},
                              lanes={x["lane"] for x in b["lanes"]})
                st.set_visibility(d["submission_id"], "board")
            except SubmissionError as e:
                raise E2EError(f"submission create: {e.code} {e.detail}") from e
            sid = d["submission_id"]
            self.state["submission"] = {"submission_id": sid, "image_digest": digest, "board_version": bv}
            self._save()
            self.log(f"[submit] {sid} RECEIVED on {bv} (image {digest[:19]}…)")
        t0 = time.time()
        if sc["worker"] == "inline":
            fn = self.hooks.process_one
            if fn is None:
                from awr.bench.subworker import process_one as fn
            kw = {"docker": self.hooks.docker, "smoke_s": float(sc["smoke_s"]), "n_boot": int(self.cfg["board"]["n_boot"]),
                  "log": lambda m: self.log(f"[subworker] {m}")}
            if self.hooks.run_job is not None:
                kw["run_job"] = self.hooks.run_job
            while st.get(sid)["state"] not in TERMINAL:
                got = fn(self.data_root, **kw)
                if got is None and st.get(sid)["state"] not in TERMINAL:
                    raise E2EError(f"{sid}: the pipeline left the submission in {st.get(sid)['state']} (board version frozen?)")
        else:
            while st.get(sid)["state"] not in TERMINAL:
                if time.time() - t0 > float(sc["timeout_s"]):
                    raise E2EError(f"{sid}: still {st.get(sid)['state']} after {sc['timeout_s']} s (external worker)")
                time.sleep(float(sc["poll_s"]))
        d = st.get(sid)
        hist = [h["state"] for h in d["_meta"]["history"]]
        res = {"submission_id": sid, "board_version": bv, "state": d["state"], "history": hist, "image_digest": d["image_digest"],
               "reason": d.get("reason"), "result": d.get("result"), "wall_s": round(time.time() - t0, 1)}
        if d["state"] != "DONE":
            raise E2EError(f"{sid}: {d['state']} {d.get('reason')}")
        b = read_json(bdir / "board.json")
        e = next((x for x in b["entries"] if x["entry_id"] == f"{d['_meta']['team']}/{sid}"), None)
        res["listed"] = e is not None
        if e is None:
            raise E2EError(f"{sid}: DONE but not listed on {bv}")
        res["entry"] = {"entry_id": e["entry_id"], "metrics": e["metrics"], "dominance_tier": e.get("dominance_tier"), "status": e.get("status")}
        return res

    def _submission_runs(self, sid: str) -> list[dict]:
        """内部提交的冒烟与正式 Run（子流水线的运行目录）：墙钟与白名单审计。"""
        out = []
        dirs = [("smoke", self.data_root / "submissions" / sid / "smoke")]
        dirs += [(p.name, p) for p in sorted((self.data_root / "dataset" / self.event_id / "runs").glob(f"{sid}-r*"))]
        rs = self.specs[self.cfg["submission"]["group"]]
        main_hz, physics_hz = _hz(rs)
        for name, d in dirs:
            if not (d / "manifest.json").exists():
                continue
            man = read_json(d / "manifest.json")
            n = int(man.get("n_ticks", 0))
            sim_s = n / main_hz
            wall = float(man.get("wall_s", 0.0))
            out.append({"key": name, "kind": "smoke" if name == "smoke" else "submission", "run_dir": str(d), "n_ticks": n, "sim_s": round(sim_s, 3),
                        "env_wall_s": wall, "wall_ratio": round(wall / sim_s, 4) if sim_s > 0 else None, "physics": man.get("physics"),
                        "lockstep": lockstep_check(man, main_hz, physics_hz), "obs_audit": _audit(man, None),
                        "evaluated": (d / "evaluation.json").exists()})
        return out

    # -------------------------------------------------------------- 7 报告
    def st_report(self) -> dict:
        cfg, stg = self.cfg, self.state["stages"]
        recs = self.records()
        done = [r for r in recs if r.get("status") == "COMPLETED"]
        pre = self.out / "records" / "preflight-lockstep.json"
        pre_rec = read_json(pre) if pre.exists() else None
        sub = (stg.get("submit") or {}).get("result") or {}
        sid = sub.get("submission_id") or (self.state.get("submission") or {}).get("submission_id")
        sub_runs = self._submission_runs(sid) if sid else []
        checks: list[dict] = []

        def check(cid: str, ok: bool, summary: str, **detail) -> None:
            checks.append({"id": cid, "passed": bool(ok), "summary": summary, **({"detail": detail} if detail else {})})

        w = (stg.get("world") or {})
        check("world_package", w.get("status") == "done", f"world package {cfg['world']['id']}: " +
              (f"{w['result']['errors']} errors, {w['result']['warnings']} warnings, {w['result']['rules_checked']} rules" if w.get("result")
               else w.get("error", "not run")))
        c = (stg.get("compile") or {})
        check("event_strict_compile", c.get("status") == "done", f"{self.event_id} v{self.event_version} strict: " +
              (", ".join(f"gate {g['gate']} {g['status']}" for g in c["result"]["gates"]) if c.get("result") else c.get("error", "not run")))
        lk = (stg.get("lockstep") or {})
        lsr = [(r["key"], r.get("lockstep") or {}) for r in done] + [(x["key"], x["lockstep"]) for x in sub_runs if x["kind"] == "submission"]
        ls_bad = [k for k, x in lsr if not x.get("applicable") or not x.get("ok")]
        realistic = cfg["physics"] == "realistic"
        check("gz_lockstep", lk.get("status") == "done" and realistic and bool(done) and not ls_bad,
              f"preflight {'ok' if lk.get('status') == 'done' else lk.get('error', 'not run')}; " +
              (f"{len(lsr) - len(ls_bad)}/{len(lsr)} formal runs (baselines and submission) satisfy iterations = steps_per_tick x ticks"
               if realistic else "physics = kinematic (development round, not gz lockstep)"), violations=ls_bad[:20])
        planned = len(recs)
        check("runs_completed", len(done) == planned and planned > 0, f"{len(done)}/{planned} runs COMPLETED "
              f"({len(cfg['groups'])} groups x {len(cfg['baselines'])} baselines x {cfg['seeds']} secret seeds x {cfg['repeats']} repeats)",
              not_completed=[r["key"] for r in recs if r.get("status") != "COMPLETED"][:20])
        ev_bad = [r["key"] for r in done if not (Path(r["run_dir"]) / "evaluation.json").exists()
                  or sha256_file(Path(r["run_dir"]) / "evaluation.json") != r.get("evaluation_sha256")
                  or any(m not in (r.get("components") or {}) for m in METRICS)]
        check("evaluations", bool(done) and not ev_bad, f"{len(done) - len(ev_bad)}/{len(done)} evaluation.json with EC, EU, AO_real v0, CE v0",
              bad=ev_bad[:20])
        b = (stg.get("board") or {}).get("result") or {}
        bl_bad = []
        boards = {}
        for g in cfg["groups"]:
            info = b.get(g["obs_level"])
            if not info:
                bl_bad.append(f"{g['obs_level']}: not published")
                continue
            bd = read_json(Path(info["board_dir"]) / "board.json")
            ents = {e["entry_id"]: e for e in bd["entries"]}
            for p in cfg["baselines"]:
                e = ents.get(f"baseline/{p}")
                if e is None or not e.get("baseline") or any(e["metrics"][m].get("value") is None and m != "ce" for m in METRICS) \
                        or any(len(e["metrics"][m].get("ci95") or []) != 2 for m in METRICS):
                    bl_bad.append(f"{info['board_version']}: baseline/{p}")
            boards[g["obs_level"]] = {"board_version": info["board_version"], "current": info["current"], "lanes": [x["lane"] for x in bd["lanes"]],
                                      "entries": [{"entry_id": e["entry_id"], "baseline": e.get("baseline"), "status": e.get("status"),
                                                   "dominance_tier": e.get("dominance_tier"),
                                                   "metrics": {m: {k: e["metrics"][m].get(k) for k in ("value", "ci95", "rank", "tie_group", "repeat_delta")}
                                                               for m in METRICS}} for e in bd["entries"]]}
        check("board_baselines", bool(b) and not bl_bad, f"{len(boards)} board version(s), {len(cfg['baselines'])} baseline reference rows each "
              "with four metrics and 95% CI", missing=bl_bad)
        check("internal_submission", sub.get("state") == "DONE" and bool(sub.get("listed")),
              f"{sid or 'none'}: {sub.get('state', (stg.get('submit') or {}).get('error', 'not run'))}, "
              f"history {'>'.join(sub.get('history', []))}, listed={sub.get('listed', False)}")
        audited = [("formal", r["key"], r["obs_audit"]) for r in done] + [("submission", x["key"], x["obs_audit"]) for x in sub_runs]
        if pre_rec and pre_rec.get("status") == "COMPLETED":
            audited.append(("preflight", pre_rec["key"], pre_rec["obs_audit"]))
        tot = {k: sum(int(a.get(k, 0)) for _kind, _k, a in audited) for k in AUDIT_COUNTS}
        au_bad = [k for _kind, k, a in audited if not a.get("present") or not a.get("passed") or a.get("unknown_fields", 0)
                  or a.get("schema_violations", 0) or a.get("jpeg_segment_violations", 0) or a.get("lidar_tag_violations", 0)
                  or a.get("decode_violations", 0) or not a.get("schema_checked", 0)]
        check("obs_v1_audit", bool(audited) and not au_bad, f"{len(audited)} runs audited: {tot['schema_checked']} messages checked, unknown fields "
              f"{tot['unknown_fields']}, schema violations {tot['schema_violations']}", totals=tot, bad=au_bad[:20])
        ratio = float(cfg["wall_ratio_max"])
        walls = [(r["key"], r.get("wall_ratio")) for r in done] + [(x["key"], x.get("wall_ratio")) for x in sub_runs if x["kind"] == "submission"]
        w_bad = [k for k, v in walls if v is None or v > ratio]
        mx = max((v for _k, v in walls if v is not None), default=None)
        check("wall_clock", bool(walls) and not w_bad, f"{len(walls)} formal runs, max wall/sim {mx} (limit {ratio}; shared GPU, prototype)",
              over=w_bad[:20])
        leak = self._leak_scan()
        man_bad = [r["key"] for r in done if not (r.get("run_manifest") or {}).get("ok")
                   or (r.get("ranked") and not self._commit_ok(r))]
        check("secret_seeds", self.store.path.exists() and not leak["hits"] and not man_bad,
              f"{cfg['seeds']} CSPRNG seeds in the evalstore only; {leak['files']} text artefacts scanned, {len(leak['hits'])} hits; "
              f"{len(done) - len(man_bad)}/{len(done)} run_manifest.json valid with HMAC seed_commit", hits=leak["hits"][:20], manifests=man_bad[:20])
        passed = all(x["passed"] for x in checks)
        formal = is_formal(cfg)
        rep = {"schema": REPORT_SCHEMA, "round": cfg["round"], "generated_utc": utc_now(), "fingerprint": self.fp, "formal": formal,
               "passed": passed, "formal_passed": passed and formal, "acceptance": "D3-AC-56",
               "scale": {"groups": [g["obs_level"] for g in cfg["groups"]], "baselines": cfg["baselines"], "seeds": int(cfg["seeds"]),
                         "repeats": int(cfg["repeats"]), "duration_s": cfg["duration_s"], "physics": cfg["physics"], "fleet_vehicle": cfg.get("fleet_vehicle")},
               "event": {"id": self.event_id, "version": self.event_version,
                         "tape_content_sha256": ((stg.get("compile") or {}).get("result") or {}).get("tape_content_sha256")},
               "seed_set_commit": self.store.seed_set_commit(self.event_id) if self.store.path.exists() else None,
               "images": self.state.get("images") or {},
               "stages": {s: {k: v for k, v in (stg.get(s) or {}).items() if k in ("status", "started_utc", "ended_utc", "wall_s", "error")}
                          for s in STAGES if s != "report"},
               "checks": checks, "boards": boards, "submission": sub or None,
               "runs": [{k: r.get(k) for k in ("key", "run_id", "group", "policy", "seed_index", "rep", "status", "attempts", "wall_s", "env_wall_s",
                                               "sim_s", "wall_ratio", "components", "ranked_valid", "invalid_reasons", "pose_sha256",
                                               "evaluation_sha256")}
                        | {"obs_audit": {k: (r.get("obs_audit") or {}).get(k) for k in ("schema_checked", "unknown_fields", "schema_violations", "passed")},
                           "lockstep_ok": (r.get("lockstep") or {}).get("ok")} for r in recs],
               "submission_runs": sub_runs, "preflight": pre_rec and {k: pre_rec.get(k) for k in ("status", "wall_s", "sim_s", "wall_ratio", "lockstep",
                                                                                                   "obs_audit")},
               "repeat_pose_identical": self._repeat_identity(done), "representative_runs": self._representatives(done),
               "pending_elsewhere": [{"clause": "sample pack browsable in the internal workspace", "by": "tools/bench/d3/sample_pack.py (B20)"},
                                     {"clause": "Playwright lab/mbl", "by": "tests/e2e/lab-mbl.spec.ts (F09)"}]}
        txt = json.dumps(rep, ensure_ascii=False, indent=1, sort_keys=True)
        if self.store.path.exists() and self._hits_in(txt, self.store.seeds(self.event_id)):
            raise E2EError("the report would carry a secret seed")      # 不应发生：报告只由不含种子的记录生成
        write_json(self.out / "mbl_e2e.json", rep)
        self.state["stages"]["report"] = {"status": "done", "ended_utc": rep["generated_utc"], "passed": passed}
        self._save()
        return rep

    def _commit_ok(self, r: dict) -> bool:
        if not self.store.path.exists():
            return False
        seed = self.store.seeds(self.event_id)[int(r["seed_index"])]
        p = Path(r["run_dir"]) / "run_manifest.json"
        if not p.exists():
            return False
        m = read_json(p)
        return m.get("seed_commit") == self.store.seed_commit(r["run_id"], seed) and "seed" not in m and "seed" not in (m.get("spec") or {})

    @staticmethod
    def _hits_in(text: str, seeds: list[int]) -> list[str]:
        low = text.lower()
        return [f"seed[{i}]" for i, s in enumerate(seeds) if str(s) in text or f"{s:x}" in low or f"{s:016x}" in low]

    def _leak_scan(self) -> dict:
        """在本轮全部文本产物（检查点、日志、记录、运行清单与评测、审计、榜单与提交文档）中检索秘密种子明文。evalstore、
        服务端榜单配置 config.json 与真值层 truth/ 按设计持有种子，不在检索范围内。"""
        if not self.store.path.exists():
            return {"files": 0, "hits": []}
        seeds = self.store.seeds(self.event_id)
        files: list[Path] = [self.state_path, self.log_path, self.out / "console.log", self.out / "config.effective.json"]
        files += sorted((self.out / "records").glob("*.json")) + sorted((self.out / "checks").glob("*.json"))
        names = ("manifest.json", "run_manifest.json", "evaluation.json", "agents/obs_audit.json", "agents/wire_stats.json")
        run_dirs = [self.out / "runs" / it["key"] for it in self.plan()] + [self.out / "preflight" / "lockstep"]
        sid = (self.state.get("submission") or {}).get("submission_id")
        if sid:
            run_dirs += [Path(x["run_dir"]) for x in self._submission_runs(sid)]
            files.append(self.data_root / "submissions" / f"{sid}.json")
        files += [d / n for d in run_dirs for n in names]
        for g in self.cfg["groups"]:
            bd = self.data_root / "boards" / self.board_version(g["obs_level"])
            files += [bd / "rows.jsonl", bd / "board.json", bd / "details.json", self.out / "board" / bd.name / "rows.jsonl"]
        hits, n = [], 0
        for p in files:
            if not p.is_file():
                continue
            n += 1
            hits.extend(f"{p}: {h}" for h in self._hits_in(p.read_text(encoding="utf-8", errors="replace"), seeds))
        return {"files": n, "hits": hits}

    @staticmethod
    def _repeat_identity(done: list[dict]) -> dict:
        """同组、同基线、同种子序号的重复之间位姿摘要是否逐位一致（只作记录，确定性判定见 D3-AC-17、18）。"""
        by: dict[tuple, list[str]] = {}
        for r in done:
            by.setdefault((r["group"], r["policy"], r["seed_index"]), []).append(r.get("pose_sha256"))
        multi = {k: v for k, v in by.items() if len(v) > 1}
        same = sum(len(set(v)) == 1 and v[0] is not None for v in multi.values())
        return {"groups": len(multi), "identical": same}

    @staticmethod
    def _representatives(done: list[dict]) -> list[dict]:
        """每组每个基线 1 个代表运行（第 1 次重复中 EC 取中位的种子），供样例包（B20）使用。"""
        out = []
        for key in sorted({(r["group"], r["policy"]) for r in done}):
            cand = sorted((r for r in done if (r["group"], r["policy"]) == key and r["rep"] == 1),
                          key=lambda r: ((r.get("components") or {}).get("ec") or 0.0, r["seed_index"]))
            if cand:
                r = cand[(len(cand) - 1) // 2]
                out.append({"group": key[0], "policy": key[1], "key": r["key"], "run_id": r["run_id"], "run_dir": r["run_dir"]})
        return out


class _Mismatch(Exception):
    """检查点与本次配置指纹不一致（退出码 3）。"""


# ------------------------------------------------------------------------------------------------------------------ 命令行
def main(argv: list[str] | None = None, hooks: Hooks | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mbl_e2e.py", description="D3-AC-56 minimal benchmark loop, end to end, resumable")
    ap.add_argument("--config", type=Path, help="YAML or JSON (drone4d.bench.mbl_e2e_config.v1); defaults are the formal setting")
    ap.add_argument("--out", type=Path, required=True, help="round directory (checkpoint, runs, evalstore, report)")
    ap.add_argument("--worlds-root", type=Path, default=Path("worlds"))
    ap.add_argument("--data-root", type=Path, default=Path(os.environ.get("AWR_BENCH_DATA_ROOT", "benchroot")),
                    help="bench data root (boards/, submissions/, dataset/)")
    ap.add_argument("--stages", help=f"comma-separated subset of {','.join(STAGES)}")
    ap.add_argument("--force", default="", help="comma-separated stages to execute again although done")
    ap.add_argument("--seeds", type=int)
    ap.add_argument("--repeats", type=int)
    ap.add_argument("--duration-s", type=float)
    ap.add_argument("--baselines")
    ap.add_argument("--groups", help="comma-separated observation levels, e.g. L1")
    ap.add_argument("--physics", choices=("realistic", "kinematic"))
    ap.add_argument("--plan", action="store_true", help="print the effective configuration and the run plan, then exit")
    a = ap.parse_args(argv)
    ov = {"seeds": a.seeds, "repeats": a.repeats, "duration_s": a.duration_s, "physics": a.physics,
          "baselines": a.baselines.split(",") if a.baselines else None, "groups": a.groups.split(",") if a.groups else None}
    try:
        cfg = load_config(a.config, ov)
        stages = [s.strip() for s in a.stages.split(",")] if a.stages else None
        force = {s.strip() for s in a.force.split(",") if s.strip()}
        unknown = [s for s in (stages or []) + sorted(force) if s not in STAGES]
        if unknown:
            raise ConfigError(f"unknown stages {unknown}")
        rnd = Round(cfg, a.out, worlds_root=a.worlds_root, data_root=a.data_root, hooks=hooks)
    except (ConfigError, OSError, ValueError, KeyError) as e:
        print(f"mbl_e2e: {e}", file=sys.stderr)
        return 2
    if a.plan:
        print(json.dumps({"config": cfg, "fingerprint": rnd.fp, "formal": is_formal(cfg), "runs": rnd.plan()}, ensure_ascii=False, indent=1))
        return 0
    try:
        return rnd.run(stages, force)
    except _Mismatch as e:
        print(f"mbl_e2e: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
