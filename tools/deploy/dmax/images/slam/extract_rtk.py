#!/usr/bin/env python3
"""d3-livo 容器内：从真机 bag 抽取 RTK 位置（sensor_msgs/NavSatFix）→ 以首个有效点为原点的本地 ENU（WGS-84），TUM 格式（姿态填单位四元数）。

用于 `slam_regress`（MARS-LVIG HKisland03，M27-FR-110，D3-AC-14）；与 r1 §3.3 `extract_rtk.py` 同口径：丢弃 `status.status < 0`
与纬度为 0 的样本，时刻取消息头。运行环境为镜像自带的 ROS noetic Python（rosbag）。
用法: extract_rtk.py <bag> <out.tum> [topic=/dji_osdk_ros/rtk_position]
"""
import math
import sys

import rosbag

A, F = 6378137.0, 1 / 298.257223563
E2 = F * (2 - F)


def ecef(lat, lon, h):
    lat, lon = math.radians(lat), math.radians(lon)
    n = A / math.sqrt(1 - E2 * math.sin(lat) ** 2)
    return ((n + h) * math.cos(lat) * math.cos(lon), (n + h) * math.cos(lat) * math.sin(lon), (n * (1 - E2) + h) * math.sin(lat))


def main():
    bag, out = sys.argv[1], sys.argv[2]
    topic = sys.argv[3] if len(sys.argv) > 3 else "/dji_osdk_ros/rtk_position"
    ref, n = None, 0
    with open(out, "w") as fo:
        for _, m, _t in rosbag.Bag(bag).read_messages(topics=[topic]):
            if m.status.status < 0 or m.latitude == 0:
                continue
            x, y, z = ecef(m.latitude, m.longitude, m.altitude)
            if ref is None:
                ref = (x, y, z, math.radians(m.latitude), math.radians(m.longitude))
            dx, dy, dz = x - ref[0], y - ref[1], z - ref[2]
            sl, cl, so, co = math.sin(ref[3]), math.cos(ref[3]), math.sin(ref[4]), math.cos(ref[4])
            e = -so * dx + co * dy
            nn = -sl * co * dx - sl * so * dy + cl * dz
            u = cl * co * dx + cl * so * dy + sl * dz
            fo.write(f"{m.header.stamp.to_sec():.6f} {e:.4f} {nn:.4f} {u:.4f} 0 0 0 1\n")
            n += 1
    print("wrote", n, "poses to", out)
    return 0 if n else 1


if __name__ == "__main__":
    sys.exit(main())
