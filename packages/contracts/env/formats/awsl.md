# AWSL v1（流线）

本文件是 AWR-16 §8.6 的副本，头部布局的机器可读真源为 `rt/layouts.json` 的 `awr.env.AwslHeader.v1`。


小端；头部 48 B（g06 §7.4，按单位后缀改名）：

| 偏移 | 类型 | 字段 | 说明 |
|---|---|---|---|
| 0 | char[4] | `magic` | `"AWSL"` |
| 4 | u16 | `version` | 1 |
| 6 | u16 | `flags` | bit0 τ̂ 已归一到单位参考风速；bit1 线序已打乱 |
| 8 | u32 | `n_lines` | 线数 |
| 12 | u32 | `n_verts` | 顶点总数 |
| 16 | f32 | `dir_from_deg` | 该组流线对应的来向 |
| 20 | f32 | `ref_speed_mps` | 1.0 |
| 24 | u32 | `field_version` | 所用风场的 hash32 |
| 28 | u32 | `stride` | 20（每顶点字节数） |
| 32 | f32 | `tau_hat_max_m` | τ̂ 最大值 |
| 36 | f32 | `s_hat_max` | ŝ 最大值 |
| 40 | u32 | `seed` | 播种随机种子 |
| 44 | u32 | `reserved` | 0 |
| 48 | u32[n_lines + 1] | `line_offsets` | 每条线的首顶点下标，末项等于 `n_verts`；补零到 8 字节对齐 |
| … | f32[n_verts][5] | `verts` | `(x, y, z, τ̂, ŝ)`：ENU m；τ̂ 为参考风速 1 m/s 时的累计"飞行距离"（m）；ŝ = 归一化风速 |

约束：每条线 8–64 个顶点（少于 8 点丢弃，满 64 点停止，g06 §7.4）；1000 条 × 64 点约 1.3 MB。

**D1-ext 落点**：解析场（L0/L1 廓线）流线按需生成，不落盘，经 `GET /api/env/streamlines/{field_id}/d{deg:03d}.awsl` 返回（immutable，服务端 LRU 16 份；接口定义见 [17](../../../../docs/17-接口与实时协议规范.md)）；风向变化 ≥ 1° 时换一组，最多 2 Hz（g06 §7.4）。V0.3 起 L2 流线库按扇区预计算，落盘于 `environment/streamlines/<library_id>/d{deg:03d}.awsl` 并列入 `files[]`。

