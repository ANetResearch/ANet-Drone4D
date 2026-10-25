"""EGL 网格光栅化（M25 §6.4.2、§6.4.3 通道 A 的静态部分；M25-FR-030 至 033、039；docs/05 §5.4.3 效果链；D3-AC-11）。

一遍 MRT：`RGBA16F` 线性颜色、`R32F` 平面深度（片元光学帧 z，米）、`R16UI` 实例（静态场景 0）、`R8UI` 语义（几何外 255）、
`R8UI` 天空分类（`qual`：0 有几何或仰角 ≥ 0 的天空，1 无几何下视，2 仰角接近 0 由 CPU 以 float64 复核）、`DEPTH32F` reversed-Z
（`glClipControl(LOWER_LEFT, ZERO_TO_ONE)`，清除 0，比较 GREATER）。材质 unlit，纹理以 `SRGB8` 上传由硬件解码到线性，三线性 mip
与 16× 各向异性。读回第 0 行即图像顶部。

几何之后：天空遍以 `GEQUAL` 只覆盖深度仍为清除值 0 的像素（仰角 ≥ 0 为天空渐变，否则为远景雾色，语义 255；§6.4.3 天空），
同时写天空分类（`invalid_mask` 第 0 位"无几何下视"由此得到，帧组装不再逐像素求视线仰角）。

演员（M25 §6.4.4）：全部代理网格合并为一个顶点缓冲，逐帧以 SSBO 给出每名演员的模型视图矩阵、实例号与语义（`actorbuf`），
一次绘制调用画出全部演员；只渲染演员的无遮挡通道同样一次绘制。

卷帘（M25 §15.4，`rs_method = vertex`）：`rs_nodes=[(R, p)] × 9` 时几何遍换用逐顶点行时刻投影的顶点着色器：读出窗口内 9 个
等间隔相机位姿节点以瓦片相对的模型视图矩阵传入，顶点按 2 次不动点迭代求自身所在交付行的时刻（有畸变时先对归一化坐标施加
交付相机的畸变模型，行时刻按交付图像的行计），节点间对矩阵线性插值；深度、实例、语义与颜色同一遍写出。天空遍取 t_mid 位姿。
9 个节点相同（静止机体，`rs_static`）时改走全局快门程序，与全局快门输出逐字节一致。验收参照见 `camera.rowref`。

效果链（`fx=FxParams`；docs/05 §5.4.3 顺序，参考实现见 `camera.effects`）：
1. 渲染域（无畸变；交付相机有畸变时按 `proj.build_remap` 取含裕量的渲染相机）着色遍：接触阴影贴花（静态像素，逐演员椭圆衰减）、
   2048² 投影阴影图（从太阳方向正交投影绘制本帧演员，16 采样 PCF）、雨天湿润、雾与雨的消光（沿视线斜距）与雨线（16 采样，
   雨滴网格按帧键哈希）；
2. 畸变重映射遍（只在 `dist` 非零时）：RGB 线性域双线性，深度、实例、语义、天空分类与无遮挡实例最近邻，输出交付尺寸；
3. 交付域：增益、渐晕、Poisson–Gaussian 噪声、sRGB 与量化（逐像素正态以 `GL_ARB_gpu_shader_int64` 的 SplitMix64 与参考实现同式），
   另加 L2-depth 遍（观测深度写入单独的 R32F）与自动曝光测光用的 1/8 降采样遍。

读回（M25 §6.7）：`submit` 绘制并把所需平面以 `glGetTextureImage` 写入持久映射的读回槽位（`camera.readback`）后立即返回
`RenderJob`；`RenderJob.result()` 只等栅栏，数组是槽位内存上的只读视图，`release()` 归还槽位。调用方因此可以在 GPU 执行与 DMA
期间提交下一帧，帧组装线程直接读视图。`render` 为同步包装（提交、等待、复制、归还），返回可写数组，与此前接口一致。

moderngl 负责程序、顶点数组与纹理；它不提供的 clip control、深度浮点附件与整型附件清除经 EGL 取函数地址以 ctypes 调用。
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import time
from dataclasses import dataclass, field

import numpy as np

from awr.sensorsvc.camera import effects as _fx
from awr.sensorsvc.camera.actorbuf import GLSL_ACTOR_SSBO, ActorBuffers
from awr.sensorsvc.camera.actors import GLSL_VIS_CS, VIS_STRIDE, vis_init
from awr.sensorsvc.camera.effects import FxParams
from awr.sensorsvc.camera.model import CameraModel
from awr.sensorsvc.camera.proj import (
    RemapTable,
    build_remap,
    gl_mat4,
    look_at_R,
    mv_tiles,
    proj_reversed_z,
    world_to_opt,
)
from awr.sensorsvc.camera.readback import ReadbackRing, ReadJob, plane_layout
from awr.sensorsvc.camera.scene import AppearanceScene

__all__ = ["RENDERER_VERSION", "RS_NODES", "SHADOW_MAP_SIZE", "ActorFrame", "RasterCamera", "RenderJob", "RenderOut", "linear_to_srgb8",
           "ortho_reversed_z", "srgb8_to_linear"]

RENDERER_VERSION = "raster-gl@0.5.0"
SEM_NONE = 255
QUAL_EPS = 1e-5                                # 天空分类：|仰角正弦| ≤ 1e-5 记为 2，由 CPU 以 float64 复核
SHADOW_MAP_SIZE = 2048
SHADOW_SPAN_MIN_M, SHADOW_SPAN_MAX_M = 8.0, 400.0
SHADOW_NEAR_M, SHADOW_FAR_M, SHADOW_DIST_M = 1.0, 1000.0, 500.0
SHADOW_BIAS = 2e-4                             # 光源深度（归一化）偏置，约 0.2 m
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
# 卷帘行时刻（交付行）：u_fycy、u_H 为交付相机；有畸变时对归一化坐标先施加交付相机的畸变模型（OpenCV k1、k2、p1、p2、k3）
_GLSL_RS = """
const int NC = 9;
uniform vec2 u_fycy;
uniform float u_H;
uniform int u_hasdist;
uniform vec4 u_dist0;
uniform float u_k3;
float row_of(vec4 q) {
    if (!(q.z > 1e-3)) return 0.5 * u_H;
    if (u_hasdist == 0) return u_fycy.x * q.y / q.z + u_fycy.y;
    float xn = q.x / q.z;
    float yn = q.y / q.z;
    float r2 = xn * xn + yn * yn;
    float rad = 1.0 + r2 * (u_dist0.x + r2 * (u_dist0.y + r2 * u_k3));
    float yd = yn * rad + u_dist0.z * (r2 + 2.0 * yn * yn) + 2.0 * u_dist0.w * xn * yn;
    return u_fycy.x * yd + u_fycy.y;
}
float s_of(mat4 mv, vec4 p) {
    float i = clamp(row_of(mv * p) - 0.5, 0.0, u_H - 1.0);
    return ((i - 0.5 * (u_H - 1.0)) / u_H + 0.5) * float(NC - 1);
}
"""
VS_RS = """
#version 450
""" + _GLSL_RS + """
uniform mat4 u_P;
uniform mat4 u_mvn[NC];
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
layout(location = 4) out uint o_qual;
void main() {
    vec3 c = u_has_tex ? texture(u_tex, v_uv).rgb : vec3(0.2158);
    o_col = vec4(c, 1.0);
    o_z = v_z;
    o_inst = u_inst;
    o_sem = u_sem;
    o_qual = 0u;
}
"""

# 演员（M25 §6.4.4）：合批绘制，逐演员数据取自 SSBO（actorbuf）；深度测试值朝相机偏置 1 cm，平面深度附件写无偏置值
ACTOR_VS = """
#version 450
""" + GLSL_ACTOR_SSBO + """
uniform mat4 u_P;
uniform vec3 u_sun;
in vec3 in_pos;
in vec3 in_nrm;
in vec3 in_col;
in float in_aid;
out vec3 v_col;
out float v_z;
flat out uint v_inst;
flat out uint v_sem;
void main() {
    int a = int(in_aid + 0.5);
    uvec4 inf = act_info[u_base + a];
    v_inst = inf.x;
    v_sem = inf.y;
    vec3 n = normalize(mat3(act_rw[u_base + a]) * in_nrm);
    v_col = in_col * (0.55 + 0.45 * max(0.0, dot(n, u_sun)));
    vec4 q = act_mv[u_mvbase + a] * vec4(in_pos, 1.0);
    v_z = q.z;
    gl_Position = inf.z == 0u ? vec4(2.0, 2.0, 2.0, 1.0)                 // 本帧未出现：退化到裁剪体外
                              : u_P * vec4(q.z > 0.02 ? q.xyz * ((q.z - 0.01) / q.z) : q.xyz, 1.0);   // 相机后方的顶点不缩放
}
"""
ACTOR_VS_RS = """
#version 450
""" + GLSL_ACTOR_SSBO + _GLSL_RS + """
uniform mat4 u_P;
uniform vec3 u_sun;
in vec3 in_pos;
in vec3 in_nrm;
in vec3 in_col;
in float in_aid;
out vec3 v_col;
out float v_z;
flat out uint v_inst;
flat out uint v_sem;
int g_m0;
mat4 mv_at(float s) {
    float c = clamp(s, 0.0, float(NC - 1));
    int k = min(int(floor(c)), NC - 2);
    float f = c - float(k);
    return act_mv[g_m0 + k] * (1.0 - f) + act_mv[g_m0 + k + 1] * f;
}
void main() {
    int a = int(in_aid + 0.5);
    uvec4 inf = act_info[u_base + a];
    v_inst = inf.x;
    v_sem = inf.y;
    g_m0 = u_mvbase + a * NC;
    vec3 n = normalize(mat3(act_rw[u_base + a]) * in_nrm);
    v_col = in_col * (0.55 + 0.45 * max(0.0, dot(n, u_sun)));
    vec4 p = vec4(in_pos, 1.0);
    float s = 0.5 * float(NC - 1);
    for (int it = 0; it < 2; ++it) s = s_of(mv_at(s), p);
    vec4 q = mv_at(s) * p;
    v_z = q.z;
    gl_Position = inf.z == 0u ? vec4(2.0, 2.0, 2.0, 1.0)
                              : u_P * vec4(q.z > 0.02 ? q.xyz * ((q.z - 0.01) / q.z) : q.xyz, 1.0);
}
"""
ACTOR_FS = """
#version 450
in vec3 v_col;
in float v_z;
flat in uint v_inst;
flat in uint v_sem;
layout(location = 0) out vec4 o_col;
layout(location = 1) out float o_z;
layout(location = 2) out uint o_inst;
layout(location = 3) out uint o_sem;
layout(location = 4) out uint o_qual;
void main() {
    o_col = vec4(v_col, 1.0);
    o_z = v_z;
    o_inst = v_inst;
    o_sem = v_sem;
    o_qual = 0u;
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
uniform float u_eps;
layout(location = 0) out vec4 o_col;
layout(location = 1) out float o_z;
layout(location = 2) out uint o_inst;
layout(location = 3) out uint o_sem;
layout(location = 4) out uint o_qual;
void main() {
    vec3 r = vec3((gl_FragCoord.x - u_K.z) / u_K.x, (gl_FragCoord.y - u_K.w) / u_K.y, 1.0);
    float el = (u_Rwo * r).z / length(r);
    float k = sqrt(clamp(el, 0.0, 1.0));
    o_col = vec4(el >= 0.0 ? mix(u_horizon, u_zenith, k) : u_fog, 1.0);
    o_z = 0.0;
    o_inst = 0u;
    o_sem = 255u;
    o_qual = el < -u_eps ? 1u : (el > u_eps ? 0u : 2u);
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



# 渲染域着色遍（docs/05 §5.4.3；参考实现 `effects.shade_reference`，投影阴影图除外）
SHADE_FS = """
#version 450
layout(std430, binding = 6) readonly buffer Cast { vec4 casters[]; };   // 每演员 2 项：(cx, cy, z0, 0)、(ux, uy, 1/a, 1/b)
uniform sampler2D u_col;
uniform sampler2D u_z;
uniform usampler2D u_inst;
uniform sampler2D u_sm;
uniform vec4 u_K;
uniform mat3 u_Rwo;
uniform vec3 u_p;
uniform int u_flags;                                     // 1 接触阴影、2 投影阴影图、4 消光、8 雨（湿润与雨线）
uniform int u_ncast;
uniform mat4 u_L;
uniform float u_sigma;
uniform vec3 u_fogc;
uniform vec2 u_rdir;
uniform float u_rdens;
uniform uvec2 u_rkey;
layout(location = 0) out vec4 o_col;
const vec2 PD[16] = vec2[](vec2(-0.94, -0.40), vec2(0.95, -0.77), vec2(-0.09, -0.93), vec2(0.34, 0.29), vec2(-0.92, 0.46),
    vec2(-0.82, -0.15), vec2(-0.38, 0.28), vec2(-0.19, 0.84), vec2(0.82, 0.31), vec2(0.37, -0.74), vec2(0.18, -0.14),
    vec2(0.62, 0.71), vec2(-0.59, -0.80), vec2(0.06, 0.40), vec2(-0.45, -0.32), vec2(0.89, -0.09));
uint pcg(uint v) {
    uint s = v * 747796405u + 2891336453u;
    uint w = ((s >> ((s >> 28u) + 4u)) ^ s) * 277803737u;
    return (w >> 22u) ^ w;
}
float h01(uvec2 c, uint k) { return float(pcg(c.x ^ pcg(c.y ^ pcg(k)))) * (1.0 / 4294967296.0); }
void main() {
    ivec2 q = ivec2(gl_FragCoord.xy);
    vec3 c = texelFetch(u_col, q, 0).rgb;
    float z = texelFetch(u_z, q, 0).r;
    vec3 r = vec3((float(q.x) + 0.5 - u_K.z) / u_K.x, (float(q.y) + 0.5 - u_K.w) / u_K.y, 1.0);
    float rng = z * length(r);
    if (z > 0.0) {
        vec3 X = u_Rwo * (r * z) + u_p;
        float sh = 1.0;
        if ((u_flags & 1) != 0 && texelFetch(u_inst, q, 0).r == 0u) {
            float a = 0.0;
            for (int i = 0; i < u_ncast; ++i) {
                vec4 c0 = casters[2 * i];
                vec4 c1 = casters[2 * i + 1];
                vec2 d = X.xy - c0.xy;
                float al = (d.x * c1.x + d.y * c1.y) * c1.z;
                float ac = (-d.x * c1.y + d.y * c1.x) * c1.w;
                float e = al * al + ac * ac;
                if (e < 1.0) a = max(a, (1.0 - smoothstep(0.5, 1.0, e)) * (1.0 - smoothstep(0.0, @CDZ@, abs(X.z - c0.z))));
            }
            sh = 1.0 - @CK@ * a;
        }
        if ((u_flags & 2) != 0) {
            vec4 L = u_L * vec4(X, 1.0);
            vec2 uv = L.xy * 0.5 + 0.5;
            if (all(greaterThan(uv, vec2(0.0))) && all(lessThan(uv, vec2(1.0)))) {
                float o = 0.0;
                for (int i = 0; i < 16; ++i) {               // 16 采样 PCF；reversed-Z：深度值大者更靠近光源
                    float d = texture(u_sm, uv + PD[i] * (1.5 / @SMS@.0)).r;
                    o += d > L.z + @SMB@ ? 1.0 : 0.0;
                }
                sh = min(sh, 1.0 - @SMK@ * o / 16.0);
            }
        }
        c *= sh;
        if ((u_flags & 8) != 0) {
            vec3 wet = c * @WK@;
            float g = dot(wet, vec3(0.2126, 0.7152, 0.0722));
            c = mix(wet, vec3(g), @WD@);
        }
        if ((u_flags & 4) != 0) c = mix(u_fogc, c, exp(-u_sigma * rng));
    }
    if ((u_flags & 8) != 0) {
        float zz = z > 0.0 ? rng : 1e4;
        float acc = 0.0;
        for (int i = 0; i < @RS@; ++i) {                     // 雨线：沿屏幕位移方向逐点取雨滴网格，雨滴比场景近时可见
            precise vec2 s = vec2(q) - u_rdir * (float(i) / @RS1@.0);
            uvec2 cell = uvec2(max(s, vec2(0.0)) / @RC@);
            float rr = h01(cell, u_rkey.x);
            precise float d = 0.5 + 29.5 * h01(cell, u_rkey.y);
            if (rr < u_rdens && d < zz) acc += 1.0 / (1.0 + 0.15 * d);
        }
        c = mix(c, vec3(@RL@), clamp(acc * @RA@, 0.0, @RM@));
    }
    o_col = vec4(c, 1.0);
}
"""
for _k, _v in {"@CDZ@": repr(_fx.CONTACT_DZ_M), "@CK@": repr(_fx.CONTACT_K), "@SMS@": str(SHADOW_MAP_SIZE), "@SMB@": repr(SHADOW_BIAS),
               "@SMK@": repr(_fx.SHADOW_MAP_K), "@WK@": repr(_fx.WET_K), "@WD@": repr(_fx.WET_DESAT), "@RS@": str(_fx.RAIN_SAMPLES),
               "@RS1@": str(_fx.RAIN_SAMPLES - 1), "@RC@": repr(_fx.RAIN_CELL_PX), "@RL@": repr(_fx.RAIN_STREAK_LIN),
               "@RA@": repr(_fx.RAIN_ALPHA_K), "@RM@": repr(_fx.RAIN_ALPHA_MAX)}.items():
    SHADE_FS = SHADE_FS.replace(_k, _v)

# 畸变重映射遍（docs/05 §5.4.3；参考实现 `proj.remap_bilinear`、`proj.remap_nearest`）：交付像素 → 渲染域连续坐标（LUT，RG32F）
RESOLVE_FS = """
#version 450
uniform sampler2D u_col;
uniform sampler2D u_z;
uniform usampler2D u_inst;
uniform usampler2D u_sem;
uniform usampler2D u_qual;
uniform usampler2D u_unocc;
uniform sampler2D u_map;
uniform bool u_has_unocc;
layout(location = 0) out vec4 o_col;
layout(location = 1) out float o_z;
layout(location = 2) out uint o_inst;
layout(location = 3) out uint o_sem;
layout(location = 4) out uint o_qual;
layout(location = 5) out uint o_unocc;
vec3 C(ivec2 p) { return texelFetch(u_col, p, 0).rgb; }
void main() {
    ivec2 q = ivec2(gl_FragCoord.xy);
    ivec2 sz = textureSize(u_z, 0);
    vec2 m = texelFetch(u_map, q, 0).rg;
    ivec2 n = clamp(ivec2(floor(m)), ivec2(0), sz - 1);
    vec2 uv = m - 0.5;
    ivec2 i0 = ivec2(floor(uv));
    vec2 f = uv - vec2(i0);
    ivec2 i1 = clamp(i0 + 1, ivec2(0), sz - 1);
    i0 = clamp(i0, ivec2(0), sz - 1);
    vec3 c = (1.0 - f.y) * ((1.0 - f.x) * C(i0) + f.x * C(ivec2(i1.x, i0.y))) + f.y * ((1.0 - f.x) * C(ivec2(i0.x, i1.y)) + f.x * C(i1));
    o_col = vec4(c, 1.0);
    o_z = texelFetch(u_z, n, 0).r;
    o_inst = texelFetch(u_inst, n, 0).r;
    o_sem = texelFetch(u_sem, n, 0).r;
    o_qual = texelFetch(u_qual, n, 0).r;
    o_unocc = u_has_unocc ? texelFetch(u_unocc, n, 0).r : 0u;
}
"""


RS_STATIC_ROT, RS_STATIC_POS_M = 1e-9, 1e-6


def rs_static(rs_nodes: list[tuple[np.ndarray, np.ndarray]]) -> bool:
    """读出窗口内位姿节点相同（旋转矩阵逐元素差 ≤ 1e-9、位置差 ≤ 1 µm，对应像素位移 < 1e-5 px）：静止机体。

    逐顶点着色器对节点矩阵线性插值并单独乘投影矩阵，float32 舍入与全局快门的 `u_mvp` 不逐位相同（实测 1080p 约 30 个深度像素
    差 1 ulp）；静止时直接走全局快门程序，卷帘与全局快门输出逐字节一致。"""
    R0, p0 = (np.asarray(v, np.float64) for v in rs_nodes[0])
    return all(np.abs(np.asarray(Rk, np.float64) - R0).max() <= RS_STATIC_ROT and np.abs(np.asarray(pk, np.float64) - p0).max() <= RS_STATIC_POS_M
               for Rk, pk in rs_nodes[1:])


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



def ortho_reversed_z(span_m: float, near_m: float, far_m: float) -> np.ndarray:
    """光源正交投影（光学帧 → 裁剪空间，ZERO_TO_ONE 深度：z = near 时 1，z = far 时 0）。"""
    P = np.zeros((4, 4))
    P[0, 0] = P[1, 1] = 2.0 / span_m
    P[2, 2] = -1.0 / (far_m - near_m)
    P[2, 3] = far_m / (far_m - near_m)
    P[3, 3] = 1.0
    return P


@dataclass
class RenderOut:
    rgb8: np.ndarray          # (H, W, 3) uint8 sRGB
    depth_m: np.ndarray | None   # (H, W) float32 平面深度，几何外 0（异步读回未请求 `z` 时为 None）
    instance: np.ndarray      # (H, W) uint16
    semantic: np.ndarray      # (H, W) uint8，几何外 255
    draw_ms: float
    readback_ms: float
    batches_drawn: int
    rgb_lin: np.ndarray | None = None     # (H, W, 3) float32 线性，效果链增益之前的交付域图像（`linear=True`）
    depth_obs: np.ndarray | None = None   # (H, W) float32 L2-depth 观测深度，`fx.depth` 给出时
    meter_sub: np.ndarray | None = None   # (⌈H/8⌉, ⌈W/8⌉, 3) float32 线性 1/8 降采样，`fx.meter` 时
    instance_unocc: np.ndarray | None = None   # (H, W) uint16 只渲染演员的实例（`actors` 给出时）
    actors_drawn: int = 0
    qual: np.ndarray | None = None        # (H, W) uint8 天空分类：0 有几何或天空、1 无几何下视、2 接近地平线（CPU 复核）
    remap: RemapTable | None = None       # 有畸变时的渲染相机与映射表（天空分类复核用）
    gpu_ms: float | None = None           # GL 计时查询：本帧绘制与效果遍的 GPU 时间（`timer=True`）
    debug: dict | None = None             # `debug=True`：渲染域的着色前线性图像、着色后图像、平面深度、实例与天空分类
    vis: np.ndarray | None = None         # (K, VIS_STRIDE) uint32 GPU 可见度统计（`actors.visibility_stats` 同一布局；读回 `vis` 且有演员时）


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
            # PYFUNCTYPE：调用期间不释放 GIL（这些调用只是把命令放进驱动队列，微秒级；CFUNCTYPE 每次释放后重新竞争 GIL，帧组装
            # 线程繁忙时渲染线程每次调用都可能等到切换间隔）
            return ctypes.PYFUNCTYPE(u if name == "glCheckFramebufferStatus" else None, *args)(addr)

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
        self.Flush = fn("glFlush")


class _Mrt:
    """MRT 帧缓冲：颜色 f16×4（`col_dtype = "f4"` 时 f32×4，热成像的表面温度与发射率）、平面深度 f32、实例 u16、语义 u8，
    `qual = True` 时另加天空分类 u8；加 DEPTH32F 深度附件（reversed-Z）。"""

    def __init__(self, ctx, gl: _GL, w: int, h: int, *, col_dtype: str = "f2", qual: bool = False) -> None:
        self.col = ctx.texture((w, h), 4, dtype=col_dtype)
        self.z = ctx.texture((w, h), 1, dtype="f4")
        self.inst = ctx.texture((w, h), 1, dtype="u2")
        self.sem = ctx.texture((w, h), 1, dtype="u1")
        self.qual = ctx.texture((w, h), 1, dtype="u1") if qual else None
        atts = [t for t in (self.col, self.z, self.inst, self.sem, self.qual) if t is not None]
        for t in atts:
            t.filter = (0x2600, 0x2600)                                     # NEAREST
        d = ctypes.c_uint(0)
        gl.GenTextures(1, ctypes.byref(d))
        gl.BindTexture(_GL_TEXTURE_2D, d.value)
        gl.TexStorage2D(_GL_TEXTURE_2D, 1, _GL_DEPTH32F, w, h)
        self.depth_glo = d.value
        f = ctypes.c_uint(0)
        gl.GenFramebuffers(1, ctypes.byref(f))
        gl.BindFramebuffer(_GL_FRAMEBUFFER, f.value)
        for k, t in enumerate(atts):
            gl.FramebufferTexture2D(_GL_FRAMEBUFFER, _GL_COLOR_ATTACHMENT0 + k, _GL_TEXTURE_2D, t.glo, 0)
        gl.FramebufferTexture2D(_GL_FRAMEBUFFER, _GL_DEPTH_ATTACHMENT, _GL_TEXTURE_2D, self.depth_glo, 0)
        self.n_att = len(atts)
        bufs = (ctypes.c_uint * self.n_att)(*[_GL_COLOR_ATTACHMENT0 + k for k in range(self.n_att)])
        gl.DrawBuffers(self.n_att, bufs)
        st = gl.CheckFramebufferStatus(_GL_FRAMEBUFFER)
        if st != _GL_FB_COMPLETE:
            raise RuntimeError(f"framebuffer incomplete: 0x{st:04X}")
        self.fbo_glo = f.value
        self.fbo = ctx.detect_framebuffer(f.value)

    _ZERO_F = (ctypes.c_float * 4)(0.0, 0.0, 0.0, 0.0)
    _ZERO_U = (ctypes.c_uint * 4)(0, 0, 0, 0)
    _SEM_U = (ctypes.c_uint * 4)(SEM_NONE, 0, 0, 0)

    def clear(self, gl: _GL) -> None:
        gl.BindFramebuffer(_GL_FRAMEBUFFER, self.fbo_glo)
        zf, zu = self._ZERO_F, self._ZERO_U
        gl.ClearBufferfv(_GL_COLOR, 0, zf)
        gl.ClearBufferfv(_GL_COLOR, 1, zf)
        gl.ClearBufferuiv(_GL_COLOR, 2, zu)
        gl.ClearBufferuiv(_GL_COLOR, 3, self._SEM_U)
        if self.qual is not None:
            gl.ClearBufferuiv(_GL_COLOR, 4, zu)
        gl.ClearBufferfv(_GL_DEPTH, 0, zf)

    def release(self, gl: _GL) -> None:
        gl.DeleteFramebuffers(1, ctypes.byref(ctypes.c_uint(self.fbo_glo)))
        gl.DeleteTextures(1, ctypes.byref(ctypes.c_uint(self.depth_glo)))
        for t in (self.col, self.z, self.inst, self.sem, self.qual):
            if t is not None:
                t.release()


class _Target:
    """一个（交付尺寸，渲染尺寸）组合的全部渲染目标：渲染域 MRT（含天空分类）与只渲染演员的通道、着色遍输出（RGBA32F）、畸变重映射
    输出（交付尺寸）、交付 RGB8、L2-depth 与测光小图。渲染域与交付域在无畸变时尺寸相同。"""

    def __init__(self, ctx, gl: _GL, w: int, h: int, wr: int, hr: int) -> None:
        self.ctx, self.gl = ctx, gl
        self.w, self.h, self.wr, self.hr = w, h, wr, hr
        self.mrt = _Mrt(ctx, gl, wr, hr, qual=True)
        self.unocc: _Mrt | None = None                                     # 只渲染演员的通道，首次使用时创建
        self.col2 = self.col2_fb = None                                    # 着色遍输出
        self.res: list | None = None                                       # 畸变重映射输出 [col, z, inst, sem, qual, unocc]
        self.res_fb = None
        self.rgb = ctx.texture((w, h), 4, dtype="f1")
        self.enc = ctx.framebuffer(color_attachments=[self.rgb])
        self.zobs = ctx.texture((w, h), 1, dtype="f4")
        self.zfb = ctx.framebuffer(color_attachments=[self.zobs])
        self.sw, self.sh = -(-w // 8), -(-h // 8)
        self.sub = ctx.texture((self.sw, self.sh), 4, dtype="f4")              # f32：读回不经驱动的 f16 → f32 CPU 转换
        self.subfb = ctx.framebuffer(color_attachments=[self.sub])
        self.maps: dict[int, tuple[RemapTable, object]] = {}

    def shade_target(self):
        if self.col2 is None:
            self.col2 = self.ctx.texture((self.wr, self.hr), 4, dtype="f4")
            self.col2.filter = (0x2600, 0x2600)
            self.col2_fb = self.ctx.framebuffer(color_attachments=[self.col2])
        return self.col2_fb

    def resolve_target(self):
        if self.res is None:
            c = self.ctx
            self.res = [c.texture((self.w, self.h), 4, dtype="f4"), c.texture((self.w, self.h), 1, dtype="f4"),
                        c.texture((self.w, self.h), 1, dtype="u2"), c.texture((self.w, self.h), 1, dtype="u1"),
                        c.texture((self.w, self.h), 1, dtype="u1"), c.texture((self.w, self.h), 1, dtype="u2")]
            for t in self.res:
                t.filter = (0x2600, 0x2600)
            self.res_fb = c.framebuffer(color_attachments=self.res)
        return self.res_fb

    def remap_tex(self, rt: RemapTable):
        got = self.maps.get(id(rt))
        if got is None:
            m = np.stack([rt.map_u, rt.map_v], -1).astype("<f4")
            t = self.ctx.texture((self.w, self.h), 2, m.tobytes(), dtype="f4")
            t.filter = (0x2600, 0x2600)
            got = self.maps[id(rt)] = (rt, t)
        return got[1]

    def release(self) -> None:
        self.mrt.release(self.gl)
        if self.unocc is not None:
            self.unocc.release(self.gl)
        objs = [self.rgb, self.enc, self.zobs, self.zfb, self.sub, self.subfb, self.col2, self.col2_fb, self.res_fb, *(self.res or [])]
        objs += [t for _rt, t in self.maps.values()]
        for o in objs:
            if o is not None:
                with contextlib.suppress(Exception):
                    o.release()


@dataclass
class RenderJob:
    """已提交的一帧（`RasterCamera.submit`）：`result()` 在渲染线程等待读回并返回 `RenderOut`（数组为读回槽位上的只读视图），
    `release()` 归还槽位（任一线程，幂等；归还后视图失效）。"""

    read: ReadJob
    meta: dict
    query: object | None = None
    _out: RenderOut | None = None
    _t_submit: float = field(default_factory=time.perf_counter)

    def result(self) -> RenderOut:
        if self._out is None:
            t0 = time.perf_counter()
            v = self.read.wait()
            m = self.meta
            gpu = self.query.elapsed / 1e6 if self.query is not None else None
            dbg = {k[4:]: v[k] for k in v if k.startswith("dbg_")} or None
            vis = v.get("vis")
            self._out = RenderOut(v["rgb"], v.get("z"), v["inst"], v["sem"], m["draw_ms"], (time.perf_counter() - t0) * 1e3, m["batches"],
                                  v.get("lin"), v.get("zobs"), v.get("sub"), v.get("unocc"), m["actors"], v.get("qual"), m["remap"], gpu, dbg,
                                  None if vis is None else vis.reshape(-1, VIS_STRIDE))
        return self._out

    def release(self) -> None:
        self.read.release()
        self._out = None


def _copy_out(o: RenderOut) -> RenderOut:
    """同步接口：把槽位视图复制为可写数组（槽位随后归还）。"""
    def cp(a):
        return None if a is None else np.array(a)

    return RenderOut(cp(o.rgb8), cp(o.depth_m), cp(o.instance), cp(o.semantic), o.draw_ms, o.readback_ms, o.batches_drawn, cp(o.rgb_lin),
                     cp(o.depth_obs), cp(o.meter_sub), cp(o.instance_unocc), o.actors_drawn, cp(o.qual), o.remap, o.gpu_ms,
                     None if o.debug is None else {k: np.array(v) for k, v in o.debug.items()}, cp(o.vis))


class RasterCamera:
    """持有 EGL 上下文与常驻世界外观；`render`、`submit` 线程不安全（每 GPU 一个 worker 进程，M25 §6.14.1）。

    `textures = False` 时不解码与上传纹理（热成像等只用几何的通道共用本对象的上下文时，省去纹理显存与加载时间；此时可见光绘制
    按无纹理材质输出）。`readback_slots` 为异步读回槽位数（同时未归还的帧数上限）。"""

    def __init__(self, scene: AppearanceScene, *, ctx=None, textures: bool = True, readback_slots: int = 6) -> None:
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
        self.shadep = self.ctx.program(vertex_shader=FULLSCREEN_VS, fragment_shader=SHADE_FS)
        self.resp = self.ctx.program(vertex_shader=FULLSCREEN_VS, fragment_shader=RESOLVE_FS)
        self.actp = self.ctx.program(vertex_shader=ACTOR_VS, fragment_shader=ACTOR_FS)
        self.actp_rs = self.ctx.program(vertex_shader=ACTOR_VS_RS, fragment_shader=ACTOR_FS)
        self.actors: ActorBuffers | None = None
        self.actor_sem: list[int] = []
        self.actor_dims = np.zeros((0, 3))
        self.fxp["u_col"].value = 1
        self.subp["u_col"].value = 1
        self.dep["u_z"].value = 2
        for k, v in (("u_horizon", SKY_HORIZON), ("u_zenith", SKY_ZENITH), ("u_fog", FOG)):
            self.sky[k].value = tuple(float(x) for x in v)
        self.sky["u_eps"].value = QUAL_EPS
        self.enc["u_col"].value = 1
        for k, unit in (("u_col", 1), ("u_z", 2), ("u_inst", 3), ("u_sm", 4)):
            self._put(self.shadep, k, unit)
        for k, unit in (("u_col", 1), ("u_z", 2), ("u_inst", 3), ("u_sem", 4), ("u_qual", 5), ("u_unocc", 6), ("u_map", 7)):
            self._put(self.resp, k, unit)
        self.sky_vao = self.ctx.vertex_array(self.sky, [])
        self.enc_vao = self.ctx.vertex_array(self.enc, [])
        self.fx_vao = self.ctx.vertex_array(self.fxp, [])
        self.dep_vao = self.ctx.vertex_array(self.dep, [])
        self.sub_vao = self.ctx.vertex_array(self.subp, [])
        self.shade_vao = self.ctx.vertex_array(self.shadep, [])
        self.res_vao = self.ctx.vertex_array(self.resp, [])
        t0 = time.perf_counter()
        Image.MAX_IMAGE_PIXELS = None
        self.textures = []
        for _mime, data in scene.textures if textures else ():
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
        tiles = sorted({b.tile for b, *_ in self.draws})                  # 瓦片序号 → 原点；每批次所属瓦片（逐帧向量化计算模型视图）
        first = {}
        for b, *_ in self.draws:
            first.setdefault(b.tile, b.origin)
        self.tile_org = np.asarray([first[t] for t in tiles], np.float64).reshape(-1, 3)
        self.draw_tile = np.asarray([tiles.index(b.tile) for b, *_ in self.draws], np.int64)
        self.ctx.finish()
        self.upload_s = time.perf_counter() - t1
        self.gl.ClipControl(_GL_LOWER_LEFT, _GL_ZERO_TO_ONE)
        self._targets: dict[tuple[int, int, int, int], _Target] = {}
        self.ring = ReadbackRing(slots=readback_slots)
        self._queries: list = []
        self._sm = self._sm_fb = None
        self.cast_buf = self.ctx.buffer(reserve=32)
        self.visp = self.ctx.compute_shader(GLSL_VIS_CS)                  # 可见度统计（GPU 归约，M25 §6.6.1）
        for k, unit in (("u_inst", 8), ("u_unocc", 9), ("u_sem", 10)):
            self._put(self.visp, k, unit)
        self.vis_buf = self.ctx.buffer(reserve=VIS_STRIDE * 4)
        self.vis_K = 1
        self._vis0 = vis_init(1).tobytes()
        self.scene = scene
        self.set_sun(None)

    @property
    def readback_slots(self) -> int:
        """异步读回槽位数，即 `submit` 同时未归还的帧数上限：调用方一次提交、尚未归还的帧数超过它时 `submit` 等待归还（同一线程内
        先提交后取回会等满超时），因此 L2Sensors 按它分批提交（D3-AC-60）。"""
        return self.ring.n

    @staticmethod
    def _put(prog, k: str, v) -> None:
        with contextlib.suppress(KeyError):                               # 驱动裁掉的未使用 uniform
            prog[k].value = v

    def set_sun(self, az_el_deg: tuple[float, float] | None) -> None:
        from awr.sensorsvc.camera.actors import sun_dir

        self.sun = sun_dir(az_el_deg)
        L = tuple(float(v) for v in self.sun)
        for prog in (self.actp, self.actp_rs):
            prog["u_sun"].value = L

    def set_actors(self, actors: list[dict]) -> None:
        """磁带演员表（按 idx）→ 合批的代理网格（实例号 idx + 1、语义码）与演员尺寸（接触阴影、阴影图取景）。"""
        from awr.sensorsvc.camera.actors import SEM_CODE, proxy_mesh

        if self.actors is not None:
            self.actors.release()
        meshes, info = [], []
        for k, a in enumerate(actors):
            P, N, C = proxy_mesh(a)
            meshes.append(np.hstack([P, N, C]))
            info.append((k + 1, SEM_CODE.get(a["class"], 13)))
        self.actor_sem = [s for _i, s in info]
        self.actor_dims = np.array([[s / 1000.0 for s in a["size_mm"]] for a in actors], np.float64).reshape(-1, 3)
        self.actors = ActorBuffers(self.ctx, meshes, "3f 3f 3f", ("in_pos", "in_nrm", "in_col"), info)
        self.vis_K = len(actors) + 1
        self._vis0 = vis_init(self.vis_K).tobytes()

    def _target(self, w: int, h: int, wr: int | None = None, hr: int | None = None) -> _Target:
        key = (w, h, wr or w, hr or h)
        t = self._targets.get(key)
        if t is None:
            t = self._targets[key] = _Target(self.ctx, self.gl, *key)
        return t

    def _visible(self, cm: CameraModel, M: np.ndarray) -> np.ndarray:
        """视锥剔除（保守，全部批次一次计算）：八个角点同在某一裁剪面外侧时剔除。"""
        Xo = self.corners @ M[:3, :3].T + M[:3, 3]
        x, y, z = Xo[..., 0], Xo[..., 1], Xo[..., 2]
        out = ((z < cm.near_m).all(1) | (z > cm.far_m).all(1) | (x * cm.fx < -cm.cx * z).all(1)
               | (x * cm.fx > (cm.width - cm.cx) * z).all(1) | (y * cm.fy < -cm.cy * z).all(1) | (y * cm.fy > (cm.height - cm.cy) * z).all(1))
        return ~out

    def _shadow_light(self, act: ActorFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """投影阴影图的光源取景：正交投影中心取本帧演员脚底位置的均值，沿太阳方向后退 500 m；视野覆盖全部演员的外接球（另加 1 m），
        在 [8, 400] m 内。返回 (光源视图 4×4, 投影 4×4, 世界 → 裁剪 4×4)。"""
        pos = np.asarray(act.pos, np.float64).reshape(-1, 3)
        dims = self.actor_dims[np.asarray(act.idx, np.int64)] if len(self.actor_dims) else np.ones((len(pos), 3))
        c0 = pos.mean(0)
        p_l = c0 + self.sun * SHADOW_DIST_M
        R_l = look_at_R(p_l, c0)
        V = world_to_opt(R_l, p_l)
        Xo = pos @ V[:3, :3].T + V[:3, 3]
        rad = 0.5 * np.linalg.norm(dims, axis=1)
        span = 2.0 * float(np.max(np.maximum(np.abs(Xo[:, 0]), np.abs(Xo[:, 1])) + rad)) + 2.0
        span = min(max(span, SHADOW_SPAN_MIN_M), SHADOW_SPAN_MAX_M)
        P = ortho_reversed_z(span, SHADOW_NEAR_M, SHADOW_FAR_M)
        return V, P, P @ V

    def _shadow_map(self, base: int, mvbase: int, P: np.ndarray) -> None:
        mgl = self._mgl
        if self._sm is None:
            self._sm = self.ctx.depth_texture((SHADOW_MAP_SIZE, SHADOW_MAP_SIZE))
            self._sm.compare_func = ""
            self._sm.filter = (mgl.NEAREST, mgl.NEAREST)
            self._sm_fb = self.ctx.framebuffer(depth_attachment=self._sm)
        self._sm_fb.use()
        self._sm_fb.clear(depth=0.0)
        self.ctx.viewport = (0, 0, SHADOW_MAP_SIZE, SHADOW_MAP_SIZE)
        self.ctx.enable(mgl.DEPTH_TEST)
        self.ctx.depth_func = ">"
        self.actp["u_P"].write(P.T.astype("<f4").tobytes())
        self.actors.draw(self.actp, base, mvbase)
        self.ctx.disable(mgl.DEPTH_TEST)

    def _shade(self, tg: _Target, rcm: CameraModel, R_ws, p_w, fx: FxParams, act: ActorFrame | None, light: np.ndarray | None) -> None:
        from awr.sensorsvc.camera.proj import R_FLU_OPT

        prog = self.shadep
        flags = 0
        ncast = 0
        if fx.contact_shadow and act is not None and len(act.idx):
            cast = _fx.contact_casters(act.pos, self.actor_dims[np.asarray(act.idx, np.int64)], self.sun)
            b = cast.astype("<f4").tobytes()
            self.cast_buf.orphan(max(len(b), self.cast_buf.size))      # 每帧换新存储：不与上一帧仍在执行的着色遍同步
            self.cast_buf.write(b)
            ncast = len(cast)
            flags |= 1
        if light is not None:
            flags |= 2
            self._sm.use(4)
            prog["u_L"].write(light.T.astype("<f4").tobytes())
        if fx.fog_sigma_m > 0:
            flags |= 4
        if fx.rain_mmh > 0:
            flags |= 8
            self._put(prog, "u_rdir", _fx.rain_streak(rcm, R_ws))
            self._put(prog, "u_rdens", _fx.rain_density(fx.rain_mmh))
            self._put(prog, "u_rkey", (fx.key_rain & 0xFFFFFFFF, (fx.key_rain >> 32) & 0xFFFFFFFF))
        tg.shade_target().use()
        self.ctx.viewport = (0, 0, tg.wr, tg.hr)
        tg.mrt.col.use(1)
        tg.mrt.z.use(2)
        tg.mrt.inst.use(3)
        self.cast_buf.bind_to_storage_buffer(6)
        Rwo = np.asarray(R_ws, np.float64) @ R_FLU_OPT
        with contextlib.suppress(KeyError):
            prog["u_Rwo"].write(Rwo.T.astype("<f4").tobytes())       # 列主序
        for k, v in (("u_K", (rcm.fx, rcm.fy, rcm.cx, rcm.cy)), ("u_p", tuple(float(x) for x in p_w)), ("u_flags", flags), ("u_ncast", ncast),
                     ("u_sigma", float(fx.fog_sigma_m)), ("u_fogc", tuple(float(x) for x in _fx.FOG_LIN))):
            self._put(prog, k, v)
        self.shade_vao.render(self._mgl.TRIANGLES, vertices=3)

    def _fx_passes(self, tg: _Target, cm: CameraModel, fx: FxParams, lin, zsrc) -> None:
        def key2(k: int) -> tuple[int, int]:
            return (k & 0xFFFFFFFF, (k >> 32) & 0xFFFFFFFF)

        put = self._put
        lin.use(1)
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
            zsrc.use(2)
            for k, v in (("u_d", (fx.depth.a, fx.depth.d_min_m, fx.depth.d_max_m)), ("u_key", key2(fx.key_depth)), ("u_W", tg.w)):
                put(self.dep, k, v)
            self.dep_vao.render(self._mgl.TRIANGLES, vertices=3)

    def _query(self, k: int):
        while len(self._queries) <= k:
            self._queries.append(self.ctx.query(time=True))
        return self._queries[k]

    def submit(self, cm: CameraModel, R_ws: np.ndarray, p_w: np.ndarray, *, fx: FxParams | None = None,
               rs_nodes: list[tuple[np.ndarray, np.ndarray]] | None = None, actors: ActorFrame | None = None,
               read: tuple[str, ...] | None = None, linear: bool = False, debug: bool = False, timer: bool = False) -> RenderJob:
        """绘制一帧并提交读回，立即返回 `RenderJob`。`R_ws`、`p_w` 为 t_mid 位姿；`rs_nodes` 给出时按卷帘逐顶点投影（9 个节点，
        时间升序，覆盖读出窗口）；`actors` 给出时在静态场景之后绘制演员，并另以只渲染演员的通道得到 `instance_unocc`。

        `read` 为读回平面（缺省全部）：rgb、z（平面深度）、inst、sem、qual、unocc、zobs（L2-depth）、sub（测光小图）、vis（GPU 可见度
        统计，`RenderOut.vis`）；rgb、inst、sem 总是读回，unocc、vis、zobs、sub 只在对应输入给出时读回。`linear=True` 另读回效果链增益之前的交付域线性图像，`debug=True`
        另读回渲染域中间结果，`timer=True` 以 GL 计时查询记录 GPU 时间。"""
        mgl = self._mgl
        W, H = cm.width, cm.height
        rt = build_remap(cm) if cm.has_distortion() else None
        rcm = rt.render if rt is not None else cm
        Wr, Hr = rcm.width, rcm.height
        tg = self._target(W, H, Wr, Hr)
        rs = rs_nodes is not None
        if rs and len(rs_nodes) != RS_NODES:
            raise ValueError(f"rolling shutter needs {RS_NODES} pose nodes")
        if rs and rs_static(rs_nodes):                                   # 静止机体：退化为全局快门，输出逐字节一致（D3-AC-60）
            rs_nodes, rs = None, False
        has_act = actors is not None and self.actors is not None
        want = set(read) if read is not None else {"rgb", "z", "inst", "sem", "qual", "unocc", "zobs", "sub", "vis"}
        shaded = fx is not None and fx.shade
        items = [("rgb", W, H, "rgb8"), ("inst", W, H, "u16"), ("sem", W, H, "u8")]
        items += [(nm, W, H, f) for nm, f in (("z", "f32"), ("qual", "u8")) if nm in want]
        if "unocc" in want and has_act:
            items.append(("unocc", W, H, "u16"))
        want_vis = "vis" in want and has_act
        if want_vis:
            items.append(("vis", VIS_STRIDE * self.vis_K, 1, "u32"))
        if "zobs" in want and fx is not None and fx.depth is not None:
            items.append(("zobs", W, H, "f32"))
        if "sub" in want and fx is not None and fx.meter:
            items.append(("sub", tg.sw, tg.sh, "rgb32f"))
        if linear:
            items.append(("lin", W, H, "rgb32f"))
        if debug:
            items += [("dbg_lin_render", Wr, Hr, "rgb32f"), ("dbg_z_render", Wr, Hr, "f32"), ("dbg_inst_render", Wr, Hr, "u16"),
                      ("dbg_qual_render", Wr, Hr, "u8")] + ([("dbg_shaded", Wr, Hr, "rgb32f")] if shaded else [])
        layout, nbytes = plane_layout(items)
        t0 = time.perf_counter()
        slot = self.ring.acquire(nbytes)
        slot_q = None
        if timer:
            slot_q = self._query(slot)
            slot_q.mglo.begin()
        P = proj_reversed_z(rcm)
        Mw = world_to_opt(R_ws, p_w)
        # 演员数据：相机视图（全局快门 1 个、卷帘 9 个节点）与阴影图的光源视图
        sets_meta = None
        light = None
        if has_act:
            views = [world_to_opt(Rk, pk) for Rk, pk in rs_nodes] if rs else [Mw]
            sets = [(actors, views)]
            if fx is not None and fx.shadow_map and len(actors.idx):
                Vl, Pl, light = self._shadow_light(actors)
                sets.append((actors, [Vl]))
            sets_meta = self.actors.upload(sets)
        tg.mrt.fbo.use()
        self.ctx.viewport = (0, 0, Wr, Hr)
        tg.mrt.clear(self.gl)
        self.ctx.enable(mgl.DEPTH_TEST)
        self.ctx.disable(mgl.CULL_FACE)
        self.ctx.depth_func = ">"
        prog = self.prog_rs if rs else self.prog
        prog["u_inst"].value = 0
        prog["u_sem"].value = 0
        rs_unis = None
        if rs:
            d = tuple(float(v) for v in cm.dist)
            rs_unis = (("u_fycy", (cm.fy, cm.cy)), ("u_H", float(H)), ("u_hasdist", int(cm.has_distortion())), ("u_dist0", d[:4]), ("u_k3", d[4]))
            prog["u_P"].write(P.T.astype("<f4").tobytes())
            for k, v in rs_unis:
                self._put(prog, k, v)
        n = 0
        bound = None
        tile = None
        if not self.draws:
            vis = np.zeros(0, bool)
        elif rs:                                                         # 读出窗口首、中、末三个位姿的视锥并集
            vis = np.zeros(len(self.draws), bool)
            for Rk, pk in (rs_nodes[0], rs_nodes[RS_NODES // 2], rs_nodes[-1]):
                vis |= self._visible(rcm, world_to_opt(Rk, pk))
        else:
            vis = self._visible(rcm, Mw)
        if vis.any():                                                    # 全部瓦片的模型视图一次计算（float64），转列主序 float32
            if rs:
                mvn = gl_mat4(np.stack([mv_tiles(Rk, pk, self.tile_org) for Rk, pk in rs_nodes], 1))     # (瓦片, 节点, 4, 4)
            else:
                MV = mv_tiles(R_ws, p_w, self.tile_org)
                mvb, mvpb = gl_mat4(MV), gl_mat4(P @ MV)
        for (b, vao, vao_rs), ok, ti in zip(self.draws, vis, self.draw_tile, strict=True):
            if not ok:
                continue
            if b.tile != tile:
                if rs:
                    prog["u_mvn"].write(mvn[ti].tobytes())
                else:
                    prog["u_mv"].write(mvb[ti].tobytes())
                    prog["u_mvp"].write(mvpb[ti].tobytes())
                tile = b.tile
            tex = b.texture if b.texture is not None and b.texture < len(self.textures) else None
            if tex != bound:
                prog["u_has_tex"].value = tex is not None
                if tex is not None:
                    self.textures[tex].use(0)
                bound = tex
            (vao_rs if rs else vao).render()
            n += 1
        na = 0
        aprog = self.actp_rs if rs else self.actp
        if has_act and len(actors.idx):
            aprog["u_P"].write(P.T.astype("<f4").tobytes())
            if rs:
                for k, v in rs_unis:
                    self._put(aprog, k, v)
            base, mvbase, na = sets_meta[0]
            self.actors.draw(aprog, base, mvbase)
        self.ctx.depth_func = ">="                                       # 天空遍：只覆盖深度仍为 0 的像素
        self.sky["u_Rwo"].write(Mw[:3, :3].astype("<f4").tobytes())     # 列主序写入 R_woᵀ 的转置，即 R_wo
        self.sky["u_K"].value = (rcm.fx, rcm.fy, rcm.cx, rcm.cy)
        self.sky_vao.render(mgl.TRIANGLES, vertices=3)
        self.ctx.disable(mgl.DEPTH_TEST)
        if has_act:                                                      # 只渲染演员的通道（渲染域）
            if tg.unocc is None:
                tg.unocc = _Mrt(self.ctx, self.gl, Wr, Hr)
            tg.unocc.fbo.use()
            self.ctx.viewport = (0, 0, Wr, Hr)
            tg.unocc.clear(self.gl)
            if len(actors.idx):
                self.ctx.enable(mgl.DEPTH_TEST)
                self.ctx.depth_func = ">"
                self.actors.draw(aprog, *sets_meta[0][:2])
                self.ctx.disable(mgl.DEPTH_TEST)
        lin_r = tg.mrt.col                                               # 渲染域线性颜色（着色遍之后为 col2）
        if shaded:
            if light is not None:
                self._shadow_map(*sets_meta[1][:2], Pl)
            self._shade(tg, rcm, R_ws, p_w, fx, actors if has_act else None, light)
            lin_r = tg.col2
        src = {"col": lin_r, "z": tg.mrt.z, "inst": tg.mrt.inst, "sem": tg.mrt.sem, "qual": tg.mrt.qual,
               "unocc": tg.unocc.inst if has_act else None}
        if rt is not None:                                               # 畸变重映射到交付尺寸
            fb = tg.resolve_target()
            fb.use()
            self.ctx.viewport = (0, 0, W, H)
            for unit, t in ((1, lin_r), (2, tg.mrt.z), (3, tg.mrt.inst), (4, tg.mrt.sem), (5, tg.mrt.qual)):
                t.use(unit)
            if has_act:
                tg.unocc.inst.use(6)
            tg.remap_tex(rt).use(7)
            self._put(self.resp, "u_has_unocc", bool(has_act))
            self.res_vao.render(mgl.TRIANGLES, vertices=3)
            src = dict(zip(("col", "z", "inst", "sem", "qual", "unocc"), tg.res, strict=True))
            if not has_act:
                src["unocc"] = None
        if fx is None:
            tg.enc.use()
            self.ctx.viewport = (0, 0, W, H)
            src["col"].use(1)
            self.enc_vao.render(mgl.TRIANGLES, vertices=3)
        else:
            self._fx_passes(tg, cm, fx, src["col"], src["z"])
        if want_vis:                                                     # 交付域实例、无遮挡实例与语义 → 逐实例统计（原子归约）
            self.vis_buf.orphan(len(self._vis0))
            self.vis_buf.write(self._vis0)
            self.vis_buf.bind_to_storage_buffer(7)
            src["inst"].use(8)
            src["unocc"].use(9)
            src["sem"].use(10)
            self.visp["u_size"].value = (W, H)
            self.visp["u_K"].value = self.vis_K
            self.visp.run(-(-W // 16), -(-H // 16), 1)
            self.ctx.memory_barrier()
        if slot_q is not None:
            slot_q.mglo.end()
        tex = {"rgb": tg.rgb.glo, "inst": src["inst"].glo, "sem": src["sem"].glo, "z": src["z"].glo, "qual": src["qual"].glo,
               "zobs": tg.zobs.glo, "sub": tg.sub.glo, "lin": src["col"].glo, "dbg_lin_render": tg.mrt.col.glo, "dbg_z_render": tg.mrt.z.glo,
               "dbg_inst_render": tg.mrt.inst.glo, "dbg_qual_render": tg.mrt.qual.glo, "dbg_shaded": lin_r.glo}
        if src["unocc"] is not None:
            tex["unocc"] = src["unocc"].glo
        self.ring.issue(slot, layout, {name: tex[name] for name, *_ in items if name != "vis"}, {"vis": self.vis_buf.glo} if want_vis else None)
        t1 = time.perf_counter()
        meta = {"draw_ms": (t1 - t0) * 1e3, "batches": n, "actors": na, "remap": rt}
        return RenderJob(ReadJob(self.ring, slot, layout, tuple(name for name, *_ in items)), meta, slot_q)

    def render(self, cm: CameraModel, R_ws: np.ndarray, p_w: np.ndarray, *, linear: bool = False, fx: FxParams | None = None,
               rs_nodes: list[tuple[np.ndarray, np.ndarray]] | None = None, actors: ActorFrame | None = None, debug: bool = False) -> RenderOut:
        """同步接口：`submit` 后立即等待读回，返回可写数组（全部平面）。参数同 `submit`。"""
        job = self.submit(cm, R_ws, p_w, fx=fx, rs_nodes=rs_nodes, actors=actors, linear=linear, debug=debug)
        try:
            out = _copy_out(job.result())
        finally:
            job.release()
        return out

    def release(self) -> None:
        for t in self._targets.values():
            t.release()
        self._targets.clear()
        self.ring.close()
        if self.actors is not None:
            self.actors.release()
        self.ctx.release()
