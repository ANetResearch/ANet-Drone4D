"""
r20 SIH probe: connect to N PX4 SIH instances over MAVLink (UDP 14540+i),
arm + takeoff all of them, run an offboard position-step test on instance 0
and a DO_REPOSITION (auto goto) test on instance 1, record trajectories and
measure CPU cost of the px4 processes.

usage: python sih_probe.py N OUTDIR [--speed S]
"""
# ruff: noqa: SIM105, SIM115  （r20 研究脚本原样迁入，只在具备 docker 的机器上重录时运行，M08-FR-079）
import csv
import math
import os
import sys
import threading
import time

import psutil
from pymavlink import mavutil

N = int(sys.argv[1])
OUT = sys.argv[2]
SPEED = float(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[3] == "--speed" else 1.0
os.makedirs(OUT, exist_ok=True)

HOME_AMSL = 489.4  # SIH_LOC_H0 default
PX4_MAIN = dict(MANUAL=1, ALTCTL=2, POSCTL=3, AUTO=4, ACRO=5, OFFBOARD=6, STAB=7)
AUTO_SUB = dict(READY=1, TAKEOFF=2, LOITER=3, MISSION=4, RTL=5, LAND=6)

conns = []
for i in range(N):
    c = mavutil.mavlink_connection(f"udpout:127.0.0.1:{14580 + i}", source_system=250, source_component=190)
    c.mav.heartbeat_send(mavutil.mavlink.MAV_TYPE_GCS, mavutil.mavlink.MAV_AUTOPILOT_INVALID, 0, 0, 0)
    conns.append(c)

rec = {i: [] for i in range(N)}
state = {i: {} for i in range(N)}
stop = False


def reader(i):
    c = conns[i]
    while not stop:
        m = c.recv_match(blocking=True, timeout=0.5)
        if m is None:
            continue
        t = m.get_type()
        st = state[i]
        if t == "LOCAL_POSITION_NED":
            st["lpos"] = (m.time_boot_ms, m.x, m.y, m.z, m.vx, m.vy, m.vz)
            rec[i].append(("lpos", time.time(), m.time_boot_ms, m.x, m.y, m.z, m.vx, m.vy, m.vz))
        elif t == "ATTITUDE":
            st["att"] = (m.time_boot_ms, m.roll, m.pitch, m.yaw)
            rec[i].append(("att", time.time(), m.time_boot_ms, m.roll, m.pitch, m.yaw, m.rollspeed, m.pitchspeed, m.yawspeed))
        elif t == "GLOBAL_POSITION_INT":
            st["gpos"] = (m.lat * 1e-7, m.lon * 1e-7, m.alt * 1e-3, m.relative_alt * 1e-3)
        elif t == "HEARTBEAT" and m.get_srcSystem() == i + 1 and m.get_srcComponent() == 1:
            st["hb"] = (m.base_mode, m.custom_mode, m.system_status)
        elif t == "COMMAND_ACK":
            st.setdefault("acks", []).append((m.command, m.result))
        elif t == "STATUSTEXT":
            st.setdefault("text", []).append(m.text)


def heartbeat_loop():
    while not stop:
        for c in conns:
            c.mav.heartbeat_send(mavutil.mavlink.MAV_TYPE_GCS, mavutil.mavlink.MAV_AUTOPILOT_INVALID, 0, 0, 0)
        time.sleep(0.5)


def cmd(i, command, *p):
    p = list(p) + [0] * (7 - len(p))
    conns[i].mav.command_long_send(i + 1, 1, command, 0, *p)


def set_mode(i, main, sub=0):
    cmd(i, mavutil.mavlink.MAV_CMD_DO_SET_MODE, 1, main, sub)


def px4_procs():
    return [p for p in psutil.process_iter(["name", "cmdline"]) if p.info["name"] == "px4"]


def cpu_seconds():
    import subprocess
    cid = subprocess.run(["docker", "inspect", "-f", "{{.Id}}", "px4sih_r20"], capture_output=True, text=True).stdout.strip()
    with open(f"/sys/fs/cgroup/system.slice/docker-{cid}.scope/cpu.stat") as f:
        for line in f:
            if line.startswith("usage_usec"):
                return int(line.split()[1]) / 1e6
    return 0.0


def host_load():
    return os.getloadavg()[0]


def rss_mb():
    s = 0
    for p in px4_procs():
        try:
            s += p.memory_info().rss
        except psutil.Error:
            pass
    return s / 1e6


threads = [threading.Thread(target=reader, args=(i,), daemon=True) for i in range(N)]
for t in threads:
    t.start()
threading.Thread(target=heartbeat_loop, daemon=True).start()

log = open(os.path.join(OUT, "log.txt"), "w")


def L(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    log.write(s + "\n")
    log.flush()


t0 = time.time()
# wait for heartbeats
while time.time() - t0 < 60 and not all("hb" in state[i] for i in range(N)):
    time.sleep(0.2)
# throttle the onboard stream (gateway best practice): msg_id -> interval_us (-1 = off)
RATES = {105: -1, 31: -1, 331: -1, 36: -1, 290: -1, 291: -1, 83: -1, 230: -1, 241: -1, 30: 50000, 32: 50000, 33: 200000}
for i in range(N):
    for mid, iv in RATES.items():
        cmd(i, mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL, mid, iv)
L("heartbeats:", sum("hb" in state[i] for i in range(N)), "/", N, "after", round(time.time() - t0, 1), "s")

# ---- idle CPU window (disarmed) ----
time.sleep(3)
c0, w0 = cpu_seconds(), time.time()
tb0 = {i: state[i].get("att", (0,))[0] for i in range(N)}
time.sleep(10)
c1, w1 = cpu_seconds(), time.time()
tb1 = {i: state[i].get("att", (0,))[0] for i in range(N)}
rtf_idle = [(tb1[i] - tb0[i]) / 1000.0 / (w1 - w0) for i in range(N)]
L(f"IDLE  N={N} px4 procs={len(px4_procs())} cpu_cores={(c1 - c0) / (w1 - w0):.3f} "
  f"per_inst={(c1 - c0) / (w1 - w0) / N:.3f} rss_total_MB={rss_mb():.0f} "
  f"rtf(min/mean)={min(rtf_idle):.2f}/{sum(rtf_idle) / N:.2f} host_load1={host_load():.1f}")

# ---- arm + takeoff all (auto takeoff) except inst 0 (offboard) ----
TKO_ALT = 10.0


def arm_takeoff(i):
    for _ in range(60):
        cmd(i, mavutil.mavlink.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, float("nan"), float("nan"), float("nan"), HOME_AMSL + TKO_ALT)
        time.sleep(0.2)
        cmd(i, mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 1)
        time.sleep(0.8)
        hb = state[i].get("hb")
        if hb and hb[0] & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED:
            return True
    return False


# instance 0: offboard. Stream setpoints first (proof of life), then arm + switch.
off_sp = [0.0, 0.0, -TKO_ALT]
off_on = False


def offboard_stream():
    while not stop:
        if off_on:
            conns[0].mav.set_position_target_local_ned_send(
                0, 1, 1, mavutil.mavlink.MAV_FRAME_LOCAL_NED,
                0b0000_1111_1111_1000,  # use position only (ignore vel/acc/yaw rate), yaw ignored
                off_sp[0], off_sp[1], off_sp[2], 0, 0, 0, 0, 0, 0, 0, 0)
        time.sleep(0.05)  # 20 Hz


threading.Thread(target=offboard_stream, daemon=True).start()

t_arm = time.time()
res = {}
off_on = True
time.sleep(1.0)
for i in range(N):
    if i == 0:
        ok = False
        for _ in range(60):
            set_mode(0, PX4_MAIN["OFFBOARD"])
            time.sleep(0.2)
            cmd(0, mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 1)
            time.sleep(0.8)
            hb = state[0].get("hb")
            if hb and hb[0] & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED:
                ok = True
                break
        res[0] = ok
    else:
        res[i] = arm_takeoff(i)
L("armed:", sum(res.values()), "/", N, "in", round(time.time() - t_arm, 1), "s")
t_mark = {"offboard_arm": time.time()}

# wait for climb
time.sleep(15)

# ---- flying CPU window ----
c0, w0 = cpu_seconds(), time.time()
tb0 = {i: state[i].get("att", (0,))[0] for i in range(N)}
time.sleep(10)
c1, w1 = cpu_seconds(), time.time()
tb1 = {i: state[i].get("att", (0,))[0] for i in range(N)}
rtf_fly = [(tb1[i] - tb0[i]) / 1000.0 / (w1 - w0) for i in range(N)]
alts = [round(-state[i]["lpos"][3], 1) if "lpos" in state[i] else None for i in range(N)]
L(f"FLY   N={N} cpu_cores={(c1 - c0) / (w1 - w0):.3f} per_inst={(c1 - c0) / (w1 - w0) / N:.3f} "
  f"rss_total_MB={rss_mb():.0f} rtf(min/mean)={min(rtf_fly):.2f}/{sum(rtf_fly) / N:.2f} host_load1={host_load():.1f}")

# ---- step tests ----
# inst 0: offboard 50 m position step north
t_mark["offboard_step"] = time.time()
off_sp[0] = 50.0
# inst 1: DO_REPOSITION 50 m north (auto loiter goto, jerk-limited)
if N > 1 and "gpos" in state[1]:
    lat, lon, alt, rel = state[1]["gpos"]
    dlat = 50.0 / 6378137.0 * 180.0 / math.pi
    conns[1].mav.command_int_send(2, 1, mavutil.mavlink.MAV_FRAME_GLOBAL, mavutil.mavlink.MAV_CMD_DO_REPOSITION, 0, 0,
                                  -1, mavutil.mavlink.MAV_DO_REPOSITION_FLAGS_CHANGE_MODE, 0, float("nan"),
                                  int((lat + dlat) * 1e7), int(lon * 1e7), alt)
    t_mark["reposition"] = time.time()
time.sleep(25)

# ---- wind test on inst 2: set SIH_WIND_N = 8 m/s and observe tilt in hold ----
if N > 2:
    conns[2].mav.param_set_send(3, 1, b"SIH_WIND_N", 8.0, mavutil.mavlink.MAV_PARAM_TYPE_REAL32)
    t_mark["wind"] = time.time()
    time.sleep(20)

stop = True
time.sleep(0.6)
for i in range(min(N, 3)):
    with open(os.path.join(OUT, f"inst{i}.csv"), "w", newline="") as f:
        w = csv.writer(f)
        for r in rec[i]:
            w.writerow(r)
with open(os.path.join(OUT, "marks.csv"), "w") as f:
    for k, v in t_mark.items():
        f.write(f"{k},{v}\n")
for i in range(min(N, 3)):
    L(f"inst{i} texts:", state[i].get("text", [])[-6:])
L("done")
