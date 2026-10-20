#!/usr/bin/env python3
"""tools/bench/d3/crossmachine.py：跨机器确定性对拍（X01；D3-AC-02、03、07、33、35；AWR-05 §11）。

同一份代码快照分别在本机（Xeon E5-2603 v4，AVX2）与 dmax（Xeon Gold 6326，AVX-512，CPU 池容器）上执行 `collect`，各得一份指纹
（`drone4d.bench.crossmachine.v1`），再以 `compare` 对两份指纹逐项比对，输出对拍报告（`drone4d.bench.crossmachine_report.v1`）。
对拍项与验收条款：

    world/<id>      D3-AC-02  从原始数据重建世界包（crop、town）：`contentVersion`、全部内容文件 sha256；构建耗时只记录不比对
    navmesh/<id>    D3-AC-03  NavMesh 烘焙产物（行人与车辆 profile 的 bin、图层 JSON）sha256、nav.graph `content_sha256`，以及 navsvc
                              （Recast/Detour）对同一请求序列（随机点、路径、视线、吸附、高度共 1,200 条）的应答原始字节摘要
    tape/<name>     D3-AC-07  事件磁带 `content_sha256`、`body_sha256`、`header_sha256`、`file_sha256` 与脚本演员列哈希：
                              起伏地形合成世界 43 名演员 360 s（E03 用例同一输入）与 crop-handoff-001（strict、ranked、Detour）
    vehicle/<name>  D3-AC-33  车道图车辆生成摘要（`Sim.digest()`）：合成十字路网 20 辆 90 s、crop 车道图 60 辆 180 s，以及合成世界包上
                              `vehicle_track` 事件 strict 编译的 `content_sha256` 与 `report.lanes.digest`
    fast/<name>     D3-AC-35  Fast 档 CPU L1（`physics = fast`）每架机观测流（`obs.sha256`）与动作流（`acts.jsonl`）的字节摘要、
                              PoseHistory 摘要 `pose_sha256` 与规格摘要（`config_sha256`、`spec_sha256`；`run_hash` 含平台版本，
                              只在同机复核中比较）：合成世界（P07 用例同一场景）与 crop-handoff-001

每一项在本机内部另做复核（`checks`，例如同机两次生成相同、内容版本可由内容文件重算、磁带出处为 Detour 后端），复核失败的项不计为
一致。跨机比对的口径：两份指纹的代码快照摘要（`code.tree_sha256`）相同且采集期间未变化，每一项的 `digests` 与 `files` 逐键相同；
`info`（耗时、版本、路径）只进报告不参与比对。配对方式按 §11 原文区分：D3-AC-02、03、07、33 要求"AVX2 与 AVX-512 两台机器"
（`cross`：硬件分别为 AVX2 与 AVX-512，numpy 各按硬件派发）；D3-AC-35 要求"同 ISA 的第二台机器复测"（`same`：两侧 numpy 派发档
相同，AVX-512 机器以 `NPY_DISABLE_CPU_FEATURES="X86_V4 AVX512_ICL AVX512_SPR"` 按 AVX2 档派发）。不适用于当前配对的条款记为
`n/a`，其对拍项的差异只记录；报告结论 = 无缺项、出错与复核失败，且全部适用条款通过。

用法：
    # 每台机器各执行一次（dmax 在 d3-compile 容器内，代码快照以卷挂载，原始数据经 data/raw/d3-glb 符号链接）
    python tools/bench/d3/crossmachine.py collect --label local --out var/x01 [--scratch DIR] \\
        [--items world,navmesh,tape,vehicle,fast] [--worlds oryahovo-crop1,smalltown-01] [--published-worlds worlds] [--keep]
    python tools/bench/d3/crossmachine.py compare var/x01/crossmachine-local.json var/x01/crossmachine-dmax.json \\
        --out var/x01/crossmachine-report.json [--md var/x01/crossmachine-report.md]
    # D3-AC-35 同 ISA 复测：dmax 另以 NPY_DISABLE_CPU_FEATURES="X86_V4 AVX512_ICL AVX512_SPR" 采集 fast 项（--label dmax-avx2）后比对
退出码：collect 0 完成且本机复核全部通过、1 有复核失败或某项出错、2 参数错误；compare 0 报告结论成立、1 不成立、2 参数错误。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for _p in (ROOT / "python", ROOT / "tools/sdk/drone4d/src", ROOT / "tests/d3/world", ROOT / "tests/d3/events"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

SCHEMA = "drone4d.bench.crossmachine.v1"
REPORT_SCHEMA = "drone4d.bench.crossmachine_report.v1"
ITEMS = ("world", "navmesh", "tape", "vehicle", "fast")
AC_OF = {"world": "D3-AC-02", "navmesh": "D3-AC-03", "tape": "D3-AC-07", "vehicle": "D3-AC-33", "fast": "D3-AC-35"}
# 各条款要求的配对方式（§11 原文）：02、03、07、33 为"AVX2 与 AVX-512 两台机器"（cross）；35 为"同 ISA 的第二台机器复测"（same）
AC_PAIR = {"D3-AC-02": "cross", "D3-AC-03": "cross", "D3-AC-07": "cross", "D3-AC-33": "cross", "D3-AC-35": "same"}
CROP_ID = "oryahovo-crop1"
TOWN_ID = "smalltown-01"
DEFAULT_WORLDS = (CROP_ID, TOWN_ID)
CROP_EVENT = ROOT / "configs" / "events.d3" / "crop-handoff-001"
CROP_RUN_SPEC = ROOT / "configs" / "bench.d3" / "crop-handoff-001.l1-3uav.run_spec.json"
NAV_POINTS = 400                                  # navsvc 请求序列：400 个随机点（每 4 个取 1 个车辆 profile），相邻两点构成 200 对
NAV_SEED0 = 20261019
FAST_SYNTH_S = 20.0                               # Fast L1 合成场景时长（与 P07 用例相同：200 tick）
FAST_CROP_S = 30.0                                # Fast L1 crop 场景时长（300 tick；只为跨机字节比对，不评成绩）
SCRATCH_DIRS = ("worlds", "tape-terrain", "tape-crop", "synth", "vt", "fast")      # collect 在 scratch 下建立的子目录
ISA_FLAGS = ("sse4_2", "avx", "avx2", "fma", "avx512f", "avx512dq", "avx512bw", "avx512vl", "avx512_vnni", "avx512_bf16", "amx_tile")
# 代码快照摘要覆盖的路径（影响对拍项产物的全部代码、配置与测试夹具）；__pycache__ 与 .pyc 除外
CODE_PATHS = ("python/awr", "tools/navsvc/src", "tools/navsvc/WASM_SHA256", "tools/navsvc/package.json", "tools/worldglb",
              "tools/sdk/drone4d/src", "configs/worlds.d3", "configs/events.d3", "configs/bench.d3", "packages/contracts",
              "vehicles", "tests/d3/world", "tests/d3/events", "tools/bench/d3/crossmachine.py")


# ---------------------------------------------------------------------------------------------------- 通用
def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with Path(p).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canon(obj) -> bytes:
    """规范 JSON（键排序、紧凑、UTF-8）：指纹内的映射摘要用。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")              # noqa: UP017 - dmax 镜像为 Python 3.10


