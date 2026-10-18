"""命令行（M27 §7.10 `awr.bench.rerun.register` 的实现入口）：

    python -m awr.bench.catalog register --catalog eval|dist --run <run_dir | export_dir | export.json> [--url URL]
    python -m awr.bench.catalog asset --catalog eval|dist --dataset <world>.<event> --file basemap.rrd [--url URL]
    python -m awr.bench.catalog rebuild --catalog eval|dist [--url URL]          # 由数据集目录重建注册清单
    python -m awr.bench.catalog deregister --file <x.rrd> [--url-eval URL] [--url-dist URL]
    python -m awr.bench.catalog list --catalog eval|dist
    python -m awr.bench.catalog serve --catalog eval|dist [--host H] [--port P]  # catalog 服务：启动时重放清单
    python -m awr.bench.catalog perf --work DIR [--runs 3] [--uavs 3] [--duration 360] [--out result.json]

数据根取 `--data-root` 或 `AWR_BENCH_DATA_ROOT`（缺省 /data/d3），注册清单位于 `<data_root>/rerun/<catalog>/registry.json`
（`--root` 可改）。未给出 `--url` 时连接 `service.json` 记录的在线服务；服务未启动时只写清单，服务启动时重放。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from awr.bench.catalog.registry import (
    CATALOGS,
    CatalogError,
    Registry,
    deregister_callback,
    rebuild,
    register_asset,
    register_run,
)
from awr.bench.catalog.service import CatalogService, live_client


def _export_of(p: Path) -> Path:
    for c in (p, p / "export.json", p / "derived" / "rrd" / "export.json"):
        if c.is_file():
            return c
    raise SystemExit(f"{p}: no export.json (run the rrd export first)")


def main(argv: list[str] | None = None) -> int:
    from awr.bench.settings import BenchSettings

    ap = argparse.ArgumentParser(prog="python -m awr.bench.catalog", description="Rerun eval / dist catalogs (M27 §4.7)")
    ap.add_argument("--data-root", type=Path, default=None)
    ap.add_argument("--root", type=Path, default=None, help="registry root (default <data_root>/rerun)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("register")
    r.add_argument("--catalog", choices=CATALOGS, required=True)
    r.add_argument("--run", type=Path, required=True)
    r.add_argument("--url", default=None)
    a = sub.add_parser("asset")
    a.add_argument("--catalog", choices=CATALOGS, required=True)
    a.add_argument("--dataset", required=True)
    a.add_argument("--file", type=Path, required=True)
    a.add_argument("--url", default=None)
    b = sub.add_parser("rebuild")
    b.add_argument("--catalog", choices=CATALOGS, required=True)
    b.add_argument("--url", default=None)
    d = sub.add_parser("deregister")
    d.add_argument("--file", type=Path, required=True)
    d.add_argument("--url-eval", default=None)
    d.add_argument("--url-dist", default=None)
    ls = sub.add_parser("list")
    ls.add_argument("--catalog", choices=CATALOGS, required=True)
    s = sub.add_parser("serve")
    s.add_argument("--catalog", choices=CATALOGS, required=True)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=None)
    p = sub.add_parser("perf")
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--uavs", type=int, default=3)
    p.add_argument("--duration", type=float, default=360.0)
    p.add_argument("--cam-hz", type=float, default=10.0)
    p.add_argument("--lidar-points", type=int, default=5000)
    p.add_argument("--reps", type=int, default=10)
    p.add_argument("--keep", action="store_true")
    p.add_argument("--no-optimize", action="store_true")
    p.add_argument("--out", type=Path, default=None)
    ns = ap.parse_args(argv)
    st = BenchSettings.from_env(**({"data_root": ns.data_root} if ns.data_root else {}))
    root = ns.root or st.data_root / "rerun"
    try:
        if ns.cmd == "register":
            out = register_run(_export_of(ns.run), catalog=ns.catalog, root=root, client=live_client(root, ns.catalog, ns.url))
        elif ns.cmd == "asset":
            out = register_asset(ns.file, dataset=ns.dataset, catalog=ns.catalog, root=root, client=live_client(root, ns.catalog, ns.url))
        elif ns.cmd == "rebuild":
            out = rebuild(st.data_root, catalog=ns.catalog, root=root, client=live_client(root, ns.catalog, ns.url))
        elif ns.cmd == "deregister":
            clients = {c: x for c, u in (("eval", ns.url_eval), ("dist", ns.url_dist)) for x in [live_client(root, c, u)] if x is not None}
            deregister_callback(root, clients)(ns.file)
            out = {"deregistered": str(ns.file), "live": sorted(clients)}
        elif ns.cmd == "list":
            reg = Registry.load(root, ns.catalog)
            out = {"catalog": ns.catalog, "datasets": {ds: sorted({f"{e.segment}/{e.layer}" for e in reg.of_dataset(ds)}) for ds in reg.datasets()},
                   "assets": sorted(a["path"] for a in reg.assets.values())}
        elif ns.cmd == "serve":
            svc = CatalogService(root, ns.catalog, host=ns.host, port=ns.port).start()
            print(json.dumps({"catalog": ns.catalog, "url": svc.url, "replay": svc.report}, ensure_ascii=False), flush=True)
            svc.serve_forever()
            return 0
        else:
            from awr.bench.catalog.perf import measure

            out = measure(ns.work, runs=ns.runs, n_uav=ns.uavs, duration_s=ns.duration, cam_hz=ns.cam_hz, lidar_points=ns.lidar_points,
                          reps=ns.reps, keep=ns.keep, optimize=not ns.no_optimize)
            if ns.out:
                ns.out.write_text(json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    except CatalogError as e:
        print(json.dumps({"ok": False, "code": e.code, "error": str(e), "detail": e.detail}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **out}, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
