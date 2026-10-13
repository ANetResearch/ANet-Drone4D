"""ADR-109 第 3 条（R-D2-28；D2-AC-24）：六城 world.json 的数据集说明与 `worldpkg dataset` 只重写元数据。

1. 适配器的 `DATASET` 写"经数据集权利方授权使用"（`terms`），许可不再写科研用途，`name` 与 `citation` 不变，`redistribution` 仍为 false；
2. `refresh_world` 只替换 `dataset` 段：保留 `sourceFiles` 与其余字段、contentVersion 与派生缓存键不变、写后校验通过、幂等；
   `--check` 只报告；非 UrbanScene3D 世界跳过；构建锁被占用时报告 busy、不写；
3. 本机已构建的六城（needs_worlds）带当前说明。
"""

from __future__ import annotations

import json

from m03_common import CITIES, WORLDS, load, needs_worlds

from awr.world.ingest import urbanscene3d
from awr.world.package.dataset import expected_dataset, refresh_all, refresh_world
from awr.world.package.publish import Publisher
from awr.world.package.validate import validate_world

D1_DATASET = {
    "name": "UrbanScene3D",
    "version": "virtual_cities-sampled (GitHub Release v0.0.1)",
    "url": "https://vcc.tech/UrbanScene3D",
    "citation": "Lin et al., Capturing, Reconstructing, and Simulating: the UrbanScene3D Dataset, ECCV 2022",
    "license": "UrbanScene3D terms of use: non-commercial research only; see url",
    "redistribution": False,
    "notice": "Recorded for provenance only; distribution follows ADR-034.",
}


def test_adapter_dataset_states_the_authorization():
    d = urbanscene3d.DATASET
    assert d["name"] == "UrbanScene3D"                       # 部署套件按 name 识别授权范围
    assert "ECCV 2022" in d["citation"]
    assert d["redistribution"] is False                      # 授权不转授第三方（事实记录，ADR-034）
    assert "经数据集权利方授权使用" in d["terms"] and "ADR-109" in d["terms"]
    assert "authorization of the dataset rights holders" in d["license"]
    for k in ("license", "notice", "terms"):
        assert "research only" not in d[k].lower() and "ADR-034" not in d[k], k


def _as_city(world_dir, city: str, monkeypatch) -> dict:
    """tiny world 冒充六城之一：dataset 写回 D1 文字（sourceFiles 取 tiny 构建的记录）。"""
    monkeypatch.setitem(urbanscene3d.CITIES, "tiny", urbanscene3d.CITIES[city])
    wj = world_dir / "world.json"
    w = load(wj)
    w["dataset"] = D1_DATASET | {"sourceFiles": w["dataset"]["sourceFiles"]}
    wj.write_text(json.dumps(w, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return w


def test_refresh_rewrites_only_the_dataset_block(tiny_copy, monkeypatch):
    before = _as_city(tiny_copy, "shenzhen", monkeypatch)
    worlds = tiny_copy.parent
    assert refresh_world(worlds, "tiny", check=True).action == "stale"
    assert load(tiny_copy / "world.json") == before            # --check 不写

    r = refresh_world(worlds, "tiny")
    assert r.action == "updated", r
    after = load(tiny_copy / "world.json")
    assert after["dataset"] == expected_dataset("tiny", before["dataset"])
    assert after["dataset"]["sourceFiles"] == before["dataset"]["sourceFiles"]
    assert after["dataset"]["terms"] == urbanscene3d.DATASET["terms"]
    assert {k: v for k, v in after.items() if k != "dataset"} == {k: v for k, v in before.items() if k != "dataset"}
    assert list(after) == list(before)                       # 键序不变（确定性序列化）
    assert after["contentVersion"] == before["contentVersion"]
    assert validate_world(tiny_copy, deep=False).ok
    assert not list(tiny_copy.glob(".world.json.tmp-*"))

    assert refresh_world(worlds, "tiny").action == "current"   # 幂等
    assert refresh_world(worlds, "tiny", check=True).action == "current"


def test_refresh_keeps_the_derive_key(tiny_copy, monkeypatch):
    """派生缓存键 = sha256(contentVersion, coordinate.sha256, GeoParams, DERIVE_VERSION)：world.json 不在其中。"""
    from awr.world.geometry.status import read_world_header as read_header

    _as_city(tiny_copy, "shenzhen", monkeypatch)
    h0 = read_header(tiny_copy)
    assert refresh_world(tiny_copy.parent, "tiny").action == "updated"
    h1 = read_header(tiny_copy)
    assert (h1.content_version, h1.coordinate_sha256) == (h0.content_version, h0.coordinate_sha256)


def test_refresh_skips_other_worlds_and_missing(tiny_copy):
    worlds = tiny_copy.parent
    assert refresh_world(worlds, "tiny").action == "skipped"         # 不在六城中
    assert refresh_world(worlds, "shenzhen").action == "missing"
    res = refresh_all(worlds)
    assert [r.world_id for r in res] == sorted(CITIES) and {r.action for r in res} == {"missing"}


def test_refresh_respects_the_build_lock(tiny_copy, monkeypatch):
    before = _as_city(tiny_copy, "shanghai", monkeypatch)
    with Publisher(tiny_copy.parent, "tiny").lock():
        r = refresh_world(tiny_copy.parent, "tiny")
    assert r.action == "busy" and load(tiny_copy / "world.json") == before
    assert refresh_world(tiny_copy.parent, "tiny").action == "updated"     # 锁释放后照常


def test_refresh_restores_on_validation_failure(tiny_copy, monkeypatch):
    _as_city(tiny_copy, "shenzhen", monkeypatch)
    w = load(tiny_copy / "world.json")
    w["tags"] = sorted(set(w.get("tags") or []) | {"builtin"})  # V-W-12 只检查 builtin 世界：citation 等非空
    (tiny_copy / "world.json").write_text(json.dumps(w, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    before_text = (tiny_copy / "world.json").read_text(encoding="utf-8")
    monkeypatch.setattr(urbanscene3d, "DATASET", dict(urbanscene3d.DATASET) | {"citation": ""})
    r = refresh_world(tiny_copy.parent, "tiny")
    assert r.action == "failed" and "V-W-12" in r.detail, r
    assert (tiny_copy / "world.json").read_text(encoding="utf-8") == before_text


def test_cli_dataset_check_exit_codes(tiny_copy, monkeypatch, capsys):
    from awr.world.package.cli import main

    _as_city(tiny_copy, "suzhou", monkeypatch)
    worlds = str(tiny_copy.parent)
    assert main(["dataset", "--check", "--worlds", worlds, "paris"]) == 3          # 只接受六城 id
    assert main(["dataset", "--check", "--worlds", worlds, "tiny"]) == 1           # 过期：只报告
    capsys.readouterr()
    assert main(["dataset", "--json", "--worlds", worlds, "tiny"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == [{"world_id": "tiny", "action": "updated", "detail": out[0]["detail"]}]
    assert main(["dataset", "--check", "--worlds", worlds, "tiny"]) == 0


@needs_worlds
def test_built_cities_carry_the_authorization():
    """本机六城（make worlds 之后 make worlds-dataset）：dataset 与适配器一致，世界面板显示授权说明而不是科研用途。"""
    for c in CITIES:
        d = load(WORLDS / c / "world.json")["dataset"]
        assert d == expected_dataset(c, d), c
        assert "research only" not in d["license"].lower(), c
