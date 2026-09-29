# MS1/MS2 集成验证报告

| 项 | 内容 |
|---|---|
| 工作包 | MS1/MS2 集成验证（本阶段可修改任何路径以修复集成问题，越权修改逐条列于第 5 节） |
| 日期 | 2026-09-29 |
| 依据 | AWR-03 §3.3、§4.2、§4.3、§5.11、§8.4（D1-AC-01、13、20、35）、§8.6（D1-MS1、D1-MS2）；AWR-10 §3.3；AWR-11 §7.3；AWR-17 §4.3.12、§9.5；AWR-18 §10、§13；AWR-19 §4.3；M03、M04、M11、M15 PRD；工作包报告 M00-A、M00-B、M15-S、M11-R、M03-M04；`.cache/impl/requests/` 全部 32 份请求 |
| 结论 | 修复后 `make lint`、`make test-contracts`、`pytest -m "not perf"`、`npm run build`、`tsc`、Vitest（unit + browser）、`worldpkg validate worlds/* --deep`、Playwright 冒烟与 `make ci` 全部通过。出口核对：D1-AC-13（契约部分）通过；D1-AC-20（lint 部分）通过；D1-AC-01 构建侧全部满足，但"`/world/<id>` 可访问"未满足（api 未交付）；D1-AC-35 未通过（`FakeSource.ts` 等 M11 前端 net 与 Gateway 最小协议栈未交付）。因此 D1-MS1、D1-MS2 **均未完全达到出口** |

## 1. 结论摘要

| 出口项 | 所属里程碑 | 判定 | 未通过的原因 |
|---|---|---|---|
| D1-AC-13（契约部分） | MS1 | 通过 | — |
| D1-AC-20（lint 部分） | MS1 | 通过 | — |
| D1-AC-35 | MS1 | 未通过 | Python 部分（fake_gw 合成与回放、夹具 golden）通过；`FakeSource.ts`、RtClient、rt.worker 仍是占位；"无后端跑 flight60 `scene=full`"依赖 MS5 的点云引擎与 harness |
| D1-AC-01 | MS2 | 未通过（构建侧四项通过，访问侧一项未通过） | 删除世界后 `make run` 自动重建已验证；api 因 `awr.api.main` 不存在而 FAILED，`/world/<id>` 不可访问（M11 §12 把静态服务排在 MS3） |

## 2. 执行的检查与结果

首次运行只有 1 处失败（pytest `tests/runtime/test_cli.py`），其余命令首次即通过；另在核对中发现 11 处集成缺陷（第 3 节），全部修复后重跑。

