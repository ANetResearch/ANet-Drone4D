"""容器 Agent 的启动与回收（AWR-17 §18.9.1；M26 §7.2；ADR-161）：每架机一个隔离容器，经只挂载进该容器的套接字目录与 Env 通信。

容器参数：`--network none`、只读根文件系统（`/tmp` 为 tmpfs）、`--cap-drop ALL`、`no-new-privileges`、非 root 用户（65534）、
CPU、内存与进程数上限；入口为镜像缺省 CMD 或提交的 `entry.cmd`，`entry.env` 以 `-e` 传入（键须为大写标识符）。
运行器本身在容器中（dmax 的 d3-gz）时，`docker -v` 的源路径须为宿主路径：`AWR_HOST_PATH_MAP="/repo=/data/…/d3dev"` 按前缀换算。
Unix 套接字路径上限约 108 字节：运行目录较深时以 `AWR_AGENT_SOCK_ROOT` 指定短根目录（每机子目录 `<run 摘要>-<uav>`）。
回收时把各容器日志尾部（64 KiB）写入 `agents/<uav>/container.log`。
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

from awr.agentsdk.env.remote import SocketAgentHost, run_remote_episode
from awr.agentsdk.wire import WireError
from awr.phys.gz.instance import host_path

__all__ = ["AgentContainers", "host_path", "run_container_episode"]

_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
LOG_TAIL_B = 64 * 1024


class AgentContainers:
    def __init__(self, run_dir: Path, run_id: str, uavs: Sequence[str], image: str, *, cmd: Sequence[str] | None = None,
                 env: dict[str, str] | None = None, cpus: float = 1.0, mem_gb: float = 2.0, docker: str = "docker",
                 hello_timeout_s: float = 60.0, ready_timeout_s: float = 30.0) -> None:
        for k in env or {}:
            if not _ENV_KEY.match(k) or k.startswith("DRONE4D_"):
                raise ValueError(f"entry.env key {k!r} is not allowed")
        self.run_dir, self.run_id, self.uavs, self.image = Path(run_dir), run_id, list(uavs), image
        self.cmd, self.env, self.cpus, self.mem_gb, self.docker = list(cmd or []), dict(env or {}), cpus, mem_gb, docker
        self.hello_timeout_s, self.ready_timeout_s = hello_timeout_s, ready_timeout_s
        self.hosts: dict[str, SocketAgentHost] = {}
        self.names: dict[str, str] = {}

    def name(self, uav: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]", "-", f"d3-agent-{self.run_id}-{uav}")[:120]

    def start(self) -> None:
        root = os.environ.get("AWR_AGENT_SOCK_ROOT")
        tag = hashlib.sha256(f"{self.run_dir.resolve()}|{self.run_id}".encode()).hexdigest()[:10]
        for u in self.uavs:
            d = Path(root) / f"{tag}-{u}" if root else self.run_dir / "agent_sock" / u
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o755)
            self.hosts[u] = SocketAgentHost(u, d / "agent.sock", hello_timeout_s=self.hello_timeout_s, ready_timeout_s=self.ready_timeout_s)
            name = self.name(u)
            subprocess.run([self.docker, "rm", "-f", name], capture_output=True, check=False)
            argv = [self.docker, "run", "-d", "--name", name, "--label", f"d3.run={self.run_id}", "--network", "none", "--read-only",
                    "--tmpfs", "/tmp:rw,size=256m", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "256",
                    "--cpus", f"{self.cpus:g}", "--memory", f"{self.mem_gb:g}g", "--user", "65534:65534",
                    "-v", f"{host_path(d)}:/run/drone4d", "-e", "DRONE4D_AGENT_SOCK=/run/drone4d/agent.sock"]
            for k, v in sorted(self.env.items()):
                argv += ["-e", f"{k}={v}"]
            r = subprocess.run([*argv, self.image, *self.cmd], capture_output=True, text=True, check=False)
            if r.returncode != 0:
                self.stop()
                raise WireError(664, f"container_start: {r.stderr.strip()[-300:]}")
            self.names[u] = name

    def accept(self) -> None:
        try:
            for h in self.hosts.values():
                h.accept()
        except WireError:
            self.stop()
            raise

    def stop(self) -> dict[str, int | None]:
        """CLOSE 之后 Agent 应自行退出：等待 ≤ 10 s，否则强制结束；→ 每机容器退出码。"""
        for h in self.hosts.values():
            h.close("run_failed")
        codes: dict[str, int | None] = {}
        for u, name in self.names.items():
            try:
                subprocess.run([self.docker, "wait", name], capture_output=True, timeout=10, check=False)
            except subprocess.TimeoutExpired:
                subprocess.run([self.docker, "kill", name], capture_output=True, check=False)
            ins = subprocess.run([self.docker, "inspect", "-f", "{{.State.ExitCode}}", name], capture_output=True, text=True, check=False)
            code = ins.stdout.strip()
            codes[u] = int(code) if ins.returncode == 0 and code.lstrip("-").isdigit() else None
            logs = subprocess.run([self.docker, "logs", name], capture_output=True, check=False)
            out = self.run_dir / "agents" / u
            out.mkdir(parents=True, exist_ok=True)
            (out / "container.log").write_bytes((logs.stdout + logs.stderr)[-LOG_TAIL_B:])
            subprocess.run([self.docker, "rm", "-f", name], capture_output=True, check=False)
        self.names.clear()
        return codes


def run_container_episode(env, run_dir: Path, image: str, *, cmd: Sequence[str] | None = None, entry_env: dict[str, str] | None = None,
                          cpus: float = 1.0, mem_gb: float = 2.0, step_budget_s: float | None = None, docker: str = "docker",
                          hello_timeout_s: float = 60.0) -> tuple[dict, dict]:
    """→ (运行清单, 每机线协议统计与容器退出码)。容器在 Env reset 之前启动并完成 HELLO（Env 预滚期间 Agent 已就绪）。"""
    ac = AgentContainers(run_dir, env.spec.run_id, env.possible_agents, image, cmd=cmd, env=entry_env, cpus=cpus, mem_gb=mem_gb, docker=docker,
                         hello_timeout_s=hello_timeout_s)
    ac.start()
    try:
        ac.accept()
        manifest, stats = run_remote_episode(env, ac.hosts, step_budget_s=step_budget_s)
    finally:
        codes = ac.stop()
    return manifest, {u: {**stats.get(u, {}), "exit_code": codes.get(u)} for u in env.possible_agents}
