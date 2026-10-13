# mk/docs-site.mk（D2.2-D1）：Drone4D 文档站（Sphinx + sphinx-rtd-theme + MyST Markdown，Read the Docs 目录布局）。
#   make docs-site            构建 en 与 zh_CN 到 docs-site/_build/html/{en,zh-cn}/latest，并写入根跳转页 index.html（-W：告警即失败）
#   make docs-site-linkcheck  两种语言的 sphinx linkcheck，结果在 docs-site/_build/linkcheck/<lang>/output.{txt,json}
#                             （对外链接超时只记录、不判失败：conf_common.linkcheck_report_timeouts_as_broken = False）
#   make docs-site-serve      以只读静态服务器在 127.0.0.1:$(DOCS_PORT) 预览 _build/html（Ctrl+C 结束）
#   make docs-site-clean      删除 docs-site/_build
# 依赖固定在 docs-site/requirements.txt，安装在独立虚拟环境 .venv-docs（不影响项目主依赖 requirements.lock）；
# 发布见 tools/deploy/docs/publish.sh（只准备待发布目录并打印 rsync 命令，不上传）。

DOCS_VENV     ?= $(ROOT)/.venv-docs
DOCS_PY       := $(DOCS_VENV)/bin/python
DOCS_SRC      := $(ROOT)/docs-site
DOCS_OUT      := $(DOCS_SRC)/_build
DOCS_VERSION  ?= latest
DOCS_PORT     ?= 8080
DOCS_OPTS     ?= -q
# <源目录>:<URL 语言段>
DOCS_LANGS    := en:en zh_CN:zh-cn
DOCS_REQ_STAMP := $(DOCS_VENV)/.requirements.sha256

.PHONY: docs-site docs-site-venv docs-site-linkcheck docs-site-serve docs-site-clean

docs-site-venv: ## 创建或更新文档构建环境 .venv-docs（按 docs-site/requirements.txt）
	@[ -x "$(DOCS_PY)" ] || { echo "创建 .venv-docs（$(PYTHON_BOOT)）"; $(PYTHON_BOOT) -m venv "$(DOCS_VENV)"; } || \
	  $(call fail,$(EXIT_DEP_MISSING),无法用 $(PYTHON_BOOT) 创建 .venv-docs,sudo apt install python3.12-venv)
	@want=$$(sha256sum "$(DOCS_SRC)/requirements.txt" | cut -d' ' -f1); \
	if [ -f "$(DOCS_REQ_STAMP)" ] && [ "$$(cat $(DOCS_REQ_STAMP))" = "$$want" ]; then :; else \
	  "$(DOCS_PY)" -m pip install --quiet --disable-pip-version-check -r "$(DOCS_SRC)/requirements.txt" && echo "$$want" > "$(DOCS_REQ_STAMP)"; \
	fi

docs-site: docs-site-venv ## 构建文档站两种语言（sphinx-build -W）与根跳转页
	@set -e; for pair in $(DOCS_LANGS); do \
	  src=$${pair%%:*}; slug=$${pair##*:}; \
	  echo "sphinx-build $$src -> _build/html/$$slug/$(DOCS_VERSION)"; \
	  "$(DOCS_PY)" -m sphinx -b html -W --keep-going $(DOCS_OPTS) -d "$(DOCS_OUT)/doctrees/$$slug" \
	    "$(DOCS_SRC)/$$src" "$(DOCS_OUT)/html/$$slug/$(DOCS_VERSION)"; \
	done
	@install -m 0644 "$(DOCS_SRC)/_shared/root/index.html" "$(DOCS_OUT)/html/index.html"
	@echo "文档站已构建：$(DOCS_OUT)/html（根页按浏览器语言跳转到 en 或 zh-cn）"

docs-site-linkcheck: docs-site-venv ## 文档站链接检查（对外链接超时只记录）
	@rc=0; for pair in $(DOCS_LANGS); do \
	  src=$${pair%%:*}; slug=$${pair##*:}; \
	  echo "sphinx linkcheck $$src -> _build/linkcheck/$$slug"; \
	  "$(DOCS_PY)" -m sphinx -b linkcheck -q -d "$(DOCS_OUT)/doctrees-linkcheck/$$slug" \
	    "$(DOCS_SRC)/$$src" "$(DOCS_OUT)/linkcheck/$$slug" || rc=1; \
	  grep -hE '\[(broken|timeout)\]' "$(DOCS_OUT)/linkcheck/$$slug/output.txt" || true; \
	done; exit $$rc

docs-site-serve: ## 在 127.0.0.1:$(DOCS_PORT) 预览已构建的文档站
	@[ -f "$(DOCS_OUT)/html/index.html" ] || $(call fail,$(EXIT_GENERIC),文档站尚未构建,make docs-site)
	@echo "http://127.0.0.1:$(DOCS_PORT)/  （Ctrl+C 结束）"
	@cd "$(DOCS_OUT)/html" && "$(DOCS_PY)" -m http.server --bind 127.0.0.1 $(DOCS_PORT)

docs-site-clean: ## 删除文档站构建产物 docs-site/_build
	rm -rf "$(DOCS_OUT)"