| 检查 | 命令 | 首次结果 | 修复后结果 |
|---|---|---|---|
| 全部 lint | `make lint` | 通过（rc 0，8.6 s） | 通过（rc 0，10.3 s；新增 DET-01、PY-CB-01、PERF-01 与完整包边界表） |
| 契约 CI | `make test-contracts` | 通过：6 个生成器 `--check` up to date（layout_id 0x3D5E08D0）；pytest 286 通过、2 跳过；Vitest 7 个文件 225 例 | 通过：pytest 293 通过、2 跳过；Vitest 7 个文件 225 例 |
| pytest 全量 | `pytest -m "not perf"` | 1 失败、810 通过、2 跳过、12 未选（perf） | 831 通过、2 跳过、12 未选（240.6 s） |
| 前端构建 | `npm run build -w apps/web` | 通过（1.5 s） | 通过；ui 块 727 kB、three 块 737 kB 有 > 500 kB 提示（见遗留） |
| 类型检查 | `tsc -p apps/web/tsconfig.json --noEmit` | 通过 | 通过 |
| Vitest | `vitest run --project unit --project browser` | 23 个文件 315 例通过 | 同左；清空 `node_modules/.vite` 后冷启动 3 次均通过 |
| World 深校验 | `worldpkg validate worlds/* --deep` | 六城 OK，errors = 0、warnings = 0，每城 2.3–3.9 s | 同左 |
| World 校验入口 | `make validate` | 只含 worldpkg | 追加 Ajv strict：71 份文档 0 份不合法（19.7 s） |
| 缺失判定 | `make worlds` | 六城 up to date，geo warm 全部命中（3.4 s） | 同左 |
| 自动重建 | 硬链接副本删除 shenzhen 后 `AWR_WORLDS_DIR=<副本> make run` | — | 重建 26.7 s，contentVersion `cf5fcd7b3791` 与原件逐字节相同；supervisor 打印 READY；api FAILED（`module_missing:awr.api.main`），`GET /world/shenzhen` 连接被拒 |
| 原始数据 | `make fetch-data VERIFY=1` | — | 六个 PLY 字节数与 sha256 校验通过（4.9 s） |
| 早期数据源 | `make test-fixtures` | 目标不存在 | 新增后通过：`gen_fixtures --check`；pytest 21 例；Vitest `frame.test.ts` 7 例 |
| M15 冒烟 | `make smoke-m15` | 7 例通过（33.8 s） | 7 例通过（33.8 s） |
| 集成冒烟 | `npx playwright test ms-smoke --project=e2e` | 配置的 webServer 下构建产物 502（代理遮挡） | 3 例通过（15.6 s）：首页无 pageerror、`/assets/*` 全部 200、`/worlds` 路由与数据路径分流、`/api` 经 preview 代理到 fake_gw |
| 本机 CI | `make ci` | — | 通过（4 min 36 s；lint → contracts-check → tsc → vite build → pytest → Vitest） |

两处 pytest 跳过均为下游模块未交付时的对拍：`test_state_model.py::test_m08_state_model_if_present`（M08）、`test_env_golden.py::test_m07_implementation_if_present`（M07）。12 个未选用例为 perf 标记（按任务排除）。唯一警告来自第三方 `starlette.testclient` 的 httpx 弃用提示。

## 3. 发现并修复的集成问题

