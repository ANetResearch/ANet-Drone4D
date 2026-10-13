# D2-FX-R2-web 报告（区域 web）

| 项 | 内容 |
|---|---|
| 工作包 | D2-FX-R2-web 第 2 次（2026-10-12）：D2 验收第 1 轮（2026-10-11 版）判在 web 区域的两条 P0 不通过项 D2-AC-01（TTFP 相对 D1 +34%）与 D2-AC-30（沙盒整景 synthcity p50 50 ms、> 50 ms 11.2%）逐项定位根因并修复；按任务允许跨模块改动该区域相关代码。第 1 次（2026-10-07，D2-AC-01、21、30、34、38、41、44、48）的报告原文保留为附录 A，其他文件中的"报告 3.x"指附录 A 的 A.3.x |
| 日期 | 2026-10-12 |
| 依据 | `docs/impl/D2-验收报告-第1轮.md`（第 3 节 D2-AC-01、30 行，第 4.6 节）；`.cache/impl/requests/d2-ACC-1-to-M06.md` 第 3、4 条、`d2-ACC-1-to-文档总编.md` 第 5、7 条；AWR-04 §13 D2-AC-01、D2-AC-30；AWR-03 §8.4 D1-AC-02、D1-AC-03b（ADR-071 冻结）与 §11 TTFP 定义；M05-FR-004、FR-005；M18-FR-024、M18-NFR-005、M18-M19 PRD §8.2；ADR-086/087（WebGL2 最小上限） |
| 环境 | VMware 8 vCPU（Xeon E5-2603 v4）、无 GPU；Chrome for Testing 151（SwiftShader，C1，Tier S）；Playwright 1.63.0；Node 22.12.0；`.venv` Python 3.12 |
| 约束执行 | 没有安装依赖；没有执行 git 写操作；没有跑登记的性能基准与 harness（并行阶段）；诊断脚本与构建放在 `.cache/fxr2web-r2/`（不动共享的 `apps/web/dist`）；诊断期间本机有其他工作包的批处理（load 7–24），时延类数值只作方向性证据，凡需判定的都请第 2 轮验收在安静环境按 ADR-033 复测；本包同时用不受墙钟影响的量（提交帧序号、逐任务主线程耗时、每帧进程 CPU、单测）证明根因与修复；没有 emoji 与禁用字形，没有新增颜色；`make lint` 通过 |

## 1 结论摘要

| 验收 | 第 1 轮（2026-10-11 版）实测 | 根因（本包定位） | 处置 | 本包验证 |
|---|---|---|---|---|
| D2-AC-01 进度条（只剩 TTFP） | TTFP D1 13.6 → D2 18.25 ms（6 次中位 +34%，离散度 > 25%）；揭开 +2.5%、沙盒路径、长帧均已通过 | 首屏 `Progress = 1` 的提交从来不在"第一个非 WARMING 帧"，而在它的下一帧：M05 冷启动突发上传额度 `targetPts` 按预算截断后的绘制点数累加（synthcity 首屏 20 000），而上传以整节点进行（22 074），最后一个目标节点被推迟一帧。TTFP 因此包含两帧之间由合成器与 GPU 进程决定的帧间隔；D1 与 D2 都有此缺陷，D2 遮罩下合成更重，间隔更长、更分散（本机 3–53 ms） | 突发额度按目标节点的整节点点数累加（`PointCloudEngine.captureTarget`），首屏目标集一帧上传、同帧提交 | 6 次 D1/D2 交替：提交帧总是"就绪帧 + 1"（D1 3/3、D2 3/3）；修复后 4/4 同帧提交，TTFP 6.3–8.7 ms（同轮 D1 第 6 轮构建 27–131 ms、D2-ACC-1 构建 18–274 ms，load 7–17）；单测：4 城首屏只有 1 个上传帧且在该帧提交，改回原式则 2 城失败 |
| D2-AC-30 可视化性能 | 沙盒整景 Tier S：synthcity p50 50.0 ms、> 50 ms 11.2%（不通过）；sanfrancisco p50 33.3、9.95%（通过）；治理器都在第 7 步 | 主线程：`world:targets.update`（识别物图层）是沙盒整景最大的任务，治理器第 7 步时 10.7 ms/帧、全部 0 级时 15.5 ms/帧（本机高负载）。局部网格缓存键含目标 id 与 1 度航向桶，29 个游走目标一转向就重建扇区网格并分配（10 Hz）；线与面的 info 每次整段重传；面为非索引三角形（43 560 顶点）每次重传。GPU 进程侧：与 D1 世界页相同，D2 只多出识别物描边与沙盒 UI 合成 | 局部网格按形状共享、复制时按航向旋转；面改为索引网格（重传 8 250 顶点）；info 与索引逐项比较、只在变化时上传（`ThinLineBatch.segsXform`/`commitMoved`） | `targets.update` 10.7 → 0.38 ms/帧（第 7 步）、15.5 → 1.45 ms/帧（0 级）；Node 同场景每次重建 3.97 → 1.0 ms（0 级）、2.91 → 0.48 ms（1 级）；A/B 8 次（load 13–24）：渲染进程 CPU 19.2–29.6 → 11.4–15.2 ms/帧、rAF 回调中位 12.4–22.4 → 3.3–5.6 ms；GPU 进程 CPU 不变（178–194 ms/帧）；帧间隔被负载决定（p50 83–200 ms），须安静复测 |

没有放宽任何 P0 阈值；没有冻结暂定阈值（本轮没有安静环境数据；D1-AC-03b 的数值已由 ADR-071 冻结，D2-AC-01 的 5% 不是暂定值）。

## 2 根因定位

诊断脚本在 `.cache/fxr2web-r2/specs/`（未登记，只用于本工作包），分析脚本 `an_*.py` 在同目录。

