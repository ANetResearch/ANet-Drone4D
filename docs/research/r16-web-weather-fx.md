# r16 研究笔记：Web 天气视觉（EnvironmentLayer 的雨 / 雪 / 沙尘 / 雾 / 云 / 湿地面 / 闪电）

> 研究单元：r16 ｜ 日期：2026-09-28 ｜ 对应设计：`docs/01-design.md` §12、§16（EnvironmentLayer）、§17–25（Environment Engine、E 场、风、雨、雾、沙、云）、§37（刷新频率）、§39（Timeline）、§46–47（V0.3 / V0.4）
> 仓库快照（均为 shallow clone，路径 `refs/weather/<repo>`，文中路径都相对仓库根目录）：
> - `natural-disasters` @ `d2bae38`（2026-08-31，273 stars，MIT，three r169 + WebGL2/GLSL3）
> - `Eanpa-Sky` @ `a197d3d`（2026-09-12，56 stars，MIT，内置 three r186，WebGPU + TSL）
> - `procedural-weather-threejs` @ `26ad580`（2026-02-08，11 stars，MIT，只有文档形式的 Claude skill，没有可运行代码）
> - `procedural-clouds` @ `1c9481c`（2026-02-08，6 stars，MIT，原生 WebGPU + WGSL）
>
> 本单元的结论都来自源码精读。另外在本机 headless Chromium（SwiftShader WebGL2）上做了一组微基准，脚本在 `/data/projs/anet-drone/.cache/research/r16/`。测试时机器负载很高（load average 13–20，8 核，同时有其他研究单元在跑），所以**绝对毫秒数不可信，只用来比较相对量级**。凡是估算都会标注"估算"。
> 本文和 r11（three.js WebGPU）是互补关系：r11 定了渲染器、分档和点云；本文把 EnvironmentLayer 细化到可以直接写代码的程度。两者冲突的地方在 §7 统一说明。

---

## 0. 结论速览

| 仓库 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|
| **natural-disasters**（ABYSSAL） | 纯程序化的海洋与极端天气，**WebGL2/GLSL3**。包括：无状态闭式雨（完全在顶点着色器里算）；float RT 乒乓的 GPGPU 飞沫；128³ Perlin-Worley 与 32³ 细节噪声的运行时烘焙；四通道天气图；体积云 1/16 Bayer 摊销加重投影；Catmull-Rom 上采样；5 档质量加闭环自适应（含 `potato` 软件渲染档）；实例化线段闪电；GPU 分项计时 | **port**：它是 WebGL2 / 软件档的主要算法来源，GLSL 要改写成 TSL | V0.3（雨、雾、2D 云、质量控制）→ V0.4（体积云 Med 档）| 5/5 |
| **Eanpa-Sky** | three r186 WebGPU/TSL 的天空与天气引擎，2026-09 仍在活跃开发。包括：**世界锚定的雨**加积分相位；雨向深度场（遮挡、落点、积水）；材质湿润包装；GPU 湿度历史；**确定性闪电节律**；云阴影图；快照插值过渡；按质量档的预设；懒加载；全景缓存云 | **port**：算法和工程约定。**reference**：整套引擎（只支持 WebGPU，资源 345 MB，着色器编译要几十秒）| V0.3（雨锚定、闪电、过渡）→ V0.5（湿地面、云阴影图、High 档）| 4/5 |
| **procedural-weather-threejs** | 文档形式的 skill，内容有：12 种天气状态参数表、转移矩阵与路由、生物群系预设、WGSL 粒子 compute 草稿、中点位移闪电、湿镜头和霜冻后处理 | **reference**：预设词汇、转移路由、UI 命名。代码有多处 bug（§2.3），不能照抄 | V0.3（预设表、路由）| 2/5 |
| **procedural-clouds** | 原生 WebGPU：compute 把 Blender 节点图（4D Perlin 加 4D Voronoi）写进 3D 密度缓存，再做全屏 ray march | **reference**：compute 写 3D 纹理加双缓冲的管线组织方式。**skip**：噪声本身，每个体素要评估约 650 个 Voronoi 格点，太贵 | V0.5（High 档云演化，作参考）| 2/5 |

**实现者先读这 12 条（每条都有源码或实测依据）：**

1. **雨在所有档位都用"无状态闭式粒子"。** 粒子位置是 `(粒子编号, 积分相位, 锚点)` 的纯函数，不需要 compute，不存状态，Timeline 可以任意 seek（`natural-disasters/src/weather/Precipitation.js` 的 `RAIN_VERT`，加上 `Eanpa-Sky/engine/weather_system.js` 的 world-tiling）。有状态粒子（compute 或 GPGPU）只用在需要真正跟随风场积分的**沙尘、雪漂移、风迹线**上。
2. **SwiftShader 上实例化绘制有严重的逐实例开销。** 同页配对测量：实例化雨 1000 个增加约 55 ms/帧，4000 个增加约 390 ms/帧（每实例 50–100 µs）；把同样的四边形展开成普通顶点缓冲、用 `vertexIndex / 6` 取粒子编号，5000 个只增加约 12–23 ms，快 15–25 倍（§3.10）。软件档每多一个全屏 pass 也要 15–20 ms。**所有粒子、闪电线段、风箭头统一用"扁平四边形展开"**（TSL 的 `vertexIndex`，WebGL2 后端编译成 `gl_VertexID`，WebGPU 编译成 `vertex_index`，两个后端都有）。在真实 GPU 上这种写法也不会比实例化慢。
3. **下落和风的位移必须在 CPU 上用 float64 积分成相位，不能写成 `time * speed`。** 速度插值时 `d(t·v)/dt = v + t·dv/dt` 会变号，雨会"倒吸"回天上（`Eanpa-Sky/engine/weather_system.js` 中 `update()` 的 `totalFallDistance / totalWindDistance`，以及 `engine/cloud_motion.js` 的梯形积分）。服务端下发 E 场时也带上这两个累积量，Timeline seek 才能精确复现（§3.1.4）。
4. **world-tiling 公式**：`p = anchor + (fract(h + (windOff − anchor)/P) − 0.5)·P`。雨幕固定在世界坐标里，相机穿行时雨不会跟着平移。`procedural-weather-threejs` 那种"把 mesh 挂到相机位置再做局部 mod"的写法是错的，雨会贴着镜头走。
5. **亚像素补偿**：宽度取 `max(真实粗细, 距离 × 每像素米数 × 1.15)`，不透明度乘以 `真实粗细 / 实际宽度`（`vThin`）。远处的雨会自然融成灰色雨幕，不会闪成噪点（natural-disasters `RAIN_VERT`，Eanpa 同理取 1.7 倍）。
6. **雾只有一个真值：MOR（米）→ 消光系数 σ。** 前端用**指数高度雾的解析积分**（§3.3，不要用 three 自带 `exponentialHeightFogFactor` 的平方近似）。Low/Med 档在点云材质里**逐顶点**计算雾，不增加全屏 pass；同一个 σ 交给服务端 Sensor 模块。
7. **云分三档，用同一张 2D 天气图驱动。** Low：天空穹顶上的平面云，每像素一次纹理采样，不做 ray march。Med：低分辨率缓冲取屏幕的 0.35 倍（每个轴），每帧只 march 其中 1/16 的像素（4×4 Bayer 摊销）、历史重投影、邻域钳制、Catmull-Rom 上采样，全部照 natural-disasters `src/sky/Clouds.js`。High：WebGPU 半分辨率逐帧 march，加光照缓存（Eanpa）。**云阴影在所有档位都开**，点云每个顶点在"太阳光线与云中层的交点"处查一次天气图，代价几乎为零，而且和可见的云严格对齐。
8. **3D 噪声离线预烘焙成 `.bin` 随 World Package 分发**：shape 96³ 或 128³ RGBA8（3.4 MB / 8 MB），detail 32³（128 KB），加上 2%/98% 分位数归一化参数。不要在浏览器里实时烘焙：natural-disasters 用 GPU 烘焙后再 `readRenderTargetPixels`，128³ Perlin-Worley 在软件渲染下预计要数秒（估算）；本机主线程 JS 生成 64³ 四个八度的值噪声就用了约 210 ms（实测）。
9. **天气状态的权威在服务端。** 服务端负责过渡，推送的是物理量（mm/h、m、m/s）；客户端只做 τ≈0.3 s 的临界阻尼，用来掩盖 2–5 Hz 更新造成的台阶。离线 mock 模式用同一份 TS 预设表和过渡函数（§3.7）。云型、降水、能见度、风是**相互独立的轴**，各自有时间常数（Eanpa `src/weathersky.js`、natural-disasters `Weather.js` 的 `rates` 表）。
10. **闪电节律是 `(seed, 事件序号)` 的纯函数**（Eanpa `LIGHTNING_CADENCE`、`lightningStrokePlanAt`），可以复现、可以审计、可以回放。几何用"12 锚点 + 4 次中点位移 + 2–4 条分支"，写进固定大小的 buffer（448 顶点）。闪光通过共享 uniform 同时照亮点云、云、天空和雾。
11. **UrbanScene3D 点云带法线**（实测 PLY 字段是 `x y z nx ny nz`，**没有颜色**，Z 轴朝上）。各城市的坐标框架并不一致：Shenzhen 约 1.8×2.0 km、最高 342 m；New York 约 2.9×3.2 km、最高 259 m；shanghai 约 7.7×6.2 km、最高约 625 m；Chicago 只有约 4×8 个单位、z 在 0–1 之间（疑似归一化坐标）；Suzhou 的 z 整体偏移约 −680。环境层所有以米为单位的参数，都依赖 World Package 先做单位归一化和地面基准。环境光照（日照 × 云阴影、天空环境光、湿润变暗、闪光、雾）可以直接作用在点上，这是本项目环境视觉"质感"的主要来源（§3.6、§3.11）。
12. **数字沙盘的俯视相机会让"以相机为中心的雨盒"失效**。相机在 500 m 高空时，雨只落在镜头附近的空中，地面上看不到。降水锚点要在"相机"和"轨道焦点"之间插值，盒子尺寸按八度离散分档并交叉淡化，远景再把 σ_rain 并入雾（§3.2.6）。

---

## 1. 仓库概览

| 项 | natural-disasters | Eanpa-Sky | procedural-weather-threejs | procedural-clouds |
|---|---|---|---|---|
| star / 最后提交 | 273 / 2026-08-31 | 56 / 2026-09-12 | 11 / 2026-02-08 | 6 / 2026-02-08 |
| 运行时 | three `^0.169`，`WebGLRenderer`，全部 `RawShaderMaterial` + `GLSL3` | 内置 three r186（`vendor/three`，带 8 处本地补丁，见 `vendor/three/EANPA_PATCHES.md`），`WebGPURenderer` + TSL，**只支持 WebGPU**（`src/main.js` 探测 `navigator.gpu`） | 文档，示例代码使用 `ShaderMaterial`、WGSL 片段 | 原生 WebGPU（`navigator.gpu`），不依赖 three |
| 规模 | `src/` 下 25 个 JS 文件共约 9600 行（GLSL 以模板字符串内嵌），**没有任何外部资源**（纹理全部运行时烘焙） | `src/` 与 `engine/` 约 3.2 万行 JS，仓库共 1194 个文件，默认演示要下载约 **345 MB** 资源（`KNOWN_ISSUES.md`） | 4 份 md，约 60 KB | `main.js` 445 行，`cloud.wgsl` 255 行，`noise.wgsl` 414 行 |
| 构建与测试 | Vite 8；`tools/smoke.mjs`、`tools/sb.mjs` 是 puppeteer 截图和计时工具（带 SwiftShader 参数） | 静态服务即可运行（`qa/dev-server.py`）；`node --test tests/*.test.mjs` 有 33 个单测；`qa/` 下有 GPU 基准和回归夹具 | 无 | Vite 6 + lil-gui + stats.js |
| 与本项目的关系 | WebGL2 回退和低端档的**首选算法来源** | WebGPU 高端档的**工程范本**，一些关键算法（锚定、积分相位、确定性闪电、湿地面）所有档位都要用 | 预设命名与转移路由 | compute 加 3D 缓存的管线写法 |
| 2026 活跃度 | 高（8 月仍有提交，CI 和 Pages 在跑） | 很高（9 月每天都有 review 报告，版本 0.2.1） | 低（2 月之后没有提交） | 低 |

---

## 2. 源码结构与关键模块

### 2.1 natural-disasters

