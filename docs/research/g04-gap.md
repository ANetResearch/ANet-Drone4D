# G4 深挖：DroneState、命令与状态机语义统一（映射表、命令矩阵、Full64 位定稿）

> 研究单元：g04（补充深挖，对应 00-index §9 G4）｜ 日期：2026-09-28 ｜ 关联单元：r27、r20、r21、r24、r19、r22、d05、r08
>
> 读码范围（均为本机 clone，只读）：
> - PX4-Autopilot `b3e343c`（2026-09-27）：`msg/versioned/VehicleStatus.msg`、`src/modules/commander/{Commander.cpp, px4_custom_mode.h, commander_params.yaml, failsafe/framework.h}`、`src/modules/mavlink/streams/{HEARTBEAT,EXTENDED_SYS_STATE,CURRENT_MODE}.hpp`、`src/modules/mavlink/mavlink_main.cpp`、`src/modules/flight_mode_manager/tasks/Orbit/FlightTaskOrbit.cpp`
> - MAVSDK `34d4995`（2026-09-28）：`cpp/src/mavsdk/core/{px4_custom_mode.hpp, mavlink_command_sender.{hpp,cpp}, mavsdk_impl.hpp}`、`plugins/action/action_impl.cpp`
> - MAVLink `message_definitions/v1.0/common.xml`（MAV_RESULT、MAV_LANDED_STATE、DO_ORBIT/DO_REPOSITION/DO_PAUSE_CONTINUE）
> - Prometheus `5dcd8cf`（2025-11-21）：`Modules/uav_control/{src/uav_controller.cpp, include/uav_controller.h}`、`Modules/common/prometheus_msgs/msg/{UAVControlState,UAVCommand,UAVSetup,UAVState}.msg`、`Modules/communication/{shard/include/Struct.hpp, src/uav_basic_topic.cpp}`
> - MRS（`.cache/research/r24/`）：`mrs_msgs/msg/uav_managers/{UavManagerDiagnostics,ControlManagerDiagnostics,EstimationDiagnostics}.msg`、`TrackerStatus.msg`、`mrs_uav_managers/src/control_manager/control_manager.cpp`、`estimation_manager/estimation_manager.cpp`
> - ANetCore v0.14.0：`/data/projs/anet-oss/ANetCore/effect/effect.go`
>
> 原型：`.cache/research/g04/state_model.py`。内容包括状态枚举、位打包、PX4 与 Prometheus 推导、Mock 显示仿真、准入矩阵、Full64/Lite32 numpy dtype。自测对 41,800 组输入穷举，全部通过：推导是全函数（任何输入都有定义的输出），黄金字节向量全部命中，Mock 仿真的有损点只有 9 处，且都已列出。运行命令：`/data/projs/anet-drone/.venv/bin/python .cache/research/g04/state_model.py --matrix`。

---

## 0. 结论速览

1. **状态分成七根正交的轴**：生命周期、飞行相位、原生飞控态、控制权、定位、任务、调用与效果。其中 **FlightState 是全系统唯一的"飞行相位"**，其他轴不允许塞进它。例如 Pause 属于任务轴（mission=PAUSED），此时飞行相位仍是 FLYING/HOVER。r22 的 `ACTIVE` 不再作为生命周期状态，它只是 `READY ∧ armed` 的显示标签。
2. **FlightState 定稿为 14 个值**：`0 UNKNOWN`，加上 r24 的 13 态，数值冻结。另加 3 位子模式，与状态共用 Full64 的 byte 2（低 5 位是状态，高 3 位是子模式）。r24 原型 `mrs_mock.py` 里的 `FS` 枚举（含 IDLE、EHOVER，编号 0–10）作废，需要按本文重新编号。
3. **Full64 byte 3（flags）定稿**：bit0 ARMED、bit1 IN_AIR、bit2 LOC_OK、bit3 FAILSAFE、bit4 GCS_LINK、bit5 FCU_LINK、bit6 LOC_DEGRADED、bit7 ALERT。原来的 `rtk_fix` 移到 `state_ext.gnss`，`simulated` 移到 advertise 的 `agents` 表，因为二者都是低频或静态信息。
4. **Full64 byte 7 由 `authority` 改名为 `ctrl`**：owner（3 位）、locked（1 位）、native（2 位）、pose_src（2 位）。native 的取值与 Prometheus ROS 版 `UAVControlState` 的 0..3（INIT/MANUAL/COMMAND/LAND）数值相同，PX4 按 nav_state 归一到同一刻度。Lite32 的 byte 31 `_reserved` 同样改为 `ctrl`。两个 schema 更名为 `awr.DroneState64.v1` 和 `awr.SwarmLite32.v1`。
5. **PX4 推导只需要三条报文**：HEARTBEAT（`base_mode` 的 armed 位、`custom_mode`、`system_status`）、EXTENDED_SYS_STATE、CURRENT_MODE。`system_status` 本身已经编码了 failsafe（CRITICAL）、prearm 是否通过（STANDBY 或 UNINIT）以及 kill/termination（FLIGHT_TERMINATION），见 `HEARTBEAT.hpp` L107–125。CURRENT_MODE 同时携带显示模式和用户意图模式（intended），因此可以检测"ACK 已接受，但被 failsafe 挡住没进模式"的情况。SIH 需要额外把 CURRENT_MODE 的发送间隔设成 4 Hz，默认只有 0.5 Hz。
6. **PX4 返回 `ACK=ACCEPTED` 只表示"模式意图被接受"**，不代表动作已经发生，效果必须靠读回确认。源码里有三个实例：
   - Orbit 半径超出 `[1, MC_ORBIT_RAD_MAX=1000]` 时，commander 仍然回 ACCEPTED，而 FlightTaskOrbit 随后报 "Orbit radius limit exceeded"；
   - `DO_ORBIT` 的 param4（圈数）被忽略；
   - `MAV_CMD_DO_PAUSE_CONTINUE` 没有 handler，走默认分支，返回 UNSUPPORTED。
7. **Prometheus 有六个硬坑**，均已在源码中确认：
   - ① `control_state` 在线上按 ROS 枚举 0..3 编码，`Struct.hpp` 里的 0..4 是错的；
   - ② `SET_CONTROL_MODE` 只认 `"COMMAND_CONTROL"` 这一个字符串，并且要求已解锁；
   - ③ 在 COMMAND 和 RC_POS 状态下，100 Hz 主循环每一拍都会重新请求 OFFBOARD；INIT 状态强制 POSCTL，LAND 状态强制 AUTO.LAND。因此用 `SET_PX4_MODE("AUTO.RTL")` 会被立刻改回去，**RTL 只能由 Gateway 仿真**；
   - ④ 不在 COMMAND 时下发的 UAVCommand 会被丢弃，同时命令被重置为 `Init_Pos_Hover`。之后一旦进入 COMMAND，飞机会飞回起飞点上方。**Gateway 必须先确认 `control_state==2` 再发**；
   - ⑤ 速度指令会一直保持，机上没有超时，Gateway 必须用 500 ms 看门狗补发 `Current_Pos_Hover`；
   - ⑥ 没有原生 ACK。能拿到的只有 UAVCommand 回显（带 `Command_ID`，但不带 `Control_Level`），以及回显里被 PX4 `target_local` 覆盖后的 `position_ref`，后者可作为 V2 级读回。
8. **命令矩阵**（10 个命令 × 3 类后端，§6）：
   - Mock 全部原生支持；
   - SIH 除两处外全部原生：Pause 用"切 LOITER，恢复时切回 MISSION"实现；Orbit 的圈数由 Gateway 计数；
   - Prometheus 原生支持 Takeoff、Land、GoTo、Hover、Velocity、SafetyStop（即 ABSOLUTE 锁）；RTL、Orbit、FollowPath、Pause 由 Gateway 仿真；**不支持** Kill，也不支持 GoTo 的速度参数（进入 COMMAND 时 `MPC_XY_VEL_MAX` 被设为 1.0 m/s）。
9. **ACK 统一为一个调用生命周期**：`accepted`（准入通过且拿到原生确认）→ `running`（状态读回符合预期）→ `succeeded`、`failed` 或 `canceled`，另有 `rejected` 与 `timeout` 两个出口。每一帧都附带 `effect{status, verify_trust}`，与 ANet 五态一一对应：
   - accepted 和 running：UNVERIFIED，信任级别分别为 V1、V2；
   - succeeded：OK，读回为 V2，仿真真值为 V4 并标 `simulated`；
   - Gateway 准入拒绝或超时：UNAVAILABLE；
   - 原生拒绝或执行失败：FAILED；
   - 被新命令取代：UNVERIFIED，原因为 superseded。
10. **对其他单元的修订清单**见 §10。合入后，00-index 的 C12 与 G4 可以关闭。

---

## 1. 七轴状态模型

| 轴 | 规范枚举 | 计算者 | 线上位置 | 频率 |
|---|---|---|---|---|
| **A 生命周期** | PENDING、PROVISIONING、STARTING、BOOTED、READY、DEGRADED、LOST、RESTARTING、FAILED、DRAINING、STOPPED、REMOVED（r22，去掉 ACTIVE） | Orchestrator：`sim/orchestrator/lifecycle.py` | 事件 `sim.vehicle.state`；`state_ext.lifecycle`；投影到 flags.FCU_LINK 和 UNKNOWN 的子模式 | 变化即推 |
| **B 飞行相位** | FlightState（14 值）+ 子模式（§3） | Gateway 的 StateFuser。Mock 直接取 Safety FSM | Full64 与 Lite32 的 byte 2 | 30–50 Hz / 10–20 Hz |
| **C 原生飞控态** | PX4：arming、nav_state、nav_state_user_intention、landed_state、system_status。Prometheus：control_state、mode、armed、failsafe、Agent_CMD、Move_mode、Command_ID。MRS：UavManager.state、active_tracker、flying_normally | 各 Adapter | 原始值放 `state_ext.px4`、`.prom`、`.mrs`；归一化后写入 ctrl.native | 1–2 Hz，变化即推 |
| **D 控制权** | lease{owner, priority, ttl}、lock、native 等级 | Gateway LeaseManager：`sim/core/authority.py` | ctrl byte；`state_ext.lease` | 位随状态帧；详情变化即推 |
| **E 定位** | NO_MAP、WAIT_INIT、ALIGNING、TRACKING、DEGRADED、LOST、OUT_OF_MAP（r08）；GNSS fix 0..8 | LocalizationService 或 Adapter | flags.LOC_OK、flags.LOC_DEGRADED；`state_ext.loc` | 1–2 Hz |
| **F 任务** | A2A 七态；mission ∈ {IDLE, RUNNING, PAUSED, DONE, ABORTED} | Mission 引擎、Agent Runtime | Full64 的 mission_item；`state_ext.mission`；`agent/tasks` | 变化即推 |
| **G 调用与效果** | 调用状态 status ∈ {accepted, rejected, running, succeeded, failed, canceled, timeout}；效果 effect ∈ {OK, UNVERIFIED, FAILED, UNAVAILABLE}；信任级别 V0–V4 | CommandEngine：`sim/core/command.py` | RPC 的 `result` 与 `progress` 帧（§7） | 事件 |

**规则**

- **R1 轴正交。** 一个现象只能落在一根轴上。例如"GCS 链路丢失"：链路本身落在 D/A 轴的位上，由它引起的动作（HOLD/LINK_LOSS）落在 B 轴，由它引起的任务中断落在 F 轴。
- **R2 单一权威。** 后端是 PX4 或 Prometheus 时，B 轴由原生态推导。Supervisor 只能以"覆盖层"的方式叠加自己的动作（CORRECTING、HOLD 的若干子模式），并且必须先让原生态进入对应模式（例如 LOITER 或 reposition）。原生态严重度更高时，以原生态为准（§8.2）。
- **R3 热路径只放三样东西**：B/D/E 轴的状态位，以及 F 轴的 `mission_item`。原生原始值一律放进 `state_ext`。
- **R4 未知值约定**：枚举未知写 0，浮点未知写 NaN。`flight_state=UNKNOWN` 时必须同时满足 `FCU_LINK=0`，或者生命周期不在 READY。

---

## 2. 源码核实的事实（设计依据）