| 脚本 | 用途 |
|---|---|
| `ttfp-diag.spec.ts` | D1 第 6 轮测试构建与 D2 各构建在 M06 测试服务器（FakeSource）`/world/synthcity` 交替：`__perf.load`（ttfp、warmupWaitMs）、`__perf.marks`（`pc.firstScreen` = TTFP 起点、`boot.warm`、`pc.firstFrame` = 提交）、包装 rAF 记录每个回调的起止、LoAF |
| `pcdbg.spec.ts` | 打了日志补丁的诊断构建（未压缩、带 sourcemap）：首屏阶段每帧的已驻留目标数、目标集节点数、绘制点数、本帧上传点数、突发额度 |
| `trace.spec.ts`、`an_trace.py`、`an_swap.py` | 启动 Chromium trace（含 V8 CPU 采样）：就绪帧内的调用栈、GPU 主线程 SwapBuffers 与 WebGL 任务 |
| `scene.spec.ts`、`an_prof.py`、`an_main.py` | 真实沙盒栈（`perf/d2/live.ts`）上的 D2-ACC-1 整景（`capacity_config(world, seed=30)` 加 2 架 P600 = 12 架 + 30 识别物 + 敏感体）：诊断构建补丁给出逐任务主线程耗时与 `window.__gov`；可冻结治理器、设置 knob 级别、隐藏图层根或子对象、隐藏 UI；CPU profile、trace 线程时间；CDP `SystemInfo.getProcessInfo` 的每帧进程 CPU |
| `base.spec.ts` | D1 与 D2 世界页的每帧 GPU 进程 CPU（原样、隐藏全部世界图层、再隐藏 UI） |
| `ab30.spec.ts` | D2-ACC-1 `ac30-scene` 口径（揭开后 20 s 起 60 s）在 D2-ACC-1 测试构建（A）与本修复（B）上交替，并记录每帧进程 CPU、rAF 回调分布与治理器历史 |

### 2.1 D2-AC-01：TTFP

1. **TTFP 的实际组成**：TTFP = 提交时刻 − max(首屏数据到达, 预热结束后的第一个非 WARMING 帧)。`ttfp-diag` 6 次（load 1–4）显示提交在"就绪帧"的下一帧，D1 与 D2 全部如此；TTFP = 就绪帧剩余部分（D1 3.9–5.9、D2 3.8–6.5 ms）+ 到下一个 rAF 的间隔（D1 6.7–23.0、D2 3.4–53.2 ms）+ 下一帧到 governor 相位（2.6–4.6 ms）。D1 与 D2 的差异全部来自中间的帧间隔：D2 遮罩下合成更重（GPU 进程 SwapBuffers 忙），rAF 节拍常为 100 ms（D1 为 17–33 ms）。
2. **为什么是下一帧**：`pcdbg` 两次运行，第一个非 WARMING 帧上传 14 492 点、目标集 3 个节点中只驻留 2 个；突发额度 20 000（=`targetPts`，按 `sel.cnt` 即预算截断后的绘制点数累加），第三个节点 7 582 点放不下（`drainUploads` 整节点上传，14 492 + 7 582 = 22 074 > 20 000），下一帧才上传并提交。M05-FR-004 的本意是"遮罩揭开前首帧目标集一次性上传"。
3. **就绪帧本身的耗时**（trace）：就绪帧主要花在 `copyTextureToTexture` 的 `getParameter` 同步等待 GPU 进程（等预热之后的合成帧画完）；它在 `warmReadyAt` 之前，按 §11 口径计入预热等待并扣除，修复不改变这一口径。

### 2.2 D2-AC-30：整景帧节奏

1. **主线程**（`scene` 诊断构建逐任务计时，synthcity，load 15–20）：治理器第 7 步时 rAF 回调 18.4 ms/帧，其中 `world:targets.update` 10.7 ms；冻结治理器并全部置 0 级时 25.8 ms/帧，其中 `targets.update` 15.5 ms；其余任务都 ≤ 1.4 ms（`render:backend.render` 3.7–5.5、`overlay:lf-scheduler` 0.6–1.4）。CPU profile：`targets.update` 771 ms / 10 s，其中 `localMesh` 541 ms（`bodyMesh` 340、`newMeshBuffers` 分配 176），另有 GC 255 ms。
2. **缓存为何总不命中**：`TargetsOverlay.localMesh` 的键是 `${id}|…|${yawKey}|${view}`，扇区的 `yawKey` 为 1 度航向桶；29 个随机游走目标持续转向，`swarm/target/state` 每 100 ms 一次，几乎每次都重建并分配扇区网格；行数据每次变化都重写并整段上传线与面的位置与 info（面为非索引三角形 43 560 顶点，约 0.87 MB/次）。
3. **GPU 进程**（CDP 每帧进程 CPU；负载下 SwiftShader 线程自旋会放大绝对值，只看差值）：
   - D1 与 D2 世界页相同：原样 163.8 / 163.1 ms/帧，隐藏全部世界图层 65.7 / 66.2，再隐藏 UI 55.8 / 56.0。
   - 沙盒整景第 7 步约 190 ms/帧；隐藏点云（10k 点）−67、地面与天空约 −20、识别物 −21（其中描边约 −18，冻结更新不变、把描边线段退化为零长度与隐藏相同：是逐像素成本，不是上传或图元数）、沙盒 UI 合成 −12 至 −17。D2 特有的 GPU 侧增量只有识别物描边（PRD 规定保留）与沙盒 UI。
   - 识别物图层修复前后 GPU 进程 CPU 不变（A/B：A 178.4–193.5、B 179.1–193.1 ms/帧），渲染进程 CPU 下降约 45%。

## 3 实现清单

### 3.1 M05：冷启动突发上传额度（D2-AC-01；M05-FR-004、FR-005；D1-AC-02 口径不变）

`apps/web/src/engine/pointcloud/PointCloudEngine.ts` `captureTarget()`：`targetPts` 改为累加目标节点的 `t.numPoints[i]`（`drainUploads` 上传的量），不再累加 `sel.cnt[k]`（绘制量）；`targetPts` 只用作揭开前的突发额度，没有其他读者。首帧目标集在第一个非 WARMING 帧全部上传并在同一帧的 governor 相位提交；揭开后的每帧额度、TTFP 起点、预热等待扣除都不变。冷启动首屏少一帧（揭开门控 `firstFrame` 提前一帧），遮罩下第一个上传帧多上传至多一个被截断节点的余量（synthcity 约 7 600 点）。

