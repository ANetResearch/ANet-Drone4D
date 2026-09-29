"""S3 类检测场景（测试用）：a1 天底 RGB 搜索、b1 热成像确认、c1 中继（无检测能力）。"""

from __future__ import annotations

import numpy as np

T1 = np.array([40.0, -20.0, 0.0])


def setup_s3(b, *, targets=True):
    rt = b.rt
    rt.configure_vehicle("a1", caps=["rgb.zoom"])
    rt.configure_vehicle("b1", caps=["thermal.imaging"])
    rt.configure_vehicle("c1", caps=["comm.relay"])
    b.spawn(0, 1, pos=(T1[0] + 3.0, T1[1] - 2.0, 60.0), vid="a1")
    b.spawn(1, 2, pos=(900.0, 900.0, 60.0), vid="b1")
    b.spawn(2, 3, pos=(T1[0] - 5.0, T1[1] + 4.0, 50.0), vid="c1")
    b.run(0.02)
    rt.set_default_for(0, "expanding_square", {})
    rt.set_default_for(2, "expanding_square", {})
    if targets:
        rt.spawn_target("t1", T1.tolist(), "person", conf_first=0.42, conf_confirm=0.9)
    b.run(1.2)  # 云台转到天底


def move_b1_to_orbit(b):
    b.set_pose(1, (T1[0] + 20.0, T1[1], 60.0), yaw_deg=180.0)
    b.rt.set_default_for(1, "orbit", {"center_enu_m": T1.tolist()})


def run_s3(b, seconds=60.0):
    moved = [False]

    def on_tick(bb, t):
        if not moved[0] and bb.events.of("sensor.detect"):
            move_b1_to_orbit(bb)
            moved[0] = True

    b.run(seconds, on_tick=on_tick)
    return b.events.of("sensor.detect")
