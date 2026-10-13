"""M20 的 checkpoint 扩展段 `x.tasking`（M20-FR-075、M20-AC-032；AWR-04 §15.7.4 驻留恢复；D2-AC-45）。

M08 在 checkpoint 捕获时（持 sim-core 状态锁、tick 边界）调用 `encode(rt)`，恢复时在机群、识别物实体表、roster、租约与调用表都已
恢复之后调用 `decode_into(rt, blob)`（`register_checkpoint_extension("tasking", ...)`，见 `plugin.install`）。

段内容：机巢（电池、充电通道、在巢机体、起降位时间线与队列）、任务（规格、状态、工作项、几何、访问栅格、接力控制器与统计）、
架次（状态、动作队列、在途调用号）、机巢外落地的机体、编号序列与调用号映射。不保存的：规划作业的在途结果（恢复后按状态重新提交：
PLANNING 的任务重新分解、接力的站位作业清除挂起标记、分配重新登记）、可行窗口与禁入圆缓存、预览（墙钟 TTL）。

编码：msgpack，结构化标记（不使用 pickle：驻留目录的文件不应能在恢复时执行代码）。元组、集合、非字符串键的字典、numpy 数组与
允许清单内的运行时类各有标记，解码时按清单重建；遇到清单外的对象即抛 TypeError（捕获失败时 M08 省略该段并记日志）。
"""

from __future__ import annotations

import contextlib
import dataclasses
import itertools
import math
from typing import Any

import msgpack
import numpy as np

from awr.tasking.model import WorkItem
from awr.tasking.nestmodel import PadTimeline

from .book import ItemRT, SortieRT, TaskRT
from .nests import AirRT, ChannelRT, NestRT, PackRT
from .relay import RelayRT
from .visits import VisitGrid

__all__ = ["SCHEMA", "decode_into", "encode"]

SCHEMA = "awr.tasking.ckpt.v1"
_CLASSES: dict[str, type] = {c.__name__: c for c in (ItemRT, TaskRT, SortieRT, RelayRT, NestRT, PackRT, ChannelRT, AirRT, PadTimeline,
                                                     WorkItem, VisitGrid)}
_T, _S, _D, _A, _C = "~t", "~s", "~d", "~a", "~c"


_PRIM = (type(None), bool, int, float, str, bytes)


def _enc(o: Any) -> Any:
    t = type(o)
    if t in _PRIM:
        return o
    if t is list:
        if all(type(x) in _PRIM for x in o):     # 数值与字符串列表：msgpack 原样打包（访问栅格的间隔样本等）
            return o
        return [_enc(x) for x in o]
    if t is dict:
        if all(type(k) is str and not k.startswith("~") for k in o):
            return {k: (v if type(v) in _PRIM else _enc(v)) for k, v in o.items()}
        return {_D: [[_enc(k), _enc(v)] for k, v in o.items()]}
    if t is tuple:
        return {_T: [x if type(x) in _PRIM else _enc(x) for x in o]}
    if isinstance(o, np.ndarray):
        a = np.ascontiguousarray(o)
        return {_A: [a.dtype.str, list(a.shape), a.tobytes()]}
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, (bool, int, float, str, bytes)):
        return o
    if isinstance(o, list):
        return [_enc(x) for x in o]
    if isinstance(o, tuple):
        return {_T: [_enc(x) for x in o]}
    if isinstance(o, dict):
        return _enc(dict(o))
    if isinstance(o, (set, frozenset)):
        return {_S: [_enc(x) for x in sorted(o, key=str)]}
    name = t.__name__
    if _CLASSES.get(name) is t:
        if t is TaskRT:
            return _enc_task(o)
        return {_C: name, "f": {k: (v if type(v) in _PRIM else _enc(v)) for k, v in vars(o).items()}}
    raise TypeError(f"tasking checkpoint: unsupported object {name}")


_STATIC_TASK_FIELDS = ("spec", "geometry", "preview", "scan_detail")
_task_cache: dict[int, tuple[int, int, int, int, int, dict]] = {}


def _enc_task(T: TaskRT) -> dict:
    """任务：规格、几何、预览与扫描明细在分解后基本不变，按（对象、revision）缓存其编码（每代 checkpoint 只编码可变部分）。"""
    key = (T.revision, id(T.spec), id(T.geometry), id(T.preview), id(T.scan_detail))
    hit = _task_cache.get(id(T))
    if hit is not None and hit[:5] == key:
        static = hit[5]
    else:
        static = {k: _enc(getattr(T, k)) for k in _STATIC_TASK_FIELDS}
        if len(_task_cache) > 64:
            _task_cache.clear()
        _task_cache[id(T)] = (*key, static)
    f = dict(static)
    for k, v in vars(T).items():
        if k not in f:
            f[k] = v if type(v) in _PRIM else _enc(v)
    return {_C: "TaskRT", "f": f}


