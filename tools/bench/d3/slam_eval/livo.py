"""FAST-LIVO2 容器执行（M27-FR-101、FR-104、FR-105、§6.6.2；r1 §3.1、§4.7 `run_livo_bag.sh`）。

- 参数文件：FAST-LIVO2 的两级 yaml（`section:` 下缩进 `key: value`，相机 yaml 为顶层键）。`yaml_get` / `patch_yaml` 只按行处理这两级，
  补丁键不存在时报错（避免拼写错误静默失效）。模式：`livo` = `common/img_en: 1`（主口径），`lio` = `common/img_en: 0`（单列对照）；
  每次运行另写 `evo/pose_output_en: true`、`evo/seq_name`，`pcd_save/pcd_save_en` 按作业设置（长航时可关闭）。
- 容器：`d3-livo` 镜像（`tools/deploy/dmax/images/slam`）的 `/opt/d3-livo/run_livo.sh`；`--network none`、`--memory` 与
  `--memory-swap` 同值（不允许换页）、`--cpus`；算例目录与 bag 目录只读挂载，运行目录可写，FAST-LIVO2 的 `Log/` 挂到运行目录。
  容器不加 `--rm`，退出后先 `docker inspect` 读取 `OOMKilled` 与退出码再删除。
- 684 判定（FR-105）：容器 OOM（`State.OOMKilled`、退出码 137，或容器内 cgroup `memory.events` 的 `oom_kill` 计数 > 0：
  OOM 只结束 FastLIVO2 节点时入口脚本以 4 退出）、非零退出、或 `livo.log` 连续 `watchdog_s`（缺省 600 s）无增长
  （作业层 `docker kill`）；保留日志（失败尝试的日志移入 `attempt-<n>/`），同一重复最多重试 1 次。
- 逐帧耗时：解析日志中的 LIO / VIO 计时表与预处理点数（r1 `analyze_real.parse_log`）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

__all__ = [
    "MODES",
    "LivoRun",
    "classify_exit",
    "docker_argv",
    "extrinsic_T_I_L",
    "image_file",
    "image_id",
    "parse_log",
    "patch_yaml",
    "read_resources",
    "read_source",
    "run_container",
    "run_parallel",
    "run_with_retry",
    "yaml_get",
    "yaml_vector",
]

MODES: dict[str, dict[str, object]] = {"livo": {"common/img_en": 1}, "lio": {"common/img_en": 0}}
TOPICS = "/livox/lidar /livox/imu /left_camera/image/compressed"
ENTRY = "/opt/d3-livo/run_livo.sh"
LOG_DIR_IN_IMAGE = "/ws/src/FAST-LIVO2/Log"
SLAM_JOB_FAILED = 684


# ---------------------------------------------------------------------------------------------------------------- yaml
_SEC = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(#.*)?$")
_KEY = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*):(\s*)(.*?)(\s*#.*)?$")


def _fmt(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, tuple, np.ndarray)):
        return "[" + ", ".join(_fmt(x) for x in v) + "]"
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str) and (not v or v[0] in "\"'[" or re.fullmatch(r"[-+0-9.eE]+|true|false", v)):
        return v
    if isinstance(v, str):
        return f'"{v}"'
    return str(v)


def _locate(lines: list[str], key: str) -> int | None:
    sec, _, k = key.rpartition("/")
    cur = None
    for i, ln in enumerate(lines):
        m = _SEC.match(ln)
        if m:
            cur = m.group(1)
            continue
        km = _KEY.match(ln)
        if km is None:
            continue
        indented = bool(km.group(1))
        if not indented:
            cur = None
            if not sec and km.group(2) == k:
                return i
        elif sec and cur == sec and km.group(2) == k:
            return i
    return None


def yaml_get(text: str, key: str) -> str | None:
    """`section/key`（或顶层 `key`）的原始值字符串（去掉行尾注释与引号）；不存在时 None。"""
    lines = text.split("\n")
    i = _locate(lines, key)
    if i is None:
        return None
    v = _KEY.match(lines[i]).group(4).strip()
    return v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'" else v


def yaml_vector(text: str, key: str) -> np.ndarray:
    v = yaml_get(text, key)
    if v is None or not (v.startswith("[") and v.endswith("]")):
        raise KeyError(f"{key} is not a list in the parameter file")
    return np.array([float(x) for x in v[1:-1].split(",") if x.strip()], np.float64)


def patch_yaml(text: str, kv: Mapping[str, object]) -> str:
    """按行替换 `section/key` 的值，保留缩进；键不存在时 KeyError。"""
    lines = text.split("\n")
    for key, val in kv.items():
        i = _locate(lines, key)
        if i is None:
            raise KeyError(f"parameter {key} not found")
        km = _KEY.match(lines[i])
        lines[i] = f"{km.group(1)}{km.group(2)}: {_fmt(val)}"
    return "\n".join(lines)


def extrinsic_T_I_L(livo_yaml: str) -> np.ndarray:
    """FAST-LIVO2 `extrin_calib/extrinsic_T`、`extrinsic_R`（LiDAR 在 IMU 系的位姿，`p_I = R·p_L + T`）→ 4 × 4。"""
    T = np.eye(4)
    T[:3, 3] = yaml_vector(livo_yaml, "extrin_calib/extrinsic_T")
    T[:3, :3] = yaml_vector(livo_yaml, "extrin_calib/extrinsic_R").reshape(3, 3)
    return T


# ---------------------------------------------------------------------------------------------------------------- 镜像
def _docker(args: Sequence[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, **kw)


def image_id(image: str) -> str | None:
    r = _docker(["image", "inspect", "--format", "{{.Id}}", image])
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def image_file(image: str, path: str) -> str:
    """读取镜像内的文本文件（如 `/ws/src/FAST-LIVO2/config/MARS_LVIG.yaml`），容器 `--rm --network none`。"""
    r = _docker(["run", "--rm", "--network", "none", "--entrypoint", "cat", image, path])
    if r.returncode != 0:
        raise FileNotFoundError(f"{image}:{path}: {r.stderr.strip()}")
    return r.stdout


def read_source(spec: str | Path, image: str) -> str:
    """`image:<path>` 从镜像读取，否则读本地文件。"""
    s = str(spec)
    return image_file(image, s[len("image:"):]) if s.startswith("image:") else Path(s).read_text()


# ---------------------------------------------------------------------------------------------------------------- 运行
@dataclass
class LivoRun:
    """一次 FAST-LIVO2 回放（模式 × 重复）。路径均为宿主机绝对路径。"""

    name: str                       # 容器名（d3-<作业>-<模式>-r<k>）
    mode: str
    repeat: int
    run_dir: Path
    bag: Path
    image: str
    mem_gb: float
    cpus: float
    rate: float = 4.0
    start_s: float | None = None
    duration_s: float | None = None
    topics: str = TOPICS
    compressed: bool = True
    quiet_s: int = 10
    extra_mounts: list[tuple[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["run_dir"], d["bag"] = str(self.run_dir), str(self.bag)
        return d


def docker_argv(r: LivoRun) -> list[str]:
    rd, bag = Path(r.run_dir).resolve(), Path(r.bag).resolve()
    mem = f"{int(np.ceil(r.mem_gb * 1024))}m"
    argv = ["docker", "run", "--name", r.name, "--network", "none", "--memory", mem, "--memory-swap", mem, "--cpus", f"{r.cpus:g}",
            "-v", f"{bag.parent}:/bag:ro", "-v", f"{rd}:/run", "-v", f"{rd / 'Log'}:{LOG_DIR_IN_IMAGE}",
            "-e", "CFG=/run/livo.yaml", "-e", "CAM=/run/camera.yaml", "-e", f"BAG=/bag/{bag.name}", "-e", f"RATE={r.rate:g}",
            "-e", f"TOPICS={r.topics}", "-e", f"COMPRESSED={'true' if r.compressed else 'false'}", "-e", "OUT=/run",
            "-e", f"QUIET_S={int(r.quiet_s)}"]
    if r.start_s is not None:
        argv += ["-e", f"START={r.start_s:g}"]
    if r.duration_s is not None:
        argv += ["-e", f"DURATION={r.duration_s:g}"]
    for src, dst in r.extra_mounts:
        argv += ["-v", f"{src}:{dst}:ro"]
    return [*argv, "--entrypoint", "bash", r.image, ENTRY]


def classify_exit(state: Mapping | None, status: Mapping | None, stalled: bool) -> dict:
    """容器状态 → 运行结论。`state` 为 `docker inspect .State`，`status` 为容器写出的 `status.json`。"""
    st = dict(state or {})
    code = st.get("ExitCode")
    if stalled:
        return {"ok": False, "code": SLAM_JOB_FAILED, "why": "log_stalled", "exit_code": code}
    if st.get("OOMKilled") or code == 137 or (status or {}).get("oom_kill_events"):    # 子进程被 cgroup OOM 结束时容器退出码为 4
        return {"ok": False, "code": SLAM_JOB_FAILED, "why": "oom_kill", "exit_code": code}
    if code is None:
        return {"ok": False, "code": SLAM_JOB_FAILED, "why": "no_container_state", "exit_code": None}
    if code != 0:
        why = (status or {}).get("why") or "nonzero_exit"
        return {"ok": False, "code": SLAM_JOB_FAILED, "why": why, "exit_code": code}
    if not (status or {}).get("result_ok"):
        return {"ok": False, "code": SLAM_JOB_FAILED, "why": "no_trajectory", "exit_code": code}
    return {"ok": True, "code": 0, "why": None, "exit_code": code}


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return -1


def run_container(r: LivoRun, *, watchdog_s: float = 600.0, poll_s: float = 2.0) -> dict:
    """执行一次容器并返回结论（不重试）。日志无增长超过 watchdog_s 时 `docker kill` 并记 684。"""
    dk = _docker
    rd = Path(r.run_dir)
    (rd / "Log").mkdir(parents=True, exist_ok=True)
    dk(["rm", "-f", r.name])
    t0 = time.time()
    with open(rd / "docker.log", "w") as fo:
        p = subprocess.Popen(docker_argv(r), stdout=fo, stderr=subprocess.STDOUT)
        last, last_t, stalled = -2, time.time(), False
        while p.poll() is None:
            time.sleep(poll_s)
            cur = _size(rd / "livo.log")
            if cur != last:
                last, last_t = cur, time.time()
            elif time.time() - last_t > watchdog_s:
                stalled = True
                dk(["kill", r.name])
                try:                                                        # docker run 客户端随容器结束退出；宽限期后强制结束
                    p.wait(timeout=min(30.0, max(watchdog_s, 1.0)))
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
                break
    ins = dk(["inspect", "--format", "{{json .State}}", r.name])
    state = json.loads(ins.stdout) if ins.returncode == 0 and ins.stdout.strip() else None
    dk(["rm", "-f", r.name])
    try:
        status = json.loads((rd / "status.json").read_text())
    except (OSError, ValueError):
        status = None
    out = classify_exit(state, status, stalled)
    out.update({"wall_s": time.time() - t0, "status": status, "oom_killed": bool((state or {}).get("OOMKilled"))})
    return out


def _archive_attempt(rd: Path, k: int) -> None:
    dst = rd / f"attempt-{k}"
    dst.mkdir(exist_ok=True)
    for n in ("livo.log", "play.log", "res.txt", "status.json", "docker.log"):
        if (rd / n).exists():
            shutil.move(str(rd / n), str(dst / n))
    if (rd / "Log").exists():
        shutil.rmtree(rd / "Log", ignore_errors=True)
    (rd / "Log").mkdir()


def run_with_retry(r: LivoRun, *, retries: int = 1, **kw) -> dict:
    """FR-105：684 时保留本次日志并重试，最多 `retries` 次；返回最后一次结论与尝试记录。"""
    attempts = []
    for k in range(1, retries + 2):
        res = run_container(r, **kw)
        attempts.append({"attempt": k, **{x: res.get(x) for x in ("ok", "code", "why", "exit_code", "wall_s", "oom_killed")}})
        if res["ok"] or k == retries + 1:
            break
        _archive_attempt(Path(r.run_dir), k)
    res["attempts"] = attempts
    return res


def run_parallel(runs: Sequence[LivoRun], parallel: int, **kw) -> list[dict]:
    """有界并行（FAST-LIVO2 并行度 ≤ 4，ADR-178）；单个运行失败不影响其他运行。"""
    results: list[dict | None] = [None] * len(runs)
    sem = threading.Semaphore(max(int(parallel), 1))

    def work(i: int) -> None:
        with sem:
            try:
                results[i] = run_with_retry(runs[i], **kw)
            except Exception as e:
                results[i] = {"ok": False, "code": SLAM_JOB_FAILED, "why": f"driver_error: {e}", "attempts": []}

    th = [threading.Thread(target=work, args=(i,), daemon=True) for i in range(len(runs))]
    for t in th:
        t.start()
    for t in th:
        t.join()
    return [r for r in results if r is not None]


# ---------------------------------------------------------------------------------------------------------------- 日志与资源
def parse_log(path: Path | str) -> dict:
    """FAST-LIVO2 日志：每次 LIO / VIO 更新耗时（ms，取计时表 `Current Total Time` 行）与预处理输入、输出点数。"""
    lio, vio, pre_in, pre_out = [], [], [], []
    cur = None
    try:
        f = open(path, "rb")                                                # noqa: SIM115
    except OSError:
        return {}
    with f:
        for raw in f:
            line = raw.decode("utf-8", "ignore")
            if "LIO Mapping Time" in line:
                cur = "lio"
            elif "VIO Time" in line:
                cur = "vio"
            elif "Current Total Time" in line and cur:
                v = re.findall(r"\|\s*([0-9.]+)\s*\|", line)
                if v:
                    (lio if cur == "lio" else vio).append(float(v[-1]))
                cur = None
            elif "[ Preprocess ] Input point number" in line or "[ Preprocess ] Output point number" in line:
                m = re.search(r"number: (\d+)", line)
                if m:
                    (pre_in if "Input" in line else pre_out).append(int(m.group(1)))

    def st(x):
        x = np.asarray(x, np.float64)
        return {"n": len(x), "mean_ms": float(x.mean() * 1e3), "p50_ms": float(np.percentile(x, 50) * 1e3),
                "p95_ms": float(np.percentile(x, 95) * 1e3), "max_ms": float(x.max() * 1e3)} if len(x) else {"n": 0}

    return {"lio": st(lio), "vio": st(vio), "pts_in_mean": float(np.mean(pre_in)) if pre_in else None,
            "pts_out_mean": float(np.mean(pre_out)) if pre_out else None}


def read_resources(run_dir: Path | str) -> dict:
    """`res.txt`（墙钟 s、RSS kB、CPU%）与 `status.json` → 峰值 RSS、平均 CPU、CPU 秒与回放时序。"""
    rd = Path(run_dir)
    out: dict = {}
    try:
        status = json.loads((rd / "status.json").read_text())
    except (OSError, ValueError):
        status = {}
    out["timing"] = {k: status.get(k) for k in ("play_s", "drain_s", "save_s")}
    try:
        a = np.loadtxt(rd / "res.txt", skiprows=1, ndmin=2)
    except (OSError, ValueError):
        a = np.empty((0, 3))
    if len(a):
        out.update({"rss_peak_gb": float(a[:, 1].max() * 1024 / 1e9), "rss_end_gb": float(a[-1, 1] * 1024 / 1e9),
                    "cpu_pct_mean": float(a[:, 2].mean()), "samples": len(a)})
    if status.get("cpu_ticks") is not None:
        out["cpu_s_total"] = float(status["cpu_ticks"]) / float(status.get("clk_tck") or 100)
        play = (status.get("play_s") or 0) + (status.get("drain_s") or 0)
        out["cpu_cores_equiv"] = out["cpu_s_total"] / play if play > 0 else None
    if status.get("cgroup_mem_peak_bytes"):
        out["cgroup_mem_peak_gb"] = float(status["cgroup_mem_peak_bytes"]) / 1e9
    out["_res"] = a
    out["_status"] = status
    return out