### 3.2 M18：识别物与敏感体图层（D2-AC-30；M18-FR-024、M18-NFR-005；M18-M19 PRD §8.2）

`apps/web/src/engine/targets/overlay.ts`、`bodyMesh.ts`；对外接口、3 次绘制、治理器两级（0 全部、1 去掉面）、红色规则都不变。

1. **共享的规范网格**：局部网格按形状键（形状、半径、相对 z 范围、半角；取整到 1 cm 与 1e-4 rad）缓存，所有同形状的体共用；扇区以 +x 为中心建一次，复制时按体的 yaw（已含方位偏置）旋转；球、半球、圆柱轴对称不旋转。识别物框按尺寸键缓存（yaw 0），复制时按航向旋转。不再使用的键在每次重建后释放（代数标记，无 Set 分配）。扇区朝向由原来的 1 度桶改为精确航向。
2. **索引面**：新 `bodyFaces()` 生成与 `bodyMesh()` 相同的三角形（同顺序、同绕向）的索引网格，共用顶点（扇区 2 × 49 + 2、半球 25 × 7、球 25 × 13）；`TARGETS_CAPS.faceVerts` 改为唯一顶点上限 64 × 325，新增 `faceIdx` 64 × 1728。30 个识别物移动时面重传 8 250 个顶点（原 43 560），顶点着色次数同比下降。
3. **只传变化**：位置每次重建上传；面 info、索引与线 info 在写入时逐项比较（按 float32 存储值比较，`FACE_ALPHA` 与 0.8 不是 float32 数），只在变化或数量变化时上传。

### 3.3 M06：线批次（`apps/web/src/engine/mission/lineBatch.ts`）

`ThinLineBatch` 新增 `segsXform(src, n, tx, ty, tz, c, s, palette, alpha)`（局部网格的 n 段绕上轴旋转并平移后写入；实线不用线距，写 0；info 比较后写）与 `commitMoved()`（上传位置，info 只在变化时上传）。`seg()`、`commit()` 行为不变（`seg()` 标记 info 已变）。只有识别物图层使用新方法。

### 3.4 测试

1. `apps/web/tests/pointcloud/engine.stream.test.ts` 新增"first-frame target set: one burst, committed in the frame that draws it"（synthcity、shenzhen、sanfrancisco、suzhou 的 flight60 起始位姿）：首屏只有 1 个上传帧，`pc.first.frame` 在该帧发出；把 3.1 改回原式时 synthcity、shenzhen 两例失败（上传帧 2）。
2. `apps/web/tests/m18/overlayMove.test.ts`（新）：扇区按航向放置与 `bodyMesh` 逐点一致（≤ 2e-3 m）、索引面展开后与非索引三角形逐点一致；30 个转向游走目标只有 2 个体网格与 1 个框网格；20 次只移动的重建中 lineInfo、bodyInfo、索引版本不变、位置版本 +20、面位置上传范围 8 250 × 3；选中变化时三种 info 各重传一次。

## 4 偏差（已请文档总编修订，`d2-FX-R2-web-to-文档总编.md` 第 2 次第 7–9 条）

1. **M05-FR-004 与 M05 PRD"首帧目标集"一节**："突发配额 = 首帧目标集点数，≤ B0"改为"首帧目标集各节点点数之和（整节点上传，可超过 B0 至多一个被截断节点的余量）"（3.1）。
2. **识别物图层内部表示**：面由非索引改为索引网格，`TARGETS_CAPS.faceVerts` 的含义改为唯一顶点上限并新增 `faceIdx`；扇区朝向由 1 度桶改为精确值（3.2）。PRD §8.2 的段数（扇区 48 段、球与半球 24 × 12）与图层上限不变。
3. **`ThinLineBatch.segsXform` 写入的实线线距为 0**：只有虚线的丢弃测试读线距，实线不受影响（3.3）。
4. **改动落在其他模块拥有的路径（任务允许）**：M05 `engine/pointcloud/PointCloudEngine.ts`、`tests/pointcloud/engine.stream.test.ts`；M18 `engine/targets/overlay.ts`、`engine/targets/bodyMesh.ts`、`tests/m18/overlayMove.test.ts`；M06 `engine/mission/lineBatch.ts`。已逐一知会（第 7 节）。

## 5 测试结果

| 项 | 结果 |
|---|---|
| `make lint` | 通过（退出码 0：ruff、oxlint type-aware、m06-lint、check-brand、check-deps、check-version、check-thresholds、shadcn postadd 幂等、M16 用例注册表 143 例） |
| `tsc --noEmit`（apps/web） | 通过 |
| Vitest unit | 168 文件 1380 例通过（1 文件 1 例跳过，原状）；含新增 4 + 2 例 |
| Vitest browser | 21 文件 62 例通过（含 `tests/m18/overlay.browser.test.ts`：32 识别物 64 体在 WebGL2 最小上限 24/12/12/24/16 384 下无 GL 错误、无越限绑定） |
| M06 与 D2 GPU 上限（`perf/m06/gpu-limits`、`sensor-views`、`warmup`，`perf/d2/gpu-limits-d2`；本包测试构建与演示构建，`apps/web/perf/m06/gpu-limits.spec.ts` 的上限模拟） | 20/20 通过（测试构建与演示构建 × Tier S/B × min/mid 上限、低多边形实例矩阵、D2.1 场视图 4 例、传感器视图 4 例、揭开后无新程序、沙盒表面含识别物与编排与传感器视图） |
| D2 进度条与 M15 功能（`perf/d2/boot-progress`、`perf/m15/d21-shell`、`sandbox-d2`、`smoke`、`tasks-d2`） | 首轮 22/26，`smoke` 4 例在 load 14–17 下 `#boot-mask` 30 s 未消失（启动超时）；load 7–9 时 `smoke` 重跑 7/7 通过，合计 26/26 |
| 诊断（非登记） | 第 1、2 节所列；TTFP、`targets.update`、进程 CPU 的数值见第 1 节 |
| 性能基准与 harness | 未跑（并行阶段）；帧节奏须安静复测 |

