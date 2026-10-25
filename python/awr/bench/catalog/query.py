"""跨 Run 查询封装 `D3Catalog`（M27-FR-081、083、084；AWR-05 §6.1.4–6.1.6；r8 §10.7；D3-AC-20、D3-AC-16、D3-AC-25）。

    cat = D3Catalog("rerun+http://catalog-eval:51234", token=..., target="eval")
    cat.runs("campus-v2.handoff-01", policy="sdk*")            # segment 表（pyarrow.Table）：segment、layers、时间范围与 d3 段属性
    cat.poses(["run-0001", "run-0002", "run-0003"], hz=10)     # (segment, sim_time, uav, x, y, z, qx, qy, qz, qw)，latest-at 对齐
    cat.query("campus-v2.handoff-01", "/truth/**")             # 任意实体过滤（分发 catalog 上 /truth/** 为 0 行）
    cat.frames("run-0001", "uav-01", "cam0", (35.0, 40.0))     # EncodedImage / VideoStream 样本（按需取 Chunk）
    cat.visibility("person_a", (35.0, 39.0))                   # 各 Run、各机的可见帧数（只在评测 catalog）
    cat.viewer_url("run-0001", (35.0, 40.0))                   # catalog 深链接 + Viewer 配置；rerun_version 不一致 → 694 + 迁移提示

约定：

- 时间参数以秒给出（仿真时间，事件起点起算），返回表中的 `sim_time` 为 int64 纳秒；表转换不依赖 pandas；
- `poses` 的采样网格按绝对仿真时间对齐（`k · period`，period = round(1e9 / hz)），覆盖每个 segment、每机位姿样本的时间范围，
  latest-at 取不晚于网格点的最后一个样本（不跨 segment）；实体只取 `/fleet/<uav>`（`source="nav"`，Agent 收到的带噪导航解）或
  `/truth/fleet/<uav>`（`source="truth"`，只在评测 catalog 有数据）精确路径，不读相机与 LiDAR 块。服务端只按 segment 与实体下推
  过滤并返回原始行，重采样在客户端以 `searchsorted` 完成：OSS catalog 的 `using_index_values` 重采样每个网格点约 70 µs
  （3 Run × 3 机 × 120 s 在 50 Hz 网格上 1.3 s），读取原始行与客户端重采样与网格频率无关；
- `visibility` 只在评测 catalog 提供（分发 catalog 抛 115 ROLE_FORBIDDEN）；
- `viewer_url` 比较 segment 属性 `rerun_version` 与运行时 rerun-sdk、锁定版本：不同 minor 时 `strict` 抛 694（`detail` 含迁移提示与
  链接），否则在返回值中带 `migrate_hint`；Viewer 配置 `memory_limit` ≤ 1.5 GB、`hide_welcome_screen`、不检查更新（AWR-05 §6.1.6）；
  经 rerun-proxy 访问时以 `proxy_base` 替换服务地址，链接中不含令牌（令牌经路径限定 Cookie `d3_rr`，§6.1.4、17 §3.2）。
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Literal

import numpy as np

from awr.bench.catalog.registry import CatalogError, CatalogVersionError
from awr.bench.recorder.rerun_export import RERUN_LOCKED, check_version, path_part, sdk_version

if TYPE_CHECKING:
    import pyarrow

__all__ = ["VIEWER_MEMORY_LIMIT", "VIEWER_MEMORY_LIMIT_MAX", "D3Catalog", "Frame", "ViewerLink", "parse_bytes", "viewer_options"]

ROLE_FORBIDDEN = 115
VIEWER_MEMORY_LIMIT = "1.5GB"                       # Web Viewer 配置（AWR-05 §6.1.6、UX-NFR-042：memory_limit ≤ 1.5 GB）
VIEWER_MEMORY_LIMIT_MAX = 1_500_000_000
_FLEET = re.compile(r"^/fleet/([a-z0-9_-]+)$")
_TRUTH_FLEET = re.compile(r"^/truth/fleet/([a-z0-9_-]+)$")
_UNITS = {"": 1, "b": 1, "kb": 10 ** 3, "mb": 10 ** 6, "gb": 10 ** 9, "kib": 2 ** 10, "mib": 2 ** 20, "gib": 2 ** 30}
_PROP = "property:d3:"
_HOST = re.compile(r"^rerun(\+https?)?://[^/]+")


def parse_bytes(s: str) -> int:
    """`1.5GB`、`512MiB`、`1500000000` → 字节数（GB 为 10^9，GiB 为 2^30，与 Rerun 的内存上限写法一致）。"""
    m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]*)\s*$", str(s))
    if not m or m.group(2).lower() not in _UNITS:
        raise ValueError(f"not a byte size: {s!r}")
    return int(float(m.group(1)) * _UNITS[m.group(2).lower()])


def viewer_options(memory_limit: str = VIEWER_MEMORY_LIMIT) -> dict:
    """Web Viewer 启动配置（宿主页面传给 `@rerun-io/web-viewer-react`）；`memory_limit` 超过 1.5 GB 时拒绝。"""
    if parse_bytes(memory_limit) > VIEWER_MEMORY_LIMIT_MAX:
        raise ValueError(f"memory_limit {memory_limit} exceeds 1.5 GB (AWR-05 §6.1.6)")
    return {"memory_limit": memory_limit, "hide_welcome_screen": True, "check_for_updates_on_startup": False}


@dataclass(frozen=True)
class Frame:
    """一个图像样本：`kind` 为 `encoded`（EncodedImage，`media_type` 如 image/jpeg）或 `video`（VideoStream 样本，`codec` 如 h264）。"""

    segment: str
    t_ns: int
    kind: str
    data: bytes
    media_type: str | None = None


@dataclass(frozen=True)
class ViewerLink:
    url: str
    dataset: str
    segment: str
    catalog: str
    rerun_version: str | None
    viewer: dict = field(default_factory=viewer_options)
    migrate_hint: dict | None = None


def _ns(t_s: float) -> int:
    return round(float(t_s) * 1e9)


def _int64(arr):
    """duration / timestamp 列 → int64 纳秒（pyarrow），不经 pandas。"""
    import pyarrow as pa

    a = arr.combine_chunks() if hasattr(arr, "combine_chunks") else arr
    if pa.types.is_duration(a.type) or pa.types.is_timestamp(a.type):
        a = a.cast(pa.int64())
    return a


def _first(arr):
    """list<T> 列 → 每行第一个元素（空列表或 null → null）。"""
    import pyarrow as pa
    import pyarrow.compute as pc

    a = arr.combine_chunks() if hasattr(arr, "combine_chunks") else arr
    if not len(a):
        return a
    empty = pc.fill_null(pc.equal(pc.list_value_length(a), 0), False)
    if pc.any(empty).as_py():
        a = pc.if_else(empty, pa.scalar(None, a.type), a)
    return pc.list_element(a, 0)


class D3Catalog:
    """评测（`eval`）或分发（`dist`）catalog 的查询客户端；`client` 可直接传入已连接的 `rerun.catalog.CatalogClient`。"""

    def __init__(self, url: str | None = None, token: str | None = None, target: Literal["eval", "dist"] = "eval", *, client=None) -> None:
        if target not in ("eval", "dist"):
            raise ValueError(f"target {target!r} not in ('eval', 'dist')")
        if client is None:
            if url is None:
                raise ValueError("url or client is required")
            from rerun.catalog import CatalogClient

            client = CatalogClient(url, token=token)
        self.client = client
        self.target = target
        self._seg_ds: dict[str, str] = {}

    # ------------------------------------------------------------------ dataset 与 segment

    def datasets(self) -> list[str]:
        return sorted(self.client.dataset_names())

    def _dataset(self, name: str):
        return self.client.get_dataset(name)

    def event_of(self, runs: Sequence[str]) -> str:
        """runs 所在的 dataset（全部 runs 须属于同一 dataset）。"""
        want = list(dict.fromkeys(runs))
        miss = [r for r in want if r not in self._seg_ds]
        if miss:
            for ds in self.datasets():
                for s in self._dataset(ds).segment_ids():
                    self._seg_ds[s] = ds
        names = {self._seg_ds.get(r) for r in want}
        if None in names:
            raise CatalogError(f"runs {[r for r in want if r not in self._seg_ds]} are not registered in the {self.target} catalog")
        if len(names) != 1:
            raise CatalogError(f"runs span several datasets {sorted(names)}; query one event at a time")
        return names.pop()

    def runs(self, event: str, **props) -> pyarrow.Table:
        """segment 表：`segment`、`layers`、`size_bytes`、`t_start_ns`、`t_end_ns` 与 d3 段属性列（`policy`、`n_uav`……）；
        `props` 为筛选条件（字符串值按 fnmatch 通配匹配，其余值相等匹配）。"""
        import pyarrow as pa

        st = self._dataset(event).segment_table().to_arrow_table()
        cols: dict[str, object] = {"segment": st.column("rerun_segment_id"), "layers": st.column("rerun_layer_names"),
                                   "size_bytes": st.column("rerun_size_bytes")}
        for src, dst in (("sim_time:start", "t_start_ns"), ("sim_time:end", "t_end_ns")):
            if src in st.column_names:
                cols[dst] = _int64(st.column(src))
        for name in st.column_names:
            if name.startswith(_PROP) and name[len(_PROP):] not in ("layer", "uav"):        # 层级属性在 segment 上无意义
                cols[name[len(_PROP):]] = _first(st.column(name))
        t = pa.table(cols)
        if props:
            keep = []
            for i in range(t.num_rows):
                ok = True
                for k, want in props.items():
                    if k not in t.column_names:
                        ok = False
                        break
                    v = t.column(k)[i].as_py()
                    ok = fnmatch.fnmatchcase(str(v), want) if isinstance(want, str) else v == want
                    if not ok:
                        break
                keep.append(ok)
            t = t.filter(pa.array(keep, pa.bool_()))
        return t.sort_by("segment")

    def _ranges(self, event: str, runs: Sequence[str]) -> dict[str, tuple[int, int]]:
        st = self.runs(event)
        seg = st.column("segment").to_pylist()
        if "t_start_ns" not in st.column_names:
            return {}
        t0, t1 = st.column("t_start_ns").to_pylist(), st.column("t_end_ns").to_pylist()
        r = {s: (a, b) for s, a, b in zip(seg, t0, t1, strict=True) if a is not None and b is not None}
        return {s: r[s] for s in runs if s in r}

    def _entities(self, event: str, pattern: re.Pattern, component: str) -> list[tuple[str, str]]:
        """dataset schema 中匹配 `pattern` 且带 `component` 列的实体 → [(实体, 第一个捕获组)]。"""
        out = set()
        for c in self._dataset(event).schema().component_columns():
            m = pattern.match(c.entity_path)
            if m and c.component == component:
                out.add((c.entity_path, m.group(1)))
        return sorted(out)

    # ------------------------------------------------------------------ 查询

    def query(self, event: str, contents: str | Sequence[str], *, runs: Sequence[str] | None = None, index: str | None = "sim_time",
              window: tuple[float, float] | None = None) -> pyarrow.Table:
        """按实体过滤（精确路径或末尾 `/**`，AWR-05 §6.1.3）读取 → pyarrow.Table（`sim_time` 为 int64 纳秒）。"""
        import pyarrow as pa
        from datafusion import col, lit

        from awr.bench.recorder.rerun_export import check_filter

        exprs = [contents] if isinstance(contents, str) else list(contents)
        for e in exprs:
            check_filter(e)
        view = self._dataset(event)
        if runs is not None:
            view = view.filter_segments(list(runs))
        df = view.filter_contents(exprs).reader(index=index)
        if window is not None and index == "sim_time":
            lo, hi = (lit(pa.scalar(_ns(x), pa.duration("ns"))) for x in window)
            df = df.filter((col("sim_time") >= lo) & (col("sim_time") <= hi))
        tb = df.to_arrow_table()
        if "sim_time" in tb.column_names:
            tb = tb.set_column(tb.column_names.index("sim_time"), "sim_time", _int64(tb.column("sim_time")))
        return tb

    def poses(self, runs: Sequence[str] | pyarrow.Table, hz: float = 10.0, *, event: str | None = None,
              source: Literal["nav", "truth"] = "nav", uavs: Sequence[str] | None = None) -> pyarrow.Table:
        """跨 Run 位姿（latest-at 对齐采样）→ (segment, sim_time, uav, x, y, z, qx, qy, qz, qw)，按 segment、sim_time、uav 排序。
        `runs` 可为 run_id 列表或 `runs()` 返回的表；`source="truth"` 只在评测 catalog 有数据（分发 catalog 返回 0 行）。"""
        import pyarrow as pa

        if hasattr(runs, "column"):
            runs = runs.column("segment").to_pylist()
        runs = list(dict.fromkeys(runs))
        if hz <= 0:
            raise ValueError("hz must be positive")
        schema = pa.schema([("segment", pa.string()), ("sim_time", pa.int64()), ("uav", pa.string()),
                            *[(k, pa.float64()) for k in ("x", "y", "z", "qx", "qy", "qz", "qw")]])
        if not runs:
            return schema.empty_table()
        event = event or self.event_of(runs)
        pat = _FLEET if source == "nav" else _TRUTH_FLEET
        ents = [(e, u) for e, u in self._entities(event, pat, "Transform3D:translation") if uavs is None or u in uavs]
        if not ents:
            return schema.empty_table()
        period = max(1, round(1e9 / hz))
        view = self._dataset(event).filter_segments(runs).filter_contents([e for e, _ in ents])
        tb = view.reader(index="sim_time").to_arrow_table()                  # 原始行（不在服务端重采样，见模块说明）
        if not tb.num_rows:
            return schema.empty_table()
        seg_all = np.asarray(tb.column("rerun_segment_id").to_pylist(), dtype=object)
        t_all = _int64(tb.column("sim_time")).to_numpy(zero_copy_only=False)
        cols: dict[str, list] = {k: [] for k in schema.names}
        for e, u in ents:
            tr_name, q_name = f"{e}:Transform3D:translation", f"{e}:Transform3D:quaternion"
            if tr_name not in tb.column_names:
                continue
            tr = _first(tb.column(tr_name))
            ok = tr.is_valid().to_numpy(zero_copy_only=False)
            if not ok.any():
                continue
            xyz = tr.filter(pa.array(ok)).flatten().to_numpy(zero_copy_only=False).astype(np.float64).reshape(-1, 3)
            quat = np.tile([0.0, 0.0, 0.0, 1.0], (len(xyz), 1))
            if q_name in tb.column_names:
                q = _first(tb.column(q_name)).filter(pa.array(ok))
                qv = q.is_valid().to_numpy(zero_copy_only=False)
                if qv.any():
                    quat[qv] = q.filter(q.is_valid()).flatten().to_numpy(zero_copy_only=False).astype(np.float64).reshape(-1, 4)
            seg, t = seg_all[ok], t_all[ok]
            for sid in sorted(set(seg.tolist())):
                m = seg == sid
                ts, order = t[m], np.argsort(t[m], kind="stable")
                ts, pv, qq = ts[order], xyz[m][order], quat[m][order]
                g = np.arange(-(-int(ts[0]) // period), int(ts[-1]) // period + 1, dtype=np.int64) * period
                idx = np.searchsorted(ts, g, side="right") - 1                 # latest-at：取 t ≤ 网格点的最后一个样本
                keep = idx >= 0
                g, idx = g[keep], idx[keep]
                cols["segment"] += [sid] * len(g)
                cols["sim_time"].append(g)
                cols["uav"] += [u] * len(g)
                for k, (arr, j) in {"x": (pv, 0), "y": (pv, 1), "z": (pv, 2), "qx": (qq, 0), "qy": (qq, 1), "qz": (qq, 2), "qw": (qq, 3)}.items():
                    cols[k].append(arr[idx, j])
        if not cols["segment"]:
            return schema.empty_table()
        out = pa.table({k: (pa.array(v, pa.string()) if k in ("segment", "uav") else pa.array(np.concatenate(v), schema.field(k).type))
                        for k, v in cols.items()}, schema=schema)
        return out.sort_by([("segment", "ascending"), ("sim_time", "ascending"), ("uav", "ascending")])

    def frames(self, run: str, uav: str, cam: str, t: tuple[float, float], *, event: str | None = None) -> Iterator[Frame]:
        """`/fleet/<uav>/<cam>/image/rgb` 在时间窗 `t`（秒）内的样本：EncodedImage（review 档 JPEG）或 VideoStream（dataset 档
        H.264）；只取该实体的 Chunk。"""
        event = event or self.event_of([run])
        ent = f"/fleet/{path_part(uav)}/{path_part(cam)}/image/rgb"
        tb = self.query(event, ent, runs=[run], window=t)
        ts = tb.column("sim_time").to_pylist() if tb.num_rows else []
        blob, media = f"{ent}:EncodedImage:blob", f"{ent}:EncodedImage:media_type"
        sample = f"{ent}:VideoStream:sample"
        if blob in tb.column_names:
            data, mt = _first(tb.column(blob)).to_pylist(), _first(tb.column(media)).to_pylist() if media in tb.column_names else None
            for i, d in enumerate(data):
                if d is not None:
                    yield Frame(run, int(ts[i]), "encoded", bytes(d), mt[i] if mt else None)
        elif sample in tb.column_names:
            for i, d in enumerate(_first(tb.column(sample)).to_pylist()):
                if d is not None:
                    yield Frame(run, int(ts[i]), "video", bytes(d), "video/h264")

    def visibility(self, actor: str, window: tuple[float, float], *, event: str | None = None, runs: Sequence[str] | None = None,
                   min_value: float = 0.0) -> pyarrow.Table:
        """各 Run、各机对演员 `actor` 的可见度（`/eval/visibility/<uav>/<actor>`，只在评测 catalog）→ (segment, uav, n_samples,
        n_visible)；`n_visible` 为第一个分量（可见像素数或无遮挡高度）> `min_value` 的样本数。"""
        import pyarrow as pa

        if self.target != "eval":
            raise CatalogError("115 ROLE_FORBIDDEN: visibility is served by the eval catalog only", code=ROLE_FORBIDDEN)
        a = path_part(actor)
        events = [event] if event else ([self.event_of(runs)] if runs else self.datasets())
        rows: list[tuple[str, str, int, int]] = []
        for ev in events:
            pat = re.compile(rf"^/eval/visibility/([a-z0-9_-]+)/{re.escape(a)}$")
            ents = self._entities(ev, pat, "Scalars:scalars")
            if not ents:
                continue
            tb = self.query(ev, [e for e, _ in ents], runs=runs, window=window)
            if not tb.num_rows:
                continue
            seg = tb.column("rerun_segment_id").to_pylist()
            for e, u in ents:
                name = f"{e}:Scalars:scalars"
                if name not in tb.column_names:
                    continue
                vals = _first(tb.column(name)).to_pylist()
                acc: dict[str, list[int]] = {}
                for s, v in zip(seg, vals, strict=True):
                    if v is None:
                        continue
                    c = acc.setdefault(s, [0, 0])
                    c[0] += 1
                    c[1] += int(float(v) > min_value)
                rows += [(s, u, n, k) for s, (n, k) in acc.items()]
        rows.sort()
        return pa.table({"segment": pa.array([r[0] for r in rows], pa.string()), "uav": pa.array([r[1] for r in rows], pa.string()),
                         "n_samples": pa.array([r[2] for r in rows], pa.int64()), "n_visible": pa.array([r[3] for r in rows], pa.int64())})

    # ------------------------------------------------------------------ Viewer 深链接

    def segment_version(self, run: str, *, event: str | None = None) -> tuple[str | None, dict | None]:
        """segment 属性 `rerun_version` 与运行时 rerun-sdk、锁定版本比较 → (版本, 迁移提示或 None)。"""
        event = event or self.event_of([run])
        st = self.runs(event)
        found = None
        if "rerun_version" in st.column_names:
            for s, v in zip(st.column("segment").to_pylist(), st.column("rerun_version").to_pylist(), strict=True):
                if s == run:
                    found = v
        if found is None:
            return None, {"code": 694, "found": None, "expected": RERUN_LOCKED,
                          "hint": "segment 缺少 rerun_version 属性：以当前导出器由 MCAP 重新派生后重新注册"}
        return found, check_version(found, sdk_version()) or check_version(found, RERUN_LOCKED)

    def viewer_url(self, run: str, t: tuple[float, float] | None = None, *, event: str | None = None, proxy_base: str | None = None,
                   strict: bool = True, memory_limit: str = VIEWER_MEMORY_LIMIT) -> ViewerLink:
        """catalog 深链接（`.../dataset/<id>?segment_id=<run>#when=sim_time@…&time_selection=…`）与 Viewer 配置。"""
        event = event or self.event_of([run])
        ds = self._dataset(event)
        if run not in ds.segment_ids():
            raise CatalogError(f"run {run!r} is not registered in {event} ({self.target} catalog)")
        opts = viewer_options(memory_limit)
        if t is not None:
            url = ds.segment_url(run, "sim_time", timedelta(seconds=float(t[0])), timedelta(seconds=float(t[1])))
        else:
            url = ds.segment_url(run)
        if proxy_base:
            url = _HOST.sub(proxy_base.rstrip("/"), url, count=1)
        found, hint = self.segment_version(run, event=event)
        if hint is not None and strict:
            raise CatalogVersionError(f"694 RERUN_VERSION_MISMATCH: run {run} was derived with rerun {found}: {hint['hint']}",
                                      {**hint, "run": run, "url": url})
        return ViewerLink(url, event, run, self.target, found, opts, hint)
