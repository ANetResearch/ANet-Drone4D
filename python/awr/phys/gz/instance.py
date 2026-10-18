"""gz 实例（M24 §6.8.1、§6.9；每 Run 重启）：一个 `d3-gz` 容器运行 `gz sim -s -r run.sdf`，控制套接字在运行目录的 `sock/`
（容器内 /run/d4d）。宿主与容器以同一 uid 运行，ipc 套接字经绑定挂载共享；容器无网络（GZ_IP=127.0.0.1）。

`GzInstance.start()` 写出 run.sdf 并启动容器，`wait_socket()` 等待 Drone4DClock 绑定控制端点（631）；`stop()` 停止并删除容器。
`launcher = "local"` 时在本进程所在环境（例如 d3-gz 容器内的宿主侧测试）以子进程运行 `gz sim`，不经 docker。
`dry_run = True` 时只返回命令行（测试与日志）。

`endpoint` 为宿主侧端点，`gz_endpoint` 为写入 run.sdf 的 gz 侧端点（docker 时为容器内路径）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from awr.phys import PhysError

__all__ = ["GzInstance", "host_path"]


def host_path(p: Path, mapping: str | None = None) -> str:
    """容器内路径 → 宿主路径（`AWR_HOST_PATH_MAP="/repo=/data/…/d3dev"`，逗号分隔多对，按前缀换算）：本进程在容器中经宿主
    docker 启动兄弟容器时，`-v` 的源路径须为宿主路径。"""
    s = str(Path(p).resolve())
    for pair in (mapping if mapping is not None else os.environ.get("AWR_HOST_PATH_MAP", "")).split(","):
        src, _, dst = pair.partition("=")
        if src and dst and (s == src or s.startswith(src.rstrip("/") + "/")):
            return dst.rstrip("/") + s[len(src.rstrip("/")):]
    return s


@dataclass
class GzInstance:
    run_dir: Path
    name: str
    image: str = "d3-gz:dev"
    cpus: float = 2.0
    mem_gb: float = 2.0
    world_export: Path | None = None                                # 世界级导出目录（含 world_static/）；docker 时挂载到 /opt/d4d/worlds
    extra_mounts: dict[str, str] = field(default_factory=dict)      # 宿主路径 → 容器路径（只读）
    verbose: int = 1
    docker: str = "docker"
    launcher: str = "docker"                                       # docker | local
    dry_run: bool = False
    _proc: subprocess.Popen | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        # gz 按 GZ_SIM_RESOURCE_PATH 解析 model:// 时，相对路径会与模型目录二次拼接（网格找不到、静默无碰撞）；docker 挂载同样要求绝对路径
        self.run_dir = Path(self.run_dir).resolve()
        if self.world_export is not None:
            self.world_export = Path(self.world_export).resolve()

    @property
    def sock_dir(self) -> Path:
        return Path(self.run_dir) / "sock"

    @property
    def endpoint(self) -> str:
        return f"ipc://{self.sock_dir / 'ctl.sock'}"

    @property
    def gz_endpoint(self) -> str:
        return self.endpoint if self.launcher == "local" else "ipc:///run/d4d/ctl.sock"

    def command(self) -> list[str]:
        if self.launcher == "local":
            return ["gz", "sim", "-s", "-r", "-v", str(self.verbose), str(Path(self.run_dir) / "run.sdf")]
        uid = f"{os.getuid()}:{os.getgid()}"
        cmd = [self.docker, "run", "-d", "--rm", "--name", self.name, "--user", uid, "--network", "none",
               "--cpus", f"{self.cpus:g}", "--memory", f"{self.mem_gb:g}g", "-e", "HOME=/tmp", "-e", "GZ_IP=127.0.0.1",
               "-v", f"{host_path(self.sock_dir)}:/run/d4d", "-v", f"{host_path(Path(self.run_dir) / 'run.sdf')}:/run/d4d-sdf/run.sdf:ro"]
        if self.world_export is not None:
            cmd += ["-v", f"{host_path(self.world_export)}:/opt/d4d/worlds:ro", "-e", "GZ_SIM_RESOURCE_PATH=/opt/d4d/models:/opt/d4d/worlds"]
        for host, ctr in sorted(self.extra_mounts.items()):
            cmd += ["-v", f"{host_path(Path(host))}:{ctr}:ro"]
        return [*cmd, self.image, "gz", "sim", "-s", "-r", "-v", str(self.verbose), "/run/d4d-sdf/run.sdf"]

    def start(self, sdf_text: str) -> list[str]:
        self.sock_dir.mkdir(parents=True, exist_ok=True)
        (Path(self.run_dir) / "run.sdf").write_text(sdf_text, encoding="utf-8")
        for p in self.sock_dir.glob("*.sock"):
            p.unlink()
        cmd = self.command()
        if self.dry_run:
            return cmd
        if self.launcher == "local":
            env = {**os.environ, "GZ_IP": "127.0.0.1"}
            if self.world_export is not None:
                env["GZ_SIM_RESOURCE_PATH"] = ":".join(p for p in (env.get("GZ_SIM_RESOURCE_PATH", ""), str(self.world_export)) if p)
            log = (Path(self.run_dir) / "gz.log").open("wb")
            self._proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
            return cmd
        if shutil.which(self.docker) is None:
            raise PhysError(630, "docker is not available on this host")
        r = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if r.returncode != 0:
            raise PhysError(631, f"gz container failed to start: {r.stderr.strip()[:400]}")
        return cmd

    def wait_socket(self, timeout_s: float = 60.0) -> None:
        sock = self.sock_dir / "ctl.sock"
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout_s:
            if sock.exists():
                miss = [ln for ln in self.logs().splitlines() if "Unable to find file" in ln]
                if miss:                                            # 世界网格缺失时 gz 照常运行但没有碰撞体
                    raise PhysError(638, f"gz could not load {len(miss)} mesh file(s): {miss[0][-300:]}")
                return
            if not self.alive():
                raise PhysError(638, f"gz container exited before binding the control socket: {self.logs()[-600:]}")
            time.sleep(0.1)
        raise PhysError(631, f"control socket not ready after {timeout_s:g} s")

    def alive(self) -> bool:
        if self.launcher == "local":
            return self._proc is not None and self._proc.poll() is None
        r = subprocess.run([self.docker, "inspect", "-f", "{{.State.Running}}", self.name], capture_output=True, text=True, check=False)
        return r.returncode == 0 and r.stdout.strip() == "true"

    def logs(self) -> str:
        if self.launcher == "local":
            p = Path(self.run_dir) / "gz.log"
            return p.read_text(errors="replace") if p.exists() else ""
        r = subprocess.run([self.docker, "logs", self.name], capture_output=True, text=True, check=False)
        return (r.stdout or "") + (r.stderr or "")

    def stop(self, timeout_s: float = 10.0) -> None:
        if self.dry_run:
            return
        if self.launcher == "local":
            if self._proc is not None and self._proc.poll() is None:
                os.killpg(self._proc.pid, 15)
                try:
                    self._proc.wait(timeout_s)
                except subprocess.TimeoutExpired:
                    os.killpg(self._proc.pid, 9)
                    self._proc.wait()
            return
        subprocess.run([self.docker, "rm", "-f", self.name], capture_output=True, check=False)
