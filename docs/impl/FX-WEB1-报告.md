# FX-WEB1 前端引擎与视口集成：验收与加固报告

| 项 | 内容 |
|---|---|
| 工作包 | FX-WEB1（区域：前端引擎与视口集成，M05 / M06 / M07 / M13 前端，另含 M12 固定时刻、M11 模型静态路由） |
| 日期 | 2026-09-29（第 1 轮，因额度中断，未验证、未写报告）；2026-10-01（续作：核查第 1 轮改动、修复、补测试、视觉复核与文档） |
| 依据 | INT-1 报告 §3、§6、§7；AWR-03 §4.3、§8.4、ADR-011、ADR-012、ADR-033、ADR-044；AWR-17 §5.1；AWR-18 §3、§4.5、§8.6、§9、§10；M05、M06、M07、M11、M12、M13 PRD；请求 M05-to-M06、M06-to-M05、M13-to-M06、M06-to-M11、FX-WEB2-to-M06-M12 |
| 环境 | 8 核 CPU、无 GPU；Chromium 151（SwiftShader，组合 C1）；Node 22；`.venv`。私有测试构建与生产构建放在会话 scratchpad（`VITE_AWR_TEST_SWITCHES=1 vite build --outDir <私有目录>`），经 `M05_DIST`、`M06_DIST`、`M07_DIST`、`AWR_PERF_DIST`、`AWR_WEB_DIST` 使用，不覆盖共享的 `apps/web/dist`；`AWR_PORT_OFFSET=17` |
| 约束执行 | 未安装任何依赖；未使用 git；未运行性能基准与 perf 口径（`M05_PERF`、`M06_PERF`、`M07_PERF` 均未设置，Playwright 只跑功能断言）；颜色只用 token；无 emoji 与禁用字形；未引入 lucide-react、backdrop-filter；没有新增 UI 组件；`make lint` 最终通过 |
| 结论 | 五项任务全部完成。flight60 驱动与 `perf/m05/flight60-pc.spec.ts` 四城不再跳过；EDL 在应用内 Tier B 生效、画质掩码、点云拾取端到端、PerfGovernor 接 CAS、RedArbiter 的 hero 类别、点程序进 shader zoo（TTFP 不含首次编译）、`drawCount()` 竞态与生产包测试开关均有功能用例覆盖；M13 视锥与云台、M07 着色提供者与 EnvSample32、M12 固定时刻渲染、P600 hero 模型路由与无人机三档都已核验。续作新修 4 个缺陷（首次点拾取落到别处、hero glb 晚到时 Tier B 揭开后编译、固定时刻用例相机被打开世界覆盖、world.json 的 UI 读取未 revalidate），并按视觉复核把点径上限改为按节点区分（ADR-063）。功能测试：vitest 948/948（另 1 例原有跳过）；Playwright 本区域功能用例 56 例全部通过（M05 22、M06 17、M07 5、skeleton 4、e2e 8；另有按需运行的视觉复核 8 例通过；interaction 在负载下首次失败一次、复跑通过，见 §5）；pytest 本区域子集 12/12；`make lint` 通过 |

ADR 编号说明：任务书给出"从 ADR-057 起或复用 054"。第 1 轮在代码注释中预留了 ADR-054；续作写入基线时，FX-SIM1 已同时把 ADR-054 与 ADR-057–062 用于其决策（`docs/03` 附录 E），因此本包的决策登记为 **ADR-063**，代码注释与 M05 PRD 已全部改为 ADR-063。

## 1 续作起点：第 1 轮改动核查

第 1 轮（2026-09-29 12:00–14:18）修改了约 45 个文件，并在 `apps/web/perf/_fxdbg/` 留下 7 个调试用例，没有报告，也没有验证。续作先做了以下核查：

- `tsc`、`oxlint --type-aware`：0 错误。vitest（本区域 unit + browser）全部通过。
- 生产包扫描 `prod-bundle-scan.mjs`：干净。
- Playwright：`perf/m05` 22 例中 2 例失败（`pick.spec.ts`、`requests.spec.ts`）；`perf/m06` 12 例全部通过；`perf/m07/env-fixed-time.spec.ts` 失败（画面均差 14/255）；`frustum-live.spec.ts` 通过。
- `make lint`：只有其他工作包进行中的 Python 文件报错。本区域没有违规。
- 第 1 轮把 Tier B 的"气泡"观感处理成帧级点径上限（`Selector.leafKey`）。续作视觉复核发现这个方案在纽约等城市失效，改为按节点区分（§2.5）。

以上失败项与问题都已在续作中修复（§2）。调试用例目录 `perf/_fxdbg/` 与第 1 轮的调试截图 `fx-web1-dbg*.png` 已删除。

## 2 实现清单

### 2.1 任务 1：flight60 驱动与 `scene=pc` 用例

