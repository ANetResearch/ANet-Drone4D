"""tests/rt 的沙盒替身：FakePool（池命名空间 `awr/_sandbox/pool` 的 `sys/session/*` 服务方、1 Hz 摘要与池事件）与 SbStack
（FakePool + 每会话一个 FakeSim + 真实 sandbox-api 应用 + uvicorn 线程；全部经 LocalBus，不依赖 awr.sandbox）。

FakePool 只实现 sandbox-api（M11）可观察的池契约（AWR-17 §17.10；M17 PRD §7.5，WP-03 实现报告"池控制面约定"）：
- 请求 `{v, cid, principal, ts, args, sig}` 以 `K_pool`（`<state_dir>/pool.secret`）验签，principal 只认 `svc:sandbox-api`；
- `list` → `{v, cid, status: "ok", code: 0, result: {sessions: [会话], n_max}}`；`get{sid}`；`rate{sid, rate}` →
  `{result: {granted, cap, limited_by}}`；`activity{items}` 记录；其余 op 回 `status: "rejected"`；
- 会话：`{sid, state, world_id, template_id, principal_id, expires_unix_ns, renewable, limits, end_reason, binding: RunBinding}`；
- 测试操纵：`add`（READY，起 FakeSim）、`set_state`、`end`、`switch_world`、`publish_summary`、`emit`（池事件）、`down`/`up`。
"""

from __future__ import annotations

import os
import secrets
import shutil
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import fakesim
import rtc

from awr.contracts import bus_keys
from awr.runtime.binding import RunBinding
from awr.runtime.bus import LocalBus, pack
from awr.runtime.sbauth import (
    POOL_SECRET_NAME,
    TOKEN_KEY_NAME,
    load_or_create_key,
    make_sandbox_token,
    pool_key,
    sandbox_auth_key,
    verify_pool_request,
)
from awr.runtime.statering import LocalRing

B32 = "abcdefghijklmnopqrstuvwxyz234567"


def new_sid() -> str:
    return "sb-" + "".join(secrets.choice(B32) for _ in range(10))