class Item:
    """一个对拍项的指纹：`digests` 与 `files` 跨机比对，`checks` 为本机复核（全部为真才计一致），`info` 只记录。"""

    def __init__(self, name: str, ac: Iterable[str]) -> None:
        self.name = name
        self.ac = sorted(set(ac))
        self.digests: dict[str, str | int] = {}
        self.files: dict[str, str] = {}
        self.checks: dict[str, bool] = {}
        self.info: dict = {}
        self.error: str | None = None
        self.seconds = 0.0

    def to_json(self) -> dict:
        d = {"ac": self.ac, "digests": dict(sorted(self.digests.items())), "checks": dict(sorted(self.checks.items())),
             "info": self.info, "seconds": round(self.seconds, 1)}
        if self.files:
            d["files"] = dict(sorted(self.files.items()))
        if self.error:
            d["error"] = self.error
        return d


# ---------------------------------------------------------------------------------------------------- 机器与代码
def cpu_info(text: str | None = None) -> dict:
    """`/proc/cpuinfo` → 型号、逻辑核数与 ISA 标志子集；`isa`：有 avx512f 为 AVX-512，否则有 avx2 为 AVX2，否则 baseline。"""
    if text is None:
        try:
            text = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    model, flags, n = "", set(), 0
    for line in text.splitlines():
        k, _, v = line.partition(":")
        k = k.strip()
        if k == "processor":
            n += 1
        elif k == "model name" and not model:
            model = v.strip()
        elif k == "flags" and not flags:
            flags = set(v.split())
    isa = "AVX-512" if "avx512f" in flags else "AVX2" if "avx2" in flags else "baseline"
    return {"model": model or platform.processor(), "logical_cpus": n, "isa": isa, "flags": [f for f in ISA_FLAGS if f in flags]}


def _cgroup_cpus() -> float | None:
    """容器 CPU 配额（cgroup v2 `cpu.max`，docker --cpus）；无配额时为 None。"""
    try:
        q, p = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        return None if q == "max" else round(int(q) / int(p), 2)
    except (OSError, ValueError):
        return None


def _numpy_simd() -> dict:
    try:
        from numpy._core._multiarray_umath import __cpu_features__ as f  # numpy ≥ 2
    except ImportError:                                                             # pragma: no cover - numpy 1.x
        from numpy.core._multiarray_umath import __cpu_features__ as f
    feats = {k: bool(f[k]) for k in ("X86_V3", "X86_V4", "AVX2", "FMA3", "AVX512F", "AVX512_SKX", "AVX512_ICL") if k in f}
    return {"dispatch": numpy_dispatch(feats), "features": feats}


def numpy_dispatch(feats: dict) -> str:
    """numpy 运行期 SIMD 派发档：X86_V4（numpy ≥ 2.5 的分组名，旧版为 AVX512_SKX）可用为 AVX-512，X86_V3 / AVX2 可用为 AVX2。
    `NPY_DISABLE_CPU_FEATURES="X86_V4 AVX512_ICL AVX512_SPR"` 时 AVX-512 机器按 AVX2 档派发（float64 log、exp、arctan2 等的
    AVX-512 实现与 AVX2 / libm 路径存在 1 ulp 级差异，X01 实测）。"""
    if feats.get("X86_V4") or feats.get("AVX512_SKX"):
        return "AVX-512"
    return "AVX2" if feats.get("X86_V3") or feats.get("AVX2") else "baseline"


def _versions() -> dict:
    from importlib import metadata

    out = {"python": platform.python_version()}
    for m in ("numpy", "scipy", "open3d", "numba", "zstandard", "pillow", "shapely", "trimesh"):
        try:
            out[m] = metadata.version(m)
        except metadata.PackageNotFoundError:
            try:                                                        # 发行包名与模块名不同（如 open3d-cpu）
                out[m] = str(__import__(m).__version__)
            except (ImportError, AttributeError):
                continue
    return out


def _node_info() -> dict:
    from awr.world.glb.derive import navmesh as nm

    d: dict = {"recast": nm.recast_version(), "wasm_sha256": nm.wasm_sha256()}
    try:
        exe = nm.node_bin()
        d["node"] = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
        d["modules"] = str(nm.navsvc_modules())
    except Exception as e:                                                          # 只记录，不中断指纹
        d["error"] = str(e)
    return d


def machine_info(label: str) -> dict:
    import numpy as np

    return {"label": label, "cpu": cpu_info(), "cgroup_cpus": _cgroup_cpus(), "affinity_cpus": len(os.sched_getaffinity(0)),
            "kernel": platform.release(), "arch": platform.machine(), "numpy_simd": _numpy_simd(), "numpy_blas": _blas(np),
            "versions": _versions(), "navsvc": _node_info(),
            "env": {k: os.environ[k] for k in ("AWR_EVENT_NAV", "AWR_NAV_THREADS", "AWR_NODE", "AWR_VEHICLES_DIR",
                                                                 "AWR_CONTRACTS_DIR", "NPY_DISABLE_CPU_FEATURES", "OMP_NUM_THREADS",
                                                                 "PYTHONHASHSEED") if k in os.environ}}


def _blas(np) -> str:
    try:
        cfg = np.show_config(mode="dicts")
        b = cfg.get("Build Dependencies", {}).get("blas", {})
        return f"{b.get('name', '?')} {b.get('version', '')}".strip()
    except Exception:
        return "?"