## 6 遗留与风险

1. **D2-AC-30 须安静复测**：本包修掉了 D2 特有的主线程热点（渲染进程 CPU −45%），但沙盒整景在 Tier S 上以 GPU 进程（SwiftShader）为主，它与 D1 世界页同量级，第 7 步时 D2 只多出识别物描边（逐像素，PRD 规定保留）与沙盒 UI 合成。第 1 轮验收 synthcity 与 sanfrancisco 都处在 33.3/50 ms 的分界上，主线程与 CPU 占用下降应把 p50 拉回 33.3，但本机负载下无法判定。若安静复测仍不达标，下一步的杠杆在 GPU 侧：描边着色移到顶点阶段（`ThinLineBatch` 的调色与输出变换现在在片元阶段）、按屏幕尺寸减少描边段数、或沙盒 UI 的合成面积；均涉及 M06 共享线程序或 UI 设计，需要先在安静环境量化。
2. **D2-AC-01**：修复后 D2 的 TTFP 不含帧间隔，D1 基线（第 6 轮构建）仍含，相对判据可满足；须按协议与 D1 构建交替 ≥ 6 次复测。
3. **沙盒 flight60 变体**：`/sandbox/<sid>?bench=flight60&scene=full` 可用（sanfrancisco 实测），但 synthcity 的 flight60 相机文件 `coordinate_sha256` 与当前世界不一致（M06-E012 / PERF-E009 拒绝运行），须由 M16 重新生成后登记（`d2-FX-R2-web-to-M16.md` 第 11 条）。
4. 诊断时 live 栈偶发 503（load 约 19），未影响测量。

## 7 跨所有权请求

| 文件 | 接收方 | 要点 |
|---|---|---|
| `.cache/impl/requests/d2-FX-R2-web-to-M05.md`（新） | M05 | 突发上传额度的修复、效果与单测，请复核 |
| `.cache/impl/requests/d2-FX-R2-web-to-M18.md`（新） | M18 | 识别物图层共享网格、索引面、只传变化；效果与单测，请复核 |
| `.cache/impl/requests/d2-FX-R2-web-to-M06.md`（追加第 7–9 条） | M06 | `lineBatch` 两个新方法；GPU 进程成本分解；安静复测 |
| `.cache/impl/requests/d2-FX-R2-web-to-M16.md`（追加第 9–12 条） | M16 | TTFP 复测要点；D2-AC-30 安静复测与进程 CPU 记录；沙盒 flight60 变体与 synthcity 相机文件；503 |
| `.cache/impl/requests/d2-FX-R2-web-to-文档总编.md`（追加第 7–9 条） | 文档总编 | M05-FR-004 突发配额条文；TTFP 相对判据说明；D2-AC-30 方法栏 |

## 附录 A 第 1 次修复（2026-10-07，原文）

以下为第 1 次报告原文，标题降一级、节号加前缀 A.；其他文件中引用的"报告 3.x"即本附录 A.3.x。

| 项 | 内容 |
|---|---|
| 工作包 | D2-FX-R2-web：第 1 轮验收判在 web 区域的 8 条不通过或未测项（D2-AC-01、21、30、34、38、41、44、48）逐项定位根因并修复；按任务允许跨模块改动该区域相关代码 |
| 日期 | 2026-10-07 |
| 依据 | `docs/impl/D2-验收报告-第1轮.md`（第 3、4.5、4.7 节）、`.cache/impl/requests/d2-ACC-1-to-{M15,M06,M16,文档总编}.md`；AWR-04 §10.6、§15.4、§15.5、§15.7、§15.8、§15.11、§13 与 §15.14 的验收表；ADR-071、ADR-076、ADR-086、ADR-108、ADR-119、ADR-121、ADR-122；M06 PRD §6.3、M15-FR-215 至 FR-217、FR-225、FR-227、FR-228；M18-FR-022；AWR-14 §20.10 |
| 环境 | VMware 8 vCPU、无 GPU；Chrome for Testing 151（SwiftShader，C1）；Playwright 1.63.0；Node 22.12.0；`.venv` Python 3.12 |
| 约束执行 | 没有安装依赖；没有执行 git 写操作；没有跑登记的性能基准与 harness（并行阶段）；诊断脚本放在 `.cache/fxr2web/`，构建输出到 scratchpad（不动共享的 `apps/web/dist`）；诊断运行时本机有其他工作包的批处理与浏览器（load 6–20），文中的时延数值只作方向性证据，须由第 2 轮验收在安静环境按 ADR-033 复测；没有 emoji 与禁用字形，颜色只用 token；`make lint` 通过（全量运行退出码 0） |

### A.1 结论摘要

