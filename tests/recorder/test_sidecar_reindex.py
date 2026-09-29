"""派生索引与格式（M12 §7.5；FR-037；M12-AC-037）：

- RecPrefix8、块头与 `.ovw`、`.evx` 布局尺寸与契约一致；marker 类别表与草案 `fixtures/markers.json`（前端同表）一致；
- 在线写出的 `.ovw`、`.evx` 与从 MCAP 重建的结果逐字节一致（含回滚重跑段：RERUN bin、SUPERSEDED 记录）；
- `.evx` 记录数等于 `/event` 消息数，mseq 为 0 起的写入序号；`.ovw` bin 数覆盖段长，GAP、level 计数正确。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import rechelp

from awr.contracts.layouts import REC_REC_PREFIX8, SWARM_LITE32BLOCK
from awr.recorder.formats import (
    BLOCK_HDR,
    EVX_HDR,
    EVX_REC,
    EVX_SUPERSEDED,
    OVW_BIN,
    OVW_HDR,
    OVW_TRACK,
    PREFIX,
    channel_spec,
    prefix,
    split_prefix,
)
from awr.recorder.reindex import reindex
from awr.recorder.sidecar import MARKER_RULES, MarkerClass, marker_class

pytestmark = pytest.mark.ext
FIX = Path(__file__).resolve().parent / "fixtures"


def test_layouts_and_channels() -> None:
    assert PREFIX.size == REC_REC_PREFIX8.itemsize == 8
    assert BLOCK_HDR.size == SWARM_LITE32BLOCK.itemsize == 16
    assert (OVW_HDR.size, OVW_BIN.size, OVW_TRACK.size, EVX_HDR.size, EVX_REC.size) == (128, 32, 16, 32, 16)
    ep, rf, dt, payload = split_prefix(prefix(70000, 1, -5) + b"xyz")
    assert (ep, rf, dt, bytes(payload)) == (70000 & 0xFFFF, 1, -5, b"xyz")
    assert channel_spec("/uav/p600-01/state").schema == "awr.DroneState64.v1"
    assert channel_spec("/uav/p600-01/sensor/cam/pose").backfill == "latest"
    assert channel_spec("/swarm/uav/safety_block").backfill == "keyframe+delta"
    assert channel_spec("/agent/a1/status").schema == "awr.agent.v1"
    with pytest.raises(KeyError):
        channel_spec("/nope")


def test_marker_rules_match_draft_contract() -> None:
    draft = json.loads((FIX / "markers.json").read_text(encoding="utf-8"))
    assert draft == MARKER_RULES
    assert marker_class("sim.restarted", 2, {}) == MarkerClass.WARNING
    assert marker_class("safety.geofence", 3, {}) == MarkerClass.CRITICAL
    assert marker_class("uav.state", 0, {"to": "LANDED"}) == MarkerClass.LIFECYCLE
    assert marker_class("cmd.accepted", 0, {"op": "orbit"}) == MarkerClass.ROUTE
    assert marker_class("scenario.mark", 0, {}) == MarkerClass.SYSTEM
    assert marker_class("mission.item_reached", 0, {}) == MarkerClass.NONE


def _counts(run_dir: Path) -> tuple[int, int]:
    msgs = rechelp.messages(run_dir / "rec-000.mcap")
    return sum(1 for t, _l, _d in msgs if t == "/event"), len(msgs)


def test_online_equals_reindex(std_run: dict, tmp_path: Path) -> None:
    d = Path(std_run["dir"])
    ovw, evx = (d / "rec-000.ovw").read_bytes(), (d / "rec-000.evx").read_bytes()
    o2, e2 = reindex(d / "rec-000.mcap", tmp_path)
    assert o2.read_bytes() == ovw
    assert e2.read_bytes() == evx
    n_ev, _ = _counts(d)
    magic, _ver, flags, rb, _mv, n, _t0, seg, _ = EVX_HDR.unpack_from(evx, 0)
    assert magic == b"AWRX" and flags & 1 and rb == 16 and seg == 0
    assert n == n_ev == (len(evx) - 32) // 16
    mseqs = [EVX_REC.unpack_from(evx, 32 + 16 * i)[1] for i in range(n)]
    assert mseqs == list(range(n))
    levels = [EVX_REC.unpack_from(evx, 32 + 16 * i)[2] for i in range(n)]
    assert levels.count(3) == 4  # 7、14、21、28 s
    h = OVW_HDR.unpack_from(ovw, 0)
    assert h[0] == b"AWRO" and h[2] & 1 and h[4] == 1_000_000_000
    n_bins, bin_bytes, n_tracks = h[5], h[6], h[7]
    assert n_tracks == 2 and bin_bytes == 32 + 16 * 2
    assert n_bins in (30, 31) and len(ovw) == 128 + n_bins * bin_bytes
    b0 = OVW_BIN.unpack_from(ovw, 128)
    assert b0[0] == 60 and b0[1] == 60 and b0[9] & 1 == 0  # 60 架在场、空中，非 GAP
    assert sum(OVW_BIN.unpack_from(ovw, 128 + k * bin_bytes)[4] for k in range(n_bins)) == levels.count(0)


def test_reindex_after_rollback(tmp_path: Path) -> None:
    from awr.recorder.synth import synthesize

    d = tmp_path / rechelp.RUN2
    synthesize(d, n=10, sim_s=20, lineage=(12_000_000_000, 8_000_000_000), events_per_s=4)
    ovw, evx = (d / "rec-000.ovw").read_bytes(), (d / "rec-000.evx").read_bytes()
    out = tmp_path / "re"
    out.mkdir()
    o2, e2 = reindex(d / "rec-000.mcap", out)
    assert o2.read_bytes() == ovw and e2.read_bytes() == evx
    n = (len(evx) - 32) // 16
    sup = [EVX_REC.unpack_from(evx, 32 + 16 * i) for i in range(n)]
    superseded = [r for r in sup if r[3] & EVX_SUPERSEDED]
    assert superseded and all(r[0] >= 8_000_000_000 for r in superseded)
    assert all(not r[3] & EVX_SUPERSEDED for r in sup if r[0] < 8_000_000_000)
    h = OVW_HDR.unpack_from(ovw, 0)
    rerun = [k for k in range(h[5]) if OVW_BIN.unpack_from(ovw, 128 + k * h[6])[9] & 4]
    assert rerun and min(rerun) == 8 and max(rerun) <= 12
    shutil.rmtree(d)