| 文件 / 符号 | 作用与关键参数 | 对本项目的价值 |
|---|---|---|
| `src/core/Quality.js` · `PRESETS` | 5 档：`potato / low / medium / high / ultra`。`renderScale` 取 0.60→1.0；`cloudScale` 取 0.32→0.62；`cloudSteps` 取 34/48/66/96/148；`cloudLightSteps` 取 4→8；`rainCount` 取 9k/22k/48k/96k/180k；`envCloudSteps` 取 12→26 | 直接作为 EnvTier 的起点（§3.9） |
| `Quality.tick(dtMs)` | 闭环控制。窗口为 24 帧或 400 ms，二者先到者结束，至少 4 帧（`SAMPLE_FRAMES / SAMPLE_MS / MIN_FRAMES`）。**用中位数判定 panic**（超过预算 4 倍）后调用 `_shed`，一次跳过 `round(log2(ms/target))` 档；平均值超过预算 1.25 倍时 `dynamicScale −0.09`，低于 0.68 倍时 `+0.045`，冷却时间 0.9–4 s；`MIN_SCALE=0.5` | 和 r11 §3.4 的控制器合并。"时间窗口"和"中位数判定 panic"两点要照抄，否则 2 FPS 时一个 24 帧窗口就是 12 s |
| `autoDetectPreset()` | 根据 `UNMASKED_RENDERER` 字符串分档，`swiftshader / llvmpipe` 对应 `potato` | 与 r11 的 `Tier` 探测对齐 |
| `src/weather/Precipitation.js` · `Rain` / `RAIN_VERT` | 无状态雨（下文 §3.2.1 有移植后的伪代码）：`live = step(h.x, uRain*1.15)`；锚点沿视线前推 `box.x*0.45`；终速 `mix(4.2, 9.4, size)`；半径 `box.x*pow(h.y,1.7)` 使雨滴在屏幕上均匀；流光长度 `clamp(speed*0.042, 0.3, 3.2)` 模拟快门积分；亚像素补偿 `vThin`；`fbm2Tiled` 驱动的雨幕 curtain 门控；`instanceCount = budget·min(rain·1.2, 1)` 直接调节数量，零成本 | 雨的主体实现 |
| `RAIN_FRAG` | 把雨滴当透镜，按折射方向采样环境图，加上太阳 glint（`pow(dot,24)`）和闪电环境光；颜色预乘 | Med 以上档的雨色 |
| `Spray` / `SPRAY_SIM` | **float RT 乒乓 GPGPU**：两张 RGBA32F（`pos+age`、`vel+seed`）经 MRT 一个 pass 同时更新。拖曳系数 `mix(0.35, 2.6, seed)`；每个死粒子最多尝试 3 次重生；半径偏置 `pow(r.y,1.55)`；带 NaN 防护 | WebGL2 档的沙尘和风迹线（§3.2.3） |
| `src/sky/Clouds.js` · `CLOUD_COMMON` | `weatherAt()` 读天气图得到 `(覆盖, 云型, 基底抬升)`；`heightProfile(h, type)` 在层云、积云、积雨云与云砧之间插值；`cloudDensity()` 做 Schneider 膨胀 remap、用 curl 扭曲两层 detail 侵蚀，detail 随距离连续淡出 | Med/High 档云密度函数 |
| `CLOUD_MARCH` · `sampleLight / marchClouds` | `SIGMA=0.022`；光照 march 最多 8 步，步长每步乘 1.62；3 个多次散射八度（`a*=0.5, b*=0.42, c*=0.68`）；powder `1−exp(−14d)`；`dualHG(0.82, −0.32, 0.55)`；**双速步进**：大步 `stride=3·fine` 找云边界，找到后回退一步再细步积分，连续 4 个空样本后退回大步；细步 `nearFine=clamp(厚度·0.005, 22, 48) m`，随距离增长到 22 倍；2 个向上 tap 估计天空可见度 | 同上 |
| `CLOUD_FRAG` / `CLOUD_REPROJ_FRAG` / `CLOUD_UPSAMPLE_FRAG` | 每帧只 march 1/16 的低分辨率像素（4×4 Bayer 槽，每 16 帧整体旋转一次黄金比）；其余像素按历史深度重投影，3×3 邻域钳制（`tol=1.5·(hi−lo)+0.06`），`uBlend=0.4`；上采样用 4 tap Catmull-Rom，相机移动时逐渐混入 4×4 box 来消除摊销网格（`_reprojectionShift` 用屏幕中心加四角 5 个探针估计位移）| Med 档云的性能关键 |
| `src/gfx/ProceduralTextures.js` | `CLOUD_SHAPE_FRAG`：perlin fbm（频率 4，5 个八度）与 worley fbm（4/8/14/22）合成 `perlinWorley = w0 + perlin·(1−w0)`；`CLOUD_DETAIL_FRAG`：worley 3/6/11；`WEATHER_FRAG`：r=天气尺度覆盖，g=中尺度单体，b=云型，a=对流核心；`CURL_FRAG`；`atlasTo3D()`（2D 图集读回后组成 `Data3DTexture`，并算 `channelPercentiles` 2%/98%） | 离线烘焙脚本的算法来源（§3.4.1） |
| `src/weather/Weather.js` | `state / target` 两份连续参数，`damp(a,b,l,dt)=a+(b−a)(1−e^{−l·dt})`，每个参数有自己的 `rates`；阵风 `1+g·(0.5 sin + 0.3 sin(2.37…) + 0.2 sin(5.1…))`；蒲福风级表；云层风向相对地面风偏转 +0.35 rad（`windAngle + 0.35`），风速 `2.5+0.42·ws` | 状态机的连续插值层（§3.7） |
| `src/weather/Lightning.js` | 实例化线段四边形（core 层 + glow 层）；`_grow` 递归中点位移，深度 ≤5 或线段 <40 m 时停止，扰动每层乘 0.55，深度 <3 时以 0.42 概率分叉；回击 1–3 次，幅度 `0.62^i`，时长 35–125 ms；余辉 `exp(−7·age)·0.05`；片状闪电按 `rate·dt·0.9` 触发、按 `exp(−5.5·dt)` 衰减；最强的两次闪电写入 `uLightning0/1`，供海面、云、天空共同使用 | 闪电渲染与光照耦合 |
| `src/core/SharedUniforms.js` | 全局共享一个 uniform 对象图 `U`，材质按引用使用；`updateFrameUniforms()` 每帧更新一次 | TSL 里用 `uniform().setGroup(renderGroup)` 实现同样的效果 |
| `src/core/GpuProfiler.js` | 用 `EXT_disjoint_timer_query_webgl2` 做分区计时（结果延迟几帧），没有扩展时退回 `gl.finish()` 加 CPU 计时 | EnvBudget 分项计时（§3.9） |

### 2.2 Eanpa-Sky

| 文件 / 符号 | 作用与关键参数 |
|---|---|
| `engine/weather_system.js` · `WEATHER` | 8 种状态：`clear / sunshower / fair / overcast / rain / storm / cyclone / darkstorm`。每种状态包括云预设 `clouds` 与覆写 `over`、`sunDim / grey / dark / greyTint`、`rain / wet / windK / len / fall / dash / lightning`、`cellLo/cellHi`（雨区门控阈值）、`celestialVisibility`。`LEGACY_STATES` 负责旧名映射 |
| 雨（约 L370–480） | `N_RAIN=16000`、`RAD=45 m`、`HGT=24 m`；`hashI(n,k)` 是独立的整数 PCG 通道；**72% 的雨滴放在 RAD/3 的近层**；world-tiling；`fallPhase` 与 `fallPhaseSmall`（小雨滴速度 ×0.72）；流光轴沿速度方向；宽度 `max(直径, dist·pixelWorldScale·1.7)`，直径 `h2²·0.0035+0.002 m`；近处 0.9–2.6 m 淡出；圆形羽化替代方形盒边界；`countGate=smoothstep(pop, pop+0.001, rainK)`；**雨区门控 `rainCellAt`**：在上风向的"发射源"位置（`world.xz − wind.xz·(cloudBase−y)/fallSpeed`）采样云覆盖 |
| 溅落（约 L480–600） | 每个事件画 1 个接触面片和 6 颗弹道水珠，都在一个 draw 里；事件种子为 `instanceIndex + floor(clock)·K`，所以每次位置都不同；落点和法线来自 `surfaceField.impactAt/normalAt`；水珠按重力 9.81 做抛物线；亚像素补偿 |
| 闪电（`LIGHTNING_CADENCE`、`lightningEventGapAt`、`lightningStrokePlanAt`、`lightningFlashAt`、`lightningChannelAt`、`rebuildBolt`） | 标准节律：事件间隔 16–38 s，稀有度系数 `sqrt(ref/level)`，上限 2.75；每个事件最多 2 次回击，间隔 85–190 ms；场景闪光包络 `exp(−25·age)`，通道余辉 `exp(−6.5·age)`；用 `jsHash` 做确定性随机。几何为 12 个锚点、4 次中点位移（扰动取线段长度的 0.16），2–4 条分支，每条可有子分支（深度 ≤2）；顶点 buffer 固定为 448 顶点 / 1280 索引（**不在每次闪电时重建 attribute**，避免 WebGPU buffer 泄漏）；点光源峰值 12000，范围 900 m，本地落雷冷却 45 s |
| 湿润材质 `wrapMaterial` | 包装 Standard/Physical 材质：计算入射率 `dot(normalGeom, rainSourceDir)` 和暴露度 `surfaceField.visibilityAt`；湿度来自 `accumulation.sample` 的 `(薄湿, 积水)`；水坑形状用"域扭曲值噪声取中段阈值"，避免出现方格；多孔材质变暗 `porosity·0.5`（porosity 默认 0.65）；粗糙度向 0.045 混合；水的 F0 取 0.02037（IOR 1.333）；两层涟漪法线（只在 8–35 m 内开启）；可通过 `userData.noWet / noPuddles / wetnessFactor` 按对象控制 |
| `transitionTo / setWeather / _capture / _apply / _lerpDef` | **快照插值**：先 `_capture` 当前所有 uniform，再 `setWeather` 写入目标值并 `_capture`，然后恢复当前值；之后每帧用 `smoothstep(t/dur)` 在两份快照之间插值（默认 45 s）。`_lerpDef` 取两个状态字段的并集（缺失字段用默认值，避免 storm 特有字段在过渡开始时突变）|
| `update()` | 湿度 `τ` 上升 7 s、下降 70 s；积水目标 `wet^1.8`，`τ` 上升 32 s、下降 150 s；积分相位时 `dt` 钳制在 0.1 s 以内 |
| `engine/rain_surface_field.js` | 沿雨方向做一次**正交深度加法线**捕获（半径 ≥72 m，分辨率 768，8 Hz）；中心按纹素吸附，只有移出 20% 半径才重新居中；`visibilityAt` 比较的是"点到捕获表面平面的有符号距离"（避免斜面出现方格）；`impactAt` 做射线与平面求交得到溅落点 |
| `engine/rain_accumulation_field.js` | 世界对齐的湿度历史：两张 256² RGBA16F 乒乓，覆盖半径 1024 m，4 Hz 刷新；两次刷新之间用 `−expm1(−dt/τ)` 解析推进，避免 4 Hz 的台阶；重新居中时按整纹素偏移复制历史 |
| `engine/weather_listener.js` | 1×1 RT 加 `readRenderTargetPixelsAsync`，**不阻塞**地把 GPU 场读回 CPU（暴露度、雨区、雨强），用于音频和玩法 |
| `engine/sky_system.js` | 时间-天色调色板 `TOD`；云预设 `cumulus / stratus / cirrus / clear`；512² 的 1/f 天气图在 CPU 生成；`cloudsAt()` 两级 fbm 侵蚀（尺度 0.01 与 0.05）；`phaseMie` 为数值拟合的米氏相函数；`lightRay` 用 3 项 Beer 叠加模拟多次散射，加 powder；**分层多遍 march**（`M_PASS`）；Hillaire 能量守恒累积 `(L − L·T)/σ`；云下 20×6 步的光柱和雨幕 march（`precipK`）；128³ Uint8 密度基底缓存；compute 写的光照 froxel 缓存 |
| `engine/cloud_shadow_map.js` | 以相机为中心、6144 m 范围、384² 分辨率、10 Hz 刷新；在光照平面坐标系下积分每条光柱的透过率；两次捕获放在同一张图集里做时间混合 |
| `engine/quality_presets.js` | `high` 档：60 采样、18 光照步、5 遍、全分辨率，16000 雨滴；`balanced` 档：44 / 14 / 3 遍、半分辨率，10000 雨滴，溅落 700；`performance` 档：2048×1024 全景缓存云，9 s 刷新 |
| `src/weathersky.js` · `makeLazyWeatherAttachment` | 天气系统**懒加载**：第一次请求非晴天时才构建；"云型"和"天气"是**两条独立状态轴** |
| `qa/benchmarks/performance-balanced-20260912/README.md` | RTX 5090 Laptop、1080p、Balanced 档：应用 GPU 总计 2.59 ms，其中体积云 0.88 ms |
| `KNOWN_ISSUES.md` | 着色器编译和环境准备要**几十秒**；资源 345 MB；云阴影范围有限；地表水只是着色模型，不模拟径流 |

### 2.3 procedural-weather-threejs（只作参考；以下问题**不要照搬**）

结构：`SKILL.md` 中的 `WindSystem`、`RainSystem`、`SnowSystem`、`SplashSystem`、`LightningSystem`、`WeatherController`、`WEATHER_STATES`；`weather-shaders.md` 中的 GLSL（rain、snow、dust、ground fog、aurora、wet lens、frost）以及 WGSL `particle_update`；`weather-types.md` 中的 12 种状态、转移矩阵、`findTransitionPath`、`BIOME_WEATHER`。`procedural-weather.skill` 是上面这些文件的 zip 打包。

发现的问题：

1. `RainSystem._buildGeometry` 中 `randoms` 数组长度是 `count·2`，但顶点数是 `2·count`，`new BufferAttribute(randoms, 2, false, this.count)` 的第 4 个参数也用错了。结果**同一条雨线的上下两个端点拿到不同的种子和相位**，线段会被撕开。
2. `RAIN_VERT` 对每个顶点单独做 `mod` 回卷。雨滴回卷的那一帧，线段会横跨整个雨盒。
3. mesh 每帧移动到相机位置，然后在局部坐标里 `mod`。结果**雨跟着相机平移**，穿行时没有"穿过雨幕"的感觉。应该用 world-tiling。
4. WGSL 的 `%` 对负浮点数是截断取余，`((x+r) % 2r) − r` 在负方向回卷错误。
5. 强度门控是把粒子推到 `y=−1000`，而不是减少 `drawRange/instanceCount`，被隐藏的粒子仍然占着着色器开销。
6. 雨滴位置用 `time·speed` 计算，天气过渡时会反向运动（Eanpa 已经修正）。
7. `WeatherController.update` 使用 `performance.now()`，跟仿真时间没有关系，Timeline 无法回放。
8. `LightningSystem` 每次落雷都 new 几何体，在 WebGPU 下有 buffer 泄漏风险（Eanpa 的注释里有同样的教训）。

