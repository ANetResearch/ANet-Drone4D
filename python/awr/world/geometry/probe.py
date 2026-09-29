"""sim-core 探针服务 `GeoProbeServer` 与 op 表（M04 §6.4.9、FR-025；17 §4.3.2、§9.3）。

bus 回调只把 `("geo", kind, query)` 放进 sim-core inbox；主循环步顶 drain inbox 时调用 `enqueue`（主线程），
慢任务中以每迭代 ≤ 0.5 ms 的片预算调用 `run`，可跨迭代续算。队列上限 64，满时回复 213；入队后 1 s（墙钟）未开始
执行则丢弃（api 已超时）；同一 `id` 的重试替换队列中尚未开始的旧请求。

每个 op 是生成器：点类查询每 16 点一块、`ray_hit` 与 `segment_los` 每 256 m 一块 yield 一次。
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

from .types import BAD_REQUEST, INTERNAL_ERROR, PARAM_OUT_OF_RANGE, SERVICE_UNAVAILABLE, WORLD_NOT_READY, GeoError

LIMITS = {"height_dsm": 64, "ground_dtm": 64, "agl": 64, "clearance": 64, "probe": 16, "segment_los": 16,
          "path_coarse_check": 1000, "heightmap_top": 16}
SOURCE = {"height_dsm": "dsm_2m", "ground_dtm": "dtm_10m", "agl": "dtm_10m", "clearance": "dsm_2m", "probe": "dsm_2m",
          "ray_hit": "dsm_2m", "segment_los": "dsm_2m", "path_coarse_check": "zones", "heightmap_top": "heightmap_2m",
          "terrain_profile": "heightmap_2m"}
KIND_OPS = {"height": ("height_dsm", "ground_dtm", "agl", "clearance", "probe", "segment_los", "path_coarse_check",
                       "heightmap_top", "terrain_profile"),
            "ray_hit": ("ray_hit",)}
POINT_BLOCK = 16


class ProbeQuery(Protocol):
    id: str
    op: str
    args: dict

    def reply(self, payload: dict) -> None: ...


class BusQuery:
    """把 `awr.runtime.bus.Request`（`msg()` 解包、`reply_msg()` 打包回复）适配为 `ProbeQuery`。

    M08 的同步 handler 只把 `Request` 放进 inbox；`enqueue` 收到带 `msg()` 而没有 `op` 的对象时自动包装。
    请求体 `{v, id, world_id, op, args}`（17 §9.3）；解包失败时 op 为空串，由 `run` 回复 300。
    """

    __slots__ = ("args", "id", "op", "req", "world_id")

    def __init__(self, req) -> None:
        self.req = req
        try:
            m = req.msg() or {}
        except (ValueError, TypeError):
            m = {}
        if not isinstance(m, dict):
            m = {}
        self.id = str(m.get("id", ""))
        self.op = str(m.get("op", ""))
        self.args = m.get("args") if isinstance(m.get("args"), dict) else {}
        self.world_id = m.get("world_id")

    def reply(self, payload: dict) -> None:
        fn = getattr(self.req, "reply_msg", None)
        if fn is not None:
            fn(payload)
        else:
            self.req.reply(payload)


def _nullify(v: np.ndarray) -> list:
    return [None if not np.isfinite(x) else float(x) for x in np.asarray(v, np.float64)]


def _arr(args: dict, key: str, width: int, op: str) -> np.ndarray:
    try:
        a = np.asarray(args[key], np.float64)
    except (KeyError, TypeError, ValueError) as e:
        raise GeoError(BAD_REQUEST, f"{op}: {key} missing or not numeric") from e
    if a.ndim != 2 or a.shape[1] != width:
        raise GeoError(BAD_REQUEST, f"{op}: {key} must be [[{','.join('xyz'[:width])}]]")
    if not np.all(np.isfinite(a)):
        raise GeoError(BAD_REQUEST, "BAD_VECTOR")
    lim = LIMITS.get(op)
    if lim is not None and len(a) > lim:
        raise GeoError(PARAM_OUT_OF_RANGE, f"{key}: {len(a)} > {lim}")
    return a


def _points_gen(fn: Callable[[np.ndarray], np.ndarray], P: np.ndarray) -> Generator[None, None, np.ndarray]:
    out = []
    for s in range(0, len(P), POINT_BLOCK):
        out.append(fn(P[s:s + POINT_BLOCK]))
        if s + POINT_BLOCK < len(P):
            yield None
    return np.concatenate(out) if out else np.zeros(0)


def op_generator(wq, op: str, args: dict) -> Generator[None, None, dict]:
    """返回生成器：yield 若干次后以 StopIteration.value 给出结果字典（线上格式，越界为 null）。"""
    args = args or {}
    if op in ("height_dsm", "ground_dtm"):
        P = _arr(args, "points", 2, op)
        g = wq.eff if op == "height_dsm" else None
        fn = (lambda p: g.nearest(p[:, 0], p[:, 1], "nan")) if g is not None else (lambda p: wq.dtm.bilinear(p[:, 0], p[:, 1], "nan"))
        v = yield from _points_gen(fn, P)
        return {"z_m": _nullify(v)}
    if op == "agl":
        P = _arr(args, "points", 3, op)
        v = yield from _points_gen(lambda p: p[:, 2] - wq.dtm.bilinear(p[:, 0], p[:, 1], "nan"), P)
        return {"agl_m": _nullify(v)}
    if op == "clearance":
        P = _arr(args, "points", 3, op)
        r = float(args.get("radius_m", 0.0))
        if not (0.0 <= r <= 10.0):
            raise GeoError(PARAM_OUT_OF_RANGE, "radius_m must be in [0, 10]")

        def clr(p):
            c = wq.clearance(p, r).astype(np.float64)
            inside = wq.eff.idx(p[:, 0], p[:, 1])[2]
            return np.where(inside, c, np.nan)

        v = yield from _points_gen(clr, P)
        return {"clearance_m": _nullify(v)}
    if op == "probe":
        P = _arr(args, "points", 2, op)
        items: list[dict] = []
        for s in range(0, len(P), POINT_BLOCK):
            items.extend(wq.probe(P[s:s + POINT_BLOCK]))
            yield None
        return {"items": items}
    if op == "ray_hit":
        o = np.asarray(args.get("origin_enu_m"), np.float64)
        d = np.asarray(args.get("dir"), np.float64)
        if o.shape != (3,) or d.shape != (3,) or not (np.all(np.isfinite(o)) and np.all(np.isfinite(d))):
            raise GeoError(BAD_REQUEST, "BAD_VECTOR")
        if abs(float(np.linalg.norm(d)) - 1.0) > 1e-3:
            raise GeoError(BAD_REQUEST, "BAD_VECTOR")
        hit = yield from wq.ray_hit_iter(o, d, float(args.get("max_range_m", 5000.0)))
        return hit.to_json()
    if op == "segment_los":
        try:
            pairs = np.asarray(args["pairs"], np.float64)
        except (KeyError, TypeError, ValueError) as e:
            raise GeoError(BAD_REQUEST, "segment_los: pairs must be [[[x,y,z],[x,y,z]]]") from e
        if pairs.ndim != 3 or pairs.shape[1:] != (2, 3) or not np.all(np.isfinite(pairs)):
            raise GeoError(BAD_REQUEST, "segment_los: pairs must be [[[x,y,z],[x,y,z]]]")
        if len(pairs) > LIMITS["segment_los"]:
            raise GeoError(PARAM_OUT_OF_RANGE, f"pairs: {len(pairs)} > 16")
        eps = float(args.get("eps_m", 0.5))
        vis, blk = [], []
        for a, b in pairs:
            ok, pt = yield from wq.segment_los_iter(a, b, eps)
            vis.append(bool(ok))
            blk.append(None if pt is None else [float(x) for x in pt])
        return {"visible": vis, "first_block_enu_m": blk}
    if op == "path_coarse_check":
        P = _arr(args, "polyline", 3, op)
        yield None
        res = wq.path_coarse_check(P, buffer_m=float(args.get("buffer_m", 1.0)),
                                   active_zone_ids=args.get("active_zone_ids"))
        return res.to_json()
    if op == "heightmap_top":
        try:
            segs = np.asarray(args["segments"], np.float64)
        except (KeyError, TypeError, ValueError) as e:
            raise GeoError(BAD_REQUEST, "heightmap_top: segments must be [[[x,y,z],[x,y,z]]]") from e
        if segs.ndim != 3 or segs.shape[1] != 2 or segs.shape[2] < 2 or not np.all(np.isfinite(segs)):
            raise GeoError(BAD_REQUEST, "heightmap_top: segments must be [[[x,y,z],[x,y,z]]]")
        if len(segs) > LIMITS["heightmap_top"]:
            raise GeoError(PARAM_OUT_OF_RANGE, f"segments: {len(segs)} > 16")
        yield None
        top = wq.heightmap_top_along(segs[:, 0], segs[:, 1], tol_m=float(args.get("tol_m", 100.0)))
        return {"top_m": [float(v) for v in np.atleast_1d(top)]}
    if op == "terrain_profile":
        P = _arr(args, "polyline", 3, "terrain_profile")
        yield None
        prof = wq.terrain_profile(P, float(args.get("ds_m", 4.0)))
        return {k: [float(x) for x in np.asarray(v, np.float64)] for k, v in prof.items()}
    raise GeoError(BAD_REQUEST, "UNKNOWN_OP")


@dataclass
class _Job:
    kind: str
    query: Any
    t_wall_ns: int
    gen: Generator | None = None
    t_proc_ns: int = 0


@dataclass
class ProbeStats:
    done: int = 0
    rejected: int = 0
    expired: int = 0
    slice_us: deque = field(default_factory=lambda: deque(maxlen=512))

    def p99_slice_us(self) -> float:
        return float(np.percentile(list(self.slice_us), 99)) if self.slice_us else 0.0


class GeoProbeServer:
    def __init__(self, wq, *, queue_max: int = 64, deadline_ms: int = 1000, clock_ns: Callable[[], int] = time.monotonic_ns):
        self.wq = wq
        self.queue_max = queue_max
        self.deadline_ns = int(deadline_ms) * 1_000_000
        self.clock_ns = clock_ns
        self.q: deque[_Job] = deque()
        self.pending_by_id: dict[str, _Job] = {}
        self.stats = ProbeStats()
        self.closed = False

    def _reply(self, job: _Job, ok: bool, code: int, result: dict | None, detail: str = "") -> None:
        q = job.query
        op = getattr(q, "op", "")
        payload = {"v": 1, "id": getattr(q, "id", ""), "ok": ok, "code": int(code), "result": result,
                   "content_version": self.wq.content_version, "derive_sha8": self.wq.derive_sha8,
                   "source": SOURCE.get(op, "dsm_2m"), "t_proc_us": int(job.t_proc_ns // 1000)}
        if detail:
            payload["detail"] = detail
        self.pending_by_id.pop(getattr(q, "id", ""), None)
        q.reply(payload)

    def enqueue(self, kind: str, query) -> None:
        """主线程：步顶 drain inbox 时调用。`query` 为 `ProbeQuery`，或 `awr.runtime.bus.Request`（自动包装为 BusQuery）。"""
        if not hasattr(query, "op") and hasattr(query, "msg"):
            query = BusQuery(query)
        qid = getattr(query, "id", "")
        if self.closed:
            self._reply(_Job(kind, query, 0), False, WORLD_NOT_READY, None, "GEO_NOT_READY")
            return
        wid = getattr(query, "world_id", None)
        if wid and wid != getattr(self.wq, "world_id", wid):
            self._reply(_Job(kind, query, 0), False, WORLD_NOT_READY, None, "WORLD_NOT_IN_SESSION")
            return
        old = self.pending_by_id.get(qid)
        if old is not None and old.gen is None:
            old.query = query                          # 同 id 重试：替换尚未开始的旧请求，保留队列位置
            old.kind = kind
            return
        if len(self.q) >= self.queue_max:
            self.stats.rejected += 1
            self._reply(_Job(kind, query, 0), False, SERVICE_UNAVAILABLE, None, "GEO_QUEUE_FULL")
            return
        job = _Job(kind, query, self.clock_ns())
        self.q.append(job)
        self.pending_by_id[qid] = job

    def run(self, budget_us: int = 500) -> int:
        """慢任务中调用；返回本次完成数。"""
        t_start = self.clock_ns()
        t_end = t_start + int(budget_us) * 1000
        done = 0
        while self.q and self.clock_ns() < t_end:
            job = self.q[0]
            if job.gen is None:
                if self.clock_ns() - job.t_wall_ns > self.deadline_ns:
                    self.q.popleft()
                    self.pending_by_id.pop(getattr(job.query, "id", ""), None)
                    self.stats.expired += 1
                    continue
                op = getattr(job.query, "op", "")
                if op not in KIND_OPS.get(job.kind, ()):
                    self.q.popleft()
                    self._reply(job, False, BAD_REQUEST, None, "UNKNOWN_OP")
                    done += 1
                    continue
                job.gen = op_generator(self.wq, op, getattr(job.query, "args", {}) or {})
            t0 = self.clock_ns()
            try:
                while self.clock_ns() < t_end:
                    next(job.gen)
                job.t_proc_ns += self.clock_ns() - t0
            except StopIteration as fin:
                job.t_proc_ns += self.clock_ns() - t0
                self.q.popleft()
                self._reply(job, True, 0, fin.value)
                self.stats.done += 1
                done += 1
            except GeoError as e:
                job.t_proc_ns += self.clock_ns() - t0
                self.q.popleft()
                self._reply(job, False, e.code, None, e.detail)
                done += 1
            except Exception as e:
                self.q.popleft()
                self._reply(job, False, INTERNAL_ERROR, None, type(e).__name__)
                done += 1
            self.stats.slice_us.append((self.clock_ns() - t0) / 1000)
        return done

    def close(self) -> None:
        """会话切换或 WorldQuery 关闭：在途请求一律回复 123。"""
        self.closed = True
        while self.q:
            job = self.q.popleft()
            self._reply(job, False, WORLD_NOT_READY, None, "GEO_NOT_READY")

    def metrics(self) -> dict:
        return {"queue_len": len(self.q), "probe_done": self.stats.done, "rejected": self.stats.rejected,
                "expired": self.stats.expired, "slice_us_p99": round(self.stats.p99_slice_us(), 1)}
