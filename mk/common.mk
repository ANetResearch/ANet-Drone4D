# mk/common.mk（M00）：公共变量、.env.local 读入、全局性能锁包装与扩展点变量。
# 依据：AWR-03 §4.1、§4.3、§4.5；AWR-19 §6.3、§7.2–§7.5、§16.2；AWR-18 §3.1 PR-1、§12。
#
# 扩展方式（各模块只新增 mk/<module>.mk，不修改 Makefile 与本文件）：
#   SETUP_TARGETS  make setup 末尾执行的生成与校验步骤（契约生成物 --check、车辆模型等）
#   WORLDS_TARGETS make worlds 的实现（M03：worldpkg build --missing）
#   BUILD_TARGETS  make build 在 vite build 之后追加的步骤
#   RUN_PRE_TARGETS make run / make dev 在启动 supervisor 前追加的预检（numba 预热等）
#   LINT_TARGETS   make lint 追加的检查（tools/lint/* 各规则、模块自有检查）
#   TEST_TARGETS   make test 追加的步骤；make test 已全量运行 pytest -m "not perf" 与 Vitest unit、browser，
#                  模块若只是运行自己目录下的 pytest 或 Vitest 用例，无需再登记（避免重复执行）
#   CI_TARGETS     make ci 追加的门禁（契约检查、包体积记录等）
#   PERF_TARGETS   make perf 的实现（M16 harness），在排他性能锁内执行
#   CHAOS_TARGETS  make chaos 的实现（M16），在排他性能锁内执行
# 写法示例（mk/m09.mk）：
#   TEST_TARGETS += test-safety-extra
#   test-safety-extra: ; $(PY) tools/xxx.py --check

SHELL := /bin/bash
.SHELLFLAGS := -o pipefail -c

ROOT := $(patsubst %/,%,$(dir $(abspath $(firstword $(MAKEFILE_LIST)))))

# 本 worktree 的覆盖（AWR_PORT_OFFSET、AWR_WORLDS_DIR 等），读入并导出（AWR-19 §6.1）
-include $(ROOT)/.env.local
ENV_LOCAL_VARS := $(shell [ -f $(ROOT)/.env.local ] && sed -n 's/^[[:space:]]*\([A-Za-z_][A-Za-z0-9_]*\)[[:space:]]*=.*/\1/p' $(ROOT)/.env.local)
ifneq ($(strip $(ENV_LOCAL_VARS)),)
export $(ENV_LOCAL_VARS)
endif

# ---- 工具链 -----------------------------------------------------------------
VENV    ?= $(ROOT)/.venv
PY      := $(VENV)/bin/python
PIP     := $(VENV)/bin/pip
UV      := $(VENV)/bin/uv
UV_VERSION   := 0.12.19
PYTHON_BOOT  ?= python3.12
NODE_VERSION := $(shell cat $(ROOT)/.nvmrc 2>/dev/null)

# Node 22.12：优先使用本机 ~/.local/node/bin（本机安装位置），其次 PATH 中的 node
NODE_HOME ?= $(wildcard $(HOME)/.local/node/bin)
ifneq ($(NODE_HOME),)
export PATH := $(NODE_HOME):$(PATH)
endif
NPM ?= npm
NPX ?= npx
WEB := $(ROOT)/apps/web

# Playwright 与 Vitest browser 使用本机 Chrome for Testing 151（rev 1234），不在线下载（AWR-11 TECH-FR-006）
export PW_CHROME ?= $(HOME)/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome
# numba 缓存必须持久可写，不放在源码旁（AWR-19 §6.3）
export NUMBA_CACHE_DIR ?= $(HOME)/.cache/awr/numba

# 默认目录（AWR-19 §6.3）
export AWR_WORLDS_DIR ?= $(ROOT)/worlds
export AWR_DATA_DIR   ?= $(ROOT)/data/raw
export AWR_RUNS_DIR   ?= $(ROOT)/runs

# ---- 全局性能锁（AWR-18 PR-1、AWR-19 §7.5 第 5 条） ------------------------------
# 默认取主仓库的 runs/.perf.lock，使全部 worktree 互斥；不是 git 仓库时取本仓库 runs/.perf.lock
GIT_COMMON_DIR := $(shell git -C $(ROOT) rev-parse --path-format=absolute --git-common-dir 2>/dev/null)
ifneq ($(GIT_COMMON_DIR),)
AWR_PERF_LOCK ?= $(abspath $(GIT_COMMON_DIR)/..)/runs/.perf.lock
else
AWR_PERF_LOCK ?= $(ROOT)/runs/.perf.lock
endif
export AWR_PERF_LOCK
LOCK_WAIT_S ?= 1800