| 验收 | 第 1 轮 | 根因（本包定位） | 处置 | 本包验证 |
|---|---|---|---|---|
| D2-AC-01 进度条 | 揭开 +31.3%，TTFP 16→108 ms，遮罩下长帧 2→3 | 着色器预热（遮罩下单个 7–10 s 长任务）比 D1 长约 3 s：three r186 节点材质以节点 id 作程序缓存键，同源程序重复编译链接 8 个；五个低多边形机型批次只差 buffer 块名，又是 4 个；D2 传感器视图目标的颜色与深度格式与画布不同，SwiftShader 为全部世界程序再编一套例程 | 程序按实际源码共用（构建期改写 three 查表键）；buffer 块名规范化；传感器目标改用画布格式 | 揭开前链接程序 41→30（`programsAtReveal` 38→27）；预热 9.3→7.1 s；高负载交替 3 次：揭开中位 D1 16.9 s / 修复前 20.9 s（+23.6%）/ 修复后 17.7 s（+4.8%），预热 D1 8.06 s / 修复后 7.76 s；TTFP 与遮罩下长帧仍待安静复测（第 5 节） |
| D2-AC-21 手动控制 | 限幅状态从不显示；接近提示只早 2–3 s | 服务端软围栏只发 `uav.keepout_guard{clamped}` 事件，不给 velocity 调用加 `KEEPOUT_CLAMPED` 警告，界面只看后者；预测只用当前速度直线外推，操作者加速时提前量不足 | 限幅状态跟随受控机的 `uav.keepout_guard` 事件（保持 1.6 s）；预测同时用指令速度、看 3.5 s | live 栈 T6：接近提示在三维距离 134.9 m 出现（约 3.4 s 提前），限幅在 124.7 m 出现并持续，三维最近 122.9 m；`manual-keepout` 第 1 例通过 |
| D2-AC-30 整景 | 帧节奏达标但治理器已隐藏识别物、编排、场图层 | 三个 D2 knob 排在第 1 步且可降到隐藏 | 识别物最多去掉半透明面（敏感体轮廓与识别物框保留），编排最多去掉其他任务航段，场视图最多减半点数 | 功能组通过；帧节奏须 M16 用沙盒 flight60 变体在安静环境复测 |
| D2-AC-34 访客路径 | T5 90 s 无捕获 Toast；chicago T3 102 | 均不在前端：T5 值守只到 D 级（M20）；chicago T3 `follow_path 102 GEOFENCE_REJECT`（M10 或模板） | 请求 M20、M10（`d2-FX-R2-web-to-M20-M10.md`） | 前端访客路径（2 次点击、T5 缺省）不变 |
| D2-AC-38 刷新回到同一 sid | 14.4–15.3 s | 刷新触发 pagehide，节能控制器以 SB-72 信标驻留沙盒，重载页再从 checkpoint 恢复；另：D2-ACC-1 的 Node 侧计时被遮罩下的预热长任务挡住 | 丢弃文档的 pagehide 不再驻留（bfcache 与 freeze 照旧）；测量方法请 M16 改为页内计时 | live 栈：重载后会话条在导航后 1.2–3.1 s 出现、会话不再经过驻留与恢复；判定待安静复测 |
| D2-AC-41 载入期容量 | 请求晚于入口 607–623 ms、晚于主分块；显示晚 1.3–4.4 s | 请求由入口模块发出，而入口模块要等全部静态分块下载与求值；显示经应用 store，被遮罩下长任务推迟 | 构建期把 `app/boot/early.ts` 打成 IIFE，作为 `<head>` 第一个内联经典脚本（charset 之后、样式表之前）发请求并在应答任务里直接写行；清单脚本前移，HTML 解析不再被样式表挡住 | 高负载 3 次：请求在导航后 79–157 ms、早于主分块完成（3/3），相对入口分块 −28 至 +34 ms；应答到显示 6、120、57 ms（修复前 1343–4416 ms） |
| D2-AC-44 节能主线程 | 10 s 4.05% | 节能挂起视口循环时，图表调度器（`ui/lf/scheduler.ts`）回退到自己的 60 Hz rAF；另有 4 个 4 Hz 定时器与 1 Hz 时钟照跑 | 节能挂起期间图表调度器不起 rAF；红色仲裁两处、事件桥刷新、测试探针与 1 Hz 时钟改为节能冻结的定时器 | 同一复现（FakeSource viewer）：5.52% → 1.07–1.89%，其中 0.65–1.4 个百分点为一次 V8 Major GC |
| D2-AC-48 平板摇杆 | 0 Hz，未测 | 接管后无输入时手动泵停发，服务端 250 ms 流看门狗以 209 结束接管（摇杆随之卸载）；关闭中的确认框遮罩在退出过渡期仍可命中；主线程长任务（旋转重排）也会断流；旋转不归零 | 空闲 10 Hz 零速度保活；rt.worker 桥接主线程空档；关闭中的遮罩与弹层不接收指针；旋转时摇杆复位 | live21 栈、iPad Pro 11：摇杆 30.2–30.6 Hz、键盘 30–31 Hz，4 次旋转后接管仍 ACTIVE，旋转后摇杆轴为 0、旋钮复位 |

没有放宽任何 P0 阈值，没有冻结暂定阈值（没有安静环境数据支撑）。

### A.2 根因定位方法（诊断脚本 `.cache/fxr2web/specs/`）

| 脚本 | 用途 |
|---|---|
| `boot-diag.spec.ts` | D1 第 6 轮测试构建与 D2 各构建交替：揭开、TTFP、`__perf.marks`（boot.zoo、boot.warm、pc.firstScreen、pc.firstFrame）、性能 mark（rehearse）、LoAF 明细 |
| `gl-diag.spec.ts` | init script 包装 WebGL2 调用：每个程序的完整着色器源码、链接时刻、首次绘制、同步调用耗时；可选每次绘制后同步（预热期间） |
| `cap-line.spec.ts` | D2-ACC-1 同名脚本加导航计时、`awr.boot.early` mark 与 hint 序列 |
| `trace-boot.spec.ts`、`power-trace.spec.ts` | Chromium trace：启动前 600 ms 的主线程任务；节能 10 s 内的任务归因、定时器调用栈 |
| `ac38-tabs.spec.ts`、`sb-reload.spec.ts` | 刷新回到同一 sid：live 栈与 REST 打桩两种；页内 MutationObserver 计时 |
| `ac47-48.spec.ts` | D2-ACC-1 同名脚本加命中测试（elementsFromPoint）、指针事件序列、`__ux.manual` |
| `manual-keepout.spec.ts` | `perf/m18/manual-keepout.spec.ts` 的副本，加三维距离与 `approachS` 轨迹 |

### A.3 实现清单

#### A.3.1 D2-AC-01：着色器预热（M06 ADR-007、ADR-071，M06-FR-009）

