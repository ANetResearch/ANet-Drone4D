# D2-INT 集成报告（D2 + D2.1，第 2 轮）

| 项 | 内容 |
|---|---|
| 工作包 | D2-INT 集成：D2（WP-01 至 WP-16）与 D2.1（WP-03b、WP-04b、WP-11b、WP-12c、WP-13b、WP-16 扩展、WP-19b）在 D2 验收第 1 轮（D2-ACC-1）、D2-FX-R2 七个修复区与 WP-11b、WP-12c、WP-13b 第 2 次执行（2026-10-11）之后的第 2 轮集成与功能验收 |
| 日期 | 2026-10-11（第 1 轮为 2026-10-06，与第 1 轮的差异见第 8.4 节；第 1 轮全文已被本文取代） |
| 依据 | 用户原文 `docs/inputs/D2-需求原文-2026-10-05.md`、`docs/inputs/D2.1-需求原文-2026-10-05.md`；AWR-04 v1.3（§13 D2-AC-01 至 39、§14 工作包与路径所有权、§15 D2.1 与 D2-AC-40 至 55）；17 §17、§17.20；M17、M18-M19、M20 PRD 与 M08/M10/M11/M13/M15 的 D2 增补；D2 验收第 1 轮报告、D2-FX-R2 各区报告、全部 D2 与 D2.1 工作包报告；`.cache/impl/requests/d2-*.md` 全部 237 份请求（WP 182、ACC-1 13、FX-R2 38、D2-INT 第 1 轮 4） |
| 环境 | VMware 8 vCPU、62 GiB、无 GPU；Chrome for Testing 151（SwiftShader，Tier S，C1）；Playwright 1.63；Node 22.12；Python 3.12（`.venv`）；本机（非 cgroup 模拟）。05:30–09:50 同机另有与本工作包无关的研究任务（`.cache/research/d3`，约 7 个工作线程与一个 Playwright 浏览器）运行，负载 8–25；之后 1.5–10 |
| 约束执行 | 未安装任何依赖；未执行 git 写操作（目录不是 git 仓库）；未运行性能基准（`perf` 标记、`M16_PERF=1`、cgroup 模拟、浸泡与 24 h 批处理都未跑，时间阈值只在功能口径判定）；无 emoji 与禁用字形；新增界面文案只加 i18n 键、未加组件与颜色；内部口令只存在于测试运行时随机生成的临时密钥文件（`tests/fixtures/identity.py`、`live21.makeSecretFile()`），仓库、前端、文档、日志与本报告不含任何口令；新渲染代码无（本轮前端改动不涉及着色器与绑定）；`make lint` 在最后一次修改后通过 |
| 越权修改 | 本工作包获授权修改任何路径以修复集成问题；全部越权修改逐文件列在第 5 节（所有者与原因） |

## 1 结论摘要

1. **门禁全部通过**：`make lint`、`make test-contracts`、`make build` 与演示构建（prod-bundle-scan 与 REL-04 扫描 clean）、`make validate`（七个世界 0 错误）、
   vitest（unit 1374、browser 62）、全量 `pytest -m "not perf"`（最终一轮 4257 passed；负载 17–25 下失败的 11 例全部为计时与超时，负载回落后按文件
   复跑全部通过）、七城 42 个场景模板 180 s 批处理 42/42、Playwright 全部 D2 与 D2.1 功能用例与 D1 回归组（第 2 节）。
2. **本轮修复的集成缺陷 9 项**（第 3.1 节），其中两项 P0 或用户主路径：①观看席位满载时节能恢复永远轮不到（页面以另一 principal 排队，D2-AC-43、44）；
   ②界面放置识别物后拿不到新 id（网关丢弃插件命令的 result）。另有：公开站身份令牌读审计与连接检查 403（D2-AC-51）、suzhou T5 区域值守整体
   583、chicago T3 转场穿楼 102、shenzhen 与 chicago 的 T1/T6 无捕获（D2-FX-R2 之后的回归，多旋翼到站不转机头）、循环剧本重置后位姿停发、
   节能期间的动画采样器与挂起间隙。并处理了 WP 第 2 次执行与 FX-R2 发来的可在集成中落实的请求（第 3.2、6.1 节）。
3. **新发现的 P0 遗留**：沙盒驻留后恢复（含空闲驻留、抢占与 L3 CPU 回收）会丢失任务与接力状态——任务回到 PLANNING、接力统计 404、空中机体不在
   任何架次中。根因是 M20-FR-075 的 checkpoint 扩展段从未实现（M08 登记点未提供）；D2-AC-45 因此为"部分"，修复需要 M08 与 M20 共同完成
   （`d2-INT-to-M08-M20.md`，第 9.2 节第 0 条），规模超出集成修复，未在本轮实现。
4. **S7**：完整 S7（AWR-V1 值守 → 接力）在批处理 3600 s 中两个接力各交接 2 次、被发现 0；在真实沙盒中完整 S7（治理器按成本授予 ×1）40.8 min 内
   完成 2 次交接、被发现 0，接力部分以 ×2 快进 20.2 min 完成 2 次交接、被发现 0（第 8 节）。第 1 轮的"AWR-V1 值守周期不可用"已不成立。
5. **本机全栈演示**：public profile v2（共享展示 S7 + sandbox-pool + sandbox-api，N_max = 3）上 3 个并发沙盒（两个 SDK 用户、一个平板指挥器）+ 1 个排队访客，
   手机与平板截图均已查看（第 7 节）。
6. **验收（第 4 节）**：D2-AC 55 项中通过 27、功能通过 18（性能子项待 ADR-033 复测）、部分 4（D2-AC-19、34、35、45）、未执行 6（D2-AC-04、20、27、39、46、55）。
   D2 退出标准目前不满足，主要阻塞为：识别物 stage 成本（约 290 ms/仿真秒，预算 3–4）、驻留恢复的编排状态、T5 首次捕获与资源、霾时段接力间隙、
   以及全部性能子项的安静环境复测（第 9 节）。

## 2 门禁与测试执行结果

全部在最后一次相应修改之后运行。最后一次全量 pytest（07:52 起）之后的 Python 修改为：`sim/tasking/executor.py` 的多旋翼定点转向（第 3.1 节第 9 项，
复跑 `tests/tasking`、`tests/mission`、`tests/e2e/test_sandbox_templates.py` 289 passed 与七城 42 模板）、`tests/e2e/test_sandbox_park.py` 的比较字段（复跑 8 passed）
与 `tools/bench/fleet_ladder/run.py` 的记录项（ruff 通过）。同机负载见表头"环境"一行。

| 项 | 命令 | 结果 |
|---|---|---|
| lint | `make lint` | 通过（最后一次修改后重跑；M16 用例登记 142 个；生成物 `--check` up to date；`check-cn-keys` ok） |
| 契约 | `make test-contracts` | pytest 434 passed、3 skipped；vitest 8 文件 281 passed（rpc 修改之后重跑） |
| pytest 全量（最终） | `pytest -m "not perf"`（07:52–09:53，同机负载 17–25，与真实沙盒 S7 用例并行） | 4257 passed、88 skipped、62 deselected、11 failed；11 例全部为负载下的计时或超时：`tests/e2e/test_capacity_e2e.py` 3、`test_demo_check.py` 2（计时 24.9 s）、`test_field_sampling_e2e.py` 1、`test_identity_e2e.py` 2（`test_r79_constant_time` 等）、`tests/runtime/test_supervisor.py::test_descendants_reaped_after_child_killed`、`tests/sandbox/test_park_pool.py::test_park_and_resume_from_checkpoint`、`tests/sensors/test_pose_chain_e2e.py` 1。负载回落后按文件复跑：`test_park_pool.py`、`test_supervisor.py`、`test_pose_chain_e2e.py`、`test_demo_check.py` 31 passed；`test_capacity_e2e.py`、`test_field_sampling_e2e.py`、`test_identity_e2e.py`、`test_identity_public_e2e.py` 27 passed；`test_sandbox_park.py`（改字段后）、`tests/tasking/test_decompose.py`、`tests/rt/test_rpc_entity_result.py`、`test_identity_read.py` 35 passed |
| pytest 全量（基线） | 同上（05:26–06:56，负载 3–16） | 4255 passed、88 skipped、2 failed（`tests/targets/test_wire.py::test_lite32_and_detail`、`tests/sensors/test_pose_chain_e2e.py`，单独复跑 2 passed；同两例在 WP-11b 与 WP-12c 的全量中同样失败，见第 9.5 节） |
| SDK 全流程 | `tests/e2e/test_sandbox_sdk_e2e.py`（全量内） | 通过（创建、排队、工作量证明、实体增删改、任务、租约速度控制、§11.7 字段、订阅、快照、场景二示例脚本） |
| vitest | `npx vitest run --project unit` / `--project browser`（最后一次前端修改之后，12:00 复跑） | unit 167 文件 1374 passed、1 skipped；browser 21 文件 62 passed |
| 构建 | `make build`、测试构建（`perf/m15/helpers/build-ui.mjs`，`ci` 通道）、演示构建（`VITE_AWR_DEMO=public vite build`） | 全部通过；`make build` 的 prod-bundle-scan clean；演示构建以运行时随机口令文件扫描（REL-04）clean，`/api/auth/identity` 只在 1 个分块 |
| 世界包 | `make validate`（`worldpkg validate --deep` 与 Ajv strict） | 七个世界 errors 0、warnings 0；82 份文档 0 invalid |
| 模板批处理 | `tools/sandbox/check_templates.py --until 180`（7 世界 × T1–T6，最后一次 Python 修改之后） | **42/42 无错误**（本轮修复前 40/42：chicago T3 `follow_path 102`、suzhou T5 `INFEASIBLE 583`）；首次捕获【仿真】T1 七城 53.5–62.1 s、T6 七城 53.2–61.4 s（修复第 3.1 节第 9 项之前 shenzhen 与 chicago 的 T1、T6 180 s 内无）、T5 71–160 s（chicago 无）、T3 四城 146–169 s |
| S7 批处理 | `tools/sandbox/s7_check.py --until 3600 --start-local 13:00` | 两个接力各交接 2 次、被发现 0（第 8.1 节）；168 h 可持续性"否" |
| Playwright | 见下表 | 全部通过；首跑失败的 4 例为用例缺陷或负载（`integration-live` 识别物例：发现第 3.1 节第 2 项产品缺陷，修复后通过；S7 例：用例 token 过期，修正后通过；e2e `honesty` 第 1 例：D2 布局下 1280 px 时左栏收起，改为 1600 px 后通过；e2e `a11y`：负载下揭开超时，复跑通过） |

