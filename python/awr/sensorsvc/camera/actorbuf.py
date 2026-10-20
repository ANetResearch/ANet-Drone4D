"""演员代理几何的合批绘制（M25 §6.4.4；D3-AC-11 吞吐）。

全部演员的代理网格（非索引三角形）合并为一个顶点缓冲，逐顶点附加演员序号 `in_aid`；每帧以三个 SSBO 给出逐演员数据，一次绘制
调用画出全部演员（此前每名演员一次绘制、6–8 次 uniform 写入，40 余名演员时每遍 CPU 提交约 1.5 ms）：

- `ActMv`（binding 2）：模型视图矩阵（光学帧，列主序 mat4），按"集合"排列，集合内为 [演员][节点]（全局快门 1 个节点、卷帘 9 个）；
- `ActRw`（binding 3）：世界旋转（绕 z 偏航，mat4 左上 3×3），用于法向；
- `ActInfo`（binding 4）：uvec4（实例号 idx + 1、语义码、本帧是否绘制、0）。

一个集合对应一次绘制所用的视图（相机视图、阴影图的光源视图或热成像的某个子帧）；着色器以 `u_mvbase`（集合在 ActMv 中的起点，
mat4 个数）与 `u_base`（集合在 ActRw、ActInfo 中的起点）定位本演员的数据。本帧未出现的演员 `ActInfo.z = 0`，顶点着色器把其顶点
放到裁剪体之外（退化三角形，不产生片元）。模型视图矩阵在 float64 中按"视图 · 模型"计算后转为 float32（与逐演员绘制时的
uniform 相同的数值路径）。
"""

from __future__ import annotations

import numpy as np

__all__ = ["GLSL_ACTOR_SSBO", "ActorBuffers", "actor_models"]

GLSL_ACTOR_SSBO = """
layout(std430, binding = 2) readonly buffer ActMv { mat4 act_mv[]; };
layout(std430, binding = 3) readonly buffer ActRw { mat4 act_rw[]; };
layout(std430, binding = 4) readonly buffer ActInfo { uvec4 act_info[]; };
uniform int u_base;
uniform int u_mvbase;
"""


def actor_models(pos: np.ndarray, yaw: np.ndarray) -> np.ndarray:
    """(n, 4, 4) float64 模型矩阵：绕 z 偏航 yaw 后平移到 pos（世界 ENU）。"""
    pos = np.asarray(pos, np.float64).reshape(-1, 3)
    c, s = np.cos(np.asarray(yaw, np.float64)), np.sin(np.asarray(yaw, np.float64))
    M = np.zeros((len(pos), 4, 4))
    M[:, 0, 0], M[:, 0, 1], M[:, 1, 0], M[:, 1, 1] = c, -s, s, c
    M[:, 2, 2] = M[:, 3, 3] = 1.0
    M[:, :3, 3] = pos
    return M


class ActorBuffers:
    """演员合批的顶点缓冲与逐帧 SSBO。`meshes[i]` 为演员 i 的逐顶点属性 (n_i, k) float32（按 `fmt` 排列），`info0[i]` 为
    (实例号, 语义码)。`vao(prog)` 返回绑定合并缓冲的顶点数组（属性名 `attrs` 加 `in_aid`）。"""

    def __init__(self, ctx, meshes: list[np.ndarray], fmt: str, attrs: tuple[str, ...], info0: list[tuple[int, int]]) -> None:
        self.ctx = ctx
        self.n = len(meshes)
        self.fmt, self.attrs = fmt, tuple(attrs)
        if self.n:
            data = np.concatenate([np.hstack([np.asarray(m, np.float32), np.full((len(m), 1), i, np.float32)]) for i, m in enumerate(meshes)])
        else:
            data = np.zeros((3, int(sum(int(t[:-1] or 1) for t in fmt.split())) + 1), np.float32)
        self.vertices = len(data) if self.n else 0
        self.vbo = ctx.buffer(data.astype("<f4").tobytes())
        self._vaos: dict[int, object] = {}
        self.info0 = np.zeros((max(self.n, 1), 4), np.uint32)
        for i, (inst, sem) in enumerate(info0):
            self.info0[i, 0], self.info0[i, 1] = int(inst), int(sem)
        self.mv_buf = ctx.buffer(reserve=64)
        self.rw_buf = ctx.buffer(reserve=64)
        self.info_buf = ctx.buffer(reserve=16)
        self.sets = 0

    def vao(self, prog):
        v = self._vaos.get(id(prog))
        if v is None:
            v = self._vaos[id(prog)] = self.ctx.vertex_array(prog, [(self.vbo, self.fmt + " 1f", *self.attrs, "in_aid")])
        return v

    @staticmethod
    def _grow(buf, nbytes: int):
        """每帧换新存储（orphan）后写入：上一帧仍在 GPU 上执行的绘制继续读旧存储，写入不与之同步。"""
        buf.orphan(max(nbytes, buf.size))
        return buf

    def upload(self, sets: list[tuple[object, list[np.ndarray]]]) -> list[tuple[int, int, int]]:
        """逐集合给出 (ActorFrame 或 None, [视图矩阵 4×4（world → 光学帧）, ...])；写入三个 SSBO，返回每个集合的
        (u_base, u_mvbase, 本集合绘制的演员数)。"""
        n = max(self.n, 1)
        mv_parts, rw_parts, info_parts, out = [], [], [], []
        mvbase = 0
        for k, (act, views) in enumerate(sets):
            nodes = len(views)
            mv = np.zeros((n, nodes, 4, 4), np.float32)
            rw = np.zeros((n, 4, 4), np.float32)
            info = self.info0.copy()
            drawn = 0
            if act is not None and len(act.idx) and self.n:
                idx = np.asarray(act.idx, np.int64)
                M = actor_models(act.pos, act.yaw)
                V = np.stack([np.asarray(v, np.float64) for v in views])            # (nodes, 4, 4)
                mv[idx] = np.matmul(V[None, :, :, :], M[:, None, :, :]).astype(np.float32)
                rw[idx, :3, :3] = M[:, :3, :3]
                rw[idx, 3, 3] = 1.0
                info[idx, 2] = 1
                drawn = len(idx)
            mv_parts.append(np.ascontiguousarray(mv.transpose(0, 1, 3, 2)))       # 列主序
            rw_parts.append(np.ascontiguousarray(rw.transpose(0, 2, 1)))
            info_parts.append(info)
            out.append((k * n, mvbase, drawn))
            mvbase += n * nodes
        mvb = np.concatenate([p.reshape(-1) for p in mv_parts]).astype("<f4").tobytes()
        rwb = np.concatenate([p.reshape(-1) for p in rw_parts]).astype("<f4").tobytes()
        ifb = np.concatenate(info_parts).astype("<u4").tobytes()
        for buf, b in ((self.mv_buf, mvb), (self.rw_buf, rwb), (self.info_buf, ifb)):
            self._grow(buf, len(b)).write(b)
        self.sets = len(sets)
        return out

    def bind(self) -> None:
        self.mv_buf.bind_to_storage_buffer(2)
        self.rw_buf.bind_to_storage_buffer(3)
        self.info_buf.bind_to_storage_buffer(4)

    def draw(self, prog, base: int, mvbase: int) -> None:
        if not self.vertices:
            return
        prog["u_base"].value = int(base)
        prog["u_mvbase"].value = int(mvbase)
        self.bind()
        self.vao(prog).render(vertices=self.vertices)

    def release(self) -> None:
        for v in self._vaos.values():
            v.release()
        self._vaos.clear()
        for b in (self.vbo, self.mv_buf, self.rw_buf, self.info_buf):
            b.release()
