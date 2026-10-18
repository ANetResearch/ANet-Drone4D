"""EGL 网格光栅化（M25 §6.4.2、§6.4.3 通道 A 的静态部分；M25-FR-030 至 033、039）。

一遍 MRT：`RGBA16F` 线性颜色、`R32F` 平面深度（片元光学帧 z，米）、`R16UI` 实例（静态场景 0）、`R8UI` 语义（几何外 255）、
`DEPTH32F` reversed-Z（`glClipControl(LOWER_LEFT, ZERO_TO_ONE)`，清除 0，比较 GREATER）。材质 unlit，纹理以 `SRGB8` 上传由硬件
解码到线性，三线性 mip 与 16× 各向异性。读回第 0 行即图像顶部。

几何之后两遍全屏三角形：天空遍以 `GEQUAL` 只覆盖深度仍为清除值 0 的像素（仰角 ≥ 0 为天空渐变，否则为远景雾色，语义 255；§6.4.3
天空）；编码遍把线性颜色按 sRGB 量化为 8 位。读回 8 位 RGB、R32F 深度、实例与语义。

卷帘（M25 §15.4，`rs_method = vertex`）：`render(..., rs_nodes=[(R, p)] × 9)` 时几何遍换用逐顶点行时刻投影的顶点着色器：
读出窗口 [t_mid − T_ro/2, t_mid + T_ro/2] 内 9 个等间隔相机位姿节点以瓦片相对的模型视图矩阵传入，顶点按 2 次不动点迭代求自身
所在行的时刻，节点间对矩阵线性插值（节点间隔 T_ro/8，旋转量很小，插值误差远小于 0.01 px）；深度、实例、语义与颜色同一遍
写出。天空遍取 t_mid 位姿。

`render(..., fx=FxParams)` 时编码遍换成效果链遍（M25 §6.5：增益、渐晕、Poisson–Gaussian 噪声、sRGB 与量化；逐像素正态以
`GL_ARB_gpu_shader_int64` 的 SplitMix64 与 `camera.effects` 参考实现同式），另加 L2-depth 遍（观测深度写入单独的 R32F）与
自动曝光测光用的 1/8 降采样遍（读回小图，直方图在 CPU 上按参考实现计算）。

moderngl 负责程序、顶点数组与纹理；它不提供的 clip control、深度浮点附件与整型附件清除经 EGL 取函数地址以 ctypes 调用。
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import math
import time
from dataclasses import dataclass

import numpy as np

from awr.sensorsvc.camera.effects import FxParams
from awr.sensorsvc.camera.model import CameraModel
from awr.sensorsvc.camera.proj import mv_tile, proj_reversed_z, world_to_opt
from awr.sensorsvc.camera.scene import AppearanceScene

__all__ = ["RENDERER_VERSION", "RS_NODES", "RasterCamera", "RenderOut", "linear_to_srgb8", "srgb8_to_linear"]

RENDERER_VERSION = "raster-gl@0.4.1"
SEM_NONE = 255
_GL_TEXTURE_2D, _GL_DEPTH32F, _GL_FRAMEBUFFER = 0x0DE1, 0x8CAC, 0x8D40
_GL_COLOR_ATTACHMENT0, _GL_DEPTH_ATTACHMENT, _GL_FB_COMPLETE = 0x8CE0, 0x8D00, 0x8CD5
_GL_COLOR, _GL_DEPTH = 0x1800, 0x1801
_GL_LOWER_LEFT, _GL_ZERO_TO_ONE = 0x8CA1, 0x935F
_GL_SRGB8 = 0x8C41

VS = """
#version 450
uniform mat4 u_mvp;
uniform mat4 u_mv;
in vec3 in_pos;
in vec2 in_uv;
out vec2 v_uv;
out float v_z;
void main() {
    vec4 p = vec4(in_pos, 1.0);
    v_uv = in_uv;
    v_z = (u_mv * p).z;
    gl_Position = u_mvp * p;
}
"""
RS_NODES = 9
VS_RS = """
#version 450
const int NC = 9;
uniform mat4 u_P;
uniform mat4 u_mvn[NC];
uniform vec2 u_fycy;
uniform float u_H;
in vec3 in_pos;
in vec2 in_uv;
out vec2 v_uv;
out float v_z;
mat4 mv_at(float s) {
    float c = clamp(s, 0.0, float(NC - 1));
    int k = min(int(floor(c)), NC - 2);
    float f = c - float(k);
    return u_mvn[k] * (1.0 - f) + u_mvn[k + 1] * f;
}
float s_of(mat4 mv, vec4 p) {
    vec4 q = mv * p;
    float y = q.z > 1e-3 ? u_fycy.x * q.y / q.z + u_fycy.y : 0.5 * u_H;
    float i = clamp(y - 0.5, 0.0, u_H - 1.0);
    return ((i - 0.5 * (u_H - 1.0)) / u_H + 0.5) * float(NC - 1);
}
void main() {
    vec4 p = vec4(in_pos, 1.0);
    float s = 0.5 * float(NC - 1);
    for (int it = 0; it < 2; ++it) s = s_of(mv_at(s), p);
    vec4 q = mv_at(s) * p;
    v_uv = in_uv;
    v_z = q.z;
    gl_Position = u_P * q;
}
"""
FS = """
#version 450
uniform sampler2D u_tex;
uniform bool u_has_tex;
uniform uint u_inst;
uniform uint u_sem;
in vec2 v_uv;
in float v_z;
layout(location = 0) out vec4 o_col;
layout(location = 1) out float o_z;
layout(location = 2) out uint o_inst;
layout(location = 3) out uint o_sem;
void main() {
    vec3 c = u_has_tex ? texture(u_tex, v_uv).rgb : vec3(0.2158);
    o_col = vec4(c, 1.0);
    o_z = v_z;
    o_inst = u_inst;
    o_sem = u_sem;
}
"""

# 演员（M25 §6.4.4）：模型视图逐演员给出；深度测试值朝相机偏置 1 cm，平面深度附件写无偏置值
ACTOR_VS = """
#version 450
uniform mat4 u_P;
uniform mat4 u_mv;
uniform mat3 u_Rw;
uniform vec3 u_sun;
in vec3 in_pos;
in vec3 in_nrm;
in vec3 in_col;
out vec3 v_col;
out float v_z;
void main() {
    vec3 n = normalize(u_Rw * in_nrm);
    v_col = in_col * (0.55 + 0.45 * max(0.0, dot(n, u_sun)));
    vec4 q = u_mv * vec4(in_pos, 1.0);
    v_z = q.z;
    gl_Position = u_P * vec4(q.z > 0.02 ? q.xyz * ((q.z - 0.01) / q.z) : q.xyz, 1.0);   // 相机后方的顶点不缩放（交给裁剪）
}
"""
ACTOR_VS_RS = """
#version 450
const int NC = 9;
uniform mat4 u_P;
uniform mat4 u_mvn[NC];
uniform vec2 u_fycy;
uniform float u_H;
uniform mat3 u_Rw;
uniform vec3 u_sun;
in vec3 in_pos;
in vec3 in_nrm;
in vec3 in_col;
out vec3 v_col;
out float v_z;
mat4 mv_at(float s) {
    float c = clamp(s, 0.0, float(NC - 1));
    int k = min(int(floor(c)), NC - 2);
    float f = c - float(k);
    return u_mvn[k] * (1.0 - f) + u_mvn[k + 1] * f;
}
float s_of(mat4 mv, vec4 p) {
    vec4 q = mv * p;
    float y = q.z > 1e-3 ? u_fycy.x * q.y / q.z + u_fycy.y : 0.5 * u_H;
    float i = clamp(y - 0.5, 0.0, u_H - 1.0);
    return ((i - 0.5 * (u_H - 1.0)) / u_H + 0.5) * float(NC - 1);
}
void main() {
    vec3 n = normalize(u_Rw * in_nrm);
    v_col = in_col * (0.55 + 0.45 * max(0.0, dot(n, u_sun)));
    vec4 p = vec4(in_pos, 1.0);
    float s = 0.5 * float(NC - 1);
    for (int it = 0; it < 2; ++it) s = s_of(mv_at(s), p);
    vec4 q = mv_at(s) * p;
    v_z = q.z;
    gl_Position = u_P * vec4(q.z > 0.02 ? q.xyz * ((q.z - 0.01) / q.z) : q.xyz, 1.0);   // 相机后方的顶点不缩放（交给裁剪）
}
"""
ACTOR_FS = """
#version 450
uniform uint u_inst;
uniform uint u_sem;
in vec3 v_col;
in float v_z;
layout(location = 0) out vec4 o_col;
layout(location = 1) out float o_z;
layout(location = 2) out uint o_inst;
layout(location = 3) out uint o_sem;
void main() {
    o_col = vec4(v_col, 1.0);
    o_z = v_z;
    o_inst = u_inst;
    o_sem = u_sem;
}
"""


FULLSCREEN_VS = """
#version 450
void main() {
    vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2) * 2.0 - 1.0;
    gl_Position = vec4(p, 0.0, 1.0);
}
"""
SKY_FS = """
#version 450
uniform mat3 u_Rwo;
uniform vec4 u_K;
uniform vec3 u_horizon;
uniform vec3 u_zenith;
uniform vec3 u_fog;
layout(location = 0) out vec4 o_col;
layout(location = 1) out float o_z;
layout(location = 2) out uint o_inst;
layout(location = 3) out uint o_sem;
void main() {
    vec3 r = vec3((gl_FragCoord.x - u_K.z) / u_K.x, (gl_FragCoord.y - u_K.w) / u_K.y, 1.0);
    float el = (u_Rwo * r).z / length(r);
    float k = sqrt(clamp(el, 0.0, 1.0));
    o_col = vec4(el >= 0.0 ? mix(u_horizon, u_zenith, k) : u_fog, 1.0);
    o_z = 0.0;
    o_inst = 0u;
    o_sem = 255u;
}
"""
ENCODE_FS = """
#version 450
uniform sampler2D u_col;
layout(location = 0) out vec4 o_rgb;
void main() {
    vec3 x = clamp(texelFetch(u_col, ivec2(gl_FragCoord.xy), 0).rgb, 0.0, 1.0);
    vec3 y = mix(1.055 * pow(x, vec3(1.0 / 2.4)) - 0.055, x * 12.92, vec3(lessThanEqual(x, vec3(0.0031308))));
    o_rgb = vec4(floor(y * 255.0 + 0.5) / 255.0, 1.0);
}
"""

_GLSL_H64 = """
#extension GL_ARB_gpu_shader_int64 : require
uint64_t sm(uint64_t z) {
    z += 0x9E3779B97F4A7C15UL;
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9UL;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBUL;
    return z ^ (z >> 31);
}
float gauss(uint64_t K, uint64_t i) {
    uint64_t k = sm(K ^ i);
    uint64_t b = sm(k ^ 0x94D049BB133111EBUL);
    float u1 = (float(uint(k >> 40)) + 0.5) * (1.0 / 16777216.0);
    float u2 = float(uint(b >> 40)) * (1.0 / 16777216.0);
    return sqrt(-2.0 * log(u1)) * cos(6.2831855 * u2);
}
"""
FX_FS = """
#version 450
""" + _GLSL_H64 + """
uniform sampler2D u_col;
uniform float u_gain;
uniform float u_vig;
uniform vec4 u_K;
uniform float u_read;
uniform float u_shot;
uniform uvec2 u_key;
uniform uint u_W;
layout(location = 0) out vec4 o_rgb;
void main() {
    ivec2 p = ivec2(gl_FragCoord.xy);
    vec3 I = texelFetch(u_col, p, 0).rgb * u_gain;
    if (u_vig > 0.0) {
        float x = (gl_FragCoord.x - u_K.z) / u_K.x;
        float y = (gl_FragCoord.y - u_K.w) / u_K.y;
        float c = 1.0 / (1.0 + (x * x + y * y));
        I *= 1.0 - u_vig * (1.0 - c * c);
    }
    if (u_read > 0.0 || u_shot > 0.0) {
        uint64_t K = packUint2x32(u_key);
        uint64_t i0 = (uint64_t(p.y) * uint64_t(u_W) + uint64_t(p.x)) * 3UL;
        vec3 x = 255.0 * clamp(I, 0.0, 1.0);
        vec3 s = sqrt(u_shot * x + u_read * u_read);
        I = (x + s * vec3(gauss(K, i0), gauss(K, i0 + 1UL), gauss(K, i0 + 2UL))) / 255.0;
    }
    vec3 c = clamp(I, 0.0, 1.0);
    vec3 y = mix(1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055, c * 12.92, vec3(lessThanEqual(c, vec3(0.0031308))));
    o_rgb = vec4(floor(y * 255.0 + 0.5) / 255.0, 1.0);
}
"""
DEPTH_FS = """
#version 450
""" + _GLSL_H64 + """
uniform sampler2D u_z;
uniform vec3 u_d;
uniform uvec2 u_key;
uniform uint u_W;
layout(location = 0) out float o_z;
void main() {
    ivec2 p = ivec2(gl_FragCoord.xy);
    float d = texelFetch(u_z, p, 0).r;
    bool ok = d >= u_d.y && d <= u_d.z;
    float o = ok ? d : 0.0;
    if (ok && u_d.x > 0.0) {
        uint64_t i = uint64_t(p.y) * uint64_t(u_W) + uint64_t(p.x);
        o = max(d + u_d.x * (0.005 * d + 0.0005 * d * d) * gauss(packUint2x32(u_key), i), u_d.y);
    }
    o_z = o;
}
"""
SUB_FS = """
#version 450
uniform sampler2D u_col;
layout(location = 0) out vec4 o_col;
void main() {
    o_col = texelFetch(u_col, ivec2(gl_FragCoord.xy) * 8, 0);
}
"""


def srgb8_to_linear(c: np.ndarray) -> np.ndarray:
    x = np.asarray(c, np.float64) / 255.0
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


def _srgb_encode(x: np.ndarray) -> np.ndarray:
    x = np.clip(np.asarray(x, np.float64), 0.0, 1.0)
    y = np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(x, 1 / 2.4) - 0.055)
    return np.floor(y * 255.0 + 0.5).astype(np.uint8)


_LUT_N = 1 << 16
_SRGB_LUT = _srgb_encode((np.arange(_LUT_N) + 0.5) / _LUT_N)


def linear_to_srgb8(x: np.ndarray) -> np.ndarray:
    """线性 [0, 1] → sRGB 8 位（65536 级查表，与逐像素公式的差 ≤ 1 DN）。"""
    i = np.clip(np.asarray(x, np.float32) * _LUT_N, 0, _LUT_N - 1).astype(np.int32)
    return _SRGB_LUT[i]


SKY_HORIZON = srgb8_to_linear((200, 214, 228))
SKY_ZENITH = srgb8_to_linear((110, 150, 200))
FOG = srgb8_to_linear((190, 196, 202))


@dataclass
class RenderOut:
    rgb8: np.ndarray          # (H, W, 3) uint8 sRGB
    depth_m: np.ndarray       # (H, W) float32 平面深度，几何外 0
    instance: np.ndarray      # (H, W) uint16
    semantic: np.ndarray      # (H, W) uint8，几何外 255
    draw_ms: float
    readback_ms: float
    batches_drawn: int
    rgb_lin: np.ndarray | None = None     # (H, W, 3) float32 线性，`render(..., linear=True)` 时读回
    depth_obs: np.ndarray | None = None   # (H, W) float32 L2-depth 观测深度，`fx.depth` 给出时
    meter_sub: np.ndarray | None = None   # (⌈H/8⌉, ⌈W/8⌉, 3) float32 线性 1/8 降采样，`fx.meter` 时
    instance_unocc: np.ndarray | None = None   # (H, W) uint16 只渲染演员的实例（`actors` 给出时）
    actors_drawn: int = 0


@dataclass(frozen=True)
class ActorFrame:
    """一帧的演员位姿（只含可见演员）：idx 为磁带演员序号（实例号 idx + 1），pos (n, 3) world ENU，yaw (n,) rad。"""
    idx: np.ndarray
    pos: np.ndarray
    yaw: np.ndarray


class _GL:
    """经 eglGetProcAddress 取得的少量 GL 4.5 入口。"""

    def __init__(self) -> None:
        lib = ctypes.CDLL(ctypes.util.find_library("EGL") or "libEGL.so.1")
        gpa = lib.eglGetProcAddress
        gpa.restype = ctypes.c_void_p
        gpa.argtypes = [ctypes.c_char_p]
        u, i, p = ctypes.c_uint, ctypes.c_int, ctypes.c_void_p

        def fn(name: str, *args):
            addr = gpa(name.encode())
            if not addr:
                raise RuntimeError(f"GL entry point {name} not available")
            return ctypes.CFUNCTYPE(u if name == "glCheckFramebufferStatus" else None, *args)(addr)

        self.ClipControl = fn("glClipControl", u, u)
        self.GenTextures = fn("glGenTextures", i, p)
        self.DeleteTextures = fn("glDeleteTextures", i, p)
        self.BindTexture = fn("glBindTexture", u, u)
        self.TexStorage2D = fn("glTexStorage2D", u, i, u, i, i)
        self.GenFramebuffers = fn("glGenFramebuffers", i, p)
        self.DeleteFramebuffers = fn("glDeleteFramebuffers", i, p)
        self.BindFramebuffer = fn("glBindFramebuffer", u, u)
        self.FramebufferTexture2D = fn("glFramebufferTexture2D", u, u, u, u, i)
        self.DrawBuffers = fn("glDrawBuffers", i, p)
        self.CheckFramebufferStatus = fn("glCheckFramebufferStatus", u)
        self.ClearBufferfv = fn("glClearBufferfv", u, i, p)
        self.ClearBufferuiv = fn("glClearBufferuiv", u, i, p)


class _Mrt:
    """MRT 帧缓冲：颜色 f16×4、平面深度 f32、实例 u16、语义 u8，加 DEPTH32F 深度附件（reversed-Z）。"""

    def __init__(self, ctx, gl: _GL, w: int, h: int) -> None:
        self.col = ctx.texture((w, h), 4, dtype="f2")
        self.z = ctx.texture((w, h), 1, dtype="f4")
        self.inst = ctx.texture((w, h), 1, dtype="u2")
        self.sem = ctx.texture((w, h), 1, dtype="u1")
        for t in (self.col, self.z, self.inst, self.sem):
            t.filter = (0x2600, 0x2600)                                     # NEAREST
        d = ctypes.c_uint(0)
        gl.GenTextures(1, ctypes.byref(d))
        gl.BindTexture(_GL_TEXTURE_2D, d.value)
        gl.TexStorage2D(_GL_TEXTURE_2D, 1, _GL_DEPTH32F, w, h)
        self.depth_glo = d.value
        f = ctypes.c_uint(0)
        gl.GenFramebuffers(1, ctypes.byref(f))
        gl.BindFramebuffer(_GL_FRAMEBUFFER, f.value)
        for k, t in enumerate((self.col, self.z, self.inst, self.sem)):
            gl.FramebufferTexture2D(_GL_FRAMEBUFFER, _GL_COLOR_ATTACHMENT0 + k, _GL_TEXTURE_2D, t.glo, 0)
        gl.FramebufferTexture2D(_GL_FRAMEBUFFER, _GL_DEPTH_ATTACHMENT, _GL_TEXTURE_2D, self.depth_glo, 0)
        bufs = (ctypes.c_uint * 4)(*[_GL_COLOR_ATTACHMENT0 + k for k in range(4)])
        gl.DrawBuffers(4, bufs)
        st = gl.CheckFramebufferStatus(_GL_FRAMEBUFFER)
        if st != _GL_FB_COMPLETE:
            raise RuntimeError(f"framebuffer incomplete: 0x{st:04X}")
        self.fbo_glo = f.value
        self.fbo = ctx.detect_framebuffer(f.value)

    def clear(self, gl: _GL) -> None:
        gl.BindFramebuffer(_GL_FRAMEBUFFER, self.fbo_glo)
        zero_f = (ctypes.c_float * 4)(0.0, 0.0, 0.0, 0.0)
        gl.ClearBufferfv(_GL_COLOR, 0, zero_f)
        gl.ClearBufferfv(_GL_COLOR, 1, zero_f)
        gl.ClearBufferuiv(_GL_COLOR, 2, (ctypes.c_uint * 4)(0, 0, 0, 0))
        gl.ClearBufferuiv(_GL_COLOR, 3, (ctypes.c_uint * 4)(SEM_NONE, 0, 0, 0))
        gl.ClearBufferfv(_GL_DEPTH, 0, zero_f)

    def release(self, gl: _GL) -> None:
        gl.DeleteFramebuffers(1, ctypes.byref(ctypes.c_uint(self.fbo_glo)))
        gl.DeleteTextures(1, ctypes.byref(ctypes.c_uint(self.depth_glo)))


class _Target:
    def __init__(self, ctx, gl: _GL, w: int, h: int) -> None:
        self.w, self.h = w, h
        m = self.mrt = _Mrt(ctx, gl, w, h)
        self.col, self.z, self.inst, self.sem = m.col, m.z, m.inst, m.sem
        self.depth_glo, self.fbo_glo, self.fbo = m.depth_glo, m.fbo_glo, m.fbo
        self.unocc: _Mrt | None = None                                     # 只渲染演员的通道，首次使用时创建
        self.rgb = ctx.texture((w, h), 4, dtype="f1")
        self.enc = ctx.framebuffer(color_attachments=[self.rgb])
        self.zobs = ctx.texture((w, h), 1, dtype="f4")
        self.zfb = ctx.framebuffer(color_attachments=[self.zobs])
        self.sw, self.sh = -(-w // 8), -(-h // 8)
        self.sub = ctx.texture((self.sw, self.sh), 4, dtype="f2")
        self.subfb = ctx.framebuffer(color_attachments=[self.sub])


class RasterCamera:
    """持有 EGL 上下文与常驻世界外观；`render` 线程不安全（每 GPU 一个 worker 进程，M25 §6.14.1）。"""

    def __init__(self, scene: AppearanceScene, *, ctx=None) -> None:
        import moderngl
        from PIL import Image

        self._mgl = moderngl
        self.ctx = ctx or moderngl.create_standalone_context(backend="egl", require=450)
        self.gl = _GL()
        self.info = {k: self.ctx.info.get(k) for k in ("GL_RENDERER", "GL_VERSION", "GL_VENDOR")}
        self.prog = self.ctx.program(vertex_shader=VS, fragment_shader=FS)
        self.prog_rs = self.ctx.program(vertex_shader=VS_RS, fragment_shader=FS)
        self.sky = self.ctx.program(vertex_shader=FULLSCREEN_VS, fragment_shader=SKY_FS)
        self.enc = self.ctx.program(vertex_shader=FULLSCREEN_VS, fragment_shader=ENCODE_FS)
        self.fxp = self.ctx.program(vertex_shader=FULLSCREEN_VS, fragment_shader=FX_FS)
        self.dep = self.ctx.program(vertex_shader=FULLSCREEN_VS, fragment_shader=DEPTH_FS)
        self.subp = self.ctx.program(vertex_shader=FULLSCREEN_VS, fragment_shader=SUB_FS)
        self.actp = self.ctx.program(vertex_shader=ACTOR_VS, fragment_shader=ACTOR_FS)
        self.actp_rs = self.ctx.program(vertex_shader=ACTOR_VS_RS, fragment_shader=ACTOR_FS)
        self.actor_vaos: list[tuple[object, object, int]] = []
        self.actor_sem: list[int] = []
        self.fxp["u_col"].value = 1
        self.subp["u_col"].value = 1
        self.dep["u_z"].value = 2
        for k, v in (("u_horizon", SKY_HORIZON), ("u_zenith", SKY_ZENITH), ("u_fog", FOG)):
            self.sky[k].value = tuple(float(x) for x in v)
        self.enc["u_col"].value = 1
        self.sky_vao = self.ctx.vertex_array(self.sky, [])
        self.enc_vao = self.ctx.vertex_array(self.enc, [])
        self.fx_vao = self.ctx.vertex_array(self.fxp, [])
        self.dep_vao = self.ctx.vertex_array(self.dep, [])
        self.sub_vao = self.ctx.vertex_array(self.subp, [])
        t0 = time.perf_counter()
        Image.MAX_IMAGE_PIXELS = None
        self.textures = []
        for _mime, data in scene.textures:
            import io

            with Image.open(io.BytesIO(data)) as im:
                rgb = np.asarray(im.convert("RGB"))
            t = self.ctx.texture((rgb.shape[1], rgb.shape[0]), 3, np.ascontiguousarray(rgb).tobytes(), internal_format=_GL_SRGB8)
            t.build_mipmaps()
            t.filter = (moderngl.LINEAR_MIPMAP_LINEAR, moderngl.LINEAR)
            t.anisotropy = 16.0
            self.textures.append(t)
        self.texture_s = time.perf_counter() - t0
        t1 = time.perf_counter()
        self.draws = []
        corners = []
        for b in sorted(scene.batches, key=lambda b: (b.tile, -1 if b.texture is None else b.texture)):
            vbo = self.ctx.buffer(np.hstack([b.pos, b.uv]).astype("<f4").tobytes())
            ibo = self.ctx.buffer(b.idx.astype("<u4").tobytes())
            vao = self.ctx.vertex_array(self.prog, [(vbo, "3f 2f", "in_pos", "in_uv")], ibo)
            vao_rs = self.ctx.vertex_array(self.prog_rs, [(vbo, "3f 2f", "in_pos", "in_uv")], ibo)
            lo = b.origin + b.pos.min(0)
            hi = b.origin + b.pos.max(0)
            corners.append([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
            self.draws.append((b, vao, vao_rs))
        self.corners = np.asarray(corners, np.float64).reshape(-1, 8, 3)
        self.ctx.finish()
        self.upload_s = time.perf_counter() - t1
        self.gl.ClipControl(_GL_LOWER_LEFT, _GL_ZERO_TO_ONE)
        self._targets: dict[tuple[int, int], _Target] = {}
        self.scene = scene
        self.set_sun(None)

    def set_sun(self, az_el_deg: tuple[float, float] | None) -> None:
        from awr.sensorsvc.camera.actors import sun_dir

        L = tuple(float(v) for v in sun_dir(az_el_deg))
        for prog in (self.actp, self.actp_rs):
            prog["u_sun"].value = L

    def set_actors(self, actors: list[dict]) -> None:
        """磁带演员表（按 idx）→ 每名演员一个代理网格 VAO（全局快门与卷帘各一）。"""
        from awr.sensorsvc.camera.actors import SEM_CODE, proxy_mesh

        for vbo, vao, vao_rs in self.actor_vaos:
            for o in (vao, vao_rs, vbo):
                o.release()
        self.actor_vaos, self.actor_sem = [], []
        for a in actors:
            P, N, C = proxy_mesh(a)
            vbo = self.ctx.buffer(np.hstack([P, N, C]).astype("<f4").tobytes())
            fmt = [(vbo, "3f 3f 3f", "in_pos", "in_nrm", "in_col")]
            self.actor_vaos.append((vbo, self.ctx.vertex_array(self.actp, fmt), self.ctx.vertex_array(self.actp_rs, fmt)))
            self.actor_sem.append(SEM_CODE.get(a["class"], 13))

    def _draw_actors(self, act: ActorFrame, cm: CameraModel, P: np.ndarray, R_ws, p_w, rs_nodes) -> int:
        rs = rs_nodes is not None
        prog = self.actp_rs if rs else self.actp
        prog["u_P"].write(P.T.astype("<f4").tobytes())
        if rs:
            prog["u_fycy"].value = (cm.fy, cm.cy)
            prog["u_H"].value = float(cm.height)
            views = [world_to_opt(Rk, pk) for Rk, pk in rs_nodes]
        else:
            view = world_to_opt(R_ws, p_w)
        n = 0
        for i, pos, yaw in zip(act.idx, act.pos, act.yaw, strict=True):
            c, s_ = math.cos(float(yaw)), math.sin(float(yaw))
            Rz = np.array([[c, -s_, 0.0], [s_, c, 0.0], [0.0, 0.0, 1.0]])
            Mm = np.eye(4)
            Mm[:3, :3] = Rz
            Mm[:3, 3] = pos
            prog["u_Rw"].write(Rz.T.astype("<f4").tobytes())
            prog["u_inst"].value = int(i) + 1
            prog["u_sem"].value = self.actor_sem[int(i)]
            if rs:
                prog["u_mvn"].write(np.stack([(V @ Mm).T for V in views]).astype("<f4").tobytes())
            else:
                prog["u_mv"].write((view @ Mm).T.astype("<f4").tobytes())
            _vbo, vao, vao_rs = self.actor_vaos[int(i)]
            (vao_rs if rs else vao).render()
            n += 1
        return n

    def _target(self, w: int, h: int) -> _Target:
        t = self._targets.get((w, h))
        if t is None:
            t = self._targets[(w, h)] = _Target(self.ctx, self.gl, w, h)
        return t

    def _visible(self, cm: CameraModel, M: np.ndarray) -> np.ndarray:
        """视锥剔除（保守，全部批次一次计算）：八个角点同在某一裁剪面外侧时剔除。"""
        Xo = self.corners @ M[:3, :3].T + M[:3, 3]
        x, y, z = Xo[..., 0], Xo[..., 1], Xo[..., 2]
        out = ((z < cm.near_m).all(1) | (z > cm.far_m).all(1) | (x * cm.fx < -cm.cx * z).all(1)
               | (x * cm.fx > (cm.width - cm.cx) * z).all(1) | (y * cm.fy < -cm.cy * z).all(1) | (y * cm.fy > (cm.height - cm.cy) * z).all(1))
        return ~out

    def _fx_passes(self, tg: _Target, cm: CameraModel, fx: FxParams) -> None:
        def key2(k: int) -> tuple[int, int]:
            return (k & 0xFFFFFFFF, (k >> 32) & 0xFFFFFFFF)

        def put(prog, k: str, v) -> None:
            with contextlib.suppress(KeyError):                           # 驱动裁掉的未使用 uniform
                prog[k].value = v

        tg.col.use(1)
        if fx.meter:
            tg.subfb.use()
            self.ctx.viewport = (0, 0, tg.sw, tg.sh)
            self.sub_vao.render(self._mgl.TRIANGLES, vertices=3)
        tg.enc.use()
        self.ctx.viewport = (0, 0, tg.w, tg.h)
        for k, v in (("u_gain", fx.gain), ("u_vig", fx.vignette_k), ("u_K", (cm.fx, cm.fy, cm.cx, cm.cy)), ("u_read", fx.read_sigma_dn),
                     ("u_shot", fx.shot_k_dn), ("u_key", key2(fx.key_noise)), ("u_W", tg.w)):
            put(self.fxp, k, v)
        self.fx_vao.render(self._mgl.TRIANGLES, vertices=3)
        if fx.depth is not None:
            tg.zfb.use()
            tg.z.use(2)
            for k, v in (("u_d", (fx.depth.a, fx.depth.d_min_m, fx.depth.d_max_m)), ("u_key", key2(fx.key_depth)), ("u_W", tg.w)):
                put(self.dep, k, v)
            self.dep_vao.render(self._mgl.TRIANGLES, vertices=3)

    def render(self, cm: CameraModel, R_ws: np.ndarray, p_w: np.ndarray, *, linear: bool = False, fx: FxParams | None = None,
               rs_nodes: list[tuple[np.ndarray, np.ndarray]] | None = None, actors: ActorFrame | None = None) -> RenderOut:
        """`R_ws`、`p_w` 为 t_mid 位姿；`rs_nodes` 给出时按卷帘逐顶点投影（9 个节点，时间升序，覆盖读出窗口）；`actors` 给出时
        在静态场景之后绘制演员，并另以只渲染演员的通道得到 `instance_unocc`。"""
        mgl = self._mgl
        W, H = cm.width, cm.height
        tg = self._target(W, H)
        t0 = time.perf_counter()
        tg.fbo.use()
        self.ctx.viewport = (0, 0, W, H)
        tg.mrt.clear(self.gl)
        self.ctx.enable(mgl.DEPTH_TEST)
        self.ctx.disable(mgl.CULL_FACE)
        self.ctx.depth_func = ">"
        P = proj_reversed_z(cm)
        Mw = world_to_opt(R_ws, p_w)
        rs = rs_nodes is not None
        if rs and len(rs_nodes) != RS_NODES:
            raise ValueError(f"rolling shutter needs {RS_NODES} pose nodes")
        prog = self.prog_rs if rs else self.prog
        prog["u_inst"].value = 0
        prog["u_sem"].value = 0
        if rs:
            prog["u_P"].write(P.T.astype("<f4").tobytes())
            prog["u_fycy"].value = (cm.fy, cm.cy)
            prog["u_H"].value = float(H)
        n = 0
        bound = None
        tile = None
        if not self.draws:
            vis = np.zeros(0, bool)
        elif rs:                                                         # 读出窗口首、中、末三个位姿的视锥并集
            vis = np.zeros(len(self.draws), bool)
            for Rk, pk in (rs_nodes[0], rs_nodes[RS_NODES // 2], rs_nodes[-1]):
                vis |= self._visible(cm, world_to_opt(Rk, pk))
        else:
            vis = self._visible(cm, Mw)
        for (b, vao, vao_rs), ok in zip(self.draws, vis, strict=True):
            if not ok:
                continue
            if b.tile != tile:
                if rs:
                    mvs = np.stack([mv_tile(Rk, pk, b.origin).T for Rk, pk in rs_nodes]).astype("<f4")
                    prog["u_mvn"].write(mvs.tobytes())
                else:
                    mv = mv_tile(R_ws, p_w, b.origin)
                    prog["u_mv"].write(mv.T.astype("<f4").tobytes())
                    prog["u_mvp"].write((P @ mv).T.astype("<f4").tobytes())
                tile = b.tile
            if b.texture != bound:
                prog["u_has_tex"].value = b.texture is not None
                if b.texture is not None:
                    self.textures[b.texture].use(0)
                bound = b.texture
            (vao_rs if rs else vao).render()
            n += 1
        na = 0
        if actors is not None and len(actors.idx):
            na = self._draw_actors(actors, cm, P, R_ws, p_w, rs_nodes)
        self.ctx.depth_func = ">="                                       # 天空遍：只覆盖深度仍为 0 的像素
        self.sky["u_Rwo"].write(Mw[:3, :3].astype("<f4").tobytes())     # 列主序写入 R_woᵀ 的转置，即 R_wo
        self.sky["u_K"].value = (cm.fx, cm.fy, cm.cx, cm.cy)
        self.sky_vao.render(mgl.TRIANGLES, vertices=3)
        self.ctx.disable(mgl.DEPTH_TEST)
        if fx is None:
            tg.enc.use()
            tg.col.use(1)
            self.enc_vao.render(mgl.TRIANGLES, vertices=3)
        else:
            self._fx_passes(tg, cm, fx)
        unocc = None
        if actors is not None:
            if tg.unocc is None:
                tg.unocc = _Mrt(self.ctx, self.gl, W, H)
            tg.unocc.fbo.use()
            self.ctx.viewport = (0, 0, W, H)
            tg.unocc.clear(self.gl)
            if len(actors.idx):
                self.ctx.enable(mgl.DEPTH_TEST)
                self.ctx.depth_func = ">"
                self._draw_actors(actors, cm, P, R_ws, p_w, rs_nodes)
                self.ctx.disable(mgl.DEPTH_TEST)
        self.ctx.finish()
        t1 = time.perf_counter()
        if actors is not None:
            unocc = np.frombuffer(tg.unocc.inst.read(), np.uint16).reshape(H, W).copy()
        rgb = np.frombuffer(tg.rgb.read(), np.uint8).reshape(H, W, 4)[..., :3].copy()
        z = np.frombuffer(tg.z.read(), np.float32).reshape(H, W).copy()
        inst = np.frombuffer(tg.inst.read(), np.uint16).reshape(H, W).copy()
        sem = np.frombuffer(tg.sem.read(), np.uint8).reshape(H, W).copy()
        lin = np.frombuffer(tg.col.read(), np.float16).reshape(H, W, 4)[..., :3].astype(np.float32) if linear else None
        zo = np.frombuffer(tg.zobs.read(), np.float32).reshape(H, W).copy() if fx is not None and fx.depth is not None else None
        sub = (np.frombuffer(tg.sub.read(), np.float16).reshape(tg.sh, tg.sw, 4)[..., :3].astype(np.float32)
               if fx is not None and fx.meter else None)
        t2 = time.perf_counter()
        return RenderOut(rgb, z, inst, sem, (t1 - t0) * 1e3, (t2 - t1) * 1e3, n, lin, zo, sub, unocc, na)

    def release(self) -> None:
        for t in self._targets.values():
            t.mrt.release(self.gl)
            if t.unocc is not None:
                t.unocc.release(self.gl)
        self._targets.clear()
        self.ctx.release()
