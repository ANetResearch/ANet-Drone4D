#!/usr/bin/env python3
"""tools/deploy/dmax/audit_manifests.py：D3 容器清单审计（AWR-05 §4.6、§4.7、ADR-151、ADR-178；M24-FR-071；D3-AC-22）。

审计对象（逐个渲染出真实的 `docker run` 参数表，再按 `awr.agentsdk.env.containers.RULES` 的 CM-01 至 CM-08 逐项断言）：
  1. Python 启动点：gz 实例（`GzInstance.command()`，physics 档 docker 启动，按两个 Run 渲染）与 `launcher = local` 的子进程环境、
     Agent 隔离容器（`AgentContainers.argv()`，两个 Run × 两架机）；另扫描 python/awr 与 tools 下的 `.py`（不计注释与文档字符串），
     出现未登记的 `docker run` 启动点即报错（CM-00）。
  2. dmax 启动脚本：`tools/deploy/dmax/**/*.sh` 中含 `docker run` 的脚本按 `SHELL_SITES` 登记的变体（参数与环境变量）执行，
     `ssh`、`rsync`、`scp` 与 `docker` 由记录桩替换：远端命令串在本机受限 PATH 的 bash 中展开（另有 `mkdir`、`cp`、`chown` 空操作桩、
    输出 0 的 `stat` 桩与 `id`、`head`、`tail` 本机只读命令），`docker` 桩只记录参数表，
     不连接 dmax、不启动任何容器；变体声明 `reject = True` 时断言脚本拒绝该参数（如 `GPU=2,3`）且没有发出 `docker run`。
     未登记的脚本按无参数缺省变体渲染并告警，无法渲染时报错（CM-00）。
  3. 运行中容器（可选）：`--inspect FILE`（`docker inspect` JSON）或 `--live`（`ssh dmax` 只读执行 `docker inspect`，
     对象为名称以 `d3-` 开头的容器）。

规则：CM-01 `--memory`；CM-02 `--cpus`；CM-03 只经 `--gpus device=<N>` 绑定一张 D3 可用卡；CM-04 CPU 池（含 geosvc、gz physics）
不挂载 GPU；CM-05 GPU Worker 恰好一张卡；CM-06 命名 `d3-<组件>`，`d3-gz-*` 只用于 gz 实例，不同 Run 不同名；CM-07 gz 实例
`GZ_PARTITION` 每 Run 唯一、`GZ_IP=127.0.0.1`，非 gz 容器不在容器层设 `GZ_PARTITION`；CM-08 CPU 池进入 `d3.slice`（告警级）；
CM-00 启动点登记与可渲染。

用法：
    python tools/deploy/dmax/audit_manifests.py                 # 仓库内全部启动点
    python tools/deploy/dmax/audit_manifests.py --json out.json # 另写 JSON 报告（awr.d3.manifest_audit.v1）
    python tools/deploy/dmax/audit_manifests.py --live          # 另审计 dmax 上运行中的 d3-* 容器（只读）
    python tools/deploy/dmax/audit_manifests.py --strict-cgroup # CM-08 按错误计
退出码 0 通过（无错误级发现）、1 有错误级发现、2 参数或环境错误。
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tokenize
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT / "python") not in sys.path:
    sys.path.insert(0, str(ROOT / "python"))

from awr.agentsdk.env.containers import (  # noqa: E402
    RULES,
    AgentContainers,
    Finding,
    Launch,
    check_launches,
    launch_from_inspect,
    parse_docker_run,
)
from awr.phys.gz.instance import GzInstance  # noqa: E402

SCHEMA = "awr.d3.manifest_audit.v1"
SCRIPT_ROOT = "tools/deploy/dmax"
AUDIT_RUNS = ("audit-run-a", "audit-run-b")
AUDIT_UAVS = ("uav-01", "uav-02")
RENDER_TIMEOUT_S = 60.0

# 已登记的 Python 启动点（源文件相对路径 → 渲染函数名）；新增 docker 启动点须在此登记并给出渲染
PY_LAUNCHERS = {
    "python/awr/phys/gz/instance.py": "_render_gz",
    "python/awr/agentsdk/env/containers.py": "_render_agents",
    "tools/bench/d3/slam_eval/livo.py": "_render_slam_livo",
    "tools/bench/d3/slam_eval/cli.py": "_render_slam_cli",
    "tools/deploy/emax/d3-public/drill.py": "_render_d3_public_drill",
    "tools/bench/d3/lidar_collect.py": "_render_lidar_collect",
}
_PY_LAUNCH = re.compile(r"""["']run["']\s*,\s*["']-d["']|["']docker["']\s*,\s*["']run["']|\bdocker\s+run\b""")
_SH_LAUNCH = re.compile(r"\bdocker\s+(?:container\s+)?run\b")
ENV_SHOWN = frozenset({"GZ_PARTITION", "GZ_IP", "NVIDIA_VISIBLE_DEVICES", "NVIDIA_DRIVER_CAPABILITIES", "HOME"})
RULES_ALL = {"CM-00": "每个 D3 容器启动点已登记且可渲染出参数表；登记的拒绝变体被脚本拒绝", **RULES}


@dataclass(frozen=True)
class Variant:
    label: str
    component: str | None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    gpu: int | None = None                                       # 期望绑定的卡（None：不挂载 GPU）
    gpu_by_name: dict[str, int | None] = field(default_factory=dict)  # 按容器名覆盖期望卡（同一脚本含 GPU 阶段与 CPU 池阶段时）
    reject: bool = False                                         # 期望脚本拒绝该变体、不发出 docker run


# run_proto_board.sh 的启动前核对容器（宿主依赖与世界派生缓存，K07）：L2 变体中也在 CPU 池，不挂载 GPU
_BOARD_PREFLIGHT: dict[str, int | None] = {"d3-board-audit-pre": None, "d3-board-audit-geo": None}

# dmax 启动脚本的登记变体（仓库相对路径 → 变体）
SHELL_SITES: dict[str, tuple[Variant, ...]] = {
    f"{SCRIPT_ROOT}/bench_ctl.sh": (Variant("default", "bench", ("board", "list")),),
    f"{SCRIPT_ROOT}/run_d3_tests.sh": (                         # 主阶段绑定一张卡；gz 阶段与合并阶段在 CPU 池
        Variant("default", "test", (), {"HOST": "0"}, gpu_by_name={"d3-test-main": 2}),     # HOST=0：主机阶段在本机跑 pytest，渲染时不执行
        Variant("gpu3", "test", (), {"GPU": "3", "TASK_ID": "audit", "HOST": "0"}, gpu_by_name={"d3-test-audit": 3}),
        Variant("multi-card", "test", (), {"GPU": "2,3"}, reject=True),
        Variant("all-cards", "test", (), {"GPU": "all"}, reject=True),
        Variant("foreign-card", "test", (), {"GPU": "0"}, reject=True),
    ),
    f"{SCRIPT_ROOT}/run_d3_gate.sh": (Variant("start", "test", (), {"SYNC": "0", "WAIT": "0"}),),   # 回归门重载部分（CPU 池）：复用快照、只启动
    f"{SCRIPT_ROOT}/run_gz_tests.sh": (Variant("default", "test"),),
    f"{SCRIPT_ROOT}/run_gz_perf.sh": (Variant("default", "test", ("audit",)),),  # gz physics 性能复测宿主（CPU 池）：startup 与 gzcpu 各 1 条
    f"{SCRIPT_ROOT}/run_mbl_e2e.sh": (                          # 最小基准闭环宿主（D3-AC-56）：L2 渲染绑定 GPU 参数指定的一张卡
        Variant("gpu2", "board", ("audit",), gpu=2),
        Variant("gpu3", "board", ("audit",), {"GPU": "3"}, gpu=3),
        Variant("multi-card", "board", ("audit",), {"GPU": "2,3"}, reject=True),
        Variant("all-cards", "board", ("audit",), {"GPU": "all"}, reject=True),
        Variant("foreign-card", "board", ("audit",), {"GPU": "0"}, reject=True),
    ),
    f"{SCRIPT_ROOT}/run_proto_board.sh": (
        Variant("l1", "board", ("audit",)),
        Variant("l2-gpu2", "board", ("audit",), {"GPU": "2"}, gpu=2, gpu_by_name=_BOARD_PREFLIGHT),   # 启动前核对（K07）在 CPU 池
        Variant("l2-gpu3", "board", ("audit",), {"GPU": "3"}, gpu=3, gpu_by_name=_BOARD_PREFLIGHT),
        Variant("multi-card", "board", ("audit",), {"GPU": "2,3"}, reject=True),
        Variant("all-cards", "board", ("audit",), {"GPU": "all"}, reject=True),
        Variant("foreign-card", "board", ("audit",), {"GPU": "0"}, reject=True),
    ),
    f"{SCRIPT_ROOT}/geo_cache.sh": (Variant("check", "worldbuild", ("check",)), Variant("warm", "worldbuild", ("warm",))),  # 世界派生缓存（K07）
    f"{SCRIPT_ROOT}/images/gz/pylib.sh": (Variant("verify", "test", ("verify",)), Variant("install", "test", ("install",))),  # pylib-gz 重建（K07；build = 下载 + install + 替换）
    f"{SCRIPT_ROOT}/run_subworker.sh": (                         # 缺省：materialize 伴随容器 + subworker（数据根 $LAB_DATA）
        Variant("default", "subworker", ("--once",)),
        Variant("no-materialize", "subworker", ("--once",), {"MATERIALIZE": "0"}),
        Variant("benchroot", "subworker", ("--once",), {"BENCH_ROOT": "benchroot"}),
    ),
    f"{SCRIPT_ROOT}/images/agent/scan.sh": (Variant("default", "subworker", ("d3-agent-py:audit",)),),  # 提交扫描门复核（K18；CPU 池）
    f"{SCRIPT_ROOT}/images/trivy/build.sh": (Variant("default", "subworker"),),   # 构建后以 --version 自检一次（CPU 池）
    f"{SCRIPT_ROOT}/registry/regctl.sh": (Variant("up", "registry", ("up",)),),  # d3-registry 三个容器（CPU 池）
    f"{SCRIPT_ROOT}/lab/profile.sh": (Variant("up", None, ("up",)),),            # d3 profile 六个容器，组件取 d3.component 标签
    f"{SCRIPT_ROOT}/scan_images.sh": (Variant("default", "test"),),             # 镜像依赖扫描（每镜像 pip 与扫描两个容器 + 汇总）
}


# ==================================================================== Python 启动点


def _render_gz(tmp: Path) -> tuple[list[Launch], list[Finding]]:
    """gz physics 档：docker 启动（core.py 缺省容器名 d3-gz-<run_id>）按两个 Run 渲染；local 子进程环境逐项检查 CM-07。"""
    out, finds = [], []
    exp = tmp / "gz-export"
    exp.mkdir(parents=True, exist_ok=True)
    local_parts: dict[str, str] = {}
    for run in AUDIT_RUNS:
        inst = GzInstance(tmp / "gz" / run, f"d3-gz-{run}", world_export=exp, dry_run=True)
        out.append(parse_docker_run(inst.command(), "python:gz-instance[docker]", component="gz", run=run))
        loc = GzInstance(tmp / "gz-local" / run, f"d3-gz-{run}", launcher="local", world_export=exp, dry_run=True)
        env = loc.local_env()
        site = "python:gz-instance[local]"
        if not env.get("GZ_PARTITION"):
            finds.append(Finding("CM-07", "error", site, loc.name, "local gz subprocess has no GZ_PARTITION"))
        if env.get("GZ_IP") != "127.0.0.1":
            finds.append(Finding("CM-07", "error", site, loc.name, f"local gz subprocess GZ_IP={env.get('GZ_IP')!r}"))
        p = env.get("GZ_PARTITION", "")
        if p and p in local_parts and local_parts[p] != run:
            finds.append(Finding("CM-07", "error", site, loc.name, f"GZ_PARTITION {p!r} shared by runs {local_parts[p]!r} and {run!r}"))
        local_parts.setdefault(p, run)
    return out, finds


def _render_agents(tmp: Path) -> tuple[list[Launch], list[Finding]]:
    """Agent 隔离容器：两个 Run × 两架机，std 档 2 核、8 GB（ADR-178）。"""
    out = []
    for run in AUDIT_RUNS:
        ac = AgentContainers(tmp / "agents" / run, run, list(AUDIT_UAVS), "d3-agent-py:dev", cmd=["python", "-m", "drone4d.serve"], cpus=2.0, mem_gb=8.0)
        out += [parse_docker_run(ac.argv(u, tmp / "agents" / run / "sock" / u), "python:agent-containers", component="agent", run=run)
                for u in AUDIT_UAVS]
    return out, []


def _slam_eval():
    """slam_eval 作业包（tools/bench/d3/slam_eval，只依赖 numpy）→ (cli, livo) 模块。"""
    d = str(ROOT / "tools" / "bench" / "d3")
    if d not in sys.path:
        sys.path.insert(0, d)
    import slam_eval.cli as cli
    import slam_eval.livo as livo

    return cli, livo


def _render_slam_livo(tmp: Path) -> tuple[list[Launch], list[Finding]]:
    """FAST-LIVO2 容器（`docker_argv`）与读取镜像内参数文件的一次性容器（`image_file_argv`），两个作业各一份。"""
    _, livo = _slam_eval()
    bag = tmp / "slam" / "case" / "sim.bag"
    out = []
    for run in AUDIT_RUNS:
        r = livo.LivoRun(f"d3-livo-{run}-direct-livo-r1", "livo", 1, tmp / "slam" / run / "livo-r1", bag, "d3-livo:dev", mem_gb=8.0, cpus=2.0)
        out.append(parse_docker_run(livo.docker_argv(r), "python:slam-livo[run]", component="livo", run=run))
        argv = ["docker", *livo.image_file_argv("d3-livo:dev", "/ws/src/FAST-LIVO2/config/MARS_LVIG.yaml")]
        out.append(parse_docker_run(argv, "python:slam-livo[image-file]", component="livo", run=run))
    return out, []


_SLAM_YAML = "common:\n  img_en: 1\nevo:\n  seq_name: x\n  pose_output_en: false\npcd_save:\n  pcd_save_en: true\n"


def _render_slam_cli(tmp: Path) -> tuple[list[Launch], list[Finding]]:
    """slam_eval 作业的容器（命令行缺省参数，`--name-prefix` 取两个 Run 名）：`run` 的 livo / lio 回放（`_prepare` 与资源模型）、
    评测容器、GT 参考点容器，`regress` 的 RTK 抽取容器与回放容器。"""
    cli, livo = _slam_eval()
    cd = tmp / "slam-cli" / "case"
    cd.mkdir(parents=True, exist_ok=True)
    (cd / "livo.yaml").write_text(_SLAM_YAML, encoding="utf-8")
    (cd / "camera.yaml").write_text("cam_model: Pinhole\n", encoding="utf-8")
    case = cli.Case(dir=cd, name="case", bag=cd / "sim.bag", livo_yaml=cd / "livo.yaml", camera_yaml=cd / "camera.yaml", gt_imu=None,
                    gt_lidar_ref=None)
    ps = cli.build_parser()
    out: list[Launch] = []
    for run in AUDIT_RUNS:
        jo = tmp / "slam-cli" / run
        a = ps.parse_args(["run", "--case", str(cd), "--out", str(jo), "--name-prefix", run])
        res = cli.job_resources(case.lidar_model, 360.0)
        site = "python:slam-cli[run]"
        for mode in livo.MODES:
            lr = cli._prepare(case, jo, mode, 1, a, res, float(res["mem_gb"]), float(res["cpus"]))
            out.append(parse_docker_run(livo.docker_argv(lr), site, component="livo", run=run))
        out.append(parse_docker_run(cli._eval_docker_argv(a, case, jo / "runs" / "livo-r1", jo / "eval.json", None, None, None), site,
                                    component="eval", run=run))
        out.append(parse_docker_run(cli._ref_docker_argv(a, case, jo, jo / "gt_lidar_ref.ply"), site, component="eval", run=run))
        g = ps.parse_args(["regress", "--bag", str(case.bag), "--out", str(jo / "regress"), "--name-prefix", run])
        site = "python:slam-cli[regress]"
        out.append(parse_docker_run(cli._rtk_docker_argv(g, case.bag, jo / "regress", jo / "regress" / "rtk_enu.tum"), site,
                                    component="livo", run=run))
        res = cli.job_resources(g.lidar_model, g.flight_s, min_gb=g.min_mem_gb, min_cpus=g.min_cpus)
        lr = livo.LivoRun(name=cli._cname(g, "livo", "regress"), mode="livo", repeat=1, run_dir=jo / "regress" / "run", bag=case.bag,
                          image=g.image, mem_gb=float(res["mem_gb"]), cpus=float(res["cpus"]))
        out.append(parse_docker_run(livo.docker_argv(lr), site, component="livo", run=run))
    return out, []


def _render_lidar_collect(tmp: Path) -> tuple[list[Launch], list[Finding]]:
    """多机 LiDAR 采集作业（`lidar_collect.py all`，D3-AC-15）的两个容器阶段（`stage_argv`，命令行缺省参数，`--task` 取两个 Run 名）：
    采集容器为 GPU Worker（d3-sensorsvc-*，缺省 GPU 2），收尾容器为 CPU 池评测容器（d3-eval-*）；宿主 `slam` 阶段经 slam_eval 命令行
    启动的容器由 `_render_slam_cli` 覆盖。"""
    d = str(ROOT / "tools" / "bench" / "d3")
    if d not in sys.path:
        sys.path.insert(0, d)
    lc = sys.modules.get("d3_lidar_collect_audit")
    if lc is None:                                               # 脚本文件（非包）：按路径加载；dataclass 需模块已在 sys.modules
        spec = importlib.util.spec_from_file_location("d3_lidar_collect_audit", ROOT / "tools" / "bench" / "d3" / "lidar_collect.py")
        lc = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = lc
        spec.loader.exec_module(lc)
    ps = lc.build_parser()
    out: list[Launch] = []
    for run in AUDIT_RUNS:
        a = ps.parse_args(["all", "--src", str(ROOT), "--out", str(tmp / "lidar-collect" / run), "--world-dir", str(tmp / "worlds" / "w"),
                           "--task", run])
        out.append(parse_docker_run(lc.stage_argv(a, "collect"), "python:lidar-collect[collect]", component="sensorsvc", run=run))
        out.append(parse_docker_run(lc.stage_argv(a, "post"), "python:lidar-collect[post]", component="eval", run=run))
    return out, []


def _render_d3_public_drill(tmp: Path) -> tuple[list[Launch], list[Finding]]:
    """公开样例发布线演练（开发机，D3-AC-21）：nginx 容器与收尾的 chown 容器（组件 test，CPU 池）；进程号固定，只渲染一次。"""
    d = str(ROOT / "tools" / "deploy" / "emax" / "d3-public")
    if d not in sys.path:
        sys.path.insert(0, d)
    import drill

    box = drill.NginxBox(tmp / "d3-public-drill", "d3-f08-nginx:local", 18640, 18641)
    site = "python:d3-public-drill"
    return [parse_docker_run(box.run_argv("nginx -g 'daemon off;'"), site, component="test"),
            parse_docker_run(box.chown_argv(), site, component="test")], []


def _code_only(txt: str) -> str:
    """去掉注释与文档字符串（模块、类、函数体首个字符串表达式）后的源码；只用于启动模式扫描，语法错误时原样返回。"""
    try:
        tree = ast.parse(txt)
        toks = list(tokenize.generate_tokens(io.StringIO(txt).readline))
    except (SyntaxError, tokenize.TokenError, ValueError):
        return txt
    lines = txt.splitlines()
    for tk in reversed(toks):                                    # 同一行内自右向左截断，列号不受影响
        if tk.type == tokenize.COMMENT:
            r, c = tk.start
            lines[r - 1] = lines[r - 1][:c]
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            d = node.body[0]
            if isinstance(d, ast.Expr) and isinstance(d.value, ast.Constant) and isinstance(d.value.value, str):
                for i in range(d.lineno - 1, (d.end_lineno or d.lineno)):
                    lines[i] = ""
    return "\n".join(lines)


def discover_python_launchers(root: Path = ROOT) -> list[str]:
    """python/awr 与 tools 下出现 docker run 启动模式的 `.py`（仓库相对路径；本脚本除外；注释与文档字符串中的叙述不计）。"""
    hits = []
    for base in (root / "python" / "awr", root / "tools"):
        for p in sorted(base.rglob("*.py")):
            if "__pycache__" in p.parts or p.resolve() == Path(__file__).resolve():
                continue
            try:
                txt = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if _PY_LAUNCH.search(txt) and _PY_LAUNCH.search(_code_only(txt)):      # 先按原文粗筛，命中再去注释与文档字符串复核
                hits.append(p.relative_to(root).as_posix())
    return hits


def audit_python(tmp: Path, root: Path = ROOT) -> tuple[list[Launch], list[Finding]]:
    launches: list[Launch] = []
    finds = [Finding("CM-00", "error", f"python:{rel}", None, "docker launch site is not registered in PY_LAUNCHERS")
             for rel in discover_python_launchers(root) if rel not in PY_LAUNCHERS]
    for rel, fn in PY_LAUNCHERS.items():
        ls, fs = globals()[fn](tmp)
        if not ls:
            finds.append(Finding("CM-00", "error", f"python:{rel}", None, "registered launcher rendered no docker run"))
        launches += ls
        finds += fs
    return launches, finds


# ==================================================================== 启动脚本（记录桩渲染）

_SSH_STUB = """#!{py}
import os, subprocess, sys
a = sys.argv[1:]
i = 0
while i < len(a) and a[i].startswith("-"):
    i += 2 if a[i] in ("-o", "-p", "-i", "-l", "-F", "-J", "-E") else 1
cmd = " ".join(a[i + 1:])
env = {{"PATH": os.environ["AUDIT_REMOTE_PATH"], "HOME": os.environ.get("HOME", "/tmp"), "AUDIT_REC": os.environ["AUDIT_REC"],
       "LANG": "C.UTF-8"}}
sys.exit(subprocess.run(["/bin/bash", "-c", cmd], env=env).returncode)
"""
_DOCKER_STUB = """#!{py}
import json, os, sys
with open(os.environ["AUDIT_REC"], "a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if "run" in sys.argv[1:3] and ("-d" in sys.argv or "--detach" in sys.argv):
    print("0" * 64)
"""


def _stub(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def render_shell(script: Path, args: tuple[str, ...] = (), env: dict[str, str] | None = None, *, timeout_s: float = RENDER_TIMEOUT_S
                 ) -> tuple[int, list[list[str]], str]:
    """在记录桩下执行启动脚本 → (退出码, 发出的 docker 参数表列表（不含 docker 本身）, stderr 尾部)。

    本机侧 PATH 前置桩目录（ssh、rsync、scp、docker）；ssh 桩把远端命令串交给只含 docker 桩、mkdir、cp、chown 与 chmod 空操作桩、
    stat 桩（输出 0）、id、head 与 tail（只读）的 PATH 下的 bash，
    远端命令串里的其他命令找不到即失败，不会在本机产生副作用。"""
    with tempfile.TemporaryDirectory(prefix="d3-manifest-") as td:
        t = Path(td)
        loc, rem, rec = t / "local", t / "remote", t / "docker.jsonl"
        loc.mkdir()
        rem.mkdir()
        py = sys.executable
        _stub(loc / "ssh", _SSH_STUB.format(py=py))
        for n in ("rsync", "scp"):
            _stub(loc / n, "#!/bin/sh\nexit 0\n")
        for d in (loc, rem):
            _stub(d / "docker", _DOCKER_STUB.format(py=py))
        for n in ("mkdir", "cp", "chown", "chmod"):
            _stub(rem / n, "#!/bin/sh\nexit 0\n")
        _stub(rem / "stat", "#!/bin/sh\necho 0\n")              # `stat -c %g /var/run/docker.sock`（--group-add）：渲染时取 0
        for n in ("id", "head", "tail"):
            exe = shutil.which(n)
            if exe:
                (rem / n).symlink_to(exe)
        e = {"PATH": f"{loc}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}", "HOME": str(t), "LANG": "C.UTF-8",
             "AUDIT_REC": str(rec), "AUDIT_REMOTE_PATH": str(rem), **(env or {})}
        try:
            r = subprocess.run(["/bin/bash", str(script), *args], env=e, capture_output=True, text=True, timeout=timeout_s, check=False)
            code, err = r.returncode, r.stderr
        except subprocess.TimeoutExpired:
            code, err = 124, f"timeout after {timeout_s:g} s"
        calls = [json.loads(ln) for ln in rec.read_text(encoding="utf-8").splitlines()] if rec.exists() else []
    return code, calls, err[-400:]


def _is_run(argv: list[str]) -> bool:
    return argv[:1] == ["run"] or argv[:2] == ["container", "run"]


def discover_scripts(root: Path = ROOT) -> list[str]:
    """`tools/deploy/dmax/**/*.sh` 中含 docker run 的脚本（仓库相对路径）。"""
    out = []
    for p in sorted((root / SCRIPT_ROOT).rglob("*.sh")):
        try:
            if _SH_LAUNCH.search(p.read_text(encoding="utf-8")):
                out.append(p.relative_to(root).as_posix())
        except (OSError, UnicodeDecodeError):
            continue
    return out


def audit_scripts(root: Path = ROOT, sites: dict[str, tuple[Variant, ...]] | None = None) -> tuple[list[Launch], list[Finding], list[dict]]:
    """→ (渲染出的启动, 发现, 每个变体的渲染记录)。"""
    sites = SHELL_SITES if sites is None else sites
    launches: list[Launch] = []
    finds: list[Finding] = []
    renders: list[dict] = []
    found = discover_scripts(root)
    for rel in sorted(set(found) | set(sites)):
        variants = sites.get(rel)
        registered = variants is not None
        if variants is None:
            finds.append(Finding("CM-00", "warn", f"sh:{rel}", None, "launch script is not registered in SHELL_SITES; rendered with defaults"))
            variants = (Variant("default", None),)
        if not (root / rel).exists():
            finds.append(Finding("CM-00", "error", f"sh:{rel}", None, "registered launch script does not exist"))
            continue
        for v in variants:
            site = f"sh:{rel}[{v.label}]"
            code, calls, err = render_shell(root / rel, v.args, v.env)
            runs = [c for c in calls if _is_run(c)]
            renders.append({"site": site, "exit": code, "docker_calls": len(calls), "docker_runs": len(runs)})
            if v.reject:
                if code == 0 or runs:
                    finds.append(Finding("CM-03", "error", site, None,
                                         f"script accepted {v.env} (exit {code}, {len(runs)} docker run) instead of refusing it"))
                continue
            if code != 0 or not runs:
                finds.append(Finding("CM-00", "error", site, None, f"could not render (exit {code}, {len(runs)} docker run): {err.strip()[-200:]}"))
                continue
            for c in runs:
                lc = parse_docker_run(["docker", *c], site, component=v.component)
                launches.append(lc)
                g = v.gpu_by_name.get(lc.name, v.gpu)
                want = [] if g is None else [f"device={g}"]
                got = [g.strip().strip("'\"") for g in lc.gpus]
                if registered and got != want:
                    finds.append(Finding("CM-03", "error", site, lc.name, f"GPU binding {got} differs from the variant's {want}"))
    return launches, finds, renders


# ==================================================================== 运行中容器


def live_records(host: str = "dmax", timeout_s: float = 60.0) -> list[dict]:
    """`ssh <host>` 只读列出名称以 d3- 开头的容器并 `docker inspect`；没有容器时返回空表。"""
    cmd = "ids=$(docker ps -aq --filter name=^/d3-); if [ -n \"$ids\" ]; then docker inspect $ids; else echo '[]'; fi"
    r = subprocess.run(["ssh", host, cmd], capture_output=True, text=True, timeout=timeout_s, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"ssh {host} docker inspect failed: {r.stderr.strip()[-300:]}")
    return json.loads(r.stdout or "[]")


def audit_inspect(records: list[dict], site: str = "live") -> list[Launch]:
    return [launch_from_inspect(rec, site) for rec in records]


# ==================================================================== 汇总


def _public(lc: Launch) -> dict:
    """报告中的启动记录：不含原始参数表；环境变量只保留白名单的值，其余只列键（运行中容器的环境可能含凭据）。"""
    d = {k: v for k, v in asdict(lc).items() if k != "argv"}
    d["env"] = {k: (v if k in ENV_SHOWN else "<redacted>") for k, v in lc.env.items()}
    return d


def run_audit(*, root: Path = ROOT, inspect: list[dict] | None = None, strict_cgroup: bool = False) -> dict:
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="d3-manifest-py-") as td:
        py_l, py_f = audit_python(Path(td), root)
    sh_l, sh_f, renders = audit_scripts(root)
    live_l = audit_inspect(inspect) if inspect is not None else []
    launches = [*py_l, *sh_l, *live_l]
    finds = [*py_f, *sh_f, *check_launches(launches)]
    if strict_cgroup:
        finds = [Finding(f.rule, "error", f.site, f.name, f.message) if f.rule == "CM-08" else f for f in finds]
    errors = [f for f in finds if f.level == "error"]
    return {
        "schema": SCHEMA,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rules": RULES_ALL,
        "launches": [_public(lc) for lc in launches],
        "renders": renders,
        "findings": [asdict(f) for f in finds],
        "summary": {"launches": len(launches), "python": len(py_l), "scripts": len(sh_l), "live": len(live_l),
                    "errors": len(errors), "warnings": len(finds) - len(errors), "elapsed_s": round(time.monotonic() - t0, 3)},
        "pass": not errors,
    }


def _print(rep: dict) -> None:
    for lc in rep["launches"]:
        gpu = ",".join(lc["gpus"]) or "-"
        print(f"{lc['site']:<52} {lc['name'] or '-':<34} mem={lc['memory'] or '-':<6} cpus={lc['cpus'] or '-':<4} gpu={gpu}")
    for f in rep["findings"]:
        print(f"{f['level'].upper():<5} {f['rule']} {f['site']} {f['name'] or '-'}: {f['message']}")
    s = rep["summary"]
    print(f"manifest audit: {'PASS' if rep['pass'] else 'FAIL'} ({s['launches']} launches: {s['python']} python, {s['scripts']} scripts, "
          f"{s['live']} live; {s['errors']} errors, {s['warnings']} warnings; {s['elapsed_s']} s)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="D3 container manifest audit (D3-AC-22)")
    ap.add_argument("--json", type=Path, help="write the JSON report here")
    ap.add_argument("--inspect", type=Path, help="docker inspect JSON of running containers to audit as well")
    ap.add_argument("--live", action="store_true", help="also audit running d3-* containers on dmax (read-only ssh)")
    ap.add_argument("--host", default="dmax")
    ap.add_argument("--strict-cgroup", action="store_true", help="count CM-08 (d3.slice) as an error")
    a = ap.parse_args(argv)
    try:
        recs = json.loads(a.inspect.read_text(encoding="utf-8")) if a.inspect else None
        if a.live:
            recs = [*(recs or []), *live_records(a.host)]
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as e:
        print(f"manifest audit: {e}", file=sys.stderr)
        return 2
    rep = run_audit(inspect=recs, strict_cgroup=a.strict_cgroup)
    _print(rep)
    if a.json:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(json.dumps(rep, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0 if rep["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
