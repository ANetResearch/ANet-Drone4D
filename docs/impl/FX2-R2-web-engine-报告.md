# FX2-R2-web-engine 前端引擎与视口：验收与加固报告（第 2 轮修复）

| 项 | 内容 |
|---|---|
| 工作包 | FX2-R2-web-engine（修复区域 web-engine：`apps/web/src/{engine,viewport,net}` 与性能驱动） |
| 日期 | 2026-10-01（17:40–22:30） |
| 依据 | `docs/impl/D1-验收报告-第1轮.md`（§3、§4.2、§4.5、§5、§7）；INT-1 报告 §3、§7；AWR-03 §3.6、§3.8、§8.4、ADR-007、ADR-011、ADR-012、ADR-033、ADR-041、ADR-063；AWR-18 §1.5、§2.5、§3、§4、§5.2、§6.3、§8.6、§10；M05、M06、M07 PRD；FX-WEB1 报告 |
| 环境 | 8 核 Xeon E5-2603 v4、无 GPU；Chrome for Testing 151（SwiftShader，组合 C1）；Node 22.12；`.venv`。本阶段其他区域的修复包同时在本机运行（gateway、sim、web-ui 等），机器负载常在 4–10 之间 |
| 约束执行 | 没有安装依赖；没有使用 git；没有跑验收用的性能基准与 perf 标记用例。诊断性短时测量全部在排他锁 `runs/.perf.lock` 下进行，每次持锁不超过 9 分钟（脚本 `runlock.sh`：取锁后在锁内等 1 分钟 loadavg ≤ 4，最多 4 分钟，再以 `taskset -c 2-6` 运行，整批 540 s 超时）；功能测试（vitest browser、Playwright）持共享锁；颜色只用 token，无 emoji 与禁用字形，未引入 lucide-react 与 backdrop-filter，没有新增 UI 组件 |
| 结论 | 第 1 轮"Tier S 帧节奏"根因已定位并修复：产品点材质与 Tier S 天空的逐片元成本（雾 3 个 `exp`、sRGB 输出 3 个 `pow`、全屏天空）是 g02 原型没有的光栅开销，层配对测法本身失真（WebGL `finish()` 不等光栅）掩盖了它。深圳 `scene=pc` harness 自测（3 次中位，同机其他修复包运行中，运行内 load 最大 9–9.7）：p50 33.3、p95 66.7、p99 83.3 ms、> 50 ms 5.97%、> 100 ms 0.21%、最大间隔 150 ms、冷启动 3.64 s、进入目标带 1.77 s、我方长帧 0；p95 与 > 50 ms 两项在边界（第 1 次运行 50 ms、4.29% 满足），其余全部满足，第 1 轮为 p95 83.3、18.7%、最大 483 ms。整景（FakeSource 诊断）p50 33.3 / p95 66.7 / p99 83.3 ms、> 50 ms 6.6%、> 100 ms 0.19%，五项进入暂定阈值，最大间隔仍有 UI 首次光栅造成的 333 ms 尖峰。规格层面新增 ADR-064（不改任何阈值），修订 AWR-18 §5.2、§6.3、M05、M06 PRD 与 AWR-14 降级 Toast 原则。功能测试全部通过（见第 6 节） |

## 1 结论摘要

- **根因（4.2 帧节奏，影响 03a、03b、04、05、06、09a、19、25、26、30）**：
  1. 点材质在片元阶段计算场景雾与 sRGB 输出变换。GL 点精灵的变化量都来自同一顶点，这两项可以在顶点阶段算出完全相同的结果。改为逐顶点后，2.5 万点的点通道由 35.1 ms 降到 18.9 ms（层配对，离屏 RT，1 像素同步回读）。
  2. Tier S 的 SkyQuad 是第一个绘制、关闭深度测试的全屏四边形，每个像素都算环境天空，约 15 ms；改为远平面上 32 × 18 格、逐顶点着色、在不透明物体之后深度测试绘制的网格，约 2.2 ms。地面网格与禁飞区墙面的线色、墙色也改为顶点阶段输出变换。地面与天空合计 17–18 ms 降到 4.8 ms，固定层合计 7.6 ms（≤ 10 ms）。
  3. 顶点拉取在整张 DrawTable 上做 12 步依赖纹素读取的二分（约 4.3 ms），加了 64 顶点一块的块索引，通常 0–1 步；纹素读取去掉 three 默认的 uv 矩阵乘法，位测试改整数移位。
  4. 测法失真：`finishForBench()` 用 WebGL `finish()`，Chrome 151 中它在命令入队后即返回，层配对只测到提交耗时（第 1 轮"四个固定层合计 0.33 ms"不成立）；配对画到离屏 RT，three 为 RT 另编一套程序并耗尽 uniform buffer 绑定点（layers 用例的控制台错误）。两者都已修正。
