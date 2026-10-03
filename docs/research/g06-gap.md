# G6 补充深挖：环境场 E(x,y,z,t) 缺少统一的服务端与前端数据契约——`environment.query` API、schema、二进制格式与推送频率定稿

> 缺口编号：G6（见 `00-index.md` §9）｜ 日期：2026-09-28 ｜ 相关单元：r16、r17、n04、n03、r27、r23（旁及 r04、r06、r11、x01）
> 对应设计：`docs/01-design.md` §17–25（Environment Engine、E(x,y,z,t)、风场分级、风场可视化、雨、雾、沙尘、云）、§36–37（实时通信、频率）、§41（World Package）
>
> 输入：上述单元笔记；源码 `refs/weather/windninja/src/ninja/{ninjaMathUtility.h,windProfile.cpp}`、`refs/weather/natural-disasters/src/weather/Weather.js`、`refs/weather/Eanpa-Sky/engine/weather_system.js`（`transitionTo`、`totalWindDistance`）、`refs/discovery/rotorpy/rotorpy/wind/dryden_utils.py`、`refs/discovery/cesium-wind-layer/packages/cesium-wind-layer/src/`、`refs/web3d/three.js/src/renderers/webgpu/utils/WebGPUTextureUtils.js`（r187dev）；原型 `.cache/research/r17/`、`.cache/research/n04/bench/`。
>
> 本次新写的验证脚本都在 `/data/projs/anet-drone/.cache/research/g06/`：
> - `g06_blend_rot.py`：方向约定、扇区混合的旋转形式、反对称、2 倍降采样的归一化方式、查询耗时
> - `g06_query_opt.py`、`g06_query_flat.py`：物理侧查询的实现方式与耗时
> - `g06_dryden.py`、`g06_dryden2.py`：Dryden 的两种离散化，以及 RotorPy 实现的 σ 标定
> - `env_ref.py`：契约的**参考实现**，包括 derive、方向换算、廓线、扇区选槽、光学厚度、阵风锋面；它同时输出预设的 `mor_bg`
> - `derive_check.mjs`：Node 与 Python 的 derive 对拍
>
> 本机负载较高（有其他 agent 在并发跑任务），所以耗时只看量级。

---

## 0. 结论速览（8 个分歧逐条裁决）

| # | 分歧 | 裁决 | 关键依据（本次核实） |
|---|---|---|---|
| 1 | 雾消光：σ = 3.0/MOR 还是 3.912/V | 全系统只存一个能见度量 **MOR（米）**，换算常数取 **K_MOR = ln 20 = 2.9957**（通常写作 3.0），**σ₅₅₀ = K_MOR / MOR**。能见度按**加性分解**：`mor_bg_m`（背景气溶胶、雾、霾、沙尘、吹雪，不含降水）是状态量；降水的 σ 另外相加；总 MOR 是派生量。**3.912 = ln 50 只允许出现在一个地方**：传感器做波长换算（Kim 模型）时，由 σ 反推 2% 阈值能见度 V₂ = 3.912/σ | WMO 定义 MOR 用 5% 对比阈值，Koschmieder 用 2%，两者描述的是不同的量，不存在谁对谁错，错误在于把它们混用。加性分解让"雨强滑杆"自动压低能见度，r16 的 12 个预设可以无损转换（§4.2）。JS 与 Python 的 derive 结果逐位一致到 1e-12 |
| 2 | 风体纹理第 4 通道：solid fraction 还是 \|u\| | 用 **RGBA16F = (u·φ, v·φ, w·φ, a)**。其中 a 为实体占比，φ = 1 − a（即 RGB 是**按流体占比预乘**的速度）；采样后按 `u = rgb / max(1 − a, 0.5)` 还原。\|u\| 在 shader 里由 rgb 现算，色带上限 `vmax` 写进 manifest。**禁止用 NaN 或 −1 标记实体**（线性过滤会把 NaN 扩散出去） | 对 SF 全部流体格：朴素三线性的平均误差 0.13 m/s，p95 0.62，系统性偏慢 −0.11；预除以 max(1−a, 0.5) 后分别降到 0.07、0.19、+0.00（10 m/s 参考风速，`g06_query_opt.py`）。实体占比无法从 rgb 推出，所以第 4 通道必须存 a |
| 3 | r27 的 `env/wind/field` 只有 3 个 f16 分量 | 废弃这个 3 分量布局，改为 **AWRV v1** 体数据格式：64 B 头，`comp=4`，RGBA16F，x 变化最快，格心采样。它是 World Package 里带 hash 的**不可变静态资产**，走 HTTP 下载；WS 只推引用它的 `env/state`。typed-blob 只在 V0.4+ 的时变风场（LBM 帧）里使用 | three r187dev 的 WebGPU 后端：`RGBFormat` 只支持 `RGB9E5`/`RG11B10`（`WebGPUTextureUtils.js` L1661–1676），3 分量 f16 无法直接上传，只能用 RGBA16F。r17 实测 SF 可视化网格为 1.34 MB，本来就超过 r27 规定的 256 KB WS 上限 |
| 4 | WindSpec 的方向约定 | 标量方向**只有一种**：`dir_from_deg`，气象来向，从**网格北（World ENU +Y）**起顺时针。所有矢量一律是**去向 ENU**（u 向东、v 向北、w 向上）。r16 WindSpec 里的矢量字段 `base/gust/dispWind` 不再作为线上主数据；换算只在 `conventions.{py,ts}` 里做 | WindNinja `wind_sd_to_uv`：`u = −s·sin θ, v = −s·cos θ`。r16 与 r17 在这一点上一致，分歧出在字段形态（矢量还是 speed+dir）。另外 natural-disasters 的 `windAngle` 是"去向、数学角、弧度"，gz 用去向 ENU，PX4 和 AirSim 用 NED，移植时各需一次换算（§2.2） |
| 5 | 湍流：每机 Dryden 还是共享冻结湍流盒 | 物理侧默认用 **`box`**：全场共享的冻结 von Kármán 盒（64³×4 m），物理和前端粒子读**同一个 f16 资产**；它是确定性的，seek 和回放无需保存 RNG 状态，多机之间也有空间相关。**`dryden`** 保留给单机 MIL 规范回归和 SIH 对照，采用**精确离散**：`Qd = P∞ − ΦP∞Φᵀ`，并从平稳分布起步。**RotorPy 的 `GustModelBase` 不要照搬** | 盒子按"平移采样坐标、旋转输出矢量"的方式使用（§5.5），风向改变时连续。RotorPy 实测：参数 σ=1 时输出 std 只有 0.029（dt=0.01）和 0.082（dt=0.05），输入是未按 1/√dt 缩放的 U(−1,1)。r17 记录的"dt=0.02 时 σ 偏高 10%"是单条 3000 s 序列的样本误差；400–2000 机集合实测 ZOH 与精确离散的偏差都 ≤0.8%（§5.5.3） |
| 6 | 流线 `streamlines.bin` 格式 | 定为 **AWSL v1**：48 B 头 + `u32 line_offsets[n+1]` + 每顶点 `f32 (x,y,z, τ̂, ŝ)`，线的顺序预先打乱，前端按"前缀 + drawRange"调密度。τ̂ 是**单位参考风速下的飞行时间**（单位 m，即"参考风走过的距离"），虚线相位 = `fract((τ̂ − S(t))/T̂)`，其中 S(t) = ∫speed_ref dt 由 anchors 提供 | 质量守恒场对风速是线性的，所以流线的几何只取决于风向；按 1° 量化缓存即可，改风速不用重算，相位也不会跳变 |
| 7 | `presets.json` 与 `derive` 的前后端对拍 | `packages/contracts/env/presets.json` v1 同时携带**常数表**、窗口、速率、路由和 12 个预设（字段是物理量，另加 `mor_bg_m`）。derive、eval_env、廓线、方向换算、选槽、光学厚度、阵风形状全部由 `tools/contracts/gen_env_golden.py` 生成 golden，Python 与 TS 都跑同一份，判据为相对误差 ≤1e-9 | `derive_check.mjs` 对 7 个预设实测，JS 与 Python 在 12 位小数内完全一致 |
| 8 | 天气推送频率：5–20 Hz 还是变化时推送 | **采用变化时推送 + 1 Hz 心跳**。`env/state` 是 msgpack 的 EnvKeyframe，包含起止快照、过渡窗口、事件列表和积分锚点（anchors）。客户端每帧用与服务端同一个纯函数 `eval_env(kf, t)` 求值，因此 60 fps 平滑、可以精确 seek，带宽低于 1 KB/s。单机的真实风（含湍流）另走 `uav/{id}/env`（raw 32 B，10 Hz，只推选中机体）。物理侧环境采样 50 Hz（>250 机时 25 Hz），物理子步之间零阶保持 | r16 需要 5–20 Hz，是因为阵风、积分位移、过渡都由服务端逐帧算好再推送。把这些都改成"关键帧 + 确定性求值"之后，高频推送就没有必要了，也不会占用 r27 的 credit 窗口 |

另外有 3 个会影响实现的新发现：

- **r17 的 SF 风场库在错误的单位上计算。** 它直接用了原始坐标：`l2_SanFrancisco_meta.json` 的 mn 为 (−370.1, −358.5)，网格 186×180×40，标称 4 m。x01 已确认旧金山 1 单位 ≈ 10.15 m，而且有 Twin Peaks 丘陵。所以这份库的物理含义不成立，只能当算法基准。**库必须在规范化后的 World ENU 上重建**，廓线高度也要改为地形跟随（`z − dtm(x,y)`）。manifest 必须带 `coordinate_hash`，入库时校验（§7.2）。
- **r17 原型 `lib_sf_manifest.json` 的 `origin` 写成了 [0,0,0]**，真实值是 (mn_x, mn_y, ground)。这会导致三线性采样整体错位。
- **r17 原型的阵风是全局单值**，驱动它的 V 取了所有机体相对空速的平均值，而且每一步都用 RNG 决定是否触发，所以前端无法复现。本文改为服务端调度、确定性求值的"阵风锋面"（§5.4）。