Playwright（Chromium 151、SwiftShader；测试构建 `.cache/d2int/test`，演示构建 `.cache/d2int/demo`，`make build` 的 `apps/web/dist`；真实栈用例经
`tests/e2e/sbstack.py`；全部在最后一次前端修改之后的构建上运行）：

| 用例 | 结果 | 说明 |
|---|---|---|
| `perf/skeleton.spec.ts` | 4 passed | D1-AC-34、35 |
| `perf/m06/gpu-limits.spec.ts` | 14 passed（24.1 min） | 测试、演示构建 × Tier S/B × 最低（24/12/12/24/16 384）与 mid 上限，实例矩阵 2 例，D2.1 场视图 4 例（桌面 min、手机与平板 mobile 组）：违例 0、gl 0、three 0、错误 0；绑定点 3、单程序块 V 3 / F 2；揭开后 programs 27 → 27；mobile 组 `pointSizeMode = quad` |
| `perf/d21/gpu-limits-d21.spec.ts` | 6 passed | D2.1 页面在最低与移动上限下（含演示构建与落地页） |
| `perf/d2/gpu-limits-d2.spec.ts` | 1 passed | 整景 |
| `perf/m06/field-views.spec.ts`、`sensor-views.spec.ts` | 6、4 passed | 场视图五档、触控手势与 FPV 拖动；三种传感器视图 |
| `perf/d2/boot-progress.spec.ts` | 2 passed | 进度条 |
| `perf/d21/boot-live.spec.ts` | 3 passed | 版本行、五段进度条、载入期排队可见（页内计时：请求早于主分块） |
| `perf/d21/seats-live.spec.ts` | 1 passed | 观看席位满载：节能释放、观看排队、载入时"实时观看已满 · 第 2 位"、自动连接（P0 修复后） |
| `perf/d21/power-boot.spec.ts`、`power-live.spec.ts` | 1、3 passed | 节能：揭开前不计时；viewer 倒计时、节能 10 s 内 0 个请求、拖动恢复；沙盒驻留、恢复排队与认领；演示构建 |
| `perf/d21/identity-live.spec.ts` | 4 passed | 身份切换：折叠入口、错误口令、正确口令徽标、内部档沙盒预留槽位、撤销、锁定；`identity-backend = show-api` |
| `perf/d21/field-live.spec.ts` | 3 passed | 读数卡逐行等于 SB-73、场视图五档与 Shift+V、实时场帧 |
| `perf/d21/mobile-live.spec.ts` | 11 passed | 手机与平板设备模拟（iPhone 15 竖横、Pixel 8、iPad Pro 11 竖横）、演示构建手机 viewer、快捷指令悬停、返航、盯防、平板任务条与虚拟摇杆（旋转归零） |
| `perf/m15/sandbox-d2.spec.ts`、`d21-shell.spec.ts`、`d21-mobile.spec.ts`、`tasks-d2.spec.ts` | 5、9、11、6 passed | 界面（FakeSource） |
| `perf/m18/targets-panel.spec.ts`、`manual-keepout.spec.ts` | 2、2 passed | 识别物 REST 增删改与删除后离开面板；手动控制与软围栏（fixme 去掉） |
| `perf/m19/capture-toast.spec.ts`、`hud.spec.ts` | 3、1 passed | 明确捕获 Toast 与 HUD |
| `perf/d2/sandbox-live.spec.ts` | 3 passed | 开沙盒全流程（Sheet → 首次捕获 Toast）、`/sandbox/api`、手动控制（≥ 25 Hz、倍速锁定、释放） |
| `perf/d2/demo-live.spec.ts` | 2 passed | 角色切换：落地页两次点击进入沙盒（operator 徽标）、共享展示 viewer 无写请求 |
| `perf/d2/integration-live.spec.ts` | 底图、七类任务、识别物、S7 各 1 passed（S7 完整剧本 40.8 min、接力部分 20.2 min，第 8.2 节） | 底图七世界经会话条切换 5.8 min；七类任务在 T5 会话中运行并列入 Dock；识别物界面放置与删除（第 3.1 节第 2 项修复后） |
| e2e 组（`tests/e2e/*.spec.ts`） | honesty 4、a11y 1、brand 1、interaction 1、motion 2、timeline 3、ms-smoke 3、demo_rehearsal 1 passed，sanitize 1 skipped（按设计） | D1 回归；honesty 第 1 例改视口后通过（d2-FX-R2-deploy-to-M16 第 2 条），a11y 首跑负载下超时、复跑通过 |
| 本机演示（`.cache/d2int/demo-live/`，非仓库用例） | 1 passed（3.1 min） | public profile v2，3 个并发沙盒 + 1 个排队访客 + 手机与平板（第 7 节） |


## 3 D2-INT 第 2 轮的集成修复

### 3.1 缺陷修复

| # | 问题（发现方式） | 根因 | 修复 | 证据 |
|---|---|---|---|---|
| 1 | **观看席位满载时节能恢复永远轮不到（P0，D2-AC-43、44）**（WP-13b 第 2 轮 `seats-live` 在真实栈发现，d2-WP-13b-to-M15 第 10 条） | 页面以沙盒的 32 位十六进制提示发 R81，且 `principalHint()` 覆盖了 D1 页面 token 写入共用键 `awr.principal_hint` 的 base32 提示；show-api 只认 base32，给票据一个随机 principal，`seat_ready` 的预留与页面实时 token 永不相符 | M15 `ui/sandbox/session.ts`：已有提示为 base32（16–64）或 32 位十六进制时原样复用、从不覆盖，新生成 26 位 base32；M11 `api/security.py`：`HINT_RE` 同时接受两种形式（与沙盒接口同一正则），共享展示、观看排队与沙盒共用一个 principal | `perf/d21/seats-live.spec.ts`（条件 `test.fail` 改为断言）真实栈 1 passed：A 节能 → B 占席 → A 观看排队第 1 位 → C 载入时"实时观看已满（1 / 1）· 进入后排队，第 2 位" → B 离开后 A 自动连接；`tests/m15/sandboxSession.test.ts`、`tests/rt/test_identity_read.py` |
| 2 | **界面放置识别物后拿不到新 id，详情页不打开（D2-AC-11 界面部分）**（D2-INT 新增用例发现） | M08 `_plugin_op` 把插件命令处理者的 `result`（`target/add` 的 `{id, cfg}`）放在受理回复 `detail.result` 与终态事件 `cmd.succeeded` 的 `data.result`；M11 `rt/rpc.py` 只转发状态，丢弃了 result | `rt/rpc.py`：受理回复与终态事件中的 result 并入 `result` 帧的 `data`（受理帧与终态帧都带）；没有 result 的机体指令不变。界面的 `targetsModel.setConfig` 与 M20 `task/create` 的 `tid` 同样由此生效 | `tests/rt/test_rpc_entity_result.py` 3 例；`perf/d2/integration-live.spec.ts`"识别物"例：类别菜单 → 视口点击放置 → 详情页打开 → 删除（确认）→ REST 与面板都去掉 |
| 3 | 公开站带身份令牌的 GET 在 `/api/sys/audit`、`/api/rt/inspect` 上 403 `115`（d2-WP-13b-to-M11 第 7 条，D2-AC-51"隐藏 REST 功能族 200"） | 公开策略已放行，路由自身的 admin 依赖不认身份令牌 | M11 `api/deps.py` 新增 `identity_read()`，`require_role("admin")` 对身份令牌的 GET、HEAD 按 admin 放行（写入仍按 token 角色）；`rt/inspect.py` 同口径 | `tests/rt/test_identity_read.py` 6 例；`tests/e2e/test_identity_public_e2e.py` 去掉 `KNOWN_403` 与 xfail |
| 4 | **suzhou T5 区域值守整体 `INFEASIBLE 583 keepout`**（七城模板 180 s 批处理，42 中唯一 INFEASIBLE；D2 验收第 2 轮未完成的功能记录中也有） | 区域贴近世界北缘，静止与游走两个识别物的运动包络禁入圆（170、228 m）把巡边环的南北两边都盖住，绕行只能越出世界边界内缩带，`ring_detour` 无解即整个值守 583 | M20 `tasking/decompose.py`：值守在巡边被阻断时去掉巡边段并告警 `perimeter_blocked`（i18n 已加），保留区域巡查与看洞；单独的 `perimeter_patrol` 仍 583 | suzhou T5 首次捕获 71.1 s【仿真】并转入接力；`tests/tasking/test_decompose.py` 新增一例 |
| 5 | **chicago T3 每次转场一条 `cmd.failed follow_path 102`**（D2-AC-34 第 2 子项，第 1 轮起的遗留） | 多旋翼转场高度封顶 DTM + 160 m，chicago 高楼高于封顶；M20 `route()` 只对固定翼绕高楼，多旋翼的转场航线在 159 m 穿楼，M08 细校验 `PATH_OBSTACLE` 拒绝后再退回 M10 自动 goto | M20 `sim/tasking/executor.py`：多旋翼也使用高楼障碍圆（余量 `TALL_MR_M` 10 m，无解退回只绕禁入圆）；封顶后走廊仍高于航线（`route_blocked`）时直接下发 M10 自动航线，不再先发必被拒的 follow_path | chicago T1–T6 180 s 全部无错误；七城 42 个模板见第 2 节 |
| 6 | 循环剧本每轮重置后视锥位姿（SensorPose48）与 nav 停发一整轮（d2-FX-R2-other-to-M13 第 5 条） | `next_due` 不随仿真时间回退 | M13 `pose_pack.py`、`nav_pub.py`：仿真时间回退时重新按 10 Hz 对齐（同 FX-R2 对 PayloadPub 的处理） | `tests/sensors/test_payload_scale.py` 新增一例 |
| 7 | 节能期间仍有 1 Hz 的动画采样器（d2-WP-12c-to-M06 第 5 条，D2-AC-44/46 主线程占比的最后一项周期任务） | `viewport/animationSampler.ts` 不看节能冻结信号 | M06：订阅 `lib/powerFreeze`，冻结时取消待执行的定时器与 idle 回调，恢复时立即采样一次 | `tests/m06/animationSampler.browser.test.ts` 新增一例 |
| 8 | 挂起间隙计入呈现间隔（d2-WP-12c-to-M06 第 6 条） | 帧钩子把恢复后的第一帧（以及按需帧之间）的数秒间隙记入 `frame.interval` | M06 `engine/perf/index.ts`：挂起中与挂起切换时 `frame.lastNow = -1` | vitest 全量 |
| 9 | **shenzhen 与 chicago 的 T1 定点盯防、T6 在 180 s 内无捕获**（本轮七城模板批处理发现；第 1 轮七城都在 54–70 s 捕获，属 D2-FX-R2 之后的回归；影响 D2-AC-14、D2-AC-34 访客路径与 Sheet 的"首次反馈"） | 识别物在站位西侧（方位 175–179°）的两个世界失败、其余五个成功：FX-R2-tasking 起短距离转场改为直飞 goto，多旋翼保持起飞时机头朝东；到站后只把云台指向识别物，云台方位限位约 ±160°，识别物在机身正后方时够不到（实测云台方位停在 160°） | M20 `sim/tasking/executor.py`：多旋翼非接力定点到站时以当前位置 goto 带 `yaw_rad`（朝盯防点）代替 hover；接力站位与固定翼不变 | 七城 T1 53.5–62.1 s、T6 53.2–61.4 s【仿真】；`tests/tasking`、`tests/mission`、`tests/e2e/test_sandbox_templates.py` 289 passed；`perf/d2/sandbox-live.spec.ts` 3 passed（复跑） |