- **尖峰（最大间隔）**：trace 显示 200–500 ms 的帧间隔全部是 GPU 进程 `SwapBuffers` 的长等待，紧跟在合成器第一次光栅某类 DOM 内容（`RasterDecoderImpl::DoEndRaster`）与 ANGLE 新建 Vulkan 管线（`ANGLEPlatformImpl::RunWorkerTask`、管线缓存写盘）之后。`scene=pc` 中唯一的 452 ms 尖峰来自 PerfGovernor 第 ① 步（轨迹，在该场景下根本不显示）弹出的 Toast。PerfGovernor 旋钮增加 `visible(level)`，不改变屏上内容的降级步照常降级与记录但不发 Toast；整景中剩余的早期尖峰来自 UI 壳首次光栅（web-ui 区域，第 7 节）。
- **其他修复**：LoAF 我方长帧按条目起点判定（遮罩下 1.37 s 的预热帧不再计为揭开后长帧）；`cas.inBandAtMs` 按 AWR-18 §1.5 逐帧判定；`scene=pc` 只预热点云程序、自检读回与 zoo 重叠（画布独显冷启动 3.7 s）；GC 用例改为测渲染主线程停顿；逐帧路径的 `Math.hypot` 改为逐位相同的无分配实现；`__perf.meta` 补全，`?chrome=0` 不挂载 ViewCube。
- **阈值**：没有放宽任何阈值，也没有冻结暂定阈值（整景尖峰与冷启动仍受 web-ui 区域影响，冻结待验收阶段 3 次中位数据，见第 5 节建议）。

## 2 定位过程与证据

诊断脚本与原始输出都在会话 scratchpad 的 `fx2web/`（`f60.mjs` flight60 单次运行摘要、`full.mjs` 整景 FakeSource、`lay.mjs` 层配对、`spike.mjs`/`gaps3.mjs` trace 尖峰归因、`alloc.mjs` 分配采样与 GC、`tti.mjs` 启动时间线、`b*.log` 各批结果）。下文数据除第 6 节 harness 自测外都是排他锁下的单次诊断运行，不作判定。着色器变体实验用的是 `apps/web` 的临时副本（`.cache/fx2web/`，带 URL 开关），实验结束后已删除，仓库源码中没有实验开关。

### 2.1 测法：层配对为什么读到 0.33 ms

- 同一构建、同一相机路径、2.5 万点：`finishForBench = gl.finish()` 时点通道读作 0.19 ms；改为对目标做 1 像素同步 `readPixels` 后读作 19.4 ms。Chrome 151 的 WebGL `finish()` 不等待 GPU 进程中的 SwiftShader 光栅。
- 配对画到离屏 RT 时，three 的程序键含输出色彩空间（RT 为线性），每个材质在揭开后再编译一套程序，`UniformsGroup` 绑定点耗尽，控制台报 "Maximum number of simultaneously usable uniforms groups reached"（第 1 轮 layers 用例被判异常的原因）。改为在帧自身渲染之前画到绘制缓冲：同尺寸、同格式、同一套程序；帧渲染随后清屏重画并重置 `renderer.info`，出图唯一断言不受影响。

### 2.2 点通道的成本拆分（2.5 万点，配对基底，ms）

| 变体 | 中位 | 说明 |
|---|---|---|
| 第 1 轮材质（逐片元雾与输出变换） | 35.1 | p90 65.8 |
| 雾与输出变换移到顶点 | 18.9 | RT 上测得 |
| 同上，点径钳为 1 px | 15.8 | 光栅只占约 3 ms，其余在顶点阶段 |
| 同上，常量颜色、不做 12 步二分 | 9.2 | 二分约 4.3 ms，着色约 2.3 ms |
| 当前实现（块索引、整数位测试、无 uv 矩阵），绘制缓冲 | 18.1 | 绘制缓冲回读比 RT 多约 3 ms 固定开销 |
| 当前实现：去掉光照 / 去掉雾 / 去掉输出变换 / 1 px 点径 / 常量颜色 | 16.2 / 16.5 / 18.1 / 13.6 / 12.8 | 光照约 1.9 ms（含云阴影采样，晴天现已跳过）、雾约 1.7 ms、光栅约 4.5 ms |

