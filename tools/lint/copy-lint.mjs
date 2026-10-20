#!/usr/bin/env node
// Enterprise copy rules (docs/05 §7.10, R-D3-21, ADR-153; D3-AC-46, D3-AC-23, D3-AC-53). Two rule sets, Chinese and English,
// over the release-facing copy: by default the D2 release surface of D3-AC-46 (README.md, README.zh-CN.md, the application
// dictionaries zh-CN.json and en.json that also carry the landing and about pages, CITATION.cff); tools/lint/copy-lint.scope.json
// adds entries (D3 pages, the /lab/ public build, the dataset release page and the OpenAPI descriptions, D3-AC-23).
//   COPY-01 phrase table: colloquial, internet-slang and marketing phrases (Chinese and English), matched as phrases; the
//           whitelist keeps ordinary terms ("超时", "超过", "超出", "超限", "超配额", "<number> 秒", super-resolution, magic number)
//   COPY-02 exclamation marks, tildes and tone marks (Chinese modal particles at a clause end, "?!", "！", "～"); a tilde before a
//           number ("~5 GB") or between numbers ("1~3") is an approximation or a range, not a tone mark
//   COPY-03 ellipsis outside a menu item that opens a dialog (a short label ending in one ellipsis) or a truncated display
//           ("sha256:…"); "……", "。。。" and an ellipsis inside prose are reported
//   COPY-04 emoji (EMOJI-01) and forbidden glyph blocks (GLYPH-01), the same character classes as no-emoji.mjs
//   COPY-05 a performance or scale number without its conditions (hardware, resolution, sensor set, clock or physics tier,
//           measurement method) in the same sentence: throughput (fps, 帧/s, steps/s, GB/s), speed-ups ("3x faster", "提升 40%"),
//           fleet scale ("up to 1,000 drones", "1,000 vehicles", "1 000 架") and latency or duration claims ("首帧 ≤ 5 s",
//           "loads in 2 s"); timeouts, retry intervals and countdowns ("超时 30 秒", "5 秒后重试") are parameters, not claims
//   COPY-06 product and proper-noun spelling: "ANet Drone4D" at first mention in a document, then "Drone4D"; the repository
//           identifier ANet-Drone4D only in URLs and package names; official spellings (Gazebo Sim, Rerun, FAST-LIVO2, RotorPy,
//           Livox MID-360, PX4, LiDAR, UrbanScene3D, FleetSim, GitHub)
//   COPY-07 English sentence case for titles, buttons, menus and other short labels ("Export dataset", not "Export Dataset");
//           proper nouns, acronyms and units keep their spelling
//   COPY-08 superlatives and unverifiable comparisons ("首创", "唯一", "最强", "业界领先", first-ever, industry-leading,
//           state-of-the-art, best-in-class, world-class); comparisons with external work belong to the D3-AC-53 report
//   COPY-09 slogan-style taglines: "X in, Y out", and short headings closed by a period ("从真实世界，到无人机集群。")
//   COPY-10 delivery and term names (§7.10 term table): D1 "三维场景视图", D2 "交互沙盒", D3 "事件沙盘"; "沙盘" only in D3 copy
//           (keys under lab.*); the next major version is D4, never "Drone4D 2.0"; physics tiers, observation and run states
//           use the table terms ("快速仿真", "每机私有观测", not "极速模式", "上帝视角", "跑起来了")
//   COPY-BL-01 a baseline exception or allowance that no longer matches any finding (remove it from the register)
//   COPY-BL-02 a malformed register entry (missing file, rule, match or reason; an allowance also needs reviewed)
//   COPY-BL-03 with --strict (the P0b exit, D3-AC-23): baseline exceptions remain
// Exceptions live in tools/lint/copy-lint.baseline.json: "exceptions" is the P-1 baseline (each entry registered with its reason;
// cleared before the P0b exit, `--strict` fails while any remain), "allow" holds reviewed permanent allowances (reason and
// reviewer required). A Markdown line can carry `<!-- copy-lint-allow COPY-05 reason -->` (same line, or the line before).
// Usage: node tools/lint/copy-lint.mjs [--strict] [--no-baseline] [--write-baseline] [--rules COPY-05,COPY-08|sup]
//                                      [--lang zh|en] [--kind md|i18n|cff|openapi|html|text] [--text "..."] [files...]
//   explicit files replace the scope (language from the file name or the CJK share, kind from the extension); --text checks one
//   string (GitHub About, topics, release notes before they are posted); --write-baseline rewrites "exceptions" from the current
//   findings and keeps the reasons of entries that still match.
// Scope file (optional): { "files": [{ "path": "docs/datasets/", "match": "\\.md$", "lang": "zh|en|auto", "kind": "md" }], "replace_default": false };
//   entries are added to DEFAULT_SCOPE, a path ending in "/" is walked (files filtered by "match"), missing files are skipped.
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { ROOT, Reporter, isMain, lineCol, readText, run, walk } from './_common.mjs'
import { scanText as glyphScan } from './no-emoji.mjs'

export const BASELINE_FILE = 'tools/lint/copy-lint.baseline.json'
export const SCOPE_FILE = 'tools/lint/copy-lint.scope.json'
/** D3-AC-46 release surface (P-1); the landing and about pages read their texts from the two dictionaries (landing.*, about.*) */
export const DEFAULT_SCOPE = [
  { path: 'README.md', lang: 'en', kind: 'md' },
  { path: 'README.zh-CN.md', lang: 'zh', kind: 'md' },
  { path: 'apps/web/src/app/i18n/en.json', lang: 'en', kind: 'i18n' },
  { path: 'apps/web/src/app/i18n/zh-CN.json', lang: 'zh', kind: 'i18n' },
  { path: 'CITATION.cff', lang: 'en', kind: 'cff' },
]
export const RULES = ['COPY-01', 'COPY-02', 'COPY-03', 'COPY-04', 'COPY-05', 'COPY-06', 'COPY-07', 'COPY-08', 'COPY-09', 'COPY-10']
const RULE_ALIAS = { sup: ['COPY-08'], superlative: ['COPY-08'], numbers: ['COPY-05'], names: ['COPY-06', 'COPY-10'] }

