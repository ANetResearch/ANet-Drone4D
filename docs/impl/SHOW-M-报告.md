# SHOW-M 真实截图与演示动图（合成城市 synthcity）

| 项 | 内容 |
|---|---|
| 工作包 | SHOW-M：README 用真实界面截图 6 张与演示动图 1 个，只用合成城市 synthcity 与剧本 S0（不含任何 UrbanScene3D 渲染画面） |
| 日期 | 2026-10-03（17:05 至 19:50） |
| 依据 | 任务书 SHOW-M；DEMO-W 报告；D1 验收报告第 3 轮 §3–§5；FX2-R3 各报告；AWR-03 §1.3、§8.4、ADR-033、附录 E；AWR-18 §2.5、§9.5（测试开关）；M06 PRD §6.10；M07-NFR-019；M12 §6.3 |
| 环境 | VMware 8 vCPU（Xeon E5-2603 v4 1.7 GHz）、无 GPU；Node 22.12；Python 3.12（`.venv`）；Chrome for Testing 151（SwiftShader WebGL2，组合 C1）；Playwright 1.63（根 `node_modules` 已有）；Pillow 12.3（`.cache/publish/venv` 已有；项目 `.venv` 没有 Pillow）；六城与 `worlds/synthcity` 已生成。本阶段有其他工作包并行（`.cache/cleanroom` 的 pytest、shadcn 校验等），负载均值 1–11 波动 |
| 约束执行 | 没有安装依赖（项目锁文件未动）；没有执行任何 git 写操作；构建、拍摄、测试都持共享锁 `runs/.perf.lock`，**没有取排他锁**；共享的 `apps/web/dist` 只经 `make build` 重建（源码有改动），测试构建放在私有目录 `.cache/showm/dist-test`；后端用端口偏移 13（8130）、独立 run，结束后已停止；`make lint` 通过 |
| 规格变更 | 没有新 ADR，没有冻结或放宽任何阈值。M06 PRD §6.10 轨迹 CPU 历史一行补写"时间倒退"的判定容差（随本包的缺陷修复，属实现细节的澄清，不改变任何 FR/AC） |

## 1 结论摘要

| 任务 | 结果 | 证据 |
|---|---|---|
| 1 构建并加载 synthcity + S0 | 完成。`make build` 与私有测试构建（`VITE_AWR_TEST_SWITCHES=1`）；supervisor 以 `AWR_WORLD=synthcity AWR_SCENARIO=s0-synthcity-showcase` 启动（`make run` 的最后一步，`.cache/showm/backend.sh`），Playwright + Chrome 151 在 1920 × 1080、deviceScaleFactor 1 拍摄 | `.cache/showm/backend.log` 等；每个批次一个新 run |
| 1a hero-sandbox | 完成：`screenshot-hero.jpg`。完整 UI（顶栏 logo 与 ANet Drone4D、左栏世界/图层/环境、右栏 7 架机群、底部 Timeline、性能 HUD），Tier B、`?fixedB=1500000`，1,274,052 点，地标塔双段立面螺旋与街区覆盖航线，4 架带轨迹或航线 | §2、§4 |
| 1b streaming | 完成：`screenshot-streaming.jpg`，相机跳到近景后粗层 → 细化 → 完成三联，图注为各帧 HUD 读数（点数、加载比例、在途、受点预算限制） | §4.2 |
| 1c swarm | 完成：`screenshot-swarm.jpg`，三机 V 形编队掉头（轨迹）与覆盖机割草机航线（任务图层） | §4 |
| 1d weather | 完成：`screenshot-weather.jpg`，同一视角小雨 → 起雾过渡 → 雾，能见度 6.0 km / 690 m / 150 m | §4 |
| 1e fpv / 跟随 | 完成：`screenshot-follow.jpg`，Third 追尾跟随编队长机（FPV 候选画面只有近处立面，未采用） | §4 |
| 1f perf | 完成：`screenshot-perf.jpg`，默认配置（无测试开关、自动 Tier S）下 S0 实时运行的 Perf 面板全部 lieflat 卡片 | §4、§5 |
| 1g 暗色 | 全部暗色（`colorScheme: dark`，产品默认） | — |
| 2 演示动图 | 完成：`demo-flight.webp`，960 × 540、200 帧、20 fps、10.0 s、5.76 MiB（≤ 8 MB），确定性逐帧（服务器 `sim/step` 0.1 s + 固定相机位姿）；GIF 试算 35.5 MB 超限未保留 | §3、§4.3 |
| 3 产出与记录 | 7 个文件在 `docs/media/`，JPEG 每张 302–454 KB（≤ 900 KB）；复现步骤、URL 参数、相机位姿与读数在 `.cache/publish/screens.md` | §2 |
| 拍摄中发现的缺陷 | **暂停或单步时轨迹消失**（M06 TrailRing 的 Float32 时间比较），已修复并加单测 | §3 |

