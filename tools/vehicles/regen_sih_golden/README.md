# regen_sih_golden（D1-ext，M08-FR-079）

在具备 docker 的机器上重录 `tests/golden/sih_x500_px4-1.18rc1/`（本机无 docker，无法重新生成）。脚本由 r20 研究原样整理：

1. `bash run_sih_x500.sh 3`：在 `px4io/px4-sitl:v1.18.0-rc1` 容器中起 3 个 SIH 实例（`PX4_PARAM_*` 注入 x500 回归参数，
   与 `vehicles/x500/params.yaml` 一致：`SIH_MASS 2.064`、`SIH_T_MAX 8.55`、`SIH_KDV 0.35`、`SIH_T_TAU 0.03`、`SIH_L_ROLL 0.174`、
   `MPC_THR_HOVER 0.59`）；
2. `python sih_probe.py 3 <outdir>`（pymavlink 2.4.50）：三实例解锁起飞到 10 m 悬停，inst0 offboard 北向 50 m 阶跃、inst1
   `DO_REPOSITION` 北 50 m、inst2 `SIH_WIND_N = 8`，记录 `LOCAL_POSITION_NED` 与 `ATTITUDE` 到 `inst*.csv`，事件时刻到 `marks.csv`；
3. 把 `<outdir>` 的 `inst0..2.csv`、`marks.csv`、`log.txt` 覆盖到 `tests/golden/sih_x500_px4-1.18rc1/`，更新 README 中的 sha256，
   运行 `make regress-sih`（`pytest tests/sim/test_fleet_sih_parity.py`），新旧差异随合并说明提交（g08 §9.5）。

PX4 升级时镜像 tag 同步修改；任何 x500 回归参数变化都必须重录（M08-FR-044）。