const CJK = /[\u3400-\u9fff\uf900-\ufaff]/
const CJK_G = /[\u3400-\u9fff\uf900-\ufaff]/g
const NUM = String.raw`(?<![\w.])\d[\d,.]*(?:\s?(?:k|K|M|万|千|百万|million|thousand))?`

// ---- COPY-01 phrase table and whitelist -------------------------------------------------------------------------------------
// Chinese: phrase patterns, never single characters (RV-45). "超" and "秒" are reported only in the listed combinations.
const ZH_PHRASES = [
  /超(?:快|强|酷|赞|棒|好用|爽|牛|值|炫|神|给力|级(?!管理员))/g, /秒(?:出|杀|懂|开|变|回)/g, /巨(?:好|快|强|牛|方便)/g,
  /黑科技|炫酷|酷炫|搞定|神器|爆款|硬核|给力|牛[逼掰]|绝绝子|yyds|吊打|碾压|逆天|躺赢|拿捏|丝滑|炸裂|王炸|天花板|杀手级|震撼|惊艳/g,
  /颠覆性|革命性|划时代|极致|完美(?:支持|呈现|还原|适配|运行)?|无敌|小白|傻瓜式|一键搞定|分分钟|妥妥|贼(?:快|好|强)|666/g,
]
// English: the §7.10 list plus the same family; matched as words
const EN_PHRASES = [
  /\b(?:awesome|amazing|amazingly|blazing(?:ly)?(?:[- ]fast)?|lightning[- ]fast|revolutionary|game[- ]chang(?:ing|er)|cutting[- ]edge|seamless(?:ly)?|effortless(?:ly)?|insanely|incredibl[ey]|stunning|breathtaking|mind[- ]blowing|jaw[- ]dropping|supercharged?|unleash(?:es|ed)?|next[- ]gen(?:eration)?|hassle[- ]free|rock[- ]solid|bulletproof|silky[- ]smooth|buttery|dead[- ]simple|epic|crazy|wow|killer)\b/gi,
  /\b(?:super|Super|SUPER)\b(?!-[A-Z]{2})/g, /\bmagic(?:al(?:ly)?)?\b/gi, /\bultimate\b/gi,
]
// whitelist: a phrase finding (COPY-01, COPY-08) inside one of these spans is not reported
const ZH_WHITELIST = [
  /超(?:时|过|出|限|配额|分辨率|参数|声波?|链接|市)/g, /\d+(?:\.\d+)?\s*(?:[-~～至]\s*\d+(?:\.\d+)?\s*)?秒/g, /[一二三四五六七八九十两几数百千]+\s*秒/g,
  /唯一(?:标识|编号|键|约束|来源|真源|入口|性|值|ID|的(?:标识|键|编号))|[必须应需]唯一|不唯一|并非唯一|是否唯一/g,
  /最佳(?:实践|优先|匹配|估计|等级|值|参数|视点|拟合)|最好的情况|首个(?:里程碑|子里程碑|阶段|版本|迭代|事件|运行|窗口|关键帧|样例|分片|结果|请求|错误)/g,
]
const EN_WHITELIST = [
  /\bsuper[- ]?(?:resolution|sampling|set|user)s?\b/gi, /\bmagic (?:number|bytes?|string|header|value|cookie)s?\b/gi, /\bbest[- ]effort\b/gi,
]

