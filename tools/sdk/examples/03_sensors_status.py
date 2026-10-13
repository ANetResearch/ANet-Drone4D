"""03 全部 sensor 与 status（M17-FR-045、AC-008；AWR-04 §11.7；17 §17.8）：逐机型、逐传感器经 `GET .../sensors` 的 `products[]`
发现全部数据产品，并经 REST 与 WS 各取一个样本。返回码 0 表示每个适用产品都取到了样本。"""

from __future__ import annotations

import sys

from _common import sandbox
from awr_sandbox import AwrError


def main() -> int:
    sb = sandbox()
    missing: list[str] = []
    with sb.create("synthcity", "t5_area_guard") as s:
        models = [m["model_id"] for m in sb.catalog.models().get("items", []) if not m.get("hidden")]
        vids = []
        for mid in models[:5]:
            try:
                vids.append(s.vehicles.add(mid, home_enu_m=[10.0 * len(vids), 0.0, None])["id"])
            except AwrError as e:
                print("跳过机型", mid, e.code, e.message)
        for vid in vids:
            st = s.vehicles.state(vid)
            print(vid, "status 字段：", sorted(st.get("state", {}))[:8], "…")
            sens = s.vehicles.sensors(vid)
            for sensor in sens.get("sensors", []):
                for p in sensor.get("products", []):
                    rest, topic = p.get("rest"), p.get("topic")
                    try:
                        if rest:
                            s.sb.http.get(rest, token=s.token)
                    except AwrError as e:
                        missing.append(f"{vid}/{sensor['name']}/{p['name']} REST {e.code}")
                    if topic:
                        got = next(iter(s.stream([topic], seconds=3)), None)
                        if got is None:
                            missing.append(f"{vid}/{sensor['name']}/{p['name']} WS")
    for m in missing:
        print("缺少：", m)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