| # | 问题 | 影响 | 修复 | 依据 |
|---|---|---|---|---|
| 1 | `tests/runtime/test_cli.py::test_external_subcommands_not_delivered` 断言 `awr data fetch urbanscene3d` 退出码为 8，M03 交付 `awr.jobs.awr_cli` 插件后返回 0 | pytest 与 `make ci` 失败 | 该用例只保留 `awr backup` 为 8；新增"插件已注册"（`awr data --help` 列出 fetch）与"无插件时回退为退出码 8"两例 | M03-M04 报告 §1.5；AWR-19 §16.2 |
| 2 | `vite.config.ts` 代理 `/assets` 与 `/worlds`；preview 继承 server.proxy | `vite preview` 与 Playwright 配置的 webServer 下全部 JS、CSS 为 502，页面只剩静态遮罩；`/worlds`（World Hub 路由）被转发到后端 | 只代理 `/api`（含 WS）与 `^/worlds/.+`，preview 显式使用同一张表 | AWR-03 §3.3（`/assets/` 是构建产物前缀，不作代理）；16 F-09；R2-42；M15-FR-003 |
| 3 | `bus/geo.schema.json` 的 op 枚举为 `height` 等旧名，回复不允许 `detail` | M04 探针的合法请求与失败回复都被契约拒绝 | op 改为 M04 §7.3 的 10 个名字并保留 V0.2 的 raycast、los、lidar；回复增加可选 `detail`；新增 `tests/world_query/test_bus_contract.py`（11 例）以 GeoProbeServer 真实回复校验 | M04 §7.3；M03-to-M00 第 2 条 |
| 4 | `rt/payloads/sys_procs.schema.json` 状态枚举缺 STOPPING、多 EXITED；字段名与 17 不一致，supervisor 双写 | supervisor 停止进程期间的 `sys/procs` 不合契约（本次 `make run` 停止时确有 STOPPING） | 按 17 §4.3.12 冻结六态与字段名（`hb_age_ms`、`last_exit`、`uptime_s`、`log_tail`），supervisor 附加字段登记为可选；删除 supervisor 输出中重复的 `heartbeat_age_ms` | AWR-17 §4.3.12；AWR-19 §4.2；M11-R-to-M00 第 1 条 |
| 5 | `bus/event.schema.json` 不允许外层 `batch_id` | 批量命令逐机事件（M11 §6.4.12）不合契约 | 登记可选 `batch_id: string \| null` | M11-FR-061、§6.4.12；M11-R-to-M00 第 2 条 |
| 6 | `perf/bench-result.schema.json` 没有 IPC 指标位置 | bench_state、bench_cmd 的门禁指标只能另写非契约文件 | 登记可选 `records[].ipc`（带单位后缀键名的扁平数值映射） | AWR-18 §7.6；AWR-03 §5.11 第 1 条；M11-R-to-M00 第 3 条 |
| 7 | `make test-fixtures` 未定义 | D1-AC-35、DATA-AC-018、M11-AC-040 的测试方法无法执行 | `mk/contracts.mk` 新增：夹具 `--check` → pytest 夹具 golden 与 fake_gw → Vitest TS 夹具解码与 `tests/net`、`tests/m11`（FakeSource 交付后自动纳入） | AWR-18 §12.2；M11-R-to-M00 第 4 条 |
| 8 | Ajv strict 校验只在 M03 测试目录的草稿脚本中，`make validate` 不含 | D1-AC-01 的"Ajv strict 校验通过"不在其测试方法 `make validate` 中 | 采纳为 `tools/contracts/check-world.mjs`，`make validate` 调用；M03 测试改用新位置，删除草稿 | M03-to-M00 第 1 条；AWR-16 §15.1 |
| 9 | 18 §13.1 的 DET-01、PY-CB-01、PERF-01 未接入 `make lint`；`check_py_imports.py` 只实现受限第三方库，`awr.api` 中 `import awr.sim` 不会被拦截 | "make lint（全部 lint 规则）"不完整；ARCH-AC-001 的变异样例漏检 | DET-01 进 `.oxlintrc.json`；新增 `check_py_callbacks.py`（PY-CB-01）与 `tools/ci/check-perf-flags.mjs`（PERF-01）并登记到 `mk/lint.mk`；`check_py_imports.py` 按 10 §3.3 规则 2 补全包边界表（含相对导入解析、`from awr import x` 形式、awr.api 白名单 `tools/lint/py-imports.toml`）与仿真路径墙钟禁用；新增 3 个 lint 单元用例 | AWR-18 §13.1；AWR-10 §3.3、ADR-045、ADR-049；AWR-03 §4.2；ARCH-AC-001 |
| 10 | check-brand（BRAND-02）把模块路径 `@/ui/brand/PanelEmpty` 当作品牌资产 | M15 只能绕道桶文件导入 | 跳过 import、export、`import()` 的模块说明符（说明符本身是图片文件时仍报）；selftest 补用例 | M15-to-M00 第 3 条 |
| 11 | `apps/web/.vitest/`（浏览器用例失败截图）未忽略 | 引入 git 后会被误提交 | `.gitignore` 增加 `/apps/web/.vitest/` | M15-to-M00 第 4 条 |

## 4. D1-MS1、D1-MS2 出口逐条核对

### 4.1 D1-AC-13（契约部分）：通过

