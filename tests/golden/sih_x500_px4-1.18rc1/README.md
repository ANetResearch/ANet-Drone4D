# PX4 SIH x500 黄金数据（v1.18.0-rc1）

M08-FR-075 的入库副本：由 r20 研究的 `sih_probe.py` 与 `run_sih_x500.sh` 在具备 docker 的机器上录制（`.cache/research/r20/x500_n3/`
原样复制），本机无法重新生成；重录脚本见 `tools/vehicles/regen_sih_golden/`（D1-ext，M08-FR-079）。任何改动 `vehicles/x500/params.yaml`
回归参数的变更必须同时重录本目录（M08-FR-044）。

## 来源

| 项 | 值 |
|---|---|
| 镜像 | `px4io/px4-sitl:v1.18.0-rc1`（`PX4_SIM_MODEL=sihsim_quadx`，3 个实例 `px4 -i 0..2`，同一容器，`--network host`） |
| SIH 参数（`PX4_PARAM_*` 注入） | `SIH_MASS 2.064`、`SIH_IXX 0.0217`、`SIH_IYY 0.0217`、`SIH_IZZ 0.040`、`SIH_T_MAX 8.55`、`SIH_Q_MAX 0.137`、`SIH_L_ROLL 0.174`、`SIH_L_PITCH 0.174`、`SIH_KDV 0.35`、`SIH_KDW 0.02`、`SIH_T_TAU 0.03`、`MPC_THR_HOVER 0.59`、`CA_ROTOR0..3_PX/PY = ±0.174` |
| 测试 | 三实例都先在 10 m 悬停：inst0 offboard 北向 50 m 位置阶跃（20 Hz 流）；inst1 `DO_REPOSITION` 北 50 m；inst2 AUTO_LOITER 中 `SIH_WIND_N = 8`（来向为北，空气向南流动） |
| 采样 | `LOCAL_POSITION_NED` 与 `ATTITUDE`，约 21 Hz（48 ms） |
| 对齐 | Mock 按 48 ms 采样，比较时整体右移 0.12 s（SIH 指令链路延迟，g08 §9.2） |

## 文件与格式

| 文件 | 内容 | sha256 |
|---|---|---|
| `inst0.csv` | offboard 阶跃 | `7d967f97dfd04cf1b4cf70b7504191aa07f4252214f5b222a5337b8bab371e6a` |
| `inst1.csv` | DO_REPOSITION 50 m | `34df5038b9323f9b77ae458016b4c38fc4c9a749f99dc04ea71b03090f257749` |
| `inst2.csv` | 8 m/s 风悬停 | `2dbcdee7a171c643aacf491451944b3a2c86c53c189124072962bcbd1c89b1f1` |
| `marks.csv` | 事件墙钟时刻（`offboard_arm`、`offboard_step`、`reposition`、`wind`） | `7497c58fdf3cb0d347d13600a94ba47faba7db5171e8e23cf360130b1f2517a2` |
| `log.txt` | 录制日志（每实例约 0.21 核、RTF 0.95） | `c6cd4b8821f1770951b4b31defc063232651aa27ba1926d1323004f0eb35e65f` |
| `models_x500_model.sdf` | gz x500 模型（参考） | `cb76a4afd439f253a8c516b1fcd3528f80347619cfa46bebb620557054fbf125` |
| `models_x500_base_model.sdf` | gz x500 基础模型（参考） | `e807dca3406f7cd5cb3d545898601c3ec17e0e5242e25cf467a69df1812cf436` |

CSV 行：`lpos,<墙钟 s>,<time_boot_ms>,x,y,z,vx,vy,vz`（LOCAL_POSITION_NED）与
`att,<墙钟 s>,<time_boot_ms>,roll,pitch,yaw,rollspeed,pitchspeed,yawspeed`（ATTITUDE）。

## 使用方

- `tests/sim/test_fleet_sih_parity.py`：17 项指标 × 4 种配置 + 反例（M08-AC-005，D1-AC-12）；
- `tests/sim/backends/test_replay.py`：ReplayBackend（L0）加载 inst1 的幽灵机回放（M08-AC-031）。
