"""输入日志 `runs/<run>/inputs.msgpack`（D1-ext，M08-FR-084；ADR-049；AWR-16 §13.8）。

sim-core 顺序写入的 msgpack 对象流：第一个对象为头 `{schema: awr.inputlog.v1, schema_version, run_id, segment, world_seed,
meta_sha256}`，其后每条 `{tick, kind, principal, cid, payload_sha256, payload}`（`tick` 为实际生效的 apply_tick）。
kind：`cmd`、`clock`、`lease`、`env_set`、`env_preset`、`fault`、`plan_result`、`geo_result`、`roster`、`scenario_event`。
主循环只把条目追加到内存队列；后台线程每 0.5 s 批量写盘（主循环内禁止非 tmpfs 文件 I/O，M08-FR-003）。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import threading
from pathlib import Path
from typing import Any

import msgpack

__all__ = ["InputLog", "attach_inputlog", "meta_sha256", "read_inputlog"]

SCHEMA = "awr.inputlog.v1"
KIND_MAP = {"cmd": "cmd", "clock": "clock", "lease": "lease", "roster": "roster", "env": "env_set", "fault": "fault",
            "plan_result": "plan_result", "geo_result": "geo_result"}


_PLAIN_TYPES = frozenset((float, int, str, bool, type(None)))


def _plain_seq(x: list | tuple) -> bool:
    """x 的元素都是 msgpack 原样编码的标量，或这类标量的列表、元组（航点、样条控制点）。"""
    pt = _PLAIN_TYPES
    for v in x:
        tv = type(v)
        if tv in pt:
            continue
        if tv is list or tv is tuple:
            for u in v:
                if type(u) not in pt:
                    return False
            continue
        return False
    return True


def _plain(x: Any) -> Any:
    """去掉 `sig` 键、numpy 标量转 Python 标量、bytearray 转 bytes。只由数值组成的序列（例如 follow_path 的 1000 个航点
    与样条控制点）原样返回：msgpack 对元组与列表、对这些标量的编码与逐元素复制后相同，载荷字节不变；此前逐元素递归，
    一条带样条的转场 follow_path 约 3.5 ms（主循环准入内，ADR-073 第 5 条）。"""
    if type(x) in _PLAIN_TYPES:
        return x
    if isinstance(x, dict):
        return {str(k): _plain(v) for k, v in x.items() if k != "sig"}
    if isinstance(x, (list, tuple)):
        if _plain_seq(x):
            return x
        return [_plain(v) for v in x]
    if isinstance(x, (bytes, bytearray)):
        return bytes(x)
    if hasattr(x, "item"):
        return x.item()
    return x


def meta_sha256(binding: dict, sim: dict) -> str:
    """meta.json 的 binding 与 sim 两节规范化后的 sha256（重仿真兼容性判据）。"""
    s = json.dumps({"binding": binding, "sim": sim}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


class InputLog:
    def __init__(self, path: Path, header: dict, *, flush_s: float = 0.5) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.entries = 0
        self._q: list[bytes] = [msgpack.packb({"schema": SCHEMA, "schema_version": "1.0.0", **header}, use_bin_type=True)]
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._flush_s = flush_s
        self._sha = hashlib.sha256()
        self._fh = open(self.path, "ab")  # noqa: SIM115 - 后台线程持有
        self._thread = threading.Thread(target=self._writer, name="awr-inputlog", daemon=True)
        self._thread.start()

    def append(self, kind: str, apply_tick: int, msg: Any) -> None:
        """主线程：记一条外部输入（载荷去掉签名后按 msgpack 编码）。"""
        m = _plain(msg)
        p = m.get("principal") if isinstance(m, dict) else None
        payload = msgpack.packb(m, use_bin_type=True)
        rec = {"tick": int(apply_tick), "kind": KIND_MAP.get(kind, kind),
               "principal": None if not isinstance(p, dict) else {"id": p.get("principal_id"), "role": p.get("role"),
                                                                    "entry": p.get("entry")},
               "cid": m.get("cid") if isinstance(m, dict) else None,
               "payload_sha256": hashlib.sha256(payload).hexdigest(), "payload": payload}
        b = msgpack.packb(rec, use_bin_type=True)
        with self._lock:
            self._q.append(b)
        self.entries += 1

    def _drain(self) -> None:
        with self._lock:
            q, self._q = self._q, []
        if q:
            for b in q:
                self._sha.update(b)
                self._fh.write(b)
            self._fh.flush()

    def _writer(self) -> None:
        while not self._stop.wait(self._flush_s):
            with contextlib.suppress(OSError):
                self._drain()

    def sha256(self) -> str:
        return self._sha.hexdigest()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        with contextlib.suppress(OSError):
            self._drain()
            self._fh.close()


def read_inputlog(path: Path) -> tuple[dict, list[dict]]:
    """读取输入日志（末尾截断的不完整对象丢弃）；返回 (头, 条目列表)。"""
    data = Path(path).read_bytes()
    up = msgpack.Unpacker(raw=False, strict_map_key=False)
    up.feed(data)
    objs: list = []
    with contextlib.suppress(msgpack.OutOfData, ValueError):
        objs.extend(up)
    if not objs or objs[0].get("schema") != SCHEMA:
        raise ValueError(f"{path}: not an {SCHEMA} stream")
    return objs[0], objs[1:]


def attach_inputlog(core: Any, ctx: Any) -> None:
    """supervisor 形态：`runs/<run>/inputs.msgpack`（`AWR_SIM_INPUTLOG=0` 关闭）。"""
    import os

    if not getattr(ctx, "supervised", False) or os.environ.get("AWR_SIM_INPUTLOG", "1") in ("0", "false", "no"):
        return
    binding, sim = {}, {}
    with contextlib.suppress(OSError, ValueError):
        meta = json.loads((Path(ctx.persist_dir) / "meta.json").read_text(encoding="utf-8"))
        binding, sim = meta.get("binding", {}), meta.get("sim", {})
    core.inputlog = InputLog(Path(ctx.persist_dir) / "inputs.msgpack", inputlog_header(core, ctx.run_id, binding, sim))


def inputlog_header(core: Any, run_id: str, binding: dict | None = None, sim: dict | None = None) -> dict:
    """输入日志头：重建 sim-core 所需的全部配置（内核、版本、FleetConfig、世界、布设、world_seed）。"""
    c = core.cfg
    return {"run_id": run_id, "segment": core.segment, "world_seed": c.world_seed,
            "meta_sha256": meta_sha256(binding or {}, sim or {}),
            "sim": {"kernel": c.fleet.kernel, "fleet_config": c.fleet.to_json(), "world_id": c.world_id,
                    "n_vehicles": c.n_vehicles, "profile_id": c.profile_id, "load_world": c.load_world,
                    "spawn_xy": c.spawn_xy, "spawn_spacing_m": c.spawn_spacing_m, "limits_profile": c.limits_profile,
                    "autoplay": c.autoplay, "version": _version()}}


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("awr")
    except Exception:
        return "0"
