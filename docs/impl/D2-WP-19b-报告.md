# D2 WP-19b 感知量化与场采样：实现报告

| 项 | 内容 |
|---|---|
| 工作包 | WP-19b 感知量化与场采样（所有者 M19，含 M18 字段；AWR-04 §15.15.1；依赖 WP-05、WP-06；第 4–6 周） |
| 日期 | 2026-10-05 |
| 依据 | 用户 D2.1 原文 `docs/inputs/D2.1-需求原文-2026-10-05.md`（"待识别物的感知能力需要量化……声音视图……递减的辐射场"）；AWR-04 v1.3 §15.10、§15.11、§15.12、§15.14（D2-AC-52、53、55）、§15.15、ADR-124、ADR-125、ADR-126；M18-M19 PRD §15（M18-FR-028 至 032、M19-FR-030 至 037、M19-NFR-007 至 009、M18-AC-023 至 025、M19-AC-021 至 026）；17 §17.20.6、§17.20.7；14 §20.26.7；D2 报告 WP-05、WP-06；请求 d2-WP-06-to-M18（`LosCache.peek`） |
| 路径（只写本包） | `python/awr/sim/perception/**`、`python/awr/sim/targets/**`、`python/awr/api/rest/perception.py`、`apps/web/src/engine/perception/**`、`apps/web/src/stores/perception.ts`；测试目录 `tests/perception/**`、`tests/targets/**`（M18 字段）、`apps/web/tests/m19/**` |
| 约束执行 | 未安装依赖；未执行 git 写操作；未跑性能基准（成本用例 `test_field_budget.py` 标 `perf`，未运行；§4.3 的数字为开发机观测，不作结论）；无 emoji 与禁用字形（本包文件 no-emoji 通过，Python 文档中的希腊字母按 ruff RUF002 改为拉丁拼写）；本包无颜色、无新渲染代码（场的绘制属 WP-11b）；跨所有权需求写入 `.cache/impl/requests/d2-WP-19b-to-*.md`（9 份）与契约草案目录 `d2-WP-19b-drafts/` |
| 结论 | P0 全部实现并有功能测试：M18 的四个新字段与类别缺省、逐对量化读数（声学、视觉、合成概率、判据一致性、驻留、反向感知）、四种辐射场剖面、感知体表、按订阅生成的场服务与三类载荷、REST SB-73 至 SB-75、前端解码与插值、配置体掩码与 store 订阅；P1 全部实现：雷达敏感体判据与雷达读数、显式采样 SB-78。新增 Python 用例 77 个（另 1 个 perf 未运行）、TS 用例 18 个，全部通过；本包文件 ruff、oxlint、tsc 与 tools/lint 全部通过；最后一次 `make lint` 只剩他包文件 `tests/e2e/test_sandbox_templates.py` 的 no-emoji 3 处（M16，§4.4）。端到端（网关拆分与兴趣下推）依赖 M11、M08 的请求（§6），在此之前由本地订阅驱动的台架验证 |

## 0. 摘要

1. **量化读数**（`perception/quant.py`）：每个（识别物，飞行器）对给出 24 带合成到达声压、主导带加权带级、该带阈值、背景、余量与声学概率（logistic，2 dB，m = 0 处 0.5）；
   角尺寸、可见尺寸、等效像素（`visual_acuity_arcmin`）、检测阈、比值、表观对比度、照度档、灯光、角向覆盖与视觉概率（TTPF 同形，x = 1 处 0.5）；雷达 SNR 与概率（P1）；
   合成概率；按当前判据模式的 `would_discover`（与 M18 判据逐项一致，1000 组随机对 100%）；驻留与所需驻留；反向感知（该机成像传感器对该识别物的最佳感知）。
2. **辐射场与感知体**（`perception/field.py`）：声学、视觉、热（参考 `lwir640` 25 mm）、雷达（参考 `mmw77`）四种剖面（共享对数网格 32 点、6 个仰角格因子与截止距离、
   参考壳半径），识别物感知体有效半径表（8 方位 × 6 仰角，与 M18 `effective_radius` 同参数）与过渡宽度；确定性 Halton 图案与显式采样；rev 缓存与关键帧。