---

## 1. 分歧来源与核实记录

| 分歧 | 各方原文 | 核实结果 |
|---|---|---|
| σ 常数 | r16 §3.3：`σ_total = 3.0/MOR`（ICAO 5%）；r04、r06、r11、r23 §3.10、n04 §3.1.3：`3.912/V`（Koschmieder 2%）；index C9 已倾向 3.0 | 两者都是正确的公式，但描述的是不同的量。MOR 是 WMO 标准能见度（5% 阈值），METAR 和 UI 显示的都是它。V₂% 只出现在光学文献里，比如 Kim 模型的波长指数 q(V)。若把 MOR 直接代入 3.912/V，σ 会高估 30.6%（ln50/ln20 = 1.306），LiDAR 的双程衰减 e^{−2σR} 在 R = MOR/2 处会从 5% 降到 2%，差 2.5 倍 |
| 第 4 通道 | r17 §3.9：`(u,v,w,实体率)`，a > 0.5 视为建筑内部；n04 §3.3.0：`A = |u|` 或实体掩码（实体写 NaN 或 −1）；r16 §3.1.1：`w = 湍流强度 σ_u` | 实体率不可替代（粒子撞楼、箭头隐藏都要用），\|u\| 可以由 rgb 算出。σ 只有 L3 才随空间变化，V0.6 另外给一张 R16F 纹理。NaN 放在可过滤纹理里，会通过双线性或三线性过滤污染相邻 8 个样本，n04 的 NaN 方案不可取 |
| 3 分量 | r27 §3.5：`u32 version | u16 nx,ny,nz | u16 comp | f32 origin[3] | f32 cell[3] | payload f16[nx*ny*nz*3]` | WebGPU 后端不支持 RGB+HalfFloat（已引源码）。而且头部缺少 dtype、预乘标志、第 4 通道语义、扇区角、缩放和校验字段 |
| 方向 | r16 WindSpec：`base/gust` 是 ENU 矢量，UI 用来向；r17：`dir_from` 标量加 `e(θ)=(−sinθ, −cosθ)`；natural-disasters `Weather.js`：`uWindDir = (cos windAngle, sin windAngle)`，即去向数学角，而且对 `windAngle` 直接做标量阻尼（跨 0/2π 时会绕远路）；Eanpa：固定去向矢量 `(1,0,0.22)·windK·3.2` | 定为单一标量 `dir_from_deg`，矢量一律为去向。过渡时方向走最短弧（§3.3） |
| 湍流 | n03 §3.2：每机 Dryden，E 场只返回 (σ, L)，"Web 不用 Dryden"；r17 §3.3(d)：多机时切换到共享冻结盒，seed 和盒子一起下发给前端；r16：前端用 curl noise | 物理默认用 box（理由见 §5.5）；前端 Med/High 档用同一个盒子，Low 档不做湍流。curl noise 只作为 High 档之外的纯装饰选项，不代表物理 |
| 流线 | n04 §3.3.1：每条 K 个点 `(x,y,z,τ,|u|)` Float32，没有定义头、偏移和版本 | 定为 AWSL v1（§7.4） |
| 对拍 | r16 §3.7.2 与 §3.12：`derive.ts`/`derive.py` 在 1000 组输入上相对误差 <1e−6，但没有规定常数放在哪里、golden 由谁生成 | 常数进 `presets.json`，golden 由 Python 生成（§9） |
| 频率 | r16 §3.1.1：`base/gust/disp*` 随 `env.state` 10–20 Hz 发送，§3.7.3 写 5 Hz；r17 §3.8：DroneState 加 wind 10–50 Hz，摘要 1–2 Hz；r27 §3.6：`env/weather` 变化时发送，`env/wind/field` 变化时或 1 Hz | 定为关键帧加心跳（§8） |

---

## 2. 统一约定

### 2.1 坐标、单位、时间、高度基准

| 项 | 约定 |
|---|---|
| 位置 | World ENU（x 东、y 北、z 上），米，相对 `coordinate.json` 的 ENU 原点。服务端用 float64，GPU 上用 float32（10 km 内误差 ≤0.7 mm，index §3.1） |
| "北" | **网格北 = ENU +Y**。UrbanScene3D 是合成锚点（`anchor.kind="synthetic"`），不存在真北与网格北的偏差。真实场景若有子午线收敛角，由 `coordinate.json` 给出，环境模块不处理 |
| 风矢量 | 去向 ENU，m/s：`(u, v, w)` |
| 风向标量 | `dir_from_deg ∈ [0, 360)`，气象来向，顺时针。270 表示西风，吹向东 |
| 高度（环境标量） | 云底、云顶、雾顶都是 **AGL，相对 `coordinate.json.ground_z`**；廓线用的 `z_agl` 在物理侧取 `z − dtm(x,y)`（地形跟随，DTM 是裸地面，屋顶不重置廓线），前端没有 DTM 纹理时退化为 `z − ground_z` |
| 气压、温度 | ISA：`h_msl = z − ground_z + ground_h_msl`；`T = 288.15 + isa_dT − 0.0065·h_msl`（K）；`p = 101325·(T_isa/288.15)^5.25588`；`ρ = p/(287.053·T)` |
| 时间 | 线上 int64 `t_sim_ns`，从会话起点计。2⁵³ ns ≈ 104 天，单次会话远小于这个值，@msgpack/msgpack 默认解码为 number 不会丢精度；超过这个时长的录制必须开 `useBigInt64`。积分量在 CPU 上用 float64 |
| 未知值 | 二进制写 NaN，JSON 写 `null`（index §3.1）。**但可过滤纹理里禁止出现 NaN** |
| 命名 | 线上字段一律 snake_case 并带单位后缀（`_m`、`_ms`、`_mmh`、`_deg`、`_ns`）。TS 类型由 JSON Schema 生成（json-schema-to-typescript），Python 用 pydantic v2 生成（datamodel-code-generator） |

### 2.2 方向换算（唯一实现：`environment/conventions.py` 与 `apps/web/src/engine/environment/state/conventions.ts`）

```python
def from_to_uv(speed, dir_from_deg):            # WindNinja wind_sd_to_uv
    t = radians(dir_from_deg); return (-speed*sin(t), -speed*cos(t))
def uv_to_from(u, v, calm=1e-6):                 # -> (speed, dir_from); 静风时 dir 取 0 并置 calm 标志
    s = hypot(u, v);  return (0.0, 0.0) if s < calm else (s, (degrees(atan2(-u, -v)) + 360) % 360)
def e(dir_from):  t = radians(dir_from); return (-sin(t), -cos(t))   # 去向单位矢量（纵向）
def n(dir_from):  ex, ey = e(dir_from);  return (-ey, ex)             # 横向（e 逆时针转 90°）
def shortest_arc(a, b): return ((b - a + 540) % 360) - 180           # 过渡用
enu_to_three(u, v, w) = (u, w, -v);   three_to_enu(x, y, z) = (x, -z, y)
enu_to_ned(u, v, w)   = (v, u, -w)                                    # PX4 / AirSim simSetWind
```

测试向量已用 `g06_blend_rot.py` 核对过，速度 10 m/s：

| dir_from | (u, v) | 反算 |
|---|---|---|
| 0 | (0, −10) | 0 |
| 45 | (−7.071, −7.071) | 45 |
| 90 | (−10, 0) | 90 |
| 180 | (0, 10) | 180 |
| 225 | (7.071, 7.071) | 225 |
| 270 | (10, 0) | 270 |
| 359.5 | (0.0873, −9.9996) | 359.5 |

移植外部代码时的换算：

| 来源 | 其方向含义 | 转为我们的 dir_from |
|---|---|---|
| WindNinja、气象数据（METAR/NWP） | 来向，从北顺时针 | 相同 |
| OpenFOAM `flowDir` | 去向单位矢量 | `flowDir = (e_x, e_y, 0)` |
| gz `WindEffects`、`/world/<w>/wind_info` | ENU 去向矢量；种子方向是数学角（从东逆时针，去向） | `dir_from = (270 − φ_deg) mod 360` |
| natural-disasters `windAngle` | 去向数学角（弧度），并且在 three 的 XZ 平面内 | 按同一公式换算；而且标量阻尼要改成最短弧 |
| PX4、AirSim | NED 矢量 | 先转 ENU，再用 `uv_to_from` |

---

## 3. 环境状态模型：配置、标量、派生、锚点、事件

### 3.1 五层划分

| 层 | 内容 | 是否插值 | 谁计算 | 线上位置 |
|---|---|---|---|---|
| **Config**（非插值配置） | 风场等级 `level`、风场库引用、廓线参数（取自 World）、湍流模型与盒子资产、阵风模型、`seed` | 否。变更时发一个 `mode:"step"` 关键帧，前端对纹理做交叉淡化 | 服务端 | `env/state.config` |
| **EnvScalars**（插值标量） | 见 §3.2 | 是，按轴分窗口 | 服务端与客户端同一纯函数 `eval_env` | `env/state.from/to/via` |
| **Derived**（派生量） | σ 的各分量、总 MOR、rainK/snowK、sunVis 等 | — | 同一纯函数 `derive`（§4） | 不上线，两端各自算 |
| **Anchors**（积分锚点） | `S_m`、`D_enu_m`、`fall_m`、`wetness`、`puddle` | — | 服务端积分为权威，客户端用 float64 逐帧积分并向锚点校正 | `env/state.anchors` |
| **Events**（离散事件） | 阵风锋面、闪电 | — | 服务端调度（RNG 只在服务端），提前 2 s 下发；形状函数两端一致 | `env/state.events` |

