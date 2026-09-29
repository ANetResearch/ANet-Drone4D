// dev/test build switch (M15 §1.5; AWR-18 PR-7): the Vite dev server or a production build made with
// VITE_AWR_TEST_SWITCHES=1. Constant folded at build time, so window.__ux, ?tier=, ?rb=, ?allowFallback= and ?motion=
// parsing are removed from plain production builds (M15-AC-002). Never use import.meta.env.MODE for this.
export const TEST_SWITCHES: boolean = import.meta.env.DEV || import.meta.env.VITE_AWR_TEST_SWITCHES === '1'