固定预算下的帧节奏（`scene=pc`，测试构建 `fixedB`，35 s）：1 万点 p95 33.3、> 50 ms 0.17%；2 万点 p95 50、> 50 ms 1.18%、最大 83 ms；3 万点 p95 66.7、> 50 ms 6.2%。Tier S 质量下限 B ≥ 2 万（AWR-18 §4.1(c)），下限处已有余量。

### 2.3 地面与天空（配对增量，ms）

| 天空实现 | groundSky 增量 | 天空部分（减去网格 2.8） |
|---|---|---|
| 第 1 轮：全屏四边形，逐像素，最先绘制、无深度 | 17–18 | 约 15 |
| 64 × 36 格逐顶点，远平面深度测试，不透明物体之后 | 8.9 | 6.1 |
| 32 × 18 格（采用） | 5.0 | 2.2 |
| 16 × 9 格 | 4.4 | 1.6 |
| 单个四边形（只有填充，颜色不正确，仅作下限） | 3.9 | 1.1 |

网格较密时三角形建立成本主导。32 × 18 格（格距 20 光栅像素）对环境天空（`(1 − up)^3` 渐变、天空雾、2D 云遮罩）的线性插值误差在 1/255 以内；无环境的恒等天空在地平线处斜率无界，640 × 360 下 97% 以上像素误差 ≤ 1/255、最大 8/255（`tests/m06/vertexOutput.browser.test.ts` 断言）。

### 2.4 尖峰归因（trace）

- 整景 30 s trace 中前四个最大间隔 524、334、290、226 ms：渲染主线程空闲，GPU 进程 `SwapBuffers` 分别等待 459、332、229、214 ms，同时段有 `RasterDecoderImpl::DoEndRaster`（合成器光栅 DOM 图块，43–67 ms）、`ANGLEPlatformImpl::RunWorkerTask` 与 `GpuPersistentCache::DiskCache::Cache::Insert`（新管线写入缓存）。这些尖峰都在揭开后约 10 s 内，之后最大间隔 ≤ 172 ms。
- `scene=pc` 45 s trace：唯一大于 140 ms 的间隔 452 ms 与 `DoEndRaster` 67 ms + 5 个 ANGLE 工作任务重合；同时刻主线程 `Paint` 的节点是 `DIV class='group/toast …'`，即 PerfGovernor 第 ① 步（`floor:trails:1`）弹出的 Toast。该场景隐藏全部可选图层，这一步对画面没有任何作用。
- 结论：SwiftShader 下合成器第一次光栅一类 DOM 内容要新建 Vulkan 管线，代价 0.2–0.5 s。前端引擎能做的是不制造无意义的 DOM 变化：旋钮增加 `visible(level)`（轨迹、视锥按当前是否绘制；标签、低模按当前数量是否超过新上限；motion 在 `?chrome=0` 下为假），不可见的步只记录不发事件。修复后 `scene=pc` 最大间隔 83 ms。整景中 UI 壳首次光栅的尖峰属于 web-ui 区域（第 7 节）。

### 2.5 启动时间线（`__perf.marks`，ms，诊断运行）

| 阶段 | `scene=pc`（修复后） | 整景（FakeSource） |
|---|---|---|
| `boot.canvas`（R3F gl 工厂开始） | 1374 | 2117 |
| `boot.gl`（WebGL 上下文与 WebGLRenderer 创建完成） | 2426 | 3242 |
| `pc.open` / `pc.firstScreen` | 2561 / 3161 | 4186 / 6181 |
| `boot.zoo` / `boot.warm`（shader zoo） | 2849 / 3118 | 4450 / 5906 |
| `pc.firstFrame` / 揭开遮罩（TTI） | 3722 | 9236 / 9237 |

- 画布独显冷启动由 4.36–6.43 s 降到 3.7 s：`scene=pc` 下其余图层在页面生命期内隐藏，zoo 只预热点云（1.36 s → 0.27 s）。
- 整景中自检读回原先排在 zoo 之前，读回轮询要等主线程空闲，而 UI 壳正在挂载，自检开始到 zoo 开始间隔 2.5 s；现在自检先发出绘制、读回在 zoo 之后等待（结果只用于报告，点材质在自检之前已经建好）。整景首屏到首帧仍需 3 s，主要是 UI 壳挂载与首次光栅占用主线程与 GPU 进程（web-ui 区域）。WebGL 上下文创建本身约 1.05 s，属 SwiftShader 初始化。