## 2 产出清单（`docs/media/`）

| 文件 | 尺寸 | 体积 | 内容与关键读数 |
|---|---|---|---|
| `screenshot-hero.jpg` | 1920 × 1080 | 454 KB | T+129.96 s（暂停），小雨；Orbit eye (640, −520, 430) → (90, 40, 105)；1.27M / 1.50M 点，已达目标精度；选中 h1、h2、c1、c2 |
| `screenshot-streaming.jpg` | 1920 × 613 | 302 KB | 724K 点 52% 在途 4 → 1.04M 65% 受点预算限制 → 1.50M 100% 在途 0 |
| `screenshot-swarm.jpg` | 1920 × 1080 | 453 KB | T+129.96 s；编队 f1–f3 在河道东端掉头，c1 覆盖航线；1.27M 点 |
| `screenshot-weather.jpg` | 1920 × 613 | 308 KB | T+147.9 / 169.9 / 191.9 s；MOR 6000 / 691 / 150 m；雨强 k 0.233 / 0.027 / 0 |
| `screenshot-follow.jpg` | 1920 × 1080 | 449 KB | Third 跟随 f1（焦点低延迟），三机标签与轨迹；1.32M 点 |
| `screenshot-perf.jpg` | 1920 × 1080 | 345 KB | Tier S；T+76 s；呈现间隔 p50 133.3 / p95 200.0 ms；B 10.0K（soft-min，降级阶梯第 7 步） |
| `demo-flight.webp` | 960 × 540 | 5.76 MiB | T+118.0–137.9 s，每帧 0.1 s（2 倍速播放），相机绕地标塔区转 28°；每帧约 849K 点（`?fixedB=1000000&chrome=0`） |

导出：JPEG quality 95、progressive、4:4:4 色度（与 `images.md` 的选择一致：细红线与品牌红不糊）；animated WebP quality 80、method 6、每帧 50 ms、无限循环（回读 ANMF 块：200 帧合计 10,000 ms）。脚本 `.cache/showm/export.py`。

## 3 拍摄中发现并修复的缺陷：暂停或单步时轨迹消失（M06）

**现象**：S0 播放到 T+108 s 后暂停，选中机的轨迹只剩机体处一小段；再单步 12 次（每次 1 s），7 架机的轨迹历史行全部只剩 1 个样本（`window.__vp.vpSession.drones.layer.trails.count` = `[1, 1, 1, 1, 1, 1, 1]`，暂停前为 33 左右）。任何用户暂停仿真、单步、`?simTime=&paused=1` 钉时刻或回放暂停，轨迹都会消失，与 M06-FR-042（全部在场机体 256 样本历史）不符。

**根因**：`engine/drones/trails/TrailRing.ts` 以相对块起点的 Float32 存储样本时间，追加时以 `dt = t − 最新样本时间 < 0` 判定"时间倒退（seek、回放）"并清空该行。暂停时每帧都以同一 tRender 追加，而存储值经 Float32 舍入后可能比原值略大。实例：块起点 6.44 s，t = 108.104 s，相对值 101.664 存成 101.66400146484375，于是 dt = −1.46 × 10⁻⁶ < 0，**每一帧**都把该行清成 1 个样本。是否触发取决于舍入方向（130.096 s 时为正、108.104 与 143.096 s 时为负），因此有时表现为"暂停后轨迹偶尔还在"。播放时相邻帧的时间差远大于舍入误差，所以平时看不出来。

**修复**：`TRAIL.backS = 1e-3`，只有早于最新样本超过 1 ms 才算倒退（块内 2 h 处 Float32 半个 ulp 约 0.24 ms；seek 与回放倒退都远大于 1 ms）。`tests/m06/trails.test.ts` 新增用例：同一时刻重复追加 10 次不清行、恢复播放正常追加、倒退 8.6 s 仍清行。修复后在同一流程下单步到 T+107.9 s，各行 93–94 个样本，轨迹完整（`screenshot-swarm.jpg`、`screenshot-hero.jpg`、动图都依赖这一点）。M06 PRD §6.10 轨迹 CPU 历史一行补写了这条判定。