3. **场服务**：sim-core 慢任务 `m19.field`（10 Hz【墙钟】检查，无订阅时零工作），按兴趣下推的 `streams.field`、`streams.quant`、`streams.fsub` 生成：剖面 1 Hz、
   感知体表 2 s 检查（变更与 30 s 关键帧）、逐对读数 2 Hz【仿真】；总线 `state/sim-core/field`、`state/sim-core/target_quant`，每个载荷只编码一次。
4. **REST**：SB-73 `GET /perception/quant`、SB-74 `GET /perception/field/{kind}`、SB-75 `GET /perception/volumes`、SB-78（P1）`POST /perception/field/{kind}/samples`。
5. **前端**：`engine/perception/quant.ts`（逐对纯函数的 TS 移植，与 Python 对拍）、`engine/perception/field.ts`（三类载荷解码、增量帧合并、剖面插值、等级带、等值壳半径、
   感知体表插值与概率、配置体掩码、`fieldModel`）、`stores/perception.ts`（场视图模式、感知体模式、物理参考、设备档、图例、读数与订阅管理，手机降频）。
6. **M18 字段**：`visual_acuity_arcmin`、`hearing_band_adj_db[24]`、`radar_rx`（P1，雷达敏感体判据、有效半径、禁入体、detail）、`rf_rx`（P2，只入 schema）；
   签名增加 RCS、热斑温升、热斑占比、发射率与派生量 `SL_A`、`D_vis`、`d_hot`；`LosCache.peek`。

## 1. 实现清单（需求与验收编号）

### 1.1 M18 字段与判据（`python/awr/sim/targets/`）

| 需求 | 实现 | 文件 | 验收（测试） |
|---|---|---|---|
| M18-FR-028 角分辨率 | `visual_acuity_arcmin`（0.3–20′，110 字段级）；类别缺省 person 1.0、uav 2.5、dog 4.0、vehicle 1.0，machine、object 无（`visual_acuity_of`，使用处解析）；不进入判据 | `model.py` | M18-AC-023：`tests/targets/test_d21_fields.py`（校验、缺省、导出不含、`test_acuity_does_not_change_criterion` 发现时刻相同）；`test_quant.py::test_class_default_acuity` |
| M18-FR-029 类别频段修正 | `hearing_band_adj_db[24]`（−20 至 +20 dB，null 清除）；`acoustic_margin`、`acoustic_radius`、`acoustic_radii` 的 `adj_db`（None 时不做加法，与 D2 逐位一致）；M18 判据与有效半径计入 | `model.py`、`acoustic.py`、`runtime.py`、`effective.py` | M18-AC-023：`test_band_adjustment_default_is_bitwise_d2`（50 组 `array_equal`、+7 dB 余量加 7）；M18-AC-009 至 011 原用例复跑通过 |
| M18-FR-030 雷达敏感体（P1） | `radar_rx{snr_ref_db, r_ref_m, snr_req_db, fov_az_deg, fov_el_deg}`；配置后 radar 体进入判据（geometric 只看配置体；physical 另要求接收视场内且 SNR ≥ snr_req，雨衰 1.0·RR^0.7 dB/km）、有效半径、禁入体与 detail 的 `radius_eff_m`；未配置时仍只入 schema 并给 warning | `radar.py`（新）、`model.py`、`runtime.py`、`effective.py`、`envview.py`（`rain_mmph`） | M18-AC-024：`tests/targets/test_radar_body.py`（首次满足距离与解析半径 189.15 m 在一个步长内、视场外不满足、geometric 600 m、未配置不参与、禁入体与 detail） |
| M18-FR-031 RF 接收（P2） | `rf_rx{sens_dbm, f_mhz, bw_mhz}` 校验，只入 schema | `model.py` | `test_new_fields_validate` |
| M18-FR-032 配置体掩码 | `bodyMask(body, targetPos, yaw, pts, out)`：调用 `engine/targets/geometry` 的 `bodyVolumes` 与 `insideVolume`（与禁入预测同一实现）；位置见偏差第 10 条 | `apps/web/src/engine/perception/field.ts` | M18-AC-025：`apps/web/tests/m19/field.test.ts`（10⁴ 个随机点 × 4 种形状与 Python `inside_points` 逐点相等） |
| 签名特征量 | `VehicleSignature` 追加 `rcs_m2`、`ir_delta_t_k`、`ir_hot_fraction`（M21 写入前取 §15.10.2 机型缺省表）、`ir_emissivity`；`lwa_dba`、`sl_a_1m_dba`、`d_vis_faces`、`d_hot_m`、`lights_on` | `signature.py` | D2-AC-52 派生量：`test_quant.py::test_derived_signature_values`（P600、Q1、Q9、F1、V1 的 SL_A、D_vis 三向、d_hot、RCS 与表一致） |
| 只读接口 | `LosCache.peek`（不消耗令牌、不移动 LRU，返回值与年龄；d2-WP-06-to-M18 第 2 条）；`_key` 改为标量 `math.floor`（结果不变）；`TargetRuntime.fleet(S, ctx)`、`quant_view(id)` | `los_cache.py`、`runtime.py` | `test_d21_fields.py::test_los_peek_is_read_only`；M18 全部原用例 |