### 2.6 GC（D1-AC-30）

- 分配采样（整景 FakeSource 20 s，非压缩构建）：前 40 个分配点中 UI 壳与 React 占大部分；我方最大的是 `Math.hypot`（2.1 MB/20 s）。V8 的 `Math.hypot` 每次调用把参数复制进新数组。逐帧路径（选择器的盒距离与平面、插值、相机、无人机分档、轨迹、标签、环境锚点等 33 处）改为 `engine/hypot.ts` 的 `hypot2/3/4`：逐步复现 V8 算法（最大值缩放、Kahan 累加、sqrt 乘回），与 `Math.hypot` 逐位相同（60 万组随机与特殊值单测），g02 选择器预言差分不受影响。
- 第 1 轮 GC 用例把所有线程、所有嵌套层级的 GC 事件相加。同一段 20 s：旧口径 0.94%，渲染主线程停顿（区间并集）0.16%；并行辅助线程 60 ms、Worker 29 ms 不阻塞帧。`perf/gc.spec.ts` 改为渲染主线程停顿，旧口径以 `gc_all_threads_pct` 另记（AWR-18 §6.3 第 7 条同步修订）。

## 3 改动清单

所有者按 AWR-03 §4.3；都在本区域（web-engine）内，另有 1 个 M07 测试用例与文档。

