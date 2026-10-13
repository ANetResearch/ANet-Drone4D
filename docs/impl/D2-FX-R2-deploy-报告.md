# D2-FX-R2-deploy 报告（区域 deploy；第 2 轮：D2 验收第 1 轮复测之后）

| 项 | 内容 |
|---|---|
| 工作包 | D2-FX-R2-deploy：D2 验收第 1 轮（2026-10-11 至 12 复测版）中区域 deploy 的未通过项：D2-AC-42 容量接口（P0，未测：nginx 微缓存子项）与 D2-AC-27 公开站部署（P1，未测：emax） |
| 区域与改动路径 | M00 部署套件：`tools/deploy/emax/{drone4d.agentnetwork.org.cn.conf,nginx-check.sh,README.md}`、`mk/deploy.mk`、`tests/ops/{test_nginx_site_e2e.py,test_deploy_nginx.py,test_nginx_static_parity.py}`；按工作包授权跨模块修改了 M16 的 harness 用例登记 `apps/web/perf/d21/cases.mjs`（只增一条 owner M00 的用例） |
| 日期 | 2026-10-12 |
| 依据 | `docs/impl/D2-验收报告-第1轮.md`（D2-AC-27、42 行与第 4、5 节）；AWR-04 v1.3 §13（D2-AC-27）、§15.14（D2-AC-42）、§15.5.4 与 §15.6（容量接口与 nginx 微缓存）、§15.6.5（池不可达时启动屏显示"容量信息暂不可用"）、ADR-119、R-D2-31、§14.2 与 §15.15（WP-16 与 WP-16 扩展的路径）；17 §17.20.2、API-FR-087、API-NFR-026、API-AC-063；M17-FR-062、M17-AC-020；19 §16.2（退出码）；`docs/impl/D2-WP-16-报告.md`（一次性容器内 nginx 对拍的既有做法） |
| 环境 | VMware 8 vCPU、无 GPU；Python 3.12.3（`.venv`）；Node 22.12；Docker 29.4（本地 `ubuntu:24.04` 镜像）；容器内 Ubuntu 24.04 的发行版 nginx 1.24.0（1.24.0-2ubuntu7.18，与 emax 相同）；并行工作包使本机负载在 7.8 至 23 之间；没有 emax 访问 |
| 约束执行 | 本机与仓库没有安装任何依赖：nginx 只在用完即删的 `ubuntu:24.04` 容器内以 apt 安装（与 WP-16 的 `make deploy-nginx-check` 相同），仓库只读挂载；没有执行 git 写操作；没有跑性能基准（容量用例是功能性的到达计数，8 s）；没有 emoji 与禁用字形；没有界面与渲染代码改动；"暂定"阈值与 N_max 均未冻结（没有 emax 数据），P0 阈值未放宽；跨所有权请求追加到 `.cache/impl/requests/d2-FX-R2-deploy-to-{文档总编,M16,M17}.md` 的"第 2 轮"段，另新建 `d2-FX-R2-deploy-to-M11.md` |

## 1 结论摘要

- **D2-AC-42 的 nginx 子项（P0）："nginx 微缓存下 100 个并发客户端每秒到达 sandbox-api ≤ 1 次"**——已定位根因并修复，本包自测通过：
  1. **未测的根因**：自 WP-16 起就有不在本机安装 nginx 的执行途径 `make deploy-nginx-check`（一次性 Ubuntu 24.04 容器、nginx 1.24），
     但它没有登记为验收入口（M16 harness 无对应用例），用例的跳过原因写着"部署后在 emax 上执行"，判据的测试方法列只写"nginx 配置测试"，
     于是第 1 轮验收按"本机无 nginx，不装新依赖"记为未测。现已登记 harness 用例 `d21.nginx-site`，跳过原因改为指向该目标，脚本输出机器可读证据。
  2. **既有用例弱于判据，掩盖了站点文件的真实缺陷**：原用例只用一个地址发 100 个请求（允许 429）、判"2 s 内 ≤ 3 次"，且只测上游正常的情形。
     按判据重写后，在容器内 nginx 1.24 上测出改前的容量 location 有四处缺陷：上游 503（池不可达）或 500 时到达 **69 至 85 次/s** 且访客
     一直拿到过期的 200；GET 与 HEAD 混合时 **2 次/s**；冷缓存且上游 2.5 s 时 **101 次**（每秒最多 82 次）；访客的请求头（X-Request-Id、
     Origin 等）随回源进入共享副本（真实 sandbox-api 对外来 Origin 回 403 并回显访客的 X-Request-Id）。另有 GET、HEAD 以外的方法不经缓存直达。
  3. **修复后**：100 个并发客户端（各自的地址前缀，GET 与 HEAD 混合，一半带查询参数，全部自带访客标识）持续 8 s、3100 次请求全部 200，
     到达 sandbox-api **5 次、每个自然秒最多 1 次、相邻最小间隔 1.009 s**；上游 503、500、冷缓存慢上游、卡住与不可达五种故障下同样
     **每秒最多 1 次**；共享副本不带任何访客值；真实 sandbox-api 置于真实站点文件之后：200、通过 `awr.capacity.v1` schema、663 B、无 X-Request-Id。
     D2-AC-42 的其余子项在第 1 轮复测中已通过（p95 45.2 ms ≤ 50 等），本包未改动 sandbox-api。**结论待验收第 2 轮以 `d21.nginx-site` 复核。**
- **D2-AC-27（P1）**：根因是环境（没有 emax 访问），本包无法消除；观测工具 `tools/deploy/emax/observe.py` 与部署手册 5.7 节仍就绪
  （18 例用例本轮复跑通过，与当前池状态字段核对一致）。**仍为"未测"，列为 blocker**；N_max 没有冻结，没有写 ADR。

## 2 根因定位

### 2.1 D2-AC-42 的 nginx 子项

