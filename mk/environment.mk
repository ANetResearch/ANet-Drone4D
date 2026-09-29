# mk/environment.mk（M07 环境引擎）：便捷目标。M07-to-M00 第 2 条，INT-1 代建。make test 已全量运行 pytest 与 Vitest
# （含 tests/environment、apps/web/tests/environment），下列目标不追加到 TEST_TARGETS。

.PHONY: test-env bench-env env-assets env-fixtures

test-env: ## M07 功能测试（pytest 不含 perf、slow + Vitest unit/browser tests/environment）
	@$(call with_lock_sh,$(PY) -m pytest $(ROOT)/tests/environment -m "not perf and not slow" $(PYTEST_ARGS))
	@cd $(WEB) && $(NPX) vitest run --project unit tests/environment $(VITEST_ARGS)
	@cd $(WEB) && $(NPX) vitest run --project browser tests/environment $(VITEST_ARGS)

bench-env: ## env stage 与查询性能用例（perf 标记），持排他性能锁
	@$(call with_lock_ex,$(PY) -m pytest $(ROOT)/tests/environment/test_stage_bench.py -m perf $(PYTEST_ARGS))

env-assets: ## 按 AWR_WORLD_SEED 生成共享环境资产（worlds/_shared/env/{turb,weather}）
	@cd $(ROOT) && $(PY) -c "import os; from pathlib import Path; from awr.environment.io.assets import ensure_turb_box, ensure_weather_map; d = Path(os.environ.get('AWR_WORLDS_DIR', 'worlds')) / '_shared'; s = int(os.environ.get('AWR_WORLD_SEED', '0') or 0); print(ensure_turb_box(d, s)); print(ensure_weather_map(d, s))"

env-fixtures: ## 重新生成 M07 的 TS 夹具（tests/environment/envfix.py）
	@cd $(ROOT) && $(PY) tests/environment/envfix.py