| # | 事实 | 出处 | 影响 |
|---|---|---|---|
| F1 | VehicleStatus 为 v4。nav_state 取值 0–30，新增 6 POSITION_SLOW、7 GUIDED_COURSE、8 ALTITUDE_CRUISE、9 MANUAL_PARKING（r20 的表里没有）。另有 `nav_state_user_intention`（L29）、`nav_state_display`（L66）、`executor_in_charge`（L65）、`failsafe_defer_state`（L90）、`gcs_connection_lost`（L93）、`pre_flight_checks_pass`（L125） | `msg/versioned/VehicleStatus.msg` | §4.2 的全值映射 |
| F2 | 部分 nav_state 的 custom_mode 编码不直观：ORBIT = POSCTL(3)/sub 1；POSITION_SLOW = POSCTL/sub 2；DESCEND = AUTO(4)/sub 20；GUIDED_COURSE = AUTO/sub 19；TERMINATION 的 main=10；EXTERNALn = AUTO/sub 11–18。**MAVSDK 自带的 `px4_custom_mode.hpp` 已过期**：没有 POSCTL 子模式，没有 DESCEND 和 EXTERNAL，还保留了已删除的 RTGS | PX4 `px4_custom_mode.h`；MAVSDK `core/px4_custom_mode.hpp` | 解码表必须照 PX4 头文件生成（原型 `NAV_TO_CM`） |
| F3 | HEARTBEAT 的 `system_status`：已解锁时，failsafe 为 CRITICAL(5)，否则 ACTIVE(4)；未解锁时，prearm 通过为 STANDBY(3)，否则 UNINIT(0)；kill、termination、lockdown 一律为 FLIGHT_TERMINATION(8)。`custom_mode` 取自 `nav_state_display`。发送频率固定 1 Hz | `mavlink/streams/HEARTBEAT.hpp` L104–130；`mavlink_main.cpp` L2633 | flags.FAILSAFE 和 DISARMED 子模式可以直接从这里得到 |
| F4 | CURRENT_MODE 同时带 `custom_mode`（显示模式）和 `intended_custom_mode`（`nav_state_user_intention`），默认 0.5 Hz。EXTENDED_SYS_STATE 在 onboard 模式下 5 Hz，在 normal 模式下 1 Hz。**landed_state 只有在 auto 模式且 setpoint 类型为 TAKEOFF 或 LAND 时**才会报 TAKEOFF(3) 或 LANDING(4)，其余情况只报 ON_GROUND 或 IN_AIR | `streams/CURRENT_MODE.hpp` L67–68；`EXTENDED_SYS_STATE.hpp`；`mavlink_main.cpp` L1632/1644/1723/1729 | 读回时延（§7.3）；IN_AIR 的推导 |
| F5 | commander 的 ACK 语义：绝大多数命令在 `_user_mode_intention.change()` 成功时就回 ACCEPTED，失败回 TEMPORARILY_REJECTED。具体到各命令：<br>• `DO_REPOSITION` 带 CHANGE_MODE 位时进入 AUTO_LOITER（如果当前是 GUIDED_COURSE 且没给经纬度则保持原模式）；不带该位时，只有当前已在 LOITER 才 ACCEPTED，否则 DENIED；<br>• `NAV_LAND` 使用 `force=true`，失效保护状态下也能切入；<br>• `DO_ORBIT` 进入 ORBIT，固定翼改为进入 LOITER；<br>• `MISSION_START` 同时切模式并解锁 | `Commander.cpp` L878–913、L930–1083、L1097–1133、L1199–1260、L1276–1304、L1317–1351 | §6、§7 |
| F6 | `VEHICLE_CMD_DO_PAUSE_CONTINUE` 只在 msg 里有定义，commander、navigator、mavlink 中都没有 handler，默认返回 `VEHICLE_CMD_RESULT_UNSUPPORTED` | 对 `src/` 做 grep 的结果；`Commander.cpp` L874 | Pause 在 PX4 上用 LOITER 代替 |
| F7 | FlightTaskOrbit 只读取 param1/2/3/5/6/7，**param4（圈数，单位 rad）被忽略**。半径不在 `[1, MC_ORBIT_RAD_MAX]`（默认 1000）范围内时，只打一条 critical log 并返回 `success=false`，而此前 commander 已经回过 ACCEPTED。param1 为正表示顺时针 | `FlightTaskOrbit.cpp` L60–90；`.hpp` L74；`flight_task_orbit_params.yaml` | Gateway 在准入阶段检查半径，并自己数圈 |
| F8 | PX4 默认参数：`COM_DISARM_LAND=2.0` s；`COM_DISARM_PRFLT=10.0` s；`COM_OF_LOSS_T=1.0` s；`COM_OBL_RC_ACT=0`（Position，需要遥控器）；`COM_DL_LOSS_T=10` s；`NAV_DLL_ACT=0`（禁用）；`COM_LOW_BAT_ACT=0`（只告警） | `commander/commander_params.yaml` | SIH 的参数覆写（§6.3） |
| F9 | MAV_RESULT 取值 0..10，其中 10 为 NOT_IN_CONTROL，与控制权语义对应。MAVSDK 的默认超时是 0.5 s，重试 3 次；收到 IN_PROGRESS 后把超时放宽到 3.0 s | `common.xml`；`mavsdk_impl.hpp` L197；`mavlink_command_sender.hpp` L26、`.cpp` L268–289 | §7.3 的时限 |
| F10 | Prometheus 的 `control_state` 有两套枚举：ROS msg 为 `0 INIT, 1 RC_POS_CONTROL, 2 COMMAND_CONTROL, 3 LAND_CONTROL`（与 `uav_controller.h` L104–109 一致）；线上 Struct 为 `0 INIT, 1 MANUAL, 2 HOVER, 3 COMMAND, 4 LAND`。`controlStateCb` 原样拷贝数值，所以**线上的值实际是 ROS 语义** | `UAVControlState.msg`；`Struct.hpp` L649–656；`uav_basic_topic.cpp` L146–151 | 解码时必须按 ROS 语义 |
| F11 | `uav_setup_cb` 的 SET_CONTROL_MODE **只处理 `"COMMAND_CONTROL"` 且要求已解锁**；INIT、MANUAL、HOVER、LAND 这些字符串都被静默忽略 | `uav_controller.cpp` L1092–1104 | 无法通过地面站协议退出 COMMAND |
| F12 | 主循环：RC_POS 或 COMMAND 状态下，每拍先执行 `check_failsafe()`，然后只要模式不是 OFFBOARD 就调用 `set_px4_mode_func("OFFBOARD")`；INIT 强制 POSCTL；LAND 强制 AUTO.LAND。解锁状态消失后回到 INIT，同时把命令重置为 Init_Pos_Hover | `uav_controller.cpp` L212–258、L263–360 | RTL 只能仿真 |
| F13 | `uav_cmd_cb`：非 COMMAND 状态下直接丢弃新指令，并把命令置为 Init_Pos_Hover。处于 ABSOLUTE 锁时，只接受 ABSOLUTE 或 EXIT_ABSOLUTE 等级的指令；被丢弃的指令会触发 `stop_control_state=true` | L717–752 | 发送前先检查状态；锁状态由 Gateway 自己跟踪 |
| F14 | Init_Pos_Hover 的目标是 `Takeoff_position + (0,0,Takeoff_height)`，**yaw 固定为 0**；Takeoff_position 在 armed 上升沿锁定 | L474–483 | Takeoff 语义 |
| F15 | `check_failsafe` 的检查顺序：未连接（返回 −1，只等待）→ 非仿真模式下 RC 超过 1.5 s（返回 3）→ 超出围栏（返回 1）→ odom 无效（返回 2，置 quick_land）。1、2、3 都会转入 LAND_CONTROL，并在 TextInfo 中给出 ERROR 文本 | L1044–1076、L214–250 | 用 ELAND 与 LANDING 区分失效原因 |
| F16 | 回显：`uavCmdCb` 拷贝 Agent_CMD、Move_mode、Command_ID、yaw 和经纬度，**但不拷贝 `Control_Level`**，并且把 BODY 模式改写成 XYZ_POS。`px4PosTargetCb` 用 PX4 的 `setpoint_raw/target_local` 加 offset **覆盖** `position_ref` 和 `velocity_ref` 后再发一次 | `uav_basic_topic.cpp` L199–240 | 回显可当 V1 确认；target_local 可当 V2 读回 |
| F17 | MRS：`UavManagerDiagnostics.state` 定义了 0..5 六个值，但 uav_manager 并不填充；ControlManager 的 `hover/ehover/eland` 同步返回 `(success, message)`，已处于 eland 或 failsafe 时一律拒绝；`TrackerStatus.have_goal=false` 表示当前目标已完成；EstimationManager 的状态机共 13 态，用白名单校验转移 | 各 msg；`control_manager.cpp` L7316/7355/7426/5912；`estimation_manager.cpp` L80–92 | §4.5 |
| F18 | ANet 的效果状态：`OK / UNVERIFIED / FAILED / UNAVAILABLE / PAYMENT_REQUIRED`；`Verifiable() = Status==OK && Record!=nil`；Evidence 共 8 个字段 | `ANetCore/effect/effect.go` | succeeded 帧必须带 metrics（§7.5） |

---

## 3. FlightState 定稿

### 3.1 枚举、子模式、严重度与 UI

线上字节：`flight_state = state | (sub << 5)`。低 5 位最多可表示 32 个状态，目前用了 14 个；每个状态有 3 位子模式，最多 8 个。

| 值 | 状态 | 含义 | 子模式（0..7，0 为默认） | 严重度 | UI Badge（只用灰、黑、白、红） | 图标（d03） |
|---|---|---|---|---|---|---|
| 0 | UNKNOWN | 没有数据，或生命周期不在 READY | 0 NO_DATA、1 BOOTING、2 LINK_LOST、3 RESTARTING、4 FAILED | — | 灰色虚线描边；位置显示为最后已知值 | `link.lost` |
| 1 | DISARMED | 未解锁 | 0 READY_TO_ARM、1 NOT_READY、2 KILLED | — | 灰描边，文字 g500 | `drone.power` |
| 2 | PREFLIGHT | 正在解锁或做预检：Mock 为预检窗口；PX4/Prometheus 为解锁请求在途 | 0 CHECKING、1 COUNTDOWN | — | 灰色，加 transitions.dev 脉冲 | `drone.armed` |
| 3 | READY | 已解锁、在地面待命 | 0 IDLE | — | 白描边 | `drone.armed` |
| 4 | TAKING_OFF | 起飞中 | 0 SPOOLUP、1 CLIMB | 0 | 白字 | `drone.takeoff` |
| 5 | FLYING | 正常飞行 | 0 HOVER、1 GOTO、2 PATH、3 ORBIT、4 VELOCITY、5 SWARM、6 MANUAL、7 EXTERNAL | 0 | 白色实底，标签显示子模式 | `mode.hold` 或 `mode.offboard` 或 `mode.manual` |
| 6 | CORRECTING | Supervisor 的可逆纠正 | 0 GEOFENCE、1 ALT_MAX、2 ALT_MIN、3 BUMPER、4 SEPARATION | 1 | 白底加红点 | — |
| 7 | HOLD | 保护性悬停，只接受少数命令 | 0 SAFETY_STOP（加锁）、1 LINK_LOSS、2 ESCALATE、3 SEPARATION、4 LOC_LOST、5 AUTOPILOT、7 OTHER | 2 | 白底加红点；SAFETY_STOP 时再加红色描边 | `mode.hold` |
| 8 | RTL | 返航 | 0 CLIMB、1 CRUISE、2 DESCEND、3 FINAL、7 OPAQUE（PX4 无法得知阶段时使用） | 3 | 红描边 | `mode.rtl` |
| 9 | LANDING | 降落 | 0 DESCEND、1 GOTO（先飞到指定点再降）、2 TOUCHDOWN | 4 | 由操作员发起时白描边；FAILSAFE 位为 1 时红描边 | `drone.land` |
| 10 | ELAND | 紧急降落，锁存 | 0 CONTROLLED、1 NO_POSITION | 5 | 红色实底 | `drone.land` |
| 11 | FAILSAFE | 前馈下坠或终止，锁存 | 0 DESCENT、1 TERMINATION | 6 | 红色实底，脉冲 | — |
| 12 | LANDED | 已触地，等待自动上锁 | 0 SETTLING | — | 灰描边 | `drone.land` |
| 13 | CRASHED | 坠毁，只在仿真中出现 | 0 TILT、1 COLLISION_WORLD、2 COLLISION_UAV、3 IMPACT | 8 | 红色实底，带斜纹 | — |

- 严重度表沿用 r24 §4.4，只升不降、锁存和 grace 规则保持不变。
- `accept_commands` 由 §6.2 的准入矩阵决定，**不再写进 flags**。原因是它可以由 `(state, sub, FAILSAFE)` 确定性地推出，客户端和服务端共用同一份 `commands.json` 表。

### 3.2 对 r24 转移白名单的修订

在 r24 §4.3 的 `ALLOWED` 基础上做以下改动，原型的 `admit()` 已按此实现：

```python
ALLOWED |= {
  "UNKNOWN": {"*"},                                   # 由生命周期覆盖层驱动，不是 FSM 自身的动作
  "READY":   {"PREFLIGHT", "LANDED", "DISARMED"},     # PX4/Prometheus 一解锁就到 READY，没有 Mock 那样的预检窗口
  "FLYING":  {"TAKING_OFF", "CORRECTING", "HOLD", "RTL", "LANDING"},   # 新增 LANDING→FLYING：操作员中止自己发起的降落
  "HOLD":    {"FLYING", "CORRECTING", "TAKING_OFF", "RTL", "LANDING"}, # 新增 RTL/LANDING→HOLD：只允许操作员 SafetyStop 且 AGL≥2 m
  "RTL":     {"FLYING", "CORRECTING", "HOLD", "LANDING"},              # 新增 LANDING→RTL：只在 FAILSAFE=0 时允许
}
# "自动转移只升不降"（r24 规则 2）只约束 Supervisor；操作员命令可以降级，前提是没有被锁存。
```