| 现象 | 根因 |
|---|---|
| 第 1 轮验收记为"未测（本机无 nginx，不装新依赖）" | 执行途径早已存在（`make deploy-nginx-check`：一次性容器内 apt 安装 nginx，宿主与仓库不变，WP-16 报告 §4 已用它跑通 41 例），但①验收入口（M16 harness `cases.mjs`）没有登记它；②`test_nginx_site_e2e.py` 与 `test_nginx_static_parity.py` 的跳过原因写"部署后在 emax 上执行"；③D2-AC-42、API-AC-063 的测试方法列只写"nginx 配置测试"，没有指明目标 |
| 原用例通过，但与判据不符 | 一个客户端、同一地址（同一限流前缀）在 2 s 内发 100 个请求，允许 429，断言"到达 ≤ 3 次"（即 1.5 次/s）；没有"100 个并发客户端""每秒""上游故障"三个要素 |
| 上游 503 或 500 时到达 69 至 85 次/s，访客一直拿到过期的 200 | `proxy_cache_use_stale … http_500 http_502 http_503 http_504` 与 `proxy_cache_background_update on` 同用：后台刷新拿到 5xx 时 nginx 以过期副本代替它，错误回复不入缓存，条目始终过期，下一个访客请求又触发一次刷新（`updating` 只串行化，不限速）。池不可达（503 211）正是 sandbox-api 最需要保护的时刻；访客看到的也是过期容量而不是 AWR-04 §15.6.5 规定的"容量信息暂不可用" |
| GET 与 HEAD 混合时 2 次/s | 缓存键 `"$request_method$uri"` 而 `proxy_cache_convert_head` 缺省 on：HEAD 被转为 GET 回源，却存成另一份副本，两份各自按秒刷新 |
| 冷缓存且上游 ≥ 2 s 时 101 次 | `proxy_cache_lock_timeout 2s` 小于上游可能的时延（读超时继承 server 层的 60 s）：等锁超时的请求各自回源且结果不入缓存（nginx 文档语义） |
| 访客标识进入共享副本 | 回源转发访客的全部请求头：sandbox-api 回显访客自带的 X-Request-Id（≤ 128 字符，problem 正文的 `request_id` 也取它），并对外来 Origin 回 403、对超限前缀回 429；这些回复进入或冲掉共享副本（改前不缓存它们，因而又变成每个请求回源一次） |
| 其他方法直达上游 | `proxy_cache_methods GET HEAD` 之外的方法照常转发，每次都到达 sandbox-api |

改前测量（容器内 nginx 1.24，100 个客户端各自的源地址，每 0.2 s 一次，持续 5 s；`.cache` 外的临时探针，见 4.2）：

| 情形 | 到达次数 | 每秒最多 | 访客状态 |
|---|---|---|---|
| 上游正常 | 3 | 1 | 200 |
| 上游 503 | 321 | 69 | 全部 200（过期副本） |
| 上游 500 | 351 | 85 | 全部 200（过期副本） |
| GET 与 HEAD 各半 | 6 | 2 | 200 |
| 冷缓存，上游 2.5 s | 101 | 82 | 200 |

### 2.2 D2-AC-27

| 子项 | 根因 |
|---|---|
| emax 部署 72 h（OOM 0、非预期重启 0、共享展示 RTF ≥ 0.99） | 环境：没有 emax 访问。工具（第 1 轮交付的 `observe.py run/mark/report`）就绪 |
| emax 实测 §4.4.1 成本与世界成本表、ADR 冻结 N_max | 同上；成本须在生产流量下采集，本机 cgroup 模拟不能代替（任务约束：暂定阈值只在数据支撑时以 ADR 冻结） |
| S7 连续 7 天线上统计 | 同上；需要连续 168 h 的线上运行 |

## 3 修复与实现清单

| 编号 | 实现 | 路径 |
|---|---|---|
| D2-AC-42、API-AC-063、API-NFR-026（≤ 1 次/s）、ADR-119 | 容量 location：①`proxy_cache_valid any 1s`（任何上游回复，含 503 211、500，均缓存 1 s；nginx 自己的 502、504 同样按 1 s 记）；②`proxy_cache_use_stale updating`，`proxy_cache_background_update off`（过期后由一个访客请求同步刷新，其余访客拿到上一份；不再以过期副本掩盖错误）；③缓存键 `"$uri"`（HEAD 与 GET 共用一份）；④`if ($request_method !~ ^(GET\|HEAD)$ ) { return 405; }`；⑤回源 `proxy_connect_timeout 1s`、`proxy_send_timeout 3s`、`proxy_read_timeout 3s`，`proxy_cache_lock_timeout 5s`、`proxy_cache_lock_age 5s`（锁等待长于回源的最长时间）；⑥`proxy_ignore_headers` 增加 `X-Accel-Expires`（有效期与上游头无关）；注释写明每条的原因与改前实测 | `tools/deploy/emax/drone4d.agentnetwork.org.cn.conf` |
| D2-AC-42"响应不含任何访客标识"（经 nginx） | 共享副本只由 nginx 以自己的身份回源：`proxy_pass http://drone4d_sandbox$uri`（只回源规范路径，不带查询参数与原始编码）、`proxy_pass_request_headers off`、`proxy_pass_request_body off`，只设 `Host`、`X-Forwarded-Proto` 与空 `Connection`（上游长连接）；`proxy_hide_header X-Request-Id`、`proxy_hide_header Set-Cookie` | 同上 |
| 结构断言（M17-FR-062） | `test_capacity_microcache` 按新口径重写；新增 `test_capacity_lock_outlasts_upstream_timeouts`（连接 + 读超时 < 锁等待且 ≤ 锁龄）、`test_capacity_copy_is_visitor_independent`、`test_location_proxy_set_header_is_deliberate`（location 内出现 `proxy_set_header` 时要么整组重复 server 层，要么有意不转发访客请求头）；`upstream()` 适配带变量的 proxy_pass | `tests/ops/test_deploy_nginx.py`（61 例） |
| D2-AC-42 行为用例（真实 nginx） | 重写容量部分：`test_capacity_microcache_100_clients`（判据原文：100 个并发客户端，源地址 127.0.<k+1>.1 即各自的前缀，每 4 个中 1 个用 HEAD，一半带查询参数，全部自带 X-Request-Id、Cookie、Authorization、Origin 与 X-AWR-Identity，同时起跑后持续 8 s：全部 200；每个自然秒 ≤ 1 次且相邻间隔 > 1 s；3 ≤ 到达 ≤ 时长 + 1；稳态副本年龄 < 3 s；回源只带规范路径、Host 与 X-Forwarded-Proto；回复无 X-Request-Id 与 Set-Cookie；HEAD 无正文）；`test_capacity_copy_is_visitor_independent`；`test_capacity_other_methods_do_not_reach_upstream`（POST、PUT、DELETE 405）；故障用例各起一套新 nginx：`test_capacity_upstream_errors_are_cached[503,500]`（≤ 1 次/s，起跑 1 s 后全部为该错误）、`test_capacity_cold_cache_slow_upstream`（2.5 s，≤ 3 次，全部 200）、`test_capacity_hung_upstream`（6 s > 读超时，≤ 3 次，200 或 504）、`test_capacity_upstream_down`（0 次，多数 502）；`AWR_NGINX_EVIDENCE_DIR` 有值时每例写 `capacity-<用例>.json`（`awr.deploy.nginx-capacity.v1`）；桩记录到达时刻与收到的请求头，回复带 X-Request-Id 与 Set-Cookie（最坏的上游） | `tests/ops/test_nginx_site_e2e.py`（21 例） |
| 可发现性 | 两个 nginx 用例文件的跳过原因改为"用 make deploy-nginx-check（一次性 Ubuntu 24.04 容器，nginx 1.24）执行；emax 上直接执行" | `tests/ops/test_nginx_site_e2e.py`、`tests/ops/test_nginx_static_parity.py` |
| 证据与退出码 | `nginx-check.sh [--out DIR] [-- <pytest 参数>]`：证据目录可写挂载（其余只读），写 `nginx-version.txt`、`nginx-t.log`、`apt.log`、`pytest.log`、`junit.xml`、`capacity-*.json` 与汇总 `nginx-check.json`（`awr.deploy.nginx-check.v1`），退出前 chown 回宿主用户；镜像已有 nginx 时不再 apt；退出码按 19 §16.2：0 通过、1 用例失败、2 `nginx -t` 失败或参数错误、8 缺少 docker、镜像、`.venv` 或软件源（原为 2 或 apt 的 100）；`make deploy-nginx-check NGINX_CHECK_OUT=…` | `tools/deploy/emax/nginx-check.sh`、`mk/deploy.mk` |
| 验收入口 | M16 harness 用例 `d21.nginx-site`（owner M00，kind `shell`，P0，G2d）：`bash tools/deploy/emax/nginx-check.sh --out {runDir}/nginx`，acIds D2-AC-42、M17-AC-020、M11-AC-211；`run.mjs --check` 通过（143 例） | `apps/web/perf/d21/cases.mjs` |
| 手册 | 2.3 节容量行写明现行口径；2.4 节增加两条 curl 核对（容量回复无 X-Request-Id；POST 405）与 nginx 检查的范围、证据与退出码；文件表更新 | `tools/deploy/emax/README.md` |

