# D2.1 WP-13b D2.1 用例：实现报告

| 项 | 内容 |
|---|---|
| 工作包 | WP-13b D2.1 用例（所有者 M16；AWR-04 §15.15.1 第 6 行；依赖全部 D2.1 工作包；第 5–8 周） |
| 日期 | 第 1 轮 2026-10-06；第 2 轮 2026-10-11（"第 2 轮"一节，位于第 1 轮内容之前） |
| 依据 | 用户原文 `docs/inputs/D2.1-需求原文-2026-10-05.md`；AWR-04 v1.3 §15（R-D2-29 至 R-D2-43、ADR-117 至 ADR-126、D2-AC-40 至 D2-AC-55、§15.15）；17 §17.20；M17 PRD §14、M18-M19 PRD §15、M15 PRD §15.12、M11 PRD §15.12；D2.1 报告 WP-04b、WP-11b、WP-12c、WP-16、WP-19b 与 D2 报告 WP-13；发给 M16 的请求 d2-WP-04b、d2-WP-11b、d2-WP-12c、d2-WP-16、d2-WP-19b；第 2 轮另读 D2-INT 集成报告、D2 验收第 1 轮报告、D2-FX-R2 各区报告、WP-11b 与 WP-12c 第 2 轮报告，以及发给 M16 的 d2-INT-to-M20-M21-M16、d2-ACC-1、d2-FX-R2-{sandbox,deploy,sim,web,other,perception,tasking}、d2-WP-11b（第 2 次执行）、d2-WP-12c（第 2 轮）请求 |
| 路径（只写本包） | `apps/web/perf/**`（新增 `perf/d21/`）、`tests/{e2e,chaos}/**`、`tools/bench/sandbox/**`、`tests/fixtures/identity.py`；请求 `.cache/impl/requests/d2-WP-13b-to-*.md` |
| 约束执行 | 只写本包路径（18 号文档的测试清单登记以请求交文档总编，偏差 1）；未执行 git 写操作；未装依赖；未跑性能基准（基准工具只跑 `--smoke`，时间阈值只在 `M16_PERF=1` 下断言）；无 emoji 与禁用字形；无界面与渲染代码（颜色、组件、图标不涉及）；口令只在测试运行时随机生成（`tests/fixtures/identity.py`、`live21.makeSecretFile()`），仓库与本文不含任何口令；新增 Playwright 用例在 WebGL2 规范最低上限模拟（24/12/12/24、16 384，移动组另加点径 1、纹理 4096、无浮点颜色缓冲）下 0 错误；`make lint` 通过。第 2 轮同样只写本包路径（另改 M16 路径内的 `perf/m18/targets-panel.spec.ts` 与 `perf/harness/{runner,run}.mjs`，均为发给 M16 的请求），口令仍只在运行时随机生成，新用例 `seats-live` 在最低上限模拟下 0 个 GL 消息 |
| 结论 | P0 全部落地：真实栈（supervisor + 池 + 真实会话 sim-core）的 pytest 用例 6 个模块与 1 个混沌模块、Playwright 真实栈用例 6 个文件（启动屏版本号与排队可见、节能进入与恢复含满载恢复排队、身份切换、场视图与读数、手机与平板设备模拟、D2.1 页面的 GPU 规范最低上限）、身份口令夹具、容量基准变体 D（D2-AC-55）与登记；P1 的节能节省基准（D2-AC-46）与浏览器主线程测量也已实现。测试中发现并登记 M15 三处缺陷（以 `test.fail` 表达）与若干界面观测、M11（WP-03b 未交付）与 M17 的事项 |
| 第 2 轮结论 | WP-03b 已由 D2-INT 交付：基线复跑 D2.1 pytest 40 例全部通过，本轮把第 1 轮按能力探测的 skip/xfail 全部改为失败断言、浏览器用例不再经身份替身；处置发给 M16 的 D2.1 相关请求：页内计时的 D2-AC-41、平板虚拟摇杆（D2-AC-48，30.3 Hz、旋转归零）、节能主线程中 GC 单列（D2-AC-46）、harness `--runs` 双向覆盖与 `power-live` 去 serial、`targets-panel` 时序、受理失败附件（定位为 105 STATE 的用例时序）、演示分块口令扫描；新增观看席位满载用例 `seats-live`（真实 4431、观看排队、启动屏"观看已满"）与公开站口径的身份用例（R01 共用计数、速率 111、隐藏功能族、批处理 404），共享展示的内部控制与命令。新发现 M15 P0 缺陷 1 项（观看排队 principal 不一致，满载时恢复永远轮不到）与 M11 缺陷 1 项（两个隐藏端点的身份 GET 为 403），分别以条件 `test.fail` 与 xfail 表达。结果：pytest 43 passed、1 xfailed，混沌 3 passed，D2.1 Playwright 全部通过（`seats-live` 为预期失败），M15 的 D2.1 用例 20 passed，vitest 1372 passed，`make lint` 通过 |

## 第 2 轮（2026-10-11，WP-03b 于 D2-INT 交付、验收第 1 轮与 D2-FX-R2 之后）

第 1 轮之后，D2-INT 补齐了 WP-03b（R79/R80、presence 与 4430、观看席位与 R81–R83、状态文件、场与读数 topic 转发、PARKED 时领域路由
409 `515`），并改动了本包的 7 个文件（去掉已修缺陷的 `test.fail`、真实 R79、读数卡容差等）；验收第 1 轮与 D2-FX-R2 各区又发来 9 份
请求，WP-11b 与 WP-12c 第 2 轮各追加若干条。第 2 轮先在当前工作树上原样复跑（基线），再逐条处置，最后全部复跑。

### R2.1 输入与处置总表