| 项 | 实现 | 文件 |
|---|---|---|
| 生成物 | `tools/bench/flight60/gen.py`（M05）已生成六城 `apps/web/public/bench/flight60/<world>.{bin,json}`，`gen.py --check` 通过（`coordinate_sha256`、`bin_sha256`、`content_version` 与世界一致） | 无改动 |
| 驱动不启动的根因（第 1 轮） | `?chrome=0` 时不挂载 UI 壳，启动门 `shell` 永不解析，遮罩不揭开。flight60 驱动等揭开后才开始，因此 `bench.mode` 一直为空，用例被跳过。`CanvasOnly` 在首次提交时解析 `shell` 门并渲染启动遮罩 | `src/app/App.tsx` |
| 用例解除跳过 | 删除 `test.skip(mode !== 'flight60')`，改为断言 `bench.mode === 'flight60'`、`bench.flightT ≥ 60`、稳态窗口（t > 2 s）内的帧数 > 30；其余不变式（drawn ≤ B、失败节点 0、驻留与缓存上限、重复下载比、上传配额）不变。帧节奏与 CAS 阈值仍只在 `M05_PERF=1` 下判定 | `perf/m05/flight60-pc.spec.ts` |

### 2.2 任务 2：点云与视口的接线

| 项 | 实现与核验 | 文件 |
|---|---|---|
| EDL 在应用内生效（Tier B） | P2 合成四边形改用 M05 的 `EdlCompositeMaterial`：预热前先 `setEdl`，保证 shader zoo 预热的就是它；render 相位每帧确认一次。经典 WebGLRenderer 的 RT 纹理不翻转，合成取样方向按此修正（第 1 轮，`composite.browser.test.ts` 回归用例）。新增 `perf/m06/wiring.spec.ts`：Tier B、rung 2（4 tap）下 P2 的材质就是 EDL 材质。开关 EDL 的对比为 552,960 像素中 381,956 个变暗、87 个变亮，RGB 和均值 218 对 293；切换不编译程序，pass 计划一致 | `src/viewport/hostRuntime.ts`、`viewport/backend/{webgl2,composite}.ts`、`engine/pointcloud/render/edlComposite.ts`、`tests/m06/composite.browser.test.ts`、`perf/m06/wiring.spec.ts`（新增） |
| 画质掩码 | `?quality=1` 采样帧在 overlay 相位，以同一 DrawTable 重画点通道，画到点通道尺寸的 RGBA8 目标；回读 alpha 后按 1 位/像素打包。覆盖目标在测试构建中预热，采样帧不编译程序（第 1 轮）。`perf/m05/quality-mask.spec.ts` 通过 | `src/viewport/qualityMask.ts`、`viewport/backend/webgl2.ts`、`perf/m05/quality-mask.spec.ts` |
| 拾取端到端 | 第 1 轮接通：Picker 的 `point` 分支、`pickPass.ts` 在下一帧 render 相位渲染 5×5 光栅像素的 ID pass（计入 pass 计划）、异步回读、M05 解码；facade 提供 `pick.pointAt` 与 `vp.pointPick`。**续作修复**：拾取相机的投影是手写的（窗口矩阵 × 帧相机投影），但它的 `reversedDepth` 标志为 false。three r186 在反向深度缓冲下首次使用这样的相机时，会按 fov/aspect 调用 `updateProjectionMatrix()` 重建投影，导致页面第一次拾取落到画面外的点（实测 (192, 216) 返回 (−908, −984, 0)，投影不在屏上）。现在构造时按 `caps.reversedZ` 置位。`pick.spec.ts` 的窗口容差改为按档位 `maxPxSparse` 取上界（点径随稀疏态在 maxPx 与 maxPxSparse 之间平滑），失败信息附投影距离 | `src/viewport/pickPass.ts`、`engine/picking/Picker.ts`、`engine/pointcloud/pick/PointPicker.ts`、`viewport/{facade,interaction,session}.ts`、`tests/m06/pickPass.test.ts`（新增）、`perf/m05/pick.spec.ts` |
| PerfGovernor 接 CAS | governor 相位每 tick 调用 `setCas(services.cas)`。第 1 轮修正：同一句柄重复设置不再重建第 7 步旋钮（否则已放开的下限会被遗留）。新增单测；`wiring.spec.ts` 断言 `hasCas` 与第 7 步 `pc.floor` | `src/engine/perf/governor.ts`、`viewport/hostRuntime.ts`、`tests/perf/governor.unit.test.ts` |
| RedArbiter hero 类别 | Class 着色模式、类别 11 可见时，"电力线"作为数据 hero 参与仲裁；视口有更高优先级的红色实体（例如选中机）时调用 `setHeroClassActive(true)`，M05 把类别 11 改用 g50。`wiring.spec.ts`：选中机体后降级，清空选择后恢复 | `src/viewport/bindings/redOwner.ts` |
| 点程序纳入 shader zoo，TTFP 不含首次编译 | 预热时，被预热对象的祖先节点（点云根是拾取对象的父节点）保持可见、层掩码置 0，three 会下降遍历但不绘制它。这样点材质与 ID 材质都在遮罩下编译（第 1 轮）。后端处于 WARMING 时点云不绘制，等待时长记入 `load.warmupWaitMs` 并从 TTFP 扣除。`ttfp.spec.ts` 六城都断言揭开后 `gpu.compiledAfterReveal = 0`，且 `warmupWaitMs` 为有限非负值 | `src/viewport/backend/warmup.ts`、`engine/pointcloud/PointCloudEngine.ts`、`perf/m05/ttfp.spec.ts` |
| `drawCount()` 切换世界竞态（M06-to-M05 第 1 条） | INT-1 已改为 `root.visible ? 1 : 0`（three 对可见但绘制范围为空的 Points 也计 1 次 draw）。续作核实：pass 计划与出图在同一同步 render 相位内依次执行，之间没有异步点，`root.visible` 不会在两者之间变化。切换世界的 `warmup.spec.ts`、`switch.spec.ts`、`feat-matrix` 两档在续作中各通过 2 次，`planMismatches = 0`。续作另外让测试构建的 M06-E006 诊断带出本帧新编译程序的 cacheKey 前缀，便于定位 | `src/viewport/backend/webgl2.ts` |
| 测试开关不进生产包（M06-AC-010） | M05 的 `fixedB`、`pcInject` 解析与写入都在 `TEST_SWITCHES` 常量分支内，引擎字段改名为中性的 `lockB`（第 1 轮）。续作把 `pcInject` 加入扫描禁词，并补单测。生产构建扫描结果为 clean，`prod.spec.ts` 通过 | `src/engine/pointcloud/PointCloudEngine.ts`、`viewport/layers/pointcloud.tsx`、`tests/m06/lint/prod-bundle-scan.mjs`、`tests/m06/bundleScan.test.ts` |