| 文件 | 所有者 | 改动 |
|---|---|---|
| `src/engine/pointcloud/render/pointMaterial.ts` | M05 | 雾与输出变换移到顶点阶段（`outputTransform`），`fog = false`、`userData.awrOutputInVertex`；位测试整数移位；纹理节点 `updateMatrix = false`；块索引纹理节点 |
| `src/engine/pointcloud/render/fetchNode.ts` | M05 | 有块索引时在 `[block[b], block[b+1]]` 内二分并提前退出；拾取子表仍为整表二分 |
| `src/engine/pointcloud/render/idMaterial.ts` | M05 | 位测试整数移位 |
| `src/engine/pointcloud/gpu/DrawTable.ts` | M05 | `buildBlockIndex`、`DrawTables.block`（R32UI）、`commitBlocks`（更新区间 ×4，three 按 RGBA 折算） |
| `src/engine/pointcloud/params.ts` | M05 | `drawIndexBlockLog2: 6` |
| `src/engine/pointcloud/PointCloudEngine.ts` | M05 | 块索引接线与预热保存恢复；`trackInBand`（AWR-18 §1.5 逐帧判定进入目标带，插入排序无分配）；`pc.open`、`pc.firstScreen`、`pc.firstFrame` 标记 |
| `src/engine/pointcloud/core/stats.ts` | M05 | `PerfSink.marks` |
| `src/engine/pointcloud/core/frustum.ts`、`core/DownloadQueue.ts`、`pick/PointPicker.ts` | M05 | `hypot3/2`；拾取表纹理节点无 uv 矩阵 |
| `src/engine/shading.ts` | M06 | `outputTransform()`（按构建目标：画布编码、RT 线性）、`OUTPUT_COLOR_SPACE` |
| `src/engine/hypot.ts`（新增） | M06 | 无分配、逐位相同的 `hypot2/3/4` |
| `src/engine/perf/loaf.ts`、`perf/probe.ts` | M06 | LoAF 按条目起点（≥ `load.revealAt`、≥ 最近一次 reset）计数；`loaf.sinceMs` |
| `src/engine/perf/governor.ts` | M06 | `GovernorKnob.visible(level)`，不可见的步不发 `governor.step` |
| `src/engine/perf/latency.ts`、`time/interpRing.ts`、`time/hermite.ts`、`camera/CameraRig.ts`、`camera/flight.ts`、`picking/Picker.ts`、`geo/frames.ts`、`sensors/gimbalTrack.ts`、`drones/{buckets,index,lowpoly,frustums}.ts`、`drones/trails/TrailRing.ts` | M06、M12、M13 | `Math.hypot` 换为 `hypot2/3/4`（逐位相同） |
| `src/engine/environment/lighting/EnvShading.ts` | M07 | `fogColor` 每次读取新建 uniform 节点；云阴影放在 uniform 分支内（晴天跳过天气图采样与 `exp`），自带 `Fn` 栈 |
| `src/engine/environment/clouds/WeatherMap.ts`、`terrain/dtmSampler.ts` | M07 | 纹理节点 `updateMatrix = false` |
| `src/engine/environment/EnvRuntime.ts`、`precip/PrecipAnchor.ts` | M07 | `hypot3` |
| `src/engine/mission/zones.ts` | M06 | 墙面颜色顶点阶段输出变换；Tier S 不生成阴影线项 |
| `src/viewport/anetNodesHandler.ts` | M06 | 第 4 条：`awrOutputInVertex` 材质不追加输出变换 |
| `src/viewport/layers/groundSky.materials.ts`、`layers/groundSky.tsx` | M06 | Tier S 天空网格（`SKY`、`makeSkyQuadGeometry`、`makeSkyQuadMaterial(u, reversedZ)`）；地面网格线色顶点阶段输出变换 |
| `src/viewport/backend/benchFinish.ts`（新增）、`backend/webgl2.ts` | M06 | `finishForBench` = 1 像素同步回读（M06-L-03 例外与设备微基准并列）；注释更新 |
| `src/viewport/bench.ts` | M06 | 层配对画到绘制缓冲、在帧渲染之前（order −1000）；`meta.scene` |
| `src/viewport/hostRuntime.ts` | M06 | `__perf.meta` 画布尺寸、绘制缓冲、比例、世界、内容版本；`scene=pc` 只预热点云；自检读回与 zoo 重叠；`boot.*` 标记；motion 旋钮 `visible` |
| `src/viewport/WorldCanvas.tsx`、`renderer.ts` | M06 | `?chrome=0` 不挂载 ViewCube；`boot.canvas/gl/backend` 标记 |
| `src/viewport/layers/{trails,sensors,drones}.tsx`、`overlay/LabelHost.tsx` | M06 | 旋钮 `visible(level)`；标签距离 `hypot3` |
| `tests/m06/vertexOutput.browser.test.ts`（新增） | M06 | 顶点阶段输出变换与逐片元一致（画布、RT）；天空网格对逐像素天空的误差与遮挡 |
| `tests/m06/composite.browser.test.ts`、`tests/m06/visual.browser.test.ts` | M06 | 天空新接口（网格几何、`reversedZ`） |
| `tests/m06/lint/m06-lint.mjs` | M06 | `benchFinish.ts` 加入同步回读例外 |
| `tests/pointcloud/drawindex.test.ts`（新增）、`tests/pointcloud/pointsize.test.ts` | M05 | 块索引与整表二分逐顶点一致、步数上界；`DrawTables` 新签名 |
| `tests/perf/hypot.test.ts`（新增）、`tests/perf/governor.unit.test.ts` | M06 | `hypot` 逐位相同；不可见步无事件 |
| `perf/gc.spec.ts` | M16（D1-AC-30 测法，本区域负责项） | GC 停顿 = 渲染主线程 GC 区间并集；旧口径 `gc_all_threads_pct` |
| `perf/m07/env-fixed-time.spec.ts` | M07 | 同时隐藏标签，并等暂停数据源的陈旧机体字形离开视野后再截图（帧率提高后截图提前，第一张图带着"STALE 8.5 s"标签与字形，属用例时序问题） |

文档：`docs/03`（ADR-064、§7.0 索引、§3.8 测法注）、`docs/18`（§5.2 第 1 条、§6.3 第 3、7 条）、`docs/modules/M05`（§6.7.1 块索引、§6.7.2 顶点拉取、§6.7.5 逐顶点雾与输出变换）、`docs/modules/M06`（FR-024、FR-027、FR-076、FR-080、§6.4 预热表与第 4 条、pass 表、场景树）、`docs/14`（降级 Toast 原则）。

## 4 本区域验收项逐条结果

"诊断"指排他锁下的单次运行（FakeSource 或静态服务，非判定）；"自测"指 harness 3 次中位（第 6 节）。最终判定以验收阶段为准。