### 3.2 EnvScalars 字段（全部为物理单位）

| 字段 | 单位 | 插值组 | 插值空间 | 说明 |
|---|---|---|---|---|
| `wind.speed_ref_ms` | m/s | wind | 线性 | `ref_height_m` 处、平坦地面的入流风速（不是机体处的风） |
| `wind.dir_from_deg` | ° | wind | **最短弧** | 气象来向 |
| `wind.w_mean_ms` | m/s | wind | 线性 | L0/L1 的均匀垂直风；L2 起必须为 0（由风场库提供 w） |
| `wind.turb_sigma_u_ref_ms` | m/s | wind | 线性 | 10 m 高度处纵向湍流 rms；随高度的变化按 MIL-F-8785C 的形状（§5.5.1） |
| `wind.gust_amp_ms`、`wind.gust_rate_hz`、`wind.gust_length_m` | m/s、1/s、m | wind | 线性 | 服务端调度阵风锋面时使用 |
| `cloud.cover` | 0–1 | cloud | 线性 | |
| `cloud.type` | 0 层云 – 1 积雨云 | cloud | 线性 | |
| `cloud.base_m`、`cloud.top_m` | m AGL | cloud | 线性 | |
| `precip.rain_mmh` | mm/h | precip | 线性 | 名义雨强；实际落下的是 `rain_eff = rain·gate(cover)` |
| `precip.snow_mmh` | mm/h 水当量 | precip | 线性 | 同上 |
| `atmosphere.mor_bg_m` | m | vis | **对数** | 背景 MOR（不含降水），允许范围 [1, 50000] |
| `atmosphere.fog_top_agl_m` | m | vis | 线性 | 0 表示没有平顶雾层 |
| `atmosphere.dust` | 0–1 | vis | 线性 | 只驱动粒子数、雾色和 LiDAR 杂波类型；它的光学贡献**已经计入 `mor_bg_m`**，不能重复计算 |
| `atmosphere.isa_dT_c`、`atmosphere.rh` | K、0–1 | misc | 线性 | V0.3 为占位 |
| `lightning.rate_per_min` | 次/min | precip | 线性 | |

预设只写需要改变的字段（部分快照）。风向默认不在预设里，切换预设时保持当前风向。z0、d 等粗糙度参数属于 World，不属于天气，所以不进预设。

### 3.3 `eval_env(kf, t)`：两端共用的过渡求值

```python
def eval_env(kf, t_ns) -> EnvScalars:
    if kf.mode == "step" or t_ns >= kf.t1_ns: return kf.to
    if t_ns <= kf.t0_ns: return kf.from_
    if kf.mode == "exp":                                   # 连续驱动（脚本化 E 场、真实气象）
        dt = (t_ns - kf.t0_ns) * 1e-9
        return {f: interp(space(f), kf.from_[f], kf.to[f], 1 - exp(-RATE[group(f)] * dt)) for f in FIELDS}
    route = [kf.from_, *kf.via, kf.to]; nseg = len(route) - 1        # "smooth"：带路由的分段 smoothstep
    x = (t_ns - kf.t0_ns) / (kf.t1_ns - kf.t0_ns)
    seg = min(int(x * nseg), nseg - 1); xl = x * nseg - seg
    A, B = route[seg], route[seg + 1]
    W = WINDOWS["enter" if precip_level(B) > precip_level(A) else "leave"]     # precip_level = rain + 10·snow
    return {f: interp(space(f), A[f], B[f], smoothstep(*W[group(f)], xl)) for f in FIELDS}

interp("lin", a, b, k) = a + (b - a)·k
interp("log", a, b, k) = exp(ln a + (ln b - ln a)·k)                # MOR 在对数空间插值，感知上均匀
interp("arc", a, b, k) = (a + shortest_arc(a, b)·k) mod 360
```

- 窗口：`enter`：cloud [0, 0.7]，precip [0.3, 1]，vis [0.2, 1]，wind [0, 0.8]，misc [0, 1]；`leave`：cloud [0.3, 1]，precip [0, 0.6]，vis [0, 0.8]，wind [0, 0.8]。沿用 r16，vis 的 leave 窗口是本文补的默认值。
- exp 速率（1/s）：cloud 0.9，precip 1.6，wind 1.1，vis 1.2，misc 0.5（r16 取 natural-disasters `rates×4`）。
- 默认时长：切换预设 30 s；在 UI 上改风速或风向 3 s（r17 §3.6 建议 2–5 s，与 transitions.dev 的长动效一致）；场景重置和 seek 用 `step`。
- 这里的数值（窗口、速率、路由、常数）**全部放在 `presets.json`**，两端代码不硬编码。

### 3.4 Anchors：积分量如何保持两端一致又能精确 seek

| 锚点 | 定义 | 用途 |
|---|---|---|
| `S_m` | ∫ speed_ref(t) dt | 流线虚线相位（§7.4）；阵风锋面位置 X = f(adv_height)·S |
| `D_enu_m` | ∫ (speed_ref·e(θ), w_mean) dt | 湍流盒平移量 = f(adv_height)·D（§5.5）；雨雪 world-tiling 漂移（r16 的 `dispWind`） |
| `fall_m` | ∫ v_fall(rain_eff, snow_eff) dt | r16 的 `dispFall` |
| `wetness`、`puddle` | 服务端 ODE（τ 上升 7 s、下降 70 s；积水 τ 32/150 s，Eanpa） | 湿地面 |

客户端逐帧做 `anchor += rate(eval_env(kf, t))·dt`，用 float64 梯形积分。收到服务端锚点 `(t_a, A_srv)` 时，先算误差 `e = A_srv − A_local(t_a)`：|e| < 1 m（或湿度误差 < 0.02）时，在 0.5 s 内线性摊平；否则直接吸附。seek 和换 epoch 时一律吸附。服务端按物理 env tick（50 Hz）积分，是权威值。

### 3.5 EnvKeyframe 线上形态（msgpack；JSON Schema：`packages/contracts/env/env_state.schema.json`）

```jsonc
{
  "schema": "awr.env.keyframe.v1",
  "world_id": "sanfrancisco", "version": 17, "epoch": 3, "seed": 7,
  "t_ns": 123400000000,                         // 本条消息生成时刻（心跳也会更新）
  "config": {
    "wind": {
      "level": 2,
      "profile": { "kind": "log", "z_ref_m": 10, "z0_m": 0.5, "d_m": 0, "alpha": 0.25, "adv_height_m": 40 },
      "library": { "id": "sf-l2-masscons-v1", "manifest": "/worlds/sanfrancisco/environment/wind/l2/manifest.json", "sha256": "…" },
      "turbulence": { "model": "box", "asset": "/assets/env/turb/vk_s7_n64_dx4_L30.awrv", "n": 64, "dx_m": 4, "L_m": 30 },
      "gust": { "model": "cos1_front" }
    },
    "presets_sha256": "…"                         // 两端必须加载同一份 presets.json，不一致时前端报警
  },
  "mode": "smooth", "t0_ns": 120000000000, "t1_ns": 150000000000,
  "from": { /* EnvScalars */ }, "via": [ /* EnvScalars… */ ], "to": { /* EnvScalars */ },
  "anchors": { "t_ns": 123400000000, "S_m": 18234.5, "D_enu_m": [18230.1, -402.7, 0.0], "fall_m": 60211.0, "wetness": 0.42, "puddle": 0.08 },
  "events": [
    { "kind": "gust", "id": 12, "t0_ns": 123000000000, "X0_m": 5120.4, "s_min_m": -3710.0, "amp_ms": 5.0, "lam_m": 120.0 },
    { "kind": "lightning", "id": 3, "t_ns": 125100000000, "pos_enu": [812.0, -233.5, 96.0], "peak": 0.93,
      "strokes_ms": [0, 110, 262], "amps": [1.0, 0.61, 0.47] }
  ],
  "vis": { "streamlines": "/api/sim/env/streamlines/sf-l2-masscons-v1/d270.awsl", "vmax_ms": 14.2 }
}
```

---

## 4. 能见度与消光：唯一真值与派生

### 4.1 derive（`environment/weather/derive.py` 与 `engine/environment/state/derive.ts`，逐行对应 `env_ref.py::derive`）

```text
gate      = smoothstep(0.6, 0.85, cover)                       # 云量不够时不会真的下雨（过渡途中不会晴天落雨）
rain_eff  = rain_mmh · gate ;  snow_eff = snow_mmh · gate      # 物理（阻力、传感器）和视觉都用 *_eff
σ_rain    = 2.6e−4 · rain_eff^0.63      (m⁻¹，占位系数，待标定；r16)
σ_snow    = 1.0e−3 · snow_eff^0.8
σ_precip  = σ_rain + σ_snow
σ_bg      = K_MOR / clamp(mor_bg_m, 1, 50000)                  # K_MOR = ln 20 = 2.995732273553991
σ_fog     = fog_top_agl_m > 0 ? 0.9·σ_bg : 0                   # 平顶雾层（fog_top 以下）
σ_haze0   = σ_bg − σ_fog                                       # 指数霾，地面值；标高 H = 1500 m
σ_ground  = σ_haze0 + σ_fog + σ_precip                         # 2 m AGL、雾层内的总消光
mor_m     = K_MOR / σ_ground                                   # UI 显示的"能见度 MOR"
rainK     = clamp(ln(1+rain_eff)/ln 51, 0, 1) ;  snowK = clamp(ln(1+10·snow_eff)/ln 51, 0, 1)
sunVis    = (1 − 0.9·cover^1.5)·(1 − 0.6·dust)
mpLambda  = 4.1 · max(rain_eff, 0.1)^−0.21                     # Marshall–Palmer（mm⁻¹）
cloudOD   = clamp((top_m − base_m)·0.022·0.35, 0, 6)
```