1. **定位**：`gl-diag` 显示 D1 揭开前链接 30 个程序、D2 41 个；按完整源码比较，D2 的 41 个中有 5 组逐字相同（一组 5 个线程序、四组各 2 个），D1 也有 4 对。原因是 three r186 `WebGLNodesHandler` 的程序键含节点图缓存键，而 `Node.customCacheKey()` 缺省返回实例 id，同一工厂分别创建的材质得到不同的键。另有 5 个实例化网格程序只差 `NodeBuffer_<id>`/`buffer<id>`（每个低多边形机型一个 InstancedMesh、一个材质，D2 由 3 个机型增到 5 个）。按预热分段同步计时，传感器目标那一遍 0.53 s：D2 的传感器目标为 SRGB8_ALPHA8 + DEPTH_COMPONENT32F，与画布（RGBA8 + DEPTH24_STENCIL8）不同，SwiftShader 按帧缓冲格式另编例程。
2. **程序按源码共用**：`apps/web/vite.config.ts` `threeProgramsPlugin`（构建期 transform `three/build/three.module.js`）在 `WebGLPrograms.acquireProgram` 开头把查表键换成 `awrSourceKey`：RawShaderMaterial 路径以 WebGLProgram 实际读取的全部输入为键（GLSL 版本、SHADER_TYPE、SHADER_NAME、defines、属性 0 绑定、光源与裁剪平面数、并行编译开关、两段源码），其余材质不变；共享走 three 内置材质共用程序的 `usedTimes` 机制，uniform 按材质逐次上传，UBO 由 UboBinder 按 (材质, 程序) 的组绑定。原文匹配不到时构建失败（`patchThreePrograms`）。
3. **buffer 块名规范化**：`viewport/anetNodesHandler.ts` fix 7 `canonicalBufferNames`：build 后把 `NodeBuffer_<id>`、`buffer<id>` 按出现顺序改为 `NodeBuffer_awr<k>`、`awrBuf<k>`，并在 `onUpdateProgram` 把该 builder 生成的组改为同名（UboBinder 按名找块）；同一材质换了 buffer 节点时保留原名（M06 §6.3 规则 1 保证每个 InstancedMesh 自有材质）。块名保留 `NodeBuffer` 前缀，`gpu-limits` 的实例矩阵用例照常读取。
4. **传感器目标用画布格式**：`viewport/sensorView/pass.ts` 颜色附件 RGBA8（纹理色彩空间仍为 sRGB，three 照旧给世界图层画布程序；存储值与画布相同，采样返回同值，混合语义不变）、深度 DEPTH24_STENCIL8（`DepthTexture` + `DepthStencilFormat`，与 WebGL 绘制缓冲一致，反向 Z 精度同画布）；`materials.ts` 占位深度纹理同格式。`tests/m06/lint/m06-lint.mjs` 只对该文件的 `SENSOR_COLOR_FORMAT` 放行规则 8（偏差 4.3）。
5. **效果**（诊断构建、高负载）：同一次运行 `programsAtReveal` 38→30→27、链接 41→33→30，预热 9.26 s → 7.14 s；传感器目标那一遍 0.53 → 0.045 s。交替 3 次（load 约 11）见第 1 节表；预热段已短于 D1，剩余差距在预热之后（排练相位 D1 3.2–3.8 s、修复后 4.1–4.5 s，第 5 节）。

#### A.3.2 D2-AC-41：载入期容量（ADR-119，M15-FR-225）

1. **定位**：D2-ACC-1 测的是测试构建，入口就是 `main.tsx`，模块脚本要等全部静态分块下载并求值后才执行（容量请求在入口分块到达后约 0.6 s）。演示构建由 `entry.tsx` 发，但同样要等 HTML 解析结束。trace 与 `cap-line` 还显示：模块脚本最早在 `domInteractive`（本机 0.28–1.06 s）执行；`window.__AWR_BOOT_MANIFEST` 内联脚本位于样式表之后，会把 HTML 解析挡到样式表下载完成。vite 又把 `index.html` 里的多个模块脚本合成一个入口，单独加模块脚本无效（已试）。
2. **新模块**：`app/boot/capacityText.ts`（纯函数 `capacityTexts(t)`、`writeCapacityLine`、`bootCapacityFetch`，不依赖 i18n 运行时与 store）；`app/i18n/capacity.ts` 改为把它绑定到 i18n 运行时（对外接口不变）；`app/boot/early.ts` 启动容量行：在显示遮罩的路径上立即发请求（1.5 s 超时，遮罩期间每 5 s 一次），应答到达的同一任务里写行，行节点尚未解析时由 MutationObserver 在插入后立即写、最迟在解析结束写；本机构建只写成功应答；演示构建落地页与 /sandbox/api 不发；交接 `window.__awrBootCapacity` 不变（`app/boot/capacity.ts` 的应用 store 复用在途请求，不发第二个）。文案只带 zh-CN 的 `capacity.*` 键（`vite.config.ts` `bootI18nPlugin` 虚拟模块，类型 `app/boot/virtual.d.ts`），格式化与应用一致（单测逐项比对）。
3. **构建**：`earlyInlinePlugin` 用 vite `build()`（lib、iife、不写盘）把 `early.ts` 打成约 5.6 KB 的内联经典脚本插在 `<meta charset>` 之后；开发服务器插入源文件模块脚本；`bootManifest` 脚本移到 charset 之后；新增 `boot` 分组（`capacityText.ts`、`plural.ts`、虚拟模块）。`app/demo/entry.tsx` 去掉自己的容量请求。
4. **效果**：见第 1 节表（`d21-shell` 的版本行、五段、三种注入文案与遮罩期间 ≥ 5 s 间隔全部通过）。

#### A.3.3 D2-AC-44：节能主线程（ADR-121，M15-FR-227、FR-228）

1. **定位**：节能 10 s 的 trace 中 598 次 `FireAnimationFrame`（60 Hz）占 366.6 ms，回调为 `ui/lf/scheduler.ts` 的回退 rAF：视口循环挂起即 `loop.running = false`，调度器以为"循环没在跑"便自己起 rAF。其余为 4 个 4 Hz 定时器（`ui/notify/redFigures.ts`、`viewport/bindings/redOwner.ts`、`ui/notify/eventBridge.ts`、测试构建的 `ui/testing/uxBindings.ts`）、1 Hz 时钟（`useSecond`）与 GC。
2. **处置**：`ui/shell/frameCap.ts` 新增 `suspendedBy(source)` 与 `onChange`；调度器只在挂起源含 `power` 时不起回退 rAF（报表页、窄窗、手机非视口窗口照旧回退）。新 `lib/powerFreeze.ts`（`setPowerFrozen`、`freezableInterval`：冻结时清除定时器，恢复时立即跑一次再起定时器）；`PowerController` 在 SAVING 与 QUEUED 置冻结、在 RESUMING 与 ACTIVE 解除；上述 4 个 4 Hz 定时器与 `useSecond` 改用 `freezableInterval`。
3. **效果**：同一复现 5.52% → 2.84%（只修调度器）→ 1.07%、1.89%（再冻结定时器），余量中 65–141 ms 是节能后 V8 memory reducer 触发的一次 Major GC（60 s 窗口摊薄）。帧数增量 ≤ 1、除容量外无请求（`d21-shell` 第 3 例）不变。

