# mk/m05.mk（M05）：Web 点云引擎的 flight60 基线生成与便捷目标（M05 §9.1、M05-FR-055；AWR-18 §8.6）。
# make worlds 之后自动生成缺失或失效的 flight60（WORLDS_TARGETS += flight60，排在 M03 的 worlds-build 之后）。
# pytest tests/pointcloud 与 Vitest apps/web/tests/pointcloud 已由 make test 全量运行，这里只提供便捷目标。
#   make flight60          为六城生成缺失或失效的 apps/web/public/bench/flight60/<world>.{bin,json}
#   make flight60-force    无条件重生六城 flight60
#   make flight60-check    只校验（coordinate_sha256、bin_sha256、contentVersion），失效时退出 1
#   make test-m05          只运行 M05 的 pytest 与 Vitest（unit + browser）
#   make smoke-m05         Playwright 功能冒烟 perf/m05（测试构建，不持性能锁，不判定性能阈值）
#   make perf-m05          Playwright perf/m05 性能口径（排他性能锁，只在验收阶段运行）

FLIGHT60_GEN := $(PY) $(ROOT)/tools/bench/flight60/gen.py

WORLDS_TARGETS += flight60

.PHONY: flight60 flight60-force flight60-check test-m05 smoke-m05 perf-m05

flight60: ## 生成缺失或失效的 flight60（AWR-18 §8.6）
	@cd $(ROOT) && $(FLIGHT60_GEN) --missing

flight60-force: ## 无条件重生六城 flight60
	@cd $(ROOT) && $(FLIGHT60_GEN)

flight60-check: ## 校验 flight60 生成物与世界一致
	@cd $(ROOT) && $(FLIGHT60_GEN) --check

test-m05: ## M05 的 pytest 与 Vitest（make test 已包含，便捷目标）
	@cd $(ROOT) && $(PY) -m pytest -q tests/pointcloud -m "not perf" $(PYTEST_ARGS)
	@cd $(WEB) && $(NPX) vitest run --project unit --project browser tests/pointcloud $(VITEST_ARGS)

smoke-m05: ## perf/m05 功能冒烟（需要测试构建：VITE_AWR_TEST_SWITCHES=1 make build）
	@cd $(WEB) && M05_SMOKE=1 $(NPX) playwright test perf/m05 --project perf

perf-m05: ## perf/m05 性能口径（排他性能锁）
	@$(call with_lock_ex,cd $(WEB) && $(NPX) playwright test perf/m05 --project perf)
