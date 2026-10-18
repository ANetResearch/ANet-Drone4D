"""LiDAR 型号参数（M25 §6.8.1）：由 M21 型号文件 `vehicles/sensor_models/<model>.yaml`（`awr.sensor.lidar_livox.v1`）读取。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = ["MOUNT_INVERTED_MAPPING", "LidarModel", "load_lidar_model"]

MOUNT_INVERTED_MAPPING = ((0.0, 0.0, -0.08), (180.0, 0.0, 0.0))     # p600_mid360_inv（ADR-157）


@dataclass(frozen=True)
class LidarModel:
    model_id: str = "mid360"
    point_rate_hz: int = 200_000
    frame_rate_hz: int = 10
    slots_per_frame: int = 20_000
    point_dt_ns: int = 5_000
    v_min_deg: float = -7.2
    v_max_deg: float = 52.2
    blind_m: float = 0.1
    r_at_rho10_m: float = 40.0
    exponent: float = 0.269
    hard_max_m: float = 100.0
    range_sigma_near_m: float = 0.03
    range_sigma_far_m: float = 0.02
    angle_sigma_deg: float = 0.1
    false_alarm: float = 1.0e-4
    pose_nodes: int = 11

    @property
    def frame_ns(self) -> int:
        return 1_000_000_000 // self.frame_rate_hz

    def r_max(self, rho_eff):
        """r_max(rho) = min(r_hard, 40·(rho/0.1)^0.269)。"""
        import numpy as np

        return np.minimum(self.hard_max_m, self.r_at_rho10_m * np.power(np.maximum(rho_eff, 1e-6) / 0.1, self.exponent))


def load_lidar_model(path: Path | str | None = None) -> LidarModel:
    import yaml

    p = Path(path) if path else Path(__file__).resolve().parents[4] / "vehicles" / "sensor_models" / "mid360.yaml"
    d = yaml.safe_load(p.read_text(encoding="utf-8"))
    rate = int(d["pattern"]["point_rate_hz"])
    fr = int(d["frame_rate_hz"])
    rg, nz = d["range"], d["noise"]
    return LidarModel(model_id=str(d["model_id"]), point_rate_hz=rate, frame_rate_hz=fr, slots_per_frame=rate // fr,
                      point_dt_ns=1_000_000_000 // rate, v_min_deg=float(d["fov"]["v_min_deg"]), v_max_deg=float(d["fov"]["v_max_deg"]),
                      blind_m=float(rg["blind_m"]), r_at_rho10_m=float(rg["r_at_rho10_m"]), exponent=float(rg["exponent"]),
                      hard_max_m=float(rg["hard_max_m"]), range_sigma_near_m=float(nz["range_sigma_near_m"]),
                      range_sigma_far_m=float(nz["range_sigma_far_m"]), angle_sigma_deg=float(nz["angle_sigma_deg"]),
                      false_alarm=float(nz["false_alarm"]))