class FakePool:
    def __init__(self, state_dir: Path, *, shm_root: Path, n_max: int = 4, namespace: str | None = None) -> None:
        self.state_dir = state_dir
        self.shm_root = shm_root
        self.k_pool = pool_key(load_or_create_key(state_dir / POOL_SECRET_NAME))
        self.k_sbauth = sandbox_auth_key(load_or_create_key(state_dir / TOKEN_KEY_NAME))
        self.n_max = n_max
        self.sessions: dict[str, dict] = {}
        self.sims: dict[str, fakesim.FakeSim] = {}
        self.calls: list[tuple[str, dict]] = []
        self.activity: list[dict] = []
        self.rejected_sig = 0
        self.grant: dict[str, Any] = {"cap": 5.0, "limited_by": "cap"}
        # 每个替身一个独立的池命名空间（进程内 LocalNet 按 namespace 隔离；多个栈可同时存在）
        self.namespace = namespace or f"{bus_keys.POOL_NAMESPACE}-{secrets.token_hex(3)}"
        self.bus = LocalBus.open("sandbox-pool", namespace=self.namespace, announce=False)
        self.pub_summary = self.bus.publisher(bus_keys.STATE_SANDBOX_POOL)
        self.pub_events = self.bus.publisher(bus_keys.EVT_SANDBOX_POOL)
        self.handles: list[Any] = []
        self.up()
        self.lock = threading.RLock()

    # ------------------------------------------------------------ 服务
    def up(self) -> None:
        if self.handles:
            return
        for op in ("list", "get", "rate", "activity", "keepalive", "stop", "create"):
            self.handles.append(self.bus.serve(bus_keys.sys_session(op), lambda r, op=op: self._serve(op, r)))

    def down(self) -> None:
        for h in self.handles:
            h.close()
        self.handles.clear()

    def _reply(self, req: Any, cid: Any, status: str, code: int = 0, result: Any = None) -> None:
        req.reply_msg({"v": 1, "cid": cid, "status": status, "code": code, "detail": None, "result": result})

    def _serve(self, op: str, req: Any) -> None:
        m = req.msg()
        if not verify_pool_request(self.k_pool, op, m):
            self.rejected_sig += 1
            self._reply(req, (m or {}).get("cid") if isinstance(m, dict) else None, "rejected", 115)
            return
        args = m.get("args") or {}
        self.calls.append((op, args))
        with self.lock:
            if op == "list":
                self._reply(req, m["cid"], "ok", 0, {"sessions": [self._wire(s) for s in self.sessions.values()],
                                                     "n_max": self.n_max})
            elif op == "get":
                s = self.sessions.get(args.get("sid"))
                self._reply(req, m["cid"], "ok" if s else "rejected", 0 if s else 305, self._wire(s) if s else None)
            elif op == "rate":
                want = float(args.get("rate", 1.0))
                cap = float(self.grant["cap"])
                granted = min(want, cap)
                self._reply(req, m["cid"], "ok", 0, {"granted": granted, "cap": cap,
                                                     "limited_by": self.grant["limited_by"] if granted < want else None})
            elif op == "activity":
                self.activity.append(args)
                self._reply(req, m["cid"], "ok", 0, {"ok": True})
            else:
                self._reply(req, m["cid"], "rejected", 110)

    def _wire(self, s: dict) -> dict:
        d = {k: v for k, v in s.items() if k != "binding_obj"}
        b = s.get("binding_obj")
        if b is not None:
            d["binding"] = b.to_msg()
        return d

    # ------------------------------------------------------------ 会话操纵
    def binding_for(self, sid: str, world: str) -> RunBinding:
        rd = self.state_dir / "runs" / sid
        rd.mkdir(parents=True, exist_ok=True)
        sec = rd / "secret"
        if not sec.exists():
            sec.write_bytes(os.urandom(32))
            os.chmod(sec, 0o600)
        return RunBinding.make(sid, world, kind="sandbox", run_dir=self.shm_root / sid, secret_path=sec,
                               persist_dir=rd, created_unix_ns=time.time_ns())

    def add(self, *, world: str = rtc.WORLD, principal: str = "p-test", state: str = "READY", n: int = 2,
            sim: bool = True, lifetime_s: int = 1800) -> str:
        sid = new_sid()
        b = self.binding_for(sid, world)
        with self.lock:
            self.sessions[sid] = {"sid": sid, "state": state, "world_id": world, "template_id": "t5_area_guard",
                                  "principal_id": principal, "expires_unix_ns": str(time.time_ns() + lifetime_s * 10**9),
                                  "renewable": True, "limits": {"vehicles_max": 12, "targets_max": 30},
                                  "end_reason": None, "binding_obj": b}
        if sim:
            self.start_sim(sid, n=n)
        return sid

    def start_sim(self, sid: str, *, n: int = 2, **kw: Any) -> fakesim.FakeSim:
        b: RunBinding = self.sessions[sid]["binding_obj"]
        b.run_dir.mkdir(parents=True, exist_ok=True)
        st = SimpleNamespace(secret=b.read_secret(), run_id=b.run_id, ring_path=b.ring_path, namespace=b.namespace,
                             run_dir=b.run_dir)
        sim = fakesim.FakeSim(st, n=n, **kw)
        self.sims[sid] = sim
        return sim

    def stop_sim(self, sid: str) -> None:
        sim = self.sims.pop(sid, None)
        if sim is not None:
            sim.close()

    def token(self, sid: str, *, principal: str | None = None, exp_in_s: int = 600, role: str = "operator") -> str:
        s = self.sessions[sid]
        tok, _ = make_sandbox_token(self.k_sbauth, principal_id=principal or s["principal_id"], sid=sid, mode="loopback",
                                    exp_unix_s=int(time.time()) + exp_in_s, role=role)
        return tok

    def set_state(self, sid: str, state: str, **kw: Any) -> None:
        with self.lock:
            self.sessions[sid].update(state=state, **kw)
        self.emit("sandbox.state", sid, to=state)

    def end(self, sid: str, reason: str = "user") -> None:
        self.set_state(sid, "ENDED", end_reason=reason)
        self.stop_sim(sid)

    def drop(self, sid: str) -> None:
        with self.lock:
            self.sessions.pop(sid, None)
        self.stop_sim(sid)

    def switch_world(self, sid: str, world: str) -> None:
        """底图切换：新世界、新环位置（同一 sid），SPAWNING → READY。"""
        self.stop_sim(sid)
        b = self.binding_for(sid, world)
        b = replace(b, run_dir=self.shm_root / f"{sid}-{world}", ring_path=self.shm_root / f"{sid}-{world}" / "state.sim-core")
        with self.lock:
            self.sessions[sid].update(world_id=world, binding_obj=b, state="SPAWNING")
        self.emit("sandbox.state", sid, to="SPAWNING")

    def publish_summary(self, extra: dict[str, dict] | None = None) -> None:
        items = []
        with self.lock:
            for sid, s in self.sessions.items():
                d = {"sid": sid, "state": s["state"], "expires_unix_ns": s["expires_unix_ns"], "renewable": True,
                     "clock": {"rate_granted": 1.0, "rate_cap": 5.0, "limited_by": None}, "queue_len": 0,
                     "degrade_level": "L0"}
                d.update((extra or {}).get(sid, {}))
                items.append(d)
        self.pub_summary.put(pack({"v": 1, "t_unix_ns": str(time.time_ns()), "sessions": items, "n_max": self.n_max}))

    def emit(self, kind: str, sid: str, **data: Any) -> None:
        ev = {"seq": 1, "epoch": 1, "producer": "sandbox-pool", "kind": kind, "severity": 1, "t_sim_ns": 0,
              "t_wall_ns": time.time_ns(), "uav": None, "cid": None, "data": {"sid": sid, **data}}
        self.pub_events.put(pack([ev]))

    def close(self) -> None:
        for sid in list(self.sims):
            self.stop_sim(sid)
        self.down()
        self.bus.close()