## 4 测试结果

### 4.1 nginx 检查（`make deploy-nginx-check`，一次性容器，nginx 1.24.0）

最后一次运行（本机负载 23）：引导站点与正式站点 `nginx -t` 均通过；`test_nginx_static_parity.py` 29 例与 `test_nginx_site_e2e.py` 21 例，
**50 passed**（58 s，含 apt 共 86 s）。证据：`.cache/deploy/emax/nginx-check/d2-fx-r2-deploy/`（`nginx-check.json` 等）。

| 用例 | 请求数（次/s） | 状态 | 到达 sandbox-api | 每秒最多 | 相邻最小间隔 | 稳态副本年龄最大 |
|---|---|---|---|---|---|---|
| 100 个并发客户端，8 s | 3100（369） | 200 × 3100 | 5 | **1** | 1.009 s | 2.008 s |
| 上游 503（池不可达），5 s | 1937（357） | 503 × 1935、200 × 2 | 3 | **1** | 1.857 s | 1.998 s |
| 上游 500，5 s | 1931（356） | 500 × 1922、200 × 9 | 3 | **1** | 1.113 s | 2.000 s |
| 冷缓存，上游 2.5 s，6 s | 1358（209） | 200 × 1358 | 2 | **1** | 3.584 s | — |
| 上游卡住 6 s（> 读超时 3 s），7 s | 2699（364） | 200 × 2292、504 × 407 | 2 | **1** | 4.103 s | — |
| 上游不可达，3 s | 1162（342） | 502 × 1162 | 0 | 0 | — | — |

说明：503、500 时的少量 200 是第一次刷新进行中（毫秒级）其余访客按 `updating` 拿到的上一份；"每秒最多"按自然秒计，且相邻两次到达
间隔都 > 1 s（任意 1 s 窗口内 ≤ 1 次）。这由 nginx 的整秒有效期保证：第 S 秒存入的 `valid 1s` 副本到第 S + 2 秒才过期，而回源的到达
时刻早于存入时刻。此前另有 2 次完整运行（负载约 8 与 20）均 50 passed，100 客户端的到达均为 5 次、每秒最多 1 次。

### 4.2 改前与改后对照（同一探针、同一容器）

| 情形 | 改前到达 / 每秒最多 | 改后到达 / 每秒最多 |
|---|---|---|
| 上游正常 | 3 / 1 | 3 / 1 |
| 上游 503 | 321 / 69 | 3 / 1 |
| 上游 500 | 351 / 85 | 3 / 1 |
| GET 与 HEAD 各半 | 6 / 2 | 3 / 1 |
| 冷缓存，上游 2.5 s | 101 / 82 | 2 / 1 |

（100 个客户端各自的源地址，每 0.2 s 一次，持续 5 s；探针为临时脚本，不入库。）

### 4.3 真实 sandbox-api 置于真实站点文件之后

`tests/e2e/sbstack.py` 起真实栈（sandbox profile、synthcity；`AWR_ALLOWED_HOSTS`、`AWR_ORIGINS` 取公开站的值），容器内 nginx 1.24
（host 网络）加载改写后的站点文件。访客头 `Origin: https://evil.example`、`Authorization: Bearer junk`、`X-Request-Id: visitor-secret`：
直连 sandbox-api 为 403 且回显 `visitor-secret`（即改前经 nginx 会进入共享副本的回复）；经 nginx 为 200、`awr.capacity.v1` schema 0 错误、
663 B、`Cache-Control: public, max-age=1`、无 X-Request-Id 与 Set-Cookie、正文不含访客值；HEAD 200；POST 405；50 个并发客户端 3 s
共 972 次全部 200。证据 `.cache/deploy/emax/nginx-check/d2-fx-r2-deploy/real-sandbox-api.json`。栈与容器已停止。

