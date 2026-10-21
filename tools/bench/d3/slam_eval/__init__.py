"""`slam_eval` 作业（M27-FR-101–110、§6.6.2、§6.7.7；AWR-05 §5.4.6、§5.6.2；D3-AC-14、D3-AC-15、D3-AC-47）。

一个作业对一个"算例"（每机一个 SLAM bag 与真值侧车）执行：bag 合同读回校验（`awr.sensorsvc.slam.bag.verify_slam_bag`，违反 642）
→ 按内存模型申请资源 → 以 `d3-livo` 容器（FAST-LIVO2，GPL-2.0，独立进程，`--network none`）按"模式 × 重复"回放（缺省 LIVO 与 LIO
各 3 次、4 倍速）→ `eval_sim` 轨迹与地图指标 → 分布统计与 D3-AC-14 判定 → `metrics.json`（`drone4d.bench.slam_eval.v1`）。

模块：
- `traj`：TUM 读写、真值插值、Umeyama 刚体对齐、轨迹指标（锚定漂移、对齐 ATE、RPE100 段均值 / 段中位数 / 最大值、终点姿态误差），
  以及真机回归的"估计 vs RTK 位置"ATE；
- `mapeval`：地图点体素降采样、到 GLB 网格的无符号距离（PLY/OBJ/GLB 网格或世界包 `MeshWorldQuery`）、对可观测参考点的完整度、
  F@0.1/0.2/0.5、Chamfer，锚定与对齐两种坐标；真值位姿 LiDAR 参考集（B1）与 L0 离线地图（`awr.bench.eval.lidar_map`）；
- `memmodel`：`mem_limit = 1.5 × (2 GB + 430 B × r_map × T)`、型号核数（MID-360 4 核、4.5 万点/s，ADR-215）与长量程下限
  （≥ 20 GB、4 核，暂定），由资源采样标定 r_map；
- `livo`：FAST-LIVO2 参数 yaml 的模式补丁、容器参数、OOM / 非零退出 / 600 s 日志无增长的 684 判定与一次重试、逐帧耗时解析；
- `report`：按"种子 × 重复"的分布（中位数、p10、p90、最大值）与 D3-AC-14 阈值判定（ADR-215 冻结表；clean 地图到网格以事件零点锚定）；
- `case`：算例目录（`case.json` 或 r1 `rig.json`）读取，外部 bag 经合同写出器重编码（`rebag`）；
- `cli`：子命令 `run`、`eval`、`regress`、`judge`、`memmodel`、`rebag`、`fleet`。

运行：`PYTHONPATH=python python tools/bench/d3/slam_eval <子命令> ...`（宿主进程只需 numpy；`eval` 需要 open3d，缺省在
`d3-sensorsvc` 容器内执行）。镜像构建见 `tools/deploy/dmax/images/slam/`。
"""

SCHEMA = "drone4d.bench.slam_eval.v1"
EVAL_SCHEMA = "drone4d.bench.slam_eval.run.v1"
REGRESS_SCHEMA = "drone4d.bench.slam_regress.v1"
JUDGE_SCHEMA = "drone4d.bench.slam_judge.v1"
EVAL_VERSION = "eval_sim-2"                                   # 2：缺省以事件零点锚定（ADR-215 (1)），`traj.anchor` 记录口径