# 用法：$(call with_lock_sh,<命令>)、$(call with_lock_ex,<命令>)；<命令> 在 bash -c 中执行（可含 `cd x && ...`，不得含单引号；
# INT-1：此前 flock 直接 exec 命令，`cd $(ROOT) && ...` 形式的目标单独调用时以"flock: failed to execute cd"失败）
# 已持有锁的嵌套调用（例如 make ci 内的 make test）不重复加锁；持有方式经 AWR_PERF_LOCK_HELD 传给子进程，
# harness 等工具据此判断无需再次加锁。等锁超时：共享锁返回 11（BUSY），排他锁返回 14（PERF_ENV_NOT_READY）。
define with_lock_sh
if [ -n "$$AWR_PERF_LOCK_HELD" ]; then $(1); else \
  mkdir -p "$(dir $(AWR_PERF_LOCK))" && \
  AWR_PERF_LOCK_HELD=sh flock -s -w $(LOCK_WAIT_S) -E 11 "$(AWR_PERF_LOCK)" bash -c '$(1)' || { rc=$$?; \
  [ $$rc -eq 11 ] && echo "等待全局性能锁超时（$(AWR_PERF_LOCK)）；修复：等待性能用例结束后重试 make $@" >&2; exit $$rc; }; fi
endef
define with_lock_ex
if [ "$$AWR_PERF_LOCK_HELD" = "ex" ]; then $(1); else \
  mkdir -p "$(dir $(AWR_PERF_LOCK))" && \
  AWR_PERF_LOCK_HELD=ex flock -x -w $(LOCK_WAIT_S) -E 14 "$(AWR_PERF_LOCK)" bash -c '$(1)' || { rc=$$?; \
  [ $$rc -eq 14 ] && echo "等待全局性能锁超时（PERF-E001，$(AWR_PERF_LOCK)）；修复：等待构建与测试结束后重试 make $@" >&2; exit $$rc; }; fi
endef

# ---- 退出码（AWR-19 §16.2） -------------------------------------------------
EXIT_GENERIC       := 1
EXIT_DEP_MISSING   := 8
EXIT_ARCH          := 13

# 用法：$(call fail,<退出码>,<说明>,<修复命令>)；最后一行打印可直接复制执行的修复命令
define fail
{ echo "错误（退出码 $(1)）：$(2)" >&2; echo "修复：$(3)" >&2; exit $(1); }
endef

# ---- 扩展点变量 ---------------------------------------------------------------
SETUP_TARGETS   ?=
WORLDS_TARGETS  ?=
BUILD_TARGETS   ?=
RUN_PRE_TARGETS ?=
LINT_TARGETS    ?=
TEST_TARGETS    ?=
CI_TARGETS      ?=
PERF_TARGETS    ?=
CHAOS_TARGETS   ?=

# pytest 与 Vitest 的附加参数（例如 make test PYTEST_ARGS="-k statering -x"）
PYTEST_ARGS ?=
VITEST_ARGS ?=

# ---- 锁文件（AWR-19 §7.4；依赖增删由 M00 执行） ---------------------------------
PY_LOCK_EXTRAS := --extra geo --extra tools --extra test --extra dev --extra fetch
PY_LOCK_CMD = $(UV) pip compile pyproject.toml requirements.in $(PY_LOCK_EXTRAS) --generate-hashes \
  --python $(PY) --custom-compile-command "make lock" --quiet

.PHONY: lock lock-check
lock: ## 由 pyproject.toml 与 requirements.in 重新生成 requirements.lock（M00）
	cd $(ROOT) && $(PY_LOCK_CMD) -o requirements.lock

lock-check: ## 校验 requirements.lock 与 pyproject.toml、requirements.in 一致（不改写文件）
	@cd $(ROOT) && tmp=$$(mktemp) && cp requirements.lock $$tmp && \
	$(PY_LOCK_CMD) -o $$tmp && \
	if diff -q requirements.lock $$tmp >/dev/null; then echo "requirements.lock 一致"; rm -f $$tmp; \
	else diff requirements.lock $$tmp | head -40; rm -f $$tmp; $(call fail,$(EXIT_DEP_MISSING),requirements.lock 与依赖声明不一致,make lock); fi