### 4.4 回归

| 范围 | 命令 | 结果 |
|---|---|---|
| 部署套件全部（本机） | `pytest -m "not perf and not slow" tests/ops` | 220 passed、50 skipped（需要 nginx 的两组，已在 4.1 的容器内全部通过） |
| nginx 结构断言 | `pytest tests/ops/test_deploy_nginx.py` | 61 passed |
| D2-AC-27 观测工具 | `pytest tests/ops/test_deploy_observe.py` | 18 passed（99 s）；观测工具读取的池状态字段（`n_max`、`degrade_level`、`queue_len`、`accepting`、会话行）与 `awr.sandbox.broker` 现行输出一致 |
| harness 登记 | `node perf/harness/run.mjs --check`、`--list` | registry OK：143 例；`d21.nginx-site` 列出 |
| 脚本 | `shellcheck`（`koalaman/shellcheck:stable` 镜像）`nginx-check.sh`；参数错误退出 2、镜像缺失退出 8；`-- -k …` 透传（4 passed、46 deselected） | 通过 |
| `make lint` | ruff（`python tools tests`）、tools/lint 全部规则（no-emoji、no-hex、lint-lf、check-release、check-version、py-imports、py-callbacks、perf-flags、thresholds 等）、M06、M15 生成物 | 本包文件全部通过；唯一失败为 oxlint 对 `apps/web/tests/m18/zz-fxr2-overlay-cost.test.ts` 第 49、66 行的 unbound-method——该文件由并行的 perception 修复工作包在本包运行期间（07:05 后）新写入，与本包无关 |

### 4.5 未执行

- 没有经 harness 实际运行 `d21.nginx-site`：harness 在负载 ≤ 4 时才起跑，本机负载 7.8 至 23（并行工作包）；同一命令行（`bash
  tools/deploy/emax/nginx-check.sh --out <dir>`，cwd 为仓库根）已由 `make deploy-nginx-check` 验证。
- 没有跑性能基准；D2-AC-42 的"变体 A 负载下 p95 ≤ 50 ms"沿用第 1 轮复测的 45.2 ms（本包未改 sandbox-api；经 nginx 的命中路径只会更快）。
- D2-AC-27 没有执行（没有 emax）。

## 5 偏差

| # | 偏差 | 原因与影响 | 处置 |
|---|---|---|---|
| 1 | 容量微缓存的配置不再是 AWR-04 §15.6 与 17 §17.20.2 字面的"`proxy_cache_valid 200 1s`，`proxy_cache_lock on`" | 字面配置（及第 1 轮的 `200 503 1s` 加 5xx stale）在上游错误、HEAD 与冷缓存慢上游时不满足 P0"≤ 1 次/s"（2.1 节实测）；判据阈值未放宽 | 请文档总编按现行口径改写 AWR-04、ADR-119 后果段、17、M17 PRD §14.5 第 2 条（`d2-FX-R2-deploy-to-文档总编.md` 第 7 条） |
| 2 | 经 nginx 的容量请求不再携带访客地址（X-Forwarded-For、X-Real-IP） | 副本与访客无关的前提；sandbox-api 把这些请求计在回环地址的 `sbx_public` 桶（nginx 每秒至多 1 次，远低于 20/s），每访客限流在 nginx 的 `drone4d_api` | 知会 M17；请文档总编视需要在 17 §3.4 或 M17 §6.6.7 登记（第 10 条） |
| 3 | sandbox-api 不可达、卡住或回 5xx 时访客拿到 502、504 或原样的 503、500，而不是过期的 200 | 与 AWR-04 §15.6.5（池不可达时显示"容量信息暂不可用"）一致；前端 `bootCapacityFetch` 对非 2xx 即显示不可用 | 无需处置 |
| 4 | 改动了 M16 拥有的 `apps/web/perf/d21/cases.mjs`（新增一条 owner M00 的用例） | 第 1 轮"未测"的直接原因是验收入口未登记该检查；工作包授权跨模块修改该区域相关代码 | 知会 M16（`d2-FX-R2-deploy-to-M16.md` 第 5、6 条） |
| 5 | nginx 在一次性 `ubuntu:24.04` 容器内以 apt 安装后执行 | 与 WP-16 的既有做法相同，宿主与仓库的依赖集不变；版本与 emax 相同（1.24） | 在脚本、手册与本报告中写明；软件源不可达时退出 8 并提示 |
| 6 | 本报告覆盖同名的第 1 轮报告 | 同一工作包编号的第 2 轮 | 第 1 轮原文完整保留为附录 A |

## 6 遗留

1. **D2-AC-27（blocker）**：需要有 emax 访问的运维按手册 5.7 运行 `observe.py run --hours 168`（前 72 h 给出服务判据，满 168 h 给出 S7 七天统计），
   并把 `report.json`、`samples.jsonl`、`events.jsonl` 拷回开发机作为验收证据与 N_max 冻结 ADR 的输入。本机无法替代。
2. **验收第 2 轮**：请以 harness 用例 `d21.nginx-site`（或 `make deploy-nginx-check`）复核 D2-AC-42 的 nginx 子项，证据为 `nginx-check.json`。
3. **"2 s 内与真值一致"的测量点**：现由直连 sandbox-api 的 e2e 测量（通过）；公开路径另有 nginx 副本年龄（稳态 < 2 s，本轮实测最大 2.0 至 2.13 s），
   请文档总编澄清判据指 sandbox-api 的响应（第 9 条，不改阈值）。
4. **`make lint`**：`apps/web/tests/m18/zz-fxr2-overlay-cost.test.ts` 的两处 unbound-method 由其所属的并行工作包处理。
5. **文档**：第 7 至 10 条请求待文档总编改写；第 1 轮的第 1 至 6 条中未处置者仍有效。

## 7 跨所有权请求

