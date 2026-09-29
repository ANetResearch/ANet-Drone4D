#!/usr/bin/env node
// VIS-L-01: hex colour literals outside the token files (AWR-15 §12; ADR-032; AWR-18 §13.2 item 3).
// VIS-L-02: CSS colour keywords in CSS and TSX under apps/web/src (transparent, currentColor, inherit allowed).
// Scope of VIS-L-01: code and style directories of the §13.2 include set; docs/** is not scanned.
// Exempt: apps/web/public/brand/*.svg (BRAND-01 checks them), apps/web/src/styles/theme.css,
// apps/web/src/styles/motion/tokens.css (generated from _root.css), apps/web/src/lib/tokens/scene.gen.ts.
// Usage: node tools/lint/no-hex.mjs [files...]
import { Reporter, inWebSrc, isBinaryPath, isCode, isMain, isStyle, lineCol, readText, run, scopeFiles, stripComments } from './_common.mjs'

const EXEMPT = [/^apps\/web\/public\/brand\/[^/]+\.svg$/, /^apps\/web\/src\/styles\/theme\.css$/, /^apps\/web\/src\/styles\/motion\/tokens\.css$/,
  /^apps\/web\/src\/lib\/tokens\/scene\.gen\.ts$/]
// words made only of a-f letters that appear after '#' in prose, anchors and issue references
const WORDS = new Set(['add', 'bad', 'bed', 'bee', 'cab', 'cad', 'dab', 'dad', 'deb', 'ebb', 'fab', 'fad', 'fed', 'fee', 'ace', 'abbe', 'babe', 'bade',
  'bead', 'beef', 'cafe', 'dace', 'dead', 'deaf', 'deed', 'face', 'fade', 'feed', 'decade', 'deface', 'efface', 'accede', 'facade', 'defaced'])
const HEX = /(?<![&\w])#([0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})(?![0-9a-zA-Z_-])/g
const NAMED = ['aliceblue', 'antiquewhite', 'aqua', 'aquamarine', 'azure', 'beige', 'bisque', 'black', 'blanchedalmond', 'blue', 'blueviolet', 'brown',
  'burlywood', 'cadetblue', 'chartreuse', 'chocolate', 'coral', 'cornflowerblue', 'cornsilk', 'crimson', 'cyan', 'darkblue', 'darkcyan', 'darkgoldenrod',
  'darkgray', 'darkgreen', 'darkgrey', 'darkkhaki', 'darkmagenta', 'darkolivegreen', 'darkorange', 'darkorchid', 'darkred', 'darksalmon', 'darkseagreen',
  'darkslateblue', 'darkslategray', 'darkslategrey', 'darkturquoise', 'darkviolet', 'deeppink', 'deepskyblue', 'dimgray', 'dimgrey', 'dodgerblue',
  'firebrick', 'floralwhite', 'forestgreen', 'fuchsia', 'gainsboro', 'ghostwhite', 'gold', 'goldenrod', 'gray', 'green', 'greenyellow', 'grey', 'honeydew',
  'hotpink', 'indianred', 'indigo', 'ivory', 'khaki', 'lavender', 'lavenderblush', 'lawngreen', 'lemonchiffon', 'lightblue', 'lightcoral', 'lightcyan',
  'lightgoldenrodyellow', 'lightgray', 'lightgreen', 'lightgrey', 'lightpink', 'lightsalmon', 'lightseagreen', 'lightskyblue', 'lightslategray',
  'lightslategrey', 'lightsteelblue', 'lightyellow', 'lime', 'limegreen', 'linen', 'magenta', 'maroon', 'mediumaquamarine', 'mediumblue', 'mediumorchid',
  'mediumpurple', 'mediumseagreen', 'mediumslateblue', 'mediumspringgreen', 'mediumturquoise', 'mediumvioletred', 'midnightblue', 'mintcream', 'mistyrose',
  'moccasin', 'navajowhite', 'navy', 'oldlace', 'olive', 'olivedrab', 'orange', 'orangered', 'orchid', 'palegoldenrod', 'palegreen', 'paleturquoise',
  'palevioletred', 'papayawhip', 'peachpuff', 'peru', 'pink', 'plum', 'powderblue', 'purple', 'rebeccapurple', 'red', 'rosybrown', 'royalblue',
  'saddlebrown', 'salmon', 'sandybrown', 'seagreen', 'seashell', 'sienna', 'silver', 'skyblue', 'slateblue', 'slategray', 'slategrey', 'snow',
  'springgreen', 'steelblue', 'tan', 'teal', 'thistle', 'tomato', 'turquoise', 'violet', 'wheat', 'white', 'whitesmoke', 'yellow', 'yellowgreen']