### 3.2 处置请求时落实的其余改动

| 请求 | 落实 |
|---|---|
| d2-WP-12c-to-M17 第 1 条（容量 store 的生产接管 API） | M17 `stores/capacity.ts` 新增 `adoptBootCapacity(promise)`（不重置 store、定时器与租约），M15 `app/boot/capacity.ts` 改用它；`tests/m17/capacity.test.ts` 新增一例 |
| d2-FX-R2-deploy-to-M17 第 1 条（会话重启计数） | M17 `sandbox/pool.py` 运维状态行增加 `restarts`；M00 `tools/deploy/emax/observe.py` 把会话重启增量计入"非预期重启"（`tests/ops/test_deploy_observe.py` 18 passed；`tests/sandbox/test_pool.py` 增加断言） |
| d2-WP-13b-to-M17 第 1 条（正式身份夹具） | `tests/sandbox/test_rest_park.py`、`test_identity_lib.py` 改用 `tests/fixtures/identity.py`，删除 `tests/sandbox/identitykit.py` |
| d2-WP-11b-to-M15 第 9 条 | 删除 `styles/mobile.css` 中与 M06 `ViewCube` 同值的 coarse 覆盖规则 |
| d2-FX-R2-web-to-M16 第 4 条、d2-ACC-1-to-M16 第 5 条 | `perf/m18/manual-keepout.spec.ts` 去掉两例 fixme，第 2 例改用三维距离（声学体为 120 m 半球），真实栈 2 passed |
| d2-FX-R2-sim-to-M17 第 2 条 | `tools/sdk/README.md` 写明 `retry_after_s` 取自 `retry_after_ms`、只用 REST 的会话的无连接驻留与 515 自动恢复 |
| d2-FX-R2-sandbox-to-M11-M10-M00 第 10 条 | `tools/deploy/emax/README.md` 补会话父组 `sandbox/sb/` 的 `cpu.max` 说明 |
| d2-INT-to-文档总编 第 3 条（第 1 轮） | `tools/contracts/sandbox_api.py`、`gen_openapi.py` 的说明改为 SB-00 至 SB-78（快照文件未重生成，描述串不变） |
| D2-INT 用例 | `perf/d2/integration-live.spec.ts`：S7 例改为完整 S7（AWR-V1 值守 → 接力，`D2INT_S7_RELAY_PART=1` 保留第 1 轮的接力部分）、每轮取页面当前 token（创建时的 token 在 30 min 会话寿命后过期，读取全为 401）、页面以 `viewport=off` 打开（`D2INT_S7_VIEWPORT=1` 保留画布）、墙钟预算 `D2INT_S7_MIN`；新增"识别物界面放置与删除"例；`tests/e2e/sbstack.py` 命令行增加 `--profile`（本机 public profile v2 演示） |
| d2-FX-R2-deploy-to-M16 第 2 条 | `tests/e2e/honesty.spec.ts` 第 1 例以 1600 × 900 打开（D2 布局 1280 px 时左栏收起），4 passed |
| `tests/e2e/test_sandbox_park.py` | `_world_state` 比较任务时取 `tid`、`type`（此前取 `id`、`kind`，比较的是一列 None）；任务状态与接力统计不比较，见第 9.2 节第 0 条 |

### 3.3 试过并撤回的改动

- **值守周期从离机巢最近的看洞点开始**（为 D2-AC-34"会话 ACTIVE 后 ≤ 60 s 首次捕获"）：七城 T5 180 s 批处理中 sanfrancisco 由 109.5 s 降到 70.7 s，但
  synthcity 与 shanghai 在 180 s 内不再有捕获（近处的看洞点对着游走的人），已撤回，问题转 M20 与 M16（第 8 节）。

## 4 D2 验收状态（D2-AC-01 至 D2-AC-55）

状态：**通过** = 功能口径全部满足（含本轮可在功能口径判定的计时）；**功能通过** = 功能部分在本轮全部通过，性能、cgroup 模拟、时长或安静环境
计时的子项留在性能阶段（ADR-033），并注明 D2 验收第 1 轮（ACC-1）的不通过项与 D2-FX-R2 的处置；**部分** = 本轮有未满足的功能子项；
**不通过** = 最近一次测量（ACC-1 或 FX-R2 自测）不通过且本轮无新证据；**未执行** = 只能在性能阶段、emax 或长时批处理执行。