// ---- COPY-08 superlatives (D3-AC-53) ----------------------------------------------------------------------------------------
const ZH_SUPERLATIVE = [
  /(?:国内|全球|世界|业界|行业)?(?:首创|首款)|第一款|(?:国内|全球|世界|业界|行业)首(?:个|款|次|家)?/g,
  /(?:国内|全球|世界|业内|业界|行业|目前|迄今|当前)唯一|唯一(?:一个|一款|一家|能够?|可以|支持|实现|提供|具备|的(?:平台|基准|系统|仿真|产品|方案|开源|工具|框架))/g, /最(?:强|快|好|佳|先进|领先|全面|完整|真实|精确|准确|高效|优秀)/g,
  /(?:业界|行业|全球|国际|国内|世界)(?:领先|一流|顶尖|顶级)|领先水平|遥遥领先|顶级|顶尖|独家|史上|前所未有|独一无二|无与伦比|首屈一指/g,
  /填补(?:了)?.{0,8}?空白/g,
]
const EN_SUPERLATIVE = [
  /\b(?:first[- ]ever|world'?s first|first of its kind|one[- ]of[- ]a[- ]kind|second to none)\b/gi,
  /\b(?:the|our|a)\s+first\s+(?:[\w-]+\s+){0,3}?(?:benchmark|platform|simulator|system|framework|tool|runtime|dataset|solution|product)s?\b/gi,
  /\b(?:industry|market|world)[- ]leading\b|\bthe leading\b|\bbest[- ]in[- ]class\b|\bworld[- ]class\b|\bstate[- ]of[- ]the[- ]art\b|\bSOTA\b/gi,
  /\b(?:the best|best[- ]ever|fastest|strongest|unmatched|unrivall?ed|unparalleled|unprecedented)\b/gi,
  /\bmost (?:advanced|powerful|realistic|comprehensive|complete|accurate|efficient|capable)\b/gi,
  /\bthe only (?:[\w-]+\s+){0,2}?(?:benchmark|platform|simulator|system|framework|tool|runtime|dataset|solution|product)s?\b/gi,
]

// ---- COPY-10 term table (§7.10) ----------------------------------------------------------------------------------------------
const TERMS = [
  [/数字沙盘/g, '"数字沙盘" names the D1 view; use 三维场景视图 (D1), 交互沙盒 (D2) or 事件沙盘 (D3)'],
  [/事件实验室/g, 'the D3 delivery is 事件沙盘 (Event Lab)'],
  [/极速模式/g, 'use 快速仿真 (physics tier)'], [/真实模式/g, 'use 高保真仿真（Gazebo Sim）'],
  [/以假乱真|电影级/g, 'use 照片级渲染（基于 GLB 网格）'], [/上帝视角|开天眼/g, 'use 每机私有观测 or 观测档 L1/L2'],
  [/跑起来了|挂了/g, 'use the run states 排队中、准备中、运行中、收尾中、评测中、已完成、失败、已取消'],
  [/无人机小游戏|仿真神器/g, 'use the product name ANet Drone4D'], [/飞快|秒出图/g, 'state the number with its conditions'],
  [/\b(?:ANet[ -])?Drone4D\s*2(?:\.0)?\b/gi, 'the next major version is D4 (research and concept design)'],
  [/\bgod(?:'s)?[- ]?(?:eye )?(?:view|mode)\b/gi, 'use per-drone private observation'],
]

// ---- COPY-06 spellings ---------------------------------------------------------------------------------------------------------
// [pattern, canonical forms]; a match that is not one of the canonical forms is reported. Lower-case forms adjacent to a path or
// identifier character (. / _ -) are package or module names and are skipped (drone4d.serve, drone4d-camera).
const SPELLINGS = [
  [/(?<![\w./@-])gazebo\s+sim(?![\w-])/gi, ['Gazebo Sim']],
  [/(?<![\w./@-])fast[-_ ]?livo2?(?![\w])/gi, ['FAST-LIVO2', 'FAST-LIVO']],
  [/(?<![\w./@-])rotorpy(?![\w/-]|\.\w)/gi, ['RotorPy']],
  [/(?<![\w./@-])mid[-_ ]?360(?![\w])/gi, ['MID-360']],
  [/(?<![\w./@-])px4(?![\w/-]|\.\w)/gi, ['PX4']],
  [/(?<![\w./@-])lidar(?![\w/-]|\.\w)/gi, ['LiDAR']],
  [/(?<![\w./@-])urbanscene\s?3d(?![\w/-]|\.\w)/gi, ['UrbanScene3D']],
  [/(?<![\w./@-])fleetsim(?![\w/-]|\.\w)/gi, ['FleetSim']],
  [/(?<![\w./@-])github(?![\w/-]|\.\w)/gi, ['GitHub']],
  [/(?<![\w./@-])rerun(?![\w/-]|\.\w)/gi, ['Rerun', 'rerun']],
  [/(?<![\w./@-])(?:anet|ANET|Anet)(?![\w-])/g, ['ANet']],
  [/(?<![\w./@-])drone\s?4d(?![\w/-]|\.\w)/gi, ['Drone4D']],
]
const REPO_ID = /(?<![\w./@-])ANet-Drone4D(?![\w/-]|\.\w)/g
const PRODUCT = /(?<![\w./@-])(?:ANet[ -])?Drone4D(?![\w/-]|\.\w)/g

// ---- COPY-07 sentence case -----------------------------------------------------------------------------------------------------
// multi-word proper nouns are masked before the word check; single words are matched case-sensitively
const PROPER_PHRASES = ['ANet Synthetic City', 'ANet Tower', 'Agent Network Research', 'Gazebo Sim', 'Gazebo Classic', 'Event Lab',
  'Interactive Sandbox', 'Scene View', 'World Package', 'World Runtime', 'Read the Docs', 'Mock ANet', 'ANet Open Source License',
  'Apache License', 'Python SDK', 'Livox MID-360', 'Web Viewer', 'Open API', 'ANet Graphite', 'Creative Commons', 'Open Data Commons',
  'OpenStreetMap', 'Johnson', 'Potree', 'Skyfall-GS', 'SuperSplat', 'ModelScope', 'Hugging Face', 'Docker Hub', 'Sandbox API']
const PROPER_WORDS = new Set(['ANet', 'Drone4D', 'Gazebo', 'Rerun', 'RotorPy', 'Livox', 'Python', 'Linux', 'Windows', 'Chrome', 'Firefox',
  'Safari', 'Edge', 'Apple', 'Intel', 'Mac', 'Docker', 'Kubernetes', 'Ubuntu', 'English', 'Chinese', 'Shenzhen', 'Hong', 'Kong', 'Mock',
  'Johnson', 'Potree', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday', 'January', 'February', 'March',
  'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December', 'I', 'Hz', 'Pa', 'Celsius', 'Kelvin',
  'Recast', 'Detour', 'Vite', 'React', 'Playwright', 'Vitest', 'Sphinx', 'Zenodo', 'ModelScope', 'ZeroTier', 'Gaussian', 'Mapbox',
  'Cesium', 'Kalman', 'Gauss', 'Euler', 'Bayer', 'Perlin', 'Dryden', 'Kaimal', 'Monte', 'Carlo', 'Unreal', 'Unity', 'Isaac', 'Jetson'])

// ---- COPY-05 numbers --------------------------------------------------------------------------------------------------------------
const PERF = [
  new RegExp(String.raw`${NUM}\s*(?:fps|FPS|帧\s*/\s*(?:s|秒)|帧每秒|steps?\s*/\s*s|步\s*/\s*(?:s|秒)|points?\s*/\s*s|点\s*/\s*(?:s|秒)|[KMGT]i?B\s*/\s*s|[KMG]bps|req(?:uests)?\s*/\s*s|次\s*/\s*(?:s|秒)|ops\s*/\s*s)(?![\w])`, 'g'),
  new RegExp(String.raw`${NUM}\s*(?:x|×|倍)\s*(?:faster|quicker|speed[- ]?up|real[- ]?time|realtime|加速|提速|更快|性能提升|实时)`, 'gi'),
  new RegExp(String.raw`(?:提升|降低|减少|加速|缩短)\s*${NUM}\s*%|${NUM}\s*%\s*(?:faster|less|lower|higher|more|reduction|improvement|speed[- ]?up|提升|降低|减少|加速)`, 'gi'),
  new RegExp(String.raw`(?:[Uu]p to|[Aa]s many as|最多|多达|高达|至多|上限)?\s*${NUM}\s*(?:\+\s*)?(?:drones?|vehicles?|agents?|UAVs?|aircraft|concurrent (?:sessions?|users?)|无人机|架(?:飞行器|无人机)?(?![构设势空子式])|个智能体|个并发会话|路并发)(?![\w])`, 'g'),
]
// a per-session or per-sandbox quota is a product limit, not a performance claim
const SCALE_LIMIT = /per (?:sandbox|session|visitor|user|account)|each (?:sandbox|session)|每个(?:沙盒|会话)|单个(?:沙盒|会话)|每位访客|quota|配额|容量上限/i
const DURATION = new RegExp(String.raw`(?:[≤<]=?\s*)?\d[\d.]*(?:\s*(?:to|-|–|~|至)\s*\d[\d.]*)?\s*(?:ms|毫秒|μs|s|sec(?:onds?)?|秒|min(?:utes?)?|分钟)(?![\w/])`, 'g')
const DURATION_CLAIM = /latency|first frame|takes?\b|loads? in|builds? in|starts? in|ready in|within|under \d|in about|延迟|时延|首帧|耗时|用时|仅需|只需|内完成|即可完成|加载完成|启动时间|响应时间|吞吐|throughput|帧率|frame rate/i
const DURATION_PARAM = /timeout|time-out|超时|重试|retry|retries|interval|间隔|每隔|倒计时|countdown|后自动|after \d|every \d|过期|expires?|有效期|renewable|续期|session|会话时长|保留/i
const CONDITION = [
  /\b(?:A40|A100|H100|H200|L40S?|L4|T4|V100|RTX\s?\d{3,4}\w*|GTX\s?\d{3,4}|GPUs?|CPUs?|vCPUs?|cores?|threads?|Xeon|EPYC|Ryzen|Core i\d|Apple M\d|M\d (?:Pro|Max|Ultra)|laptop|desktop|Tier [SBA])\b/i,
  /核|显卡|单卡|单张|笔记本|台式机|服务器|主机|档位|时钟档|物理档|快速档|锁步|lockstep|晴天档/,
  /\b\d{3,4}p\b|\b\d{3,4}\s*[x×]\s*\d{3,4}\b|分辨率|resolution/i,
  /\bmedian\b|中位|\bp(?:50|90|95|99)\b|\bpercentile|分位|\bmeasured\b|实测|\bbenchmarked\b|基准测试|基准报告|测量|\baverage\b|平均|\bmean\b|\d+\s*(?:runs\b|次运行|次取)|\bconditions?:|条件[：:]/i,
  /\bRGB(?:-?D)?\b|\bdepth\b|深度|语义|实例分割|\bIMU\b/i,
]

// ---- helpers ------------------------------------------------------------------------------------------------------------------
const blankRange = (s, a, b) => s.slice(0, a) + s.slice(a, b).replace(/[^\n]/g, ' ') + s.slice(b)
function blankAll(s, re, keep = null) {
  return s.replace(re, (...args) => {
    const m = args[0]
    if (!keep) return m.replace(/[^\n]/g, ' ')
    return keep(m, ...args.slice(1))
  })
}
const isLabelText = (text, lang) => {
  const t = text.trim()
  if (!t) return false
  if (lang === 'zh' || CJK.test(t)) return [...t].length <= 16 && !/[。！？；]/.test(t.replace(/[。.]$/, ''))
  const words = t.split(/\s+/).length
  return words <= 8 && !/[.?!;](?:\s|$)/.test(t.replace(/(?:…|\.\.\.)$/, ''))
}
export function detectLang(text) {
  const cjk = (text.match(CJK_G) ?? []).length
  const letters = (text.match(/[A-Za-z]/g) ?? []).length
  return cjk > 0 && cjk * 3 >= letters * 0.5 ? 'zh' : 'en'
}
const overlaps = (spans, a, b) => spans.some(([x, y]) => a < y && b > x)
const spansOf = (text, res) => {
  const out = []
  for (const re of res) for (const m of text.matchAll(re)) out.push([m.index, m.index + m[0].length])
  return out
}
// sentences: split on Chinese and English sentence-final punctuation and table cell bars (a semicolon joins clauses of one
// sentence, so a condition after it still counts); a period followed by a digit is a decimal point
function sentences(text) {
  const out = []
  let start = 0
  const re = /[。！？!?|\n]|\.(?=\s|$)/g
  for (const m of text.matchAll(re)) {
    if (m[0] === '\n' && !/\n\s*\n/.test(text.slice(m.index, m.index + 3)) && !/^\s*(?:[-*+]|\d+\.)\s/.test(text.slice(m.index + 1, m.index + 8))) continue
    out.push([start, m.index + 1])
    start = m.index + 1
  }
  if (start < text.length) out.push([start, text.length])
  return out
}

// ---- segment extraction ----------------------------------------------------------------------------------------------------------
// A segment is one unit of visible copy: { text, lang, label, heading, anchor, where(i) -> [line, col], line0 }.
// Markdown keeps the file's offsets (invisible parts blanked), so where(i) is lineCol on the blanked text.

/** Markdown and plain text: blank code, URLs, link targets, HTML markup (keeping alt, title and aria-label values) and comments. */
export function mdVisible(raw) {
  let s = raw.replace(/\r/g, ' ')
  // fenced code blocks
  s = blankAll(s, /^[ \t]*(```|~~~)[^\n]*\n[\s\S]*?(?:^[ \t]*\1[^\n]*$|(?![\s\S]))/gm)
  s = blankAll(s, /<!--[\s\S]*?-->/g)
  s = blankAll(s, /<![A-Za-z][^>]*>/g)
  s = blankAll(s, /<(script|style)\b[\s\S]*?<\/\1>/gi)
  s = blankAll(s, /`[^`\n]+`/g)
  // reference definitions and GitHub alerts
  s = blankAll(s, /^\s{0,3}\[[^\]\n]+\]:\s*\S+.*$/gm)
  s = blankAll(s, /\[!(?:NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]/g)
  // images and links: keep the alt or link text, blank the brackets and the target
  // images first, so a badge (an image inside a link) leaves no "!" behind
  s = s.replace(/!\[([^\]\n]*)\]\(([^)\n]*)\)/g, (m, text) => '  ' + text + ' '.repeat(m.length - 2 - text.length))
  s = s.replace(/\[([^\]\n]*)\]\(([^)\n]*)\)/g, (m, text) => ' ' + text + ' '.repeat(m.length - 1 - text.length))
  s = s.replace(/\[([^\]\n]*)\]\[[^\]\n]*\]/g, (m, text) => ' ' + text + ' '.repeat(m.length - 1 - text.length))
  // HTML tags: keep visible attribute values
  s = s.replace(/<\/?[A-Za-z][^<>]*>/g, (m) => {
    let out = m.replace(/[^\n]/g, ' ')
    for (const a of m.matchAll(/\b(?:alt|title|aria-label)\s*=\s*(["'])(.*?)\1/g)) {
      const at = a.index + a[0].indexOf(a[2], a[0].indexOf(a[1]))
      out = out.slice(0, at) + a[2] + out.slice(at + a[2].length)
    }
    return out
  })
  s = blankAll(s, /\b(?:https?|ftp):\/\/[^\s<>()"'`]+|\bwww\.[^\s<>()"'`]+|[\w.+-]+@[\w-]+\.[\w.-]+/g)
  s = blankAll(s, /&[a-z]+;|&#\d+;/g)
  s = blankAll(s, /^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$/gm)
  return s
}

/** allow directives `<!-- copy-lint-allow COPY-05 reason -->` by 1-based line (the directive line and the next line) */
function mdAllows(raw) {
  const out = new Map()
  for (const m of raw.matchAll(/<!--\s*copy-lint-allow\s+((?:COPY-\d\d[ ,]*)+)\s*(.*?)-->/g)) {
    const [line] = lineCol(raw, m.index)
    const rules = m[1].split(/[ ,]+/).filter(Boolean)
    for (const l of [line, line + 1]) out.set(l, [...(out.get(l) ?? []), ...rules])
  }
  return out
}

function mdSegments(file, raw, lang) {
  const vis = mdVisible(raw)
  const lines = vis.split('\n')
  const rawLines = raw.split('\n')
  const segs = []
  let off = 0
  let para = null
  const flush = () => {
    if (para) segs.push(para)
    para = null
  }
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i]
    const r = rawLines[i] ?? ''
    const start = off
    off += l.length + 1
    if (!l.trim()) {
      flush()
      continue
    }
    const heading = /^\s{0,3}#{1,6}\s/.test(r) || /<h[1-6]\b/i.test(r)
    const single = heading || /^\s*\|/.test(r) || /^\s*(?:[-*+]|\d+\.)\s/.test(r) || /^\s*>/.test(r)
    if (single) {
      flush()
      const text = heading ? l.replace(/^(\s{0,3})(#{1,6})(\s)/, (m) => ' '.repeat(m.length)) : l
      segs.push({ start, text, heading, label: heading })
      continue
    }
    if (para) para.text += '\n' + l
    else para = { start, text: l, heading: false, label: false }
  }
  flush()
  return segs.map((sg) => ({
    ...sg, lang, file,
    where: (i) => lineCol(vis, sg.start + i),
    anchorAt: (i) => (rawLines[lineCol(vis, sg.start + i)[0] - 1] ?? '').trim().replace(/\s+/g, ' ').slice(0, 160),
  }))
}

/** i18n dictionaries: flat or nested JSON objects; the anchor is the dotted key */
function jsonSegments(file, raw, lang, { fields = null } = {}) {
  const data = JSON.parse(raw)
  const segs = []
  let cursor = 0
  const visit = (v, path) => {
    if (typeof v === 'string') {
      const key = path[path.length - 1]
      // the cursor follows every string value (also the skipped ones), so that a reported key is located at its own occurrence
      const quoted = JSON.stringify(String(key)) + ':'
      let at = raw.indexOf(quoted, cursor)
      if (at < 0) at = raw.indexOf(quoted)
      if (at >= 0) cursor = at + quoted.length
      // OpenAPI: summaries and descriptions; generated schema titles ("Task Id") are not copy, the document title is
      if (fields && !(fields.includes(key) || (key === 'title' && path.length === 2 && path[0] === 'info'))) return
      const vAt = at >= 0 ? raw.indexOf('"', at + quoted.length) + 1 : 0
      const [line, col] = lineCol(raw, Math.max(vAt, 0))
      // ICU placeholders and arguments are not copy; OpenAPI descriptions are CommonMark, their code spans are not copy either
      let text = v.replace(/\{[^{}]*\}/g, (m) => ' '.repeat(m.length))
      if (fields) text = text.replace(/`[^`\n]+`/g, (m) => ' '.repeat(m.length))
      const sLang = lang === 'auto' ? detectLang(v) : lang
      const anchor = fields ? path.join('/') : path.join('.')
      segs.push({ text, lang: sLang, file, heading: false, label: fields ? key === 'summary' || key === 'title' : isLabelText(text, sLang), anchor,
        where: (i) => [line, col + i], anchorAt: () => anchor, key: path.join('.') })
    } else if (Array.isArray(v)) v.forEach((x, i) => visit(x, [...path, String(i)]))
    else if (v && typeof v === 'object') for (const [k, x] of Object.entries(v)) visit(x, [...path, k])
  }
  visit(data, [])
  return segs
}

/** CITATION.cff: title, message, abstract and keywords of the software (references to third-party works are skipped) */
function cffSegments(file, raw, lang) {
  const lines = raw.split('\n')
  let key = null
  let masked = ''
  for (const l of lines) {
    const top = /^([A-Za-z-]+):/.exec(l)
    if (top) key = top[1]
    let vis = l.replace(/[^\n]/g, ' ')
    if (['title', 'message', 'abstract', 'keywords'].includes(key)) {
      vis = l.replace(/^([A-Za-z-]+:\s*(?:>-?|\|-?)?\s*)/, (m) => ' '.repeat(m.length)).replace(/^(\s*-\s+)/, (m) => ' '.repeat(m.length))
      vis = vis.replace(/^(\s*)(["'])(.*)\2\s*$/, (m, sp, q, body) => sp + ' ' + body + ' ')
    }
    masked += vis + '\n'
  }
  return mdSegments(file, masked.slice(0, -1), lang).map((s) => ({ ...s, anchorAt: (i) => (lines[s.where(i)[0] - 1] ?? '').trim().slice(0, 160) }))
}

/** HTML pages: visible text nodes and the alt, title, aria-label and meta description values */
function htmlSegments(file, raw, lang) {
  let s = raw.replace(/<head\b[\s\S]*?<\/head>/i, (m) => {
    let out = m.replace(/[^\n]/g, ' ')
    for (const re of [/<title>([\s\S]*?)<\/title>/i, /<meta\s+name=["']description["']\s+content=["']([^"']*)["']/i]) {
      const x = re.exec(m)
      if (x) {
        const at = x.index + x[0].indexOf(x[1])
        out = out.slice(0, at) + x[1] + out.slice(at + x[1].length)
      }
    }
    return out
  })
  return mdSegments(file, s, lang).map((sg) => ({ ...sg, heading: sg.heading, label: sg.label || isLabelText(sg.text, lang) }))
}

export function segmentsOf(file, raw, { lang, kind }) {
  const k = kind ?? kindOf(file)
  // documents take one language ("auto" detects it from the CJK share); dictionaries and OpenAPI decide per string
  if (lang === 'auto' && k !== 'i18n' && k !== 'openapi') lang = detectLang(raw)
  if (k === 'i18n') return jsonSegments(file, raw, lang ?? 'auto')
  if (k === 'openapi') return jsonSegments(file, raw, lang ?? 'auto', { fields: ['summary', 'description'] })
  if (k === 'cff') return cffSegments(file, raw, lang ?? 'en')
  if (k === 'html') return htmlSegments(file, raw, lang ?? detectLang(raw))
  return mdSegments(file, raw, lang ?? detectLang(raw))
}
export const kindOf = (f) => (f.endsWith('.json') ? (/openapi/i.test(f) ? 'openapi' : 'i18n') : f.endsWith('.cff') ? 'cff' : /\.html?$/.test(f) ? 'html' : 'md')
export const langOf = (f) => (/zh(?:[-_](?:CN|Hans))?[./]|\.zh\b|中文/i.test(f) ? 'zh' : /(?:^|[./_-])en(?:[-_]US)?[./]/.test(f) || /^README\.md$/.test(f) ? 'en' : undefined)

// ---- rules ------------------------------------------------------------------------------------------------------------------------
function phraseHits(seg, add) {
  const t = seg.text
  const wl = spansOf(t, [...ZH_WHITELIST, ...EN_WHITELIST])
  const hit = (rule, res, msg) => {
    for (const re of res) for (const m of t.matchAll(re)) {
      if (overlaps(wl, m.index, m.index + m[0].length)) continue
      add(rule, m.index, m[0], msg(m[0]))
    }
  }
  hit('COPY-01', ZH_PHRASES, (w) => `colloquial or marketing phrase "${w}" (§7.10 principle 6); state the capability and its conditions`)
  hit('COPY-01', EN_PHRASES, (w) => `banned word "${w}" (§7.10 English rule 2); state the capability and its conditions`)
  hit('COPY-08', ZH_SUPERLATIVE, (w) => `unverifiable superlative "${w}" (§7.10 principle 4); comparisons need the D3-AC-53 report`)
  hit('COPY-08', EN_SUPERLATIVE, (w) => `unverifiable superlative "${w}" (§7.10 principle 4); comparisons need the D3-AC-53 report`)
}

function toneHits(seg, add) {
  const t = seg.text
  for (const m of t.matchAll(/[!！]+|[?？]{2,}|[?？][!！]/g)) {
    if (m[0] === '!' && t[m.index + 1] === '=') continue
    add('COPY-02', m.index, m[0], `exclamation or emphatic punctuation "${m[0]}" (§7.10 principle 6)`)
  }
  for (const m of t.matchAll(/[~～]+/g)) {
    const before = t.slice(0, m.index).trimEnd().slice(-1)
    const after = t.slice(m.index + m[0].length).trimStart()[0] ?? ''
    if (/\d/.test(after) || (/\d/.test(before) && /\d/.test(after))) continue
    add('COPY-02', m.index, m[0], `tilde "${m[0]}" as a tone mark (§7.10 principle 6); a tilde only marks an approximation or a range of numbers`)
  }
  for (const m of t.matchAll(/[\u3400-\u9fff]([啦呀哦嘛呗噢喔咯嘞哟哇吧]+)(?=[，。！？、；：,.!?;:\s）)]|$)/g)) {
    const at = m.index + 1
    if (m[1] === '吧' && /酒吧|网吧|贴吧/.test(t.slice(at - 1, at + 1))) continue
    add('COPY-02', at, m[1], `modal particle "${m[1]}" (§7.10 principle 6); use declarative sentences`)
  }
}

function ellipsisHits(seg, add) {
  const t = seg.text
  for (const m of t.matchAll(/…+|\.{3,}|。{3,}|⋯+/g)) {
    const a = m.index
    const b = a + m[0].length
    const single = m[0] === '…' || m[0] === '...'
    // truncated display: an identifier or a value cut short ("sha256:…", "0x1f…)")
    const prev = t[a - 1] ?? ''
    const next = t[b] ?? ''
    if (single && /[\w:/-]/.test(prev) && (/^[)\]"'”’」》）】,，、。\s]$/.test(next) || next === '')) continue
    // an elided placeholder ("<...>", "[…]", "(...)")
    if (single && /[<[(（]/.test(prev) && /[>\])）]/.test(next)) continue
    // a menu item that opens a dialog: a short label ending in one ellipsis
    if (single && seg.label && !seg.heading && t.slice(b).trim() === '') continue
    add('COPY-03', a, m[0], `ellipsis "${m[0]}" outside a dialog-opening menu item or a truncated display (§7.10 principle 6)`)
  }
}

function glyphHits(seg, add) {
  for (const h of glyphScan(seg.text)) {
    const cp = 'U+' + h.ch.codePointAt(0).toString(16).toUpperCase().padStart(4, '0')
    add('COPY-04', h.index, h.ch, `${cp} is not allowed in copy (${h.rule === 'EMOJI-01' ? 'emoji' : 'forbidden glyph block'}, §7.10 principle 6)`)
  }
}

function numberHits(seg, add) {
  const t = seg.text
  for (const [a, b] of sentences(t)) {
    const s = t.slice(a, b)
    if (CONDITION.some((re) => re.test(s))) continue
    const seen = []
    for (const re of PERF) for (const m of s.matchAll(re)) {
      if (re === PERF[3] && SCALE_LIMIT.test(s)) continue
      const lead = m[0].search(/\S/)
      const at = a + m.index + Math.max(lead, 0)
      if (seen.some(([x, y]) => at < y && at + m[0].length > x)) continue
      seen.push([at, at + m[0].length])
      add('COPY-05', at, m[0].trim(), `number "${m[0].trim()}" without conditions (hardware, resolution, sensor set, clock or physics tier, method; §7.10 principle 3)`)
    }
    if (DURATION_CLAIM.test(s) && !DURATION_PARAM.test(s)) {
      for (const m of s.matchAll(DURATION)) {
        const at = a + m.index
        if (seen.some(([x, y]) => at < y && at + m[0].length > x)) continue
        add('COPY-05', at, m[0], `timing claim "${m[0]}" without conditions (hardware, data size, method; §7.10 principle 3)`)
      }
    }
  }
}

function nameHits(seg, add, state) {
  const t = seg.text
  for (const m of t.matchAll(REPO_ID)) add('COPY-06', m.index, m[0], 'the repository identifier ANet-Drone4D is for URLs and package names only; write ANet Drone4D in copy')
  for (const [re, ok] of SPELLINGS) {
    for (const m of t.matchAll(re)) {
      if (ok.includes(m[0])) continue
      add('COPY-06', m.index, m[0], `"${m[0]}" is spelled ${ok[0]}`)
    }
  }
  if (state && !state.productSeen) {
    const m = PRODUCT.exec(t)
    PRODUCT.lastIndex = 0
    if (m) {
      state.productSeen = true
      if (m[0] === 'Drone4D') add('COPY-06', m.index, m[0], 'the first mention in a document is "ANet Drone4D"; "Drone4D" may follow')
    }
  }
}

function termHits(seg, add) {
  const t = seg.text
  for (const [re, msg] of TERMS) for (const m of t.matchAll(re)) add('COPY-10', m.index, m[0], `"${m[0]}": ${msg} (§7.10 term table)`)
  // "沙盘" belongs to D3 copy only: in the dictionaries outside lab.* it names the D1 view or the D2 sandbox
  if (seg.key && !seg.key.startsWith('lab.')) {
    for (const m of t.matchAll(/(?<!数字|事件)沙盘/g)) add('COPY-10', m.index, m[0], '"沙盘" is used only for the D3 Event Lab (事件沙盘); D1 is 三维场景视图, D2 is 交互沙盒')
  }
}

function sentenceCaseHits(seg, add) {
  if (seg.lang !== 'en' || !seg.label) return
  let t = seg.text
  for (const p of PROPER_PHRASES) {
    let i = t.indexOf(p)
    while (i >= 0) {
      t = blankRange(t, i, i + p.length)
      i = t.indexOf(p, i + p.length)
    }
  }
  const words = [...t.matchAll(/[A-Za-z][A-Za-z0-9'’-]*/g)]
  if (words.length < 2) return
  const first = words[0].index
  for (const w of words) {
    if (w.index === first) continue
    const before = t.slice(0, w.index).trimEnd()
    if (/[.:!?—–·|/(["“‘']$/.test(before) || before === '') continue
    for (const part of w[0].split('-')) {
      if (!/^[A-Z][a-z]+$/.test(part) || PROPER_WORDS.has(part)) continue
      add('COPY-07', w.index, w[0], `"${w[0]}" in a title or label; use sentence case ("Export dataset"), proper nouns keep their spelling (§7.10 English rule 1)`)
      break
    }
  }
}

function sloganHits(seg, add) {
  const t = seg.text
  for (const m of t.matchAll(/\b[A-Za-z]+ in, [A-Za-z]+ out\b/gi)) add('COPY-09', m.index, m[0], `slogan "${m[0]}" (§7.10 English rule 2); replace it with a capability statement`)
  if (seg.heading) {
    const s = t.trim()
    const at = t.indexOf(s)
    const short = CJK.test(s) ? [...s].length <= 20 : s.split(/\s+/).length <= 8
    if (short && /[.。!！]$/.test(s) && !/\b(?:e\.g|etc|i\.e|v\d+(?:\.\d+)*)\.$/.test(s)) add('COPY-09', at, s, `slogan-style tagline "${s}"; headings are noun phrases without a closing period, or a capability statement`)
  }
}

const RULE_FNS = [phraseHits, toneHits, ellipsisHits, glyphHits, numberHits, nameHits, termHits, sentenceCaseHits, sloganHits]

/** Run the rules over the segments of one file; returns findings { file, line, col, rule, match, anchor, msg }. */
export function checkSegments(segs, { rules = RULES, allows = new Map() } = {}) {
  const out = []
  // the first-mention rule applies to documents (Markdown, CITATION.cff, HTML), not to dictionary entries
  const state = segs.length && segs[0].key === undefined ? { productSeen: false } : null
  const keys = new Set()
  for (const seg of segs) {
    const spans = []
    const add = (rule, index, match, msg) => {
      if (!rules.includes(rule)) return
      const [line, col] = seg.where(index)
      if ((allows.get(line) ?? []).includes(rule)) return
      const k = `${line}:${col}:${rule}`
      if (keys.has(k)) return
      // one finding per span and rule ("一键搞定" is one phrase, not two)
      const end = index + Math.max(match.length, 1)
      if (spans.some(([r, x, y]) => r === rule && index < y && end > x)) return
      spans.push([rule, index, end])
      keys.add(k)
      out.push({ file: seg.file, line, col, rule, match, anchor: seg.anchorAt(index), msg })
    }
    for (const fn of RULE_FNS) fn(seg, add, state)
  }
  return out.sort((a, b) => a.line - b.line || a.col - b.col)
}

/** Check one file's text; lang and kind as in the scope entry. */
export function checkText(file, raw, { lang, kind, rules } = {}) {
  const k = kind ?? kindOf(file)
  const segs = segmentsOf(file, raw, { lang: lang ?? langOf(file), kind: k })
  const allows = k === 'md' || k === 'html' ? mdAllows(raw) : new Map()
  return checkSegments(segs, { rules, allows })
}

// ---- baseline register -----------------------------------------------------------------------------------------------------------
export function loadRegister(path = join(ROOT, BASELINE_FILE)) {
  if (!existsSync(path)) return { exceptions: [], allow: [] }
  const j = JSON.parse(readFileSync(path, 'utf8'))
  return { ...j, exceptions: j.exceptions ?? [], allow: j.allow ?? [] }
}
const sameEntry = (e, f) => e.file === f.file && e.rule === f.rule && e.match === f.match && (e.anchor === undefined || e.anchor === f.anchor)

/** Split findings into reported and excused; report malformed and stale register entries. */
export function applyRegister(findings, reg, { scannedFiles = null } = {}) {
  const problems = []
  const used = new Set()
  const entries = [...reg.exceptions.map((e) => ['exceptions', e]), ...reg.allow.map((e) => ['allow', e])]
  entries.forEach(([sec, e], i) => {
    const miss = ['file', 'rule', 'match', 'reason'].filter((k) => typeof e[k] !== 'string' || !e[k].trim())
    if (sec === 'allow' && (typeof e.reviewed !== 'string' || !e.reviewed.trim())) miss.push('reviewed')
    if (miss.length) problems.push({ file: BASELINE_FILE, line: 1, col: 1, rule: 'COPY-BL-02', msg: `${sec}[${i}] lacks ${miss.join(', ')}` })
  })
  const reported = []
  const excused = []
  for (const f of findings) {
    const j = entries.findIndex(([, e]) => sameEntry(e, f))
    if (j >= 0) {
      used.add(j)
      excused.push({ ...f, register: entries[j][0] })
    } else reported.push(f)
  }
  entries.forEach(([sec, e], i) => {
    if (used.has(i) || (scannedFiles && !scannedFiles.has(e.file))) return
    problems.push({ file: BASELINE_FILE, line: 1, col: 1, rule: 'COPY-BL-01', msg: `${sec}[${i}] ${e.file} ${e.rule} "${e.match}" no longer matches; remove the entry` })
  })
  return { reported, excused, problems }
}

export function loadScope(path = join(ROOT, SCOPE_FILE)) {
  const entries = [...DEFAULT_SCOPE]
  if (existsSync(path)) {
    const j = JSON.parse(readFileSync(path, 'utf8'))
    if (j.replace_default) entries.length = 0
    for (const e of j.files ?? []) entries.push(e)
  }
  const files = []
  for (const e of entries) {
    if (e.path.endsWith('/')) {
      const re = e.match ? new RegExp(e.match) : /\.(md|json|html?|cff|txt)$/
      for (const f of walk(e.path.slice(0, -1), (r) => re.test(r))) files.push({ ...e, path: f })
    } else if (existsSync(join(ROOT, e.path))) files.push(e)
  }
  const seen = new Set()
  return files.filter((e) => (seen.has(e.path) ? false : seen.add(e.path)))
}

function parseArgs(argv) {
  const o = { files: [], strict: false, baseline: true, write: false, rules: RULES, lang: undefined, kind: undefined, text: null }
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    if (a === '--strict') o.strict = true
    else if (a === '--no-baseline') o.baseline = false
    else if (a === '--write-baseline') o.write = true
    else if (a === '--rules') o.rules = argv[++i].split(',').flatMap((r) => RULE_ALIAS[r] ?? [r.toUpperCase()])
    else if (a === '--lang') o.lang = argv[++i]
    else if (a === '--kind') o.kind = argv[++i]
    else if (a === '--text') o.text = argv[++i]
    else if (a.startsWith('--')) throw new Error(`unknown option ${a}`)
    else o.files.push(a)
  }
  return o
}

if (isMain(import.meta.url)) {
  await run('copy-lint', async () => {
    const o = parseArgs(process.argv.slice(2))
    const R = new Reporter('copy-lint')
    let scope
    if (o.text !== null) scope = [{ path: '<text>', lang: o.lang ?? detectLang(o.text), kind: o.kind ?? 'text', raw: o.text }]
    else if (o.files.length) scope = o.files.map((f) => ({ path: f, lang: o.lang ?? langOf(f), kind: o.kind ?? kindOf(f) }))
    else scope = loadScope()
    const findings = []
    const scanned = new Set()
    for (const e of scope) {
      const raw = e.raw ?? readText(e.path)
      if (raw === null) continue
      scanned.add(e.path)
      findings.push(...checkText(e.path, raw, { lang: e.lang, kind: e.kind === 'text' ? 'md' : e.kind, rules: o.rules }))
    }
    const reg = o.baseline ? loadRegister() : { exceptions: [], allow: [] }
    if (o.write) {
      if (o.text !== null || o.files.length || o.rules.length !== RULES.length) throw new Error('--write-baseline needs the full scope and every rule')
      const { reported } = applyRegister(findings, { exceptions: [], allow: reg.allow })
      const old = loadRegister()
      const exceptions = reported.map((f) => {
        const prev = old.exceptions.find((e) => sameEntry(e, f))
        return { file: f.file, rule: f.rule, match: f.match, anchor: f.anchor, reason: prev?.reason ?? 'P-1 基线：待 D2 发布面复核（L02）改写或登记理由', until: prev?.until ?? 'P0b' }
      })
      const uniq = exceptions.filter((e, i) => exceptions.findIndex((x) => sameEntry(x, e) && x.anchor === e.anchor) === i)
      const doc = { $comment: old.$comment ?? 'copy-lint 基线例外登记（docs/05 §7.10；D3-AC-46、D3-AC-23）', updated: new Date().toISOString().slice(0, 10), exceptions: uniq, allow: reg.allow }
      writeFileSync(join(ROOT, BASELINE_FILE), JSON.stringify(doc, null, 1) + '\n')
      R.note(`wrote ${uniq.length} baseline exception(s) to ${BASELINE_FILE}`)
      R.finish({ files: scanned.size, baseline: uniq.length })
      return
    }
    const explicit = o.text !== null || o.files.length > 0
    const { reported, excused, problems } = applyRegister(findings, reg, { scannedFiles: explicit ? scanned : null })
    for (const f of reported) R.add(f.file, f.line, f.col, f.rule, f.msg)
    // stale entries are checked only when the whole scope was scanned with every rule
    if (!explicit && o.rules.length === RULES.length) for (const p of problems) R.add(p.file, p.line, p.col, p.rule, p.msg)
    else for (const p of problems.filter((x) => x.rule === 'COPY-BL-02')) R.add(p.file, p.line, p.col, p.rule, p.msg)
    const nBase = excused.filter((x) => x.register === 'exceptions').length
    if (nBase) R.note(`${nBase} finding(s) excused by baseline exceptions in ${BASELINE_FILE} (to be cleared before the P0b exit)`)
    if (o.strict && reg.exceptions.length) R.add(BASELINE_FILE, 1, 1, 'COPY-BL-03', `--strict: ${reg.exceptions.length} baseline exception(s) remain; the P0b exit requires none (D3-AC-23)`)
    R.finish({ files: scanned.size, excused: excused.length, baseline_exceptions: reg.exceptions.length })
  })
}
