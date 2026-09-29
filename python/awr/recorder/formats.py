"""录制格式常量与小结构（M12 §6.6、§7.5；AWR-16 §13.3–§13.7；契约 `rec/mcap_channels.json`、`awr.rec.RecPrefix8.v1`、
`awr.SwarmLite32Block.v1`）。

- 每条 awr-raw / awr-msgpack 消息以 RecPrefix8 开头：`epoch u16`（生产者纪元低 16 位）、`rflags u8`（bit0 KEYFRAME、
  bit1 DELTA、bit2 ZSTD、bit3 RESET）、`prefix_version u8 = 1`、`dt_us i32`；其后是原样负载；
- 整群块消息 = SwarmLite32Block 头（n u32、roster_version u32、reserved u64）+ n 行 Lite32（按 agent_no 升序）；
- channel 清单、schema 名与编码取自契约 `rec/mcap_channels.json`（经 `awr.contracts._paths` 定位），通配 topic 以模板匹配；
- `.ovw`、`.evx` 的布局由本文件定义（M12 §7.5.1、§7.5.2），前端解析在 `apps/web/src/engine/time/sidecars.ts`。
"""

from __future__ import annotations

import functools
import json
import re
import struct
from dataclasses import dataclass

from awr.contracts._paths import schema_path
from awr.contracts.layouts import (
    DRONE_STATE64,
    REC_REC_PREFIX8,
    REC_REC_PREFIX8_CONST,
    SENSOR_POSE48,
    SWARM_LITE32,
    SWARM_LITE32BLOCK,
)

__all__ = [
    "BLOCK_HDR",
    "EVX_HDR",
    "EVX_REC",
    "FULL_ROW",
    "LITE_ROW",
    "OVW_BIN",
    "OVW_HDR",
    "OVW_TRACK",
    "POSE_ROW",
    "PREFIX",
    "RFLAG_DELTA",
    "RFLAG_KEYFRAME",
    "RFLAG_RESET",
    "T_BLOCK",
    "T_CLOCK",
    "T_ENV",
    "T_EVENT",
    "T_EXT",
    "T_ROSTER",
    "T_SAFETY",
    "ChannelSpec",
    "channel_spec",
    "full_topic",
    "is_full_topic",
    "is_pose_topic",
    "pose_topic",
    "prefix",
    "split_prefix",
]

PREFIX = struct.Struct("<HBBi")
assert PREFIX.size == REC_REC_PREFIX8.itemsize == 8
PREFIX_VERSION = int(REC_REC_PREFIX8_CONST["prefix_version"])
RFLAG_KEYFRAME, RFLAG_DELTA, RFLAG_ZSTD, RFLAG_RESET = 1, 2, 4, 8

BLOCK_HDR = struct.Struct("<IIQ")
assert BLOCK_HDR.size == SWARM_LITE32BLOCK.itemsize == 16
LITE_ROW = SWARM_LITE32.itemsize  # 32
FULL_ROW = DRONE_STATE64.itemsize  # 64
POSE_ROW = SENSOR_POSE48.itemsize  # 48

# 固定 topic（16 §13.3）
T_BLOCK = "/swarm/uav/state_block"
T_EXT = "/swarm/uav/state_ext_block"
T_SAFETY = "/swarm/uav/safety_block"
T_ENV = "/env/state"
T_EVENT = "/event"
T_CLOCK = "/sim/clock"
T_ROSTER = "/sim/roster"
T_AGENT_TASKS = "/agent/tasks"


def full_topic(vehicle_id: str) -> str:
    return f"/uav/{vehicle_id}/state"


def pose_topic(vehicle_id: str, sensor: str) -> str:
    return f"/uav/{vehicle_id}/sensor/{sensor}/pose"


def mission_topic(mid: str) -> str:
    return f"/mission/{mid}/status"


def agent_topic(aid: str) -> str:
    return f"/agent/{aid}/status"


