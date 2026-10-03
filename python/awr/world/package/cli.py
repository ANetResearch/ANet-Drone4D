"""`worldpkg` 命令行（M03 §7.1；AWR-16 §15.4、§18.1）。

子命令：ingest、grid、tile、package、zones、env、qa、validate、build、status、default-world、clean、export（V0.5 预留）。
`ingest synthetic` 与 `build synthcity` 生成合成演示城市（ADR-077，`awr.world.ingest.synthetic`）。
退出码：0 成功；1 校验失败；2 ingest 门禁失败；3 参数、I/O 或锁冲突；4 原始数据缺失或 sha256 不符。
输出只用 ASCII 与中文。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

from ..ingest.manifest import default_raw_dir, default_worlds_dir, load_data_config, repo_root  # noqa: E402
from ..ingest.types import StageContext, WorldpkgError  # noqa: E402
from .params import BuildParams  # noqa: E402

WORLD_ID_RE = r"^[a-z0-9][a-z0-9-]{0,62}$"


def _p(msg: str) -> None:
    print(msg, flush=True)


def _err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _safe_path(p: str, *, must_exist: bool = False) -> Path:
    """写路径经规范化：拒绝包含 `..` 的相对路径与指向仓库外的符号链接（M03-NFR-017）。"""
    path = Path(p)
    if ".." in path.parts:
        raise WorldpkgError(f"路径不得包含 '..'：{p}")
    if must_exist and not path.exists():
        raise WorldpkgError(f"路径不存在：{p}")
    return path


def _params(a) -> BuildParams:
    return BuildParams.from_config(G=getattr(a, "G", None), leaf=getattr(a, "leaf", None), seed=getattr(a, "seed", None),
                                   forest=getattr(a, "forest", None), compression=getattr(a, "compression", None),
                                   dsm_occupancy=False if getattr(a, "no_dsm_n", False) else None,
                                   hag_grid=True if getattr(a, "hag", False) else None,
                                   keep_staging=getattr(a, "keep_staging", None) or None,
                                   keep_work=getattr(a, "keep_work", None) or None)


def _pipeline(stage: Path, a, *, log_json: bool = False):
    from .build import BuildPipeline

    return BuildPipeline(stage, _params(a), StageContext(log_json=log_json))


# ---------------------------------------------------------------- 分阶段命令


def cmd_ingest(a) -> int:
    if a.source not in ("urbanscene3d", "synthetic"):
        _err(f"worldpkg ingest {a.source}：{'D1-ext' if a.source == 'recon' else 'V0.2'} 提供；D1 只有 urbanscene3d 与 synthetic")
        return 3
    if a.semantic == "csf":
        _err("--semantic csf 为 P2（V0.2 提供），D1 默认使用规则语义")
        return 3
    out = _safe_path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    pipe = _pipeline(out, a, log_json=a.log_json)
    if a.source == "synthetic":                     # 合成演示城市（ADR-077）：类别由生成器给出
        from ..ingest.synthetic import SyntheticAdapter

        adapter = SyntheticAdapter(a.city or "synthcity")
        pipe.params = adapter.build_params(pipe.params)
    else:
        from ..ingest.urbanscene3d import UrbanScene3DAdapter

        adapter = UrbanScene3DAdapter(a.city, Path(a.raw) if a.raw else default_raw_dir())
    pipe.run_ingest(adapter)
    pipe.save_after("ingest")
    s = pipe.nc.stats
    _p(f"[{pipe.nc.world_id}] ingest done points={len(pipe.nc.xyz)} nn_median_m={s.nn_median_m} peak_hag_m={s.peak_hag_m:.1f}")
    return 0


def cmd_grid(a) -> int:
    stage = _safe_path(a.stage, must_exist=True)
    pipe = _pipeline(stage, a)
    pipe.restore("grid")
    pipe.run_grid()
    pipe.save_after("grid")
    _p(f"[{pipe.nc.world_id}] grid done dsm_max_m={pipe.grid_info['dsm_max_m']:.3f} shape={pipe.grid_info['dsm_shape']}")
    return 0


def cmd_tile(a) -> int:
    stage = _safe_path(a.stage, must_exist=True)
    if a.twin_default:
        _err("--twin-default 为 P2（16 §4.13），D1 不提供")
        return 3
    pipe = _pipeline(stage, a)
    pipe.restore("tile")
    shutil.rmtree(stage / "visual", ignore_errors=True)
    pipe.run_tile()
    pipe.save_after("tile")
    for r in pipe.roots:
        an = r.metadata["anet"]
        _p(f"[{pipe.nc.world_id}] tile root={r.name} nodes={an['nodeCount']} depth={r.depth} "
           f"levelsPoints={an['levelsPoints']}")
    return 0


def cmd_package(a) -> int:
    stage = _safe_path(a.stage, must_exist=True)
    pipe = _pipeline(stage, a)
    pipe.restore("package")
    pipe.run_derive()
    pipe.run_package()
    try:
        pipe.run_validate(deep=True)
    finally:
        if pipe.validation is not None:
            pipe.run_report()
    _p(f"[{pipe.nc.world_id}] package done contentVersion={pipe.world['contentVersion']}")
    if not a.keep_work:
        shutil.rmtree(stage / ".work", ignore_errors=True)
    return 0


def cmd_zones(a) -> int:
    from ..semantic.zones import example_curated

    if a.example:
        doc = example_curated(a.example)
        text = json.dumps(doc, indent=1, ensure_ascii=False) + "\n"
        if a.out:
            out = _safe_path(a.out)
            if out.resolve().is_relative_to((repo_root() / "scenarios").resolve()):
                _err("scenarios/ 属 M16：示例文件请写到其他位置，再经变更请求交给 M16")
                return 3
            out.write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)
        return 0
    if not a.stage:
        _err("用法：worldpkg zones <stage_dir> 或 worldpkg zones --example <world_id>")
        return 3
    stage = _safe_path(a.stage, must_exist=True)
    pipe = _pipeline(stage, a)
    pipe.restore("tile")
    from ..semantic.zones import derive_zones
    from .derivers import DeriveContext

    c = pipe.coordinate
    ctx = DeriveContext(stage=stage, world_id=c["worldId"], coordinate=c, coordinate_sha256=pipe.coordinate_sha,
                        bounds_min=c["extent"]["min"], bounds_max=c["extent"]["max"], dsm_max_m=pipe.grid_info["dsm_max_m"],
                        params=pipe.params, repo_root=repo_root())
    derive_zones(ctx)
    _p(f"[{c['worldId']}] zones written source_sha256={ctx.zones_source_sha256}")
    return 0


def cmd_env(a) -> int:
    from .env_world import build_env
    from .jsonio import read_json, sha256_file, write_json

    stage = _safe_path(a.stage, must_exist=True)
    c = read_json(stage / "coordinate.json")
    env = build_env(c["worldId"], sha256_file(stage / "coordinate.json"), c["anchor"].get("hMslM"))
    write_json(stage / "environment" / "env.json", env)
    _p(f"[{c['worldId']}] env.json written default_preset={env['default_preset']}")
    return 0


def cmd_qa(a) -> int:
    from .jsonio import read_json

    wd = _safe_path(a.world, must_exist=True)
    r = read_json(wd / "qa" / "report.json")
    if a.json:
        _p(json.dumps(r, ensure_ascii=False, indent=1))
        return 0
    _p(f"{r['world_id']} status={r['status']} contentVersion={r['content_version']}")
    for g in r["gates"]:
        _p(f"  {g['id']} {'通过' if g['pass'] else '不通过'} {g['severity']:<5} {g['name']}: {g['value']}")
    for s in r.get("stages", []):
        _p(f"  stage {s['name']:<9} {s['seconds']:7.2f} s  rss {s.get('peak_rss_mb', 0):.0f} MB")
    fs = r.get("first_screen", {})
    _p(f"  first_screen rule_g={fs.get('rule_g')} tier_s={fs.get('tier_s')} tier_ba={fs.get('tier_ba')}")
    return 0 if r["status"] != "fail" else 1


# ---------------------------------------------------------------- validate / build / status / clean / export


def cmd_validate(a) -> int:
    import re

    from .validate import is_world_dir, validate_cli_json, validate_world

    rules = set(a.rules.split(",")) if a.rules else None
    reports, skipped = [], []
    for d in a.worlds:
        p = Path(d)
        if not p.is_dir():
            _err(f"不是目录：{d}")
            return 3
        if not re.match(WORLD_ID_RE, p.name) and not (p / "world.json").exists():
            skipped.append(str(p))
            continue
        if not is_world_dir(p) and not (p / "world.json").exists():
            skipped.append(str(p))
            continue
        reports.append(validate_world(p, deep=a.deep, rules=rules, staging=".staging" in p.parts))
    if a.json:
        _p(json.dumps(validate_cli_json(reports, skipped), ensure_ascii=False, indent=1))
    else:
        for r in reports:
            fs = r.info.get("first_screen")
            _p(f"{'OK  ' if r.ok else 'FAIL'} {r.path}  errors={len(r.errors)} warnings={len(r.warnings)} first_screen={fs}"
               f" deep={r.deep} {r.seconds:.2f} s")
            for e in r.errors:
                _p(f"  E {e['rule']} {e['where']}: {e['message']}")
            for e in r.warnings:
                _p(f"  W {e['rule']} {e['where']}: {e['message']}")
        for s in skipped:
            _p(f"SKIP {s}（不是世界目录）")
    if any(not r.ok for r in reports):
        return 1
    if a.strict_warn and any(r.warnings for r in reports):
        return 2
    return 0


def cmd_build(a) -> int:
    from .build import build_missing, overall_exit, print_summary

    if a.config:
        _err("--config ingest.yaml 为 V0.2 通用配置化导入")
        return 3
    worlds = _safe_path(a.worlds) if a.worlds else default_worlds_dir()
    raw = _safe_path(a.raw) if a.raw else default_raw_dir()
    data = load_data_config()
    cities = a.ids or None
    from ..ingest.synthetic import synth_specs

    synth = synth_specs()
    for c in cities or []:
        if data.by_world(c) is None and c not in synth:
            _err(f"未知世界 {c}：只有 configs/data.yaml 中登记的城市与 configs/worldpkg.yaml 的 synthetic 世界可以构建")
            return 3
    ctx = StageContext(world_id="worldpkg", log_json=a.log_json)
    res = build_missing(worlds, raw, jobs=a.jobs, cities=cities, missing_only=a.missing, params=_params(a),
                        log_json=a.log_json, ctx=ctx)
    print_summary(res)
    if any(r.error_code == "RAW_MISSING" for r in res.values()):
        _err("提示：原始数据缺失，运行 make fetch-data（无数据试用：make demo-world 生成合成演示城市 synthcity，ADR-077）")
    return overall_exit(res)


def cmd_status(a) -> int:
    from .build import missing_reason
    from .publish import Publisher

    worlds = _safe_path(a.worlds) if a.worlds else default_worlds_dir()
    raw = _safe_path(a.raw) if a.raw else default_raw_dir()
    data = load_data_config()
    from ..ingest.synthetic import synth_specs
    from .defaults import resolve_default_world

    rows = []
    for spec in data.files:
        reason, _info = missing_reason(worlds, spec.world_id, raw_dir=raw, data=data)
        st = Publisher(worlds, spec.world_id).read_status() if (worlds / ".status").exists() else None
        rows.append({"world_id": spec.world_id, "rebuild": reason is not None, "reason": reason,
                     "raw_present": (raw / spec.name).exists(), "status": (st or {}).get("status"),
                     "content_version": (st or {}).get("content_version")})
    for wid, sp in synth_specs().items():           # 合成世界：没有原始文件（raw_present 恒为 true，ADR-077）
        reason, _info = missing_reason(worlds, wid, raw_dir=raw, data=data, synthetic=sp.params())
        st = Publisher(worlds, wid).read_status() if (worlds / ".status").exists() else None
        rows.append({"world_id": wid, "rebuild": reason is not None, "reason": reason, "raw_present": True,
                     "status": (st or {}).get("status"), "content_version": (st or {}).get("content_version"),
                     "synthetic": True})
    dw = resolve_default_world(worlds)
    if a.json:
        _p(json.dumps({"worlds": rows, "default_world": dw.to_json()}, ensure_ascii=False, indent=1))
    else:
        for r in rows:
            _p(f"{r['world_id']:<13} status={r['status'] or '-':<8} rebuild={'yes' if r['rebuild'] else 'no'} "
               f"reason={r['reason'] or '-'} raw={'yes' if r['raw_present'] else 'no'}")
        _p(f"default world: {dw.world} ({dw.reason}) scenario={dw.scenario or '-'}")
    return 0


def cmd_default_world(a) -> int:
    """打印生效的默认世界（ADR-077 回退规则；mk/m03.mk 与 make run 预检使用）。--json 输出完整判定。"""
    from .defaults import resolve_default_world

    worlds = _safe_path(a.worlds) if a.worlds else default_worlds_dir()
    dw = resolve_default_world(worlds)
    if a.json:
        _p(json.dumps(dw.to_json(), ensure_ascii=False))
    elif a.field == "scenario":
        _p(dw.scenario or "")
    else:
        _p(dw.world)
    return 0


def cmd_clean(a) -> int:
    worlds = _safe_path(a.worlds) if a.worlds else default_worlds_dir()
    targets = []
    if a.staging or a.all:
        targets.append(worlds / ".staging")
    if a.trash or a.all:
        targets.append(worlds / ".trash")
    if a.shared or a.all:
        targets.append(worlds / "_shared")
    for t in targets:
        if t.exists():
            shutil.rmtree(t)
            _p(f"removed {t}")
    return 0


def cmd_export(a) -> int:
    _err("worldpkg export（3D Tiles 1.1、COPC）在 V0.5 提供")
    return 3


# ---------------------------------------------------------------- 入口


def _add_tile_opts(p) -> None:
    p.add_argument("--G", type=int, choices=[16, 32, 64, 128, 256], default=None)
    p.add_argument("--leaf", type=int, default=None)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--forest", choices=["auto", "off"], default=None)
    p.add_argument("--compression", choices=["none"], default=None)


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="worldpkg", description="World Package 构建、切片与校验（M03）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest", help="读取与规范化（写 coordinate.json、DTM 与 .work）")
    p.add_argument("source", choices=["urbanscene3d", "synthetic", "recon", "generic"])
    p.add_argument("--city", help="urbanscene3d 城市 id；synthetic 时为合成世界 id（缺省 synthcity）")
    p.add_argument("--raw")
    p.add_argument("--out", required=True)
    p.add_argument("--keep-work", action="store_true")
    p.add_argument("--semantic", choices=["rules", "csf"], default="rules")
    p.add_argument("--log-json", action="store_true")
    p.set_defaults(fn=cmd_ingest)
    p = sub.add_parser("grid", help="由 .work 计算 DSM、dsm_2m_n 与源点云")
    p.add_argument("stage")
    p.add_argument("--dsm-cell", type=float, default=2.0)
    p.add_argument("--no-dsm-n", action="store_true")
    p.add_argument("--hag", action="store_true", help="同时写 HAG 2 m 栅格（P2，默认不写）")
    p.set_defaults(fn=cmd_grid)
    p = sub.add_parser("tile", help="切片（ANET_Q16 容器、森林、首屏前缀）")
    p.add_argument("stage")
    _add_tile_opts(p)
    p.add_argument("--twin-default", action="store_true")
    p.set_defaults(fn=cmd_tile)
    p = sub.add_parser("package", help="派生、files[]、world.json、deep 校验与 qa/report.json（不发布）")
    p.add_argument("stage")
    _add_tile_opts(p)
    p.add_argument("--no-dsm-n", action="store_true")
    p.add_argument("--keep-work", action="store_true")
    p.set_defaults(fn=cmd_package)
    p = sub.add_parser("zones", help="写 semantic/zones.geojson；--example 输出 M16 curated 区域草稿")
    p.add_argument("stage", nargs="?")
    p.add_argument("--example", metavar="WORLD_ID")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_zones)
    p = sub.add_parser("env", help="写 environment/env.json（默认值取自 contracts）")
    p.add_argument("stage")
    p.set_defaults(fn=cmd_env)
    p = sub.add_parser("qa", help="显示 qa/report.json（门禁、阶段耗时、首屏）")
    p.add_argument("world")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_qa)
    p = sub.add_parser("validate", help="结构 + 语义（+ deep）校验")
    p.add_argument("worlds", nargs="+")
    p.add_argument("--deep", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--strict-warn", action="store_true")
    p.add_argument("--rules")
    p.set_defaults(fn=cmd_validate)
    p = sub.add_parser("build", help="构建并原子发布（--missing 只构建缺失或失效的世界）")
    p.add_argument("ids", nargs="*")
    p.add_argument("--missing", action="store_true")
    p.add_argument("--jobs", type=int, default=3)
    p.add_argument("--raw")
    p.add_argument("--worlds")
    p.add_argument("--keep-staging", action="store_true")
    p.add_argument("--config")
    p.add_argument("--log-json", action="store_true")
    p.add_argument("--no-dsm-n", action="store_true")
    p.add_argument("--hag", action="store_true", help="同时写 HAG 2 m 栅格（P2，默认不写）")
    _add_tile_opts(p)
    p.set_defaults(fn=cmd_build)
    p = sub.add_parser("status", help="逐城状态与 --missing 预演（只读）")
    p.add_argument("--json", action="store_true")
    p.add_argument("--worlds")
    p.add_argument("--raw")
    p.set_defaults(fn=cmd_status)
    p = sub.add_parser("default-world", help="生效的默认世界（AWR_WORLD 显式优先；深圳未构建且合成城市可用时回退，ADR-077）")
    p.add_argument("--json", action="store_true")
    p.add_argument("--field", choices=["world", "scenario"], default="world")
    p.add_argument("--worlds")
    p.set_defaults(fn=cmd_default_world)
    p = sub.add_parser("clean", help="清理 staging、trash、_shared")
    p.add_argument("--staging", action="store_true")
    p.add_argument("--trash", action="store_true")
    p.add_argument("--shared", action="store_true")
    p.add_argument("--all", action="store_true")
    p.add_argument("--worlds")
    p.set_defaults(fn=cmd_clean)
    p = sub.add_parser("export", help="V0.5：3D Tiles 1.1、COPC")
    p.add_argument("id")
    p.add_argument("--tiles3d", action="store_true")
    p.add_argument("--copc", action="store_true")
    p.set_defaults(fn=cmd_export)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = parser()
    a = ap.parse_args(argv)
    if a.cmd == "ingest" and a.source == "urbanscene3d" and not a.city:
        ap.error("ingest urbanscene3d 需要 --city")
    try:
        return int(a.fn(a))
    except WorldpkgError as e:
        _err(f"错误（退出码 {e.exit_code}）：{e}")
        return e.exit_code
    except OSError as e:
        _err(f"错误（退出码 3）：{e}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