### 2.3 任务 3：M13、M07、M12

| 项 | 实现与核验 | 文件 |
|---|---|---|
| M13：`rt.swap` 后 `ingestFrame` | telemetry 相位 `swapFrame()` 之后，同一帧把 TelemetryFrame 交给 `engine/sensors` 的 `ingestFrame`（SensorPose48 原始记录与机体姿态），并暴露 `frame`、`frameSeq` 供 M07 读 EnvSample32。`frustum-live.spec.ts`（真实 supervisor + sim-core + api，S1 两架 P600）：p600-01 的相机视图有 ≥ 3 个不同采样时刻，云台在 P600 限位内，视锥轴线与最新样本光轴夹角 ≤ 6°、顶点距样本位置 ≤ 8 m，pass 计划一致，无运行期编译 | `src/engine/drones/{index,frustums}.ts`、`perf/m06/frustum-live.spec.ts`（新增，第 1 轮） |
| M07：着色提供者、`sky()`、`lambert()` | `engine/shading.ts`（M06 所有）定义 `SceneShadingProvider` 与恒等实现，并提供 `setSceneShading`、`onSceneShading`。环境适配器在其余图层与 shader zoo 之前挂载并设置提供者。M05 点材质逐顶点调用 `lambert()`，云阴影与 sun_vis 只乘在直射项上，不再整体乘 `lam·cloudShadow`。无人机网格调用 `lambert()` 并开雾。Tier S 的 SkyQuad 与 Tier B/A 的 P2 背景都调用 `sky()` | `src/engine/shading.ts`（新增）、`engine/environment/lighting/EnvShading.ts`、`engine/pointcloud/render/pointMaterial.ts`、`engine/drones/models.ts`、`viewport/layers/{groundSky.tsx,groundSky.materials.ts,environment.tsx}`、`viewport/WorldCanvas.tsx` |
| M07：去掉临时云四边形、逐顶点云阴影 | 2D 云只由天空的 `sky()` 绘制，远平面临时云四边形已删除，地面与点云的云阴影使用同一天气图与偏移。`env-visual.spec.ts` 断言 Tier S 雨天环境 draws ≤ 3（雨盒、淡出盒、箭头） | `src/engine/environment/EnvRuntime.ts`、`perf/m07/env-visual.spec.ts` |
| M07：EnvSample32 原始记录 | `EnvSampleCache` 从每帧 raw 区（schema 1，80 B 一项，载荷在 +16）复制选中机的最新有效记录（帧槽在下一次 swap 会被复用）。新鲜（≤ 1.5 s 墙钟）且 `flags.valid` 时，面板读数取服务端样本，否则在渲染位姿处本地求值。续作补单元用例 | `src/engine/environment/summary.ts`、`viewport/layers/environment.tsx`、`tests/environment/sample32.test.ts`（新增） |
| M12：`?simTime=&paused=1` | 测试构建把 tRender、tFocus 钉在指定时刻（时钟 PAUSED，倍率 0），`window.__time.setFixed(s)` 移动钉住的时刻，`setFixed(null)` 释放（第 1 轮）；`__env.injectStepAt` 在绝对时刻注入阶跃关键帧。**续作修复用例**：A 页在世界打开之前就设置了相机，随后 `pc.world.opened` 把相机切回 home，两页视角不同，均差 14/255。现在等世界进入 streaming 后再设相机。修复后直接定位与正向播放逐像素一致（均差 0、最大 0），钉住的时刻 2 s 内不变 | `src/engine/time/register.ts`、`engine/environment/dev/testHooks.ts`、`perf/m07/env-fixed-time.spec.ts` |