### 1.2 M19 量化与场（`python/awr/sim/perception/`）

| 需求 | 实现 | 文件 | 验收（测试） |
|---|---|---|---|
| M19-FR-030 逐对量化 | `acoustic_pair`、`visual_pair`、`radar_pair`（单对纯函数）与 `acoustic_batch`、`visual_batch`（批量，逐项相等）；`quant_pairs(t, uavs, env, los_fn, reverse_fn, uav_filter)` 按斜距升序 ≤ 12 对；视线先读 `LosCache.peek`，只在视线决定读数或判据时以 M19 配额请求；反向感知取 `uav/{id}/perception` 帧中该识别物的最佳等级 | `quant.py`、`field.py` | M19-AC-021：`tests/perception/test_quant.py`（标定、单带锚点 145.7 m 处 P = 0.50、§15.10.2 算例、与 `ref_quant.py` 各 200 组对拍：声压与余量 ≤ 0.1 dB、角尺寸与等效像素与对比度 ≤ 1%、雷达 ≤ 0.1 dB；批量与单对相等；排序、上限与过滤） |
| M19-FR-031 判据一致性 | physical：参与者 ∧ 某模态（位于配置体内 ∧ 门控 ∧ P ≥ 0.5），布尔值与 M18 判据同一表达式；geometric：参与者 ∧ 位于任一判定体内 | `quant.py` | `test_would_discover_matches_m18_criterion_1000_pairs[physical, geometric]`（各 1000 组随机识别物、机体、环境、灯光、频段修正、雷达接收：与 M18 `_evaluate` 的命中 100% 一致） |
| M19-FR-032 剖面 | 四种剖面、共享网格（√2 阶梯上取整的 r_max，≤ 2000 m）、仰角因子、截止距离、参考壳 `shell_m`、帧级 `iso`；rev（0.5 dB / 2%） | `field.py` | M19-AC-022：`tests/perception/test_field.py`（五个机型 × 两种环境 × 四种剖面与参考实现逐点一致、声学单调与下限、视觉截止与夜间规则、§15.11.3 算例 1499 m / 100 m / 85 m、插值与精确采样 ≤ 0.5 dB 或 2%、线格式互逆、rev 阈值、Halton） |
| M19-FR-033 感知体表 | `volume_table(raw, body, sig, env, a)`：声学按方位 `acoustic_radii`、视觉按仰角（M18 判据口径）、雷达按视场；过渡宽度；缓存（参数、机型、灯光、环境键、警觉度） | `field.py` | M19-AC-023：`tests/perception/test_volumes.py`（与 M18 `effective_radius` 与参考实现 ≤ 1 m、背向衰减、P(r_eff) = 0.5、警觉度与频段修正、雷达 P1、折叠互逆） |
| M19-FR-034 topic 与服务 | `FieldService`：订阅驱动（`streams.field/quant/fsub` 与本地订阅）、剖面 1 Hz 与 5 s 关键帧（`same` 引用）、感知体表 2 s 检查与 30 s 关键帧（未变识别物跳过重算）、读数 2 Hz【仿真】且墙钟 ≥ 0.25 s、新订阅即关键帧；`state/sim-core/{field,target_quant}`，items 为 msgpack bin（`use_single_float`） | `field.py`、`plugin.py`、`runtime.py` | M19-AC-024：`tests/perception/test_field_wire.py`（无订阅零生成、1 Hz 与增量帧、5 s 关键帧、fsub 关键帧、四种剖面与感知体与读数通过 schema、帧大小、带宽） |
| M19-FR-035 REST | SB-73、SB-74、SB-75、SB-78（P1）；564 在契约合入前以 110 回复并附 `detail.code = 564` | `api/rest/perception.py`、`plugin.py` | `test_field_wire.py::test_rest_*`（形状与 schema、404/400/422、REST 剖面与 topic 同一状态、显式采样的确定性、单调与配置体筛选） |
| M19-FR-036 前端 store | 见 §1.3 | `stores/perception.ts` | M19-AC-025：`apps/web/tests/m19/store-field.test.ts` |
| M19-FR-037 参考实现 | `tests/perception/ref_quant.py`（只用 math；带表与谱形取 M18 的独立参考 `ref_acoustic.py`）；TS 夹具生成器 `test_field_golden.py` | 测试 | 生成 `apps/web/tests/m19/{quant_cases,field_cases}.json` 并校验与当前实现一致 |
| M19-NFR-007 成本 | 静态缓存与批量化（§2 第 5 条）；`test_field_budget.py`（perf） | `field.py`、`quant.py` | 未运行（并行阶段约束），开发机观测见 §4.3 |
| M19-NFR-008 带宽 | 剖面关键帧 0.89–1.07 KB（12 架、3 机型）、读数帧 1.06 KB（12 对，`soa1`）、感知体关键帧 7.4 KB（30 识别物），60 s 平均 3.0 KB/s | 同上 | `test_profile_topic_keyframes_rev_and_size`、`test_quant_topic_soa_and_rest_parity`、`test_bandwidth_per_subscriber` |
| M19-NFR-009 一致性 | 前端插值与显式采样 ≤ 0.5 dB 或 2%；表与 `effective_radius` ≤ 1 m | 同上 | `test_field.py::test_interpolation_vs_exact_samples`、`field.test.ts`、`test_volumes.py` |