| 来源与条目 | 第 2 轮所见现状 | 处置 | 验证 |
|---|---|---|---|
| WP-03b 交付（D2-INT） | 基线复跑：D2.1 pytest 40 例全部通过，0 skip、0 xfail（第 1 轮的 8 个 skip 与 3 个 xfail 均已转为通过） | 能力探测的 skip/xfail 一律改为失败断言：`test_viewer_power_e2e.py`（serverInfo.power、`viewer_seats`、状态文件）、`test_identity_e2e.py`（R79 缺失即失败，`M16_IDENTITY_STANDIN=1` 仍可对旧栈验证用例本身）、`test_field_sampling_e2e.py`（场与读数 topic）、`test_sandbox_park.py`（PARKED 时领域路由 409 515）；`live21.ts` 不再缺省启动身份替身，`identity-live` 断言 `identity-backend = show-api` | pytest 43 passed、1 xfailed（新公开站用例中的 M11 缺陷）；`identity-live` 4 passed |
| d2-ACC-1 第 1 条：`capacity.py` 的 `%%` 与变体 C 保活 | 已在 | 复核保留 | `test_sandbox_power_bench.py` 通过 |
| d2-ACC-1 第 2 条 D2-AC-41、d2-FX-R2-web 第 1 条：容量请求改由 `<head>` 首个内联脚本发出，Node 侧轮询计时粒度 1 s | 用例仍以 Node 侧时刻判断 | `boot-live` 第 2 例改为页内计时：init script 的 MutationObserver 记录行切到提示的时刻，Resource Timing 取容量请求与主分块（入口与 modulepreload）的时刻，`awr.boot.early` 标记；"请求早于主分块到达"每次断言，"早期脚本后 ≤ 50 ms"与"应答后 ≤ 100 ms 显示"只在 `M16_PERF=1` 断言、平时记注解 | 一次实测：标记 63 ms、请求 68 ms、应答 120 ms、主分块 343 ms、行显示 230 ms（见 R2.4 第 4 条） |
| d2-ACC-1 第 3 条：harness `--runs` 只能减少；`power-live` 为 serial | 同左 | `runner.mjs`：显式 `--runs` 双向覆盖注册值（非数值时用注册值），`run.mjs` 帮助改为 `--runs n`；`power-live` 去掉 serial（三例各自把池恢复为空，失败时 worker 重启并重新起栈，后续例照常运行） | `perf/harness/selftest.mjs` OK；`power-live` 3 passed |
| d2-FX-R2-web 第 3 条：登记 D2-AC-48 平板虚拟摇杆 | 无登记用例 | `mobile-live` 新增"tablet commander: the virtual stick…"：iPad Pro 11 横屏、T6 会话、等 p600-01 FLYING、接管、CDP 触摸按住左摇杆 2 s 计 `__ux.sandbox.setpoints` 速率（`M16_PERF=1` 时 ≥ 25 Hz，功能运行 ≥ 10 Hz，实测值记注解）、按住时旋转到竖屏：`__ux.manual.stick` 全 0、旋钮 `translate(0px, 0px)`、接管仍 ACTIVE（不以 setpoint 计数判定归零，空闲保活仍发零速度包） | 30.3 Hz；旋转后轴全 0、ACTIVE |
| d2-FX-R2-web 第 7 条：节能 10 s 窗口内的 Major GC | 主线程占比含 GC | `power-live` 在测量窗口内以 CDP Tracing（`devtools.timeline`、`v8`、`disabled-by-default-v8.gc`）统计渲染主线程 `MajorGC`、`MinorGC`、`V8.GC_*` 区间并集，注解同时给出总占比、GC 与去掉 GC 后的占比；`M16_PERF=1` 的 ≤ 1% 判定改用去掉 GC 的占比 | 一次实测（10 s）：1.87%，其中 GC 102 ms（1.01%），去掉 GC 0.86% |
| d2-WP-12c 第 2 轮：普通节能覆盖层不再轮询容量 | 第 1 轮用例把容量请求列为例外 | `power-live` 第 1 例改为节能 10 s 内 0 个请求 | 3 passed |
| d2-WP-12c 第 5 条：`perf/m18/targets-panel.spec.ts` 第 2 例的 `test.fail` 掩盖用例时序 | `start()` 在会话条出现时返回，会话仍 SPAWNING | `start()` 末尾轮询 `GET /sessions/{sid}` 到 READY 或 ACTIVE；第 2 例去掉 `test.fail`，写入断言 201 | 2 passed |
| d2-WP-12c 第 8 条：返航受理 Toast 偶发 20 s 内未出现 | 第 2 轮负载约 8 时复现一次 | `accepted()` 失败时附 Toast 日志、最近 20 次调用与页面截图；附件显示根因为服务端 `105 STATE`（会话刚启动、p600-01 尚未离地，返航被拒，界面正确提示"指令未受理"），不是界面缺陷。三个手机与平板指令例在下发前等待 p600-01 `FLYING`（`waitFlying`） | 复跑 5 passed |
| d2-WP-11b 第 6 条：`prod-bundle-scan --secret-file` | 未接入 | `identity-live` 新增一例：以本次运行的随机口令另写 0600 临时文件，对演示构建运行 `prod-bundle-scan.mjs`：退出码 0（首屏分块无隐藏入口、身份字面量只在一个分块、口令不在任何文件），输出不含口令 | passed |
| D2-AC-51"共享展示可申请控制并下发命令" | 浏览器侧未覆盖 | `identity-live` 新增一例（演示构建：访客只取 viewer，不占席位；测试构建页面在回环模式缺省取 operator 并占席，故先等席位在 30 s 宽限后为 FREE）：解锁后经顶栏徽标"申请控制"以身份令牌换取 admin（R01 `identity_token`），席位 held；Space 使共享展示时钟 PAUSED、再 PLAYING（`GET /api/sessions/current`）；释放后 FREE | passed |
| D2-AC-51 公开站口径（R01 `admin_secret` 共用计数、隐藏功能族、批处理 404、请求速率） | 本包无公开模式用例（M11 只在 FakeSim 上验证部分） | 新增 `tests/e2e/test_identity_public_e2e.py`：`public` profile 真实栈（`AWR_ORIGINS`、受信代理 `X-Forwarded-For` 区分前缀）4 例：R79 错 3 次 + R01 错 2 次后第 6 次 429 `516` 且他前缀 200；连发 6 次时第 4 次起 429 `111`；隐藏功能族 viewer 404、加身份头 200（`/api/sys/audit`、`/api/rt/inspect` 为 403，xfail，见 R2.4 第 2 条）；内部档免工作量证明 201、批处理 404 `305` | 3 passed、1 xfailed |
| D2-AC-43、44"观看满载时的节能恢复"（任务"节能模式进入与恢复（含满载排队）"） | 第 1 轮只能以注入 4431 验证（M15 `d21-shell`） | 新增 `perf/d21/seats-live.spec.ts`（`viewer_seats.max = 1`）：A 节能释放席位 → B 占席 → A 任意键 4431 → 观看排队卡片"第 1 位"、容量 `queue = 1` → 第三个访客 C 载入时启动屏显示"实时观看已满（1 / 1）· 进入后排队，第 2 位"与"需排队" → B 离开后 A 应自动连接；A 在规范最低上限模拟下运行（节能、排队静态浏览与拖动 0 个 GL 消息） | 前四步通过；第五步发现 M15 缺陷（R2.4 第 1 条），以条件 `test.fail` 表达 |
| d2-FX-R2-sandbox 第 1、2、5、6 条：`_sbbench` 的 `mem_high = 1.8G`、变体 B 测试额度、`CapacityProbe` 长连接、`emax_like` | 已在 | 复核保留；变体 A、D 的内存结论须注明 `emax_like`（无热备用、不钉核）口径 | 基准冒烟通过 |
| d2-FX-R2-deploy 第 3 条、d2-WP-12c 第 7 条：报告页脚"科研用途" | 与 ADR-109 不一致 | 转文档总编裁决（`d2-WP-13b-to-文档总编.md` 第 9 条），未改 | — |
| 其余发给 M16 的 D2（非 D2.1）条目 | 见 R2.6 | 属 WP-13（D2 用例与剧本），本包不处理 | — |

