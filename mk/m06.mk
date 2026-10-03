# mk/m06.mk (M06, AWR-03 §4.3: modules add their own mk/<module>.mk). make lint adds lint-m06: the coding rules of
# M06 §6.3 that are checked statically (material.onBeforeRender, info.render.frame, synchronous read-back, drei white
# list, shared-path restrictions, texture.internalFormat, onObjectUpdate on engine hot paths; M06-AC-005, AC-018).
#   make lint-m06     only the M06 rules
#   make test-m06     Vitest of tests/m06 and tests/perf (unit), tests/m06 browser cases (make test already runs them)
#   make scan-m06-bundle  production bundle scan for test-switch identifiers (M06-AC-010; needs a production build)
LINT_TARGETS += lint-m06

.PHONY: lint-m06 test-m06

lint-m06: ## M06 coding rules (handler constraints, sync read-back, drei white list)
	@cd $(ROOT) && node apps/web/tests/m06/lint/m06-lint.mjs

test-m06: ## M06 Vitest (unit + browser)
	cd $(WEB) && $(NPX) vitest run --project unit tests/m06 tests/perf && $(NPX) vitest run --project browser tests/m06

# make build (plain production build) ends with the scan, so make ci and the hosted CI (G1h) keep M06-AC-010; a test build
# (VITE_AWR_TEST_SWITCHES=1 make build) is not scanned (AWR-03 ADR-079)
BUILD_TARGETS += $(if $(filter 1,$(VITE_AWR_TEST_SWITCHES)),,scan-m06-bundle)

.PHONY: scan-m06-bundle
scan-m06-bundle: ## M06-AC-010: production bundle has no test-switch identifiers (DIST=apps/web/dist, a build without VITE_AWR_TEST_SWITCHES)
	@cd $(ROOT) && node apps/web/tests/m06/lint/prod-bundle-scan.mjs $(or $(DIST),apps/web/dist)
