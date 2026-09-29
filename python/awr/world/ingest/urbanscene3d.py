"""唯一的 UrbanScene3D 适配器（AWR-03 §4.3；M03-FR-006）：六城 CFG、文件名映射（含空格与大小写差异）与名称表。

数值依据 x01 §3.2–§3.3、g03 `CITIES` 与 AWR-16 §10.2；上海、纽约补地标证据（M03 O-7，否则门禁 G-08 失败）。
测试与其他模块不得自行拼接原始文件名，一律经 `RAW_FILES` 或 `configs/data.yaml` 取得。
"""

from __future__ import annotations

from pathlib import Path

from .manifest import DataConfig, check_raw_file, load_data_config
from .rawply import read_ply
from .types import ConfigError, IngestConfig, Landmark, RawCloud

CITIES: dict[str, IngestConfig] = {
    "shenzhen": IngestConfig(
        "shenzhen", "dataset", 1.0, "+z", False, 0.0, "assumed", "assumed",
        Landmark("China Resources HQ", 22.51694, 113.94167, 5.0), None,
        evidence=("tallest HAG 381.3 m, inferred China Resources HQ (393 m); north not verified",), north_evidence=()),
    "shanghai": IngestConfig(
        "shanghai", "dataset", 1.0, "+z", False, 0.0, "verified", "landmark",
        Landmark("Shanghai Tower", 31.2355, 121.501, 4.0), None,
        evidence=("Shanghai Tower 636.7 m vs 632 m", "Shanghai Tower->Oriental Pearl (-504,+738) vs (-554,+681) m"),
        north_evidence=("Shanghai Tower->Oriental Pearl bearing within 5 deg",)),
    "newyork": IngestConfig(
        "newyork", "dataset", 1.0, "+z", False, 0.0, "verified", "landmark",
        Landmark("70 Pine Street", 40.70639, -74.00750, 5.0), None,
        evidence=("70 Pine 287.4 m vs 290 m", "70 Pine->40 Wall (-172,+64) vs (-152,+67) m"),
        north_evidence=("Battery SW, Brooklyn SE",)),
    "sanfrancisco": IngestConfig(
        "sanfrancisco", "dataset", 10.15, "+z", False, 90.0, "verified", "landmark",
        Landmark("Transamerica Pyramid", 37.7952, -122.4028, 15.0), None,
        evidence=("Transamerica->Sutro 6.25 km / 619 u (x10.10)", "Transamerica->Oracle Park 2.19 km / 216 u (x10.15)",
                  "rotation +90.6/+91.5 deg measured, +90 applied"),
        north_evidence=("residual yaw about +1 deg after the applied +90",)),
    "suzhou": IngestConfig(
        "suzhou", "dataset", 1.0, "+y", False, 0.0, "unknown", "assumed",
        None, (31.30, 120.62, 3.0), evidence=(), north_evidence=()),
    "chicago": IngestConfig(
        "chicago", "dataset", 1000.0, "+z", True, 0.0, "verified", "landmark",
        Landmark("Willis Tower", 41.8789, -87.6358, 181.0), None,
        evidence=("Willis Tower 443.1 m vs 442 m roof", "Willis->Hancock (+1086,+2220) vs (+1066,+2219) m"),
        north_evidence=("Lake Michigan east, Navy Pier east",)),
}

RAW_FILES = {"shenzhen": "Shenzhen_sampled_5m.ply", "shanghai": "shanghai_sampled_5m.ply",
             "newyork": "New York_sampled_5m.ply", "sanfrancisco": "San Francisco_sampled_5m.ply",
             "suzhou": "Suzhou_sampled_5m.ply", "chicago": "Chicago_sampled_5m.ply"}

NAMES = {"shenzhen": ("Shenzhen", "深圳"), "shanghai": ("Shanghai", "上海"), "newyork": ("New York", "纽约"),
         "sanfrancisco": ("San Francisco", "旧金山"), "suzhou": ("Suzhou", "苏州"), "chicago": ("Chicago", "芝加哥")}

DATASET = {
    "name": "UrbanScene3D",
    "version": "virtual_cities-sampled (GitHub Release v0.0.1)",
    "url": "https://vcc.tech/UrbanScene3D",
    "citation": "Lin et al., Capturing, Reconstructing, and Simulating: the UrbanScene3D Dataset, ECCV 2022",
    "license": "UrbanScene3D terms of use: non-commercial research only; see url",
    "redistribution": False,
    "notice": "Recorded for provenance only; distribution follows ADR-034.",
}
SOURCE_DATASET_LABEL = "UrbanScene3D (virtual cities, sampled 5M)"
DESCRIPTION = "UrbanScene3D virtual city, sampled 5M points, normalised to world ENU (m, Z-up)."
TAGS = ["urbanscene3d", "synthetic", "builtin"]


class UrbanScene3DAdapter:
    """`IngestAdapter` 实现；原始文件的字节数与 sha256 以 `configs/data.yaml` 为真源（F-05）。"""

    kind = "urbanscene3d"

    def __init__(self, city: str, raw_dir: Path, data_config: DataConfig | None = None):
        if city not in CITIES:
            raise ConfigError(f"未知城市 {city!r}；可选 {sorted(CITIES)}")
        self.city = city
        self.raw_dir = Path(raw_dir)
        self.data = data_config or load_data_config()
        spec = self.data.by_world(city)
        if spec is None:
            raise ConfigError(f"configs/data.yaml 中没有 {city}")
        if spec.name != RAW_FILES[city]:
            raise ConfigError(f"configs/data.yaml 的文件名 {spec.name!r} 与适配器 {RAW_FILES[city]!r} 不一致")
        self.spec = spec
        self._files: list[dict] = []

    def config(self) -> IngestConfig:
        return CITIES[self.city]

    def names(self) -> tuple[str, str]:
        en, zh = NAMES[self.city]
        return f"{en} (UrbanScene3D)", f"{zh}（UrbanScene3D）"

    def raw_path(self) -> Path:
        return self.raw_dir / self.spec.name

    def load(self) -> RawCloud:
        p = check_raw_file(self.spec, self.raw_dir)
        xyz, nrm, info, sha = read_ply(p, expect_sha256=self.spec.sha256, expect_bytes=self.spec.bytes)
        if info.n != self.spec.points:
            raise ConfigError(f"{p.name}: 点数 {info.n} 与 configs/data.yaml 的 {self.spec.points} 不符")
        self._files = [{"name": self.spec.name, "bytes": info.file_size, "points": info.n, "sha256": sha,
                        "header_bytes": info.header_bytes}]
        return RawCloud(xyz=xyz, normal=nrm, class_las=None, files=list(self._files))

    def provenance(self) -> dict:
        d = dict(DATASET)
        files = self._files or [{"name": self.spec.name, "bytes": self.spec.bytes, "sha256": self.spec.sha256}]
        d["sourceFiles"] = [{"name": f["name"], "bytes": f["bytes"], "sha256": f["sha256"]} for f in files]
        return d

    def manifest_extras(self) -> dict:
        name, name_zh = self.names()
        return {"name": name, "nameZh": name_zh, "description": DESCRIPTION, "tags": list(TAGS),
                "source_dataset": SOURCE_DATASET_LABEL,
                "default_color_mode": "hag" if self.city == "sanfrancisco" else "height"}