def _dec(o: Any) -> Any:
    if isinstance(o, list):
        return [_dec(x) for x in o]
    if not isinstance(o, dict):
        return o
    if len(o) == 1:
        k = next(iter(o))
        if k == _T:
            return tuple(_dec(x) for x in o[_T])
        if k == _S:
            return {_dec(x) for x in o[_S]}
        if k == _D:
            return {_dec(kk): _dec(vv) for kk, vv in o[_D]}
        if k == _A:
            dt, shape, raw = o[_A]
            return np.frombuffer(bytes(raw), dtype=np.dtype(dt)).reshape(tuple(int(x) for x in shape)).copy()
    if _C in o and set(o) == {_C, "f"}:
        cls = _CLASSES.get(str(o[_C]))
        if cls is None:
            raise TypeError(f"tasking checkpoint: unknown class {o[_C]!r}")
        obj = object.__new__(cls)
        for k, v in o["f"].items():
            setattr(obj, str(k), _dec(v))
        if dataclasses.is_dataclass(cls):
            # 旧版本 checkpoint 中没有的字段取类的缺省值（新增字段不使恢复失败）
            for fd in dataclasses.fields(cls):
                if not hasattr(obj, fd.name):
                    if fd.default is not dataclasses.MISSING:
                        setattr(obj, fd.name, fd.default)
                    elif fd.default_factory is not dataclasses.MISSING:
                        setattr(obj, fd.name, fd.default_factory())
        return obj
    return {k: _dec(v) for k, v in o.items()}


def _peek(rt: Any, attr: str) -> int:
    """编号序列（itertools.count）的下一个值；读取后以同一起点重建，序列不跳号。"""
    n = next(getattr(rt, attr))
    setattr(rt, attr, itertools.count(n))
    return int(n)


def encode(rt: Any) -> bytes:
    """当前编排运行时状态 → 字节串（tick 边界；不改动运行时可见的状态）。"""
    st = {
        "schema": SCHEMA, "t_ns": int(rt.t_ns),
        "nests": rt.nests, "tasks": rt.tasks, "sorties": rt.sorties, "lost": set(rt.lost), "revision": int(rt.revision),
        "ext_plans": rt.ext_plans, "results": list(rt.results),
        "seq": {"tid": _peek(rt, "_tid_seq"), "nid": _peek(rt, "_nid_seq"), "pv": _peek(rt, "_pv_seq"), "sortie": _peek(rt, "_sortie_seq")},
        "by_cid": rt.ex.by_cid, "ex_stats": rt.ex.stats, "pending_first": rt.relays.pending_first,
        "guard_inside": rt.guard.inside_cfg, "l95_s": float(rt.jobs.l95_s),
    }
    return msgpack.packb(_enc(st), use_bin_type=True)


def decode_into(rt: Any, blob: bytes) -> None:
    """恢复（`encode` 的逆操作）：替换机巢、任务、架次与编号序列；在途规划作业的结果已丢失，按状态重新提交或清除挂起标记。"""
    st = _dec(msgpack.unpackb(blob, raw=False, strict_map_key=False))
    if not isinstance(st, dict) or st.get("schema") != SCHEMA:
        raise ValueError("tasking checkpoint: schema mismatch")
    rt.nests = dict(st["nests"])
    rt.tasks = dict(st["tasks"])
    rt.sorties = dict(st["sorties"])
    rt.lost = set(st.get("lost") or ())
    rt.revision = int(st.get("revision", 0))
    rt.ext_plans = dict(st.get("ext_plans") or {})
    rt.results = [tuple(x) for x in st.get("results") or []]
    seq = st.get("seq") or {}
    rt._tid_seq = itertools.count(int(seq.get("tid", 1)))
    rt._nid_seq = itertools.count(int(seq.get("nid", 1)))
    rt._pv_seq = itertools.count(int(seq.get("pv", 1)))
    rt._sortie_seq = itertools.count(int(seq.get("sortie", 1)))
    rt.ex.by_cid = dict(st.get("by_cid") or {})
    rt.ex.stats.update(st.get("ex_stats") or {})
    rt.relays.reset()
    rt.relays.pending_first.update(st.get("pending_first") or {})
    rt.guard.inside_cfg = dict(st.get("guard_inside") or {})
    with contextlib.suppress(Exception):
        rt.jobs.l95_s = float(st.get("l95_s", rt.jobs.l95_s))
    # 会话冷启动（恢复之前）提交的规划作业属于被替换的任务：丢弃未生效的结果
    rt.jobs.due.clear()
    rt.jobs.waiting.clear()
    rt.previews.clear()
    rt.models.clear()
    rt._ko_cache.clear()
    rt._ko_base = None
    rt._trt_cache = None
    rt.alloc_job = None
    rt.alloc_dirty = True
    for T in sorted(rt.tasks.values(), key=lambda x: x.tid):
        if T.state == "PLANNING":
            T.plan_job = None
            with contextlib.suppress(Exception):
                rt._submit_decompose(T)       # 分解作业的结果随驻留丢失：重新提交
        r = T.relay
        if r is not None:
            r.station_job = r.relief_job = None
            r.job_t = -math.inf
            r.t_station = -math.inf
            r.next_forecast = 0.0
            r.next_weather = 0.0
            r.t_zoom = -math.inf
    rt.restored_t_ns = int(st.get("t_ns", 0))
    rt.reapply_after_restore = True
