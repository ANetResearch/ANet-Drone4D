"""第一批四类事件集与 dev/test 划分登记（D3-AC-62、D3-AC-43；M23-FR-082，M27-FR-113、FR-114；AWR-05 §5.6.9；E10）。

1. 登记表 `configs/bench.d3/split.yaml`：赛道与 `board-v1/board.json` 的 lanes 一一对应；每条赛道 ≥ 1 个 dev 与恰好
   `events_per_lane` 个 test 事件（共 ≥ 12 个新事件），事件 id 唯一，哈希字段为 64 位十六进制，test 划分摘要可按
   `awr.bench.split.SplitRegistry` 的口径计算。
2. 仓库内容：dev 事件源文件入库且与登记的 `source_sha256`、`overlay_sha256` 逐字节一致，event_id、event_type、version、世界与
   登记一致；test 事件不入库（`configs/`、`tests/` 下没有 test 事件目录或源文件，登记表之外的入库文件不引用 test 事件 id）。
3. 门 1（不需要世界包）：dev 事件 schema、引用、类型必备配置（`E-TYPE`）与外观可区分性（`E-APPEARANCE`）通过；Re-ID 与车辆追踪
   事件的目标与每名干扰者属性距离 ≥ 2 且至少共享 1 个属性（难负例）。
4. 许可登记：`assets/actors/LICENSES.md` 登记 appearance_lib@1（自制、`LicenseRef-ANet-OSL`）与库中出现的全部程序化资产与骨架。
5. crop 实数据（有 `worlds/oryahovo-crop1`、open3d 与 navsvc 时）：dev 事件以 strict、ranked（Detour）编译通过，`content_sha256`、
   `truth_sha256`、`dsl_sha256` 与登记一致；按类型核对关键事件：Re-ID ≥ 2 处 ≥ 5 s 遮挡与等数的 `target_reacquire`；交汇与交接
   `meet`、`handover`、`disperse` 与 `handover during meet`，可导出 OpenLABEL；车辆追踪目标车的 `turn`、`stop`、`park` 与车道审计；
   目标搜索 60 s 窗口计数、`brief.json` 与 `bench/task.v1` 对实际位置泄漏扫描 0 命中。
6. 数据根（设 `AWR_E10_DATA_ROOT` 时，在 dmax 上运行）：test 事件只在 `hidden/<eid>/event/v1/`（目录 0700、文件 0600），哈希与登记
   一致；`events/<eid>/` 下只有对研究者开放的 `brief.json`；`bench/splits.json` 的登记与本表一致、摘要相同；HiddenStore 无错放；
   目标搜索 test 事件的简报对实际位置 0 命中。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from pathlib import Path

import numpy as np
import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[3]
SPLIT = ROOT / "configs" / "bench.d3" / "split.yaml"
BOARD = ROOT / "configs" / "bench.d3" / "board-v1" / "board.json"
EVENTS = ROOT / "configs" / "events.d3"
LICENSES = ROOT / "assets" / "actors" / "LICENSES.md"
CROP = ROOT / "worlds" / "oryahovo-crop1"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
HASH_FIELDS = ("source_sha256", "overlay_sha256", "dsl_sha256", "content_sha256", "truth_sha256")


def _reg() -> dict:
    return yaml.safe_load(SPLIT.read_text(encoding="utf-8"))


def _rows(split: str | None = None) -> list[dict]:
    return [r for r in _reg()["events"] if split is None or r["split"] == split]


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


DEV = [r["event_id"] for r in _rows("dev")]


def _dev(eid: str) -> dict:
    return next(r for r in _rows("dev") if r["event_id"] == eid)


# ---------------------------------------------------------------------------------------------------- 1. 登记表
def test_registration_covers_every_lane() -> None:
    reg, board = _reg(), json.loads(BOARD.read_text(encoding="utf-8"))
    assert reg["schema"] == "drone4d.bench.split_set.v1" and reg["test_split_version"] == "v1"
    assert [(ln["lane"], ln["event_type"]) for ln in reg["lanes"]] == [(ln["lane"], ln["event_type"]) for ln in board["lanes"]]
    lane_type = {ln["lane"]: ln["event_type"] for ln in reg["lanes"]}
    rows = reg["events"]
    ids = [r["event_id"] for r in rows]
    assert len(ids) == len(set(ids))
    for r in rows:
        assert r["split"] in ("dev", "test") and r["version"] == 1 and lane_type[r["lane"]] == r["event_type"], r
        assert all(HEX64.match(str(r[k])) for k in HASH_FIELDS), r
        assert (r["source"] == "hidden") == (r["split"] == "test"), r
    for lane in lane_type:
        dev = [r for r in rows if r["lane"] == lane and r["split"] == "dev"]
        test = [r for r in rows if r["lane"] == lane and r["split"] == "test"]
        assert len(dev) >= 1 and len(test) == board["events_per_lane"] == 2, lane
    new = [r for r in rows if r["event_id"] != "crop-handoff-001"]                 # P0a 固定事件之外的第一批事件
    assert len(new) >= 12 and len([r for r in rows if r["split"] == "test"]) == len(lane_type) * board["events_per_lane"]


def test_registration_loads_into_split_registry(tmp_path: Path) -> None:
    """登记表逐条写入 `splits.json`（与 dmax 上的服务端登记同一调用），各事件的有效划分与 test 摘要可计算。"""
    from awr.bench.auth import AuditLog
    from awr.bench.split import SplitRegistry, read_audit

    reg = _reg()
    sr = SplitRegistry.for_root(tmp_path, audit=AuditLog(tmp_path / "audit"))
    for r in reg["events"]:
        sr.register(r["event_id"], r["split"], by="e10", version=reg["test_split_version"])
    assert sr.events("test") == sorted(r["event_id"] for r in reg["events"] if r["split"] == "test")
    assert all(sr.split_of(r["event_id"]) == r["split"] for r in reg["events"])
    assert HEX64.match(sr.digest()) and len(read_audit(tmp_path / "audit")) == len(reg["events"])


# ---------------------------------------------------------------------------------------------------- 2. 仓库内容
@pytest.mark.parametrize("eid", DEV)
def test_dev_source_matches_registration(eid: str) -> None:
    r = _dev(eid)
    d = ROOT / r["source"]
    assert d.parent == (EVENTS if eid == "crop-handoff-001" else EVENTS / r["lane"])
    assert _sha(d / "event.yaml") == r["source_sha256"] and _sha(d / "anchors.json") == r["overlay_sha256"]
    doc = yaml.safe_load((d / "event.yaml").read_text(encoding="utf-8"))
    assert (doc["event_id"], doc["event_type"], doc.get("version", 1)) == (eid, r["event_type"], r["version"])
    assert doc["world"]["id"] == _reg()["world"]["id"] == json.loads((d / "anchors.json").read_text())["world_id"]
    assert doc["anchors_ref"] == "event:anchors.json"


def test_test_events_are_not_in_repo() -> None:
    """test 事件只存隐藏区（M23-FR-082）：仓库中没有其目录或源文件，登记表之外不引用其 id。"""
    test_ids = {r["event_id"] for r in _rows("test")}
    for base in (ROOT / "configs", ROOT / "tests"):
        names = {p.name for p in base.rglob("*")}
        assert not (test_ids & names), sorted(test_ids & names)
    for p in (ROOT / "configs").rglob("*"):
        if p.is_file() and p != SPLIT and p.suffix in (".yaml", ".yml", ".json"):
            text = p.read_text(encoding="utf-8", errors="ignore")
            assert not [t for t in test_ids if t in text], p
    lanes = {r["lane"] for r in _rows()}
    assert {p.name for p in EVENTS.iterdir() if p.is_dir()} <= lanes | {"crop-handoff-001"}
    for lane in lanes:
        want = {r["event_id"] for r in _rows("dev") if r["lane"] == lane and Path(r["source"]).parent.name == lane}
        assert {p.name for p in (EVENTS / lane).iterdir()} == want


# ---------------------------------------------------------------------------------------------------- 3. 门 1
def _gate1(eid: str):
    from awr.sim.events import search
    from awr.sim.events.anchors import load_overlay, resolve_anchors
    from awr.sim.events.dsl import gate1, load_doc

    d = ROOT / _dev(eid)["source"]
    doc = load_doc(d / "event.yaml")
    anchors = resolve_anchors([], load_overlay(d / "anchors.json"))         # crop 世界包无锚点图层，锚点全部来自覆盖层
    doc, anchors = search.prepare(doc, anchors, d)
    return doc, gate1(doc, anchors)


@pytest.mark.parametrize("eid", DEV)
def test_dev_event_passes_gate1(eid: str) -> None:
    pytest.importorskip("jsonschema")
    from awr.sim.events import appearance as al

    doc, (plan, errs, _warns) = _gate1(eid)
    assert errs == [] and plan is not None, [e.as_dict() for e in errs]
    lib = al.load_library(al.LIB_PATH)
    rule = lib.rules.get(doc["event_type"])
    if rule is None:
        return
    acts = doc["actors"]
    (tgt,) = [a for a in acts if "target" in (a.get("roles") or ())]
    dis = [a for a in acts if "distractor" in (a.get("roles") or ())]
    assert len(dis) >= (3 if doc["event_type"] == "reid_occlusion" else 2)
    t = lib.by_id[tgt["appearance_ref"]]
    for a in dis:
        e = lib.by_id[a["appearance_ref"]]
        shared = [k for k in t.entry["attrs"] if t.entry["attrs"][k] == e.entry["attrs"][k]]
        assert lib.distance(t, e) >= 2 and shared and not lib.pair_whys(rule, t, e), (tgt["id"], a["id"])
    brief = {b["actor"]: b for b in doc["brief"]["targets"]}[tgt["id"]]
    assert (brief["description_zh"], brief["description_en"]) == (t.entry["description"]["zh"], t.entry["description"]["en"])


# ---------------------------------------------------------------------------------------------------- 4. 许可登记
def test_actor_asset_licences_registered() -> None:
    from awr.sim.events import appearance as al
    from awr.sim.events.dsl import CLASS_DEFAULTS

    text = LICENSES.read_text(encoding="utf-8")
    lib = json.loads(al.LIB_PATH.read_text(encoding="utf-8"))
    assert lib["lib_id"] in text and lib["license"]["spdx"] in text and lib["generator"]["spec_sha256"] in text
    assert lib["license"]["kind"] == "original" and lib["license"]["third_party"] == []
    assert all(e["license"]["spdx"] == lib["license"]["spdx"] for e in lib["entries"])
    used = {e["asset"]["generator"] for e in lib["entries"]} | {e["asset"]["rig"] for e in lib["entries"]}
    used |= {c["asset"] for c in CLASS_DEFAULTS.values()}
    assert all(f"`{a}`" in text for a in used), sorted(a for a in used if f"`{a}`" not in text)


# ---------------------------------------------------------------------------------------------------- 5. crop 实数据
@pytest.fixture(scope="module")
def crop_cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not (CROP / "world.json").exists():
        pytest.skip("needs worlds/oryahovo-crop1")
    pytest.importorskip("open3d")
    pytest.importorskip("zstandard")
    from awr.world.glb.derive.navmesh import navsvc_modules

    if not navsvc_modules().is_dir():
        pytest.skip("needs tools/navsvc node_modules (Detour)")
    return tmp_path_factory.mktemp("e10-cache")


def _key(truth: dict, typ: str, actor: str | None = None) -> list[dict]:
    return [k for k in truth["key_events"] if k["type"] == typ and (actor is None or actor in k.get("participants", []))]


@pytest.mark.parametrize("eid", DEV)
def test_dev_event_compiles_strict_on_crop(eid: str, crop_cache: Path, tmp_path: Path) -> None:
    from awr.sim.events.compile import CompileOptions, compile_event

    r0 = _dev(eid)
    r = compile_event(ROOT / r0["source"] / "event.yaml", CROP,
                      opts=CompileOptions(strict=True, ranked=True, out=tmp_path / "v1", cache_dir=crop_cache))
    assert r.status == "compiled" and r.reason is None, json.dumps(r.as_dict(), ensure_ascii=False)[:3000]
    out = r.out
    man = json.loads((out / "tape.json").read_text())
    rep = json.loads((out / "compile_report.json").read_text())
    truth = json.loads((out / "truth" / "r1" / "event_truth.json").read_text())
    assert man["provenance"]["generator_version"] == _reg()["generator_version"]
    assert man["header"]["world"]["content_version"] == _reg()["world"]["content_version"]
    assert (truth["dsl_sha256"], man["content_sha256"], rep["truth_summary"]["truth_sha256"]) == \
        (r0["dsl_sha256"], r0["content_sha256"], r0["truth_sha256"])
    assert all(c["verified"] for c in truth["annotation_checks"]) and all(c["verified"] for c in truth["relation_checks"])
    _CHECKS[r0["event_type"]](out, rep, truth)


def _check_reid(out: Path, rep: dict, truth: dict) -> None:
    occ = [o for o in rep["gate3"]["extra"]["reid_occlusion"]["occlusions"] if o["valid"]]
    assert len(occ) >= 2 and all(o["duration_ms"] >= 5000 for o in occ)
    assert len(_key(truth, "target_reacquire")) == len(occ)


def _check_rendezvous(out: Path, rep: dict, truth: dict) -> None:
    from awr.bench.export import openlabel

    if truth["event_id"] == "crop-handoff-001":                                     # P0a 形式：无物体、无 roles
        assert _key(truth, "board") and _key(truth, "enter_building")
        return
    assert _key(truth, "meet") and _key(truth, "handover") and _key(truth, "disperse")
    rel = {(k["src"].split("-", 1)[1], k["rel"], k["dst"].split("-", 1)[1]) for k in truth["key_relations"]}
    assert ("handover", "during", "meet") in rel and ("meet", "before", "disperse") in rel
    doc = json.loads(openlabel.export_event(out, out / "ol.json").read_text())
    assert doc["openlabel"]["relations"] and doc["openlabel"]["events"]


def _check_vehicle(out: Path, rep: dict, truth: dict) -> None:
    man = json.loads((out / "tape.json").read_text())
    assert man["report"]["lanes"]["audit"]["ok"] and rep["gate3"]["extra"]["vehicle_track"]["key_events_ok"]
    tgt = rep["gate3"]["extra"]["vehicle_track"]["target"]
    for typ in ("depart", "turn", "stop", "park"):
        assert _key(truth, typ, tgt), typ
    assert _key(truth, "enter_zone", tgt) and _key(truth, "exit_zone", tgt)


def _check_search(out: Path, rep: dict, truth: dict) -> None:
    from awr.agentsdk.task import task_from_event
    from awr.sim.events import search
    from awr.sim.events.tape import open_tape

    t = open_tape(out)
    f = np.concatenate([t.block_frames(i, background=False) for i in range(len(t.blocks))])
    i = [a["id"] for a in t.header["actors"]].index("target_p")
    present = np.isin(f["state"][:, i], (1, 2))
    xy = np.stack([f["x_mm"][present, i], f["y_mm"][present, i]], 1) / 1000.0
    wins = _key(truth, "target_window")
    assert len(wins) == math.ceil(present.sum() / 10 / 60.0) == 3
    brief = json.loads((out / "brief.json").read_text())
    task = task_from_event(out)
    assert search.leak_scan(brief, xy) == [] and search.leak_scan(task, xy) == []
    assert search.SEARCH_ANCHOR not in json.dumps(brief) + json.dumps(task)


_CHECKS = {"reid_occlusion": _check_reid, "rendezvous_handoff": _check_rendezvous, "vehicle_track": _check_vehicle,
           "target_search": _check_search}


# ---------------------------------------------------------------------------------------------------- 6. 数据根（dmax）
@pytest.fixture(scope="module")
def data_root() -> Path:
    p = os.environ.get("AWR_E10_DATA_ROOT")
    if not p:
        pytest.skip("set AWR_E10_DATA_ROOT to the bench data root (dmax) to check the published event set")
    return Path(p)


def test_data_root_hidden_layout(data_root: Path) -> None:
    from awr.bench.auth import AuditLog
    from awr.bench.split import HIDDEN_ONLY, HiddenStore, SplitRegistry

    reg = _reg()
    sr = SplitRegistry.for_root(data_root)
    hs = HiddenStore.for_root(data_root, AuditLog(data_root / "audit"))
    want = {r["event_id"]: r["split"] for r in reg["events"]}
    assert {k: v for k, v in sr.load()["events"].items() if k in want} == want
    assert sr.load()["test_split_version"] == reg["test_split_version"]
    assert hs.misplaced(sr, events_root=data_root / "events", dataset_root=data_root / "dataset") == []
    for r in reg["events"]:
        eid = r["event_id"]
        d = (hs.event_dir(eid, 1) if r["split"] == "test" else data_root / "events" / eid / "v1")
        man = json.loads((d / "tape.json").read_text())
        rep = json.loads((d / "compile_report.json").read_text())
        truth = json.loads((d / "truth" / "r1" / "event_truth.json").read_text())
        assert (truth["dsl_sha256"], man["content_sha256"], rep["truth_summary"]["truth_sha256"]) == \
            (r["dsl_sha256"], r["content_sha256"], r["truth_sha256"]), eid
        if r["split"] == "dev":
            continue
        assert _sha(d / "event.yaml") == r["source_sha256"] and _sha(d / "anchors.overlay.json") == r["overlay_sha256"]
        for p in [d, *d.rglob("*")]:
            mode = stat.S_IMODE(p.stat().st_mode)
            assert mode == (0o700 if p.is_dir() else 0o600), (p, oct(mode))
        pub = data_root / "events" / eid
        if pub.exists():
            names = {p.relative_to(pub).as_posix() for p in pub.rglob("*") if p.is_file()}
            assert names <= {"v1/brief.json"} and not (names & HIDDEN_ONLY), (eid, names)
        if r["event_type"] == "target_search":
            from awr.sim.events import search
            from awr.sim.events.tape import open_tape

            t = open_tape(d)
            f = np.concatenate([t.block_frames(i, background=False) for i in range(len(t.blocks))])
            i = [a["id"] for a in t.header["actors"]].index("target_p")
            present = np.isin(f["state"][:, i], (1, 2))
            xy = np.stack([f["x_mm"][present, i], f["y_mm"][present, i]], 1) / 1000.0
            brief = json.loads((pub / "v1" / "brief.json").read_text())
            assert search.leak_scan(brief, xy) == [] and brief["event_id"] == eid