| 编号 | 第 1 轮 | 处理 | 修复后（诊断或自测） | 预期状态 |
|---|---|---|---|---|
| D1-AC-02 | P0 子项通过；冷启动 pc 4.36–6.43 s、整景 10.7 s（P1 暂定 4.0 s） | `scene=pc` 只预热点云；自检与 zoo 重叠；启动标记 | 自测 tti 中位 3.64 s、TTFP 4 ms；整景 7.7–9.2 s（诊断），主要在 UI 壳挂载与首次光栅 | pc 通过；整景仍超（web-ui），见第 5 节 |
| D1-AC-03a | p50 50 / p95 83.3 / > 50 ms 18.7% / 最大 483 | 第 1 节 1–3 条；无意义 Toast 不再弹出 | 诊断（load 3.5–5.3）：p50 33.3 / p95 50 / p99 66.7、> 50 ms 1.8%、> 100 ms 0、最大 83；自测（3 次中位，运行内 load 最大 9–9.7）：p50 33.3 / p95 66.7 / p99 83.3、> 50 ms 5.97%、> 100 ms 0.21%、最大 150 | 边界：p95 与 > 50 ms 在同机高负载下超线，其余通过；独占条件下复测 |
| D1-AC-03b | p50 83.3 / p95 183.3 / > 50 ms 90.4% | 同上；天空、网格、墙面；固定层测法修正 | 整景 FakeSource：p50 33.3 / p95 66.7 / p99 83.3、> 50 ms 6.6%、> 100 ms 0.19%、最大 333（揭开后 2.7 s 的 UI 首次光栅）；固定层合计 7.6 ms | 五项进入暂定阈值；最大间隔待 web-ui |
| D1-AC-04 | 进入目标带 5.8 s（pc） | 帧节奏修复；`inBandAtMs` 逐帧判定（AWR-18 §1.5） | 自测：进入目标带 1.77 s、换档 0、来回 0、B 反向 12.4 次/分钟；整景 3.4–3.8 s（诊断，揭开后头几秒 UI 首次光栅） | pc 通过；整景待 web-ui |
| D1-AC-05 | 空洞率 26.4%、点数 14 116（被压在下限） | 帧节奏修复 | pc 平均点数 2.1–2.2 万（B 不再被压到 1 万） | 点数项预计通过，空洞率需验收阶段参考渲染复测 |
| D1-AC-06 | 我方 > 50 ms 长帧 1 次（揭开前的预热帧被误计） | LoAF 按条目起点计数 | 自测 0、0、0；失败节点 0、违例 0、驻留 59 601、CPU 缓存 1.3 MB、选择 p95 0.09 ms、上传 ≤ 14 394、重复下载比 1.00 | 通过 |
| D1-AC-09a | n200 p50 116.7 / p95 233.3 | 同 03b | 未单测（需 live ladder 后端）；前端开销按 03b 同比下降 | 待验收 |
| D1-AC-14 | Tier A 用例不存在（P1） | 未处理 | — | 不通过（P1，第 7 节） |
| D1-AC-19 | 过渡期间 > 100 ms 帧 65.4% | 帧节奏修复；云阴影 uniform 分支；`env-switch` 功能用例通过 | 未做性能测量 | 待验收 |
| D1-AC-25 | 编译项通过；操作后最大间隔中位 517 ms | 稳态帧节奏修复；不可见的降级步不弹 Toast | 未做性能测量；`perf/m06/warmup.spec.ts` 功能通过 | 待验收；首次 DOM 光栅停顿属 web-ui |
| D1-AC-26 | t_sim 到像素 p95 300 ms（D 钳在上限）、帧率约 6 fps | 帧节奏修复（整景约 25–30 fps） | 未做 live 测量 | 待验收；用例缺 3 个子项（第 7 节） |
| D1-AC-30 | GC 5.49% | 测法改为主线程停顿；逐帧 `Math.hypot` 去分配 | 主线程 0.16%（旧口径 0.94%） | 预计通过 |

## 5 规格变更与阈值