| 验收 | 状态 | 本轮证据 | 说明（ACC-1 → FX-R2 → 本轮） |
|---|---|---|---|
| D2-AC-01 进度条 | 功能通过 | `perf/d2/boot-progress.spec.ts` 2、`perf/m15/sandbox-d2.spec.ts` 5、`perf/d21/boot-live.spec.ts` 3 | 揭开 +31.3% → FX-R2-web 程序去重与早期脚本（自测 +4.8%）→ 待安静环境复测 |
| D2-AC-02 沙盒创建 | 功能通过 | 全量 pytest（`tests/sandbox/test_pool.py`、`tests/e2e/test_sandbox_sdk_e2e.py`） | READY 中位 47 s（cgroup，stage 成本）→ FX-R2-sandbox 拉起权重与滞回 → 待 M18 识别物 stage 成本修复后复测 |
| D2-AC-03 隔离与故障恢复 | 通过 | 全量 pytest；ACC-1 混沌 8 例通过 | |
| D2-AC-04 并发容量 | 未执行 | — | ACC-1 不通过（stage 成本）；识别物 stage（约 290 ms/仿真秒）仍未修，见 9.2 |
| D2-AC-05 排队与降级 | 通过 | `perf/m15/sandbox-d2.spec.ts` 排队例、全量 pytest、本机演示第 4 个访客排队（第 7 节） | |
| D2-AC-06 回收、寿命与抢占 | 功能通过 | `tests/e2e/test_sandbox_park.py`、`tests/sandbox/test_park_pool.py` | 驻留后 +14.3 MB → FX-R2-sandbox trim 与预热（自测 +8.4 MB）→ 待复测；抢占与 L3 回收改为驻留后，恢复的会话丢失任务与接力状态（见 D2-AC-45） |
| D2-AC-07 倍速治理 | 功能通过 | `tests/sandbox/test_governor.py`；`d2.integration-live` S7 例按成本授予 | 判据取决于 stage 成本 |
| D2-AC-08 sandbox100 | 功能通过 | `tests/sim/test_fleet_sih_parity.py`、`tests/mission` | 0.255 核 → FX-R2-sim 基础循环 0.14 核；识别物与感知 stage 合计约 0.26 核未修（9.2） |
| D2-AC-09 机型目录 | 功能通过 | `tests/catalog/`（全量） | 增删改 1.1–1.2 s 为 SDK 整秒退避 → FX-R2-sim SDK 按 `retry_after_ms` → 待复测 |
| D2-AC-10 固定翼与 VTOL | 通过 | `tests/catalog/test_fixedwing.py`、`test_vtol.py`；S7 批处理中 AWR-V1 值守正常 | |
| D2-AC-11 识别物 | 通过 | `perf/m18/targets-panel.spec.ts` 2；`d2.integration-live`"识别物"例（界面放置与删除，D2-INT 修复第 3.1 节第 2 项后通过）；`tests/targets/` | |
| D2-AC-12 被发现判据 | 通过 | `tests/targets/test_discover_matrix.py`、`test_acoustic.py` | |
| D2-AC-13 感知判据 | 功能通过 | `tests/perception/`（全量） | 490.6 ms/仿真秒 → FX-R2-perception 自测在阈值内（余量 5–10%）→ 待复测 |
| D2-AC-14 明确捕获与提示 | 通过 | `perf/m19/capture-toast.spec.ts` 3、`hud.spec.ts` 1、`perf/d2/sandbox-live.spec.ts` 第 1 例、七城 T1/T6 批处理首次捕获 53–62 s【仿真】 | shenzhen、chicago 的 T1/T6 回归由本轮修复（第 3.1 节第 9 项） |
| D2-AC-15 消光与昼夜 | 通过 | `tests/environment/` | |
| D2-AC-16 四类任务 | 功能通过 | `d2.integration-live` 七类任务例；七城 42 模板 180 s 无错误；FX-R2-tasking 的 F1 弹射与巡边绕行用例 | ACC-1 不通过（F1 预检、巡边 583）→ FX-R2-tasking 修复；10 m/s 风下的固定翼定点端到端批处理本轮未复跑 |
| D2-AC-17 GSD 扫描 | 通过 | `tests/tasking/test_scan.py`、T4 七城 | |
| D2-AC-18 贪心分配 | 通过 | `tests/tasking/test_greedy.py`；ACC-1 50 × 12 p95 28.8 ms | |
| D2-AC-19 接力 24 h | 部分 | S7 完整剧本 3600 s 批处理（第 8 节）与真实沙盒 S7 例 | ACC-1 不通过 → FX-R2-tasking 白天与晴夜通过，霾时段（night + MOR 3 km）仍有间隙；24 h 批处理本轮未跑 |
| D2-AC-20 接力 7×24 h | 未执行 | — | 随 D2-AC-19 |
| D2-AC-21 手动控制与传感器视图 | 功能通过 | `perf/d2/sandbox-live.spec.ts` 第 3 例、`perf/m18/manual-keepout.spec.ts` 2（去掉 fixme）、`perf/m06/sensor-views.spec.ts` 4、`perf/d21/mobile-live.spec.ts` 平板摇杆例 | 命令到可见 p95 无用例（d2-FX-R2-web-to-M16 第 5 条） |
| D2-AC-22 开放接口 | 通过 | `tests/contracts/test_sandbox_openapi.py`、`tests/e2e/test_sandbox_sdk_e2e.py`（SDK 全流程）、`sandbox-live` 第 2 例 | |
| D2-AC-23 配额与限流 | 通过 | `tests/sandbox/test_prefix.py`、`test_pow.py`、`test_sdk_retry.py` | |
| D2-AC-24 底图 | 通过 | `d2.integration-live` 底图例（七城经会话条切换）、`make validate` 七城 0 错误、`tests/ops`、`tests/world/test_dataset_terms.py` | NOTICE BibTeX 与六城数据集说明由 FX-R2-deploy 修复 |
| D2-AC-25 设计体系 | 通过 | `make lint`；`perf/m19/capture-toast.spec.ts`（一处红） | |
| D2-AC-26 D1 不回退 | 功能通过 | `perf/skeleton.spec.ts` 4、全量 pytest（D1 用例）、e2e 组（第 2 节） | N = 1000 卡顿与 trails 由 FX-R2-other 修复 → 待复测 |
| D2-AC-27 公开站部署 | 未执行 | — | 需 emax；`tools/deploy/emax/observe.py` 本轮把会话重启计入判据 |
| D2-AC-28 内存 | 功能通过 | `tests/world_query/test_grid25.py`、`tests/planning/test_worker_mem.py` | S 级 152 MB → FX-R2-sandbox 规划作业延后 → 待复测 |
| D2-AC-29 安全 | 通过 | `tests/rt/test_public_mode.py`、`test_sandbox_api.py`、`test_identity_read.py` | |
| D2-AC-30 可视化性能 | 功能通过 | `perf/d2/gpu-limits-d2.spec.ts` 1 | 治理器下限已收紧（FX-R2-web），整景帧节奏待复测 |
| D2-AC-31 雷达与声阵列（基础） | 通过 | `tests/perception/`、`tests/sensors/` | |
| D2-AC-32 确定性 | 功能通过 | `tests/sim/test_batch_determinism.py`、`tests/tasking/test_batch.py` | FX-R2-sim 批处理按固定延迟生效，1 h 逐字节一致（自测） |
| D2-AC-33 雷达与声阵列（进阶） | 通过 | `tests/perception/` | P1 |
| D2-AC-34 访客路径与模板 | 部分 | `perf/d2/demo-live.spec.ts` 2、`sandbox-live` 第 1 例、`d21-shell`、七城 42 模板 180 s 全部无错误（D2-INT 修复第 3.1 节第 4、5 项后） | **T5 首次捕获未达"会话 ACTIVE 后 ≤ 60 s"**：71–160 s，chicago 180 s 内无（第 8.3 节） |
| D2-AC-35 应用场景一端到端 | 部分 | `d2.integration-live` 七类任务例（区域值守 RUNNING）；完整 S7 批处理值守转入两个接力 | T5 资源在算术上不能满足间隙口径（d2-FX-R2-tasking-to-M16 第 1 条，待 M16 与架构裁决）；24 h 批处理未跑 |
| D2-AC-36 应用场景二端到端 | 通过 | `tests/e2e/test_sandbox_sdk_e2e.py::test_scenario2_example_script` | |
| D2-AC-37 共享展示可用交互 | 通过 | `perf/d2/demo-live.spec.ts` 第 2 例、`perf/m15/tasks-d2.spec.ts` viewer 例 | |
| D2-AC-38 会话恢复与同一出口 | 功能通过 | `tests/sandbox/test_pool.py`、`test_prefix.py`、`perf/m15/sandbox-d2.spec.ts` | 刷新 14 s → FX-R2-web 丢弃文档不驻留（自测 1.2–3.1 s）→ 待复测 |
| D2-AC-39 S7 浸泡 | 未执行 | S7 3600 s 批处理被发现 0（第 8 节） | 24 h 未跑；ACC-1 的"AWR-V1 被车辆发现"已由 FX-R2-tasking 修复 |
| D2-AC-40 版本号 | 通过 | `perf/d21/boot-live.spec.ts` 第 1 例、`make lint`（check-version） | |
| D2-AC-41 五段进度条与载入期容量 | 功能通过 | `perf/d21/boot-live.spec.ts` 3（页内计时，请求早于主分块）、`perf/m15/d21-shell.spec.ts` | ≤ 50 ms 与 ≤ 100 ms 待安静环境（`M16_PERF=1`） |
| D2-AC-42 容量接口 | 功能通过 | `tests/e2e/test_capacity_e2e.py`、`tests/sandbox/test_capacity.py` | p95 待复测；nginx 微缓存本机未测 |
| D2-AC-43 实例口径与观看排队 | 通过 | `perf/d21/seats-live.spec.ts`（D2-INT 修复后真实栈通过）、`tests/e2e/test_viewer_power_e2e.py`、`test_capacity_e2e.py` | 第 1 轮后发现的 P0（观看排队 principal）已修复 |
| D2-AC-44 节能（viewer） | 功能通过 | `perf/d21/power-live.spec.ts` 3（节能 10 s 内 0 个请求）、`power-boot.spec.ts` 1、`seats-live` | 主线程 4.05% → FX-R2-web、WP-12c、D2-INT 冻结全部周期任务 → 待复测 |
| D2-AC-45 节能（沙盒） | 部分 | `tests/e2e/test_sandbox_park.py` 8（`_world_state` 的任务字段已改正）、`perf/d21/power-live.spec.ts` 第 2 例、D2-INT 诊断 `.cache/d2int/diag/park_tasks.py` | **恢复后任务与接力状态丢失（P0）**：接力 1 次交接后驻留并恢复，任务回到 PLANNING、接力统计 404（M20-FR-075 checkpoint 扩展段未实现，第 9.2 节第 0 条）；内存回落 +14.3 MB 待复测 |
| D2-AC-46 节能节省（实测） | 未执行 | — | P1；viewer 阈值待 ADR（d2-FX-R2-sandbox-to-文档总编） |
| D2-AC-47 移动端布局 | 通过 | `perf/m15/d21-mobile.spec.ts` 11、`perf/d21/mobile-live.spec.ts` 11、本机演示手机与平板截图 | |
| D2-AC-48 触控手势与快捷指令 | 通过 | `perf/m06/field-views.spec.ts` 6（触控）、`perf/d21/mobile-live.spec.ts`（快捷指令、平板摇杆、旋转归零）、`perf/m15/d21-mobile.spec.ts` | 摇杆 ≥ 25 Hz 在 `M16_PERF=1` 下判定（WP-13b 实测 30.3 Hz） |
| D2-AC-49 移动画质档与上限 | 通过 | `perf/d21/gpu-limits-d21.spec.ts` 6、`perf/m06/gpu-limits.spec.ts` D2.1 场视图 4 例 | |
| D2-AC-50 PWA 与全屏 | 通过 | `perf/m15/d21-mobile.spec.ts`、`check-brand` | P1 |
| D2-AC-51 内部身份 | 通过 | `perf/d21/identity-live.spec.ts` 4、`tests/e2e/test_identity_e2e.py`、`test_identity_public_e2e.py`（去掉 xfail）、演示构建 REL-04 扫描 0 处 | 口令只在测试运行时随机生成 |
| D2-AC-52 感知量化 | 通过 | `tests/perception/test_quant.py`、`tests/e2e/test_field_sampling_e2e.py`、`perf/d21/field-live.spec.ts` | |
| D2-AC-53 场视图与感知体 | 功能通过 | `perf/m06/field-views.spec.ts` 6、`perf/d21/field-live.spec.ts` 3 | 切换 ≤ 300 ms 为性能项 |
| D2-AC-54 WebGL2 上限下 0 错误 | 通过 | `perf/m06/gpu-limits.spec.ts` 14、`perf/d21/gpu-limits-d21.spec.ts` 6、`perf/d2/gpu-limits-d2.spec.ts` 1：违例 0、gl 0、three 0、错误 0，绑定点 ≤ 3，单程序块 V 3 / F 2 | |
| D2-AC-55 emax 资源（D2.1） | 未执行 | — | ACC-1 不通过（stage 成本）；场与读数服务成本（9.7 ms/仿真秒）未修 |

