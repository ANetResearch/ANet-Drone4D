#!/usr/bin/env python3
"""沙盒场景模板 T1–T6（七个世界）与共享展示剧本 S7 的生成器（WP-13，M16；AWR-04 §10.4、ADR-094、ADR-112）。

输入只有世界锚点 `scenarios/sandbox/anchors/<world>.json`（`anchors.py` 在世界几何上搜索并验证），输出：

- `scenarios/sandbox/<world>/<template_id>.json`（`awr.sandbox.template.v1`，42 个；配置段与配置快照同构，M17 池读取后写入
  `session.json`，M08 冷启动装配）；
- `scenarios/s7-synthcity-relay.json`（`awr.sandbox.snapshot.v1`：共享展示与批处理共用的会话配置，见下文"S7"）。

同一输入两次生成逐字节一致（规范化 JSON）；`--check` 只比较不写（CI：`tests/e2e/test_sandbox_templates.py`）。

模板（AWR-04 §10.4 表）：

| 模板 | 内容 |
|---|---|
| `t1_point_watch` | 机巢箱（锚点 `nest_watch`，站位外侧）、2 架 P600、1 个静止的人；`point_watch` 于锚点 `watch_point`（AGL 60 m，云台锁定该人） |
| `t2_polyline` | 机巢箱、1 架 P600；`polyline_patrol` 四点循环（AGL 70 m，前视，自动绕行禁入体） |
| `t3_area_patrol` | 两个机巢箱、2 架 P600 与 1 架 AWR-Q1、3 个游走的人；`area_patrol`（重访 300 s，发现即提示） |
| `t4_scan_gsd` | 机巢箱、4 架 P600；`area_scan_gsd`（GSD 3 cm，架数自动；20 s 后开始，先显示预览的架数与完工曲线） |
| `t5_area_guard`（缺省） | 机巢箱（2 架 P600）与 VTOL 起降台（1 架 AWR-V1）、静止的人与游走的人；`area_guard`，`on_detect = relay` |
| `t6_manual` | 单槽机巢箱（锚点 `nest_watch`）、1 架 P600（`point_watch` 起飞后在识别物附近悬停，供接管手动飞行）、1 个静止的人 |

演示尺度的敏感体：模板与 S7 中人的敏感体取视觉扇形（半角 60°）150 m、声学半球 120 m（类别缺省为 300 m，AWR-04 §7.2）。
理由：类别缺省半径是"昼夜两档都有机型能在禁入体外以 R 级看住"的保守值，放在 0.16 km² 的值守区里会使区内几乎全部成为禁入体，
首次反馈（T1 约 40 s、T5 ≤ 60 s【仿真】）无法达到；访客可在识别物详情中改回缺省或采纳建议半径。D2-AC-19、20 的接力批处理
（`tools/bench/sandbox/relay_batch.py`）仍按类别缺省模板运行。

S7（ADR-094）：机巢 A 为机巢箱（6 架 P600，均挂 `nir_ill_l`；8 个 6S 通道；电池共 14 块 = 机上 6 + 备用 8），机巢 B 为 VTOL
起降台（2 架 AWR-V1；2 个 12S 通道；电池共 4 块 = 机上 2 + 备用 2）；静止的人与游走的人（接力目标）、沿路车辆（只做巡查检测）；
`area_guard`（锚点 `aoi_guard`，0.16 km²，重访 600 s）只用机巢 B 的 AWR-V1（"值守周期"合并工作项，M20-FR-014），发现后的接力由
P600 承担。日历在文件中为占位值，共享展示启动时改为当前时刻（北京时间同步，见 d2-WP-13-to-M11）；天气 `clear`（MOR 30 km）。

用法：
    python tools/sandbox/gen_templates.py              # 写出（只写有变化的文件）
    python tools/sandbox/gen_templates.py --check      # 与提交的文件比较，不一致退出码 1
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sbcommon as C

TEMPLATE_SCHEMA = "awr.sandbox.template.v1"
SNAPSHOT_SCHEMA = "awr.sandbox.snapshot.v1"

# 演示尺度的人敏感体（见模块文档）
PERSON_SENS: list[dict[str, Any]] = [
    {"kind": "visual", "shape": "sector", "radius_max_m": 150, "z_min_m": 0, "z_max_m": 150, "half_angle_deg": 60,
     "azimuth_offset_deg": 0, "gain_back_db": 0, "enabled": True},
    {"kind": "acoustic", "shape": "hemisphere", "radius_max_m": 120, "z_min_m": 0, "z_max_m": 120, "half_angle_deg": 180,
     "azimuth_offset_deg": 0, "gain_back_db": 0, "enabled": True},
]

META: dict[str, dict[str, Any]] = {
    C.T1: {"name_zh": "T1 定点巡航", "seed": 11,
           "summary_zh": "1 个机巢箱、2 架 P600：定点悬停并以云台锁定 1 个静止的人",
           "first_feedback_zh": "约 40 s 明确捕获"},
    C.T2: {"name_zh": "T2 多点巡线", "seed": 12,
           "summary_zh": "1 架 P600 沿四个航点循环巡线，可切换 FPV 与传感器视图",
           "first_feedback_zh": "航段与 FPV"},
    C.T3: {"name_zh": "T3 多机区域巡查", "seed": 13,
           "summary_zh": "2 个机巢箱、3 架无人机（P600 与 AWR-Q1）分区巡查，区内 3 个游走的人",
           "first_feedback_zh": "子区与检测"},
    C.T4: {"name_zh": "T4 GSD 完整扫描", "seed": 14,
           "summary_zh": "4 架 P600 按 3 cm GSD 扫描验证区：先给出架数与完工曲线，20 s 后开始执行",
           "first_feedback_zh": "架数与完工曲线"},
    C.T5: {"name_zh": "T5 区域值守与接力", "seed": 15,
           "summary_zh": "机巢箱与 VTOL 起降台、P600 与 AWR-V1：区域值守发现识别物后自动转入 7×24 接力",
           "first_feedback_zh": "会话开始后约 60 s【仿真】内首次明确捕获，随后转入接力"},
    C.T6: {"name_zh": "T6 手动飞行与传感器视图", "seed": 16,
           "summary_zh": "1 架已起飞的 P600 在识别物附近悬停：可接管手动飞行，切换日视、夜视与热像",
           "first_feedback_zh": "接管提示"},
}


# ---------------------------------------------------------------- 构件
def xyz(p: Any) -> list:
    """World ENU；锚点给出高度（机巢：槽位 DSM 最大值）时沿用，否则 null（贴 DSM）。"""
    return [C.rnd(p[0]), C.rnd(p[1]), C.rnd(p[2], 2) if len(p) > 2 and p[2] is not None else None]


def nest_box(nid: str, name: str, pos: Any, *, slots: int, spare_6s: int, channels: int) -> dict:
    """机巢箱（多旋翼，换电；C1-XR 1–6 节、10 A；P600 电池 6S 10 Ah）。"""
    return {"id": nid, "name": name, "pos": xyz(pos), "kind": "box", "slots": slots,
            "energy": {"mode": "swap", "swap_s": 180, "packs": [{"cells": 6, "count": spare_6s, "capacity_ah": 10}]},
            "chargers": [{"model": "c1_xr", "count": channels, "channels": 1, "i_max_a": 10, "cells_max": 6}],
            "turnaround_s": 60, "launch_interval_s": 30, "link_range_m": 3500}


def nest_pad(nid: str, name: str, pos: Any, *, slots: int, spare_12s: int, channels: int) -> dict:
    """VTOL 起降台（垂直起降、自动换电；12S 充电器 10 A；AWR-V1 电池 12S 22 Ah）。"""
    return {"id": nid, "name": name, "pos": xyz(pos), "kind": "vtol_pad", "slots": slots,
            "energy": {"mode": "swap", "swap_s": 180, "packs": [{"cells": 12, "count": spare_12s, "capacity_ah": 22}]},
            "chargers": [{"model": "chg12s_ref", "count": channels, "channels": 1, "i_max_a": 10, "cells_max": 12}],
            "turnaround_s": 60, "launch_interval_s": 30, "link_range_m": 3500}


def vehicle(vid: str, model: str, nest: str | None, *, label: str, home: Any = None, sensors: list | None = None) -> dict:
    d: dict[str, Any] = {"id": vid, "model_id": model, "nest_id": nest}
    if home is not None:
        d["home_enu_m"] = xyz(home)
    d["label"] = label
    if sensors is not None:
        d["sensors"] = sensors
    return d


def person_static(tid: str, a: dict, label: str = "静止的人") -> dict:
    t = a["target_static"]
    return {"id": tid, "label": label, "class": "person", "pos": xyz(t["pos"]), "yaw_rad": t["yaw_rad"],
            "motion": {"mode": "static"}, "sensitivity": PERSON_SENS}


def person_walk(tid: str, region: list, label: str, speed_mps: float | None = None) -> dict:
    cx, cy = C.centroid(region)
    motion: dict[str, Any] = {"mode": "random_walk", "region": [C.pt(p) for p in region]}
    if speed_mps is not None:
        motion["speed_mps"] = speed_mps
    return {"id": tid, "label": label, "class": "person", "pos": [C.rnd(cx), C.rnd(cy), None], "motion": motion, "sensitivity": PERSON_SENS}


# 值守区里与静止的人同在时的游走者：取离静止的人最远的游走区、散步速度 1.0 m/s。M20 规划口径对运动识别物按 `v_nom·τ_route`
# （20 s）外扩禁入圆，类别缺省 1.4 m/s 时两个禁入圆在部分世界（sanfrancisco）把值守周期的起点围住（unassigned:keepout）
WALKER_SPEED_MPS = 1.0


def far_walk_region(a: dict) -> list:
    t = a["target_static"]["pos"]
    return max(a["walk_regions"], key=lambda r: C.dist(C.centroid(r), t))


# 车内的人对无人机的敏感范围（演示尺度）：前向扇形 80 m、声学半球 50 m（车窗与发动机噪声遮蔽）
CAR_SENS: list[dict[str, Any]] = [
    {"kind": "visual", "shape": "sector", "radius_max_m": 80, "z_min_m": 0, "z_max_m": 80, "half_angle_deg": 60,
     "azimuth_offset_deg": 0, "gain_back_db": 0, "enabled": True},
    {"kind": "acoustic", "shape": "hemisphere", "radius_max_m": 50, "z_min_m": 0, "z_max_m": 50, "half_angle_deg": 180,
     "azimuth_offset_deg": 0, "gain_back_db": 0, "enabled": True},
]
CAR_SPEED_MPS = 3.0


def car_on_road(tid: str, road: list) -> dict:
    """沿路车辆：往返 3 m/s（园区道路慢行）。M20 规划口径对运动识别物按 `v_nom·τ_route`（20 s）外扩禁入圆，类别缺省 10 m/s 与
    150 m 前向扇形会使 0.16 km² 值守区的巡边整体落入禁入圆（583），因此速度与敏感体取演示尺度。"""
    a, b = road
    yaw = math.atan2(b[1] - a[1], b[0] - a[0])
    return {"id": tid, "label": "沿路车辆", "class": "vehicle", "pos": xyz(a), "yaw_rad": C.yaw(yaw),
            "motion": {"mode": "path", "path": [xyz(a), xyz(b)], "loop": "pingpong", "speed_mps": CAR_SPEED_MPS},
            "sensitivity": CAR_SENS}


def camera(a: dict) -> dict:
    cx, cy = C.centroid(a["aoi_guard"])
    return {"center_enu_m": [C.rnd(cx), C.rnd(cy), 0], "range_m": 900, "pitch_deg": -40, "yaw_deg": 0}


def head(world: str, tid: str) -> dict:
    m = META[tid]
    return {"schema": TEMPLATE_SCHEMA, "template_id": tid, "world_id": world, "name_zh": m["name_zh"], "summary_zh": m["summary_zh"],
            "first_feedback_zh": m["first_feedback_zh"], "recommended": tid == C.DEFAULT_TEMPLATE,
            "calendar_default": {"start_local": "10:00", "tz": C.TZ[world]}, "acoustic_bg": C.ACOUSTIC_BG[world],
            "env": {"preset": "clear", "overrides": {}}, "seed": m["seed"]}


# ---------------------------------------------------------------- 模板
def t1(world: str, a: dict) -> dict:
    return {**head(world, C.T1),
            "nests": [nest_box("nest-a", "机巢箱 A", a["nest_watch"], slots=4, spare_6s=2, channels=2)],
            "vehicles": [vehicle("p600-01", "p600_mid360", "nest-a", label="P600 一号"),
                         vehicle("p600-02", "p600_mid360", "nest-a", label="P600 二号")],
            "targets": [person_static("person-01", a)],
            "tasks": [{"type": "point_watch", "label": "定点巡航",
                       "params": {"point": C.pt(a["watch_point"]), "alt_m": 60, "alt_ref": "agl", "dwell_s": None,
                                  "look": {"mode": "target", "target": "person-01"}},
                       "common": {"priority": "normal", "on_detect": "notify"}}],
            "camera": camera(a), "anchors_ref": f"scenarios/sandbox/anchors/{world}.json"}


def t2(world: str, a: dict) -> dict:
    return {**head(world, C.T2),
            "nests": [nest_box("nest-a", "机巢箱 A", a["nest_box"], slots=4, spare_6s=2, channels=2)],
            "vehicles": [vehicle("p600-01", "p600_mid360", "nest-a", label="P600 一号")],
            "targets": [],
            "tasks": [{"type": "polyline_patrol", "label": "四点循环巡线",
                       "params": {"waypoints": [C.pt(p) for p in a["polyline"]], "mode": "loop", "alt_m": 70,
                                  "sensor_facing": "forward", "auto_detour": True, "duration_s": None},
                       "common": {"priority": "normal", "on_detect": "notify"}}],
            "camera": camera(a), "anchors_ref": f"scenarios/sandbox/anchors/{world}.json"}


def t3(world: str, a: dict) -> dict:
    w = a["walk_regions"]
    return {**head(world, C.T3),
            "nests": [nest_box("nest-a", "机巢箱 A", a["nest_box"], slots=4, spare_6s=2, channels=2),
                      nest_box("nest-b", "机巢箱 B", a["nest_pad"], slots=2, spare_6s=0, channels=1)],
            "vehicles": [vehicle("p600-01", "p600_mid360", "nest-a", label="P600 一号"),
                         vehicle("p600-02", "p600_mid360", "nest-a", label="P600 二号"),
                         vehicle("q1-01", "awr_q1", "nest-b", label="AWR-Q1 一号")],
            "targets": [person_walk("person-01", w[0], "游走的人 一"), person_walk("person-02", w[1], "游走的人 二"),
                        person_walk("person-03", w[2], "游走的人 三")],
            "tasks": [{"type": "area_patrol", "label": "多机区域巡查",
                       "params": {"polygon": [C.pt(p) for p in a["aoi_guard"]], "n_vehicles": "auto", "target_class": "person",
                                  "level_req": "R", "revisit_s": 300},
                       "common": {"priority": "normal", "on_detect": "notify"}}],
            "camera": camera(a), "anchors_ref": f"scenarios/sandbox/anchors/{world}.json"}


def t4(world: str, a: dict) -> dict:
    return {**head(world, C.T4),
            "nests": [nest_box("nest-a", "机巢箱 A", a["nest_box"], slots=4, spare_6s=4, channels=4)],
            "vehicles": [vehicle(f"p600-0{k}", "p600_mid360", "nest-a", label=f"P600 {n}号")
                         for k, n in ((1, "一"), (2, "二"), (3, "三"), (4, "四"))],
            "targets": [],
            "tasks": [{"type": "area_scan_gsd", "label": "GSD 完整扫描",
                       "params": {"polygon": [C.pt(p) for p in a["aoi_scan"]], "gsd_cm": 3.0, "sidelap": 0.2, "frontlap": 0.2,
                                  "theta_max_deg": 15, "h_cap_m": 120, "n_vehicles": "auto", "allow_multi_sortie": False},
                       "common": {"priority": "normal", "start_at_s": 20, "on_detect": "notify"}}],
            "camera": camera(a), "anchors_ref": f"scenarios/sandbox/anchors/{world}.json"}


def t5(world: str, a: dict) -> dict:
    # D2-FX-R2-tasking 第 2 次（D2-AC-35"批处理 24 h、间隙按 D2-AC-19 口径"）：两个人都转入接力，每个接力的稳态需要 3 架挂远距红外照明器的 P600
    # （夜间可行）、6 块电池、4 个充电通道（D2-AC-19 (a) 的配置；D2-AC-20 减一个通道即瓶颈 charger），值守周期另占 P600。此前模板
    # 只有 2 架不挂照明器的 P600、3 块备用电池、2 个通道，按算术不可能满足（d2-FX-R2-tasking-to-M16 第 1 条方案 A，与 S7 的接力资源
    # 相同）；机体数 3 → 7，仍在会话上限 12 架内
    nir = [{"name": "nir_ill", "enabled": True}]
    p600 = [vehicle(f"p600-0{k}", "p600_mid360", "nest-a", label=f"P600 {n}号", sensors=nir)
            for k, n in ((1, "一"), (2, "二"), (3, "三"), (4, "四"), (5, "五"), (6, "六"))]
    return {**head(world, C.T5),
            "nests": [nest_box("nest-a", "机巢箱 A", a["nest_box"], slots=6, spare_6s=8, channels=8),
                      nest_pad("nest-b", "VTOL 起降台 B", a["nest_pad"], slots=2, spare_12s=2, channels=2)],
            "vehicles": [*p600, vehicle("v1-01", "awr_v1", "nest-b", label="AWR-V1 一号")],
            "targets": [person_static("person-01", a), person_walk("person-02", far_walk_region(a), "游走的人", WALKER_SPEED_MPS)],
            "tasks": [{"type": "area_guard", "label": "区域值守",
                       "params": {"polygon": [C.pt(p) for p in a["aoi_guard"]], "target_class": "person", "level_req": "R",
                                  "revisit_s": 600},
                       "common": {"priority": "normal", "on_detect": "relay", "allowed_models": list(T5_GUARD_MODELS)}}],
            "camera": camera(a), "anchors_ref": f"scenarios/sandbox/anchors/{world}.json"}


def t6(world: str, a: dict) -> dict:
    return {**head(world, C.T6),
            "nests": [nest_box("nest-a", "机巢箱 A", a["nest_watch"], slots=1, spare_6s=1, channels=1)],
            "vehicles": [vehicle("p600-01", "p600_mid360", "nest-a", label="P600 一号")],
            "targets": [person_static("person-01", a)],
            "tasks": [{"type": "point_watch", "label": "悬停待接管",
                       "params": {"point": C.pt(a["watch_point"]), "alt_m": 60, "alt_ref": "agl", "dwell_s": None,
                                  "look": {"mode": "target", "target": "person-01"}},
                       "common": {"priority": "low", "on_detect": "notify"}}],
            "camera": camera(a), "anchors_ref": f"scenarios/sandbox/anchors/{world}.json"}


# T5 的值守周期只派多旋翼（过渡做法）：AWR-V1 按多旋翼语义执行 M20 的 takeoff 与 follow_path 时在 4 个世界 30–40 s 内
# POS_ERR_FAILSAFE 或 TILT_ERR_KILL（d2-WP-13-to-M20-M21 第 1 条）；修复后去掉该限制，由贪心在 P600 与 AWR-V1 之间分配。
# AWR-V1 仍在 VTOL 起降台上，接力可用（M20 按可行窗口选组）。S7 按 ADR-094 由 AWR-V1 承担值守周期，不受此限制。
T5_GUARD_MODELS = ("p600_mid360",)

BUILDERS = {C.T1: t1, C.T2: t2, C.T3: t3, C.T4: t4, C.T5: t5, C.T6: t6}


# ---------------------------------------------------------------- S7
S7_CALENDAR_PLACEHOLDER = {"start_utc": "2026-10-05T00:00:00Z", "tz": "Asia/Shanghai"}   # 启动时改为当前时刻（北京时间 08:00）
S7_REVISIT_S = 600


def s7(a: dict) -> dict:
    """共享展示剧本 S7（`awr.sandbox.snapshot.v1`，ADR-094）。"""
    nir = [{"name": "nir_ill", "enabled": True}]
    p600 = [vehicle(f"p600-0{k}", "p600_mid360", "nest-a", label=f"P600 {n}号", sensors=nir)
            for k, n in ((1, "一"), (2, "二"), (3, "三"), (4, "四"), (5, "五"), (6, "六"))]
    v1 = [vehicle("v1-01", "awr_v1", "nest-b", label="AWR-V1 一号"), vehicle("v1-02", "awr_v1", "nest-b", label="AWR-V1 二号")]
    return {
        "schema": SNAPSHOT_SCHEMA, "contracts": C.CONTRACTS_VERSION, "world_id": "synthcity", "template_id": C.S7_ID,
        "calendar": dict(S7_CALENDAR_PLACEHOLDER), "acoustic_bg": "urban", "seed": 7,
        "env": {"preset": "clear", "overrides": {}},
        "nests": [nest_box("nest-a", "机巢箱 A", a["nest_box"], slots=6, spare_6s=8, channels=8),
                  nest_pad("nest-b", "VTOL 起降台 B", a["nest_pad"], slots=2, spare_12s=2, channels=2)],
        "vehicles": p600 + v1,
        "targets": [person_static("person-01", a), person_walk("person-02", far_walk_region(a), "游走的人", WALKER_SPEED_MPS),
                    car_on_road("vehicle-01", a["road"])],
        "tasks": [{"type": "area_guard", "label": "区域值守",
                   "params": {"polygon": [C.pt(p) for p in a["aoi_guard"]], "target_class": "person", "level_req": "R",
                              "revisit_s": S7_REVISIT_S},
                   "common": {"priority": "normal", "on_detect": "relay", "allowed_nests": ["nest-b"],
                              "allowed_models": ["awr_v1"]}}],
    }


# ---------------------------------------------------------------- 生成与比较
def anchors(world: str) -> dict:
    return C.load_json(C.ANCHORS / f"{world}.json")


def build_all() -> dict[Path, dict]:
    out: dict[Path, dict] = {}
    for w in C.WORLDS:
        a = anchors(w)
        for tid, fn in BUILDERS.items():
            out[C.SANDBOX / w / f"{tid}.json"] = fn(w, a)
    out[C.S7_PATH] = s7(anchors("synthcity"))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tools/sandbox/gen_templates.py")
    ap.add_argument("--check", action="store_true", help="只比较，不写文件")
    a = ap.parse_args(argv)
    docs = build_all()
    diff = []
    for p, d in docs.items():
        s = C.dumps_canonical(d)
        if a.check:
            if not p.exists() or p.read_text(encoding="utf-8") != s:
                diff.append(str(p.relative_to(C.ROOT)))
        elif C.write_canonical(d, p):
            print("written", p.relative_to(C.ROOT))
    if a.check:
        print("OK" if not diff else "DIFF " + " ".join(diff))
        return 1 if diff else 0
    print(f"{len(docs)} files up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
