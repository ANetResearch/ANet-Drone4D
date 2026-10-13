"""沙盒运行时的接线钩子（M08-FR-225；AWR-04 §4.4.3、§10.6）。

- velocity setpoint 软围栏钩子（`register_setpoint_filter`）：ingest 在写入新鲜 setpoint 之后按会话的 `keepout_guard` 调用，
  沙盒档缺省 clamp、d1_250 缺省 off；钩子可原地改写 `S.vel_cmd`；
- 预热进程接替载荷 → 环境（`standby_env`）：`binding` 映射为 AWR_RUN、AWR_WORLD、AWR_RUN_DIR、AWR_SECRET_FILE，
  `config_snapshot` 映射为 AWR_SESSION_CONFIG；
- 会话 limits 经 AWR_SANDBOX_LIMITS 覆盖（非法内容忽略）。
"""

from __future__ import annotations

import numpy as np
import pytest
from simlib import CoreHarness

from awr.contracts.layouts import BUS_SETPOINT32
from awr.sim.fleet.stages import registry as R
from awr.sim.runtime.main import standby_env


def _sp(h: CoreHarness, vel_enu, seq: int) -> None:
    s = h.slot()
    ag = next(a for a, sl in h.core.agent_slot.items() if sl == s)
    r = np.zeros(1, BUS_SETPOINT32)
    r["agent_no"] = ag
    r["seq"] = seq
    r["vel"] = vel_enu
    h.core.on_setpoint(r.tobytes())


@pytest.mark.parametrize(("profile", "guard", "want"), [("sandbox100", None, "clamp"), ("d1_250", None, "off"),
                                                       ("sandbox100", "warn", "warn")])
def test_setpoint_filter_called_with_guard_mode(profile: str, guard: str | None, want: str) -> None:
    calls: list[tuple[list[int], list[str]]] = []

    def keepout(S, slots, modes, ctx) -> None:
        calls.append((slots.tolist(), list(modes)))
        S.vel_cmd[slots, 0] = np.minimum(S.vel_cmd[slots, 0], 0.5)  # NED 北向限幅（模拟 clamp）

    with R.isolated_registry() as reg:
        R.register_setpoint_filter(keepout, owner="M18")
        R.register_setpoint_filter(keepout, owner="M18")  # 重复登记忽略
        assert len(reg.setpoint_filters) == 1
        h = CoreHarness(n=1, reg=reg, fleet_cfg={"clock_profile": profile})
        try:
            h.takeoff(10.0)
            args = {"frame": "world"} if guard is None else {"frame": "world", "keepout_guard": guard}
            adm = h.cmd("velocity", args, cid="v-1")
            assert adm["status"] == "accepted", adm
            for k in range(20):
                _sp(h, (0.0, 3.0, 0.0), k)  # ENU 北向 3 m/s
                h.W[0] += h.core.clock.tick_ns
                h.core.iterate()
            assert calls, "setpoint 钩子未被调用"
            slots, modes = calls[-1]
            assert slots == [h.slot()] and modes == [want]
            assert h.S.vel_cmd[h.slot(), 0] == pytest.approx(0.5)
        finally:
            h.close()


def test_standby_env_mapping() -> None:
    env = standby_env({"env": {"AWR_X": 1}, "binding": {"run_id": "sb-7", "world_id": "shenzhen",
                                                         "ring_path": "/dev/shm/awr/sb-7/state.sim-core",
                                                         "namespace": "awr/sb-7", "secret_path": "/run/awr/sb-7.key"},
                       "config_snapshot": "/var/awr/sb-7/session.json"})
    assert env == {"AWR_X": "1", "AWR_RUN": "sb-7", "AWR_WORLD": "shenzhen", "AWR_RUN_DIR": "/dev/shm/awr/sb-7",
                   "AWR_SECRET_FILE": "/run/awr/sb-7.key", "AWR_SESSION_CONFIG": "/var/awr/sb-7/session.json"}
    assert standby_env({"env": {"A": "b"}}) == {"A": "b"}  # D1 载荷不变