与 r16 的差别：r16 的 `σ_fog0 = σ_total`、`σ_haze = 0.1·σ_total`，雾层内地面处 σ 为 1.1·σ_total，与 MOR 的定义不一致，而且把降水也算进了雾层。本文保证**地面雾层内的 σ 恰好等于 K_MOR/mor_m**，并且降水只进入"云底以下的均匀层"。

### 4.2 12 个预设（r16 表，只把总 MOR 换成 `mor_bg_m`；由 `env_ref.py::mor_bg_from_total` 计算）

| 预设 | 云量 | 雨/雪 mm/h | 作者给的总 MOR | **mor_bg_m** | 雾顶 m | derive 回算 MOR | σ_precip | 风速 | σ_u,ref | 阵风 幅值/速率（待标定） |
|---|---|---|---|---|---|---|---|---|---|---|
| clear | 0.05 | 0/0 | 30000 | 30000 | 0 | 30000 | 0 | 3 | 0.5 | 0 |
| partlyCloudy | 0.35 | 0/0 | 20000 | 20000 | 0 | 20000 | 0 | 5 | 1.0 | 0 |
| overcast | 0.90 | 0/0 | 12000 | 12000 | 0 | 12000 | 0 | 6 | 1.2 | 0 |
| lightRain | 0.90 | 1.5/0 | 6000 | 18309 | 0 | 6000 | 3.36e−4 | 5 | 1.2 | 0 |
| rain | 0.95 | 6/0 | 3000 | 15389 | 0 | 3000 | 8.04e−4 | 8 | 2.0 | 3 m/s / 1/60 s |
| heavyRain | 1.00 | 25/0 | 1200 | 5751 | 0 | 1200 | 1.98e−3 | 11 | 3.0 | 5 / 1/45 |
| thunderstorm | 1.00 | 45/0 | 800 | 3390 | 0 | 800 | 2.86e−3 | 14 | 5.0 | 8 / 1/30 |
| fog | 0.50 | 0/0 | 150 | 150 | 60 | 150 | 0 | 1.5 | 0.3 | 0 |
| haze | 0.20 | 0/0 | 3000 | 3000 | 0 | 3000 | 0 | 2 | 0.4 | 0 |
| snow | 0.95 | 0/2 | 1500 | 11700 | 0 | 1500 | 1.74e−3 | 5 | 1.5 | 0 |
| blizzard | 1.00 | 0/5 | 150 | 183 | 0 | 150 | 3.62e−3 | 18 | 5.0 | 8 / 1/30 |
| sandstorm | 0.30 | 0/0 | 400 | 400 | 0 | 400 | 0 | 16 | 5.0 | 8 / 1/30 |

- r16 表中的"阵风 σ"一列即 `turb_sigma_u_ref_ms`。
- 所有预设的 σ_bg 都为正，说明 r16 作者给的总 MOR 与雨雪经验公式能够自洽。
- 前端 UI：能见度主数字显示派生的 `mor_m`；滑杆编辑 `mor_bg_m`，标签为"背景能见度（不含降水）"。调雨强时，主数字会跟着降低，这是期望行为。

### 4.3 沿路径的光学厚度（传感器与前端雾共用同一数学）

`optical_depth(ro, rd, L)` 由三部分相加：

- 指数霾：`σ_haze0·e^{−z_agl/H}`，Quilez 解析积分，即 r16 的 `fogOD`；
- 平顶雾层：`σ_fog`，只计 z_agl < fog_top 的线段长度；
- 降水层：`σ_precip`，只计 z_agl < cloud_base 的线段长度。

后两部分用同一个 `groundFogOD(top)` 函数。前端 shader 的 uniform 与之一一对应：

| uniform | 取值 |
|---|---|
| uSigma0 | σ_haze0 |
| uH0 | ground |
| uHs | H |
| uSigmaFog | σ_fog |
| uFogTop | fog_top |
| uSigmaPrecip | σ_precip |
| uPrecipTop | cloud_base |

`env_ref.py::optical_depth` 与 2 万段中点数值积分在 200 条随机射线上的最大相对误差为 1.8e−4，来源是数值积分本身的截断误差。天空射线（L = 20 km）在雨天会被降水层裁到云底，不会整片变黑。

### 4.4 各模块怎么用 σ（强制规则）

| 模块 | 规则 |
|---|---|
| Web 雾、天空 | `T = exp(−optical_depth)`，禁止使用 three 的 `FogExp2` 和 `densityFogFactor`（它们是 Exp² 形式） |
| 相机退化（V0.4） | `I = J·T + A·(1 − T)`，与 Web 雾同一个函数 |
| LiDAR（V0.4，r04/r05/r06） | 波长换算：`V₂ = K_V2/σ_bg,550`（K_V2 = ln 50 = 3.912，单位 km 代入 Kim 模型）；`q = 1.6 (V₂>50)、1.3 (6<V₂≤50)、0.16V₂+0.34 (1<V₂≤6)、V₂−0.5 (0.5<V₂≤1)、0 (≤0.5)`；`σ_λ = σ_bg·(λ/550)^−q + σ_precip`（降水粒径远大于波长，与 λ 无关）；两程透过率 `T² = exp(−2·τ_λ)`。MID-360 的 λ = 905 nm；V₂ = 10 km 时气溶胶部分的系数约为 0.52 |
| takram 大气（High 档，n04） | Mie 系数按 `mor_m` 量化到档位后再用，并加防抖，避免反复重算 LUT |
| UI | 显示 `MOR 850 m` / `12.0 km`（d01 数字格式），绝不显示 σ 或无单位的 0–1 |
| 禁止 | 任何模块自己从 MOR 用 3.912 换算 σ，或者自己再维护一个"visibility"字段 |

---

## 5. 风场：均值 + 阵风 + 湍流

### 5.1 合成公式（物理与前端相同，前端只是分档省略部分项）

```text
W(p, t) = s(t)·M(p; θ(t))                    # 均值：L0/L1 解析廓线，或 L2/L3 风场库
        + w_mean(t)·ẑ                         # 仅 L0/L1
        + G(p, t)·e(θ(t))                     # 阵风锋面（§5.4）
        + T(p, t)                             # 湍流：box 或 dryden（§5.5）
其中 s = speed_ref_ms，θ = dir_from_deg；
M 是"参考风速为 1 m/s"的归一化场。
```

### 5.2 廓线与高度（`environment/wind/profile.py`，与 `env_ref.py::profile` 一致）

- `log`：`f = ln((z_agl−d)/z0) / ln((z_ref−d)/z0)`，当 z_agl ≤ d+z0 时 f = 0（WindNinja `windProfile.cpp` L73–76 的原样行为）。
- `power`：`f = (z_agl/z_ref)^α`。
- `uniform`：f = 1。
- 廓线参数来自 `world.json.environment.roughness`（UrbanScene3D 城区默认 z0 = 0.5 m、d = 0）。**启用风场库时一律以库 manifest 的 `ref` 为准**，UI 上把这部分参数置灰，这样库边界外的解析场和库内能连续衔接。
- `adv_height_m = 40`：湍流盒和阵风锋面的迁移速度取 `s·f(40 m)`，这是 Taylor 冻结假设下的对流速度。

### 5.3 L2/L3 风场库：扇区混合的旋转形式、反对称与选槽

**旋转形式。** r17 的"flow-aligned"混合（分解到各扇区的 (∥, ⊥, w)，混合后再重组）**在数学上等价于**：把每个扇区场的水平分量**旋转 −(θ − aᵢ)**（ENU 中逆时针为正），再做线性加权。`g06_blend_rot.py` 实测两种写法的最大差 1.07e−14；SF 270/300 → 285 的误差与 r17 相同（平均 0.019，p95 0.060 m/s）。这样 shader 里每个槽只需要一个 2×2 矩阵：

```text
slot_i = (tex_i, M_i = wᵢ·sgnᵢ·Rot(−(θ − aᵢ)), m_i = wᵢ·sgnᵢ)     # CPU 每帧算好，作为 uniform 传入
M(p;θ).xy = Σ_i M_i · f_i(p).xy ;   M(p;θ).z = Σ_i m_i · f_i(p).z   # i = 两个相邻扇区
```

**反对称。** L2 满足 `f(a+180°) = −f(a)`（实测最大误差 1.9e−6），所以只存 [0°, 180°) 的 6 个扇区。选槽时把它们展开成 12 个虚拟扇区，符号 sgn 折进矩阵里（`env_ref.py::sector_slots`）。例如 θ = 285° 时，两个槽分别是 file 3（90°）取 sgn = −1 和 file 4（120°）取 sgn = −1，权重各 0.5；θ = 350° 时，是 file 5（150°）sgn = −1、权重 1/3，以及 file 0（0°）sgn = +1、权重 2/3。L3 是非线性的，需要存满 360°（15°–22.5° 一步），`antisymmetric=false`，风速档取最近一档并按比例缩放（Re 无关近似）。

**网格边界。** 以下规则物理和前端完全一致，CPU 镜像参与对拍：

```text
dist = min(p − gridMin, gridMax − p) 在 x、y 与顶面方向上的最小值（地面方向不做淡出）
wg   = smoothstep(0, 2·cell, dist)
mean = mix(s·f(z_agl)·e(θ), s·M_lib(p;θ), wg)          # 出界、越过顶面时，平滑退回 L0 廓线
```

