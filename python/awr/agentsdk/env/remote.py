"""容器 Agent 的宿主侧（AWR-17 §18.9.4–§18.9.7；M26 §7.2）：每架机一个 Unix 套接字（宿主侧随机路径，只挂载进该机的容器），
Env 经线协议交付 CONFIG 与逐步 OBS，并在墙钟预算内收集 ACT。

- 握手：60 s 内无 HELLO 为 665 `hello_timeout`，30 s 内无 READY 为 665 `ready_timeout`；协议主版本不同返回 ERROR 666 `version`；
- 步进：先向本步被调用的全部 Agent 发出 OBS，再按 `decision.step_budget_s`（自发出起计）收集 ACT；超时由 Env 按 `timeout`
  回执并沿用上一指令；迟到的 ACT（`k` 小于当前步）丢弃并计数；`k` 不一致返回 ERROR 666 `k_mismatch`，连续 3 次协议错误视为退出；
- Agent 发送 BYE、断开或连续协议错误后视为退出：其后各步不再交付观测，Env 对该机按超时处置（沿用上一指令）。
"""

from __future__ import annotations

import contextlib
import os
import socket
import time
from collections.abc import Mapping
from pathlib import Path

from awr.agentsdk.wire import PROTOCOL, T, WireError, WireSocket

__all__ = ["SocketAgentHost", "run_remote_episode"]


class SocketAgentHost:
    def __init__(self, uav: str, sock_path: Path, *, hello_timeout_s: float = 60.0, ready_timeout_s: float = 30.0) -> None:
        self.uav, self.path = uav, Path(sock_path)
        self.hello_timeout_s, self.ready_timeout_s = hello_timeout_s, ready_timeout_s
        if len(os.fsencode(self.path)) > 100:
            raise ValueError(f"agent socket path is {len(os.fsencode(self.path))} bytes (AF_UNIX limit ~108); set AWR_AGENT_SOCK_ROOT to a short directory")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.path.unlink()
        self.lsock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.lsock.bind(str(self.path))
        os.chmod(self.path, 0o666)                           # 路径即授权：只挂载进该机的容器（容器用户 uid 可不同）
        self.lsock.listen(1)
        self.ws: WireSocket | None = None
        self.hello: dict | None = None
        self.k: int | None = None
        self.exited: str | None = None
        self.closed = False
        self.stats = {"late": 0, "protocol_errors": 0, "acts": 0, "timeouts": 0}
        self._errs = 0

    # ------------------------------------------------------------ 握手
    def accept(self) -> dict:
        self.lsock.settimeout(self.hello_timeout_s)
        t0 = time.monotonic()
        try:
            conn, _ = self.lsock.accept()
        except TimeoutError:
            raise WireError(665, "hello_timeout") from None
        self.ws = WireSocket(conn)
        t, body = self.ws.recv(t0 + self.hello_timeout_s)
        if t != T.HELLO or not isinstance(body, dict):
            self._error(666, "frame")
            raise WireError(666, "frame")
        proto = str(body.get("protocol", ""))
        if proto != PROTOCOL:
            self._error(666, "version")
            raise WireError(666, "version")
        self.hello = body
        return body

    def configure(self, config: dict) -> None:
        assert self.ws is not None
        self.ws.send(T.CONFIG, config)
        deadline = time.monotonic() + self.ready_timeout_s
        while True:
            try:
                t, _b = self.ws.recv(deadline)
            except TimeoutError:
                raise WireError(665, "ready_timeout") from None
            if t == T.READY:
                return
            self._protocol_error("frame")

    # ------------------------------------------------------------ 步进
    def send_obs(self, item: dict) -> bool:
        if self.exited or self.ws is None:
            return False
        self.k = int(item["obs"]["k"])
        try:
            self.ws.send(T.OBS, {"obs": item["obs"], "inbox": item["inbox"]})
        except OSError:
            self.exited = "disconnected"
            return False
        return True

    def recv_act(self, deadline: float) -> dict | None:
        while not self.exited and self.ws is not None:
            try:
                t, body = self.ws.recv(deadline)
            except TimeoutError:
                self.stats["timeouts"] += 1
                return None
            except ConnectionError:
                self.exited = "disconnected"
                return None
            except WireError as e:
                self._protocol_error(e.why)
                continue
            if t == T.BYE:
                self.exited = "bye"
                return None
            if t != T.ACT or not isinstance(body, dict):
                self._protocol_error("frame")
                continue
            k = body.get("k")
            if isinstance(k, int) and self.k is not None and k < self.k:
                self.stats["late"] += 1                      # 上一步超时后才到达的 ACT
                continue
            if k != self.k:
                self._protocol_error("k_mismatch")
                continue
            self._errs = 0
            self.stats["acts"] += 1
            return body
        return None

    def close(self, reason: str = "episode_end") -> None:
        if self.closed:
            return
        self.closed = True
        if self.ws is not None and not self.exited:
            try:
                self.ws.send(T.CLOSE, {"reason": reason})
                self.ws.recv(time.monotonic() + 2.0)         # 可选的 BYE
            except (OSError, TimeoutError, ConnectionError, WireError):
                pass
        if self.ws is not None:
            self.ws.close()
        self.lsock.close()
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()

    def _error(self, code: int, why: str) -> None:
        if self.ws is not None:
            with contextlib.suppress(OSError):
                self.ws.send(T.ERROR, {"code": code, "why": why})

    def _protocol_error(self, why: str) -> None:
        self.stats["protocol_errors"] += 1
        self._errs += 1
        self._error(666, why)
        if self._errs >= 3:
            self.exited = "protocol"


def run_remote_episode(env, hosts: Mapping[str, SocketAgentHost], *, step_budget_s: float | None = None) -> tuple[dict, dict]:
    """hosts 须已 `accept()`；→ (env.close() 的运行清单, 每机线协议统计)。"""
    obs, infos = env.reset()
    for u, h in hosts.items():
        h.configure(infos[u]["config"])
    budget = float(step_budget_s if step_budget_s is not None else infos[next(iter(hosts))]["config"]["decision"].get("step_budget_s", 2.0))
    reason = "episode_end"
    try:
        while env.agents:
            sent = [u for u, o in obs.items() if u in env.agents and hosts[u].send_obs(o)]
            deadline = time.monotonic() + budget
            acts = {}
            for u in sent:
                a = hosts[u].recv_act(deadline)
                if a is not None:
                    acts[u] = a
            obs, _r, _t, _tr, _i = env.step(acts)
    except BaseException:
        reason = "run_failed"
        raise
    finally:
        manifest = env.close()
        for h in hosts.values():
            h.close(reason)
    return manifest, {u: {**h.stats, "exited": h.exited} for u, h in hosts.items()}