汇总：通过 27、功能通过 18、部分 4（D2-AC-19、34、35、45）、未执行 6（D2-AC-04、20、27、39、46、55），本轮没有判为"不通过"的项：ACC-1 的不通过项
或已有修复、待性能阶段按 ADR-033 复测（列为功能通过），或仍有功能缺口（列为部分），或只能在性能阶段、emax 与长时批处理判定（列为未执行）。
按 D2 退出标准（P0 全部通过、P1 ≥ 80%），D2 目前**不满足**退出标准：4 项部分与 6 项未执行都是 P0 或含 P0 子项，且功能通过项的性能子项尚未复测。

## 5 越权修改清单

全部是 D2-INT 第 2 轮的修改（本工作包获授权修改任何路径）。按文件列出所有者与原因（新增文件标"新增"，删除标"删除"）：

| 文件 | 所有者 | 原因 |
|---|---|---|
| `apps/web/perf/d2/integration-live.spec.ts` | M16（D2-INT 用例） | S7 例改为完整 S7、页面当前 token、`viewport=off` 与墙钟预算；新增识别物界面放置与删除例 |
| `apps/web/perf/d21/seats-live.spec.ts` | M16 | 观看排队 principal 缺陷已修，条件 `test.fail` 改为断言 |
| `apps/web/perf/m18/manual-keepout.spec.ts` | M16 | 去掉 fixme；第 2 例改用三维距离 |
| `apps/web/src/app/boot/capacity.ts` | M15 | 改用 M17 的 `adoptBootCapacity` |
| `apps/web/src/app/i18n/zh-CN.json`、`en.json` | M15 | `tasking.warning.perimeter_blocked`、`tasking.warning.look_in_los` |
| `apps/web/src/engine/perf/index.ts` | M06 | 挂起间隙不计入帧间隔 |
| `apps/web/src/stores/capacity.ts` | M17 | `adoptBootCapacity` |
| `apps/web/src/styles/mobile.css` | M15 | 删除与 M06 ViewCube 同值的覆盖规则 |
| `apps/web/src/ui/sandbox/session.ts` | M15（过渡 store） | 共用 principal 提示（P0，第 3.1 节第 1 项） |
| `apps/web/src/viewport/animationSampler.ts` | M06 | 节能冻结时停止采样 |
| `apps/web/tests/m06/animationSampler.browser.test.ts` | M06 | 冻结用例 |
| `apps/web/tests/m15/sandboxSession.test.ts` | M15 | principal 提示用例 |
| `apps/web/tests/m17/capacity.test.ts` | M17 | `adoptBootCapacity` 用例 |
| `python/awr/api/deps.py` | M11 | `identity_read`；身份令牌的只读按 admin |
| `python/awr/api/rt/inspect.py` | M11 | 同上 |
| `python/awr/api/rt/rpc.py` | M11 | 插件命令的 result 进入 result 帧 data（第 3.1 节第 2 项） |
| `python/awr/api/security.py` | M11 | `HINT_RE` 接受 32 位十六进制 |
| `python/awr/sandbox/pool.py` | M17 | 运维状态行 `restarts` |
| `python/awr/sim/sensors/nav_pub.py`、`pose_pack.py` | M13 | 仿真时间回退后重新对齐 10 Hz |
| `python/awr/sim/tasking/executor.py` | M20 | 多旋翼绕高楼；`route_blocked` 时交 M10 自动航线；多旋翼定点到站转向盯防点 |
| `python/awr/tasking/decompose.py` | M20 | 值守巡边被阻断时去掉巡边段 |
| `tests/e2e/honesty.spec.ts` | M16 | 第 1 例视口 1600 × 900 |
| `tests/e2e/sbstack.py` | M16 | 命令行 `--profile` |
| `tests/e2e/test_sandbox_park.py` | M16 | `_world_state` 的任务字段 |
| `tests/e2e/test_identity_public_e2e.py` | M16 | 去掉 `KNOWN_403` 与 xfail |
| `tests/rt/test_identity_read.py` | M11（新增） | 身份令牌只读与提示形式 |
| `tests/rt/test_rpc_entity_result.py` | M11（新增） | 插件命令 result |
| `tests/sandbox/identitykit.py` | M17（删除） | 改用 `tests/fixtures/identity.py` |
| `tests/sandbox/test_identity_lib.py`、`test_rest_park.py` | M17 | 同上 |
| `tests/sandbox/test_pool.py` | M17 | `restarts` 断言 |
| `tests/sensors/test_payload_scale.py` | M13 | 位姿与 nav 重置后发布用例 |
| `tests/tasking/test_decompose.py` | M20 | 巡边被阻断用例 |
| `tools/bench/fleet_ladder/run.py` | M08 | 记录窗口内 `slow_ms_per_s` 均值（d2-FX-R2-other-to-M16 第 2 条） |
| `tools/contracts/sandbox_api.py`、`gen_openapi.py` | M00 | 说明改为 SB-00 至 SB-78 |
| `tools/deploy/emax/README.md` | M00 | `sandbox/sb/` 行 |
| `tools/deploy/emax/observe.py` | M00 | 会话重启计入"非预期重启" |
| `tools/sdk/README.md` | M17 | `retry_after_ms` 与无连接驻留说明 |
| `docs/impl/D2-INT-集成报告.md` | D2-INT | 本报告（取代第 1 轮） |
| `.cache/impl/requests/d2-INT-*.md` | D2-INT | 第 2 轮请求（第 6 节） |

另有不入仓库的产物：`.cache/d2int/{test,demo}`（测试构建与演示构建）、`.cache/d2int/demo-live/`（本机演示脚本）、`.cache/d2int/diag/park_tasks.py`
（驻留恢复诊断）、`.cache/impl/shots/`（截图）。

## 6 跨模块请求处置

`.cache/impl/requests/d2-*.md` 共 237 份（WP 182、ACC-1 13、FX-R2 38、D2-INT 第 1 轮 4；另有 `d2-WP-04b-drafts/`、`d2-WP-19b-drafts/`
两个已合入的草稿目录）。第 1 轮已处置的 184 份 WP 请求在本轮按当前代码复核状态；ACC-1 的 13 份已由 D2-FX-R2 七个修复区逐条处置（各区报告），
本轮复核其结果并处理 FX-R2 与 WP 第 2 次执行新发出或追加的条目。处置分为：**D2-INT 落实**（本轮实现，第 3 节）；**已落实**（他人已交付，
本轮门禁或抽查代码确认）；**延后**（需要所有者、性能阶段或文档裁决，列入第 9 节）；**知会**。没有写出的条目都是"已落实"或"知会"。

D2-INT 第 2 轮新发出或追加 7 份请求：`d2-INT-to-M08-M20.md`（新，P0：驻留恢复后任务与接力状态丢失）、`d2-INT-to-M11.md`（新）、
`d2-INT-to-M06-M13.md`（新）、`d2-INT-to-M16.md`（新）、`d2-INT-to-M15-M17.md`（追加第 5–9 条）、`d2-INT-to-M20-M21-M16.md`（追加第 4–9 条）、
`d2-INT-to-文档总编.md`（追加第 5–9 条）。

### 6.1 本轮 D2-INT 落实的请求

| 请求 | 条目 | 落实 |
|---|---|---|
| d2-WP-13b-to-M15 | 10 | 观看排队 principal（第 3.1 节第 1 项） |
| d2-WP-13b-to-M11 | 7 | 身份令牌 GET 按 admin（第 3.1 节第 3 项） |
| d2-WP-13b-to-M17 | 1 | 正式身份夹具，删除 `identitykit.py` |
| d2-WP-12c-to-M06 | 5、6 | 动画采样器冻结；挂起间隙不计入帧间隔 |
| d2-WP-12c-to-M17 | 1 | `adoptBootCapacity` |
| d2-WP-11b-to-M15 | 9 | 删除 ViewCube 覆盖规则 |
| d2-FX-R2-other-to-M13 | 5 | 位姿与 nav 在仿真时间回退后重新对齐 |
| d2-FX-R2-deploy-to-M17 | 1 | 池状态行 `restarts`，观测工具计入判据 |
| d2-FX-R2-sim-to-M17 | 2 | SDK README |
| d2-FX-R2-sandbox-to-M11-M10-M00 | 10 | emax README 的 `sandbox/sb/` |
| d2-FX-R2-other-to-M16、d2-FX-R2-other-to-M08 | 2、3 | `fleet_ladder` 记录 `slow_ms_per_s` |
| d2-FX-R2-web-to-M16 | 4 | `manual-keepout` 去掉 fixme、三维距离 |
| d2-ACC-1-to-M16 | 5 | 同上 |
| d2-INT-to-M20-M21-M16（第 1 轮） | 1、3 | 第 1 条：S7 的 AWR-V1 值守周期已由 FX-R2-tasking 修复，本轮以完整 S7 批处理与真实沙盒复核；第 3 条：chicago T3 转场 102（第 3.1 节第 5 项） |
| d2-INT-to-M15-M17（第 1 轮） | 3、4 | 第 3 条：FX-R2-web 已以保活修复（本轮 `manual-keepout` 2 passed）；第 4 条：WP-12c 第 2 轮已修 |
| d2-INT-to-文档总编（第 1 轮） | 3 | 设计表说明 SB-78 |
| d2-WP-13-to-M20-M21（第 1 轮延后） | 2 | chicago T3 转场细校验（第 3.1 节第 5 项） |
| D2 验收第 2 轮未完成运行（`runs/acceptance/d2-round2/func/check-templates-fail/`） | suzhou T5 583 | 第 3.1 节第 4 项 |

### 6.2 按接收方的处置（第 2 轮状态）

