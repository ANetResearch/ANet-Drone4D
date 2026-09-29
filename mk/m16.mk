# mk/m16.mk（M16）：演示、剧本、流畅性 harness、报告与混沌（M16 §7.1；AWR-18 §12.2；M00-to-M16）。
# 根 Makefile 的 perf、chaos 是总目标：本文件只登记 PERF_TARGETS += perf-harness、CHAOS_TARGETS += chaos-harness（排他锁由
# mk/common.mk 的 with_lock_ex 取得并导出 AWR_PERF_LOCK_HELD=ex，harness 据此不再加锁）。门禁集目标逐用例持锁（suite.mjs）。
#   make perf CASE=<id|glob> [CITY= SCENE= N= NET= SOURCE= RUNS=]   单个用例（node perf/harness/run.mjs）
#   make perf-nightly | perf-weekly | perf-milestone MS=<n> | perf-release   G2 日集、G2 周集、G3、G4（suite.mjs）
#   make perf-list [GATE=]       列出已登记用例        make perf-selftest   harness 自检（PERF-AC-001，无浏览器）
#   make perf-baseline-accept CASE= RUN=   接受基线（只接受 PASS）
#   make perf-report RUN=<runId> [PDF=1] [PLAIN=1]   重新生成 report.json、report.html 并做合规检查
#   make chaos-core / make chaos        混沌 core（D1-AC-11a）/ 全部（含 ext，D1-AC-11b）；只在 ci profile 的自起后端上注入
#   make demo [FORCE=1] / demo-check / demo-card / demo-rehearse [RECORD=1]   一键演示、检查清单、提示卡、自动彩排
#   make scenarios-check / scenarios-generate [CHECK=1] / scenarios-pin [CHECK=1] / scenarios-energy
#   make remote-smoke [HOST=user@host]   SSH 转发冒烟（D1-AC-33）      make first-use-time   首次可用时间（P1）
# make test 已运行 pytest（tests/e2e、tests/scenarios、tests/chaos 的非 perf 部分）与 Vitest（apps/web/tests/m16）；
# TEST_TARGETS 只追加 harness 自检与 /bench 汇总自检，LINT_TARGETS 追加用例注册表校验（PERF-E017）。

M16_RUN   := cd $(WEB) && node perf/harness/run.mjs
M16_SUITE := cd $(WEB) && node perf/harness/suite.mjs
M16_REP   := cd $(WEB) && node perf/report

PERF_TARGETS  += perf-harness
CHAOS_TARGETS += chaos-harness
TEST_TARGETS  += test-m16-harness
LINT_TARGETS  += lint-m16-registry

.PHONY: perf-harness chaos-harness chaos-core perf-nightly perf-weekly perf-milestone perf-release perf-list perf-selftest \
        perf-baseline-accept perf-report test-m16-harness lint-m16-registry demo demo-check demo-card demo-rehearse \
        scenarios-check scenarios-generate scenarios-pin scenarios-energy remote-smoke first-use-time test-m16

perf-harness:
	@[ -n "$(CASE)" ] || $(call fail,2,缺少 CASE（例如 make perf CASE=flight60.shenzhen.pc；make perf-list 列出全部）,make perf-list)
	@$(M16_RUN) --case '$(CASE)' $(if $(CITY),--city $(CITY)) $(if $(SCENE),--scene $(SCENE)) $(if $(N),--n $(N)) \
	  $(if $(NET),--net $(NET)) $(if $(SOURCE),--source $(SOURCE)) $(if $(RUNS),--runs $(RUNS)) $(if $(GATE),--gate $(GATE)) --locked

chaos-harness:
	@cd $(ROOT) && $(PY) -m pytest tests/chaos -m chaos -p no:cacheprovider -q $(PYTEST_ARGS)

chaos-core: _need-installed ## 混沌 core：kill -9 api 与 sim-core（D1-AC-11a；排他性能锁）
	@$(call with_lock_ex,cd $(ROOT) && $(PY) -m pytest tests/chaos -m "chaos and not ext" -p no:cacheprovider -q $(PYTEST_ARGS))

perf-nightly: _need-installed ## G2 日集（AWR-18 §12.1，逐用例持排他锁）
	@$(M16_SUITE) --gate G2d

perf-weekly: _need-installed ## G2 周集（每周日追加）
	@$(M16_SUITE) --gate G2w

perf-milestone: _need-installed ## G3 里程碑出口（MS=<1..6>）
	@[ -n "$(MS)" ] || $(call fail,2,缺少 MS,make perf-milestone MS=4)
	@$(M16_SUITE) --gate G3 --ms $(MS)

perf-release: _need-installed ## G4 发布门禁（D1-core P0 全部、P1 通过率与豁免）
	@$(M16_SUITE) --gate G4

