"""RunBinding：一个 run 的寻址与密钥位置（M11-FR-201；AWR-04 §4.7 第 1 条；AWR-17 §17.10.1）。

D1 的 api 进程即一个 run（`AWR_RUN` 等变量）；D2 的 sandbox-api 同时服务多个沙盒 run，因此把"run 在哪里"从进程级设置中
拆出：`Gateway` 按 RunBinding 实例化，不再读进程级的 `run_id`、`run_dir`、`ring_path`。

- 共享展示（`kind = shared`）：由 supervisor 注入的环境变量构造（`RunBinding.from_env`），与 D1 完全一致；
- 沙盒（`kind = sandbox`）：run id 即 sid（`sb-` 加 10 位小写 base32），由 sandbox-pool 在 `sys/session/{get,list}` 与
  会话变更中给出（`RunBinding.from_msg`），字段名按 17 §17.10.1；`to_msg()` 是同一形状（池可直接调用）。
- namespace 一律经生成的 `bus_keys.namespace(world, run)`（zenoh chunk 规则），run id 不合规时退回 `local`（D1 口径）。

本模块只依赖 `awr.contracts`，sandbox-pool（M17）与 api 都可 import。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from awr.contracts import bus_keys

__all__ = ["CLOCK_PROFILES", "RUN_RE", "SID_RE", "RunBinding", "is_sid"]

SID_RE = re.compile(r"^sb-[a-z2-7]{10}$")
RUN_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
CLOCK_PROFILES = ("d1_250", "sandbox100")
DEFAULT_SHM_ROOT = Path("/dev/shm/awr")


def is_sid(s: Any) -> bool:
    return isinstance(s, str) and SID_RE.match(s) is not None


def _ns(world_id: str, run_id: str) -> str:
    return bus_keys.namespace(world_id, run_id if RUN_RE.match(run_id) else "local")


@dataclass(frozen=True)
class RunBinding:
    run_id: str                                   # 共享展示为 D1 run id；沙盒为 sid
    kind: Literal["shared", "sandbox"]
    world_id: str
    run_dir: Path                                 # /dev/shm/awr/<run>
    ring_path: Path                               # <run_dir>/state.<producer>
    namespace: str                                # awr/<world>/<run>
    secret_path: Path | None                      # 该 run 的 per-run secret（0600）；None 时由调用方直接给出秘密
    producer: str = "sim-core"
    clock_profile: str = "d1_250"
    persist_dir: Path | None = None               # runs/<run>（共享展示）或 var/sandbox/runs/<sid>（沙盒）
    created_unix_ns: int | None = None
    extra: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    # ------------------------------------------------------------ 构造
    @classmethod
    def make(cls, run_id: str, world_id: str, *, kind: Literal["shared", "sandbox"] | None = None,
             run_dir: Path | str | None = None, secret_path: Path | str | None = None, producer: str = "sim-core",
             clock_profile: str | None = None, persist_dir: Path | str | None = None,
             shm_root: Path | str = DEFAULT_SHM_ROOT, created_unix_ns: int | None = None) -> RunBinding:
        k = kind or ("sandbox" if is_sid(run_id) else "shared")
        rd = Path(run_dir) if run_dir is not None else Path(shm_root) / run_id
        cp = clock_profile or ("sandbox100" if k == "sandbox" else "d1_250")
        return cls(run_id=run_id, kind=k, world_id=world_id, run_dir=rd, ring_path=rd / f"state.{producer}",
                   namespace=_ns(world_id, run_id), secret_path=Path(secret_path) if secret_path else None,
                   producer=producer, clock_profile=cp, persist_dir=Path(persist_dir) if persist_dir else None,
                   created_unix_ns=created_unix_ns)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, *, producer: str = "sim-core") -> RunBinding:
        """D1 口径（supervisor 注入）：`AWR_RUN`、`AWR_WORLD`、`AWR_RUN_DIR`、`AWR_PERSIST_DIR`、`AWR_SECRET_FILE`。"""
        e = os.environ if env is None else env
        run_id = e.get("AWR_RUN", "local")
        runs_root = Path(e.get("AWR_RUNS_DIR", "runs"))
        return cls.make(run_id, e.get("AWR_WORLD", "shenzhen"), kind="shared",
                        run_dir=e.get("AWR_RUN_DIR", f"/dev/shm/awr/{run_id}"),
                        secret_path=e.get("AWR_SECRET_FILE") or None, producer=producer,
                        clock_profile=e.get("AWR_CLOCK_PROFILE") or "d1_250",
                        persist_dir=e.get("AWR_PERSIST_DIR", str(runs_root / run_id)))

    @classmethod
    def from_msg(cls, d: Mapping[str, Any]) -> RunBinding:
        """池在 `sys/session/{get,list}` 与会话变更中给出的绑定（17 §17.10.1）。缺省字段按沙盒口径补齐；
        run id 或 world id 非法时抛 ValueError（不信任总线上的字符串去拼路径）。"""
        run_id = str(d.get("run_id") or d.get("sid") or "")
        world = str(d.get("world_id") or "")
        if not RUN_RE.match(run_id) or not RUN_RE.match(world):
            raise ValueError(f"非法的 run 绑定：run_id={run_id!r} world_id={world!r}")
        kind = d.get("kind") if d.get("kind") in ("shared", "sandbox") else ("sandbox" if is_sid(run_id) else "shared")
        producer = str(d.get("producer") or "sim-core")
        ring = d.get("ring_path")
        run_dir = Path(d["run_dir"]) if d.get("run_dir") else (Path(ring).parent if ring else DEFAULT_SHM_ROOT / run_id)
        ns = str(d.get("namespace") or _ns(world, run_id))
        if ns != _ns(world, run_id):
            raise ValueError(f"namespace 与 run 不一致：{ns!r}")
        cp = d.get("clock_profile") if d.get("clock_profile") in CLOCK_PROFILES else (
            "sandbox100" if kind == "sandbox" else "d1_250")
        created = d.get("created_unix_ns")
        try:
            created_i = int(created) if created is not None else None
        except (TypeError, ValueError):
            created_i = None
        secret = d.get("secret_path")
        persist = d["persist_dir"] if "persist_dir" in d else (str(Path(secret).parent) if secret else None)
        return cls(run_id=run_id, kind=kind, world_id=world, run_dir=run_dir,
                   ring_path=Path(ring) if ring else run_dir / f"state.{producer}", namespace=ns,
                   secret_path=Path(secret) if secret else None, producer=producer, clock_profile=str(cp),
                   persist_dir=Path(persist) if persist else None, created_unix_ns=created_i)

    def to_msg(self) -> dict[str, Any]:
        d: dict[str, Any] = {"run_id": self.run_id, "kind": self.kind, "world_id": self.world_id,
                             "run_dir": str(self.run_dir), "ring_path": str(self.ring_path), "namespace": self.namespace,
                             "secret_path": str(self.secret_path) if self.secret_path else None,
                             "producer": self.producer, "clock_profile": self.clock_profile,
                             "persist_dir": str(self.persist_dir) if self.persist_dir else None}
        if self.created_unix_ns is not None:
            d["created_unix_ns"] = str(self.created_unix_ns)
        return d

    # ------------------------------------------------------------ 派生
    @property
    def is_sandbox(self) -> bool:
        return self.kind == "sandbox"

    @property
    def tick_hz(self) -> int:
        return 100 if self.clock_profile == "sandbox100" else 250

    def read_secret(self) -> bytes:
        if self.secret_path is None:
            raise FileNotFoundError(f"run {self.run_id} 没有 secret_path")
        return Path(self.secret_path).read_bytes()

    def with_world(self, world_id: str) -> RunBinding:
        """底图切换（同一 sid、新世界）：namespace 随世界变化，环与密钥位置不变（池可另给新绑定，以池为准）。"""
        return replace(self, world_id=world_id, namespace=_ns(world_id, self.run_id))