**实体。** 物理侧用全分辨率的 `solid.u8` 判定。点落在实体内时 wind = 0，`flags.IN_SOLID = 1`，`valid = false`；机体进入实体本身就是碰撞，交给 safety 处理。前端用 AWRV 的 a 通道：a > 0.5 时粒子重生、箭头隐藏。

**预乘还原。** 前端采样后做 `u = rgb/max(1 − a, 0.5)`。它的误差见 §0 第 2 行；只看近壁流体格，平均误差从 1.63 降到 0.79 m/s，偏差从 −1.31 降到 +0.38 m/s。

### 5.4 阵风：服务端调度、两端确定性求值的"1−cos 锋面"

r17 原型的阵风是全局单值，每步用 RNG 触发，前端没法复现，而且对所有机体同时生效，不符合 Taylor 冻结假设。改为沿风向推进的锋面：

```text
服务端（仅此处用 RNG）：事件间隔 gap ~ Exp(gust_rate_hz)，clamp 到 [2, 600] s；
  amp = gust_amp_ms·U(0.5, 1)；lam = 2·gust_length_m（默认 d_m = 60 m，即 MIL-F-8785C 的 1−cos 全波）
  s_min = min over world bbox corners of e(θ₀)·p_xy        # 上风边界
  事件至少提前 2 s 写入 env/state.events
两端求值（env_ref.py::gust_along）：
  X(t) = f(adv)·S(t)                                        # 锋面行进距离
  ξ = X(t) − X0 + s_min − e(θ(t))·p_xy
  G = (0 ≤ ξ ≤ lam) ? amp/2·(1 − cos(2π ξ/lam)) : 0       # 同时存在的多个锋面相加
```

锋面穿过所有 X(t) − X0 > (s_max − s_min) + lam 的位置之后即可删除。不同位置的机体会先后遇到同一道锋面，前端粒子也能看到它扫过。

### 5.5 湍流

#### 5.5.1 强度与尺度（两种模型共用）

- 高度按 MIL-F-8785C 低空形状缩放：`σ_w = 0.5295·σ_u,ref`（常数）；`σ_u(h) = σ_v(h) = σ_w/(0.177 + 0.000823·h_ft)^0.4`，其中 h_ft = max(z_agl/0.3048, 10)。
- 系数 0.5295 = (0.177 + 0.000823·32.8)^0.4，即 10 m 高度处的 σ_u 恰好等于 `turb_sigma_u_ref_ms`。
- UI 的"轻 / 中 / 强"标签对应 W20 = 7.7 / 15.4 / 23.1 m/s，换算成 σ_u,ref = 1.45 / 2.91 / 4.36 m/s。
- L3 的 σ 用 `sqrt(2k/3)`，按 (s/s_bin) 缩放。

#### 5.5.2 `model: "box"`（默认）

```python
# 生成：r17_turb_box.py::vk_box(N=64, dx=4, L=30, seed)
# 必须使用修正波数 sin(kΔ)/Δ 做 Helmholtz 投影；每分量归一化为单位 rms；存成 AWRV kind=2, RGBA16F（A=0）
# 采样（物理与前端相同）：
q     = p − f(adv)·D(t)                                      # 平移采样坐标（D 是锚点积分量）
b     = trilerp_periodic(box, q / dx)                        # 64³ 周期盒，周期 256 m
Rθ    = [[e_x, n_x], [e_y, n_y]]                             # 平均风坐标系 → ENU（水平）
T.xy  = Rθ · diag(σ_u(z), σ_v(z)) · Rθᵀ · b.xy               # 旋转输出矢量，而不是旋转采样坐标
T.z   = σ_w · b.z
```

- **不能**照 r17 伪代码去旋转采样坐标 `R(θ)ᵀ(x − U t)`：θ 一变，远离原点的点会整片跳变。盒子是各向同性的，本来就不需要随风向转。
- 物理侧读取的是与前端**同一份 f16 资产**（解码成 f32 后查询），所以数值完全一致。
- 为什么选它作默认：
  1. 确定性：由 (seed, D(t)) 唯一决定，seek 和回放不需要保存 RNG 状态；
  2. 空间相关：两架相距 5 m 的机体感受到相关的扰动，编队和避碰评估才有意义；
  3. 前端能看到与物理一致的扰动；
  4. 成本低：生成 0.25 s，查询约 0.5 ms/1000 点。

#### 5.5.3 `model: "dryden"`（MIL 规范回归、与 SIH 对照）

以 r17 的向量化实现为基础，做两处改动：v、w 两个二阶通道改为**精确随机离散**，并且从**平稳分布**起步（否则开头 L/V 秒内几乎没有湍流）。

```python
# u 通道：精确 OU（r17 原样）：a = exp(−V·dt/L_u); x = a·x + σ_u·sqrt(1−a²)·N(0,1)
# v/w 通道：A = [[0,1],[−1/T²,−2/T]]，T = L/V，输入强度 q = π，输出 y = (K/T²)·(x1 + √3·T·x2)，K = σ·sqrt(L/(πV))
Φ   = e^{−dt/T}·[[1+dt/T, dt], [−dt/T², 1−dt/T]]
P∞  = diag(π·T³/4, π·T/4)                                   # 闭式平稳协方差 => Var(y) = σ²
Qd  = P∞ − Φ·P∞·Φᵀ ;  Lq = chol(Qd)                          # 对任意 dt 都精确（常参数时）
x0  = chol(P∞)·N(0, I)                                      # 平稳起步
x   = Φ·x + Lq·N(0, I)
V   = max(|U_mean − v_vehicle|, 0.5)                          # 相对空速（r17）；T 与 σ 按 env tick 更新
RNG = numpy.random.Generator(PCG64(SeedSequence([seed, agent_no])))   # 每机独立流，与 agent 顺序无关
```

实测（`g06_dryden.py`、`g06_dryden2.py`，400–2000 机集合）：

| 条件 | r17 的 ZOH 离散 | 精确离散 |
|---|---|---|
| h 为 20–120 m，dt 为 0.01、0.02、0.05 | σ 偏差 ≤0.8% | σ 偏差 ≤0.8% |
| dt/T = 0.25（L = 3 m，V = 15 m/s，dt = 0.05） | −0.6% | +0.0% |

- **r17 所说"dt = 0.02 时 σ 偏高约 10%"是单条 3000 s 序列的样本误差**，不是离散化误差。
- 选精确离散主要是为了平稳起步，并且结论不依赖 dt。
- **RotorPy `GustModelBase` 不能照搬**：
  - 输入是 U(−1,1)，没有按 1/√dt 缩放，输出 std ≈ σ·√(dt/3π)，而且随 dt 变化。实测 σ = 1 时，dt = 0.01 输出 0.029，dt = 0.05 输出 0.082。
  - 它的传递函数是 MIL-HDBK-1797 的 2L 形式，却配了 8785C 的 L_z = h，相当于尺度放大了 2 倍。
  - 默认 V = 1 m/s。
  - n03 §3.2 建议移植 RotorPy 的 Dryden，应改为移植本节的实现。

#### 5.5.4 前端

- Low 档：没有湍流。
- Med、High 档：用同一个 box 纹理，乘以同一个 σ(z)。
- curl noise 只在 High 档作为可选的"视觉细节"，默认关闭。

---

## 6. `environment.query` API

### 6.1 Python（sim-core 进程内，向量化；`environment/field.py`）

```python
class Frame(IntEnum):                     # 语义取自 gz TransformTypes.hh（r23 §3.9），只作用于风矢量
    GLOBAL = 0                            # ENU 世界系
    LOCAL = 1                             # 机体 FLU：Rᵀ·w（R = WORLD←BODY，由 quat_xyzw 给出）
    ADD_VELOCITY_GLOBAL = 2               # w − v（相对风，即空速计看到的气流，ENU）
    ADD_VELOCITY_LOCAL = 3                # Rᵀ·(w − v)

class Fields(IntFlag):
    WIND = 1          # wind（总风）
    WIND_PARTS = 2    # wind_mean / wind_gust / wind_turb
    TURB_SPEC = 4     # turb_sigma(3) / turb_L(3)（n03 需要的谱参数）
    OPTICS = 8        # sigma_ext / mor_m / in_fog / sigma_precip
    PRECIP = 16       # rain_eff_mmh / snow_eff_mmh / dust
    THERMO = 32       # temperature_c / pressure_pa / rho / rh
    DEFAULT = WIND | OPTICS | THERMO
    ALL = 63

class EnvFlags(IntFlag):
    VALID = 1; IN_SOLID = 2; OUTSIDE_GRID = 4; ABOVE_GRID = 8; IN_FOG_LAYER = 16; BELOW_CLOUD_PRECIP = 32; CALM = 64

@dataclass(slots=True)
class EnvSampleSoA:                       # 全部为预分配的 numpy 数组，形状 (N,) 或 (N,3)，可复用 out= 参数
    wind: f64[N,3]; wind_mean: f64[N,3]; wind_gust: f64[N,3]; wind_turb: f64[N,3]
    turb_sigma: f32[N,3]; turb_L: f32[N,3]
    sigma_ext: f32[N]; mor_m: f32[N]; sigma_precip: f32[N]
    rain_eff_mmh: f32[N]; snow_eff_mmh: f32[N]; dust: f32[N]
    temperature_c: f32[N]; pressure_pa: f32[N]; rho: f32[N]; rh: f32[N]
    flags: u8[N]; source_level: u8[N]     # 0 解析 L0、1 解析 L1、2 质量守恒库、3 CFD 库、4 LBM 帧（V0.4+）

class EnvironmentService(Protocol):
    def query(self, pos: f64[N,3], t_ns: int, *, fields: Fields = Fields.DEFAULT, frame: Frame = Frame.GLOBAL,
              vel: f64[N,3] | None = None, quat_xyzw: f64[N,4] | None = None,
              agent_idx: i32[N] | None = None, out: EnvSampleSoA | None = None) -> EnvSampleSoA: ...
    def optical_depth(self, p0: f64[N,3], p1: f64[N,3], t_ns: int, *, wavelength_nm: float = 550.0) -> f64[N]: ...
    def scalars(self, t_ns: int) -> EnvScalars: ...          # = eval_env(current_kf, t)
    def derived(self, t_ns: int) -> Derived: ...             # = derive(scalars(t))
    def keyframe(self) -> EnvKeyframe: ...                   # 当前关键帧（含 anchors、events）
    def apply(self, cmd: EnvCommand) -> EnvKeyframe: ...     # set/preset/wind/reset；生成新的关键帧，version+1
    def grid(self, bbox: Box3, spacing_m: float, t_ns: int, fields: Fields) -> bytes: ...   # AWRV kind=3，调试和热力图切片
    def streamlines(self, n_lines: int = 1000, k: int = 64) -> bytes: ...                   # AWSL，按 (library, θ 取整到 1°) 缓存
    def checkpoint(self) -> bytes: ...; def restore(self, b: bytes) -> None: ...            # 包含 DrydenBank 状态与 anchors
```

