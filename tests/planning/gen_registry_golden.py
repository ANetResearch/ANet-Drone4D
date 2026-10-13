"""生成注册表等价黄金数据（M10-AC-201）：`python tests/planning/gen_registry_golden.py [--check]`。

在 D1 if/elif 分派下（WP-15 改造前）运行一次写入 `golden/registry_d1.json`；之后 `--check` 在当前分派下重放并逐项比较
（`test_registry.py` 做同样的比较）。合成小世界写在临时目录；六城缺失时对应用例不写入。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from registry_cases import CITIES, GOLDEN, ROOT, city_cases, scenario_cases, summary, tiny_cases  # noqa: E402

from awr.sim.planning.worker import set_world, worker_main  # noqa: E402

CACHE_ROOT: Path | None = None


def _open_city(name: str):
    from awr.world.geometry.query import open_world_query
    from awr.world.geometry.types import GeoLoadError

    d = ROOT / "worlds" / name
    if not (d / "world.json").exists():
        return None
    if CACHE_ROOT is not None:                      # 派生到独立目录（不改动 worlds/.geo-cache）
        return open_world_query(d, CACHE_ROOT, allow_derive=True)
    try:
        return open_world_query(d, allow_derive=False)
    except GeoLoadError as e:
        print(f"skip {name}: {e}", file=sys.stderr)
        return None


def run_all() -> dict[str, list]:
    from awr.world.geometry.fake import fake_world_query

    out: dict[str, list] = {}
    with tempfile.TemporaryDirectory() as td:
        w = fake_world_query(Path(td))
        set_world((w.world_id, w.content_version, w.coordinate_sha256), w)
        for req in tiny_cases(w):
            out[req.job_id] = summary(worker_main(req))
    for name in CITIES:
        w = _open_city(name)
        if w is None:
            continue
        set_world((w.world_id, w.content_version, w.coordinate_sha256), w)
        for req in city_cases(name, w):
            out[req.job_id] = summary(worker_main(req))
        sc = {"shenzhen": "s1-shenzhen-facade", "chicago": "s4-chicago-lakeshore"}.get(name)
        if sc:
            for req in scenario_cases(sc, w):
                out[req.job_id] = summary(worker_main(req))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--cache-root", default=None, help="六城派生缓存目录（缺省用 worlds/.geo-cache 且不派生）")
    a = ap.parse_args()
    global CACHE_ROOT
    CACHE_ROOT = Path(a.cache_root) if a.cache_root else None
    got = run_all()
    if a.check:
        ref = json.loads(GOLDEN.read_text(encoding="utf-8"))
        bad = {k: (ref.get(k), v) for k, v in got.items() if k in ref and ref[k] != v}
        print(json.dumps({"n": len(got), "compared": sum(k in ref for k in got), "mismatch": bad}, indent=1))
        return 1 if bad else 0
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    st: dict[str, int] = {}
    for v in got.values():
        st[v[0]] = st.get(v[0], 0) + 1
    print(len(got), st)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
