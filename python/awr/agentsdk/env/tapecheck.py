"""BenchEnv 打开磁带时的世界版本与 NavMesh 校验（M23 §6.7.6、M26 §6.6.1；ADR-196 决策 (5)；原因码 615 `TAPE_WORLD_MISMATCH`）。

两级判据：

1. 所有运行：磁带头 `world.content_version` 须等于所加载世界包 `world.json` 的 `contentVersion`（`TapeReader.check_world`，与
   采集端 `sensorsvc.capture`、运行时 `EventRuntime.load` 同一判据），不符即 615；世界包缺 `world.json` 的开发夹具（DSM 合成世界）
   只在非正式运行中放行；
2. 正式运行（`is_formal`：排名运行且物理档为 `realistic`，即正式成绩的唯一来源）另核对磁带出处与寻路图层：
   - 磁带清单 `tape.json` 的 `provenance.generator_version` 以 `-grid` 结尾（`awr-navgrid@1` 栅格后端编译，只供无 NavMesh 图层的
     测试夹具）→ 615 `why = grid_generator`；清单或出处缺失 → 615 `why = provenance_missing`；
   - 磁带头 `world.navmesh_sha256`（{profile: sha256}）逐 profile 与世界包 `detour-navmesh@1` 图层的 `bin.sha256` 比对：图层缺失
     → 615 `why = navmesh_layer_missing`，不等 → 615 `why = navmesh_mismatch`；磁带未记录 NavMesh 哈希 → 615
     `why = navmesh_unrecorded`。

`content_version` 已覆盖图层 sha256（ADR-196 决策 (2)），第 2 级在其之上排除"为同一世界版本、但以栅格后端编译"的磁带。
校验结果 `{content_version, formal, generator_version?, navmesh?}` 写入运行清单 `tape_check`。raycast 网格哈希不在此复算
（逐文件 SHA-256，世界包可达数百 MB）；`content_version` 相同即网格相同。
"""

from __future__ import annotations

import json
from pathlib import Path

__all__ = ["GRID_SUFFIX", "check_tape_world", "is_formal", "world_content_version", "world_navmesh_sha256"]

GRID_SUFFIX = "-grid"


def is_formal(spec) -> bool:
    """正式运行：排名运行且物理档为 `realistic`（ADR-196 决策 (5) 的"正式运行"；开发档与 Fast 档的排名运行不出正式成绩）。"""
    return bool(getattr(spec, "ranked", False)) and getattr(spec, "physics", None) == "realistic"


def world_content_version(world_dir: Path | str) -> str | None:
    """世界包 `world.json` 的 `contentVersion`；无 `world.json` 时为 None。"""
    p = Path(world_dir) / "world.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8")).get("contentVersion")


def world_navmesh_sha256(world_dir: Path | str) -> dict[str, str]:
    """世界包各 profile 的 `detour-navmesh@1` 图层 `bin.sha256`（即编译器写入磁带头的 `navmesh_sha256`，navsvc `hello` 的 profile 哈希）。"""
    from awr.sim.events.nav import navmesh_profiles

    return {p: str((d.get("bin") or {}).get("sha256", "")) for p, d in navmesh_profiles(world_dir).items()}


def _provenance(tape) -> dict | None:
    path = getattr(tape, "path", None)
    m = Path(path).parent / "tape.json" if path is not None else None
    if m is None or not m.is_file():
        return None
    return json.loads(m.read_text(encoding="utf-8")).get("provenance")


def check_tape_world(tape, world_dir: Path | str, *, formal: bool) -> dict:
    """按模块文档的两级判据校验 `tape`（`TapeReader`）与 `world_dir`；不符抛 `EventError(615)`，通过时返回校验摘要。"""
    from awr.sim.events.errors import EventError

    w = tape.header.get("world") or {}
    cv = world_content_version(world_dir)
    if cv is None:
        if formal:
            raise EventError(615, "formal runs need the world manifest (world.json) to check the tape against",
                             {"why": "world_manifest_missing", "world": Path(world_dir).name, "tape": {"content_version": w.get("content_version")}})
    else:
        tape.check_world(cv)
    out: dict = {"content_version": cv, "formal": bool(formal)}
    if not formal:
        return out
    prov = _provenance(tape)
    gv = (prov or {}).get("generator_version")
    if not gv:
        raise EventError(615, "formal runs need the tape provenance (tape.json generator_version)", {"why": "provenance_missing"})
    if str(gv).endswith(GRID_SUFFIX):
        raise EventError(615, "tape was compiled with the awr-navgrid@1 fixture backend; formal runs accept Detour tapes only (ADR-196 (5))",
                         {"why": "grid_generator", "generator_version": gv})
    want = w.get("navmesh_sha256")
    if not isinstance(want, dict) or not want:
        raise EventError(615, "tape header records no NavMesh hashes", {"why": "navmesh_unrecorded", "navmesh_sha256": want})
    have = world_navmesh_sha256(world_dir)
    for prof, sha in sorted(want.items()):
        if prof not in have:
            raise EventError(615, f"world has no detour-navmesh@1 layer for profile {prof!r}",
                             {"why": "navmesh_layer_missing", "profile": prof, "loaded": sorted(have)})
        if have[prof] != sha:
            raise EventError(615, f"tape NavMesh ({prof}) differs from the world layer",
                             {"why": "navmesh_mismatch", "profile": prof, "tape": sha, "loaded": have[prof]})
    out.update(generator_version=gv, navmesh={p: want[p] for p in sorted(want)})
    return out
