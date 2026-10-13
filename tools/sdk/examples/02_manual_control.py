"""02 手动控制：添加一架 P600、起飞、申请租约后以速度指令（WS CLIENT_DATA，<= 50 Hz）向东飞 5 秒、停止、释放租约、降落。"""

from __future__ import annotations

import time

from _common import sandbox


def main() -> None:
    sb = sandbox()
    with sb.create("synthcity", "t6_manual") as s:
        v = s.vehicles.add("p600_mid360", home_enu_m=[0.0, 0.0, None], label="手动机")
        vid = v["id"]
        s.command(vid, "arm")
        s.command(vid, "takeoff", alt_m=30.0)
        with s.acquire(vid, priority="normal"):
            t_end = time.monotonic() + 5.0
            while time.monotonic() < t_end:
                s.velocity(vid, 3.0, 0.0, 0.0)          # 东向 3 m/s（world 坐标系）
                time.sleep(0.04)                          # 25 Hz
            s.velocity_stop(vid)
        s.command(vid, "land")
        print("位置：", s.vehicles.state(vid)["state"].get("pos"))


if __name__ == "__main__":
    main()
