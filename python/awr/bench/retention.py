"""存储治理：配额、保留期、`ranked_eval` 精简与去重（AWR-05 ADR-163 v1.3；M27-FR-057 至 062、FR-171；D3-AC-22、D3-AC-63）。

保留分两级，保留登记在运行目录的 `retention.json`（`drone4d.bench.retention.v1`，由本模块原子写入）：

- Run 级（`raw.mcap`、`frames.idx`、真值文件与 Env 运行的 Agent 层）：transient 14 天、standard 180 天、pinned 不清理；FAILED 与
  CANCELLED 一律 14 天；`.attempts/<n>/` 14 天。起算时刻为登记的 `t0_unix_ns`（缺省取清单的 `ended_unix_ns`、`created_unix_ns`、
  清单文件修改时刻）。清理后保留 `run_manifest.json`、`manifest.json`、`task.json`、`evaluation.json` 与 `retention.json`，
  对已清理制品的访问返回 697 `ARTIFACT_EXPIRED`（`expired_reason`）。
- 派生物级（`derived/<kind>/`；不在已知子目录下的逐文件导出记为 `files`）：bag transient 7 天，其余缺省 standard 30 天，pinned
  不清理；来源 Run 被清理时其非 pinned 派生物同时清理；`.rrd`（含 `/data/d3/evalstore/rrd/<event>/<run>/`）删除前先经回调在
  catalog 注销，注销失败时不删除。
- pinned：保留类别为 `pinned`，或 `pinned_by` 非空（`release:<id>`、`leaderboard:<id>`、`static:<ver>`）；`sync_references`
  把发布包、静态制品或排行榜引用的 Run 与派生物自动升为 pinned，引用撤销后回落为原类别。pinned 永不清理，也不做评测精简。

评测运行（`ranked_eval`）：评分后 `finalize_ranked_eval` 对每个"提交 × test 事件"以 CSPRNG 抽 1 个 Run 保留完整录制 30 天，其余
Run 立即精简：只保留逐帧摘要 `frames.digest`（每帧 `(uav, sensor, seq, sha256, bytes)`，只含交付给 Agent 的通道）、精简真值
`truth_min`（`truth/` 下的真值位姿 `fleet.npz`、`objects[]` 行 `l1.jsonl` 与 `l2.jsonl`、接触、运行种子，以及 report 证据帧的
实例图 `truth/evidence/<uav>/<cam>/<seq>.gt`，清单为 `truth_min.json`）、动作与配置（`agents/`）、消息（`net/`）、report、
`evaluation.json`（含评测器升级前另存的 `evaluation.<版本>.json`）、墙钟超限记录 `termination.json` 与 2 Hz 预览（`preview/`）。
同一"提交 × 事件 × 种子"的重复运行，逐帧摘要与首次运行相同时去重：只登记 `dup_of`，不再保存摘要与预览；精简真值仍保留
（K17：评测器读取的运行真值逐 Run 保留，去重的 Run 同样可以复评，`board_v1_formal.py rescore` 逐行重评不依赖首次运行的真值；
`truth_min` 每 Run 约 1–30 MB，相对 `raw.mcap` 可忽略）。抽样 Run 到期后由 `retention_gc` 按同一规则精简。精简保留的文件覆盖评测器读取的全部输入（`awr.bench.eval.truth.RUN_TRUTH_FILES` ⊆
`TRUTH_MIN_FILES`，另有 `manifest.json`、`task.json`、`reports.jsonl`、`net/`、`agents/`），精简后复评 `evaluation.json` 逐字节一致。

- 已移入隐藏区的 test 运行真值（`<root>/hidden/<event>/truth/runs/<run_id>/`，`board_v1_formal.py seal-truth`）按同一规则精简：
  `TRUTH_MIN_FILES` 之外的文件（真值录制 `run_<id>.mcap` 等）删除，`truth_min.json` 只登记其文件名与字节数（`store = hidden`，
  不读取内容、不写摘要）；无权限删除时记入 `errors`，不中断精简。
- 逐帧摘要（`frames.digest`，`drone4d.bench.frames_digest.v1`）：`raw.mcap` 按文件顺序单遍读取（`FrameStore.scan_frames`，每个
  chunk 只解压一次；原按通道逐页读取约为 5 倍读放大，B33 遗留 4）；LiDAR 行（`lidar-v2`）的 sha256 与 bytes 取去 run 规范形式
  （`recorder.lidar.canonical_lidar_frame`：帧头 `frame_id` 的 `<run>/` 段去掉；Agent 收到的 LiDAR 帧本不含 run_id，run 段由录制端
  存档时加入），使摘要符合"不含 run_id"的定义、同一帧流的重复运行摘要逐字节相同（此前 LiDAR 行含 run_id，去重不成立；B33 遗留 2）。
  其余通道不变（交付字节）。
- `finalize_ranked_eval` 可重入：已登记为 sampled 或 compacted 的 Run 沿用登记的摘要与抽样结论，不重读、不重抽；pinned 的 Run
  不改类别、不精简；单个 Run 的摘要失败只记该 Run（`phase = error`），不影响其余。`dry_run` 只返回计划（逐 Run 的摘要、去重与
  可释放字节），不写运行目录与数据根（显式给出的摘要缓存目录 `digest_dir` 除外，`cached_frames_digest`）。精简结束时清理运行目录中残留的 AF_UNIX 套接字（`clean_run_sockets`，如 `phys/sock/ctl.sock`，
  其存在使 `shutil.copytree` 失败）。
- 运行目录不在数据根标准布局（`dataset/<event>/runs/<id>`、`scratch/<id>`）下时（`mbl_e2e` 的 `<out>/runs/<key>`），首次登记即写入
  `<root>/bench/retention_runs.txt`，`retention_gc` 与 `sync_references` 一并扫描，抽样录制到期后同样精简。
- `finalize_board`：按榜单版本的评测行（`rows.jsonl` 中 `evaluation.ranked` 为真的行）定位运行目录并调用 `finalize_ranked_eval`
  （提交行以 submission_id、基线行以 `<版本>:<entry_id>` 为"提交"键），供正式榜单流程的评分后步骤与历史产物回填使用。

配额（M27-FR-060）：`/data/d3` 10 TB；占用 ≥ 80% 告警；≥ 95% 停止准入新 Run 与新派生作业（685，`blocked_by = storage`，
新运行保持 QUEUED），运行中作业不中断；每租户软配额 2 TB，超出时该租户的新运行排队并告警（685 `tenant_soft_quota`）。

审计：删除前逐项写 `<data_root>/audit/retention-<UTC 日期>.jsonl`（路径、字节数、原因）；685 与 80% 告警同写。`--dry-run`
只返回计划，不删除、不写审计。

命令行：`python -m awr.bench.retention gc [--dry-run]`、`usage`、`pin <run_id> --ref R [--derived K]`、`unpin …`、
`finalize --board V [--runs-root DIR …] [--entry E …] [--skip-run RUN_ID …] [--dry-run] [--digest-dir DIR [--jobs N]] [--out FILE]`；数据根取 `AWR_BENCH_DATA_ROOT`（缺省 /data/d3）。测试以 `RetentionPolicy(day_ns=60 * 10**9)`（1 天 = 1 min）缩短时钟。
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import random
import re
import secrets
import shutil
import stat
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

__all__ = ["DERIVED_KINDS", "POLICY", "RETENTION_FILE", "Admission", "RankedRun", "RetentionPolicy", "StorageGate", "Usage",
           "cached_frames_digest", "clean_run_sockets", "compact_ranked", "expired_reason", "finalize_board", "finalize_ranked_eval", "frames_digest", "load_record",
           "main", "pin", "register_derived", "register_run", "retention_gc", "run_bytes", "sync_references", "unpin"]

SCHEMA = "drone4d.bench.retention.v1"
DIGEST_SCHEMA = "drone4d.bench.frames_digest.v1"
EXTERNAL_INDEX = "bench/retention_runs.txt"                 # 不在标准布局下的 ranked_eval 运行目录（每行一个绝对路径）
RETENTION_FILE = "retention.json"
DAY_NS = 86_400 * 10 ** 9
TB = 10 ** 12

RUN_CLASSES = ("transient", "standard", "pinned", "ranked_eval")
DERIVED_CLASSES = ("transient", "standard", "pinned")
DERIVED_KINDS = ("bag", "rrd", "video", "slam", "compare", "lerobot", "openlabel", "slices", "files")
DERIVED_DEFAULT = {"bag": "transient"}                                  # 其余缺省 standard（M27-FR-058）
ACTIVE_STATES = ("QUEUED", "PREPARING", "RUNNING", "FINALIZING", "EVALUATING")
FAILED_STATES = ("FAILED", "CANCELLED")
REF = re.compile(r"^(release|leaderboard|static):[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")

KEEP_ON_EXPIRE = frozenset({"run_manifest.json", "manifest.json", "task.json", "evaluation.json", RETENTION_FILE})
RANKED_KEEP_FILES = frozenset(KEEP_ON_EXPIRE | {"frames.digest", "truth_min.json", "reports.jsonl", "termination.json"})
RANKED_KEEP_PATTERN = re.compile(r"^evaluation\.[0-9A-Za-z._-]+\.json$")    # 评测器升级前另存的评测（board_v1_formal.py rescore）
RANKED_KEEP_DIRS = ("agents", "net", "preview")
TRUTH_MIN_FILES = frozenset({"fleet.npz", "l1.jsonl", "l2.jsonl", "contacts.jsonl", "run_seed.json"})
REASON_685 = "STORAGE_QUOTA_EXCEEDED"
REASON_697 = "ARTIFACT_EXPIRED"


@dataclass(frozen=True)
class RetentionPolicy:
    day_ns: int = DAY_NS
    run_days: Mapping[str, int] = field(default_factory=lambda: {"transient": 14, "standard": 180})
    derived_days: Mapping[str, int] = field(default_factory=lambda: {"transient": 7, "standard": 30})
    failed_days: int = 14
    attempt_days: int = 14
    sample_days: int = 30
    quota_bytes: int = 10 * TB
    warn_frac: float = 0.80
    stop_frac: float = 0.95
    tenant_soft_bytes: int = 2 * TB


POLICY = RetentionPolicy()


def _now() -> int:
    return time.time_ns()


# ------------------------------------------------------------------ 登记读写
def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_json(p: Path, doc: dict) -> None:
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def _manifest(run_dir: Path) -> dict:
    return _read_json(run_dir / "run_manifest.json") or _read_json(run_dir / "manifest.json")


def load_record(run_dir: Path) -> dict:
    """`retention.json`；缺失时由运行清单推出（类别取 `retention`，缺省 standard；排名的 Env 运行不推为 ranked_eval，
    ranked_eval 只由 `finalize_ranked_eval` 登记）。"""
    run_dir = Path(run_dir)
    rec = _read_json(run_dir / RETENTION_FILE)
    if rec:
        rec.setdefault("pinned_by", [])
        rec.setdefault("derived", {})
        rec.setdefault("expired", {})
        return rec
    m = _manifest(run_dir)
    t0 = m.get("ended_unix_ns") or m.get("created_unix_ns")
    if t0 is None:
        for name in ("run_manifest.json", "manifest.json"):
            if (run_dir / name).exists():
                t0 = (run_dir / name).stat().st_mtime_ns
                break
    cls = m.get("retention") if m.get("retention") in RUN_CLASSES else "standard"
    return {"schema": SCHEMA, "run_id": run_dir.name, "retention": cls, "state": m.get("state", "COMPLETED"),
            "tenant": m.get("tenant") or m.get("submitted_by"), "t0_unix_ns": t0, "pinned_by": [], "derived": {}, "expired": {}}


def _save(run_dir: Path, rec: dict) -> None:
    rec["schema"] = SCHEMA
    rec["pinned_by"] = sorted(set(rec.get("pinned_by", [])))
    _write_json(Path(run_dir) / RETENTION_FILE, rec)


@contextlib.contextmanager
def _locked(root: Path) -> Iterator[None]:
    """数据根级互斥（`retention_gc` 与 pin、登记不并发改写同一 `retention.json`）。"""
    p = Path(root) / "bench" / "retention.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _audit(root: Path, now: int, **rec) -> None:
    day = datetime.fromtimestamp(now / 1e9, UTC).strftime("%Y%m%d")
    p = Path(root) / "audit" / f"retention-{day}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"t_unix_ns": now, **rec}, ensure_ascii=False, sort_keys=True) + "\n")
        f.flush()


def register_run(run_dir: Path, *, retention: str = "standard", state: str = "COMPLETED", tenant: str | None = None,
                 t0_unix_ns: int | None = None, now: int | None = None) -> dict:
    """调度器在 Run 进入终态时登记保留类别与起算时刻（M27-FR-057）。"""
    if retention not in RUN_CLASSES:
        raise ValueError(f"retention {retention!r} not in {RUN_CLASSES}")
    rec = load_record(run_dir)
    rec.update(retention=retention, state=state, t0_unix_ns=int(t0_unix_ns if t0_unix_ns is not None else (now or _now())))
    if tenant is not None:
        rec["tenant"] = tenant
    _save(run_dir, rec)
    return rec


def register_derived(run_dir: Path, kind: str, *, retention: str | None = None, now: int | None = None) -> dict:
    """派生作业（export、export_rrd、slam_eval）完成时登记派生物类别与起算时刻（M27-FR-058）。"""
    if kind not in DERIVED_KINDS:
        raise ValueError(f"derived kind {kind!r} not in {DERIVED_KINDS}")
    cls = retention or DERIVED_DEFAULT.get(kind, "standard")
    if cls not in DERIVED_CLASSES:
        raise ValueError(f"derived retention {cls!r} not in {DERIVED_CLASSES}")
    rec = load_record(run_dir)
    d = rec["derived"].setdefault(kind, {"pinned_by": []})
    d.update(retention=cls, created_unix_ns=int(now or _now()))
    rec["expired"].get("derived", {}).pop(kind, None)
    _save(run_dir, rec)
    return rec


# ------------------------------------------------------------------ 引用与 pinned
def _check_ref(ref: str) -> None:
    if not REF.match(ref):
        raise ValueError(f"pin reference {ref!r} must be release:<id>, leaderboard:<id> or static:<ver>")


def pin(run_dir: Path, ref: str, *, derived: str | None = None) -> dict:
    _check_ref(ref)
    rec = load_record(run_dir)
    tgt = rec if derived is None else rec["derived"].setdefault(derived, {"retention": DERIVED_DEFAULT.get(derived, "standard"), "pinned_by": []})
    tgt["pinned_by"] = sorted(set(tgt.get("pinned_by", [])) | {ref})
    _save(run_dir, rec)
    return rec


def unpin(run_dir: Path, ref: str, *, derived: str | None = None) -> dict:
    rec = load_record(run_dir)
    tgt = rec if derived is None else rec["derived"].get(derived, {})
    tgt["pinned_by"] = sorted(set(tgt.get("pinned_by", [])) - {ref})
    _save(run_dir, rec)
    return rec


def _external_dirs(root: Path) -> list[Path]:
    p = Path(root) / EXTERNAL_INDEX
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [Path(x) for x in dict.fromkeys(ln.strip() for ln in lines if ln.strip())]


def _register_external(root: Path, dirs: Iterable[Path]) -> None:
    """不在标准布局下的运行目录登记到 `EXTERNAL_INDEX`（已登记的不重复写；调用方持有 `_locked`）。"""
    have = {str(d) for d in _external_dirs(root)}
    new = [str(Path(d).resolve()) for d in dirs if _root_of(Path(d)) is None]
    new = [x for x in dict.fromkeys(new) if x not in have]
    if new:
        p = Path(root) / EXTERNAL_INDEX
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write("".join(x + "\n" for x in new))


def _run_dirs(root: Path) -> list[Path]:
    root = Path(root)
    std = [*root.glob("dataset/*/runs/*"), *root.glob("scratch/*")]
    return sorted(dict.fromkeys(d for d in [*std, *_external_dirs(root)] if d.is_dir()))


def sync_references(root: Path, ref: str, targets: Iterable[tuple[str, str | None]]) -> dict:
    """把引用 `ref`（发布包、静态制品或排行榜）的目标集合同步为 pinned：`targets` 为 `(run_id, derived_kind | None)`；
    不在集合内的同名引用被撤销（类别回落）。→ `{pinned: n, unpinned: n, missing: [run_id…]}`。"""
    _check_ref(ref)
    want: dict[str, set[str | None]] = {}
    for rid, kind in targets:
        want.setdefault(rid, set()).add(kind)
    out = {"pinned": 0, "unpinned": 0, "missing": []}
    with _locked(root):
        seen = set()
        for d in _run_dirs(root):
            rec = load_record(d)
            kinds = want.get(d.name, set())
            if kinds:
                seen.add(d.name)
            changed = False
            for kind, tgt in [(None, rec), *((k, v) for k, v in rec["derived"].items())]:
                has = ref in tgt.get("pinned_by", [])
                if kind in kinds and not has:
                    tgt["pinned_by"] = [*tgt.get("pinned_by", []), ref]
                    out["pinned"] += 1
                    changed = True
                elif kind not in kinds and has:
                    tgt["pinned_by"] = [r for r in tgt["pinned_by"] if r != ref]
                    out["unpinned"] += 1
                    changed = True
            for kind in kinds - {None} - set(rec["derived"]):
                rec["derived"][kind] = {"retention": DERIVED_DEFAULT.get(kind, "standard"), "pinned_by": [ref]}
                out["pinned"] += 1
                changed = True
            if changed:
                _save(d, rec)
        out["missing"] = sorted(set(want) - seen)
    return out


def _pinned(tgt: Mapping) -> bool:
    return tgt.get("retention") == "pinned" or bool(tgt.get("pinned_by"))


# ------------------------------------------------------------------ 存储账
def _size(p: Path) -> int:
    if p.is_symlink() or not p.exists():
        return 0
    if p.is_file():
        return p.stat().st_size
    n = 0
    for dp, _, fs in os.walk(p):
        for f in fs:
            q = Path(dp) / f
            if not q.is_symlink():
                with contextlib.suppress(OSError):
                    n += q.stat().st_size
    return n


def _root_of(run_dir: Path) -> Path | None:
    """`<root>/dataset/<event>/runs/<id>` 或 `<root>/scratch/<id>` → `<root>`。"""
    run_dir = Path(run_dir)
    if run_dir.parent.name == "runs" and run_dir.parent.parent.parent.name == "dataset":
        return run_dir.parents[3]
    if run_dir.parent.name == "scratch":
        return run_dir.parent.parent
    return None


def _event_of(run_dir: Path) -> str | None:
    run_dir = Path(run_dir)
    return run_dir.parent.parent.name if run_dir.parent.name == "runs" else None


def _external_truth(run_dir: Path, root: Path | None) -> list[Path]:
    """数据集层真值文件：dev `<event>/truth/run_<id>.{mcap,frames.idx}`，test `<root>/hidden/<event>/truth/run_<id>.*`。"""
    run_dir = Path(run_dir)
    ev = _event_of(run_dir)
    if ev is None:
        return []
    out = [run_dir.parent.parent / "truth" / f"run_{run_dir.name}{s}" for s in (".mcap", ".frames.idx")]
    if root is not None:
        out += [Path(root) / "hidden" / ev / "truth" / f"run_{run_dir.name}{s}" for s in (".mcap", ".frames.idx")]
    return [p for p in out if p.exists()]


def _evalstore_rrd(run_dir: Path, root: Path | None) -> Path | None:
    ev = _event_of(run_dir)
    if root is None or ev is None:
        return None
    p = Path(root) / "evalstore" / "rrd" / ev / Path(run_dir).name
    return p if p.exists() else None


def _derived_entries(run_dir: Path) -> dict[str, list[Path]]:
    d = Path(run_dir) / "derived"
    out: dict[str, list[Path]] = {}
    if not d.is_dir():
        return out
    for p in sorted(d.iterdir()):
        kind = p.name if p.is_dir() and p.name in DERIVED_KINDS else "files"
        out.setdefault(kind, []).append(p)
    return out


def run_bytes(run_dir: Path, root: Path | None = None) -> dict:
    """M27-FR-062 存储账：`{raw, frames_idx, truth, derived{<kind>}, other, total}`（字节，含数据集层与 evalstore 的真值）。"""
    run_dir = Path(run_dir)
    root = root if root is not None else _root_of(run_dir)
    raw = _size(run_dir / "raw.mcap")
    fidx = _size(run_dir / "frames.idx")
    truth = _size(run_dir / "truth") + sum(_size(p) for p in _external_truth(run_dir, root))
    der = {k: sum(_size(p) for p in ps) for k, ps in _derived_entries(run_dir).items()}
    rrd = _evalstore_rrd(run_dir, root)
    if rrd is not None:
        der["rrd"] = der.get("rrd", 0) + _size(rrd)
    total_dir = _size(run_dir)
    other = total_dir - raw - fidx - _size(run_dir / "truth") - _size(run_dir / "derived")
    total = total_dir + sum(_size(p) for p in _external_truth(run_dir, root)) + (_size(rrd) if rrd is not None else 0)
    return {"raw": raw, "frames_idx": fidx, "truth": truth, "derived": der, "other": other, "total": total}


@dataclass(frozen=True)
class Usage:
    used_bytes: int
    by_tenant: Mapping[str, int]

    @classmethod
    def scan(cls, root: Path) -> Usage:
        """数据根全部文件的字节数（不跟随符号链接）与按租户归属的 Run 字节数。"""
        by: dict[str, int] = {}
        for d in _run_dirs(root):
            t = load_record(d).get("tenant")
            if t:
                by[t] = by.get(t, 0) + run_bytes(d, Path(root))["total"]
        return cls(_size(Path(root)), by)


# ------------------------------------------------------------------ 配额与准入
@dataclass(frozen=True)
class Admission:
    ok: bool
    used_bytes: int
    quota_bytes: int
    warn: bool
    reason: dict | None = None

    @property
    def used_frac(self) -> float:
        return self.used_bytes / self.quota_bytes if self.quota_bytes else 1.0

    @property
    def blocked_by(self) -> tuple[str, ...]:
        return () if self.ok else ("storage",)


class StorageGate:
    """`/data/d3` 配额准入（M27-FR-060）。`usage_fn` 缺省为 `Usage.scan(root)`；准入只作用于新 Run 与新派生作业，运行中作业不受影响。"""

    def __init__(self, root: Path, policy: RetentionPolicy = POLICY, *, usage_fn: Callable[[], Usage] | None = None,
                 clock: Callable[[], int] = _now, audit: bool = True) -> None:
        self.root, self.policy, self.clock, self.audit = Path(root), policy, clock, audit
        self.usage_fn = usage_fn or (lambda: Usage.scan(self.root))
        self._warned = False

    def check(self, tenant: str | None = None, *, kind: str = "run", usage: Usage | None = None) -> Admission:
        u = usage or self.usage_fn()
        q = self.policy.quota_bytes
        frac = u.used_bytes / q if q else 1.0
        warn = frac >= self.policy.warn_frac
        detail = {"used_bytes": u.used_bytes, "quota_bytes": q, "used_frac": round(frac, 6), "kind": kind}
        if frac >= self.policy.stop_frac:
            return Admission(False, u.used_bytes, q, True, {"code": 685, "name": REASON_685, "detail": {"why": "quota_stop", **detail}})
        tu = u.by_tenant.get(tenant, 0) if tenant else 0
        if tenant and tu >= self.policy.tenant_soft_bytes:
            return Admission(False, u.used_bytes, q, True, {"code": 685, "name": REASON_685, "detail": {
                "why": "tenant_soft_quota", "tenant": tenant, "tenant_bytes": tu, "tenant_soft_bytes": self.policy.tenant_soft_bytes, **detail}})
        return Admission(True, u.used_bytes, q, warn)

    def admit_queue(self, queued: Sequence[dict]) -> list[dict]:
        """对 QUEUED 的 Run（或派生作业）逐项准入：拒绝者保持 QUEUED、`blocked_by` 含 `storage`、`reason_code = 685` 并审计
        （同一 Run 只在首次受阻时记录）；通过者清除存储受阻标记并返回。非 QUEUED 项（运行中作业）原样跳过。"""
        u = self.usage_fn()
        now = self.clock()
        out = []
        any_warn = False
        for r in queued:
            if r.get("state", "QUEUED") != "QUEUED":
                continue
            a = self.check(r.get("tenant"), kind=r.get("kind", "run"), usage=u)
            any_warn |= a.warn
            blocked = [b for b in r.get("blocked_by", []) if b != "storage"]
            if a.ok:
                r["blocked_by"] = blocked
                if r.get("reason_code") == 685:
                    r["reason_code"] = r["reason_detail"] = None
                out.append(r)
                continue
            first = r.get("reason_code") != 685
            r["blocked_by"] = [*blocked, "storage"]
            r["reason_code"], r["reason_detail"] = 685, a.reason["detail"]
            if first and self.audit:
                _audit(self.root, now, action="storage_blocked", code=685, run_id=r.get("run_id"), job_id=r.get("job_id"), **a.reason["detail"])
        if any_warn and not self._warned and self.audit:
            _audit(self.root, now, action="storage_warn", used_bytes=u.used_bytes, quota_bytes=self.policy.quota_bytes,
                   used_frac=round(u.used_bytes / self.policy.quota_bytes, 6))
        self._warned = any_warn
        return out


# ------------------------------------------------------------------ 逐帧摘要与 ranked_eval 精简
def frames_digest(run_dir: Path) -> bytes:
    """逐帧摘要 `frames.digest` 的字节：首行 `#drone4d.bench.frames_digest.v1`，其后每帧一行
    `uav\\tsensor\\tseq\\tsha256\\tbytes`，按 (uav, sensor, seq) 排序。帧取自 `raw.mcap` + `frames.idx` 中交付给 Agent 的通道
    （sha256 与 bytes 为交付字节；LiDAR 为去 run 规范形式，模块说明），`raw.mcap` 按文件顺序单遍读取；Env 运行另含
    `agents/<uav>/obs.sha256` 的观测摘要（sensor = `obs`，bytes 记 `-`）。不含 run_id，同一帧流的两个 Run 摘要逐字节相同。"""
    run_dir = Path(run_dir)
    rows: list[tuple[str, str, int, str, str]] = []
    if (run_dir / "raw.mcap").exists() and (run_dir / "frames.idx").exists():
        from awr.bench.recorder.lidar import LIDAR_CODEC, canonical_lidar_frame
        from awr.bench.recorder.read import FrameStore

        fs = FrameStore(run_dir)
        try:
            chans = [c for c in fs.idx.channels() if (fs.channel_info(*c) or {}).get("agent_delivered", "true") != "false"]
            lidar = {c for c in chans if (fs.channel_info(*c) or {}).get("codec") == LIDAR_CODEC}
            for uav, sensor, f in fs.scan_frames(chans):
                if (uav, sensor) in lidar:
                    b = canonical_lidar_frame(f.payload)
                    rows.append((uav, sensor, f.seq, hashlib.sha256(b).hexdigest(), str(len(b))))
                else:
                    rows.append((uav, sensor, f.seq, f.sha256, str(len(f.payload))))
        finally:
            fs.close()
    for p in sorted((run_dir / "agents").glob("*/obs.sha256")):
        for line in p.read_text(encoding="utf-8").splitlines():
            k, _, h = line.partition(" ")
            if h:
                rows.append((p.parent.name, "obs", int(k), h.strip(), "-"))
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    return ("#" + DIGEST_SCHEMA + "\n" + "".join(f"{u}\t{s}\t{q}\t{h}\t{n}\n" for u, s, q, h, n in rows)).encode()


def _digest_stamp(run_dir: Path) -> dict:
    """逐帧摘要的输入标识（`raw.mcap`、`frames.idx` 与各 `obs.sha256` 的字节数与修改时刻）：缓存命中的判据。"""
    run_dir = Path(run_dir)
    out = {}
    for p in [run_dir / "raw.mcap", run_dir / "frames.idx", *sorted((run_dir / "agents").glob("*/obs.sha256"))]:
        with contextlib.suppress(OSError):
            st = p.stat()
            out[str(p.relative_to(run_dir))] = [st.st_size, st.st_mtime_ns]
    return {"schema": DIGEST_SCHEMA, "run_dir": str(run_dir.resolve()), "inputs": out}


def cached_frames_digest(run_dir: Path, digest_dir: Path | None) -> bytes:
    """`frames_digest`，以 `digest_dir/<运行目录名>-<路径摘要>.digest` 缓存（输入标识不变时直接读取）：回填的 dry-run 与执行、
    中断后续做不重复读取 `raw.mcap`。`digest_dir` 为 None 时不缓存。"""
    if digest_dir is None:
        return frames_digest(run_dir)
    run_dir = Path(run_dir)
    tag = hashlib.sha256(str(run_dir.resolve()).encode()).hexdigest()[:12]
    p = Path(digest_dir) / f"{run_dir.name}-{tag}.digest"
    m = p.with_suffix(".stamp.json")
    stamp = _digest_stamp(run_dir)
    if p.is_file() and _read_json(m) == stamp:
        return p.read_bytes()
    body = frames_digest(run_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes(body)
    os.replace(tmp, p)
    _write_json(m, stamp)
    return body


def _digest_job(args: tuple[str, str]) -> tuple[str, str | None]:
    """进程池单元：→ (运行目录, 错误或 None)。"""
    try:
        cached_frames_digest(Path(args[0]), Path(args[1]))
        return args[0], None
    except Exception as e:
        return args[0], f"{type(e).__name__}: {e}"[:300]


def clean_run_sockets(run_dir: Path) -> list[str]:
    """删除运行目录中残留的 AF_UNIX 套接字（gz 控制套接字 `phys/sock/ctl.sock` 等）与随之变空的 `sock/` 目录；→ 删除的相对路径。
    套接字在 Run 结束后不再被使用，残留时 `shutil.copytree` 报错（B33 遗留 5）。"""
    run_dir = Path(run_dir)
    out: list[str] = []
    if not run_dir.is_dir():
        return out
    for dp, _ds, fs in os.walk(run_dir):
        for f in fs:
            q = Path(dp) / f
            with contextlib.suppress(OSError):
                if stat.S_ISSOCK(q.lstat().st_mode):
                    q.unlink()
                    out.append(str(q.relative_to(run_dir)))
    for rel in sorted({str(Path(x).parent) for x in out}, key=len, reverse=True):
        d = run_dir / rel
        if d.name == "sock":
            with contextlib.suppress(OSError):
                d.rmdir()
    return sorted(out)


def _evidence_list(x) -> list[dict]:
    """report 内全部含 `frame_seq` 的证据项（EU 证据：det_id、bbox_xywh、point_xy）。"""
    if isinstance(x, dict):
        return ([x] if "frame_seq" in x else []) + [e for v in x.values() for e in _evidence_list(v)]
    if isinstance(x, list):
        return [e for v in x for e in _evidence_list(v)]
    return []


def _evidence_frames(run_dir: Path) -> set[tuple[str, int]]:
    """`reports.jsonl` 中证据引用的 (uav, frame_seq)；证据项无 `uav` 时取 report 行的 `uav`。"""
    out: set[tuple[str, int]] = set()
    p = run_dir / "reports.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            for ev in _evidence_list(row.get("report", row)):
                uav = ev.get("uav", row.get("uav"))
                with contextlib.suppress(TypeError, ValueError):
                    if uav is not None:
                        out.add((str(uav), int(ev["frame_seq"])))
    return out


def _hidden_run_truth(run_dir: Path, root: Path | None) -> Path | None:
    """已移入隐藏区的运行真值目录 `<root>/hidden/<event>/truth/runs/<run_id>/`（不存在或无权访问时为 None）。"""
    ev = _event_of(run_dir)
    if root is None or ev is None:
        return None
    p = Path(root) / "hidden" / ev / "truth" / "runs" / Path(run_dir).name
    try:
        return p if p.is_dir() else None
    except OSError:
        return None


def _hidden_files(hdir: Path | None) -> list[Path]:
    if hdir is None:
        return []
    try:
        return sorted(q for q in hdir.iterdir() if q.is_file() or q.is_symlink())
    except OSError:
        return []


def _truth_min(run_dir: Path, root: Path | None) -> dict:
    """写出证据帧实例图并返回 `truth_min.json` 内容（不删除任何文件）。隐藏区的运行真值只登记文件名与字节数。"""
    files = []
    tdir = run_dir / "truth"
    for name in sorted(TRUTH_MIN_FILES):
        p = tdir / name
        if p.exists():
            files.append({"path": f"truth/{name}", "bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()})
    for q in _hidden_files(_hidden_run_truth(run_dir, root)):
        if q.name in TRUTH_MIN_FILES:
            with contextlib.suppress(OSError):
                files.append({"path": f"truth/{q.name}", "bytes": q.stat().st_size, "store": "hidden"})
    evid = []
    ext = [p for p in _external_truth(run_dir, root) if p.suffix == ".mcap"]
    frames = _evidence_frames(run_dir)
    if ext and frames:
        from awr.bench.recorder.read import TruthStore

        mc = ext[0]
        ts = TruthStore(mc, mc.with_suffix(".frames.idx"))
        try:
            cams = ts.idx.header["sensors"]
            for uav, seq in sorted(frames):
                for cam in cams:
                    got = ts.get(uav, cam, seq)
                    if got is None:
                        continue
                    rel = f"truth/evidence/{uav}/{cam}/{seq:06d}.gt"
                    q = run_dir / rel
                    q.parent.mkdir(parents=True, exist_ok=True)
                    q.write_bytes(got[1])
                    evid.append({"path": rel, "uav": uav, "cam": cam, "seq": seq, "t_ns": got[0], "bytes": len(got[1]),
                                 "sha256": hashlib.sha256(got[1]).hexdigest()})
        finally:
            ts.close()
    return {"schema": "drone4d.bench.truth_min.v1", "files": files, "evidence": evid}


def _ranked_keep(rel: Path, *, dup: bool) -> bool:
    parts = rel.parts
    if len(parts) == 1:
        if dup and parts[0] == "frames.digest":
            return False
        return parts[0] in RANKED_KEEP_FILES or bool(RANKED_KEEP_PATTERN.match(parts[0]))
    if parts[0] in RANKED_KEEP_DIRS:
        return not (dup and parts[0] == "preview")
    if parts[0] == "truth":
        return (len(parts) == 2 and parts[1] in TRUTH_MIN_FILES) or parts[1] == "evidence"
    return False


@dataclass
class _Plan:
    dry_run: bool
    root: Path
    now: int
    deleted: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    gone: dict[Path, int] = field(default_factory=dict)              # dry-run 中已计划删除的路径（与实际清理的计划逐项一致）

    def delete(self, p: Path, reason: str, run_id: str | None, *, tolerant: bool = False, **extra) -> int:
        """删除一项并审计；`tolerant` 时删除失败（隐藏区无权限等）记入 `errors` 而不抛出。"""
        if not p.exists() and not p.is_symlink():
            return 0
        if any(q == p or q in p.parents for q in self.gone):
            return 0
        n = _size(p) - sum(b for q, b in self.gone.items() if p in q.parents)
        if self.dry_run:
            self.gone = {q: b for q, b in self.gone.items() if p not in q.parents} | {p: n + sum(b for q, b in self.gone.items() if p in q.parents)}
        item = {"path": str(p), "bytes": n, "reason": reason, "run_id": run_id, **extra}
        if not self.dry_run:
            try:
                if p.is_dir() and not p.is_symlink():
                    shutil.rmtree(p)
                else:
                    p.unlink()
            except OSError as e:
                if not tolerant:
                    raise
                self.errors.append({"path": str(p), "run_id": run_id, "error": f"{type(e).__name__}: {e.strerror or e}"})
                return 0
            _audit(self.root, self.now, action="delete", **item)
        self.deleted.append(item)
        return n


def _prune_ranked(run_dir: Path, plan: _Plan, *, dup: bool, reason: str) -> None:
    root = plan.root
    for p in _external_truth(run_dir, root):
        plan.delete(p, reason, run_dir.name)
    for q in _hidden_files(_hidden_run_truth(run_dir, root)):         # 隐藏区运行真值：同一保留规则
        if q.name not in TRUTH_MIN_FILES:
            plan.delete(q, reason, run_dir.name, tolerant=True, store="hidden")
    for p in sorted(run_dir.rglob("*"), key=lambda q: len(q.parts)):
        if not p.exists() and not p.is_symlink():
            continue
        rel = p.relative_to(run_dir)
        if p.is_dir() and not p.is_symlink():
            keep_any = any(_ranked_keep(q.relative_to(run_dir), dup=dup) for q in p.rglob("*") if q.is_file())
            if not keep_any:
                plan.delete(p, reason, run_dir.name)
        elif not _ranked_keep(rel, dup=dup):
            plan.delete(p, reason, run_dir.name)


def compact_ranked(run_dir: Path, *, root: Path | None = None, dup_of: tuple[str, str] | None = None, now: int | None = None,
                   dry_run: bool = False, reason: str = "ranked_eval_compact", digest: bytes | None = None, _plan: _Plan | None = None) -> dict:
    """把一个已评分的 `ranked_eval` Run 精简为逐帧摘要 + 精简真值 + Agent 输出 + 评测（FR-171）；`dup_of = (首次 run_id,
    摘要 sha256)` 时按重复运行去重（不存摘要与预览）；`digest` 为已算好的 `frames_digest` 字节（省去重读 `raw.mcap`）。pinned 的 Run 不精简。
    → `{compacted, deleted[], errors[], frames_digest_sha256, bytes_after}`。"""
    run_dir = Path(run_dir)
    root = Path(root) if root is not None else (_root_of(run_dir) or run_dir.parent)
    now = now or _now()
    rec = load_record(run_dir)
    if _pinned(rec):
        return {"compacted": False, "why": "pinned", "deleted": [], "errors": []}
    plan = _plan or _Plan(dry_run, root, now)
    n0, e0 = len(plan.deleted), len(plan.errors)
    if dup_of is None:
        body = digest if digest is not None else frames_digest(run_dir)
        sha = hashlib.sha256(body).hexdigest()
        if not dry_run:
            (run_dir / "frames.digest").write_bytes(body)
    else:
        sha = dup_of[1]
    if not dry_run:
        _write_json(run_dir / "truth_min.json", _truth_min(run_dir, root))
    _prune_ranked(run_dir, plan, dup=dup_of is not None, reason=reason)
    if not dry_run:
        rr = rec.setdefault("ranked_eval", {})
        rr.update(phase="compacted", frames_digest_sha256=sha, compacted_unix_ns=now)
        if dup_of is not None:
            rr["dup_of"] = dup_of[0]
        rec["retention"] = "ranked_eval"
        _save(run_dir, rec)
    return {"compacted": True, "deleted": plan.deleted[n0:], "errors": plan.errors[e0:], "frames_digest_sha256": sha,
            "bytes_after": None if dry_run else _run_total(run_dir, root)}


def _run_total(run_dir: Path, root: Path | None) -> int:
    """保留处理口径的 Run 字节：`run_bytes` 合计加隐藏区运行真值（seal-truth 布局，精简会删除其中非最小集文件）。"""
    return run_bytes(run_dir, root)["total"] + sum(_size(p) for p in _hidden_files(_hidden_run_truth(run_dir, root)))


@dataclass(frozen=True)
class RankedRun:
    run_dir: Path
    submission_id: str
    event_id: str
    seed_index: int
    rep: int


def _choose_samples(groups: Mapping[tuple[str, str], list[RankedRun]], recs: Mapping[Path, dict], rng: random.Random) -> set[Path]:
    """每个"提交 × 事件"抽 1 个保留完整录制的 Run：已登记为抽样的沿用；组内已有 Run 精简过而无抽样记录时（重入）不再补抽。"""
    out: set[Path] = set()
    for g in groups.values():
        g = sorted(g, key=lambda r: (r.seed_index, r.rep, str(r.run_dir)))
        rr = [(r, (recs[Path(r.run_dir)].get("ranked_eval") or {})) for r in g]
        prev = [r for r, x in rr if x.get("phase") == "sampled" or x.get("sample")]
        if prev:
            out.add(Path(prev[0].run_dir))
            continue
        if any(x.get("phase") == "compacted" for _r, x in rr):
            continue
        cand = [r for r in g if not _pinned(recs[Path(r.run_dir)])]
        if cand:
            out.add(Path(cand[rng.randrange(len(cand))].run_dir))
    return out


def finalize_ranked_eval(runs: Sequence[RankedRun], *, root: Path, policy: RetentionPolicy = POLICY, now: int | None = None,
                         rng: random.Random | None = None, tenant: str | None = None, dry_run: bool = False,
                         log: Callable[[str], None] | None = None, digest_dir: Path | None = None) -> dict:
    """提交评分完成后调用：按"提交 × test 事件"抽 1 个 Run 保留完整录制 `sample_days` 天（缺省以 CSPRNG 抽取），其余立即精简；
    同一"提交 × 事件 × 种子"的重复运行与首次运行（rep 最小者）逐帧摘要相同时去重（重入、pinned、失败与 `dry_run` 见模块说明）。
    `digest_dir` 给出时逐帧摘要经 `cached_frames_digest` 缓存。→ 每 Run（运行目录名）的处理结果
    `{phase, frames_digest_sha256, dup_of, bytes_before, bytes_after | bytes_freed, …}`。"""
    now = now or _now()
    rng = rng or secrets.SystemRandom()
    root = Path(root)
    log = log or (lambda _m: None)
    out: dict[str, dict] = {}
    present = []
    for r in runs:
        if Path(r.run_dir).is_dir():
            present.append(r)
        else:
            out[Path(r.run_dir).name] = {"phase": "missing"}
    with _locked(root):
        recs = {Path(r.run_dir): load_record(Path(r.run_dir)) for r in present}
        by_ev: dict[tuple[str, str], list[RankedRun]] = {}
        for r in present:
            by_ev.setdefault((r.submission_id, r.event_id), []).append(r)
        sampled = _choose_samples(by_ev, recs, rng)
        first: dict[tuple[str, str, int], tuple[str, str]] = {}
        if not dry_run:
            _register_external(root, [Path(r.run_dir) for r in present])
        for r in sorted(present, key=lambda r: (r.submission_id, r.event_id, r.seed_index, r.rep, str(r.run_dir))):
            d = Path(r.run_dir)
            rec = recs[d]
            key = (r.submission_id, r.event_id, r.seed_index)
            prev = rec.get("ranked_eval") or {}
            if _pinned(rec):                                  # pinned：不改类别、不精简（发布包或榜单引用）
                out[d.name] = {"phase": "pinned", "pinned_by": rec.get("pinned_by", [])}
                continue
            if prev.get("phase") in ("sampled", "compacted") and prev.get("frames_digest_sha256"):
                sha = prev["frames_digest_sha256"]              # 重入：沿用登记的摘要与结论
                if prev.get("dup_of") is None:
                    first.setdefault(key, (d.name, sha))
                out[d.name] = {"phase": prev["phase"], "frames_digest_sha256": sha, "dup_of": prev.get("dup_of") or prev.get("dup_candidate"),
                               "reused": True}
                if not dry_run:
                    clean_run_sockets(d)
                continue
            b0 = _run_total(d, root)
            try:
                body = cached_frames_digest(d, digest_dir)
            except Exception as e:                            # 单个 Run 的录制损坏或不可读：只记该 Run
                out[d.name] = {"phase": "error", "error": f"{type(e).__name__}: {e}"[:300], "bytes_before": b0}
                log(f"{d.name}: frames_digest failed: {type(e).__name__}: {e}")
                continue
            sha = hashlib.sha256(body).hexdigest()
            dup = first.get(key)
            if dup is None:
                first[key] = (d.name, sha)
            elif dup[1] != sha:
                dup = None                                        # 摘要不同：独立保存（平台复现审计据此告警）
            rec.update(retention="ranked_eval", state=rec.get("state") or "COMPLETED", t0_unix_ns=rec.get("t0_unix_ns") or now)
            if tenant is not None:
                rec["tenant"] = tenant
            rec["ranked_eval"] = {"submission_id": r.submission_id, "event_id": r.event_id, "seed_index": r.seed_index, "rep": r.rep,
                                  "frames_digest_sha256": sha, **({"dup_candidate": dup[0]} if dup else {})}
            if d in sampled:
                rec["ranked_eval"].update(phase="sampled", sample=True, sample_until_unix_ns=now + policy.sample_days * policy.day_ns)
                if not dry_run:
                    _save(d, rec)
                    clean_run_sockets(d)
                out[d.name] = {"phase": "sampled", "frames_digest_sha256": sha, "dup_of": dup[0] if dup else None, "bytes_before": b0,
                               "bytes_after": b0}
                continue
            if not dry_run:
                _save(d, rec)
            res = compact_ranked(d, root=root, dup_of=dup, now=now, digest=body, dry_run=dry_run)
            freed = sum(x["bytes"] for x in res["deleted"])
            out[d.name] = {"phase": ("planned" if dry_run else "compacted") if res["compacted"] else res.get("why"), "frames_digest_sha256": sha,
                           "dup_of": dup[0] if dup else None, "bytes_before": b0, "bytes_freed": freed,
                           "bytes_after": res.get("bytes_after") if not dry_run else b0 - freed, "errors": res.get("errors", [])}
            log(f"{d.name}: {out[d.name]['phase']} digest {sha[:12]}{' dup of ' + dup[0] if dup else ''} freed {freed / 1e6:.1f} MB")
    return out


def _board_run_dir(root: Path, cfg: dict, row: dict, runs_roots: Sequence[Path]) -> Path | None:
    """评测行 → 运行目录：`row.run_dir`、数据根 `dataset/<event>/runs/<run_id>`、`runs_roots/<run_id>`，以及 mbl_e2e 原型榜的
    `<mbl_round.out>/runs/<key>`（key 为去掉轮次前缀的 run_id）。"""
    rid = str(row.get("run_id") or "")
    if not rid and row.get("submission_id"):                 # 旧版榜单配置的提交行不带 run_id：`<submission_id>-r<k>`
        rid = f"{row['submission_id']}-r{int(row.get('seed', 0))}"
    if not rid:
        return None
    rnd = (cfg.get("mbl_round") or {}).get("round")
    names = [rid] + ([rid[len(rnd) + 1:]] if rnd and rid.startswith(f"{rnd}-") else [])
    cands = [Path(row["run_dir"])] if row.get("run_dir") else []
    cands.append(Path(root) / "dataset" / str(row.get("event")) / "runs" / rid)
    mo = (cfg.get("mbl_round") or {}).get("out")
    roots = [*map(Path, runs_roots), *([Path(mo) / "runs"] if mo else [])]
    cands += [r / n for r in roots for n in names]
    return next((c for c in cands if c.is_dir()), None)


def finalize_board(root: Path, board_dir: Path, *, runs_roots: Sequence[Path] = (), dry_run: bool = False, now: int | None = None,
                   rng: random.Random | None = None, policy: RetentionPolicy = POLICY, entries: set[str] | None = None,
                   log: Callable[[str], None] | None = None, digest_dir: Path | None = None, jobs: int = 1,
                   exclude: set[str] | None = None) -> dict:
    """榜单版本的 `ranked_eval` 保留（模块说明）：`rows.jsonl` 中 `evaluation.ranked` 为真的评测行逐行定位运行目录并按条目分组调用
    `finalize_ranked_eval`；`entries` 给出时只处理所列 entry_id；`exclude` 所列 run_id 本次不处理（登记为 `excluded`，例如待平台
    原因重跑的运行，其完整录制留作判定证据）。`digest_dir` 给出时逐帧摘要缓存于此，`jobs > 1` 时先以进程池
    并行算出尚未登记的 Run 的摘要（回填用）。→ `{board_version, runs{名: 结果}, unresolved[], totals}`。"""
    from awr.bench.boardstore import load_config, load_rows

    root, board_dir = Path(root), Path(board_dir)
    cfg = load_config(board_dir)
    ver = str(cfg.get("board_version") or board_dir.name)
    by_tenant: dict[str | None, list[RankedRun]] = {}
    unresolved, excluded = [], []
    for row in load_rows(board_dir):
        ev = row.get("evaluation") or {}
        if not ev.get("ranked") or (entries is not None and row.get("entry_id") not in entries):
            continue
        if exclude and row.get("run_id") in exclude:
            excluded.append(str(row["run_id"]))
            continue
        d = _board_run_dir(root, cfg, row, runs_roots)
        if d is None:
            unresolved.append(row.get("run_id"))
            continue
        # 基线条目在不同分轨下同名（B21：`baseline/track_share` 同时在 std3-l1 与 std3-l2），分组须带分轨，否则跨分轨比对重复与抽样
        sub = row.get("submission_id") or (f"{ver}:{row['track']}:{row['entry_id']}" if row.get("track") else f"{ver}:{row['entry_id']}")
        by_tenant.setdefault(row.get("team"), []).append(RankedRun(d, str(sub), str(row["event"]), int(row.get("seed", 0)), int(row.get("rep", 0))))
    if digest_dir is not None and jobs > 1:
        from concurrent.futures import ProcessPoolExecutor

        todo = [str(r.run_dir) for rs in by_tenant.values() for r in rs
                if not _pinned(load_record(r.run_dir)) and (load_record(r.run_dir).get("ranked_eval") or {}).get("phase") not in ("sampled", "compacted")]
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            for d, err in ex.map(_digest_job, [(x, str(digest_dir)) for x in todo]):
                if err is not None and log is not None:
                    log(f"{Path(d).name}: frames_digest failed: {err}")
    runs: dict[str, dict] = {rid: {"phase": "excluded"} for rid in excluded}
    for team, rs in sorted(by_tenant.items(), key=lambda x: str(x[0])):
        runs.update(finalize_ranked_eval(rs, root=root, policy=policy, now=now, rng=rng, tenant=team, dry_run=dry_run, log=log,
                                         digest_dir=digest_dir))
    ph = [x.get("phase") for x in runs.values()]
    totals = {"runs": len(runs), **{p: ph.count(p) for p in sorted(set(ph), key=str)},
              "dup": sum(1 for x in runs.values() if x.get("dup_of")), "bytes_before": sum(x.get("bytes_before", 0) for x in runs.values()),
              "bytes_freed": sum(x.get("bytes_freed", 0) for x in runs.values()),
              "errors": sum(len(x.get("errors") or []) + (x.get("phase") == "error") for x in runs.values())}
    return {"board_version": ver, "dry_run": dry_run, "runs": runs, "unresolved": unresolved, "totals": totals}


# ------------------------------------------------------------------ 697
def expired_reason(run_dir: Path, what: str = "run") -> dict | None:
    """已按保留策略清理的制品 → 697 原因；`what` 为 `run`（录制与真值）、派生物种类或 `full`（ranked_eval 的完整录制）。"""
    rec = load_record(run_dir)
    exp = rec.get("expired", {})
    t = exp.get("run") if what in ("run", "full") else exp.get("derived", {}).get(what)
    if what == "full" and t is None and rec.get("ranked_eval", {}).get("phase") == "compacted":
        t = rec["ranked_eval"].get("compacted_unix_ns")
    if t is None:
        return None
    return {"code": 697, "name": REASON_697, "detail": {"run_id": Path(run_dir).name, "what": what, "expired_unix_ns": t}}


# ------------------------------------------------------------------ retention_gc
def _derived_t0(rec: Mapping, kind: str, paths: list[Path]) -> int:
    t = rec.get("derived", {}).get(kind, {}).get("created_unix_ns")
    if t is not None:
        return int(t)
    ts = [q.stat().st_mtime_ns for p in paths for q in ([p] if p.is_file() else [p, *p.rglob("*")]) if q.exists()]
    return max(ts) if ts else 0


def _delete_rrd(paths: Iterable[Path], plan: _Plan, run_id: str, reason: str, deregister: Callable[[Path], None] | None) -> bool:
    """`.rrd` 先在 catalog 注销再删除（FR-085）；注销失败 → 不删除并记错误。"""
    files = [q for p in paths for q in ([p] if p.is_file() else sorted(p.rglob("*.rrd")))]
    if deregister is not None and not plan.dry_run:
        for q in files:
            try:
                deregister(q)
            except Exception as e:                               # 注销失败不得删除（catalog 中的段仍可被引用）
                plan.errors.append({"path": str(q), "run_id": run_id, "error": f"deregister: {e}"})
                return False
    for p in paths:
        plan.delete(p, reason, run_id, deregistered=deregister is not None)
    return True


def _gc_run(d: Path, plan: _Plan, policy: RetentionPolicy, deregister: Callable[[Path], None] | None) -> None:
    rec = load_record(d)
    now = plan.now
    rid = d.name
    state = rec.get("state", "COMPLETED")
    if state in ACTIVE_STATES:
        return
    changed = False
    run_pinned = _pinned(rec)
    # .attempts/<n>/：失败 attempt 与重启前部分产物 14 天
    if not run_pinned:
        for a in sorted((d / ".attempts").glob("*")) if (d / ".attempts").is_dir() else []:
            if a.stat().st_mtime_ns + policy.attempt_days * policy.day_ns <= now:
                plan.delete(a, "attempt_expired", rid)
    # ranked_eval 抽样录制到期 → 精简
    rr = rec.get("ranked_eval") or {}
    if rec.get("retention") == "ranked_eval" and rr.get("phase") == "sampled" and not run_pinned:
        if rr.get("sample_until_unix_ns", 0) <= now:
            dup = (rr["dup_candidate"], rr["frames_digest_sha256"]) if rr.get("dup_candidate") else None
            compact_ranked(d, root=plan.root, dup_of=dup, now=now, dry_run=plan.dry_run, reason="ranked_eval_sample_expired", _plan=plan)
        return
    if rec.get("retention") == "ranked_eval":
        return
    # Run 级
    run_expired = rec.get("expired", {}).get("run") is not None
    if not run_pinned and not run_expired:
        days = policy.failed_days if state in FAILED_STATES else policy.run_days.get(rec.get("retention", "standard"))
        t0 = rec.get("t0_unix_ns")
        if days is not None and t0 is not None and int(t0) + days * policy.day_ns <= now:
            reason = "failed_expired" if state in FAILED_STATES else f"{rec.get('retention', 'standard')}_expired"
            for p in _external_truth(d, plan.root):
                plan.delete(p, reason, rid)
            for p in sorted(d.iterdir()):
                if p.name in KEEP_ON_EXPIRE or p.name == "derived":
                    continue
                plan.delete(p, reason, rid)
            rec.setdefault("expired", {})["run"] = now
            run_expired = changed = True
    # 派生物级
    entries = _derived_entries(d)
    rrd_ext = _evalstore_rrd(d, plan.root)
    if rrd_ext is not None:
        entries.setdefault("rrd", []).append(rrd_ext)
    for kind, paths in sorted(entries.items()):
        drec = rec.get("derived", {}).get(kind, {})
        if _pinned(drec):
            continue
        cls = drec.get("retention") or DERIVED_DEFAULT.get(kind, "standard")
        if run_expired:
            reason = "source_run_expired"
        elif _derived_t0(rec, kind, paths) + policy.derived_days[cls] * policy.day_ns <= now:
            reason = f"derived_{cls}_expired"
        else:
            continue
        if kind == "rrd":
            ok = _delete_rrd(paths, plan, rid, reason, deregister)
        else:
            for p in paths:
                plan.delete(p, reason, rid)
            ok = True
        if ok:
            rec.setdefault("expired", {}).setdefault("derived", {})[kind] = now
            changed = True
    if changed and not plan.dry_run:
        _save(d, rec)


def retention_gc(root: Path, *, policy: RetentionPolicy = POLICY, now: int | None = None, dry_run: bool = False,
                 deregister_rrd: Callable[[Path], None] | None = None) -> dict:
    """清理作业 `retention_gc`（FR-059，每小时）：Run 级、派生物级、`.attempts` 与到期的 ranked_eval 抽样录制。
    → `{dry_run, deleted[], bytes, errors[]}`；`dry_run` 不删除、不写审计、不改登记。"""
    root = Path(root)
    plan = _Plan(dry_run, root, now or _now())
    with _locked(root):
        for d in _run_dirs(root):
            _gc_run(d, plan, policy, deregister_rrd)
    return {"dry_run": dry_run, "deleted": plan.deleted, "bytes": sum(x["bytes"] for x in plan.deleted), "errors": plan.errors}


# ------------------------------------------------------------------ 命令行
def _find(root: Path, run_id: str) -> Path:
    for d in _run_dirs(root):
        if d.name == run_id:
            return d
    raise SystemExit(f"run {run_id} not found")


def main(argv: list[str] | None = None) -> int:
    from awr.bench.settings import BenchSettings

    ap = argparse.ArgumentParser(prog="python -m awr.bench.retention")
    ap.add_argument("--data-root", type=Path, default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gc")
    g.add_argument("--dry-run", action="store_true")
    sub.add_parser("usage")
    for name in ("pin", "unpin"):
        p = sub.add_parser(name)
        p.add_argument("run_id")
        p.add_argument("--ref", required=True)
        p.add_argument("--derived", choices=DERIVED_KINDS)
    f = sub.add_parser("finalize", help="ranked_eval retention for the ranked rows of a board version (sample, compact, dedupe)")
    f.add_argument("--board", required=True, help="board version under <data_root>/boards/")
    f.add_argument("--runs-root", type=Path, action="append", default=[], help="extra directory holding run dirs (repeatable)")
    f.add_argument("--entry", action="append", default=None, help="only these entry_id values (repeatable)")
    f.add_argument("--skip-run", action="append", default=None, help="leave this run_id untouched (repeatable)")
    f.add_argument("--dry-run", action="store_true")
    f.add_argument("--digest-dir", type=Path, default=None, help="cache frames digests here (reused by a later run)")
    f.add_argument("--jobs", type=int, default=1, help="parallel digest workers (needs --digest-dir)")
    f.add_argument("--out", type=Path, default=None, help="write the full result JSON here")
    a = ap.parse_args(argv)
    root = a.data_root or BenchSettings.from_env().data_root
    if a.cmd == "finalize":
        res = finalize_board(root, Path(root) / "boards" / a.board, runs_roots=a.runs_root, dry_run=a.dry_run,
                             entries=set(a.entry) if a.entry else None, log=lambda m: print(m, flush=True), digest_dir=a.digest_dir,
                             jobs=a.jobs, exclude=set(a.skip_run) if a.skip_run else None)
        if a.out is not None:
            a.out.parent.mkdir(parents=True, exist_ok=True)
            a.out.write_text(json.dumps(res, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"board_version": res["board_version"], "dry_run": res["dry_run"], "unresolved": res["unresolved"][:20],
                          "totals": res["totals"]}, ensure_ascii=False, indent=1))
        return 1 if res["unresolved"] or res["totals"]["errors"] else 0
    if a.cmd == "gc":
        res = retention_gc(root, dry_run=a.dry_run)
        print(json.dumps({"dry_run": res["dry_run"], "n_deleted": len(res["deleted"]), "bytes": res["bytes"], "errors": res["errors"],
                          "deleted": res["deleted"] if a.dry_run else []}, ensure_ascii=False, indent=1))
        return 1 if res["errors"] else 0
    if a.cmd == "usage":
        u = Usage.scan(root)
        adm = StorageGate(root, usage_fn=lambda: u, audit=False).check()
        print(json.dumps({"used_bytes": u.used_bytes, "quota_bytes": POLICY.quota_bytes, "used_frac": round(adm.used_frac, 6),
                          "warn": adm.warn, "admitting": adm.ok, "by_tenant": dict(u.by_tenant)}, indent=1))
        return 0
    with _locked(root):
        d = _find(root, a.run_id)
        rec = (pin if a.cmd == "pin" else unpin)(d, a.ref, derived=a.derived)
    print(json.dumps({"run_id": a.run_id, "pinned_by": rec["pinned_by"], "derived": rec["derived"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