### 2.4 procedural-clouds

`main.js`：手写矩阵库、两条 pipeline（`cs` compute 和 `vs/fs` 全屏三角形）、`paramsBuffer` 共 96 B 分 6 个 vec4 打包、lil-gui 参数面板（`rayMarchSteps` 16–64、`lightMarchSteps` 1–8、`cacheResolution` 32–128，默认 96、`cacheUpdateRate` 1–4）。`shaders/cloud.wgsl`：`cloudDensity()` 把 Blender 的 5 级节点图原样翻译过来；`intersectBox` 求交；`lightMarch` 步长 0.15；HG 相函数（g=0.45）按 0.6 混合；IGN 抖动；Reinhard 加 gamma。`@compute @workgroup_size(8,8,4) fn cs` 把密度写进 `rgba16float` 的 3D 纹理。`shaders/noise.wgsl`：Jenkins lookup3 哈希、`perlin_noise_4d`（16 个角点）、`noise_fbm`、`hash_pcg4d_i`、`voronoi_f1`（3⁴=81 个邻格）、`fractal_voronoi_x_fx`。

问题与代价：

- 默认参数下，每个体素要算 Voronoi1 两个八度加 Voronoi2 六个八度，共约 `81×8≈650` 次格点哈希，另加 4D Perlin。96³ 的缓存每 2 帧更新一次，约 5.7 亿次格点计算，**核显和软件渲染完全吃不消，只适合离线烘焙**。
- 时间混合有 bug：采样器永远按 `mix(tex0, tex1, blend)` 混合，但最新写入的纹理在 0 和 1 之间交替，**每隔一次更新混合方向就反过来**；而且缓存总是"当前时刻"生成的，`blend` 几乎恒为 1，实际上没有插值。
- `rgba16float` 只用了 `.r` 通道，浪费 4 倍显存。

---

## 3. 可复用算法与实现（含伪代码和参数）

### 3.0 坐标约定与每帧共享的环境 uniform

- **服务端和数据使用 ENU**（x 东、y 北、z 上，单位 m）。UrbanScene3D 本身是 Z-up。**three 使用 Y-up**：`three = (e, u, −n)`。风矢量 `W_enu=(u,v,w)` 换到 three 是 `(u, w, −v)`。转换只在 `EnvStore` 入口做一次，着色器内部一律用 three 坐标。
- 风向按气象惯例用 **"来向" θ**（度，从北顺时针）：`u = −V·sin θ`，`v = −V·cos θ`。
- 所有环境效果共用一组 uniform `EnvUniforms`（TSL 中 `uniform(x).setGroup(renderGroup)`，每帧只上传一次，参考 natural-disasters `SharedUniforms.js` 与 Eanpa `T3.frameGroup` 的做法）：`simTime`、`camPos/camRight/camUp`、`pixelWorldScale = 2·tan(fov/2)/viewportHeight`、`sunDir/sunColor/celestialVis`、`ambSky/ambGround`、`windBase/windOffset/fallPhase4/fallSpeed4`、`rainK/snowK/dustK`、`sigmaHaze/sigmaFog/fogTop/fogColor`、`cloudCover/cloudBase/cloudTop/cloudWindOffset/weatherScale`、`wetness/puddle`、`flashPos/flashI/ambientFlash`。

### 3.1 统一风场接口 W(x,y,z,t)

#### 3.1.1 数据契约（服务端 → 前端）

```ts
// state/EnvTypes.ts —— 前后端共享（Python 侧用 pydantic 生成同构 schema）
export interface WindSpec {
  level: 0 | 1 | 2 | 3;                  // 对应设计文档 §20
  seed: number;                          // 确定性湍流、阵风
  base: [number, number, number];        // ENU m/s，10 m 参考高度处的平均风（不含阵风）；GPU 上的 windBase = base + gust
  gust: [number, number, number];        // ENU m/s，当前阵风分量（服务端 Dryden/OU 生成，10–20 Hz 推送）
  shear: { alpha: number; zRef: number }; // 幂律 V(z)=V_ref·(z/zRef)^alpha，城市 α≈0.3，开阔地 α≈0.14
  turb: { sigma: number; lengthScale: number }; // 视觉湍流：curl noise 的幅度（m/s）和尺度（m）
  grid?: WindGridRef;                    // Level 2/3：3D 栅格
  dispFall: number;                      // 累积下落位移 ∫v_fall dt（m）—— 回放用
  dispWind: [number, number, number];    // 累积水平风位移 ∫W_base dt（ENU m）—— 回放用
}
export interface WindGridRef {
  id: string; t0: number; t1: number;    // 两帧时间戳；前端按 t 在 A/B 之间混合
  originEnu: [number, number, number];   // 栅格最小角
  sizeM: [number, number, number];       // 覆盖范围（m）
  dims: [number, number, number];        // 例如 64×64×16（约 47 m×47 m×25 m 一个格子）
  encoding: 'rgba16f';                   // xyz=ENU 风速，w=湍流强度 σ_u
}
```

- 推送频率：`base/gust/disp*` 随 `env.state` 按 10–20 Hz 发送；`grid` 以二进制帧按 0.5–2 Hz 或变化时发送（64×64×16×8 B ≈ 0.5 MB，Level 2 只在变化时发）。Level 3（OpenFOAM VDB）由服务端重采样成同样的栅格格式，前端**不直接读 VDB**。
- 嵌套栅格（V0.5+）：全局粗网格（约 47 m）加上焦点附近的细网格（128×128×32，4 m，覆盖 512×512×128 m），采样时优先取细网格。

#### 3.1.2 GPU 采样（TSL，两个后端通用）

```ts
// wind/windNode.ts
export const windAt = Fn(([pW]) => {                 // pW: three 世界坐标 → 返回 three 空间 m/s
  const agl = max(pW.y.sub(E.groundRef), 1.0);        // groundRef：锚点处 DSM 高度（CPU 每帧更新）
  const shear = pow(agl.div(E.shearZRef), E.shearAlpha).clamp(0.3, 2.5);
  const base = E.windBase.mul(vec3(shear, 1.0, shear));      // 只放大水平分量
  const uvw = pW.sub(E.gridMin).div(E.gridSize).toVar();
  const inGrid = E.gridOn.mul(step(0, uvw.x).mul(step(uvw.x, 1)).mul(step(0, uvw.y)).mul(step(uvw.y, 1))
                 .mul(step(0, uvw.z)).mul(step(uvw.z, 1)));
  const g = mix(texture3D(E.gridA, uvw).xyz, texture3D(E.gridB, uvw).xyz, E.gridBlend);
  const w = mix(base, g, inGrid);
  const turb = curlNoise(pW.mul(E.turbFreq).add(E.turbPhase)).mul(E.turbSigma);  // jsm/tsl/math/curlNoise.js
  return w.add(turb);
});
```

- 格点纹理是 `Data3DTexture`（RGBA16F，`LinearFilter`）。格点边缘要淡入，否则粒子穿过边界时速度会跳变：`inGrid` 可以改用 `smoothstep` 在边缘 5% 内过渡。
- `gridBlend = clamp((t − t0)/(t1 − t0))`：两张纹理轮换，服务端推新帧时把 A 换成 B。
- `turbPhase` 在 CPU 上按 `turbPhase += windBase·dt·turbFreq` 积分（与 §3.1.4 同理），湍流会随风平移，不在原地抖动。
- 湍流的代价：three 的 `examples/jsm/tsl/math/curlNoise.js` 每次调用要算两次 `snoiseVec3`（共 6 次 simplex 噪声），逐粒子使用的开销不小。**只有 High 档的 compute 使用解析 curl**；Med 档改为查预烘焙的 curl 纹理（natural-disasters `CURL_FRAG`，256² RG8，平铺），用 `(xz, y)` 两次采样拼成 3D 扰动；Low 档不加湍流。

#### 3.1.3 CPU 镜像（无人机 mock、HUD、UI 箭头）

`WindField.sampleCPU(pEnu, t)` 使用同一套公式，但不含视觉湍流。阵风直接取服务端推送的 `gust`；Level 2/3 保留一份最近收到栅格的 Float32 拷贝，做三线性插值。**动力学仍以服务端为准**，前端的这个镜像只用于显示（风力箭头、`Wind Force` 可视化，见设计文档 §40）和离线 mock。

#### 3.1.4 积分相位（防止倒吸，保证回放精确）

```ts
// wind/WindIntegrator.ts（全部 float64）
update(dt: number) {                       // dt 钳制在 [0, 0.1]，Eanpa 同样处理
  const vFall = fallSpeed * fallMul;       // 按雨强插值后的"代表终速"
  this.fall += vFall * dt;                 // 服务端给出 dispFall 时直接用服务端的值（seek 精确）
  this.wx += windBase.x * dt; this.wz += windBase.z * dt;
  for (let c = 0; c < 4; c++)              // 4 个速度档：比例 [0.55, 0.75, 0.9, 1.0]
    U.fallPhase4[c] = (this.fall * RATIO[c]) % HGT_MAX;   // 在 float64 里先乘再取模，不会跳变
  U.fallSpeed4 = RATIO.map(r => r * vFall);
  U.windOffset.x = ((this.wx % P_MAX) + P_MAX) % P_MAX;   // P_MAX 必须是所有层周期的公倍数
  U.windOffset.z = ((this.wz % P_MAX) + P_MAX) % P_MAX;
}
```

**周期必须整除**：Eanpa 的近层周期是 `2·RAD/3`，远层是 `2·RAD`，`windOffset` 按远层周期取模，所以近层 `fract((off − cam)/period)` 仍然连续。我们的多档盒子（§3.2.6）半径采用 20/60/180/540 m 这样的 3 倍关系，`P_MAX` 取最大档的周期 1080 m；各档高度 20/48/120/320 m，`HGT_MAX` 取它们的最小公倍数 960 m。

#### 3.1.5 两类消费者

| 消费者 | 风的用法 |
|---|---|
| **无状态**（雨、Low 档雪和沙） | 全局漂移用 `windOffset`；局部偏差用 `(windAt(p) − windBase)·age`。`age` 是粒子在本次下落周期中的已下落时间 = `fract(...)·HGT/v`，回卷时归零。偏差只在盒顶和盒底的淡出区跳变，看不出来 |
| **有状态**（沙尘、雪漂、风迹线） | `v += (windAt(p) + v_settle − v)·(1 − e^{−dt/τ_p})`，`p += v·dt`。粒子响应时间 `τ_p ≈ v_t/g`：雨滴约 0.9 s，雪约 0.1 s，沙尘 0.01–0.05 s（基本跟风走）|

### 3.2 降水粒子

#### 3.2.1 雨（无状态，全部档位；TSL 伪代码）

```ts
// precip/RainStreaks.ts —— 扁平四边形：geometry 有 6N 个顶点，position 只存角点 (x∈±0.5, y∈[0,1])
const id = vertexIndex.div(6);                           // SwiftShader 下不要用 instanceIndex（§3.10）
const h = (k) => hashU(id, k);                           // PCG 整数哈希的独立通道（Eanpa hashI）
const near = h(5).lessThan(L.nearFrac);                  // 近层比例：Low 1.0，Med/High 0.72
const R = near.select(L.Rnear, L.Rfar), P = R.mul(2), H = L.hgt;
// 雨滴直径 D（mm）：Marshall–Palmer N(D)∝e^{−ΛD}，Λ=4.1·R_mmh^−0.21。按数量分布采样时绝大多数雨滴 <0.5 mm
// （R=6 时平均仅 0.36 mm），看起来像毛毛雨；所以按体积加权 D³·N(D) 采样，即 Gamma(4, Λ)：
// D = −ln(u1·u2·u3·u4)/Λ（R=6 时均值约 1.4 mm），截断在 [0.3, 5] mm
const D = clamp(h(4).mul(h(8)).mul(h(9)).mul(h(10)).max(1e-6).log().negate().div(E.mpLambda), 0.3, 5.0);
const vt = float(9.65).sub(exp(D.mul(-0.6)).mul(10.3));   // Atlas 1973 终速公式（m/s），D=2mm 时约 6.5，只用于流光长度和着色
// 下落相位按 4 个速度档在 CPU 上分别积分（Eanpa 用 2 档：大滴与 0.72 倍速小滴）。
// 不能用 fallPhase·(vt/vRef)：fallPhase 取模回卷时，乘以非整数比会产生跳变。
const cls = floor(h(7).mul(4));                           // 速度档 0..3，比例 [0.55, 0.75, 0.9, 1.0]
const phase = pickComponent(E.fallPhase4, cls);           // vec4 uniform，每个分量对 HGT_MAX 取模
const vCls = pickComponent(E.fallSpeed4, cls);
// —— world-tiling —— 所有位移都来自积分相位
const px = A.x.add(fract(h(1).add(E.windOffset.x.sub(A.x).div(P))).sub(0.5).mul(P));
const pz = A.z.add(fract(h(2).add(E.windOffset.z.sub(A.z).div(P))).sub(0.5).mul(P));
const fallN = fract(h(3).sub(phase.div(H)).sub(A.y.div(H)));
const py = A.y.add(fallN.sub(0.35).mul(H));
const age = fallN.oneMinus().mul(H).div(vCls);           // 本周期内已下落的时间
// 局部风偏差在粒子自己的位置采样：Low 档为 0（只有均匀风）；Med 只查风栅格；High 再加湍流
const drift = windDeviation(vec3(px, py, pz));           // = windAt(p) − windBase
const wp = vec3(px, py, pz).add(drift.mul(age));
// —— 流光 —— 沿速度方向拉伸，长度 = 速度 × 快门时间（0.042 s，与 natural-disasters 一致）
const vel = vec3(E.windBase.x.add(drift.x), vCls.negate(), E.windBase.z.add(drift.z));
const dir = normalize(vel), len = clamp(length(vel).mul(0.042), 0.3, 1.2);
const toEye = normalize(E.camPos.sub(wp)), dist = length(E.camPos.sub(wp));
const side = normalize(cross(dir, toEye));
const trueW = D.mul(0.001), wide = max(trueW, dist.mul(E.pixelWorldScale).mul(1.15));
const thin = clamp(trueW.div(wide), 0.1, 1);             // 亚像素补偿：加宽多少倍，不透明度就降多少倍
// —— 门控 —— 数量门控交给 drawRange；这里只做淡出和遮挡
const ground = dsmHeight(wp.xz);                         // World Package 自带的 DSM（R16F，1–2 m/px）
const aboveGround = step(ground, wp.y.sub(len));         // 屋顶以下不画
const edge = smoothstep(R, R.mul(0.7), length(wp.xz.sub(A.xz)));
const nearF = smoothstep(0.9, 2.6, dist);                // 0.9–2.6 m 淡出；Low 档直接退化成屏外（§3.10）
const cell = rainCellGate(wp);                           // Med/High：在上风发射源处查天气图覆盖（Eanpa rainCellAt）
material.positionNode = wp.add(dir.mul(positionLocal.y.sub(0.5).mul(len))).add(side.mul(positionLocal.x.mul(wide)));
material.opacityNode = profile(uv).mul(thin).mul(edge).mul(nearF).mul(aboveGround).mul(cell).mul(E.rainOpacity);
```