_FULL_RE = re.compile(r"^/uav/[^/]+/state$")
_POSE_RE = re.compile(r"^/uav/[^/]+/sensor/[^/]+/pose$")


def is_full_topic(topic: str) -> bool:
    return bool(_FULL_RE.match(topic))


def is_pose_topic(topic: str) -> bool:
    return bool(_POSE_RE.match(topic))


def prefix(epoch: int, rflags: int = 0, dt_us: int = 0) -> bytes:
    return PREFIX.pack(epoch & 0xFFFF, rflags & 0xFF, PREFIX_VERSION, dt_us)


def split_prefix(data: bytes | memoryview) -> tuple[int, int, int, memoryview]:
    """(epoch, rflags, dt_us, payload)；payload 为去掉 RecPrefix8 的零拷贝视图。"""
    ep, rf, _ver, dt = PREFIX.unpack_from(data, 0)
    return ep, rf, dt, memoryview(data)[PREFIX.size:]


@dataclass(frozen=True)
class ChannelSpec:
    topic: str  # 契约中的 topic 模板
    schema: str
    schema_encoding: str
    message_encoding: str
    backfill: str  # latest | keyframe+delta | none
    record_policy: str


@functools.cache
def _registry() -> tuple[list[tuple[re.Pattern[str], ChannelSpec]], dict[str, dict]]:
    d = json.loads(schema_path("rec/mcap_channels.json").read_text(encoding="utf-8"))
    schemas = {s["name"]: s for s in d["schemas"]}
    out: list[tuple[re.Pattern[str], ChannelSpec]] = []
    for c in d["channels"]:
        t = c["topic"]
        pat = "^" + re.escape(t).replace(r"\*\*", ".+").replace(r"\{id\}", "[^/]+").replace(r"\{name\}", "[^/]+") \
            .replace(r"\{mid\}", "[^/]+") + "$"
        s = schemas[c["schema"]]
        out.append((re.compile(pat), ChannelSpec(t, c["schema"], s["encoding"], c["message_encoding"], c["backfill"],
                                                 c["record_policy"])))
    return out, d["writer"]


def channel_spec(topic: str) -> ChannelSpec:
    for pat, spec in _registry()[0]:
        if pat.match(topic):
            return spec
    raise KeyError(f"topic 不在 rec/mcap_channels.json 中：{topic}")


def writer_profile() -> dict:
    return dict(_registry()[1])


# ---------------------------------------------------------------- 派生索引布局（M12 §7.5.1、§7.5.2）
OVW_MAGIC = b"AWRO"
EVX_MAGIC = b"AWRX"
OVW_VERSION = 1
EVX_VERSION = 1
MAX_TRACKS = 16
# .ovw 头 128 B：magic、version、flags、t_start_ns、bin_ns、n_bins、bin_bytes、n_tracks、track_agent_no[16]、roster_version、
# segment、保留 56 B
OVW_HDR = struct.Struct("<4sHHqqIHH16HII56x")
assert OVW_HDR.size == 128
# bin 的机群部分 32 B：n_present、n_airborne、n_alert、n_failsafe、ev_by_level[4]、min_battery_pct、bin_flags、保留 u16、
# max_speed_mps、mean_z_m、max_z_m
OVW_BIN = struct.Struct("<HHHH4HBBHfff")
assert OVW_BIN.size == 32
OVW_TRACK = struct.Struct("<3fBBBx")
assert OVW_TRACK.size == 16
OVW_BIN_GAP, OVW_BIN_DECIMATED, OVW_BIN_RERUN = 1, 2, 4
# .evx 头 32 B：magic、version、flags、record_bytes、markers_version、n_records、t_start_ns、segment、保留 u32
EVX_HDR = struct.Struct("<4sHHHHIqII")
assert EVX_HDR.size == 32
EVX_REC = struct.Struct("<qIBBH")
assert EVX_REC.size == 16
EVX_SUPERSEDED = 0x10
NO_AGENT = 0xFFFF
