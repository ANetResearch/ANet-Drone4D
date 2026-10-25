"""test 划分登记、隐藏真值存放与真值访问审计（M27-FR-073、FR-113、FR-114、FR-120、FR-124、§7.2 V15；M23 §7.3.3；
AWR-17 §18.3.5；ADR-147；D3-AC-43、D3-AC-63）。

- 划分登记（M27-FR-113）：`<data_root>/bench/splits.json`（`drone4d.bench.splits.v1`，0600，管理员维护）以事件 id 为键登记
  `dev` 或 `test`，同一事件只有一个划分（文件中重复的键视为损坏，`SplitRegistryError`）；每次变更写审计 `split_change`
  （事件、原划分、新划分、操作者）。`test_split_version` 与 `digest()`（test 事件集合的摘要）进入榜单版本（M27-FR-164）。
  判定规则 `split_of`：登记表优先；事件自身声明 test（`event.json`、`plan.json` 的 `split`）时一律为 test（取更严格者，
  登记表写 dev 而事件声明 test 视为冲突，按 test 处理并由 `conflicts()` 列出）；未登记时取事件声明，缺省 dev；未知取值按 test。
- 隐藏存放（M27-FR-114）：test 事件的磁带（`tape.bin`、`tape.json`）、`truth.mcap`、`event_truth.json`、`event_graph.json`、
  `observability.npz`、路线与锚点（`plan.json`、`anchors.json`，目标搜索类含实际抽样位置）、编译报告（`compile_report.json`）、
  事件源（`event.yaml`、`event.json`、`anchors.overlay.json`）与 Run 真值文件只存于 `<data_root>/hidden/<event_id>/{event/v<N>/, truth/}`
  （目录 0700、文件 0600，属主为 Evaluator 服务用户，`perms` 子命令核对与整理）；事件编译目录 `events/<eid>/v<N>/` 只允许留存
  `EVENT_DIR_PUBLIC`（`brief.json` 与发布、停用标记），其余文件（含子目录，如编译器写出的 `truth/r<k>/event_truth.json`、
  `event_graph.json`）一律视为隐藏制品（K07）；
  `HiddenStore` 是读取 test 真值的唯一入口：每次读取写审计 `truth_read`（主体、角色、凭据类别、令牌 id、进程、相对路径、
  事件、Run、作业、request_id、结果），评测员与管理员以外的主体拒绝并返回 691（同样写审计，`result = denied`）。
  `seal_event` 把 test 事件的上述制品从事件编译目录（递归，保持相对路径）与数据集目录移入 hidden；`misplaced` 递归列出仍在
  hidden 之外的 test 制品（含已发布后才登记为 test 的事件）。
- 制品授权（M23 §7.3.3）：dev 事件的全部制品对研究者开放；test 事件只有 `brief.json` 对研究者开放，其余（`event.yaml`、
  `plan.json`、`anchors.json`、`compile_report.json`、磁带与真值）只对评测员及以上，越权 691。
- 运行与导出：Run 的划分取 RunStore 记录、运行清单（`run_manifest.json`、`manifest.json`、`evaluation.json`）与所属事件划分中
  最严格者（`run_split`）；test 划分的 Run 与评测运行（`retention = ranked_eval`、`kind = submission`）不可导出，返回 691
  （`export_check`，M27-FR-073；D3-AC-63）；研究者直接以 test 事件提交 Run 返回 691（`submit_check`，V15；M27-AC-032）。
- 外部访问计数（M27-FR-124；D3-AC-43）：`truth_access_summary` 统计审计日志中的 test 真值读取，`external` 为评测员与管理员以外
  主体的成功读取次数，验收要求恒为 0；`python -m awr.bench.split audit` 在 external > 0 时退出码为 1。

审计写入 `awr.bench.auth.AuditLog`（与 bench-api 访问审计同一组文件 `<data_root>/audit/access-<UTC 日期>.jsonl`），不记录令牌、
Cookie 值、种子明文与文件内容。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from awr.bench.auth import ROLES, AuditLog, Principal

__all__ = ["EVENT_ARTIFACTS", "EVENT_DIR_PUBLIC", "HIDDEN_ONLY", "INTERNAL_ROLES", "PUBLIC_ARTIFACTS", "SCHEMA", "SERVICE_EVALUATOR", "SPLITS",
           "TRUTH_FILES", "TRUTH_READ",
           "HiddenStore", "SplitAccessDenied", "SplitRegistry", "SplitRegistryError", "artifact_allowed", "declared_run_splits",
           "export_check", "external_truth_reads", "main", "read_audit", "run_split", "strictest", "submit_check", "truth_access_summary"]

SCHEMA = "drone4d.bench.splits.v1"
SPLITS = ("dev", "test")
INTERNAL_ROLES = ("evaluator", "admin")            # 可读 test 真值的角色；其余主体（受邀成员、研究者、无角色进程）为外部主体
TRUTH_READ = "truth_read"
SPLIT_CHANGE = "split_change"
# M23 §7.3.3：事件版本目录的制品白名单；test 事件只有 brief.json 对研究者开放
EVENT_ARTIFACTS = frozenset({"brief.json", "event.yaml", "plan.json", "anchors.json", "compile_report.json", "tape.bin", "tape.json",
                             "truth.mcap", "event_truth.json", "event_graph.json", "observability.npz"})
PUBLIC_ARTIFACTS = frozenset({"brief.json"})
# M27-FR-114（K07 修订）：test 事件只存于 hidden/ 的制品：磁带与真值，另含路线、锚点、编译报告与事件源（plan.json 含演员路线，
# anchors.json 在目标搜索类中含实际抽样位置）；BN-27 对这些名称一律从 hidden 读取
HIDDEN_ONLY = frozenset({"tape.bin", "tape.json", "truth.mcap", "event_truth.json", "event_graph.json", "observability.npz", "plan.json",
                         "anchors.json", "compile_report.json", "event.yaml", "event.json", "anchors.overlay.json"})
# test 事件编译目录 `events/<eid>/v<N>/` 中允许留存的文件（顶层）：对研究者开放的简报与发布、停用标记；其余文件（含子目录）均为隐藏制品
EVENT_DIR_PUBLIC = frozenset({"brief.json", "published.json", "deprecated.json"})
TRUTH_FILES = ("event_truth.json", "event_graph.json", "observability.npz")    # 编译器写在 `truth/r<k>/` 下的真值文件
# Evaluator 服务进程的主体（离线评测与平台审计经 HiddenStore 读取 test 真值时使用）
SERVICE_EVALUATOR = Principal("svc:evaluator", "evaluator", (), "service")


class SplitAccessDenied(Exception):
    """test 划分访问拒绝：`code = 691 TEST_SPLIT_ACCESS_DENIED`（403）；`detail.why ∈ {test_split, eval_run}`。"""

    code = 691

    def __init__(self, why: str = "test_split", **detail) -> None:
        super().__init__(f"691 {why}")
        self.detail = {"why": why, **detail}


class SplitRegistryError(ValueError):
    """划分登记表损坏：schema 不符、取值不在 {dev, test}、同一事件重复登记。"""


def strictest(values: Iterable[str | None]) -> str:
    """一组划分声明中最严格者：任一为 test 或取值未知即 test；全部缺省（None 或空串）为 dev。"""
    for v in values:
        if v not in (None, "", "dev"):
            return "test"
    return "dev"


def _canon(doc) -> bytes:
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _no_dup(pairs: list[tuple[str, object]]) -> dict:
    seen: dict = {}
    for k, v in pairs:
        if k in seen:
            raise SplitRegistryError(f"duplicate key {k!r} in splits.json")
        seen[k] = v
    return seen


def _write_private(p: Path, data: bytes) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, p)


# ── 划分登记 ─────────────────────────────────────────────────────────────────────────────────────────────────────────

class SplitRegistry:
    """`splits.json` 的读取与维护；读取按文件 mtime 缓存，写入原子替换。"""

    def __init__(self, path: Path, *, audit: AuditLog | None = None, clock=time.time_ns) -> None:
        self.path = Path(path)
        self.audit = audit
        self.clock = clock
        self._lock = threading.Lock()
        self._cache: tuple[int, dict] | None = None

    @classmethod
    def for_root(cls, data_root: Path, *, audit: AuditLog | None = None) -> SplitRegistry:
        return cls(Path(data_root) / "bench" / "splits.json", audit=audit)

    def load(self) -> dict:
        """→ `{schema, test_split_version, events{eid: split}, updated_unix_ns, updated_by}`；文件不存在时为空登记表。"""
        try:
            m = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            return {"schema": SCHEMA, "test_split_version": "t0", "events": {}, "updated_unix_ns": None, "updated_by": None}
        with self._lock:
            if self._cache and self._cache[0] == m:
                return self._cache[1]
        doc = json.loads(self.path.read_text(encoding="utf-8"), object_pairs_hook=_no_dup)
        if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or not isinstance(doc.get("events"), dict):
            raise SplitRegistryError(f"{self.path}: not a {SCHEMA} document")
        bad = sorted(k for k, v in doc["events"].items() if v not in SPLITS)
        if bad:
            raise SplitRegistryError(f"{self.path}: split must be one of {SPLITS}: {bad}")
        with self._lock:
            self._cache = (m, doc)
        return doc

    def registered(self, event_id: str) -> str | None:
        return self.load()["events"].get(event_id)

    def split_of(self, event_id: str, declared: str | None = None) -> str:
        """事件的有效划分（规则见模块说明）：登记值与事件声明中最严格者；都缺省时为 dev。"""
        return strictest((self.registered(event_id), declared))

    def events(self, split: str) -> list[str]:
        return sorted(k for k, v in self.load()["events"].items() if v == split)

    def digest(self) -> str:
        """test 划分摘要：sha256(JCS({test_split_version, test[]}))，用于榜单版本登记（M27-FR-164）。"""
        doc = self.load()
        return hashlib.sha256(_canon({"test_split_version": doc.get("test_split_version"), "test": self.events("test")})).hexdigest()

    def register(self, event_id: str, split: str, *, by: str, version: str | None = None) -> dict:
        """登记或改登记一个事件（管理员维护）；`version` 给出时同时更新 `test_split_version`。写审计 `split_change`。"""
        if split not in SPLITS:
            raise ValueError(f"split must be one of {SPLITS}")
        with self._lock:
            self._cache = None
        doc = json.loads(json.dumps(self.load()))
        prev = doc["events"].get(event_id)
        doc["events"][event_id] = split
        doc["events"] = dict(sorted(doc["events"].items()))
        if version is not None:
            doc["test_split_version"] = version
        doc["updated_unix_ns"] = str(int(self.clock()))
        doc["updated_by"] = by
        _write_private(self.path, (json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode())
        with self._lock:
            self._cache = None
        if self.audit is not None:
            self.audit.write(SPLIT_CHANGE, principal=by, event_id=event_id, previous=prev, split=split,
                             test_split_version=doc.get("test_split_version"))
        return doc

    def conflicts(self, declared: dict[str, str | None]) -> list[dict]:
        """登记表与事件声明不一致的事件（`declared`：事件 id → 事件自身声明的划分）。"""
        out = []
        for eid, d in sorted(declared.items()):
            r = self.registered(eid)
            if r is not None and d not in (None, "") and d != r:
                out.append({"event_id": eid, "registered": r, "declared": d, "effective": strictest((r, d))})
        return out


def artifact_allowed(split: str, name: str, role: str | None) -> bool:
    """M23 §7.3.3：事件制品 `name` 在划分 `split` 下是否对角色 `role` 开放（不含最低角色 researcher 的判定）。"""
    if split == "dev" or name in PUBLIC_ARTIFACTS:
        return True
    return role in INTERNAL_ROLES


# ── 运行划分、导出与提交 ─────────────────────────────────────────────────────────────────────────────────────────────

def declared_run_splits(run_dir: Path | None) -> list[str | None]:
    """运行目录中各清单声明的划分：`run_manifest.json`、`manifest.json`（Env 运行清单）、`evaluation.json` 的 `split`。"""
    if run_dir is None:
        return []
    out: list[str | None] = []
    for n in ("run_manifest.json", "manifest.json", "evaluation.json"):
        f = Path(run_dir) / n
        if f.is_file():
            try:
                doc = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                out.append("test")                         # 清单不可读：按 test 处理（宁严勿松）
                continue
            if isinstance(doc, dict):
                out.append(doc.get("split"))
    return out


def run_split(registry: SplitRegistry, *, event_id: str | None, declared: Iterable[str | None] = ()) -> str:
    """Run 的有效划分：RunStore 记录与运行清单的声明（`declared`）及所属事件的有效划分中最严格者。"""
    vals = list(declared)
    if event_id:
        vals.append(registry.split_of(event_id))
    return strictest(vals)


def export_check(*, split: str, run_id: str | None = None, retention_class: str | None = None, kind: str | None = None) -> None:
    """导出前置检查（M27-FR-073；D3-AC-63"test 划分与评测运行不可导出（691）"）：test 划分的 Run 抛 `why = test_split`，
    评测运行（`retention_class = ranked_eval` 或 `kind = submission`）抛 `why = eval_run`。"""
    if split != "dev":
        raise SplitAccessDenied("test_split", run_id=run_id, split=split)
    if retention_class == "ranked_eval" or kind == "submission":
        raise SplitAccessDenied("eval_run", run_id=run_id, retention=retention_class, kind=kind)


def submit_check(registry: SplitRegistry, event_id: str, *, role: str, channel: str = "api", declared: str | None = None) -> None:
    """V15（M27 §7.2；M27-AC-032）：事件属于 test 划分且调用方既不是评测员及以上、也不是提交通道（`channel = submission`）→ 691。"""
    if registry.split_of(event_id, declared) == "test" and role not in INTERNAL_ROLES and channel != "submission":
        raise SplitAccessDenied("test_split", event_id=event_id, split="test")


# ── 隐藏存放 ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def _process() -> dict:
    return {"pid": os.getpid(), "argv0": Path(sys.argv[0]).name if sys.argv and sys.argv[0] else None}


@dataclass
class HiddenStore:
    """`<data_root>/hidden/`：test 真值的唯一存放与读取入口（M27-FR-114、FR-124）。"""

    root: Path
    audit: AuditLog

    @classmethod
    def for_root(cls, data_root: Path, audit: AuditLog) -> HiddenStore:
        return cls(Path(data_root) / "hidden", audit)

    # 布局
    def event_dir(self, event_id: str, version: int) -> Path:
        return self.root / event_id / "event" / f"v{int(version)}"

    def truth_dir(self, event_id: str) -> Path:
        return self.root / event_id / "truth"

    def run_truth_paths(self, event_id: str, run_id: str) -> tuple[Path, Path]:
        """test Run 的真值文件与索引：`hidden/<event>/truth/run_<id>.mcap`、`run_<id>.frames.idx`（M27-FR-044）。"""
        t = self.truth_dir(event_id)
        return t / f"run_{run_id}.mcap", t / f"run_{run_id}.frames.idx"

    def _mkdir(self, d: Path) -> None:
        """建目录链并把 hidden 根以下各级设为 0700。"""
        d.mkdir(parents=True, exist_ok=True)
        p = d
        while True:
            os.chmod(p, 0o700)
            if p == self.root or self.root not in p.parents:
                break
            p = p.parent

    def _rel(self, path: Path) -> str:
        """审计用路径：hidden 内为相对 hidden 根的路径，hidden 外（未迁移的旧位置）带 `outside:` 前缀与绝对路径。"""
        path = Path(path)
        try:
            return path.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return f"outside:{path}"

    # 授权与审计
    def authorize(self, p: Principal | None, path: Path, *, event_id: str, run_id: str | None = None, job: str | None = None,
                  request_id: str | None = None, op: str = "read") -> None:
        """test 真值访问的授权点：写一条 `truth_read` 审计；主体不是评测员或管理员时 `result = denied` 并抛 691。"""
        role = p.role if p is not None and p.role in ROLES else None
        ok = role in INTERNAL_ROLES
        self.audit.write(TRUTH_READ, principal=p.name if p else None, role=role, via=p.via if p else None, token_id=p.token_id if p else None,
                         event_id=event_id, run_id=run_id, path=self._rel(path), op=op, job=job, request_id=request_id, process=_process(),
                         result="ok" if ok else "denied", code=None if ok else 691)
        if not ok:
            raise SplitAccessDenied("test_split", event_id=event_id, **({"run_id": run_id} if run_id else {}))

    def read_bytes(self, p: Principal | None, path: Path, *, event_id: str, run_id: str | None = None, job: str | None = None,
                   request_id: str | None = None) -> bytes:
        """经授权与审计读取一个 test 真值文件；文件不存在抛 FileNotFoundError（审计仍记录该次访问）。"""
        self.authorize(p, path, event_id=event_id, run_id=run_id, job=job, request_id=request_id)
        return Path(path).read_bytes()

    # 迁移与检查
    def event_file(self, event_id: str, version: int, name: str) -> Path:
        """hidden 中事件版本的制品路径：顶层 `<name>`；真值文件（`TRUTH_FILES`）顶层不存在时取最新修订 `truth/r<k>/<name>`。"""
        d = self.event_dir(event_id, version)
        f = d / name
        if f.is_file() or name not in TRUTH_FILES:
            return f
        revs = sorted((int(r.name[1:]) for r in (d / "truth").glob("r*") if r.is_dir() and r.name[1:].isdigit()), reverse=True)
        return next((d / "truth" / f"r{k}" / name for k in revs if (d / "truth" / f"r{k}" / name).is_file()), f)

    @staticmethod
    def hidden_event_files(vdir: Path) -> list[Path]:
        """事件版本目录 `vdir` 中属于隐藏制品的文件（递归；顶层的 `EVENT_DIR_PUBLIC` 除外），按相对路径排序。符号链接也计入
        （hidden 之外不应有指向真值的链接）。"""
        vdir = Path(vdir)
        if not vdir.is_dir():
            return []
        out = []
        for f in sorted(vdir.rglob("*")):
            if f.is_dir() and not f.is_symlink():
                continue
            if f.parent == vdir and f.name in EVENT_DIR_PUBLIC:
                continue
            out.append(f)
        return out

    @staticmethod
    def _version_dirs(edir: Path) -> list[Path]:
        if not Path(edir).is_dir():
            return []
        return sorted(d for d in Path(edir).iterdir() if d.is_dir() and d.name.startswith("v") and d.name[1:].isdigit())

    def seal_event(self, event_id: str, *, events_root: Path, dataset_root: Path) -> list[dict]:
        """把 test 事件的隐藏制品移入 hidden（M27-FR-114）：`events/<eid>/v<N>/` 中除 `EVENT_DIR_PUBLIC` 以外的全部文件（递归，含
        `truth/r<k>/`）→ `hidden/<eid>/event/v<N>/<相对路径>`；`dataset/<eid>/truth/*` → `hidden/<eid>/truth/`。目标已存在且内容相同则
        删除源，不同则保留两者并报告冲突；移空的子目录随之删除。已发布后才登记为 test 的事件同样适用。→ 动作列表。"""
        out: list[dict] = []
        moves: list[tuple[Path, Path]] = []
        for vdir in self._version_dirs(Path(events_root) / event_id):
            dst_root = self.event_dir(event_id, int(vdir.name[1:]))
            moves += [(f, dst_root / f.relative_to(vdir)) for f in self.hidden_event_files(vdir)]
        tdir = Path(dataset_root) / event_id / "truth"
        if tdir.is_dir():
            moves += [(f, self.truth_dir(event_id) / f.relative_to(tdir)) for f in sorted(tdir.rglob("*")) if f.is_file()]
        for src, dst in moves:
            self._mkdir(dst.parent)
            if dst.exists():
                if not src.is_symlink() and dst.read_bytes() == src.read_bytes():
                    src.unlink()
                    out.append({"action": "dedup", "src": str(src), "dst": self._rel(dst)})
                else:
                    out.append({"action": "conflict", "src": str(src), "dst": self._rel(dst)})
                continue
            if src.is_symlink():                                  # 不把链接搬进 hidden：只报告，由管理员处置
                out.append({"action": "conflict", "src": str(src), "dst": self._rel(dst), "why": "symlink"})
                continue
            shutil.move(str(src), str(dst))
            os.chmod(dst, 0o600)
            out.append({"action": "move", "src": str(src), "dst": self._rel(dst)})
        for vdir in self._version_dirs(Path(events_root) / event_id):   # 删除移空的子目录（版本目录本身保留，供简报与发布标记）
            for d in sorted((x for x in vdir.rglob("*") if x.is_dir() and not x.is_symlink()), key=lambda x: len(x.parts), reverse=True):
                if not any(d.iterdir()):
                    d.rmdir()
        if out:
            self.audit.write("hidden_seal", event_id=event_id, n=len(out), conflicts=sum(1 for a in out if a["action"] == "conflict"),
                             process=_process())
        return out

    def misplaced(self, registry: SplitRegistry, *, events_root: Path, dataset_root: Path,
                  declared: dict[str, str | None] | None = None) -> list[str]:
        """仍在 hidden 之外的 test 制品（M27-FR-114 的违规清单）：test 事件编译目录中除 `EVENT_DIR_PUBLIC` 以外的全部文件（递归）、
        `dataset/<eid>/truth/` 下的文件、test 事件下各 Run 目录 `truth/` 中的文件。`declared` 为事件自身声明的划分（缺省只按登记表）。"""
        declared = declared or {}
        eids = set(registry.load()["events"]) | set(declared)
        for root in (Path(events_root), Path(dataset_root)):
            if root.is_dir():
                eids |= {d.name for d in root.iterdir() if d.is_dir()}
        out: list[str] = []
        for eid in sorted(eids):
            if registry.split_of(eid, declared.get(eid)) != "test":
                continue
            for vdir in self._version_dirs(Path(events_root) / eid):
                out += [str(f) for f in self.hidden_event_files(vdir)]
            ddir = Path(dataset_root) / eid
            out += [str(f) for f in sorted((ddir / "truth").rglob("*")) if f.is_file()]
            out += [str(f) for f in sorted(ddir.glob("runs/*/truth/**/*")) if f.is_file()]
        return out

    # 属主与权限
    def permission_problems(self, *, uid: int | None = None, gid: int | None = None) -> list[dict]:
        """hidden 根及其下全部条目的属主与权限核对（M27-FR-114）：目录 0700、普通文件 0600、不允许符号链接与其他类型；给出
        `uid`、`gid` 时属主须为 Evaluator 服务用户。→ [{path（相对 hidden 根）, kind, mode, uid, gid, problems[]}]。"""
        out: list[dict] = []
        if not self.root.exists():
            return out
        for p in [self.root, *sorted(self.root.rglob("*"))]:
            st = p.lstat()
            if p.is_symlink():
                kind, want = "symlink", None
            elif p.is_dir():
                kind, want = "dir", 0o700
            elif p.is_file():
                kind, want = "file", 0o600
            else:
                kind, want = "other", None
            probs = []
            if want is None:
                probs.append(f"{kind} not allowed")
            elif stat.S_IMODE(st.st_mode) != want:
                probs.append(f"mode {stat.S_IMODE(st.st_mode):04o} != {want:04o}")
            if uid is not None and st.st_uid != uid:
                probs.append(f"uid {st.st_uid} != {uid}")
            if gid is not None and st.st_gid != gid:
                probs.append(f"gid {st.st_gid} != {gid}")
            if probs:
                out.append({"path": "." if p == self.root else p.relative_to(self.root).as_posix(), "kind": kind,
                            "mode": f"{stat.S_IMODE(st.st_mode):04o}", "uid": st.st_uid, "gid": st.st_gid, "problems": probs})
        return out

    def fix_permissions(self, *, uid: int | None = None, gid: int | None = None) -> int:
        """把 hidden 根及其下目录设为 0700、文件 0600，给出 `uid`、`gid` 时改属主（需 root）；符号链接与其他类型不处理（仍由
        `permission_problems` 报告）。写审计 `hidden_perms`。→ 修改的条目数。"""
        n = 0
        if not self.root.exists():
            return n
        for p in [self.root, *sorted(self.root.rglob("*"))]:
            if p.is_symlink() or not (p.is_dir() or p.is_file()):
                continue
            st = p.lstat()
            want = 0o700 if p.is_dir() else 0o600
            changed = False
            if stat.S_IMODE(st.st_mode) != want:
                os.chmod(p, want)
                changed = True
            if (uid is not None and st.st_uid != uid) or (gid is not None and st.st_gid != gid):
                os.chown(p, st.st_uid if uid is None else uid, st.st_gid if gid is None else gid)
                changed = True
            n += changed
        self.audit.write("hidden_perms", n=n, uid=uid, gid=gid, process=_process())
        return n


# ── 审计统计 ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def read_audit(audit_dir: Path) -> list[dict]:
    """审计目录下全部 `access-*.jsonl` 记录（按文件名即 UTC 日期顺序）。"""
    out: list[dict] = []
    for f in sorted(Path(audit_dir).glob("access-*.jsonl")):
        out += [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
    return out


def external_truth_reads(records: Iterable[dict]) -> list[dict]:
    """外部主体对 test 真值的成功读取：`truth_read` 且 `result = ok`，而角色不是评测员或管理员。验收要求为空（D3-AC-43）。"""
    return [r for r in records if r.get("action") == TRUTH_READ and r.get("result") == "ok" and r.get("role") not in INTERNAL_ROLES]


def truth_access_summary(records: Iterable[dict]) -> dict:
    """test 真值访问统计：`reads`（全部 truth_read）、`internal`（评测员与管理员的成功读取）、`external`（外部主体成功读取，须为 0）、
    `denied`（拒绝次数，含 bench-api 的 691 deny 记录）与 `denied_principals`。"""
    recs = list(records)
    tr = [r for r in recs if r.get("action") == TRUTH_READ]
    ext = external_truth_reads(tr)
    deny = [r for r in tr if r.get("result") == "denied"] + [r for r in recs if r.get("action") == "deny" and r.get("code") == 691]
    return {"reads": len(tr), "internal": sum(1 for r in tr if r.get("result") == "ok") - len(ext), "external": len(ext),
            "external_principals": sorted({str(r.get("principal")) for r in ext}), "denied": len(deny),
            "denied_principals": sorted({str(r.get("principal")) for r in deny})}


# ── 命令行 ───────────────────────────────────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    """`python -m awr.bench.split show | set <event> <dev|test> --by NAME [--version V] | seal <event> | misplaced | audit
    | perms [--uid N] [--gid N] [--fix]`。"""
    from awr.bench.settings import BenchSettings

    ap = argparse.ArgumentParser(prog="python -m awr.bench.split")
    ap.add_argument("--data-root", type=Path, default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    s = sub.add_parser("set")
    s.add_argument("event_id")
    s.add_argument("split", choices=SPLITS)
    s.add_argument("--by", required=True)
    s.add_argument("--version")
    g = sub.add_parser("seal")
    g.add_argument("event_id")
    sub.add_parser("misplaced")
    sub.add_parser("audit")
    m = sub.add_parser("perms", help="check (or --fix) hidden/ ownership and modes: dirs 0700, files 0600, owner = Evaluator service user")
    m.add_argument("--uid", type=int)
    m.add_argument("--gid", type=int)
    m.add_argument("--fix", action="store_true")
    a = ap.parse_args(argv)
    root = a.data_root or BenchSettings.from_env().data_root
    audit = AuditLog(root / "audit")
    reg = SplitRegistry.for_root(root, audit=audit)
    hs = HiddenStore.for_root(root, audit)
    if a.cmd == "show":
        doc = reg.load()
        print(json.dumps({**doc, "digest": reg.digest()}, ensure_ascii=False, indent=1))
        return 0
    if a.cmd == "set":
        doc = reg.register(a.event_id, a.split, by=a.by, version=a.version)
        print(json.dumps({"event_id": a.event_id, "split": a.split, "test_split_version": doc["test_split_version"], "digest": reg.digest()}))
        return 0
    if a.cmd == "seal":
        if reg.split_of(a.event_id) != "test":
            print(json.dumps({"error": "not a test event", "event_id": a.event_id}))
            return 2
        acts = hs.seal_event(a.event_id, events_root=root / "events", dataset_root=root / "dataset")
        print(json.dumps({"event_id": a.event_id, "actions": acts}, ensure_ascii=False, indent=1))
        return 1 if any(x["action"] == "conflict" for x in acts) else 0
    if a.cmd == "perms":
        fixed = hs.fix_permissions(uid=a.uid, gid=a.gid) if a.fix else 0
        bad = hs.permission_problems(uid=a.uid, gid=a.gid)
        print(json.dumps({"root": str(hs.root), "fixed": fixed, "problems": bad}, ensure_ascii=False, indent=1))
        return 1 if bad else 0
    if a.cmd == "misplaced":
        bad = hs.misplaced(reg, events_root=root / "events", dataset_root=root / "dataset")
        print(json.dumps({"misplaced": bad}, ensure_ascii=False, indent=1))
        return 1 if bad else 0
    summ = truth_access_summary(read_audit(root / "audit"))
    print(json.dumps(summ, ensure_ascii=False, indent=1))
    return 1 if summ["external"] else 0


if __name__ == "__main__":
    sys.exit(main())