| 文件 | 接收方 | 要点（第 2 轮） |
|---|---|---|
| `d2-FX-R2-deploy-to-文档总编.md`（第 7 至 11 条） | 文档总编 | 容量微缓存的现行口径与改前实测缺陷；D2-AC-42、API-AC-063 测试方法列写明 `make deploy-nginx-check` 与 `d21.nginx-site`；"2 s 内一致"的测量点；sandbox-api 侧限流类别的登记；D2-AC-27 与 N_max 仍待 emax |
| `d2-FX-R2-deploy-to-M16.md`（第 5、6 条） | M16 | 已登记 `d21.nginx-site`，请在验收第 2 轮执行；第 1 轮"未测"的原因 |
| `d2-FX-R2-deploy-to-M17.md`（第 3、4 条） | M17 | 经 nginx 的容量请求不带访客请求头（SB-71 不得依赖 Host 以外的请求头）；错误回复同样缓存 1 s |
| `d2-FX-R2-deploy-to-M11.md`（新） | M11，抄送 M17 | 建议（P2）：可缓存的公开路由不回显访客自带的 X-Request-Id（nginx 侧已堵住，api 侧无需改动） |

## 附录 A 第 1 轮报告原文（2026-10-07，D2-AC-24 与 D2-AC-27）

以下为本工作包第 1 轮（D2 验收第 1 轮初测之后）的报告，原样保留，标题降级；其中的结论已由 2026-10-12 的复测确认（D2-AC-24 通过）。

| 项 | 内容 |
|---|---|
| 工作包 | D2-FX-R2-deploy：D2-AC-24（P0，不通过）与 D2-AC-27（P1，未测）的根因定位与修复 |
| 区域 | deploy：M00 的部署套件与发布面（`tools/deploy/emax/**`、`NOTICE`、`README.md`、`tests/ops/**`、契约 `packages/contracts/schemas/world/`）；按工作包授权跨模块修改了该区域相关的 M03（`python/awr/world/{ingest/urbanscene3d.py,package/{dataset,cli}.py}`、`mk/m03.mk`、`tests/world/`）、M15（`apps/web/src/{lib/demo.ts,ui/panels/world/WorldPanel.tsx,ui/views/{WorldHub,AboutDialog}.tsx,app/query/options.ts,app/i18n/*.json}`）与 M16（`tests/e2e/honesty.spec.ts`、`python/awr/datasets/demo/script.py`）文件 |
| 日期 | 2026-10-07 |
| 依据 | `docs/impl/D2-验收报告-第1轮.md`（D2-AC-24、27 行，§4.7、§5、§8）；AWR-04 v1.3 ADR-109（第 3 条）、R-D2-28、§4.4.1、§4.4.7、§13（D2-AC-24、27）、§14.2 第 16 行；`d2-ACC-1-to-M00.md`、`d2-WP-16-to-M03.md`；`docs/impl/D2-WP-16-报告.md`、`D2-INT-集成报告.md` 9.7；用户原文 `docs/inputs/D2-需求原文-2026-10-05.md`（R-D2-28 补充决定） |
| 环境 | VMware 8 vCPU、无 GPU；Chrome for Testing 151（SwiftShader）；Node 22.12；`.venv` Python 3.12.3；本机七个世界已构建；没有 emax 访问 |
| 约束执行 | 没有安装依赖；没有执行 git 写操作；没有跑性能基准（本机栈只做 2.4 min 的功能性观测自检，见 4.4）；没有 emoji 与禁用字形；颜色只用 token（About 的 BibTeX 块用 `bg-muted`）；新 UI 只用既有 shadcn 组件与 morphicons 图标；无新渲染代码；部署口令明文没有出现在任何文件、测试与本报告中（测试口令为占位串）；跨所有权的知会与请求写入 `.cache/impl/requests/d2-FX-R2-deploy-to-*.md`（5 份）；"暂定"阈值与 N_max 均未冻结（没有 emax 数据） |

### A.1 结论摘要

- **D2-AC-24（P0）**：第 1 轮的两处缺口都已修复，且补上了防回归的检查。
  1. `NOTICE` 补上 ECCV 2022 BibTeX（与 THIRD_PARTY_NOTICES §4、两版 README、前端常量逐字相同）；英文 README 的授权段标题加注中文原词
     "经数据集权利方授权使用"。四处发布面文档与关于页的授权语句与 BibTeX 由新用例逐字核对。
  2. 六城 `world.json` 的数据集说明改为 ADR-109 第 3 条的授权说明（`license` 改写、新增 `dataset.terms`、`notice` 不再引用 ADR-034），
     本机六城已只重写元数据（contentVersion 与派生缓存键不变）；世界面板不再显示"科研用途"，改为"经数据集权利方授权使用"；
     关于页增加可复制的 BibTeX；部署打包拒绝 ADR-109 之前的数据集说明。
  其余 D2-AC-24 子项（七城列表、切换、507、install.sh 校验）未改动；以真实七个世界重新打包、校验、解包与 deep 校验通过（4.3）。
- **D2-AC-27（P1）**：根因是环境（本机没有 emax）加上缺少可执行的线上观测工具（第 1 轮没有任何工具能在 emax 上测 72 h 判据与 S7 七天统计）。
  本包交付 `tools/deploy/emax/observe.py` 与部署手册 5.7 节：72 h 服务判据（OOM、非预期重启、共享展示 RTF）、S7 七天统计（被发现、间隙按原因）
  与 emax 实测成本（§4.4.1 与 N_max ADR 的输入）一次运行全部采集。工具在模拟环境（18 例）与本机真实栈上验证；**D2-AC-27 仍为"未测"**，
  需要有 emax 访问的运维执行；N_max 没有冻结（没有 emax 数据，不写 ADR）。

### A.2 根因定位

#### A.2.1 D2-AC-24