const NAMED_RE = NAMED.join('|')
// CSS: a colour-bearing property whose value contains a keyword
const CSS_PROP = new RegExp(`(?:^|[;{\\s])(?:color|background(?:-color)?|border(?:-(?:top|right|bottom|left|block|inline))?(?:-color)?|outline(?:-color)?|` +
  `fill|stroke|text-decoration(?:-color)?|caret-color|accent-color|column-rule(?:-color)?|box-shadow|text-shadow|stop-color|flood-color|lighting-color)` +
  `\\s*:[^;{}]*?\\b(${NAMED_RE})\\b`, 'gi')
// Tailwind colour utilities with a named palette colour (text-white, bg-black/50, border-red-500 ...)
const TW = new RegExp(`(?<![\\w-])(?:[a-z0-9-]+:)*(?:bg|text|border(?:-[trblxyse])?|fill|stroke|ring(?:-offset)?|outline|from|via|to|shadow|decoration|` +
  `divide|placeholder|caret|accent|inset-ring|inset-shadow)-(black|white|red|gray|slate|zinc|neutral|stone|orange|amber|yellow|lime|green|emerald|teal|` +
  `cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)(?:-\\d{2,3})?(?:\\/\\d{1,3})?(?![\\w-])`, 'g')
// JS style objects: color: 'red'
const JS_STYLE = new RegExp(`\\b(?:color|background|backgroundColor|borderColor|fill|stroke|outlineColor)\\s*:\\s*['"\`](${NAMED_RE})['"\`]`, 'gi')

/** Check one file (repository-relative path) and report into R. */
export function checkFile(f, text, R) {
  if (EXEMPT.some((re) => re.test(f))) return
  for (const m of text.matchAll(HEX)) {
    const v = m[1]
    if (/^[a-fA-F]+$/.test(v) && WORDS.has(v.toLowerCase())) continue
    if (/^[0-9]+$/.test(v) && v.length <= 4) continue // issue or item numbers such as #123
    const [line, col] = lineCol(text, m.index)
    R.add(f, line, col, 'VIS-L-01', `hex colour #${v} outside token files; use a CSS variable from theme.css`)
  }
  if (!inWebSrc(f) || !(isStyle(f) || isCode(f))) return
  const src = stripComments(text, { css: isStyle(f) })
  const regs = isStyle(f) ? [CSS_PROP, TW] : [TW, JS_STYLE]
  for (const re of regs) {
    for (const m of src.matchAll(re)) {
      const [line, col] = lineCol(src, m.index + m[0].lastIndexOf(m[1]))
      R.add(f, line, col, 'VIS-L-02', `colour keyword "${m[1]}"; use a semantic token (transparent and currentColor are allowed)`)
    }
  }
}

if (isMain(import.meta.url)) {
  await run('no-hex', async () => {
    const explicit = process.argv.slice(2).filter((a) => !a.startsWith('--'))
    const files = explicit.length ? explicit : scopeFiles({ docs: false, filter: (r) => !isBinaryPath(r) })
    const R = new Reporter('no-hex')
    let scanned = 0
    for (const f of files) {
      if (EXEMPT.some((re) => re.test(f))) continue
      const text = readText(f)
      if (text === null) continue
      scanned++
      checkFile(f, text, R)
    }
    R.finish({ files: scanned })
  })
}
