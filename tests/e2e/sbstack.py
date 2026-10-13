"""真实沙盒栈（WP-13 的 e2e 与 chaos 共用）：以 `sandbox` profile 的真实 supervisor 拉起共享展示 sim-core、show-api、sandbox-pool
与 sandbox-api（空闲端口、临时 runs 与沙盒状态目录），池再以 `python -m awr.sim.runtime`（sandbox100、`AWR_SESSION_CONFIG`）拉起
每个会话的真实 sim-core（M17 PRD §10.1 "真实 sim-core 版本"；d2-WP-04-to-M16 第 3 条）。

`SandboxStack(...).start()` 返回后 `base` 为 sandbox-api 根地址（SDK `Sandbox(base)`），`show_base` 为 show-api；`pid_of(name)` 读
supervisor 进程表；`stop()` 以 SIGTERM 停止进程组并删除临时目录。沙盒会话的 sim-core 由池以 setsid 拉起，停止时池以维护原因
结束全部会话；`kill_sessions()` 兜底清理残留（按环境 `AWR_RUN=<sid>` 核对，不误杀）。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from awrproc import ROOT, SHM, free_port, numba_warm

SDK = ROOT / "tools" / "sdk"


@dataclass
class SandboxStack:
    world: str = "synthcity"
    max_sessions: int = 2
    profile: str = "sandbox"
    only: str = "sim-core,api,sandbox-pool,sandbox-api"
    extra_set: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    ready_timeout_s: float = 150.0
    # 运行态
    proc: subprocess.Popen | None = None
    base: str = ""
    show_base: str = ""
    tmp: Path | None = None
    run_id: str = ""
    log: list[str] = field(default_factory=list)

    def start(self) -> SandboxStack:
        import httpx

        self.tmp = Path(tempfile.mkdtemp(prefix="awr-m16-sbx-"))
        port, sport, bus = free_port(), free_port(), free_port()
        cmd = [sys.executable, "-m", "awr.runtime.supervisor", "--profile", self.profile,
               "--set", "net.port_offset=0", "--set", f"net.port={port}", "--set", f"bus.rendezvous=tcp/127.0.0.1:{bus}",
               "--set", f"sandbox.port={sport}", "--set", f"sandbox.state_dir={self.tmp / 'sbx'}",
               "--set", f"sandbox.max_sessions={self.max_sessions}", "--set", "run.keep_run_dir=false", "--only", self.only]
        for kv in self.extra_set:
            cmd += ["--set", kv]
        env = dict(os.environ, AWR_RUNS_DIR=str(self.tmp / "runs"), PYTHONUNBUFFERED="1", AWR_WORLD=self.world, **self.env)
        for k in ("AWR_SUPERVISOR_PID", "AWR_PORT_OFFSET", "AWR_SCENARIO", "AWR_PLUGINS", "AWR_SESSION_CONFIG"):
            env.pop(k, None)
        numba_warm(env)
        self.proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     start_new_session=True)
        ready = threading.Event()

        def pump() -> None:
            assert self.proc is not None and self.proc.stdout is not None
            for line in self.proc.stdout:
                self.log.append(line.rstrip())
                if line.startswith("READY") and not ready.is_set():
                    self.run_id = next((x.split("=", 1)[1] for x in line.split() if x.startswith("run=")), "")
                    ready.set()

        threading.Thread(target=pump, daemon=True).start()
        if not ready.wait(self.ready_timeout_s) or self.proc.poll() is not None:
            tail = "\n".join(self.log[-40:])
            self.stop()
            raise RuntimeError(f"supervisor did not print READY within {self.ready_timeout_s} s:\n{tail}")
        self.base = f"http://127.0.0.1:{sport}"
        self.show_base = f"http://127.0.0.1:{port}"
        end = time.monotonic() + 60.0
        last = ""
        while time.monotonic() < end:
            try:
                r = httpx.get(self.base + "/api/sandbox/v1/health", timeout=2.0)
                if r.status_code == 200 and r.json().get("pool") == "up":
                    return self
                last = r.text[:200]
            except httpx.HTTPError as e:
                last = str(e)
            time.sleep(0.25)
        self.stop()
        raise TimeoutError(f"sandbox-api not healthy: {last}")

    # ------------------------------------------------------------ 观测
    def health(self) -> dict:
        import httpx

        return httpx.get(self.base + "/api/sandbox/v1/health", timeout=3.0).json()

    def admin_token(self) -> str:
        import httpx

        assert self.tmp is not None
        secret = (self.tmp / "runs" / self.run_id / "admin.token").read_text().strip()
        r = httpx.post(self.show_base + "/api/auth/token", json={"role": "admin", "principal_hint": "M16SBXADMINHINTAAAAAAAAA",
                                                                 "admin_secret": secret}, timeout=10)
        r.raise_for_status()
        return str(r.json()["token"])

    def procs(self) -> list[dict]:
        import httpx

        r = httpx.get(self.show_base + "/api/sys/procs", headers={"Authorization": "Bearer " + self.admin_token()}, timeout=10)
        r.raise_for_status()
        return list(r.json().get("items") or [])

    def pid_of(self, name: str, timeout_s: float = 30.0) -> int:
        """supervisor 进程表中的 pid；进程重启期间 pid 为空时等待（≤ timeout_s）。"""
        end = time.monotonic() + timeout_s
        while True:
            pid = next((p.get("pid") for p in self.procs() if p.get("name") == name), None)
            if pid or time.monotonic() > end:
                return int(pid or 0)
            time.sleep(0.2)

    def session_pid(self, sid: str) -> int | None:
        """池拉起的会话 sim-core（环境 `AWR_RUN = <sid>`）。"""
        for d in Path("/proc").iterdir():
            if not d.name.isdigit():
                continue
            try:
                env = (d / "environ").read_bytes().split(b"\0")
            except OSError:
                continue
            if f"AWR_RUN={sid}".encode() in env and b"AWR_DETACHED=1" in env:
                return int(d.name)
        return None

    def kill_sessions(self) -> None:
        for d in Path("/proc").iterdir():
            if not d.name.isdigit():
                continue
            try:
                env = (d / "environ").read_bytes().split(b"\0")
            except OSError:
                continue
            if self.tmp is not None and any(x.startswith(b"AWR_SESSION_CONFIG=" + str(self.tmp).encode()) for x in env):
                with contextlib.suppress(OSError):
                    os.kill(int(d.name), signal.SIGKILL)

    def proc_log(self, name: str) -> str:
        if self.tmp is None:
            return ""
        p = self.tmp / "runs" / self.run_id / "logs" / f"{name}.log"
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    # ------------------------------------------------------------ D2.1（WP-13b）
    @property
    def sbx_dir(self) -> Path:
        """池状态目录（`sandbox.state_dir`）：`state.json`、`audit.jsonl`、`parked/<sid>/`、`runs/<sid>/`、`snapshots/`。"""
        assert self.tmp is not None
        return self.tmp / "sbx"

    def parked_dir(self, sid: str) -> Path:
        return self.sbx_dir / "parked" / sid

    def shm_dir(self, sid: str) -> Path:
        return Path(self.env.get("AWR_SHM_ROOT") or str(SHM)) / sid

    def capacity(self) -> dict:
        import httpx

        return httpx.get(self.base + "/api/sandbox/v1/capacity", timeout=5.0).json()

    def session(self, sid: str, token: str) -> dict:
        import httpx

        return httpx.get(self.base + f"/api/sandbox/v1/sessions/{sid}", headers={"authorization": "Bearer " + token}, timeout=10.0).json()

    def pool_audit(self) -> list[dict]:
        import json

        out = []
        for name in ("audit.jsonl", "audit-api.jsonl"):
            p = self.sbx_dir / name
            with contextlib.suppress(OSError):
                for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                    with contextlib.suppress(ValueError):
                        out.append(json.loads(line))
        return out

    # ------------------------------------------------------------ 停止
    def stop(self) -> None:
        p = self.proc
        if p is not None and p.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(40)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(p.pid, signal.SIGKILL)
                p.wait(10)
        self.kill_sessions()
        if self.tmp is not None:
            shutil.rmtree(self.tmp, ignore_errors=True)
        if self.run_id:
            shutil.rmtree(SHM / self.run_id, ignore_errors=True)

    def __enter__(self) -> SandboxStack:
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()


def sdk():
    """`tools/sdk/awr_sandbox.py`（单文件 SDK，不依赖 awr 包）。"""
    if str(SDK) not in sys.path:
        sys.path.insert(0, str(SDK))
    import awr_sandbox

    return awr_sandbox


def release_file_from(build_info: Path, d: Path) -> Path:
    """由前端 `build-info.json` 写部署目录形式的 `RELEASE`（WP-16：`release=`、`version=`、`channel=`、`commit=`、`dirty=`、
    `built_at=`），供 `AWR_RELEASE_FILE`：容量接口的 `build` 与该构建逐字段相同（D2-AC-40 一致性用例）。"""
    import json

    b = json.loads(Path(build_info).read_text(encoding="utf-8"))
    p = Path(d) / "RELEASE"
    p.write_text("".join(f"{k}={str(b[k]).lower() if isinstance(b[k], bool) else b[k]}\n"
                         for k in ("release", "version", "channel", "commit", "dirty", "built_at")), encoding="utf-8")
    return p


def main(argv: list[str] | None = None) -> int:
    """命令行启动（Playwright 的 live 用例经子进程使用）：打印一行 JSON `{base, show_base, run_id, tmp, sbx_dir, identity_url}`，
    标准输入关闭或收到 SIGTERM 后停止栈。`--max-sessions n`、`--world w`、`--set K=V`（supervisor 配置覆盖）、`--env K=V`（栈进程
    的环境，例如 `AWR_POWER_JSON`、`AWR_SANDBOX_JSON`、`AWR_ADMIN_SECRET_FILE`、`AWR_IDENTITY_STATE_DIR`）、`--release-from
    <build-info.json>`（D2.1：以该构建写 RELEASE）、`--identity-standin`（D2.1：show-api 尚无 R79 时启动 `identity_standin`）、
    `--profile p`（缺省 sandbox）。"""
    import argparse
    import json

    ap = argparse.ArgumentParser(prog="tests/e2e/sbstack.py")
    ap.add_argument("--max-sessions", type=int, default=2)
    ap.add_argument("--world", default="synthcity")
    ap.add_argument("--set", action="append", default=[], help="supervisor 配置覆盖（KEY=VALUE，可重复）")
    ap.add_argument("--env", action="append", default=[], help="栈进程的环境变量（KEY=VALUE，可重复）")
    ap.add_argument("--release-from", default=None, help="前端 build-info.json：写 RELEASE 并设置 AWR_RELEASE_FILE")
    ap.add_argument("--identity-standin", action="store_true", help="show-api 未提供 R79 时启动身份端点替身")
    ap.add_argument("--profile", default="sandbox", help="supervisor profile（缺省 sandbox；public 为公开演示站 v2，D2-INT 本机全栈演示）")
    a = ap.parse_args(argv)
    env = dict(kv.split("=", 1) for kv in a.env if "=" in kv)
    rel_dir = None
    if a.release_from:
        rel_dir = Path(tempfile.mkdtemp(prefix="awr-m16-rel-"))
        env["AWR_RELEASE_FILE"] = str(release_file_from(Path(a.release_from), rel_dir))
    st = SandboxStack(world=a.world, max_sessions=a.max_sessions, profile=a.profile, extra_set=list(a.set), env=env).start()
    standin = None
    if a.identity_standin:
        from identity_standin import IdentityStandin, real_r79

        if not real_r79(st.show_base):
            standin = IdentityStandin({**os.environ, **env}).start()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    assert st.tmp is not None
    print(json.dumps({"base": st.base, "show_base": st.show_base, "run_id": st.run_id, "tmp": str(st.tmp),
                      "sbx_dir": str(st.tmp / "sbx"), "identity_url": standin.url if standin else None}), flush=True)

    def wait_stdin() -> None:
        with contextlib.suppress(Exception):
            sys.stdin.read()
        stop.set()

    threading.Thread(target=wait_stdin, daemon=True).start()
    try:
        while not stop.wait(0.5):
            pass
    finally:
        if standin is not None:
            standin.stop()
        st.stop()
        if rel_dir is not None:
            shutil.rmtree(rel_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main())