- **ADR-064**（`docs/03` 附录 E）：Tier S 光栅成本与测法。内容见第 1–2 节；不改变任何阈值、阶梯、点径公式与预算带。同步修订 AWR-18 §5.2（绘制缓冲配对、1 像素回读）、§6.3 第 3 条（LoAF 起点判定）、第 7 条（GC 停顿口径），M05 §6.7.1、§6.7.2、§6.7.5，M06 FR-024、FR-027、FR-076、FR-080、§6.4，AWR-14 降级 Toast 原则（不改变屏上内容的降级步不弹 Toast）。
- **暂定阈值**：本轮没有以 ADR 冻结或放宽任何暂定阈值。理由：整景的最大间隔与冷启动仍受 UI 壳首次光栅影响（web-ui 区域在同期修复），本区域没有 3 次中位的整景 live 数据。建议验收阶段在 web-ui 修复合入后：
  - D1-AC-03b、09a：若整景 3 次中位满足暂定值（本轮诊断已有 5/6 项满足），按 AWR-18 §5.3 冻结为暂定值；
  - D1-AC-02 冷启动：画布独显 3.7 s 已低于 4.0 s，可按 4.0 s 冻结 `scene=pc`；整景单列（本轮 7.7–9.2 s，主要来自 UI 壳），由 web-ui 区域给出数据后另定；
  - 固定层合计按修正后的测法冻结，groundSky 4.8 ms 超过 §3.8 的单层暂定预算 1 ms，但合计 7.6 ms ≤ 10 ms，建议以合计为准、单层预算按实测重新分配（AWR-18 §5.3 第 2 条）。

## 6 测试

| 范围 | 命令 | 结果 |
|---|---|---|
| 类型检查 | `npx tsc -p tsconfig.json --noEmit` | 通过 |
| oxlint | `npx oxlint src/engine src/viewport tests/...`；`make lint` 中 `oxlint --type-aware` | 通过 |
| vitest unit（本区域） | `vitest run --project unit tests/m06 tests/perf tests/pointcloud tests/environment tests/mission tests/net tests/sensors tests/time tests/geo` | 77 个文件、476 例全部通过（含新增 `drawindex`、`hypot`、governor 不可见步；`selector.diff` 与 g02 预言逐帧一致） |
| vitest browser（本区域） | `vitest run --project browser tests/m06 tests/pointcloud tests/environment tests/fixtures/env.browser.test.ts` | 8 个文件、24 例全部通过（含新增 `vertexOutput.browser.test.ts`；中途 `engine.browser.test.ts` 的类别掩码用例发现块索引更新区间问题，已修复） |
| vitest unit（其他组，回归） | `vitest run --project unit tests/m11 tests/m16 tests/contracts tests/fixtures tests/m15` | 38 个文件通过、1 个跳过（446 例通过） |
| Playwright 功能（M05、M06、M07） | `playwright test --project=perf perf/m05/ perf/m06/ perf/m07/`（测试构建，`M05/M06/M07_DIST`，共享锁） | 42 通过、7 跳过（需性能开关或 live 后端的用例按设计跳过）、1 失败：`env-fixed-time`，原因是用例时序（第 3 节表末行），修正后单独复跑通过（差异 0） |
| make lint | `make lint`（22:20 复跑） | 前端与通用规则（`oxlint --type-aware`、emoji 与禁用字形（含本包修改的文档）、hex、lint-lf、motion、icons、no-raw-controls、brand、check-units、m06-lint、perf flags、thresholds、cn-keys）全部通过；失败项只有 sim 区域同期修改中的 Python 文件（ruff I001 `python/awr/environment/field.py`，导入边界 `python/awr/sim/runtime/warm.py`；更早一次另有 `sim/fleet/kernels_watch.py` SIM109 与 `stages/tap.py` I001，已由其所有者处理），不在本区域，本包没有修改任何 Python 文件 |
| harness 自测 | 见下 | 见下 |

harness 自测（`node perf/harness/run.mjs --case flight60.shenzhen.pc --gate G4 --run-id fx2r2-web-selfcheck`，生产构建，排他锁、开跑前 load ≤ 4、3 次中位；结果在 `runs/perf/fx2r2-web-selfcheck/flight60.shenzhen.pc/`）。运行期间其他修复包仍在本机运行，运行内 load 均值 6.5–7.2、最大 9.0–9.7（第 1 轮验收为 5.9–7.2，且无其他 agent）：

