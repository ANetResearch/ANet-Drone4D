"""gz 实例（M24 §6.8.1、§6.9；每 Run 重启）：一个 `d3-gz` 容器运行 `gz sim -s -r run.sdf`，控制套接字在运行目录的 `sock/`
（容器内 /run/d4d）。宿主与容器以同一 uid 运行，ipc 套接字经绑定挂载共享；容器无网络（GZ_IP=127.0.0.1；GZ_PARTITION 每实例独立，缺省取容器名）。

`GzInstance.start()` 写出 run.sdf 并启动容器，`wait_socket()` 等待 Drone4DClock 绑定控制端点（631）；`stop()` 停止并删除容器。
`launcher = "local"` 时在本进程所在环境（例如 d3-gz 容器内的宿主侧测试）以子进程运行 `gz sim`，不经 docker。
`dry_run = True` 时只返回命令行（测试与日志）。

`endpoint` 为宿主侧端点，`gz_endpoint` 为写入 run.sdf 的 gz 侧端点（docker 时为容器内路径）。

故障恢复与注入（M24-FR-078、§6.8.1 FAILED → STOPPED；D3-AC-17、35）：
- `restart()`：按进程组回收（`stop(force=True)`：SIGKILL / `docker rm -f` 并等待容器名释放），以同一 run.sdf 重新启动并等待控制端点；
  上一次的 gz 日志保留为 `gz.attempt<N>.log`，`restarts` 计数。宿主侧"从回合起点重放"由 `awr.phys.gz.backend.SupervisedGzBackend` 完成。
- `kill()`：SIGKILL 整个进程组（docker 为 `docker kill -s KILL`），模拟 gz 进程异常退出（638）。
- `freeze()` / `thaw()`：SIGSTOP / SIGCONT 进程组（docker 为 `pause` / `unpause`），模拟步进挂起（看门狗 632）。
- `request_world_reset()`：经 gz 服务 `/world/<w>/control` 请求 `reset {all: true}`，只用于同种子 reset 复用验证（M24-FR-060）。
"""

from __future__ import annotations