## 4 拍摄方法

### 4.1 确定性时刻（不用 `?simTime`）

S0 开局自动播放。页面连上后经 Timeline 暂停（`sim/pause`）停住服务器时钟，此后只用 `sim/step`（1 s = 250 tick、0.1 s = 25 tick）前进；单步 ≤ 1 s 时客户端以 StepGlide 平滑前进，轨迹照常每 0.5 s / 5 m 采样。推进期间隐藏点云（本机 Tier B 下帧间隔由约 1–2.7 s 降到约 0.1 s），到达后再打开，等流式静止（在途、排队、待上传全为 0，绘制点数连续 3 次采样不变，相机静止）后截图。
任务书建议的 `?simTime=<t>&paused=1` 只把 tRender 与 tFocus 钉在 t（AWR-18 §9.5），机体位姿仍从插值环取（每机 32 个样本，约 3 s），回不到更早的时刻，不能逐帧推进 20 s 的飞行；用服务器单步得到的是同样确定的画面，而且时间、位姿、轨迹、环境都来自同一仿真。

### 4.2 测试开关与画面设定

- 沙盘、编队、跟随、天气、渐进加载：`?tier=B&fixedB=1500000`；动图：`?tier=B&fixedB=1000000&chrome=0`（960 × 540）；Perf 面板：无开关。都在 AWR-18 §9.5 登记。本机 SwiftShader 自动判为 Tier S（ADR-044），默认点云在 S0 下会被 PerfGovernor 压到 1 万点，城市呈块状，所以展示图用强制 Tier B 加锁定预算；HUD 上"测试开关强制档位"徽标如实显示。
- 图层偏好经 `localStorage`（`awr.layers.v1`）在页面加载前预置：视锥与禁飞区关，其余开；画质 high；着色"高度"。
- **渐进加载**：冷启动首屏阶段（21% → 58%）只有加 `?reveal=shell`（M15 测试构建开关，未在 AWR-18 §9.5 登记）才能拍到；只用已登记开关时本机启动遮罩在加载 44%–58% 之间才揭开（`raw/d/stream-00…02` 仍是遮罩画面）。因此入库版改为"首屏加载完后相机跳到近景"的细化过程：先画已驻留的粗层节点（大点径、EDL 粗轮廓），再逐步细化到预算上限，图注取自 HUD。`?reveal=shell` 版作为备选母版保留在 `.cache/showm/final/streaming-b-revealshell.png`，未入库。

### 4.3 演示动图

预滚：隐藏点云，1 s 单步到 T+117.9 s 再 0.1 s 单步到 T+118.0 s（轨迹累积约 90 s 历史）。第 k 帧（0…199）：单步 0.1 s → 等 |tRender − tSim| < 2 ms → 相机设为固定弧线上的第 k 个位姿（目标 (120, 0, 90) → (115, −30, 85)，方位 −44.4° → −72°，半径 615 → 660 m，高 300 → 285 m，smoothstep 缓动）→ 等流式静止 → 截图。200 帧墙钟 29 min（约 8.7 s/帧，主要是 SwiftShader 每帧 2–3 s 与静止判定）。先试拍过一版以塔为中心的近距离弧线，塔顶出画、编队不在画内，作废后按 5 个候选位姿的试拍选定现在的弧线（`.cache/showm/try/`）。

### 4.4 逐张检查

每张都用图像查看器逐张检查（含 2–4 倍裁切）：无 emoji（UI 图标为 morphicons 图标集，标签为中文"飞行""返航"等文字）、无报错横幅与 Toast（截图前关闭全部 Toast）、文字清晰、构图完整。剔除的候选与原因：

| 候选 | 原因 |
|---|---|
| 第一轮全部机群图（修复前） | 轨迹被清空，只剩机体处短段（§3） |
| FPV（h1、h2、c1） | 视场里只有近处立面或地面，看不出机群与城市；改用 Third 跟随 |
| 跟随锁定的 Orbit 近景 | 塔体占满画面，机体与轨迹太小 |
| 天气：街谷低机位、塔下近景 | 雨丝在 SwiftShader Low 档下只有零星几条，过渡不明显；改用三联（雾的变化清楚） |
| Perf 第一次拍摄 | 只读席位（前一页面仍占席位），仿真没播放；第二次拍摄时本包 WebP 导出与其他工作包并行，p95 583 ms 不具代表性；第三次在 load 1.56 时重拍 |
| 动图第一版 | 塔顶出画、编队不在画内 |