### 1.3 前端（`apps/web/src/engine/perception/`、`apps/web/src/stores/perception.ts`）

| 文件 | 内容 |
|---|---|
| `quant.ts`（新） | 24 带表、A 计权、ISO 9613-2 吸收插值、`pLogistic`、`pVisual`、`pAny`、`thresholdBands`、`acousticPair`（含单带变体）、`visualPair`、`radarPair`、`defaultBandShape`、`tonalMasking`、`acousticRadius`：Python 的 TS 移植，供缺省"人"壳等即时提示；`quant.test.ts` 对拍（声学 200、视觉 200、雷达 100、谱形、半径、列式解码） |
| `field.ts`（新） | `decodeFieldProfile`（关键帧、增量帧、`same`、`v_enc` 两种定标）、`profileValue`（add 对 ln r 线性，mul 对 (ln r, ln v) 线性，仰角最近格，截止）、`profileBand`、`isoRadius`、`decodeFieldVolumes`（折叠展开、增量）、`volumeREff`（方位线性、仰角最近）、`volumeP`、`tableRef`、`bodyMask`、`decodeTargetQuant`（`soa1` 与展开形式）、`QUANT_SCALE`、`fieldModel`、`volumeAnchorIndex`、`azRel`；`field.test.ts` 对拍 |
| `index.ts` | 导出 `quant`、`field` |
| `stores/perception.ts` | 新状态 `fieldView`、`volumesMode`、`physicalRef`、`deviceClass`、`legend`、`quant`、`fieldVersion`；订阅 `field/{kind}@1`、`field/volumes@1`（感知体或声学、视觉视图时）、`target/{selected}/quant@2`（手机 @1）；手机剖面 0.5 Hz（隔帧应用）；新连接清空旧数组；动作 `setFieldView`、`cycleFieldView`（Shift+V）、`setVolumes`、`setPhysicalRef`、`setDeviceClass`；`fieldTopics`、`readoutRows`；无写请求（viewer 安全）。原有 HUD、捕获与检测功能不变（`store.test.ts` 通过） |

## 2. 设计要点

1. **判定与显示分离**（ADR-124）：概率只用于读数与透明度；`would_discover` 的布尔值直接用 M18 判据的同一表达式（同序的 `acoustic_margin`、`dvis/r ≥ θ_th`、
   `|C|·τ ≥ 0.05`、`k_los > 0.5`、雷达视场与 SNR），因此与判定逐项一致，概率在边界处恰为 0.5。
2. **两种"体"的对偶**：飞行器辐射场的缺省人可闻壳与 M18 有效半径同一函数；选中识别物的壳取感知体表（与 `effective_radius` ≤ 1 m）。
3. **线格式只编码一次、按变化下发**：剖面与感知体用 rev 判定（阈值 0.5 dB / 2% / 1 m），关键帧周期 5 s / 30 s，新订阅以兴趣下推的订阅代数 `fsub` 触发即时关键帧；
   读数用列式 `soa1`（定标整数、全列相同折叠为标量），解码与 REST 逐位相等。
