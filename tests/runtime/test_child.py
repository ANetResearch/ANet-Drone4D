"""子进程侧运行时 init_child 与 JSON 行日志（M11-AC-005 子项；M11-FR-011；AWR-19 §11.1）。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import rtlib

CHILD = textwrap.dedent("""
    import json, sys, time, logging
    from pathlib import Path
    from awr.runtime.child import init_child
    ctx = init_child("fake")
    log = logging.getLogger("awr.test")
    log.info("hello", extra={"kv": {"token": "v1.secret", "n": 1, "auth": "bearer.abc"}})
    for i in range(5):
        log.info("same message", extra={"kv": {"i": i}})
    print(json.dumps({"run": ctx.run_id, "restart": ctx.restart_count, "last": ctx.last_exit, "ns": ctx.namespace,
                      "id": [ctx.id_base, ctx.id_count], "sup": ctx.supervisor_pid}), flush=True)
    while not ctx.stopping:
        time.sleep(0.01)
    Path(sys.argv[1]).write_text("stopped")
""")


def spawn_parent(tmp: Path, extra_env: dict[str, str]) -> subprocess.Popen:
    """中间"supervisor"进程：以自身 pid 作为 AWR_SUPERVISOR_PID 启动 CHILD，打印子进程 pid 后等待。"""
    marker = tmp / "stopped"
    child_py = tmp / "child.py"
    child_py.write_text(CHILD)
    parent = textwrap.dedent(f"""
        import os, subprocess, sys, time
        env = dict(os.environ, AWR_SUPERVISOR_PID=str(os.getpid()))
        p = subprocess.Popen([sys.executable, {str(child_py)!r}, {str(marker)!r}], env=env,
                             stdout=subprocess.PIPE, stderr=open({str(tmp / 'child.err')!r}, 'wb'), text=True)
        print(p.pid, flush=True)
        print(p.stdout.readline().strip(), flush=True)
        time.sleep(60)
    """)
    env = rtlib.child_env(**extra_env)
    return subprocess.Popen([sys.executable, "-c", parent], stdout=subprocess.PIPE, text=True, env=env)


def test_runctx_from_env_and_pdeathsig(tmp_path: Path) -> None:
    env = {"AWR_RUN": "r20260928-143200-a3f1", "AWR_WORLD": "shenzhen", "AWR_RESTART_COUNT": "2", "AWR_LAST_EXIT": "-9",
           "AWR_ID_BASE": "0", "AWR_ID_COUNT": "1024", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
           "MKL_NUM_THREADS": "1", "AWR_RUN_DIR": str(tmp_path)}
    parent = spawn_parent(tmp_path, env)
    try:
        child_pid = int(parent.stdout.readline())
        info = json.loads(parent.stdout.readline())
        assert info == {"run": "r20260928-143200-a3f1", "restart": 2, "last": -9, "ns": "awr/shenzhen/r20260928-143200-a3f1",
                        "id": [0, 1024], "sup": parent.pid}
        os.kill(parent.pid, signal.SIGKILL)  # supervisor 被杀：子进程经 PDEATHSIG 收到 SIGTERM，≤ 1 s 退出
        parent.wait(5)
        t0 = time.monotonic()
        assert rtlib.wait_until(lambda: not Path(f"/proc/{child_pid}").exists() or _zombie(child_pid), 3)
        assert time.monotonic() - t0 < 1.0
        assert (tmp_path / "stopped").read_text() == "stopped"  # SIGTERM 只置 stopping，主循环自行收尾
        lines = [json.loads(x) for x in (tmp_path / "child.err").read_text().splitlines() if x.startswith("{")]
        hello = next(x for x in lines if x["msg"] == "hello")
        for k in ("t_wall_ns", "t_mono_ns", "lvl", "proc", "pid", "run", "logger", "msg"):
            assert k in hello
        assert hello["kv"] == {"token": "***", "n": 1, "auth": "***"}  # 掩码：token 键与 bearer. 前缀
        same = [x for x in lines if x["msg"] == "same message"]
        assert len(same) == 1  # 限速：同一 (logger, msg) 每秒至多 1 条
        assert "v1.secret" not in (tmp_path / "child.err").read_text()
    finally:
        if parent.poll() is None:
            parent.kill()


def _zombie(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")")[1].split()[0] == "Z"
    except OSError:
        return True


def test_orphan_exits_3_and_blas_assert(tmp_path: Path) -> None:
    code = "from awr.runtime.child import init_child; init_child('x'); print('ok')"
    env = rtlib.child_env(AWR_SUPERVISOR_PID="1", OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 3 and "ok" not in r.stdout  # 父进程不是 supervisor：已成孤儿
    env = rtlib.child_env(OMP_NUM_THREADS="4")  # 在 supervisor 下 BLAS 线程变量必须为 1
    wrapper = ("import subprocess,sys,os; e=dict(os.environ, AWR_SUPERVISOR_PID=str(os.getpid()));"
               f"sys.exit(subprocess.run([sys.executable,'-c',{code!r}], env=e).returncode)")
    r = subprocess.run([sys.executable, "-c", wrapper], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode != 0 and "BLAS" in r.stderr


def test_sigusr1_dumps_threads(tmp_path: Path) -> None:
    code = ("import time;from awr.runtime.child import init_child;c=init_child('x');print('R',flush=True)\n"
            "while not c.stopping: time.sleep(0.01)")
    p = subprocess.Popen([sys.executable, "-c", code], env=rtlib.child_env(), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "R"
        os.kill(p.pid, signal.SIGUSR1)
        time.sleep(0.2)
        os.kill(p.pid, signal.SIGTERM)
        _, err = p.communicate(timeout=10)
        assert p.returncode == 0
        assert "Thread" in err or "Current thread" in err
    finally:
        if p.poll() is None:
            p.kill()
