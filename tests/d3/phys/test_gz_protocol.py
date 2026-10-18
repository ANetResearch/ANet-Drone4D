"""drone4d.gz.v1 宿主侧编解码、run.sdf 组合、实例命令行、跟踪器与 GzSimBackend 的离线检查（不需要 gz；M24 §6.5、§7.4）。"""

from __future__ import annotations

import re
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest

from awr.phys import PhysError
from awr.phys.adapter import TierCommand, VehicleStatus
from awr.phys.gz import protocol as P
from awr.phys.gz.compose import UavSpawn, compose_run
from awr.phys.gz.instance import GzInstance
from awr.phys.tracker import Tracker

ROOT = Path(__file__).resolve().parents[3]
PROTO = ROOT / "native" / "gz" / "proto" / "drone4d_gz.proto"


def _parse_proto(text: str) -> dict[str, list[tuple[str, int, str, bool]]]:
    text = re.sub(r"//[^\n]*", "", text)
    out: dict[str, list[tuple[str, int, str, bool]]] = {}
    for name, body in re.findall(r"message\s+(\w+)\s*\{((?:[^{}]|\{[^{}]*\})*)\}", text):
        body = re.sub(r"oneof\s+\w+\s*\{([^{}]*)\}", r"\1", body)
        out[name] = [(f, int(n), t, bool(rep)) for rep, t, f, n in re.findall(r"(repeated\s+)?(\w+)\s+(\w+)\s*=\s*(\d+)\s*;", body)]
    return out


def test_fields_match_proto() -> None:
    parsed = _parse_proto(PROTO.read_text(encoding="utf-8"))
    env = parsed.pop("Envelope")
    assert env[0] == ("protocol", 1, "uint32", False)
    assert [(f, n) for f, n, _t, _r in env[1:]] == [(b, i + 2) for i, b in enumerate(P.BODY)]
    assert parsed == P.FIELDS


def test_round_trip_and_samples() -> None:
    s = np.arange(2 * 3 * P.STATE_N, dtype=np.float64).reshape(2, 3, P.STATE_N)
    sd = P.msg("StepDone", seq=7, tick=3, n_steps=2, n_uav=3, t_ns=[4_000_000, 8_000_000], samples=s.astype("<f8").tobytes(),
               contacts=[P.msg("Contact", uav_index=1, step=1, kind=4, pz=0.2, speed=3.5)], gz_rtf=12.5)
    body, m = P.decode(P.encode("step_done", sd))
    assert body == "step_done" and m.seq == 7 and list(m.t_ns) == [4_000_000, 8_000_000]
    assert np.array_equal(P.samples_array(m, 3), s)
    assert m.contacts[0].kind == 4 and m.contacts[0].speed == 3.5
    with pytest.raises(ValueError):
        P.decode(P._C["Envelope"](protocol=2, bye=P.msg("Bye")).SerializeToString())


def test_compose_run_xml() -> None:
    sdf = compose_run([UavSpawn("uav-01", (1.0, 2.0, 0.3)), UavSpawn("uav-02", (5.0, 0.0, 0.3), yaw_rad=1.0)], ground_plane=True)
    root = ET.fromstring(sdf.encode())
    world = root.find("world")
    clock = next(p for p in world.findall("plugin") if p.get("name") == "drone4d::Drone4DClock")
    assert [u.get("model") for u in clock.findall("uav")] == ["uav-01", "uav-02"]
    assert clock.findtext("step_ns") == "4000000" and clock.findtext("lockstep") == "true"
    assert world.find("physics/max_step_size").text == "0.004000"
    m1 = next(m for m in world.findall("model") if m.get("name") == "uav-01")
    assert m1.findtext("pose").split()[:3] == ["1", "2", "0.06"]
    assert len([p for p in m1.findall("plugin") if "MulticopterMotorModel" in p.get("name")]) == 4
    with pytest.raises(ValueError):
        compose_run([], step_ns=4_000_500)


def test_instance_commands(tmp_path: Path) -> None:
    d = GzInstance(tmp_path, "d3-gz-t", dry_run=True)
    cmd = d.start("<sdf/>")
    assert cmd[:3] == ["docker", "run", "-d"] and "--network" in cmd and cmd[-1] == "/run/d4d-sdf/run.sdf"
    assert d.gz_endpoint == "ipc:///run/d4d/ctl.sock" and d.endpoint.endswith("/sock/ctl.sock")
    assert (tmp_path / "run.sdf").read_text() == "<sdf/>"
    loc = GzInstance(tmp_path, "x", launcher="local", dry_run=True)
    assert loc.command()[:3] == ["gz", "sim", "-s"] and loc.gz_endpoint == loc.endpoint


