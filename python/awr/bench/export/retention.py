"""导出派生物的保留期接入（M27-FR-058、059；AWR-17 §18.8.4；ADR-163；D3-AC-63"派生物按保留期清理"；B05 存储治理）。

数据集导出（`awr.bench.export.dataset`）的产物是运行目录下的派生物，保留登记与清理由 `awr.bench.retention`（B05）统一执行。本模块
把导出格式接入该机制，并补齐清理之后导出侧需要保持一致的部分：

- 格式与派生物种类：`files`（逐文件导出，`derived/` 下不在已知子目录内的条目，含 `export_manifest.json`）与 `bag`（`derived/bag/`）。
  缺省类别 bag 为 transient（7 天）、files 为 standard（30 天）（M27-FR-058）；导出请求可指定 `retention ∈ {transient, standard}`
  （AWR-17 `POST /runs/{rid}/exports`），作用于本次生成的格式。pinned 只经引用（`retention.pin`、`sync_references`）获得，不由请求指定。
- 登记：`register_export` 在导出完成后对本次生成的格式调用 `retention.register_derived`（重新登记会清除该格式的过期标记）；只导出
  bag 时，导出清单本身归入 files 类，files 未登记或已过期时以缺省类别重新登记。
- 导出清单与格式同寿：`export_manifest.json` 属于 files 类，files 先于其他格式到期（例如 bag 被发布包引用为 pinned，或 bag 在
  files 之后重新派生）时清单会随 files 删除。导出时把同一份清单逐字节镜像到拥有子目录的格式（`derived/bag/export_manifest.json`），
  镜像随该格式一起清理；`load_manifest` 先取主清单，主清单已清理时取仍存在的镜像。
- 状态：`format_status` 给出某一格式的类别、起算时刻、到期时刻与清理时刻（`retain_until_unix_ns` 与 BN-53 制品列表同口径）；
  `live_formats` 为清单中尚未清理的格式。
- 清理后的数据集索引：`retention_gc` 只改运行目录与保留登记，事件层 `index.json` 中的 `exports`、`export_manifest_sha256` 与
  `truth[]` 会滞后。`reconcile_index` 按当前状态重写：`exports` 取尚未清理的格式，清单哈希取当前生效的清单，`truth[]` 去掉已随
  来源 Run 清理的真值文件；内容不变时不写盘（索引仍无时刻字段，同一状态逐字节一致）。
- 清理作业入口：`gc` = `retention.retention_gc` + 全部事件索引的 `reconcile_index`（dry-run 时只返回计划）；部署的每小时清理作业
  （M27-FR-059）调用本入口，即 `python -m awr.bench.export.retention [--data-root D] gc [--dry-run]`，`reconcile` 子命令只重写索引。
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path

from awr.bench import retention as rt

__all__ = ["DERIVED_RETENTION", "FORMAT_DIRS", "FORMAT_KIND", "INDEX_LOCK", "MANIFEST", "REQUEST_CLASSES", "classes_for", "format_status", "gc",
           "live_formats", "load_manifest", "main", "manifest_paths", "mirror_manifest", "reconcile_all", "reconcile_index", "register_export"]

MANIFEST = "export_manifest.json"
INDEX_LOCK = ".index.lock"                                          # 与 dataset._update_index 同一锁文件
FORMAT_KIND = {"files": "files", "bag": "bag"}                      # 导出格式 → retention 派生物种类（DERIVED_KINDS）
DERIVED_RETENTION = {"files": "standard", "bag": "transient"}       # M27-FR-058 缺省类别
REQUEST_CLASSES = ("transient", "standard")                         # AWR-17 POST /runs/{rid}/exports 的 retention
FORMAT_DIRS = {"bag": "bag"}                                        # 拥有 derived/ 子目录的格式（清单镜像位置）


def classes_for(formats: Iterable[str], requested: str | None = None) -> dict[str, str]:
    """本次生成的格式 → 保留类别：给出 `requested`（transient 或 standard）时统一采用，否则取缺省类别。其他取值抛 ValueError。"""
    if requested is not None and requested not in REQUEST_CLASSES:
        raise ValueError(f"export retention {requested!r} not in {REQUEST_CLASSES}")
    return {f: requested or DERIVED_RETENTION[f] for f in formats}


def register_export(run_dir: Path, classes: Mapping[str, str], *, now: int) -> dict:
    """导出完成后登记派生物（B05 `register_derived`）：`classes` 为本次生成的格式与类别。files 未登记或已过期时（只导出 bag）以缺省
    类别登记，使新写的导出清单从本次起算。→ 保留登记。"""
    for fmt, cls in classes.items():
        rt.register_derived(run_dir, FORMAT_KIND[fmt], retention=cls, now=now)
    rec = rt.load_record(run_dir)
    if "files" not in classes and ("files" not in rec.get("derived", {}) or rt.expired_reason(run_dir, "files") is not None):
        rec = rt.register_derived(run_dir, "files", retention=DERIVED_RETENTION["files"], now=now)
    return rec


# ---------------------------------------------------------------------------------------------------------------- 清单与镜像
def manifest_paths(run_dir: Path) -> list[Path]:
    """导出清单的候选位置（按优先级）：`derived/export_manifest.json`，其后为各格式子目录中的镜像。"""
    d = Path(run_dir) / "derived"
    return [d / MANIFEST, *(d / sub / MANIFEST for sub in FORMAT_DIRS.values())]


def load_manifest(run_dir: Path) -> tuple[Path | None, dict | None]:
    """当前生效的导出清单 → (路径, 文档)；主清单已随 files 清理时取仍存在的镜像；都不存在时 (None, None)。"""
    for p in manifest_paths(run_dir):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict):
            return p, doc
    return None, None


def mirror_manifest(derived: Path, data: bytes, formats: Iterable[str]) -> list[Path]:
    """把导出清单字节镜像到 `formats` 中拥有子目录的格式（子目录存在时）；其余位置的旧镜像删除。→ 写出的镜像路径。"""
    derived = Path(derived)
    fmts = set(formats)
    out = []
    for fmt, sub in FORMAT_DIRS.items():
        p = derived / sub / MANIFEST
        if fmt in fmts and p.parent.is_dir():
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, p)
            out.append(p)
        else:
            p.unlink(missing_ok=True)
    return out


# ---------------------------------------------------------------------------------------------------------------- 状态
def format_status(run_dir: Path, fmt: str, *, policy: rt.RetentionPolicy = rt.POLICY, rec: Mapping | None = None) -> dict:
    """某一导出格式的保留状态：`{format, kind, retention, pinned_by, created_unix_ns, retain_until_unix_ns, expired_unix_ns}`；
    pinned（类别或引用）时 `retention = pinned`、`retain_until_unix_ns = None`；来源 Run 已清理时清理时刻取 Run 级。"""
    rec = rt.load_record(run_dir) if rec is None else rec
    kind = FORMAT_KIND[fmt]
    d = rec.get("derived", {}).get(kind, {})
    refs = list(d.get("pinned_by") or [])
    cls = "pinned" if refs or d.get("retention") == "pinned" else d.get("retention") or DERIVED_RETENTION[fmt]
    t0 = d.get("created_unix_ns")
    days = policy.derived_days.get(cls)
    exp = rec.get("expired", {})
    t_exp = exp.get("derived", {}).get(kind)
    return {"format": fmt, "kind": kind, "retention": cls, "pinned_by": refs, "created_unix_ns": t0,
            "retain_until_unix_ns": int(t0) + days * policy.day_ns if t0 is not None and days is not None else None,
            "expired_unix_ns": t_exp}


def live_formats(run_dir: Path, doc: Mapping | None = None) -> list[str]:
    """导出清单中尚未按保留期清理的格式（排序）；没有清单时为空。"""
    if doc is None:
        doc = load_manifest(run_dir)[1]
    if not doc:
        return []
    return sorted(f for f in doc.get("formats", []) if f in FORMAT_KIND and rt.expired_reason(run_dir, FORMAT_KIND[f]) is None)


# ---------------------------------------------------------------------------------------------------------------- 索引
@contextlib.contextmanager
def _index_locked(ev_root: Path) -> Iterator[None]:
    p = Path(ev_root) / INDEX_LOCK
    with p.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with Path(p).open("rb") as f:
        for b in iter(lambda: f.read(8 << 20), b""):
            h.update(b)
    return h.hexdigest()


def reconcile_index(ev_root: Path) -> dict:
    """按当前保留状态重写事件索引 `index.json`（见模块说明）。→ `{path, changed, runs{run_id: {exports_before, exports}}, truth_removed[]}`；
    没有索引时 `changed = False`。重写后的索引须通过 `bench/dataset_index.v1`，否则抛 ValueError 且不写盘。"""
    from awr.bench.export.dataset import check_index

    ev_root = Path(ev_root)
    p = ev_root / "index.json"
    out: dict = {"path": str(p), "changed": False, "runs": {}, "truth_removed": []}
    if not p.is_file():
        return out
    with _index_locked(ev_root):
        before = p.read_bytes()
        doc = json.loads(before)
        for row in doc.get("runs", []):
            rd = ev_root / "runs" / row["run_id"]
            mp, man = load_manifest(rd)
            live = live_formats(rd, man)
            sha = _sha_file(mp) if mp is not None else None
            if live != sorted(row.get("exports", [])) or sha != row.get("export_manifest_sha256"):
                out["runs"][row["run_id"]] = {"exports_before": row.get("exports", []), "exports": live}
            row["exports"], row["export_manifest_sha256"] = live, sha
        for layer in ("event", "truth"):
            keep = [r for r in doc.get(layer, []) if (ev_root / r["path"]).is_file()]
            if layer == "truth":
                out["truth_removed"] = sorted(r["path"] for r in doc.get(layer, []) if r not in keep)
            doc[layer] = keep
        errs = check_index(doc)
        if errs:
            raise ValueError(f"reconciled index {p} is invalid: {errs[:5]}")
        data = (json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode()
        if data != before:
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, p)
            out["changed"] = True
    return out


def reconcile_all(root: Path) -> list[dict]:
    """数据根下全部事件索引（`<root>/dataset/*/index.json`）的 `reconcile_index`；→ 有变化的索引报告。"""
    return [r for p in sorted(Path(root).glob("dataset/*/index.json")) for r in [reconcile_index(p.parent)] if r["changed"]]


def gc(root: Path, *, policy: rt.RetentionPolicy = rt.POLICY, now: int | None = None, dry_run: bool = False,
       deregister_rrd: Callable[[Path], None] | None = None) -> dict:
    """清理作业入口（M27-FR-059，每小时）：`retention.retention_gc` 后重写受影响的事件索引。→ retention_gc 的报告加 `indexes[]`
    （dry-run 时为空，不改任何文件）。"""
    out = rt.retention_gc(root, policy=policy, now=now, dry_run=dry_run, deregister_rrd=deregister_rrd)
    out["indexes"] = [] if dry_run else reconcile_all(root)
    return out


# ---------------------------------------------------------------------------------------------------------------- 命令行
def main(argv: list[str] | None = None) -> int:
    """`python -m awr.bench.export.retention [--data-root D] gc [--dry-run] | reconcile`。"""
    from awr.bench.settings import BenchSettings

    ap = argparse.ArgumentParser(prog="python -m awr.bench.export.retention", description="导出派生物保留期清理与数据集索引重写（M27-FR-058、059）")
    ap.add_argument("--data-root", type=Path, default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gc", help="retention_gc + index reconcile")
    g.add_argument("--dry-run", action="store_true")
    sub.add_parser("reconcile", help="rewrite dataset/*/index.json from the current retention state")
    a = ap.parse_args(argv)
    root = a.data_root or BenchSettings.from_env().data_root
    if a.cmd == "gc":
        rep = gc(root, dry_run=a.dry_run)
        print(json.dumps({"dry_run": rep["dry_run"], "deleted": len(rep["deleted"]), "bytes": rep["bytes"], "errors": rep["errors"],
                          "indexes": rep["indexes"]}, ensure_ascii=False, indent=1))
        return 1 if rep["errors"] else 0
    print(json.dumps(reconcile_all(root), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
