"""固定翼协调转弯运动学与横向导引（AWR-04 §5.3；ADR-096；AWR-20 §5.1–§5.3）。

状态：位置 `p`（ENU）、空速 `V_a`、航向 `ψ`（空气系机头方位，自北顺时针）、航迹倾角 `gamma`、滚转 `φ`；对地航迹角
`χ = atan2(v_g,E, v_g,N)`。导引给出指令空速 `V_c`、期望对地航迹角 `χ_d` 与航迹角速率前馈 `χ̇_ff`、指令爬升率 `ḣ_c`：

    χ̇_cmd = χ̇_ff + k_χ·wrap(χ_d − χ)
    ψ̇_cmd = χ̇_cmd·V_g / (V_a·cos(χ − ψ))           （风中航迹角速率与航向角速率的关系）
    φ_c   = atan(V_a·ψ̇_cmd / g)                     （无风时即 atan(V_g²·κ/g)）
    V̇_a = clamp((V_c − V_a)/τ_V, −a_dec, a_acc)，V_a ∈ [V_min, V_max]
    φ̇   = clamp((φ_c − φ)/τ_φ, −p_max, p_max)，|φ| ≤ φ_max(V_a) = min(φ_max, acos((V_s/V_a)²))
    ψ̇   = g·tan φ / V_a                              （协调转弯，无侧滑）
    ḣ   = clamp(ḣ_c, −sink_max, climb_max)，gamma = asin(ḣ/V_a)
    v_g = V_a·[cos gamma·sin ψ, cos gamma·cos ψ, sin gamma] + w   （w 为 M07 风场在机体处的取值，ENU）

横向导引为向量场（直线：`χ_d = χ_t − χ_∞·(2/π)·atan(k_l·e)`，e 为航迹右侧偏差；圆：`χ_d = θ + λ·(π/2 + atan(k_o·(d − R)/R))`，θ 为
自圆心的方位、λ = +1 顺时针、−1 逆时针，入圈后 `χ̇_ff = λ·V_g/R`）。纯函数，按 slot 逐个调用；不抽随机数，不读墙钟。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    "CHI_INF_RAD",
    "K_CHI",
    "K_HDOT",
    "K_LINE",
    "K_ORBIT",
    "FwKin",
    "G",
    "circle_field",
    "line_field",
    "omega_flu",
    "quat_xyzw",
    "step",
    "wrap",
]

G = 9.80665
K_CHI = 0.6  # 1/s 航迹角误差增益
K_LINE = 1.0 / 40.0  # 1/m 直线向量场增益
CHI_INF_RAD = math.radians(60.0)  # 远离航线时的最大切入角
K_ORBIT = 2.0  # 圆向量场增益
K_HDOT = 0.5  # 1/s 高度误差 -> 爬升率
COS_MIN = 0.2  # cos(χ − ψ) 下限（V_a 接近风速时的保护）


def wrap(a: float) -> float:
    return (a + math.pi) % (2.0 * math.pi) - math.pi


@dataclass
class FwKin:
    """单机运动学状态（ENU，m、m/s、rad）。"""

    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    va: float = 0.0
    psi: float = 0.0
    phi: float = 0.0
    gamma: float = 0.0
    hdot: float = 0.0
    vge: float = 0.0
    vgn: float = 0.0
    vgu: float = 0.0
    psi_dot: float = 0.0
    phi_dot: float = 0.0
    ae: float = 0.0
    an: float = 0.0
    au: float = 0.0

    @property
    def vg(self) -> float:
        return math.hypot(self.vge, self.vgn)

    @property
    def chi(self) -> float:
        return math.atan2(self.vge, self.vgn) if self.vg > 1e-6 else self.psi


def line_field(x: float, y: float, ax: float, ay: float, bx: float, by: float) -> tuple[float, float, float]:
    """直线 A→B 的向量场：返回 (χ_d, 航迹右侧偏差 e, 沿航迹距 B 的剩余长度)。"""
    chit = math.atan2(bx - ax, by - ay)
    s, c = math.sin(chit), math.cos(chit)
    e = (x - ax) * c - (y - ay) * s
    along = (bx - x) * s + (by - y) * c
    return chit - CHI_INF_RAD * (2.0 / math.pi) * math.atan(K_LINE * e), e, along


def circle_field(x: float, y: float, cx: float, cy: float, r: float, lam: float, vg: float) -> tuple[float, float, float, float]:
    """圆（圆心 c、半径 r、方向 λ）的向量场：返回 (χ_d, χ̇_ff, 径向偏差 d − r, 自圆心的方位 θ)。"""
    dx, dy = x - cx, y - cy
    d = math.hypot(dx, dy)
    th = math.atan2(dx, dy)
    chi_d = th + lam * (math.pi / 2.0 + math.atan(K_ORBIT * (d - r) / max(r, 1e-6)))
    ff = lam * vg / r if abs(d - r) < 0.5 * r else 0.0
    return chi_d, ff, d - r, th


def step(k: FwKin, *, va_c: float, chi_d: float | None, chi_ff: float, hdot_c: float, wind: tuple[float, float, float], dt: float,
         v_min: float, v_max: float, v_stall: float, phi_max: float, roll_rate_max: float, tau_v: float, tau_phi: float,
         acc_max: float, dec_max: float, climb_max: float, sink_max: float, psi_dot_cmd: float | None = None,
         clamp_va: bool = True) -> None:
    """积分一步（原地更新 k）。`chi_d` 为 None 时用 `psi_dot_cmd`（手动三通道）或保持航向。"""
    we, wn, wu = wind
    vg = k.vg
    if chi_d is not None:
        chi = k.chi
        chidot = chi_ff + K_CHI * wrap(chi_d - chi)
        c = math.cos(chi - k.psi)
        psidot_c = chidot * max(vg, 0.1) / (max(k.va, 0.1) * max(c, COS_MIN))
    else:
        psidot_c = 0.0 if psi_dot_cmd is None else float(psi_dot_cmd)
    va = max(k.va, 0.1)
    phi_c = math.atan(va * psidot_c / G)
    r = v_stall / va
    pm = min(phi_max, math.acos(min(1.0, r * r))) if r < 1.0 else 0.0
    phi_c = max(-pm, min(pm, phi_c))
    dphi = max(-roll_rate_max, min(roll_rate_max, (phi_c - k.phi) / tau_phi)) * dt
    k.phi = max(-pm, min(pm, k.phi + dphi))
    k.phi_dot = dphi / dt if dt > 0 else 0.0
    dva = max(-dec_max, min(acc_max, (va_c - k.va) / tau_v)) * dt
    k.va = k.va + dva
    if clamp_va:
        k.va = max(v_min, min(v_max, k.va))
    va = max(k.va, 0.1)
    k.psi_dot = G * math.tan(k.phi) / va
    k.psi = wrap(k.psi + k.psi_dot * dt)
    k.hdot = max(-sink_max, min(climb_max, hdot_c))
    k.gamma = math.asin(max(-1.0, min(1.0, k.hdot / va)))
    cg = math.cos(k.gamma)
    vge = va * cg * math.sin(k.psi) + we
    vgn = va * cg * math.cos(k.psi) + wn
    vgu = k.hdot + wu
    if dt > 0:
        k.ae, k.an, k.au = (vge - k.vge) / dt, (vgn - k.vgn) / dt, (vgu - k.vgu) / dt
    k.vge, k.vgn, k.vgu = vge, vgn, vgu
    k.x += vge * dt
    k.y += vgn * dt
    k.z += vgu * dt


def _qmul(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by, aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw, aw * bw - ax * bx - ay * by - az * bz)


def quat_xyzw(psi: float, gamma: float, phi: float) -> tuple[float, float, float, float]:
    """WORLD(ENU)←FLU 四元数 [x, y, z, w]：偏航 `π/2 − ψ`（ENU，逆时针），俯仰取航迹倾角 gamma（抬头为正，绕 +y 转 −gamma），
    滚转 φ（右翼下沉为正，绕 +x 转 +φ）。"""
    yaw = math.pi / 2.0 - psi
    qz = (0.0, 0.0, math.sin(yaw / 2.0), math.cos(yaw / 2.0))
    qy = (0.0, math.sin(-gamma / 2.0), 0.0, math.cos(-gamma / 2.0))
    qx = (math.sin(phi / 2.0), 0.0, 0.0, math.cos(phi / 2.0))
    return _qmul(_qmul(qz, qy), qx)


def omega_flu(q: tuple[float, float, float, float], psi_dot: float, phi_dot: float) -> tuple[float, float, float]:
    """机体 FLU 角速度：世界系绕天轴的角速度 `−ψ̇` 转到机体系，加滚转角速度。"""
    x, y, z, w = q
    # R^T·e_z（世界 z 轴在机体系的分量）= R 的第三行
    r20 = 2.0 * (x * z - w * y)
    r21 = 2.0 * (y * z + w * x)
    r22 = 1.0 - 2.0 * (x * x + y * y)
    wz = -psi_dot
    return (r20 * wz + phi_dot, r21 * wz, r22 * wz)