### R2.2 改动与新增文件（均在 WP-13b 路径内）

| 文件 | 改动 | 验收 |
|---|---|---|
| `apps/web/perf/d21/seats-live.spec.ts`（新） | 观看席位满载的节能、恢复排队、启动屏"观看已满"、轮到自动连接；最低上限模拟；主体提示诊断与条件 `test.fail` | D2-AC-41、43、44、54；M15-AC-222 |
| `apps/web/perf/d21/boot-live.spec.ts` | 页内计时（`lineClock`、`lineTiming`） | D2-AC-41 |
| `apps/web/perf/d21/power-live.spec.ts` | 去掉 serial；GC 单列（`tracedGc`）；节能期间 0 个请求 | D2-AC-44、46 |
| `apps/web/perf/d21/identity-live.spec.ts` | 断言 show-api 应答；共享展示内部控制与命令（演示构建）；演示分块扫描；容量计数改为 2 s 内轮询；锁定例移到最后 | D2-AC-51；M15-AC-227 |
| `apps/web/perf/d21/mobile-live.spec.ts` | 平板虚拟摇杆例；`waitFlying`；受理失败附件 | D2-AC-48 |
| `apps/web/perf/d21/live21.ts` | 身份替身改为 `M16_IDENTITY_STANDIN=1` 才启用 | — |
| `apps/web/perf/d21/cases.mjs` | 新增 `d21.seats-live`、`d21.e2e-identity-public`；`identity-live` 的 acIds 改正为 M15-AC-227；超时调整 | `make lint-m16-registry` |
| `apps/web/perf/m18/targets-panel.spec.ts` | `start()` 等 READY/ACTIVE；第 2 例去掉 `test.fail` | M18-AC-017 |
| `apps/web/perf/harness/runner.mjs`、`run.mjs` | `--runs` 双向覆盖 | ADR-033 |
| `tests/e2e/test_identity_public_e2e.py`（新） | 公开站口径 4 例 | D2-AC-51；M11-AC-217 |
| `tests/e2e/test_identity_e2e.py`、`test_viewer_power_e2e.py`、`test_field_sampling_e2e.py`、`test_sandbox_park.py` | skip/xfail 改为失败断言 | D2-AC-43、44、45、51、53 |

### R2.3 测试结果

运行环境同第 1 轮（8 vCPU、无 GPU、SwiftShader C1）；构建为测试构建（`ci`，`abcdef0123`）与演示构建，均以当前工作树重新构建到 `.cache/d21/`；
`AWR_PORT_OFFSET=37/38`；部分批次与 pytest 并行运行（负载 2–9）。功能运行，未取性能锁，时间阈值只在 `M16_PERF=1` 下断言。

| 范围 | 命令 | 结果 |
|---|---|---|
| D2.1 真实栈 pytest（基线，改动前） | `pytest -m "not perf" tests/e2e/test_sandbox_park.py test_capacity_e2e.py test_identity_e2e.py test_field_sampling_e2e.py test_viewer_power_e2e.py test_sandbox_power_bench.py` | 40 passed（11 min） |
| 同上（改动后，加公开站用例） | 同上加 `test_identity_public_e2e.py` | 43 passed、1 xfailed（12 min） |
| 混沌 | `pytest -m chaos tests/chaos/test_sandbox_park_chaos.py` | 3 passed |
| Playwright `perf/d21/boot-live.spec.ts` | `npx playwright test … --project perf` | 3 passed（两次） |
| `perf/d21/power-boot.spec.ts` | 同上 | 1 passed |
| `perf/d21/power-live.spec.ts` | 同上 | 3 passed（两次；第二次为最终版本） |
| `perf/d21/seats-live.spec.ts` | 同上 | 1 expected failure（R2.4 第 1 条；前四步与最低上限检查通过） |
| `perf/d21/identity-live.spec.ts` | 同上 | 4 passed（`identity-backend = show-api`） |
| `perf/d21/field-live.spec.ts` | 同上 | 3 passed |
| `perf/d21/gpu-limits-d21.spec.ts` | 同上 | 6 passed |
| `perf/d21/mobile-live.spec.ts` | 同上 | 设备矩阵与演示构建 6 passed；指令与平板 5 passed（`waitFlying` 之后复跑；此前一次"返航受理"失败，根因见 R2.1） |
| `perf/m18/targets-panel.spec.ts` | 同上 | 2 passed（不再有 `test.fail`） |
| M15 `perf/m15/d21-shell.spec.ts`、`d21-mobile.spec.ts`（以本包构建复跑） | `M15_DIST=.cache/d21/test …` | 9 + 11 passed（`d21-mobile` 手机例首跑 5 min 超时一次，单独复跑通过，见 `d2-WP-13b-to-M15` 第 12 条） |
| harness | `node perf/harness/selftest.mjs`；`make lint-m16-registry` | OK；142 个用例（D2.1 登记 20 个） |
| 静态检查 | `npm run typecheck`；`oxlint --type-aware perf/d21 perf/m18 perf/harness`；`make lint` | 0 错误；0 问题；通过 |
| vitest | `vitest run --project unit`（本包未改 `src/`，作回归） | 167 个文件 1372 passed、1 skipped |

注解中的实测（功能运行，不作结论）：viewer 倒计时 16.2 s、节能 21.0 s（阈值 20 s、预告 5 s），show-api 连接 13 ms 内消失；节能 10 s 主线程见 R2.1；
摇杆 30.3 Hz；容量计时见 R2.1；`seats-live` 中 B 离开后 45 s 内 A 仍 QUEUED，容量 `viewers = {live: 0, max: 1, queue: 1, state: full}`。

### R2.4 发现的问题