- `agent_idx` 仅在 `dryden` 模型下需要（用来索引每机的滤波器状态）；`box` 模型不需要。
- `vel` 用于 Taylor 公式里的 V 以及 `ADD_VELOCITY_*` 两种帧。
- 某些量在某些位置无意义时写 NaN，比如实体内的 `rho` 仍然有效，但 `wind` 为 0，同时置 `VALID = 0`。

### 6.2 FleetSim 中的调用（`sim/fleet/stages/env.py`，接 G8 的 pipeline）

```python
def env_stage(st, t_ns, env, c):                  # 物理 250 Hz 时 c.k = 5（即 50 Hz）；N > 250 时 c.k = 10（25 Hz）
    if st.step % c.k: return                      # 零阶保持
    s = env.query(st.pos, t_ns, fields=Fields.WIND | Fields.THERMO | Fields.OPTICS,
                  vel=st.vel, agent_idx=st.agent_no, out=c.buf)
    st.wind[:] = s.wind; st.rho[:] = s.rho; st.env_flags[:] = s.flags
# aero stage（r23 §3.8）：v_rel = v − wind；F = −½ρ·CdA·|v_rel|·v_rel − ΣΩ·c_rd·v_rel⊥；推力 ×= ρ/ρ0
# 可选：对 6 个电机位置各采样一次（N×6 个点），得到风切变力矩
```

- 耗时（本机、有负载，`g06_query_flat.py`、`g06_query_opt.py`）：2 个 L2 扇区融合查询用单次 flat-gather，N = 1000 时 1.4 ms，N = 4000 时 5.4 ms；加上 box，N = 1000 时约 2.2 ms。
- 按 50 Hz、1000 机算，约占 11% 核；按 25 Hz 算约 5.5%。
- 实现要点：
  - 两个扇区在内存里拼成 `(nz,ny,nx,6)`，下标只算一次。与逐扇区分别做三线性相比，这一步把 N = 1000 时的耗时从 3.6 ms 降到 2.2 ms（均含 box），快约 1.6 倍，是主要收益；
  - 用扁平下标 `Lf[idx]` 一次 gather 8 个角点，比三下标的 `g[k,j,i]` 再快约 8%（`g06_misc.py`：0.97 对 1.06 ms）；
  - 也比 `scipy.ndimage.map_coordinates` 逐通道调用更快。

### 6.3 对外接口

| 形式 | 名称 | 参数 → 返回 | 说明 |
|---|---|---|---|
| WS RPC（`awr.rt.v1` `call`） | `env/query` | `{points:[[x,y,z]…] ≤1024, t_ns?, fields?, frame?, vel?, quat?}` → msgpack SoA | UI 探针、Agent 工具（V1.0 的 `env.query` capability 最多 64 点，JSON） |
| WS RPC | `env/set` | `{patch: 部分 EnvScalars 或 Config, duration_s?, mode?}` → `{version}` | 需要 operator 权限（Control Lease） |
| WS RPC | `env/preset` | `{name, duration_s=30}` → `{version, route}` | 按路由生成 `via` |
| REST | `GET /api/sim/env/state?t_ns=` | → EnvKeyframe | Timeline 与迟到客户端 |
| REST | `POST /api/sim/env/query` | 同 RPC | 工具和脚本 |
| REST | `GET /api/sim/env/streamlines/{lib}/d{deg}.awsl` | → AWSL | 带 `Cache-Control: immutable` |
| 静态 | `/worlds/{wid}/environment/**`、`/assets/env/turb/*.awrv` | Range + immutable | 由 Starlette StaticFiles 提供（已实测支持 206） |

### 6.4 单机环境样本 `EnvSample32`（raw，写入 `packages/contracts/rt/layouts.json`）

```json
{"schemaName":"awr.EnvSample32.v1","layout":{"size":32,"fields":[
 {"n":"wind","t":"f32","c":3,"o":0},
 {"n":"wind_mean","t":"i16","c":3,"o":12,"scale":0.01},
 {"n":"turb_sigma_uw","t":"u8","c":2,"o":18,"scale":0.05},
 {"n":"sigma_ext","t":"f32","o":20},
 {"n":"rain_eff","t":"u16","o":24,"scale":0.01},
 {"n":"rho","t":"u16","o":26,"scale":3e-5},
 {"n":"flags","t":"u8","o":28},
 {"n":"source_level","t":"u8","o":29},
 {"n":"gust","t":"i16","o":30,"scale":0.01}]}}
```

- 数值是物理 env tick 的真值（含湍流），用于 HUD（"本机风 9.1 m/s · 来向 W · 空速 12.3"）、风力箭头（01-design §40），以及分析图表（lieflat 时序图）。
- 空速由前端用 Full64 的 `vel` 减 `wind` 得到，不单独传。

---

## 7. 文件与二进制格式

### 7.1 World Package 落点

```text
worlds/<id>/environment/
├── env.json                       # 默认 Config：roughness{z0,d}、默认预设、ground_h_msl（引用 coordinate.json）
├── wind/l2/manifest.json          # §7.2
├── wind/l2/dir_000.f16.zst …      # 物理：全分辨率 (nz,ny,nx,3) f16 + zstd-3，mmap 后解压
├── wind/l2/solid.u8.zst
├── wind/l2/vis/dir_000.awrv.gz …  # 前端：2 倍降采样 RGBA16F（预乘 + 实体占比），AWRV v1
└── wind/l3/…                      # V0.6+，同构，多一个 k 通道；vis/tke_*.awrv（R 通道）
assets/env/turb/vk_s{seed}_n64_dx4_L30.awrv       # 与 World 无关，可在 World 之间共享
packages/contracts/env/{env_state.schema.json, wind_manifest.schema.json, presets.schema.json, presets.json, golden/}
```

### 7.2 `wind/l2/manifest.json`（在 r17 §3.7 基础上的修订）

```jsonc
{
  "schema": "awr.env.wind_library.v1",
  "level": 2, "solver": "masscons-mac-v1", "alpha_h": 1.0, "alpha_v": 1.5,
  "coordinate_hash": "sha256:…",                 // * 与 worlds/<id>/coordinate.json 不一致时拒绝加载
  "units": "m",                                  // * 必须在规范化后的 World ENU 上计算（r17 的 SF 库在原始单位上，已作废）
  "grid": { "origin_enu": [x0, y0, z0],          // * 最小角（格 (0,0,0) 的角点，不是格心）；r17 原型误写为 [0,0,0]
            "cell_m": [4, 4, 4], "shape_zyx": [nz, ny, nx], "cell_centered": true,
            "z_base": "dtm", "dtype": "f16", "channels": ["u", "v", "w"] },
  "ref": { "speed_ms": 1.0, "z_ref_m": 10, "z0_m": 0.5, "d_m": 0, "profile": "log" },
  "sectors_deg": [0, 30, 60, 90, 120, 150], "antisymmetric": true, "speeds_ms": [1.0],
  "files": ["dir_000.f16.zst", "…"], "solid": "solid.u8.zst",
  "vis": { "downsample": 2, "encoding": "awrv-rgba16f-premul", "files": ["vis/dir_000.awrv.gz", "…"], "vmax_ms_per_ref": 1.92 },  // |M| 的 p99.9；1.92 是 r17 原始单位 SF 库的示例值
  "edge_blend_cells": 2, "w_clip_p999_ms_per_ref": 0.31,   // |w| 截断阈值，示例值
  "stats": { "div_max": 3.3e-6, "wall_flux_max": 0.0, "inlet_profile_rmse": 0.0 },   // 入库验收（index §3.8）
  "created": "2026-…", "source": "UrbanScene3D/SanFrancisco@x01-normalized"
}
```

入库时 `tools/wind/build_l2_library.py` 要做的：

1. 从规范化后的 World 读取 DSM/DTM；
2. 初始场的 log 廓线按 `z − dtm(x,y)` 铺开（SF 有丘陵，x01 已确认）；
3. 求解 6 个扇区；
4. 校验散度、壁面通量、入口廓线；
5. 生成 vis：2×2×2 平均，实体格速度为 0，所以结果天然是预乘的；a = 实体占比；
6. 写 manifest 和各文件的 hash。

### 7.3 AWRV v1（AWR Volume；文件与 WS typed-blob 同一字节布局，小端）