#### A.3.4 D2-AC-38：刷新回到同一 sid（ADR-121）

1. **定位**：live 栈页内计时显示会话条在重载导航后 1.2–3.1 s 已出现，D2-ACC-1 的 14–18 s 主要是 Node 侧 `page.evaluate` 被遮罩下单个 7–9 s 的预热长任务挡住（首次 `goto` 的 `viewport=off` 在路由跳转后丢失）；产品侧的问题是揭开后刷新会经 pagehide 以 SB-72 驻留，重载页再从 checkpoint 恢复（D2-ACC-1 首次刷新 3.49 s 才回到 active，本机高负载下 13 s）。
2. **处置**：`app/power/PowerController.ts` `onPageHide`：`pagehide` 且 `persisted !== true`（刷新、离开、关闭）时不进入节能、不发驻留信标；进入 bfcache 的 pagehide 与 `freeze` 照旧。关闭的标签页由服务端无连接驻留兜底（120 s，队列非空 60 s）。偏差 4.1。
3. **效果**：重载后不再出现驻留与恢复（相位只有乐观的 spawning → active）；"≤ 3 s"须按页内口径在安静环境复测（请求 M16）。

#### A.3.5 D2-AC-48：平板虚拟摇杆（ADR-122，M15-FR-215 至 FR-217）

1. **定位**（`ac47-48` 加命中测试与 `__ux.manual`）：(a) 接管后 5 个泵周期即 IDLE，原因 `manual.watchdog:209`：泵在输入为零时（零尾 5 包之后）停发，服务端 250 ms 流看门狗结束接管，`[data-stick]` 随 ManualOverlay 的 ACTIVE 条件卸载，即 D2-ACC-1 的"element(s) not found"；`perf/m18/manual-keepout.spec.ts` 注释里"接管后泵发 2–3 个包就停"同因；(b) 紧接确认后的第一次触摸落在 `data-ending-style` 的 alert-dialog 遮罩上（退出过渡在 Tier S 首帧慢时持续更久）；(c) 加了保活后，4 次旋转期间主线程长任务超过 250 ms 仍会断流；(d) 旋转时按住的摇杆不归零。
2. **处置**：`ui/manual/controller.ts` 零尾之后每 3 个泵周期发一个零速度包（`IDLE_KEEPALIVE_TICKS`，10 Hz）；服务端结束流时 `toIdle` 发 FINAL 关闭通道。`net/rt/session.ts`（worker）记住每架机最后一个非 FINAL setpoint，主线程 80 ms 未送新值时补发：500 ms 内重复最后值，之后到 1.5 s 发零速度，再之后停发由看门狗兜底（偏差 4.2）。`styles/motion/base-ui.css`：处于 `data-ending-style` 的遮罩与 dialog/alert-dialog/sheet 弹层 `pointer-events: none`。`ui/manual/ManualOverlay.tsx`：屏幕方向变化时释放指针捕获、旋钮复位、轴归零（`recentreStick`）。`ui/testing/uxProbe.ts` 新增 `__ux.manual`（state、ticks、idle、noAgent、stick、clamped、approachS、ended）。
3. **效果**：第 1 节表。旋转后每秒仍有约 10 个零速度保活包，"归零"应以轴与旋钮判定（请求 M16）。

#### A.3.6 D2-AC-21 与 D2-AC-30（M18-FR-022、M20、ADR-108）

1. **限幅状态**：`keepout_guard`（`python/awr/sim/targets/keepout_guard.py`）在钳制时发 `uav.keepout_guard{uav, target, vol_idx, d_m, clamped}`（每机 ≤ 1 Hz），不改 velocity 调用的警告。`controller.ts` 接管期间订阅页面客户端事件，受控机 `clamped !== false` 即置 `clamped`，最后一次事件后保持 `CLAMP_HOLD_MS` 1.6 s；`KEEPOUT_CLAMPED` 进度警告仍有效。
2. **接近提示**：预测同时用当前速度与指令速度（机体 FLU 按四元数航向换成 ENU；固定翼为沿航向的空速），取较早者，视界 `PREDICT_S + PREDICT_LEAD_S` = 3.5 s（`stores/targets.ts` `manualKeepoutWarning` 与 `app/shell/install.ts` 传入视界）。
3. **治理器下限（D2-AC-30）**：`viewport/layers/targets.tsx` knob 级数 2（最多去掉半透明面；M18-M19 PRD §8.2 的"只显示选中目标的体""体关闭"不再使用）、`tasking.tsx` 2（最多去掉其他任务航段）、`field.tsx` 2（最多减半点数）。偏差 4.4。

### A.4 偏差（已请文档总编修订，`d2-FX-R2-web-to-文档总编.md`）

1. AWR-04 §15.7.1、§15.7.3 的 pagehide 行：丢弃文档的 pagehide 不再驻留（3.4）。
2. AWR-14 §20.10 "输入归零后继续以 0 速发 5 个包再停发"：改为 10 Hz 零速度保活，另由 worker 桥接主线程空档（3.5）。泵整体移入 worker 的原设计仍待 M11（`d2-FX-R2-web-to-M11.md`）。
3. M06 PRD §6.3 规则 8：传感器目标颜色附件设 `internalFormat = 'RGBA8'`（WebGL2 路径的 UNORM 颜色附件，不涉及规则 8 的 WebGPU 整型纹理问题），lint 只对该常量放行（3.1）。
4. M18-M19 PRD §8.2、M20 PRD §8 与场视图降级表：三个 D2 knob 的层级截短（3.6）。
5. AWR-04 §15.5.3 "发起点"与 D2-AC-41 方法栏："入口模块首个微任务"改为"index.html 第一个内联经典脚本"；§15.4.2 清单脚本位置（3.2）。
6. 改动落在其他模块拥有的路径（任务允许）：M00 `vite.config.ts`、`index.html`（注释）；M06 `viewport/**`、`tests/m06/lint/`；M11 `net/rt/session.ts`、`tests/m11/fakeworld.test.ts`；M18 `stores/targets.ts`；M20 `viewport/layers/tasking.tsx`。已逐一知会（第 7 节）。

