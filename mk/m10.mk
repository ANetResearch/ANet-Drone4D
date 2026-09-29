# mk/m10.mk（M10 任务规划与集群）：功能测试便捷目标、规划基准、numba 预热。
# 依据：M10 §9.1、§9.5、§10；AWR-18 §3.1（性能运行协议）；mk/common.mk 扩展点约定。
# make test 已全量运行 pytest -m "not perf"（含 tests/mission、tests/planning、tests/swarm）与 Vitest
# （含 apps/web/tests/mission），下列 test-* 只是便捷目标，不追加到 TEST_TARGETS（避免重复执行）。
#   make test-mission      M10 的 pytest（不含 perf）与 Vitest 用例
#   make test-mission-data 需要已构建城市的 slow 用例（S1 能量预检、城市样本 A*、生成器安全性）
#   make bench-plan        M10 perf 标记用例（规划时延、CAPT n = 50 等），持排他性能锁
#   make m10-numba-warm    跟踪核、TOPP-lite、2.5D A* 的 numba 预热（make run 前置，NFR-015）

.PHONY: test-mission test-mission-data bench-plan m10-numba-warm

M10_PY_TESTS := $(ROOT)/tests/mission $(ROOT)/tests/planning $(ROOT)/tests/swarm

test-mission: ## M10 功能测试（tests/mission、tests/planning、tests/swarm，不含 perf；Vitest apps/web/tests/mission）
	@$(call with_lock_sh,$(PY) -m pytest $(M10_PY_TESTS) -m "not perf" $(PYTEST_ARGS))
	@$(call with_lock_sh,cd $(WEB) && $(NPX) vitest run --project unit tests/mission $(VITEST_ARGS))

test-mission-data: ## M10 需要城市数据的 slow 用例（缺少世界时自动 skip）
	@$(call with_lock_sh,$(PY) -m pytest $(M10_PY_TESTS) -m "needs_data and not perf" $(PYTEST_ARGS))

bench-plan: ## M10 规划与集群 perf 标记用例（NFR-004 至 NFR-008），持排他性能锁
	@$(call with_lock_ex,$(PY) -m pytest $(M10_PY_TESTS) -m perf $(PYTEST_ARGS))

m10-numba-warm: ## M10 numba 预热（编译并写入 NUMBA_CACHE_DIR）
	$(PY) -c "import time; t0 = time.perf_counter(); from awr.sim.planning import kernels_track as K, smooth as S, astar25 as A; K.warmup(); S.warmup(); A.warmup(); print('m10 numba warmup s', round(time.perf_counter() - t0, 2))"

RUN_PRE_TARGETS += m10-numba-warm