- **数量门控**：`geometry.setDrawRange(0, 6·N_live)`，`N_live = N_max·rainK`，`rainK = clamp(ln(1+R_mmh)/ln(51), 0, 1)`。粒子编号先随机打散，前缀就是均匀子集。改数量零上传、零重编译（与 r11 点云的 drawRange 做法一致）。
- **颜色**：Low 档用常量 `rainColor·(ambient + sunGlint)`；Med/High 档按 natural-disasters `RAIN_FRAG` 的做法，沿折射方向采样环境图。闪电时加 `ambientFlash·lightningColor`。混合模式用预乘 `One, OneMinusSrcAlpha`，关闭 depthWrite，开启 depthTest（这样会被点云和建筑遮挡）。
- **雨幕 curtain 门控（Med+）**：`curtain = smoothstep(0.30, 0.72, fbm2(xz·0.0055 − curtainOff) ·0.5 + 0.55 + rainK·0.35)`（natural-disasters 原式为 `wind·t·9`，这里同样改成 CPU 积分的偏移 `curtainOff`）。雨会成片地一阵一阵出现，这是"阵雨感"的主要来源。

#### 3.2.2 雪（无状态；扁平四边形或 glpoint）

- 与雨共用 tiling 框架。终速 `vt = mix(0.6, 1.5, size)`；飘动 `x += sin(t·(1.5+2s)+φ)·A`，`z += cos(t·(1+1.5s)+2φ)·0.7A`，`A` 从 2 m（小雪）降到 0.5 m（暴风雪，风压过飘动）。这些参数来自 `procedural-weather-threejs/weather-shaders.md` 的 `snow.vert`；`t` 要换成积分相位。
- 形状：在 WebGL2 后端可以用 r11 §3.2.3 的 `gl_PointSize` 补丁画圆点（1 个顶点一片雪，最省）；WebGPU 下点只有 1 px，要用扁平四边形。Low 档 3k 片，Med 15k，High 40k（High 可以改用有状态 compute 加堆积）。
- 暴风雪时流光拉长：`len = |v|·0.03`，雪片会被拉成短线。

#### 3.2.3 沙尘（有状态，Med/High；Low 退回无状态）

WebGL2 档（float RT 乒乓，照搬 natural-disasters `SPRAY_SIM` 的结构，在 WebGPURenderer 的 WebGL2 后端用 `RenderTarget(count:2, FloatType)` 加 `QuadMesh` 的 MRT 实现）：

```glsl
// 纹理 A：pos.xyz + age；纹理 B：vel.xyz + seed；边长 side=ceil(sqrt(N))，一个全屏 pass 更新两张
vec3 w = windAt(P.xyz);
float vt = mix(0.05, 0.6, seed);                          // 沉降速度（细尘到沙粒）
float tau = mix(0.01, 0.08, seed);
vec3 target = w + vec3(0.0, -vt, 0.0) + curlTex(P.xyz*0.02 + uTurbPhase) * uTurbSigma;   // Med 查 curl 纹理（§3.1.2）
V.xyz = mix(V.xyz, target, 1.0 - exp(-uDt / tau));
P.xyz += V.xyz * uDt;  P.w -= uDt;
bool dead = !(P.w > 0.0) || any(isnan(P.xyz)) || P.y < dsm(P.xz) || outsideBox(P.xyz - uAnchor);
if (dead) {                                               // 重生：70% 在上风面，30% 在近地"起沙层"
  vec3 r = hash33(vec3(vUv*512.0, uFrame*0.017));
  P.xyz = (r.z < 0.7) ? upwindFace(r) : vec3(boxXZ(r), dsm(boxXZ(r)) + r.y*uSaltH);
  P.w = mix(2.0, 6.0, r.x);  V.xyz = w;
}
```

绘制：扁平四边形，顶点着色器按 `vertexIndex/6` 算出纹理坐标，`texelFetch` 读 pos。尺寸 `max(0.05 m, dist·pixelWorldScale·1.5)`，同样做亚像素补偿。颜色 `(0.76,0.62,0.42)·(ambient + sun·0.6)`。**沙尘同时向雾里加 σ_dust 并染色**：远处的"沙尘感"主要来自这个雾染色，粒子只负责近景（§3.3）。

WebGPU 档（High，compute）：

```ts
const P = instancedArray(N, 'vec4'), V = instancedArray(N, 'vec4');     // 每粒子 32 B，10 万粒子 3.2 MB
const sim = Fn(() => {
  const p = P.element(instanceIndex).toVar(), v = V.element(instanceIndex).toVar();
  /* 同上：windAt、弛豫、积分、重生 */
  P.element(instanceIndex).assign(p); V.element(instanceIndex).assign(v);
})().compute(N, [64]);                                   // dispatch = ceil(N/64)
// 绘制：几何体 6N 顶点，positionNode 用 P.element(vertexIndex.div(6))（需要 requiredLimits.maxStorageBuffersInVertexStage ≥ 1，r11 §2.4）
```

注意：r11 实测 WebGL2 后端的 compute 走 Transform Feedback，要求 `instancedArray`，**绘制时就只能实例化**，而实例化在 SwiftShader 上很慢（§3.10）。所以 **software 档不开有状态粒子**。

#### 3.2.4 溅落（无状态事件，Med+）

照搬 Eanpa 的事件模型：`clock = simTime·rate_i + phase_i`；`cycle = floor(clock)`；`seed = id + cycle·1597334677u`；位置在近场方盒 `SP=min(9, R/3)` 内做 world-tiling；落点高度 `dsm(xz)`，法线用 DSM 中心差分得到。Med 档每个事件只画 1 个接触面片（时长约 30 ms，半径 `0.008 + 0.2·age`）；High 档再加 6 颗弹道水珠（`launch = 切向·0.1–0.6 + 法向·0.36–1.06`，`y −= 4.905·age²`）。数量：Med 300 个、High 1000 个（Eanpa balanced/high 档为 700/1100），与雨强一起按 `drawRange` 门控。

#### 3.2.5 compute 的必要性判断（避免过度设计）

| 效果 | 是否需要状态 | 理由 |
|---|---|---|
| 雨 | 否 | 终速大，下落 24 m 只要约 3 s，风场的空间变化用"粒子位置处的风偏差 × age"近似就够了；无状态还能完美回放 |
| 雪 | Low/Med 否，High 可选 | 飘动可以用解析公式；High 档想要"绕楼回旋"和屋顶堆积时，才值得上 compute |
| 沙尘、风迹线 | 是 | 视觉效果本身就是"跟着风场积分走"；可视化风场是它存在的意义 |
| 溅落、闪电 | 否 | 是事件，用哈希加时间的纯函数即可 |

#### 3.2.6 沙盘俯视相机的降水锚点（本项目特有）

问题：相机离地（AGL）500 m 俯视城市时，以相机为中心、24 m 高的雨盒全在空中，地面完全看不到雨；把盒子放大又会让 world-tiling 的周期连续变化，雨滴会滑动。

```ts
// precip/PrecipAnchor.ts
const LEVELS = [ {R:20,H:20}, {R:60,H:48}, {R:180,H:120}, {R:540,H:320} ];  // 周期 3 倍递增，彼此整除
function update(cam, focus, dsm) {
  const agl = cam.y - dsm.heightAt(cam.x, cam.z);
  const s = Math.max(agl, 0.35 * cam.distanceTo(focus));       // "观察尺度"
  const lvl = pickLevelWithHysteresis(s, LEVELS, 0.8, 1.25);    // 带滞回
  const k = smoothstep(80, 400, agl);                           // 离地越高，锚点越靠近焦点
  anchor.lerpVectors(cam, focus, k);
  anchor.y = lerp(cam.y, dsm.heightAt(focus) + LEVELS[lvl].H * 0.35, k);
  if (lvl !== cur) crossfade(cur → lvl, 0.6 s);                 // 两个 mesh 同时绘制，不透明度交叉；粒子预算按 60/40 分配
}
```

- 粒子总数不变，盒子越大，粒子越稀。亚像素补偿会让远层自然变成"雨幕灰"，正是俯视视角下应有的观感。
- 离地超过 150 m 时，把 `σ_rain(R)` 并入雾（§3.3）。Med+ 档在天空着色器中加"云下雨幕"（Eanpa `sky_system.js` 的 below-cloud march，20×6 步降为 12 步且不做光照 march：`ρ_precip = precipK·cellGate·colMod·2.1e−4`）。
- FPV 和跟随相机强制使用第 0 档，锚点就是相机本身。

### 3.3 雾（高度雾、指数雾、体积雾）

**单一真值**：服务端 E 场给出 MOR（米，ICAO 5% 对比阈值）和可选的分量。`σ_total = 3.0 / MOR`。设计文档和 r11 用的是 `3.912/V`（2% 阈值），**需要统一口径**，本文建议用 MOR 的 3.0（航空和气象通用）。服务端分量占位公式（**待标定**）：`σ_rain = 2.6e−4·R^0.63`（m⁻¹，R 单位 mm/h，大致对应 R=25 时能见度 1.5 km、R=50 时 1 km），`σ_snow = 1.0e−3·S^0.8`（S 为水当量 mm/h），`σ_dust` 由沙尘浓度直接给出。前端只消费 `σ_haze`（均匀部分）、`σ_fog0 / fogTop / fogScale`（高度相关部分）和 `fogColor`。

**指数高度雾的解析积分**（Quilez 形式，外加一层均匀霾；已在本机用 20 万段数值积分对 4 组射线核对，结果一致到 1e−6）：沿线段 `ro → ro + rd·L` 有 `σ(h) = σ0·e^{−(h−h0)/H} + σ_haze`，

```glsl
float fogOD(vec3 ro, vec3 rd, float L) {
  float a = uSigma0 * exp(-(ro.y - uH0) / uHs);
  float k = rd.y * L / uHs;                                   // Δh/H
  float f = abs(k) > 1e-4 ? (1.0 - exp(-k)) / k : 1.0;        // k→0 时的极限
  return a * L * f + uSigmaHaze * L;
}
// 平顶辐射雾（fogTop 以下为常数 σf）：只算线段落在 fogTop 以下的那一段
float groundFogOD(vec3 ro, vec3 rd, float L) {
  float t0 = 0.0, t1 = L;
  if (abs(rd.y) > 1e-5) { float tc = (uFogTop - ro.y) / rd.y; if (rd.y > 0.0) t1 = min(t1, tc); else t0 = max(t0, tc); }
  else if (ro.y > uFogTop) return 0.0;
  return uSigmaFog * max(t1 - t0, 0.0);
}
vec3 applyFog(vec3 c, vec3 ro, vec3 rd, float L) {
  float T = exp(-(fogOD(ro, rd, L) + groundFogOD(ro, rd, L)));
  float sunAmt = pow(max(dot(rd, uSunDir), 0.0), 8.0) * uCelestialVis;
  vec3 inscat = mix(uFogColor, uSunColor, sunAmt * 0.5) + uLightningColor * uAmbientFlash * 0.25;
  return c * T + inscat * (1.0 - T);
}
```

- **雾色必须等于天空地平线颜色**（procedural-weather "Common Pitfalls 4"；Eanpa `applyToLights` 用地平线调色板给 `fog.color` 着色），否则远处会出现一条硬边。天空着色器对 `rd` 用同一个 `applyFog`，取 `L = fogFar`（例如 20 km）。
- **在哪里算**：Low 档在点云材质中**逐顶点**计算 `T` 和 `inscat`，作为 varying 传下去（点云本来就是逐顶点着色，零额外 pass）；Med 档逐像素计算，网格与点云共用 `fogNode`；High 档另加 froxel 体积雾（V1.0，160×90×64，compute 注入云阴影和无人机灯光），这一项不进 MVP。
- 雾色随天气变化：雨雪时取地平线色的去饱和灰蓝；沙尘时按 `dustK` 混向 `(0.76,0.62,0.42)`；霾时取暖灰。闪电时 `inscat` 加一个闪光项，雾里会"亮一下"，这是风暴场景里最有冲击力的细节，而且几乎没有代价。
- 地面雾（valley fog）的**噪声飘带**：procedural-weather 的 `ground_fog.frag` 是一个水平面片加 fbm，在俯视沙盘下看起来像"贴纸"，不推荐。改为在 `groundFogOD` 中用 `fbm2(xz·0.01 − wind·t)` 调制 `uFogTop ± 15 m`，让雾顶高度起伏。

