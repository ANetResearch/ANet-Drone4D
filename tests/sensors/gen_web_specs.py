#!/usr/bin/env python3
"""生成 `apps/web/src/engine/sensors/specs.gen.ts`（M13-FR-001、FR-022；M13 §6.4.3）。

浏览器端的传感器参数与 sim-core 同源：本脚本对 `vehicles/*/params.yaml` 中声明了 `sensors:` 的每个机型目录调用
`awr.sim.sensors.describe.describe_model()`（与 `GET /api/fleet/profiles/{id}` 的 `sensors{}` 同一函数），把有视场与云台的
字段写成 TS 常量。`--check` 只比较不写入（tests/sensors/test_web_specs.py 调用）。

D2（M13-FR-217）：每个传感器另给 `modelId` 与光学变焦参数 `zoom`（焦距范围、F 数、全程变焦时间、像元、输出档与裁切）；
`SENSOR_MODELS` 为型号库（`vehicles/sensor_models/`，M21；交付前回退到 `tests/sensors/fixtures/sensor_models/`，取值同 AWR-20）
中每个型号的变焦、镜头与照明器参数，前端 `withFocal` 与焦距平滑按此计算 FPV 与视锥。
用法：.venv/bin/python tests/sensors/gen_web_specs.py [--check]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python"))

from awr.sim.sensors.describe import describe_model  # noqa: E402
from awr.sim.sensors.enums import SensorKind  # noqa: E402

OUT = ROOT / "apps" / "web" / "src" / "engine" / "sensors" / "specs.gen.ts"
KIND_CODE = {k.name.lower(): int(k) for k in SensorKind}
LIB_OFFICIAL = ROOT / "vehicles" / "sensor_models"
LIB_FALLBACK = ROOT / "tests" / "sensors" / "fixtures" / "sensor_models"


def zoom_json(z) -> dict | None:
    if z is None:
        return None
    return {"fMinMm": z.f_min_mm, "fMaxMm": z.f_max_mm, "fNumMin": z.fnum_min, "fNumMax": z.fnum_max, "law": z.law,
            "zoomTimeS": z.zoom_time_s, "pixelUm": z.pixel_um, "sensorWpx": z.width_px, "sensorHpx": z.height_px,
            "analysisDefault": z.analysis_default,
            "outputs": [{"name": o.name, "w": o.w, "h": o.h, "cropW": o.crop_w, "cropH": o.crop_h} for o in z.outputs]}


def model_entries() -> tuple[str, dict]:
    """型号库 -> {model_id: {modelId, type, zoom, lenses, illum}}；官方型号库不存在时用测试型号库。"""
    from awr.sim.sensors.spec import build_spec

    lib = LIB_OFFICIAL if LIB_OFFICIAL.is_dir() and any(LIB_OFFICIAL.glob("*.yaml")) else LIB_FALLBACK
    out: dict[str, dict] = {}
    for f in sorted(lib.glob("*.yaml")):
        doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        if "mount" not in doc:
            doc = {**doc, "mount": {"xyz_m": [0.0, 0.0, 0.0], "rpy_deg": [0, 0, 0]}}
        sp = build_spec(str(doc["model_id"]), doc, 0, str(f))
        il = sp.illum
        out[str(doc["model_id"])] = {
            "modelId": str(doc["model_id"]), "version": str(doc["model_version"]), "type": str(doc["schema"]).split(".")[2],
            "zoom": zoom_json(sp.zoom),
            "lenses": [{"fMm": x.f_mm, "fNumber": x.f_number} for x in sp.lenses] or None,
            "illum": None if il is None else {"wavelengthNm": il.wavelength_nm, "powerW": il.p_w, "followHfov": il.follow_hfov,
                                              "divergenceRad": None if il.follow_hfov else round(il.divergence_rad, 9),
                                              "rRefM": il.r_ref_m, "pRefW": il.p_ref_w, "thetaRefRad": round(il.theta_ref_rad, 9),
                                              "rhoRef": il.rho_ref}}
    return str(lib.relative_to(ROOT)), out


def entry(d: dict) -> dict:
    it = d.get("intrinsics")
    g = d.get("gimbal")
    R = d["mount"]["R"]
    t = d["mount"]["xyz_m"]
    mount = [R[0][0], R[0][1], R[0][2], t[0], R[1][0], R[1][1], R[1][2], t[1], R[2][0], R[2][1], R[2][2], t[2], 0, 0, 0, 1]
    e = {"name": d["name"], "kind": d["kind"], "kindCode": KIND_CODE[d["kind"]], "sensorNo": d["sensor_no"],
         "w": it["w"] if it else 0, "h": it["h"] if it else 0, "fx": it["fx"] if it else 0, "fy": it["fy"] if it else 0,
         "cx": it["cx"] if it else 0, "cy": it["cy"] if it else 0,
         "hfov": d.get("hfov_rad"), "vfov": d.get("vfov_rad"), "rangeM": d.get("range_m"), "rateHz": d.get("rate_hz"),
         "mount": mount, "conf": d.get("conf"),
         "gimbal": None if g is None else {k: g[k] for k in ("yaw_min_deg", "yaw_max_deg", "pitch_min_deg", "pitch_max_deg",
                                                              "rate_max_deg_s", "default_mode", "default_yaw_deg",
                                                              "default_pitch_deg")},
         "capability": (d.get("detector") or {}).get("capability"),
         "modelId": d.get("model_id"), "zoom": None}
    z = d.get("zoom")
    if z is not None:
        e["zoom"] = {"fMinMm": z["f_mm"][0], "fMaxMm": z["f_mm"][1], "fNumMin": z["f_number"][0], "fNumMax": z["f_number"][1],
                     "law": z["law"], "zoomTimeS": z["zoom_time_s"], "pixelUm": z["pixel_um"], "sensorWpx": z["sensor_px"][0],
                     "sensorHpx": z["sensor_px"][1], "analysisDefault": z["analysis_default"],
                     "outputs": [{"name": o["name"], "w": o["w_px"], "h": o["h_px"], "cropW": o["crop_w_px"], "cropH": o["crop_h_px"]}
                                 for o in z["outputs"]]}
    return e


def render() -> str:
    models: dict[str, list[dict]] = {}
    for p in sorted((ROOT / "vehicles").glob("*/params.yaml")):
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not doc.get("sensors"):
            continue
        d = describe_model(p.parent)
        models[p.parent.name] = [entry(d[r["name"]]) for r in d["rig"]]
    body = json.dumps(models, ensure_ascii=False, indent=2, sort_keys=True)
    lib, sm = model_entries()
    mbody = json.dumps(sm, ensure_ascii=False, indent=2, sort_keys=True)
    return (
        "// Generated by tests/sensors/gen_web_specs.py from vehicles/*/sensors/*.yaml (M13-FR-001, FR-022). Do not edit.\n"
        "// Same values as GET /api/fleet/profiles/{id} sensors{} (awr.sim.sensors.describe); mount is the row-major\n"
        "// T_base_mount, angles of fov in rad, gimbal limits in degrees; keyed by the vehicle directory (roster `model`).\n"
        "export interface SensorSpecGimbal {\n"
        "  default_mode: string\n  default_pitch_deg: number\n  default_yaw_deg: number\n  pitch_max_deg: number\n"
        "  pitch_min_deg: number\n  rate_max_deg_s: number\n  yaw_max_deg: number\n  yaw_min_deg: number\n}\n"
        "export interface SensorSpecJson {\n"
        "  name: string\n  kind: string\n  kindCode: number\n  sensorNo: number\n  w: number\n  h: number\n  fx: number\n"
        "  fy: number\n  cx: number\n  cy: number\n  hfov: number | null\n  vfov: number | null\n  rangeM: number | null\n"
        "  rateHz: number | null\n  mount: number[]\n  conf: string | null\n  gimbal: SensorSpecGimbal | null\n"
        "  capability: string | null\n  /** D2: sensor model id (spec_ref) */\n  modelId?: string | null\n"
        "  /** D2: optical zoom (eo_zoom), null for fixed lenses */\n  zoom?: SensorSpecZoom | null\n}\n"
        "export interface SensorSpecOutput {\n  name: string\n  w: number\n  h: number\n  cropW: number\n  cropH: number\n}\n"
        "export interface SensorSpecZoom {\n  fMinMm: number\n  fMaxMm: number\n  fNumMin: number\n  fNumMax: number\n  law: string\n"
        "  zoomTimeS: number\n  pixelUm: number\n  sensorWpx: number\n  sensorHpx: number\n  analysisDefault: string\n"
        "  outputs: SensorSpecOutput[]\n}\n"
        "export interface SensorModelIllum {\n  wavelengthNm: number\n  powerW: number\n  followHfov: boolean\n"
        "  divergenceRad: number | null\n  rRefM: number\n  pRefW: number\n  thetaRefRad: number\n  rhoRef: number\n}\n"
        "export interface SensorModelJson {\n  modelId: string\n  version: string\n  type: string\n  zoom: SensorSpecZoom | null\n"
        "  lenses: { fMm: number; fNumber: number }[] | null\n  illum: SensorModelIllum | null\n}\n"
        f"export const SENSOR_SPECS: Record<string, readonly SensorSpecJson[]> = {body}\n"
        f"/** sensor model library ({lib}) */\n"
        f"export const SENSOR_MODELS: Record<string, SensorModelJson> = {mbody}\n"
    )


def main() -> int:
    text = render()
    if "--check" in sys.argv:
        cur = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if cur != text:
            print(f"{OUT.relative_to(ROOT)} is out of date: run tests/sensors/gen_web_specs.py", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
