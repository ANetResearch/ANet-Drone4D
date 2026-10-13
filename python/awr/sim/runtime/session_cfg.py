"""会话配置的装配与导出（M08 §14.3 `sim/runtime/**` 会话配置行；AWR-04 §4.6；17 §17.4.5；`awr.sandbox.snapshot.v1`）。

- 冷启动：sandbox-pool 拉起沙盒 sim-core 时写 `session.json`（场景模板或配置快照）并以 `AWR_SESSION_CONFIG` 传入路径，sim-core
  在 `start()` 末尾原地装配机巢、机队、识别物与任务（不经多次 RPC 拼装）；
- 会话内载入模板或快照：`ctl/sim-core/clock` 的 `reset{sandbox_config}`（先清空机队、识别物与会话克隆，再按配置装配）；
- 导出：`ctl/sim-core/snapshot{op: export}` → `awr.sandbox.snapshot.v1`（只含配置，不含运行状态）。

各段按 order 升序装配：`models`（10，M08：会话克隆）→ `calendar`、`acoustic_bg`、`env`（20，M07 登记）→ `nests`（30，M20 登记）→
`vehicles`（40，M08）→ `targets`（50，M18 登记）→ `tasks`（60，M20 登记）。环境段 calendar、acoustic_bg、surface_class、env
没有登记装配者时由 M08 合为一次调用交给环境服务（`ctx.session_config`；服务已存在时调用其 `configure_session`），导出取其
`session_snapshot(t)`。领域段的装配与导出由所有者经
`register_session_loader(section, fn, order=)`、`register_snapshot_exporter(section, fn)` 登记，`fn(items, api)` 中的 `api` 即本类
实例（`ctx`、`world`、`entities`、`profiles`、`submit(op, args, uav=)` 以内部 principal 走同一准入）。有内容却没有登记装配者的列表段
记为问题 `NO_LOADER`（不中断其余段；对象段 calendar、acoustic_bg、env 原样保留供导出）。装配只在步边界执行（主循环 drain 或启动序列）。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from awr.contracts import CONTRACTS_VERSION
from awr.contracts.reasons import Reason

__all__ = ["DEFAULT_CALENDAR", "SECTION_ORDER", "SessionConfig"]

log = logging.getLogger("awr.sim.runtime.session")

SECTION_ORDER = {"models": 10, "calendar": 20, "acoustic_bg": 21, "surface_class": 22, "env": 23, "nests": 30, "vehicles": 40,
                 "targets": 50, "tasks": 60}
BUILTIN = ("models", "vehicles")
ENV_SECTIONS = ("calendar", "acoustic_bg", "surface_class", "env")  # M07 的会话配置段（environment.configure_session）
DEFAULT_CALENDAR = {"start_utc": "2026-10-05T00:00:00Z", "tz": "Asia/Shanghai"}
SNAPSHOT_SCHEMA = "awr.sandbox.snapshot.v1"


class SessionConfig:
    def __init__(self, core: Any) -> None:
        self.core = core
        self.loaded: dict | None = None  # 最近一次装配的配置（导出时补足没有导出者的段）
        self.problems: list[dict] = []
        self.template_id: str | None = None
        self.seed: int | None = None

    # ------------------------------------------------------------ 供装配者使用的接口
    @property
    def ctx(self) -> Any:
        return self.core.ctx

    @property
    def world(self) -> Any:
        return self.core.world

    @property
    def entities(self) -> Any:
        return self.core.entities

    @property
    def profiles(self) -> Any:
        return self.core.T

    def submit(self, op: str, args: dict, *, uav: str | None = None, principal: str = "session") -> dict:
        """以内部 principal（role scenario，免租约与席位）提交一条命令，走同一准入与生命周期；返回 Admission。"""
        core = self.core
        core._session_seq = getattr(core, "_session_seq", 0) + 1
        cmd = {"cid": f"sess-{core._session_seq:05d}", "op": op, "uav": uav, "args": dict(args)}
        return core.engine.submit_internal(cmd, {"principal_id": principal, "role": "scenario", "entry": "scenario",
                                                 "seat": False})

    # ------------------------------------------------------------ 装配
    def cold_start(self) -> list[dict]:
        path = os.environ.get("AWR_SESSION_CONFIG")
        if not path:
            return []
        try:
            cfg = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.error("session config unreadable", extra={"kv": {"path": path, "error": str(e)}})
            self.problems = [{"section": "*", "why": "UNREADABLE", "detail": str(e)}]
            return self.problems
        return self.apply(cfg)

    def apply(self, cfg: dict) -> list[dict]:
        """装配一份配置（`awr.sandbox.snapshot.v1` 或场景模板的同构对象）；返回问题列表（空为全部成功）。"""
        core = self.core
        probs: list[dict] = []
        if not isinstance(cfg, dict):
            self.problems = [{"section": "*", "why": "NOT_A_MAPPING"}]
            return self.problems
        wid = cfg.get("world_id")
        if wid is not None and str(wid) != str(core.cfg.world_id):
            self.problems = [{"section": "world_id", "why": "WORLD_MISMATCH", "code": 512, "world_id": wid,
                              "session_world": core.cfg.world_id}]
            return self.problems
        self.template_id = cfg.get("template_id")
        if isinstance(cfg.get("seed"), int):
            self.seed = int(cfg["seed"])
            core.ctx.rng = core._rng_streams(self.seed)
        lim = cfg.get("limits")
        if isinstance(lim, dict):
            for k in ("targets", "vehicles", "clones"):
                if isinstance(lim.get(k), int):
                    core.limits[k] = int(lim[k])
            if core.entities is not None and isinstance(lim.get("targets"), int):
                core.entities["target"].limit = min(int(lim["targets"]), core.entities["target"].capacity)
        reg = core.reg
        sections = [k for k in cfg if k in SECTION_ORDER or k in reg.session_loaders]

        def order(k: str) -> int:
            spec = reg.session_loaders.get(k)
            return spec.order if spec is not None else SECTION_ORDER.get(k, 99)

        env_done = False
        for k in sorted(sections, key=lambda x: (order(x), x)):
            items = cfg[k]
            if items in (None, [], {}):
                continue
            if k in ENV_SECTIONS and k not in reg.session_loaders:
                if not env_done:  # 四个环境段合为一次装配
                    env_done = True
                    probs += self._load_env({kk: cfg[kk] for kk in ENV_SECTIONS
                                             if kk not in reg.session_loaders and cfg.get(kk) not in (None, [], {})})
                continue
            if k == "models":
                probs += self._load_models(items)
            elif k == "vehicles":
                probs += self._load_vehicles(items)
            elif k in reg.session_loaders:
                try:
                    r = reg.session_loaders[k].fn(items, self)
                except Exception as e:
                    log.exception("session loader failed", extra={"kv": {"section": k}})
                    r = [{"section": k, "why": "LOADER_ERROR", "detail": f"{type(e).__name__}: {e}"}]
                probs += [dict(x, section=x.get("section", k)) if isinstance(x, dict) else {"section": k, "detail": str(x)}
                          for x in (r or [])]
            elif isinstance(items, list):  # 列表段（nests、targets、tasks）没有装配者：内容会丢失，记为问题
                probs.append({"section": k, "why": "NO_LOADER", "n": len(items)})
            # 对象段（calendar、acoustic_bg、env）没有装配者时原样保留，供导出（M07 装配后生效）
        self.loaded = dict(cfg)
        self.problems = probs
        if probs:
            log.warning("session config applied with problems", extra={"kv": {"n": len(probs), "first": probs[:3]}})
        return probs

    def _load_env(self, envcfg: dict) -> list[dict]:
        """M07 部分（WP-09 → M08 第 3 条）：置 `ctx.session_config`（环境服务首次构造时读取）；服务已存在时直接调用其
        `configure_session(cfg, t_ns)`（尚未推进网格点时改写初始关键帧，否则下一网格点生效）。拒绝记为问题（code 300、110）。"""
        ctx = self.core.ctx
        ctx.session_config = dict(envcfg)
        env = getattr(ctx, "env", None)
        fn = getattr(env, "configure_session", None)
        if not callable(fn):
            return []
        try:
            fn(dict(envcfg), int(ctx.t_ns) or None)
        except Exception as e:
            code = getattr(e, "code", None)
            return [{"section": "env", "why": "ENV_REJECTED", "code": int(code) if isinstance(code, int) else int(Reason.PARAM_OUT_OF_RANGE),
                     "detail": getattr(e, "detail", None) or f"{type(e).__name__}: {e}"}]
        return []

    def _load_models(self, items: list) -> list[dict]:
        T = self.core.T
        specs = [{"id": d["id"], "base": d["base"], "set": d.get("set") or {}, "display_name": d.get("display_name")}
                 for d in (items if isinstance(items, list) else []) if isinstance(d, dict) and d.get("id") and d.get("base")]
        bad = T.restore_sessions(specs)
        return [{"section": "models", "id": b, "why": "CLONE_INVALID", "code": int(Reason.VEHICLE_CLONE_INVALID)}
                for b in bad]

    def _load_vehicles(self, items: list) -> list[dict]:
        core = self.core
        probs = []
        for k, v in enumerate(items if isinstance(items, list) else []):
            if not isinstance(v, dict):
                probs.append({"section": "vehicles", "index": k, "why": "NOT_A_MAPPING"})
                continue
            args = {"model_id": v.get("model_id"), "vehicle_id": v.get("id"), "nest_id": v.get("nest_id"),
                    "pos_enu_m": v.get("home_enu_m"), "label": v.get("label"), "yaw_rad": v.get("yaw_rad"),
                    "initial_soc": v.get("initial_soc", 1.0), "sensors_cfg": v.get("sensors"),
                    "limits_profile": v.get("limits_profile"), "lights": v.get("lights")}
            code, detail = core.admit_add({kk: vv for kk, vv in args.items() if vv is not None})
            if code:
                probs.append({"section": "vehicles", "index": k, "id": v.get("id"), "code": int(code), "detail": detail})
        return probs

    # ------------------------------------------------------------ 导出（ctl/sim-core/snapshot）
    def export(self) -> dict:
        core = self.core
        reg = core.reg
        prev = self.loaded or {}
        out: dict[str, Any] = {"schema": SNAPSHOT_SCHEMA, "contracts": CONTRACTS_VERSION, "world_id": core.cfg.world_id,
                               "template_id": self.template_id,
                               "calendar": prev.get("calendar") or dict(DEFAULT_CALENDAR),
                               "seed": int(self.seed if self.seed is not None else core.cfg.world_seed),
                               "models": core.T.clone_docs(), "nests": [], "vehicles": self._export_vehicles(),
                               "targets": [], "tasks": [], "created_unix_ns": str(time.time_ns())}
        for k in ("acoustic_bg", "surface_class", "env"):
            if k in prev:
                out[k] = prev[k]
        env = getattr(core.ctx, "env", None)
        snap = getattr(env, "session_snapshot", None)
        if callable(snap):  # M07 的当前值优先（日历取当前时刻对应的日历时刻）
            with contextlib.suppress(Exception):
                out.update({k: v for k, v in (snap(int(core.ctx.t_ns)) or {}).items() if k in ENV_SECTIONS})
        for k, fn in reg.snapshot_exporters.items():
            with contextlib.suppress(Exception):
                val = fn(self)
                if val is not None:
                    out[k] = val
        if not out["models"]:
            out.pop("models")
        return out

    def _export_vehicles(self) -> list[dict]:
        core = self.core
        out = []
        for s in core.roster.slots_in_order():
            e = core.roster.by_slot[s]
            if e.backend == "replay":
                continue
            vc = core.vehicle_cfg.get(e.id, {})
            p = core.T.get(e.profile_id)
            mid = p.ref if "@" not in p.ref else p.profile_id
            d: dict[str, Any] = {"id": e.id, "model_id": mid, "nest_id": vc.get("nest_id"),
                                 "home_enu_m": None if vc.get("nest_id") else [float(x) for x in e.home_enu_m],
                                 "label": vc.get("label"), "yaw_rad": float(e.yaw_rad), "initial_soc": float(e.initial_soc),
                                 "limits_profile": e.limits_profile}
            if vc.get("sensors") is not None:
                d["sensors"] = vc["sensors"]
            if vc.get("lights") is not None:
                d["lights"] = vc["lights"]
            out.append(d)
        return out