4. **视线预算**：读数的视线先读缓存（`peek`，不耗令牌），只有在配置体内或视线可见时概率可能非零的对才以 M19 配额请求一条顶部射线（12 对 2 Hz 最多 24 条/s，实际多为缓存命中）。
5. **成本**：剖面的静态量（r_max、仰角因子、截止、参考壳）与每机型的线格式按（指标，机型，灯光，环境键，网格）缓存；感知体表按识别物输入签名（配置版本、警觉度、
   环境、机型与灯光）跳过未变者，表与线格式按参数键缓存；读数按模态批量计算；慢任务在无订阅时不绑定、不生成。
6. **前端分层**：物理量只在服务端；前端只解码、插值与查表（P-02）；`fieldModel` 框架无关，渲染层按 `version` 取用；store 只读。

## 3. 偏差

| # | 条款 | 偏差 | 理由与处置 |
|---|---|---|---|
| 1 | 17 §17.20.6 `v_r` "u16，量化 = 值 × 10" | 帧级 `v_enc`：dB 量 `lin`（×10，雷达加 `v_off = −10`）；视觉与热 `log`（`round((lg v + 8)·4000)`） | ×10 在 1′、1 px 量级误差 5%，违反 M19-NFR-009 的 2%；已提文档总编第 1 条 |
| 2 | 17 §17.20.6 剖面字段 | 增加 `keyframe`、`v_enc`、`v_off`、`iso`、`shell_ref`、每机 `shell_m` 与 `same`；非关键帧省略网格与图例；`f_el`、`cut_el_m` 全相同时 1 项；末尾不绘制项省略 | 满足 1.2 KB 帧与 4 KB/s 带宽；文档总编第 2、6 条 |
| 3 | 17 §17.20.6 `target/{id}/quant` 的 `pairs[{...}]` | topic 为列式 `soa1`；REST 保持 `pairs[{...}]` | 逐对键名编码 12 对约 4.9 KB，2 Hz 超带宽；列式 1.06 KB。文档总编第 3 条 |
| 4 | §15.11.4 仰角因子 | 剖面与感知体都按最近仰角格取值（剖面顶面因子实际在 |e| ≥ 30° 生效）；感知体视觉表沿用 M18 判据的"仰角 ≥ 45° 取顶面"；感知体前端方位线性插值、过渡宽度按 r_eff 比例缩放；SB-78 识别物锚点给表模型值 | 6 格离散化的必然结果，前端与服务端显式采样同一模型；文档总编第 4 条 |
| 5 | 读数 2 Hz【仿真】 | 另加墙钟间隔 ≥ 0.25 s | 高倍速时避免无谓计算（网关按订阅频率也会丢弃）；文档总编第 5 条 |
| 6 | 手机剖面 0.5 Hz | 订阅 1 Hz（rate_classes 最低 1），前端隔帧应用 | 不改契约；若需节省带宽请 M00、M11 增加 0.5 档（d2-WP-19b-to-M11 第 4 条） |
| 7 | 感知体关键帧 | 除 30 s 外，新订阅（`fsub` 变化）与机型集合、识别物集合变化时关键帧 | 第二个订阅者不必等 30 s；需 M11 下推 `fsub` |
| 8 | 类别缺省（§15.10.1） | 在使用处解析，不写入合并后的配置 | 契约 `target.schema.json` 的 `additionalProperties: false`，合入 1.2.0 前写入会使导出与快照校验失败；文档总编第 8 条 |
| 9 | M18-FR-030 雷达判据 | 判据另含接收视场（`fov_*` 为半角，方位轴为航向 + 敏感体偏置）；SB-46 建议半径不含雷达体 | 视场是 `radar_rx` 的组成部分；建议半径 schema 的 kind 只有 visual、acoustic。文档总编第 9 条、M00 第 6 条 |
| 10 | M18-FR-032 `bodyMask` 在 `engine/targets/geometry.ts` | 放在 `engine/perception/field.ts`，内部调用 geometry 的 `bodyVolumes` 与 `insideVolume` | engine/targets 不在 WP-19b 路径；同一实现；文档总编第 10 条、d2-WP-19b-to-M06 第 3 条 |
| 11 | 推力与灯光 | 推力比取 1；灯光取 M18 `lights_fn`（缺省关闭） | 与 WP-05 相同的接口缺口（d2-WP-19b-to-M08 第 2 条） |
| 12 | PRD M19-FR-037 夹具位置 `packages/contracts/fixtures/perception/` | 夹具生成在 M19 测试目录 `apps/web/tests/m19/` | packages/contracts 属 M00（d2-WP-19b-to-M00 第 9 条） |

