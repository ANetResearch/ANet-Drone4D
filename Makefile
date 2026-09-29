# AWR 根 Makefile（M00）：只含总目标 setup | worlds | run | dev | build | test | perf | chaos | lint | ci，
# 以及 include mk/*.mk（AWR-03 §4.1、ADR-050；AWR-19 §7.2）。模块目标与扩展点变量见 mk/common.mk 文件头。

include mk/common.mk
include $(filter-out mk/common.mk,$(sort $(wildcard mk/*.mk)))

.DEFAULT_GOAL := help
.PHONY: help setup worlds run dev build test perf chaos lint ci \
        _setup-python _setup-node _setup-hooks _test _test-py _test-web _lint _lint-py _lint-web \
        _typecheck _build-web _build _ci _run-pre _need-installed _need-supervisor

help: ## 列出目标
	@grep -hE '^[a-zA-Z0-9_.-]+:.*## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/' | sort | awk -F'\t' '{printf "  %-22s %s\n", $$1, $$2}'

# ---- setup：幂等安装锁定依赖（AWR-19 OPS-FR-006、§7.3；AWR-11 TECH-FR-001） ----------------------
setup: ## 安装锁定依赖与生成物（uv 自举、uv pip sync、pip install -e、npm ci、生成物校验）
	@[ "$$(uname -m)" = "x86_64" ] || $(call fail,$(EXIT_ARCH),只支持 x86-64 Linux（StateRing 依赖 TSO，AWR-19 OPS-NFR-011）,更换 x86-64 主机)
	@$(MAKE) --no-print-directory _setup-python _setup-node
	@$(MAKE) --no-print-directory _setup-hooks
	@echo "make setup 完成"

_setup-python:
	@[ -x "$(PY)" ] || { echo "创建 .venv（$(PYTHON_BOOT)）"; $(PYTHON_BOOT) -m venv "$(VENV)"; } || \
	  $(call fail,$(EXIT_DEP_MISSING),无法用 $(PYTHON_BOOT) 创建 .venv,sudo apt install python3.12-venv)
	@[ "$$($(UV) --version 2>/dev/null | cut -d' ' -f2)" = "$(UV_VERSION)" ] || \
	  { echo "自举 uv $(UV_VERSION)"; $(PIP) install --quiet uv==$(UV_VERSION); } || \
	  $(call fail,$(EXIT_DEP_MISSING),uv $(UV_VERSION) 自举失败,$(PIP) install uv==$(UV_VERSION))
	cd $(ROOT) && $(UV) pip sync requirements.lock --python $(PY) $(if $(OFFLINE),--offline,)
	cd $(ROOT) && $(PIP) install --quiet -e . --no-deps $(if $(OFFLINE),--no-index --no-build-isolation,)

# npm ci 每次都会删除 node_modules；锁文件未变化且上次安装成功时跳过（FORCE=1 强制重装）
NPM_STAMP := $(ROOT)/node_modules/.awr-npm-ci.sha256
_setup-node:
	@v=$$(node --version 2>/dev/null | sed 's/^v//'); case "$$v" in 22.1[2-9].*|22.[2-9][0-9].*) ;; \
	  *) $(call fail,$(EXIT_DEP_MISSING),需要 Node 22.12.x（.nvmrc = $(NODE_VERSION)），当前为 $${v:-无},nvm install $(NODE_VERSION) 或把 Node 22.12 加入 PATH);; esac
	@cd $(ROOT) && want=$$(sha256sum package-lock.json | cut -d' ' -f1); \
	if [ -z "$(FORCE)" ] && [ -f "$(NPM_STAMP)" ] && [ "$$(cat $(NPM_STAMP))" = "$$want" ]; then \
	  echo "node_modules 与 package-lock.json 一致，跳过 npm ci（FORCE=1 强制）"; \
	else \
	  $(NPM) ci --no-audit --no-fund $(if $(OFFLINE),--offline,) && echo "$$want" > "$(NPM_STAMP)"; \
	fi
	@[ -x "$(PW_CHROME)" ] || echo "警告：未找到本机 Chrome 151（PW_CHROME=$(PW_CHROME)）；浏览器测试与性能用例将无法运行" >&2

_setup-hooks: $(SETUP_TARGETS)
	@if $(PY) -c "import awr.runtime.cli" 2>/dev/null; then $(VENV)/bin/awr doctor --quick; \
	 else echo "提示：awr.runtime.cli 尚未交付（M11），跳过 awr doctor --quick"; fi

_need-installed:
	@[ -x "$(PY)" ] && [ -d "$(ROOT)/node_modules" ] || $(call fail,$(EXIT_DEP_MISSING),.venv 或 node_modules 不存在,make setup)

# ---- worlds：构建缺失或无效的世界（实现由 mk/m03.mk 以 WORLDS_TARGETS 提供，AWR-19 §8.3） --------------
worlds: _need-installed ## 构建缺失或无效的世界（worldpkg build --missing）
	@[ -n "$(strip $(WORLDS_TARGETS))" ] || $(call fail,$(EXIT_GENERIC),WORLDS_TARGETS 为空：mk/m03.mk 尚未登记世界构建实现,等待 M03 交付 mk/m03.mk)
	@$(call with_lock_sh,$(MAKE) --no-print-directory $(WORLDS_TARGETS))

# ---- build：前端生产构建（AWR-19 §7.2） ------------------------------------------------------------
build: _need-installed ## 前端生产构建（npm run build -w apps/web）
	@$(call with_lock_sh,$(MAKE) --no-print-directory _build)

_build: _build-web $(BUILD_TARGETS)

_build-web:
	@[ -f "$(WEB)/index.html" ] || $(call fail,$(EXIT_GENERIC),apps/web/index.html 不存在（M15 尚未交付前端入口）,等待 M15 交付 apps/web 骨架)
	cd $(ROOT) && $(NPM) run build -w apps/web

# ---- run / dev：预检并启动 supervisor（AWR-19 §4.3） --------------------------------------------------
run: _need-installed ## 预检并启动（profile = demo）：worlds、build、supervisor
	@$(MAKE) --no-print-directory _run-pre
	@[ -z "$(strip $(WORLDS_TARGETS))" ] || $(MAKE) --no-print-directory worlds
	@[ -f "$(WEB)/dist/index.html" ] && [ -z "$$(find $(WEB)/src $(WEB)/index.html -newer $(WEB)/dist/index.html -print -quit 2>/dev/null)" ] || \
	  $(MAKE) --no-print-directory build
	cd $(ROOT) && AWR_PROFILE=$${AWR_PROFILE:-demo} exec $(PY) -m awr.runtime.supervisor

dev: _need-installed ## 开发启动（profile = dev，含 vite 5173；不做生产构建）
	@$(MAKE) --no-print-directory _run-pre
	@[ -z "$(strip $(WORLDS_TARGETS))" ] || $(MAKE) --no-print-directory worlds
	cd $(ROOT) && AWR_PROFILE=$${AWR_PROFILE:-dev} exec $(PY) -m awr.runtime.supervisor

_run-pre: _need-supervisor $(RUN_PRE_TARGETS)

_need-supervisor:
	@$(PY) -c "import awr.runtime.supervisor" 2>/dev/null || \
	  $(call fail,$(EXIT_DEP_MISSING),awr.runtime.supervisor 不可导入（M11 尚未交付进程监管）,等待 M11 交付 python/awr/runtime/supervisor.py)
	@if $(PY) -c "import awr.runtime.cli" 2>/dev/null; then $(VENV)/bin/awr doctor --quick; fi

# ---- lint：全部 lint 规则（AWR-18 §13；AWR-03 D1-AC-20） ------------------------------------------
lint: _need-installed ## ruff（PY-RUFF-01）、oxlint（TS-BND-01、TS-TYPE-01）与 LINT_TARGETS
	@$(MAKE) --no-print-directory -k _lint

_lint: _lint-py _lint-web $(LINT_TARGETS)

_lint-py:
	cd $(ROOT) && $(PY) -m ruff check python tools tests

_lint-web:
	cd $(WEB) && $(NPX) oxlint --type-aware

# ---- test：G1 测试（pytest -m "not perf"；Vitest unit 与 browser；AWR-18 §8、§12.1） --------------
test: _need-installed ## pytest（不含 perf 标记）、Vitest unit 与 browser、TEST_TARGETS
	@$(call with_lock_sh,$(MAKE) --no-print-directory _test)

_test: _test-py _test-web $(TEST_TARGETS)

_test-py:
	cd $(ROOT) && $(PY) -m pytest -m "not perf" $(PYTEST_ARGS)

_test-web:
	cd $(WEB) && $(NPX) vitest run --project unit --project browser --passWithNoTests $(VITEST_ARGS)

_typecheck:
	cd $(WEB) && $(NPX) tsc -p tsconfig.json --noEmit

# ---- perf / chaos：持排他性能锁，实现由 M16 以 PERF_TARGETS、CHAOS_TARGETS 提供（AWR-18 §3、§12.2） ------
perf: _need-installed ## 性能用例（make perf CASE=<id>，M16 harness，排他性能锁）
	@[ -n "$(strip $(PERF_TARGETS))" ] || $(call fail,$(EXIT_GENERIC),PERF_TARGETS 为空：mk/m16.mk 尚未登记性能 harness,等待 M16 交付 mk/m16.mk)
	@$(call with_lock_ex,$(MAKE) --no-print-directory $(PERF_TARGETS))

chaos: _need-installed ## 混沌用例（D1-ext，M16，排他性能锁）
	@[ -n "$(strip $(CHAOS_TARGETS))" ] || $(call fail,$(EXIT_GENERIC),CHAOS_TARGETS 为空：mk/m16.mk 尚未登记混沌用例,等待 M16 交付 mk/m16.mk)
	@$(call with_lock_ex,$(MAKE) --no-print-directory $(CHAOS_TARGETS))

# ---- ci：本机合并门禁 G1（AWR-03 §4.5；AWR-18 §12.1） ---------------------------------------------
# tools/ci/run.sh 存在时由它承载（AWR-03 §4.5 第 3 条）；否则按 G1 顺序执行内置步骤
ci: _need-installed ## 本机 CI（lint、契约检查、tsc、vite build、pytest、Vitest、CI_TARGETS）
	@if [ -x "$(ROOT)/tools/ci/run.sh" ]; then $(call with_lock_sh,$(ROOT)/tools/ci/run.sh); \
	 else $(call with_lock_sh,$(MAKE) --no-print-directory _ci); fi

_ci:
	@$(MAKE) --no-print-directory _lint
	@$(MAKE) --no-print-directory $(CI_TARGETS) _typecheck
	@if [ -f "$(WEB)/index.html" ]; then $(MAKE) --no-print-directory _build; \
	 else echo "提示：apps/web/index.html 尚未交付（M15），跳过 vite build"; fi
	@$(MAKE) --no-print-directory _test
	@echo "make ci 通过"