### 3.4 体积云

#### 3.4.1 离线资产（World Package / 通用资源包）

| 资产 | 规格 | 生成方式（Python，照 natural-disasters `ProceduralTextures.js` 的算法） |
|---|---|---|
| `cloud_shape_{96,128}.bin` | RGBA8：r = Perlin-Worley（`w0 + perlin·(1−w0)`，perlin 频率 4、5 个八度，w0 取 worley 4），g/b/a = worley 8/14/22；可平铺；另附 `percentiles.json`（2%/98%） | numpy 向量化，128³ 离线约数十秒（估算） |
| `cloud_detail_32.bin` | RGBA8：worley 3/6/11 与均值 | 同上 |
| `weather_1024.png` | RGBA8：r 天气尺度覆盖（带状脊状 fbm）、g 中尺度单体（worley+fbm）、b 云型、a 对流核心 | 同上；也可以由服务端 E 场的"云覆盖栅格"替换 r 通道 |
| `curl_256.png` | rg = curl，b/a = fbm | 同上 |

前端加载时把 `percentiles` 作为 `uShapeLo/Hi` 传入，着色器中 `clamp((tex − lo)/(hi − lo))`。这一步让阈值参数在不同噪声配方之间保持可调，natural-disasters 的注释对此有详细说明。

#### 3.4.2 密度函数（Med/High 共用，TSL 改写 natural-disasters `cloudDensity`）

```
wm = weatherAt(p.xz + cloudWindOffset·(0.6 + 1.5h))         // 高层风更快，塔顶顺风倾斜；原式为 cloudWind·t，这里改成积分偏移
cover = clamp((field(wm) − 0.5)·contrast + uCover) · smoothstep(0, 0.05, uCover)
type  = clamp(0.30 + 0.20·wm.b + anvil·(0.34 + 0.55·wm.b + 0.6·wm.a))
hs    = h − lift(wm)                                          // 云底随系统起伏，不是一个平面
shape = shapeTex(uvw + warp)                                  // warp 来自 detail，打破 128³ 晶格
base  = remap(shape.r, fbmLow·0.92 − 1, 1, 0, 1) · heightProfile(hs, type)
d     = remap(base, mix(0.99, 0.20, cover^0.67), 1, 0, 1);  d = d²(3 − 2d)
d     = erode(d, detail(curl-warped), bite = mix(0.78, 0.14, smoothstep(0.2, 0.78, d)))   // detail 权重随距离淡出
return d · uDensity
```

本项目的尺度：无人机作业高度 ≤150 m，UrbanScene3D 的楼高大多在 350 m 以内，但 shanghai 场景最高约 625 m。预设里的云底（§3.7）是相对地面的名义值，每个 World 再按 `cloudBaseMin = max(500, P99.9 楼高 + 150) m` 抬高，保证沙盘视角下云不会切过楼群；如果要模拟"低云吞没超高层"，在 World 配置里显式关闭这条规则。云层厚度 300–8000 m（积雨云）。天气图一个周期 `weatherScale = 20–40 km`，shape 一个周期 `cloudScaleM = 3–7 km`。

#### 3.4.3 march 与光照

- **包围几何**：城市范围只有几公里，不需要地球曲率。用水平平板 `[cloudBase, cloudTop]` 求交即可（natural-disasters 的球壳写法可以简化）。在沙盘视角下，相机常常位于云层**上方或内部**，见下一条。
- **沙盘视角裁切（本项目特有）**：相机高于 `cloudBase − 50 m` 时，自动进入 **cutaway 模式**：只 march `rd.y > 0` 的射线（向上看的背景天空），俯视城市时不画体积云，只保留云阴影；此外提供一个可切换的"云场图层"，把云覆盖画成半透明等值面或热力切片（lieflat 色板），作为数据可视化，而不是写实效果。否则俯视时城市会被整层云盖住。
- **步进**：双速步进（大步 `stride = 3·fine`，发现密度后回退，再用细步；连续 4 个空样本退出），细步 `clamp(厚度·0.005, 22, 48) m`，随距离放大；步数预算用完前逐步拉长步长，让射线平滑淡出（natural-disasters `marchClouds`）。
- **光照**：`sampleLight` 做 `LSTEPS` 次指数增长步长的 tap（×1.62），只采基础八度；3 个多次散射八度；powder 取局部密度；`dualHG(0.82·c, −0.32·c, 0.55)`；环境光按高度在 `ambBottom`（侧向天空加地面反弹）和 `ambTop` 之间插值，再乘以"上方云层可见度"（2 个向上 tap）。累积用 Hillaire 的能量守恒形式：`S += T·(1 − tr)·L`，`T *= tr`，`tr = exp(−σ·d·Δt)`。
- **Med 档摊销**（natural-disasters 三个 pass）：
  1. `march`：分辨率为 `(W·s/4) × (H·s/4)`，其中 `s = 0.35`（Med），每个片元代表 4×4 块中的第 `bayer[frame%16]` 个像素；射线起点抖动取 `fract(bayer4(pix) + 0.618·floor(frame/16))`（TSL 版 Bayer 抖动矩阵可以直接用 three 的 `examples/jsm/tsl/math/Bayer.js`）。
  2. `reproject`：低分辨率历史按"历史云深度"重投影（没有深度时用云层中高度 ×6）；越界时从当前帧双线性取样；3×3 邻域钳制后，新鲜像素 `mix(hist, cur, 0.4)`。
  3. `upsample`：Catmull-Rom 4 tap；相机移动量（5 探针估计）越大，越多混入 4×4 box（开启快、释放慢：0.55 对 0.045）。
  4. 合成：`sky·a + rgb`（预乘），只作用在天空像素（点云没有覆盖的像素），然后再叠加雾。
- **High 档**（WebGPU）：半分辨率逐帧 march（Eanpa `cloudDiv=2`，44 步、14 光照步、3 遍分层），加上 compute 生成的光照 froxel 缓存（Eanpa `lightCache` 112×28×112，0.22 s 刷新）。

#### 3.4.4 Low 档：2D 云（CloudLayer2D）

天空着色器中，射线与 `cloudBase` 平面求交，交点 `xz` 查天气图，得到 `cover`；再用 `fbm2(xz/cloudScale)` 雕细节，`alpha = smoothstep(0.4, 0.8, cover·shapeN)·(1 − 地平线衰减)`；光照 `mix(暗底, 亮顶, N·L 近似)`，其中法线由天气图的中心差分得到。每个天空像素 1–3 次纹理采样。这与 Eanpa 的 wisp/cirrus 层是同一思路。

#### 3.4.5 云阴影（全档位）

```glsl
float cloudShadow(vec3 p) {                      // 点云和网格逐顶点调用
  float hMid = uCloudBase + 0.35 * (uCloudTop - uCloudBase);
  vec2 xz = p.xz + uSunDir.xz / max(uSunDir.y, 0.15) * (hMid - p.y);
  float cov = coverage(texture(uWeatherMap, (xz + uCloudWindOffset) / uWeatherScale));
  return mix(1.0, exp(-cov * uCloudOD), uShadowStrength);   // uCloudOD ≈ 厚度·SIGMA·平均密度，钳制在 6 以内
}
```

这和可见云使用同一张天气图、同一个 `cloudWindOffset`，**阴影和云严格对齐**（natural-disasters 的海面云影就是这么做的）。High 档可以换成 Eanpa 的 `cloud_shadow_map.js`（对真实体积积分，10 Hz，384²，覆盖 6144 m），但 MVP 不需要。

### 3.5 天空

| 档位 | 天空 | 说明 |
|---|---|---|
| Low | 解析渐变（天顶色、地平线色、日盘加光晕），参数由 Eanpa 的 `TOD` 调色板按太阳高度插值，再按天气做去饱和（`grey`、`dark`） | 每像素只有几十条 ALU 指令 |
| Med | three 自带的 `SkyMesh`（TSL 版 Preetham），加天气调暗 | r11 §3.7 |
| High | Hillaire LUT（transmittance、multi-scatter、sky-view），参考 natural-disasters `src/sky/Atmosphere.js` | V0.5+ |

太阳、月亮的方向和颜色**同时驱动点云光照**（§3.11）。阴天通过 `celestialVis = 1 − 0.9·cover^1.5` 降低直射光，同时提高环境光占比（Eanpa `sunDim/hemiDim` 的做法）。

### 3.6 湿地面（点云与网格）

**动力学**放在服务端（与 Sensor、落地摩擦等物理量共用），前端只显示；离线 mock 时由前端执行同样的公式：

```
wetTarget   = clamp(rainK·1.2)·rainCellGate   （阴天湿度加一个下限 0.15）
wetness    += (wetTarget − wetness)·(1 − e^{−dt/τ})，τ = 7 s（变湿）/ 70 s（变干）         —— Eanpa update()
waterTarget = wetTarget^1.8
puddle     += (waterTarget − puddle)·(1 − e^{−dt/τ})，τ = 32 s（积水）/ 150 s（消退）
```

**点云着色**（点有法线 `n`，three 空间下 `n.y` 朝上）：

```glsl
float up = smoothstep(0.3, 0.9, n.y);
float exposed = step(dsm(p.xz) - 0.6, p.y);                      // 顶面点暴露；立面和檐下的点不暴露
float windward = clamp(dot(n, -normalize(rainDir)), 0.0, 1.0);    // 迎风立面会被斜雨打湿
float wet = uWetness * max(up * exposed, 0.5 * windward);
col *= 1.0 - wet * 0.65 * 0.5;                                    // 多孔材质变暗，Eanpa porosity=0.65
float flat_ = smoothstep(0.985, 0.998, n.y) * exposed;
float cavity = warpedValueNoise(p.xz);                            // 域扭曲值噪声取中段阈值（Eanpa），不会形成方格水坑
float pud = smoothstep(fill, fill + 0.055, cavity) * flat_ * uPuddle * (1.0 - smoothstep(160.0, 450.0, dist));
spec = mix(spec, 1.0, pud) ; rough = mix(rough, 0.045, max(wet * 0.5, pud));   // 高光：Blinn-Phong 近似，F0 取 0.02
```

- `fill = mix(0.79, 0.56, puddle)`（Eanpa）。点云没有真正的反射，Med 档用"天空色 × Fresnel(`n`, 视线)"在 `pud` 区域做一个假反射；High 档可以接 SSR（r11 后处理）。
- 网格（无人机模型、地形 mesh）直接用 Eanpa 的 `wrapMaterial` 思路，TSL 覆写 `colorNode / roughnessNode / metalnessNode`，**分批包装**（每帧只包装几个材质，避免一次性重编译卡几十秒，这是 Eanpa 在 `wrapScene({budget})` 注释里记录的教训）。
- **暴露度**：用 DSM 近似 Eanpa 的"雨向深度捕获"。点云只有 1 px，运行时捕获的深度会有大量空洞，r11 也得出同样结论。有风时查询点沿风倾斜：`dsm(p.xz − wind.xz/vt·(dsm − p.y))`。
- **DSM 实测**（本机对 UrbanScene3D 500 万点采样版逐格取最大 z，脚本 `/data/projs/anet-drone/.cache/research/r16/dsm_probe.py`）：

  | 城市 | 分辨率 | 栅格 | R16F 大小 | 空洞比例 | 3 次膨胀后仍空 | 暴露点占比（容差 0.6 m） |
  |---|---|---|---|---|---|---|
  | Shenzhen | 1 m | 1849×2000 | 7.4 MB | 45.5% | 0.0% | 62.5% |
  | Shenzhen | 2 m | 925×1000 | 1.9 MB | 9.5% | 0.0% | 53.0% |
  | New York | 1 m | 2929×3167 | 18.6 MB | 74.4% | 5.6% | 52.6% |
  | New York | 2 m | 1465×1584 | 4.6 MB | 37.3% | 5.1% | 41.8% |

  结论：采样点云的密度约 1 点/m²，**1 m DSM 空洞太多，默认用 2 m/px**（2–5 MB）。空洞只能用"有效邻居的中位数"迭代填补；如果用最大值膨胀，建筑轮廓会向外扩出几米，屋檐外的地面会被误判为"有遮挡"。剩余的大块空洞（城市边界外）填成地面分位数（P5）。DSM 由 World Package 离线生成，与点云八叉树一起发布。

### 3.7 天气状态机与过渡插值

#### 3.7.1 预设表（物理单位，前后端共享 JSON）

| 预设 | 云量 | 云型 0层云–1积雨云 | 云底/云顶 m | 雨 mm/h | 雪 mm/h(水当量) | MOR m | 地面雾顶 m | 沙尘 | 风速 m/s | 阵风 σ | 闪电 次/min |
|---|---|---|---|---|---|---|---|---|---|---|---|
| clear | 0.05 | 0.5 | 1500/2500 | 0 | 0 | 30000 | – | 0 | 3 | 0.5 | 0 |
| partlyCloudy | 0.35 | 0.5 | 1200/2600 | 0 | 0 | 20000 | – | 0 | 5 | 1.0 | 0 |
| overcast | 0.90 | 0.1 | 700/1500 | 0 | 0 | 12000 | – | 0 | 6 | 1.2 | 0 |
| lightRain | 0.90 | 0.2 | 600/1800 | 1.5 | 0 | 6000 | – | 0 | 5 | 1.2 | 0 |
| rain | 0.95 | 0.3 | 500/2500 | 6 | 0 | 3000 | – | 0 | 8 | 2.0 | 0 |
| heavyRain | 1.00 | 0.5 | 450/4000 | 25 | 0 | 1200 | – | 0 | 11 | 3.0 | 0.5 |
| thunderstorm | 1.00 | 1.0 | 800/9000 | 45 | 0 | 800 | – | 0 | 14 | 5.0 | 6 |
| fog | 0.50 | 0.0 | 300/700 | 0 | 0 | 150 | 60 | 0 | 1.5 | 0.3 | 0 |
| haze | 0.20 | 0.4 | 1500/2500 | 0 | 0 | 3000 | – | 0.15 | 2 | 0.4 | 0 |
| snow | 0.95 | 0.2 | 500/2000 | 0 | 2 | 1500 | – | 0 | 5 | 1.5 | 0 |
| blizzard | 1.00 | 0.3 | 400/2500 | 0 | 5 | 150 | – | 0 | 18 | 5.0 | 0 |
| sandstorm | 0.30 | 0.4 | 2000/3500 | 0 | 0 | 400 | – | 1.0 | 16 | 5.0 | 0 |

