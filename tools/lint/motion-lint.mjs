#!/usr/bin/env node
// Motion token lint (AWR-15 §8; ADR-029; AWR-18 §13.1). Scope: apps/web/src/**.
//   MOT-01 duration or easing literals in TS, TSX, TSL and CSS (ms/s values in transition/animation, cubic-bezier(),
//          Tailwind duration-[..] / ease-[..] / delay-[..] / duration-150, animation option objects with numeric duration)
//   MOT-02 !important on transition declarations (it overrides Base UI's inline `transition: none` on the starting frame)
//   MOT-03 more than 2 resident infinite loops (animate-spin/pulse/ping/bounce, animation ... infinite, iterations: Infinity)
//   MOT-04 transition-all outside ui/components/ui/**
// Exempt from MOT-01: generated token files styles/motion/tokens.css, lib/tokens/*.gen.ts, ui/motion/tokens.ts and the
// project extension table styles/motion/ext-tokens.css (token definitions, AWR-15 §8.1).
// Usage: node tools/lint/motion-lint.mjs [files...]
import { Reporter, WEB_SRC, inShadcn, isCode, isMain, isStyle, lineCol, readText, run, stripComments, walk } from './_common.mjs'

const TOKEN_FILES = [/^apps\/web\/src\/styles\/motion\/(tokens|ext-tokens)\.css$/, /^apps\/web\/src\/lib\/tokens\/[^/]+\.gen\.ts$/, /^apps\/web\/src\/ui\/motion\/tokens\.ts$/]
const MAX_LOOPS = 2

const CSS_TIME = /(?:^|[;{\s])(?:transition(?:-duration|-delay)?|animation(?:-duration|-delay)?)\s*:[^;{}]*?(?<![\w.-])(\d*\.?\d+)(ms|s)\b/g
const BEZIER = /cubic-bezier\s*\(/g
const TW_ARB = /(?<![\w-])(?:[a-z0-9-]+:)*(duration|ease|delay)-\[[^\]]+\]/g
const TW_NUM = /(?<![\w-])(?:[a-z0-9-]+:)*(duration|delay)-(\d+)(?![\w-])/g
const JS_TIME_STR = /['"`][^'"`\n]*?(?<![\w.-])\d*\.?\d+m?s\b[^'"`\n]*?\b(?:ease|linear|cubic-bezier|steps)\b[^'"`\n]*['"`]|['"`](?:[^'"`\n]*\s)?(?:transition|animation)[^'"`\n]*?(?<![\w.-])\d*\.?\d+m?s\b[^'"`\n]*['"`]/g
const JS_OPT = /\b(duration|delay|easing)\s*:\s*(?:\d|['"`](?:ease|linear|cubic-bezier|steps))/g
const IMPORTANT = /(?:^|[;{\s])transition(?:-[a-z-]+)?\s*:[^;{}]*!important|(?<![\w-])!transition(?:-[\w-]+)?|(?<![\w-])transition(?:-[\w-]+)?!/g
const LOOP = /(?<![\w-])(?:[a-z0-9-]+:)*animate-(spin|pulse|ping|bounce)(?![\w-])|(?:^|[;{\s])animation(?:-iteration-count)?\s*:[^;{}]*\binfinite\b|\biterations\s*:\s*Infinity\b/g
const ALL = /(?<![\w-])(?:[a-z0-9-]+:)*transition-all(?![\w-])|(?:^|[;{\s])transition(?:-property)?\s*:\s*all\b/g

export function checkFile(f, text, R, loops) {
  if (!f.startsWith(WEB_SRC + '/') || !(isCode(f) || isStyle(f))) return
  const src = stripComments(text, { css: isStyle(f) })
  const at = (i) => lineCol(src, i)
  const tokenFile = TOKEN_FILES.some((re) => re.test(f))
  if (!tokenFile) {
    const regs = isStyle(f) ? [CSS_TIME, BEZIER, TW_ARB, TW_NUM] : [BEZIER, TW_ARB, TW_NUM, JS_TIME_STR, JS_OPT]
    for (const re of regs) {
      for (const m of src.matchAll(re)) {
        const [l, c] = at(m.index)
        R.add(f, l, c, 'MOT-01', `motion literal ${m[0].trim().slice(0, 60)}; use --duration-* and --ease-* tokens (AWR-15 §8.1)`)
      }
    }
  }
  for (const m of src.matchAll(IMPORTANT)) {
    const [l, c] = at(m.index)
    R.add(f, l, c, 'MOT-02', '!important on a transition overrides Base UI starting-style handling (AWR-15 §8.3)')
  }
  if (!inShadcn(f)) {
    for (const m of src.matchAll(ALL)) {
      const [l, c] = at(m.index)
      R.add(f, l, c, 'MOT-04', 'transition-all outside ui/components/ui; list the animated properties')
    }
  }
  for (const m of src.matchAll(LOOP)) {
    const [l, c] = at(m.index)
    loops.push({ f, l, c, what: m[0].trim().slice(0, 40) })
  }
}

export function checkLoops(loops, R) {
  const outside = loops.filter((x) => !inShadcn(x.f))
  if (outside.length > MAX_LOOPS) {
    for (const x of outside) R.add(x.f, x.l, x.c, 'MOT-03', `resident loop ${x.what}: ${outside.length} loops declared, budget ${MAX_LOOPS} (ADR-029)`)
  }
}

if (isMain(import.meta.url)) {
  await run('motion-lint', async () => {
    const explicit = process.argv.slice(2).filter((a) => !a.startsWith('--'))
    const files = explicit.length ? explicit : walk(WEB_SRC, (r) => isCode(r) || isStyle(r))
    const R = new Reporter('motion-lint')
    if (!files.length) R.note(`${WEB_SRC} has no source files yet`)
    const loops = []
    for (const f of files) {
      const t = readText(f)
      if (t !== null) checkFile(f, t, R, loops)
    }
    checkLoops(loops, R)
    R.finish({ files: files.length, loops: loops.length })
  })
}
