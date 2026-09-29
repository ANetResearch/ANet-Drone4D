# mk/m08.mk（M08 仿真内核与飞行器适配）：sim 测试、SIH 回归、机群阶梯基准、车辆模型生成、numba 预热。
# 依据：M08 §9.1、§9.5、§10；AWR-18 §3.1（性能运行协议）、§7.6；AWR-16 §11.5；mk/common.mk 扩展点约定。
# make test 已全量运行 pytest -m "not perf"（含 tests/sim），下列 test-* 只是便捷目标，不追加到 TEST_TARGETS。

.PHONY: test-sim test-sim-perf regress-sih bench-ladder bench-ladder-smoke vehicles-models numba-warm

LADDER_N ?= 10,50,100,200,500,1000
LADDER_DUR ?= 60

test-sim: ## M08 功能测试（tests/sim，不含 perf 标记）
	@$(call with_lock_sh,$(PY) -m pytest $(ROOT)/tests/sim -m "not perf" $(PYTEST_ARGS))

test-sim-perf: ## M08 perf 标记用例（冷启动、批量 RTL 准入、内存与零分配），持排他性能锁
	@$(call with_lock_ex,$(PY) -m pytest $(ROOT)/tests/sim -m perf $(PYTEST_ARGS))

regress-sih: ## SIH 黄金数据回归 17 项 × 4 配置 + oracle + 反例，鲁棒 R4–R8（D1-AC-12）
	@$(call with_lock_sh,$(PY) -m pytest $(ROOT)/tests/sim/test_fleet_sih_parity.py $(ROOT)/tests/sim/test_fleet_robust.py $(PYTEST_ARGS))

bench-ladder: ## 机群阶梯（真实 supervisor + sim-core，性能运行协议；make bench-ladder LADDER_N=1000 LADDER_DUR=60）
	@$(call with_lock_ex,$(PY) $(ROOT)/tools/bench/fleet_ladder/run.py --n $(LADDER_N) --dur $(LADDER_DUR))

bench-ladder-smoke: ## 阶梯脚本功能自检（N = 4、8 s，不持锁，不作性能判定）
	$(PY) $(ROOT)/tools/bench/fleet_ladder/run.py --n 4 --dur 8 --warm 6 --smoke

vehicles-models: ## P600 高模（≤ 5k）与低模（≤ 300 / ≤ 150）glb，并复制到 apps/web/public/models/
	$(PY) $(ROOT)/tools/vehicles/stl2glb.py --copy-web
	$(PY) $(ROOT)/tools/vehicles/lowpoly.py --copy-web

numba-warm: ## numba 预热（编译并写入 NUMBA_CACHE_DIR；make run 前置，FR-005）
	$(PY) -c "import time; from awr.sim.fleet import kernels_l1 as K; t0 = time.perf_counter(); K.warmup(); print('numba warmup s', round(time.perf_counter() - t0, 2))"

RUN_PRE_TARGETS += numba-warm