def code_identity(root: Path = ROOT, paths: Iterable[str] = CODE_PATHS) -> dict:
    """代码快照摘要：所列路径下全部文件（排除 __pycache__、.pyc、node_modules）的 `{相对路径: sha256}` 与其规范 JSON 的 sha256。"""
    files: dict[str, str] = {}
    for rel in paths:
        p = root / rel
        if p.is_file():
            files[rel] = sha256_file(p)
            continue
        if not p.is_dir():
            continue
        for q in sorted(p.rglob("*")):
            if q.is_file() and "__pycache__" not in q.parts and "node_modules" not in q.parts and q.suffix != ".pyc":
                files[q.relative_to(root).as_posix()] = sha256_file(q)
    raw = root / "configs/worlds.d3/raw-manifest.json"
    return {"tree_sha256": sha256_bytes(canon(files)), "n_files": len(files), "files": files,
            "raw_manifest_sha256": sha256_file(raw) if raw.is_file() else None, "base_commit": os.environ.get("AWR_X01_COMMIT")}


# ---------------------------------------------------------------------------------------------------- 世界包与 NavMesh
def collect_world(wid: str, out_root: Path, published: Path | None) -> Item:
    from awr.world.glb.pipeline import GlbAdapter
    from awr.world.ingest.types import StageContext
    from awr.world.package.build import build_world
    from awr.world.package.manifest import content_version, scan_content_files
    from awr.world.package.params import BuildParams

    it = Item(f"world/{wid}", ["D3-AC-02"])
    t0 = time.perf_counter()
    res = build_world(GlbAdapter(wid), out_root, params=BuildParams.from_config(), ctx=StageContext(world_id=wid))
    it.info["build_wall_s"] = round(time.perf_counter() - t0, 1)
    it.checks["build_exit_0"] = res.exit_code == 0
    if res.exit_code != 0:
        it.error = f"build exit {res.exit_code}: {res.message}"
        return it
    w = out_root / wid
    files = scan_content_files(w)
    wj = json.loads((w / "world.json").read_text(encoding="utf-8"))
    it.files = {f["path"]: f["sha256"] for f in files}
    it.digests["contentVersion"] = wj["contentVersion"]
    it.digests["content_files_sha256"] = sha256_bytes(canon(it.files))
    it.digests["n_content_files"] = len(files)
    it.checks["content_version_recomputes"] = wj["contentVersion"] == content_version(files) == res.content_version
    it.checks["world_json_files_match_scan"] = {f["path"]: f["sha256"] for f in wj["files"]} == it.files
    rep = json.loads((w / "qa/report.json").read_text(encoding="utf-8"))
    g = rep.get("glb") or {}
    stages = {s["name"]: round(float(s["seconds"]), 1) for s in g.get("stages", [])}
    lv = float((g.get("leveling") or {}).get("seconds") or 0.0)
    it.info.update(stages_s=stages, leveling_qa_s=round(lv, 1), build_s_excl_registration=round(sum(stages.values()) + lv, 1),
                   qa_status=rep.get("status"), scaleStatus=wj.get("scaleStatus"))
    if published is not None and (published / wid / "world.json").is_file():
        pv = json.loads((published / wid / "world.json").read_text(encoding="utf-8"))["contentVersion"]
        it.info["published_contentVersion"] = pv
        it.info["matches_published"] = pv == wj["contentVersion"]
    return it


def _navsvc_requests(ns, profiles: Iterable[str]) -> tuple[list[dict], list[bytes]]:
    """W12 跨机对拍的请求序列：400 个随机点（有车辆 profile 时 i % 4 == 0 取车辆，否则全为行人），相邻两点一对（profile 取前一点）
    做 path、los、snap、height。"""
    import json as _json

    has_vehicle = "vehicle" in set(profiles)
    reqs, raws, pts = [], [], []
    for i in range(NAV_POINTS):
        prof = "vehicle" if has_vehicle and i % 4 == 0 else "person"
        reqs.append({"op": "random_point", "profile": prof, "seed": NAV_SEED0 + i})
        raw = ns.call_raw("random_point", profile=prof, seed=NAV_SEED0 + i)
        raws.append(raw)
        pts.append((prof, _json.loads(raw)["p"]))
    for i in range(0, NAV_POINTS, 2):
        (pa, a), (_pb, b) = pts[i], pts[i + 1]
        for op in ("path", "los", "snap", "height"):
            kw = {"a": a, "b": b} if op in ("path", "los") else {"p": b}
            reqs.append({"op": op, "profile": pa, **kw})
            raws.append(ns.call_raw(op, profile=pa, **kw))
    return reqs, raws


def collect_navmesh(wid: str, world: Path) -> Item:
    from awr.sim.events.gen import load_lane_graph
    from awr.world.glb.derive import navmesh as nm

    it = Item(f"navmesh/{wid}", ["D3-AC-03", "D3-AC-33"])
    it.info["world_dir"] = str(world)
    profiles = sorted(q.stem for q in (world / "geometry/nav").glob("*.bin"))         # town 只有行人 profile（配方未要求车辆）
    it.digests["profiles"] = ",".join(profiles)
    for prof in profiles:
        lay = json.loads((world / "geometry/nav" / f"{prof}.json").read_text(encoding="utf-8"))
        b = sha256_file(world / "geometry/nav" / f"{prof}.bin")
        it.digests[f"{prof}.bin"] = b
        it.digests[f"{prof}.json"] = sha256_file(world / "geometry/nav" / f"{prof}.json")
        it.checks[f"{prof}_layer_bin_sha_matches_file"] = lay["bin"]["sha256"] == b
        isl = lay.get("islands") or {}
        it.info[prof] = {"bytes": lay["bin"].get("bytes"), "islands_kept": isl.get("kept"), "islands_total": isl.get("total")}
    g = load_lane_graph(world)
    it.checks["navgraph_layer_present"] = g is not None
    if g is not None:
        it.digests["navgraph_content_sha256"] = g.head["content_sha256"]
    replies = []
    for _ in range(2):                                                            # 两个独立 navsvc 进程：同机应答逐字节相同
        with nm.NavSvc(world=world) as ns:
            hello = ns.info
            reqs, raws = _navsvc_requests(ns, profiles)
        replies.append(b"".join(raws))
    it.digests["navsvc_requests_sha256"] = sha256_bytes(canon(reqs))
    it.digests["navsvc_replies_sha256"] = sha256_bytes(replies[0])
    it.digests["navsvc_n_requests"] = len(reqs)
    it.checks["navsvc_two_processes_identical"] = replies[0] == replies[1]
    it.info["navsvc_hello"] = {k: hello.get(k) for k in ("version", "recast", "wasm_sha256", "profiles") if k in hello}
    it.info["navsvc_failed_replies"] = sum(b'"ok":false' in r for r in replies[0].splitlines())
    return it