| 接收方 | 请求 | 处置 |
|---|---|---|
| M00 | d2-ACC-1-to-M00、d2-FX-R2-web-to-M00、d2-FX-R2-sandbox-to-M11-M10-M00（M00 部分）；第 1 轮的 WP 请求 | 已落实：NOTICE BibTeX 与六城数据集说明（FX-R2-deploy）、构建插件（FX-R2-web，知会）、`cgroup-exec.sh` 档位与会话父组（FX-R2-sandbox）；D2-INT：emax README、观测工具的会话重启；延后：部署侧加 CSP 时两段内联脚本的 hash（d2-FX-R2-web-to-M00 第 5 条）、第 1 轮已列的 d2-WP-04-to-M00 第 4、5 条、d2-WP-08-to-M00 第 1、2、3、5 条、d2-WP-12b-to-M00 第 1、2 条、d2-WP-13-to-M00 第 1 条 |
| M03 | d2-FX-R2-deploy-to-M03、d2-WP-16-to-M03 | 已落实（数据集说明与 `worldpkg dataset`，FX-R2-deploy）；延后：生成器版本与产品版本解耦（d2-WP-16-to-M03 第 3 条） |
| M04 | d2-FX-R2-sandbox-to-M11-M10-M00 第 9b 条 | 延后：`zones.raster` 匿名金字塔改 memmap（sanfrancisco worker PSS 约 91 MB） |
| M05 | 第 1 轮的 WP 请求 | 延后不变：点预算上限读取、`releaseLowPriority`、`visible.tasking/targets`、原地换世界 |
| M06 | d2-ACC-1-to-M06、d2-FX-R2-web-to-M06、d2-FX-R2-other-to-M06、d2-WP-12c-to-M06、d2-WP-12b-to-M06 | 已落实：trails 航线上限（FX-R2-other）、程序去重与治理器下限（FX-R2-web）、ViewCube、灰阶 token、FPV 拖动接口与边缘带、prod-bundle-scan 身份检查（WP-11b 第 2 次）；D2-INT：第 5、6 条；延后：安静环境复测（d2-FX-R2-web-to-M06 第 6 条，M16）、warming 段与原地换世界（第 1 轮） |
| M07 | d2-FX-R2-perception-to-M07、d2-FX-R2-sim-to-M07-…（M07 部分） | 已落实（稳态缓存，FX-R2-sim，知会）；延后：`optical_depth` 复用 `derive` 与 numba 化（约 190 µs/次，感知真实组合成本） |
| M08 | d2-ACC-1-to-M08、d2-FX-R2-sandbox-to-M08-M18、d2-FX-R2-other-to-M08、d2-FX-R2-tasking-to-M08、第 1 轮的 WP 请求 | **延后（P0）：checkpoint 扩展段登记（d2-WP-07-to-M08 第 1 条，第 1 轮按 P1 延后；本轮查明是 D2-AC-45 的根因，`d2-INT-to-M08-M20.md`）**；已落实：批处理确定性与 fleet_ladder（FX-R2-sim）、结束段顺序（FX-R2-tasking）、N = 1000 卡顿（FX-R2-other）；延后：契约注册表冷启动后释放与低频 `malloc_trim`（d2-FX-R2-sandbox-to-M08-M18 第 3 条）、`sensors`/`contact` stage 稳态成本（d2-FX-R2-other-to-M08 第 2 条）、第 1 轮已列的正式驻留 checkpoint 与 d2-WP-08-to-M08 各条 |
| M09 | d2-FX-R2-sim-to-…（M09 部分） | 已落实（知会）；延后：电池化学体系 OCV（P1，第 1 轮） |
| M10 | d2-ACC-1-to-M10、d2-FX-R2-web-to-M20-M10、d2-INT-to-M10（第 1 轮）、d2-FX-R2-sim、d2-FX-R2-sandbox（M10 部分） | 已落实：批处理固定延迟、worker trim 与预热（FX-R2）；chicago T3 由 D2-INT 在 M20 执行器侧处理；延后：规划结果与刹停段连续拼接、近重合航点去重（d2-INT-to-M10 第 2、3 条） |
| M11 | d2-ACC-1-to-M11、d2-FX-R2-web-to-M11、d2-FX-R2-sandbox-to-M11-M10-M00、d2-WP-13b-to-M11、d2-WP-12c-to-M11 | 已落实：固定席位 116 重登记、hub 让出、容量快速路径（FX-R2-sandbox）、setpoint 保活（FX-R2-web）、WP-03b（第 1 轮 D2-INT）；D2-INT：第 7 条、principal 提示、插件命令 result；延后：手动泵移入 rt.worker（d2-FX-R2-web-to-M11 第 2 条）、观看排队 net 层函数（d2-WP-12c-to-M11 第 4 条）、`_inspect` 按码统计（d2-ACC-1-to-M11 第 1 条）、setpoint 泵与 0.5 Hz 剖面（第 1 轮） |
| M12 | 第 1 轮的 WP 请求 | 延后不变：沙盒会话录制 |
| M13 | d2-FX-R2-perception-to-M13、d2-FX-R2-other-to-M13、d2-FX-R2-sim-to-…（M13 部分） | 已落实（payload 生成集合与分片、融合遍历，知会）；D2-INT：第 5 条；延后：批量与缓存的 `sensor_world_pose`、`payload_view` 复用（感知真实组合成本） |
| M14 | — | 知会 |
| M15 | d2-ACC-1-to-M15、d2-FX-R2-sandbox-to-M15、d2-FX-R2-deploy-to-M15、d2-WP-13b-to-M15、d2-WP-11b-to-M15、d2-INT-to-M15-M17（第 1 轮） | 已落实：揭开、载入期容量、节能主线程、软围栏（FX-R2-web）、WP-12c 第 2 轮 9 项；D2-INT：第 10 条、第 9 条；知会：第 11、12 条（观测）；延后：会话 store 整体替换（M17）、内部页面懒加载与 `data-internal-entry`（d2-WP-11b-to-M15 第 14 条）、FPV 拖动改云台的界面接线（同上第 11 条）、报告页脚口径（文档总编） |
| M16 | 全部 to-M16 | WP-13b 第 2 轮已处理 D2.1 部分；D2-INT：`seats-live`、`manual-keepout`、`integration-live`、`test_identity_public_e2e`、`sbstack`；延后：性能阶段复测清单（d2-FX-R2-* 各条）、T5 模板与 S7 几何（d2-FX-R2-tasking-to-M16 第 1、2 条，需架构裁决）、`layers` 的被测负载记录、`observe.py` 登记（`fleet_ladder` 的 `slow_ms_per_s` 与 honesty 第 1 例已由 D2-INT 处理） |
| M17 | d2-ACC-1-to-M17、d2-FX-R2-sim-to-M17、d2-FX-R2-deploy-to-M17、d2-WP-13b-to-M17、d2-WP-12c-to-M17 | 已落实：准入与滞回、驻留内存、容量快速路径（FX-R2-sandbox）、SDK 退避（FX-R2-sim）；D2-INT：`restarts`、`adoptBootCapacity`、夹具、SDK README；延后：会话 store 整体替换、恢复后无访客时钟即播放（待裁决）、会话 perf 自报经池暴露（D2-AC-55 场成本）、驻留跨版本容量（d2-FX-R2-sim-to-M17 第 3 条） |
| M18 | d2-ACC-1-to-M18、d2-FX-R2-sim-to-M18、d2-FX-R2-perception-to-M18、d2-FX-R2-sandbox-to-M08-M18 | 已落实：视线缓存核（FX-R2-perception，知会）；**延后（P0 性能）**：识别物 stage 约 290 ms/仿真秒（预算 3–4），`step_motion` 与 `passable` 逐识别物逐点 Python、`_evaluate` 逐对（d2-FX-R2-sim-to-M18 第 2 条），是 D2-AC-02、04、07、08、55 剩余的最大单项 |
| M19 | d2-ACC-1-to-M19、d2-FX-R2-sim-to-M19、d2-FX-R2-sandbox-to-M19、d2-FX-R2-tasking-to-M19 | 已落实：感知 stage 向量化（FX-R2-perception，自测达标）；延后：没有识别物时 `collect` 提前返回、场与读数服务 9.7 ms/仿真秒（d2-FX-R2-sandbox-to-M19 第 1 条）、照明器作用距离口径（d2-FX-R2-tasking-to-M19） |
| M20 | d2-ACC-1-to-M20、d2-FX-R2-sandbox-to-M20、d2-FX-R2-sim-to-…（M20 部分） | 已落实：接力与值守的执行层修复（FX-R2-tasking 30 余项）、规划作业延后（FX-R2-sandbox）；D2-INT：值守巡边阻断、多旋翼绕高楼、多旋翼定点转向盯防点；延后：**M20-FR-075 编排状态的 checkpoint 保存与恢复（P0，D2-AC-45）**、霾时段接力间隙（D2-AC-19）、T5 首次捕获（D2-AC-34）、chicago T3 区域起点被围（d2-INT-to-M20-M21-M16 第 6 条） |
| M21 | d2-ACC-1-to-M21、d2-FX-R2-tasking-to-M21 | 已落实：弹射前静止等预检（FX-R2-tasking）；延后：垂起机 `follow_path` 末点精确到达或 `land{pos}`（d2-FX-R2-tasking-to-M21 第 3 条）、机型特征量入库（第 1 轮） |
| 文档总编 | 全部 to-文档总编（ACC-1 1、FX-R2 6、WP 若干、D2-INT 第 1 轮） | 全部延后给文档总编（实现已按各报告偏差表执行，均未放宽 P0 阈值）；D2-INT 第 2 轮追加 5 条（第 6 节开头） |

## 7 本机 public profile v2 全栈演示与截图

**栈**：`tests/e2e/sbstack.py --profile public --max-sessions 3`（本轮增加 `--profile`），即 `python -m awr.runtime.supervisor --profile public
--only sim-core,api,sandbox-pool,sandbox-api`：共享展示 sim-core 运行 S7（`awr.sandbox.snapshot.v1` 冷启动 + D2 插件）、show-api（公开模式、回环绑定、
`AWR_ORIGINS` 为四个本机预览源）、sandbox-pool（`quota_profile public`，工作量证明开启，N_max = 3）、sandbox-api；`AWR_ADMIN_SECRET_FILE` 为运行时随机
生成的口令文件，`identity.state_dir` 指向临时目录，`AWR_RELEASE_FILE` 由演示构建的 `build-info.json` 写出。前端为演示构建（`VITE_AWR_DEMO=public`），
四个 vite preview 按 nginx 路由代理（`/api/sandbox` → sandbox-api，其余 `/api`、`/worlds`、`/vehicles` → show-api），每个预览带不同的
`X-Forwarded-For`（不同访客的 /24；回环代理在 `trusted_proxies` 内）。脚本：`.cache/d2int/demo-live/public-demo.spec.ts`、`sdk_users.py`（非仓库用例）。