## 5 实测与观察（本机无 GPU，仅供参考，不作性能判定）

| 项 | 读数 |
|---|---|
| Tier B、约 0.7–1.5M 点、1920 × 1080 的帧间隔 | HUD p95 约 0.9–3.9 s（各截图左下角）；隐藏点云时约 8.9 帧/s，显示约 49 万点时约 2.4 帧/s |
| Tier B 锁定 1.5M 预算，冷启动首屏到完成 | 190,507 点首屏后约 29 s 到 1,274,638 点（`limitedBy = headroom`；他包并行、负载高时 50 s）；相机跳到近景后约 50 s 到 1.5M（`budget`） |
| 默认 Tier S、S0 ×1 播放 | 揭开后约 50 s 内 PerfGovernor 走完 7 步，B = 1 万（soft-min），呈现间隔 p50 133.3 / p95 200.0 ms（load 1.56 开拍、运行中他包并行）；与 D1 验收第 3 轮 §4.2 第 3 条同一现象 |
| 操作席位 | 浏览器上下文关闭后，新页面 90 s 内仍是只读（seat none、viewer），只读页面不能暂停或播放 |
| 启动遮罩 | Tier B 锁定 1.5M、冷启动时本机遮罩在加载 44%（31.7 s）与 58%（37.8 s）两次采样之间揭开 |

## 6 测试

| 项 | 结果 |
|---|---|
| `npx vitest run --project unit tests/m06/trails.test.ts` | 5 通过（新增 1 例） |
| `npx vitest run --project unit tests/m06` | 31 个文件、104 通过 |
| `make build`、测试构建 | 通过（修复后重建） |
| `make lint` | 通过（ruff、oxlint、no-emoji 等全部规则） |

## 7 文档变更

- `docs/modules/M06-Web视口与渲染后端PRD.md` §6.10 轨迹表"CPU 历史"：补写倒退判定容差（> 1 ms 才清行）与原因。
- `.cache/publish/screens.md`（新）：每张图的 URL 参数、相机位姿、时刻、图层、读数、裁切与合成命令、一键导出与 README 引用建议。

## 8 遗留与建议

1. **README 尚未引用这些图**：任务书只要求产出到 `docs/media/` 与记录复现步骤，README 的排版留给发布展示的工作包；`screens.md` §5 给出引用片段与图注建议（应写明"程序生成的合成城市、无 GPU 软件渲染机器上截取"）。
2. **`?reveal=shell` 未登记**：M15 的这个测试构建开关不在 AWR-18 §9.5 表中；如果以后需要冷启动首屏的展示或测试，建议在 §9.5 登记。
3. **操作席位保留时间**：关闭页面后约 90 s 以上新页面才能取得席位，自动化脚本若在同一后端上换页面要等待或重启后端（M11）。
4. **Tier S 默认画面**：S0 下 PerfGovernor 很快放开 B_floor，城市只剩 1 万点（D1 验收第 3 轮已记录）；展示用图因此全部用强制 Tier B + 锁定预算，GPU 机器上的默认画面会好得多，建议以后在有 GPU 的机器上补拍一组默认配置截图。
5. **雨的可见度**：SwiftShader Low 档下雨丝很稀，天气图主要靠雾的变化；Med 档交付后（M07 D1-ext）可重拍。
6. **GIF**：960 × 540、200 帧、相机运动的 GIF 即使 128 色也有 35.5 MB；如需 GIF，建议 480 × 270、10 fps 另做一版。

## 9 文件清单

修改：`apps/web/src/engine/drones/trails/TrailRing.ts`、`apps/web/tests/m06/trails.test.ts`、`docs/modules/M06-Web视口与渲染后端PRD.md`。
新增：`docs/media/screenshot-{hero,streaming,swarm,weather,follow,perf}.jpg`、`docs/media/demo-flight.webp`、本报告。
不入库的过程文件：`.cache/publish/screens.md`；`.cache/showm/`（`backend.sh`、`restart.sh`、`pwserve.cjs`、`pwlib.py`、`capture_{a,b,c,e}.py`、`export.py`、`compose/`、`raw/` 母版与 `meta*.json`、`final/`、`dist-test/`、各日志）。
