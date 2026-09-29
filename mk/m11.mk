# mk/m11.mk（M11，工作包 M11-R 运行时与 IPC、M11-api 网关）：运行时库与网关测试、IPC 与网关基准、早期数据源。
# 依据：M11 §9.6；AWR-18 §3.1（性能运行协议）、§7.6；AWR-03 D1-AC-08、D1-AC-10、D1-AC-35；mk/common.mk 扩展点约定。
# make test 已全量运行 pytest -m "not perf"（含 tests/runtime），下列 test-* 只是便捷目标，不追加到 TEST_TARGETS。
# 性能锁包装 with_lock_* 的参数必须是单条可执行命令（flock 直接 exec），因此一律使用绝对路径而不是 cd。

.PHONY: test-runtime test-runtime-perf test-fake-gw test-rt test-rt-chaos bench-ipc bench-state bench-cmd bench-gateway \
        bench-gateway-10 fake-gw synthetic-gw

BENCH_IPC_DIR := $(ROOT)/tools/bench/ipc
FAKE_N ?= 200
BENCH_SECS ?=

test-runtime: ## 运行时库单元与集成测试（StateRing、Bus、事件、supervisor、checkpoint、配置、CLI、fake_gw）
	@$(call with_lock_sh,$(PY) -m pytest $(ROOT)/tests/runtime -m "not perf" $(PYTEST_ARGS))

test-runtime-perf: ## 运行时库的 perf 标记用例（query RTT、checkpoint save p99），持排他性能锁
	@$(call with_lock_ex,$(PY) -m pytest $(ROOT)/tests/runtime -m perf $(PYTEST_ARGS))

test-fake-gw: ## fake_gw 合成 N∈{1,200,1000} 与 .awrrt 回放对拍 golden（D1-AC-35 的 Python 部分）
	@$(call with_lock_sh,$(PY) -m pytest $(ROOT)/tests/runtime/test_fake_gw.py $(PYTEST_ARGS))

bench-state: ## StateRing 与读环基准（M11-AC-002；D1-AC-08 读环部分），3 次取中位，写 runs/perf/bench_state-*/
	@$(call with_lock_ex,$(PY) $(BENCH_IPC_DIR)/bench_state.py --n 1000 $(if $(BENCH_SECS),--secs $(BENCH_SECS),))

bench-cmd: ## 命令 RTT 与事件可靠性基准（D1-AC-10；M11-AC-004、026），含丢弃注入，写 runs/perf/bench_cmd-*/
	@$(call with_lock_ex,$(PY) $(BENCH_IPC_DIR)/bench_cmd.py --events 570 --cps 50 --drop-every 200 $(if $(BENCH_SECS),--secs $(BENCH_SECS),))

test-rt: ## 网关与运行时功能测试（M11 §9.6：tests/runtime、tests/rt 不含 perf、chaos 标记 + vitest apps/web/tests/m11 与 net）
	@$(call with_lock_sh,$(PY) -m pytest $(ROOT)/tests/runtime $(ROOT)/tests/rt -m "not perf and not chaos" $(PYTEST_ARGS))
	@if [ -d "$(WEB)/tests/m11" ] || [ -d "$(WEB)/tests/net" ]; then cd $(WEB) && $(NPX) vitest run --project unit \
	  $$(for d in tests/m11 tests/net; do [ -d "$$d" ] && printf '%s ' "$$d"; done) --passWithNoTests $(VITEST_ARGS); fi

test-rt-chaos: ## 网关混沌用例（kill -9 api 后 ≤ 3 s 重连与 duplicate，D1-AC-11a），持排他性能锁
	@$(call with_lock_ex,$(PY) -m pytest $(ROOT)/tests/rt -m chaos $(PYTEST_ARGS))

bench-gateway: ## 网关容量（3 客户端，N = 1000，合成生产者）：api ≤ 0.35 核、tick 年龄 p99 ≤ 15 ms、on_tick p99 ≤ 1.5 ms
	@$(call with_lock_ex,$(PY) $(BENCH_IPC_DIR)/bench_gateway.py --n 1000 --clients 3 $(if $(BENCH_SECS),--secs $(BENCH_SECS),))

bench-gateway-10: ## 10 个轻量客户端（NFR-007，M11-AC-020）：api ≤ 0.6 核、swarm ≥ 9.5 Hz
	@$(call with_lock_ex,$(PY) $(BENCH_IPC_DIR)/bench_gateway.py --n 1000 --clients 10 --profile light $(if $(BENCH_SECS),--secs $(BENCH_SECS),))

bench-ipc: ## IPC 基准全集（性能运行协议：排他锁、开跑前负载、3 次中位数）
	@$(MAKE) --no-print-directory bench-state
	@$(MAKE) --no-print-directory bench-cmd
	@$(MAKE) --no-print-directory bench-gateway
	@if [ -f "$(BENCH_IPC_DIR)/bench_encode.py" ]; then $(call with_lock_ex,$(PY) $(BENCH_IPC_DIR)/bench_encode.py); \
	 else echo "提示：bench_encode.py 由网关工作包提供，尚未交付，跳过"; fi

synthetic-gw: ## 真实 Gateway 协议栈 + 合成数据（make synthetic-gw FAKE_N=1000 FAKE_PORT=8097）
	cd $(ROOT) && $(PY) -m awr.api.rt.sources.synthetic --n $(FAKE_N) $(if $(FAKE_PORT),--port $(FAKE_PORT),)

fake-gw: ## 启动早期数据源 fake_gw（make fake-gw FAKE_N=1000；回放：make fake-gw FAKE_REPLAY=packages/contracts/fixtures/rt/swarm_n200.awrrt）
	cd $(ROOT) && $(PY) tools/fake/fake_gw.py --n $(FAKE_N) $(if $(FAKE_REPLAY),--replay $(FAKE_REPLAY) --loop,) $(if $(FAKE_PORT),--port $(FAKE_PORT),)
