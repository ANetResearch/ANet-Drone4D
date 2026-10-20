"""gz 实例启动计时与控制端点等待（D3-AC-34 "physics 实例启动 ≤ 5 s"；M24 §6.8.1 LAUNCHING → LOADING）。

以替身 docker 命令（可执行脚本，`inspect` 可设定为慢速，模拟守护进程繁忙）驱动 `GzInstance`：
- 存活检查在后台线程中执行，`docker inspect` 每次耗时 3 s 时控制端点仍在出现后立即被发现，不把守护进程的等待计入启动时长；
- `start()` 记录 docker run 本身的耗时，`wait_socket()` 记录端点出现与日志检查的时刻；
- 容器先于端点退出报 638，网格缺失报 638，超时报 631。
"""

from __future__ import annotations

import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from awr.phys import PhysError
from awr.phys.gz.instance import GzInstance

FAKE_DOCKER = """#!{py}
import os, sys, time
from pathlib import Path
d = Path(os.environ["FAKE_DOCKER_DIR"])
cmd = sys.argv[1]
with (d / "calls.log").open("a") as f:
    f.write(cmd + "\\n")
if cmd == "run":
    print("0123456789ab")
elif cmd == "inspect":
    time.sleep(float(os.environ.get("FAKE_INSPECT_S", "0")))
    if (d / "removed").exists():
        sys.exit(1)
    print("false" if (d / "dead").exists() else "true")
elif cmd == "logs":
    p = d / "logs.txt"
    print(p.read_text() if p.exists() else "")
elif cmd == "rm":
    (d / "removed").touch()
"""


@pytest.fixture
def fake_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "fake"
    d.mkdir()
    exe = d / "docker"
    exe.write_text(FAKE_DOCKER.format(py=sys.executable), encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("FAKE_DOCKER_DIR", str(d))
    return d


def _bind_later(inst: GzInstance, delay_s: float) -> threading.Thread:
    """模拟 Drone4DClock 在 `delay_s` 后绑定控制端点（创建套接字文件）。"""

    def bind() -> None:
        time.sleep(delay_s)
        (inst.sock_dir / "ctl.sock").touch()

    th = threading.Thread(target=bind, daemon=True)
    th.start()
    return th


def test_socket_found_without_waiting_on_slow_daemon(tmp_path: Path, fake_docker: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_INSPECT_S", "3.0")                                   # 守护进程繁忙：每次 inspect 3 s
    inst = GzInstance(tmp_path / "run", "d3-gz-k04", docker=str(fake_docker / "docker"))
    inst.start("<sdf/>")
    assert inst.launch["cmd_s"] >= 0 and (tmp_path / "run" / "run.sdf").read_text() == "<sdf/>"
    _bind_later(inst, 0.2)
    t0 = time.monotonic()
    inst.wait_socket(10.0, alive_every_s=0.05)
    waited = time.monotonic() - t0
    assert waited < 1.5, waited                                                   # 原实现逐轮同步 inspect，至少 3 s
    assert 0.15 <= inst.launch["socket_s"] < 1.5 and inst.launch["log_check_s"] >= 0.0
    calls = (fake_docker / "calls.log").read_text().split()
    assert calls[0] == "run" and calls.count("logs") == 1


def test_exited_container_and_missing_mesh(tmp_path: Path, fake_docker: Path) -> None:
    inst = GzInstance(tmp_path / "a", "d3-gz-k04a", docker=str(fake_docker / "docker"))
    inst.start("<sdf/>")
    (fake_docker / "dead").touch()
    t0 = time.monotonic()
    with pytest.raises(PhysError) as e:
        inst.wait_socket(10.0, alive_every_s=0.05)
    assert e.value.code == 638 and time.monotonic() - t0 < 3.0
    (fake_docker / "dead").unlink()

    inst2 = GzInstance(tmp_path / "b", "d3-gz-k04b", docker=str(fake_docker / "docker"))
    inst2.start("<sdf/>")
    (fake_docker / "logs.txt").write_text("[Err] Unable to find file[meshes/c0000.stl]\n")
    (inst2.sock_dir / "ctl.sock").touch()
    with pytest.raises(PhysError) as e:
        inst2.wait_socket(10.0)
    assert e.value.code == 638 and "c0000.stl" in str(e.value)


def test_socket_timeout(tmp_path: Path, fake_docker: Path) -> None:
    inst = GzInstance(tmp_path / "run", "d3-gz-k04t", docker=str(fake_docker / "docker"))
    inst.start("<sdf/>")
    with pytest.raises(PhysError) as e:
        inst.wait_socket(0.3, alive_every_s=0.05)
    assert e.value.code == 631


def test_dry_run_records_no_launch(tmp_path: Path) -> None:
    inst = GzInstance(tmp_path / "run", "d3-gz-dry", dry_run=True)
    cmd = inst.start("<sdf/>")
    assert cmd[:3] == ["docker", "run", "-d"] and inst.launch == {}
    assert os.path.exists(tmp_path / "run" / "run.sdf")