| 现象 | 根因 |
|---|---|
| `NOTICE` 只有授权语句与"见 THIRD_PARTY_NOTICES 第 4 节"的引用指向，没有 BibTeX | WP-16 改写发布面时把 NOTICE 当作纯文本摘要，只放了指向；判据要求四处都含 BibTeX；仓库里没有任何用例核对发布面文字，回归无人发现 |
| 六城 `world.json` 的 `dataset.license` 仍为 "non-commercial research only"，`notice` 仍引用 ADR-034 | ADR-109 第 3 条要求"world.json 的 dataset.terms 写同一说明"，生成器文字归 M03（`urbanscene3d.DATASET`），WP-16 只能写请求（`d2-WP-16-to-M03.md` 第 1 条），一直未处置；另外没有"只重写元数据"的途径：`missing_reason` 不比较数据集文字，`make worlds` 永远不会更新已构建世界的说明，而重建六城需要原始数据与数十分钟。实际上 world.json 是非内容文件（`manifest.NON_CONTENT`），派生缓存键 `sha256(contentVersion, coordinate.sha256, GeoParams, DERIVE_VERSION)` 也与它无关，"改写需重算派生键"（第 1 轮诊断）并不成立 |
| 世界面板显示"科研用途" | 两层原因：数据层同上；界面层 `WorldPanel.tsx` 只按 `redistribution === true` 二选一（`world.datasetFree` 或 `world.dataset`"…· 科研用途"），而六城的 `redistribution` 按 ADR-109 应保持 false（授权不转授第三方），因此即使 world.json 改写，面板仍会显示"科研用途" |
| 部署包会把旧文字带上公开站 | `worldpack.py` 的 `world_terms()` 按名字给出常量，不看 world.json 本身；公开站由 nginx 直出 world.json，世界面板照它显示 |

#### A.2.2 D2-AC-27

| 子项 | 根因 |
|---|---|
| 72 h：OOM 0、非预期重启 0、共享展示 RTF ≥ 0.99 | 没有 emax 访问（环境）；且没有能连接到已部署服务的观测工具：`tools/bench/sandbox/soak.py` 只在 cgroup 模拟下自建栈，公开模式隐藏 `/api/sys/procs`，RTF 只在 StateRing 与 WS 性能话题里 |
| emax 实测 §4.4.1 成本与世界成本表、ADR 冻结 N_max | 同上；成本需要在生产流量下按 cgroup 叶子组采集，此前没有工具 |
| S7 连续 7 天被发现 0、间隙按原因分列 | 同上；需要连续 7 天拉取 show-api 事件并防止游标过期 |

### A.3 修复与实现清单

| 编号 | 实现 | 路径 |
|---|---|---|
| D2-AC-24、ADR-109 第 3 条（NOTICE） | 补 ECCV 2022 BibTeX，说明同一条目在 THIRD_PARTY_NOTICES §4、两版 README 与关于页；英文 README 授权段标题加注"经数据集权利方授权使用" | `NOTICE`、`README.md` |
| D2-AC-24、ADR-109 第 3 条（world.json） | 适配器 `DATASET`：`license` 为授权说明，新增 `terms`（常量 `AUTH_TERMS`："经数据集权利方授权使用 (used under authorization of the dataset rights holders) … (ADR-109, R-D2-28)"），`notice` 不再引用 ADR-034；`name`、`version`、`url`、`citation` 不变，`redistribution` 保持 false | `python/awr/world/ingest/urbanscene3d.py` |
| 契约 | `world.schema.json` 的 `dataset` 增加可选 `terms`（minLength 1）；重新生成 Python 与 TS 类型（只多出该字段，layout_id 0x009600AE 不变）；两个 UrbanScene3D world 夹具改用适配器文字 | `packages/contracts/schemas/world/world.schema.json`、`python/awr/contracts/schema_types.py`、`packages/contracts/gen/ts/types.ts`、`tools/contracts/gen_fixtures.py`、`packages/contracts/fixtures/world/{shenzhen,sanfrancisco}/world.json` |
| 只重写元数据 | `worldpkg dataset [--check] [--json] [ids]`：只替换 `dataset` 段（保留 `sourceFiles`、其余字段与键序），持构建锁、同目录临时文件原子改名、写后校验失败即还原、锁被占用报 busy（退出码 3）；`make worlds-dataset`（`CHECK=1` 只报告），`make worlds` 构建后自动执行 | `python/awr/world/package/{dataset,cli}.py`、`mk/m03.mk` |
| 本机六城 | 已执行 `worldpkg dataset`：六城 updated，contentVersion 不变；重写前后 `cache_status` 的 derive_sha8 与条目逐一相同；`worldpkg validate worlds/*` 与 `check-world.mjs`（82 个文档）0 错误 0 警告 | `worlds/*/world.json`（本机生成物，不入库） |
| 部署打包闸门 | `world_terms()`：六城取 world.json 的 `dataset.terms`；缺少 terms 或许可仍写科研用途时拒绝打包并提示 `make worlds-dataset`；合成城市可带 terms，否则用常量 | `tools/deploy/emax/worldpack.py`；手册 2.1 |
| 世界面板 | 纯函数 `datasetNoteKey()`：可再分发 → `world.datasetFree`；有 `terms` 或名字为 UrbanScene3D → 新键 `world.datasetAuthorized`（"数据 {name} {version} · 经数据集权利方授权使用"；ADR-109 之前的包同样显示授权说明）；其余 → 通用行。Tooltip 在引用之外显示 terms；新增 `data-world-dataset-note` | `apps/web/src/lib/demo.ts`、`apps/web/src/ui/panels/world/WorldPanel.tsx`、`apps/web/src/app/query/options.ts`、`apps/web/src/app/i18n/{zh-CN,en}.json` |
| 关于页 | 数据行增加 BibTeX 块（`data-about-bibtex`）与"复制 BibTeX"（`data-copy-bibtex`，Toast `about.bibtexCopied`）；BibTeX 常量从 `WorldHub.tsx` 移到 `lib/demo.ts`，世界列表与关于页共用 | `apps/web/src/ui/views/{AboutDialog,WorldHub}.tsx` |
| 发布面一致性 | 演示提示卡 D7 改为"经数据集权利方授权使用，附 BibTeX"；`honesty.spec.ts` 的关于页与世界组断言按 ADR-109 更新（原"科研"断言在 D2 已不可能通过） | `python/awr/datasets/demo/script.py`、`tests/e2e/honesty.spec.ts` |
| D2-AC-27（工具） | `observe.py run/mark/report`：每 10 s 采 systemd（InvocationID、NRestarts）、当前发布、cgroup（单元根、core/、sandbox/、plan、ctl 与每会话叶子组的 memory.events、memory.current/peak、anon + shmem、cpu.stat）、sandbox-api 健康；后台线程每 0.5 s 在进程内读共享展示 StateRing 头部（只映射、不登记读者游标；按 `<runs>/current` 与 effective-config 的 shm_root 定位，重启后按 inode 重新映射）；每 60 s `awr status --json`（supervisor 进程表 restarts；root 下以 subprocess 降权为 awr，0600 的 EnvironmentFile 经 env= 传入，不出现在命令行上）；每 120 s `sbctl.sh --json status`；每 30 s 拉 `/api/events`（不带 types，游标覆盖全部事件，只保留 target、relay、capture、sandbox、sim 事件）；每 10 min 内核日志 OOM 行；可中断续跑；维护窗口登记 | `tools/deploy/emax/observe.py` |
| D2-AC-27（判据） | ①OOM：oom_kill 增量 0 且内核日志无本单元 OOM 行；②非预期重启：InvocationID 变化中非发布切换、非维护窗口者 + 进程表 restarts 增量；③RTF：逐秒自报 rtf 最小值与 10 s 窗口 Δt_sim/Δt_wall 最小值均 ≥ 0.99，心跳 > 2 s 未更新按 0 计，时钟 STOPPED/PAUSED/STEPPING/ENDED/RESTARTING、倍速非 ×1、维护窗口、重启或 epoch 变化（含软重置）后 120 s 内不计，STALLED 照常计入；不能导入 awr 时退回 ≥ 300 s 窗口且结论最多"未完成"；④时长 ≥ --hours、覆盖率 ≥ 0.95；⑤S7：≥ 168 h 且游标未过期时给出七天结论，带我方机体的 target.discovered 为 0，relay.gap 按 cause 分列次数、合计、最长；成本段给出 core/ 与 sandbox/ 核数、按世界的每会话核数、memory.peak、降级级别时长与生效 N_max；退出码 0/1/2/3 | 同上；部署手册 5.7 |
| 部署用例维护 | `test_demo_chunks_landing_and_api_docs` 允许 D2-FX-R2-web 新增的早期 `boot` 小分块（< 64 KiB），其余断言不变 | `tests/ops/test_deploy_install.py` |