| # | 问题 | 归属 | 表达 |
|---|---|---|---|
| 1 | **观看席位满载时节能恢复永远轮不到（P0，D2-AC-43、44）**：页面以沙盒的 32 位十六进制提示（`ui/sandbox/session.ts` `principalHint()`，并覆盖共用键 `awr.principal_hint`）发 R81，show-api 只认 base32 提示，票据得到随机 principal；`seat_ready` 的预留与页面实时 token 的 principal 不符，页面一直 QUEUED，席位空着到预留过期。注解：`R01 BXLSJSMOLMFZNI6KQYJTRVX6IY \| R81 e8d5ee0be6130b529985d5ec0b75b6ea` | M15 | `seats-live` 在第 3 步后检测"R81 提示不是 token 提示"即声明 `test.fail`；`d2-WP-13b-to-M15` 第 10 条 |
| 2 | 公开模式带身份令牌的 GET 在 `/api/sys/audit`、`/api/rt/inspect` 上为 403 `115`（其余隐藏功能族 200；AWR-04 §15.9.3 要求按 admin 处理） | M11 | `test_identity_public_e2e.py` xfail；`d2-WP-13b-to-M11` 第 7 条 |
| 3 | 返航受理偶发失败：服务端 `105 STATE`（机体未离地），非界面缺陷 | 用例 | 已改用例（`waitFlying`） |
| 4 | D2-AC-41"应答后 ≤ 100 ms 显示"：一次 110 ms，差值为解析器插入行节点的时刻（应答先于节点到达） | M15、M00（观测） | 只记注解；`d2-WP-13b-to-M15` 第 11 条 |
| 5 | 容量应答 ≤ 1 s 缓存，立即读取可得旧值 | 用例 | `identity-live` 改为 2 s 内轮询；知会 M17（`d2-WP-13b-to-M17` 第 8 条） |
| 6 | 回环模式测试构建页面缺省以 operator 取 token 并占共享展示席位（D1 语义），离开后 30 s 宽限 | 用例 | 共享展示控制例改用演示构建并等待 FREE |

### R2.5 偏差（第 2 轮）

| # | 条款 | 偏差 | 理由与处置 |
|---|---|---|---|
| 1 | 任务"登记到 docs/18 测试清单" | 仍以请求提交（第 2 轮更新表格，`d2-WP-13b-to-文档总编.md` 第 5 条） | 18 号文档不在本包路径；第 1 轮请求尚未处理 |
| 2 | D2-AC-48"摇杆 ≥ 25 Hz" | 功能运行只断言 ≥ 10 Hz，25 Hz 在 `M16_PERF=1` 下断言 | 速率受 SwiftShader 与同机负载影响；实测 30.3 Hz |
| 3 | D2-AC-43"断开后 ≤ 5 s 轮到并自动连接" | 功能运行界限 15 s，5 s 在 `M16_PERF=1` 下；当前因 R2.4 第 1 条为预期失败 | 同上；服务端一侧的 ≤ 5 s 由 `test_viewer_power_e2e.py` 判定（通过） |
| 4 | D2-AC-51"免工作量证明" | 只验证"带身份令牌、不带工作量证明创建 201"，未验证"公开访客不带即被拒" | 公开访客的工作量证明按触发计数自适应（`broker.pow_required`），空闲站点上首个请求不要求 |
| 5 | 共享展示申请控制 | 在演示构建上验证 | 测试构建页面在回环模式缺省占席（R2.4 第 6 条），演示站才是该功能的使用场景 |
| 6 | `perf/m18/targets-panel.spec.ts`（D2 的 M18 用例）在 D2.1 工作包中修改 | 只改 `start()` 与去掉 `test.fail` | WP-12c 第 2 轮把该条发给 M16（本包），改动在 M16 路径内 |
| 7 | 第 1 轮偏差 5（D2-AC-44 与容量轮询冲突） | 已消解 | WP-12c 第 2 轮后覆盖层不轮询；文档口径仍请文档总编改（第 6 条） |

第 1 轮其余偏差（2、3、4、6、9）不变。

### R2.6 遗留

| 编号 | 事项 | 负责 |
|---|---|---|
| WP13b-L1 | 第 1 轮：WP-03b 合入后复跑 | 已完成（本轮） |
| WP13b-L2 | 第 1 轮：M15 修复缺陷后去掉 `test.fail` | 已完成（D2-INT 去掉；本轮复跑通过） |
| WP13b-L3 | MS5 性能运行：`d21.*`（`M16_PERF=1`，可用 `--runs 3`）、`d21.bench-capacity-d`、`d21.bench-power`、`d21.chaos-park`；D2-FX-R2-sandbox 第 3 条的复测清单 | M16 |
| WP13b-L4 | 变体 D 场与读数成本：池或 sandbox-api 暴露会话 perf 自报 | M17、M16 |
| WP13b-L5 | 18 号文档登记 | 文档总编 |
| WP13b-L6 | M17 测试改用 `tests/fixtures/identity.py` | M17 |
| WP13b-L7 | M15 修复观看排队的 principal（第 10 条）后 `seats-live` 按原断言通过（条件 `test.fail` 不再触发） | M15、M16 |
| WP13b-L8 | M11 修复身份令牌 GET（第 7 条）后去掉 `KNOWN_403` | M11、M16 |
| WP13b-L9 | 发给 M16 的 D2（非 D2.1）条目，属 WP-13：d2-INT-to-M20-M21-M16 第 2 条（S7 过渡剧本）；d2-ACC-1 第 2 条的 D2-AC-01、02、07、16、18、20、30 测量缺口、第 3 条 `relay_batch.evaluate_ab` 覆盖率判据与 `sandbox-live` 的 serial、第 5 条 `manual-keepout`；d2-FX-R2-web 第 2、4、5、6、8 条；d2-FX-R2-deploy 第 2 条（`honesty.spec.ts` 第 1 例）与第 4 条（`observe.py` 登记）；d2-FX-R2-sim、d2-FX-R2-other、d2-FX-R2-perception 的复测；d2-FX-R2-tasking 第 1、2 条（T5 资源与 S7 几何，需架构裁决） | M16（WP-13） |
| WP13b-L10 | 报告页脚"科研用途"口径 | 文档总编、M16、M15 |

### R2.7 请求（第 2 轮追加）

`d2-WP-13b-to-M15.md` 第 10–12 条、`d2-WP-13b-to-M11.md` 第 7–8 条、`d2-WP-13b-to-M17.md` 第 6–8 条、`d2-WP-13b-to-文档总编.md` 第 5–10 条。

---

## 第 1 轮（2026-10-06）

## 0. 摘要

1. **夹具**（`tests/fixtures/identity.py`）：运行时随机口令的 0600 密钥文件（与 M17 过渡夹具同接口）、身份库状态目录与环境、以同一 `K_ident`
   签发与撤销身份令牌、在文件树中查找口令（只报位置）、REL-04 的调用包装（只数 REL-04、检查输出不含口令）。
2. **真实栈扩展**（`tests/e2e/sbstack.py`）：命令行 `--env`、`--release-from <build-info.json>`（以被测构建写部署目录形式的 RELEASE，容量接口
   `build` 与之逐字段一致）、`--identity-standin`；`sbx_dir`、`parked_dir()`、`shm_dir()`、`capacity()`、`session()`、`pool_audit()`。
