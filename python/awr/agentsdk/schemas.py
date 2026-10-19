"""`packages/contracts/bench/*.v1` 校验器（共用 `awr.sim.schemas.contracts_registry` 的一次扫描）与热成像观测 `ThermalObs` 的投影。

载荷中的二进制字段（`x-awr-bin`，如消息 `payload`、`ThermalObs.temp_ck`）在进程内为 bytes；schema 对这些字段不设类型约束，校验照常
通过，字节层面的约束（如 `temp_ck` 的解压长度）由 `thermal_payload_ok` 检查。

`ThermalObs`（M26-FR-124、§7.3；API-FR-132；D3-AC-61）：由 M25 热成像帧记录（`drone4d.frame.v1`，`kind = thermal`）的 `agent_view`
逐字段投影，`temp_ck` 取载荷块 `blocks[agent_view.temp_ck.ref]` 的同一 bytes 对象，不复制、不重编码，因此 Agent 收到的字节、帧记录
`agent_view.temp_ck.sha256` 与 `raw.mcap` 热成像通道（`awr.bench.recorder.channels.thermal_message`）存档的字节逐字节相同。真值分区
（实例、语义、`objects[].dT_app_k`）与 `internal` 不进入投影。字段映射：

- `mount_nominal`：帧记录的 `{xyz_m, rpy_deg}`（FLU，ZYX）→ `{t_bs_m, q_bs_xyzw}`，`q` 为 `Rz(yaw)·Ry(pitch)·Rx(roll)` 的四元数
  （与 `awr.sim.sensors.frames.mount_R` 同一约定，w ≥ 0）；
- `gimbal_reading`：`{az_deg, el_deg}`（带编码器噪声的读数）→ `{roll_deg: 0, pitch_deg: el_deg, yaw_deg: az_deg}`，与 L2 相机相同；
- `codec`：`radiometric_u16`（u16 小端 + zstd，0.01 K/LSB）；`agc_jpeg` 只用于开发运行与预览，排名运行请求时 682 拒绝
  （`check_thermal_codec`，M25-FR-189、M26-AC-032）。

字段顺序与 schema 声明顺序一致（msgpack map 按插入顺序写出，黄金向量 `packages/contracts/bench/golden/thermal` 依赖此顺序）。
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from functools import cache

__all__ = ["BENCH_ID", "THERMAL_CODECS", "ThermalCodecRejected", "bench_validator", "check_thermal_codec", "decode_thermal_obs", "mount_quat_xyzw",
           "thermal_obs", "thermal_payload_ok"]

BENCH_ID = "https://schemas.anet-drone.dev/bench/1/{name}.v1.schema.json"
THERMAL_CODECS = ("radiometric_u16", "agc_jpeg")


@cache
def bench_validator(name: str):
    """name ∈ {act, obs, agent_config, task, evaluation, board, score, run_manifest, ...}。"""
    from jsonschema import Draft202012Validator

    from awr.sim.schemas import contracts_registry

    reg = contracts_registry()
    return Draft202012Validator(reg.contents(BENCH_ID.format(name=name)), registry=reg)


class ThermalCodecRejected(ValueError):
    """682 `RUN_SPEC_INVALID`：排名运行请求 `agc_jpeg`，或热成像编码不在 {radiometric_u16, agc_jpeg} 内；`detail.why = thermal_codec`。"""

    code = 682

    def __init__(self, sensor_id: str, codec: str, ranked: bool) -> None:
        super().__init__(f"682 RUN_SPEC_INVALID thermal_codec {sensor_id}={codec!r} (ranked={ranked})")
        self.detail = {"why": "thermal_codec", "sensor_id": sensor_id, "codec": codec, "allowed": ["radiometric_u16"] if ranked else list(THERMAL_CODECS)}


def check_thermal_codec(codec: str, *, ranked: bool, sensor_id: str = "therm0") -> str:
    """排名运行只允许 `radiometric_u16`；开发运行另允许 `agc_jpeg`（预览）。通过时返回 codec。"""
    if codec not in THERMAL_CODECS or (ranked and codec != "radiometric_u16"):
        raise ThermalCodecRejected(sensor_id, codec, ranked)
    return codec


def mount_quat_xyzw(rpy_deg) -> list[float]:
    """ZYX 挂载角（度）→ 传感器到机体旋转 `Rz(yaw)·Ry(pitch)·Rx(roll)` 的四元数 [x, y, z, w]（w ≥ 0）。"""
    r, p, y = (math.radians(float(v)) / 2 for v in rpy_deg)
    cr, sr, cp, sp, cy, sy = math.cos(r), math.sin(r), math.cos(p), math.sin(p), math.cos(y), math.sin(y)
    q = [sr * cp * cy - cr * sp * sy, cr * sp * cy + sr * cp * sy, cr * cp * sy - sr * sp * cy, cr * cp * cy + sr * sp * sy]
    return [-v for v in q] if q[3] < 0 else q


def thermal_obs(rec: dict, blocks: Mapping[str, bytes], *, ranked: bool = False, verify: bool = True) -> dict:
    """热成像帧记录（frame.v1，`kind = thermal`）与其载荷块 → `bench/obs.v1` `ThermalObs`。

    `temp_ck` 为 `blocks[agent_view.temp_ck.ref]` 本身（同一对象）；`verify` 时核对其 sha256 与帧记录一致、zstd 帧声明长度为 2·W·H。"""
    if rec.get("kind") != "thermal":
        raise ValueError(f"not a thermal frame record: kind={rec.get('kind')!r}")
    av = rec["agent_view"]
    tc = av["temp_ck"]
    sid = str(av["sensor_id"])
    codec = check_thermal_codec(str(tc["codec"]), ranked=ranked, sensor_id=sid)
    data = blocks[tc["ref"]]
    if not isinstance(data, bytes):
        raise TypeError(f"temp_ck block {tc['ref']!r} must be bytes, got {type(data).__name__}")
    mt, g = av["mount_nominal"], av["gimbal_reading"]
    obs = {"sensor_id": sid, "seq": int(av["seq"]), "t_mid_ns": int(av["t_mid_ns"]), "width": int(av["width"]), "height": int(av["height"]),
           "K": [float(v) for v in av["K"]], "dist": [float(v) for v in av["dist"]],
           "mount_nominal": {"t_bs_m": [float(v) for v in mt["xyz_m"]], "q_bs_xyzw": mount_quat_xyzw(mt["rpy_deg"])},
           "gimbal_reading": {"roll_deg": 0.0, "pitch_deg": float(g["el_deg"]), "yaw_deg": float(g["az_deg"])},
           "temp_ck": data, "codec": codec}
    if verify:
        if hashlib.sha256(data).hexdigest() != tc["sha256"]:
            raise ValueError(f"temp_ck sha256 mismatch for {rec.get('frame_id')}")
        if not thermal_payload_ok(obs):
            raise ValueError(f"temp_ck payload of {rec.get('frame_id')} does not decode to {obs['width']}x{obs['height']} u16")
    return obs


def thermal_payload_ok(obs: dict) -> bool:
    """`radiometric_u16` 的 `temp_ck` 为单个 zstd 帧，帧头声明且实际解压长度均为 2·W·H；`agc_jpeg` 以 JPEG SOI 开头、EOI 结尾。"""
    data = obs.get("temp_ck")
    if not isinstance(data, bytes | bytearray):
        return False
    if obs.get("codec") == "agc_jpeg":
        return data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9"
    import zstandard

    n = 2 * int(obs["width"]) * int(obs["height"])
    try:
        if zstandard.frame_content_size(bytes(data)) != n:
            return False
        return len(zstandard.ZstdDecompressor().decompress(bytes(data), max_output_size=n)) == n
    except zstandard.ZstdError:
        return False


def decode_thermal_obs(obs: dict):
    """`radiometric_u16` 的 ThermalObs → (H, W) uint16 表观温度（0.01 K/LSB；开尔文 = 值 / 100）。"""
    import numpy as np
    import zstandard

    if obs.get("codec") != "radiometric_u16":
        raise ValueError(f"codec {obs.get('codec')!r} is not radiometric")
    w, h = int(obs["width"]), int(obs["height"])
    raw = zstandard.ZstdDecompressor().decompress(bytes(obs["temp_ck"]), max_output_size=2 * w * h)
    if len(raw) != 2 * w * h:
        raise ValueError(f"temp_ck payload has {len(raw)} bytes, expected {2 * w * h}")
    return np.frombuffer(raw, "<u2").reshape(h, w).astype(np.uint16)
