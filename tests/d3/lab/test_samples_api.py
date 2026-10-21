"""样例包目录与 Run 派生物接口（D3-AC-56"样例包在内部工作区可浏览"；AWR-17 §18.8.4 BN-53、BN-54；`awr.bench.api.samples`）。

数据由 `tools/bench/d3/sample_pack.py` 对合成正式轮次（tests/d3/bench/sample_round.py）生成并安装到 bench-api 的数据根：

- `GET /samples`：返回 `pack.json` 原样（`{items}`），运行未安装的样例不列出，test 划分的运行对评测员以下不列出；
- BN-53：列出派生物索引中的视频、轨迹、磁带、录制与评测（字节数、sha256、保留类别 pinned）；已清理种类与真值层（评测员以下）不列出；
- BN-54：下载与 sha256 一致，`ETag` / `If-None-Match` 304，单区间 Range 206、越界 416 `309`；非法或未登记路径 305；
  已清理 410 `697`；真值层评测员以下 403 `691`；test 划分运行评测员以下 403 `691`。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

pytest.importorskip("av")
pytest.importorskip("mcap")
pytest.importorskip("PIL")

ROOT = Path(__file__).resolve().parents[3]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return sys.modules[name]


sp = _load("sample_pack", ROOT / "tools" / "bench" / "d3" / "sample_pack.py")
sr = _load("sample_round", ROOT / "tests" / "d3" / "bench" / "sample_round.py")


@pytest.fixture()
def packed(lab, tmp_path: Path):
    rnd = sr.build_round(tmp_path / "repo")
    root = lab.settings.data_root
    pack, build = sp.build_pack(rnd["report"], root, base=rnd["base"], evalstore=rnd["evalstore"], opts=sp.BuildOpts(root), log=lambda *_: None)
    assert build["passed"], build
    return lab, pack


def _l2(pack: dict) -> dict:
    return next(s for s in pack["samples"] if s["group"] == "l2" and s["policy"] == "track_share")


def _run_dir(lab, rid: str) -> Path:
    return lab.settings.data_root / "dataset" / sr.EVENT / "runs" / rid


def test_samples_lists_packs_for_researchers(packed) -> None:
    lab, pack = packed
    r = lab.client.get(f"{lab.P}/samples", headers=lab.bearer("researcher"))
    assert r.status_code == 200
    assert r.json() == {"items": [pack]}
    # 运行目录被移走的样例不列出；新的样例包排在前面
    other = {**pack, "pack_id": "p0a-zz", "generated_utc": "2099-01-01T00:00:00Z",
             "samples": [*pack["samples"], {**pack["samples"][0], "run_id": "sp-missing-run"}]}
    d = lab.settings.data_root / "samples" / "p0a-zz"
    d.mkdir()
    (d / "pack.json").write_text(json.dumps(other))
    (lab.settings.data_root / "samples" / "junk").mkdir()
    (lab.settings.data_root / "samples" / "junk" / "pack.json").write_text("{not json")
    items = lab.client.get(f"{lab.P}/samples", headers=lab.bearer("researcher")).json()["items"]
    assert [x["pack_id"] for x in items] == ["p0a-zz", pack["pack_id"]]
    assert len(items[0]["samples"]) == len(pack["samples"])
    assert lab.client.get(f"{lab.P}/samples", headers=lab.bearer("invited")).status_code == 403


def test_samples_hide_test_split_runs_below_evaluator(packed) -> None:
    lab, pack = packed
    s = _l2(pack)
    rid = "sp-test-run"
    d = lab.settings.data_root / "dataset" / "tst-hidden" / "runs" / rid
    shutil.copytree(_run_dir(lab, s["run_id"]), d)
    m = json.loads((d / "run_manifest.json").read_text())
    (d / "run_manifest.json").write_text(json.dumps({**m, "run_id": rid, "split": "test"}))     # 运行清单声明 test 划分
    doc = {**pack, "pack_id": "p0a-test", "samples": [{**s, "run_id": rid}]}
    (lab.settings.data_root / "samples" / "p0a-test").mkdir()
    (lab.settings.data_root / "samples" / "p0a-test" / "pack.json").write_text(json.dumps(doc))
    by = {x["pack_id"]: x for x in lab.client.get(f"{lab.P}/samples", headers=lab.bearer("researcher")).json()["items"]}
    assert by["p0a-test"]["samples"] == []
    by = {x["pack_id"]: x for x in lab.client.get(f"{lab.P}/samples", headers=lab.bearer("evaluator")).json()["items"]}
    assert [x["run_id"] for x in by["p0a-test"]["samples"]] == [rid]
    r = lab.client.get(f"{lab.P}/runs/{rid}/artifacts", headers=lab.bearer("researcher"))
    assert (r.status_code, r.json()["code"]) == (403, 691)
    assert lab.client.get(f"{lab.P}/runs/{rid}/artifacts", headers=lab.bearer("evaluator")).status_code == 200


def test_artifact_list_matches_pack(packed) -> None:
    lab, pack = packed
    s = _l2(pack)
    r = lab.client.get(f"{lab.P}/runs/{s['run_id']}/artifacts", headers=lab.bearer("researcher"))
    assert r.status_code == 200
    items = {it["path"]: it for it in r.json()["items"]}
    assert set(items) == {f["path"] for f in s["files"]}
    for f in s["files"]:
        it = items[f["path"]]
        assert (it["bytes"], it["sha256"], it["layer"]) == (f["bytes"], f["sha256"], f["layer"])
        assert it["retention"] == "pinned" and it["retain_until_unix_ns"] is None
        assert it["kind"] == ("video" if f["kind"] == "video" else "files")
    assert lab.client.get(f"{lab.P}/runs/run-zzz/artifacts", headers=lab.bearer("researcher")).json()["code"] == 305


def test_download_etag_and_range(packed) -> None:
    lab, pack = packed
    s = _l2(pack)
    h = lab.bearer("researcher")
    base = f"{lab.P}/runs/{s['run_id']}/artifacts"
    for f in s["files"]:
        r = lab.client.get(f"{base}/{f['path']}", headers=h)
        assert r.status_code == 200, f["path"]
        assert hashlib.sha256(r.content).hexdigest() == f["sha256"] and r.headers["etag"] == f'"{f["sha256"]}"'
        assert int(r.headers["content-length"]) == f["bytes"]
    v = next(f for f in s["files"] if f["kind"] == "video")
    assert lab.client.get(f"{base}/{v['path']}", headers=h).headers["content-type"] == "video/mp4"
    r = lab.client.get(f"{base}/{v['path']}", headers={**h, "If-None-Match": f'"{v["sha256"]}"'})
    assert r.status_code == 304
    full = (_run_dir(lab, s["run_id"]) / "derived" / v["path"]).read_bytes()
    r = lab.client.get(f"{base}/{v['path']}", headers={**h, "Range": "bytes=10-99"})
    assert r.status_code == 206 and r.content == full[10:100] and r.headers["content-range"] == f"bytes 10-99/{len(full)}"
    r = lab.client.get(f"{base}/{v['path']}", headers={**h, "Range": "bytes=-16"})
    assert r.status_code == 206 and r.content == full[-16:]
    r = lab.client.get(f"{base}/{v['path']}", headers={**h, "Range": f"bytes={len(full)}-"})
    assert (r.status_code, r.json()["code"]) == (416, 309)
    for bad in ("video/none.mp4", "video/a b.mp4", "index.json", "video/.%2E/raw.mcap"):
        r = lab.client.get(f"{base}/{bad}", headers=h)
        assert (r.status_code, r.json()["code"]) == (404, 305), bad


def test_expired_and_truth_layer(packed) -> None:
    from awr.bench import retention

    lab, pack = packed
    s = _l2(pack)
    d = _run_dir(lab, s["run_id"])
    base = f"{lab.P}/runs/{s['run_id']}/artifacts"
    # 真值层派生物（索引登记 layer = truth）：评测员以下不列出、下载 691
    (d / "derived" / "gt").mkdir()
    (d / "derived" / "gt" / "inst.png").write_bytes(b"png")
    idx = json.loads((d / "derived" / "index.json").read_text())
    idx["items"].append({"path": "gt/inst.png", "kind": "files", "layer": "truth", "bytes": 3, "sha256": hashlib.sha256(b"png").hexdigest()})
    (d / "derived" / "index.json").write_text(json.dumps(idx))
    paths = {it["path"] for it in lab.client.get(base, headers=lab.bearer("researcher")).json()["items"]}
    assert "gt/inst.png" not in paths
    r = lab.client.get(f"{base}/gt/inst.png", headers=lab.bearer("researcher"))
    assert (r.status_code, r.json()["code"]) == (403, 691)
    assert lab.client.get(f"{base}/gt/inst.png", headers=lab.bearer("evaluator")).content == b"png"
    # 视频按保留期清理后：不列出，下载 410 697
    rec = retention.load_record(d)
    rec.setdefault("expired", {}).setdefault("derived", {})["video"] = 123
    (d / "retention.json").write_text(json.dumps(rec))
    items = lab.client.get(base, headers=lab.bearer("researcher")).json()["items"]
    assert not [it for it in items if it["kind"] == "video"]
    v = next(f for f in s["files"] if f["kind"] == "video")
    r = lab.client.get(f"{base}/{v['path']}", headers=lab.bearer("researcher"))
    assert (r.status_code, r.json()["code"]) == (410, 697)