**负载与结果（1 passed，3.1 min）**：两个 SDK 用户（203.0.113.0/24、198.51.100.0/24）各开一个 T5 沙盒（SDK 自动求解工作量证明，每 15 s 一次会话内
REST 保活）；平板指挥器（iPad Pro 11 横屏，192.0.2.0/24）在落地页"开启我的沙盒"→ T5 → 进入自己的沙盒，容量接口 `sandbox.active = 3`；第 4 个访客
（桌面，198.18.0.0/24）在落地页看到"沙盒 3 / 3 在用 · 排队 0 人 · 预计约 29 min · 开启沙盒需排队 · 需排队 · 第 1 位"，开启后排队：容量接口
`active 3、max 3、queue 1、state queue、degrade_level L1`；手机 viewer（iPhone 15）与桌面 viewer 观看共享展示 S7。四个页面错误 0、WebGL 消息 0。

| 截图（`.cache/impl/shots/`，均已用 Read 查看） | 内容 |
|---|---|
| `d2int-pub-01-landing-full.png` | 演示落地页：容量行"沙盒 3 / 3 在用 · 排队 0 人 · 预计约 29 min"、"开启沙盒需排队"、"需排队 · 第 1 位 · 约 29 min" |
| `d2int-pub-02-start-sheet-queue.png` | 开启沙盒 Sheet：城市选择、T1–T6（T5 推荐）、仿真时段、"容量不足，可排队"、"当前 3 / 3 个沙盒在运行" |
| `d2int-pub-03-queued-boot.png`、`-03-queued.png` | 第 4 个访客排队后进入共享展示的启动屏：版本行 `V0.2.0-demo · abcdef0 · 2026-10-11`、五段分格进度条（"读取世界清单" → "预热着色器与界面"）、"沙盒 3 / 3 在用 · 排队 1 人 · 预计约 29 min · 开启沙盒需排队" |
| `d2int-pub-04-tablet-landscape.png`、`-05-tablet-portrait.png` | 平板指挥器持有第 3 个会话：顶栏（世界 › 会话、我的沙盒 · 可操作、场视图、全屏）、停靠的机队列表、会话条、底部任务条（区域值守运行中、前往、悬停、返航、盯防、倍速） |
| `d2int-pub-06-phone-viewport.png`、`-07-phone-{fleet,tasks,targets,alerts}.png` | 手机共享展示 viewer：五个窗口单窗切换，机群窗口列出 S7 的 8 架（v1-01 值守中） |
| `d2int-pub-08-desktop-show.png`、`-09-desktop-field.png` | 桌面共享展示 S7（"公开演示 · 只读"、"开启我的沙盒"）与声学场视图（球形散点与可闻壳、图例 dB(A) 五档、可闻壳与背景噪声壳、模态点形） |

观察（P2，第 9.4 节）：手机机群窗口中"性能降级：敏感体显示降级"的 Toast 盖住列表末行；Sheet 中 T5 的"首次反馈：会话开始后约 60 s【仿真】内
首次明确捕获"与实测（synthcity 160 s）不符、T1 的"约 40 s"与实测 54 s 不符（第 8.3 节）；共享展示按北京时间同步日历，本机演示时为夜间（19:37）。

## 8 S7（共享展示剧本）

### 8.1 批处理

S7（`scenarios/s7-synthcity-relay.json`，ADR-094：机巢箱 A 6 架 P600、VTOL 起降台 B 2 架 AWR-V1、3 个识别物、AWR-V1 区域值守 `on_detect = relay`）
以 `tools/sandbox/s7_check.py --until 3600 --start-local 13:00`（sandbox100，D1 + D2 全部插件，批处理 RTF 3.28）运行：

| 项 | person-01（静止的人，`t-2`） | person-02（游走的人，`t-3`） |
|---|---|---|
| 值守转入接力 | 是（首次捕获 196.4 s【仿真】） | 是（427.8 s） |
| 交接 | 2 次（1318 s、2461 s），重叠 15.2 / 15.4 s | 2 次（1457 s、3491 s），重叠 15.0 / 15.6 s |
| 覆盖率、间隙 | 1.0；间隙 0 | 1.0；间隙 0 |
| 被发现、进入配置体、能量 RTL | 0、0、0 | 0、0、0 |
| 最小禁入余量 | 7.25 m | 11.75 m |

事件流中没有 `target.discovered`、`vtol.hover_limit`、`safety.fastguard`；`task.warning` 5 条（`home_blocked_forced` 4、`handover_slow` 1，均为 FX-R2-tasking
设计的告警）。第 1 轮的结论"AWR-V1 值守周期不可用、值守发现后不会转入接力"已不成立（D2-FX-R2-tasking 修复）；第 1 轮建议的过渡剧本撤回。
`s7_check` 的 168 h 可持续性仍为"否"（`airframe`：V1 需 22 有 2；B 机巢 `charger` 需 29 有 2、`battery` 需 41 有 4），即 S7 的值守周期按 7 天口径不可持续，
是否影响 D2-AC-39 请文档总编明确（d2-INT-to-文档总编 第 9 条）。

### 8.2 真实沙盒中快进运行

`perf/d2/integration-live.spec.ts` 第 4 例（完整 S7，以 `config_snapshot` 在真实沙盒栈中创建会话，页面持有会话，请求 ×10，每 60 s 在授予低于
可达值时再请求）共运行四次，另以接力部分运行一次：

| 运行 | 条件 | 结果 |
|---|---|---|
| 第 1 次（07:28） | 同机负载 17–22（另一研究进程与全量 pytest） | 25 min 时仿真 1392 s、×1、RTF 0.72，判断在 45 min 内到不了第 2 次交接，中止 |
| 第 2 次（07:57，预算 100 min） | 同上 | 池在 CPU 压力下 8 次 L3 回收（`reclaimed_cpu`）驻留，页面每次自动恢复；由于驻留恢复后任务与接力状态丢失（第 9.2 节第 0 条），接力每次从头开始，6010 s 内 0 次交接，失败 |
| 第 3 次（09:51，预算 75 min） | 负载回落后（约 9，主要是该页面 SwiftShader 渲染 S7 场景约 5.6 核） | ×1（`limited_by budget/cost_cap`），无回收；D2-INT 以同一 token 密钥签发的只读 token 查询（会话内仿真 3658 s）：`t-2`（person-01）交接 3 次、`t-3`（person-02）交接 2 次、被发现 0、覆盖率 0.993 / 0.980；但用例自己的读取全部为 401——**用例缺陷**：创建时的 token 在会话寿命（30 min）后过期，页面续期换了 token 而用例仍用旧的，接力数始终读成 0。中止并修正用例 |
| 第 4 次（10:53，预算 60 min） | 用例改为每轮取页面当前 token；页面以 `viewport=off` 打开（接力与 Dock 不需要画布） | **1 passed（40.8 min）**：完整 S7，治理器授予 ×1（`limited_by budget`，可达 ×2.3），值守转入两个接力，`t-2`（person-01）在仿真约 41 min 时交接 2 次、平均重叠 15 s、覆盖率 100.0%、间隙 0、被发现 0、最小禁入余量 7.3 m；全部接力被发现 0，事件中无 `target.discovered`；Dock 接力监控页截图 `d2int-s7-relay.png` |
| 同时：接力部分（`D2INT_S7_RELAY_PART=1`，11:03） | 机巢 A、3 架 P600、静止的人、`relay_watch` | **1 passed（20.2 min）**：授予 ×2（快进），2 次交接、平均重叠 15 s、覆盖率 100.0%、间隙 0、被发现 0、最小禁入余量 50.7 m（截图被第 4 次覆盖，数值见本表） |

第 3 次运行中值守与接力的时间线（会话事件）：V1 在 23 s 捕获车辆（vehicle 类不转接力），126 s 以 R 级捕获 person-01 → 创建 `t-2`、派出 p600-06；
224 s 捕获 person-02 → `t-3`、派出 p600-03；之后两个接力按能量交接。`t-3` 有 28 次短间隙（最长 8.8 s，`other`，游走的人在楼角附近的视线闪断，
与 FX-R2-tasking 报告的现象一致），`t-2` 有 1 次 23 s 间隙与一次 0 s 重叠交接。

### 8.3 T5 首次捕获（D2-AC-34）

七城 T5 180 s 批处理（D2-INT 修复之后）的首次捕获【仿真】：synthcity 159.7 s、shenzhen 132.5、newyork 83.1、shanghai 137.7、sanfrancisco 109.5、
suzhou 71.1 s，chicago 180 s 内无。判据为会话 ACTIVE 后 ≤ 60 s，不满足（第 9.2 节第 1 条）。

### 8.4 与第 1 轮的差异

- 第 1 轮的 P0 遗留"S7 的 AWR-V1 值守周期"已由 D2-FX-R2-tasking 修复（8.1）；chicago T3 的 102 与 suzhou T5 的 583 由本轮修复（第 3 节）。
- 第 1 轮报告称 `perf/m18/targets-panel.spec.ts` 第 2 例已通过，ACC-1 指出当时仍以 `test.fail` 失败；WP-12c 与 WP-13b 第 2 轮确认是用例时序并修正，
  本轮 2 passed。
- 第 1 轮的 `manual-keepout` fixme（偏差 6）本轮去掉，2 passed。
- 第 1 轮之后新发现并在本轮修复的 P0：观看排队 principal（D2-AC-43、44）；新发现并修复的界面缺陷：放置识别物后拿不到 id（第 3.1 节第 2 项）。
- 第 1 轮 D2-AC 统计（通过 29、功能通过 16、部分 4、未执行 6）与本轮不可直接比较：本轮把 ACC-1 测得的不通过项逐条注明修复与复测状态。