3. **R79/R80 替身**（`tests/e2e/identity_standin.py`）：show-api 尚未提供 R79/R80（WP-03b 未交付）。替身只是两条路由的薄壳，比对、限流、三级锁定、
   固定时延、令牌、撤销表与审计都是产品库 `awr.sandbox.identity`，状态目录与池相同，因此池的内部档验签与撤销是真实路径。pytest 只在
   `M16_IDENTITY_STANDIN=1` 时用它验证用例本身，缺省只对真实 R79 运行（缺失则 skip）；浏览器用例自动用它并以注解 `identity-backend` 记录。
4. **pytest 真实栈用例**：驻留与恢复（D2-AC-45）、容量与实例口径（D2-AC-42、43）、内部身份（D2-AC-51 服务端）、量化读数与场采样对拍
   （D2-AC-52、53）、观看节能兜底与席位（D2-AC-43、44；WP-03b 缺失时 skip）、混沌（池与 sandbox-api kill -9、驻留中途杀池）。
5. **Playwright 真实栈用例**（`apps/web/perf/d21/`，`live21.ts` 起栈与仿 nginx 预览）：启动屏、节能、身份、场视图与读数、移动设备矩阵；
   `gpu-limits-d21.spec.ts` 在 M06 测试服务器（FakeSource）上把 D2.1 全部界面放进规范最低上限模拟；`power-boot.spec.ts` 为新发现缺陷的回归。
6. **基准**：`capacity.py --variant D`（D2-AC-55：变体 A + 场与读数订阅 + 2 个驻留会话 + viewer 节能循环 + 容量接口 10 次/s + 可选内部档）、
   `power.py`（D2-AC-46：驻留前后的沙盒组 CPU 与内存、viewer 断开前后的 show-api CPU）；判据单测与 `--smoke` 用例。
7. **登记**：`apps/web/perf/d21/cases.mjs` 18 个用例（含 M15 的 `m15.d21-shell`、`m15.d21-mobile`）；`make lint-m16-registry` 139 个用例通过。

## 1. 实现清单（需求与验收编号）

### 1.1 pytest（`tests/e2e`、`tests/chaos`）

| 文件 | 用例 | 验收 |
|---|---|---|
| `test_sandbox_park.py`（主栈 N_max = 1、`sandbox_idle_s` 30、`grace_s` 5、`warn_s` 5、`idle.enter_s` 5、`idle_disconnected_s` 15；第二栈 `retention_s` 20、`preempt_after_s` 30、`notice_s` 5） | 客户端驻留：PARKED（0.6 s）、会话 sim-core 退出、tmpfs 目录删除、容量 `active` 0 与 `parked` 1、驻留目录 ≤ 8 MB 且含元数据与配置快照、寿命冻结、时钟端点 409 515 带 `resume_url`；有空位恢复：201 换发 token、READY（2.8–3.0 s，`M16_PERF=1` 时 ≤ 6 s）、epoch + 1、仿真时刻从驻留时刻继续、机群识别物机巢任务逐项一致（含驻留前新增的机体）、`session.resumed{checkpoint}`；SDK 遇 515 自动恢复；满员恢复票 `kind = resume` 第 1 位、之后的新访客第 2 位、槽位释放后恢复票先 `slot_ready` 并认领同一 sid；不兼容 checkpoint（元数据发布不同）退回快照（`planned = checkpoint`、`resume_mode = snapshot`）；带 WS 无交互兜底驻留（36–37 s，阈值 30 + 余量 5）原因 `idle` 与 `sandbox.expiring{action: park}`；无连接驻留（16–18 s）；保留到期 ENDED 且快照可取；抢占改为驻留（`preempted`）并可凭 sid 排队恢复 | D2-AC-45；M17-AC-027 至 029 |
| `test_capacity_e2e.py` | 公开无 token、≤ 1 KiB、`Cache-Control: public, max-age=1`、通过 `awr.capacity.v1`（契约合入前用 M17 草案）、不含访客标识；`build` 与 RELEASE 逐字段一致、`power` 为服务端阈值；创建、驻留、恢复、结束后 2 s 内与池一致（SPAWNING 起占槽位、PARKED 不占）；内部档用预留槽位（`internal_active` 1、分母不变）、第三个公开请求仍排队；状态文件新鲜、满、两者都满、停更 > 5 s 时的 `viewers` 与 `hint` | D2-AC-42、43（沙盒口径）、40（服务端一侧）；M17-AC-025、026 |
| `test_identity_e2e.py` | 池与 sandbox-api：内部档 token `tier: internal`、寿命上限 8 h、内部票排在公开票之前、每身份第三个 409 504 `per_identity`、公开会话不被抢占；他人密钥与格式错误 401 302；撤销 ≤ 2.5 s 生效（`identity_revoked`）。R79/R80（真实或替身）：200 令牌被池接受、正确与错误中位数差 ≤ 10 ms、R80 后 ≤ 2 s 失效、第 6 次 429 516（`Retry-After` ≈ 1800、`scope = prefix`）且锁定期内正确口令不比对、重启后锁定保留、审计四类、栈的日志审计状态中口令 0 处与令牌不入审计、REL-04 报告植入口令且不打印 | D2-AC-51；M17-AC-030、031 |
| `test_field_sampling_e2e.py`（真实 T1） | SB-74 四种指标的形状与单调；以本用例独立实现的前端插值规则在剖面上插值，与 SB-78 显式采样比较（声学 0.05 dB、视觉 0.4%、热 0.4%、雷达 0.04 dB，阈值 0.5 dB / 2%），并以独立的 Halton 图案复现采样点位；SB-75 的表与 M18 有效半径一致（≤ 1 m，详情半径按配置半径截断）、识别物锚点采样只在配置体内且与前端表模型 P 一致（最大差 0.0008）；SB-73 字段与内部关系（`p_ac` logistic、`n_eq = θ/角分辨率`、`ratio`、θ = D_vis/r、`p_any`）、斜距升序、`uav` 过滤；参数校验 422、未知识别物 404 543；未配置密钥文件时 403 115；实时 topic（网关未转发，xfail） | D2-AC-52、53（服务端与对拍）；M19-AC-021 至 023 |
| `test_viewer_power_e2e.py` | 观看连接无 presence 时 4430 + 518、presence 延后回收、V_max = 3 时 517 + 4431、R81 排队与 `seat_ready`、519、状态文件；按能力探测，WP-03b 缺失时 skip | D2-AC-43（观看）、44（服务端兜底） |
| `test_sandbox_power_bench.py` | 变体 D 与节能节省的判据单测（通过、不通过、高负载不判定、缺自报或 cgroup 不判定）；两个工具的 `--smoke` | D2-AC-55、46 |
| `tests/chaos/test_sandbox_park_chaos.py`（chaos + perf） | 驻留后 kill -9 池：重启后仍 PARKED、原因与仿真时刻不变、恢复且新增机体仍在；驻留后 kill -9 sandbox-api：恢复成功；驻留中途 kill -9 池：会话不丢失 | D2-AC-45（池重启） |