### 2.4 任务 4：P600 hero 模型与无人机三档

| 项 | 实现与核验 | 文件 |
|---|---|---|
| 静态路由 | M11 已有 `/vehicles/{model}/model/{file}.glb`（INT-1）。第 1 轮新增 `/models/{file}.glb`：服务前端构建自带的副本（`apps/web/public/models/` → `dist/models/`），只暴露 `*.glb`，no-cache + ETag；缺失时返回 404 `305`，不落入 SPA 回退。此前该路径返回 `index.html`，GLTFLoader 解析失败，P600 一律退回低模。`SPA_RESERVED` 增加 `models`。续作补 pytest（200 `model/gltf-binary`、304、HEAD、`?v=` immutable、缺失与非 glb 名称 404 `305`、SPA 回退仍可用） | `python/awr/api/static.py`、`tests/rt/test_static_models.py`（新增） |
| 加载顺序 | `HERO_SOURCES.p600 = ['/models/p600.glb', '/vehicles/p600/model/p600.glb']`，取第一个能解析的源。只服务 `dist/` 的服务器（vite preview、测试服务器）也能加载 hero；`perf/m05/server.ts` 补 `.glb` MIME | `src/engine/drones/vehicleModels.ts`、`perf/m05/server.ts` |
| **续作修复**：Tier B 揭开后编译 hero 程序 | glb 是异步加载的，通常晚于 shader zoo 到达。`HeroBatch.setGeometry` 此前会新建材质，而 three r186 的 node 程序缓存键默认取节点 id（`Node.customCacheKey()` 返回 `this.id`），新材质的节点图哪怕结构相同也会编译新程序。Tier B 上必现 M06-E006。改为只替换几何、保留已预热的材质（烘焙几何与占位几何同为 `{position, normal}`） | `src/engine/drones/models.ts` |
| 三档截图 | 新增 `perf/m06/drone-tiers.spec.ts`（Tier S、Tier B 各 1 例）：FakeSource 200 架地面环形布局，相机距 0 号机 4.3 m，同一帧内 hero 1 架、低模 9 架、标记点 200 个；断言三档都有在屏机体、`/models/p600.glb` 返回 200、pass 计划一致、揭开后无编译。整帧截图隐藏点云、禁飞区、降水、视锥、轨迹与标签，另按每档在屏半径最大的一架输出裁剪图 | `perf/m06/drone-tiers.spec.ts`（新增） |

### 2.5 任务 5：视觉质量复核与点径规则（ADR-063）

做法：新增 `perf/m05/visual-tierb.spec.ts`（`M05_VISUAL=1` 时运行，可选 `M05_VISUAL_CITIES`、`M05_VISUAL_COLOR`、`AWR_SHOTS_DIR`）。六城都用 `?tier=B&fixedB=2000000&chrome=0`，并把画质锁在 high 档（rung 5：τ 1 px、rs 1、EDL 8 tap）；software 设备的自动上限是 soft 档，这一步等同于用户在图层面板选择"高"。相机放在最高楼（flight60 的 peak）沿进近方向后退 700 m、高 380 m 的斜视中景。用例断言 Tier B、EDL 生效、非稀疏（τ 受限）、drawn ≤ B、失败节点 0、pass 计划一致、揭开后无编译，并要求表面内部空洞 < 5%。表面内部空洞的定义是：背景像素且 8 邻域中至少 6 个有点。

复核结论：

1. **"气泡"观感**：Lite 只缩小 1 级，前沿层与其父层的点径 < 2·sizeK·τ。两级及以上的祖先 pitch 至少是前沿间距的 2 倍，在 τ 受限时被 maxPx 8 钳成圆盘，盖住细层，深圳、纽约、旧金山、芝加哥都出现了。第 1 轮的帧级方案（`max(上限, ⌈sizeK·leafKey⌉)`）有缺陷：只要一个近处叶节点，就会把整帧上限抬回 8，纽约、旧金山、上海、芝加哥在 `limitedBy = complete` 时又出现气泡。续作改为按节点区分，即 ADR-063：
   - 非叶节点在非稀疏帧中钳到 `min(maxPx, max(4, ⌈2.5·sizeK·τ⌉))`：medium 6、high 5、ultra 4；soft-min 至 low 仍为 8，Tier S 不变。
   - 叶节点保持档位 maxPx，用 NodeTable t1.z 的叶标志区分。
   - 稀疏帧不变。

   同时删除了 `Selector.leafKey`，选择器与原型 oracle 的差分不再涉及该字段。