| 指标 | 3 次 | 中位 | 阈值 | 判定 |
|---|---|---|---|---|
| p50 / p95 / p99（ms） | 33.3 / 50, 66.7, 66.7 / 66.7, 83.3, 83.3 | 33.3 / 66.7 / 83.3 | ≤ 33.4 / 50 / 100 | p95 不通过，其余通过 |
| > 50 ms / > 100 ms（%） | 4.29, 9.75, 5.97 / 0.06, 0.21, 0.26 | 5.97 / 0.21 | ≤ 5 / ≤ 0.5 | > 50 ms 不通过 |
| 最大间隔（ms） | 117, 150, 183 | 150 | ≤ 250 | 通过（第 1 轮 483） |
| 冷启动可交互 tti（ms） | 3641, 3851, 3556 | 3641 | ≤ 4000（暂定，P1） | 通过（第 1 轮 4550） |
| TTFP（ms，扣除预热等待） | 4.0, 1.9, 5.7 | 4.0 | ≤ 1000 | 通过（离散度告警，几毫秒量级） |
| 进入目标带（ms） | 1647, 1769, 1944 | 1769 | ≤ 2000 | 通过（第 1 轮 5800） |
| 换档 / 来回 / B 反向（次/分钟） | 0 / 0 / 12.4, 6.2, 14.5 | 0 / 0 / 12.4 | ≤ 2 / 0 / ≤ 15 | 通过 |
| 我方 > 50 ms 长帧 | 0, 0, 0 | 0 | 0 | 通过（第 1 轮 1） |
| 失败节点、drawn ≤ B 违例、CPU 缓存、选择 p95、每帧上传、重复下载比 | 0、0、1.3 MB、0.09 ms、14 394、1.00 | — | 见 03 | 通过 |
| 平均点数 | 21 094、20 731、20 574 | 20 731 | ≥ 2 万（画质用例判定） | 达到（第 1 轮 14 116） |

p50 稳定在 33.3 ms，帧时间分布落在 33.3 / 50 ms 两个 vsync 档的交界：`drop_pct`（> 1.5·T*，含恰为 50 ms 的帧）24.5–42.1%，> 50 ms（即 66.7 ms 及以上）4.3–9.8%。CAS 在 Tier S 质量下限 B = 2 万处饱和，点数已不能再降；安静机器上固定 2 万点为 > 50 ms 1.18%、p95 50 ms（第 2.2 节），说明余量主要被同机其他进程占去（SwiftShader 线程与其他 agent 的进程共享 core 2–6）。本区域判断：在验收阶段的独占条件下 D1-AC-03a 应能通过；若仍在边界，下一步是第 7 节第 5 条的顶点阶段余量。

## 7 遗留问题与建议

| 编号 | 问题 | 区域 | 建议 |
|---|---|---|---|
| 1 | 整景揭开后约 10 s 内的 200–500 ms 尖峰与冷启动 7.7–9.2 s：合成器第一次光栅 UI 壳各类内容时新建 Vulkan 管线（SwiftShader JIT），以及 UI 壳挂载占用主线程 | web-ui | 在遮罩下让 UI 壳的各类内容（面板、HUD、Toast、图表）先光栅一次（遮罩之下可见、不透明度非 0），或推迟非首屏面板的挂载；本区域已不再制造无意义的 Toast |
| 2 | D1-AC-14 Tier A 功能矩阵（P1）未实现：产品没有 WebGPURenderer 后端，`?tier=A` 回退经典路径 | web-engine | 在 `viewport/dev/featMatrix.ts` 增加 WebGPURenderer 版 28 项（g01 `results/F_feat_*.json` 的 WebGPU 列为预期值），用例只用 C2 标志的 SwiftShader WebGPU；属 P1，本轮优先处理 P0 |
| 3 | D1-AC-26 用例缺 3 个子项：命令到可见需要命令路径打 `cmd.sent` 标记（UI 热键经 `ui/actions/vehicleCommands` 下发，不经 `viewport/gotoRule.ts` 的 `markSent`）；关注集切换与 ×10 HOLD 用例未写 | web-ui、M16 | `runForSelection` 改走 `sendVehicleCommand` 或调用 `latency().markCmd`；`latency.spec.ts` 增加关注集切换与 ×10 段 |
| 4 | D1-AC-05 空洞率未复测（需要参考页构建） | 验收 | 验收阶段按第 1 轮方法复测；本轮点数已不再被压到 1 万 |
| 5 | 点通道仍是 Tier S 帧的主要成本（2 万点约 14–15 ms）：顶点拉取（6 次纹素读取）与逐顶点光照、雾 | web-engine | 若需要更多余量：环境雾的霾项 `σ·exp(−z0/H)` 每帧在 CPU 预计算，晴天跳过雾层与降水层分支；Lite 点径在 sparse 帧的 16 px 上限（ADR-063 保留）是光栅成本的主要来源 |
| 6 | `env-fixed-time` 一类以墙钟截图比对的用例对"暂停数据源下机体陈旧超时"敏感 | M07、M16 | 已在该用例中处理；同类用例应隐藏全部机体表示或等待其离开 |