雨强分级参考 AMS：小雨 <2.5，中雨 2.5–7.6，大雨 >7.6 mm/h。procedural-weather 的 `aurora`（极光）和 `rainbow`（彩虹）与业务无关，不纳入。`haze`（城市霾）对光学和 LiDAR 很重要，所以新增。

#### 3.7.2 派生量（`state/derive.ts`，纯函数，前后端各实现一份并做对拍测试）

```
rainK   = clamp(ln(1+R)/ln(51)) · smoothstep(0.6, 0.85, cover)    // 云量不够时不下雨（过渡中途不会晴天落雨）
snowK   = clamp(ln(1+10S)/ln(51)) · smoothstep(0.6, 0.85, cover)
σ_total = 3.0 / MOR ；σ_fog0 = (fogTop>0) ? σ_total : 0 ；σ_haze = (fogTop>0) ? 0.1·σ_total : σ_total
sunVis  = (1 − 0.9·cover^1.5) · (1 − 0.6·dust)
mpLambda= 4.1 · max(R, 0.1)^−0.21                                  // Marshall–Palmer，雨滴直径分布参数
cloudOD = clamp((top − base)·0.022·0.35, 0, 6)                      // 云阴影光学厚度
```

#### 3.7.3 过渡

- **权威在服务端**：`POST /env/preset {name, durationS}`，由服务端按 `smoothstep` 在两份快照之间插值（Eanpa `transitionTo` 的快照法，字段取并集），保证 Sensor 和动力学看到的是同一个值。`env.state` 按 5 Hz 推送。
- **前端显示层**：`display = damp(display, latestServer, λ=1/0.3s)`，只用来掩盖推送间隔造成的台阶，**不再做第二次长时间过渡**，避免与服务端的过渡叠加成两重平滑。
- **各轴独立推进**（离线 mock 与服务端共用同一实现），分两种驱动方式：
  - 预设切换：每个轴都用 smoothstep，但在总时长内的起止区间不同。云 `[0, 0.7]`，降水 `[0.3, 1.0]`，能见度 `[0.2, 1.0]`，风 `[0, 0.8]`。进入降水状态时，先看到云层变厚，再开始下雨；湿度按 §3.6 的 τ 独立积分，地面随后慢慢变湿。离开降水状态时反过来：降水 `[0, 0.6]`，云 `[0.3, 1.0]`。
  - 连续驱动（脚本化 E 场、接入真实气象数据）：每个轴做指数阻尼，λ 取 natural-disasters `rates` 表乘 4 后的值：云量 0.9/s，雨 1.6/s，风 1.1/s，能见度 1.2/s。
- **路由**：直接切换不合理的组合按路由表经过中间态，改编自 `weather-types.md` 的 `findTransitionPath`：

```ts
const ROUTES: Record<string, Preset[]> = {
  'thunderstorm→clear': ['rain', 'overcast', 'partlyCloudy', 'clear'],
  'clear→thunderstorm': ['partlyCloudy', 'overcast', 'rain', 'heavyRain', 'thunderstorm'],
  'blizzard→clear':     ['snow', 'overcast', 'clear'],
  'sandstorm→rain':     ['haze', 'overcast', 'rain'],
  'fog→thunderstorm':   ['overcast', 'rain', 'thunderstorm'],
};  // 其余组合直接过渡；总时长按段数平均分配
```

- **确定性**：状态机只读仿真时间（`simTime`），不读 `performance.now()`。Timeline seek 时由服务端回放 E 场，前端 `damp` 直接吸附（`reset=true`）。

### 3.8 闪电

- **节律**（纯函数，前后端一致）：第 i 个事件的间隔 `gap_i = clamp(−ln(1 − hash(seed, i))·60/rate, 2, 120) s`，其中 rate 单位为次/min（泊松过程；Eanpa 用的是区间均匀分布，16–38 s 乘稀有度系数，这里改成按速率参数化，以便与服务端 E 场的闪电频度对应）；回击次数 `1 + [hash > 0.72] + [hash > 0.93]`，回击间隔 75–190 ms（Eanpa `LIGHTNING_CADENCE`）；每次回击幅度 `0.78–1.0`，后续回击 `0.42–0.8`。
- **包络**：场景闪光 `Σ exp(−25·age_k)·amp_k`（驱动点云、雾和天空）；通道余辉 `Σ exp(−6.5·age_k)·amp_k`（只驱动闪电条带本身，持续约 0.5 s，肉眼能看清）；片状闪电（云内闪）`ambientFlash`，按 `rate·dt·0.9` 的概率触发，按 `exp(−5.5·dt)` 衰减（natural-disasters）。
- **落点**：在当前雷暴单体范围内，按 `DSM 高度^2` 加权抽样，偏向高楼顶。这既更真实，也和无人机避雷风险评估（V1.0 的 Agent 感知）相关。服务端生成 `env.event {type:'lightning', t, posEnu, peak}`，前端也可以按 seed 本地复现。
- **几何**（Eanpa `rebuildBolt`）：从云底 ×1.04 到落点，取 12 个锚点，扰动 `34·(1 − 0.35t)` 并以 0.22 的比例拉回目标；4 次中点位移，扰动取线段长度的 0.16；2–4 条分支，每条 `7 − 2·depth` 个点，60% 概率有子分支（`rng > 0.4`）。条带宽度 `max(w, dist·pixelWorldScale·2.8)`，保证远处也至少有几个像素宽。写入固定大小的 buffer（448 顶点 / 1280 索引），用 `setDrawRange` 控制，**不要**每次 new attribute。
- **渲染**：扁平四边形条带，core 用 `pow(prof,18)·1.3`，corona 用 `pow(prof,2)·0.18`（Eanpa）；Low 档只画 core，最多 1 条；Med 档加 glow，最多 2 条；High 档最多 3 条，加云内辉光（`lightningGlow(p) = color·I·6e6/max(d², 4e4)`，natural-disasters）。加法混合，关闭 depthWrite。
- **光照耦合**：`flashPos/flashI` 写入 EnvUniforms，点云着色加 `flashColor·I·max(dot(n, L), 0)/(1 + d²/r0²)`，其中 `r0 = 400 m`；雾和天空加 `ambientFlash`。**不要**用 three 的 `PointLight` 给点云打光（点云材质不走 three 的光照管线，而且 WebGPU 的光源拓扑变化会触发重编译）。

### 3.9 质量档位与性能预算

档位映射：`software → Low`，`webgl2 → Med`（核显 WebGL2 也从 Low 起步），`gpu-high → High`，另有 `Off`。与 r11 §3.1 的 Tier 一一对应，之后由闭环控制器在线调整。

| 效果 | Low（软件渲染 / 核显 WebGL2） | Med（WebGL2 独显 / 核显 WebGPU） | High（WebGPU 独显） |
|---|---|---|---|
| 雨 | 无状态，扁平四边形，`N_max` 3k（software）/ 8k（核显），单层 R=20 m，近处 3 m 以内直接剔除，无 curtain，远景只靠 σ_rain | 20k，双层（72% 近层），curtain 门控，DSM 遮挡，环境图着色 | 60k，加雨区门控和云下雨幕 |
| 溅落 | 关 | 300 个接触面片 | 1000 个，每个加 6 颗水珠 |
| 雪 | 3k（glpoint 或扁平） | 15k | 40k（可选 compute 加堆积） |
| 沙尘 | 2k（software）/ 3k 无状态，σ_dust 雾染色 | 16k float RT 乒乓 GPGPU，吃风场 | 100k compute，加 curl 与起沙层 |
| 风可视化 | 箭头网格 24×24（扁平四边形） | 8k GPGPU 迹线，加箭头 | 64k compute 迹线，带尾迹 |
| 雾 | 解析高度雾，点云逐顶点 | 逐像素，加地面雾顶起伏 | 加 froxel 体积雾（V1.0） |
| 云 | CloudLayer2D，每天空像素 1–3 次采样 | 体积云，`s=0.35`，1/16 摊销，24–32 步，光照 2–3 步 | 半分辨率逐帧，48–64 步，光照 5 步，加光照缓存 |
| 云阴影 | 天气图解析（逐顶点） | 同左 | 同左，或 10 Hz 阴影图 |
| 天空 | 解析渐变 | SkyMesh（Preetham） | Hillaire LUT |
| 湿地面 | 只做变暗 | 加水坑和假反射 | 加涟漪法线和 SSR |
| 闪电 | 1 条，只画 core | 2 条，加 glow | 3 条，加云内辉光和落点冲击 |
| 镜头效果（只在 FPV 下） | 关 | 湿镜头 | 湿镜头加霜冻 |
| **环境总预算（GPU ms，设计目标，估算）** | **≤1.5 ms**（核显 1080p·0.75）；软件档要求环境部分不超过帧时间的 20% | **≤3.0 ms**（GTX 1650 级，1080p） | **≤5.0 ms**（RTX 3060 级，1440p） |

预算依据：natural-disasters 在 GTX 1650、1600×900、high 档下整帧（含 FFT 海洋、96 步云、TAA 和后处理）为 9.3 ms（晴）到 13.8 ms（风暴）；Eanpa 在 RTX 5090 Laptop 1080p Balanced 档，体积云 0.88 ms，全部应用 GPU 工作 2.59 ms。本项目帧时间的大头是点云（r11 按档位给出了 6 万到 300 万点的预算），所以环境部分应限制在帧时间的 20–30% 以内（设计取值）。

**降级链**（接入 r11 §3.4 的 `QualityController`，每一步都是即时生效、零重编译的操作优先）：

1. 溅落水珠 → 只留接触面片 → 关；
2. 降水 `drawRange` ×0.5（即时，零重编译）；
3. 云 march 步数 48→32→24，光照步数 4→2（uniform 驱动的循环上限，需要着色器支持动态上界）；
4. 云分辨率 `s` 0.5→0.35→0.25（重建 RT）；
5. VolumeClouds → CloudLayer2D（切换对象，预先编译好两套）；
6. 沙尘和迹线：有状态 → 无状态；
7. 闪电 glow 关闭；雾从逐像素改为逐顶点。

恢复需要 5 s 滞回，并且要求 `avg < 0.68·target` 连续两个窗口。panic 判定用中位数（natural-disasters `Quality.tick`）。**所有档位的着色器在加载页预编译**（`renderer.compileAsync`；Eanpa 的 `pipelineWarmupObjects()` 会把隐藏的对象临时设为可见再编译，因为 `compileAsync` 会跳过不可见对象），避免第一次打雷或第一次下雨时卡顿。

### 3.10 本机微基准（SwiftShader WebGL2，960×540，只看相对量级）

脚本：`/data/projs/anet-drone/.cache/research/r16/www/bench.html`、`run.mjs`、`suite3.mjs`（原始输出 `suite3.out`）。使用经典 `WebGLRenderer` 加 GLSL3，每帧以 `readPixels(1×1)` 同步。机器负载很高，所以采用**同页配对测量**：效果开、关逐帧交替各 20 帧，取"开 − 关"的中位数作为该效果的增量；整套跑 3 轮，报告 3 轮增量的中位数，括号内为最小值。场景是 41 个盒子加地面，相机在 40 m 高度。

| 用例 | 增量 ms/帧：3 轮中位数（最小值） | 说明 |
|---|---|---|
| 雨，**实例化**四边形 1000 个 | 55（52） | 每实例约 50 µs |
| 雨，**实例化**四边形 4000 个 | 391（269） | 每实例约 70–100 µs |
| 雨，**扁平四边形** 5000 个，3 m 内剔除 | 23（12） | 同等数量下比实例化快 15–25 倍 |
| 雨，扁平 20000 个，3 m 内剔除 | 71（57） | |
| 雨，扁平 20000 个，不剔除 | 85（77） | 近处剔除约省 15%（近处的雨滴在屏幕上占很大面积） |
| 雨，扁平 50000 个，3 m 内剔除 | 368（202） | 软件渲染不可用 |
| 雪，`gl.POINTS` 5000 个（gl_PointSize） | 19（2） | 点精灵每个粒子 1 个顶点，比四边形便宜 |
| 雪，`gl.POINTS` 20000 个 | 52（43） | 比同数量的扁平四边形雨便宜约 25% |
| GPGPU 沙尘 4096 个（float RT 模拟 + 扁平绘制，风场用 32×8×32 的 3D 纹理） | 21（11） | |
| GPGPU 沙尘 16384 个 | 85（61） | |
| GPGPU 沙尘 4096 个，**实例化**绘制 | 495（300） | 实例化再次成为瓶颈 |
| 高度雾：场景渲到 RT + 1 个全屏雾 pass | 20（18） | **每多一个全屏 pass 约 15–20 ms** |
| 体积云：240×136 低分辨率全量 march（24 步，光照 2 步）+ 雾 + 合成 | 61（43） | 每帧 32640 条射线 |
| 体积云：1/16 Bayer 摊销（24 步，光照 2 步） | 41（32） | 每帧 2040 条射线；代价主要来自额外的全屏 pass |
| 体积云：1/16 Bayer 摊销（48 步，光照 4 步） | 45（41） | 摊销后步数翻倍只多约 4 ms |

