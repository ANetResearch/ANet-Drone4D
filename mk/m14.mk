# mk/m14.mk（M14 智能体运行时与 ANet 集成）：便捷测试目标。M14-to-M00 第 7 条，INT-1 代建（AWR-03 §4.3：各模块只新增
# mk/<module>.mk）。make test 已全量运行 pytest -m "not perf"（含 tests/agent）；stores/agents.ts 的单元测试位于 apps/web 之外
# （tests/agent/web，自带 vitest 配置），以 test-agent-web 追加到 make test（与 M09 tests/safety/web 同一做法）。

.PHONY: test-agent test-agent-web

test-agent: ## M14 功能测试（tests/agent，不含 perf）
	@$(call with_lock_sh,$(PY) -m pytest $(ROOT)/tests/agent -m "not perf" $(PYTEST_ARGS))

test-agent-web: ## stores/agents.ts 单元测试（Vitest，node 环境）
	@cd $(WEB) && $(NPX) vitest run --config ../../tests/agent/web/vitest.config.ts

TEST_TARGETS += test-agent-web
