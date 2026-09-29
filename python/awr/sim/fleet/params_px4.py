"""PX4 v1.18 控制参数默认值（M08 §6.6；g08 §4、r20 §3.3–§3.5）。限速配置可覆盖标"可覆盖"的项（ProfileTable 的 limits 表）。"""

from __future__ import annotations

import math

G = 9.80665
RHO0 = 1.225
K_RHO = 1.0

MPC_XY_P = 0.95
MPC_Z_P = 1.0
MPC_XY_VEL_P_ACC = 1.8
MPC_XY_VEL_I_ACC = 0.4
MPC_XY_VEL_D_ACC = 0.2
MPC_Z_VEL_P_ACC = 4.0
MPC_Z_VEL_I_ACC = 2.0
MPC_Z_VEL_D_ACC = 0.0
MPC_XY_VEL_MAX = 12.0  # 可覆盖
MPC_XY_CRUISE = 5.0  # 可覆盖
MPC_Z_VEL_MAX_UP = 3.0
MPC_Z_VEL_MAX_DN = 1.5
MPC_Z_V_AUTO_UP = 3.0
MPC_Z_V_AUTO_DN = 1.5
MPC_ACC_HOR = 3.0  # 可覆盖
MPC_ACC_UP_MAX = 4.0
MPC_ACC_DOWN_MAX = 3.0
MPC_JERK_AUTO = 4.0
MPC_XY_ERR_MAX = 2.0
MPC_Z_ERR_MAX = 1.0
MPC_TILTMAX_AIR = 45.0
MPC_THR_MIN = 0.12
MPC_THR_MAX = 1.0
MPC_THR_XY_MARG = 0.3
MC_ROLL_P = 4.0
MC_PITCH_P = 4.0
MC_YAW_P = 2.8
MC_ROLLRATE_MAX = 220.0
MC_PITCHRATE_MAX = 220.0
MC_YAWRATE_MAX = 200.0  # 可覆盖
MPC_YAWRAUTO_MAX = 60.0
MPC_TKO_SPEED = 1.5
MIS_TAKEOFF_ALT = 2.5
COM_SPOOLUP_TIME = 1.0
MPC_TKO_RAMP_T = 3.0
MPC_LAND_SPEED = 0.7
MPC_LAND_CRWL = 0.3
MPC_LAND_ALT1 = 10.0
MPC_LAND_ALT2 = 5.0
MPC_LAND_ALT3 = 1.0
NAV_ACC_RAD = 2.0
RTL_RETURN_ALT = 30.0
RTL_DESCEND_ALT = 10.0
COM_DISARM_LAND = 2.0

TILTMAX_RAD = math.radians(MPC_TILTMAX_AIR)

# ---------------------------------------------------------------- 本模块补充的常量（M08 §6.6 表中的其余行）
LAND_FAST_SPEED = 1.5  # AGL > MPC_LAND_ALT1 时的下降速度（= MPC_Z_V_AUTO_DN），m/s
TOUCHDOWN_RAMP_S = 1.0  # 触地后推力上限斜坡到 THR_MIN 的时长（FR-026）
ELAND_SPEED = 0.5  # ELAND 恒定下降速度（g08 §7.2）
DESCENT_FF_SPEED = 1.0  # FAILSAFE/DESCENT 垂直前馈（g08 §7.2）
VEL_AXIS_LOCK_MPS = 0.09  # Velocity 零速轴锁定阈值（g04 §6.3）
VEL_AXIS_DRIFT_M = 0.04  # 零速轴漂移阈值
VEL_AXIS_PULL = 1.8  # 零速轴回拉增益，1/s
VEL_FREE_MARGIN_M = 2.0  # Velocity 方向限速：vmax_from_dist(J, a, max(d_free − 2 m, 0))
VEL_FREE_MAX_M = 200.0  # free_distance 查询上限（本文设定：200 m 处限速约 30 m/s）
LAND_DETECT_VZ = 0.25  # 通用触地判据（r20 §3.5）
LAND_DETECT_VXY = 1.5
LAND_DETECT_OMEGA = math.radians(20.0)
LAND_DETECT_THR = 0.3  # 推力 < 0.3·hover
SIH_ALIGN_DELAY_S = 0.12  # SIH 指令链路对齐延迟（只用于回归比较，不写入 Mock）
