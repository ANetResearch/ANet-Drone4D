"""M25 传感器生成服务（D3）：SensorBackend v2、网格光栅相机、网格 LiDAR、IMU/GNSS 与 GPU worker。

D2 代码不得 import 本包（import 方向规则）；`awr.sensorsvc.camera` 为可打包核心，只依赖 numpy、Pillow 与 moderngl（M25 §6.1）。
"""
