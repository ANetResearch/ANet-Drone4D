# AWRV v1（AWR Volume，体数据）

本文件是 AWR-16 §8.3 与 §8.4 的副本，字节布局以 `rt/layouts.json` 的 `awr.env.AwrvHeader.v1` 为机器可读真源；两者不一致时以 AWR-16 为准并修正本文件。


文件与 WS typed-blob 使用同一字节布局；小端；头部 64 B（依据 g06 §7.3）。文件后缀 `.awrv`，可整体 gzip 为 `.awrv.gz`（整文件压缩，由客户端 `DecompressionStream` 解压，不使用 HTTP `Content-Encoding`）。

| 偏移 | 类型 | 字段 | 取值与说明 |
|---|---|---|---|
| 0 | char[4] | `magic` | `"AWRV"`（u32 LE = 0x56525741） |
| 4 | u16 | `version` | 1 |
| 6 | u16 | `kind` | 1 wind_sector（归一化扇区场）、2 turb_box（湍流盒）、3 wind_snapshot（已合成，m/s）、4 scalar_field、5 lbm_frame、6 noise_field（v1.0 新增，M07 §14 第 4 条：可平铺的 2D/3D 噪声或天气图，`dtype = u8`、`comp = 4`，2D 时 `nz = 1`，`value_scale = 1.0` 表示 unorm8，`origin_enu_m` 写 0、`cell_m` 写 1 且不参与世界定位，采样时 wrap = repeat） |
| 8 | u16 | `nx` | x（东）向格数 |
| 10 | u16 | `ny` | y（北）向格数 |
| 12 | u16 | `nz` | z（上）向格数 |
| 14 | u16 | `comp` | 分量数：4 = RGBA（上传 GPU 时一律 4 通道）；标量场为 1 |
| 16 | u8 | `dtype` | 1 f16、2 f32、3 u8 |
| 17 | u8 | `layout` | 0 = zyx C 序，x 变化最快（等价于 `Data3DTexture(width=nx, height=ny, depth=nz)`） |
| 18 | u8 | `premultiplied` | 1 = rgb 已乘流体占比 (1 − a) |
| 19 | u8 | `alpha` | a 通道语义：0 无、1 solid_frac（实体占比）、2 speed、3 sigma |
| 20 | f32[3] | `origin_enu_m` | 最小角（格 (0,0,0) 的角点，不是格心）；格心 = `origin + (i + 0.5)·cell` |
| 32 | f32[3] | `cell_m` | 格边长 |
| 44 | f32 | `dir_from_deg` | 扇区的气象来向；无意义时写 NaN |
| 48 | f32 | `value_scale` | 乘以它得到 m/s；归一化扇区为 1.0；湍流盒为 1.0（表示 σ = 1） |
| 52 | u32 | `field_version` | 生成参数的 hash32（§8.4） |
| 56 | u32 | `payload_bytes` | 等于 `nx·ny·nz·comp·sizeof(dtype)` |
| 60 | u32 | `crc32` | zlib CRC-32（多项式 0xEDB88320）覆盖 payload |
| 64 | bytes | `payload` | 数据 |

规则：可线性过滤的纹理中**禁止 NaN**；实体或无效区域用 a 通道表达（AWR-03 §5.7）。解码器遇到 `magic` 不符、`version ≠ 1`、`payload_bytes` 或 CRC 不符时拒绝（V-E-04）。参考解码签名：TS `decodeAWRV(buf: ArrayBuffer): {nx, ny, nz, origin, cell, dir, scale, tex: Data3DTexture, cpu: Float16Array | Float32Array}`（g06 §7.3 原型）；Python `awr.environment.io.awrv.read(path) -> AwrvVolume`、`write(path, vol)`。

### 8.4 共享环境资产 `worlds/_shared/env/`（湍流盒 D1-core）

`worlds/_shared/env/` 下三类资产都与世界无关，经既有 `/worlds` 静态路由以 immutable 提供（17 §5.1）；文件名带种子与尺寸，内容变化即换名：

| 子目录 | 文件 | AWRV | 生成者与时机 | D1 |
|---|---|---|---|---|
| `turb/` | `vk_s{seed}_n64_dx4_L30.awrv` | kind 2，见下 | M07，sim-core 启动时按需 | core |
| `weather/` | `weather_s{seed}_512.awrv` | kind 6，`nx = ny = 512`、`nz = 1`、RGBA8 | M07，sim-core 启动时按需 | core |
| `cloud/` | `shape_n96.awrv`、`detail_n32.awrv` | kind 6，96³ 与 32³、RGBA8 | M07，`make worlds` 时离线烘焙 | ext |

湍流盒的规定如下：

1. 冻结 von Kármán 湍流盒是全场共享资产（ADR-024），物理侧与前端共用同一文件，保证两端对拍（D1-AC-13 env-gpu 用例）。
2. 文件：`worlds/_shared/env/turb/vk_s{seed}_n64_dx4_L30.awrv`；AWRV `kind = 2`、`nx = ny = nz = 64`、`comp = 4`（RGB 为 u、v、w，A 写 0）、`dtype = f16`、`premultiplied = 0`、`alpha = 0`、`origin_enu_m = (0,0,0)`、`cell_m = (4,4,4)`、`dir_from_deg = NaN`、`value_scale = 1.0`；payload 2,097,152 B，文件 2,097,216 B。
3. `field_version` = sha256(`"vk|seed=<seed>|n=64|dx=4|L=30|v=1"` 的 UTF-8 字节) 前 4 字节按小端解释的 u32。
4. 由 M07 的 `vk_box(N=64, dx=4, L=30, seed)`（g06 §5.5.2，生成约 0.25 s）在 sim-core 启动时按需生成：文件不存在或 `field_version` 不符时写临时文件后原子改名。前端经 `GET /worlds/_shared/env/turb/<name>.awrv`（immutable）取得，URL 写入 EnvKeyframe 的 `config.wind.turbulence.asset`。
5. `worlds/_shared/` 不参与任何世界的 `contentVersion`；`worldpkg clean --shared` 可以删除它（下次启动或 `make worlds` 时重建）。天气图与云噪声的 `field_version` 按第 3 条同式计算，参数串由 M07 定义。