# ---------------------------------------------------------------------------------------------------- 事件磁带
def _tape_digests(it: Item, out: Path, prefix: str = "") -> object:
    from awr.sim.events import tape as tp

    t = tp.open_tape(out)
    h = t.hashes()
    for k in ("content_sha256", "body_sha256", "header_sha256", "file_sha256"):
        it.digests[prefix + k] = h[k]
    it.digests[prefix + "scripted_sha256"] = t.scripted_sha256()
    it.info[prefix + "shape"] = {"n_actors": t.n_actors, "n_scripted": t.n_scripted, "n_ticks": t.n_ticks}
    return t


def collect_tape_synth(tmp: Path) -> Item:
    """E03 用例同一输入（起伏地形合成世界、5 名脚本演员 + 38 名背景、360 s）：两次编译相同；背景 0 人时脚本演员列哈希相同。"""
    import pytest
    import test_tape_determinism as td

    from awr.sim.events.compile import CompileOptions, compile_event

    it = Item("tape/terrain-det-43", ["D3-AC-07"])
    mp = pytest.MonkeyPatch()
    try:
        w = td.build_terrain_world(tmp, mp)
        it.digests["world_contentVersion"] = json.loads((w / "world.json").read_text())["contentVersion"]
        res = {}
        for name, ev in (("run1", td._event()), ("run2", td._event()), ("bg0", td._event(bg=0))):
            d = tmp / name
            d.mkdir(parents=True, exist_ok=True)
            (d / "anchors.json").write_text(json.dumps(td.OVERLAY))
            (d / "event.json").write_text(json.dumps(ev))
            t0 = time.perf_counter()
            r = compile_event(d / "event.json", w, opts=CompileOptions(out=d / "v1", cache_dir=tmp / "cache"))
            it.info[f"{name}_compile_s"] = round(time.perf_counter() - t0, 2)
            if r.status != "compiled":
                it.error = f"{name}: {r.status} {r.reason}"
                return it
            res[name] = r
        t1 = _tape_digests(it, res["run1"].out)
        b = [(res[k].out / "tape.bin").read_bytes() for k in ("run1", "run2")]
        it.checks["two_compiles_byte_identical"] = b[0] == b[1]
        from awr.sim.events import tape as tp

        t0_ = tp.open_tape(res["bg0"].out)
        it.digests["bg0_content_sha256"] = t0_.hashes()["content_sha256"]
        it.checks["scripted_sha_same_bg0_bg38"] = t0_.scripted_sha256() == t1.scripted_sha256()
        it.checks["n_actors_43"] = t1.n_actors == 43 and t1.n_scripted == td.N_S
        man = json.loads((res["run1"].out / "tape.json").read_text())
        it.info["provenance"] = {k: man["provenance"].get(k) for k in ("generator_version", "recast", "node") if k in man["provenance"]}
        it.checks["provenance_detour"] = str(man["provenance"].get("generator_version", "")).endswith("-detour")
    finally:
        mp.undo()
    return it


def compile_crop_handoff(world: Path, out: Path):
    from awr.sim.events.compile import CompileOptions, compile_event

    return compile_event(CROP_EVENT / "event.yaml", world, opts=CompileOptions(out=out / "v1", cache_dir=out / "cache", strict=True, ranked=True))


def collect_tape_crop(world: Path, tmp: Path) -> tuple[Item, Path | None]:
    """crop-handoff-001 v1（K06 正式编译口径：strict、ranked、Detour）：磁带哈希、头部世界引用与 report.lanes。"""
    it = Item("tape/crop-handoff-001", ["D3-AC-07", "D3-AC-33"])
    out = tmp / "crop-handoff-001"
    t0 = time.perf_counter()
    r = compile_crop_handoff(world, out)
    it.info["compile_s"] = round(time.perf_counter() - t0, 1)
    it.checks["compiled_strict_ranked"] = r.status == "compiled" and r.reason is None
    if r.status != "compiled":
        it.error = json.dumps(r.as_dict(), ensure_ascii=False)[:2000]
        return it, None
    _tape_digests(it, r.out)
    man = json.loads((r.out / "tape.json").read_text())
    hw = man["header"]["world"]
    for k in ("content_version", "raycast_sha256", "anchors_sha256", "navgraph_sha256"):
        if k in hw:
            it.digests[f"header.world.{k}"] = hw[k]
    nv = hw.get("navmesh_sha256")
    if isinstance(nv, dict):
        for p, v in sorted(nv.items()):
            it.digests[f"header.world.navmesh_sha256.{p}"] = v
    elif nv:
        it.digests["header.world.navmesh_sha256"] = nv
    lanes = (man.get("report") or {}).get("lanes") or {}
    if lanes:
        it.digests["report.lanes.digest"] = lanes["digest"]
        it.checks["lanes_audit_ok"] = bool(lanes["audit"]["ok"])
    if r.truth and r.truth.get("truth_sha256"):
        it.digests["truth_sha256"] = r.truth["truth_sha256"]
    prov = man.get("provenance") or {}
    it.info["provenance"] = {k: prov.get(k) for k in ("generator_version", "recast", "wasm_sha256", "node") if k in prov}
    it.checks["provenance_detour"] = str(prov.get("generator_version", "")).endswith("-detour")
    return it, out


# ---------------------------------------------------------------------------------------------------- 车辆
def collect_vehicle_cross() -> Item:
    """test_vehicle 十字路网（primary × residential）：20 辆背景车、种子 5、90 s；同机两次（第二次新建 Network）摘要相同。"""
    import test_vehicle as tv

    from awr.sim.events import vehicle as V

    it = Item("vehicle/synth-cross-20", ["D3-AC-33"])
    g = tv._graph({1: (-300, 0), 2: (0, 0), 3: (300, 0), 4: (0, -200), 5: (0, 200)},
                  [(100, {"highway": "primary"}, [1, 2, 3]), (101, {"highway": "residential"}, [4, 2, 5])])
    net = V.Network(g)
    specs = V.background(g, 20, 5, net=net)
    s1 = V.simulate(g, specs, 90_000, net=net)
    s2 = V.simulate(g, V.background(g, 20, 5, net=net), 90_000, net=V.Network(g))
    it.digests["specs_sha256"] = sha256_bytes(canon([[s.id, s.depart_ms, list(s.route.segments), s.route.s_start] for s in specs]))
    it.digests["sim_digest"] = s1.digest()
    it.digests["xy_sha256"] = sha256_bytes(s1.xy.tobytes())
    it.checks["repeat_identical"] = s1.digest() == s2.digest() and s1.log == s2.log
    au = V.audit(g, s1.routes, s1.tape(), log=s1.log, ids=s1.ids, net=net)
    it.checks["audit_ok"] = bool(au["ok"])
    return it