### A.5 测试结果

| 项 | 结果 |
|---|---|
| `make lint` | 通过（退出码 0：ruff、oxlint type-aware、m06-lint、check-brand、check-deps、check-version、check-thresholds、shadcn postadd 幂等等）；中途一次因另一工作包正在修改的 `python/awr/sim/tasking/relay.py` 报 ruff，该包修正后全量通过，本包未改 Python |
| `tsc --noEmit` | 通过 |
| Vitest unit | 166 文件 1359 例通过（1 文件跳过，原状）；新增 `tests/m15/fxr2web.test.ts` 8 例（three 改写只应用一次、原文变化即失败；buffer 块名规范化；内联位置在 charset 之后、样式表与模块脚本之前；启动文案与 i18n 运行时逐项一致；早期模块在页面解析中发请求、行出现即写；冻结定时器；限幅事件过滤；较早预测）；改写 `tests/m15/manualControl.test.ts`（零尾后 10 Hz 保活）、`tests/m11/fakeworld.test.ts`（worker 桥接后越过保持窗口才 209） |
| Vitest browser | 21 文件 61 例通过 |
| M06 功能（`perf/m06`，测试构建） | `gpu-limits`（min 组 Tier S/B、低多边形实例矩阵 Tier S/B、D2.1 场视图 4 例）、`sensor-views` 4 例（切换不编译、LWIR 无红）、`warmup`（揭开后无新程序）：13/13 通过 |
| D2.1 GPU 上限（`perf/d21/gpu-limits-d21.spec.ts`） | 测试构建桌面、手机通过，平板首轮超时失败、重跑通过（8.0 min，load 约 13）；演示构建桌面、手机、落地页与启动屏 3/3 通过 |
| M15 功能（`perf/m15`） | `d21-shell` 9 例 + `d21-mobile` 11 例：20/20 通过；`sandbox-d2`、`smoke`、`tasks-d2` 18 例：首轮 3 例 `toHaveCount` 超时（load 约 20），重跑 18/18 通过 |
| live 诊断（非登记） | `manual-keepout` 第 1 例通过、第 2 例除水平距离断言外全部满足（见 3.6 与 M16 请求第 4 条）；`ac47-48` 平板摇杆与旋转见 3.5；`ac38-tabs` 见 3.4 |
| 性能基准 | 未跑（并行阶段）；第 1 节的时延为高负载下的诊断值 |

### A.6 遗留与风险

1. **D2-AC-01 仍可能不满足**：高负载交替 3 次揭开中位 +4.8%，安静环境下预热段已不长于 D1，剩余差距在预热之后的排练相位（ADR-076 的命令面板与 Toast 排练，D2 的壳更重，相位更常走到 800 ms 上限）与首帧。TTFP 的 D1 基线约 16 ms，D2 是否在同一帧提交点云取决于世界打开相对首个预热后帧的时序（D2 首帧的 rAF 回调更重），呈双峰（D2-ACC-1：12、110、108 ms）；"相对 D1 劣化 ≤ 5%"对 16 ms 的基线相当于同帧，本包未能确定性解决。遮罩下我方 > 50 ms 长帧以点云首屏数据在预热长任务结束后集中到达的 Worker 消息簇为主（D1 也有），高负载下 D1 3–4、修复后 2–3，须安静复测。可继续的方向：把预热按目标或按批切片让出主线程（需重新论证 ADR-071 的例程变体覆盖），或缩短排练相位的结束条件（需论证 ADR-076、ADR-081 的覆盖）。
2. **D2-AC-30**：治理器不再隐藏 D2 内容，Tier S 整景帧节奏未测；若不达 D1-AC-03b，需要再降 D2 图层自身成本（识别物面片的填充率、场视图点数）。
3. **D2-AC-34**：根因在 M20、M10，本包无改动。
4. **D2-AC-38、41、44、48** 的时延与比率判定都须安静环境复测；D2-AC-38 的计时口径待文档明确。
5. three 升级时 `threeProgramsPlugin` 会使构建失败（有意为之），须重新核对 WebGLProgram 读取的参数。

### A.7 跨所有权请求

| 文件 | 接收方 | 要点 |
|---|---|---|
| `.cache/impl/requests/d2-FX-R2-web-to-M16.md` | M16 | AC-41 计时起点随内联脚本改变；AC-38 改页内计时；登记 AC-48 平板摇杆用例与判定方式；`manual-keepout` 去 fixme、改三维距离；AC-21 命令到可见；AC-30 沙盒 flight60 变体；AC-44 GC 单列；AC-01 用例补 D1 对照与程序数 |
| `.cache/impl/requests/d2-FX-R2-web-to-文档总编.md` | 文档总编 | 第 4 节偏差 1–5 的条文修订；D2-AC-38 计时定义；D2-AC-30"整景"含敏感体 |
| `.cache/impl/requests/d2-FX-R2-web-to-M06.md` | M06 | 程序共用、fix 7、传感器目标格式、治理器下限、图表调度器的复核与安静复测 |
| `.cache/impl/requests/d2-FX-R2-web-to-M11.md` | M11 | worker setpoint 桥接的复核；手动泵整体移入 worker |
| `.cache/impl/requests/d2-FX-R2-web-to-M00.md` | M00 | `vite.config.ts` 三个插件与清单脚本位置；将来加 CSP 时的内联脚本 hash |
| `.cache/impl/requests/d2-FX-R2-web-to-M20-M10.md` | M20、M10 | D2-AC-34 的两个非 web 根因 |