### 1.2 Playwright（`apps/web/perf/d21/`）

| 文件 | 内容 | 验收 |
|---|---|---|
| `live21.ts` | D2.1 栈（`sbstack.py` + 测试阈值 + RELEASE + 运行时口令与替身）、仿 nginx 预览（含 `/api/auth/identity` 路由）、构建（`D21_DIST`、`D21_DEMO_DIST`，缺省构建到 `.cache/d21/`）、会话 REST、show-api 实时连接数（R58 `/api/rt/inspect`；页面的实时连接在 Worker 中，`page.on('websocket')` 看不到）、设备上下文、命中区审计、无横向滚动、字形扫描、Toast 记录 | — |
| `limits.ts` | 规范最低上限与移动组的严格模拟（与 M06 同一规则，另检查纹理尺寸） | ADR-086、087 |
| `boot-live.spec.ts` | 禁用 JS 的版本行 = build-info、meta、五段宽度；真实容量 `build` 与 build-info 逐字段一致、12 s 内无"站点已更新"；唯一槽位被占时遮罩期间即显示"沙盒 1 / 1 在用 · 排队 0 人 · 预计约 30 min"与"开启沙盒需排队"，遮罩期间请求间隔 ≥ 5 s，开启沙盒 Sheet"需排队 · 第 1 位"，演示构建落地页同样显示并在槽位释放后变为可用；容量接口不可达时显示"不可用"且照常揭开 | D2-AC-40、41、42 |
| `power-live.spec.ts` | viewer：倒计时（13 s 后）、节能（18 s 后）、show-api 连接 ≤ 2 s 内消失、10 s 内帧增量 ≤ 1 且除容量外无请求、主线程占比（`M16_PERF=1` 时 60 s ≤ 1%）、首次拖动恢复、连接与帧恢复；沙盒：SB-69 驻留（池 PARKED，原因 idle，槽位释放）→ 另一访客占满 → 任意键 SB-70 → 202 → 节能覆盖层内"恢复沙盒需排队 · 第 1 位"（QUEUED）→ 另一会话结束后页面自行认领、同一 sid、epoch + 1、回到 ACTIVE；演示构建按服务端阈值（容量接口下发）节能与恢复 | D2-AC-44、45、46（浏览器）、43 |
| `power-boot.spec.ts` | 揭开前不应计时（缺陷回归，`test.fail`） | D2-AC-44 |
| `identity-live.spec.ts` | 缺省折叠；错误口令"口令不正确"；正确口令徽标"内部 · 全功能"；口令不在存储、页面文本、HTML 与控制台；公开槽位被占时经开启沙盒 Sheet 在预留槽位上创建内部档会话（token `tier: internal`、寿命 > 3 h、`internal_active` 1）；退出后 ≤ 2.5 s 池拒绝旧令牌；5 次错误后第 6 次"尝试次数过多，请 30 分钟后再试"且按钮禁用 | D2-AC-51 |
| `field-live.spec.ts` | 读数卡逐行等于 SB-73 的服务端数值（主导带级 / 阈、角尺寸 / 阈、合成概率、读数句）且无写请求；真实沙盒页面上场视图五档与 Shift+V、感知体全部，`programs` 不增、无 GL 告警与错误；实时场帧（网关未转发时 skip 并注明） | D2-AC-52、53 |
| `mobile-live.spec.ts` | iPhone 15 竖横、Pixel 8、iPad Pro 11 竖横：设备档与外壳、手机五窗口（机群列出共享展示的 7 架实时机体）、平板停靠列表与任务条 / 竖屏抽屉、每个窗口无横向滚动、命中区审计 0 违例、字形 0；演示构建手机 viewer 无指令入口；手机 operator：悬停与返航各 ≤ 4 次点按、悬停"指令已受理"、返航由服务端状态确认；"盯防"与返航受理 Toast 为 `test.fail`（缺陷 1、2）；平板任务条悬停受理、旋转保留选择与连接、出现抽屉、无溢出 | D2-AC-47、48 |
| `gpu-limits-d21.spec.ts` | 测试构建（桌面 min、手机 mobile + Adreno、平板 mobile + Adreno）与演示构建（桌面 min、手机 mobile）：启动屏与揭开、经界面切换四种场视图（测试构建注入 M06 合成剖面，点数 > 0）、手机五窗口、节能覆盖层（按需渲染、尺寸变化补帧、拖动恢复）、设置页身份区；落地页。WebGL 错误 0、[gpu-limits] 拒绝 0、three 告警 0、页面错误 0、每个程序块数与块大小在限值内、移动组 `pointSizeMode = quad`、测试构建揭开后 `programs` 不增 | D2-AC-54、49；d2-WP-11b-to-M16 第 2 条（演示构建走界面） |
| `cases.mjs` | 18 个用例：7 个 Playwright（`d21.*`）、M15 的 2 个、7 个 pytest、2 个基准 | `make lint-m16-registry` |

### 1.3 基准（`tools/bench/sandbox/`）

| 工具 | 内容 | 验收 |
|---|---|---|
| `capacity.py --variant D [--internal]` | 先建 2 个 T1 会话并驻留；空载与 10 次/s 各 60 s 对照 sandbox-api CPU；N_max 个满载会话 + operator 订阅场与读数 + `FieldViewers`（订阅声学场、感知体与读数，每 2 min 断开 30 s 再连）+ `CapacityProbe`（10 次/s，p50/p95）+ 可选内部档会话（运行三分之一时，运行时随机口令）；判据：变体 A 全部 + 容量 p95 ≤ 50 ms、错误 0、CPU 增量 ≤ 0.01 核、驻留会话不在进程表、驻留目录 ≤ 16 MB、内部档期间 L2 及以上连续 ≤ 10 s、viewer 节能循环发生、场与读数成本 ≤ 2 ms/仿真秒（读 perf 自报 `slow_ms_per_s.m19.field`，缺失时不可判定） | D2-AC-55 |
| `power.py` | `sandbox` 子组基线 → 满载会话 → 驻留后的 CPU 与匿名页 + shmem；24 个 viewer 连接与断开前后的 show-api CPU；判据：CPU 降 ≥ 0.12 核、内存降 ≥ 120 MB、驻留后 ≤ 基线 + 10 MB、驻留目录 ≤ 8 MB、viewer CPU 降 ≥ 0.3 核（按 viewer 数折算） | D2-AC-46（P1）、D2-AC-45 内存回落 |

## 2. 设计要点

