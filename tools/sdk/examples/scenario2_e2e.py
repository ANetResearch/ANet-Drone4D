"""应用场景二端到端（D2-AC-36、M17-AC-013）：只经 SDK 增删无人机、申请租约后以速度指令手动控制并释放、拉取 §11.7 全部 sensor
与 status、订阅感知与事件，最后结束会话。每一步打印 PASS 或 FAIL，全部通过时退出码 0。"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable

from _common import sandbox
from awr_sandbox import AwrError

results: list[tuple[str, bool, str]] = []


def step(name: str, fn: Callable[[], object]) -> object:
    try:
        out = fn()
        results.append((name, True, ""))
        print("PASS", name)
        return out
    except (AwrError, AssertionError, KeyError, StopIteration) as e:
        results.append((name, False, str(e)))
        print("FAIL", name, e)
        return None


def main() -> int:
    sb = sandbox()
    s = sb.create("synthcity", "t6_manual")
    try:
        a = step("add vehicle", lambda: s.vehicles.add("p600_mid360", home_enu_m=[0.0, 0.0, None]))
        b = step("add second vehicle", lambda: s.vehicles.add("p600_mid360", home_enu_m=[8.0, 0.0, None]))
        if b:
            step("remove vehicle", lambda: s.vehicles.remove(b["id"]))
        vid = a["id"] if a else None
        if vid:
            def takeoff() -> object:
                # 新加的机体先要进入机群并完成启动（107 NO_VEHICLE、108 生命周期不在 READY）：短暂重试（D2-INT）
                end = time.monotonic() + 20.0
                while True:
                    try:
                        r = s.command(vid, "takeoff", alt_m=20.0)
                    except AwrError as e:
                        if getattr(e, "code", None) not in (107, 108) or time.monotonic() > end:
                            raise
                        time.sleep(0.5)
                        continue
                    if isinstance(r, dict) and r.get("status") == "rejected":
                        if r.get("code") in (107, 108) and time.monotonic() <= end:
                            time.sleep(0.5)
                            continue
                        raise AssertionError(f"takeoff rejected {r.get('code')}")
                    return r

            step("takeoff", takeoff)
            end = time.monotonic() + 60.0  # 速度控制需要 FLYING（起飞到 20 m 约 10 s；D2-INT：此前只等 3 s，租约 105）
            while time.monotonic() < end and (s.vehicles.state(vid)["state"] or {}).get("flight_state") != "FLYING":
                time.sleep(0.5)

            def manual() -> None:
                with s.acquire(vid):
                    for _ in range(75):
                        s.velocity(vid, 2.0, 0.0, 0.0)
                        time.sleep(0.04)
                    s.velocity_stop(vid)

            step("lease + velocity + release", manual)
            step("state", lambda: s.vehicles.state(vid)["state"])
            sens = step("sensors", lambda: s.vehicles.sensors(vid)) or {}
            for sensor in sens.get("sensors", []):
                for p in sensor.get("products", []):
                    if p.get("rest"):
                        step(f"REST {sensor['name']}/{p['name']}", lambda p=p: s.sb.http.get(p["rest"], token=s.token))
            step("perception", lambda: s.vehicles.perception(vid))
            step("stream perception", lambda: next(iter(s.stream([f"uav/{vid}/perception"], seconds=5))))
        step("events", lambda: s.events(0)["items"])
        step("stream session", lambda: next(iter(s.stream(["sandbox/session"], seconds=3))))
    finally:
        s.close()
    ok = all(r[1] for r in results)
    print("RESULT", "PASS" if ok else "FAIL", f"{sum(r[1] for r in results)}/{len(results)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