def collect_vehicle_crop(world: Path) -> Item:
    """crop 已发布车道图上 60 辆背景车 180 s（test_vehicle crop 用例同一参数）：摘要、审计与关键事件。"""
    from awr.sim.events import vehicle as V
    from awr.sim.events.gen import load_lane_graph

    it = Item("vehicle/crop-bg-60", ["D3-AC-33"])
    g = load_lane_graph(world)
    if g is None:
        it.error = "crop world has no nav.graph layer"
        return it
    net = V.Network(g)
    specs = V.background(g, 60, 33, depart_window_ms=60_000, net=net)
    t0 = time.perf_counter()
    sim = V.simulate(g, specs, 180_000, net=net)
    it.info["simulate_s"] = round(time.perf_counter() - t0, 1)
    it.digests["navgraph_content_sha256"] = g.head["content_sha256"]
    it.digests["sim_digest"] = sim.digest()
    au = V.audit(g, sim.routes, sim.tape(), log=sim.log, ids=sim.ids, net=net)
    st = au["stats"]
    ev = V.key_events(g, sim.routes, sim.tape(), ids=sim.ids, net=net)
    it.digests["key_events_sha256"] = sha256_bytes(canon(ev))
    it.checks["audit_ok"] = bool(au["ok"]) and st["corridor_samples_frac"] == 1.0 and st["overlaps"] == 0 and st["min_gap_m"] >= V.MIN_GAP_M
    it.checks["repeat_identical"] = V.simulate(g, specs, 180_000, net=net).digest() == sim.digest()
    it.info["audit"] = {k: st[k] for k in ("corridor_samples_frac", "min_gap_m", "overlaps", "junction_traversals") if k in st}
    it.info["safety_stops"] = sim.log.get("safety_stops")
    return it


def collect_vehicle_track(synth: Path, tmp: Path) -> Item:
    """test_types_vehicle 合成世界包（写入 nav.graph 图层）上 `vehicle_track` 事件 strict 编译两次：content_sha256 与 report.lanes。"""
    import numpy as np
    import test_types_vehicle as tvv

    from awr.sim.events.compile import CompileOptions, compile_event
    from awr.world.glb.derive import lanes as L

    it = Item("vehicle/synth-vehicle-track", ["D3-AC-33", "D3-AC-07"])
    w = tmp / "worlds" / "synth-glb"
    shutil.copytree(synth, w)
    res = L.write_lanes(w, "synth-glb", "0" * 64, tvv._roads(), sources=[{"name": "synthetic", "sha256": "1" * 64}],
                        z_fn=lambda xy: np.zeros(len(xy)))
    wj = json.loads((w / "world.json").read_text())
    wj["layers"].append({"id": res.layer.id, "type": "navgraph", "role": "geometry", "format": L.FORMAT, "href": L.HREF, "status": "ready"})
    (w / "world.json").write_text(json.dumps(wj, indent=1))
    it.digests["navgraph_content_sha256"] = res.doc["awr"]["content_sha256"]
    outs = []
    for name in ("vt", "vt-again"):
        d = tmp / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "anchors.json").write_text(json.dumps({"schema": "awr.anchors.v1", "extends": "world", "world_id": "synth-glb",
                                                    "anchors": tvv.OVERLAY_ANCHORS}))
        (d / "event.json").write_text(json.dumps(tvv._doc()))
        r = compile_event(d / "event.json", w, opts=CompileOptions(out=d / "v1", cache_dir=tmp / "cache-vt"))
        if r.status != "compiled" or r.reason is not None:
            it.error = json.dumps(r.as_dict(), ensure_ascii=False)[:2000]
            return it
        outs.append(r)
    _tape_digests(it, outs[0].out)
    mans = [json.loads((r.out / "tape.json").read_text()) for r in outs]
    it.digests["report.lanes.digest"] = mans[0]["report"]["lanes"]["digest"]
    it.digests["truth_sha256"] = outs[0].truth["truth_sha256"]
    it.checks["two_compiles_identical"] = ((outs[0].out / "tape.bin").read_bytes() == (outs[1].out / "tape.bin").read_bytes()
                                          and mans[0]["report"]["lanes"]["digest"] == mans[1]["report"]["lanes"]["digest"])
    it.checks["lanes_audit_ok"] = bool(mans[0]["report"]["lanes"]["audit"]["ok"])
    return it


# ---------------------------------------------------------------------------------------------------- Fast 档 CPU L1
def _episode(spec, make_agent) -> dict:
    from awr.agentsdk.env.core import BenchEnv

    env = BenchEnv(spec)
    obs, infos = env.reset()
    ags = {u: make_agent(u) for u in env.possible_agents}
    for u, a in ags.items():
        a.reset(infos[u]["config"])
    while env.agents:
        obs, *_ = env.step({u: ags[u].step(o["obs"], o["inbox"]) for u, o in obs.items()})
    return env.close()


def _run_digest(run_dir: Path, m: dict, uavs: Iterable[str]) -> dict[str, str]:
    """一次运行的跨机比对摘要。`run_hash` 含平台版本（Python、numpy 补丁号与镜像摘要，M27-FR-007），两台机器本应不同，只记入 info；
    比对 `config_sha256` 与 `spec_sha256`（规格本身）。"""
    d: dict[str, str] = {"pose_sha256": m["pose_sha256"], "n_ticks": m["n_ticks"]}
    d.update({k: m[k] for k in ("config_sha256", "spec_sha256") if m.get(k)})
    for u in uavs:
        d[f"{u}.acts.jsonl"] = sha256_file(run_dir / "agents" / u / "acts.jsonl")
        d[f"{u}.obs.sha256"] = sha256_file(run_dir / "agents" / u / "obs.sha256")
    return d


def _fast_pair(it: Item, make_spec: Callable[[str], object], make_agent, tmp: Path) -> None:
    """同一规格跑两次（a、b）：逐流字节相同计入复核，a 的摘要进入跨机比对。"""
    runs = {}
    for name in ("a", "b"):
        spec = make_spec(name)
        t0 = time.perf_counter()
        m = _episode(spec, make_agent)
        it.info[f"{name}_wall_s"] = round(time.perf_counter() - t0, 1)
        runs[name] = _run_digest(Path(spec.out_dir), m, [f.uav for f in spec.fleet])
        it.info[f"{name}_run_hash"] = m.get("run_hash")
        if name == "a":
            it.info["versions"] = m.get("versions")
            it.info["tier"], it.info["clock_profile"] = m.get("tier"), m.get("clock_profile")
            mc = (m.get("tier_info") or {}).get("mesh_contact") or {}
            it.info["mesh_contact"] = {k: mc.get(k) for k in ("enabled", "crashes")}
            it.checks["tier_fast"] = m.get("tier") == "fast"
    it.digests.update(runs["a"])
    it.checks["same_machine_two_runs_bytewise"] = runs["a"] == runs["b"] and it.info["a_run_hash"] == it.info["b_run_hash"]