| 子项 | 判定 | 证据 |
|---|---|---|
| `state_model` 7 组断言 | 通过（对 oracle） | `tests/contracts/test_state_model.py::test_group1_custom_mode_bijection` 至 `test_group7_admission_spot_checks_and_matrix` 全部通过，oracle 为 `tools/contracts/state_model_ref.py`；M08 实现交付后 `test_m08_state_model_if_present` 自动对拍（当前跳过） |
| layouts 字节往返；Python 与 TS 解码一致 | 通过 | `test_layouts.py::test_golden_bin_round_trip`（5 种 raw 布局各 1 万条）；`apps/web/tests/contracts/layouts.test.ts` 对全部 golden `.bin` 逐字节重编码；`frame.test.ts` 四个 `.awrrt` 夹具经 TS 参考路径解码后与 `fixtures/payloads/*.json` 相等 |
| 环境 golden | 通过 | `test_env_golden.py`（10030 例，presets sha256 自洽）；两端对拍待 M07 |
| 帧换算 golden（含 `uavNN/local`、GPST/UTC） | 通过 | `golden/frames/local_px4.json`、`time.json`；`tests/georef/test_frames.py`、`test_px4_geo.py`、`test_time.py::test_gpst_minus_utc_2026_is_18s`；TS 子集 3445 例 `apps/web/tests/geo/frames.test.ts` |
| TIME 编解码（含 LIVE 与 bit7） | 通过 | `golden/rt/time.json` 40 例；`test_time_frame.py::test_time_golden`、`test_time_covers_all_states_and_bits`；`apps/web/tests/contracts/time.test.ts` |
| 单位后缀字典 | 通过 | `rt/units.json`；`check-units.mjs` ok；`test_lint_tools.py::test_repository_passes_unit_suffix_lint` |
| 生成物一致 | 通过 | `make contracts-check`：6 个生成器 up to date；本次 4 个 schema 修订后重新生成 `schema_types.py` 与 `types.ts`，`--check` 通过 |
| GPU 采样（`env-gpu.spec.ts`） | 不属契约部分 | M07，MS5 |

### 4.2 D1-AC-20（lint 部分）：通过

`make lint` rc = 0，10.3 s（18 §13.1 要求 ≤ 90 s）。

