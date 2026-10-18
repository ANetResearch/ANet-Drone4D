"""平台复现审计 `platform_audit`（ADR-177；M27-FR-172；M24 §14.5；D3-AC-17 末项）：每日以基线策略复跑抽样 Run 并比对逐帧摘要。

`python -m awr.bench.platform_audit run --data-root <dir> [--board V | --all] [--date YYYY-MM-DD] [--per-event N] [--force]`
（数据根缺省取 `AWR_BENCH_DATA_ROOT`），另有 `snapshot`、`status`、`unfreeze` 子命令，见 `main`。

**逐帧摘要**（`frames.digest`，`drone4d.bench.frames_digest.v1`）：由运行目录（`awr.agentsdk.env.recorder` 布局）派生，首行为
`# ` 加 JSON 头（`run_id`、`event`、`pose_sha256`，清单含运行身份时另有 `versions` 与 `run_hash`），其后每行 `<流> <k> <sha256>`：

- `obs/<uav>`：每次交付观测的规范 JSON 摘要（`agents/<uav>/obs.sha256` 原样；L2 相机与 LiDAR 帧字节以 base64 进入规范 JSON，
  即 M25 帧摘要）；
- `act/<uav>`：`agents/<uav>/acts.jsonl` 每行（动作与回执）的 sha256，k 取该行的 `k`；
- `pose`：`truth/fleet.npz` 每 tick 末的真值（t_ns、位置、速度、偏航、导航位置、状态）逐 tick 摘要；
- `report`、`net`、`truth/<名>`：`reports.jsonl`、`net/records.jsonl`、`truth/*.jsonl` 逐行摘要，k 为行号。

墙钟量（`wall_s`、线协议时延）不进入摘要；`run_id` 只记在头中，复跑沿用首跑的 `run_id`，使输入逐字节相同。

**抽样**：榜单版本的基线行（排名运行；开发榜 `track.dev` 除外）按 test 事件分组，每个事件每日至少抽 1 个 Run（`per_event`）。抽样起点
`sha256(board_version | date | event)` 取模，逐日轮换；只抽有首跑摘要的 Run。首跑摘要依次取：`<board>/digests/<event>/<条目>-s<seed>.digest`
（发布时由 `snapshot_references` 固化）、行的 `run_dir`、`runs_root`、`<board>/runs/` 或 `<board>/index.json`（`proto_board` 产出）所指
运行目录中的 `frames.digest`，缺失时由运行目录现算。某事件无可用首跑摘要时报告记 `no_reference`，审计状态为 `incomplete`（不冻结）。

**复跑与比对**：缺省以 `awr.bench.runner.run_job` 按榜单配置（`run_spec`、物理档、gz 选项、基线镜像）以参考策略复跑到
`<board>/audit/runs/<date>/`；平台原因异常重跑一次，仍失败记 `replay_failed`。比对项：各流帧数与逐帧摘要（报首个不一致的 k）、
`pose_sha256`（gz 位姿存档摘要）、`versions`（平台镜像摘要，`platform_digest`）、`run_hash`，以及复跑评测分量与评测行是否相同。

**冻结**：任一不一致即写 `<board>/audit/freeze.json`（`drone4d.bench.board_freeze.v1`）并告警（日志与非零退出码）：该榜单版本的
新评测停止（`subworker` 不再领取该版本的 RECEIVED 提交，`ensure_open` 抛 `BoardFrozenError`），已发布的榜单保持只读可见；调查结束后
由维护者 `unfreeze`（须署名与说明，记入 `audit/freeze_history.jsonl`）。一致时复跑目录只保留 `frames.digest`、`manifest.json` 与
`evaluation.json`，不一致时完整保留供调查。审计报告写入 `<board>/audit/<date>.json`（`drone4d.bench.platform_audit.v1`），同日重复
调用直接返回已有报告（`force` 除外）；报告只含种子序号，不含种子值。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from awr.bench.boardstore import load_config, load_rows

__all__ = ["DIGEST_NAME", "DIGEST_SCHEMA", "FREEZE_SCHEMA", "REPORT_SCHEMA", "BoardFrozenError", "FramesDigest", "ReplayJob", "audit_board",
           "compare_digests", "ensure_open", "frames_digest", "freeze", "freeze_state", "is_frozen", "load_digest", "reference_digest",
           "replay_baseline", "sample_runs", "snapshot_references", "unfreeze", "write_digest"]

DIGEST_SCHEMA = "drone4d.bench.frames_digest.v1"
REPORT_SCHEMA = "drone4d.bench.platform_audit.v1"
FREEZE_SCHEMA = "drone4d.bench.board_freeze.v1"
DIGEST_NAME = "frames.digest"
KEEP_ON_PASS = (DIGEST_NAME, "manifest.json", "evaluation.json")
MAX_LISTED = 20                                              # 报告中每个 Run 列出的不一致项上限


# ------------------------------------------------------------------ 逐帧摘要
@dataclass
class FramesDigest:
    head: dict
    streams: dict[str, list[tuple[int, str]]] = field(default_factory=dict)

    def sha256(self) -> str:
        """全部流的摘要（去重与快速比对用；不含头）。"""
        h = hashlib.sha256()
        for s in sorted(self.streams):
            for k, d in self.streams[s]:
                h.update(f"{s} {k} {d}\n".encode())
        return h.hexdigest()

    def dumps(self) -> str:
        head = {"schema": DIGEST_SCHEMA, **{k: v for k, v in self.head.items() if k != "schema"}, "digest_sha256": self.sha256()}
        lines = ["# " + json.dumps(head, ensure_ascii=False, sort_keys=True, separators=(",", ":"))]
        lines += [f"{s} {k} {d}" for s in sorted(self.streams) for k, d in self.streams[s]]
        return "\n".join(lines) + "\n"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _line_stream(p: Path, key: str | None = None) -> list[tuple[int, str]]:
    out = []
    with p.open("rb") as f:
        for i, ln in enumerate(x for x in f if x.strip()):
            ln = ln.rstrip(b"\n")
            out.append((int(json.loads(ln)[key]) if key else i, _sha(ln)))
    return out


def _pose_stream(p: Path) -> list[tuple[int, str]]:
    z = np.load(p, allow_pickle=False)
    t = np.asarray(z["t_ns"], "<i8")
    cols = [np.ascontiguousarray(z[k], dt) for k, dt in (("pos", "<f8"), ("vel", "<f4"), ("yaw", "<f4"), ("nav", "<f8"))]
    status = z["status"]
    return [(i, _sha(t[i].tobytes() + b"".join(c[i].tobytes() for c in cols) + "\x1f".join(map(str, status[i])).encode()))
            for i in range(len(t))]


def frames_digest(run_dir: Path) -> FramesDigest:
    """由运行目录派生逐帧摘要（模块文档）。"""
    d = Path(run_dir)
    man = json.loads((d / "manifest.json").read_text(encoding="utf-8")) if (d / "manifest.json").is_file() else {}
    head = {"run_id": man.get("run_id"), "event": (man.get("event") or {}).get("id"), "pose_sha256": man.get("pose_sha256")}
    for k in ("versions", "run_hash"):
        if man.get(k) is not None:
            head[k] = man[k]
    fd = FramesDigest(head)
    agents = d / "agents"
    for u in sorted(p.name for p in agents.iterdir() if p.is_dir()) if agents.is_dir() else []:
        o = agents / u / "obs.sha256"
        if o.is_file():
            fd.streams[f"obs/{u}"] = [(int(k), h) for k, h in (ln.split() for ln in o.read_text(encoding="utf-8").splitlines() if ln.strip())]
        a = agents / u / "acts.jsonl"
        if a.is_file():
            fd.streams[f"act/{u}"] = _line_stream(a, "k")
    if (d / "truth" / "fleet.npz").is_file():
        fd.streams["pose"] = _pose_stream(d / "truth" / "fleet.npz")
    for name, p in (("report", d / "reports.jsonl"), ("net", d / "net" / "records.jsonl")):
        if p.is_file():
            fd.streams[name] = _line_stream(p)
    for p in sorted((d / "truth").glob("*.jsonl")) if (d / "truth").is_dir() else []:
        fd.streams[f"truth/{p.stem}"] = _line_stream(p)
    return fd


def write_digest(fd: FramesDigest, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(fd.dumps(), encoding="utf-8")
    os.replace(tmp, path)
    return path


def load_digest(path: Path) -> FramesDigest:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    if not lines or not lines[0].startswith("# "):
        raise ValueError(f"{path}: missing {DIGEST_SCHEMA} header")
    head = json.loads(lines[0][2:])
    if head.get("schema") != DIGEST_SCHEMA:
        raise ValueError(f"{path}: schema {head.get('schema')!r} is not {DIGEST_SCHEMA}")
    fd = FramesDigest({k: v for k, v in head.items() if k not in ("schema", "digest_sha256")})
    for ln in lines[1:]:
        if ln:
            s, k, h = ln.split(" ")
            fd.streams.setdefault(s, []).append((int(k), h))
    return fd


def compare_digests(ref: FramesDigest, got: FramesDigest) -> list[dict]:
    """→ 不一致项（空表示逐帧一致）：`stream_missing`、`stream_extra`、`frame`（首个不同的 k）、`count`、`pose_sha256`、
    `platform_digest`（`versions` 不同，列出不同的键）、`run_hash`。"""
    out: list[dict] = []
    for s in sorted(set(ref.streams) | set(got.streams)):
        a, b = ref.streams.get(s), got.streams.get(s)
        if b is None:
            out.append({"kind": "stream_missing", "stream": s, "expected": len(a)})
            continue
        if a is None:
            out.append({"kind": "stream_extra", "stream": s, "got": len(b)})
            continue
        i = next((j for j, (x, y) in enumerate(zip(a, b, strict=False)) if x != y), None)
        if i is not None:
            out.append({"kind": "frame", "stream": s, "index": i, "k": a[i][0], "expected": a[i][1], "got": b[i][1],
                        **({"k_got": b[i][0]} if b[i][0] != a[i][0] else {})})
        elif len(a) != len(b):
            out.append({"kind": "count", "stream": s, "expected": len(a), "got": len(b)})
    if ref.head.get("pose_sha256") != got.head.get("pose_sha256"):
        out.append({"kind": "pose_sha256", "expected": ref.head.get("pose_sha256"), "got": got.head.get("pose_sha256")})
    va, vb = ref.head.get("versions"), got.head.get("versions")
    if va is not None and vb is not None and va != vb:
        keys = sorted(k for k in set(va) | set(vb) if va.get(k) != vb.get(k))
        out.append({"kind": "platform_digest", "keys": keys, "expected": {k: va.get(k) for k in keys}, "got": {k: vb.get(k) for k in keys}})
    elif ref.head.get("run_hash") and got.head.get("run_hash") and ref.head["run_hash"] != got.head["run_hash"]:
        out.append({"kind": "run_hash", "expected": ref.head["run_hash"], "got": got.head["run_hash"]})
    return out


# ------------------------------------------------------------------ 冻结
class BoardFrozenError(Exception):
    """榜单版本已被平台复现审计冻结，不接受新评测。"""

    def __init__(self, board_version: str, state: dict) -> None:
        super().__init__(f"board {board_version} is frozen by platform_audit since {state.get('audit')}")
        self.board_version, self.state = board_version, state


def _freeze_path(board_dir: Path) -> Path:
    return Path(board_dir) / "audit" / "freeze.json"


def freeze_state(board_dir: Path) -> dict | None:
    p = _freeze_path(board_dir)
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def is_frozen(board_dir: Path) -> bool:
    return _freeze_path(board_dir).is_file()


def ensure_open(board_dir: Path) -> None:
    """新评测入口调用：版本已冻结时抛 `BoardFrozenError`。"""
    st = freeze_state(board_dir)
    if st is not None:
        raise BoardFrozenError(Path(board_dir).name, st)


def _write_json(p: Path, doc: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def freeze(board_dir: Path, *, audit: str, reasons: list[dict], now_ns: int | None = None) -> dict:
    """冻结（幂等：已冻结时追加本次审计的原因，保留首次冻结时间）。"""
    board_dir = Path(board_dir)
    now = time.time_ns() if now_ns is None else int(now_ns)
    st = freeze_state(board_dir) or {"schema": FREEZE_SCHEMA, "board_version": board_dir.name, "since_unix_ns": now, "audit": audit, "events": []}
    st["events"].append({"audit": audit, "t_unix_ns": now, "reasons": reasons})
    _write_json(_freeze_path(board_dir), st)
    return st


def unfreeze(board_dir: Path, *, by: str, note: str, now_ns: int | None = None) -> dict:
    """调查结束后解除冻结；署名与说明必填，冻结记录连同解除信息追加到 `audit/freeze_history.jsonl`。"""
    if not by.strip() or not note.strip():
        raise ValueError("unfreeze requires a maintainer name and an investigation note")
    st = freeze_state(board_dir)
    if st is None:
        raise ValueError(f"board {Path(board_dir).name} is not frozen")
    st["lifted"] = {"by": by, "note": note, "t_unix_ns": time.time_ns() if now_ns is None else int(now_ns)}
    with (Path(board_dir) / "audit" / "freeze_history.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(st, ensure_ascii=False, sort_keys=True) + "\n")
    _freeze_path(board_dir).unlink()
    return st


# ------------------------------------------------------------------ 抽样与首跑摘要
def _audit_rows(board_dir: Path, cfg: dict) -> list[dict]:
    """可审计的评测行：基线行中的排名运行（开发榜不过滤），与上榜口径一致（boardstore._board_rows）。"""
    from awr.bench.eval.aggregate import ranked_row

    dev = bool((cfg.get("track") or {}).get("dev"))
    return [r for r in load_rows(board_dir) if r.get("baseline") and (dev or ranked_row(r))]


def _policy(row: dict) -> str:
    return str(row.get("policy") or row["entry_id"].split("/", 1)[-1])


def _seed(row: dict, cfg: dict) -> int:
    """基线行的 `seed` 为种子值（proto_board 约定）；带 `seed_index` 的行按榜单配置的种子表取值。"""
    if "seed_index" in row:
        return int(cfg["seeds"][int(row["seed_index"])])
    return int(row["seed"])


def _seed_index(row: dict, cfg: dict) -> int | None:
    if "seed_index" in row:
        return int(row["seed_index"])
    seeds = [int(s) for s in cfg.get("seeds") or []]
    s = _seed(row, cfg)
    return seeds.index(s) if s in seeds else None


def _ref_key(row: dict) -> str:
    return f"{row['event']}/{row['entry_id'].replace('/', '~')}-s{row.get('seed_index', row['seed'])}"


def _snapshot_path(board_dir: Path, row: dict) -> Path:
    return Path(board_dir) / "digests" / f"{_ref_key(row)}.digest"


def _run_dir_candidates(board_dir: Path, cfg: dict, row: dict, runs_root: Path | None) -> list[Path]:
    out = [Path(row["run_dir"])] if row.get("run_dir") else []
    name = f"{_policy(row)}-s{row['seed']}"
    out += [Path(r) / name for r in (runs_root, cfg.get("runs_root"), Path(board_dir) / "runs") if r]
    idx = Path(board_dir) / "index.json"
    if idx.is_file():                                        # proto_board 的评测行索引：evaluation 为运行目录内的 evaluation.json
        key = (row["entry_id"], row["event"], row["seed"])
        out += [Path(e["evaluation"]).parent for e in json.loads(idx.read_text(encoding="utf-8"))
                if (e.get("entry_id"), e.get("event"), e.get("seed")) == key and isinstance(e.get("evaluation"), str)]
    return out


def reference_digest(board_dir: Path, cfg: dict, row: dict, runs_root: Path | None = None) -> tuple[FramesDigest, str] | None:
    """→ (首跑摘要, 来源) 或 None。"""
    snap = _snapshot_path(board_dir, row)
    if snap.is_file():
        return load_digest(snap), "snapshot"
    for d in _run_dir_candidates(board_dir, cfg, row, runs_root):
        if (d / DIGEST_NAME).is_file():
            return load_digest(d / DIGEST_NAME), "run_dir"
        if (d / "manifest.json").is_file():
            return frames_digest(d), "run_dir"
    return None


def snapshot_references(board_dir: Path, runs_root: Path | None = None) -> dict:
    """发布时固化首跑摘要：对每个可审计基线行把首跑摘要写到 `<board>/digests/`（已存在的不覆盖）；→ {written, kept, missing[]}。"""
    board_dir = Path(board_dir)
    cfg = load_config(board_dir)
    out = {"written": 0, "kept": 0, "missing": []}
    for r in _audit_rows(board_dir, cfg):
        p = _snapshot_path(board_dir, r)
        if p.is_file():
            out["kept"] += 1
            continue
        ref = reference_digest(board_dir, cfg, r, runs_root)
        if ref is None:
            out["missing"].append(_ref_key(r))
            continue
        write_digest(ref[0], p)
        out["written"] += 1
    return out


def sample_runs(rows: list[dict], *, board_version: str, date: str, per_event: int = 1, has_ref: Callable[[dict], bool] = lambda _r: True
                ) -> dict[str, list[dict]]:
    """每个 test 事件抽 `per_event` 个有首跑摘要的 Run（起点按 `sha256(board_version|date|event)` 逐日轮换）；→ event → 行（可为空）。"""
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(str(r["event"]), []).append(r)
    out: dict[str, list[dict]] = {}
    for ev in sorted(by):
        c = sorted(by[ev], key=lambda r: (r["entry_id"], int(r.get("seed_index", r["seed"]))))
        start = int(_sha(f"{board_version}|{date}|{ev}".encode())[:16], 16) % len(c)
        picked: list[dict] = []
        for j in range(len(c)):
            r = c[(start + j) % len(c)]
            if has_ref(r):
                picked.append(r)
                if len(picked) >= per_event:
                    break
        out[ev] = picked
    return out


# ------------------------------------------------------------------ 复跑
@dataclass(frozen=True)
class ReplayJob:
    board_version: str
    event: str
    entry_id: str
    policy: str
    seed: int
    seed_index: int | None
    run_id: str
    run_dir: Path
    run_spec: dict
    cfg: dict
    docker: str = "docker"


def _run_spec_for(cfg: dict, event: str) -> dict:
    rs = (cfg.get("run_specs") or {}).get(event)
    if rs is None and cfg["run_spec"]["event"]["id"] == event:
        rs = cfg["run_spec"]
    if rs is None:
        raise KeyError(f"board config has no run_spec for event {event}")
    return rs


def replay_baseline(job: ReplayJob) -> Path:
    """缺省复跑：按榜单配置以参考策略在隔离容器中运行（与正式评测同一路径 `run_job`）；→ 运行目录。"""
    from awr.bench.runner import run_job

    cfg = job.cfg
    gz = dict(cfg.get("gz") or {})
    if gz:
        gz["name"] = f"d3-gz-audit-{job.event}-{job.policy}-s{job.seed_index if job.seed_index is not None else 'x'}"
    run_job(job.run_spec, policy={"kind": "reference", "name": job.policy}, seed=job.seed, run_id=job.run_id, run_dir=job.run_dir,
            worlds_root=Path(cfg["worlds_root"]), events_root=Path(cfg["events_root"]), physics=cfg.get("physics"), gz=gz or None,
            agent_image=cfg.get("agent_image", "d3-agent-py:dev"), docker=job.docker)
    return job.run_dir


def _prune(run_dir: Path) -> None:
    for p in run_dir.iterdir():
        if p.name in KEEP_ON_PASS:
            continue
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()


def _audit_one(board_dir: Path, cfg: dict, row: dict, ref: FramesDigest, src: str, *, date: str, out_root: Path,
               replay: Callable[[ReplayJob], Path], docker: str, log) -> dict:
    ev, pol, seed, si = str(row["event"]), _policy(row), _seed(row, cfg), _seed_index(row, cfg)
    tag = f"{row['entry_id'].replace('/', '~')}-s{si if si is not None else 'x'}"
    run_dir = Path(out_root) / ev / tag
    job = ReplayJob(board_version=board_dir.name, event=ev, entry_id=row["entry_id"], policy=pol, seed=seed, seed_index=si,
                    run_id=ref.head.get("run_id") or f"{ev}-{pol}-s{seed}", run_dir=run_dir, run_spec=_run_spec_for(cfg, ev), cfg=cfg, docker=docker)
    item = {"event": ev, "entry_id": row["entry_id"], "seed_index": si, "reference": src, "run_dir": str(run_dir)}
    errors = []
    for attempt in (0, 1):                                   # 平台原因异常重跑一次（与正式评测相同）
        if run_dir.exists():
            shutil.rmtree(run_dir)
        try:
            replay(job)
            break
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")
            log(f"platform_audit {board_dir.name} {ev} {tag}: replay attempt {attempt} failed\n{traceback.format_exc()[-1500:]}")
    else:
        return {**item, "consistent": False, "mismatches": [{"kind": "replay_failed", "errors": errors}]}
    got = frames_digest(run_dir)
    write_digest(got, run_dir / DIGEST_NAME)
    mm = compare_digests(ref, got)
    evp = run_dir / "evaluation.json"
    if evp.is_file() and isinstance(row.get("evaluation"), dict):
        e = json.loads(evp.read_text(encoding="utf-8"))
        want = row["evaluation"]
        if e.get("components") != want.get("components") or bool(e.get("ranked_valid")) != bool(want.get("ranked_valid")):
            mm.append({"kind": "evaluation", "expected": {"components": want.get("components"), "ranked_valid": want.get("ranked_valid")},
                       "got": {"components": e.get("components"), "ranked_valid": e.get("ranked_valid")}})
    item.update({"consistent": not mm, "digest_sha256": got.sha256(), "reference_sha256": ref.sha256(),
                 "frames": {s: len(v) for s, v in sorted(got.streams.items())}, "mismatches": mm[:MAX_LISTED], "n_mismatches": len(mm)})
    if not mm:
        _prune(run_dir)
    return item


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def audit_board(board_dir: Path, *, date: str | None = None, per_event: int = 1, replay: Callable[[ReplayJob], Path] | None = None,
                out_root: Path | None = None, runs_root: Path | None = None, force: bool = False, docker: str = "docker", log=print,
                now_ns: int | None = None) -> dict:
    """对一个榜单版本执行一次（当日）审计；→ 报告（`status`：`consistent`、`inconsistent`（已冻结）、`incomplete`（有事件无首跑摘要）、
    `empty`（无可审计行））。"""
    board_dir = Path(board_dir)
    date = date or _today()
    rp = board_dir / "audit" / f"{date}.json"
    if rp.is_file() and not force:
        return json.loads(rp.read_text(encoding="utf-8"))
    cfg = load_config(board_dir)
    rows = _audit_rows(board_dir, cfg)
    refs: dict[int, tuple[FramesDigest, str] | None] = {}

    def has_ref(r: dict) -> bool:
        if id(r) not in refs:
            refs[id(r)] = reference_digest(board_dir, cfg, r, runs_root)
        return refs[id(r)] is not None

    picked = sample_runs(rows, board_version=board_dir.name, date=date, per_event=per_event, has_ref=has_ref)
    out_root = Path(out_root) if out_root else board_dir / "audit" / "runs" / date
    t0 = time.time()
    items, gaps = [], []
    for ev, rs in picked.items():
        if not rs:
            gaps.append(ev)
            continue
        for r in rs:
            ref, src = refs[id(r)]
            it = _audit_one(board_dir, cfg, r, ref, src, date=date, out_root=out_root, replay=replay or replay_baseline, docker=docker,
                            log=log)
            items.append(it)
            log(f"platform_audit {board_dir.name} {ev} {r['entry_id']} s#{it['seed_index']}: "
                + ("consistent" if it["consistent"] else f"INCONSISTENT {[m['kind'] for m in it['mismatches']]}"))
    bad = [it for it in items if not it["consistent"]]
    status = "inconsistent" if bad else ("incomplete" if gaps else ("consistent" if items else "empty"))
    report = {"schema": REPORT_SCHEMA, "board_version": board_dir.name, "date": date, "per_event": per_event, "status": status,
              "events": sorted(picked), "no_reference": gaps, "runs": items, "wall_s": round(time.time() - t0, 1),
              "t_unix_ns": time.time_ns() if now_ns is None else int(now_ns)}
    if bad:
        st = freeze(board_dir, audit=date, reasons=[{k: it[k] for k in ("event", "entry_id", "seed_index")} | {"kinds": sorted({m["kind"] for m in it["mismatches"]})}
                                                    for it in bad], now_ns=now_ns)
        report["frozen"] = {"since_unix_ns": st["since_unix_ns"], "audit": st["audit"]}
        log(f"ALERT platform_audit {board_dir.name}: {len(bad)} sampled run(s) differ from their first run; new evaluations are frozen")
    else:
        report["frozen"] = None if not is_frozen(board_dir) else {k: freeze_state(board_dir)[k] for k in ("since_unix_ns", "audit")}
    _write_json(rp, report)
    with (board_dir / "audit" / "log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"date": date, "status": status, "runs": len(items), "inconsistent": len(bad), "no_reference": gaps},
                           ensure_ascii=False, sort_keys=True) + "\n")
    return report


# ------------------------------------------------------------------ 命令行
def _boards(data_root: Path, a) -> list[Path]:
    from awr.bench.boardstore import current_version, versions

    root = data_root / "boards"
    vs = versions(root)
    if a.board:
        return [root / v for v in a.board]
    if a.all:
        return [root / v for v in vs if not (load_config(root / v).get("track") or {}).get("dev")]
    v = current_version(root, vs)
    return [root / v] if v else []


def main(argv: list[str] | None = None) -> int:
    """退出码：0 一致；1 不一致（已冻结）；2 参数或榜单错误；3 有事件缺首跑摘要（未完成全覆盖）。"""
    ap = argparse.ArgumentParser(prog="python -m awr.bench.platform_audit")
    ap.add_argument("--data-root", type=Path, default=os.environ.get("AWR_BENCH_DATA_ROOT"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="daily audit: replay sampled baseline runs and compare per-frame digests")
    r.add_argument("--board", action="append", help="board version (repeatable); default: current")
    r.add_argument("--all", action="store_true", help="all published non-development board versions")
    r.add_argument("--date", help="UTC date YYYY-MM-DD (default: today)")
    r.add_argument("--per-event", type=int, default=1)
    r.add_argument("--runs-root", type=Path, help="directory holding the first runs <policy>-s<seed>/")
    r.add_argument("--force", action="store_true", help="re-run even if today's report exists")
    r.add_argument("--docker", default="docker")
    s = sub.add_parser("snapshot", help="store first-run digests of a board version under <board>/digests/")
    s.add_argument("board")
    s.add_argument("--runs-root", type=Path)
    st = sub.add_parser("status")
    st.add_argument("board", nargs="?")
    u = sub.add_parser("unfreeze")
    u.add_argument("board")
    u.add_argument("--by", required=True)
    u.add_argument("--note", required=True)
    a = ap.parse_args(argv)
    if a.data_root is None:
        print("--data-root or AWR_BENCH_DATA_ROOT is required", file=sys.stderr)
        return 2
    root = Path(a.data_root)
    if a.cmd == "run":
        boards = _boards(root, a)
        if not boards or any(not (b / "config.json").is_file() for b in boards):
            print(f"no board to audit under {root / 'boards'}", file=sys.stderr)
            return 2
        codes = []
        for b in boards:
            rep = audit_board(b, date=a.date, per_event=a.per_event, runs_root=a.runs_root, force=a.force, docker=a.docker,
                              log=lambda m: print(m, flush=True))
            print(json.dumps({k: rep[k] for k in ("board_version", "date", "status", "no_reference", "frozen")}, ensure_ascii=False))
            codes.append({"inconsistent": 1, "incomplete": 3}.get(rep["status"], 0))
        return 1 if 1 in codes else max(codes)
    if a.cmd == "snapshot":
        print(json.dumps(snapshot_references(root / "boards" / a.board, a.runs_root), ensure_ascii=False))
        return 0
    if a.cmd == "status":
        from awr.bench.boardstore import versions

        names = [a.board] if a.board else versions(root / "boards")
        print(json.dumps({v: freeze_state(root / "boards" / v) for v in names}, ensure_ascii=False, indent=1))
        return 0
    try:
        unfreeze(root / "boards" / a.board, by=a.by, note=a.note)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"{a.board}: unfrozen")
    return 0


if __name__ == "__main__":
    sys.exit(main())