def collect_fast_synth(synth: Path, tmp: Path) -> Item:
    """P07 用例同一场景：合成世界、test_compile 事件（lenient 编译）、2 架机 coverage_scan、20 s、种子 5。"""
    import test_compile as tc
    from drone4d.baselines import BASELINES

    from awr.agentsdk.env.spec import EnvSpec, FleetEntry
    from awr.sim.events.compile import CompileOptions, compile_event

    it = Item("fast/synth-l1", ["D3-AC-35"])
    ev = copy.deepcopy(tc.EVENT)
    ev["annotations"] = [
        {"id": "meet_ab", "type": "proximity_encounter", "participants": ["person_a", "person_b"], "window": [5, 25]},
        {"id": "board_bc", "type": "board_vehicle", "participants": ["person_b", "vehicle_c"], "window": [70, 90]},
        {"id": "enter_c", "type": "enter_building", "participants": ["person_c"], "window": [35, 45], "params": {"anchor": "ev_door"}}]
    ev["brief"] = {"public_anchors": ["ev_plaza", "ev_door"],
                   "targets": [{"actor": "person_a", "description_en": "Blue top", "description_zh": "蓝色上衣"}]}
    d = tmp / "fastev"
    d.mkdir(parents=True, exist_ok=True)
    (d / "anchors.json").write_text(json.dumps(tc.OVERLAY))
    (d / "event.json").write_text(json.dumps(ev))
    r = compile_event(d / "event.json", synth, opts=CompileOptions(out=d / "v1", cache_dir=tmp / "cache-fast", strict=False))
    if r.status != "compiled":
        it.error = json.dumps(r.as_dict(), ensure_ascii=False)[:2000]
        return it
    it.digests["tape_content_sha256"] = r.tape["content_sha256"]
    fleet = (FleetEntry("uav-01", (-10.0, 50.0, 0.0)), FleetEntry("uav-02", (10.0, 50.0, 0.0)))
    _fast_pair(it, lambda n: EnvSpec(f"x01-fast-{n}", 5, synth, d / "v1", fleet, physics="fast", duration_s=FAST_SYNTH_S,
                                     out_dir=tmp / f"fast-{n}"),
               lambda u: BASELINES["coverage_scan"](), tmp)
    return it


def collect_fast_crop(worlds_root: Path, events_root: Path, tmp: Path) -> Item:
    """crop-handoff-001 L1 3 架机运行规格改为 `physics = fast`、30 s、种子 1，参考策略 track_share。"""
    from drone4d.baselines import BASELINES

    from awr.agentsdk.env.spec import EnvSpec

    it = Item("fast/crop-handoff-001-l1", ["D3-AC-35"])
    rs = json.loads(CROP_RUN_SPEC.read_text(encoding="utf-8"))
    rs["duration_s"], rs["ranked"], rs["physics"] = FAST_CROP_S, False, "fast"
    pol = rs["policy"]["name"]
    _fast_pair(it, lambda n: EnvSpec.from_run_spec(rs, run_id=f"x01-crop-fast-{n}", seed=1, worlds_root=worlds_root,
                                                   events_root=events_root, out_dir=tmp / f"crop-fast-{n}", physics="fast"),
               lambda u: BASELINES[pol](), tmp)
    return it


# ---------------------------------------------------------------------------------------------------- collect
def _run(items: dict[str, dict], name_hint: str, fn: Callable[[], Item | tuple]) -> Item | tuple | None:
    t0 = time.perf_counter()
    print(f"[{_now()}] {name_hint} ...", file=sys.stderr, flush=True)
    try:
        r = fn()
    except Exception as e:                                                          # 单项失败记录后继续
        it = Item(name_hint, [])
        it.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-3000:]}"
        it.seconds = time.perf_counter() - t0
        items[name_hint] = it.to_json()
        print(f"[{_now()}] {name_hint} error: {e}", file=sys.stderr, flush=True)
        return None
    it = r[0] if isinstance(r, tuple) else r
    it.seconds = time.perf_counter() - t0
    items[it.name] = it.to_json()
    bad = [k for k, v in it.checks.items() if not v]
    print(f"[{_now()}] {it.name} {it.seconds:.1f} s checks {'ok' if not bad and not it.error else bad or it.error[:200]}",
          file=sys.stderr, flush=True)
    return r