## 4. 测试结果

本机运行（多工作包并行，load average 约 8）；性能基准未运行。

### 4.1 命令与结果

| 范围 | 命令 | 结果 |
|---|---|---|
| 本包新增 Python | `pytest tests/perception/test_{quant,field,volumes,field_wire,field_golden}.py tests/targets/test_{d21_fields,radar_body}.py` | 77 passed（`test_field_budget.py` 1 个 perf 未运行） |
| 本包全部 Python 与相关 | `pytest tests/targets tests/perception -m "not perf"` | 348 passed、3 deselected（perf） |
| 相关回归 | `pytest tests/targets tests/perception tests/tasking tests/contracts/test_d2_stubs.py -m "not perf"` | 437 passed（冻结签名检查对正式模块生效） |
| 契约 | `pytest tests/contracts -m "not perf"` | 351 passed（期间曾有 1 failed：`test_lint_tools.py::test_repository_passes_py_imports`，违例在 WP-04b 进行中的 `rest/sandbox.py`，已由该包修复后复跑通过） |
| TS 本包 | `npx vitest run --project unit tests/m19 tests/m18` | 11 个文件 52 passed（新增 `quant.test.ts` 7、`field.test.ts` 5、`store-field.test.ts` 3；原有用例不变） |
| TS 全量单元 | `npx vitest run --project unit --reporter=verbose` | 156 个文件 1266 passed、1 skipped（62 s；缺省报告器在高负载下曾 25 min 无输出被超时终止，改用 verbose 报告器一次通过） |
| 类型检查 | `npx tsc -p tsconfig.json --noEmit` | 无错误 |
| lint（本包） | `ruff check`（本包 Python）、`npx oxlint --type-aware`（本包 TS）、`tools/lint` 的 no-emoji、no-hex、lint-lf、check-units、check-deps、no-raw-controls、check-icons、motion-lint、check_py_callbacks | 全部通过（本包文件） |
| `make lint`（最后一次） | `make lint` | ruff、oxlint、py-imports、py-callbacks、check-units、check-deps、check-version 等全部通过；唯一失败 no-emoji 3 处在 `tests/e2e/test_sandbox_templates.py`（M16，非本包），见 §4.4 |
| Playwright | — | 本包无界面与渲染代码，没有 Playwright 用例；场视图、读数卡的浏览器用例属 WP-11b、WP-12c、WP-13b（请求已发） |

### 4.2 线上形式实测（`Scene` 台架：12 架 P600、AWR-Q9、AWR-F1 各 4 架，30 识别物）

| 项 | 实测 | 阈值 |
|---|---|---|
| 剖面关键帧（声学 / 视觉 / 热 / 雷达） | 973 / 1044 / 1062 / 894 B | ≤ 1.2 KB |
| 剖面增量帧（rev 未变） | < 0.5 KB | — |
| 感知体关键帧（30 识别物 × 2 体 × 3 机型） | 7.4 KB | ≤ 20 KB |
| 读数帧（12 对，列式 `soa1`；展开形式对照） | 1056 B（展开 4943 B） | ≤ 1.5 KB |
| 声学场 + 感知体 + 读数，60 s 平均 | 声学 418 + 感知体 401 + 读数 2162 = 2980 B/s | ≤ 4 KB/s |

### 4.3 开发期成本观测（非基准，负载约 8，`time.process_time`）

| 项 | 观测 | 预算（M19-NFR-007） |
|---|---|---|
| 剖面（12 架、单指标，静态缓存命中） | 0.67–0.9 ms/次 | ≤ 0.5 ms/次 |
| 感知体表（稳态，未变识别物跳过） | 0.08 ms/次 | — |
| 感知体表（未命中） | 2.7 ms/（体，机型） | ≤ 3 ms |
| 逐对读数（12 对，含列式编码前的展开） | 1.3 ms CPU/次（`quant_pairs`），1.6 ms（含组帧） | ≤ 0.3 ms/次 |
| 无订阅 | 慢任务只读 `ctx.streams` 后返回 | 0 |