2. **背景穿透率**（Tier B，high 档约 1.70M 点，g02 原值 → ADR-063；cloudRT 下部 55% 中的背景像素，含建筑间的真实背景）：

   | 城市 | 近景 | 中景 | 街景 |
   |---|---|---|---|
   | 深圳 | 0.0055% → 0.022% | 0.024% → 0.051% | 0.039% → 0.12% |
   | 纽约 | 0.14% → 0.37% | 0.077% → 0.21% | 5.8% → 7.0% |
   | 芝加哥 | 0.52% → 1.04% | 0.083% → 0.19% | 15.3% → 18.6% |

   六城斜视中景的表面内部空洞为 0.011%–0.42%，覆盖率 28%–85%。对比截图中气泡消失，楼体轮廓与竖向纹理清楚，EDL 勾边正常，高度渐变（旧金山为 HAG）由暗到亮。法线模式另截深圳、旧金山两张，立面明暗随朝向变化，屋顶受光。
3. **Tier S 低预算的"体素块"**：按 M05 PRD 复核。点径以光栅像素计，`H_px = dbH`，DPR 0.5 已包含在内；稀疏态取 maxPxSparse，300 ms 平滑；Tier S 为方点，无片元 discard。实现与规格一致，块状来自 25k 点预算下的规定降级（INT-1 §6 也这样记录）。按 RK-M05-02 的候选对策实测 soft-min、soft 的 maxPxSparse 16 → 12（`fixedB=25000`，深圳）：背景穿透率近景 1.0% → 3.0%，街景 11.7% → 18.2%。方块观感并未消失，楼体反而开始破碎，因此保持 g02 定案 16，结论记入 ADR-063 依据与 M05 RK-M05-02。

### 2.6 本区域收到的请求（FX-WEB2-to-M06-M12）

| 条目 | 处理 | 文件 |
|---|---|---|
| 1 实时暂停时机体标签显示"信号延迟 STALE 276.8 S" | 冻结态（TIME 状态 PAUSED 或 STEPPING，且 TIME 本身新鲜）不再置 `MARK.STALE`：暂停时没有新样本是因为没有运动，不是信号延迟；TIME 过期（连接降级）时照常标记。插值环的墙钟年龄在暂停时按 `rateNominal` 折算，会把仿真时间差显示成很长的延迟，这正是上述现象的来源 | `src/engine/drones/{DroneLayer,index}.ts`、`tests/m06/drones-path.test.ts` |
| 2 标签格式化器收到含子模式位的状态字节 | `LabelHost` 传入解码后的 FlightState（`flight_state` 位 0–4），M15 侧的 `& 0x1f` 保留，仍然幂等 | `src/viewport/overlay/{LabelHost.tsx,labelFormatter.ts}` |
| 3 Mock 重建世界下 ViewCube 被顶栏遮挡 | 未处理：该产物世界已被 FX-WEB2 删除，本轮无法复现，见 §7 | — |

### 2.7 其他修正

- `perf/m05/requests.spec.ts` 失败的根因：FX-WEB2 新增的 `worldDatasetQuery` 读取 `world.json` 时未带 `cache: 'no-cache'`，排在引擎请求之前，导致用例取到的第一条请求没有 no-cache。按 AWR-17 §5.1，`world.json` 是 contentVersion 的权威来源，所有读者都应 revalidate，因此 UI 侧补 `cache: 'no-cache'`。用例改为断言每一次 `world.json` 读取都 revalidate，不再依赖请求顺序。涉及 `src/app/query/options.ts`（M15）与 `perf/m05/requests.spec.ts`。

## 3 验收对照（本区域）

| 编号 | 结论 | 证据 |
|---|---|---|
| D1-AC-02（首屏，功能部分） | 功能通过；时限属性能项，未测 | `ttfp.spec.ts` 六城：首屏字节数与规则 R 一致，揭开后无编译，`warmupWaitMs` 记录并从 TTFP 扣除。≤ 1.0 s 只在 `M05_PERF=1` 下判定 |
| D1-AC-03a（flight60 `scene=pc`，功能部分） | 驱动可用；节奏属性能项，未测 | `flight60-pc.spec.ts` 四城、`perf/m06/bench.spec.ts`（位姿 ≤ 1e-3 m、60 s 置 done、sha 不符拒绝） |
| D1-AC-05（画质，功能部分） | 采样与掩码可用；对比属性能项，未测 | `quality-mask.spec.ts`；视觉复核见 §2.5 |
| D1-AC-14（渲染后端 B、S） | 通过 | `feat-matrix.spec.ts` 两档 |
| D1-AC-25（无运行期编译） | 通过 | `perf/m06/warmup.spec.ts`；`ttfp.spec.ts`；`pick.spec.ts`（ID pass 不编译）；`drone-tiers.spec.ts`（glb 晚到不编译）；`wiring.spec.ts`（EDL 开关不编译） |
| D1-AC-34（集成门禁） | 通过 | `perf/skeleton.spec.ts` 4/4（`AWR_PERF_DIST` 指向本包测试构建） |
| D1-AC-32（交互） | 通过 | `tests/e2e/interaction.spec.ts`（见 §5 负载说明） |
| M05-AC-007、AC-013、AC-022、AC-025、AC-026、AC-027、AC-029、AC-031、AC-032、AC-033 | 功能通过 | `perf/m05` 22 例 |
| M06-AC-008、009、010、016、021、035、042、045、049、052 | 功能通过 | `perf/m06` 17 例；`tests/m06/*` |
| M07-AC-020、021、023、045（功能部分） | 通过 | `env-visual.spec.ts`、`env-fixed-time.spec.ts`、`env-gpu.spec.ts` 两档、`env-switch.spec.ts`；`tests/environment/sample32.test.ts` |
| M11-AC-033（模型路由部分） | 通过 | `tests/rt/test_static_models.py`、`test_skeleton_chain.py::test_rest_and_static` |
| M13-FR-020–022、M13-to-M06 第 1 条 | 通过 | `frustum-live.spec.ts` |

