"""控制协议 `drone4d.gz.v1`（M24 §7.4；native/gz/proto/drone4d_gz.proto）的宿主侧编解码。

消息类在运行期由字段表构造（descriptor_pb2 → DescriptorPool → message_factory），不依赖 protoc 生成代码，因此与 gz 镜像内
protoc 3.21 生成的 C++ 代码和本环境的 protobuf 运行时版本无关；线格式只由字段号与类型决定。`FIELDS` 与 .proto 的一致性由
tests/d3/phys/test_gz_protocol.py 逐字段核对。
"""

from __future__ import annotations

from typing import Any

import numpy as np
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

__all__ = ["FIELDS", "PROTOCOL", "STATE_N", "decode", "encode", "msg", "samples_array"]

PROTOCOL = 1
STATE_N = 16
PACKAGE = "drone4d.gz.v1"

# 消息 → [(字段, 编号, 类型, repeated)]；类型为标量名或消息名
FIELDS: dict[str, list[tuple[str, int, str, bool]]] = {
    "Hello": [("run_id", 1, "string", False), ("attempt", 2, "uint32", False), ("uav_models", 3, "string", True),
              ("step_ns", 4, "uint32", False), ("tick_steps", 5, "uint32", False), ("manifest_sha256", 6, "string", False)],
    "HelloAck": [("gz_sim_version", 1, "string", False), ("plugin_build", 2, "string", False), ("n_uav", 3, "uint32", False),
                 ("initial_state", 4, "bytes", False), ("t_ns", 5, "sfixed64", False)],
    "VelSp": [("uav_index", 1, "uint32", False), ("apply_step", 2, "uint32", False), ("vx", 3, "double", False),
              ("vy", 4, "double", False), ("vz", 5, "double", False), ("yaw_rate", 6, "double", False), ("enabled", 7, "bool", False)],
    "WindSp": [("uav_index", 1, "uint32", False), ("apply_step", 2, "uint32", False), ("wx", 3, "double", False),
               ("wy", 4, "double", False), ("wz", 5, "double", False)],
    "Control": [("seq", 1, "uint64", False), ("tick", 2, "uint64", False), ("grant_steps", 3, "uint32", False),
                ("setpoints", 4, "VelSp", True), ("wind", 5, "WindSp", True), ("mean_wind_x", 6, "double", False),
                ("mean_wind_y", 7, "double", False), ("mean_wind_z", 8, "double", False), ("actor_frames", 9, "bytes", False)],
    "Contact": [("uav_index", 1, "uint32", False), ("step", 2, "uint32", False), ("kind", 3, "uint32", False),
                ("px", 4, "double", False), ("py", 5, "double", False), ("pz", 6, "double", False), ("nx", 7, "double", False),
                ("ny", 8, "double", False), ("nz", 9, "double", False), ("speed", 10, "double", False)],
    "StepDone": [("seq", 1, "uint64", False), ("tick", 2, "uint64", False), ("n_steps", 3, "uint32", False),
                 ("n_uav", 4, "uint32", False), ("t_ns", 5, "sfixed64", True), ("samples", 6, "bytes", False),
                 ("contacts", 7, "Contact", True), ("gz_rtf", 8, "float", False)],
    "Reset": [("seed_check", 1, "uint64", False)],
    "Shutdown": [],
    "Bye": [("iterations", 1, "uint64", False)],
    "Error": [("code", 1, "uint32", False), ("detail", 2, "string", False)],
}
BODY = ("hello", "hello_ack", "control", "step_done", "reset", "shutdown", "bye", "error")
_BODY_TYPE = {"hello": "Hello", "hello_ack": "HelloAck", "control": "Control", "step_done": "StepDone", "reset": "Reset",
              "shutdown": "Shutdown", "bye": "Bye", "error": "Error"}

_T = descriptor_pb2.FieldDescriptorProto
_SCALAR = {"string": _T.TYPE_STRING, "bytes": _T.TYPE_BYTES, "bool": _T.TYPE_BOOL, "uint32": _T.TYPE_UINT32,
           "uint64": _T.TYPE_UINT64, "double": _T.TYPE_DOUBLE, "float": _T.TYPE_FLOAT, "sfixed64": _T.TYPE_SFIXED64}


def _build() -> dict[str, Any]:
    fdp = descriptor_pb2.FileDescriptorProto(name="drone4d_gz.proto", package=PACKAGE, syntax="proto3")
    for name, fields in FIELDS.items():
        m = fdp.message_type.add(name=name)
        for fname, num, typ, rep in fields:
            f = m.field.add(name=fname, number=num, label=_T.LABEL_REPEATED if rep else _T.LABEL_OPTIONAL)
            if typ in _SCALAR:
                f.type = _SCALAR[typ]
            else:
                f.type = _T.TYPE_MESSAGE
                f.type_name = f".{PACKAGE}.{typ}"
    env = fdp.message_type.add(name="Envelope")
    env.field.add(name="protocol", number=1, type=_T.TYPE_UINT32, label=_T.LABEL_OPTIONAL)
    env.oneof_decl.add(name="body")
    for i, b in enumerate(BODY):
        env.field.add(name=b, number=i + 2, type=_T.TYPE_MESSAGE, type_name=f".{PACKAGE}.{_BODY_TYPE[b]}", label=_T.LABEL_OPTIONAL,
                      oneof_index=0)
    pool = descriptor_pool.DescriptorPool()
    pool.Add(fdp)
    return {n: message_factory.GetMessageClass(pool.FindMessageTypeByName(f"{PACKAGE}.{n}")) for n in [*FIELDS, "Envelope"]}


_C = _build()


def msg(name: str, **kw):
    """新建消息（例如 `msg("VelSp", uav_index=0, vx=1.0)`）。"""
    return _C[name](**kw)


def encode(body: str, m) -> bytes:
    """body ∈ BODY；m 为对应类型的消息。"""
    env = _C["Envelope"](protocol=PROTOCOL)
    getattr(env, body).CopyFrom(m)
    return env.SerializeToString()


def decode(b: bytes) -> tuple[str, Any]:
    """→ (body 名, 消息)；协议号不符时 ValueError。"""
    env = _C["Envelope"]()
    env.ParseFromString(b)
    if env.protocol != PROTOCOL:
        raise ValueError(f"drone4d.gz protocol {env.protocol} != {PROTOCOL}")
    which = env.WhichOneof("body")
    if which is None:
        raise ValueError("empty envelope")
    return which, getattr(env, which)


def samples_array(sd, n_uav: int) -> np.ndarray:
    """StepDone.samples → f64 (n_steps, n_uav, 16)。"""
    a = np.frombuffer(sd.samples, "<f8")
    return a.reshape(int(sd.n_steps), n_uav, STATE_N)