读数在本机负载下约为预算的 4–5 倍，MS5 按 ADR-033 在空载机测量；后续优化方向见 §5 L1。

### 4.4 `make lint` 的他包失败

开始时的基线 `make lint` 有 ruff（`python/awr/sandbox`，WP-04b）、oxlint（`src/ui/panels/tasks`，M15）两项失败，并行工作包已在本包结束前修复。最后一次运行只剩：

| 规则 | 文件 | 归属 |
|---|---|---|
| no-emoji（GLYPH-01 两处、EMOJI-01 一处） | `tests/e2e/test_sandbox_templates.py:78` | M16（WP-13 或 WP-13b，非本包） |

## 5. 遗留

| 编号 | 事项 | 负责 | 说明 |
|---|---|---|---|
| L1 | 逐对读数成本（目标 0.3 ms/次） | M19 | 本机负载下约 1.3 ms CPU；下一步由批量数组直接生成列式载荷（跳过逐对对象与展开形式），REST 按需展开；MS5 实测后定 |
| L2 | 端到端：网关兴趣下推（`field`、`quant`、`fsub`）与拆分（`field/{kind}`、`target/{id}/quant`），sim-core 兴趣流名 | M11、M08 | d2-WP-19b-to-M11、M08；在此之前服务只能由本地订阅驱动 |
| L3 | 契约 1.2.0：三类 schema、topics、564、FieldKind、总线键、interest、target 与 vehicle 字段、设计表与 OpenAPI 快照 | M00 | d2-WP-19b-to-M00（草案齐全） |
| L4 | 场视图渲染、图例、读数卡、工具条、Shift+V、手机图层抽屉 | M06、M15 | d2-WP-19b-to-M06、M15（接口已稳定） |
| L5 | Playwright（D2-AC-52、53）、成本与变体 D 基准（D2-AC-55） | M16 | d2-WP-19b-to-M16 |
| L6 | 机型目录 `ir.hot_fraction`、`derived.sl_a_1m_dba` | M21 | d2-WP-19b-to-M21（运行时已用表值） |
| L7 | 推力比与灯光的只读接口 | M08 | 声学场随推力与夜间视觉随灯光（偏差第 11 条） |
| L8 | 热与雷达场的识别物侧判据之热像部分 | M19 | P2（AWR-04 §15.10.3），只入 schema |

## 6. 请求

`.cache/impl/requests/` 下 9 份与草案目录：`d2-WP-19b-to-{M00,M06,M08,M11,M15,M16,M17,M21,文档总编}.md`，草案 `d2-WP-19b-drafts/`（三类载荷 schema、topics、
reasons、enums、bus keys 与 interest、target 与 class_defaults、vehicle_profile、设计表四行）。要点：M00 合入契约 1.2.0 与设计表；M11 兴趣下推与拆分；M08 兴趣流名；
M06、M15 的前端接口（`fieldModel`、解码与插值函数、store 动作与状态、读数卡列取 `lp_band_db / thr_dba`）；M16 的用例与基准；M21 的目录字段；M17 的 SDK 方法；
文档总编的 11 处差异。

## 7. 文件清单

- Python（新）：`python/awr/sim/perception/{quant,field}.py`、`python/awr/sim/targets/radar.py`。
- Python（改）：`python/awr/sim/perception/{plugin,runtime}.py`、`python/awr/sim/targets/{acoustic,effective,envview,los_cache,model,runtime,signature}.py`、
  `python/awr/api/rest/perception.py`。
- 前端（新）：`apps/web/src/engine/perception/{quant,field}.ts`；（改）`apps/web/src/engine/perception/index.ts`、`apps/web/src/stores/perception.ts`。
- 测试（新）：`tests/perception/{ref_quant,test_quant,test_field,test_volumes,test_field_wire,test_field_golden,test_field_budget}.py`、
  `tests/perception/schemas/{field_profile,field_volumes,target_quant}.schema.json`（契约草案的本地副本）、`tests/targets/{test_d21_fields,test_radar_body}.py`、
  `apps/web/tests/m19/{quant,field,store-field}.test.ts`、`apps/web/tests/m19/{quant_cases,field_cases}.json`（由 `test_field_golden.py` 生成）。