perf-list: ## 列出已登记的性能与门禁用例（GATE=G2d 等过滤）
	@$(M16_RUN) --list $(if $(GATE),--gate $(GATE))

perf-selftest: ## harness 自检（PERF-AC-001；锁、负载、补跑、判定、注册表、豁免）
	@cd $(WEB) && node perf/harness/selftest.mjs

perf-baseline-accept: ## 接受基线：CASE=<id> RUN=<runId>（只接受 PASS；提交信息以 perf-baseline: 开头）
	@[ -n "$(CASE)" ] && [ -n "$(RUN)" ] || $(call fail,2,需要 CASE 与 RUN,make perf-baseline-accept CASE=flight60.shenzhen.pc RUN=p20260929-030000-abcdef1)
	@cd $(WEB) && node perf/harness/baseline.mjs accept --case $(CASE) --run $(RUN)

perf-report: ## 重新生成报告：RUN=<runId> [PDF=1] [PLAIN=1]
	@[ -n "$(RUN)" ] || $(call fail,2,缺少 RUN,make perf-report RUN=p20260929-030000-abcdef1)
	@$(M16_REP)/build-report.mjs --run $(RUN)
	@$(M16_REP)/render.mjs --run $(RUN) $(if $(PDF),--pdf) $(if $(PLAIN),--plain)
	@$(M16_REP)/check-report.mjs --run $(RUN)

test-m16-harness:
	@cd $(WEB) && node perf/harness/selftest.mjs >/dev/null && node perf/report/aggregate-bench.mjs --selftest

lint-m16-registry:
	@$(M16_RUN) --check --lenient

test-m16: ## M16 的 pytest 与 Vitest（make test 已包含，便捷目标）
	@cd $(ROOT) && $(PY) -m pytest -q tests/e2e tests/scenarios tests/chaos -m "not perf" $(PYTEST_ARGS)
	@cd $(WEB) && $(NPX) vitest run --project unit tests/m16 $(VITEST_ARGS)

# ---- 演示（M16-FR-004、FR-027） ----------------------------------------------------------------------------------
demo: _need-installed ## 一键演示：检查清单 -> 提示卡 -> make run（深圳 + S1，profile demo）
	@cd $(ROOT) && $(PY) -m awr.datasets.demo check $(if $(FORCE),--force)
	@cd $(ROOT) && $(PY) -m awr.datasets.demo card
	@AWR_PROFILE=demo AWR_WORLD=shenzhen AWR_SCENARIO=s1-shenzhen-facade AWR_SCENARIO_PROFILE=demo $(MAKE) --no-print-directory run

demo-check: _need-installed ## 演示前检查（JSON）
	@cd $(ROOT) && $(PY) -m awr.datasets.demo check --json $(if $(FORCE),--force)

demo-card: ## 打印 D0-D7 演示提示卡
	@cd $(ROOT) && $(PY) -m awr.datasets.demo card

demo-rehearse: _need-installed ## 自动彩排 D0-D5（RECORD=1 输出 1280x720 webm 录屏兜底）
	@$(if $(RECORD),AWR_DEMO_RECORD=1) $(M16_RUN) --case demo.rehearsal --gate local

# ---- 剧本（M16-FR-007、FR-024、FR-029） ----------------------------------------------------------------------------
scenarios-check: ## 剧本静态校验（G1 已包含）
	@cd $(ROOT) && $(PY) -m awr.datasets.scenarios check
	@cd $(ROOT) && $(PY) -m pytest -q tests/e2e/test_scenarios_static.py tests/scenarios -p no:cacheprovider $(PYTEST_ARGS)

scenarios-generate: ## 由 awr.datasets.scenarios.authoring 生成全部剧本、catalog 与 zones（CHECK=1 只比较）
	@cd $(ROOT) && $(PY) -m awr.datasets.scenarios generate $(if $(CHECK),--check)

scenarios-pin: ## 写入 world_coordinate_sha256（世界构建后；CHECK=1 只核对）
	@cd $(ROOT) && $(PY) -m awr.datasets.scenarios pin $(if $(CHECK),--check)

scenarios-energy: ## 离线能量复核表（编写期工具）
	@cd $(ROOT) && $(PY) -m awr.datasets.scenarios energy

# ---- 远程与首次可用（D1-AC-33；PRD-NFR-027） -----------------------------------------------------------------------
remote-smoke: ## SSH 转发下的 WS 与 Range 冒烟（HOST=user@host；缺省本机回环转发）
	@cd $(ROOT) && bash tests/e2e/remote_smoke.sh $(if $(BASE),$(BASE)) $(if $(HOST),--ssh $(HOST))

first-use-time: ## 首次可用时间：干净克隆 -> make setup -> make run -> 远程首屏（P1，约 30 min）
	@cd $(ROOT) && bash tests/e2e/first_use_time.sh
