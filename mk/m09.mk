# mk/m09.mk（M09 安全与健康）：安全功能测试、故障注入 CI 场景、前端 store 单元测试。
# 依据：M09 §10.2、§10.3；AWR-18 §3.1（性能运行协议）；mk/common.mk 扩展点约定。
# make test 已全量运行 pytest -m "not perf"（含 tests/safety），下列 test-* 只是便捷目标，不追加到 TEST_TARGETS；
# 前端 store 用例位于 apps/web 之外（tests/safety/web），以 test-safety-web 追加到 make test。

.PHONY: test-safety test-safety-core ci-faults test-safety-web test-safety-perf

test-safety: ## M09 功能测试（tests/safety，不含 perf 标记；含 needs_data 的深圳 RTL 剖面）
	@$(call with_lock_sh,$(PY) -m pytest $(ROOT)/tests/safety -m "not perf" $(PYTEST_ARGS))

test-safety-core: ## M09 D1-core 场景库（不含故障注入 ext 与 needs_data）
	@$(call with_lock_sh,$(PY) -m pytest $(ROOT)/tests/safety -m "not perf and not ext and not needs_data" $(PYTEST_ARGS))

ci-faults: ## M09 D1-ext 故障注入场景（六类故障，只限 Mock 后端）
	@$(call with_lock_sh,$(PY) -m pytest $(ROOT)/tests/safety -m "ext and not perf" $(PYTEST_ARGS))

test-safety-web: ## stores/safety.ts 单元测试（Vitest，node 环境）
	@cd $(WEB) && $(NPX) vitest run --config ../../tests/safety/web/vitest.config.ts

test-safety-perf: ## M09 perf 标记用例（1000 次转移 ≤ 4 ms 等），持排他性能锁
	@$(call with_lock_ex,$(PY) -m pytest $(ROOT)/tests/safety -m perf $(PYTEST_ARGS))

TEST_TARGETS += test-safety-web
