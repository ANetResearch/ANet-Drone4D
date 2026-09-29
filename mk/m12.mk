# mk/m12.mk（M12 时间轴、录制与回放）：便捷目标与基准。M12-to-M00 第 7 条，INT-1 代建。make test 已全量运行
# pytest -m "not perf"（含 tests/recorder）与 Vitest unit（含 apps/web/tests/time），下列目标不追加到 TEST_TARGETS；
# 基准持排他性能锁（AWR-18 PR-1）。

.PHONY: test-m12 bench-rec bench-seek bench-replay m12-reindex m12-repair

test-m12: ## M12 功能测试（tests/recorder 不含 perf + Vitest tests/time）
	@$(call with_lock_sh,$(PY) -m pytest $(ROOT)/tests/recorder -m "not perf" $(PYTEST_ARGS))
	@cd $(WEB) && $(NPX) vitest run --project unit tests/time $(VITEST_ARGS)

bench-rec: ## recorder 写入基准（D1-AC-18：recorder ≤ 0.1 核、≤ 60 MB/min），持排他性能锁
	@$(call with_lock_ex,$(PY) $(ROOT)/tools/bench/rec/bench_write.py)

bench-seek: ## 回放 seek 基准（首个 backfill 帧 ≤ 500 ms），持排他性能锁
	@$(call with_lock_ex,$(PY) $(ROOT)/tools/bench/rec/bench_seek.py)

bench-replay: ## 20× 回放基准（api ≤ 0.35 核、tick 年龄 p99 ≤ 15 ms），持排他性能锁
	@$(call with_lock_ex,$(PY) $(ROOT)/tools/bench/rec/bench_replay.py)

m12-reindex: ## 重建录制段索引：make m12-reindex SEG=runs/<run>/rec-<k>.mcap
	@$(PY) -m awr.recorder.reindex $(SEG)

m12-repair: ## 修复未收尾的录制段：make m12-repair SEG=runs/<run>/rec-<k>.mcap
	@$(PY) -m awr.recorder.repair $(SEG)
