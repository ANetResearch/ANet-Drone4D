# mk/hosted-ci.mk（SHOW-CI）：托管 CI（.github/workflows/ci.yml）与干净克隆验证共用的无数据门禁 G1h（AWR-18 §12.1、
# AWR-03 ADR-078）。不需要 GPU、Chrome for Testing、UrbanScene3D 原始数据或已构建的六城；make ci 仍是本机完整门禁 G1。
#   make ci-nodata            G1h 全套：lint → 契约 → tsc → pytest 无数据子集 → Vitest unit → 生产构建
#   make typecheck            tsc --noEmit（make ci 的 _typecheck）
#   make test-py-nodata       pytest -m "not perf and not slow and not needs_data"（缺数据的用例必须跳过而不是失败）；
#                             PYTEST_SHARD=1 只跑仿真类目录（PYTEST_SHARD1_DIRS），PYTEST_SHARD=2 跑其余全部（托管 CI 的两路并行）
#   make test-web-unit        Vitest unit 项目，以及 apps/web 之外的两个 node 环境配置（stores/safety、stores/agents）
#   make validate-demo-world  synthcity 的 worldpkg validate --deep 与 Ajv strict 校验（先 make demo-world）
#   make test-demo-world      只需要 synthcity 的 needs_data 用例（not slow）：已发布事实、剧本加载器规则；S0 ci ×5 端到端标 slow，
#                             含墙钟断言且冷 numba 缓存下超时，只在 make ci 中运行

.PHONY: ci-nodata typecheck test-py-nodata test-web-unit validate-demo-world test-demo-world

PYTEST_NODATA      := not perf and not slow and not needs_data
PYTEST_SHARD1_DIRS := sim mission planning swarm safety
ifeq ($(PYTEST_SHARD),1)
PYTEST_SHARD_ARGS := $(addprefix tests/,$(PYTEST_SHARD1_DIRS))
else ifeq ($(PYTEST_SHARD),2)
PYTEST_SHARD_ARGS := $(addprefix --ignore=tests/,$(PYTEST_SHARD1_DIRS))
else
PYTEST_SHARD_ARGS :=
endif

typecheck: _need-installed ## tsc --noEmit（apps/web；make ci 的 _typecheck）
	@$(MAKE) --no-print-directory _typecheck

test-py-nodata: _need-installed ## pytest 无数据子集（not perf、not slow、not needs_data；PYTEST_SHARD=1|2 分两路）
	@$(call with_lock_sh,cd $(ROOT) && $(PY) -m pytest -m "$(PYTEST_NODATA)" -q -rfE $(PYTEST_SHARD_ARGS) $(PYTEST_ARGS))

test-web-unit: _need-installed ## Vitest unit（node 环境，不需要浏览器）与 tests/safety/web、tests/agent/web
	@cd $(WEB) && $(NPX) vitest run --project unit $(VITEST_ARGS)
	@$(MAKE) --no-print-directory test-safety-web test-agent-web

validate-demo-world: _need-installed ## 合成演示城市 synthcity 的深度校验与 Ajv strict（先 make demo-world）
	@cd $(ROOT) && $(PY) -m awr.world.package.cli validate $(AWR_WORLDS_DIR)/synthcity --deep || \
	  $(call fail,1,synthcity 校验存在错误,make demo-world)
	@cd $(ROOT) && node tools/contracts/check-world.mjs $(AWR_WORLDS_DIR)/synthcity || \
	  $(call fail,1,synthcity 的 JSON 未通过 Ajv strict 校验,make demo-world)

test-demo-world: _need-installed ## 只需要 synthcity 的 needs_data 用例（已发布事实、剧本加载器规则；先 make demo-world）
	@[ -f "$(AWR_WORLDS_DIR)/synthcity/world.json" ] || $(call fail,4,worlds/synthcity 未生成,make demo-world)
	@$(call with_lock_sh,cd $(ROOT) && $(PY) -m pytest -m "needs_data and not perf and not slow" -k synthcity -q -rfE \
	  tests/world/test_synthcity.py tests/e2e/test_synthcity_showcase.py tests/e2e/test_scenarios_static.py $(PYTEST_ARGS))

ci-nodata: _need-installed ## 托管 CI 同款无数据门禁 G1h（lint、契约、tsc、pytest 无数据子集、Vitest unit、生产构建）
	@$(MAKE) --no-print-directory lint
	@$(MAKE) --no-print-directory test-contracts
	@$(MAKE) --no-print-directory typecheck
	@$(MAKE) --no-print-directory test-py-nodata
	@$(MAKE) --no-print-directory test-web-unit
	@$(MAKE) --no-print-directory build
	@echo "make ci-nodata 通过"
