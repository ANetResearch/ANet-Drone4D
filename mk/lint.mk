# mk/lint.mk（M00-B）：tools/lint/* 全部规则并入 make lint（AWR-18 §13；AWR-03 D1-AC-20）。
# ruff（PY-RUFF-01）与 oxlint（TS-BND-01、TS-TYPE-01）由根 Makefile 的 _lint-py、_lint-web 执行。
# 每个脚本输出 `<file>:<line>:<col> <规则编号> <说明>`，退出码 0 通过、1 有违规、2 工具错误，并写 runs/lint/<ts>-<tool>.json。
# 前端目录尚未交付时各脚本给出提示并通过（M15 交付 theme.css、registry.ts、brand.lock.json 后自动生效）。
# 另含 PY-CB-01（tools/lint/check_py_callbacks.py，总线回调只入队）与 PERF-01（tools/ci/check-perf-flags.mjs，性能用例禁用 C3 标志），
# 由 MS1/MS2 集成验证补齐（AWR-18 §13.1）；MMD-01（check-mermaid.mjs，只报告）需要 mermaid 11 依赖，尚未接入。

LINT_SCRIPTS := no-emoji no-hex lint-lf motion-lint check-icons no-raw-controls check-brand check-deps check-units

.PHONY: lint-tools lint-selftest lint-py-imports lint-py-callbacks lint-perf-flags lint-thresholds $(addprefix lint-,$(LINT_SCRIPTS))

LINT_TARGETS += lint-selftest $(addprefix lint-,$(LINT_SCRIPTS)) lint-py-imports lint-py-callbacks lint-perf-flags lint-thresholds

lint-tools: lint-selftest $(addprefix lint-,$(LINT_SCRIPTS)) lint-py-imports lint-py-callbacks lint-perf-flags lint-thresholds ## 只运行 tools/lint 全部规则（不含 ruff、oxlint）

lint-selftest:
	@cd $(ROOT) && node tools/lint/selftest.mjs

lint-lint-lf:
	@cd $(ROOT) && node tools/lint/lint-lf.mjs --palette

lint-no-emoji lint-no-hex lint-motion-lint lint-check-icons lint-no-raw-controls lint-check-brand lint-check-deps lint-check-units:
	@cd $(ROOT) && node tools/lint/$(patsubst lint-%,%,$@).mjs

lint-py-imports:
	@cd $(ROOT) && $(PY) tools/lint/check_py_imports.py

lint-py-callbacks:
	@cd $(ROOT) && $(PY) tools/lint/check_py_callbacks.py

lint-perf-flags:
	@cd $(ROOT) && node tools/ci/check-perf-flags.mjs

# 阈值表覆盖（AWR-18 §1.3 第 4 条，FX-GW 交付）：THR-01/02/03 为错误；只由断言判定、没有阈值条目的 P0 PERF-AC 以 THR-04
# 列出（--strict 时为错误，待 M16 补齐 thresholds.json 条目后改为 --strict）
lint-thresholds:
	@cd $(ROOT) && node tools/ci/check-thresholds.mjs
