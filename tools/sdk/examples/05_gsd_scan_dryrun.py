"""05 GSD 完整扫描的预览（dry-run）：给出区域与所需 GSD，打印航高、条带、架数与完工时间曲线。"""

from __future__ import annotations

from _common import sandbox


def main() -> None:
    sb = sandbox()
    with sb.create("synthcity", "t4_scan_gsd") as s:
        req = {"type": "area_scan_gsd", "params": {"polygon": [[0, 0], [300, 0], [300, 200], [0, 200]], "gsd_cm": 3.0,
                                                   "sidelap": 0.1}}
        pv = s.tasks.create(req, dry_run=True)
        print("预览", pv.get("preview_id"), "架数", pv.get("n_vehicles"), "完工", pv.get("makespan_s"), "s")
        print("扫描参数", pv.get("scan"))
        for p in pv.get("makespan_curve", []):
            print(f"  n={p['n']}  makespan={p['makespan_s']:.0f} s")


if __name__ == "__main__":
    main()