import contextlib
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
    partition: str = ""                                            # GZ_PARTITION；缺省取容器名（每 Run 唯一，§4.6、M24-FR-071）
    _proc: subprocess.Popen | None = field(default=None, init=False, repr=False)
    _log: object | None = field(default=None, init=False, repr=False)
    restarts: int = field(default=0, init=False)                   # restart() 次数（同一 Run 内）

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

    @property
    def gz_partition(self) -> str:
        return self.partition or self.name

    def local_env(self) -> dict[str, str]:
        """`launcher = "local"` 时 gz 子进程的环境：独立 GZ_PARTITION、GZ_IP = 127.0.0.1，世界导出目录追加到资源路径。"""
        env = {**os.environ, "GZ_IP": "127.0.0.1", "GZ_PARTITION": self.gz_partition}
        if self.world_export is not None:
            env["GZ_SIM_RESOURCE_PATH"] = ":".join(p for p in (env.get("GZ_SIM_RESOURCE_PATH", ""), str(self.world_export)) if p)
        return env

    def command(self) -> list[str]:
        if self.launcher == "local":
            return ["gz", "sim", "-s", "-r", "-v", str(self.verbose), str(Path(self.run_dir) / "run.sdf")]
        uid = f"{os.getuid()}:{os.getgid()}"
        cmd = [self.docker, "run", "-d", "--rm", "--name", self.name, "--user", uid, "--network", "none",
               "--cpus", f"{self.cpus:g}", "--memory", f"{self.mem_gb:g}g", "-e", "HOME=/tmp", "-e", "GZ_IP=127.0.0.1",
               "-e", f"GZ_PARTITION={self.gz_partition}",
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
            env = self.local_env()
            self._log = (Path(self.run_dir) / "gz.log").open("wb")
            self._proc = subprocess.Popen(cmd, stdout=self._log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
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

    def stop(self, timeout_s: float = 10.0, *, force: bool = False) -> None:
        """正常停止：SIGTERM 进程组，超时后 SIGKILL；`force = True`（FAILED → STOPPED）直接 SIGKILL，挂起（SIGSTOP）的进程也能回收。
        docker 一律 `docker rm -f`，并等待容器名释放，以便同名重启。"""
        if self.dry_run:
            return
        if self.launcher == "local":
            if self._proc is not None and self._proc.poll() is None:
                self._signal(9 if force else 15)
                if not force:
                    self._signal(18)                                # SIGCONT：已挂起的进程组也要处理 SIGTERM
                try:
                    self._proc.wait(timeout_s)
                except subprocess.TimeoutExpired:
                    self._signal(9)
                    self._proc.wait()
            if self._log is not None:
                self._log.close()
                self._log = None
            return
        subprocess.run([self.docker, "rm", "-f", self.name], capture_output=True, check=False)
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout_s:                    # --rm 的自动删除可能仍在进行：等到名称可用
            r = subprocess.run([self.docker, "inspect", "-f", "{{.State.Status}}", self.name], capture_output=True, text=True, check=False)
            if r.returncode != 0:
                return
            time.sleep(0.1)

    # ------------------------------------------------------------ 故障恢复与注入
    def restart(self, ready_timeout_s: float = 60.0) -> None:
        """FAILED → STOPPED → 以同一 run.sdf 重新启动并等待控制端点（631 / 638 照常上抛）。"""
        sdf = (Path(self.run_dir) / "run.sdf").read_text(encoding="utf-8")
        self.stop(force=True)
        self.restarts += 1
        if self.launcher == "local":
            log = Path(self.run_dir) / "gz.log"
            if log.exists():
                log.replace(Path(self.run_dir) / f"gz.attempt{self.restarts - 1}.log")
        self.start(sdf)
        if not self.dry_run:
            self.wait_socket(ready_timeout_s)

    def _signal(self, sig: int) -> None:
        if self._proc is None:
            raise PhysError(631, "gz instance is not running")
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self._proc.pid, sig)

    def _docker(self, *args: str) -> None:
        r = subprocess.run([self.docker, *args], capture_output=True, text=True, check=False)
        if r.returncode != 0:
            raise PhysError(631, f"docker {args[0]} {self.name} failed: {r.stderr.strip()[:300]}")

    def kill(self) -> None:
        """故障注入：gz 进程异常退出（`kill -9` 进程组）。"""
        if self.launcher == "local":
            self._signal(9)
        else:
            self._docker("kill", "-s", "KILL", self.name)

    def freeze(self) -> None:
        """故障注入：步进挂起（进程存活、不再回复 StepDone）。"""
        if self.launcher == "local":
            self._signal(19)
        else:
            self._docker("pause", self.name)

    def thaw(self) -> None:
        if self.launcher == "local":
            self._signal(18)
        else:
            self._docker("unpause", self.name)

    def world_reset_command(self, world: str = "d4d", timeout_ms: int = 3000) -> list[str]:
        svc = ["gz", "service", "-s", f"/world/{world}/control", "--reqtype", "gz.msgs.WorldControl", "--reptype", "gz.msgs.Boolean",
               "--timeout", str(int(timeout_ms)), "--req", "reset: {all: true}"]
        return svc if self.launcher == "local" else [self.docker, "exec", self.name, *svc]

    def request_world_reset(self, world: str = "d4d", timeout_ms: int = 3000) -> tuple[bool, str]:
        """经 `/world/<w>/control` 请求世界复位（M24-FR-060 验证用）→ (服务应答 data: true, 输出)。"""
        env = self.local_env() if self.launcher == "local" else None
        try:
            r = subprocess.run(self.world_reset_command(world, timeout_ms), capture_output=True, text=True, check=False, env=env,
                               timeout=timeout_ms / 1000 + 10)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, f"{type(e).__name__}: {e}"
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        return r.returncode == 0 and "data: true" in out, out[-400:]