### A.4 测试结果

#### A.4.1 新增与修改的用例

| 用例 | 内容 | 结果 |
|---|---|---|
| `tests/ops/test_release_notices.py`（7 例） | NOTICE、THIRD_PARTY_NOTICES、README、README.zh-CN 各含"经数据集权利方授权使用"与逐字相同的 BibTeX；NOTICE 保留授权范围；前端常量与之相同；关于页与世界面板的文案 | 通过 |
| `tests/world/test_dataset_terms.py`（8 例） | 适配器文字；只改 dataset 段、键序与 contentVersion 不变、派生键输入不变、幂等、--check；跳过与缺失；构建锁 busy；校验失败还原；CLI 退出码；本机六城与适配器一致 | 通过 |
| `tests/ops/test_deploy_install.py`（改 2 例、增 1 例） | `world_terms` 取 world.json 的 terms；拒绝 ADR-109 之前的说明并提示 `make worlds-dataset`；落地页分块 | 通过 |
| `tests/ops/test_deploy_observe.py`（18 例） | 模拟 cgroupfs、systemctl、journalctl、awr status、sbctl 与回环 HTTP 桩：干净运行通过；时长不足与覆盖不足为未完成；memory.events 与内核日志 OOM；单元重启（无因、发布切换、维护窗口）；子进程退避重启；RTF 下降、软重置预热不计；暂停不计、STALLED 计入；sim-core 卡死（心跳停）；无 StateRing 时退回稀疏采样为未完成；S7 被发现与事件游标过期；续跑；按世界的会话成本与 epoch 增量；口令不进命令行；CLI；真实 StateRing 的 RingReader（不占读者游标、重启后重新映射） | 通过（83 s） |
| `apps/web/tests/m15/datasetNote.test.ts`（3 例） | `datasetNoteKey` 的全部分支；授权文案不含"科研"；BibTeX 与四份发布面文档逐字相同 | 通过 |

#### A.4.2 回归

| 范围 | 命令 | 结果 |
|---|---|---|
| Python | `pytest -m "not perf and not slow" tests/ops tests/contracts tests/world tests/e2e/test_builtin_worlds.py tests/e2e/test_demo_check.py` | 887 passed、1 failed、41 skipped（需要 nginx）；失败的 `test_demo_chunks_landing_and_api_docs` 由并行的 D2-FX-R2-web 在 `vite.config.ts` 新增 `boot` 分块引起，修正断言后单独复跑通过，`tests/ops` 全部复跑 217 passed、41 skipped |
| 契约 | `make contracts-check` | 全部 up to date（layout_id 0x009600AE，contracts 1.2.0） |
| Vitest | `npx vitest run --project unit` | 164 个文件通过（1 个跳过），1347 passed、1 skipped |
| 类型与 lint（本包文件） | `npx tsc -p tsconfig.json --noEmit`；`npx oxlint --type-aware`（本包 6 个 TS 文件）；`ruff check`（本包全部 Python）；`no-emoji`、`no-hex` | 全部通过 |
| Playwright e2e | 私有测试构建（`M15_DIST=… node perf/m15/helpers/build-ui.mjs`，`AWR_WEB_DIST` 指向它）后 `npx playwright test --project=e2e honesty` | 4 例中 3 例通过（含改写后的关于页与世界组断言：授权语句、BibTeX、世界组"经数据集权利方授权使用"）；第 1 例"world facts are labelled honestly"失败，为 D2 布局下的既有问题（世界组内容不在 innerText，点击落在菜单栏"世界"），与本包无关，已请 M16 处理 |
| Playwright M15 | `npx playwright test --project=perf m15/fxweb2.spec.ts -g "About content|report fidelity"` | 2 passed（关于页 glyphScan 为空） |
| 世界与派生缓存 | `worldpkg dataset --check`；`worldpkg validate worlds/*`；`node tools/contracts/check-world.mjs worlds/*` | 六城 current；七个世界 0 错误 0 警告；82 个文档 0 invalid |

#### A.4.3 真实世界的打包与安装模拟

`worldpack.py pack`（七个世界，gzip 1）→ `verify` → `install` 到临时世界目录 → `worldpkg dataset --check` 与 `geometry.cli check --all --deep`：
打包 90 s、合计 0.99 GiB；索引中六城的 `terms` 为 world.json 的授权说明、synthcity 为可再分发说明；校验通过；解包后六城 current，七个世界的
派生缓存条目（derive_sha8 与重写前相同）deep 校验均为可用。临时产物已删除。