## 4 规格变更（已同步设计文档）

| 变更 | 文档 |
|---|---|
| 非叶节点点径上限 `min(maxPx, max(4, ⌈2.5·sizeK·τ⌉))`，叶节点保持 maxPx，NodeTable t1 = `[spacing_L, level, leaf, 0]`，`__perf.pc.maxPxEff` 语义不变，新增引擎统计 `maxPxCapEff`；Tier S 稀疏点径复核结论 | `docs/03` 附录 E **ADR-063** 与 §7.0 索引；M05 PRD 头部修订行、FR-029、FR-030、§6.2.3、§6.7.3、§6.8.1 注、RK-M05-02、AC-007 |
| 静态路由 `/models/{file}.glb`（前端构建内的机型模型副本，缺失时 404 `305`，不落入 SPA 回退）与 hero 源顺序 | 17 §5.1 路由表；M11 PRD FR-082、路由一览表、AC-033；M06 PRD FR-035（同时写明 glb 晚到时只替换几何、保留材质） |
| 拾取相机的 `reversedDepth` 标志须与渲染器一致 | M06 PRD FR-064 |
| 生产包禁词增加 `pcInject`、`selftestNoFix` | M06 PRD AC-010 |
| 测试开关登记 `?fixedB=`、`?pcInject=fail:<p>`、`?simTime=<s>&paused=1`、`?hero=procedural` | 18 §9.5 |
| 着色提供者位于 `engine/shading.ts`（含 `onSceneShading`；不再写 `engine/loop.ts`），`setSceneShading` 返回恢复函数，临时云四边形删除，M07 第 3 条请求改为已落实 | M07 PRD §0 第 5 条、FR-035、§7.2、§13 M06 行、§14 第 3 条 |
| M07-AC-021 判定命令增加 `env-fixed-time.spec.ts` | M07 PRD AC-021 |

## 5 测试结果

| 类别 | 命令 | 结果 |
|---|---|---|
| 类型与静态检查 | `npx tsc -p tsconfig.json --noEmit`；`npx oxlint --type-aware` | 0 错误 |
| Vitest | `cd apps/web && npx vitest run --project unit --project browser` | 122 个文件，948 例通过，1 例跳过（原有） |
| 本包新增或修改的单测 | `tests/m06/pickPass.test.ts`（5）、`tests/pointcloud/pointsize.test.ts`（3）、`tests/environment/sample32.test.ts`（3）、`tests/perf/governor.unit.test.ts`（+1）、`tests/m06/drones-path.test.ts`（+1）、`tests/m06/bundleScan.test.ts`（+1 断言） | 全部通过；去掉拾取相机的置位后 `pickPass.test.ts` 失败（已验证回归能被捕获） |
| pytest（本区域子集） | `pytest -m "not perf" tests/pointcloud tests/rt/test_static_models.py tests/rt/test_skeleton_chain.py::test_rest_and_static` | 12 通过 |
| Playwright M05 | `M05_DIST=<私有> npx playwright test perf/m05 --project perf` | 22 通过，6 跳过（`visual-tierb` 需 `M05_VISUAL=1`；单独运行时 6 + 2（法线模式）全部通过） |
| Playwright M06 | `M06_DIST=<私有> M06_PROD_DIST=<私有生产> npx playwright test -c perf/m06/playwright.m06.config.ts` | 17 通过（bench 3、drone-tiers 2、feat-matrix 2、frustum-live 1、layout 2、prod 1、smoke 3、warmup 1、wiring 2） |
| Playwright M07 | `M07_DIST=<私有> npx playwright test perf/m07 --project perf` | 5 通过（env-fixed-time 均差 0、最大 0） |
| Playwright 门禁与 e2e | `perf/skeleton.spec.ts`；`--project e2e interaction honesty timeline` | skeleton 4/4；honesty 4、timeline 3 通过；interaction 首次在负载下失败一次，复跑通过（见下） |
| 生产包扫描 | `node apps/web/tests/m06/lint/prod-bundle-scan.mjs <私有生产构建>` | clean |
| lint | `make lint` | 通过（续作开始时被其他工作包进行中的 Python 文件阻断；结束时已由其所有者修正） |

负载说明：续作期间 FX-SIM1、FX-WEB2 续作同时在本机运行剧本与浏览器用例。

