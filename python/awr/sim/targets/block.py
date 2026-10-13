"""M18 的实体块列（`EntityTables["target"]`，容量 32；M18-FR-001；字段与 `tools/fake/targets_stub.M18_FIELDS` 冻结表逐项一致，
`tests/contracts/test_d2_stubs.py` 与 `tests/targets/test_limits.py` 比对）。M08 保留 active、target_no、roster_flags；M19 与 M20
的列由其各自登记（一字段一写者）。M08 `tap_targets` 按 `SCALAR_COLUMNS`、`BIT_COLUMNS` 把这些列打包为 TargetLite32。"""

from __future__ import annotations

__all__ = ["BLOCK", "FIELDS", "OWNER"]

BLOCK = "targets"
OWNER = "M18"
F64, F32, U8, U16, U32, I16 = "<f8", "<f4", "u1", "<u2", "<u4", "<i2"
FIELDS: dict[str, tuple[str, tuple[int, ...]]] = {
    "tgt_cls": (U8, ()), "tgt_pos": (F64, (3,)), "tgt_vel": (F64, (3,)), "tgt_yaw": (F64, ()), "tgt_speed": (F64, ()),
    "tgt_size": (F32, (3,)), "tgt_occl": (F32, ()), "tgt_aware": (U8, ()), "tgt_alert": (F32, ()), "tgt_crit": (U8, ()),
    "tgt_enabled": (U8, ()), "tgt_moving": (U8, ()), "tgt_occupied": (U8, ()), "tgt_sound_on": (U8, ()), "tgt_hidden": (U8, ()),
    "tgt_disc_recent": (U8, ()), "tgt_disc_n": (U16, ()), "tgt_episode": (U16, ()), "tgt_n_vol": (U8, ()), "tgt_keepout_r": (F32, ()),
    "tgt_cfg_rev": (U32, ()), "tgt_path_idx": (I16, ()), "tgt_sound_phase": (F32, ()),
}