Mock 的预检窗口改为可配置：`safety.preflight.window_s`，demo 档为 1.0 s，realistic 档为 5.0 s；倒计时 `countdown_s` 在 demo 档为 0。READY 状态下 10 s 没有起飞就自动上锁，与 PX4 的 `COM_DISARM_PRFLT` 一致，上锁原因记为 `PREFLIGHT_INACTION`。

---

## 4. 统一映射表

### 4.1 总表：FlightState 在各系统中的对应物

| FlightState/sub | PX4（arming, landed, nav_state, system_status） | Prometheus（armed, control_state[ROS], 最近 Agent_CMD 或 mode） | MRS（参考） | r22 生命周期前提 | Mock 显示仿真出的 PX4 nav_state |
|---|---|---|---|---|---|
| UNKNOWN/BOOTING | 还没收到 HEARTBEAT | GS 会话未建立 | — | PENDING、PROVISIONING、STARTING | —（不仿真） |
| UNKNOWN/LINK_LOST | HEARTBEAT 超过 5 s 未到 | `connected=0`，或 GS HEARTBEAT(msg 6) 超过 5 s | hw_api `connected=0` | LOST | — |
| UNKNOWN/RESTARTING、FAILED | — | — | — | RESTARTING、FAILED | — |
| DISARMED/READY_TO_ARM | armed=0，STANDBY(3) | armed=0，odom_valid=1 | armed=0 | BOOTED 或 READY | 4 LOITER，STANDBY |
| DISARMED/NOT_READY | armed=0，UNINIT(0) | armed=0，odom_valid=0 | — | BOOTED | 4，UNINIT |
| DISARMED/KILLED | FLIGHT_TERMINATION(8) 且已触地；或 Gateway 发过 kill | Gateway 发过 kill（仅 rosbridge 路径） | — | — | 4，TERMINATION |
| PREFLIGHT/* | Gateway 的解锁请求在途（≤2 s） | 已发 UAVSetup ARMING，armed 尚未置位 | autostart 倒计时 | READY | 4，UNINIT（有损） |
| READY | armed，ON_GROUND，nav≠17，本次解锁后还没飞起来过 | armed，不在空中，control_state ∈ {0,1,2} | NullTracker，output_on | READY | 4，ACTIVE，ON_GROUND |
| TAKING_OFF/SPOOLUP | armed，ON_GROUND，nav=17 | control_state=2，不在空中，intent=takeoff | LandoffTracker 起飞 | READY | 17，ON_GROUND |
| TAKING_OFF/CLIMB | nav ∈ {17,22} 或 landed=TAKEOFF(3) | control_state=2，Init_Pos_Hover，`z_rel < H−0.2` | 同上 | READY | 17，TAKEOFF |
| FLYING/HOVER | nav=4，ACTIVE，无 intent 或 intent 已完成 | control_state=2，Agent_CMD ∈ {1,2} | flying_normally，tracker HOVER | READY | 4 |
| FLYING/GOTO | nav=4 且 intent=goto 未到达；或 nav=7 GUIDED_COURSE；或 nav=14 且 intent=goto | Move，Move_mode ∈ {0,3,8} | tracker REFERENCE | READY | 4 |
| FLYING/PATH | nav=3 MISSION；或 nav=14 且 intent=follow_path | Move_mode=6，或 Gateway 正在执行航点序列 | tracker TRAJECTORY | READY | 3 或 14 |
| FLYING/ORBIT | nav=21；或 nav=14 且 intent=orbit | Gateway 正在仿真 orbit | — | READY | 21 |
| FLYING/VELOCITY | nav=14 且 intent=velocity | Move_mode ∈ {1,2,4,5} | — | READY | 14 |
| FLYING/SWARM | nav=14 且 intent=swarm | Move_mode=6，且来自规划器或编队 | — | READY | 14 |
| FLYING/MANUAL | nav ∈ {0,1,2,6,8,9,10,15} | 在空中且 control_state ∈ {0,1} | joystick_active | READY | 2 POSCTL |
| FLYING/EXTERNAL | nav ∈ {19, 23..30} 或无法解码 | Agent_CMD=5，或 Move_mode=7 | — | READY | 23 |
| CORRECTING/* | nav=4，ACTIVE，Supervisor 覆盖层激活 | control_state=2，Supervisor 覆盖层激活 | bumper_active；越过最大/最小高度 | READY | 4（ACTIVE） |
| HOLD/SAFETY_STOP | nav=4，Gateway 持锁 | control_state=2，Gateway 以 ABSOLUTE 等级发了 Current_Pos_Hover | — | READY | 4 |
| HOLD/LINK_LOSS、AUTOPILOT | nav=4 且 CRITICAL（PX4 自己的 failsafe 悬停） | —（Prometheus 没有 hold 类 failsafe） | ehover | READY | 4，CRITICAL |
| HOLD/ESCALATE、SEPARATION、LOC_LOST | nav=4，Supervisor 动作 | control_state=2，Supervisor 动作 | ehover | READY | 4，CRITICAL |
| RTL/* | nav=5 | Gateway 正在仿真 RTL（航点序列加 Land） | — | READY | 5 |
| LANDING/GOTO | nav=4 且 intent=land 尚未到点 | control_state=2，land-there 的航点段 | UavManager LandGoto | READY | 4 |
| LANDING/DESCEND、TOUCHDOWN | nav ∈ {18,20} 或 landed=LANDING(4) | control_state=3 且不是 odom 失效 | tracker LAND | READY | 18 |
| ELAND/NO_POSITION | nav=12 DESCEND | control_state=3，failsafe=1，TextInfo 包含 "Odom invalid" | eland（估计失效时） | READY | 18，CRITICAL（有损） |
| ELAND/CONTROLLED | —（PX4 没有对应物） | — | eland | READY | 18，CRITICAL（有损） |
| FAILSAFE/TERMINATION | nav=13，或 FLIGHT_TERMINATION 且在空中 | —（地面站协议不可达） | failsafe | READY | 13，TERMINATION |
| FAILSAFE/DESCENT | —（与 PX4 DESCEND 最接近，但 DESCEND 反推为 ELAND） | — | failsafe | READY | 12（有损） |
| LANDED | armed，ON_GROUND，本次解锁后飞起来过 | control_state=3，不在空中；或 armed 且触地 | 触地检测通过、上锁前 | READY | 18，ON_GROUND |
| CRASHED/* | 只能由 Gateway 真值检测（SIH 真值端口） | — | — | READY | 不仿真（有损） |

"有损"指 Mock 的显示仿真是单向的：规范态可以算出显示用的 nav_state，但这个 nav_state 反推回来得不到原来的状态。反推回来会不同的只有 9 种组合，已由原型 `_selftest` 第 4 步断言：PREFLIGHT 的两个子模式、ELAND 的两个子模式、FAILSAFE/DESCENT，以及 CRASHED 的四个子模式。**Mock 的规范态始终取自 Safety FSM，绝不从仿真出来的显示值反推。**

### 4.2 PX4 推导（StateFuser，纯函数）

输入：HEARTBEAT（1 Hz）、CURRENT_MODE（Gateway 设为 4 Hz）、EXTENDED_SYS_STATE（onboard 模式下 5 Hz）、Gateway 的 `Intent`、Supervisor 覆盖层。

```python
def derive_px4(r, g):                                  # 原型 state_model.py: derive_px4
    nav = nav_from_custom_mode(r.custom_mode)          # 用 PX4 px4_custom_mode.h 生成的表反查，不用 MAVSDK 的表
    failsafe = r.system_status == MAV_STATE_CRITICAL
    if r.system_status == MAV_STATE_FLIGHT_TERMINATION:
        return (FAILSAFE, TERMINATION) if r.landed in (IN_AIR, TAKEOFF, LANDING) else (DISARMED, KILLED)
    if not r.armed:   return DISARMED, (READY_TO_ARM if r.system_status == STANDBY else NOT_READY)
    if r.landed == ON_GROUND:
        if nav == AUTO_TAKEOFF:     return TAKING_OFF, SPOOLUP
        return (LANDED, 0) if g.airborne_since_arm else (READY, 0)
    if nav == TERMINATION:          return FAILSAFE, TERMINATION
    if nav == DESCEND:              return ELAND, NO_POSITION
    if nav in (AUTO_LAND, AUTO_PRECLAND) or r.landed == LANDING:  return LANDING, DESCEND   # 近地时由 AGL 启发式判为 TOUCHDOWN
    if nav in (AUTO_TAKEOFF, AUTO_VTOL_TAKEOFF) or r.landed == TAKEOFF: return TAKING_OFF, CLIMB
    if nav == AUTO_RTL:             return RTL, rtl_phase(kin, params)       # 推不出阶段时为 OPAQUE(7)
    if nav == AUTO_LOITER:
        if failsafe:                return HOLD, g.hold_reason or AUTOPILOT   # PX4 自己的 failsafe 优先
        if g.supervisor:            return g.supervisor                       # 本方 CORRECTING 或 HOLD 覆盖层
        if g.lock:                  return HOLD, SAFETY_STOP
        if g.cmd == "land" and not g.arrived: return LANDING, GOTO
        return FLYING, (GOTO if g.cmd == "goto" and not g.arrived else HOVER)
    if nav == AUTO_MISSION:         return FLYING, PATH
    if nav == ORBIT:                return FLYING, ORBIT
    if nav == OFFBOARD:             return FLYING, {follow_path:PATH, velocity:VELOCITY, swarm:SWARM, orbit:ORBIT, goto:GOTO}.get(g.cmd, EXTERNAL)
    if nav in {0,1,2,6,8,9,10,15}:  return FLYING, MANUAL
    if nav == GUIDED_COURSE:        return FLYING, GOTO
    return FLYING, EXTERNAL                                                    # 19、22..30、无法解码
# RTL 阶段启发式：vz>+0.3 且 z<RTL_RETURN_ALT−1 → CLIMB；|v_xy|>1 → CRUISE；vz<−0.3 且 z>RTL_DESCEND_ALT → DESCEND；landed=LANDING → FINAL
# flags.FAILSAFE = failsafe or g.supervisor is not None；若 STATUSTEXT 出现 "user took over"（对应 failsafe_and_user_took_over），则 FAILSAFE=0 且 ALERT=1
```

PX4 的 `hold_reason` 靠 STATUSTEXT 或 events 做文本匹配：出现 "Connection to ground station lost" 记为 LINK_LOSS，其余情况记为 AUTOPILOT。**`nav_state_user_intention ≠ nav_state` 且持续 1.5 s**，就说明 failsafe 挡住了用户选的模式：进行中的调用判为 `failed MODE_NOT_ENTERED`，同时置 ALERT。

### 4.3 Prometheus 推导

**前置规则**

- `control_state` 一律按 ROS 枚举 0..3 解码；遇到 4 按 INIT 处理，并记一次 `PROM.ENUM_ANOMALY`。
- `in_air` 由 Gateway 带滞回推导，因为地面站协议里没有 landed_state：
  - 满足 `armed ∧ (z_rel > 0.3 ∨ |v| > 0.3)` 时进入"在空中"；
  - 满足 `z_rel < 0.15 ∧ |v| < 0.2` 并持续 1 s 时回到"在地面"；
  - `z_rel = z − z_home`，其中 `z_home` 在 armed 上升沿锁定，与 Prometheus 的 `Takeoff_position` 一致。
  - 走 rosbridge 时，改用 `mavros/extended_state`。

**判定顺序**：从上到下，命中第一条即停。

| # | 条件 | FlightState/sub | FAILSAFE 位 | ctrl.native | ctrl.owner |
|---|---|---|---|---|---|
| 1 | `UAVState.connected=0`，或 GS HEARTBEAT 超过 5 s | UNKNOWN/LINK_LOST | 保持原值 | INIT | 保持原值 |
| 2 | armed=0 | DISARMED/(odom_valid ? READY_TO_ARM : NOT_READY) | 0 | INIT | NONE |
| 3 | control_state=3，且不在空中 | LANDED | =failsafe | LAND | SAFETY 或租约持有者 |
| 4 | control_state=3，failsafe=1，且最近一条 ERROR 包含 "Odom invalid" | ELAND/NO_POSITION | 1 | LAND | SAFETY |
| 5 | control_state=3（其余情况：围栏越界、RC 丢失或正常 Land） | LANDING/DESCEND | =failsafe | LAND | failsafe ? SAFETY : 持有者 |
| 6 | 不在空中，且本次解锁后飞起来过 | LANDED | — | 按 cs | — |
| 7 | 不在空中，control_state=2，intent=takeoff | TAKING_OFF/SPOOLUP | 0 | COMMAND | 持有者 |
| 8 | 不在空中 | READY | — | 按 cs | NONE |
| 9 | 在空中，control_state ∈ {0,1} | FLYING/MANUAL | =failsafe | INIT 或 MANUAL | PILOT |
| 10 | control_state=2，Supervisor 覆盖层激活 | 覆盖层（CORRECTING 或 HOLD） | 1 | COMMAND | SAFETY |
| 11 | control_state=2，Gateway 持有 ABSOLUTE 锁 | HOLD/SAFETY_STOP，locked=1 | 0 | COMMAND | SAFETY |
| 12 | control_state=2，Gateway 正在仿真 RTL | RTL/CLIMB、CRUISE、DESCEND 或 FINAL | 0 或 1（取决于 RTL 的发起原因） | COMMAND | 持有者或 SAFETY |
| 13 | control_state=2，intent=takeoff，且 `z_rel < H−0.2` | TAKING_OFF/CLIMB | 0 | COMMAND | 持有者 |
| 14 | control_state=2，Agent_CMD ∈ {1,2} | FLYING/HOVER | 0 | COMMAND | 持有者 |
| 15 | control_state=2，Agent_CMD=4 | Move_mode ∈ {0,3,8} 为 GOTO（到达后改为 HOVER）；{1,2,4,5} 为 VELOCITY；6 为 PATH；7 为 EXTERNAL。若 intent 是 follow_path、orbit 或 swarm，就把 GOTO/PATH 替换成对应子模式 | 0 | COMMAND | 持有者 |
| 16 | 其余 | FLYING/EXTERNAL | 0 | COMMAND | EXTERNAL |

**模式抢夺检测**：control_state ∈ {1,2}，但 `UAVState.mode ≠ "OFFBOARD"` 且持续超过 1 s，就发出事件 `PROM.MODE_FIGHT` 并置 ALERT。典型成因有两个：QGC 同时连着改了模式；或者有人错误地用 SET_PX4_MODE 发了 AUTO.RTL（F12）。

### 4.4 Mock（FleetSim）

- **规范态**：直接取 Safety FSM（r24 §4.3，编号改为本文 §3.1）。**Mock 不做任何推导**。
- **显示仿真**：为了与 PX4 后端共用同一套 UI 代码路径，Mock 在 `state_ext.px4` 里填入仿真出来的 `{arming_state, nav_state, landed_state, system_status, custom_mode}`，实现见原型的 `mock_emulate_px4()`。只有 PX4 自己的失效保护才会标 CRITICAL：自动触发的 HOLD、ELAND、FAILSAFE。CORRECTING 不标，因为在 PX4 上它是通过普通 reposition 命令执行的。
- `ctrl.native`：
  - DISARMED、PREFLIGHT、UNKNOWN 为 INIT；
  - FLYING/MANUAL（虚拟摇杆）为 MANUAL；
  - LANDING、ELAND、FAILSAFE、LANDED、CRASHED 为 LAND；
  - 其余为 COMMAND。
- `ctrl.pose_src` 为 TRUTH。

### 4.5 MRS 映射（参考；后续若接 MRS 后端时直接使用）

| MRS 信号 | FlightState |
|---|---|
| `HwApiStatus.armed=0` | DISARMED |
| armed、offboard、`active_tracker=NullTracker`、`output_enabled` | READY。autostart 倒计时期间为 PREFLIGHT/COUNTDOWN |
| `active_tracker=LandoffTracker` 且 `TrackerStatus.state=TAKEOFF(2)` | TAKING_OFF/CLIMB |
| `flying_normally` | FLYING，子模式按 TrackerStatus.state 取：HOVER(3) 为 HOVER，REFERENCE(4) 为 GOTO，TRAJECTORY(5) 为 PATH |
| `bumper_active` | CORRECTING/BUMPER |
| `joystick_active` | FLYING/MANUAL，owner=PILOT |
| `callbacks_enabled=0`，且 active_tracker 是 ehover 所用的 tracker | HOLD/ESCALATE |
| `TrackerStatus.state=LAND(6)` | LANDING |
| `active_controller` 为 eland 所用的 controller 名（来自配置） | ELAND/CONTROLLED |
| `active_controller` 为 failsafe 所用的 controller 名 | FAILSAFE/DESCENT |
| EstimationManager 处于 ESTIMATOR_SWITCHING 或 ERROR | 定位轴为 DEGRADED 或 LOST |

- 完成判据：`TrackerStatus.have_goal` 从 1 变为 0。
- ACK：服务同步返回 `(success, message)`。

### 4.6 生命周期（r22）投影

| 生命周期 | FlightState | flags | 其他 |
|---|---|---|---|
| PENDING、PROVISIONING、STARTING | UNKNOWN/BOOTING | FCU=0，ALERT=0 | 3D 场景中不渲染机体，只显示出生点占位 |
| BOOTED | DISARMED/NOT_READY（prearm 尚未通过） | FCU=1 | — |
| READY | 按后端推导 | FCU=1 | ACTIVE 仅作显示标签，等于 READY ∧ ARMED |
| DEGRADED（心跳超过 2.5 s） | 保持最后值 | FCU=0，ALERT=1 | 位姿按 dt_us 外推，外推上限 1 s |
| LOST（超过 5 s） | UNKNOWN/LINK_LOST | FCU=0，ALERT=1；ARMED 与 IN_AIR 保持最后已知值 | 进行中的调用判 `failed VEHICLE_LOST` |
| RESTARTING | UNKNOWN/RESTARTING | FCU=0，ALERT=1 | 重生事件 `sim.vehicle.respawned` 触发任务重规划 |
| FAILED | UNKNOWN/FAILED | FCU=0，ALERT=1 | — |
| DRAINING | 由 Gateway 下发 Land，按正常推导 | — | 结束后进入 STOPPED |
| STOPPED、REMOVED | 从 advertise 的 agents 表里删除 | — | — |

### 4.7 定位（r08、GNSS、Prometheus odom、MRS 估计）投影

| 来源 | 状态 | LOC_OK | LOC_DEG | 对 FlightState 或准入的影响 |
|---|---|---|---|---|
| LIO+MAP（r08） | TRACKING | 1 | 0 | — |
| | DEGRADED（3–10 s 内没有成功匹配） | 1 | 1 | 限速到 2 m/s（ConstraintManager 的 slow 档）；ALERT；事件 `SAF.EST.DEGRADED` |
| | OUT_OF_MAP | 1 | 1 | 禁止执行需要地图的任务段 |
| | ALIGNING、WAIT_INIT | 0 | 1 | 在地面时禁止 arm：需要连续 3 次 TRACKING |
| | LOST（超过 10 s） | 0 | 0 | 在空中时进入 HOLD/LOC_LOST，做 30 s 多假设重定位；仍失败则有 RTK 时 RTL，否则 LANDING，控制失稳时进入 ELAND |
| | NO_MAP | 0 | 0 | 禁止 arm（仅限地图定位的任务） |
| GNSS（PX4 `GPS_RAW_INT.fix_type` / Prometheus `gps_status`） | 6 RTK_FIXED、3 3D | 1 | 0 | — |
| | 5 RTK_FLOAT、4 DGPS | 1 | 1 | 任务要求 RTK 时置 ALERT |
| | ≤2 | 0 | fix=2 时为 1 | 镜像 PX4 自身的 failsafe（DESCEND 或 LAND） |
| Prometheus `odom_valid` | 0 | 0 | 0 | 原生会进入 LAND_CONTROL（quick_land），对应 ELAND/NO_POSITION |
| 状态估计缺失（r24） | 超过 0.1 s | 0 | 0 | FAILSAFE/DESCENT |
| Mock | 真值直接送入 | 1 | 0 | 做 `loc_drop` 或 `gnss_denied` 故障注入时，按上面几行处理 |

### 4.8 flags 各位的来源

| 位 | 名称 | Mock | PX4 SIH | Prometheus |
|---|---|---|---|---|
| 0 | ARMED | FSM 中的 armed | `HEARTBEAT.base_mode & 128` | `UAVState.armed` |
| 1 | IN_AIR | 触地检测（r20：\|vz\|<0.25、\|vxy\|<1.5、\|ω\|<20°/s、推力 <0.3·hover，持续 1 s）取反 | EXTENDED_SYS_STATE 的 `landed_state ∈ {2,3,4}` | 按 §4.3 带滞回推导 |
| 2 | LOC_OK | §4.7 | §4.7；另加 SYS_STATUS 中 EKF 健康位 | `odom_valid` ∧ §4.7 |
| 3 | FAILSAFE | Supervisor 的自动动作处于激活状态（CORRECTING、自动 HOLD、自动 RTL、自动 LANDING、ELAND、FAILSAFE） | `system_status==CRITICAL`，或 Supervisor 覆盖层激活 | `UAVControlState.failsafe`，或 Supervisor 覆盖层激活 |
| 4 | GCS_LINK | 租约持有者会话的心跳小于 1.5 s；无租约时看任意 operator；配置 `gcs_loss_policy: ignore` 时恒为 1 | Gateway 心跳正常外发，且没有处于激活状态的 "ground station lost" STATUSTEXT | GS 的 TCP 会话存活，且机载 HEARTBEAT(msg 6) 小于 3 s |
| 5 | FCU_LINK | 1；做 `link_drop` 或 `state_drop` 故障注入时为 0 | HEARTBEAT 距今小于 2.5 s | `UAVState.connected`，且 UAVState 距今小于 1 s |
| 6 | LOC_DEGRADED | §4.7 | §4.7 | §4.7 |
| 7 | ALERT | 存在任意激活的 WARN 及以上条件：r24 §4.5 的 L1 条件、生命周期 DEGRADED、定位 DEGRADED 或 LOST、模式抢夺 | 同左，另加 10 s 内出现过 WARNING 及以上级别的 STATUSTEXT | 同左，另加 10 s 内出现过 WARN 或 ERROR 级别的 TextInfo |

ALERT 只表示"当前有条件处于激活状态"。**告警是否已确认是每个客户端各自的 UI 状态，不上线**。

### 4.9 ctrl 各字段的来源

| 字段 | 位 | 取值 | 规则 |
|---|---|---|---|
| owner | 0–2 | 0 NONE、1 OPERATOR、2 MISSION、3 AGENT、4 SWARM、5 SAFETY、6 PILOT、7 EXTERNAL | 默认取 LeaseManager 的持有者类别。以下情况覆盖：FAILSAFE=1 或持锁时为 **SAFETY**；native=MANUAL 时为 **PILOT**（PX4 手动模式，或 Prometheus 在空中且处于 RC_POS/INIT）；PX4 的 nav_state 变化既不对应任何进行中的调用、也不是 failsafe 时为 **EXTERNAL**（例如 QGC 改了模式），同时置 ALERT |
| locked | 3 | 0/1 | SafetyStop 或 Prometheus ABSOLUTE 锁，由 Gateway 跟踪；租约优先级为 OVERRIDE 时同样置 1 |
| native | 4–5 | 0 INIT、1 MANUAL、2 COMMAND、3 LAND（与 Prometheus ROS 的数值相同） | PX4：未解锁为 INIT；nav ∈ {0,1,2,6,8,9,10,15} 为 MANUAL；nav ∈ {12,13,18,20} 为 LAND；其余为 COMMAND。Prometheus：直接取 control_state。Mock：见 §4.4 |
| pose_src | 6–7 | 0 ESTIMATE、1 TRUTH、2 FUSED、3 KINEMATIC | Mock 为 TRUTH；SIH 启用真值端口（19410+i）时为 TRUTH，否则为 ESTIMATE；Prometheus 为 ESTIMATE；LocalizationService 输出的 map 系位姿为 FUSED；L0 插值回放为 KINEMATIC |

**租约抢占顺序**（owner 类别码与优先级是两回事）：

| 优先级 | 持有者 |
|---|---|
| ∞ | SAFETY（不参与租约，直接接管） |
| 6 | PILOT（真机上遥控器永远优先） |
| 5 | OPERATOR 的 override |
| 4 | OPERATOR |
| 3 | AGENT |
| 2 | MISSION |
| 1 | SWARM |

这与 r19 CommandArbiter 的顺序 safety > ui-override > ui > agent > mission > swarm 一致。

---

## 5. Full64 / Lite32 定稿

### 5.1 布局

与 r27 §3.5 相比，偏移不变，只修订 byte 2、3、7（Lite32 为 byte 2、30、31）的语义。

**Full64：`awr.DroneState64.v1`**

| off | 类型 | 字段 | 定稿语义 |
|---|---|---|---|
| 0 | u16 | agent_no | 通过 advertise 的 `agents[]` 映射为 `{uav_id, backend, simulated, model}` |
| 2 | u8 | **flight_state** | bit0–4 为 FlightState（§3.1）；bit5–7 为子模式 |
| 3 | u8 | **flags** | bit0 ARMED、bit1 IN_AIR、bit2 LOC_OK、bit3 FAILSAFE、bit4 GCS_LINK、bit5 FCU_LINK、bit6 LOC_DEGRADED、bit7 ALERT |
| 4 | u16 | mission_item | 0xFFFF 表示无 |
| 6 | u8 | battery_pct | 255 表示未知 |
| 7 | u8 | **ctrl** | bit0–2 owner、bit3 locked、bit4–5 native、bit6–7 pose_src |
| 8 | f32×3 | pos | World ENU，米 |
| 20 | f32×3 | vel | ENU，m/s |
| 32 | f32×4 | q | [x,y,z,w]，WORLD←BODY(FLU) |
| 48 | f32×3 | omega | FLU，rad/s |
| 60 | i32 | dt_us | 相对帧时刻的偏移 |

**Lite32：`awr.SwarmLite32.v1`**

| off | 类型 | 字段 | 定稿语义 |
|---|---|---|---|
| 0 | u16 | agent_no | — |
| 2 | u8 | flight_state | 与 Full64 相同 |
| 3 | u8 | battery_pct | — |
| 4 | f32×3 | pos | — |
| 16 | i16×4 | q_snorm | — |
| 24 | i16×3 | vel_cms | — |
| 30 | u8 | flags | 与 Full64 相同 |
| 31 | u8 | **ctrl** | 原 `_reserved`，现与 Full64 byte 7 相同 |

### 5.2 `packages/contracts/rt/layouts.json`（v1 片段）

```json
{"schemaName":"awr.DroneState64.v1","layout":{"size":64,"fields":[
 {"n":"agent_no","t":"u16","o":0},
 {"n":"flight_state","t":"u8","o":2,"bits":{"state":[0,5,"FlightState"],"sub":[5,3,"FlightSub"]}},
 {"n":"flags","t":"u8","o":3,"bits":{"armed":[0,1],"in_air":[1,1],"loc_ok":[2,1],"failsafe":[3,1],
   "gcs_link":[4,1],"fcu_link":[5,1],"loc_degraded":[6,1],"alert":[7,1]}},
 {"n":"mission_item","t":"u16","o":4,"none":65535},
 {"n":"battery_pct","t":"u8","o":6,"none":255},
 {"n":"ctrl","t":"u8","o":7,"bits":{"owner":[0,3,"Owner"],"locked":[3,1],"native":[4,2,"Native"],"pose_src":[6,2,"PoseSrc"]}},
 {"n":"pos","t":"f32","c":3,"o":8},{"n":"vel","t":"f32","c":3,"o":20},{"n":"q","t":"f32","c":4,"o":32},
 {"n":"omega","t":"f32","c":3,"o":48},{"n":"dt_us","t":"i32","o":60}]},
 "enums":"awr.enums.v1"}
```

`bits` 的格式是 `[起始位, 位宽, 可选的枚举名]`。旧客户端不认识 `bits` 时按 u8 读取，不会出错。

### 5.3 `packages/contracts/rt/enums.json`（`awr.enums.v1`，唯一真源）

```json
{"FlightState":["UNKNOWN","DISARMED","PREFLIGHT","READY","TAKING_OFF","FLYING","CORRECTING","HOLD","RTL",
                "LANDING","ELAND","FAILSAFE","LANDED","CRASHED"],
 "FlightSub":{"UNKNOWN":["NO_DATA","BOOTING","LINK_LOST","RESTARTING","FAILED"],
   "DISARMED":["READY_TO_ARM","NOT_READY","KILLED"],"PREFLIGHT":["CHECKING","COUNTDOWN"],"READY":["IDLE"],
   "TAKING_OFF":["SPOOLUP","CLIMB"],"FLYING":["HOVER","GOTO","PATH","ORBIT","VELOCITY","SWARM","MANUAL","EXTERNAL"],
   "CORRECTING":["GEOFENCE","ALT_MAX","ALT_MIN","BUMPER","SEPARATION"],
   "HOLD":["SAFETY_STOP","LINK_LOSS","ESCALATE","SEPARATION","LOC_LOST","AUTOPILOT",null,"OTHER"],
   "RTL":["CLIMB","CRUISE","DESCEND","FINAL",null,null,null,"OPAQUE"],"LANDING":["DESCEND","GOTO","TOUCHDOWN"],
   "ELAND":["CONTROLLED","NO_POSITION"],"FAILSAFE":["DESCENT","TERMINATION"],"LANDED":["SETTLING"],
   "CRASHED":["TILT","COLLISION_WORLD","COLLISION_UAV","IMPACT"]},
 "Severity":{"TAKING_OFF":0,"FLYING":0,"CORRECTING":1,"HOLD":2,"RTL":3,"LANDING":4,"ELAND":5,"FAILSAFE":6,"CRASHED":8},
 "Owner":["NONE","OPERATOR","MISSION","AGENT","SWARM","SAFETY","PILOT","EXTERNAL"],
 "Native":["INIT","MANUAL","COMMAND","LAND"], "PoseSrc":["ESTIMATE","TRUTH","FUSED","KINEMATIC"],
 "Lifecycle":["PENDING","PROVISIONING","STARTING","BOOTED","READY","DEGRADED","LOST","RESTARTING","FAILED",
              "DRAINING","STOPPED","REMOVED"],
 "LocStatus":["NO_MAP","WAIT_INIT","ALIGNING","TRACKING","DEGRADED","LOST","OUT_OF_MAP"],
 "CallStatus":["accepted","rejected","running","succeeded","failed","canceled","timeout"],
 "EffectStatus":["OK","UNVERIFIED","FAILED","UNAVAILABLE","PAYMENT_REQUIRED"]}
```

### 5.4 编解码

```ts
// apps/web/src/net/rt/layouts.ts（生成）——热路径，无分配
export const fsOf  = (u8: Uint8Array, o: number) => u8[o + 2] & 0x1f;
export const subOf = (u8: Uint8Array, o: number) => u8[o + 2] >>> 5;
export const flag  = (u8: Uint8Array, o: number, bit: number) => (u8[o + 3] >>> bit) & 1;
export const ctrlOwner  = (b: number) => b & 7;
export const ctrlLocked = (b: number) => (b >>> 3) & 1;
export const ctrlNative = (b: number) => (b >>> 4) & 3;
export const ctrlPose   = (b: number) => b >>> 6;
// 3D 红色高亮（整群）：fs >= CORRECTING && fs <= CRASHED && fs !== LANDED，或 flags 的 ALERT 位为 1
```

```python
# sim/core/state_model.py（由原型转正）
pack_fs   = lambda fs, sub=0: (fs & 0x1F) | ((sub & 7) << 5)
pack_ctrl = lambda owner, locked, native, pose: (owner & 7) | (int(locked) << 3) | ((native & 3) << 4) | ((pose & 3) << 6)
# numpy 向量化写法：rec["flight_state"] = fs_arr | (sub_arr << 5)
```

### 5.5 黄金向量（契约测试，已由原型断言）

| 场景 | 输入 | flight_state | flags | ctrl |
|---|---|---|---|---|
| SIH 执行 GoTo 已到达，由操作员控制，使用 SIH 真值 | armed，`custom_mode=0x03040000`（AUTO/LOITER），ACTIVE，IN_AIR | `0x05`（FLYING/HOVER） | `0x37` | `0x61`（OPERATOR、COMMAND、TRUTH） |
| SIH 链路丢失，PX4 触发 failsafe 悬停 | 同上，但为 CRITICAL；GCS_LINK=0 | `0xA7`（HOLD/AUTOPILOT） | `0xAF` | `0x25`（SAFETY、COMMAND、ESTIMATE） |
| Prometheus 因 odom 失效快速降落 | control_state=3，failsafe=1，TextInfo 为 "Odom invalid, swtich to land control mode!" | `0x2A`（ELAND/NO_POSITION） | `0xBB` | `0x35`（SAFETY、LAND、ESTIMATE） |
| Mock 中操作员按下 SafetyStop | — | `0x07`（HOLD/SAFETY_STOP） | `0x37` | `0x6D`（SAFETY、locked、COMMAND、TRUTH） |
| 生命周期 LOST | — | `0x40`（UNKNOWN/LINK_LOST） | 保留最后已知的 ARMED 与 IN_AIR，FCU=0，ALERT=1 | 保留最后已知值 |
| Prometheus 线上 control_state=2 | — | 必须解码为 COMMAND；若被解成 Struct.hpp 的 HOVER 即为回归 | — | native=2 |

---

## 6. 命令矩阵

### 6.1 命令集与服务名（与 r27 §3.12 对齐）

| 命令 | 服务 `uav/{id}/cmd/…` | 参数（World ENU、米、弧度） | 类别 | 租约要求 |
|---|---|---|---|---|
| Takeoff | `takeoff` | `alt_m`：AGL，默认 2.5（`MIS_TAKEOFF_ALT`），Prometheus 默认 1.5；`auto_arm`：默认 true | 导航与设置 | 需要持有租约 |
| Land | `land` | `at`：`here`（默认）、`home` 或 `{pos}`（先飞到该点再降） | 安全类 | 任意 operator 均可 |
| GoTo | `goto` | `pos[3]`、`yaw?`、`speed?`、`tol_m?` | 导航 | 需要持有租约 |
| FollowPath | `follow_path` | `waypoints[]`，或 `bspline{order, ts, ctrl_pts, t0, traj_id}`；`speed`；`mode`：auto、mission 或 offboard | 导航 | 需要持有租约 |
| Orbit | `orbit` | `center[3]`、`radius`、`speed`、`cw`、`turns`（默认 0，表示一直绕）、`yaw_behavior` | 导航 | 需要持有租约 |
| Hover | `hover`（`hold` 保留为别名） | — | 安全类（轻） | 任意 operator 均可 |
| RTL | `rtl` | `land`：默认 true；`alt?` | 安全类 | 任意 operator 均可 |
| Velocity | `velocity` 建立会话，之后用 CLIENT_DATA 发 `awr.VelSetpoint16.v1` | `frame`：world 或 body；`hold_alt`；`vmax` | 导航（流式） | 需要持有租约 |
| SafetyStop | `safety_stop`；用 `resume` 解锁 | — | 安全类（加锁） | 任意 operator 均可；agent 不可用 |
| Pause | `pause`；用 `resume` 恢复 | — | 任务 | 需要持有租约 |

- **辅助命令**：`arm`、`disarm`、`kill`（需要 confirm-token，按住 1 s）、`resume`、`cancel{call_id}`、`escalate`（HOLD → ELAND，两次之间至少间隔 2 s）、`acquire` 与 `release`（租约）。
- **agent 的限制**：agent（`owner=AGENT`）被视为不可信的指挥方。安全类命令只能用于它自己持有租约的飞机，并且必须按 n03 的守卫顺序检查。

### 6.2 准入矩阵：命令 × FlightState

由原型 `admission_matrix()` 生成。图例：`Y` 为接受；`=` 为幂等，返回已在进行中的那个调用；`S` 为 409 `SAFETY_ACTIVE`；`-` 为 409 `STATE`。

| 命令 | UNK | DIS | PRE | RDY | TKO | FLY | COR | HOLD | RTL 由操作员发起 | RTL 自动 | LND 由操作员发起 | LND 自动 | ELD | FSF | LDD | CRS |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| takeoff | - | Y¹ | - | Y | = | - | - | - | - | - | - | - | - | - | Y | - |
| land | - | - | - | - | Y | Y | Y | Y | Y | Y | = | = | S | S | = | - |
| goto、follow_path、orbit、velocity | - | - | - | - | - | Y | S | S | Y | S | Y | S | S | S | - | - |
| hover | - | - | - | - | Y | Y | S | S | Y | S | Y | S | S | S | - | - |
| rtl | - | - | - | - | - | Y | Y | Y | = | = | Y | S | S | S | - | - |
| safety_stop | - | - | - | - | Y | Y | Y | = | Y | Y | Y² | Y² | S | S | - | - |
| pause | - | - | - | - | - | Y³ | - | - | - | - | - | - | - | - | - | - |
| resume | - | - | - | - | - | Y⁴ | - | Y⁵ | - | Y⁵ | - | - | - | - | - | - |
| arm / disarm / kill | –/–/– | Y¹/–/– | –/Y/– | –/Y/Y | –/–/Y | –/–/Y | –/–/Y | –/–/Y | –/–/Y | –/–/Y | –/–/Y | –/–/Y | –/–/Y | –/–/Y | –/Y/Y | –/–/– |

注：
1. 只在子模式为 READY_TO_ARM 时成立。
2. 要求 AGL ≥ 2 m，否则返回 `STATE`，降落继续进行。
3. 要求子模式 ∈ {GOTO, PATH, ORBIT, SWARM}，并且当前有任务。
4. 要求任务处于 PAUSED。
5. 要求 Supervisor 确认引发该状态的原因已经解除（link 恢复、间距恢复等）；电量原因触发的 RTL 不能 resume。

**准入流水线**：以下各步全部同步完成，耗时 ≤5 ms，按 n03 的守卫顺序：

1. 鉴权与角色；
2. 状态检查（上表）；
3. 租约；
4. 参数边界：例如 orbit 半径 ∈ [1, 1000]，alt ∈ [0.5, 120]；
5. 后端能力（§6.5）；
6. 围栏：点，以及按每米 20 步离散的路径（MRS `isPathValid`）；
7. 限流；
8. 确认令牌（kill、escalate）；
9. 分发。

### 6.3 实现矩阵

#### Takeoff

- **Mock（FleetSim）**：若 `auto_arm`，依次经过 DISARMED → PREFLIGHT（预检窗口）→ READY → TAKING_OFF.SPOOLUP（1 s）→ CLIMB（上升速度逐渐升到 1.5 m/s，即 `MPC_TKO_SPEED`）→ 到达高度后为 FLYING/HOVER。
- **PX4 SIH（MAVLink，pymavlink 或 mavlite）**：
  1. 若 `auto_arm`，先发 `COMMAND_LONG 400`（p1=1）并等待 ACK；
  2. 再发 `COMMAND_LONG 22 NAV_TAKEOFF`，其中 p7 = home_amsl + alt，p4 = NaN；
  3. PX4 进入 nav 17；到达高度后，navigator 自动切到 AUTO_LOITER。
  - 解锁后必须在 10 s（`COM_DISARM_PRFLT`）内起飞，否则会被自动上锁。
- **Prometheus（地面站协议，TCP 55555）**：
  1. 202 `ModeSelection`（建立会话）；
  2. 110 `ParamSettings{Takeoff_height=alt}`；
  3. 109 `UAVSetup{cmd:0, arming:true}`，然后等待 `armed=1`（≤3 s）；
  4. 109 `UAVSetup{cmd:3, control_state:"COMMAND_CONTROL"}`。进入 COMMAND 时 Agent_CMD 本来就是 Init_Pos_Hover，飞机会直接起飞（F14）；也可以再显式发一次 108 `{Agent_CMD:1}`。
  - yaw 被固定为 0。

#### Land

- **Mock**：
  - 若 `at≠here`，先经过 LANDING.GOTO 段；
  - DESCEND 段按 r20 的参数：`MPC_LAND_ALT1`/`ALT2` 为 10 m / 5 m，高速段 0.7 m/s（`MPC_LAND_SPEED`），末段减速到 0.3 m/s（`MPC_LAND_CRWL`）；
  - 触地检测通过后进入 LANDED，2 s 后进入 DISARMED。
- **PX4 SIH**：
  - 发 `COMMAND_LONG 21 NAV_LAND`。它是强制切换，即使处于 failsafe 也能进入（F5）；
  - PX4 进入 nav 18；landed_state 从 LANDING 变为 ON_GROUND；2 s 后自动上锁（`COM_DISARM_LAND`）；
  - 指定点降落：先发 `DO_REPOSITION`，到达后再发 `NAV_LAND`。
- **Prometheus**：
  - 要求 control_state=2，然后发 108 `{Agent_CMD:3}`；
  - 状态进入 LAND_CONTROL。GPS/RTK 定位时切 AUTO.LAND；其他定位源先把 `MC_YAWRATE_MAX` 设为 0 再切 AUTO.LAND；
  - 上锁后回到 INIT；
  - **control_state=1（遥控器在控）时拒绝，返回 `NOT_IN_CONTROL`**。

#### GoTo

- **Mock**：
  - 准入时做路径校验；
  - LineTracker 先执行 STOP_MOTION 刹停，再以 `min(speed, vmax)` 沿直线飞向目标；
  - 状态为 FLYING/GOTO，到达后为 HOVER。
- **PX4 SIH**：
  - 发 `COMMAND_INT 192 DO_REPOSITION`：frame=GLOBAL；x、y 为纬度、经度 ×1e7（由 ENU 经 World 锚点换算）；z 为 AMSL；p1 为速度（−1 表示用默认值）；p2=1（CHANGE_MODE）；p4 为 yaw（NaN 表示不控）；
  - PX4 进入 AUTO_LOITER；
  - 不需要像 MAVSDK 那样先切 Hold 再发。
- **Prometheus**：
  - 发 108 `{Agent_CMD:4, Move_mode:0, position_ref: T_local←world·p, yaw_ref, Command_ID:++, Control_Level:0}`；
  - **不支持速度参数**：进入 COMMAND 时 `MPC_XY_VEL_MAX` 被设为 1.0 m/s；
  - Gateway 可以用 carrot 点把速度压得更低，但无法超过这个值。

#### FollowPath

- **Mock**：Gateway 的 tracker 对 B-spline 或折线按速度做时间参数化（TOPP-lite），以 50 Hz 输出 p/v/a setpoint；`mission_item` 取当前段的序号。
- **PX4 SIH**：
  - **mission 模式**：依次发 `MISSION_COUNT`、`MISSION_ITEM_INT`（NAV_WAYPOINT，GLOBAL_RELATIVE_ALT_INT），收到 `MISSION_ACK` 后发 `COMMAND_LONG 300 MISSION_START`，进入 nav 3；
  - **offboard 模式**：以 30 Hz 发 `SET_POSITION_TARGET_LOCAL_NED`（type_mask=0x0800），预先流 0.5 s 后再发 `DO_SET_MODE(1,6,0)`；
  - **auto 模式**：静态且 ≤100 个航点时用 mission；规划器输出的 B-spline 用 offboard。
- **Prometheus**：
  - 地面站协议：由 Gateway 逐点发 108 XYZ_POS，距离当前点小于 0.5 m 时切到下一点，每次 `Command_ID` 加 1；
  - rosbridge（V0.5）：以 50 Hz 发 Move_mode=6（TRAJECTORY）。注意在 PX4_ORIGIN 控制器下，加速度前馈会被丢掉。

#### Orbit

- **Mock**：tracker 走圆：`p = c + R[cos θ, sin θ]`，`θ̇ = ±v/R`，yaw 朝向圆心，并累计已飞圈数。
- **PX4 SIH**：
  - 发 `COMMAND_INT 34 DO_ORBIT`：p1 = ±R（正值为顺时针），p2 = v，p3 = 0（机头朝向圆心），x、y 为圆心的纬度、经度 ×1e7，z 为 AMSL；
  - PX4 进入 nav 21；
  - **p4 被忽略（F7）**，圈数由 Gateway 计数，绕完后发 Hover。
- **Prometheus**：由 Gateway 仿真：地面站协议下以 5 Hz 在圆上发超前的 XYZ_POS carrot 点（速度 ≤1 m/s）；rosbridge 下发 TRAJECTORY 流。

#### Hover

- **Mock**：STOP_MOTION 按最大加速度刹停，然后保持当前位置。
- **PX4 SIH**：
  - 发 `COMMAND_LONG 176 DO_SET_MODE`：p1=1，p2=4（AUTO），p3=3（LOITER）；
  - 进入 nav 4；
  - 如果此前在发 offboard 流，等模式切换完成后再停流。
- **Prometheus**：发 108 `{Agent_CMD:2, Control_Level:0}`。

#### RTL

- **Mock**：RTL FSM 依次经过：CLIMB（升到 `rtl.alt`=30 m，2 m/s）→ CRUISE（5 m/s）→ DESCEND → FINAL（1 m/s）→ LANDED → DISARMED。
- **PX4 SIH**：
  - 发 `COMMAND_LONG 20 NAV_RETURN_TO_LAUNCH`，进入 nav 5；
  - 相关参数：`RTL_RETURN_ALT`（mc_defaults 为 30）、`RTL_DESCEND_ALT`（10）、`RTL_LAND_DELAY`（0）。
- **Prometheus**：**原生方式不可用（F12）**，由 Gateway 仿真：
  - 依次发 XYZ_POS：(当前 xy, rtl_alt) → (home xy, rtl_alt) → (home xy, Takeoff_height)，然后发 `Agent_CMD:3`；
  - home 取 armed 上升沿时的位置；
  - RTL 的各个子阶段由 Gateway 驱动。

#### Velocity

- **Mock**：
  - CLIENT_DATA 以 20–50 Hz 送达，超过 200 ms 的包直接丢弃；
  - 500 ms 看门狗，超时后转 Hover；
  - 零速轴保持：\|v\|≤0.09 m/s 视为锁定该轴，漂移超过 0.04 m 时下发 `v = −1.8·drift`。
- **PX4 SIH**：
  - 发 `SET_POSITION_TARGET_LOCAL_NED`，frame 为 LOCAL_NED 或 BODY_NED；type_mask 为 `0x09C7`（控速度加 yaw）或 `0x05C7`（控速度加 yaw 角速率）；
  - 预先流 0.5 s 后发 `DO_SET_MODE(1,6,0)`，进入 nav 14；
  - Gateway 看门狗 500 ms 超时后切 LOITER，赶在 PX4 的 `COM_OF_LOSS_T`（1 s）之前；
  - **SIH 需要设 `COM_OBL_RC_ACT=5`（Hold）**。
- **Prometheus**：
  - 以 10 Hz 发 108 `{Agent_CMD:4, Move_mode:2}`；需要定高时用 `Move_mode:1`；yaw 用 `yaw_ref`，或用 `Yaw_Rate_Mode` 加 `yaw_rate_ref`；
  - 零速轴保持由机上原生实现；
  - **机上没有超时**：Gateway 看门狗 500 ms 超时后发 Current_Pos_Hover；如果地面站 TCP 断开，就只剩围栏能让飞机停下（见 §11）。

#### SafetyStop

- **Mock**：进入 HOLD/SAFETY_STOP，加锁，按最大加速度刹停；租约挂起，owner 变为 SAFETY。
- **PX4 SIH**：
  - 先停止 offboard 流，然后发 `DO_SET_MODE` 切 LOITER；
  - PX4 本身没有锁的概念，由 Gateway 拒绝所有非安全类命令，返回 `LOCKED`；
  - 如果有别的地面站改了模式，owner 变为 EXTERNAL 并置 ALERT。
- **Prometheus**：
  - 发 108 `{Agent_CMD:2, Control_Level:1}`（ABSOLUTE），机上会发布 `stop_control_state=true`，规划器随之停下；
  - 解锁发 108 `{Agent_CMD:2, Control_Level:2}`（EXIT_ABSOLUTE）；
  - 回显里没有 Control_Level，锁状态只能由 Gateway 自己跟踪。

#### Pause

- **Mock**：任务引擎挂起当前游标，tracker 执行 STOP_MOTION；状态为 FLYING/HOVER，mission=PAUSED；`resume` 时从同一段继续。
- **PX4 SIH**：
  - mission 模式：`DO_SET_MODE` 切 LOITER，PX4 会保留当前航点序号；`resume` 时发 `DO_SET_MODE(1,4,4)`，从 MISSION_CURRENT 继续；
  - offboard 模式：Gateway 停止发流并切 LOITER；恢复时对剩余轨迹重新做时间参数化，再切回 OFFBOARD；
  - **`DO_PAUSE_CONTINUE` 返回 UNSUPPORTED（F6）**。
- **Prometheus**：Gateway 保留游标并发 108 Current_Pos_Hover（DEFAULT 等级）；`resume` 时重新发当前航点。

**SIH 参数覆写**（写入 `sim/backends/px4/sih_params.yaml`）：

| 参数 | 值 | 原因 |
|---|---|---|
| `COM_OBL_RC_ACT` | 5 | offboard 丢失后转 Hold。默认值 0 需要遥控器，SIH 没有 |
| `NAV_DLL_ACT` | 0 | 保持禁用。链路丢失由 Supervisor 处理（1.5 s WARN → 3 s HOLD → 13 s RTL）；如果交给 PX4 处理，改为 2（RTL） |
| `COM_LOW_BAT_ACT` | 3 | 电量 critical 时 RTL，emergency 时就地降落，与 r24 的阈值一致 |
| `MIS_TAKEOFF_ALT` | 2.5 | — |
| `RTL_RETURN_ALT` | 30 | — |
| `COM_DISARM_PRFLT` | 10 | — |

**消息速率**：`SET_MESSAGE_INTERVAL`，在 r20/r22 的列表基础上增加 `CURRENT_MODE(436)`，间隔 250 000 µs。

### 6.4 ACK、读回与完成判据

**(a) 各后端的通用证据**

| 阶段 | Mock | PX4 SIH | Prometheus |
|---|---|---|---|
| `accepted` 的依据 | 准入通过后的同一个 tick 内由 FSM 确认（进程内调用，`native_ack=true`） | COMMAND_ACK 的 result 为 ACCEPTED(0)；IN_PROGRESS(5) 发 progress 帧；复合命令（arm 加 takeoff）要求每一步都拿到 ACK | 发送时 control_state 必须为 2（设置类命令除外），然后在 1 s 内收到带本次 `Command_ID` 的 UAVCommand 回显。设置类命令以 armed 或 control_state 的跳变作为依据 |
| `running` 的依据 | 下一 tick 的 FlightState/sub 等于预期值 | CURRENT_MODE 的 `custom_mode` 等于预期 nav_state（4 Hz 下 ≤1.5 s）。**如果 `intended_custom_mode` 已经是预期值而 `custom_mode` 不是，并持续 1.5 s，判 failed MODE_NOT_ENTERED**。PX4 Orbit 还要看 `ORBIT_EXECUTION_STATUS` 的半径与请求一致 | 回显中的 `position_ref`（来自 PX4 target_local）与命令值相差小于 0.3 m（GoTo、Hover、航点类）；或 control_state 或 armed 按预期跳变（Takeoff、Land） |
| 信任等级（accepted / running / succeeded） | V1 / V2 / **V4 + simulated** | V1 / V2 / V2；使用 SIH 真值端口时 succeeded 为 V4 + simulated | V1（回显）/ V2 / V2；真机有独立 RTK 或 mocap 旁路时 succeeded 为 V3 |

**(b) 各命令的完成判据**

判据写在规范态上，因此与后端无关。这正是统一 FlightState 的收益。

| 命令 | running 期望 | succeeded 判据 | 专有的 failed 判据 | 截止时间 |
|---|---|---|---|---|
| Takeoff | TAKING_OFF.* | FLYING，且 \|z−z_t\| < max(0.3, 5%·alt)，且 \|vz\| < 0.3，持续 1 s | 离开 TAKING_OFF 后进入的不是 FLYING | alt/1.5×1.5 + 10 s，再加解锁的 2 s |
| Land | LANDING.* | 依次经过 LANDED 并到达 DISARMED | 被 ELAND/FAILSAFE 抢占时判 failed PREEMPTED_BY_SAFETY，effect 的 metrics 里仍会记录"已触地" | alt/0.7×1.5 + 10 s |
| GoTo | FLYING.GOTO | ‖p−p_t‖ < tol（Mock 0.5 m，PX4 与 Prometheus 1.0 m），且 \|v\| < 0.5，持续 1 s | 还没到达就离开了 GOTO | 1.5·ETA + 10 s，其中 ETA = d/v + v/a |
| FollowPath | FLYING.PATH | 最后一点满足到达判据；PX4 mission 模式下为收到 `MISSION_ITEM_REACHED` 且 seq=N−1 | 任务被外部清除 | 1.5·ΣETA + 10 s |
| Orbit | FLYING.ORBIT | turns>0 时，累计转角 ≥ 2π·turns，之后自动 Hover；turns=0 时，圆轨迹稳定即判为 succeeded（\|r−R\| < 1 m 且 \|v_t−v\| < 0.5，持续 3 s），之后一直绕到被取代为止 | PX4 的 `ORBIT_EXECUTION_STATUS` 显示半径不符时判 failed PARAM_REJECTED | 到达圆轨迹所需时间 + 10 s |
| Hover | FLYING.HOVER | \|v\| < 0.3，持续 1 s | — | 10 s |
| RTL | RTL.* | `land=true` 时：DISARMED 且 ‖p_xy−home‖ < 2 m。`land=false` 时：在 home 上空 rtl_alt ± 1 m | — | 1.5·t_rtl + 20 s |
| Velocity | FLYING.VELOCITY | 会话建立即判为 accepted；客户端调用 `velocity/stop` 时判 succeeded；被其他命令取代时判 canceled SUPERSEDED；看门狗超时时判 canceled WATCHDOG | — | — |
| SafetyStop | HOLD.SAFETY_STOP | \|v\| < 0.3，持续 0.5 s | — | 5 s |
| Pause | FLYING.HOVER 且 mission=PAUSED | \|v\| < 0.3，持续 1 s | — | 10 s |

**通用的 failed 判据**：

| 情况 | 结果 |
|---|---|
| FlightState 严重度高于预期（Supervisor 或飞控抢占） | `PREEMPTED_BY_SAFETY` |
| ctrl.owner 变为 PILOT 或 EXTERNAL | `PREEMPTED_BY_PILOT` |
| 生命周期 LOST | `VEHICLE_LOST` |
| 进入 CRASHED | `CRASHED` |
| 巡航段 5 s 内前进不足 0.2 m | `STALLED` |
| 超过截止时间 | `PROGRESS_TIMEOUT` |

### 6.5 后端能力声明

随 advertise 下发，前端据此把不支持的按钮置灰，Gateway 准入时同样读取这份声明。

```json
{"mock":{"cmd":{"takeoff":"native","land":"native","goto":{"impl":"native","speed":true},"follow_path":"native",
   "orbit":{"impl":"native","turns":true},"hover":"native","rtl":"native","velocity":"native","safety_stop":"native",
   "pause":"native","kill":"native"},"ack":{"native":true,"readback":"fsm"},"trust_ceiling":4,"truth":true},
 "px4_sih":{"cmd":{"takeoff":"native","land":"native","goto":{"impl":"native","speed":true},
   "follow_path":{"impl":"native","modes":["mission","offboard"]},"orbit":{"impl":"native","turns":"gateway","radius":[1,1000]},
   "hover":"native","rtl":"native","velocity":{"impl":"native","via":"offboard"},"safety_stop":{"impl":"native","lock":"gateway"},
   "pause":{"impl":"emulated","via":"AUTO_LOITER"},"kill":"native"},
   "ack":{"native":true,"timeout_s":0.5,"retries":3,"readback":["CURRENT_MODE","EXTENDED_SYS_STATE","HEARTBEAT"]},
   "trust_ceiling":2,"truth":"optional"},
 "prometheus":{"cmd":{"takeoff":{"impl":"native","yaw_fixed":0},"land":{"impl":"native","requires_native":"COMMAND"},
   "goto":{"impl":"native","speed":false,"vmax":1.0},"follow_path":{"impl":"gateway","transport":"xyz_pos"},
   "orbit":{"impl":"gateway"},"hover":"native","rtl":{"impl":"gateway"},
   "velocity":{"impl":"native","guard":"gateway_watchdog","vmax":1.0},"safety_stop":{"impl":"native","lock":"absolute"},
   "pause":{"impl":"gateway"},"kill":"none"},
   "ack":{"native":false,"echo":"Command_ID","readback":["control_state","target_local"]},"trust_ceiling":2,"truth":false}}
```

---

## 7. ACK 语义统一

### 7.1 调用生命周期

```text
            准入失败（≤5 ms）                原生拒绝（DENIED/TEMP_REJECTED/FAILED/UNSUPPORTED/NOT_IN_CONTROL）
 call ──┬──────────────────────► REJECTED ◄──────────────────────────┐
        │ 准入通过：progress{phase:"admitted"}                        │
        └─► DISPATCHED ──原生 ACK 或进程内确认──► ACCEPTED ──读回符合预期──► RUNNING ──完成判据──► SUCCEEDED
              │ 超过 ACK 时限                        │  被取代或被取消            │  抢占、失联、停滞、超时
              ▼                                      ▼                            ▼
           TIMEOUT                                CANCELED ◄──────────────────  FAILED
```

- 线上状态沿用 r27 的七个值。`DISPATCHED` 只在服务端内部使用，线上用 `progress{phase}` 表示。
- **修订 r27 §3.12**：`accepted` 表示"准入通过并拿到原生确认"，而不是"只过了准入"。这样与 MAV_RESULT_ACCEPTED 以及 r21 的 `ResultStatus.ACCEPTED` 同义。
- 对 Mock 而言，这两个时刻落在同一个 tick。对 PX4 和 Prometheus，准入通过后 ≤20 ms 先推 `progress{phase:"admitted"}`，UI 据此开始转圈，transitions.dev 的 150 ms 阈值之内看不到跳变。

### 7.2 帧示例

```json
C: {"op":"call","id":"c-7f3a","service":"uav/sih-02/cmd/goto","args":{"pos":[120,40,60],"speed":5},"timeout_ms":3000}
S: {"op":"progress","id":"c-7f3a","data":{"phase":"admitted"}}
S: {"op":"result","id":"c-7f3a","status":"accepted","code":0,
    "effect":{"status":"UNVERIFIED","verify_trust":1,"native_ack":true,"protocol":"mavlink2","requested":"goto enu=(120,40,60) v=5"}}
S: {"op":"result","id":"c-7f3a","status":"running","code":0,
    "effect":{"status":"UNVERIFIED","verify_trust":2,"observed_state":"nav_state=AUTO_LOITER intended=AUTO_LOITER"}}
S: {"op":"progress","id":"c-7f3a","data":{"phase":"executing","dist_m":82.1,"eta_s":16.4}}
S: {"op":"result","id":"c-7f3a","status":"succeeded","code":0,"final":true,
    "effect":{"status":"OK","verify_trust":2,"metrics":{"dist_err_m":0.41,"t_exec_s":21.7},"latency_ms":21730}}

C: {"op":"call","id":"c-9b01","service":"uav/p600-01/cmd/goto","args":{"pos":[10,0,5]}}
S: {"op":"result","id":"c-9b01","status":"rejected","code":10,"reason":"NOT_IN_CONTROL","final":true,
    "message":"pilot holds control (RC_POS_CONTROL)","effect":{"status":"UNAVAILABLE","verify_trust":0}}
```

### 7.3 时限常数

| 阶段 | Mock | PX4 SIH | Prometheus |
|---|---|---|---|
| 准入 | ≤5 ms | ≤5 ms | ≤5 ms |
| 原生 ACK | 同一 tick（250 Hz 下 ≤4 ms） | 单次 0.5 s，重试 3 次，共 2.0 s；收到 IN_PROGRESS 后延长到 3.0 s（照搬 MAVSDK） | 回显 1.0 s；超时后用**同一个** Command_ID 重试 1 次，BODY 模式在机上只会执行一次 |
| 模式或状态读回 | 下一个遥测 tick（≤33 ms） | CURRENT_MODE 为 4 Hz 时 ≤1.5 s；如果 SET_MESSAGE_INTERVAL 失败，退回 HEARTBEAT（1 Hz），时限 ≤2.5 s | control_state 为 10 Hz 时 ≤1.0 s |
| 进度推送 | ≤2 Hz | ≤2 Hz | ≤2 Hz |
| 完成截止 | 见 §6.4(b) | 同左 | 同左，ETA 按 vmax=1 m/s 计算 |
| 幂等窗口 | 60 s，同一个调用 id 只执行一次（r27） | 同左 | 同左 |

### 7.4 原因码（`code`，唯一真源为 `packages/contracts/rt/reasons.json`）

| 区间 | 码 | 名称 | 典型来源 | HTTP |
|---|---|---|---|---|
| 0–10 | 与 MAV_RESULT 同值 | 0 OK、1 TEMPORARILY_REJECTED、2 DENIED、3 UNSUPPORTED、4 FAILED、6 CANCELLED、10 NOT_IN_CONTROL | PX4 的 COMMAND_ACK；Prometheus control_state≠2 时 Gateway 也用 10 | 200 / 409 / 422 / 501 |
| 100–114 | 准入 | 100 LEASE_DENIED、101 SAFETY_ACTIVE、102 GEOFENCE_REJECT、103 PREFLIGHT_FAILED、104 NOT_ARMED、105 STATE、106 DUPLICATE、107 NO_VEHICLE、108 LINK_ERROR、109 BACKEND_UNSUPPORTED、110 PARAM_OUT_OF_RANGE、111 RATE_LIMITED、112 CONFIRM_REQUIRED、113 LOC_NOT_READY、114 LOCKED | Gateway | 409 / 404 / 503 / 501 / 422 / 429 / 428 |
| 200–210 | 执行 | 200 ACK_TIMEOUT、201 MODE_NOT_ENTERED、202 PROGRESS_TIMEOUT、203 STALLED、204 PREEMPTED_BY_SAFETY、205 PREEMPTED_BY_PILOT、206 SUPERSEDED、207 VEHICLE_LOST、208 CRASHED、209 WATCHDOG、210 LEASE_PREEMPTED | CommandEngine | 504 / — |

r27 原来用字符串表示原因（"GEOFENCE"、"NOT_ARMED"、"AUTHORITY"），现在改为 102、104、100，`reason` 字段仍然给出名称。

### 7.5 调用状态与 ANet 效果的对应

| 调用状态 | effect.status | verify_trust | Evidence 要求 |
|---|---|---|---|
| progress admitted | 不带 effect | — | — |
| accepted | UNVERIFIED | 有原生确认为 V1，否则为 V0 | `native_ack`、`protocol`、`requested` |
| running | UNVERIFIED | V2 | `observed_state`，例如模式读回的内容 |
| succeeded | **OK** | 用估计值读回为 V2；有独立旁路为 V3；仿真真值为 V4，同时置 `simulated=true` | **必须带 metrics**，例如 dist_err_m、alt_err_m、t_exec_s。这样 `effect.Verifiable()` 才为真，TSIR 的 THRESHOLD 也才有数可比 |
| rejected（Gateway 准入） | UNAVAILABLE | V0 | `message` 写原因 |
| rejected（原生拒绝） | FAILED | V1 | `native_ack=true`，`observed_state` 写 MAV_RESULT |
| timeout | UNAVAILABLE | V0 | — |
| failed | FAILED | V2 | `observed_state` 写抢占方或当时的状态 |
| canceled | UNVERIFIED | 保持 running 时的值 | `message="superseded"` 或 `"canceled"`，对应 d05 的 reason=interrupted |

Agent Runtime 按 d05 的规则判定"已验证"：`OK` 且 `verify_trust ≥ 2`，或者 `simulated=true`。`quirk` 字段记录本文发现的偏差修正，例如 `prometheus.control_state.ros_enum`、`px4.orbit.param4_ignored`、`prometheus.rtl.emulated`。

### 7.6 取代、取消与暂停

- 同一架机上，新的导航类命令会取代正在进行的导航类调用，被取代的调用判 `canceled SUPERSEDED`。安全类命令可以取代一切。
- `pause` 不会取消 path 或 orbit 调用，只把它们的 progress 标为 `phase:"paused"`；`resume` 后恢复为 `executing`。
- `cancel{call_id}` 相当于先发一次 Hover，再把该调用判为 canceled。
- 被租约抢占时，原持有者名下所有进行中的调用都判 `failed PREEMPTED_BY_PILOT`（对方是 PILOT 时）或 `LEASE_PREEMPTED`，并推送事件 `agent.lease.preempted`。

**UI 映射**（shadcn）：

| 调用状态 | 呈现 |
|---|---|
| accepted、running | Button 显示 loading，用 `StateIcon` 的 spinner |
| succeeded | Sonner 中性提示（白色） |
| rejected、failed、timeout | Sonner 红色提示，附 reason 与 message |
| canceled | 灰色提示 |

详情 `Popover` 中用小 `Badge` 显示信任等级，例如"V2 读回"、"V4 仿真真值"。

---

## 8. 伪代码

### 8.1 StateFuser（每个 Gateway tick，按 SoA 向量化）

```python
def fuse(v: Vehicle, now) -> Record:
    raw = v.adapter.snapshot(v.vid)                        # 只取最新值，不阻塞
    lc  = orchestrator.lifecycle(v.vid)
    if v.backend == "mock":    fs, sub, fsafe = v.fsm.state, v.fsm.sub, v.fsm.auto_active; native = mock_native(fs, sub)
    elif v.backend == "px4":   fs, sub, fsafe = derive_px4(raw, v.intent); native = native_px4(raw.armed, nav_of(raw))
    else:                      fs, sub, fsafe, native = derive_prometheus(raw, v.intent)
    fs, sub, fsafe = merge_overlay(v, fs, sub, fsafe)     # §8.2
    flags = (ARMED*raw.armed | IN_AIR*v.in_air(raw) | loc_bits(v) | FAILSAFE*fsafe
             | GCS*v.gcs_ok(now) | FCU*v.fcu_ok(now) | ALERT*v.alerts.any_active())
    fs, sub, flags = apply_lifecycle(lc, fs, sub, flags)
    owner = SAFETY if (fsafe or v.intent.lock) else PILOT if native == MANUAL else v.external_owner or v.lease.owner_class
    ctrl  = pack_ctrl(owner, v.intent.lock or v.lease.override, native, v.pose_src)
    if (fs, sub) != v.last_fs: events.emit("uav.state", v.vid, frm=v.last_fs, to=(fs, sub), reason=v.reason); v.last_fs = (fs, sub)
    return Record(v.agent_no, pack_fs(fs, sub), flags, v.mission_item, v.battery_pct, ctrl, *v.kinematics)
```

### 8.2 Supervisor 覆盖层合并（单一权威）

```python
def merge_overlay(v, fs, sub, fsafe):
    ov = v.supervisor.active_action()                   # 例如 (CORRECTING, GEOFENCE) 或 (HOLD, SEPARATION)
    if ov is None: return fs, sub, fsafe
    if v.backend == "mock": return ov + (True,)         # Mock 的 FSM 本身就是权威
    if SEVERITY.get(fs, -1) > SEVERITY[ov[0]]:          # 飞控自身动作更严重：以飞控为准，撤销覆盖层
        v.supervisor.yield_to_autopilot(fs); return fs, sub, fsafe
    if not v.supervisor.native_mode_confirmed(ov):      # 覆盖层动作尚未被原生态确认（例如还没进 LOITER）
        return fs, sub, fsafe                            # 继续显示原生态，并等待覆盖层动作确认
    if native_diverged(v):                               # 被遥控器或其他地面站接管
        v.supervisor.clear(reason="SAF.OVERRIDDEN"); return fs, sub, fsafe
    return ov + (True,)
```

### 8.3 CommandEngine

```python
async def handle_call(req):                              # req = {id, service, args}，owner 来自会话
    if (r := idem.get(req.id)): return r                 # 60 s 内同一 id 只执行一次
    v, cmd = registry[req.vid], parse(req.service, req.args)
    if (code := admit_all(v, cmd, req.owner)): return final(req, "rejected", code, effect(UNAVAILABLE, 0))
    progress(req, phase="admitted")
    supersede(v, cmd)                                    # 取代前一个导航类调用：canceled SUPERSEDED
    ack = await v.adapter.dispatch(cmd, deadline=ACK_T[v.backend])       # Mock / PX4 / Prometheus 各自实现
    if ack.timeout:  return final(req, "timeout", 200, effect(UNAVAILABLE, 0))
    if not ack.ok:   return final(req, "rejected", ack.code, effect(FAILED, 1, native_ack=True))
    send(req, "accepted", 0, effect(UNVERIFIED, 1 if ack.native else 0))
    v.intent.set(cmd)                                    # 供 §4.2 / §4.3 的推导使用
    w = Watch(expect=RUNNING_EXPECT[cmd.kind], done=DONE_PRED[cmd.kind], deadline=deadline_for(v, cmd))
    async for st in v.state_stream():                    # 与 StateFuser 同一个 tick
        match w.step(st):
            case "running":   send(req, "running", 0, effect(UNVERIFIED, 2, observed=st.describe()))
            case "progress":  progress(req, **w.progress(st))
            case "succeeded": return final(req, "succeeded", 0, effect(OK, trust_of(v), metrics=w.metrics(st)))
            case ("failed", code): return final(req, "failed", code, effect(FAILED, 2, observed=st.describe()))
            case ("canceled", code): return final(req, "canceled", code, effect(UNVERIFIED, w.trust))
```

### 8.4 Prometheus Adapter 的关键防护

```python
async def dispatch(self, cmd):
    s = self.state
    if cmd.kind in NEEDS_COMMAND and s.control_state != 2:         # F13：绝不在非 COMMAND 状态下发 UAVCommand
        return Ack(ok=False, code=NOT_IN_CONTROL)
    if cmd.kind == "rtl":   return self.start_emulated_rtl()        # F12：不发 SET_PX4_MODE AUTO.RTL
    if cmd.kind == "kill":  return Ack(ok=False, code=BACKEND_UNSUPPORTED)
    if s.abs_lock and cmd.level != ABSOLUTE and cmd.kind not in ("resume", "land"):
        return Ack(ok=False, code=LOCKED)                            # Gateway 自己跟踪 ABSOLUTE 锁
    cid = self.next_cmd_id(); self.send(108, uav_command(cmd, cid, T_local_from_world))
    ok = await self.wait_echo(cid, timeout=1.0) or (self.resend(cid) and await self.wait_echo(cid, 1.0))
    return Ack(ok=ok, native=False, code=0 if ok else ACK_TIMEOUT)
# 看门狗：velocity 会话 500 ms 没有新 setpoint，或 GS 的 TCP 断开并重连后，立即发 108 {Agent_CMD:2}
```

---

## 9. 落地文件与测试

| 路径 | 内容 | 版本 |
|---|---|---|
| `packages/contracts/rt/enums.json` | §5.3 | V0.1 |
| `packages/contracts/rt/layouts.json` | §5.2，Full64 与 Lite32 v1，带 `bits` 描述 | V0.1 |
| `packages/contracts/rt/commands.json` | §6.1 服务名与参数 schema；§6.2 准入矩阵（原型可直接导出）；§6.4(b) 完成判据参数 | V0.1 |
| `packages/contracts/rt/reasons.json` | §7.4 | V0.1 |
| `packages/contracts/rt/caps/*.json` | §6.5 | V0.1（Mock）/ V0.2 |
| `sim/core/state_model.py` | 由原型转正：枚举、打包、准入 | V0.1 |
| `sim/core/fuser.py`、`sim/core/command.py`、`sim/core/authority.py` | §8.1、§8.3，以及租约管理 | V0.1 |
| `sim/backends/px4/derive.py`、`sim/backends/px4/sih_params.yaml` | §4.2、§6.3 | V0.2 |
| `sim/backends/prometheus/derive.py`、`sim/backends/prometheus/adapter.py` | §4.3、§8.4 | V0.2（模拟器）/ V0.5（真机） |
| `apps/web/src/net/rt/{layouts,enums}.ts`（生成） | §5.4 | V0.1 |
| `apps/web/src/features/drone/state-ui.ts` | §3.1 的 Badge、图标与文案映射 | V0.1 |

**测试**

1. `tests/contracts/test_state_model.py`：原型的 `_selftest` 全部 7 组断言，包括穷举、黄金向量、准入抽查和字节往返。
2. `tests/backends/test_px4_derive.py`：用 r21 的 `fake_px4.py` 回放 HEARTBEAT、CURRENT_MODE、EXTENDED_SYS_STATE 序列。覆盖 takeoff → reposition → orbit（包括半径越界）→ RTL → 自动上锁，以及 `intended ≠ custom` 的场景。
3. `tests/backends/test_prometheus_derive.py`：用 r19 `codec.py` 构造的帧，覆盖以下场景：
   - control_state=2 必须解码为 COMMAND；
   - 非 COMMAND 状态下的 GoTo 在准入阶段被拒，并且**确认没有任何 108 帧发出**；
   - RTL 走仿真序列；
   - 速度看门狗。
4. `apps/web/src/net/rt/__tests__/layouts.test.ts`：Python 生成 fixture，TS 端解码后比对 `fs`、`sub`、`flags`、`ctrl`。
5. E2E（Mock，50 架）：每种命令各跑一遍完整生命周期，截图断言 Badge 与 Toast。在减少动效（reduced-motion）下同样验证。

---

## 10. 对已有文档的修订

| 文档与节 | 修订 |
|---|---|
| r27 §3.5 | byte 7 由 `authority` 改为 `ctrl`（§4.9）；flags 的 bit6 由 rtk_fix 改为 LOC_DEGRADED，bit7 由 simulated 改为 ALERT；`flight_state` 采用 5+3 位打包；Lite32 的 byte 31 改为 ctrl；schema 更名为 `awr.*`；advertise 的 `agents[]` 增加 `backend`、`simulated`、`model` |
| r27 §3.12 | `accepted` 改为"准入通过且拿到原生确认"；每个 result 帧附带 `effect`；reason 改为数值 code（§7.4）；服务表增加 `hover`（`hold` 为别名）、`safety_stop`、`pause`、`resume`、`cancel`、`velocity`、`escalate` |
| r24 §4.3、§4.4 | 枚举编号采用 §3.1，并新增 UNKNOWN；白名单按 §3.2 补充；明确"由操作员发起的 RTL/LANDING 可以被导航命令覆盖"；`mrs_mock.py` 的 FS 重新编号 |
| r19 §4.3、§4.4 | `FlightMode` 由 FlightState 加子模式取代；删除"RTL 用 UAVSetup AUTO.RTL"的方案，改为 Gateway 仿真；TAKEOFF 的第 ③ 步改为可选；新增硬规则"发 UAVCommand 前 control_state 必须为 2"；Velocity 必须配 Gateway 看门狗 |
| r22 §3.3 | 生命周期中去掉 ACTIVE；在 §4.6 投影表中给出 UNKNOWN 的子模式 |
| r20 §3.5 | nav_state 表补上 6–9；Hover 用 DO_SET_MODE(1,4,3)；Pause 在 PX4 上无原生支持；`SET_MESSAGE_INTERVAL` 增加 CURRENT_MODE；SIH 参数覆写（§6.3） |
| r21 §3.2、§4.1 | 保留 `ResultStatus`，它对应本文的 CallStatus 加 code；**custom_mode 解码不用 MAVSDK 的头文件**（F2） |
| d05 §2.4、§4.2 | 采用 §7.5 的映射表；succeeded 必须带 metrics；列出本文的 quirk 名称 |
| r08 §4.2.4 | 定位状态到 flags 的映射与 arm 门控，见 §4.7 |
| 00-index §3.9、§3.11、C12、G4 | §3.9 中"三者关系需统一"的说法由本文给出结论；§3.11 的载荷改为定稿后的位定义；C12 与 G4 标为已关闭 |

---

## 11. 风险与注意事项

1. **意图依赖**：PX4 LOITER 下区分 GOTO、HOVER 与 LANDING/GOTO 要靠 Gateway 的 `intent`。Gateway 重启会丢失意图，此后会暂时显示为 HOVER，直到收到下一条命令。缓解办法是把 intent 写入会话存储（Redis 或 SQLite），重启后恢复。
2. **Prometheus 速度指令的安全性**：机上没有超时。如果 Gateway 进程崩溃，飞机会一直按最后的速度飞，直到触发围栏。真机上的 Velocity 要求同时满足以下条件，否则 UI 置灰：
   - 走 rosbridge，并部署一个机上看门狗节点（约 30 行，订阅 `/uavN/prometheus/command` 的时间戳，超过 0.5 s 就发 Current_Pos_Hover）；
   - 限速 ≤1 m/s；
   - RTT 小于 200 ms。
3. **模式抢夺**：QGC 与 Gateway 同时连接 Prometheus 或 PX4 时，模式可能来回切换。§4.3 的检测会报警，但无法阻止。部署规范应要求真机只保留一个指挥通道（r21 R10）。
4. **子模式容量**：HOLD 已用 7 个，RTL 已用 5 个。若还需扩展，先放进 `state_ext.sub_mode_name`（字符串），等语义稳定后再升级 schema 到 v2。**旧客户端按 `size` 跳过记录的规则不受影响**。
5. **PX4 读回时延**：`SET_MESSAGE_INTERVAL` 如果被拒（例如链路不是 onboard 模式），读回会退回 1 Hz 的 HEARTBEAT，running 最多晚 2.5 s 才能确认。UI 在 `admitted` 之后的转圈必须能持续这么久，不能在 1 s 时就判超时。
6. **ELAND 与 LANDING+FAILSAFE 的取舍**：PX4 由失效保护触发的 AUTO_LAND 被映射为 LANDING 加 FAILSAFE 位（红描边），而不是 ELAND（红实底）。这与 r24"电量 emergency 时就地 LANDING"的设定一致。只有 PX4 DESCEND（无位置控制）会映射到 ELAND。产品上如果希望所有自动降落都显示成红实底，只需改 UI 规则，线上格式不用动。
7. **Mock 显示仿真是有损的**（§4.1 列出的 9 处）。凡是涉及飞行语义的逻辑一律读规范态，`state_ext.px4` 只用于显示和调试面板。