结论（只用于相对比较；SwiftShader 在这台机器上的空场景约 30–60 ms/帧）：

1. **实例化在 SwiftShader 上逐实例开销约 50–100 µs**，是最大的坑。扁平四边形同样数量下快 15–25 倍。这条结论决定了本文所有粒子都用 `vertexIndex` 展开。
2. **软件档每多一个全屏 pass 要 15–20 ms**。所以 Low 档雾在点云材质里逐顶点计算，云用天空穹顶上的 2D 云，不使用任何低分辨率 RT 链。摊销后的 march 本身很便宜，贵的是 RT 和全屏合成。
3. software 档的粒子上限：雨 3k（扁平）、雪 3k（点精灵）、沙尘 2k（无状态），合计增量控制在约 15 ms 以内，大约是 r11 实测软件档基线帧（约 60 ms）的 25%。在真实 GPU 上，这些粒子数的代价可以忽略（natural-disasters 在 GTX 1650 上跑 96k 雨滴、80k 飞沫仍能保持 72–108 FPS）。
4. 1/16 摊销让步数几乎"免费"：Med 档可以把步数给到 32–48，光照步数给到 3–4，而不必担心线性增长。

### 3.11 点云的环境光照契约（EnvLighting → PointMaterial）

UrbanScene3D 点云只有 `xyz + normal`，没有颜色，所以"世界的样子"几乎完全由环境光照决定。点云材质（r11 §3.2）需要实现以下统一函数，EnvironmentLayer 只负责提供 uniform：

```glsl
vec3 shadePoint(vec3 p, vec3 n, vec3 albedo /* 按高度或语义着色 */) {
  float sh  = cloudShadow(p);                                   // §3.4.5
  vec3  sun = uSunColor * uCelestialVis * sh * max(dot(n, uSunDir), 0.0);
  vec3  amb = mix(uAmbGround, uAmbSky, n.y * 0.5 + 0.5);         // 半球环境光
  vec3  fl  = uFlashColor * uFlashI * max(dot(n, normalize(uFlashPos - p)), 0.0)
              / (1.0 + dot(uFlashPos - p, uFlashPos - p) / (400.0 * 400.0));
  vec3  c   = albedo * (sun + amb + fl + uAmbientFlash * 0.1);
  c = applyWetness(c, p, n);                                    // §3.6
  return applyFog(c, uCamPos, normalize(p - uCamPos), length(p - uCamPos));   // §3.3，Low 档改为逐顶点
}
```

这样雨天会"变暗、变灰、远处被雨幕吞没"，雷暴时整座城市"闪一下"，云影在城市上缓慢掠过。这几项是环境视觉的**主要感知来源**，粒子只是点缀，而它们的代价都是逐顶点几条指令。

### 3.12 验收与回归测试（可在本机 headless CI 跑）

| 测试 | 做法 | 来源 |
|---|---|---|
| 确定性截图 | URL 参数 `?simTime=123.4&paused=1&preset=thunderstorm&seed=7`，按固定仿真时间渲染后截图；与基准图比较平均绝对差（阈值按档位分别设定） | natural-disasters `tools/sb.mjs` 与 `?paused=1`；`tools/diff.mjs` |
| 回放一致性 | 同一 `simTime` 先正向播放、再 seek 回来，两次截图必须一致（雨、溅落、闪电都是纯函数） | 本文 §3.1.4、§3.8 |
| 前后端对拍 | `derive.ts` 与 `derive.py`、`LightningSchedule.ts` 与 `lightning.py` 在 1000 组随机输入上输出一致（相对误差 < 1e−6） | Eanpa `EANPA_LIGHTNING_PROFILE` 暴露的纯函数，可以直接审计 |
| 过渡无突变 | 预设两两过渡时，逐帧记录 `rainK / σ / cover`，检查一阶差分有上界，不出现反向运动（`fallPhase` 单调） | Eanpa `diagnostics.transition` / `diagnostics.motion` |
| 相对性能 | 同一页面内效果开关交替计时（§3.10 的做法），断言"开启效果的增量 ≤ 帧时间的 20%"，不断言绝对 FPS | 本文 §3.10；r11 §3.11 |
| 着色器预热 | 首次切换到 `thunderstorm` 的那一帧，帧时间 ≤ 2 倍中位数（证明闪电和雨的管线已经预编译） | Eanpa `pipelineWarmupObjects()` |

---

## 4. 在本项目中的落点与复用方式

### 4.1 EnvironmentLayer 模块拆分（前端 `apps/web/src/environment/`）

```
environment/
├── EnvironmentLayer.ts       # THREE.Group 根节点；懒加载（第一次出现非晴天时才构建重型子系统）、换档、update(frame)
├── state/
│   ├── EnvTypes.ts           # §3.1.1 数据契约（与后端 pydantic 同构）
│   ├── EnvStore.ts           # zustand：服务端目标态、显示态（damp）、离线 mock 覆写
│   ├── presets.json          # §3.7.1（前后端共享）
│   ├── transitions.ts        # 快照 smoothstep、轴独立 damp、路由
│   └── derive.ts             # §3.7.2 物理量 → 视觉标量（纯函数，与 Python 对拍）
├── uniforms/EnvUniforms.ts   # §3.0 全局共享 uniform 组（renderGroup）
├── wind/
│   ├── WindField.ts          # GPU 资源（3D 纹理 A/B）+ CPU 镜像 sampleCPU
│   ├── windNode.ts           # TSL windAt()
│   ├── WindIntegrator.ts     # float64 积分相位（fall / wind / turb / cloud）
│   └── WindViz.ts            # 箭头网格 / GPGPU / compute 迹线
├── precip/
│   ├── PrecipAnchor.ts       # §3.2.6 锚点、八度档、交叉淡化
│   ├── flatQuads.ts          # 扁平四边形几何工厂（6N 顶点 + drawRange）
│   ├── RainStreaks.ts  SnowFlakes.ts  Splashes.ts
│   ├── DustParticles.ts      # 按后端选 compute / float RT / 无状态
│   └── nodes/                # hashU（PCG）、tiling、pixelFloor、dsmHeight、rainCellGate
├── atmosphere/
│   ├── SkyDome.ts            # 按档选 解析 / SkyMesh / LUT
│   ├── HeightFog.ts          # §3.3 fogOD/groundFogOD/applyFog（TSL Fn，点云、网格、天空共用）
│   └── RainVeil.ts           # 云下雨幕（Med+）
├── clouds/
│   ├── WeatherMap.ts         # 天气图纹理 + 覆盖函数（云、阴影、雨区门控共用）
│   ├── CloudLayer2D.ts       # Low
│   ├── VolumeClouds.ts       # Med（march/reproject/upsample 三个 pass）/ High
│   ├── CloudShadow.ts        # §3.4.5
│   └── noiseAssets.ts        # 加载 .bin 3D 噪声与分位数
├── surface/
│   ├── Dsm.ts                # DSM 纹理 + CPU 查询（锚点、溅落、闪电落点）
│   └── Wetness.ts            # 点云与网格的湿润注入；网格材质分批包装
├── lightning/
│   ├── LightningSchedule.ts  # §3.8 纯函数节律（与 Python 一致）
│   ├── BoltGeometry.ts       # 固定 buffer 预算
│   └── LightningFx.ts        # 条带渲染 + 闪光 uniform
├── lighting/EnvLighting.ts   # §3.11 汇总太阳、环境光、闪光、湿度，供点云材质使用
├── quality/
│   ├── envTiers.ts           # §3.9 档位表
│   └── EnvBudget.ts          # 分项计时（timestamp query / CPU），降级链
└── debug/EnvInspector.tsx    # shadcn 面板：各项开关、σ、粒子数、耗时（lieflat 图表）
```

后端对应（`services/sim/environment/`）：`field.py`（E 场、`wind_at`、MOR/σ 合成、湿度动力学）、`presets.py`（读取同一个 `presets.json`）、`transitions.py`、`lightning.py`（与 TS 版做对拍测试）、`publish.py`（`env.state` 5–20 Hz，`env.wind.grid` 二进制，`env.event`）。

### 4.2 复用清单

| # | 可复用项 | 来源 | 落点模块 | 版本 | 方式 | 理由 |
|---|---|---|---|---|---|---|
| 1 | 无状态雨（hash、回卷、流光、亚像素补偿、curtain） | natural-disasters `Precipitation.js` `RAIN_VERT/FRAG` | `precip/RainStreaks.ts` | V0.3 | port（GLSL→TSL） | 所有档位都能用，零 CPU、可回放 |
| 2 | world-tiling、近层 72%、积分相位、圆形羽化 | Eanpa `weather_system.js` 雨段与 `update()` | 同上 + `wind/WindIntegrator.ts` | V0.3 | port | 解决雨贴镜头和倒吸 |
| 3 | 雨区门控（上风发射源处的云覆盖） | Eanpa `rainCellAt` | `precip/nodes/rainCellGate` | V0.4 | port | 云、雨、湿度在空间上一致 |
| 4 | float RT 乒乓 GPGPU 粒子 | natural-disasters `Spray` | `precip/DustParticles.ts`、`wind/WindViz.ts` | V0.3 | port | WebGL2 档的有状态粒子 |
| 5 | 云密度、光照、双速 march | natural-disasters `Clouds.js` `CLOUD_COMMON/MARCH` | `clouds/VolumeClouds.ts` | V0.4 | port | 效果成熟，WebGL2 可用 |
| 6 | 1/16 Bayer 摊销、重投影、Catmull-Rom 上采样 | 同上 `CLOUD_FRAG/REPROJ/UPSAMPLE` | 同上 | V0.4 | port | Med 档性能关键 |
| 7 | Perlin-Worley / detail / 天气图 / curl 的配方 | natural-disasters `ProceduralTextures.js` | `tools/bake_env_noise.py` → `.bin` | V0.3 | port（改成 Python 离线） | 避免运行时烘焙 |
| 8 | 5 档质量与闭环控制（时间窗口、中位数 panic） | natural-disasters `Quality.js` | `quality/*` + r11 控制器 | V0.3 | port | 流畅性硬要求 |
| 9 | GPU 分区计时 | natural-disasters `GpuProfiler.js` | `quality/EnvBudget.ts` | V0.3 | port（WebGPU 后端用 `trackTimestamp`） | 预算可观测 |
| 10 | 连续参数阻尼、各参数 rates、阵风、蒲福风级 | natural-disasters `Weather.js` | `state/transitions.ts`、UI 风级显示 | V0.3 | port | 状态机的连续层 |
| 11 | 快照插值、字段并集、懒加载、两条独立轴 | Eanpa `transitionTo/_lerpDef`、`weathersky.js` | `state/*`、`EnvironmentLayer.ts` | V0.3 | port | 过渡不突变，启动快 |
| 12 | 确定性闪电节律与几何（固定 buffer） | Eanpa `LIGHTNING_CADENCE`、`rebuildBolt` | `lightning/*` | V0.3 | port | 可复现、不泄漏 |
| 13 | 闪电回击包络、片状闪电、光照耦合 | natural-disasters `Lightning.js` | `lightning/LightningFx.ts` | V0.3 | port | 效果好、代价低 |
| 14 | 湿度与积水动力学（τ 7/70、32/150，`wet^1.8`） | Eanpa `update()`、`rain_accumulation_field.js` | 后端 `field.py` + 前端 `surface/Wetness.ts` | V0.4 | port | 与 Sensor/物理共用 |
| 15 | 湿润材质（变暗、粗糙度、F0、水坑噪声） | Eanpa `wrapMaterial` | `surface/Wetness.ts` | V0.4 | port（点云版改写） | 雨天质感 |
| 16 | 溅落事件模型 | Eanpa 溅落段 | `precip/Splashes.ts` | V0.4 | port | 地面锚定感 |
| 17 | 云阴影图（体积积分，10 Hz） | Eanpa `cloud_shadow_map.js` | `clouds/CloudShadow.ts`（High） | V0.5 | reference | MVP 用解析阴影足够 |
| 18 | 雨向深度捕获 | Eanpa `rain_surface_field.js` | — | — | reference（用 DSM 替代） | 1 px 点云捕获会有空洞 |
| 19 | 非阻塞 GPU 读回 | Eanpa `weather_listener.js` | `EnvBudget` 与调试探针 | V0.4 | reference | 需要读 GPU 场时使用 |
| 20 | 云下光柱与雨幕 march | Eanpa `sky_system.js` L1525–1600 | `atmosphere/RainVeil.ts` | V0.4 | port（简化为 12 步） | 远景雨幕 |
| 21 | 全景缓存云（performance 档） | Eanpa `cached_cloud_display.js` | — | V1.0 | reference | 需 60 MiB 显存，实现复杂度高 |
| 22 | 12 种状态、转移矩阵、路由、生物群系 | procedural-weather `weather-types.md` | `state/presets.json`、路由表 | V0.3 | reference | 命名与路由 |
| 23 | 湿镜头、霜冻后处理 | procedural-weather `weather-shaders.md` | FPV 相机后处理 | V0.4 | port | 只用于 FPV 和 Sensor 视图 |
| 24 | compute 写 3D 缓存 + 双缓冲 | procedural-clouds `main.js`/`cs` | High 档云演化 | V0.5 | reference（修正混合 bug） | 管线写法 |
| 25 | Blender 4D Voronoi 噪声 | procedural-clouds `noise.wgsl` | — | — | skip | 太贵 |

### 4.3 版本落点

