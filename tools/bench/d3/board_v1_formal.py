#!/usr/bin/env python3
"""tools/bench/d3/board_v1_formal.py：排行榜 v1 正式评测（D3-AC-43、D3-AC-57；AWR-05 §5.6.9；M27-FR-005、FR-114、FR-124、FR-160、FR-164、
FR-166、FR-177 至 FR-183；ADR-182、ADR-191、ADR-195；B21）。

在 test 划分 v1（8 个 test 事件，每赛道 2 个）上完成 `std3-l2` 与 `std3-l1` 两个分轨的正式评测：3 个基线参考行与内部提交条目，各条目
在每个赛道的 2 个 test 事件 × K = 5 个秘密种子 × R = 2 次重复上运行（Realistic 档 gz 锁步）。子命令按阶段执行，同一数据根与 `--out`
再次执行即续跑：

1. `seal`（管理员，root）：读取划分登记（`<data_root>/bench/splits.json`）的 test 事件，经 `HiddenStore`（审计 `truth_read`）读取各事件
   `plan.json` 的 event_type，以 `aggregate.seal_board` 封板：`test_split_version = SplitRegistry.digest()`、平台镜像摘要（gz、M25 Worker 与
   Env 所在的宿主镜像、基线镜像）、GPU 型号与 CPU ISA 等级；EvalStore 写入 `<data_root>/bench/evalstore/<版本>.json`（0600，属主为
   Evaluator 服务用户），服务端配置 `<data_root>/boards/<版本>/config.json` = 模板 + 封板记录 + EvalStore 路径 + 运行参数（`worlds_root`、
   `events_root`、`events.<test 事件>{world_id, version, starts}`、dev 事件上的冒烟规格 `smoke.run_spec`、`gz`、`agent_image`；不含秘密）；
   test 事件经评测视图 `<data_root>/eval-events/<eid> → ../hidden/<eid>/event`（目录 0700）到达 Env；发布空榜并设为当前版本；
   封板记录（只含承诺值）另写 `--lock-out`。
2. `baselines`（Evaluator 服务用户）：基线参考行的正式 Run（`aggregate.expand_runs` × 基线），种子明文按（赛道、事件、k）取自 EvalStore，
   `seed_commit = HMAC-SHA256(K_eval, run_id ‖ ":" ‖ seed)`；经 `mbl_e2e.execute_run` 运行（隔离容器、`bench/obs.v1` 逐帧审计、锁步核对、
   运行清单 `bench/run_manifest.v1` 只含 seed_commit），记录写 `<out>/records/<run_id>.json`（不含种子明文）；进程池并行 `--jobs`。
3. `submit`（Evaluator 服务用户）：登记一个内部提交（`bench/submission.v1`，所选分轨的全部赛道，可见性 board），由
   `awr.bench.subworker.process_one` 逐阶段处理（扫描 → 冒烟 → v1 正式评测 → 评分并上榜）；队列中排在前面的提交由各自的进程领取。
4. `board`（Evaluator 服务用户）：基线评测行写入 `rows.jsonl`（幂等：已写入的与记录不符即报错），重算并发布榜单，再连算一次核对
   `board.json`、`details.json`、`listing.json` 逐字节一致。
5. `seal-truth`（管理员）：test 事件 Run 目录中的真值层（`dataset/<eid>/runs/<run_id>/truth/`，含 `run_seed.json`）移入
   `hidden/<eid>/truth/runs/<run_id>/`（目录 0700、文件 0600、属主为 Evaluator 服务用户），写审计 `hidden_seal`；之后 `misplaced` 须为空。
6. `report`（管理员）：逐项判定并写 `<out>/board_v1_formal.json`（`drone4d.bench.board_v1_formal.v1`）：条目与 Run 完整性、全部 Run 为
   gz 锁步与白名单审计通过、榜单值由存储分量以 `lane_metrics(K_board)` 重算逐字节一致、`weights = null` 且无合成主得分、真值访问审计外部
   访问为 0 与越权 691（负向探针）、接口可见性（匿名 404、invited 可读全部"分轨 × 赛道"）、隐藏区无错放与权限、文本产物中种子明文与
   密钥命中为 0。
3a. `resume`（Evaluator 服务用户）：处理进程意外终止后续跑停在 EVALUATING 的提交：沿用已完成 Run 的评测结果，删除中断 Run 的残留后
   重新运行其余 Run（计入平台重跑），再评分并上榜（`resume_submission`）。
7. `rebuild-timing`：以本榜单版本的真实配置（K_board 与 test 事件）构造单分轨 100 个条目 × 4 个赛道的评测行，`build_board`
   （n_boot = 10 000）计时 `--repeat` 次取中位（D3-AC-57 (h) 的 ≤ 60 s，dmax CPU 池）。

Run 标识：基线 `<版本>-<l1|l2>-<基线>-<事件>-k<k>r<rep>`，提交 `<submission_id>-<事件>-k<k>r<rep>`（subworker）；运行目录
`<data_root>/dataset/<事件>/runs/<run_id>/`。每个 test 事件 Run 在开始与评测时各写一条 `truth_read` 审计（主体 `svc:evaluator`，角色
evaluator，`job = <--job>`，op 为 `env` 与 `evaluate`）。

用法（dmax 上在宿主容器内，数据根挂载为 `/data/d3`，见 `docs/impl/D3-排行榜v1-正式评测记录.md`）：
    python tools/bench/d3/board_v1_formal.py --data-root /data/d3 --out out/b21 seal --worlds-root W --gz-image d3-gz:b21 \\
        --host-image d3-compile:b21 --agent-image d3-agent-py:b21 --gpu 3 [--board-version v1] [--lock-out configs/bench.d3/board-v1/lock.json]
    python tools/bench/d3/board_v1_formal.py --data-root /data/d3 --out out/b21 baselines --track std3-l2 --jobs 3
    python tools/bench/d3/board_v1_formal.py --data-root /data/d3 --out out/b21 submit --track std3-l2 --name relay-l2 \\
        --image d3-example-relay-scan:b21 --cmd "python -m drone4d.serve relay_scan.agent:Agent" --team d3-research --owner b21
    python tools/bench/d3/board_v1_formal.py --data-root /data/d3 --out out/b21 board | seal-truth | report | rebuild-timing
退出码：0 成功（report 为全部判定通过）；1 失败或判定不通过；2 参数或配置错误。
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import functools
import json
import os
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
for _p in (ROOT / "tools/sdk/drone4d/src", ROOT / "python", Path(__file__).resolve().parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import mbl_e2e as E  # noqa: E402

REPORT_SCHEMA = "drone4d.bench.board_v1_formal.v1"
EVAL_UID = 10002                     # Evaluator 服务用户（docs/impl/D3-隐藏区与离机备份.md）
WORLD_ID = "oryahovo-crop1"
# 起飞位：crop-handoff-001 的平屋顶起飞位（E10：8 个 test 事件的脚本演员距起飞位 ≥ 18.3 m）
STARTS = [{"kind": "pose", "pos_enu_m": [76.0, y, 0.0], "yaw_deg": 90.0} for y in (-78.0, -82.0, -86.0)]
SMOKE_EVENT = ("crop-handoff-002", 1)   # 冒烟规格所在的 dev 事件（交接类 P0b 形式）
SMOKE_OBS = "L1"
BASELINE_TEAM = "D3 reference"
JOB = "b21-formal"
TEXT_SUFFIXES = (".json", ".jsonl", ".log", ".txt", ".yaml", ".yml", ".csv", ".md")
COMPOSITE_KEYS = ("composite", "score", "weights_applied")


class FormalError(Exception):
    """阶段失败（退出码 1）。"""


# ------------------------------------------------------------------------------------------------------------------ 工具
def _log_to(path: Path | None):
    def log(m: str) -> None:
        line = f"{E.utc_now()} {m}"
        print(line, flush=True)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    return log


@contextlib.contextmanager
def board_lock(bdir: Path):
    """同一榜单版本的评测行追加与发布互斥（多个提交进程与 `board` 阶段并发时）：`<board>/.write.lock` 上的 flock。"""
    p = Path(bdir) / ".write.lock"
    fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _locked(fn, bdir_of):
    if getattr(fn, "_board_locked", False):                  # 已包装（同一进程再次调用）：不重复加锁（flock 按打开的文件描述计，重入即死锁）
        return fn

    @functools.wraps(fn)
    def wrapper(board_dir, *a, **kw):
        with board_lock(bdir_of(board_dir)):
            return fn(board_dir, *a, **kw)
    wrapper._board_locked = True
    return wrapper


def image_id(ref: str, docker: str = "docker") -> str:
    r = subprocess.run([docker, "image", "inspect", ref, "--format", "{{.Id}}"], capture_output=True, text=True, timeout=60, check=False)
    if r.returncode != 0 or not r.stdout.strip().startswith("sha256:"):
        raise FormalError(f"image {ref} not found: {r.stderr.strip()[:200]}")
    return r.stdout.strip()


def cpu_isa_level(cpuinfo: str | None = None) -> str:
    """x86-64 微架构等级（psABI v2/v3/v4），取自 /proc/cpuinfo 的 flags（与 determinism_official 同一口径）。"""
    txt = cpuinfo if cpuinfo is not None else Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^flags\s*:\s*(.+)$", txt, re.M)
    flags = set(m.group(1).split()) if m else set()
    lvl = "x86-64"
    if {"sse4_2", "popcnt", "ssse3"} <= flags:
        lvl = "x86-64-v2"
        if {"avx2", "fma", "bmi2", "movbe"} <= flags:
            lvl = "x86-64-v3"
            if {"avx512f", "avx512bw", "avx512cd", "avx512dq", "avx512vl"} <= flags:
                lvl = "x86-64-v4"
    return lvl


def gpu_model(index: str | None) -> str:
    cmd = ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"] + (["-i", str(index)] if index is not None else [])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    except OSError as e:
        raise FormalError(f"nvidia-smi is not available ({e}): run seal with the GPU attached or give the model") from None
    names = [x.strip() for x in r.stdout.splitlines() if x.strip()]
    if r.returncode != 0 or not names:
        raise FormalError(f"nvidia-smi failed: {r.stderr.strip()[:200]}")
    return names[0]


def _bv_dir(data_root: Path, bv: str) -> Path:
    return Path(data_root) / "boards" / bv


def _store_path(data_root: Path, bv: str) -> Path:
    return Path(data_root) / "bench" / "evalstore" / f"{bv}.json"


def _load(data_root: Path, bv: str) -> tuple[dict, dict]:
    """→ (服务端配置, EvalStore)；EvalStore 以明文复核封板记录（失败关闭）。"""
    from awr.bench.boardstore import load_config
    from awr.bench.eval.aggregate import verify_lock

    cfg = load_config(_bv_dir(data_root, bv))
    store = E.read_json(Path(cfg["evalstore"]))
    p = verify_lock(cfg["lock"], store, seeds_per_event=int(cfg["seeds_per_event"]))
    if p:
        raise FormalError(f"evalstore does not match the lock of {bv}: {p[:3]}")
    return cfg, store


def _all_seeds(store: dict) -> list[int]:
    return sorted({int(s) for ln in store["seeds"].values() for ss in ln.values() for s in ss})


def _test_events(cfg: dict) -> dict[str, str]:
    """test 事件 → 赛道（取自封板记录）。"""
    return {e: ln["lane"] for ln in cfg["lock"]["lanes"] for e in ln["test_events"]}


# ------------------------------------------------------------------------------------------------------------------ 审计包装
def audited_run_job(rs: dict, *, data_root: str, test_events: list[str], job: str = JOB, seed_commit: str | None = None, **kw) -> dict:
    """`awr.bench.runner.run_job` 的审计包装：test 事件的 Run 在 Env 读取磁带前与评测读取事件真值后各写一条 `truth_read`（主体
    `svc:evaluator`，op 为 `env` 与 `evaluate`，路径为 hidden 中的事件版本目录）；`seed_commit` 未由调用方给出时取绑定值（基线 Run）。"""
    from awr.bench.auth import AuditLog
    from awr.bench.runner import run_job
    from awr.bench.split import SERVICE_EVALUATOR, HiddenStore

    ev = rs["event"]["id"]
    hs = HiddenStore.for_root(Path(data_root), AuditLog(Path(data_root) / "audit")) if ev in test_events else None
    run_id = str(kw.get("run_id"))
    if hs is not None:
        hs.authorize(SERVICE_EVALUATOR, hs.event_dir(ev, int(rs["event"].get("version", 1))), event_id=ev, run_id=run_id, job=job, op="env")
    if kw.get("seed_commit") is None:
        kw["seed_commit"] = seed_commit
    res = run_job(rs, **kw)
    if hs is not None:
        hs.authorize(SERVICE_EVALUATOR, hs.event_dir(ev, int(rs["event"].get("version", 1))) / "truth", event_id=ev, run_id=run_id, job=job,
                     op="evaluate")
    return res


# ------------------------------------------------------------------------------------------------------------------ 1 封板
def seal_board_version(data_root: Path, *, board_version: str, worlds_root: Path, platform_refs: dict[str, str], gz_image: str, agent_image: str,
                       gpu: str | None, template: Path | None = None, lock_out: Path | None = None, duration_s: float | None = None,
                       eval_uid: int | None = EVAL_UID, docker: str = "docker", log=print, gpu_name: str | None = None,
                       isa: str | None = None, digests: dict[str, str] | None = None, n_boot: int = 10_000) -> dict:
    """封板并写服务端配置、EvalStore、评测视图与空榜（模块文档第 1 条）。`duration_s` 只用于开发演练（覆盖 Run 时长，不计正式口径）。
    → 摘要（不含秘密）。`digests` 给出时不调用 docker（测试）；`gpu_name`、`isa` 给出时不探测。"""
    from awr.bench.auth import AuditLog
    from awr.bench.boardstore import publish
    from awr.bench.eval.aggregate import board_config, load_board_template, seal_board
    from awr.bench.split import SERVICE_EVALUATOR, HiddenStore, SplitRegistry
    from awr.bench.submissions import SubmissionStore

    data_root = Path(data_root)
    bdir = _bv_dir(data_root, board_version)
    sp = _store_path(data_root, board_version)
    if (bdir / "config.json").exists() or sp.exists():
        raise FormalError(f"board version {board_version} is already sealed ({bdir / 'config.json'}); board versions are never re-sealed in place")
    audit = AuditLog(data_root / "audit")
    reg = SplitRegistry.for_root(data_root, audit=audit)
    hs = HiddenStore.for_root(data_root, audit)
    tests = reg.events("test")
    if not tests:
        raise FormalError(f"{data_root}/bench/splits.json registers no test event")
    types, versions = {}, {}
    for e in tests:
        vs = [int(d.name[1:]) for d in HiddenStore._version_dirs(hs.root / e / "event")]
        if not vs:
            raise FormalError(f"test event {e} has no version under hidden/{e}/event")
        versions[e] = max(vs)
        plan = json.loads(hs.read_bytes(SERVICE_EVALUATOR, hs.event_file(e, versions[e], "plan.json"), event_id=e, job=JOB))
        types[e] = str(plan["event_type"])
    tpl = load_board_template(template)
    tpl = {**tpl, "board_version": board_version}
    plat = dict(digests) if digests is not None else {k: image_id(ref, docker) for k, ref in platform_refs.items()}
    lock, store = seal_board(tpl, types, test_split_version=reg.digest(), platform=plat, gpu_model=gpu_name or gpu_model(gpu),
                             cpu_isa_level=isa or cpu_isa_level(), sealed_utc=E.utc_now())
    sp.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(sp.parent, 0o700)
    E.write_json(sp, store, mode=0o600)
    view = data_root / "eval-events"
    view.mkdir(parents=True, exist_ok=True)
    os.chmod(view, 0o700)
    for e in sorted(types):
        link = view / e
        if not link.is_symlink():
            link.symlink_to(Path("..") / "hidden" / e / "event")
    tracks = {t["track"]: t for t in tpl["tracks"]}
    events = {e: {"world_id": WORLD_ID, "version": versions[e], "starts": STARTS, **({"duration_s": float(duration_s)} if duration_s else {})}
              for e in sorted(types)}
    smoke = {"schema": "drone4d.bench.run_spec.v1", "experiment": {"name": f"board {board_version} smoke", "project": "d3-board-v1"},
             "world": {"id": WORLD_ID}, "event": {"id": SMOKE_EVENT[0], "version": SMOKE_EVENT[1]},
             "fleet": [{"uav_id": f"uav-{i + 1:02d}", "vehicle_ref": tracks["std3-l2"]["vehicle_ref"], "start": s} for i, s in enumerate(STARTS)],
             "policy": {"kind": "reference", "name": "static_guard"}, "physics": "kinematic", "obs_level": SMOKE_OBS, "noise": "realistic",
             "comm": tracks["std3-l2"]["run_spec"]["comm"], "duration_s": 20, "ranked": False}
    cfg = board_config(tpl, lock, sp)
    cfg.update({"worlds_root": str(Path(worlds_root)), "events_root": str(view), "events": events,
                "smoke": {"run_spec": smoke, "events_root": str(data_root / "events")}, "physics": "realistic",
                "gz": {"launcher": "docker", "image": gz_image}, "agent_image": agent_image,
                "formal": {"job": JOB, "sealed_utc": lock["sealed_utc"], "images": dict(platform_refs), "dry_run": bool(duration_s)}})
    bdir.mkdir(parents=True, exist_ok=True)
    E.write_json(bdir / "config.json", cfg, mode=0o600)
    if lock_out is not None:
        E.write_json(Path(lock_out), lock)
    with board_lock(bdir):
        publish(bdir, SubmissionStore(data_root / "submissions").board_view(), n_boot=n_boot)
    (data_root / "boards" / "current").write_text(board_version + "\n", encoding="utf-8")
    if eval_uid is not None and os.geteuid() == 0:            # 评测作业以 Evaluator 服务用户运行：数据目录与审计属主
        for p in (sp.parent, sp, view, *view.iterdir(), data_root / "boards", bdir, *bdir.iterdir(), data_root / "boards" / "current",
                  data_root / "audit", *(data_root / "audit").glob("access-*.jsonl")):
            os.lchown(p, eval_uid, eval_uid)
        for d in ("submissions", "scan"):
            if not (data_root / d).exists():
                (data_root / d).mkdir()
                os.chown(data_root / d, eval_uid, eval_uid)
        (data_root / "dataset").mkdir(exist_ok=True)
        for e in types:
            (data_root / "dataset" / e).mkdir(exist_ok=True)
            os.chown(data_root / "dataset" / e, eval_uid, eval_uid)
    out = {"board_version": board_version, "test_split_version": lock["test_split_version"], "seed_set_commit": lock["seed_set_commit"],
           "lanes": {ln["lane"]: ln["test_events"] for ln in lock["lanes"]}, "platform": lock["platform"], "gpu_model": lock["gpu_model"],
           "cpu_isa_level": lock["cpu_isa_level"], "evaluator_major": lock["evaluator_major"], "event_types": types}
    log(f"[seal] {board_version}: {json.dumps(out['lanes'])} split {lock['test_split_version'][:16]} seed_set {lock['seed_set_commit'][:16]}")
    return out


# ------------------------------------------------------------------------------------------------------------------ 2 基线
def plan_baselines(cfg: dict, *, track: str, baselines: list[str] | None = None, lanes: list[str] | None = None) -> list[dict]:
    """基线正式 Run 清单（不含种子）：`expand_runs(track, lanes)` × 基线。"""
    from awr.bench.eval.aggregate import expand_runs

    t = next((x for x in cfg["tracks"] if x["track"] == track), None)
    if t is None:
        raise FormalError(f"track {track} is not in board {cfg['board_version']}")
    lv = t["obs_level"].lower()
    etype = {ln["lane"]: ln.get("event_type") or ln["lane"] for ln in cfg["lock"]["lanes"]}
    out = []
    for b in baselines or list(cfg["baselines"]):
        for it in expand_runs(cfg, track, lanes):
            rid = f"{cfg['board_version']}-{lv}-{b}-{it['event']}-k{it['seed_index']}r{it['rep']}"
            out.append({"key": rid, "run_id": rid, "kind": "formal", "group": track, "obs_level": t["obs_level"], "policy": b,
                        "seed_index": it["seed_index"], "rep": it["rep"], "lane": it["lane"], "event": it["event"], "event_type": etype[it["lane"]],
                        "track": track})
    return out


def _baseline_job(cfg: dict, store: dict, it: dict, *, data_root: Path, out: Path, docker: str) -> dict:
    from awr.bench.eval.aggregate import run_spec_for
    from awr.bench.subworker import seed_commit

    x = cfg["events"][it["event"]]
    rs = run_spec_for(cfg, it["track"], world_id=str(x["world_id"]), event_id=it["event"], event_version=int(x["version"]), starts=list(x["starts"]),
                      policy={"kind": "reference", "name": it["policy"]})
    if x.get("duration_s"):
        rs["duration_s"] = float(x["duration_s"])
    seed = int(store["seeds"][it["lane"]][it["event"]][it["seed_index"]])
    sc = seed_commit(bytes.fromhex(store["k_eval"]), it["run_id"], seed)
    return {**it, "seed": seed, "seed_commit": sc, "rs": rs, "policy_doc": {"kind": "reference", "name": it["policy"]},
            "run_dir": str(Path(data_root) / "dataset" / it["event"] / "runs" / it["run_id"]), "record": str(out / "records" / f"{it['run_id']}.json"),
            "worlds_root": str(cfg["worlds_root"]), "events_root": str(cfg["events_root"]), "physics": cfg["physics"], "gz": dict(cfg.get("gz") or {}),
            "agent_image": cfg["agent_image"], "docker": docker, "retries": 1, "round": f"board-{cfg['board_version']}", "split": "test",
            "hz": E._hz(rs), "images": {}, "log_path": str(out / "baselines.log"), "data_root": str(data_root),
            "test_events": sorted(_test_events(cfg))}


def _pool_baseline(job: dict) -> dict:
    rj = functools.partial(audited_run_job, data_root=job["data_root"], test_events=job["test_events"], seed_commit=job["seed_commit"])
    return E.execute_run(job, rj, _log_to(Path(job["log_path"])))


def _done(rec_p: Path, run_dir: Path) -> bool:
    if not rec_p.exists():
        return False
    rec = E.read_json(rec_p)
    ev = run_dir / "evaluation.json"
    return rec.get("status") == "COMPLETED" and ev.exists() and E.sha256_file(ev) == rec.get("evaluation_sha256")


def run_baselines(data_root: Path, out: Path, *, board_version: str, track: str, jobs: int, baselines: list[str] | None = None,
                  lanes: list[str] | None = None, docker: str = "docker", runner=None, log=print) -> dict:
    """基线正式 Run（续跑：COMPLETED 且 evaluation.json 摘要与记录一致的跳过）。`runner(job)` 替换单个 Run 的执行（测试）。"""
    cfg, store = _load(data_root, board_version)
    items = plan_baselines(cfg, track=track, baselines=baselines, lanes=lanes)
    todo, skipped = [], 0
    for it in items:
        rp, rd = out / "records" / f"{it['run_id']}.json", Path(data_root) / "dataset" / it["event"] / "runs" / it["run_id"]
        if _done(rp, rd):
            skipped += 1
            continue
        todo.append(_baseline_job(cfg, store, it, data_root=data_root, out=out, docker=docker))
    log(f"[baselines] {track}: {len(items)} planned, {skipped} completed earlier, {len(todo)} to run with {jobs} jobs")
    seeds = _all_seeds(store)
    if runner is not None or jobs <= 1:
        for j in todo:
            (runner or _pool_baseline)(j)
    else:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            futs = {ex.submit(_pool_baseline, j): j for j in todo}
            for f in as_completed(futs):
                j = futs[f]
                try:
                    f.result()
                except Exception as e:                       # 进程级故障：记为失败，续跑时重做
                    E.write_json(Path(j["record"]), {**{k: j[k] for k in ("key", "run_id", "kind", "group", "obs_level", "policy", "seed_index", "rep")},
                                                    "status": "FAILED", "attempts": 0,
                                                    "errors": [{"attempt": 0, "error": E.redact(f"{type(e).__name__}: {e}", seeds)[:600]}]})
                    log(f"{j['key']}: worker process failed: {type(e).__name__}")
    recs = [E.read_json(out / "records" / f"{it['run_id']}.json") if (out / "records" / f"{it['run_id']}.json").exists() else {**it, "status": "PENDING"}
            for it in items]
    bad = [r["run_id"] for r in recs if r.get("status") != "COMPLETED"]
    log(f"[baselines] {track}: {len(recs) - len(bad)} / {len(recs)} COMPLETED" + (f", not completed {bad[:5]}" if bad else ""))
    return {"planned": len(items), "completed": len(recs) - len(bad), "failed": bad, "skipped_resume": skipped}


# ------------------------------------------------------------------------------------------------------------------ 3 提交
def run_submission(data_root: Path, out: Path, *, board_version: str, track: str, name: str, image: str, cmd: list[str], team: str, owner: str,
                   role: str = "researcher", notes: str = "", smoke_s: float = 20.0, n_boot: int = 10_000, trivy_cache: Path | None = None,
                   trivy_image: str = "d3-trivy:dev", docker: str = "docker", poll_s: float = 10.0, process_one=None, run_job=None,
                   digest: str | None = None, log=print) -> dict:
    """登记一个内部提交（所选分轨的全部赛道，可见性 board）并由 subworker 处理至终态；`<out>/submissions/<name>.json` 记录
    submission_id（续跑时沿用，终于失败的重新登记）。→ 终态摘要。"""
    import awr.bench.subworker as sw
    from awr.agentsdk.schemas import bench_validator
    from awr.bench.submissions import SubmissionStore

    data_root, out = Path(data_root), Path(out)
    bdir = _bv_dir(data_root, board_version)
    cfg, _store = _load(data_root, board_version)
    st = SubmissionStore(data_root / "submissions")
    rec_p = out / "submissions" / f"{name}.json"
    sid = None
    if rec_p.exists():
        sid = E.read_json(rec_p).get("submission_id")
        if sid and st.get(sid)["state"] in ("REJECTED", "FAILED", "WITHDRAWN"):
            log(f"[submit] {name}: {sid} ended {st.get(sid)['state']}, registering a new submission")
            sid = None
    if not sid:
        dg = digest or image_id(image, docker)
        req = {"track": track, "image_digest": dg, "entry": {"cmd": [str(x) for x in cmd]}, "notes": notes or f"board {board_version} formal ({name})"}
        errs = [e.message for e in bench_validator("submission").iter_errors({"schema": "drone4d.bench.submission.v1", **req})]
        if errs:
            raise FormalError(f"submission request invalid: {errs[:3]}")
        b = E.read_json(bdir / "board.json")
        d = st.create(req, owner=owner, team=team, board_version=board_version, tracks={t["track"] for t in b["tracks"]},
                      lanes={x["lane"] for x in b["lanes"]}, role=role)
        sid = d["submission_id"]
        st.set_visibility(sid, "board")
        E.write_json(rec_p, {"name": name, "submission_id": sid, "track": track, "image": image, "image_digest": dg, "team": team,
                             "created_utc": E.utc_now()})
        log(f"[submit] {name}: {sid} RECEIVED on {board_version} {track} (image {image} {dg[:19]}…)")
    if process_one is None:
        sw.append_rows = _locked(sw.append_rows, lambda d: Path(d))          # 多个提交进程并发评分时的评测行与榜单互斥
        sw.publish = _locked(sw.publish, lambda d: Path(d))
        process_one = sw.process_one
    trivy = sw.TrivyScanner(cache_dir=trivy_cache, work_dir=data_root / "scan", image=trivy_image) if trivy_cache else None
    rj = run_job or functools.partial(audited_run_job, data_root=str(data_root), test_events=sorted(_test_events(cfg)))
    t0 = time.time()
    while st.get(sid)["state"] not in E.TERMINAL:
        nxt = st.next_received()
        if st.get(sid)["state"] == "RECEIVED" and nxt is not None and nxt["submission_id"] != sid:
            time.sleep(poll_s)                               # 队列中排在前面的提交由其自己的进程领取
            continue
        if st.get(sid)["state"] != "RECEIVED":
            time.sleep(poll_s)                               # 已由其他进程领取（续跑时）：等待其终态
            continue
        got = process_one(data_root, docker=docker, smoke_s=smoke_s, n_boot=n_boot, run_job=rj, trivy=trivy,
                          log=lambda m: log(f"[subworker] {m}"))
        if got is None and st.get(sid)["state"] == "RECEIVED":
            time.sleep(poll_s)
    d = st.get(sid)
    res = {"name": name, "submission_id": sid, "track": track, "state": d["state"], "history": [h["state"] for h in d["_meta"]["history"]],
           "reason": d.get("reason"), "wall_s": round(time.time() - t0, 1), "image_digest": d["image_digest"], "team": d["_meta"]["team"]}
    E.write_json(out / "submissions" / f"{name}.result.json", res)
    log(f"[submit] {name}: {sid} {d['state']}" + (f" {d.get('reason')}" if d["state"] != "DONE" else ""))
    return res


def resume_submission(data_root: Path, out: Path, *, board_version: str, name: str, docker: str = "docker", n_boot: int = 10_000, run_job=None,
                      log=print) -> dict:
    """处理进程意外终止（容器内 GPU 失效导致段错误等，Python 异常路径未执行）后续跑停在 EVALUATING 的提交（D3-AC-57 (d)：平台原因的
    Run 重跑且不计入提交方）。

    已完成的 Run（`evaluation.json` 存在且已按 run_id 记入 GPU 用量，即 `guarded_run` 已返回）沿用其评测结果，按 subworker 同一口径生成
    评测行；其余 Run 的残留目录（含 Env 写出的运行真值）删除后由 `subworker._evaluate_v1` 重新运行，中断的 Run 计入平台重跑次数；
    最后由 `subworker._score_v1` 评分并上榜。调用方须确认原处理进程已不存在。→ 摘要（同时写 `<out>/submissions/<name>.resume.json`）。"""
    import awr.bench.subworker as sw
    from awr.bench.boardstore import slim_evaluation
    from awr.bench.submissions import SubmissionStore

    data_root, out = Path(data_root), Path(out)
    rec = E.read_json(out / "submissions" / f"{name}.json")
    sid = rec["submission_id"]
    st = SubmissionStore(data_root / "submissions")
    d = st.get(sid)
    if d["state"] != "EVALUATING" or d["_meta"]["board_version"] != board_version:
        raise FormalError(f"{name}: {sid} is {d['state']} on {d['_meta']['board_version']}, nothing to resume")
    bdir = _bv_dir(data_root, board_version)
    cfg, _store = _load(data_root, board_version)
    policy = {"kind": "image", "image_digest": d["image_digest"], "entry": d["entry"]}
    common = {"worlds_root": Path(cfg["worlds_root"]), "events_root": Path(cfg["events_root"]), "agent_image": cfg.get("agent_image", "d3-agent-py:dev"),
              "docker": docker}
    team = d["_meta"]["team"]
    charged = {g["run_id"] for g in d["_meta"].get("gpu", [])}
    runs = sw._v1_runs(cfg, d, policy)
    done_rows, todo, aborted = {}, [], []
    for it in runs:
        run_dir = data_root / "dataset" / it["event"] / "runs" / it["run_id"]
        if it["run_id"] in charged and (run_dir / "evaluation.json").is_file() and not (run_dir / "termination.json").exists():
            ev = json.loads((run_dir / "evaluation.json").read_text(encoding="utf-8"))
            done_rows[it["run_id"]] = {"entry_id": f"{team}/{sid}", "team": team, "display_name": sid, "baseline": False, "track": d["track"],
                                       "lane": it["lane"], "event_type": it["event_type"], "event": it["event"], "seed": it["seed_index"],
                                       "rep": it["rep"], "run_id": it["run_id"], "submission_id": sid,
                                       "evaluation": slim_evaluation(ev, physics=sw._run_physics(run_dir))}
            continue
        if it["run_id"] in charged:
            raise FormalError(f"{name}: run {it['run_id']} was charged but has no usable evaluation; resolve by hand")
        if run_dir.exists():
            shutil.rmtree(run_dir)                           # 中断 Run 的残留（含运行真值与种子明文），重新运行
            aborted.append(it["run_id"])
        todo.append(it)
    log(f"[resume] {name}: {sid} {len(done_rows)} runs reused, {len(aborted)} interrupted ({', '.join(aborted) or '-'}), {len(todo)} to run")
    sw.append_rows = _locked(sw.append_rows, lambda b: Path(b))
    sw.publish = _locked(sw.publish, lambda b: Path(b))
    rj = run_job or functools.partial(audited_run_job, data_root=str(data_root), test_events=sorted(_test_events(cfg)))
    got = sw._evaluate_v1(data_root, st, d, cfg, todo, policy=policy, common=common, run_job=rj, log=lambda m: log(f"[subworker] {m}"))
    res = {"name": name, "submission_id": sid, "reused": len(done_rows), "interrupted": aborted, "rerun": len(todo), "resumed_utc": E.utc_now()}
    if got is not None:
        new_rows, reruns = got
        by_id = {**done_rows, **{r["run_id"]: r for r in new_rows}}
        rows = [by_id[it["run_id"]] for it in runs]          # 与 subworker 相同的 Run 次序
        sw._score_v1(st, st.get(sid), cfg, bdir, rows, reruns + len(aborted), n_boot=n_boot, log=lambda m: log(f"[subworker] {m}"))
    d = st.get(sid)
    res.update(state=d["state"], history=[h["state"] for h in d["_meta"]["history"]], reason=d.get("reason"))
    E.write_json(out / "submissions" / f"{name}.resume.json", res)
    E.write_json(out / "submissions" / f"{name}.result.json", {**E.read_json(out / "submissions" / f"{name}.result.json"), "state": d["state"],
                                                                "history": res["history"], "reason": d.get("reason"), "resumed": True}
                 if (out / "submissions" / f"{name}.result.json").exists() else
                 {"name": name, "submission_id": sid, "track": d["track"], "state": d["state"], "history": res["history"], "reason": d.get("reason"),
                  "image_digest": d["image_digest"], "team": team, "resumed": True})
    log(f"[resume] {name}: {sid} {d['state']}")
    return res


# ------------------------------------------------------------------------------------------------------------------ 4 榜单
def baseline_rows(cfg: dict, out: Path) -> list[dict]:
    """基线参考行的评测行（由记录与运行目录生成；`seed` 为种子序号）。缺 COMPLETED 记录时报错。"""
    from awr.bench.boardstore import slim_evaluation

    rows = []
    for t in cfg["tracks"]:
        for it in plan_baselines(cfg, track=t["track"]):
            rp = Path(out) / "records" / f"{it['run_id']}.json"
            rec = E.read_json(rp) if rp.exists() else {}
            if rec.get("status") != "COMPLETED":
                raise FormalError(f"baseline run {it['run_id']} is not COMPLETED ({rec.get('status', 'no record')})")
            rd = Path(rec["run_dir"])
            ev = E.read_json(rd / "evaluation.json")
            man = E.read_json(rd / "manifest.json")
            rows.append({"entry_id": f"baseline/{it['policy']}", "team": BASELINE_TEAM, "display_name": E.DISPLAY.get(it["policy"], it["policy"]),
                         "baseline": True, "track": it["track"], "lane": it["lane"], "event_type": it["event_type"], "event": it["event"],
                         "seed": it["seed_index"], "rep": it["rep"], "run_id": it["run_id"], "first_listed_unix_ns": 0,
                         "evaluation": slim_evaluation(ev, physics=man.get("physics"))})
    return rows


def _canon_rows(rows: list[dict]) -> list[str]:
    return sorted(json.dumps(r, sort_keys=True, ensure_ascii=False) for r in rows)


def publish_board(data_root: Path, out: Path, *, board_version: str, n_boot: int = 10_000, log=print) -> dict:
    """基线评测行写入（幂等）→ 发布 → 再连算一次核对逐字节一致。→ 摘要。"""
    from awr.bench.boardstore import append_rows, load_rows, publish
    from awr.bench.submissions import SubmissionStore

    data_root = Path(data_root)
    bdir = _bv_dir(data_root, board_version)
    cfg, _store = _load(data_root, board_version)
    want = baseline_rows(cfg, out)
    subs = SubmissionStore(data_root / "submissions")
    with board_lock(bdir):
        have = [r for r in load_rows(bdir) if r.get("baseline")]
        if not have:
            append_rows(bdir, want)
            log(f"[board] appended {len(want)} baseline rows to {board_version}")
        elif _canon_rows(have) != _canon_rows(want):
            raise FormalError(f"{bdir}/rows.jsonl holds {len(have)} baseline rows that differ from the run records; publish a new board version")
        publish(bdir, subs.board_view(), n_boot=n_boot)
        files = ("board.json", "details.json", "listing.json")
        first = {f: (bdir / f).read_bytes() for f in files}
        publish(bdir, subs.board_view(), n_boot=n_boot)
        second = {f: (bdir / f).read_bytes() for f in files}
    same = {f: first[f] == second[f] for f in files}
    b = json.loads(first["board.json"])
    res = {"board_version": board_version, "baseline_rows": len(want), "entries": len(b["entries"]), "twice_identical": same,
           "sha256": {f: E.sha256_bytes(first[f]) for f in files}}
    E.write_json(out / "board_publish.json", res)
    log(f"[board] {board_version}: {len(b['entries'])} entries, byte-identical twice {same}")
    if not all(same.values()):
        raise FormalError(f"board {board_version} is not byte-identical when computed twice: {same}")
    return res


# ------------------------------------------------------------------------------------------------------------------ 5 真值入隐藏区
def seal_run_truth(data_root: Path, *, board_version: str, eval_uid: int | None = EVAL_UID, log=print) -> dict:
    """test 事件 Run 目录中的真值层移入 `hidden/<eid>/truth/runs/<run_id>/`（M27-FR-114）；同一文件已在 hidden 且内容相同即删除源。"""
    from awr.bench.auth import AuditLog
    from awr.bench.split import HiddenStore

    data_root = Path(data_root)
    cfg, _ = _load(data_root, board_version)
    audit = AuditLog(data_root / "audit")
    hs = HiddenStore.for_root(data_root, audit)
    moved = conflicts = 0
    for ev in sorted(_test_events(cfg)):
        for tdir in sorted((data_root / "dataset" / ev / "runs").glob("*/truth")):
            rid = tdir.parent.name
            dst_root = hs.truth_dir(ev) / "runs" / rid
            n = 0
            for f in sorted(p for p in tdir.rglob("*") if p.is_file()):
                dst = dst_root / f.relative_to(tdir)
                hs._mkdir(dst.parent)
                if dst.exists():
                    if dst.read_bytes() == f.read_bytes():
                        f.unlink()
                    else:
                        conflicts += 1
                    continue
                shutil.move(str(f), str(dst))
                os.chmod(dst, 0o600)
                n += 1
            for d in sorted((x for x in tdir.rglob("*") if x.is_dir()), key=lambda x: len(x.parts), reverse=True):
                with contextlib.suppress(OSError):
                    d.rmdir()
            with contextlib.suppress(OSError):
                tdir.rmdir()
            if n:
                audit.write("hidden_seal", event_id=ev, run_id=rid, n=n, conflicts=0, process={"argv0": "board_v1_formal.py"})
            moved += n
    fixed = hs.fix_permissions(uid=eval_uid, gid=eval_uid) if eval_uid is not None and os.geteuid() == 0 else 0
    res = {"moved": moved, "conflicts": conflicts, "perm_fixed": fixed}
    log(f"[seal-truth] {res}")
    return res


# ------------------------------------------------------------------------------------------------------------------ 6 报告
def _run_dirs(data_root: Path, rows: list[dict]) -> dict[str, Path]:
    return {r["run_id"]: Path(data_root) / "dataset" / r["event"] / "runs" / r["run_id"] for r in rows}


def _contains_composite(x: Any) -> list[str]:
    if isinstance(x, dict):
        return [k for k, v in x.items() if (k in COMPOSITE_KEYS and v is not None)] + [p for v in x.values() for p in _contains_composite(v)]
    if isinstance(x, list):
        return [p for v in x for p in _contains_composite(v)]
    return []


def scan_text_for_secrets(roots: list[Path], needles: list[str], *, skip: tuple[Path, ...] = ()) -> dict:
    """文本产物（`TEXT_SUFFIXES`）中秘密串的命中：→ {files, hits[{path, needle_index}]}（不回显秘密）。"""
    files, hits = 0, []
    pats = [n.encode() for n in needles if n]
    for root in roots:
        if not Path(root).exists():
            continue
        for p in Path(root).rglob("*"):
            if not p.is_file() or p.suffix not in TEXT_SUFFIXES or any(s in p.parents or p == s for s in skip):
                continue
            files += 1
            b = p.read_bytes()
            for i, n in enumerate(pats):
                if n in b:
                    hits.append({"path": str(p), "needle_index": i})
    return {"files": files, "hits": hits}


def _needles(store: dict) -> list[str]:
    out = []
    for s in _all_seeds(store):
        out += [str(s), f"{s:x}", f"{s:016x}"]
    return [*out, store["k_eval"], store["k_board"]]


def build_report(data_root: Path, out: Path, *, board_version: str, gpu_log: Path | None = None, probe: bool = True, n_boot: int = 10_000,
                 eval_uid: int = EVAL_UID, log=print) -> dict:
    """逐项判定（模块文档第 6 条）→ 报告；全部通过时 `passed = true`。"""
    from awr.bench.auth import AuditLog, Principal
    from awr.bench.boardstore import load_listing, load_rows
    from awr.bench.eval.aggregate import board_key, lane_metrics
    from awr.bench.split import HiddenStore, SplitAccessDenied, SplitRegistry, read_audit, truth_access_summary
    from awr.bench.submissions import SubmissionStore

    data_root, out = Path(data_root), Path(out)
    bdir = _bv_dir(data_root, board_version)
    cfg, store = _load(data_root, board_version)
    board = E.read_json(bdir / "board.json")
    details = E.read_json(bdir / "details.json")
    rows = load_rows(bdir)
    subs = SubmissionStore(data_root / "submissions")
    listing = load_listing(bdir) or {}
    checks: list[dict] = []

    def check(cid: str, ok: bool, summary: str, **detail) -> None:
        checks.append({"id": cid, "passed": bool(ok), "summary": summary, **detail})

    k, r = int(cfg["seeds_per_event"]), int(cfg["repeats"])
    lanes = {ln["lane"]: ln["test_events"] for ln in cfg["lock"]["lanes"]}
    per_lane = k * r * int(cfg["events_per_lane"])
    # 1 条目与 Run 完整性
    ents = board["entries"]
    by_tl: dict[tuple[str, str], list[dict]] = {}
    for e in ents:
        by_tl.setdefault((e["track"], e["lane"]), []).append(e)
    comp = {}
    for t in cfg["tracks"]:
        for ln in lanes:
            es = by_tl.get((t["track"], ln), [])
            nb = sum(e["baseline"] for e in es)
            ns = sum(not e["baseline"] for e in es)
            comp[f"{t['track']}/{ln}"] = {"baselines": nb, "submissions": ns, "statuses": sorted({e["status"] for e in es})}
    full = all(v["baselines"] == len(cfg["baselines"]) and v["submissions"] >= 2 for v in comp.values())
    check("entries", full, f"every track x lane lists {len(cfg['baselines'])} baselines and >= 2 submission entries", per_track_lane=comp)
    cnt: dict[tuple[str, str, str], dict] = {}
    for x in rows:
        c = cnt.setdefault((x["track"], x["lane"], x["entry_id"]), {"n": 0, "valid": 0, "keys": set()})
        c["n"] += 1
        c["valid"] += bool(x["evaluation"]["ranked_valid"])
        c["keys"].add((x["event"], int(x["seed"]), int(x["rep"])))
    want_keys = {ln: {(e, s, p) for e in evs for s in range(k) for p in range(r)} for ln, evs in lanes.items()}
    runs_ok = all(len(c["keys"]) == per_lane and c["keys"] == want_keys[ln] for (tr, ln, eid), c in cnt.items()
                  if any(e["entry_id"] == eid and e["track"] == tr and e["lane"] == ln for e in ents))
    check("runs_complete", runs_ok and all(e["status"] == "valid" for e in ents),
          f"each listed entry has {per_lane} runs per lane ({cfg['events_per_lane']} test events x K={k} x R={r}) and status valid",
          rows=len(rows), entries=len(ents), statuses=sorted({e["status"] for e in ents}),
          invalid_runs=sum(c["n"] - c["valid"] for c in cnt.values()))
    # 2 gz 锁步与白名单审计

    listed = [x for x in rows if x.get("baseline") or x.get("submission_id") in set(listing.values())]
    dirs = _run_dirs(data_root, listed)
    bad_lock, bad_audit, phys, walls = [], [], {}, {}
    for x in listed:
        man = E.read_json(dirs[x["run_id"]] / "manifest.json")
        rs_hz = (10, 250)
        ls = E.lockstep_check(man, *rs_hz)
        if not ls.get("applicable") or not ls.get("ok") or man.get("physics") != "realistic":
            bad_lock.append(x["run_id"])
        au = man.get("obs_audit") or {}
        if not au.get("passed") or au.get("unknown_fields", 0) or au.get("schema_violations", 0):
            bad_audit.append(x["run_id"])
        phys[man.get("physics")] = phys.get(man.get("physics"), 0) + 1
        sim = int(man.get("n_ticks", 0)) / 10.0
        if sim > 0:
            walls.setdefault(f"{x['track']}", []).append(float(man.get("wall_s", 0.0)) / sim)
        if (man.get("tier_info") or {}).get("vehicle", {}).get("name") != "p600_bench":
            bad_lock.append(f"{x['run_id']}:vehicle")
    check("gz_lockstep", not bad_lock, f"{len(listed)} listed runs are realistic gz lockstep (iterations = 25 x (ticks + preroll)) on p600_bench",
          physics=phys, failed=bad_lock[:10])
    check("obs_audit", not bad_audit, "bench/obs.v1 whitelist audit passed with 0 unknown fields on every listed run", failed=bad_audit[:10])
    # 3 榜单值由存储分量重算逐字节一致
    secret = board_key(cfg)
    listed_keep = {(x["track"], x["lane"], x["entry_id"]) for x in listed}
    mismatch = []
    for e in ents:
        rs = sorted((x for x in listed if (x["track"], x["lane"], x["entry_id"]) == (e["track"], e["lane"], e["entry_id"])),
                    key=lambda x: (x["event"], int(x["seed"]), int(x["rep"]), str(x.get("run_id", ""))))
        valid = [x for x in rs if x["evaluation"]["ranked_valid"]]
        met = lane_metrics(valid, key=(e["entry_id"], board_version, e["lane"]), n_boot=n_boot, secret=secret)[0]
        shown = {m: {k: x for k, x in v.items() if k not in ("rank", "tie_group")} for m, v in e["metrics"].items()}   # 名次与并列组为派生字段
        if json.dumps(met, sort_keys=True) != json.dumps(shown, sort_keys=True):
            mismatch.append(f"{e['track']}/{e['lane']}/{e['entry_id']}")
        for m, v in e["metrics"].items():
            if v["value"] is not None and not (v["ci95"][0] <= v["value"] <= v["ci95"][1]):
                mismatch.append(f"{e['entry_id']}:{m}:ci")
    check("recompute", not mismatch and len(listed_keep) == len(ents),
          "board metrics equal lane_metrics(stored components, K_board) byte for byte; CI contains the point value", mismatched=mismatch[:10])
    pub = E.read_json(out / "board_publish.json") if (out / "board_publish.json").exists() else {}
    check("twice_identical", bool(pub) and all((pub.get("twice_identical") or {}).values()) and
          pub.get("sha256", {}).get("board.json") == E.sha256_file(bdir / "board.json"),
          "board.json, details.json and listing.json are byte-identical when computed twice", publish=pub.get("sha256"))
    comp_keys = _contains_composite(board) + _contains_composite(details)
    check("no_composite", board.get("score_version") == "d3-score-1" and not comp_keys and all("composite" not in e for e in ents),
          "score_version d3-score-1 (weights null): no composite score in board.json or details.json", found=comp_keys[:5])
    ranks_ok = all(e["baseline"] or e["status"] != "valid" or (e.get("dominance_tier") is not None and all(
        "rank" in v and "tie_group" in v for v in e["metrics"].values())) for e in ents)
    check("ranks_ties_tiers", ranks_ok, "valid submission entries carry per-metric rank and tie group and a dominance tier; baselines are not ranked")
    # 4 只收 gz 锁步与提交
    sub_docs = {d["submission_id"]: d for d in subs.all()}
    on = [sid for sid in listing.values()]
    sub_ok = all(sub_docs[s]["state"] == "DONE" and sub_docs[s]["_meta"]["board_version"] == board_version for s in on)
    check("submissions", sub_ok and len(on) >= 2 * len(cfg["tracks"]),
          "submission entries come from DONE internal submissions on this board version (scan, smoke, formal evaluation, scoring)",
          submissions={s: {"state": sub_docs[s]["state"], "track": sub_docs[s]["track"], "team": sub_docs[s]["_meta"]["team"],
                           "history": [h["state"] for h in sub_docs[s]["_meta"]["history"]]} for s in on})
    # 5 真值访问审计与 691
    audit = AuditLog(data_root / "audit")
    hs = HiddenStore.for_root(data_root, audit)
    denied = None
    if probe:
        try:
            hs.authorize(Principal("b21-probe-researcher", "researcher", (), "token"), hs.event_dir(next(iter(_test_events(cfg))), 1) / "tape.bin",
                         event_id=next(iter(_test_events(cfg))), job=f"{JOB}-probe")
            denied = None
        except SplitAccessDenied as e:
            denied = getattr(e, "code", 691)
    summ = truth_access_summary(read_audit(data_root / "audit"))
    n_test_runs = sum(1 for x in rows if x["event"] in _test_events(cfg))
    check("truth_audit", summ["external"] == 0 and (not probe or denied == 691) and summ["internal"] >= 2 * n_test_runs,
          "test truth access audit: external reads 0; a researcher read is refused with 691; every formal run audited (env + evaluate)",
          audit=summ, probe_code=denied, formal_runs=n_test_runs)
    reg = SplitRegistry.for_root(data_root)
    mis = hs.misplaced(reg, events_root=data_root / "events", dataset_root=data_root / "dataset")
    perms = hs.permission_problems(uid=eval_uid, gid=eval_uid)
    check("hidden", not mis and not perms, "no test artifact or run truth outside hidden/; hidden/ is 0700/0600 owned by the Evaluator user",
          misplaced=mis[:10], n_misplaced=len(mis), perm_problems=perms[:10])
    if probe:                                                 # 接口可见性（D3-AC-43）：匿名 404，invited 可读全部"分轨 × 赛道"，无合成主得分
        api = api_visibility(data_root, board_version, board)
        check("api_visibility", api["passed"], "board API: anonymous requests get 404 (305); an invited member reads every track x lane with all "
              "listed entries and no composite score", api=api)
    # 6 秘密
    sp = _store_path(data_root, board_version)
    scan = scan_text_for_secrets([data_root, out], _needles(store), skip=(sp.parent, data_root / "hidden"))
    check("secrets", not scan["hits"] and len(_all_seeds(store)) == k * sum(len(v) for v in lanes.values()),
          "plaintext seeds, K_eval and K_board appear in no text artifact outside the EvalStore and hidden/", files_scanned=scan["files"],
          hits=scan["hits"][:10])
    # 7 GPU 独占
    gpu = summarize_gpu_log(gpu_log) if gpu_log is not None and Path(gpu_log).exists() else None
    if gpu is not None:
        check("exclusive_gpu", gpu["external_samples"] == 0, f"GPU {gpu.get('gpu')}: no external compute process in {gpu['samples']} samples",
              gpu=gpu)
    rep = {"schema": REPORT_SCHEMA, "board_version": board_version, "generated_utc": E.utc_now(), "checks": checks,
           "passed": all(c["passed"] for c in checks), "wall_ratio": {t: {"min": round(min(v), 3), "median": round(statistics.median(v), 3),
                                                                           "max": round(max(v), 3), "n": len(v)} for t, v in walls.items()},
           "board_sha256": E.sha256_file(bdir / "board.json")}
    E.write_json(out / "board_v1_formal.json", rep)
    for c in checks:
        log(f"  {'PASS' if c['passed'] else 'FAIL'} {c['id']}: {c['summary']}")
    log(f"[report] passed={rep['passed']} -> {out / 'board_v1_formal.json'}")
    return rep


MINE = ("d3-b21-", "d3-gz-", "d3-agent-")       # 本评测的驱动容器与其兄弟容器（gz、Agent）


def api_visibility(data_root: Path, board_version: str, board: dict) -> dict:
    """以本榜单版本的制品副本（`boards/<版本>/` 与提交文档，临时数据根）启动 bench API（TestClient，不监听端口），探测可见性：
    匿名请求 `/leaderboard` 与 `/board` 返回 404 `305`；invited 成员逐"分轨 × 赛道"读取，条目数等于榜单条目数且响应中无合成主得分。"""
    import tempfile

    from fastapi.testclient import TestClient

    from awr.api.bench_main import create_app
    from awr.bench.settings import BenchSettings

    p = "/api/bench/v1"
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "d3"
        (root / "boards").mkdir(parents=True)
        shutil.copytree(_bv_dir(data_root, board_version), root / "boards" / board_version)
        (root / "boards" / "current").write_text(board_version + "\n", encoding="utf-8")
        (root / "submissions").mkdir()
        for f in (Path(data_root) / "submissions").glob("sub-*.json"):
            shutil.copy2(f, root / "submissions" / f.name)
        app = create_app(BenchSettings(data_root=root, allowed_origins=(), secure_cookie=False))
        c = TestClient(app)
        h = {"Authorization": f"Bearer {app.state.auth.issue_token('b21-probe-invited', 'invited')[1]}"}
        anon = {}
        for u in (f"/leaderboard?board_version={board_version}", "/board"):
            r = c.get(p + u)
            anon[u] = {"http": r.status_code, "code": r.json().get("code") if isinstance(r.json(), dict) else None}
        lanes = {}
        for t in board["tracks"]:
            for ln in board["lanes"]:
                want = sum(1 for e in board["entries"] if e["track"] == t["track"] and e["lane"] == ln["lane"])
                r = c.get(f"{p}/leaderboard?board_version={board_version}&track={t['track']}&lane={ln['lane']}", headers=h)
                body = r.json()
                lanes[f"{t['track']}/{ln['lane']}"] = {"http": r.status_code, "entries": len(body.get("entries", [])), "expected": want,
                                                       "composite": _contains_composite(body)[:3]}
    ok = (all(v["http"] == 404 and v["code"] == 305 for v in anon.values())
          and all(v["http"] == 200 and v["entries"] == v["expected"] and not v["composite"] for v in lanes.values()))
    return {"passed": ok, "anonymous": anon, "invited": lanes}


def summarize_gpu_log(path: Path) -> dict:
    """GPU 采样日志（`tools` 外的宿主采样脚本按行写 JSON：{t, gpu, procs[{pid, name, mem_mib, sm, container, exited?}], util, mem_used_mib}）
    → 摘要：外部进程（容器名不以 `MINE` 开头）出现的采样数。

    短命进程可能在采样脚本读取 `/proc/<pid>/cgroup` 前退出（`container = null`，`exited = true`），此时按同一 pid 在其他采样中的
    归属认定（`attributed_by_pid`）；从未被归属的进程一律计为外部进程。"""
    samples = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    owner: dict[int, str] = {}
    for s in samples:
        for p in s.get("procs", []):
            if p.get("container"):
                owner.setdefault(int(p["pid"]), str(p["container"]))
    n, ext, by_pid, first, last, gpu, util_max, names = 0, 0, 0, None, None, None, 0, set()
    for s in samples:
        n += 1
        gpu = s.get("gpu", gpu)
        first = first or s["t"]
        last = s["t"]
        util_max = max(util_max, int(s.get("util") or 0))
        other = []
        for p in s.get("procs", []):
            c = p.get("container") or owner.get(int(p["pid"]))
            if c and not p.get("container"):
                by_pid += 1
            if not str(c or "").startswith(MINE):
                other.append(p)
        if other:
            ext += 1
            names |= {str(p.get("name")) for p in other}
    return {"gpu": gpu, "samples": n, "first_utc": first, "last_utc": last, "external_samples": ext, "external_names": sorted(names),
            "attributed_by_pid": by_pid, "util_max": util_max}


# ------------------------------------------------------------------------------------------------------------------ 7 重算计时
def rebuild_timing(data_root: Path, *, board_version: str, n_entries: int = 100, repeat: int = 3, n_boot: int = 10_000, log=print) -> dict:
    """单分轨 100 个条目 × 4 个赛道的榜单重算计时（D3-AC-57 (h)）：真实配置（K_board、test 事件、K、R），合成评测行，不写文件。"""
    import numpy as np

    from awr.bench.eval.aggregate import build_board

    cfg, _ = _load(data_root, board_version)
    track = cfg["tracks"][0]["track"]
    g = np.random.default_rng(11)
    rows = []
    for i in range(n_entries):
        mu = g.random(4)
        for ln in cfg["lock"]["lanes"]:
            for ev in ln["test_events"]:
                for s in range(int(cfg["seeds_per_event"])):
                    for rep in range(int(cfg["repeats"])):
                        c = np.clip(mu + g.normal(0, 0.05, 4), 0, 1)
                        rows.append({"entry_id": f"timing{i}/e{i}", "team": f"timing{i}", "display_name": f"e{i}", "baseline": False, "track": track,
                                     "lane": ln["lane"], "event_type": ln.get("event_type"), "event": ev, "seed": s, "rep": rep,
                                     "first_listed_unix_ns": i, "evaluation": {"components": dict(zip(E.METRICS, (float(x) for x in c), strict=True)),
                                                                                "ranked": True, "ranked_valid": True, "invalid_reasons": [],
                                                                                "physics": "realistic"}})
    times = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        b = build_board(rows, board_version=board_version, n_boot=n_boot, board=cfg)
        times.append(round(time.perf_counter() - t0, 3))
    n_ent = len(b["entries"])
    res = {"track": track, "entries": n_entries, "lanes": len(cfg["lock"]["lanes"]), "board_entries": n_ent, "rows": len(rows), "n_boot": n_boot,
           "seconds": times, "median_s": statistics.median(times), "cpus": os.cpu_count(),
           "affinity": len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None, "passed": statistics.median(times) <= 60.0}
    log(f"[rebuild-timing] {json.dumps(res)}")
    return res


# ------------------------------------------------------------------------------------------------------------------ 命令行
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="board_v1_formal.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--data-root", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--board-version", default="v1")
    ap.add_argument("--docker", default="docker")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seal")
    s.add_argument("--worlds-root", type=Path, required=True)
    s.add_argument("--gz-image", required=True)
    s.add_argument("--host-image", required=True, help="image of the host process (Env and the M25 worker run in it)")
    s.add_argument("--agent-image", required=True, help="platform agent image (reference baselines)")
    s.add_argument("--gpu", default=None, help="GPU index for the model name (nvidia-smi -i)")
    s.add_argument("--template", type=Path, default=None)
    s.add_argument("--lock-out", type=Path, default=None)
    s.add_argument("--duration-s", type=float, default=None, help="development rehearsal only: override the run duration")
    b = sub.add_parser("baselines")
    b.add_argument("--track", required=True)
    b.add_argument("--jobs", type=int, default=3)
    b.add_argument("--baselines", default=None)
    b.add_argument("--lanes", default=None)
    m = sub.add_parser("submit")
    m.add_argument("--track", required=True)
    m.add_argument("--name", required=True)
    m.add_argument("--image", required=True)
    m.add_argument("--cmd", dest="entry_cmd", required=True)
    m.add_argument("--team", required=True)
    m.add_argument("--owner", required=True)
    m.add_argument("--role", default="researcher")
    m.add_argument("--smoke-s", type=float, default=20.0)
    m.add_argument("--trivy-cache", type=Path, default=os.environ.get("AWR_TRIVY_CACHE") or None)
    m.add_argument("--trivy-image", default="d3-trivy:dev")
    u = sub.add_parser("resume")
    u.add_argument("--name", required=True)
    sub.add_parser("board")
    sub.add_parser("seal-truth")
    r = sub.add_parser("report")
    r.add_argument("--gpu-log", type=Path, default=None)
    r.add_argument("--no-probe", action="store_true")
    t = sub.add_parser("rebuild-timing")
    t.add_argument("--entries", type=int, default=100)
    t.add_argument("--repeat", type=int, default=3)
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    log = _log_to(a.out / f"{a.cmd}.log")
    try:
        if a.cmd == "seal":
            refs = {"gz": a.gz_image, "m25_worker": a.host_image, "env": a.host_image, "baseline": a.agent_image}
            res = seal_board_version(a.data_root, board_version=a.board_version, worlds_root=a.worlds_root, platform_refs=refs, gz_image=a.gz_image,
                                     agent_image=a.agent_image, gpu=a.gpu, template=a.template, lock_out=a.lock_out, duration_s=a.duration_s,
                                     docker=a.docker, log=log)
            E.write_json(a.out / "seal.json", res)
        elif a.cmd == "baselines":
            res = run_baselines(a.data_root, a.out, board_version=a.board_version, track=a.track, jobs=a.jobs,
                                baselines=a.baselines.split(",") if a.baselines else None, lanes=a.lanes.split(",") if a.lanes else None,
                                docker=a.docker, log=log)
            E.write_json(a.out / f"baselines-{a.track}.json", res)
            return 0 if not res["failed"] else 1
        elif a.cmd == "submit":
            res = run_submission(a.data_root, a.out, board_version=a.board_version, track=a.track, name=a.name, image=a.image,
                                 cmd=shlex.split(a.entry_cmd), team=a.team, owner=a.owner, role=a.role, smoke_s=a.smoke_s, trivy_cache=a.trivy_cache,
                                 trivy_image=a.trivy_image, docker=a.docker, log=log)
            return 0 if res["state"] == "DONE" else 1
        elif a.cmd == "resume":
            res = resume_submission(a.data_root, a.out, board_version=a.board_version, name=a.name, docker=a.docker, log=log)
            return 0 if res["state"] == "DONE" else 1
        elif a.cmd == "board":
            publish_board(a.data_root, a.out, board_version=a.board_version, log=log)
        elif a.cmd == "seal-truth":
            seal_run_truth(a.data_root, board_version=a.board_version, log=log)
        elif a.cmd == "report":
            rep = build_report(a.data_root, a.out, board_version=a.board_version, gpu_log=a.gpu_log, probe=not a.no_probe, log=log)
            return 0 if rep["passed"] else 1
        elif a.cmd == "rebuild-timing":
            res = rebuild_timing(a.data_root, board_version=a.board_version, n_entries=a.entries, repeat=a.repeat, log=log)
            E.write_json(a.out / "rebuild_timing.json", res)
            return 0 if res["passed"] else 1
    except FormalError as e:
        log(f"[{a.cmd}] FAILED: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