```text
off  type      name
0    char[4]   magic = "AWRV"
4    u16       version = 1
6    u16       kind            1 wind_sector(归一化)  2 turb_box  3 wind_snapshot(已合成, m/s)  4 scalar_field  5 lbm_frame
8    u16       nx
10   u16       ny
12   u16       nz
14   u16       comp            4 = RGBA（上传 GPU 时一律 4 通道）；标量场为 1
16   u8        dtype           1 f16  2 f32  3 u8
17   u8        layout          0 = zyx C-order，x 变化最快（等价于 Data3DTexture 的 width, height, depth）
18   u8        premultiplied   1 = rgb 已乘流体占比 (1 − a)
19   u8        alpha           0 无  1 solid_frac  2 speed  3 sigma
20   f32[3]    origin_enu      最小角（格心 = origin + (i+0.5)·cell）
32   f32[3]    cell_m
44   f32       dir_from_deg    扇区角；没有意义时写 NaN
48   f32       value_scale     乘以它得到 m/s（归一化扇区为 1.0；湍流盒为 1.0，表示 σ=1）
52   u32       field_version   = 生成参数的 hash32（库 id、扇区、seed…）
56   u32       payload_bytes
60   u32       crc32(payload)
64   payload   nx·ny·nz·comp·sizeof(dtype)
```

```ts
// apps/web/src/engine/environment/wind/awrv.ts
export function decodeAWRV(buf: ArrayBuffer) {
  const dv = new DataView(buf); if (dv.getUint32(0, true) !== 0x56525741) throw new Error('AWRV magic'); // "AWRV"
  const nx = dv.getUint16(8, true), ny = dv.getUint16(10, true), nz = dv.getUint16(12, true), comp = dv.getUint16(14, true);
  const origin = [0, 1, 2].map(i => dv.getFloat32(20 + 4 * i, true)), cell = [0, 1, 2].map(i => dv.getFloat32(32 + 4 * i, true));
  const data = new Uint16Array(buf, 64, nx * ny * nz * comp);                    // f16 原样作为 GPU 数据
  const tex = new THREE.Data3DTexture(data, nx, ny, nz);
  Object.assign(tex, { format: THREE.RGBAFormat, type: THREE.HalfFloatType, minFilter: THREE.LinearFilter,
                       magFilter: THREE.LinearFilter, unpackAlignment: 1, wrapS: THREE.ClampToEdgeWrapping /* box 用 Repeat */ });
  tex.needsUpdate = true;
  const cpu = typeof Float16Array !== 'undefined' ? new Float16Array(buf, 64, data.length) : decodeHalf(data); // CPU 镜像
  return { nx, ny, nz, origin, cell, dir: dv.getFloat32(44, true), scale: dv.getFloat32(48, true), tex, cpu };
}
```

- 一个 SF 扇区（93×90×20×8 B）为 1.34 MB，gzip 后 0.80 MB（r17）。前端**只拉当前两个槽用到的扇区**，再预取相邻两个。
- 风向在扇区内部变化时不需要下载任何东西，只改 uniform（`M_i`、`m_i`）。

### 7.4 AWSL v1（流线；`streamlines.awsl`）

```text
0   char[4] "AWSL" | 4 u16 version=1 | 6 u16 flags(bit0 tau 已归一到单位参考风速, bit1 已打乱)
8   u32 n_lines   | 12 u32 n_verts  | 16 f32 dir_from_deg | 20 f32 ref_speed_ms (=1.0)
24  u32 field_version | 28 u32 stride (=20) | 32 f32 tau_hat_max_m | 36 f32 s_hat_max | 40 u32 seed | 44 u32 reserved
48  u32 line_offsets[n_lines+1]（顶点下标），补齐到 8 字节
…   f32 verts[n_verts][5] = (x, y, z, τ̂, ŝ)     # ENU m；τ̂ = 参考风速 1 m/s 时累计的飞行"距离"(m)；ŝ = |M|（归一化风速）
```

- **生成**（`environment/wind/streamlines.py`，n04 算法）：
  - 播种：在 AABB 内拒绝采样，避开实体，`z = zmin + (zmax − zmin)·r^1.9`；
  - 积分：RK4，步长 `dt̂ = 0.5·cell/max(ŝ, 0.05)`；
  - 停止条件：进入实体、出界、`ŝ < 0.02` 或满 K = 64 点；少于 8 点的线丢弃；
  - 打乱线的顺序；
  - L0/L1 也走这条路径，场用解析廓线。
- **前端相位**：`φ = fract((τ̂ − S(t))/T̂)`，`T̂ = 48 m`（约相当于 8 m/s 时 6 s 一个周期，n04 用的是 T_DASH = 6 s）；颜色按 `s·ŝ/vmax` 取色带（科技灰 → 白 → #E93024）。风速改变时几何和相位都连续；风向改变 ≥1° 时换一份新流线，新旧之间用 0.4 s 交叉淡化（transitions.dev 的 crossfade token），最多 2 Hz。
- 一份 1000×64 的流线约 1.3 MB。服务端 LRU 缓存 16 份，HTTP 响应标 immutable。

---

## 8. 实时推送与频率（对 r27 §3.5–3.6 channel 表的修订）

| topic | kind | 编码 | 发送时机与频率 | 默认订阅 | 说明 |
|---|---|---|---|---|---|
| `env/state` | state | msgpack EnvKeyframe | **变化时**：`env/set`、`env/preset`、过渡开始、路由分段、新事件入表（提前 2 s）、config 变更、seek；另有 **1 Hz 心跳**（只更新 `t_ns` 与 `anchors`，约 150 B） | latest，priority 2 | 取代 `env/weather`。迟到客户端从 zenoh `cache(max_samples=1)` 或 REST 拿到最新帧即可工作 |
| `uav/{id}/env` | state | raw EnvSample32 | 原生 50 Hz（env tick） | 选中或检视的机体 10 Hz；其余不订阅 | HUD 与分析用 |
| `env/wind/frame` | blob | AWRV kind=5（超过 256 KB 时发 URL） | 只用于 L2.5 LBM 或时变 L3，0.5–2 Hz | 按需 | V0.4+；前端 A/B 两张纹理按 `(t − t0)/(t1 − t0)` 混合 |
| `event` | event | JSON | `env.changed {version, by, reason}`、`env.lightning {id, t_ns, pos}`、`env.warning {code: "wind_limit"|"mor_limit"…}` | all | 日志、Agent 与告警；闪电的视觉**不依赖**这条通道 |
| ~~`env/wind/field`~~ | — | — | **废弃** | — | 改为 World Package 里的 AWRV 静态资产，由 `env/state` 引用 |

物理与服务端内部的频率：

- 物理步 100–250 Hz；
- env 采样 50 Hz（N > 250 时 25 Hz），零阶保持；
- anchors 积分跟 env tick 走；
- 心跳 1 Hz。

湍流带宽的上限约为 V/L，量级 0.1–1 Hz（r17），所以 25–50 Hz 已经足够。

录制与回放：MCAP 按原样记录 `env/state` 的全部关键帧和心跳，回放时服务端直接重放，前端的行为与在线时完全一致。live 模式下回退到某个 checkpoint 时，`checkpoint()` 里带有 anchors 和 DrydenBank 的状态。

---

## 9. 前后端对拍（parity）

| golden 文件（`packages/contracts/env/golden/`） | 生成方式（`tools/contracts/gen_env_golden.py`，以 Python 为参考实现） | 两端测试 | 判据 |
|---|---|---|---|
| `conventions.json` | dir 从 0 到 359.5，步长 0.5，外加静风；uv<->dir、ENU<->three、ENU<->NED | `environment/tests/test_parity.py`、`engine/environment/state/parity.test.ts`（vitest） | 相对误差 ≤1e−9 |
| `profile.json` | log、power、uniform 三种廓线，各取 50 个 z，含 z ≤ d+z0 的边界 | 同上 | 相对误差 ≤1e−9 |
| `derive.json` | 12 个预设；1000 组随机状态（seed 固定，覆盖 R=0、cover<0.6、mor 两端夹紧、雾顶 0/60、dust 0/1） | 同上 | 相对误差 ≤1e−9（实测 1e−12 以内一致） |
| `eval_env.json` | 路由表中每条路由，外加 enter、leave 与 exp 三种模式，每种取 50 个 t | 同上 | 相对误差 ≤1e−9；方向比较 shortest_arc 的绝对值 ≤1e−9 |
| `sector_slots.json` | θ 从 0 到 359，步长 1；6 扇区反对称与 24 扇区非对称两种配置 | 同上 | 下标与符号完全相等；权重和矩阵 ≤1e−12 |
| `optical_depth.json` | 200 条随机射线 × 3 组状态 | 同上 | 相对误差 ≤1e−9 |
| `gust.json` | 若干事件 × 100 个 (p, S) 采样 | 同上 | ≤1e−9 |
| `awrv/sf_d090_small.awrv` + `wind_sample.json` | 16³ 的小体数据：用 Python 的 CPU 三线性（含预乘还原、边界淡出、双槽混合）采 2000 个点 | TS 的 CPU 镜像 `windCPU.ts` | 绝对误差 ≤1e−4 m/s（两边读的是同一份 f16 数据） |
| GPU 采样 | headless 下渲染一张 1024 像素的 RT：每个像素对应一个点，把 `windAtEnu` 的结果写成 RGBA32F，再读回 | Playwright（`apps/web/perf/env-gpu.spec.ts`） | 误差 ≤0.01·vmax + 0.02 m/s（GPU 线性过滤的权重可能只有 8 位小数） |

- CI 顺序：先 `python tools/contracts/gen_env_golden.py --check`（golden 与当前 Python 实现一致，确保改代码时同步更新 golden），再 `pytest environment/tests`，再 `vitest run engine/environment`。
- `presets.json` 的 sha256 同时写进 golden 头和 EnvKeyframe 的 `config.presets_sha256`。
- 为什么选 1e−9：`derive_check.mjs` 实测 JS 与 numpy/math 的 float64 超越函数差异在 1e−15 量级，1e−9 足以抓出公式层面的错误，又不会因为 ulp 级差异误报。