## 9 剩余问题（按区域）

按 D2-ACC-1 与 D2-FX-R2 的区域划分。P0 表示影响 P0 验收或用户可见的主路径。

### 9.1 性能（sim、perception；P0，D2-AC-02、04、07、08、13、55）

1. **识别物 stage（M18）约 290 ms/仿真秒（预算 3–4）**：`step_motion` 与 `passable` 逐识别物、逐采样点的 Python（DSM 取样、`np.linspace`、`np.clip`），
   `_evaluate` 逐（识别物，机）对；D2-FX-R2 各区都没有处理（d2-FX-R2-sim-to-M18）。这是满载会话成本（ACC-1 约 0.8 核，预算 0.15）剩余的最大单项，
   D2-AC-02、04、07、08、55 在它修复前不会通过。建议：候选步一次批量取 DSM（numba 双线性核），可通行判定与 `_evaluate` 向量化。
2. **感知真实组合的平台读取**：M13 `sensor_world_pose`、`payload_view`（每次评估 0.4–0.6 ms，沙盒约 50 次/仿真秒）与 M07 `optical_depth`（约 190 µs/次），
   没有识别物时 M19 `collect` 仍逐 tick 收集（约 100 ms/仿真秒）。
3. **场与读数服务 9.7 ms/仿真秒**（D2-AC-55 ≤ 2）：逐对读数链路与 SoA 编码重复（d2-FX-R2-sandbox-to-M19 第 1 条）。
4. 正式复测：D2-FX-R2 七个区的自测都在高负载下进行，D2-AC-01、06、09、13、26、28、30、38、41、42、44、45 的阈值须按 ADR-033 在安静环境复测
   （各 d2-FX-R2-*-to-M16 请求列出命令）。

### 9.2 编排与接力（tasking；P0，D2-AC-19、20、34、35、39、45）

0. **驻留恢复后任务与接力状态丢失（P0，D2-AC-45；新发现）**：S7 接力部分在 1 次交接后驻留（SB-69）并恢复（SB-70，checkpoint 口径）：恢复后任务
   `t-1` 回到 `PLANNING`、`GET /relay/t-1/stats` 为 404，任务、工作项、架次与接力统计都按配置快照重新开始，空中机体不在任何架次中
   （`.cache/d2int/diag/park_tasks.py`）。根因是 M20-FR-075 的 checkpoint 扩展段从未实现（d2-WP-07-to-M08 第 1 条请求的登记点 M08 没有提供，
   第 1 轮按 P1 延后）。后果之一：负载下池的 L3 CPU 回收（`reclaimed_cpu`）驻留并自动恢复的会话，接力每次都从头开始（第 8.2 节第 2 次运行
   8 次回收、0 次交接）。修复需要 M08 的扩展段登记与 M20 的状态保存与恢复（`d2-INT-to-M08-M20.md`），规模超出集成修复，未在本轮实现。
1. **D2-AC-34 T5 首次捕获**：七城 T5 180 s 批处理 71–160 s【仿真】，chicago 180 s 内无（判据：会话 ACTIVE 后 ≤ 60 s）。synthcity 的时间线：起飞到 10 m
   12.7 s、原地爬升到转场高度 139.9 m 约 31 s、绕禁入圆飞 606 m 到识别物另一侧的首个看洞点约 100 s。看洞点的选择与次序需按可达时间与可捕获性
   排序（M20），T5 模板的机巢与识别物距离需复核（M16）；D2-INT 试过的"从最近看洞点开始"已撤回（第 3.3 节）。
2. **D2-AC-19、20 霾时段**：night + MOR 3 km 时可行窗口很窄，FX-R2-tasking 最后一次 5 h 复测仍有间隙与重叠不足，接替不按能量派出的原因未查清；
   24 h 与 7×24 h 批处理本轮未跑。
3. **D2-AC-35 T5 资源**：模板原样（值守与两个接力共用 2 架 P600、3 块备用电池）在算术上不能满足"间隙按 D2-AC-19 口径"，需 M16 与架构裁决
   （d2-FX-R2-tasking-to-M16 第 1 条的方案 A/B）。
4. **chicago T3**：区域起点被三个游走的人的规划禁入圆与高楼围住，D2-INT 修复后不再有 102 错误，但机体反复"转场 → 守卫撤离"，180 s 内未开始作业
   （d2-INT-to-M20-M21-M16 第 6 条）。
5. **区域值守的巡查航段**按识别物物理半径裁剪，对规划圆仍有约 19 m 侵入（suzhou T5），请 M20 确认口径。
6. **S7 几何**：车辆沿路往返时其禁入圆周期性覆盖机巢箱 A（d2-FX-R2-tasking-to-M16 第 2 条）；S7 的 168 h 可持续性为"否"（第 8 节）。
7. **真实沙盒中的接力质量**（第 8.2 节第 3 次运行，仿真 3658 s）：`t-2` 一次 23 s 间隙（`other`）与一次 0 s 重叠的交接，`t-3` 28 次短间隙（最长 8.8 s，
   游走的人在楼角附近视线闪断）；批处理同一剧本 3600 s 内两个接力间隙都为 0。实时运行与批处理的规划时序不同（批处理按固定延迟生效），
   按 D2-AC-19 口径这些间隙在 24 h 运行中是非豁免间隙，请 M20 以实时栈复核。

### 9.3 沙盒（sandbox，M17）

1. 会话 store 整体替换仍未做（界面运行在 M15 过渡 store `ui/sandbox/session.ts`，行为由单测与真实栈用例覆盖）。
2. 恢复后无访客接入时时钟即播放（d2-WP-13b-to-M17 第 3 条，待裁决）。
3. 会话 perf 自报（`slow_ms_per_s`）未经池或 sandbox-api 暴露，`capacity.py --variant D` 的场成本判据不可判定。
4. 驻留跨版本（容量 1024 → 64）按冷启动重建，未实测。

### 9.4 前端（web：M15、M06、M05）

1. 观察（P2，截图）：开启沙盒 Sheet 的模板"首次反馈"文案（T5 约 60 s、T1 约 40 s）与实测不符；手机机群窗口的 Toast 盖住列表末行；节能与恢复排队（QUEUED）期间，顶栏时钟仍显示"×1 播放中"、视口中机体标签显示"信号延迟 STALE 0.7 S"、性能 HUD 在"已暂停"旁
   仍显示数值（WP-12c 写的是数值留空）；平板沙盒页面在接管后出现"p600-01 · 控制权被抢占"Toast（任务租约让给操作员，文案易被误读为操作员失去控制）；
   平板（触控）上仍显示键盘键位面板。
2. 内部页面懒加载与 `data-internal-entry`（prod-bundle-scan 的身份检查当前 0 个分块带标记）、FPV 拖动改云台的界面接线（M06 已提供 `fpv.onDrag`）未做。
3. 手动泵仍在主线程（rt.worker 只做桥接保活，d2-FX-R2-web-to-M11 第 2 条）。
4. M05 点预算上限、`releaseLowPriority`、原地换世界（沙盒切换底图仍整页刷新）。

### 9.5 网关与运行时（M11、M08）

1. 正式驻留 checkpoint（`checkpoint{to: disk}`、`AWR_RESUME_FROM`）与 d2-WP-08-to-M08 各条（第 1 轮已列）。
2. `sensors`、`contact` stage 稳态成本相对 D1 上升（N = 1000 单步 p99 余量小）。
3. 全量 pytest 中负载敏感的计时用例（第 2 节）。

### 9.6 部署（deploy，M00、M03）

1. D2-AC-27 需要 emax：`tools/deploy/emax/observe.py` 已可执行 168 h 观测；N_max 未冻结。
2. 生成器版本与产品版本解耦（d2-WP-16-to-M03 第 3 条）；部署加 CSP 时内联脚本的 hash。
3. 本机没有 nginx：nginx 相关用例与 D2-AC-42 的微缓存子项未测。

### 9.7 文档总编

ACC-1、FX-R2 与 WP 各包提交的口径冲突（均未放宽 P0 阈值）全部待处理；D2-INT 第 2 轮追加 5 条（principal 提示形式、身份令牌只读、值守巡边阻断与
多旋翼绕高楼、D2-AC-34 判据、S7 资源口径）。报告页脚"科研用途"与 ADR-109 的冲突（M15、M16）待定。

## 10 偏差

| # | 要求 | 实际 | 原因与处置 |
|---|---|---|---|
| 1 | 性能、cgroup 模拟、浸泡、24 h 与 7×24 h 批处理 | 未运行 | 并行阶段约束；第 4 节逐项标注，第 9.1 节列出复测清单 |
| 2 | 同机安静环境 | 05:30–09:50 同机另有与本工作包无关的研究任务（约 7 个工作线程），负载 8–25 | 不是本工作包的进程，未干预；计时与倍速授予在功能口径判定，负载敏感的失败单独复跑并注明 |
| 3 | `tests/chaos`（带 `perf` 标记） | 未运行 | 同 1；功能部分由 `tests/sandbox/test_pool.py`（崩溃重启、池接管）与驻留 e2e 覆盖 |
| 4 | nginx 相关用例 | skip | 本机无 nginx（不装新依赖），演示用 vite preview 代理代替 nginx 路由 |
| 5 | D2-AC-47 设备型号 | iPhone 15、Pixel 8、iPad Pro 11（WP-13b 矩阵）与 `d21-mobile` 的 iPad Pro 12.9、320 px | 与判据的设备同档（WP-13b 偏差 4） |
| 6 | 第 1 轮 D2-INT 报告 | 本文取代第 1 轮全文，第 1 轮的结论以"与第 1 轮的差异"保留（第 8.4 节） | 同一路径只有一份集成报告 |

## 11 依赖与安装

未安装任何依赖。nginx 本机不存在，nginx 相关用例按设计 skip；本机演示以 vite preview 的代理代替 nginx 路由。