- **V0.3（Environment 视觉）**：EnvStore、presets、derive、WindIntegrator、雨/雪（无状态）、沙尘（Low 无状态，Med GPGPU）、HeightFog（逐顶点加逐像素）、CloudLayer2D、云阴影、SkyDome（Low/Med）、闪电、EnvLighting、envTiers 与降级链。离线 mock 用前端状态机；服务端先实现 Level 0/1 风。
- **V0.4（Physical Environment）**：服务端成为权威（`env.state` 推送物理量与累积位移）、σ 与 Sensor 共用、湿度动力学搬到服务端、VolumeClouds Med 档、溅落、雨区门控、RainVeil、风栅格（Level 2）与 WindViz、FPV 镜头效果。
- **V0.5–V0.6**：High 档（compute 沙尘和雪、光照缓存、云阴影图、Hillaire LUT）、嵌套风栅格。
- **V1.0**：froxel 体积雾、多视角（多无人机 FPV）的环境一致性、落雷风险接入 Agent 感知。

### 4.4 与 UI 的接口（设计文档 §38 左侧 ENVIRONMENT 面板）

- 面板上显示的是 `EnvStore.display` 中的**物理量**：风 `8.2 m/s · 来向 NW · 蒲福 5 级`（风级表来自 natural-disasters `BEAUFORT`）；雨 `22 mm/h`；**能见度 `MOR 1.2 km`**，替代原文中无单位的 `Fog 0.21`；沙尘；云量 `35%` 与云底高度。数值变化用 transitions.dev 的数字滚动动效，不要瞬间跳变。
- 预设选择用 shadcn `ToggleGroup`（12 个预设），图标用 morphicons 驱动的 Lucide 数据（`Sun / CloudSun / Cloud / CloudDrizzle / CloudRain / CloudRainWind / CloudLightning / CloudFog / Haze / CloudSnow / Snowflake / Wind`），切换预设时图标之间做形变动画，**禁止 emoji**。高级参数用 shadcn `Slider`，并标注单位；过渡时长用 `Select`（即时 / 10 s / 45 s / 120 s）。
- 质量档位与环境预算放在调试面板 `EnvInspector`：各效果的开关（shadcn `Switch`）、当前档位、粒子数、分项 GPU 耗时。时序图（风速、MOR、帧时间）按 lieflat-charts 的视觉语言绘制。
- 所有面板只读写 `EnvStore`，不直接修改 uniform。在线模式下，面板写操作发往服务端（`POST /env/preset`），不在前端本地生效，保证"看到的就是仿真用的"。

---

## 5. 对比与推荐

| 维度 | natural-disasters | Eanpa-Sky | procedural-weather-threejs | procedural-clouds |
|---|---|---|---|---|
| 后端兼容 | WebGL2（与 r11 的回退路径一致，但要改写成 TSL） | 只支持 WebGPU | 两者都声称支持，但代码不完整 | 只支持 WebGPU |
| 软件渲染与低端机 | 有 `potato` 档和闭环控制 | 没有低端档（5090 上测试） | 无 | 无 |
| 算法成熟度 | 高（注释写明了每个参数为什么这样取，并有截图回归工具） | 很高（每个问题都有 review 报告和 GPU 夹具） | 低（有 bug） | 中（有 bug） |
| 与点云城市的契合度 | 高（全是程序化，没有资源依赖） | 中（很多依赖 PBR 网格与 SSR；雨向捕获对点云不适用） | 中（参数表可用） | 低 |
| 可回放性 | 雨是无状态的，但状态机用 `Math.random` | 雨和闪电都是确定性的 | 否 | 否 |
| 2026 活跃度与 star | 273 stars，8 月仍更新 | 56 stars，9 月每天更新 | 11 stars，停更 | 6 stars，停更 |

**推荐排序**：① natural-disasters（WebGL2 与低端档的主体，算法直接移植）→ ② Eanpa-Sky（锚定、积分、确定性、湿地面、过渡这些"工程正确性"的范本，High 档的参考）→ ③ procedural-weather-threejs（预设命名、路由）→ ④ procedural-clouds（只参考 compute 3D 缓存的写法）。

**实现策略**：只维护一套 TSL 代码（遵循 r11 结论）。GLSL 算法按函数逐个改写为 TSL `Fn`，并加上 `setLayout`（Eanpa `sky_noise.js` 的经验：不加 layout 会被内联展开，着色器膨胀到约 0.5 MB）。粒子统一用扁平四边形。compute 只在 High 档出现。

---

## 6. 风险与注意事项

| 风险 | 表现 | 对策 |
|---|---|---|
| **WebGPURenderer 不支持 `ShaderMaterial/RawShaderMaterial`** | natural-disasters 的 GLSL 无法直接使用（`StandardNodeLibrary` 只映射标准材质） | 按函数改写成 TSL；复杂的纯函数（噪声、相函数）用 `Fn().setLayout()` 生成独立的 WGSL/GLSL 函数 |
| **SwiftShader 实例化逐实例开销** | 4000 个实例增加约 390 ms/帧，扁平四边形 5000 个只增加 12–23 ms（§3.10） | 扁平四边形 + `vertexIndex`；software 档不开有状态粒子 |
| WebGL2 后端的 compute（TF）要求 `instancedArray`，绘制只能实例化 | 在软件渲染上很慢 | Med 档用 float RT 乒乓（绘制可以是扁平的）；High 档才用 compute |
| **着色器编译卡顿** | Eanpa 实测要几十秒；材质包装会让整批管线重编译 | 在加载页 `compileAsync` 预热所有档位（隐藏对象需要临时设为可见）；湿润包装分批进行；首次打雷前预热闪电管线 |
| 3D 噪声运行时烘焙 | 在软件渲染上预计要数秒（估算）；本机主线程生成 64³ 值噪声约 210 ms（实测） | 离线 `.bin`；最低 64³ 作为 CPU 兜底，放 Worker 里生成 |
| 云体积在俯视时遮挡城市 | 沙盘视角下被整层云盖住 | cutaway 模式（§3.4.3） |
| 雨盒随相机变化导致"滑动" | 连续改变周期 P 会改变 `fract` 相位 | 八度离散档 + 交叉淡化 + 周期整除（§3.1.4、§3.2.6） |
| float32 精度 | 城市范围约 3 km，`fract` 的输入可达 1e3 数量级 | 局部 ENU 原点；相位在 CPU 上用 float64 取模后再上传；哈希用整数 PCG，不用 `sin` 哈希（Eanpa 发现大参数下 `sin` 哈希会出现条带） |
| 视觉与物理不一致 | 雾"看起来"比 Sensor 用的能见度更浓或更淡 | MOR→σ 单一真值；`derive.ts` 前后端对拍测试 |
| 双重平滑 | 服务端过渡加前端过渡，变化过慢，并且不同步 | 前端只做 0.3 s 阻尼 |
| 点云 1 px 与 EDL | 透明层叠在点云像素上时会被 EDL 一起压暗 | 雨、雪、沙尘、闪电放在单独的 layer，在 EDL 合成之后再叠加（或者在 r11 的 MRT mask 里排除透明层像素）；雾在点云材质内计算，EDL 只看深度，不受影响 |
| 透明排序 | 雨、雪、沙尘、闪电、云彼此叠加 | 固定 `renderOrder`：天空和云(−100) → 点云和网格 → 沙尘(5) → 雨(6) → 雪(7) → 闪电(10)，全部预乘 alpha 且不写深度 |
| 数据坐标框架不一致 | Chicago 疑似归一化坐标（约 8 个单位宽），Suzhou 的 z 整体偏移约 −680，shanghai 最高约 625 m；米制参数（雨盒、雾高、云底、DSM 容差）会全部失效 | World Package 导入时做单位检测（包围盒、法线尺度、与已知城市尺寸比对）并写入 `scale`、`groundZ`（例如 DSM 的 P5）；环境层只读归一化后的 ENU 坐标 |
| 资源体积 | Eanpa 约 345 MB | 本项目环境资源控制在 ≤12 MB（shape 128³ 8 MB + detail + 天气图 + curl），并允许退到 96³ |
| Eanpa 许可 | 代码 MIT，但部分音频是 CC BY-NC-SA | 只移植代码，不使用它的资源（本项目为科研用途，按要求忽略 license，但仍建议避免直接复制资源） |

---

## 7. 对设计文档 01-design.md 的优化建议

1. **§12 "300,000 particles → GPU Compute Shader"需要改写**：雨在所有档位都应该是无状态顶点着色器粒子（零 compute、可回放），compute 只用于需要"随风场积分"的沙尘、雪漂和风迹线，而且只在 WebGPU 档启用。另外要注明：WebGL2 回退下 compute 走 Transform Feedback，只能实例化绘制，在软件渲染上很慢（本文 §3.10）。
2. **§16 EnvironmentLayer 的子节点应改成按"子系统"划分**，而不是按"天气种类"：`Wind / Precip(Rain·Snow·Dust·Splash) / Atmosphere(Sky·Fog·RainVeil) / Clouds(2D·Volume·Shadow) / Surface(Wetness·DSM) / Lightning / EnvLighting / Quality`（本文 §4.1）。原来的 `Sand` 与 `Rain` 并列，没有体现它们共享锚点、风场和雾的关系。还要补充 **EnvLighting**：点云没有颜色，环境视觉的主要来源是作用在点法线上的日照、云影、湿润、闪光和雾。
3. **§18 E(x,y,z,t) 的字段应补上单位和"单一真值"**：能见度用 **MOR（米）**，消光系数由它换算（σ=3.0/MOR，并统一 2% 与 5% 阈值的口径）；降水用 mm/h（雪用水当量）；另外新增 `surfaceWetness / puddle`（服务端积分，τ 见 §3.6），以及用于回放的累积位移 `dispFall / dispWind`。`Temperature/Humidity/Pressure` 在 V0.3 只是占位，不驱动视觉。
4. **§19–20 风场需要一个前后端统一的接口定义**（本文 §3.1）：Level 0/1 解析形式（风速、来向、幂律切变、阵风 σ、湍流尺度）加上 Level 2/3 栅格（`Data3DTexture RGBA16F`，A/B 双帧按时间插值，全局粗网格与局部细网格嵌套）。文档中 `wind/000_05.vdb` 的方案应改为"服务端插值后重采样成栅格再推送"，前端不读 VDB。还应写明坐标约定（ENU <-> three Y-up）和风向"来向"的惯例。
5. **§21 风场可视化缺少档位与数据表达**：箭头网格（Low）、GPGPU 迹线（Med）、compute 迹线加尾迹（High）；颜色映射 |W|，用 lieflat 顺序色板；在 10/50/120 m 三个高度切片之间切换（对应无人机作业高度）。
6. **§22 Rain Engine 的 Visual 部分应补上**：world-tiling 锚定、积分相位、亚像素补偿、DSM 遮挡、雨区门控（与云覆盖一致）、沙盘相机锚点与八度档（§3.2.6）、远景雨幕（σ_rain 并入雾）。Physics 部分的 `Visibility` 应明确为 `σ_rain(R)` 加到 MOR 里。
7. **§23 Fog**：前端雾要用**高度积分**形式（§3.3），而不是 `scene.fog = FogExp2`；雾色要和天空地平线一致；Low 档在点云材质中逐顶点计算，不增加全屏 pass。
8. **§25 云的三级定义需要按"观察方式"重新划分**：Level 1 = 天空穹顶上的 2D 天气图云（所有设备可用）；Level 2 = 1/16 摊销的低分辨率 ray march（WebGL2 独显）；Level 3 = WebGPU 逐帧 march 加光照缓存。**云阴影是所有档位的默认项**。另外必须写明**沙盘俯视时的 cutaway 规则**（云底 ≥500 m，高于无人机作业高度和城市最高楼，相机在云层之上时只保留阴影和数据层）。原文"第一阶段推荐 Level 2"对无 GPU 或核显设备过重，建议 V0.3 以 Level 1 加云阴影为默认。
9. **§37 刷新频率补充环境部分**：`env.state` 5–20 Hz（包含阵风和累积位移）；风栅格 0.5–2 Hz；闪电事件用单独的事件消息；前端显示层 60 FPS，阻尼时间常数 0.3 s。
10. **§39 Timeline**：环境效果必须是 `simTime` 的纯函数，或者能从服务端状态恢复（无状态雨、确定性闪电、服务端湿度）。有状态的沙尘和迹线在 seek 后需要约 2 s 重新填充（可以接受），UI 应显示"环境重建中"。
11. **§43 MVP 把"完整天气"排除在外是对的**，但建议把"雨 + 雾 + 2D 云 + 云影 + 闪电 + 质量控制"作为 V0.3 的最小完整集：代价低、感知强，而且全部能在软件渲染 CI 上跑通。
12. **新增"环境性能契约"**：环境部分 GPU 预算 Low ≤1.5 ms、Med ≤3 ms、High ≤5 ms，并给出降级链（§3.9），与点云的 point budget 控制器共享同一个 FPS 反馈环。否则"雨天掉帧"会和"点云掉帧"互相抢预算，造成振荡。
13. **§41 World 文件结构应增加环境相关的产物**：`dsm_2m.r16`（加元数据：原点、分辨率、空洞填补方式，见 §3.6 的实测）以及可选的 `cloud_cover_prior.png`（服务端云覆盖的先验）。通用环境资源 `env/cloud_shape_128.bin`、`cloud_detail_32.bin`、`weather_1024.png`、`curl_256.png` 与 `percentiles.json`，由 `tools/bake_env_noise.py` 离线生成，所有 World 共用。
14. **§41/§15 数据导入应增加"米制与地面基准校验"**：UrbanScene3D 各城市坐标框架不一致（本文 §0 第 11 条的实测），World Package 要记录 `scale`、`groundZ`、`maxBuildingHeight`（P99.9）。环境层的云底下限、雨盒高度、雾顶高度、DSM 容差都从这几个值派生。
