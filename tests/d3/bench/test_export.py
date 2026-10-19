"""数据集导出（D3-AC-63；M27-AC-012、013；M27-FR-064–068、072–074；B05 派生物保留）：

- 三层目录（事件、真值、运行）、`index.json`、`world_manifest.json`、`run_manifest.json` 与 `export_manifest.json` 通过 schema 校验，
  许可与生成工具声明字段齐全；
- 逐文件导出（RGB、`depth_log16`、实例与语义分割、LiDAR、导航解、运行层 jsonl、bag）与 `raw.mcap`、真值文件内容一致：本测试以
  `mcap` 读取器独立解码来源逐项比对，`verify_export` 同时通过；
- `depth_log16` 在 [0.3, 3000] m 对数均匀采样的往返相对误差 ≤ 7.1e-5、100 m 处绝对误差 ≤ 0.71 cm，无回波与越界写 65535；
- bag 满足 §5.4.6 合同（每帧 20,000 点、无回波零占位、IMU 单位 g、相机取 `slam_cam`）；
- test 划分与评测运行（`ranked_eval`、`kind = submission`）导出返回 691 并写审计，数据集目录中不出现任何产物；
- 两次导出逐字节一致（导出清单的生成时刻除外）；
- 派生物按保留期清理：bag 7 天、逐文件 30 天，清理后 697；来源 Run 清理后导出 697；
- 真实 Env L2-depth 运行（CPU 替身渲染）可按同一流程导出并核对一致。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("mcap_protobuf")
pytest.importorskip("foxglove_schemas_protobuf")
pytest.importorskip("zstandard")

from awr.bench import retention
from awr.bench.auth import AuditLog, Principal
from awr.bench.export.dataset import (
    LOG16_SENTINEL,
    ExportError,
    artifact_items,
    check_export_manifest,
    check_index,
    depth_log16_decode,
    depth_log16_encode,
    export_run,
    main,
    png_gray,
    read_png_gray,
    verify_export,
)
from awr.bench.recorder.frames_idx import build_frames_idx
from awr.bench.recorder.lidar import LIDAR_RECORD
from awr.bench.recorder.read import FrameStore
from awr.bench.recorder.writer import RunWriter
from awr.bench.split import SplitAccessDenied, SplitRegistry

ROOT = Path(__file__).resolve().parents[3]
RID, EID, WID = "run-exp-0001", "ev-synth-001", "w-synth"
H = "a" * 64
N_FRAMES = 10
SLOTS = 20_000
CAM = (4, 6)                                                   # (h, w)
WORLD_MANIFEST = {"schema": "awr.world_manifest.v1", "world_id": WID, "contentVersion": "1.0.0", "public": True,
                  "license": {"dataset": "CC-BY-4.0", "citation": "Mitko @bg3d, CC-BY-4.0", "url": "https://sketchfab.com/bg3d",
                              "notice": "Contains modified CC-BY-4.0 material", "redistribution": False,
                              "derivedFrom": [{"name": "OSM", "version": "2026-09", "license": "ODbL-1.0", "use": "anchors",
                                               "attribution": "OpenStreetMap contributors", "sha256": "b" * 64}],
                              "attribution": {"en": "Model by Mitko @bg3d (CC-BY-4.0)", "zh": "模型作者 Mitko @bg3d（CC-BY-4.0）"}}}


def _jpeg(i: int) -> bytes:
    return b"\xff\xd8" + np.random.default_rng(i).bytes(500 + i) + b"\xff\xd9"


def _depth(seq: int) -> np.ndarray:
    d = np.random.default_rng(100 + seq).uniform(0.1, 4000.0, CAM).astype(np.float32)
    d[0, 0], d[0, 1], d[0, 2], d[1, 0] = 0.0, np.nan, np.inf, 0.3
    d[1, 1], d[1, 2] = 3000.0, 100.0
    return d


def _lidar_points(seq: int) -> np.ndarray:
    slots = np.arange(seq % 7, SLOTS, 37)                                          # 有效点落在槽位网格上，其余槽位无回波
    pts = np.zeros(len(slots), LIDAR_RECORD)
    rng = np.random.default_rng(200 + seq)
    pts["x"], pts["y"], pts["z"] = rng.uniform(1, 50, len(slots)), rng.uniform(-5, 5, len(slots)), rng.uniform(-2, 2, len(slots))
    pts["offset_time"] = slots * 100_000_000 // SLOTS
    pts["reflectivity"] = rng.integers(1, 150, len(slots))
    pts["line"] = slots & 3
    return pts


def _manifest(split: str = "dev") -> dict:
    fleet = [{"uav_id": u, "vehicle_ref": "p600_mid360_inv", "start": {"kind": "nest", "nest_id": "n1"}} for u in ("uav-01", "uav-02")]
    spec = {"schema": "drone4d.bench.run_spec.v1", "experiment": {"name": "exp-1"}, "world": {"id": WID}, "event": {"id": EID}, "fleet": fleet,
            "policy": {"kind": "reference", "name": "coverage"}, "obs_level": "L2-depth", "physics": "fast"}
    return {"schema": "drone4d.bench.run_manifest.v1", "run_id": RID, "experiment_id": "exp-1", "attempt": 1, "restart_count": 0, "spec": spec,
            "seed": 3, "seed_commit": hashlib.sha256(f"{RID}:3".encode()).hexdigest(), "config_sha256": H, "spec_sha256": H, "run_hash": H,
            "versions": {"git_sha": "abc1234", "renderer": "0.1.0"}, "determinism_level": "2", "split": split, "ranked": False,
            "ranked_valid": None, "invalid_reasons": [], "timing": {"wall_s": 1.0, "sim_s": 1.0, "rtf": 1.0}}


def _instance(seq: int) -> np.ndarray:
    a = np.zeros(CAM, np.uint16)
    a[1:3, 2:5] = 300 + seq
    a[0, 0] = 65000
    return a


def _semantic(seq: int) -> np.ndarray:
    return (np.arange(CAM[0] * CAM[1], dtype=np.uint8).reshape(CAM) + seq) % 40


def build_run(d: Path, *, split: str = "dev", world: dict | None = None) -> Path:
    import zstandard

    zc = zstandard.ZstdCompressor(level=3)
    d.mkdir(parents=True)
    wm = WORLD_MANIFEST if world is None else world
    with RunWriter(d / "raw.mcap") as w:
        w.metadata("run", {"run_id": RID, "attempt": "1"})
        w.attachment("tape.bin", b"TAPE" + bytes(range(64)), "application/octet-stream")
        w.attachment("tape.json", json.dumps({"content_sha256": "c" * 64}).encode())
        w.attachment("world_manifest.json", json.dumps(wm, sort_keys=True, separators=(",", ":")).encode())
        w.attachment("run_spec.json", json.dumps(_manifest()["spec"], sort_keys=True).encode())
        for seq in range(N_FRAMES):
            t0 = seq * 100_000_000
            for u in ("uav-01", "uav-02"):
                w.write_msgpack(f"/uav/{u}/obs/nav", "awr.bench.NavSolution", seq, t0, {
                    "t_ns": t0, "pos_enu_m": [1.0 * seq, 2.0, 30.0 + 0.1 * seq], "vel_enu_mps": [1.0, 0.0, 0.0], "q_wb_xyzw": [0.0, 0.0, 0.0, 1.0],
                    "omega_b_rps": [0.0, 0.0, 0.01], "pos_std_m": [0.5, 0.5, 1.0], "fix": "single", "battery_soc": 0.9 - 0.01 * seq})
                w.write_json(f"/uav/{u}/action", "drone4d.bench.act.v1", seq, t0, json.dumps(
                    {"k": seq, "apply_tick": seq + 1, "act": {"nav": {"kind": "hold"}}, "acks": []}, sort_keys=True, separators=(",", ":")).encode())
            w.write_camera("uav-01", "cam0", seq, t0 + 50_000_000, _jpeg(seq), depth_m=_depth(seq), period_ns=100_000_000)
            w.write_camera("uav-01", "slam_cam", seq, t0 + 30_000_000, _jpeg(1000 + seq), period_ns=100_000_000, agent_delivered=False)
            w.write_camera("uav-02", "cam0", seq, t0 + 50_000_000, _jpeg(2000 + seq), period_ns=100_000_000)
            pts = _lidar_points(seq)
            w.write_lidar("uav-01", "lidar0", seq, t0, {"schema": "awr.sensor.lidar_frame.v2", "sensor_id": "lidar0", "frame_seq": seq,
                                                        "timebase_ns": t0, "point_count": len(pts), "point_dt_ns": 5000,
                                                        "slots_per_frame": SLOTS, "sync_type": "sim"}, pts, period_ns=100_000_000)
            acc = np.tile(np.array([0.1, -0.2, 9.80665], "<f4"), (20, 1))
            gyr = np.tile(np.array([0.0, 0.01, 0.02 * seq], "<f4"), (20, 1))
            w.write_imu("uav-01", "lidar0_imu", seq, t0, {"sensor_id": "lidar0_imu", "seq": seq, "t0_ns": t0, "dt_ns": 5_000_000, "n": 20,
                                                          "acc_mps2": acc.tobytes(), "gyro_rps": gyr.tobytes(),
                                                          "mount_nominal": {"xyz_m": [0, 0, 0], "rpy_deg": [0, 0, 0]}})
            if seq % 3 == 0:
                w.write_json("/net/records", "awr.bench.net_record.v1", seq, t0 + 1, json.dumps(
                    {"src": "uav-01", "dst": "uav-02", "bytes": 100 + seq, "delivered": True}, sort_keys=True, separators=(",", ":")).encode())
        w.write_json("/uav/uav-02/report", "drone4d.bench.report.v1", 4, 450_000_000, json.dumps(
            {"uav": "uav-02", "k_obs": 4, "apply_tick": 5, "t_apply_ns": 450_000_000, "report": {"type": "sighting"}}, sort_keys=True,
            separators=(",", ":")).encode())
    build_frames_idx(d / "raw.mcap", d / "frames.idx")
    with RunWriter(d / "truth" / f"run_{RID}.mcap") as t:
        t.metadata("run", {"run_id": RID, "layer": "truth"})
        for seq in range(N_FRAMES):
            rec = {"schema": "drone4d.frame.v1", "frame_id": f"{RID}/uav-01/cam0/{seq}", "kind": "camera",
                   "agent_view": {"sensor_id": "cam0", "seq": seq, "width": CAM[1], "height": CAM[0]},
                   "ground_truth": {"instance": {"dtype": "u16", "codec": "zstd", "ref": "instance.u16.zst"},
                                    "semantic": {"dtype": "u8", "codec": "zstd", "ref": "semantic.u8.zst"}, "objects": []}}
            t.write_camera_truth("uav-01", "cam0", seq, seq * 100_000_000 + 50_000_000, rec, {
                "instance.u16.zst": zc.compress(_instance(seq).astype("<u2").tobytes()), "semantic.u8.zst": zc.compress(_semantic(seq).tobytes())})
    (d / "run_manifest.json").write_text(json.dumps(_manifest(split), indent=1, sort_keys=True) + "\n", encoding="utf-8")
    (d / "evaluation.json").write_text(json.dumps({"run_id": RID, "ec": {"value": 0.5, "coverage": 0.4}, "ranked_valid": True}), encoding="utf-8")
    (d / "task.json").write_text(json.dumps({"schema": "drone4d.bench.task.v1", "event_id": EID}), encoding="utf-8")
    return d


@pytest.fixture()
def src(tmp_path: Path) -> Path:
    return build_run(tmp_path / "env-runs" / RID)


def _msgs(path: Path, topics=None) -> list[tuple[str, int, int, bytes]]:
    from mcap.reader import make_reader

    with Path(path).open("rb") as f:
        return [(c.topic, int(m.sequence), int(m.log_time), bytes(m.data)) for _s, c, m in make_reader(f).iter_messages(topics=topics)]


def _export(src: Path, root: Path, **kw):
    kw.setdefault("formats", ("files", "bag"))
    kw.setdefault("layers", ("obs", "run", "truth"))
    return export_run(src, dataset_root=root / "dataset", registry=SplitRegistry.for_root(root), audit=AuditLog(root / "audit"), **kw)


# ---------------------------------------------------------------------- 三层目录与 schema
def test_three_tier_layout_and_schemas(src: Path, tmp_path: Path) -> None:
    import jsonschema

    from awr.bench.eval.evaluator import check_run_manifest

    root = tmp_path / "d3"
    res = _export(src, root)
    ev = root / "dataset" / EID
    assert res.event_dir == ev and res.run_dir == ev / "runs" / RID
    # 运行层：持久文件与来源逐字节相同，运行清单通过 schema
    for n in ("run_manifest.json", "raw.mcap", "frames.idx", "evaluation.json"):
        assert (res.run_dir / n).read_bytes() == (src / n).read_bytes(), n
    assert check_run_manifest(json.loads((res.run_dir / "run_manifest.json").read_text())) == []
    # 事件层：磁带为 raw.mcap 附件原样；task.json；世界清单
    from awr.bench.recorder.read import run_attachments

    att = run_attachments(src / "raw.mcap")
    assert (ev / "event" / "tape.bin").read_bytes() == att["tape.bin"] and (ev / "event" / "tape.json").read_bytes() == att["tape.json"]
    assert (ev / "event" / "task.json").read_bytes() == (src / "task.json").read_bytes()
    assert json.loads((ev / "world_manifest.json").read_text()) == WORLD_MANIFEST
    # 真值层（dev）：Run 真值文件与来源相同
    assert (ev / "truth" / f"run_{RID}.mcap").read_bytes() == (src / "truth" / f"run_{RID}.mcap").read_bytes()
    # index.json 与 export_manifest.json：schema（经 bench_validator 与独立 jsonschema 两条路径）
    idx = json.loads((ev / "index.json").read_text())
    assert check_index(idx) == []
    assert idx["split"] == "dev" and [r["run_id"] for r in idx["runs"]] == [RID] and idx["experiments"] == [{"experiment_id": "exp-1", "runs": [RID]}]
    row = idx["runs"][0]
    assert row["metrics"] == {"ec": 0.5, "ec_coverage": 0.4, "eu_f1": None, "ao_real": None, "ranked_valid": True}
    assert row["exports"] == ["bag", "files"] and row["export_manifest_sha256"] == hashlib.sha256(res.manifest_path.read_bytes()).hexdigest()
    assert {r["path"] for r in idx["event"]} == {"event/tape.bin", "event/tape.json", "event/task.json"}
    assert {r["path"] for r in idx["truth"]} == {f"truth/run_{RID}.mcap"}
    man = json.loads(res.manifest_path.read_text())
    assert check_export_manifest(man) == []
    schema = json.loads((ROOT / "packages/contracts/bench/export_manifest.v1.schema.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(man)
    assert man["source"]["raw_mcap_sha256"] == hashlib.sha256((src / "raw.mcap").read_bytes()).hexdigest()
    assert man["source"]["truth_mcap_sha256"] == hashlib.sha256((src / "truth" / f"run_{RID}.mcap").read_bytes()).hexdigest()
    assert man["split"] == "dev" and man["formats"] == ["files", "bag"] and man["layers"] == ["obs", "run", "truth"]
    # 许可与署名、生成工具声明（M27-FR-072；ADR-154）
    lic = man["license"]
    assert lic["complete"] is True and lic["missing"] == [] and lic["dataset"] == "CC-BY-4.0" and lic["citation"] and lic["notice"]
    assert lic["derivedFrom"][0]["license"] == "ODbL-1.0" and lic["attribution"]["en"] and lic["attribution"]["zh"] and "经修改" in lic["modification"]
    gen = man["generation"]
    assert gen["synthetic"] is True and "仿真生成" in gen["statement"]["zh"] and "Synthetic" in gen["statement"]["en"]
    assert {t["role"] for t in gen["tools"]} == {"simulation", "export"} and gen["versions"]["git_sha"] == "abc1234"
    assert man["retention"] == {"files": "standard", "bag": "transient"}
    assert man["quantization"]["depth_log16"]["sentinel"] == LOG16_SENTINEL and man["quantization"]["depth_log16"]["lossless"] is False
    # 逐文件路径：多相机机号下按传感器分目录，单相机为 <uav>/rgb/<seq>.jpg
    paths = {e["path"] for e in man["files"]}
    assert {"uav-01/rgb/cam0/000003.jpg", "uav-01/rgb/slam_cam/000003.jpg", "uav-02/rgb/000003.jpg", "uav-01/depth_log16/000003.png",
            "uav-01/lidar/000003.bin", "uav-01/seg/instance/000003.png", "uav-01/seg/semantic/000003.png", "uav-01/state.jsonl",
            "uav-02/state.jsonl", "messages.jsonl", "actions.jsonl", "reports.jsonl", "bag/uav-01.bag"} <= paths
    assert man["counts"] == {"actions": 1, "bag": 1, "depth_log16": N_FRAMES, "lidar": N_FRAMES, "messages": 1, "reports": 1, "rgb": 3 * N_FRAMES,
                             "seg_instance": N_FRAMES, "seg_semantic": N_FRAMES, "state": 2}
    assert [e["path"] for e in man["files"]] == sorted(paths)
    assert {s["what"] for s in man["skipped"]} == {"bag/uav-02"}                   # uav-02 无 LiDAR
    # 派生物保留登记（B05）与制品列表
    rec = retention.load_record(res.run_dir)
    assert rec["derived"]["files"]["retention"] == "standard" and rec["derived"]["bag"]["retention"] == "transient"
    items = artifact_items(res.run_dir)
    assert items[0]["kind"] == "export_manifest" and {i["kind"] for i in items} == {"export_manifest", "files", "bag"}
    assert all(i["retain_until_unix_ns"] for i in items)
    audit = AuditLog(root / "audit").read()
    assert audit[-1]["action"] == "export" and audit[-1]["run_id"] == RID and audit[-1]["files"] == len(man["files"])


# ---------------------------------------------------------------------- 逐文件与 raw.mcap 一致
def test_files_match_raw_mcap_and_truth(src: Path, tmp_path: Path) -> None:
    import msgpack
    import zstandard
    from foxglove_schemas_protobuf.CompressedImage_pb2 import CompressedImage

    res = _export(src, tmp_path / "d3")
    der = res.run_dir / "derived"
    man = res.manifest
    by = {e["path"]: e for e in man["files"]}
    # RGB：与 foxglove.CompressedImage.data（Agent 收到的 JPEG）逐字节相同；与帧索引页 sha256 相同
    n = 0
    for topic, seq, t, data in _msgs(src / "raw.mcap"):
        if topic.endswith("/image"):
            m = CompressedImage()
            m.ParseFromString(data)
            uav, cam = topic.split("/")[2], topic.split("/")[4]
            rel = f"{uav}/rgb/{cam}/{seq:06d}.jpg" if uav == "uav-01" else f"{uav}/rgb/{seq:06d}.jpg"
            assert (der / rel).read_bytes() == m.data and by[rel]["t_ns"] == t and by[rel]["source_sha256"] == by[rel]["sha256"]
            n += 1
        elif topic == "/uav/uav-01/lidar/lidar0":
            assert (der / f"uav-01/lidar/{seq:06d}.bin").read_bytes() == data
    assert n == 3 * N_FRAMES
    fs = FrameStore(res.run_dir)
    page = fs.index_page("uav-01", "cam0")
    assert all(i["sha256"] == by[f"uav-01/rgb/cam0/{i['seq']:06d}.jpg"]["sha256"] for i in page["items"])
    fs.close()
    # depth_log16：逐像素量化误差 ≤ 半步（相对 7.03e-5），无回波与越界为哨兵
    for seq in range(N_FRAMES):
        v = read_png_gray((der / f"uav-01/depth_log16/{seq:06d}.png").read_bytes())
        d = _depth(seq).astype(np.float64)
        ok = np.isfinite(d) & (d >= 0.3) & (d <= 3000.0)
        assert v.dtype == np.uint16 and v.shape == CAM and (v[~ok] == LOG16_SENTINEL).all() and (v[ok] < LOG16_SENTINEL).all()
        rel = np.abs(depth_log16_decode(v[ok]) - d[ok]) / d[ok]
        assert rel.max() <= 7.1e-5
        assert by[f"uav-01/depth_log16/{seq:06d}.png"]["stats"]["valid"] == int(ok.sum())
    # 分割：与真值块 zstd 解码逐像素相同
    dz = zstandard.ZstdDecompressor()
    from awr.bench.recorder.truth import unpack_camera_gt

    for _topic, seq, _t, data in _msgs(src / "truth" / f"run_{RID}.mcap", ["/eval/hidden/camera/uav-01/cam0"]):
        _rec, blocks = unpack_camera_gt(data)
        inst = np.frombuffer(dz.decompress(blocks["instance.u16.zst"]), "<u2").reshape(CAM)
        sem = np.frombuffer(dz.decompress(blocks["semantic.u8.zst"]), "u1").reshape(CAM)
        assert np.array_equal(read_png_gray((der / f"uav-01/seg/instance/{seq:06d}.png").read_bytes()), inst)
        assert np.array_equal(read_png_gray((der / f"uav-01/seg/semantic/{seq:06d}.png").read_bytes()), sem)
        assert np.array_equal(inst, _instance(seq)) and np.array_equal(sem, _semantic(seq))
    # 导航解、运行层：逐行与通道消息一致
    nav = [msgpack.unpackb(d, raw=False) | {"k": s} for tp, s, _t, d in _msgs(src / "raw.mcap", ["/uav/uav-02/obs/nav"])]
    assert [json.loads(x) for x in (der / "uav-02/state.jsonl").read_text().splitlines()] == nav
    net = [d for _tp, _s, _t, d in _msgs(src / "raw.mcap", ["/net/records"])]
    assert (der / "messages.jsonl").read_bytes() == b"".join(x + b"\n" for x in net) and len(net) == 4
    acts = [json.loads(x) for x in (der / "actions.jsonl").read_text().splitlines()]
    assert len(acts) == 2 * N_FRAMES and {a["uav"] for a in acts} == {"uav-01", "uav-02"} and all("t_ns" in a and "act" in a for a in acts)
    assert [a["t_ns"] for a in acts] == sorted(a["t_ns"] for a in acts)
    reps = [json.loads(x) for x in (der / "reports.jsonl").read_text().splitlines()]
    assert reps == [{"uav": "uav-02", "k_obs": 4, "apply_tick": 5, "t_apply_ns": 450_000_000, "report": {"type": "sighting"}, "t_ns": 450_000_000}]
    # 每个文件的 sha256 与清单一致；verify_export 全部通过
    for e in man["files"]:
        b = (der / e["path"]).read_bytes()
        assert len(b) == e["bytes"] and hashlib.sha256(b).hexdigest() == e["sha256"], e["path"]
    rep = verify_export(res.run_dir)
    assert rep["ok"], rep
    assert rep["checked"] == man["counts"]
    # 篡改一个 RGB 文件（清单同步改写哈希）后，来源比对报告不一致
    p = der / "uav-02/rgb/000001.jpg"
    p.write_bytes(p.read_bytes()[:-1] + b"\x00")
    doc = json.loads(res.manifest_path.read_text())
    for e in doc["files"]:
        if e["path"] == "uav-02/rgb/000001.jpg":
            e["sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
    res.manifest_path.write_text(json.dumps(doc), encoding="utf-8")
    bad = verify_export(res.run_dir)
    assert not bad["ok"] and bad["mismatches"] == [{"path": "uav-02/rgb/000001.jpg", "why": "bytes differ from raw.mcap"}]


# ---------------------------------------------------------------------- depth_log16（M27-AC-013）与 PNG
def test_depth_log16_accuracy_and_sentinel() -> None:
    d = np.logspace(np.log10(0.3), np.log10(3000.0), 1_000_001)
    d[0], d[-1] = 0.3, 3000.0                                                     # 端点取精确值（logspace 末位舍入可落到区间外）
    v, st = depth_log16_encode(d)
    back = depth_log16_decode(v)
    assert st == {"pixels": d.size, "valid": d.size, "no_return": 0, "out_of_range": 0}
    assert (np.abs(back - d) / d).max() <= 7.1e-5
    near = np.linspace(99.0, 101.0, 20_001)
    assert np.abs(depth_log16_decode(depth_log16_encode(near)[0]) - near).max() <= 0.0071
    assert depth_log16_encode(np.array([0.3, 3000.0]))[0].tolist() == [0, 65534]
    bad = np.array([0.0, -1.0, np.nan, np.inf, 0.2999, 3000.01], np.float32)
    v, st = depth_log16_encode(bad)
    assert (v == LOG16_SENTINEL).all() and st["no_return"] == 4 and st["out_of_range"] == 2   # 0、-1、NaN、inf 为无回波
    assert np.isnan(depth_log16_decode(v)).all()
    # PNG：自编码器与通用解码器（Pillow）读回一致；同一数组逐字节一致
    a16 = np.random.default_rng(1).integers(0, 65536, (7, 9)).astype(np.uint16)
    a8 = np.random.default_rng(2).integers(0, 256, (5, 3)).astype(np.uint8)
    for a in (a16, a8):
        b = png_gray(a)
        assert b == png_gray(a.copy()) and np.array_equal(read_png_gray(b), a)
    PIL = pytest.importorskip("PIL.Image")
    import io

    assert np.array_equal(np.asarray(PIL.open(io.BytesIO(png_gray(a16)))).astype(np.uint16), a16)
    assert np.array_equal(np.asarray(PIL.open(io.BytesIO(png_gray(a8)))), a8)


# ---------------------------------------------------------------------- bag（M27-FR-068）
def test_bag_follows_slam_contract(src: Path, tmp_path: Path) -> None:
    from awr.sensorsvc.slam.bag import (
        IMAGE_TOPIC,
        IMU_TOPIC,
        LIDAR_TOPIC,
        Bag1Reader,
        unpack_compressed_image,
        unpack_custom_msg,
        unpack_imu,
        verify_slam_bag,
    )

    res = _export(src, tmp_path / "d3", formats=("bag",))
    bag = res.run_dir / "derived" / "bag" / "uav-01.bag"
    rep = verify_slam_bag(bag, slots=SLOTS)
    assert rep.lidar_frames == N_FRAMES and rep.imu_samples == 20 * N_FRAMES and rep.images == N_FRAMES
    with Bag1Reader(bag) as rd:
        frames = [unpack_custom_msg(m.data) for m in rd.messages([LIDAR_TOPIC])]
        imu = [unpack_imu(m.data) for m in rd.messages([IMU_TOPIC])]
        imgs = [unpack_compressed_image(m.data) for m in rd.messages([IMAGE_TOPIC])]
    for seq, f in enumerate(frames):
        pts = f["points"]
        assert len(pts) == SLOTS and f["timebase"] == seq * 100_000_000
        assert np.array_equal(pts["offset_time"], np.arange(SLOTS) * 5000)
        hit = ~((pts["x"] == 0) & (pts["y"] == 0) & (pts["z"] == 0))
        want = _lidar_points(seq)
        assert hit.sum() == len(want) and np.allclose(pts["x"][hit], want["x"]) and (pts["reflectivity"][~hit] == 0).all()
    assert abs(imu[0]["linear_acceleration"][2] - 1.0) < 1e-6                     # 9.80665 m/s² → 1 g
    assert [i["data"] for i in imgs] == [_jpeg(1000 + s) for s in range(N_FRAMES)]  # 相机取 slam_cam（agent_delivered = false）
    e = next(e for e in res.manifest["files"] if e["kind"] == "bag")
    assert e["stats"]["camera"] == "slam_cam" and e["stats"]["imu"] == "lidar0_imu" and e["stats"]["verify"]["ok"] is True
    assert res.manifest["formats"] == ["bag"] and res.manifest["layers"] == ["obs"]


# ---------------------------------------------------------------------- test 划分与评测运行（691）
@pytest.mark.parametrize("case", ["registry_test", "manifest_test", "ranked_eval", "submission"])
def test_test_split_and_eval_runs_are_not_exportable(case: str, tmp_path: Path) -> None:
    root = tmp_path / "d3"
    src = build_run(tmp_path / "env-runs" / RID, split="test" if case == "manifest_test" else "dev")
    reg = SplitRegistry.for_root(root)
    kw = {}
    if case == "registry_test":
        reg.register(EID, "test", by="admin")
    elif case == "ranked_eval":
        retention.register_run(src, retention="ranked_eval")
    elif case == "submission":
        kw["kind"] = "submission"
    audit = AuditLog(root / "audit")
    who = Principal("team-a/alice", "researcher")
    with pytest.raises(SplitAccessDenied) as ei:
        export_run(src, dataset_root=root / "dataset", formats=("files",), layers=("obs", "truth"), registry=reg, audit=audit, principal=who, **kw)
    why = "eval_run" if case in ("ranked_eval", "submission") else "test_split"
    assert ei.value.code == 691 and ei.value.detail["why"] == why
    assert not (root / "dataset").exists()                                        # 数据集目录中不出现任何产物（含真值层）
    deny = [r for r in audit.read() if r["action"] == "deny"]
    assert len(deny) == 1 and deny[0]["code"] == 691 and deny[0]["op"] == "export" and deny[0]["why"] == why
    assert deny[0]["run_id"] == RID and deny[0]["principal"] == "team-a/alice" and deny[0]["layers"] == ["obs", "truth"]


# ---------------------------------------------------------------------- 确定性（M27-FR-074）
def test_export_is_deterministic(src: Path, tmp_path: Path) -> None:
    t = iter(range(10 ** 18, 10 ** 18 + 100))
    a = _export(src, tmp_path / "a", clock=lambda: next(t))
    b = _export(src, tmp_path / "b", clock=lambda: next(t))

    def tree(d: Path) -> dict[str, bytes]:
        return {p.relative_to(d).as_posix(): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file() and p.name not in ("retention.json",)
                and not p.name.startswith(".")}

    ta, tb = tree(a.event_dir), tree(b.event_dir)
    assert ta.keys() == tb.keys()
    ma, mb = (json.loads(x[f"runs/{RID}/derived/export_manifest.json"]) for x in (ta, tb))
    assert ma["generated_unix_ns"] != mb["generated_unix_ns"]
    ma.pop("generated_unix_ns"), mb.pop("generated_unix_ns")
    assert ma == mb
    diff = [k for k in ta if ta[k] != tb[k]]
    assert set(diff) == {f"runs/{RID}/derived/export_manifest.json", "index.json"}   # 两者只差清单生成时刻（索引登记清单哈希）
    ia, ib = json.loads(ta["index.json"]), json.loads(tb["index.json"])
    for i in (ia, ib):
        i["runs"][0].pop("export_manifest_sha256")
    assert ia == ib
    # 原位重导出：文件不变，索引与清单（除生成时刻）不变
    c = export_run(a.run_dir, formats=("files", "bag"), layers=("obs", "run", "truth"), clock=lambda: next(t))
    tc = tree(a.event_dir)
    assert {k for k in ta if ta[k] != tc[k]} == {f"runs/{RID}/derived/export_manifest.json", "index.json"}
    assert c.manifest["files"] == a.manifest["files"]
    # 增量导出：只重导 bag 时保留上次的逐文件条目
    d = export_run(a.run_dir, formats=("bag",), clock=lambda: next(t))
    assert d.manifest["files"] == a.manifest["files"] and d.manifest["formats"] == ["files", "bag"]
    # 去掉真值层重导逐文件：分割图被移除
    e = export_run(a.run_dir, formats=("files",), layers=("obs", "run"), clock=lambda: next(t))
    assert "seg_instance" not in e.manifest["counts"] and not (a.run_dir / "derived" / "uav-01" / "seg").exists()
    assert e.manifest["counts"]["bag"] == 1 and e.manifest["layers"] == ["obs", "run"]


# ---------------------------------------------------------------------- 派生物保留（B05）与 697
def test_derived_retention_cleanup_and_697(src: Path, tmp_path: Path) -> None:
    root = tmp_path / "d3"
    pol = retention.RetentionPolicy(day_ns=60 * 10 ** 9)
    t0 = 10 ** 18
    res = _export(src, root, clock=lambda: t0)
    retention.register_run(res.run_dir, retention="standard", t0_unix_ns=t0)
    day = pol.day_ns
    out = retention.retention_gc(root, policy=pol, now=t0 + 6 * day)
    assert out["deleted"] == []
    out = retention.retention_gc(root, policy=pol, now=t0 + 8 * day)                # bag：transient 7 天
    assert {Path(x["path"]).name for x in out["deleted"]} == {"bag"}
    assert retention.expired_reason(res.run_dir, "bag")["code"] == 697 and retention.expired_reason(res.run_dir, "files") is None
    rep = verify_export(res.run_dir)
    assert rep["ok"] and rep["expired"] == ["bag/uav-01.bag"] and rep["missing"] == []
    assert (res.run_dir / "derived" / "uav-01" / "rgb").is_dir()
    out = retention.retention_gc(root, policy=pol, now=t0 + 31 * day)               # 逐文件：standard 30 天（含导出清单）
    assert not (res.run_dir / "derived" / "export_manifest.json").exists() and not (res.run_dir / "derived" / "uav-01").exists()
    assert retention.expired_reason(res.run_dir, "files")["code"] == 697
    assert verify_export(res.run_dir)["expired"] == ["export_manifest.json"]
    # 重新派生恢复（登记清除 expired）
    again = export_run(res.run_dir, formats=("files",), layers=("obs",), clock=lambda: t0 + 32 * day)
    assert retention.expired_reason(res.run_dir, "files") is None and verify_export(res.run_dir)["ok"]
    assert again.manifest["counts"]["rgb"] == 3 * N_FRAMES
    # 来源 Run 清理后（standard 180 天）导出返回 697
    retention.retention_gc(root, policy=pol, now=t0 + 181 * day)
    with pytest.raises(ExportError) as ei:
        export_run(res.run_dir, formats=("files",))
    assert ei.value.code == 697 and ei.value.detail["why"] == "run_expired"


# ---------------------------------------------------------------------- 请求与来源检查（689）
def test_rejections_689(src: Path, tmp_path: Path) -> None:
    root = tmp_path / "d3"
    for fmt, why in (("lerobot", "format_unavailable"), ("openlabel", "format_unavailable"), ("pcd", "unknown_format")):
        with pytest.raises(ExportError) as ei:
            _export(src, root, formats=(fmt,))
        assert (ei.value.code, ei.value.detail["why"]) == (689, why)
    with pytest.raises(ExportError) as ei:
        _export(src, root, layers=("agent",))
    assert ei.value.detail["why"] == "unknown_layer"
    with pytest.raises(ExportError) as ei:
        export_run(src, formats=("files",))                                        # 不在数据集目录且未给出 dataset_root
    assert ei.value.detail["why"] == "dataset_root_required"
    # 许可不全：缺省拒绝；strict_license = False 时导出并列出缺项
    nolic = build_run(tmp_path / "nolic" / RID, world={"schema": "awr.world_manifest.v1", "world_id": WID, "partial": True})
    with pytest.raises(ExportError) as ei:
        _export(nolic, tmp_path / "n1")
    assert ei.value.detail["why"] == "license_incomplete" and set(ei.value.detail["missing"]) == {"dataset", "citation", "notice", "derivedFrom",
                                                                                                   "attribution"}
    res = _export(nolic, tmp_path / "n2", formats=("files",), layers=("run",), strict_license=False)
    assert res.manifest["license"]["complete"] is False and "dataset" in res.manifest["license"]["missing"]
    # 运行清单缺失或不合 schema；frames.idx 与 raw.mcap 不对应
    bad = tmp_path / "bad" / RID
    shutil.copytree(src, bad)
    doc = json.loads((bad / "run_manifest.json").read_text())
    doc.pop("seed_commit")
    (bad / "run_manifest.json").write_text(json.dumps(doc))
    with pytest.raises(ExportError) as ei:
        _export(bad, tmp_path / "b1")
    assert ei.value.detail["why"] == "run_manifest_invalid"
    (bad / "run_manifest.json").unlink()
    with pytest.raises(ExportError) as ei:
        _export(bad, tmp_path / "b2")
    assert ei.value.detail["why"] == "run_manifest_missing"
    shutil.copy(src / "run_manifest.json", bad / "run_manifest.json")
    with (bad / "raw.mcap").open("ab") as f:
        f.write(b"\x00")
    with pytest.raises(ExportError) as ei:
        _export(bad, tmp_path / "b3")
    assert ei.value.detail["why"] == "frames_idx_mismatch"
    # 同一事件下出现不同磁带 → 事件层冲突
    _export(src, tmp_path / "c")
    other = tmp_path / "other" / RID
    shutil.copytree(src, other)
    (tmp_path / "c" / "dataset" / EID / "event" / "tape.bin").write_bytes(b"different tape")
    with pytest.raises(ExportError) as ei:
        _export(other, tmp_path / "c")
    assert ei.value.detail["why"] == "event_conflict"
    # 存储准入（685）：派生作业在配额 ≥ 95% 时排队
    gate = retention.StorageGate(tmp_path / "g", retention.RetentionPolicy(quota_bytes=100), usage_fn=lambda: retention.Usage(99, {}),
                                 audit=False)
    with pytest.raises(ExportError) as ei:
        _export(src, tmp_path / "g", gate=gate)
    assert ei.value.code == 685 and ei.value.detail["why"] == "quota_stop"


# ---------------------------------------------------------------------- 命令行
def test_cli_export_and_verify(src: Path, tmp_path: Path, capsys) -> None:
    root = tmp_path / "d3"
    assert main(["--data-root", str(root), "--run-dir", str(src), "--formats", "files,bag", "--layers", "obs,run,truth"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["counts"]["rgb"] == 3 * N_FRAMES and Path(out["manifest"]).is_file()
    assert main(["--data-root", str(root), "--run", RID, "--verify"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert main(["--data-root", str(root), "--run", RID, "--formats", "lerobot"]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == 689
    SplitRegistry.for_root(root).register(EID, "test", by="admin")
    assert main(["--data-root", str(root), "--run", RID, "--formats", "files"]) == 1
    err = json.loads(capsys.readouterr().out)
    assert err["code"] == 691 and err["detail"]["why"] == "test_split"
    assert any(r["action"] == "deny" and r["code"] == 691 for r in AuditLog(root / "audit").read())


# ---------------------------------------------------------------------- 真实 Env 运行
ENV_TESTS = ROOT / "tests" / "d3" / "env"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_conf = _load("d3_env_conftest_for_export", ENV_TESTS / "conftest.py")
synth_event = _conf.synth_event


def test_env_l2_depth_run_exports_consistently(synth_event, tmp_path: Path) -> None:
    from awr.agentsdk.env.core import BenchEnv
    from awr.agentsdk.env.spec import EnvSpec
    from awr.bench.eval.evaluator import run_manifest, write_run_manifest

    t_l2 = _load("d3_env_test_l2_for_export", ENV_TESTS / "test_l2.py")
    w, ev = synth_event
    run = tmp_path / "env" / "t-exp"
    spec = EnvSpec("t-exp", 5, w, ev, t_l2.FLEET, obs_level="L2-depth", physics="kinematic", duration_s=1.0, out_dir=run)
    env = BenchEnv(spec, l2=lambda e: t_l2._l2(e, False))
    obs, _ = env.reset()
    while env.agents:
        obs, *_ = env.step({u: {"nav": {"kind": "goto", "target_enu": [0.0, 60.0, 30.0], "speed_mps": 4.0}} for u in obs})
    env.close()
    write_run_manifest(run, run_manifest(spec, run, experiment_id="exp-env"))
    res = export_run(run, dataset_root=tmp_path / "d3" / "dataset", formats=("files", "bag"), layers=("obs", "run", "truth"), strict_license=False)
    c = res.manifest["counts"]
    assert c["rgb"] > 0 and c["depth_log16"] == c["rgb"] and c["lidar"] > 0 and c["state"] == len(t_l2.FLEET) and c["actions"] == 1
    assert c["bag"] == len(t_l2.FLEET) and all(e["stats"]["verify"]["ok"] for e in res.manifest["files"] if e["kind"] == "bag")
    assert check_export_manifest(res.manifest) == [] and check_index(json.loads(res.index_path.read_text())) == []
    assert (res.event_dir / "truth" / "run_t-exp.mcap").is_file() and (res.event_dir / "event" / "tape.bin").is_file()
    rep = verify_export(res.run_dir)
    assert rep["ok"], rep
    # Agent 收到的 RGB 字节与导出文件 sha256 相同（经帧索引页）
    fs = FrameStore(res.run_dir)
    by = {e["path"]: e for e in res.manifest["files"]}
    for uav, sensor in fs.idx.channels():
        info = fs.channel_info(uav, sensor)
        if info.get("codec", "jpeg") == "jpeg":
            page = fs.index_page(uav, sensor)
            for it in page["items"]:
                assert by[f"{uav}/rgb/{it['seq']:06d}.jpg"]["sha256"] == it["sha256"]
    fs.close()
