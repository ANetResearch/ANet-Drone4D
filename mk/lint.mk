# mk/lint.mk（M00-B）：tools/lint/* 全部规则并入 make lint（AWR-18 §13；AWR-03 D1-AC-20）。
# ruff（PY-RUFF-01）与 oxlint（TS-BND-01、TS-TYPE-01）由根 Makefile 的 _lint-py、_lint-web 执行。
# 每个脚本输出 `<file>:<line>:<col> <规则编号> <说明>`，退出码 0 通过、1 有违规、2 工具错误，并写 runs/lint/<ts>-<tool>.json。
# 前端目录尚未交付时各脚本给出提示并通过（M15 交付 theme.css、registry.ts、brand.lock.json 后自动生效）。
# 另含 PY-CB-01（tools/lint/check_py_callbacks.py，总线回调只入队）与 PERF-01（tools/ci/check-perf-flags.mjs，性能用例禁用 C3 标志），
# 由 MS1/MS2 集成验证补齐（AWR-18 §13.1）；MMD-01（check-mermaid.mjs，只报告）需要 mermaid 11 依赖，尚未接入。
# REL-01/02/03（check-release.mjs，ADR-079）检查发布集合（git ls-files --cached --others --exclude-standard）：单文件 ≤ 5 MB、不含世界与点云数据、
# 不含凭据；不在 git 仓库内（或 git 不可用）时给出提示并通过，git 目录在工作树之外时设置 GIT_DIR 与 GIT_WORK_TREE。
# REL-04（D2.1，ADR-123）：本机有部署口令文件（~/.config/awr/admin.secret 或 AWR_ADMIN_SECRET_FILE）时其内容不得出现在发布集合中。
# REL-05..09（D3，docs/05 §14，ADR-154、ADR-205；D3-AC-24、45、46）：仓库无 FAST-LIVO2 源码与链接产物、gz 分叉文件保留原版权头、产品镜像配方
# 不含仅限研究的组件、NOTICE 与 THIRD_PARTY_NOTICES 署名齐全、copy-lint 通过；自测 tools/lint/selftest-release.mjs 随 lint-selftest 运行。
# VER-01/02（check-version.mjs，D2.1，ADR-118）：pyproject.toml、apps/web/package.json、根 package.json 与 awr.__version__ 的产品版本
# 相等且为 X.Y.Z。
# COPY-01..10（copy-lint.mjs，docs/05 §7.10，ADR-153；D3-AC-46、D3-AC-23、D3-AC-53）：企业级文案中英文规则集，缺省扫描 D2 发布面
# （README.md、README.zh-CN.md、zh-CN.json、en.json、CITATION.cff），tools/lint/copy-lint.scope.json 追加范围；P-1 基线例外登记在
# tools/lint/copy-lint.baseline.json，P0b 出口前清零；v1.6（L05）起 lint-copy-lint 与 lint-check-release（REL-09）均为严格模式，
# 仍有基线例外时失败。

LINT_SCRIPTS := no-emoji no-hex lint-lf motion-lint check-icons no-raw-controls check-brand check-deps check-units check-release check-version copy-lint

.PHONY: lint-tools lint-selftest lint-py-imports lint-py-callbacks lint-perf-flags lint-thresholds lint-copy-strict $(addprefix lint-,$(LINT_SCRIPTS))

LINT_TARGETS += lint-selftest $(addprefix lint-,$(LINT_SCRIPTS)) lint-py-imports lint-py-callbacks lint-perf-flags lint-thresholds

lint-tools: lint-selftest $(addprefix lint-,$(LINT_SCRIPTS)) lint-py-imports lint-py-callbacks lint-perf-flags lint-thresholds ## 只运行 tools/lint 全部规则（不含 ruff、oxlint）

lint-selftest:
	@cd $(ROOT) && node tools/lint/selftest.mjs && node tools/lint/selftest-release.mjs

lint-lint-lf:
	@cd $(ROOT) && node tools/lint/lint-lf.mjs --palette

lint-no-emoji lint-no-hex lint-motion-lint lint-check-icons lint-no-raw-controls lint-check-brand lint-check-deps lint-check-units lint-check-version:
	@cd $(ROOT) && node tools/lint/$(patsubst lint-%,%,$@).mjs

# P0b 出口（D3-AC-23，L05）起 make lint 与 REL 均以严格模式运行 copy-lint：扫描缺省发布面与 copy-lint.scope.json 追加的 D3 页面、
# /lab/ 公开构建、原因码文案与 OpenAPI 快照，基线例外须为 0（新增放行只能登记为经评审的 allow 条目）
lint-check-release:
	@cd $(ROOT) && node tools/lint/check-release.mjs --copy-strict

lint-copy-lint lint-copy-strict:
	@cd $(ROOT) && node tools/lint/copy-lint.mjs --strict

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