- `interaction.spec.ts` 第一次运行时，"在此添加 P600"出现后按 Enter，10 s 内没有发出 `POST /api/fleet/vehicles`，roster 未增加。复跑 1/1 通过（1.3 min）。这一步属于 M15 的工具确认卡，本包未改动该路径。
- `pick.spec.ts` 在放宽窗口容差之前，近邻遮挡判定偶发失败（19/20）；按档位 `maxPxSparse` 取上界后连续通过。
- 未运行全量 `pytest -m "not perf"`（约 48 min）。本包的 Python 改动只有新增测试文件，第 1 轮的 `static.py` 由上表子集与 skeleton 链路覆盖。

## 6 截图与视觉复核

目录 `.cache/impl/shots/`：

- `fx-web1-tierB-{shenzhen,newyork,shanghai,suzhou,sanfrancisco,chicago}.png`：六城 Tier B 高预算斜视中景（世界缺省着色：旧金山 HAG，其余高度），ADR-063 后。
- `fx-web1-tierB-{shenzhen,sanfrancisco}-normal.png`：法线着色。
- `fx-web1-tierS-shenzhen-{close,street}-px16.png` 与 `-px12.png`：Tier S `fixedB=25000` 下 maxPxSparse 16 与 12 的对比（ADR-063 依据）。
- `fx-web1-drone-tiers-{S,B}.png` 与 `-{hero,low,marker}.png`：无人机三档整帧与裁剪。

复核结论：

- **Tier B 高预算**：楼体轮廓与立面纹理清楚，高度渐变与 HAG 渐变层次分明，EDL 勾边正常，没有块状或圆盘叠盖伪影。仍有可见的细小黑点，是立面在叶层的采样稀疏，内部空洞 ≤ 0.42%。
- **Tier S 低预算**：大方块是规定的降级（软件档 25k 点、方点、maxPxSparse 16 光栅像素，即 32 CSS 像素）。缩小点径会让楼体破碎。
- **无人机**：hero 为 P600 模型，四臂、起落架与天线清楚；低模为小尺寸六旋翼剪影；远处为箭头标记点。三档可辨。

## 7 改动文件清单

路径相对 `apps/web/`，除非另注。"R1"为第 1 轮，"R2"为续作。

