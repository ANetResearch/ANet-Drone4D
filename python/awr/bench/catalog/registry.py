"""catalog 注册清单（M27-FR-079、080、085；AWR-05 §6.1.4；D3-AC-20、D3-AC-16）：评测与分发两个 catalog 的层策略、注册前校验、
持久化清单 `<root>/<catalog>/registry.json` 与启动重放、按保留期清理前的注销回调。

层策略（`CATALOG_LAYERS`）：

- 评测 catalog（`eval`）注册全部层：`obs.<uav>`、`agent.<uav>`、`truth`、`eval`；
- 分发 catalog（`dist`）只注册 `obs.<uav>` 与 `agent.<uav>`：真值层与评测层在生成注册计划时排除（`excluded`），直接请求注册时
  拒绝（`CatalogError`）；在只含观测层与 Agent 层的 dataset 上查询 `/truth/**` 返回 0 行（r8 §9.6）；
- asset（世界底图 `basemap.rrd` 等，只含静态 `/world/**`）两个 catalog 均可注册：单个 ≤ 300 MiB，每个 dataset ≤ 12 个。

注册流程（M27 §6.7.8 第 3 步）：校验 → `rerun rrd optimize` 原地压实（同目录标记 `.<name>.optimized.json`，同一文件只压实一次）→ 写清单 →
在线注册。注册前校验（`check_layer_file`）：文件存在；`.rrd` 写出版本与锁定版本同一 minor，否则以 694 拒绝并给出迁移提示（`rerun rrd
migrate` 或由 MCAP 重新派生）；`lint_rrd` 路径校验（application_id、recording_id = run_id、层归属、观测分片只含本机实体）；分发
catalog 另查：实体中无 `/truth/**` 与 `/eval/**`，段属性无 `seed`（排名运行的种子只写入评测 catalog，M27-FR-006、079）。

命名：dataset = `<world>.<event>`（两段各经 `path_part` 规范化，点号只作层级分隔，例如 `campus-v2.handoff-01`）；segment =
`recording_id = run_id`；layer = 导出清单 `export.json` 的文件键（`obs.<uav>`、`truth`、`eval`），Agent 层为同目录的
`agent.<uav>.rrd`。dataset 默认版面（blueprint）随注册生成：评测 catalog 为评测版面（真值 + 观测 + 信念 + 评测序列），分发
catalog 为 Agent 版面（单机观测与信念），文件位于 `<root>/<catalog>/blueprints/`。

持久化（M27-FR-080）：catalog 服务的元数据只在内存中，注册清单 `registry.json`（`drone4d.bench.rerun_registry.v1`）由数据集目录
生成（`rebuild`，扫描 `<data_root>/dataset/*/runs/*/derived/rrd/export.json`），每次注册与注销原子改写（`registry.lock` 互斥），
catalog 服务启动时由 `replay` 自动重放；重放时文件缺失或版本不一致的条目跳过并在报告中列出，不中断其余条目。

保留期清理（M27-FR-085）：`deregister_callback` 返回 `retention_gc(deregister_rrd=...)` 的回调：先在在线 catalog 注销对应
segment 的该层（或 asset），再从注册清单删除条目；在线注销失败时抛出异常，保留清理因此不删除文件。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

from awr.bench.recorder.rerun_export import (
    EXPORT_SCHEMA,
    RERUN_LOCKED,
    VERSION_MISMATCH,
    check_version,
    lint_rrd,
    path_part,
    rrd_version,
    scan_rrd,
)

__all__ = ["ASSET_MAX_BYTES", "ASSET_MAX_PER_DATASET", "CATALOGS", "CATALOG_LAYERS", "REGISTRY_SCHEMA", "CatalogError", "CatalogVersionError",
           "Entry", "Registry", "apply_entries", "check_layer_file", "dataset_name", "deregister_callback", "is_optimized", "layer_allowed",
           "layer_kind", "optimize_rrd", "plan_export", "rebuild", "register_asset", "register_run", "replay", "write_blueprints"]

REGISTRY_SCHEMA = "drone4d.bench.rerun_registry.v1"
CATALOGS = ("eval", "dist")
CATALOG_LAYERS: dict[str, tuple[str, ...]] = {"eval": ("obs", "agent", "truth", "eval"), "dist": ("obs", "agent")}
ASSET_MAX_BYTES = 300 * 2 ** 20                         # Rerun OSS catalog：单个 asset ≤ 300 MiB
ASSET_MAX_PER_DATASET = 12                              # 每个 dataset 至多 12 个 asset
_LAYER = re.compile(r"^(obs|agent)\.([a-z0-9_-]+)$")
_BLUEPRINT_KIND = {"eval": "eval", "dist": "agent"}     # dataset 默认版面：评测 catalog 为评测版面，分发 catalog 为 Agent 版面


class CatalogError(RuntimeError):
    """注册被拒绝（层策略、路径校验、种子或 asset 限额）；`code` 为原因码（无对应原因码时为 None），`detail` 为问题列表。"""

    code: int | None = None

    def __init__(self, msg: str, detail: dict | None = None, *, code: int | None = None) -> None:
        super().__init__(msg)
        self.detail = detail or {}
        if code is not None:
            self.code = code


class CatalogVersionError(CatalogError):
    """`.rrd` 或 segment 属性 `rerun_version` 与运行时不同 minor（694 RERUN_VERSION_MISMATCH）；`detail` 含迁移提示 `hint`。"""

    code = VERSION_MISMATCH


# ---------------------------------------------------------------------- 命名与层策略


def dataset_name(world: str, event: str) -> str:
    """dataset 名 `<world>.<event>`：两段各经 `path_part` 规范化（世界或事件 id 中的点号改为 `-`），点号只作层级分隔。"""
    w, e = path_part(world), path_part(event)
    if not w or not e:
        raise ValueError(f"world and event ids are required (got {world!r}, {event!r})")
    return f"{w}.{e}"


def layer_kind(layer: str) -> tuple[str, str | None]:
    """层名 → (层类别, 机号)：`obs.<uav>` → ("obs", uav)、`agent.<uav>` → ("agent", uav)、`truth`、`eval`、`asset`。"""
    if layer in ("truth", "eval", "asset"):
        return layer, None
    m = _LAYER.match(layer)
    if not m:
        raise ValueError(f"layer {layer!r} is not one of obs.<uav>, agent.<uav>, truth, eval")
    return m.group(1), m.group(2)


def layer_allowed(catalog: str, layer: str) -> bool:
    if catalog not in CATALOGS:
        raise ValueError(f"catalog {catalog!r} not in {CATALOGS}")
    return layer_kind(layer)[0] in CATALOG_LAYERS[catalog]


def _uri(path: Path) -> str:
    return Path(path).absolute().as_uri()


def _norm(path: Path | str) -> str:
    return str(Path(path).absolute())


# ---------------------------------------------------------------------- 条目与注册计划


@dataclass(frozen=True)
class Entry:
    """注册清单的一行：一个层文件注册到某 dataset 的某 segment。"""

    dataset: str
    segment: str
    layer: str
    path: str
    application_id: str
    bytes: int = 0
    rrd_version: str = ""
    source: str | None = None                           # 来源导出清单 export.json
    registered_unix_ns: int = 0
    mtime_ns: int = 0
    optimized: bool = False                             # 注册前已经 `rerun rrd optimize` 压实

    @property
    def key(self) -> tuple[str, str, str]:
        return self.dataset, self.segment, self.layer

    @classmethod
    def from_dict(cls, d: Mapping) -> Entry:
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


def plan_export(export: Path, catalog: str, *, agent_files: Iterable[Path] = ()) -> tuple[list[Entry], list[str]]:
    """由导出清单 `export.json`（`drone4d.bench.rrd_export.v1`）生成某 catalog 的注册计划 → (条目, 被层策略排除的层名)。
    Agent 层取导出目录内的 `agent.<uav>.rrd` 与 `agent_files`（M27-FR-054 收集的 Agent 调试录制）。"""
    p = Path(export)
    if p.is_dir():
        p = p / "export.json"
    man = json.loads(p.read_text(encoding="utf-8"))
    if man.get("schema") != EXPORT_SCHEMA:
        raise CatalogError(f"{p}: not an rrd export manifest (schema {man.get('schema')!r})")
    ds = dataset_name(man["world"], man["event"])
    seg, app = str(man.get("recording_id") or man["run_id"]), str(man["application_id"])
    files: dict[str, Path] = {k: Path(f["path"]) for k, f in sorted((man.get("files") or {}).items())}
    for a in sorted({*p.parent.glob("agent.*.rrd"), *map(Path, agent_files)}):
        files.setdefault(a.name[: -len(".rrd")], a)
    out, excluded = [], []
    for layer, path in files.items():
        if not path.is_absolute():                              # 相对路径：先按导出目录解析，其次按当前目录（导出时的 --out）
            path = p.parent / path if (p.parent / path).exists() or not path.exists() else path.absolute()
        if not layer_allowed(catalog, layer):
            excluded.append(layer)
            continue
        out.append(Entry(ds, seg, layer, _norm(path), app, source=_norm(p)))
    return out, excluded


def check_layer_file(entry: Entry, catalog: str) -> Entry:
    """注册前校验（见模块说明）；通过时返回补齐字节数与写出版本的条目，否则抛 CatalogError / CatalogVersionError。"""
    if not layer_allowed(catalog, entry.layer):
        raise CatalogError(f"layer {entry.layer!r} may not be registered to the {catalog} catalog (allowed: {list(CATALOG_LAYERS[catalog])})",
                           {"layer": entry.layer, "catalog": catalog})
    p = Path(entry.path)
    if not p.is_file():
        raise CatalogError(f"{p}: file not found", {"path": str(p)})
    ver = rrd_version(p)
    h = check_version(ver, RERUN_LOCKED)
    if h is not None:
        raise CatalogVersionError(f"694 RERUN_VERSION_MISMATCH: {p.name} written by rerun {ver}: {h['hint']}", {**h, "path": str(p)})
    kind, uav = layer_kind(entry.layer)
    errs = lint_rrd(p, kind, uav=uav, run_id=entry.segment if kind != "asset" else None, app_id=entry.application_id)
    if catalog == "dist":
        inv = scan_rrd(p)
        ents = {*inv.entities, *inv.static}
        errs += [f"{e}: truth / eval entities are not allowed in the dist catalog" for e in sorted(ents)
                 if e.startswith(("/truth", "/eval"))]
        if "seed" in inv.properties:
            errs.append("segment property 'seed' is not allowed in the dist catalog (ranked seeds stay in the eval catalog)")
    if errs:
        raise CatalogError(f"{p.name}: refused for the {catalog} catalog: {errs}", {"path": str(p), "problems": errs})
    return Entry(**{**asdict(entry), "bytes": p.stat().st_size, "rrd_version": ver})


# ---------------------------------------------------------------------- 注册清单


@dataclass
class Registry:
    """一个 catalog 的注册清单 `<root>/<catalog>/registry.json`；`with reg.edit():` 内的修改在退出时原子写回（文件锁互斥）。"""

    root: Path
    catalog: str
    entries: dict[tuple[str, str, str], Entry] = field(default_factory=dict)
    assets: dict[tuple[str, str], dict] = field(default_factory=dict)          # (dataset, path) → {dataset, path, segment, bytes}
    updated_unix_ns: int = 0

    def __post_init__(self) -> None:
        if self.catalog not in CATALOGS:
            raise ValueError(f"catalog {self.catalog!r} not in {CATALOGS}")
        self.root = Path(self.root)

    @property
    def dir(self) -> Path:
        return self.root / self.catalog

    @property
    def path(self) -> Path:
        return self.dir / "registry.json"

    @classmethod
    def load(cls, root: Path, catalog: str) -> Registry:
        r = cls(Path(root), catalog)
        r._read()
        return r

    def _read(self) -> None:
        self.entries, self.assets = {}, {}
        if not self.path.is_file():
            return
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        if doc.get("schema") != REGISTRY_SCHEMA or doc.get("catalog") != self.catalog:
            raise CatalogError(f"{self.path}: not a {self.catalog} registry ({doc.get('schema')!r}, {doc.get('catalog')!r})")
        for d in doc.get("entries", []):
            e = Entry.from_dict(d)
            self.entries[e.key] = e
        for a in doc.get("assets", []):
            self.assets[(a["dataset"], a["path"])] = dict(a)
        self.updated_unix_ns = int(doc.get("updated_unix_ns", 0))

    def _write(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.updated_unix_ns = time.time_ns()
        doc = {"schema": REGISTRY_SCHEMA, "catalog": self.catalog, "updated_unix_ns": self.updated_unix_ns,
               "layers": list(CATALOG_LAYERS[self.catalog]),
               "entries": [asdict(e) for _, e in sorted(self.entries.items())],
               "assets": [a for _, a in sorted(self.assets.items())]}
        tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    @contextlib.contextmanager
    def edit(self) -> Iterator[Registry]:
        """加锁、重读、修改、原子写回；多个注册作业与保留清理并发时不丢条目。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        with (self.dir / "registry.lock").open("a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                self._read()
                yield self
                self._write()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def datasets(self) -> list[str]:
        return sorted({k[0] for k in self.entries} | {k[0] for k in self.assets})

    def of_dataset(self, dataset: str) -> list[Entry]:
        return [e for k, e in sorted(self.entries.items()) if k[0] == dataset]

    def by_path(self, path: Path | str) -> list[Entry]:
        p = _norm(path)
        return [e for e in self.entries.values() if e.path == p]


# ---------------------------------------------------------------------- 版面（blueprint）


def _uavs(entries: Iterable[Entry]) -> list[str]:
    return sorted({u for e in entries for k, u in [layer_kind(e.layer)] if u is not None})


def _cams(entries: Iterable[Entry]) -> list[str]:
    cams: set[str] = set()
    for e in entries:
        if e.source and Path(e.source).is_file():
            sm = json.loads(Path(e.source).read_text(encoding="utf-8")).get("sensor_map") or {}
            cams.update((sm.get("cam") or {}).values())
    return sorted(cams) or ["cam0"]


def blueprint(kind: str, uavs: list[str], cams: list[str]):
    """评测版面（真值 + 观测 + 信念 + 评测序列）或 Agent 版面（单机观测与信念，不含 `/truth/**` 与 `/eval/**`）。"""
    import rerun.blueprint as rrb

    if kind not in ("eval", "agent"):
        raise ValueError(f"blueprint kind {kind!r} not in ('eval', 'agent')")
    contents = ["/world/**", "/fleet/**", "/agents/**", *(["/truth/**"] if kind == "eval" else [])]
    scene = rrb.Spatial3DView(origin="/", contents=contents, name="scene")
    cams_v = [rrb.Spatial2DView(origin=f"/fleet/{u}/{c}/image", name=f"{u} {c}") for u in uavs for c in cams]
    series = [rrb.TimeSeriesView(origin="/fleet", contents=["/fleet/*/telemetry/**"], name="telemetry")]
    if kind == "eval":
        series.append(rrb.TimeSeriesView(origin="/eval", name="eval"))
    right = rrb.Vertical(rrb.Tabs(*cams_v) if cams_v else rrb.Spatial2DView(origin="/fleet"), *series)
    return rrb.Blueprint(rrb.Horizontal(scene, right, column_shares=[3, 2]), collapse_panels=True)


def write_blueprints(reg: Registry, dataset: str) -> Path | None:
    """生成 dataset 默认版面文件 `<root>/<catalog>/blueprints/<dataset>.<kind>.rbl`；dataset 无层条目时返回 None。"""
    ents = reg.of_dataset(dataset)
    if not ents:
        return None
    kind = _BLUEPRINT_KIND[reg.catalog]
    out = reg.dir / "blueprints" / f"{dataset}.{kind}.rbl"
    out.parent.mkdir(parents=True, exist_ok=True)
    blueprint(kind, _uavs(ents), _cams(ents)).save(ents[0].application_id, str(out))
    return out


# ---------------------------------------------------------------------- 在线 catalog


def apply_entries(client, reg: Registry, entries: Iterable[Entry], *, blueprints: bool = True) -> dict:
    """把条目注册到在线 catalog（rerun.catalog.CatalogClient）：每个 dataset 一次批量 register（同层重复时替换）。"""
    from rerun.catalog import OnDuplicateSegmentLayer

    by_ds: dict[str, list[Entry]] = {}
    for e in entries:
        by_ds.setdefault(e.dataset, []).append(e)
    out: dict = {"datasets": {}, "blueprints": {}}
    for ds, es in sorted(by_ds.items()):
        entry = client.create_dataset(ds, exist_ok=True)
        entry.register([_uri(Path(e.path)) for e in es], layer_name=[e.layer for e in es],
                       on_duplicate=OnDuplicateSegmentLayer.REPLACE).wait()
        out["datasets"][ds] = len(es)
        if blueprints:
            bp = write_blueprints(reg, ds)
            if bp is not None:
                old = entry.blueprints()
                entry.register_blueprint(_uri(bp))                    # 每次保存的版面有新的 recording id：替换后注销旧版面
                stale = [b for b in old if b != entry.default_blueprint()]
                if stale:
                    entry.blueprint_dataset().unregister(segments_to_drop=stale, layers_to_drop=[]).wait()
                out["blueprints"][ds] = str(bp)
    return out


def optimize_rrd(path: Path) -> None:
    """`rerun rrd optimize`（object-store 档：按时间轴压实 Chunk）原地改写：实体、段属性与逐实体行（时刻与分量值）不变，Chunk
    边界改变，因此 `rerun rrd compare`（按 Chunk 比较）须在双方同样压实后进行（离线 / 在线一致性核对在注册之前完成）；catalog
    按实体与时间过滤时读取的 Chunk 数显著减少（M27 §6.7.8 第 3 步：verify、lint、optimize，再注册）。dmax 实测 3 Run × 3 机 ×
    360 s（2.8 GB）：未压实时跨 3 Run 位姿查询 0.79–1.14 s，压实后 0.25–0.40 s。"""
    import subprocess

    from awr.bench.recorder.rerun_export import _cli_env, _rerun_cli

    p = Path(path)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.opt")
    r = subprocess.run([_rerun_cli(), "rrd", "optimize", str(p), "-o", str(tmp)], capture_output=True, text=True, timeout=3600,
                       env=_cli_env())
    if r.returncode != 0 or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        raise CatalogError(f"{p.name}: rerun rrd optimize failed: {(r.stdout + r.stderr).strip()[-2000:]}")
    os.replace(tmp, p)
    st = p.stat()
    _marker(p).write_text(json.dumps({"bytes": st.st_size, "mtime_ns": st.st_mtime_ns, "rerun_version": rrd_version(p)}) + "\n",
                          encoding="utf-8")


def _marker(p: Path) -> Path:
    return p.with_name(f".{p.name}.optimized.json")


def is_optimized(path: Path) -> bool:
    """同目录标记 `.<name>.optimized.json` 记录压实后的字节数与修改时刻；文件未变时不再重复压实（评测与分发共用观测分片、
    `rebuild` 重建清单时不重写文件）。"""
    p = Path(path)
    try:
        m = json.loads(_marker(p).read_text(encoding="utf-8"))
        st = p.stat()
    except (OSError, ValueError):
        return False
    return m.get("bytes") == st.st_size and m.get("mtime_ns") == st.st_mtime_ns


def register_run(export: Path, *, catalog: str, root: Path, client=None, agent_files: Iterable[Path] = (), optimize: bool = True) -> dict:
    """注册一个 Run 的导出物到某 catalog：生成计划（按层策略排除）→ 逐文件校验 →（`optimize`）`rerun rrd optimize` → 写注册
    清单 →（给出在线客户端时）在线注册。→ `{catalog, dataset, segment, registered[], excluded[], live}`；任一文件校验失败则整体
    拒绝，不改写文件、不写清单。"""
    plan, excluded = plan_export(Path(export), catalog, agent_files=agent_files)
    if not plan:
        raise CatalogError(f"{export}: nothing to register for the {catalog} catalog (excluded {excluded})")
    root = Path(root)
    now = time.time_ns()
    checked = [check_layer_file(e, catalog) for e in plan]
    final = []
    for e in checked:
        if optimize and not is_optimized(Path(e.path)):
            optimize_rrd(Path(e.path))
        st = Path(e.path).stat()
        final.append(Entry(**{**asdict(e), "bytes": st.st_size, "mtime_ns": st.st_mtime_ns, "optimized": bool(optimize),
                              "registered_unix_ns": now}))
    reg = Registry(root, catalog)
    with reg.edit():
        for e in final:
            reg.entries[e.key] = e
    live = apply_entries(client, reg, final) if client is not None else None
    return {"catalog": catalog, "dataset": final[0].dataset, "segment": final[0].segment, "registered": [e.layer for e in final],
            "excluded": excluded, "live": live}


def register_asset(path: Path, *, dataset: str, catalog: str, root: Path, client=None) -> dict:
    """注册 dataset 级 asset（世界底图等，只含静态 `/world/**`）：路径校验（asset 层）、≤ 300 MiB、每个 dataset ≤ 12 个。"""
    p = Path(path)
    if not p.is_file():
        raise CatalogError(f"{p}: file not found")
    n = p.stat().st_size
    if n > ASSET_MAX_BYTES:
        raise CatalogError(f"{p.name}: asset {n} B exceeds {ASSET_MAX_BYTES} B (300 MiB)", {"bytes": n})
    h = check_version(rrd_version(p), RERUN_LOCKED)
    if h is not None:
        raise CatalogVersionError(f"694 RERUN_VERSION_MISMATCH: {p.name}: {h['hint']}", h)
    inv = scan_rrd(p)
    errs = lint_rrd(p, "asset")
    if inv.entities:
        errs.append(f"assets must be static only (temporal entities {sorted(inv.entities)})")
    if errs:
        raise CatalogError(f"{p.name}: refused as an asset: {errs}", {"problems": errs})
    reg = Registry(Path(root), catalog)
    with reg.edit():
        key = (dataset, _norm(p))
        if key not in reg.assets and sum(1 for k in reg.assets if k[0] == dataset) >= ASSET_MAX_PER_DATASET:
            raise CatalogError(f"dataset {dataset}: at most {ASSET_MAX_PER_DATASET} assets")
        rec = {"dataset": dataset, "path": _norm(p), "segment": inv.recording_id, "bytes": n, "registered_unix_ns": time.time_ns()}
        reg.assets[key] = rec
    if client is not None:
        client.create_dataset(dataset, exist_ok=True).register_asset(_uri(p))
    return rec


def replay(client, root: Path, catalog: str) -> dict:
    """catalog 服务启动时重放注册清单（M27-FR-080）：缺失文件与版本不一致的条目跳过并列出（含迁移提示），其余照常注册。"""
    reg = Registry.load(Path(root), catalog)
    ok, skipped = [], []
    for e in reg.entries.values():
        p = Path(e.path)
        if not p.is_file():
            skipped.append({"key": list(e.key), "path": e.path, "why": "missing"})
            continue
        h = check_version(rrd_version(p), RERUN_LOCKED)
        if h is not None:
            skipped.append({"key": list(e.key), "path": e.path, "why": "rerun_version", "code": VERSION_MISMATCH, "hint": h["hint"]})
            continue
        ok.append(e)
    live = apply_entries(client, reg, ok)
    assets = 0
    for a in reg.assets.values():
        if Path(a["path"]).is_file():
            client.create_dataset(a["dataset"], exist_ok=True).register_asset(_uri(Path(a["path"])))
            assets += 1
        else:
            skipped.append({"key": [a["dataset"], "asset"], "path": a["path"], "why": "missing"})
    return {"catalog": catalog, "entries": len(ok), "assets": assets, "skipped": skipped, "datasets": live["datasets"]}


def rebuild(data_root: Path, *, catalog: str, root: Path | None = None, client=None) -> dict:
    """由数据集目录生成注册清单（M27-FR-080）：扫描 `<data_root>/dataset/*/runs/*/derived/rrd/export.json` 逐 Run 注册；
    单个 Run 校验失败只记入报告。`root` 缺省 `<data_root>/rerun`。"""
    data_root = Path(data_root)
    root = Path(root) if root else data_root / "rerun"
    out: dict = {"catalog": catalog, "runs": 0, "errors": []}
    for ex in sorted(data_root.glob("dataset/*/runs/*/derived/rrd/export.json")):
        try:
            register_run(ex, catalog=catalog, root=root, client=client)
            out["runs"] += 1
        except (CatalogError, ValueError, KeyError) as e:
            out["errors"].append({"export": str(ex), "error": str(e), "code": getattr(e, "code", None)})
    return out


def deregister_callback(root: Path, clients: Mapping[str, object] | None = None, *,
                        catalogs: Iterable[str] = CATALOGS) -> Callable[[Path], None]:
    """`retention_gc(deregister_rrd=...)` 的回调（M27-FR-085）：对一个即将删除的 `.rrd`，先在各在线 catalog 注销对应层（或
    asset），再从注册清单删除；在线注销抛出的异常原样上抛（保留清理据此不删除文件）。`clients` 为 {catalog: CatalogClient}。"""
    clients = dict(clients or {})
    cats = tuple(catalogs)

    def _deregister(path: Path) -> None:
        p = _norm(path)
        for cat in cats:
            reg = Registry(Path(root), cat)
            with reg.edit():
                hits = reg.by_path(p)
                assets = [a for k, a in reg.assets.items() if k[1] == p]
                c = clients.get(cat)
                if c is not None:
                    names = set(c.dataset_names())
                    for e in hits:
                        if e.dataset in names:
                            c.get_dataset(e.dataset).unregister(segments_to_drop=[e.segment], layers_to_drop=[e.layer]).wait()
                    for a in assets:
                        if a["dataset"] in names and a.get("segment"):
                            c.get_dataset(a["dataset"]).unregister_asset(a["segment"])
                for e in hits:
                    reg.entries.pop(e.key, None)
                for a in assets:
                    reg.assets.pop((a["dataset"], a["path"]), None)

    return _deregister