def test_sandbox_limits_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWR_SANDBOX_LIMITS", '{"targets": 10, "vehicles": 4, "bogus": 3, "clones": "x"}')
    with R.isolated_registry() as reg:
        h = CoreHarness(n=0, reg=reg, fleet_cfg={"clock_profile": "sandbox100"}, ready=False)
        try:
            assert h.core.limits == {"targets": 10, "vehicles": 4, "clones": 8}
        finally:
            h.close()
    monkeypatch.setenv("AWR_SANDBOX_LIMITS", "[1, 2]")
    with R.isolated_registry() as reg:
        h = CoreHarness(n=0, reg=reg, fleet_cfg={"clock_profile": "sandbox100"}, ready=False)
        try:
            assert h.core.limits == {"targets": 30, "vehicles": 12, "clones": 8}
        finally:
            h.close()


def test_sandbox_world_unavailable_fails_start(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from awr.sim.runtime.main import WorldUnavailable

    monkeypatch.setenv("AWR_GEO_ALLOW_DERIVE", "0")
    over = {"load_world": True, "world_id": "no_such_world", "worlds_dir": tmp_path}
    with R.isolated_registry() as reg, pytest.raises(WorldUnavailable) as ei:
        CoreHarness(n=0, reg=reg, fleet_cfg={"clock_profile": "sandbox100"}, ready=False, cfg_over=over)
    assert ei.value.code == 502 and ei.value.world_id == "no_such_world"
    with R.isolated_registry() as reg:  # d1_250（共享展示）：退化为水平地面，行为不变
        h = CoreHarness(n=0, reg=reg, ready=False, cfg_over=over)
        try:
            assert h.core.world is None and h.core.world_error
        finally:
            h.close()


def test_interest_streams_roster_and_entity_views() -> None:
    import msgpack

    with R.isolated_registry() as reg:
        R.register_entity_block("target", "targets", "M18", {"tgt_pos": (np.float32, (3,))})
        h = CoreHarness(n=1, reg=reg, fleet_cfg={"clock_profile": "sandbox100"})
        try:
            core = h.core
            core._dispatch(("interest", msgpack.packb({"v": 1, "seq": 1, "detail": [0], "marks": [], "topics": ["nav"],
                                                       "streams": {"nav": [3, 0], "lidar": [0], "bogus": [1]}})))
            assert sorted(core.ctx.streams) == ["lidar", "nav"] and core.ctx.streams["nav"].tolist() == [3, 0]
            assert core.ctx.rate == core.clock.rate and core.ctx.roster is core.roster
            tab = core.entities["target"]
            m = tab.add(model="person")
            tab["tgt_pos"][m.row] = (1.0, 2.0, 3.0)
            assert core.roster.target_no_of(m.id) == m.target_no and core.roster.target_id_of(m.target_no) == m.id
            assert core.roster.target_no_of("nope") is None and core.roster.target_id_of(99) is None
            blocks = tab.blocks
            assert set(blocks) == {"M08", "M18"} and "tgt_pos" in blocks["M18"] and "active" in blocks["M08"]
            feats = core._local_query("fleet/caps", {})["features"]
            assert "targets" in feats and "sandbox" in feats
        finally:
            h.close()


def test_session_env_sections_forwarded() -> None:
    got: list[tuple[dict, object]] = []

    class FakeEnv:
        def configure_session(self, cfg, t_ns=None) -> None:
            got.append((cfg, t_ns))

        def session_snapshot(self, t_ns=None) -> dict:
            return {"acoustic_bg": "rural", "env": {"preset": "calm", "overrides": {}},
                    "calendar": {"start_utc": "2026-10-05T01:00:00Z", "tz": "UTC"}}

    with R.isolated_registry() as reg:
        h = CoreHarness(n=0, reg=reg, fleet_cfg={"clock_profile": "sandbox100"}, ready=False)
        try:
            core = h.core
            cfg = {"calendar": {"start_utc": "2026-10-05T00:00:00Z", "tz": "UTC"}, "acoustic_bg": "urban",
                   "surface_class": "grass", "env": {"preset": "calm"}}
            assert core.session.apply(cfg) == []
            assert core.ctx.session_config == cfg  # 环境服务尚未构造：首次构造时读取
            core.ctx.env = FakeEnv()
            assert core.session.apply({"acoustic_bg": "rural"}) == [] and got == [({"acoustic_bg": "rural"}, None)]
            snap = core.session.export()
            assert snap["acoustic_bg"] == "rural" and snap["calendar"]["start_utc"] == "2026-10-05T01:00:00Z"
        finally:
            core.ctx.env = None
            h.close()


def test_sensor_rig_validation_rejects_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    from awr.sim.fleet.profiles import ProfileError

    class FakeRT:
        @staticmethod
        def validate_profiles(T) -> dict:
            return {"x500": [], "p600_mid360": ["sensors/eo_gx40.yaml: SN-3 spec_ref unresolved"]}

    monkeypatch.setitem(sys.modules, "awr.sim.sensors.runtime", types.SimpleNamespace(SensorRuntime=FakeRT))
    with R.isolated_registry() as reg:
        h = CoreHarness(n=0, reg=reg, ready=False)
        try:
            h.core.plugins_loaded = ["awr.sim.sensors.plugin"]
            with pytest.raises(ProfileError) as ei:
                h.core._validate_sensor_rigs()
            assert ei.value.code == 353 and ei.value.profile_id == "p600_mid360" and "SN-3" in ei.value.problems[0]
            h.core.plugins_loaded = []
            h.core._validate_sensor_rigs()  # M13 未装配：不校验
        finally:
            h.close()


def test_profile_model_dir_for_history() -> None:
    from awr.sim.fleet.profiles import ProfileTable

    T = ProfileTable()
    cur = T.get("p600_mid360")
    old = T.get("p600_mid360@1.0.0")
    assert cur.model_dir == old.model_dir and cur.model_dir.endswith("vehicles/p600")
    assert "mid360" in old.sensors_map and "mid360" in cur.sensors_map


def test_m13_wiring_with_real_plugin() -> None:
    """真实 M13 插件：沙盒实例经 configure_mounts 配置挂载（汇总记入实例配置、roster sensors[] 只列已挂载槽位），
    `gimbal` 经 M08 准入后交 M13，`fleet/vehicle_sensors` 取 M13 的 sensors_rest。"""
    from awr.contracts.reasons import Reason
    from awr.sim.sensors import plugin

    with R.isolated_registry() as reg:
        plugin.install()
        try:
            h = CoreHarness(n=0, reg=reg, fleet_cfg={"clock_profile": "sandbox100"}, ready=False)
            try:
                core = h.core
                code, rep = core.admit_add({"model_id": "p600_mid360", "pos_enu_m": [0.0, 0.0, 0.0], "label": "A"})
                assert code == 0, rep
                vid = rep["id"]
                assert core.roster.resolve(vid).sensors, "roster sensors[]"
                h.until(lambda: core.roster.resolve(vid).lifecycle == 4, 5.0)
                h.advance(0.5)
                r = h.cmd("gimbal", {"mode": "stow"}, uav=vid, cid="g-1")
                assert r["code"] != int(Reason.BACKEND_UNSUPPORTED), r  # 已路由到 M13（不是 UNKNOWN_OP）
                assert h.cmd("gimbal", {"mode": "stow"}, uav="nope-01")["code"] == int(Reason.NO_VEHICLE)
                rep = core._local_query("fleet/vehicle_sensors", {"id": vid})
                assert rep["code"] == 0 and isinstance(rep["item"]["sensors"], list) and rep["item"]["sensors"]
            finally:
                h.close()
        finally:
            plugin.uninstall()