1. **真实栈优先，替身只替缺失的端点**：D2.1 的服务端（池、驻留、容量、内部档、量化与场 REST）都已交付，用例一律在真实 supervisor 与真实会话
   sim-core 上运行；唯一缺失的 show-api R79/R80 用身份库本身的薄壳替代，并把"谁回答的"记录在注解里。show-api 的其余 D2.1 能力（presence、席位、
   4430/4431、状态文件、实时场 topic）不做替身：用例按能力探测 skip 或 xfail，交付后自动生效。
2. **观察不触碰**：兜底驻留用例只读公开容量接口与 `/proc` 判断驻留时刻（任何会话内 REST 调用都会被计为交互）；浏览器侧的实时连接在 Worker 中，
   用 show-api 的 R58 inspect 计数代替 `page.on('websocket')`。
3. **独立对拍**：场采样用例按 AWR-04 §15.11 的公式独立实现解码、插值、Halton 图案与表模型，再与服务端剖面和显式采样比较，不调用产品的解码函数。
4. **构建与服务端同源**：`--release-from` 以被测前端的 build-info 写 RELEASE，使"版本号逐字段一致"与"站点已更新"成为真实比较而非路由改写。
5. **已知缺陷的表达**：产品缺陷导致的失败用 `test.fail`（修复后提示去掉标记），依赖未交付用 skip 或 xfail 并写明请求；性能阈值只在 `M16_PERF=1` 断言。

## 3. 偏差

| # | 条款 | 偏差 | 理由与处置 |
|---|---|---|---|
| 1 | 任务"登记到 docs/18 测试清单" | 只登记到 `apps/web/perf/d21/cases.mjs`；18 号文档的 9 行登记表写入 `d2-WP-13b-to-文档总编.md` 第 1 条 | 18 号文档不在 WP-13b 的路径内（AWR-04 §15.15.1），按"只写本包路径"以请求提交 |
| 2 | D2-AC-51 方法 `tests/rt/test_identity.py` | 服务端身份用例写在 `tests/e2e/test_identity_e2e.py` | `tests/rt` 属 M11；本包的 e2e 目录覆盖真实栈口径 |
| 3 | D2-AC-45 方法 `tests/e2e/test_sandbox_park.py` 的"沙盒组内存回落到创建前 + 10 MB" | 在 `tools/bench/sandbox/power.py` 测量 | 需要 cgroup 模拟与稳定窗口，属基准；冒烟观测见 §4.4 |
| 4 | D2-AC-47 设备 | iPhone 15、Pixel 8、iPad Pro 11（任务指定），未用 iPhone 14、Pixel 7、iPad 10.9 | 同档；转文档总编（请求第 4 条）；M15 的 `d21-mobile` 另含 iPad Pro 12.9 coarse 与 320 px |
| 5 | D2-AC-44"节能期间无网络请求" | 容量接口请求列为例外 | 与 AWR-04 §15.5.3 冲突，转文档总编（请求第 2 条） |
| 6 | D2-AC-48"前往" | 手机快捷指令只测悬停、返航与盯防，未测"前往"（需在视口点地） | 视口点地依赖 SwiftShader 下的拾取与相机位姿，真机人工记录；M15 的 `d21-mobile` 以 FakeSource 覆盖了前往的点按计数 |
| 7 | D2-AC-53 实时帧 | 场视图在真实栈上没有数据（网关未转发），绘制与上限部分用 M06 的合成剖面（`gpu-limits-d21`）与服务端 REST 对拍（pytest）覆盖 | d2-WP-13b-to-M11 第 4 条 |
| 8 | 节能用例的测试阈值 | 真实栈与 GPU 上限用例把阈值设为 20 s 与 45 s（大于揭开时间） | 节能计时从页面加载开始（缺陷 3）；短于揭开时间的阈值会把遮罩卡住 |
| 9 | D2-AC-51"正确与错误口令响应时间中位数差 ≤ 10 ms" | 功能运行各 4 次、只判定 ≤ 25 ms；`M16_PERF=1` 时另起放宽锁定阈值的栈、各 30 次判定 10 ms | 补足时延带 ±25 ms 均匀抖动，4 次样本的中位数差常超过 10 ms（实测 11.3 ms 一次）；样本量又受聚合前缀锁定（10 次/10 min，成功不清零）限制 |

## 4. 测试结果

运行环境：本机 8 vCPU（Xeon E5-2603 v4）、无 GPU、SwiftShader（C1）；同机无其他工作包运行（负载 0.2–3）；构建为测试构建（`ci` 通道，固定
`abcdef0123`）与演示构建，`AWR_PORT_OFFSET=31`。

### 4.1 pytest

| 范围 | 命令 | 结果 |
|---|---|---|
| D2.1 真实栈 e2e 与基准工具（含 WP-13 的 SDK 端到端回归，`sbstack.py` 改动之后） | `pytest -m "not perf" tests/e2e/test_sandbox_park.py test_capacity_e2e.py test_identity_e2e.py test_field_sampling_e2e.py test_viewer_power_e2e.py test_sandbox_power_bench.py test_sandbox_sdk_e2e.py` | 34 passed、8 skipped、3 xfailed（8 min 43 s）。skip：R79/R80 4 个（show-api 未提供，WP-03b）、观看兜底与席位与状态文件 4 个（WP-03b）；xfail：PARKED 时领域路由 503 211、场 topic 未转发、WP-13 已登记的场景二机型 id |
| 身份（改版后单独复跑） | `pytest tests/e2e/test_identity_e2e.py`；`M16_IDENTITY_STANDIN=1 pytest …` | 缺省 4 passed、4 skipped；替身模式 9 passed（R79 中位数 357.5 / 354.0 ms，各 4 次） |
| 混沌 | `pytest -m chaos tests/chaos/test_sandbox_park_chaos.py` | 3 passed（功能运行，未取性能锁） |
| 基准判据与冒烟 | `pytest tests/e2e/test_sandbox_power_bench.py` | 5 passed（变体 D 冒烟含内部档会话与 2 个驻留会话、容量接口样本 > 100 且无错误；`power.py` 冒烟驻留 0.7 s、驻留目录 2.7 MB） |
| 逐项实测（`-s` 输出） | — | 驻留 0.59–0.6 s、恢复到 READY 2.8–3.0 s；兜底驻留 36.2–37.3 s；无连接驻留 16.3–17.5 s；场对拍最大误差：声学 0.048 dB、视觉 0.39%、热 0.40%、雷达 0.041 dB；感知体 P 最大差 0.0008 |

### 4.2 Playwright（`--project perf`，功能运行）

整套 `npx playwright test perf/d21 --project perf` 一次运行（17.8 min）：23 passed、1 skipped，另 1 个失败为平板 GPU 上限用例在雷达视图的一次性
检查早于渲染器按数据重建（WP-11b 的重建节流 ≥ 0.9 s）；改为轮询等待重建后该文件复跑 6 passed（10.1 min）。各文件结果：

