"""`python -m awr.datasets.scenarios <命令>`（M16 §7.1；mk/m16.mk 的 scenarios-* 目标）。

- `generate [--check]`：由 `authoring.build_all()` 写出全部剧本、catalog 与 curated zones；`--check` 只比较不写（逐字节，
  退出码 1 表示有差异）。坐标钉沿用已提交文件中的 `world_coordinate_sha256`。
- `pin [--check]`：读取 `$AWR_WORLDS_DIR/<id>/world.json` 的 `coordinate.sha256`，写入各剧本的
  `world_coordinate_sha256` 与 zones 的 `awr.coordinate_sha256`（M16-FR-007）；`--check` 只核对。
- `check`：静态校验摘要（schema、catalog、zones），退出码 0 通过、2 有错误（DEMO-E002、DEMO-E003）。
- `energy`：打印 S1 与 S2–S6 的离线能量复核表（编写期工具）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .authoring import build_all, dumps_canonical, world_of
from .catalog import ROOT, catalog_errors, load_catalog, scenarios_root, schema_errors, zones_errors


def _worlds_dir() -> Path:
    return Path(os.environ.get("AWR_WORLDS_DIR") or ROOT / "worlds")


def committed_pins(root: Path) -> dict[str, str | None]:
    pins: dict[str, str | None] = {}
    for p in sorted(root.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(d, dict) and d.get("schema") == "awr.scenario.v1" and d.get("world_coordinate_sha256"):
            pins.setdefault(str(d["world_id"]), d["world_coordinate_sha256"])
    return pins


def world_pins() -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for d in sorted(_worlds_dir().glob("*/world.json")):
        try:
            w = json.loads(d.read_text(encoding="utf-8"))
            out[str(w["id"])] = (w.get("coordinate") or {}).get("sha256")
        except (OSError, json.JSONDecodeError, KeyError):
            continue
    return out


def _sync(root: Path, pins: dict[str, str | None], check: bool) -> int:
    diffs = []
    for rel, doc in build_all(pins).items():
        p = root / rel
        data = dumps_canonical(doc).encode("utf-8")
        if not p.exists() or p.read_bytes() != data:
            diffs.append(rel)
            if not check:
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp = p.with_suffix(p.suffix + ".tmp")
                tmp.write_bytes(data)
                tmp.replace(p)
    for rel in diffs:
        print(f"{'differs' if check else 'wrote'}: scenarios/{rel}")
    if check and diffs:
        print("修复：python -m awr.datasets.scenarios generate（或 make scenarios-generate）", file=sys.stderr)
        return 1
    print(f"scenarios: {len(build_all(pins))} files, {len(diffs)} {'differ' if check else 'written'}")
    return 0


def cmd_check(root: Path) -> int:
    errs: list[str] = []
    gen = build_all(committed_pins(root))
    for rel in gen:
        p = root / rel
        if not p.exists():
            errs.append(f"missing scenarios/{rel}")
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        if rel.startswith("zones/"):
            errs += [f"zones/{rel}: {e}" for e in zones_errors(d, rel.split("/")[1].split(".")[0])]
        elif rel != "catalog.json":
            errs += [f"{rel}: V-SC-01 {e}" for e in schema_errors(d, "scenario/scenario.schema.json")]
            if d.get("scenario_id") != p.stem:
                errs.append(f"{rel}: scenario_id {d.get('scenario_id')} != file name")
            if d.get("world_id") != world_of(p.stem):
                errs.append(f"{rel}: world_id {d.get('world_id')}")
    errs += catalog_errors(load_catalog(root), root)
    for e in errs:
        print(e)
    print(f"scenarios check: {'OK' if not errs else f'{len(errs)} errors (DEMO-E003)'}")
    return 0 if not errs else 2


def cmd_energy() -> int:
    from .energy import S1_PLAN, SX_LEGS, EnergyModel, s1_segment_1d, simulate_sortie

    m1 = EnergyModel.from_vehicle()
    m0 = EnergyModel(wind_drag=False, e_use_wh=m1.e_use_wh, p_hover_w=m1.p_hover_w, mass_kg=m1.mass_kg)
    for vid in ("p600-01", "p600-02"):
        z0, z1 = S1_PLAN[vid]["z"]
        r, _ = s1_segment_1d(z0, z1, S1_PLAN["speed_mps"], 0.2, S1_PLAN["radius_m"], m=m0)
        print(f"S1 {vid} 1-D soc_land {r.soc_land:.3f} margin {r.min_margin:.2f} t {r.t_total_s / 60:.1f} min")
    for k, v in SX_LEGS.items():
        r = simulate_sortie(v["legs"], m0, k)
        print(f"{k:32s} soc_land {r.soc_land:.3f} (oracle {v['soc_land']:.3f}) margin {r.min_margin:.2f} "
              f"t {r.t_total_s / 60:.1f} min{' ENERGY_RTL' if r.energy_rtl_fired else ''}")
    try:
        from awr.world.geometry.query import open_world_query

        from .authoring import s1_scenario
        from .energy import WindProfile, WorldTerrain, helix_sortie

        wq = open_world_query(_worlds_dir() / "shenzhen", allow_derive=False)
        t = WorldTerrain(wq)
        wind = WindProfile.from_scenario(s1_scenario())
        for vid in ("p600-01", "p600-02"):
            z0, z1 = S1_PLAN[vid]["z"]
            r = helix_sortie(t, S1_PLAN["center"], S1_PLAN["radius_m"], S1_PLAN[vid]["home"], z0, z1,
                             S1_PLAN["dz_nom_m"], S1_PLAN["speed_mps"], wind, m1, vid)
            print(f"S1 {vid} drag  soc_land {r.soc_land:.3f} precheck {r.precheck_soc:.3f} margin {r.min_margin:.2f} "
                  f"revs {r.extra['revs']} dz {r.extra['dz_m']:.2f} landed {r.t_total_s / 60:.1f} min")
    except Exception as e:
        print(f"S1 drag model skipped: {e}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m awr.datasets.scenarios")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate", help="写出全部剧本、catalog 与 zones")
    g.add_argument("--check", action="store_true")
    p = sub.add_parser("pin", help="写入 world_coordinate_sha256（M16-FR-007）")
    p.add_argument("--check", action="store_true")
    sub.add_parser("check", help="静态校验摘要")
    sub.add_parser("energy", help="离线能量复核表")
    a = ap.parse_args(argv)
    root = scenarios_root()
    if a.cmd == "generate":
        return _sync(root, committed_pins(root), a.check)
    if a.cmd == "pin":
        pins = world_pins()
        if not pins:
            print(f"no built worlds under {_worlds_dir()}; 修复：make worlds", file=sys.stderr)
            return 6
        return _sync(root, pins, a.check)
    if a.cmd == "check":
        return cmd_check(root)
    return cmd_energy()


if __name__ == "__main__":
    sys.exit(main())
