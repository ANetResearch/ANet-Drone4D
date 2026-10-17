"""EGL 网格光栅化（M25 §6.4.2、§6.4.3 通道 A 的静态部分；M25-FR-030 至 033、039）。

一遍 MRT：`RGBA16F` 线性颜色、`R32F` 平面深度（片元光学帧 z，米）、`R16UI` 实例（静态场景 0）、`R8UI` 语义（几何外 255）、
`DEPTH32F` reversed-Z（`glClipControl(LOWER_LEFT, ZERO_TO_ONE)`，清除 0，比较 GREATER）。材质 unlit，纹理以 `SRGB8` 上传由硬件
解码到线性，三线性 mip 与 16× 各向异性。读回第 0 行即图像顶部。

几何之后两遍全屏三角形：天空遍以 `GEQUAL` 只覆盖深度仍为清除值 0 的像素（仰角 ≥ 0 为天空渐变，否则为远景雾色，语义 255；§6.4.3
天空）；编码遍把线性颜色按 sRGB 量化为 8 位（效果链与 ISP 落地后在此遍之前插入，§6.5.1）。读回 8 位 RGB、R32F 深度、实例与语义。

moderngl 负责程序、顶点数组与纹理；它不提供的 clip control、深度浮点附件与整型附件清除经 EGL 取函数地址以 ctypes 调用。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import time
from dataclasses import dataclass

import numpy as np

from awr.sensorsvc.camera.model import CameraModel
from awr.sensorsvc.camera.proj import mv_tile, proj_reversed_z, world_to_opt
from awr.sensorsvc.camera.scene import AppearanceScene

__all__ = ["RENDERER_VERSION", "RasterCamera", "RenderOut", "linear_to_srgb8", "srgb8_to_linear"]

RENDERER_VERSION = "raster-gl@0.1.0"
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


class _Target:
    def __init__(self, ctx, gl: _GL, w: int, h: int) -> None:
        self.w, self.h = w, h
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
        self.rgb = ctx.texture((w, h), 4, dtype="f1")
        self.enc = ctx.framebuffer(color_attachments=[self.rgb])


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
        self.sky = self.ctx.program(vertex_shader=FULLSCREEN_VS, fragment_shader=SKY_FS)
        self.enc = self.ctx.program(vertex_shader=FULLSCREEN_VS, fragment_shader=ENCODE_FS)
        for k, v in (("u_horizon", SKY_HORIZON), ("u_zenith", SKY_ZENITH), ("u_fog", FOG)):
            self.sky[k].value = tuple(float(x) for x in v)
        self.enc["u_col"].value = 1
        self.sky_vao = self.ctx.vertex_array(self.sky, [])
        self.enc_vao = self.ctx.vertex_array(self.enc, [])
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
            lo = b.origin + b.pos.min(0)
            hi = b.origin + b.pos.max(0)
            corners.append([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
            self.draws.append((b, vao))
        self.corners = np.asarray(corners, np.float64).reshape(-1, 8, 3)
        self.ctx.finish()
        self.upload_s = time.perf_counter() - t1
        self.gl.ClipControl(_GL_LOWER_LEFT, _GL_ZERO_TO_ONE)
        self._targets: dict[tuple[int, int], _Target] = {}
        self.scene = scene

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

    def render(self, cm: CameraModel, R_ws: np.ndarray, p_w: np.ndarray, *, linear: bool = False) -> RenderOut:
        mgl = self._mgl
        W, H = cm.width, cm.height
        tg = self._target(W, H)
        t0 = time.perf_counter()
        tg.fbo.use()
        self.ctx.viewport = (0, 0, W, H)
        self.gl.BindFramebuffer(_GL_FRAMEBUFFER, tg.fbo_glo)
        zero_f = (ctypes.c_float * 4)(0.0, 0.0, 0.0, 0.0)
        self.gl.ClearBufferfv(_GL_COLOR, 0, zero_f)
        self.gl.ClearBufferfv(_GL_COLOR, 1, zero_f)
        self.gl.ClearBufferuiv(_GL_COLOR, 2, (ctypes.c_uint * 4)(0, 0, 0, 0))
        self.gl.ClearBufferuiv(_GL_COLOR, 3, (ctypes.c_uint * 4)(SEM_NONE, 0, 0, 0))
        self.gl.ClearBufferfv(_GL_DEPTH, 0, zero_f)
        self.ctx.enable(mgl.DEPTH_TEST)
        self.ctx.disable(mgl.CULL_FACE)
        self.ctx.depth_func = ">"
        P = proj_reversed_z(cm)
        Mw = world_to_opt(R_ws, p_w)
        self.prog["u_inst"].value = 0
        self.prog["u_sem"].value = 0
        n = 0
        bound = None
        tile = None
        vis = self._visible(cm, Mw) if self.draws else np.zeros(0, bool)
        for (b, vao), ok in zip(self.draws, vis, strict=True):
            if not ok:
                continue
            if b.tile != tile:
                mv = mv_tile(R_ws, p_w, b.origin)
                self.prog["u_mv"].write(mv.T.astype("<f4").tobytes())
                self.prog["u_mvp"].write((P @ mv).T.astype("<f4").tobytes())
                tile = b.tile
            if b.texture != bound:
                self.prog["u_has_tex"].value = b.texture is not None
                if b.texture is not None:
                    self.textures[b.texture].use(0)
                bound = b.texture
            vao.render()
            n += 1
        self.ctx.depth_func = ">="                                       # 天空遍：只覆盖深度仍为 0 的像素
        self.sky["u_Rwo"].write(Mw[:3, :3].astype("<f4").tobytes())     # 列主序写入 R_woᵀ 的转置，即 R_wo
        self.sky["u_K"].value = (cm.fx, cm.fy, cm.cx, cm.cy)
        self.sky_vao.render(mgl.TRIANGLES, vertices=3)
        self.ctx.disable(mgl.DEPTH_TEST)
        tg.enc.use()
        tg.col.use(1)
        self.enc_vao.render(mgl.TRIANGLES, vertices=3)
        self.ctx.finish()
        t1 = time.perf_counter()
        rgb = np.frombuffer(tg.rgb.read(), np.uint8).reshape(H, W, 4)[..., :3].copy()
        z = np.frombuffer(tg.z.read(), np.float32).reshape(H, W).copy()
        inst = np.frombuffer(tg.inst.read(), np.uint16).reshape(H, W).copy()
        sem = np.frombuffer(tg.sem.read(), np.uint8).reshape(H, W).copy()
        lin = np.frombuffer(tg.col.read(), np.float16).reshape(H, W, 4)[..., :3].astype(np.float32) if linear else None
        t2 = time.perf_counter()
        return RenderOut(rgb, z, inst, sem, (t1 - t0) * 1e3, (t2 - t1) * 1e3, n, lin)

    def release(self) -> None:
        for t in self._targets.values():
            self.gl.DeleteFramebuffers(1, ctypes.byref(ctypes.c_uint(t.fbo_glo)))
            self.gl.DeleteTextures(1, ctypes.byref(ctypes.c_uint(t.depth_glo)))
        self._targets.clear()
        self.ctx.release()