| 文件 | 结果 |
|---|---|
| `perf/d21/boot-live.spec.ts` | 3 passed |
| `perf/d21/power-live.spec.ts` | 3 passed（viewer 倒计时与节能时刻满足 ≥ 13 s 与 ≥ 18 s，show-api 连接 ≤ 2 s 内消失；沙盒满载恢复排队后自行认领） |
| `perf/d21/power-boot.spec.ts` | 1 expected failure（缺陷 3） |
| `perf/d21/identity-live.spec.ts` | 2 passed（`identity-backend = standin`） |
| `perf/d21/field-live.spec.ts` | 2 passed、1 skipped（实时场帧未转发） |
| `perf/d21/mobile-live.spec.ts` | 10 passed（其中 2 个为预期失败：缺陷 1、2） |
| `perf/d21/gpu-limits-d21.spec.ts` | 6 passed（测试构建 3、演示构建 2、落地页 1） |
| M15 `perf/m15/d21-shell.spec.ts`、`d21-mobile.spec.ts`（以本包构建复跑，登记为 `m15.d21-shell`、`m15.d21-mobile`） | 9 + 11 = 20 passed（5.6 min） |

截图写到 `AWR_SHOTS_DIR`（32 张），抽查 5 张（启动屏排队、恢复排队卡片、读数卡、iPhone 15 机群窗口、平板指挥器），界面观测见 §5 与 `d2-WP-13b-to-M15.md` 第 4–7 条。

### 4.3 静态检查

`make lint` 通过（退出码 0；含 ruff、oxlint、no-emoji、registry 139 个用例）；`npm run typecheck` 0 错误；`oxlint --type-aware perf/d21` 0 问题；
`vitest run --project unit`（本包未改 `src/`，作回归）162 个文件 1332 passed、1 skipped。全量 `pytest -m "not perf"`（约 1 h）未在本包内重跑：
本包只改 `tests/e2e`、`tests/chaos`、`tests/fixtures/identity.py`、`tools/bench/sandbox`，受影响的集合已全部运行（§4.1）。

### 4.4 未执行的性能项与冒烟观测

未执行（并行阶段约束）：D2-AC-41 揭开劣化、D2-AC-44 主线程占比、D2-AC-45 恢复 ≤ 6 s 的计时判定、D2-AC-46、D2-AC-53 切换 ≤ 300 ms、D2-AC-55。
MS5 按 ADR-033：`capacity.py --variant D --internal`（30 min）、`power.py`、harness 用例 `d21.*`（`M16_PERF=1`）。

冒烟观测（不作结论）：`power.py --smoke`（8 s 窗口，cgroup 模拟）满载会话沙盒组 0.78 核、驻留后 0.04 核；匿名页 + shmem 创建前 87 MB、满载 239 MB、
驻留后 104 MB（比创建前 +17 MB，阈值 +10 MB，转 M17 第 5 条）；驻留目录 2.7 MB；驻留 0.71 s。

## 5. 发现的问题

| # | 问题 | 归属 | 表达 |
|---|---|---|---|
| 1 | 手机快捷指令"盯防"在真实沙盒中列不出识别物（只读识别物 store，未用识别物窗口的退回数据源） | M15 | `mobile-live` `test.fail`；d2-WP-13b-to-M15 第 1 条 |
| 2 | 快捷指令的受理 Toast 等最终结果：返航要到降落才提示 | M15 | `mobile-live` `test.fail`；第 2 条 |
| 3 | 节能计时从页面加载开始：阈值短于揭开时间（或后台标签页加载）时遮罩永不揭开 | M15 | `power-boot` `test.fail`；第 3 条 |
| 4 | 恢复排队卡片"仿真时间 —"；QUEUED 时顶栏"已断开"与红色帧间隔；读数卡横向截断；手机识别物窗口打开为空详情页 | M15 | 第 4–7 条 |
| 5 | PARKED 时领域路由 503 211（应 409 515）；场与读数 topic 未转发；R79/R80、presence、席位、状态文件未交付 | M11 | xfail / skip；d2-WP-13b-to-M11 |
| 6 | 恢复后无访客时钟即播放；perf 自报中的 M19 成本未经池暴露（变体 D 判据不可判定）；驻留后内存比创建前 +17 MB（冒烟） | M17 | d2-WP-13b-to-M17 第 3–5 条 |
| 7 | D2-AC-44 与 §15.5.3 冲突；18 号文档登记；设备型号 | 文档总编 | d2-WP-13b-to-文档总编 |

## 6. 遗留

| 编号 | 事项 | 负责 |
|---|---|---|
| WP13b-L1 | WP-03b 合入后复跑 `test_viewer_power_e2e.py`、`test_identity_e2e.py`（第 2 组改走真实 R79）、`field-live` 实时帧、`test_field_and_quant_topics…`（去掉 xfail 判断后的断言即生效） | M16、M11 |
| WP13b-L2 | M15 修复缺陷 1–3 后去掉 `test.fail`；节能用例阈值恢复为短阈值 | M15、M16 |
| WP13b-L3 | MS5 性能运行：`d21.*`（`M16_PERF=1`）、`d21.bench-capacity-d`、`d21.bench-power`、`d21.chaos-park` | M16 |
| WP13b-L4 | 变体 D 的场与读数成本：池或 sandbox-api 暴露会话 perf 自报后接入采样器 | M17、M16 |
| WP13b-L5 | 18 号文档登记 D2.1 用例表 | 文档总编 |
| WP13b-L6 | M17 测试改用 `tests/fixtures/identity.py` | M17 |

## 7. 文件清单

- 夹具与辅助：`tests/fixtures/identity.py`（新）、`tests/e2e/identity_standin.py`（新）、`tests/e2e/sbstack.py`（改：命令行与 D2.1 辅助）。
- pytest：`tests/e2e/{test_sandbox_park,test_capacity_e2e,test_identity_e2e,test_field_sampling_e2e,test_viewer_power_e2e,test_sandbox_power_bench}.py`、
  `tests/chaos/test_sandbox_park_chaos.py`（新）。
- Playwright：`apps/web/perf/d21/{live21.ts,limits.ts,boot-live.spec.ts,power-live.spec.ts,power-boot.spec.ts,identity-live.spec.ts,field-live.spec.ts,mobile-live.spec.ts,gpu-limits-d21.spec.ts,cases.mjs}`（新）。
- 基准：`tools/bench/sandbox/capacity.py`（改：变体 D）、`tools/bench/sandbox/power.py`（新）。
- 请求：`.cache/impl/requests/d2-WP-13b-to-{M15,M11,M17,文档总编}.md`。