def test_tracker_setpoints() -> None:
    tr = Tracker()
    p0 = np.array([0.0, 0.0, 0.2])
    tr.set(TierCommand("u", "takeoff", 0, alt_m=5.0), p0)
    assert tr.setpoint("u", p0, np.zeros(3)).v_enu == (0.0, 0.0, 1.5)
    near = tr.setpoint("u", np.array([0.0, 0.0, 5.0]), np.zeros(3)).v_enu
    assert 0.0 < near[2] < 0.3
    tr.set(TierCommand("u", "goto", 1, target_enu_m=(100.0, 0.0, 5.0), speed_mps=4.0), p0)
    v = tr.setpoint("u", np.array([0.0, 0.0, 5.0]), np.zeros(3)).v_enu
    assert v[0] == pytest.approx(4.0) and v[2] == 0.0
    tr.set(TierCommand("u", "kill", 2), p0)
    assert tr.setpoint("u", p0, np.zeros(3)).enabled is False
    with pytest.raises(PhysError) as e:
        tr.set(TierCommand("u", "orbit", 3), p0)
    assert e.value.code == 630


class _FakeClock:
    """REP 端的最小 Drone4DClock：静止于 z = 0.2 的机体，按协议回复（用于宿主侧网格与指令压缩检查）。"""

    def __init__(self, endpoint: str, n_uav: int, *, skew_ns: int = 0) -> None:
        import zmq

        self.sock = zmq.Context.instance().socket(zmq.REP)
        self.sock.bind(endpoint)
        self.n_uav, self.skew, self.t, self.setpoints = n_uav, skew_ns, 0, []
        self.th = threading.Thread(target=self.run, daemon=True)
        self.th.start()

    def run(self) -> None:
        row = np.zeros(P.STATE_N)
        row[2], row[6], row[15] = 0.2, 1.0, 9.80665
        while True:
            body, m = P.decode(self.sock.recv())
            if body == "hello":
                st = np.tile(row, (self.n_uav, 1))
                self.sock.send(P.encode("hello_ack", P.msg("HelloAck", n_uav=self.n_uav, t_ns=0, initial_state=st.tobytes())))
            elif body == "control":
                self.setpoints.append([(s.uav_index, s.enabled, s.vz) for s in m.setpoints])
                t = [self.t + (j + 1) * 4_000_000 + self.skew for j in range(m.grant_steps)]
                self.t += m.grant_steps * 4_000_000
                s = np.tile(row, (m.grant_steps, self.n_uav, 1))
                self.sock.send(P.encode("step_done", P.msg("StepDone", seq=m.seq, tick=m.tick, n_steps=m.grant_steps, n_uav=self.n_uav,
                                                           t_ns=t, samples=s.tobytes())))
            else:
                self.sock.send(P.encode("bye", P.msg("Bye", iterations=self.t // 4_000_000)))
                self.sock.close(0)
                return


def test_backend_against_fake_clock(tmp_path: Path) -> None:
    pytest.importorskip("zmq")
    from awr.phys.gz.backend import GzSimBackend

    ep = f"ipc://{tmp_path / 'ctl.sock'}"
    fake = _FakeClock(ep, 2)
    be = GzSimBackend(["uav-01", "uav-02"], ep, timeout_s=5.0)
    be.open()
    r0 = be.advance(0)
    assert (r0.t_start_ns, r0.t_end_ns, r0.n_steps) == (0, 100_000_000, 25)
    assert r0.status == {"uav-01": VehicleStatus.GROUNDED, "uav-02": VehicleStatus.GROUNDED}
    be.submit([TierCommand("uav-02", "takeoff", 1, alt_m=5.0)])
    be.advance(1)
    be.advance(2)
    assert fake.setpoints[0] == [(0, False, 0.0), (1, False, 0.0)]       # 地面出生：电机失能
    assert fake.setpoints[1] == [(1, True, 1.5)] and fake.setpoints[2] == []  # 只下发变化的设定
    assert be.status()["uav-02"] == VehicleStatus.AIRBORNE
    assert be.history.latest_t("uav-01") == 300_000_000
    info = be.close()
    assert info["iterations"] == 75 and len(info["pose_sha256"]) == 64


def test_backend_rejects_off_grid(tmp_path: Path) -> None:
    pytest.importorskip("zmq")
    from awr.phys.gz.backend import GzSimBackend

    ep = f"ipc://{tmp_path / 'ctl.sock'}"
    _FakeClock(ep, 1, skew_ns=1)
    be = GzSimBackend(["uav-01"], ep, timeout_s=5.0)
    be.open()
    with pytest.raises(PhysError) as e:
        be.advance(0)
    assert e.value.code == 635
    be.close()