def collect(label: str, out: Path, scratch: Path, items_sel: Iterable[str], worlds: Iterable[str], published: Path | None) -> dict:
    sel = set(items_sel)
    worlds = list(worlds)
    os.environ.setdefault("AWR_EVENT_NAV", "detour")                    # Detour 后端缺失时报错而不是退回栅格（ADR-196）
    scratch.mkdir(parents=True, exist_ok=True)
    code0 = code_identity()
    doc = {"schema": SCHEMA, "label": label, "started_at": _now(), "machine": machine_info(label), "code": code0,
           "selection": {"items": sorted(sel), "worlds": worlds}, "items": {}}
    items: dict[str, dict] = doc["items"]
    world_dirs: dict[str, Path] = {}
    if published is not None and "world" not in sel:                    # 不重建时其余各项用已发布世界包；重建失败时不退回
        world_dirs.update({w: published / w for w in worlds if (published / w / "world.json").is_file()})
    built = scratch / "worlds"
    for wid in worlds:
        if "world" in sel:
            it = _run(items, f"world/{wid}", lambda wid=wid: collect_world(wid, built, published))
            if it is not None and not it.error:
                world_dirs[wid] = built / wid
        if "navmesh" in sel and wid in world_dirs:
            _run(items, f"navmesh/{wid}", lambda wid=wid: collect_navmesh(wid, world_dirs[wid]))
    crop = world_dirs.get(CROP_ID)
    crop_events: Path | None = None
    if "tape" in sel:
        _run(items, "tape/terrain-det-43", lambda: collect_tape_synth(scratch / "tape-terrain"))
        if crop is not None:
            r = _run(items, "tape/crop-handoff-001", lambda: collect_tape_crop(crop, scratch / "tape-crop"))
            if isinstance(r, tuple) and r[1] is not None:
                crop_events = r[1].parent
    synth: Path | None = None
    if "vehicle" in sel or "fast" in sel:
        import pytest
        from synthworld import build_synth_world

        mp = pytest.MonkeyPatch()
        try:
            synth = build_synth_world(scratch / "synth", mp)
        except Exception as e:                                          # 合成世界包构建失败：依赖它的各项记为出错
            items["synth-glb"] = {"ac": [], "digests": {}, "checks": {}, "info": {}, "error": f"{type(e).__name__}: {e}"}
        finally:
            mp.undo()
    if "vehicle" in sel:
        _run(items, "vehicle/synth-cross-20", collect_vehicle_cross)
        if crop is not None:
            _run(items, "vehicle/crop-bg-60", lambda: collect_vehicle_crop(crop))
        if synth is not None:
            _run(items, "vehicle/synth-vehicle-track", lambda: collect_vehicle_track(synth, scratch / "vt"))
    if "fast" in sel:
        if synth is not None:
            _run(items, "fast/synth-l1", lambda: collect_fast_synth(synth, scratch / "fast"))
        if crop is not None:
            if crop_events is None:                                     # tape 项未执行时单独编译一次
                r = compile_crop_handoff(crop, scratch / "tape-crop" / "crop-handoff-001")
                crop_events = scratch / "tape-crop" if r.status == "compiled" else None
            if crop_events is not None:
                _run(items, "fast/crop-handoff-001-l1", lambda: collect_fast_crop(crop.parent, crop_events, scratch / "fast"))
    code1 = code_identity()
    doc["code_stable"] = code1["tree_sha256"] == code0["tree_sha256"]
    doc["finished_at"] = _now()
    doc["ok"] = doc["code_stable"] and all(not v.get("error") and all(v["checks"].values()) for v in items.values())
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"crossmachine-{label}.json"
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(str(p))
    return doc


# ---------------------------------------------------------------------------------------------------- compare
def _dispatch(d: dict) -> str:
    return ((d["machine"].get("numpy_simd") or {}).get("dispatch")) or d["machine"]["cpu"]["isa"]


def isa_pair(a: dict, b: dict) -> dict:
    """配对方式：`cross` = 两台 CPU 分别为 AVX2 与 AVX-512 且 numpy 各按硬件派发（真实跨 ISA）；`same` = 两侧 numpy 派发档相同
    （D3-AC-35 的"同 ISA 的第二台机器复测"）；其余为 `other`。"""
    ha, hb = a["machine"]["cpu"]["isa"], b["machine"]["cpu"]["isa"]
    na, nb = _dispatch(a), _dispatch(b)
    pair = {"AVX2", "AVX-512"}
    mode = "cross" if {ha, hb} == pair and {na, nb} == pair else "same" if na == nb else "other"
    return {"a": ha, "b": hb, "numpy_a": na, "numpy_b": nb, "mode": mode, "ok": mode != "other"}


def compare_item(a: dict | None, b: dict | None) -> dict:
    """一项的跨机比对：两侧均存在、均无错误、本机复核全部通过，且 `digests` 与 `files` 逐键相同。"""
    if a is None or b is None:
        return {"status": "missing", "missing_on": "a" if a is None else "b"}
    errs = {k: v["error"][:500] for k, v in (("a", a), ("b", b)) if v.get("error")}
    if errs:
        return {"status": "error", "errors": errs}
    failed = {s: sorted(k for k, ok in v["checks"].items() if not ok) for s, v in (("a", a), ("b", b))}
    differ = sorted(k for k in set(a["digests"]) | set(b["digests"]) if a["digests"].get(k) != b["digests"].get(k))
    fa, fb = a.get("files", {}), b.get("files", {})
    fdiff = sorted(k for k in set(fa) | set(fb) if fa.get(k) != fb.get(k))
    status = "match" if not differ and not fdiff and not any(failed.values()) else "differ" if differ or fdiff else "check_failed"
    out = {"status": status, "n_digests": len(a["digests"]), "n_files": len(fa), "digests": a["digests"] if status == "match" else None}
    if differ:
        out["differ"] = {k: [a["digests"].get(k), b["digests"].get(k)] for k in differ}
    if fdiff:
        out["files_differ"] = fdiff[:200]
    if any(failed.values()):
        out["checks_failed"] = {k: v for k, v in failed.items() if v}
    return out


def compare(a: dict, b: dict) -> dict:
    for d in (a, b):
        if d.get("schema") != SCHEMA:
            raise ValueError(f"not a crossmachine fingerprint: {d.get('schema')!r}")
    ca, cb = a["code"], b["code"]
    code_diff = sorted(k for k in set(ca["files"]) | set(cb["files"]) if ca["files"].get(k) != cb["files"].get(k))
    code = {"same": ca["tree_sha256"] == cb["tree_sha256"], "tree_sha256": [ca["tree_sha256"], cb["tree_sha256"]],
            "n_files": ca["n_files"], "differ": code_diff[:100], "stable": [a.get("code_stable"), b.get("code_stable")],
            "raw_manifest_same": ca.get("raw_manifest_sha256") == cb.get("raw_manifest_sha256")}
    names = sorted(set(a["items"]) | set(b["items"]))
    items = {n: {"ac": sorted(set((a["items"].get(n) or {}).get("ac", [])) | set((b["items"].get(n) or {}).get("ac", []))),
                 **compare_item(a["items"].get(n), b["items"].get(n))} for n in names}
    isa = isa_pair(a, b)
    acs: dict[str, dict] = {}
    for kind in ITEMS:
        rel = [n for n in names if n.split("/", 1)[0] == kind]
        ac = AC_OF[kind]
        applicable = AC_PAIR[ac] == isa["mode"]
        good = bool(rel) and all(items[n]["status"] == "match" for n in rel)
        acs[ac] = {"items": rel, "pair": AC_PAIR[ac], "verdict": ("pass" if good else "fail") if applicable else "n/a",
                   "ok": good if applicable else None}
        for n in rel:
            if not applicable and items[n]["status"] in ("differ", "missing"):
                items[n]["informational"] = True                      # 配对方式不适用于该条款：差异与缺项只记录
    broken = [n for n, v in items.items() if v["status"] in ("missing", "error", "check_failed") and not v.get("informational")]
    applicable = [e for e in acs.values() if e["verdict"] != "n/a"]
    ok = (code["same"] and all(code["stable"]) and isa["ok"] and not broken and bool(applicable)
          and all(e["verdict"] == "pass" for e in applicable))

    def side(d: dict) -> dict:
        m = d["machine"]
        timing = {n: v["info"].get("build_s_excl_registration") for n, v in d["items"].items() if n.startswith("world/") and v.get("info")}
        return {"label": d["label"], "cpu": m["cpu"]["model"], "isa": m["cpu"]["isa"], "flags": m["cpu"]["flags"],
                "cgroup_cpus": m.get("cgroup_cpus"), "numpy_simd": m.get("numpy_simd"), "versions": m.get("versions"),
                "navsvc": m.get("navsvc"), "started_at": d.get("started_at"), "finished_at": d.get("finished_at"),
                "world_build_s_excl_registration": timing}

    return {"schema": REPORT_SCHEMA, "created_at": _now(), "a": side(a), "b": side(b), "isa_pair": isa, "code": code,
            "acs": acs, "items": items, "ok": ok}