| 规则 | 工具 | 结果 |
|---|---|---|
| emoji 为 0、禁用字形为 0（EMOJI-01、GLYPH-01，含 docs 指定文档） | `no-emoji.mjs` | ok |
| token 以外 hex 为 0（VIS-L-01、02） | `no-hex.mjs` | ok |
| `backdrop-filter` 为 0（VIS-L-03）与 LF 规则、色卡 LF-PAL-01..05 | `lint-lf.mjs --palette` | ok；冒烟中计算样式 `backdrop-filter` 全部为 none |
| `lucide-react` import 为 0 | oxlint TS-BND-01、`check-deps.mjs` imports/boundary、`check-icons.mjs` ICON-01 | ok |
| `no-raw-controls` 违规为 0（RAW-01） | `no-raw-controls.mjs` | ok |
| motion-lint（MOT-01..04） | `motion-lint.mjs` | ok |
| check-icons（ICON-01..04，232 个语义 key） | `check-icons.mjs` | ok |
| `check_py_imports`（Python 包边界，§4.2） | `check_py_imports.py` | ok（142 个文件；本次补全 10 §3.3 规则 2） |
| check-brand（BRAND-01..03） | `check-brand.mjs`（`--strict` 亦通过） | ok |
| 其余 18 §13.1 规则 | ruff、oxlint type-aware、UNIT-01、deps/*（降级，无 BOM）、DET-01、PY-CB-01、PERF-01、`lint-m15`（生成物 `--check`、codemod 幂等、CN-01） | 全部 ok；MMD-01 未接入（只报告，需 mermaid 依赖，见遗留） |
| reduced 档 Base UI 动画数、运行时净化、徽章宽度 | Playwright `motion.spec.ts`、`sanitize.spec.ts`、`brand.spec.ts` | 不属 lint 部分（MS5 出口）；`lib/sanitize.ts` 单测已在 Vitest 中通过 |

### 4.3 D1-AC-35：未通过

| 子项 | 判定 | 证据 |
|---|---|---|
| `make test-fixtures` 存在且通过 | 通过（本次新增） | `gen_fixtures.py --check` up to date；pytest 21 例；Vitest 7 例 |
| `fake_gw.py` 按 layouts 合成 N ∈ {1, 200, 1000} 的 BATCH、TIME、EnvKeyframe 与事件，参考客户端解码与 golden 一致 | 通过 | `tests/runtime/test_fake_gw.py`（控制消息过 `rt/ops.schema.json`，roster、EnvKeyframe、state_ext、perf/server、sys/procs 过各自 schema，Lite32、Full64 按生成 dtype 解码） |
| 回放 `.awrrt` 夹具 | 通过 | 四个夹具回放字节一致，`decode_fixture` 与 payload golden 相等；fake_gw 用 `awr.contracts.frame.write_awrrt/read_awrrt` |
| `FakeSource.ts`（与 RtClient 同接口，合成与回放） | 未通过 | `apps/web/src/net/rt/FakeSource.ts` 只有空实现（M15-S 占位）；RtClient、rt.worker 亦为占位；M11 §12 把它们列为 MS1 交付，M11 网关工作包尚未开始 |
| 前端无后端跑 flight60 `scene=full` | 未通过 | 缺 FakeSource；点云引擎、无人机图层与 flight60 harness 属 MS5 |

### 4.4 D1-AC-01：未通过（构建侧通过）

| 子项 | 判定 | 证据 |
|---|---|---|
| `worldpkg validate worlds/* --deep` 零错误 | 通过 | 六城 errors = 0、warnings = 0 |
| 单城构建 ≤ 60 s（load ≤ 6） | 通过 | `make run` 自动重建深圳 26.7 s（load 约 0.3–1.9）；`tests/world/test_autobuild.py::test_deleted_world_is_rebuilt` 断言 ≤ 60 s 通过；M03 报告六城并行 27.3–35.1 s |
| Ajv strict 校验通过 | 通过 | `make validate` 与 `test_ajv_worlds.py`：71 份文档 0 份不合法 |
| 删除任一世界后 `make run` 自动重建 | 通过 | 硬链接副本删除 shenzhen 后 `make run`：`[worldpkg] shenzhen: rebuild (missing)` → `published cf5fcd7b3791 in 26.7 s` → geo warm 命中 → READY；contentVersion 与原件相同（确定性） |
| 且 `/world/<id>` 可访问 | 未通过 | api `module_missing:awr.api.main` → FAILED；`GET http://127.0.0.1:8000/world/shenzhen` 连接被拒。静态服务在 M11 §12 属 MS3 |
| 测试方法 | 通过 | `make worlds && make validate` rc 0；`pytest tests/world/test_autobuild.py` 通过 |

### 4.5 里程碑内容项

| 里程碑 | 内容项 | 状态 |
|---|---|---|
| MS1 | `git init` 与 `.gitignore` | `.gitignore` 已有；按任务书不使用 git，未 init（M00-A D-12），pre-commit 钩子未安装 |
| MS1 | Makefile、`mk/`、`pyproject.toml`、`requirements.lock`、`package-lock.json` | 已交付；`make ci` 通过 |
| MS1 | 路径所有权表生效 | 以约定与请求文件执行（无 git，无法用 CODEOWNERS 或 diff 强制） |
| MS1 | `packages/contracts` 与代码生成 | 已交付（75 个 schema、双端生成、`--check`） |
| MS1 | `awr.runtime`（StateRing、Bus、事件、心跳、child、supervisor） | 已交付（M11-R，tests/runtime 全部通过） |
| MS1 | `fake_gw.py`、`.awrrt` 与契约 golden 夹具 | 已交付 |
| MS1 | `FakeSource.ts` | 未交付（占位） |
| MS1 | lint 全套与 `make ci` | 已交付并补全（MMD-01 除外） |
| MS1 | g07 样板迁入新路径（TS 7） | 已交付（M15-S） |
| MS2 | `worldpkg`（ingest、tile、validate、build `--missing`） | 已交付 |
| MS2 | 六城生成 | 已交付（六城 ready，contentVersion 见 M03-M04 报告 §3.1） |
| MS2 | M04 WorldQuery | 已交付（tests/world_query 103 例非 perf 通过，含本次新增契约用例） |
| MS2 | DSM 与 zones | DSM 2 m、DTM 10 m、`dsm_2m_n` 六城齐全；zones 只有自动 border，策展 zones 待 M16 |
| MS2 | `make fetch-data` | 已交付（`VERIFY=1` 通过） |

## 5. 越权修改清单

| 路径 | 所有者 | 修改 | 原因 |
|---|---|---|---|
| `tests/runtime/test_cli.py` | M11 | 改写 1 个过时用例，新增 2 例 | 第 3 节 #1，pytest 失败 |
| `python/awr/runtime/supervisor.py` | M11 | `sys/procs` 条目删除重复字段 `heartbeat_age_ms` | 第 3 节 #4，按 17 冻结字段名 |
| `tests/runtime/test_lint_runtime.py` | M11 | struct 格式串用例范围扩到 `python/awr/api/**` | M11-R-to-M00 第 5 条 |
| `apps/web/vite.config.ts` | M00 | 代理表改为 `/api`、`^/worlds/.+`，preview 显式同表 | 第 3 节 #2 |
| `apps/web/.oxlintrc.json` | M00 | 增加 DET-01 覆盖（engine、perf、tests） | 第 3 节 #9 |
| `.gitignore` | M00 | 增加 `/apps/web/.vitest/` | 第 3 节 #11 |
| `packages/contracts/bus/geo.schema.json` | M00 | op 枚举与可选 `detail` | 第 3 节 #3 |
| `packages/contracts/rt/payloads/sys_procs.schema.json` | M00 | 六态枚举、17 字段名、可选附加字段 | 第 3 节 #4 |
| `packages/contracts/bus/event.schema.json` | M00 | 可选 `batch_id` | 第 3 节 #5 |
| `packages/contracts/perf/bench-result.schema.json` | M00 | 可选 `records[].ipc` | 第 3 节 #6 |
| `python/awr/contracts/schema_types.py`、`packages/contracts/gen/ts/types.ts` | M00（生成物） | `make contracts` 重新生成 | 上述 4 个 schema 修订 |
| `mk/contracts.mk` | M00 | 新增 `test-fixtures` | 第 3 节 #7 |
| `tools/contracts/check-world.mjs`（新） | M00 | 采纳 M03 的 Ajv 草稿 | 第 3 节 #8 |
| `tools/lint/check_py_imports.py` | M00 | 包边界表、相对导入、awr.api 白名单、墙钟禁用；删除被包边界表覆盖的 swarm、weather 旧规则 | 第 3 节 #9 |
| `tools/lint/check_py_callbacks.py`（新）、`tools/lint/py-imports.toml`（新）、`tools/ci/check-perf-flags.mjs`（新） | M00 | PY-CB-01、awr.api 白名单（空）、PERF-01 | 第 3 节 #9 |
| `tools/lint/check-brand.mjs`、`tools/lint/selftest.mjs` | M00 | 模块说明符跳过；BRAND 与 PERF-01 自测用例 | 第 3 节 #9、#10 |
| `mk/lint.mk` | M00 | 登记 `lint-py-callbacks`、`lint-perf-flags` | 第 3 节 #9 |
| `tests/contracts/test_integration_payloads.py`（新）、`tests/contracts/test_lint_tools.py` | M00 | 4 例载荷契约回归；3 例 lint 规则（含 ARCH-AC-001 变异） | 第 3 节 #4–#6、#9 |
| `mk/m03.mk` | M03 | `validate` 追加 Ajv strict | 第 3 节 #8 |
| `tests/world/test_ajv_worlds.py`；删除 `tests/world/ajv_check_world.mjs` | M03 | 改用 `tools/contracts/check-world.mjs` | 第 3 节 #8（M03 请求明确要求） |
| `tests/world_query/test_bus_contract.py`（新） | M04 | 探针请求与回复对契约校验 | 第 3 节 #3 |
| `tests/e2e/ms-smoke.spec.ts`（新） | M16 | 集成冒烟（配置的 webServer、生产构建、fake_gw） | 任务第 1 项；03 §4.3 规定跨模块集成用例放 tests/e2e/ |

另有运行产物：`.cache/impl/shots/ms-*.png`；`runs/lint/*.json`、`runs/playwright/`（gitignore 范围）。`make run` 验证使用 scratchpad 中的硬链接副本与独立 `AWR_RUNS_DIR`，结束后已 `awr stop` 并删除副本，未改动 `worlds/`。

## 6. 跨模块请求处理

| 请求文件 | 处置 |
|---|---|
| M00-B-to-M00 | 第 1 条 BOM：遗留；第 2、4 条：信息；第 3 条：已落实（fake_gw 使用 `write_awrrt/read_awrrt`）；第 5 条：待基线裁决（遗留） |
| M00-B-to-M02、M07、M08、M09、M10-M16、M12 | 下游模块审阅起草契约与补齐实现，均不属 MS1/MS2 出口：遗留给对应工作包 |
| M00-B-to-M03 | 已由 M03 答复并落实（M03-to-M00 第 6 条） |
| M00-B-to-M11 | 第 1–5 条已被 M11-R 使用；第 6 条 OpenAPI 快照依赖 `awr.api.main`：遗留 |
| M00-B-to-M14-M01-M04-M13 | M04 部分已处理（geo schema 修订）；其余遗留 |
| M00-B-to-M15、M00-to-M15 | 已由 M15-S 落实（theme.css、registry.ts、brand.lock.json 格式，shadcn 未改依赖清单），lint 全部通过 |
| M00-to-M03、M00-to-M11 | 已落实（`WORLDS_TARGETS`、supervisor 与 `awr` CLI 可用，`make run` 可启动） |
| M00-to-M16、M00-to-all | M16 未开始，遗留；约定性信息 |
| M03-to-M00 | 第 1、2 条已处理；第 3、4 条接受（信息）；第 5 条 OpenAPI：遗留；第 6 条关闭 |
| M03-to-M07、M03-to-M16、M04-to-M08 | 下游接线，遗留 |
| M04-to-M11 | 挂载 world_query 路由与静态服务依赖 `awr.api.main`：遗留（MS3） |
| M11-R-to-M00 | 第 1–5 条已处理（第 3 条为 schema 侧，基准脚本写入 `ipc` 由 M11 决定）；第 6 条信息；第 7 条遗留 |
| M11-R-to-M03 | 插件注册已由 M03 落实；DOC-14、DOC-15 精确检查为可选改进：遗留 |
| M11-R-to-M08、M11-R-to-M11、M11-R-to-M12、M11-R-to-M16 | 下游交接说明，遗留 |
| M15-to-M00 | 第 1 条已处理；第 2 条未能复现（冷启动 3 次均通过），未改；第 3 条 check-brand 已处理，registry-mirror 排除不采纳（03 §8.4 扫描范围），I18N-01 维持 Vitest 覆盖；第 4 条已处理 |
| M15-to-M03-M05-M07-M09-M10-M12-M13-M14、M15-to-M06、M15-to-M11 | 领域 store、engine、net 骨架交接，遗留给所有者 |
| M15-to-M16 | `playwright.config.ts` 交接遗留；webServer 现已可用 |

本次新写的请求：`.cache/impl/requests/MS12-to-M11.md`、`MS12-to-M00.md`、`MS12-to-M15-M16.md`。

## 7. 与 PRD、基线的偏差及理由

| 编号 | 偏差 | 理由 |
|---|---|---|
| V-01 | vite 代理表不再含 `/assets`，与 19 §5 端口表第 544 行不一致 | 03 §3.3（最高权威）与 16 F-09、R2-42 明确 `/assets/` 不作代理；已请 M00 修订 19 |
| V-02 | `sys_procs` 删除 EXITED，supervisor 附加字段按可选登记 | 17 §4.3.12 与 19 §4.2 的线上枚举只有六态；附加字段按 03 §5.11 第 1 条为可选新增 |
| V-03 | `batch_id`、`records[].ipc`、geo 回复 `detail` 为可选新增字段 | 03 §5.11 第 1 条允许可选新增；需在 17、18 登记 |
| V-04 | 包边界只把 10 §3.3 表中的"禁止依赖"列与 awr.contracts、awr.runtime、awr.api、awr.recorder 的封闭允许集做成规则；其他包的"允许依赖"列不作白名单 | 表中这四类写明"其他均禁止"或"只能"，其余包的允许列是示例性列举，作白名单会误报跨子包依赖（如 `awr.world.package` 依赖 `awr.world.georef`） |
| V-05 | 墙钟禁用以 `imports/boundary` 编号报告 | 10 §3.3（ADR-049 不变量 5）写明由 py-imports 的 AST 规则拦截，18 §13.1 未单列编号 |
| V-06 | PERF-01 对 `playwright.config.ts` 与 `apps/web/perf/**` 中出现的任何 C3 标志报错 | `apps/web/perf/**` 全部属于 perf 项目；允许 C3 的吞吐微基准目前没有载体 |
| V-07 | `make test-fixtures` 不追加到 `TEST_TARGETS` | `make test` 已全量运行这些 pytest 与 Vitest 用例（M00-to-all 约定） |
| V-08 | struct 格式串规则（M11 §9.5 第 2 条）仍在 M11 的 pytest 中，未进入 tools/lint | 18 §13.1 无对应编号；M03 的 World Package 读写（hierarchy、AWSL）合法使用 struct，规则只适用于 M11 代码 |

## 8. 遗留问题

1. **阻塞 MS1 出口**：M11 网关与前端 net 工作包（Gateway 最小协议栈与 SyntheticSource、`FakeSource.ts`、rt.worker、RtClient 初版）未交付，D1-AC-35 未通过（MS12-to-M11 第 1 条）。
2. **阻塞 MS2 出口**：`/world/<id>` 可访问依赖 `awr.api.main` 与静态服务（M11 §12 排在 MS3），D1-AC-01 访问侧未通过；03 §8.6 的出口划分与此不一致，已请 M00 在里程碑表注明。D1-AC-35 的 flight60 `scene=full` 同理依赖 MS5。
3. supervisor 在 api、sim-core FAILED 时仍打印 READY 与访问地址（MS12-to-M11 第 3 条）。
4. BOM（`tools/ci/bom.json`）未建立，check-deps 降级运行；MMD-01 未接入；Python 依赖图无环检查未实现（MS12-to-M00）。
5. OpenAPI 快照（17 §10.6 第 9 项）依赖 `awr.api.main`。
6. M08 `state_model`、M07 环境实现未交付，契约测试中两处对拍跳过。
7. 策展 zones（M16）缺失，六城只有 border。
8. 未执行 `git init`，pre-commit 钩子未安装，路径所有权无法由工具强制（任务书禁止使用 git）。
9. 文档同步：19 §5 `/assets` 代理、M00-B 契约结构偏差裁决、M11 §9.5 第 2 条的规则编号。
10. vite 构建 ui 块 727 kB、three 块 737 kB 超过 500 kB 提示（AWR-18 §6.3 分组），需在 MS5 性能预算中评估。
11. perf 标记用例（12 个）与 `make bench-ipc`、`make perf-world` 按任务未在本次运行；性能口径以各工作包报告为准。

## 9. 截图与产物

| 文件 | 内容 |
|---|---|
| `.cache/impl/shots/ms-home-1280x720.png` | 生产构建首页（`/` 重定向到 `/world/shenzhen`）：头栏、相机工具条、机群空态、PerfHud、时间轴 |
| `.cache/impl/shots/ms-world-hub.png` | `/worlds` 前端路由（World Hub），`/api/worlds` 未提供时的错误空态与重试 |
| `.cache/impl/shots/m15-*.png`（10 张） | `make smoke-m15` 重跑生成（沙盘两种视口、Dock 与侧栏、设置、命令面板、快捷键、World Hub、小窗口、设计样板） |
