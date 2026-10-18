"""测试用 docker 替身：`run -d --name N … -v SRC:/run/drone4d … IMAGE CMD…` 以子进程启动 CMD（`python` 换成当前解释器、
套接字取 SRC/agent.sock），`wait`、`inspect -f {{.State.ExitCode}}`、`logs`、`kill`、`rm -f` 按名字操作。状态目录取环境变量
FAKE_DOCKER_STATE；每次 run 的参数写入 <state>/<name>.argv.json 供断言。"""

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

st = Path(os.environ["FAKE_DOCKER_STATE"])
st.mkdir(parents=True, exist_ok=True)
a = sys.argv[1:]
cmd = a[0]


def _pid(name):
    p = st / f"{name}.pid"
    return int(p.read_text()) if p.exists() else None


if cmd == "run":
    name = a[a.index("--name") + 1]
    i, src, env = 1, None, {}
    while i < len(a):
        if a[i] in ("-v",):
            s, _, d = a[i + 1].partition(":")
            if d == "/run/drone4d":
                src = s
            i += 2
        elif a[i] == "-e":
            k, _, v = a[i + 1].partition("=")
            env[k] = v
            i += 2
        elif a[i] in ("--name", "--label", "--network", "--tmpfs", "--cap-drop", "--security-opt", "--pids-limit", "--cpus", "--memory", "--user"):
            i += 2
        elif a[i] in ("-d", "--read-only"):
            i += 1
        else:
            break
    image, rest = a[i], a[i + 1:]
    (st / f"{name}.argv.json").write_text(json.dumps({"argv": a, "image": image, "cmd": rest, "env": env}))
    if image == "bad:image":
        print("Unable to find image", file=sys.stderr)
        sys.exit(125)
    run = [sys.executable if rest[0] == "python" else rest[0], *rest[1:]]
    e = {**os.environ, **env, "DRONE4D_AGENT_SOCK": f"{src}/agent.sock"}
    with open(st / f"{name}.log", "wb") as log:
        p = subprocess.Popen(run, env=e, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    (st / f"{name}.pid").write_text(str(p.pid))
    print(name)
elif cmd == "wait":
    pid = _pid(a[1])
    while pid is not None:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
        try:
            if os.waitpid(pid, os.WNOHANG)[0]:
                break
        except ChildProcessError:
            pass
elif cmd == "inspect":
    print("0" if (st / f"{a[-1]}.pid").exists() else "", end="")
    sys.exit(0 if (st / f"{a[-1]}.pid").exists() else 1)
elif cmd == "logs":
    p = st / f"{a[1]}.log"
    sys.stdout.write(p.read_text() if p.exists() else "")
elif cmd == "kill":
    pid = _pid(a[1])
    if pid:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)
elif cmd == "image" and a[1] == "inspect":
    ref = a[2]
    if ref == "sha256:" + "0" * 64:
        print("No such image", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"Id": ref, "Os": "linux", "Architecture": "amd64", "Size": 123456}))
elif cmd == "rm":
    for f in st.glob(f"{a[-1]}.pid"):
        f.unlink()