| 文件 | 所有者 | 轮次 | 说明 |
|---|---|---|---|
| `src/app/App.tsx` | M15 | R1 | `CanvasOnly` 解析 `shell` 门（`?chrome=0` 下 flight60 可启动） |
| `src/app/query/options.ts` | M15 | R2 | `worldDatasetQuery` 读取 world.json 时 `cache: 'no-cache'` |
| `src/engine/shading.ts`（新增）、`src/engine/index.ts` | M06 | R1 | 着色提供者与恒等实现、导出 |
| `src/engine/loop.ts` | M06 | R1 | FrameCtx 冻结、编译与云比例信号 |
| `src/engine/perf/governor.ts` | M06 | R1 | `setCas` 同一句柄幂等、`hasCas`、`knobLevels` |
| `src/engine/picking/Picker.ts` | M06 | R1 | `point` 拾取分支 |
| `src/engine/drones/index.ts` | M06 | R1、R2 | `ingestFrame`、`frame/frameSeq`；R2 冻结判据 |
| `src/engine/drones/DroneLayer.ts` | M06 | R2 | 冻结态不置 STALE |
| `src/engine/drones/models.ts` | M06 | R1、R2 | `lambert()` 与雾；R2 `setGeometry` 保留材质 |
| `src/engine/drones/vehicleModels.ts` | M06 | R1 | hero 源顺序与回退 |
| `src/engine/drones/frustums.ts` | M06 | R1 | 视锥取 M13 样本与云台 |
| `src/engine/environment/{EnvRuntime.ts,index.ts,summary.ts,lighting/EnvShading.ts,dev/testHooks.ts}` | M07 | R1 | 提供者实现、删除临时云四边形、`EnvSampleCache`、`injectStepAt` |
| `src/engine/time/register.ts` | M12 | R1 | `?simTime=&paused=1`、`__time.setFixed` |
| `src/engine/pointcloud/PointCloudEngine.ts` | M05 | R1、R2 | 预热等待与 TTFP 扣除、测试开关折叠（`lockB`）；R2 两级点径上限 |
| `src/engine/pointcloud/params.ts` | M05 | R1、R2 | `tauCapPx`（R2 改为不含 leafKey）、常量 |
| `src/engine/pointcloud/core/Selector.ts` | M05 | R1、R2 | R1 加 `leafKey`，R2 删除（恢复原状） |
| `src/engine/pointcloud/{types.ts,core/stats.ts}` | M05 | R2 | `maxPxCapEff` |
| `src/engine/pointcloud/gpu/DrawTable.ts` | M05 | R2 | NodeTable 叶标志 |
| `src/engine/pointcloud/render/{pointMaterial.ts,idMaterial.ts}` | M05 | R1、R2 | `lambert()` 逐顶点；R2 按叶标志选上限 |
| `src/engine/pointcloud/render/edlComposite.ts`、`pick/PointPicker.ts` | M05 | R1 | 合成取样方向；拾取子表与解码 |
| `src/viewport/{WorldCanvas.tsx,hostRuntime.ts,facade.ts,interaction.ts,session.ts,testHooks.ts}` | M06 | R1 | 环境适配器先挂载、EDL、拾取、governor、测试钩子（`governor()`、`pointCloud()`） |
| `src/viewport/pickPass.ts`（新增） | M06 | R1、R2 | ID pass 调度；R2 拾取相机 `reversedDepth` |
| `src/viewport/backend/{webgl2.ts,warmup.ts,composite.ts}` | M06 | R1、R2 | 拾取 pass、quality 目标预热、合成方向、祖先保持可见；R2 E006 诊断 |
| `src/viewport/bindings/redOwner.ts` | M06 | R1 | hero 类别候选 |
| `src/viewport/layers/{environment.tsx,groundSky.tsx,groundSky.materials.ts}` | M07、M06 | R1 | 提供者设置、`sky()` |
| `src/viewport/layers/pointcloud.tsx` | M05 | R1 | 测试参数折叠、services |
| `src/viewport/overlay/{LabelHost.tsx,labelFormatter.ts}` | M06 | R2 | 解码后的 FlightState |
| `tests/m06/{pickPass.test.ts(新增),drones-path.test.ts,bundleScan.test.ts,composite.browser.test.ts,lint/prod-bundle-scan.mjs}` | M06 | R1、R2 | 单测、禁词 |
| `tests/pointcloud/pointsize.test.ts`（新增）、`tests/environment/sample32.test.ts`（新增）、`tests/perf/governor.unit.test.ts` | M05、M07、M06 | R2 | 单测 |
| `perf/m05/{flight60-pc,pick,requests,ttfp,quality-mask}.spec.ts`、`perf/m05/server.ts`、`perf/m05/visual-tierb.spec.ts`（新增） | M05 | R1、R2 | 见 §2 |
| `perf/m06/{frustum-live(新增 R1),wiring(新增 R2),drone-tiers(新增 R2)}.spec.ts` | M06 | R1、R2 | 见 §2 |
| `perf/m07/{env-fixed-time,env-visual}.spec.ts` | M07 | R1、R2 | R2 修相机时序 |
| `python/awr/api/static.py`（仓库根） | M11 | R1 | `/models/{file}.glb` |
| `tests/rt/test_static_models.py`（仓库根，新增） | M11 | R2 | 模型路由 |
| `docs/03-设计基线与决策记录.md`、`docs/17-接口与实时协议规范.md`、`docs/18-性能与测试方案.md`、`docs/modules/{M05,M06,M07,M11}*.md`（仓库根） | 文档 | R2 | §4 |
| 删除：`perf/_fxdbg/`（7 个调试用例）、`.cache/impl/shots/fx-web1-dbg*.png` | — | R2 | 第 1 轮调试残留 |

## 8 遗留问题与请求

1. **性能口径未测**（阶段规则）：D1-AC-02 的 TTFP ≤ 1 s、D1-AC-03a/03b 帧节奏、D1-AC-04 CAS、D1-AC-05 画质对比、M05-AC-027 拾取 p95 ≤ 200 ms，需在验收阶段于排他锁下以 `M05_PERF=1`、`M06_PERF=1` 与 M16 harness 运行。ADR-063 改了 Tier B/A 非叶节点的点径，D1-AC-05 应按新点径复测；Tier S 不受影响。
2. **FX-WEB2-to-M06-M12 第 3 条**（Mock 重建世界下 ViewCube 被顶栏遮挡）：产物世界已删除，本轮无法复现。请 M06 在下次生成重建世界后，复核非默认 home 相机下 ViewCube 的定位。
3. **软件档 Tier B 的观感**：`?tier=B` 强制在 software 设备上时，自动档位为 soft-min（rs 0.5，再乘 DPR 0.5，等于 CSS 分辨率的 1/4），稀疏点径为 16 光栅像素，即 64 CSS 像素的圆盘。这是强制档位下的诊断场景，不参与判定（ADR-044），真实硬件的起步档与 rs 都更高。视觉复核因此锁在 high 档进行。
4. **FakeSource 地面模式的 STALE**：`fakeGround=1` 的静止机体在 Tier B 负载下，部分标签显示"STALE 2–3 s"。实时仿真的冻结态问题已修复（§2.6）；FakeSource 静止机体的样本频率不属于本区域，暂不处理。
5. **ADR 编号并发**：FX-SIM1 在本轮同时追加 ADR-054、ADR-057–062，本包改用 ADR-063。若合入时编号再次冲突，需同步修改 ADR-063 的引用：`params.ts`、`types.ts`、`PointCloudEngine.ts`、`pointMaterial.ts`、`DrawTable.ts`、`pointsize.test.ts`、`visual-tierb.spec.ts`、M05 PRD。

## 9 依赖与安装

没有安装或升级任何依赖，没有修改 `package.json`、`package-lock.json`、`pyproject.toml`、`requirements.lock`。
