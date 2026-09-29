"""导入器协议与测试替身（M12 §9.1 桩；FR-053、FR-054 在 V0.2、V0.5 实现）。

导入器把外部轨迹（UrbanScene3D 航线、PX4 ULog）转为与仿真录制相同的 channel 集（roster、整群块、Full64、事件、环境关键帧），
由同一 Player 回放。输出目录与录制相同：`runs/<run>/meta.json`（`source{kind: "import", format, file, sha256}`）与
`rec-<k>.mcap`、`.ovw`、`.evx`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = ["ImportResult", "Importer", "SynthImporter"]


@dataclass
class ImportResult:
    run_dir: Path
    segments: int
    vehicles: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@runtime_checkable
class Importer(Protocol):
    format: str

    def run(self, src: Path, out_run_dir: Path, *, world_id: str) -> ImportResult: ...


class SynthImporter:
    """测试替身：忽略输入文件，以 `awr.recorder.synth` 生成一段录制（接口与真实导入器相同）。"""

    format = "synth"

    def __init__(self, n: int = 1, sim_s: float = 10.0) -> None:
        self.n = n
        self.sim_s = sim_s

    def run(self, src: Path, out_run_dir: Path, *, world_id: str) -> ImportResult:
        from ..synth import synthesize

        out = synthesize(out_run_dir, n=self.n, sim_s=self.sim_s, world_id=world_id)
        return ImportResult(Path(out_run_dir), len(out["segments"]), list(out["fleet"]))
