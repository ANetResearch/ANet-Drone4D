// Minimal ICU-style formatting for the i18n runtime (M15-FR-102): {name} substitutions and
// {count, plural, one {...} other {...}} with Intl.PluralRules; `#` inside a branch is the number.
const rulesCache = new Map<string, Intl.PluralRules>()

function pluralRules(locale: string): Intl.PluralRules {
  let r = rulesCache.get(locale)
  if (!r) {
    r = new Intl.PluralRules(locale)
    rulesCache.set(locale, r)
  }
  return r
}

/** format `raw` with params; unknown params render as empty strings */
export function formatMessage(raw: string, params: Record<string, string | number> | undefined, locale: string): string {
  if (!params || raw.indexOf('{') < 0) return raw
  let out = ''
  let i = 0
  while (i < raw.length) {
    const open = raw.indexOf('{', i)
    if (open < 0) {
      out += raw.slice(i)
      break
    }
    out += raw.slice(i, open)
    let depth = 0
    let j = open
    for (; j < raw.length; j++) {
      if (raw[j] === '{') depth++
      else if (raw[j] === '}' && --depth === 0) break
    }
    const body = raw.slice(open + 1, j)
    const m = /^(\w+)\s*,\s*plural\s*,\s*([\s\S]*)$/.exec(body)
    if (m) {
      const n = Number(params[m[1]] ?? 0)
      const cat = pluralRules(locale).select(n)
      const forms = new Map<string, string>()
      for (const f of m[2].matchAll(/(=\d+|\w+)\s*\{([^{}]*)\}/g)) forms.set(f[1], f[2])
      const chosen = forms.get(`=${n}`) ?? forms.get(cat) ?? forms.get('other') ?? ''
      out += chosen.replace(/#/g, String(n))
    } else {
      const v = params[body.trim()]
      out += v === undefined ? '' : String(v)
    }
    i = j + 1
  }
  return out
}