#### A.4.4 观测工具的本机真实栈自检

本机起 ci profile 的 supervisor（synthcity，sim-core + api，临时 runs 目录），以 `.venv` 运行 `observe.py run --hours 0.04 --interval 10
--status-every 30`：RTF 来源 ring（每 10 s 约 20–22 次读数），逐秒自报 rtf 最小 0.991、14 个 10 s 窗口 Δt_sim/Δt_wall 最小 0.999、心跳停顿 0；
进程表 restarts 0；事件正常拉取。自检中发现并修正了三处：时钟状态名为 PLAYING（不是 RUNNING）；StateRing 在 `<shm_root>/<run_id>/`
而不在持久运行目录；以子进程 `awr status` 的时刻推算 RTF 在 10 s 窗口上有约 ±1.5 % 的抖动（0.985–0.988 的假低值），因此改为进程内读环。
本机 `awr status` 单次 CPU 0.79 s（单线程环境变量下；默认 1.8 s），按 60 s 间隔约 0.013 核；短时自检的观测进程合计 0.067 核（启动导入占大头），
emax 上的实际值记入报告 `observer`。栈已停止，共享内存目录已清理。

### A.5 偏差

| # | 偏差 | 原因与影响 | 处置 |
|---|---|---|---|
| 1 | 修改了 M03、M15、M16 的文件 | 工作包授权"修复区域 deploy，允许跨模块修改该区域相关代码"；D2-AC-24 的根因跨发布面、生成器与界面三层 | 逐项知会所有者（`d2-FX-R2-deploy-to-{M03,M15,M16}.md`） |
| 2 | 契约增加可选字段 `dataset.terms` 而不升契约版本 | 可选字段向后兼容（ADR-109 之前的包没有它，界面与打包均按名字兜底）；升版本会改动全部生成物头部与并行工作包的布局对拍 | 契约包仍为 1.2.0、world.json schemaVersion 1.0.0；已请文档总编在 16 §3.2 登记 |
| 3 | 关于页增加 BibTeX | 第 1 轮把世界列表的引用弹层算作关于页通过；判据原文要求"关于页含 BibTeX"，关于对话框此前只有一行引用 | 关于页与世界列表共用同一常量，用例逐字核对 |
| 4 | D2-AC-27 未执行 | 没有 emax 访问 | 交付工具与手册 5.7；本机以模拟与真实栈验证；结论仍为"未测" |
| 5 | N_max 未冻结 | 判据要求以 emax 实测冻结；任务约束"暂定阈值仅在数据支撑时以 ADR 冻结" | 观测报告的 `costs` 段为冻结 ADR 的输入（请求文档总编） |
| 6 | 性能报告页脚仍写"科研用途" | M16-FR-010 与 M16 §6.3 规定了固定格式，报告页不在公开站；改动需要先修订规格 | 请求 M16 与文档总编按 ADR-109 修订 |
| 7 | "共享展示 RTF ≥ 0.99"取逐秒自报 rtf | 判据未定义窗口；取与 D2-AC-04 容量工具相同的口径，同时给出 10 s 窗口值 | 请文档总编冻结口径（可放宽为分钟级窗口） |
| 8 | 沙盒会话重启不进判据 | 池状态只有 epoch，驻留恢复也会增加 epoch | 报告记录 `sandbox_epoch_bumps`；请 M17 提供重启计数 |

### A.6 遗留

1. **D2-AC-27 的执行**：需要有 emax 访问的运维按手册 5.7 运行 168 h（72 h 服务判据 + 七天 S7），把 `report.json`、`samples.jsonl`、
   `events.jsonl` 拷回开发机，作为验收证据与 N_max ADR 的输入。
2. **既有失败**：`tests/e2e/honesty.spec.ts` 第 1 例在 D2 布局下失败（M16）。
3. **`make lint` 的非本包失败**（并行工作包正在修改的文件，本包文件全部通过）：ruff 21 处（`python/awr/sim/perception/{kernels,runtime,pairs}.py`、
   `python/awr/sim/targets/kernels_los.py`、`python/awr/sim/tasking/{relay,runtime}.py`、`python/awr/tasking/{decompose,plan_kinds,station}.py`、
   `tests/perception/test_kernels.py`）；oxlint 5 处（`apps/web/src/app/i18n/capacity.ts` unbound-method）；GLYPH-01 2 处（perception
   `kernels.py:673`、`runtime.py:919` 的 U+2194）；M06-L-06 1 处（`apps/web/src/viewport/sensorView/pass.ts:91`）。
4. **通用数据集行**：`world.dataset`（非 UrbanScene3D、不可再分发的第三方数据集）仍写"科研用途"，七个世界都不会显示，是否改为中性文字由 M15 决定。
5. **生成器版本解耦**（`d2-WP-16-to-M03.md` 第 3 条）仍待 M03。

### A.7 跨所有权请求

| 文件 | 接收方 | 要点 |
|---|---|---|
| `d2-FX-R2-deploy-to-M03.md` | M03 | 知会：`DATASET` 改写与 `AUTH_TERMS`、契约 `terms`、`worldpkg dataset` 与 `make worlds-dataset`、本机六城已重写、新用例；生成器版本解耦仍待处理 |
| `d2-FX-R2-deploy-to-M15.md` | M15 | 知会：`datasetNoteKey`、世界面板、关于页 BibTeX、新 i18n 键；通用 `world.dataset` 文字待定 |
| `d2-FX-R2-deploy-to-M16.md` | M16 | 知会 honesty 与 D7 的改动；honesty 第 1 例的既有失败；报告页脚"科研用途"与 ADR-109 冲突；观测工具可登记为 emax 口径用例 |
| `d2-FX-R2-deploy-to-M17.md` | M17 | 池状态增加会话重启计数与原因；观测报告的成本段供 N_max 冻结 |
| `d2-FX-R2-deploy-to-文档总编.md` | 文档总编 | 16 §3.2 登记 `dataset.terms`；只重写元数据的流程；D2-AC-27 测量口径与 N_max 冻结；M16-FR-010、§6.3、§6.4 D7、离线包 NOTICE 的旧文字；D2-AC-24 判据补写 world.json |