---

## 10. 前端采样要点（TSL，同时适用于 WebGPU 与 WebGLNodesHandler 两条路径）

```ts
// engine/environment/wind/windNode.ts —— 输入 three 世界坐标，返回 three 空间的 m/s
const W = windUniforms;  // level, s, e(θ), n(θ), groundZ, prof{kind,z0,d,zr,alpha}, texA/B, MA/MB(mat2), mA/mB, gridMin/Max,
                         // edgeBlend, box, boxDx, D(=f_adv·D_enu), σ(u_ref), Rθ, gust[4]{X0,s_min,amp,lam}, X
export const windAtEnu = Fn(([p]) => {                               // p 为 ENU
  const zagl = max(p.z.sub(W.groundZ), 0.0);
  const f = profileNode(zagl);                                       // 与 profile.py 同式
  const ana = vec3(W.e.mul(W.s.mul(f)), W.wMean);
  const uvw = p.sub(W.gridMin).div(W.gridMax.sub(W.gridMin));
  const a = texture3D(W.texA, uvw), b = texture3D(W.texB, uvw);
  const fa = a.xyz.div(max(float(1).sub(a.w), 0.5)), fb = b.xyz.div(max(float(1).sub(b.w), 0.5));  // 预乘还原
  const lib = vec3(W.MA.mul(fa.xy).add(W.MB.mul(fb.xy)), W.mA.mul(fa.z).add(W.mB.mul(fb.z))).mul(W.s);
  const wg = edgeBlendNode(p);                                       // smoothstep(0, 2·cell, dist)
  const mean = mix(ana, lib, wg.mul(W.libOn));
  const gust = gustNode(p).mul(vec3(W.e, 0));                        // 最多 4 个活跃锋面
  const turb = turbBoxNode(p);                                       // q = p − D；输出 Rθ·diag·Rθᵀ
  return mean.add(gust).add(turb);                                   // 调用方再做 ENU → three：(x, z, −y)
});
```

- 每帧由 `EnvStore` 做以下几件事：`kf` → `eval_env(t)` → `derive` → 各 uniform；积分 anchors；算选槽和 2×2 矩阵。**所有换算都只在这里做一次**（r16 §3.0 的原则）。
- CPU 镜像 `windCPU.ts`：同一套公式，读 AWRV 的 `cpu` 数组，供 HUD 探针、风箭头和离线 mock 使用。纹理解码优先用 `Float16Array`（Chrome 135+、Firefox 129+、Safari 18.2+），否则退回 `DataUtils.fromHalfFloat`。
- 档位：Low 只有流线（AWSL）加解析雾，不采样 3D 纹理；Med 用两张扇区纹理加 box；High 在 Med 的基础上用 compute 迹线，curl 细节可选。

---

## 11. 落点文件清单

| 文件 | 内容 | 版本 |
|---|---|---|
| `packages/contracts/env/env_state.schema.json` | EnvKeyframe、EnvScalars、Config、Anchors、Events | V0.1 |
| `packages/contracts/env/presets.json`、`presets.schema.json` | 常数、窗口、速率、路由、12 个预设（§4.2） | V0.1 |
| `packages/contracts/env/wind_manifest.schema.json` | §7.2 | V0.3 |
| `packages/contracts/env/formats/{awrv,awsl}.md` | §7.3、§7.4 | V0.2 |
| `packages/contracts/rt/layouts.json` | 追加 `awr.EnvSample32.v1` | V0.1 |
| `packages/contracts/env/golden/*` | §9 | V0.1 |
| `environment/conventions.py`、`environment/field.py` | 方向换算；`EnvironmentService`（query、optical_depth、keyframe、apply） | V0.1 |
| `environment/wind/{profile,library,gust,turbulence,streamlines}.py` | 廓线；扇区选槽与 flat-gather；阵风锋面；TurbBox 与 DrydenBank；流线 | V0.1（L0/L1 + box）/ V0.3（L2） |
| `environment/weather/{presets,derive,transitions}.py`、`environment/atmosphere/{optics,isa}.py` | derive 与 eval_env；光学厚度与 Kim 模型；ISA | V0.1 |
| `environment/io/{awrv,awsl}.py` | 编码与解码 | V0.2 |
| `tools/wind/{build_l2_library.py, build_vis.py, gen_turb_box.py}` | 离线建库；vis 降采样；湍流盒 | V0.3 |
| `tools/contracts/gen_env_golden.py` | 生成 golden 并做一致性检查 | V0.1 |
| `apps/web/src/engine/environment/state/{conventions,derive,evalEnv,anchors,EnvStore}.ts` | 前端状态与求值 | V0.1 |
| `apps/web/src/engine/environment/wind/{awrv,awsl,windNode,windCPU,Streamlines,WindUniforms}.ts` | 前端风 | V0.2（流线）/ V0.3（纹理） |
| `apps/web/src/engine/environment/atmosphere/fog.ts` | 与 `optics.py` 同构的 fogOD | V0.1 |
| `sim/fleet/stages/env.py` | §6.2 | V0.1 |

参考实现现在就可以复制：`.cache/research/g06/env_ref.py`，其中包括 derive、方向换算、廓线、选槽、光学厚度和阵风形状。

---

## 12. 需要同步修订的既有结论

| 位置 | 原结论 | 修订 |
|---|---|---|
| 00-index §8.1 C9 | σ = 3.0/MOR 作为唯一换算 | 常数取 **ln 20**（写作约 3.0）；状态量改为 `mor_bg_m`，采用加性分解；3.912 只用于 Kim 模型的波长换算 |
| 00-index §3.8 | query 返回"湍流谱参数、MOR 与 σ、…" | 以 §6.1 的 `EnvSampleSoA`、`Frame`、`Fields`、`EnvFlags` 为准；湍流默认 box；前端纹理为 RGBA16F 预乘加实体占比 |
| 00-index §3.7 | 客户端做 τ≈0.3 s 阻尼，状态 5–20 Hz | 改为客户端同函数求值、关键帧加 1 Hz 心跳；阻尼只用于 `step` 模式的视觉平滑 |
| 00-index §3.11 | `env/weather` msgpack，`env/wind/field` typed-blob | 按 §8 的 channel 表 |
| r16 §3.1.1 WindSpec | `base/gust` 为 ENU 矢量，grid 的 w 通道为 σ_u，10–20 Hz 推送 | 这些字段降级为前端内部 uniform；线上主数据为 EnvKeyframe |
| r16 §3.3 与 §3.7.2 | `σ_fog0 = σ_total`，`σ_haze = 0.1σ_total` | 按 §4.1（雾层内地面处 σ 恰好等于 K/MOR，降水层单独计算） |
| r17 §3.3(b) | "dt=0.02 时 σ_v 偏高约 10%" | 这是样本误差；改为精确离散并从平稳分布起步（§5.5.3） |
| r17 §3.3(d) | `R(θ)ᵀ(x − U t)` 旋转采样坐标 | 改为平移坐标、旋转输出矢量（§5.5.2） |
| r17 §3.7 与原型 | manifest 的 `origin` 为 [0,0,0]；SF 库建在原始单位上 | 按 §7.2；SF 库作废，需要在 x01 规范化之后重建 |
| r27 §3.5 | `env/wind/field` 头部为 3 分量 f16 | 改为 AWRV v1（§7.3） |
| n03 §3.2 | 移植 RotorPy Dryden | 不照搬（σ 未标定，L 放大 2 倍）；用 §5.5.3 的实现 |
| n04 §3.3.0 | A 通道存 \|u\| 或 NaN/−1 | A 通道存实体占比，禁止 NaN；流线格式按 AWSL |

---

## 13. 本机验证记录

```text
$ PYTHONPATH=.cache/research/r17/pylib .venv/bin/python .cache/research/g06/g06_blend_rot.py
rotation-form vs r17 decomposition max|diff| = 1.07e-14
flow-aligned(rot) 270/300->285: mean 0.019 p95 0.060 m/s (ref 10 m/s)
antisym max|f90+f270| = 1.9e-06
$ … g06_query_opt.py        （SF 2 倍降采样 vis 网格重建全分辨率流体格速度，参考风速 10 m/s）
all fluid naive              err mean 0.13 p95 0.62 bias -0.11
all fluid rgb/max(1-a,0.5)   err mean 0.07 p95 0.19 bias +0.00
near-wall naive              err mean 1.63 p95 4.37 bias -1.31
near-wall rgb/max(1-a,0.5)   err mean 0.79 p95 4.97 bias +0.38
f16 fused N=1000: 2.24 ms/query (2 sectors fused + box)
$ … g06_query_flat.py
N=1000: flat-gather 1.409 ms, scipy map_coordinates x6 1.640 ms ; N=4000: 5.409 / 8.557 ms
$ .venv/bin/python .cache/research/g06/g06_dryden.py / g06_dryden2.py
w: h=50 V=8 dt=0.02: target σ=0.770  r17-ZOH 0.770 (-0.0%)  exact 0.767 (-0.4%)
L=3 V=15 T=0.20s dt=0.05 (dt/T=0.25): r17-ZOH 0.994 (-0.6%)  exact 1.000 (+0.0%)
RotorPy GustModelBase sigma=1: output std 0.0294 (dt=0.01) / 0.0820 (dt=0.05)
$ .venv/bin/python .cache/research/g06/env_ref.py && node .cache/research/g06/derive_check.mjs
（12 个预设 mor_bg 见 §4.2；Node 与 Python 的 derive 在 7 个降水/雾预设上 12 位小数一致）
optical_depth closed form vs midpoint numeric (200 rays): max rel err 1.8e-04
```
