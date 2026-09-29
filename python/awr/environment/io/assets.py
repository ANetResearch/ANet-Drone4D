"""共享环境资产的确保（M07-FR-026；M07 §7.4；16 §8.4）。

`worlds/_shared/env/turb/vk_s{seed}_n64_dx4_L30.awrv`（AWRV kind 2，f16，文件 2,097,216 B）与
`worlds/_shared/env/weather/weather_s{seed}_512.awrv`（AWRV kind 6，RGBA8）：sim-core 启动时检查存在且 `field_version`
相符，否则生成、写临时文件后原子改名。URL 不上线，两端按 seed 与参数派生（M07 §6.2.2、§14 第 15 条）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..noise import weather_map, weather_params
from ..wind.turbulence import TURB_DX, TURB_L, TURB_N, box_to_rgba, vk_box
from . import awrv

__all__ = ["AssetResult", "ensure_turb_box", "ensure_weather_map", "turb_name", "turb_params", "turb_url", "weather_name", "weather_url"]


def _num(x: float) -> str:
    return str(int(x)) if float(x) == int(x) else repr(float(x))


def turb_params(seed: int, n: int = TURB_N, dx: float = TURB_DX, L: float = TURB_L) -> str:
    return f"vk|seed={int(seed)}|n={int(n)}|dx={_num(dx)}|L={_num(L)}|v=1"


def turb_name(seed: int, n: int = TURB_N, dx: float = TURB_DX, L: float = TURB_L) -> str:
    return f"vk_s{int(seed)}_n{int(n)}_dx{_num(dx)}_L{_num(L)}.awrv"


def turb_url(seed: int, n: int = TURB_N, dx: float = TURB_DX, L: float = TURB_L) -> str:
    return f"/worlds/_shared/env/turb/{turb_name(seed, n, dx, L)}"


def weather_name(seed: int, n: int = 512) -> str:
    return f"weather_s{int(seed)}_{int(n)}.awrv"


def weather_url(seed: int, n: int = 512) -> str:
    return f"/worlds/_shared/env/weather/{weather_name(seed, n)}"


@dataclass(slots=True)
class AssetResult:
    path: Path
    built: bool
    volume: awrv.AwrvVolume | None
    error: str | None = None


def _ok_header(path: Path, fv: int, kind: int) -> bool:
    try:
        h = awrv.read_header(path)
    except (OSError, awrv.AwrvError):
        return False
    return h["field_version"] == fv and h["kind"] == kind


def ensure_turb_box(shared_dir: Path, seed: int, n: int = TURB_N, dx: float = TURB_DX, L: float = TURB_L, *,
                    load: bool = True) -> AssetResult:
    path = Path(shared_dir) / "env" / "turb" / turb_name(seed, n, dx, L)
    fv = awrv.field_version(turb_params(seed, n, dx, L))
    try:
        if _ok_header(path, fv, awrv.KIND["turb_box"]):
            vol = awrv.read(path) if load else None
            return AssetResult(path, False, vol)
        vol = awrv.AwrvVolume(awrv.KIND["turb_box"], box_to_rgba(vk_box(n, dx, L, seed)), (0.0, 0.0, 0.0), (dx, dx, dx),
                              float("nan"), 1.0, fv)
        awrv.write_atomic(path, vol)
        return AssetResult(path, True, awrv.read(path) if load else None)
    except (OSError, awrv.AwrvError, ValueError) as ex:
        return AssetResult(path, False, None, f"{type(ex).__name__}: {ex}")


def ensure_weather_map(shared_dir: Path, seed: int, n: int = 512, *, load: bool = False) -> AssetResult:
    path = Path(shared_dir) / "env" / "weather" / weather_name(seed, n)
    fv = awrv.field_version(weather_params(seed, n))
    try:
        if _ok_header(path, fv, awrv.KIND["noise_field"]):
            return AssetResult(path, False, awrv.read(path) if load else None)
        vol = awrv.AwrvVolume(awrv.KIND["noise_field"], weather_map(seed, n), (0.0, 0.0, 0.0), (1.0, 1.0, 1.0), float("nan"), 1.0, fv)
        awrv.write_atomic(path, vol)
        return AssetResult(path, True, awrv.read(path) if load else None)
    except (OSError, awrv.AwrvError, ValueError) as ex:
        return AssetResult(path, False, None, f"{type(ex).__name__}: {ex}")