class SbStack:
    """FakePool + sandbox-api 应用（create_sandbox_app，LocalBus、LocalRing）+ uvicorn 线程。"""

    def __init__(self, *, n_max: int = 4, extra_routers: list | None = None, sb_over: dict | None = None,
                 state_dir: Path | None = None, pool: FakePool | None = None, **settings_over: Any) -> None:
        from awr.api.inproc import inproc_settings
        from awr.runtime.config import SandboxCfg

        self.tmp = Path(tempfile.mkdtemp(prefix="awr-sbx-"))
        self.state_dir = state_dir or (self.tmp / "state")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.shm = Path("/dev/shm/awr") / f"t-{secrets.token_hex(4)}"
        self.pool = pool or FakePool(self.state_dir, shm_root=self.shm, n_max=n_max)
        base = {"hello_timeout_s": 2.0, "serve_web": False}
        self.settings = replace(inproc_settings(rtc.WORLD), **(base | settings_over))
        self.sb = SandboxCfg.model_validate(sb_over or {})
        self.extra_routers = extra_routers or []
        self.port = rtc.free_port()
        self._serve()
        self.base = f"http://127.0.0.1:{self.port}"
        self.origin = self.base

    def _serve(self) -> None:
        import uvicorn
        from fastapi import Depends

        from awr.api.deps import require_sandbox_session
        from awr.api.sandbox_main import PREFIX, create_sandbox_app

        self.app = create_sandbox_app(self.settings, sb=self.sb, state_dir=self.state_dir, bus_kind="local",
                                      pool_bus=LocalBus.open("sandbox-api", namespace=self.pool.namespace,
                                                             announce=False),
                                      session_bus_factory=lambda b: LocalBus.open("sandbox-api", namespace=b.namespace,
                                                                                  announce=False),
                                      ring_cls=LocalRing)
        n0 = len(self.app.router.routes)
        for r in self.extra_routers:
            self.app.include_router(r, prefix=PREFIX + "/sessions/{sid}", dependencies=[Depends(require_sandbox_session)])
        # 测试路由排在最前：领域路由（并行工作包陆续交付）与测试路由同路径时以测试路由为准
        routes = self.app.router.routes
        routes[:] = routes[n0:] + routes[:n0]
        # proxy_headers=False：测试客户端直连回环，不让 uvicorn 采信 X-Forwarded-For（伪造头的判定交给 api 自己的
        # 可信代理逻辑，M11-AC-207）
        cfg = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, ws="websockets", log_level="warning",
                             lifespan="on", ws_per_message_deflate=False, ws_max_size=262144, proxy_headers=False)
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, name="uvicorn-sbx", daemon=True)
        self.thread.start()
        end = time.monotonic() + 20
        while not self.server.started:
            if time.monotonic() > end or not self.thread.is_alive():
                raise RuntimeError("uvicorn 未启动")
            time.sleep(0.02)

    @property
    def ctx(self) -> Any:
        return self.app.state.awr

    @property
    def hub(self) -> Any:
        return self.app.state.awr.hub

    def ws_url(self, sid: str) -> str:
        return f"ws://127.0.0.1:{self.port}/api/sandbox/v1/sessions/{sid}/rt"

    def call_in_loop(self, fn: Any, *args: Any) -> Any:
        loop = self.hub.loop
        box: dict[str, Any] = {}
        ev = threading.Event()

        def run() -> None:
            try:
                box["v"] = fn(*args)
            except BaseException as e:
                box["e"] = e
            ev.set()

        loop.call_soon_threadsafe(run)
        if not ev.wait(5):
            raise TimeoutError("事件循环未响应")
        if "e" in box:
            raise box["e"]
        return box.get("v")

    def wait(self, pred: Any, timeout: float = 5.0, what: str = "条件") -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.call_in_loop(pred):
                return
            time.sleep(0.02)
        raise AssertionError(f"等待超时：{what}")

    def wait_live(self, sid: str, timeout: float = 6.0) -> None:
        self.wait(lambda: (c := self.hub.get(sid)) is not None and c.live, timeout, f"{sid} LIVE")

    def stop_api(self) -> None:
        self.server.should_exit = True
        self.thread.join(10)

    def restart_api(self) -> None:
        self.stop_api()
        self._serve()

    def close(self) -> None:
        self.stop_api()
        self.pool.close()
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.shm, ignore_errors=True)