def render_md(rep: dict) -> str:
    """对拍报告的 Markdown 摘要（企业级文案；逐项一行，摘要值截取前 12 位）。"""
    a, b, isa = rep["a"], rep["b"], rep["isa_pair"]
    st = {"match": "一致", "differ": "不一致", "missing": "缺项", "error": "出错", "check_failed": "本机复核未通过"}
    mode = {"cross": "跨 ISA（一台 AVX2、一台 AVX-512，numpy 各按硬件派发）", "same": f"同 ISA（两侧 numpy 均按 {isa['numpy_a']} 派发）",
            "other": "不构成跨 ISA 或同 ISA 配对"}[isa["mode"]]
    verdict = {"pass": "通过", "fail": "未通过", "n/a": "本配对不适用"}
    lines = ["# 跨机器确定性对拍报告（X01）", "",
             f"- 生成时间：{rep['created_at']}（UTC）；结论：{'适用条款全部通过' if rep['ok'] else '存在未通过、缺项或出错'}",
             f"- A：{a['label']}，{a['cpu']}，硬件 {a['isa']}，numpy 派发 {isa['numpy_a']}；B：{b['label']}，{b['cpu']}，硬件 {b['isa']}，"
             f"numpy 派发 {isa['numpy_b']}；配对方式：{mode}",
             f"- 代码快照：{rep['code']['tree_sha256'][0][:16]}（{rep['code']['n_files']} 个文件），两侧{'相同' if rep['code']['same'] else '不同'}；"
             f"运行期间代码未变化：{'是' if all(rep['code']['stable']) else '否'}", "",
             "| 验收编号 | 条款要求的配对 | 对拍项 | 结论 |", "|---|---|---|---|"]
    for ac, e in sorted(rep["acs"].items()):
        lines.append(f"| {ac} | {'跨 ISA' if e['pair'] == 'cross' else '同 ISA'} | {'、'.join(e['items']) or '无'} | {verdict[e['verdict']]} |")
    lines += ["", "| 对拍项 | 结论 | 摘要项数 | 内容文件数 | 关键摘要 |", "|---|---|---|---|---|"]
    keys = ("contentVersion", "person.bin", "vehicle.bin", "navgraph_content_sha256", "navsvc_replies_sha256", "content_sha256",
            "scripted_sha256", "report.lanes.digest", "sim_digest", "pose_sha256")
    for n, v in rep["items"].items():
        dg = v.get("digests") or {}
        key = "；".join(f"{k} {str(dg[k])[:12]}" for k in keys if k in dg)
        s_ = st.get(v["status"], v["status"]) + ("（不适用条款，只记录）" if v.get("informational") else "")
        lines.append(f"| {n} | {s_} | {v.get('n_digests', '')} | {v.get('n_files') or ''} | {key} |")
    bad = {n: v for n, v in rep["items"].items() if v["status"] != "match"}
    if bad:
        lines += ["", "## 不一致明细", ""]
        for n, v in bad.items():
            det = {k: v[k] for k in ("differ", "files_differ", "checks_failed", "errors", "missing_on") if k in v}
            lines.append(f"- {n}{'（不适用条款，只记录）' if v.get('informational') else ''}：{json.dumps(det, ensure_ascii=False)[:1500]}")
    tm = [(s["label"], n, t) for s in (a, b) for n, t in s["world_build_s_excl_registration"].items() if t]
    if tm:
        lines += ["", "世界包构建耗时（不含配准，各阶段加调平 QA，s；只记录不比对）："]
        lines += [f"- {lab} {n}：{t}" for lab, n, t in tm]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="crossmachine.py", description="cross-machine determinism check (X01)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("collect", help="compute this machine's fingerprint")
    p.add_argument("--label", required=True, help="machine label written into the fingerprint (e.g. local, dmax)")
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--scratch", type=Path, help="working directory for rebuilt worlds and tapes (default: a temporary directory)")
    p.add_argument("--items", default=",".join(ITEMS))
    p.add_argument("--worlds", default=",".join(DEFAULT_WORLDS))
    p.add_argument("--published-worlds", type=Path, default=ROOT / "worlds",
                   help="published worlds root (used when the world item is not selected, and for the published-version note)")
    p.add_argument("--keep", action="store_true", help="keep the scratch directory")
    p = sub.add_parser("compare", help="compare two fingerprints and write the report")
    p.add_argument("a", type=Path)
    p.add_argument("b", type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--md", type=Path)
    a = ap.parse_args(argv)
    if a.cmd == "collect":
        sel = [x for x in a.items.split(",") if x]
        if not sel or set(sel) - set(ITEMS):
            print(f"error: --items must be a subset of {','.join(ITEMS)}", file=sys.stderr)
            return 2
        own = a.scratch is None
        scratch = Path(tempfile.mkdtemp(prefix="x01-")) if own else a.scratch
        try:
            doc = collect(a.label, a.out, scratch, sel, [x for x in a.worlds.split(",") if x],
                          a.published_worlds if a.published_worlds and a.published_worlds.is_dir() else None)
        finally:
            if not a.keep:                                              # 只删除本工具建立的目录（世界包副本不在本机长期保留）
                for q in ([scratch] if own else [scratch / s for s in SCRATCH_DIRS]):
                    shutil.rmtree(q, ignore_errors=True)
        return 0 if doc["ok"] else 1
    try:
        rep = compare(json.loads(a.a.read_text(encoding="utf-8")), json.loads(a.b.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(rep, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    md = render_md(rep)
    if a.md:
        a.md.write_text(md, encoding="utf-8")
    print(md)
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
