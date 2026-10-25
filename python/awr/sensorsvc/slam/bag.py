"""SLAM 导出 bag（§5.4.6 SLAM 导出合同；r1 §4.3、§2.10；D3-AC-13、D3-AC-42 的 bag 导出）：ROS1 bag v2.0 纯 Python 写出与读回，无 ROS 依赖。

- 话题：`/livox/lidar`（`livox_ros_driver/CustomMsg`，10 Hz）、`/livox/imu`（`sensor_msgs/Imu`，加速度单位 g，200 Hz）、
  `/left_camera/image/compressed`（`sensor_msgs/CompressedImage`，JPEG）。三路同一时基（仿真时刻 ns），记录时刻等于消息头时刻。
- CustomMsg：每帧槽位全部输出（MID-360 为 20,000 点），无回波槽位写 `(0, 0, 0)`、反射率 0、tag 0，`line` 仍为槽位光束号；
  `offset_time(i) = ⌊i · 10⁸ / N⌋` ns（N 为每帧槽位数，单调递增），`timebase` 与 `header.stamp` 均为帧首时刻。丢弃无回波点会触发
  FAST-LIVO2 `preprocess.cpp:183` 的级联时间修正（r1 §2.10-1），因此 `full_slots` 只接受与槽位网格逐点相符的输入。
- md5：按 genmsg 规则由消息定义计算（去注释、常量在前、非内置类型以其 md5 代替），`livox_ros_driver/CustomMsg` 与
  `livox_ros_driver2/CustomMsg` 均为 `e4d6829bdfe657cb6c21a746c86b21a6`（ROS1 握手只比 md5，r1 §4.6）；连接记录的
  `message_definition` 为 gendeps 全文（主定义 + 依赖，以 80 个 `=` 分隔），供无类型库的读取方动态反序列化。
- bag 结构（rosbag 2.0）：版本行、定长 4096 B 文件头记录（关闭时回写 `index_pos`）、chunk（连接记录在首次使用时写入、消息记录）
  及其后的逐连接索引记录，文件尾为全部连接记录与 chunk 信息记录。chunk 压缩取 none（缺省，回放最快）或 bz2；读取方另拒绝 lz4。
- 读回校验：`Bag1Reader` 逐 chunk 核对大小、索引条目（时刻与偏移）、chunk 信息的时间范围与计数；`verify_slam_bag` 在其上做合同检查
  （md5、点数、offset_time 网格、零点占位、timebase、帧周期、IMU 周期、相机时刻落在 LiDAR 覆盖内），违反时抛 642
  `SENSOR_CONTRACT_VIOLATION`。IMU 静止或悬停窗口、噪声参数、与 float64 参考实现之差等其余 D3-AC-13 条款由 `tests/d3/slam/test_contract.py` 承担。
"""

from __future__ import annotations

import bz2
import hashlib
import struct
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from awr.sensorsvc.errors import SensorSvcError

__all__ = [
    "BAG_MAGIC", "CUSTOM_POINT", "IMAGE_TOPIC", "IMU_MD5", "IMU_TOPIC", "LIDAR_TOPIC", "LIVOX_CUSTOM_MSG_MD5", "LIVOX_TYPES",
    "Bag1Reader", "Bag1Writer", "BagFormatError", "BagMessage", "Connection", "SlamBagReport", "SlamBagWriter",
    "check_custom_msg", "full_slots", "msg_full_text", "msg_md5", "offset_time_ns", "pack_compressed_image", "pack_custom_msg",
    "pack_imu", "unpack_compressed_image", "unpack_custom_msg", "unpack_imu", "verify_slam_bag",
]

NS = 1_000_000_000
G = 9.80665                                     # 与 awr.sensorsvc.imu.G 一致（Livox IMU 加速度单位 g）
BAG_MAGIC = b"#ROSBAG V2.0\n"
BAG_HEADER_LEN = 4096                           # 文件头记录定长（含长度前缀与空格填充），关闭时原位回写
CHUNK_THRESHOLD = 768 * 1024                    # rosbag 缺省 chunk 阈值
OP_MSG, OP_FILE_HEADER, OP_INDEX, OP_CHUNK, OP_CHUNK_INFO, OP_CONN = 0x02, 0x03, 0x04, 0x05, 0x06, 0x07

LIDAR_TOPIC = "/livox/lidar"
IMU_TOPIC = "/livox/imu"
IMAGE_TOPIC = "/left_camera/image/compressed"
LIDAR_FRAME_ID = "livox_frame"
CAMERA_FRAME_ID = "camera"
LIVOX_TYPES = ("livox_ros_driver/CustomMsg", "livox_ros_driver2/CustomMsg")
LIVOX_CUSTOM_MSG_MD5 = "e4d6829bdfe657cb6c21a746c86b21a6"
IMU_MD5 = "6a62c6daae103f4ff57a132d6f95cec2"
COMPRESSED_IMAGE_MD5 = "8f7a12909da2c9d3332d540a0977563f"

