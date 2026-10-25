"""跨机器确定性对拍工具 `tools/bench/d3/crossmachine.py`（X01；D3-AC-02、03、07、33、35）。

1. 机器信息：`/proc/cpuinfo` 文本解析为型号、逻辑核数与 ISA（AVX-512 / AVX2 / baseline）。
2. 代码快照摘要：排除 __pycache__、.pyc 与 node_modules；任一文件改变即改变摘要。
3. 比对口径：两份指纹逐项比对（digests、files），本机复核失败、缺项、出错、代码快照不同或采集期间变化时结论不成立；配对方式
   （cross：硬件与 numpy 派发分别为 AVX2 与 AVX-512；same：numpy 派发档相同；other）决定各验收编号是否适用（02、03、07、33 要求
   cross，35 要求 same），不适用条款的差异只记录；numpy 派发档判定；Markdown 摘要；命令行 `compare` 的退出码与 `collect` 的参数校验。
4. 真实采集（轻量项）：合成十字路网 20 辆车 90 s 的车辆生成指纹同机两次相同，且两份只含该项的指纹按跨机口径比对为一致。
   完整采集（重建 crop 与 town、磁带、Fast L1）在本机与 dmax 各执行一次，记录见 X01 对拍报告。
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("crossmachine", REPO / "tools/bench/d3/crossmachine.py")
cm = importlib.util.module_from_spec(_spec)
sys.modules["crossmachine"] = cm
_spec.loader.exec_module(cm)

CPU_AVX2 = """processor\t: 0
model name\t: Intel(R) Xeon(R) CPU E5-2603 v4 @ 1.70GHz
flags\t\t: fpu sse4_2 avx avx2 fma bmi2
processor\t: 1
model name\t: Intel(R) Xeon(R) CPU E5-2603 v4 @ 1.70GHz
flags\t\t: fpu sse4_2 avx avx2 fma bmi2
"""
CPU_AVX512 = """processor\t: 0
model name\t: Intel(R) Xeon(R) Gold 6326 CPU @ 2.90GHz
flags\t\t: fpu sse4_2 avx avx2 fma avx512f avx512dq avx512bw avx512vl avx512_vnni
"""


# ---------------------------------------------------------------------------------------------------- 1. 机器信息
def test_cpu_info_isa_classification() -> None:
    a = cm.cpu_info(CPU_AVX2)
    assert a == {"model": "Intel(R) Xeon(R) CPU E5-2603 v4 @ 1.70GHz", "logical_cpus": 2, "isa": "AVX2",
                 "flags": ["sse4_2", "avx", "avx2", "fma"]}
    b = cm.cpu_info(CPU_AVX512)
    assert b["isa"] == "AVX-512" and b["logical_cpus"] == 1 and {"avx512f", "avx512_vnni"} <= set(b["flags"])
    assert cm.cpu_info("processor\t: 0\nflags\t\t: fpu sse2\n")["isa"] == "baseline"
    live = cm.cpu_info()
    assert live["isa"] in ("AVX2", "AVX-512", "baseline") and live["logical_cpus"] >= 1


# ---------------------------------------------------------------------------------------------------- 2. 代码快照摘要
def test_code_identity_tree_and_exclusions(tmp_path: Path) -> None:
    (tmp_path / "python/awr/__pycache__").mkdir(parents=True)
    (tmp_path / "python/awr/a.py").write_text("x = 1\n")
    (tmp_path / "python/awr/__pycache__/a.cpython-312.pyc").write_bytes(b"\0")
    (tmp_path / "tools/navsvc/src/node_modules/m").mkdir(parents=True)
    (tmp_path / "tools/navsvc/src/node_modules/m/i.js").write_text("1")
    (tmp_path / "tools/navsvc/src/s.mjs").write_text("export {}\n")
    paths = ("python/awr", "tools/navsvc/src", "missing/dir")
    c1 = cm.code_identity(tmp_path, paths)
    assert set(c1["files"]) == {"python/awr/a.py", "tools/navsvc/src/s.mjs"} and c1["n_files"] == 2
    assert c1["tree_sha256"] == cm.sha256_bytes(cm.canon(c1["files"]))
    assert cm.code_identity(tmp_path, paths)["tree_sha256"] == c1["tree_sha256"]
    (tmp_path / "python/awr/a.py").write_text("x = 2\n")
    assert cm.code_identity(tmp_path, paths)["tree_sha256"] != c1["tree_sha256"]


# ---------------------------------------------------------------------------------------------------- 3. 比对口径
def _fp(label: str, cpu: str, dispatch: str | None = None) -> dict:
    files = {"python/awr/a.py": "1" * 64}
    items = {
        "world/oryahovo-crop1": {"ac": ["D3-AC-02"], "digests": {"contentVersion": "7d8c8b895e55", "n_content_files": 2},
                                 "files": {"coordinate.json": "a" * 64, "geometry/nav/person.bin": "b" * 64},
                                 "checks": {"content_version_recomputes": True}, "info": {"build_s_excl_registration": 150.0 if label == "dmax" else 370.0}},
        "navmesh/oryahovo-crop1": {"ac": ["D3-AC-03", "D3-AC-33"], "digests": {"person.bin": "b" * 64, "vehicle.bin": "c" * 64},
                                   "checks": {"navsvc_two_processes_identical": True}, "info": {}},
        "tape/crop-handoff-001": {"ac": ["D3-AC-07"], "digests": {"content_sha256": "d" * 64}, "checks": {"provenance_detour": True}, "info": {}},
        "vehicle/synth-cross-20": {"ac": ["D3-AC-33"], "digests": {"sim_digest": "e" * 64}, "checks": {"repeat_identical": True}, "info": {}},
        "fast/synth-l1": {"ac": ["D3-AC-35"], "digests": {"pose_sha256": "f" * 64, "uav-01.acts.jsonl": "0" * 64},
                          "checks": {"same_machine_two_runs_bytewise": True}, "info": {"a_wall_s": 3.0 if label == "dmax" else 9.0}},
    }
    hw = cm.cpu_info(cpu)
    return {"schema": cm.SCHEMA, "label": label, "machine": {"label": label, "cpu": hw, "numpy_simd": {"dispatch": dispatch or hw["isa"]}},
            "code_stable": True,
            "code": {"tree_sha256": cm.sha256_bytes(cm.canon(files)), "n_files": 1, "files": files, "raw_manifest_sha256": "9" * 64},
            "items": items, "started_at": "2026-10-20T00:00:00Z", "finished_at": "2026-10-20T00:10:00Z"}


@pytest.fixture()
def pair() -> tuple[dict, dict]:
    return _fp("local", CPU_AVX2), _fp("dmax", CPU_AVX512)


def test_numpy_dispatch_levels() -> None:
    assert cm.numpy_dispatch({"X86_V3": True, "X86_V4": True}) == "AVX-512"
    assert cm.numpy_dispatch({"X86_V3": True, "X86_V4": False, "AVX512F": True}) == "AVX2"           # NPY_DISABLE_CPU_FEATURES=X86_V4 …
    assert cm.numpy_dispatch({"AVX2": True, "AVX512_SKX": True}) == "AVX-512"                       # numpy < 2.5 的特性名
    assert cm.numpy_dispatch({"AVX2": False}) == "baseline"
    live = cm._numpy_simd()
    assert live["dispatch"] in ("AVX2", "AVX-512", "baseline") and live["features"]


def test_compare_all_match_ignores_info(pair) -> None:
    a, b = pair
    rep = cm.compare(a, b)
    assert rep["ok"] and rep["code"]["same"]
    assert rep["isa_pair"] == {"a": "AVX2", "b": "AVX-512", "numpy_a": "AVX2", "numpy_b": "AVX-512", "mode": "cross", "ok": True}
    assert all(v["status"] == "match" for v in rep["items"].values())
    assert {ac: e["verdict"] for ac, e in rep["acs"].items()} == {"D3-AC-02": "pass", "D3-AC-03": "pass", "D3-AC-07": "pass",
                                                                  "D3-AC-33": "pass", "D3-AC-35": "n/a"}
    assert rep["acs"]["D3-AC-03"]["items"] == ["navmesh/oryahovo-crop1"]
    assert rep["items"]["world/oryahovo-crop1"]["n_files"] == 2
    assert rep["a"]["world_build_s_excl_registration"] == {"world/oryahovo-crop1": 370.0}        # 耗时只记录（两侧不同不影响结论）
    md = cm.render_md(rep)
    assert "适用条款全部通过" in md and "| D3-AC-02 | 跨 ISA | world/oryahovo-crop1 | 通过 |" in md and "contentVersion 7d8c8b895e55" in md
    assert "| D3-AC-35 | 同 ISA | fast/synth-l1 | 本配对不适用 |" in md


def test_compare_detects_digest_file_check_and_missing(pair) -> None:
    a, b = pair
    b = copy.deepcopy(b)
    b["items"]["tape/crop-handoff-001"]["digests"]["content_sha256"] = "x" * 64
    b["items"]["world/oryahovo-crop1"]["files"]["geometry/nav/person.bin"] = "y" * 64
    b["items"]["vehicle/synth-cross-20"]["checks"]["repeat_identical"] = False
    del b["items"]["fast/synth-l1"]
    b["items"]["navmesh/oryahovo-crop1"]["error"] = "NavSvcError: navsvc exited"
    rep = cm.compare(a, b)
    it = rep["items"]
    assert not rep["ok"]
    assert it["tape/crop-handoff-001"]["status"] == "differ" and it["tape/crop-handoff-001"]["differ"] == {"content_sha256": ["d" * 64, "x" * 64]}
    assert it["world/oryahovo-crop1"]["status"] == "differ" and it["world/oryahovo-crop1"]["files_differ"] == ["geometry/nav/person.bin"]
    assert it["vehicle/synth-cross-20"]["status"] == "check_failed" and it["vehicle/synth-cross-20"]["checks_failed"] == {"b": ["repeat_identical"]}
    assert it["fast/synth-l1"] == {"ac": ["D3-AC-35"], "status": "missing", "missing_on": "b", "informational": True}   # 35 在跨 ISA 配对下不适用
    assert it["navmesh/oryahovo-crop1"]["status"] == "error" and "navsvc exited" in it["navmesh/oryahovo-crop1"]["errors"]["b"]
    assert {ac: e["verdict"] for ac, e in rep["acs"].items()} == {"D3-AC-02": "fail", "D3-AC-03": "fail", "D3-AC-07": "fail",
                                                                  "D3-AC-33": "fail", "D3-AC-35": "n/a"}
    md = cm.render_md(rep)
    assert "## 不一致明细" in md and "缺项" in md and "本机复核未通过" in md


def test_pair_modes_decide_applicable_clauses(pair) -> None:
    a, b = pair
    # 跨 ISA：Fast 观测流 1 ulp 差异（X01 实测）只记录，不影响 02、03、07、33 的结论
    b1 = copy.deepcopy(b)
    b1["items"]["fast/synth-l1"]["digests"]["uav-01.obs.sha256"] = "2" * 64
    rep = cm.compare(a, b1)
    assert rep["ok"] and rep["items"]["fast/synth-l1"]["status"] == "differ" and rep["items"]["fast/synth-l1"]["informational"]
    assert "不一致（不适用条款，只记录）" in cm.render_md(rep)
    b4 = copy.deepcopy(b)                                                       # 适用条款的对拍项缺项：结论不成立
    del b4["items"]["world/oryahovo-crop1"]
    rep = cm.compare(a, b4)
    assert not rep["ok"] and "informational" not in rep["items"]["world/oryahovo-crop1"] and rep["acs"]["D3-AC-02"]["verdict"] == "fail"
    # 同 ISA：dmax 以 AVX2 档派发 numpy；35 适用，其余 n/a
    b2 = _fp("dmax-avx2", CPU_AVX512, dispatch="AVX2")
    rep = cm.compare(a, b2)
    assert rep["isa_pair"]["mode"] == "same" and rep["ok"]
    assert {ac: e["verdict"] for ac, e in rep["acs"].items()} == {"D3-AC-02": "n/a", "D3-AC-03": "n/a", "D3-AC-07": "n/a",
                                                                  "D3-AC-33": "n/a", "D3-AC-35": "pass"}
    b3 = copy.deepcopy(b2)                                                      # 同 ISA 复测只采集 fast 项：其余项缺项只记录
    b3["items"] = {"fast/synth-l1": b3["items"]["fast/synth-l1"]}
    rep = cm.compare(a, b3)
    assert rep["ok"] and rep["items"]["world/oryahovo-crop1"] == {"ac": ["D3-AC-02"], "status": "missing", "missing_on": "b",
                                                                  "informational": True}
    b2["items"]["fast/synth-l1"]["digests"]["uav-01.obs.sha256"] = "2" * 64
    rep = cm.compare(a, b2)
    assert not rep["ok"] and rep["acs"]["D3-AC-35"]["verdict"] == "fail" and "informational" not in rep["items"]["fast/synth-l1"]
    # 两台 AVX-512 硬件、numpy 派发不同：不构成任何一种配对
    rep = cm.compare(_fp("x", CPU_AVX512), _fp("y", CPU_AVX512, dispatch="AVX2"))
    assert rep["isa_pair"]["mode"] == "other" and not rep["ok"] and all(e["verdict"] == "n/a" for e in rep["acs"].values())


def test_compare_requires_same_code_and_stable_runs(pair) -> None:
    a, b = pair
    b2 = copy.deepcopy(b)
    b2["code"]["files"]["python/awr/a.py"] = "2" * 64
    b2["code"]["tree_sha256"] = cm.sha256_bytes(cm.canon(b2["code"]["files"]))
    rep = cm.compare(a, b2)
    assert not rep["ok"] and rep["code"]["same"] is False and rep["code"]["differ"] == ["python/awr/a.py"]
    b3 = copy.deepcopy(b)
    b3["code_stable"] = False                                                   # 采集期间代码被改动
    assert not cm.compare(a, b3)["ok"]
    with pytest.raises(ValueError):
        cm.compare(a, {**b, "schema": "other"})


def test_cli_compare_exit_codes_and_collect_argument_check(pair, tmp_path: Path) -> None:
    a, b = pair
    pa, pb = tmp_path / "a.json", tmp_path / "b.json"
    pa.write_text(json.dumps(a))
    pb.write_text(json.dumps(b))
    assert cm.main(["compare", str(pa), str(pb), "--out", str(tmp_path / "r.json"), "--md", str(tmp_path / "r.md")]) == 0
    rep = json.loads((tmp_path / "r.json").read_text())
    assert rep["schema"] == cm.REPORT_SCHEMA and rep["ok"] and (tmp_path / "r.md").read_text().startswith("# 跨机器确定性对拍报告")
    b["items"]["tape/crop-handoff-001"]["digests"]["content_sha256"] = "1" * 64
    pb.write_text(json.dumps(b))
    assert cm.main(["compare", str(pa), str(pb), "--out", str(tmp_path / "r2.json")]) == 1
    assert cm.main(["compare", str(pa), str(tmp_path / "none.json"), "--out", str(tmp_path / "r3.json")]) == 2
    assert cm.main(["collect", "--label", "x", "--out", str(tmp_path), "--items", "world,bogus"]) == 2


# ---------------------------------------------------------------------------------------------------- 4. 真实采集（轻量项）
def test_vehicle_cross_fingerprint_is_reproducible() -> None:
    pytest.importorskip("scipy")
    i1, i2 = cm.collect_vehicle_cross(), cm.collect_vehicle_cross()
    assert i1.checks == {"repeat_identical": True, "audit_ok": True} and i1.error is None
    assert i1.digests == i2.digests and set(i1.digests) == {"specs_sha256", "sim_digest", "xy_sha256"}
    a, b = _fp("local", CPU_AVX2), _fp("dmax", CPU_AVX512)
    a["items"] = {i1.name: i1.to_json()}
    b["items"] = {i2.name: i2.to_json()}
    rep = cm.compare(a, b)
    assert rep["items"]["vehicle/synth-cross-20"]["status"] == "match" and rep["acs"]["D3-AC-33"]["ok"]
    assert not rep["acs"]["D3-AC-02"]["ok"] and not rep["ok"]                   # 未采集的验收项不计为通过，总结论不成立