# livox_ros_driver 逐点结构（ROS1 线格式 19 B，无对齐填充）
CUSTOM_POINT = np.dtype([("offset_time", "<u4"), ("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("reflectivity", "u1"), ("tag", "u1"),
                         ("line", "u1")])
assert CUSTOM_POINT.itemsize == 19

# ---------------------------------------------------------------- 消息定义（ROS noetic 与 livox_ros_driver 原文，注释从简）
_HEADER = """# Standard metadata for higher-level stamped data types.
# sequence ID: consecutively increasing ID
uint32 seq
# Two-integer timestamp: stamp.sec seconds since epoch, stamp.nsec nanoseconds since stamp.sec
time stamp
# Frame this data is associated with
string frame_id
"""
_QUATERNION = """# This represents an orientation in free space in quaternion form.

float64 x
float64 y
float64 z
float64 w
"""
_VECTOR3 = """# This represents a vector in free space.

float64 x
float64 y
float64 z
"""
_IMU = """# This is a message to hold data from an IMU (Inertial Measurement Unit)
#
# Accelerations should be in m/s^2 (not in g's), and rotational velocity should be in rad/sec
# (Livox MID-360 built-in IMU publishes linear_acceleration in g; FAST-LIVO2 expects g for /livox/imu)
#
# If the covariance of the measurement is known, it should be filled in (if all you know is the
# variance of each measurement, e.g. from the datasheet, just put those along the diagonal)
# A covariance matrix of all zeros will be interpreted as "covariance unknown", and to use the
# data a covariance will have to be assumed or gotten from some other source
#
# If you have no estimate for one of the data elements (e.g. your IMU doesn't produce an orientation
# estimate), please set element 0 of the associated covariance matrix to -1

Header header

geometry_msgs/Quaternion orientation
float64[9] orientation_covariance # Row major about x, y, z axes

geometry_msgs/Vector3 angular_velocity
float64[9] angular_velocity_covariance # Row major about x, y, z axes

geometry_msgs/Vector3 linear_acceleration
float64[9] linear_acceleration_covariance # Row major x, y z
"""
_COMPRESSED_IMAGE = """# This message contains a compressed image

Header header        # Header timestamp should be acquisition time of image
                     # Header frame_id should be optical frame of camera

string format        # Specifies the format of the data
                     #   Acceptable values: jpeg, png

uint8[] data         # Compressed image buffer
"""
_LIVOX_MSG = """# Livox publish pointcloud msg format.

Header header             # ROS standard message header
uint64 timebase           # The time of first point
uint32 point_num          # Total number of pointclouds
uint8  lidar_id           # Lidar device id number
uint8[3]  rsvd            # Reserved use
CustomPoint[] points      # Pointcloud data
"""
_LIVOX_POINT = """# Livox costom pointcloud format.

uint32 offset_time      # offset time relative to the base time
float32 x               # X axis, unit:m
float32 y               # Y axis, unit:m
float32 z               # Z axis, unit:m
uint8 reflectivity      # reflectivity, 0~255
uint8 tag               # livox tag
uint8 line              # laser number in lidar
"""
MSG_DEFS: dict[str, str] = {
    "std_msgs/Header": _HEADER,
    "geometry_msgs/Quaternion": _QUATERNION,
    "geometry_msgs/Vector3": _VECTOR3,
    "sensor_msgs/Imu": _IMU,
    "sensor_msgs/CompressedImage": _COMPRESSED_IMAGE,
    **{f"{pkg}/CustomMsg": _LIVOX_MSG for pkg in ("livox_ros_driver", "livox_ros_driver2")},
    **{f"{pkg}/CustomPoint": _LIVOX_POINT for pkg in ("livox_ros_driver", "livox_ros_driver2")},
}
_BUILTIN = frozenset({"bool", "byte", "char", "int8", "uint8", "int16", "uint16", "int32", "uint32", "int64", "uint64", "float32",
                      "float64", "string", "time", "duration"})


def _fields_of(msgtype: str) -> tuple[list[str], list[tuple[str, str, str]]]:
    """解析定义：返回 (常量行, [(字段类型原文, 裸类型全名或内置名, 字段名)])。"""
    if msgtype not in MSG_DEFS:
        raise KeyError(f"unknown ROS1 message type {msgtype!r}")
    pkg = msgtype.split("/")[0]
    consts, fields = [], []
    for raw in MSG_DEFS[msgtype].splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        typ, rest = line.split(None, 1)
        if "=" in rest:                                         # 常量：type NAME=value
            name, val = rest.split("=", 1)
            consts.append(f"{typ} {name.strip()}={val.strip()}")
            continue
        bare = typ.split("[", 1)[0]
        if bare not in _BUILTIN:
            bare = "std_msgs/Header" if bare == "Header" else (bare if "/" in bare else f"{pkg}/{bare}")
        fields.append((typ, bare, rest.strip()))
    return consts, fields


def msg_md5(msgtype: str) -> str:
    """genmsg `compute_md5_text`：常量行在前，内置类型保留原文（含数组后缀），消息类型替换为其 md5（数组后缀去掉）。"""
    consts, fields = _fields_of(msgtype)
    lines = list(consts)
    for typ, bare, name in fields:
        lines.append(f"{typ} {name}" if bare in _BUILTIN else f"{msg_md5(bare)} {name}")
    return hashlib.md5("\n".join(lines).encode()).hexdigest()


def _deps(msgtype: str, out: list[str]) -> list[str]:
    for _, bare, _ in _fields_of(msgtype)[1]:
        if bare not in _BUILTIN and bare not in out:
            out.append(bare)
            _deps(bare, out)
    return out


def msg_full_text(msgtype: str) -> str:
    """gendeps `--cat` 全文：主定义，随后每个依赖为 80 个 `=`、`MSG: <type>` 与其定义。"""
    parts = [MSG_DEFS[msgtype], *("=" * 80 + "\n" + f"MSG: {d}\n" + MSG_DEFS[d] for d in _deps(msgtype, []))]
    return "\n".join(p.rstrip("\n") for p in parts) + "\n"


# ---------------------------------------------------------------- 消息序列化（ROS1 线格式，小端）
def _stamp(t_ns: int) -> tuple[int, int]:
    t = int(t_ns)
    if t < 0 or t // NS > 0xFFFFFFFF:
        raise ValueError(f"time {t} ns outside ROS1 time range")
    return t // NS, t % NS


def _pack_header(seq: int, t_ns: int, frame_id: str) -> bytes:
    sec, nsec = _stamp(t_ns)
    fb = frame_id.encode()
    return struct.pack("<IIII", int(seq) & 0xFFFFFFFF, sec, nsec, len(fb)) + fb


def _unpack_header(b: memoryview, p: int) -> tuple[dict, int]:
    seq, sec, nsec, n = struct.unpack_from("<IIII", b, p)
    p += 16
    return {"seq": seq, "stamp_ns": sec * NS + nsec, "frame_id": bytes(b[p:p + n]).decode()}, p + n


def offset_time_ns(n: int) -> np.ndarray:
    """槽位时刻偏移 `offset_time(i) = ⌊i · 10⁸ / N⌋` ns（u32，严格单调，N ≤ 10⁸）；MID-360 N = 20,000 时为 i · 5000。"""
    n = int(n)
    if not 0 < n <= 100_000_000:
        raise ValueError("slot count must be in (0, 1e8]")
    return (np.arange(n, dtype=np.int64) * 100_000_000 // n).astype(np.uint32)


def full_slots(points: np.ndarray, slots: int, *, lines: int = 4) -> np.ndarray:
    """有效点（`LIDAR_RECORD` 或任一含 x/y/z/offset_time/reflectivity/tag/line 字段的结构数组，槽位升序）→ 全槽位 CustomPoint。

    槽位号由 offset_time 反解 i = ⌈ot · N / 10⁸⌉，并要求 offset_time 与该槽位的网格值逐点相等、槽位严格递增；
    无回波槽位为 (0, 0, 0)、反射率 0、tag 0，`line = i mod lines`（与 MeshLidar 的光束号 `i & 3` 一致）。不符时抛 642。
    """
    grid = offset_time_ns(slots)
    out = np.zeros(int(slots), CUSTOM_POINT)
    out["offset_time"] = grid
    out["line"] = np.arange(int(slots)) % int(lines)
    if len(points) == 0:
        return out
    ot = np.asarray(points["offset_time"], np.int64)
    idx = (ot * int(slots) + 100_000_000 - 1) // 100_000_000
    if (idx < 0).any() or (idx >= slots).any() or (np.diff(idx) <= 0).any() or (grid[np.clip(idx, 0, slots - 1)] != ot).any():
        raise SensorSvcError(642, "lidar points do not lie on the slot grid offset_time(i) = floor(i*1e8/N) in ascending order")
    xyz = np.stack([points["x"], points["y"], points["z"]], 1).astype(np.float32)
    if not np.isfinite(xyz).all():
        raise SensorSvcError(642, "non-finite lidar point coordinates")
    out["x"][idx], out["y"][idx], out["z"][idx] = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    out["reflectivity"][idx] = points["reflectivity"]
    out["tag"][idx] = points["tag"]
    out["line"][idx] = points["line"]
    return out


def pack_custom_msg(seq: int, t_start_ns: int, points: np.ndarray, *, frame_id: str = LIDAR_FRAME_ID, lidar_id: int = 0) -> bytes:
    """CustomMsg：`header.stamp` 与 `timebase` 均为帧首时刻，`point_num` 等于点数组长度。"""
    pts = np.ascontiguousarray(points, CUSTOM_POINT)
    return (_pack_header(seq, t_start_ns, frame_id) + struct.pack("<QIB3BI", int(t_start_ns), len(pts), int(lidar_id), 0, 0, 0, len(pts))
            + pts.tobytes())


def unpack_custom_msg(b: bytes | memoryview) -> dict:
    mv = memoryview(b)
    h, p = _unpack_header(mv, 0)
    timebase, point_num, lidar_id, r0, r1, r2, n = struct.unpack_from("<QIB3BI", mv, p)
    p += struct.calcsize("<QIB3BI")
    if p + n * CUSTOM_POINT.itemsize != len(mv):
        raise BagFormatError("CustomMsg length does not match its point array")
    pts = np.frombuffer(mv, CUSTOM_POINT, count=n, offset=p)
    return {"header": h, "timebase": timebase, "point_num": point_num, "lidar_id": lidar_id, "rsvd": (r0, r1, r2), "points": pts}


def pack_imu(seq: int, t_ns: int, gyro_rps, acc_g, *, frame_id: str = LIDAR_FRAME_ID) -> bytes:
    """sensor_msgs/Imu：角速度 rad/s、线加速度单位 g（Livox 约定）；姿态未知（单位四元数，orientation_covariance[0] = −1）。"""
    w = [float(v) for v in gyro_rps]
    a = [float(v) for v in acc_g]
    z9 = (0.0,) * 9
    return (_pack_header(seq, t_ns, frame_id) + struct.pack("<4d", 0.0, 0.0, 0.0, 1.0) + struct.pack("<9d", -1.0, *z9[1:])
            + struct.pack("<3d", *w) + struct.pack("<9d", *z9) + struct.pack("<3d", *a) + struct.pack("<9d", *z9))


def unpack_imu(b: bytes | memoryview) -> dict:
    mv = memoryview(b)
    h, p = _unpack_header(mv, 0)
    v = struct.unpack_from("<4d9d3d9d3d9d", mv, p)
    if p + struct.calcsize("<4d9d3d9d3d9d") != len(mv):
        raise BagFormatError("Imu message length mismatch")
    return {"header": h, "orientation": v[0:4], "orientation_covariance": v[4:13], "angular_velocity": v[13:16],
            "angular_velocity_covariance": v[16:25], "linear_acceleration": v[25:28], "linear_acceleration_covariance": v[28:37]}


def pack_compressed_image(seq: int, t_ns: int, data: bytes, *, fmt: str = "jpeg", frame_id: str = CAMERA_FRAME_ID) -> bytes:
    fb = fmt.encode()
    return _pack_header(seq, t_ns, frame_id) + struct.pack("<I", len(fb)) + fb + struct.pack("<I", len(data)) + bytes(data)


def unpack_compressed_image(b: bytes | memoryview) -> dict:
    mv = memoryview(b)
    h, p = _unpack_header(mv, 0)
    (n,) = struct.unpack_from("<I", mv, p)
    fmt = bytes(mv[p + 4:p + 4 + n]).decode()
    p += 4 + n
    (m,) = struct.unpack_from("<I", mv, p)
    if p + 4 + m != len(mv):
        raise BagFormatError("CompressedImage length mismatch")
    return {"header": h, "format": fmt, "data": bytes(mv[p + 4:p + 4 + m])}


# ---------------------------------------------------------------- ROS1 bag v2.0
class BagFormatError(ValueError):
    """bag 结构损坏或与索引不一致（读回校验）。"""


@dataclass(frozen=True)
class Connection:
    id: int
    topic: str
    msgtype: str
    md5sum: str
    message_definition: str
    callerid: str | None = None
    latching: bool = False


@dataclass(frozen=True)
class BagMessage:
    conn: Connection
    t_ns: int
    data: bytes


@dataclass(frozen=True)
class ChunkInfo:
    pos: int
    start_ns: int
    end_ns: int
    counts: dict[int, int]


def _u8(v: int) -> bytes:
    return struct.pack("<B", v)


def _u32(v: int) -> bytes:
    return struct.pack("<I", v)


def _u64(v: int) -> bytes:
    return struct.pack("<Q", v)


def _time(t_ns: int) -> bytes:
    return struct.pack("<II", *_stamp(t_ns))


def _fields(d: dict[str, bytes]) -> bytes:
    return b"".join(_u32(len(k) + 1 + len(v)) + k.encode() + b"=" + v for k, v in d.items())


def _record(header: dict[str, bytes], data: bytes) -> bytes:
    h = _fields(header)
    return _u32(len(h)) + h + _u32(len(data)) + data


def _parse_fields(b: memoryview | bytes) -> dict[str, bytes]:
    out, p, mv = {}, 0, memoryview(b)
    while p < len(mv):
        if p + 4 > len(mv):
            raise BagFormatError("truncated header field")
        (n,) = struct.unpack_from("<I", mv, p)
        f = bytes(mv[p + 4:p + 4 + n])
        if len(f) != n or b"=" not in f:
            raise BagFormatError("malformed header field")
        k, v = f.split(b"=", 1)
        out[k.decode()] = v
        p += 4 + n
    return out


def _read_record(mv: memoryview, p: int) -> tuple[dict[str, bytes], memoryview, int]:
    if p + 4 > len(mv):
        raise BagFormatError("truncated record")
    (hl,) = struct.unpack_from("<I", mv, p)
    h = _parse_fields(mv[p + 4:p + 4 + hl])
    q = p + 4 + hl
    if q + 4 > len(mv):
        raise BagFormatError("truncated record")
    (dl,) = struct.unpack_from("<I", mv, q)
    if q + 4 + dl > len(mv):
        raise BagFormatError("truncated record data")
    return h, mv[q + 4:q + 4 + dl], q + 4 + dl


def _read_record_file(f) -> tuple[dict[str, bytes], bytes]:
    hb = f.read(4)
    if len(hb) != 4:
        raise BagFormatError("truncated record")
    h = f.read(struct.unpack("<I", hb)[0])
    db = f.read(4)
    if len(db) != 4:
        raise BagFormatError("truncated record")
    (dl,) = struct.unpack("<I", db)
    d = f.read(dl)
    if len(d) != dl:
        raise BagFormatError("truncated record data")
    return _parse_fields(h), d


def _op(h: dict[str, bytes]) -> int:
    if "op" not in h or len(h["op"]) != 1:
        raise BagFormatError("record without op")
    return h["op"][0]


def _get_u32(h: dict[str, bytes], k: str) -> int:
    return struct.unpack("<I", h[k])[0]


def _get_time(b: bytes) -> int:
    sec, nsec = struct.unpack("<II", b)
    return sec * NS + nsec


class Bag1Writer:
    """ROS1 bag v2.0 写出：`add_connection` 登记话题，`write(conn_id, t_ns, raw)` 追加已序列化的消息，`close` 写索引并回写文件头。"""

    def __init__(self, path: Path | str, *, compression: str = "none", chunk_threshold: int = CHUNK_THRESHOLD) -> None:
        if compression not in ("none", "bz2"):
            raise ValueError(f"unsupported chunk compression {compression!r}")
        self.path = Path(path)
        self.compression = compression
        self.chunk_threshold = int(chunk_threshold)
        self.connections: list[Connection] = []
        self._f = open(self.path, "w+b")  # noqa: SIM115 — 生命周期由 close 管理
        self._f.write(BAG_MAGIC)
        self._f.write(self._file_header(0, 0, 0))
        self._chunk = bytearray()
        self._idx: dict[int, list[tuple[int, int]]] = {}
        self._t_lo = self._t_hi = 0
        self._in_chunk: set[int] = set()
        self._emitted: set[int] = set()
        self._infos: list[ChunkInfo] = []
        self._closed = False

    @staticmethod
    def _file_header(index_pos: int, conn_count: int, chunk_count: int) -> bytes:
        h = _fields({"op": _u8(OP_FILE_HEADER), "index_pos": _u64(index_pos), "conn_count": _u32(conn_count),
                     "chunk_count": _u32(chunk_count)})
        pad = BAG_HEADER_LEN - 8 - len(h)
        return _u32(len(h)) + h + _u32(pad) + b" " * pad

    @staticmethod
    def _conn_record(c: Connection) -> bytes:
        d = {"topic": c.topic.encode(), "type": c.msgtype.encode(), "md5sum": c.md5sum.encode(),
             "message_definition": c.message_definition.encode()}
        if c.callerid is not None:
            d["callerid"] = c.callerid.encode()
        if c.latching:
            d["latching"] = b"1"
        return _record({"op": _u8(OP_CONN), "conn": _u32(c.id), "topic": c.topic.encode()}, _fields(d))

    def add_connection(self, topic: str, msgtype: str, *, md5sum: str | None = None, message_definition: str | None = None,
                       callerid: str | None = None, latching: bool = False) -> int:
        """登记连接；已登记类型的 md5 与定义全文自动计算，未登记类型须显式给出。返回连接号。"""
        c = Connection(len(self.connections), topic, msgtype, md5sum or msg_md5(msgtype),
                       message_definition if message_definition is not None else msg_full_text(msgtype), callerid, latching)
        self.connections.append(c)
        return c.id

    def write(self, conn: int, t_ns: int, data: bytes) -> None:
        if self._closed:
            raise ValueError("bag is closed")
        if not 0 <= conn < len(self.connections):
            raise ValueError(f"unknown connection {conn}")
        t = int(t_ns)
        if conn not in self._emitted:                           # 连接记录在首次使用时写入当前 chunk（rosbag 行为）
            self._chunk += self._conn_record(self.connections[conn])
            self._emitted.add(conn)
        off = len(self._chunk)
        self._chunk += _record({"op": _u8(OP_MSG), "conn": _u32(conn), "time": _time(t)}, bytes(data))
        self._idx.setdefault(conn, []).append((t, off))
        if not self._in_chunk:
            self._t_lo = self._t_hi = t
        self._t_lo, self._t_hi = min(self._t_lo, t), max(self._t_hi, t)
        self._in_chunk.add(conn)
        if len(self._chunk) >= self.chunk_threshold:
            self._flush()

    def _flush(self) -> None:
        if not self._chunk:
            return
        pos = self._f.tell()
        raw = bytes(self._chunk)
        data = bz2.compress(raw) if self.compression == "bz2" else raw
        self._f.write(_record({"op": _u8(OP_CHUNK), "compression": self.compression.encode(), "size": _u32(len(raw))}, data))
        counts = {}
        for c in sorted(self._idx):
            ent = sorted(self._idx[c], key=lambda e: e[0])      # 稳定排序：同一时刻保持写入顺序
            body = b"".join(_time(t) + _u32(o) for t, o in ent)
            self._f.write(_record({"op": _u8(OP_INDEX), "ver": _u32(1), "conn": _u32(c), "count": _u32(len(ent))}, body))
            counts[c] = len(ent)
        self._infos.append(ChunkInfo(pos, self._t_lo, self._t_hi, counts))
        self._chunk = bytearray()
        self._idx = {}
        self._in_chunk = set()

    def close(self) -> None:
        if self._closed:
            return
        self._flush()
        index_pos = self._f.tell()
        for c in self.connections:
            self._f.write(self._conn_record(c))
        for ci in self._infos:
            body = b"".join(_u32(c) + _u32(n) for c, n in ci.counts.items())
            self._f.write(_record({"op": _u8(OP_CHUNK_INFO), "ver": _u32(1), "chunk_pos": _u64(ci.pos), "start_time": _time(ci.start_ns),
                                   "end_time": _time(ci.end_ns), "count": _u32(len(ci.counts))}, body))
        self._f.seek(len(BAG_MAGIC))
        self._f.write(self._file_header(index_pos, len(self.connections), len(self._infos)))
        self._f.close()
        self._closed = True

    def __enter__(self) -> Bag1Writer:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class Bag1Reader:
    """ROS1 bag v2.0 读取与结构校验：文件头、索引段（连接与 chunk 信息）、逐 chunk 的大小、索引条目与计数。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._f = open(self.path, "rb")  # noqa: SIM115 — 生命周期由 close 管理
        try:
            self._open()
        except Exception:
            self._f.close()
            raise

    def _open(self) -> None:
        f = self._f
        if f.read(len(BAG_MAGIC)) != BAG_MAGIC:
            raise BagFormatError("not a ROS1 bag v2.0 file")
        h, _ = _read_record_file(f)
        if _op(h) != OP_FILE_HEADER:
            raise BagFormatError("missing bag file header record")
        self.index_pos = struct.unpack("<Q", h["index_pos"])[0]
        n_conn, n_chunk = _get_u32(h, "conn_count"), _get_u32(h, "chunk_count")
        if self.index_pos == 0:
            raise BagFormatError("bag was not closed (index_pos = 0)")
        f.seek(self.index_pos)
        self.connections: dict[int, Connection] = {}
        for _ in range(n_conn):
            rh, rd = _read_record_file(f)
            if _op(rh) != OP_CONN:
                raise BagFormatError("expected connection record in index section")
            c = self._conn(rh, rd)
            self.connections[c.id] = c
        self.chunks: list[ChunkInfo] = []
        for _ in range(n_chunk):
            rh, rd = _read_record_file(f)
            if _op(rh) != OP_CHUNK_INFO or _get_u32(rh, "ver") != 1:
                raise BagFormatError("expected chunk info record (ver 1)")
            n = _get_u32(rh, "count")
            if len(rd) != 8 * n:
                raise BagFormatError("chunk info data length mismatch")
            counts = {struct.unpack_from("<I", rd, 8 * k)[0]: struct.unpack_from("<I", rd, 8 * k + 4)[0] for k in range(n)}
            self.chunks.append(ChunkInfo(struct.unpack("<Q", rh["chunk_pos"])[0], _get_time(rh["start_time"]), _get_time(rh["end_time"]),
                                         counts))
        if f.read(1):
            raise BagFormatError("trailing bytes after index section")

    @staticmethod
    def _conn(rh: dict[str, bytes], rd: bytes | memoryview) -> Connection:
        d = _parse_fields(rd)
        try:
            return Connection(_get_u32(rh, "conn"), d["topic"].decode(), d["type"].decode(), d["md5sum"].decode(),
                              d["message_definition"].decode(), d["callerid"].decode() if "callerid" in d else None,
                              d.get("latching", b"0") == b"1")
        except KeyError as e:
            raise BagFormatError(f"connection record lacks field {e}") from None

    def topics(self) -> dict[str, Connection]:
        return {c.topic: c for c in self.connections.values()}

    def message_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for ci in self.chunks:
            for c, n in ci.counts.items():
                out[self.connections[c].topic] = out.get(self.connections[c].topic, 0) + n
        return out

    def messages(self, topics: Iterable[str] | None = None) -> Iterator[BagMessage]:
        """按文件顺序逐 chunk 读出消息；每个 chunk 在产出前与其索引记录、chunk 信息逐条核对。"""
        want = set(topics) if topics is not None else None
        for ci in sorted(self.chunks, key=lambda c: c.pos):
            for m in self._chunk(ci):
                if want is None or m.conn.topic in want:
                    yield m

    def _chunk(self, ci: ChunkInfo) -> list[BagMessage]:
        f = self._f
        f.seek(ci.pos)
        rh, rd = _read_record_file(f)
        if _op(rh) != OP_CHUNK:
            raise BagFormatError(f"no chunk record at {ci.pos}")
        comp, size = rh["compression"].decode(), _get_u32(rh, "size")
        if comp == "none":
            raw = rd
        elif comp == "bz2":
            raw = bz2.decompress(rd)
        else:
            raise BagFormatError(f"unsupported chunk compression {comp!r}")
        if len(raw) != size:
            raise BagFormatError("chunk size mismatch")
        mv = memoryview(raw)
        p, msgs, seen = 0, [], {}
        while p < len(mv):
            off = p
            h, d, p = _read_record(mv, p)
            op = _op(h)
            if op == OP_CONN:
                c = self._conn(h, d)
                if self.connections.get(c.id) != c:
                    raise BagFormatError(f"connection {c.id} in chunk differs from index section")
            elif op == OP_MSG:
                cid, t = _get_u32(h, "conn"), _get_time(h["time"])
                if cid not in self.connections:
                    raise BagFormatError(f"message on unknown connection {cid}")
                msgs.append(BagMessage(self.connections[cid], t, bytes(d)))
                seen.setdefault(cid, []).append((t, off))
            else:
                raise BagFormatError(f"unexpected record op {op:#x} in chunk")
        for _ in range(len(ci.counts)):
            ih, idat = _read_record_file(f)
            if _op(ih) != OP_INDEX or _get_u32(ih, "ver") != 1:
                raise BagFormatError("expected index data record (ver 1) after chunk")
            cid, n = _get_u32(ih, "conn"), _get_u32(ih, "count")
            if len(idat) != 12 * n:
                raise BagFormatError("index data length mismatch")
            ent = [(_get_time(idat[12 * k:12 * k + 8]), struct.unpack_from("<I", idat, 12 * k + 8)[0]) for k in range(n)]
            if sorted(seen.get(cid, []), key=lambda e: e[0]) != ent or ci.counts.get(cid) != n:
                raise BagFormatError(f"index of connection {cid} does not match chunk content")
        if set(seen) != set(ci.counts):
            raise BagFormatError("chunk info connection set does not match chunk content")
        if msgs and (min(m.t_ns for m in msgs) != ci.start_ns or max(m.t_ns for m in msgs) != ci.end_ns):
            raise BagFormatError("chunk info time range does not match chunk content")
        return msgs

    def close(self) -> None:
        self._f.close()

    def __enter__(self) -> Bag1Reader:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ---------------------------------------------------------------- SLAM bag（三话题）
class SlamBagWriter:
    """§5.4.6 三话题写出：`lidar_frame` 补全槽位后写 CustomMsg，`imu`/`imu_samples` 换算为 g，`image` 写压缩图像。各话题 seq 自增。"""

    def __init__(self, path: Path | str, *, slots: int = 20_000, lines: int = 4, lidar_type: str = LIVOX_TYPES[0], camera: bool = True,
                 compression: str = "none", chunk_threshold: int = CHUNK_THRESHOLD) -> None:
        if lidar_type not in LIVOX_TYPES:
            raise ValueError(f"lidar_type must be one of {LIVOX_TYPES}")
        self.slots, self.lines = int(slots), int(lines)
        self.bag = Bag1Writer(path, compression=compression, chunk_threshold=chunk_threshold)
        self._c_lidar = self.bag.add_connection(LIDAR_TOPIC, lidar_type)
        self._c_imu = self.bag.add_connection(IMU_TOPIC, "sensor_msgs/Imu")
        self._c_img = self.bag.add_connection(IMAGE_TOPIC, "sensor_msgs/CompressedImage") if camera else None
        self._seq = {LIDAR_TOPIC: 0, IMU_TOPIC: 0, IMAGE_TOPIC: 0}

    def _next(self, topic: str) -> int:
        s = self._seq[topic]
        self._seq[topic] = s + 1
        return s

    def lidar_frame(self, t_start_ns: int, points: np.ndarray) -> int:
        """`points` 为帧内有效点（槽位升序，offset_time 落在槽位网格上）；返回有回波槽位数。"""
        full = full_slots(points, self.slots, lines=self.lines)
        self.bag.write(self._c_lidar, t_start_ns, pack_custom_msg(self._next(LIDAR_TOPIC), t_start_ns, full))
        return len(points)

    def imu(self, t_ns: int, gyro_rps, acc_mps2) -> None:
        acc_g = [float(a) / G for a in acc_mps2]
        self.bag.write(self._c_imu, t_ns, pack_imu(self._next(IMU_TOPIC), t_ns, gyro_rps, acc_g))

    def imu_samples(self, samples) -> int:
        """写出 `awr.sensorsvc.imu.ImuSamples`（t_ns、gyro_rps、acc_mps2）；返回样本数。"""
        for t, w, a in zip(np.asarray(samples.t_ns, np.int64), np.asarray(samples.gyro_rps), np.asarray(samples.acc_mps2), strict=True):
            self.imu(int(t), w, a)
        return len(samples.t_ns)

    def image(self, t_ns: int, data: bytes, *, fmt: str = "jpeg") -> None:
        if self._c_img is None:
            raise ValueError("camera topic disabled")
        self.bag.write(self._c_img, t_ns, pack_compressed_image(self._next(IMAGE_TOPIC), t_ns, data, fmt=fmt))

    def close(self) -> None:
        self.bag.close()

    def __enter__(self) -> SlamBagWriter:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def check_custom_msg(msg: dict, slots: int = 20_000, *, lines: int = 4) -> int:
    """单帧 CustomMsg 合同：点数 = point_num = N、offset_time 逐点等于 ⌊i·10⁸/N⌋（单调）、timebase = header.stamp、
    零点占位（坐标全零的槽位反射率与 tag 为 0）、坐标有限、line < lines。返回有回波槽位数；违反时抛 642。"""
    pts = msg["points"]
    if msg["point_num"] != len(pts) or len(pts) != slots:
        raise SensorSvcError(642, f"CustomMsg has {len(pts)} points (point_num {msg['point_num']}), expected all {slots} slots "
                                  "(no-return points must be kept as zero placeholders)", seq=msg["header"]["seq"])
    if not np.array_equal(pts["offset_time"], offset_time_ns(slots)):
        raise SensorSvcError(642, "offset_time is not floor(i*1e8/N) per slot", seq=msg["header"]["seq"])
    if msg["timebase"] != msg["header"]["stamp_ns"]:
        raise SensorSvcError(642, "timebase differs from header.stamp (frame start)", seq=msg["header"]["seq"])
    xyz = np.stack([pts["x"], pts["y"], pts["z"]], 1)
    if not np.isfinite(xyz).all():
        raise SensorSvcError(642, "non-finite point coordinates", seq=msg["header"]["seq"])
    zero = (xyz == 0).all(1)
    if (pts["reflectivity"][zero] != 0).any() or (pts["tag"][zero] != 0).any():
        raise SensorSvcError(642, "zero placeholder with non-zero reflectivity or tag", seq=msg["header"]["seq"])
    if (pts["line"] >= lines).any():
        raise SensorSvcError(642, f"line index >= {lines}", seq=msg["header"]["seq"])
    return int((~zero).sum())


@dataclass
class SlamBagReport:
    path: str
    md5: dict[str, str] = field(default_factory=dict)       # 话题 → md5sum
    types: dict[str, str] = field(default_factory=dict)     # 话题 → 消息类型
    lidar_frames: int = 0
    returns_mean: float = 0.0
    imu_samples: int = 0
    images: int = 0
    lidar_t0_ns: int = 0
    lidar_t1_ns: int = 0
    imu_t0_ns: int = 0
    imu_t1_ns: int = 0
    acc_max_g: float = 0.0
    gyro_max_rps: float = 0.0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def verify_slam_bag(path: Path | str, *, slots: int = 20_000, lines: int = 4, frame_period_ns: int | None = 100_000_000,
                    imu_period_ns: int | None = 5_000_000, require_imu: bool = True) -> SlamBagReport:
    """读回校验：bag 结构（Bag1Reader）、三话题类型与 md5、各话题记录时刻非降且等于消息头时刻、逐帧 `check_custom_msg`、
    LiDAR 帧周期与 IMU 周期（None 表示不检查）、相机时刻落在 LiDAR 覆盖 [首帧首, 末帧首 + 帧周期] 内。违反时抛 642。"""
    rep = SlamBagReport(str(path))
    expect = {LIDAR_TOPIC: (LIVOX_TYPES, LIVOX_CUSTOM_MSG_MD5), IMU_TOPIC: (("sensor_msgs/Imu",), IMU_MD5),
              IMAGE_TOPIC: (("sensor_msgs/CompressedImage",), COMPRESSED_IMAGE_MD5)}
    try:
        rd = Bag1Reader(path)
    except BagFormatError as e:
        raise SensorSvcError(642, f"bag structure: {e}") from None
    with rd:
        tops = rd.topics()
        for topic, (types, md5) in expect.items():
            c = tops.get(topic)
            if c is None:
                continue
            if c.msgtype not in types or c.md5sum != md5 or msg_md5(c.msgtype) != md5:
                raise SensorSvcError(642, f"{topic}: type {c.msgtype} md5 {c.md5sum}, expected {types[0]} md5 {md5}")
            rep.md5[topic], rep.types[topic] = c.md5sum, c.msgtype
        if LIDAR_TOPIC not in tops or (require_imu and IMU_TOPIC not in tops):
            raise SensorSvcError(642, f"bag lacks {LIDAR_TOPIC if LIDAR_TOPIC not in tops else IMU_TOPIC}")
        last: dict[str, int] = {}
        returns, img_t = 0, []
        try:
            for m in rd.messages(expect):
                topic = m.conn.topic
                if m.t_ns < last.get(topic, -1):
                    raise SensorSvcError(642, f"{topic}: record time goes backwards at {m.t_ns}")
                prev = last.get(topic)
                last[topic] = m.t_ns
                if topic == LIDAR_TOPIC:
                    msg = unpack_custom_msg(m.data)
                    returns += check_custom_msg(msg, slots, lines=lines)
                    if prev is None:
                        rep.lidar_t0_ns = m.t_ns
                    elif frame_period_ns is not None and m.t_ns - prev != frame_period_ns:
                        raise SensorSvcError(642, f"lidar frame period {m.t_ns - prev} ns != {frame_period_ns} ns")
                    rep.lidar_t1_ns = m.t_ns
                    rep.lidar_frames += 1
                elif topic == IMU_TOPIC:
                    msg = unpack_imu(m.data)
                    v = np.array(msg["angular_velocity"] + msg["linear_acceleration"])
                    if not np.isfinite(v).all():
                        raise SensorSvcError(642, f"non-finite IMU sample at {m.t_ns}")
                    if prev is None:
                        rep.imu_t0_ns = m.t_ns
                    elif imu_period_ns is not None and m.t_ns - prev != imu_period_ns:
                        raise SensorSvcError(642, f"imu period {m.t_ns - prev} ns != {imu_period_ns} ns")
                    rep.imu_t1_ns = m.t_ns
                    rep.imu_samples += 1
                    rep.gyro_max_rps = max(rep.gyro_max_rps, float(np.linalg.norm(v[:3])))
                    rep.acc_max_g = max(rep.acc_max_g, float(np.linalg.norm(v[3:])))
                else:
                    msg = unpack_compressed_image(m.data)
                    if msg["format"] not in ("jpeg", "png") or not msg["data"]:
                        raise SensorSvcError(642, f"compressed image format {msg['format']!r} or empty data")
                    img_t.append(m.t_ns)
                    rep.images += 1
                if msg["header"]["stamp_ns"] != m.t_ns:
                    raise SensorSvcError(642, f"{topic}: header.stamp {msg['header']['stamp_ns']} != record time {m.t_ns}")
        except BagFormatError as e:
            raise SensorSvcError(642, f"bag structure: {e}") from None
    if rep.lidar_frames == 0 or (require_imu and rep.imu_samples == 0):
        raise SensorSvcError(642, "bag has no lidar frames or no imu samples")
    hi = rep.lidar_t1_ns + (frame_period_ns or 0)
    if any(t < rep.lidar_t0_ns or t > hi for t in img_t):
        raise SensorSvcError(642, "camera stamp outside lidar coverage")
    rep.returns_mean = returns / rep.lidar_frames
    return rep


def main(argv: list[str] | None = None) -> int:
    """`python -m awr.sensorsvc.slam.bag <bag>`：读回校验并输出 JSON 报告；违反合同时输出原因并返回 1。"""
    import argparse
    import json

    ap = argparse.ArgumentParser(description="ROS1 SLAM bag 合同读回校验（§5.4.6、D3-AC-13）")
    ap.add_argument("bag")
    ap.add_argument("--slots", type=int, default=20_000)
    ap.add_argument("--no-period-check", action="store_true")
    a = ap.parse_args(argv)
    kw = {"frame_period_ns": None, "imu_period_ns": None} if a.no_period_check else {}
    try:
        rep = verify_slam_bag(a.bag, slots=a.slots, **kw)
    except SensorSvcError as e:
        print(json.dumps({"ok": False, "code": e.code, "detail": e.detail}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, **rep.as_dict()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
