// V0.3 发布文案预备改动（D3-文案评审记录 §4 W-14、§9；D3-AC-46）。README 能力表、运行环境表与路线表的 V0.3 行，以及落地页路线图
// 章节由"开发中"改为已交付的改动先备好，存于 docs/impl/v0.3-release/copy.prepared.json，封板后在 V0.3 发布动作中应用。本用例保证：
// 每项改动在当前工作树上仍可原样应用（find 恰好出现 count 次，且尚未提前应用）；应用后的 README 与词典仍是合法文件并通过
// copy-lint 严格模式；对外文本不出现"封板"、ADR 与验收编号。发布任务应用改动后删除该 JSON 与本用例。
import { spawnSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { basename, join } from 'node:path'
import { describe, expect, it } from 'vitest'

const REPO = join(import.meta.dirname, '..', '..', '..', '..')
const PREPARED = 'docs/impl/v0.3-release/copy.prepared.json'

interface Edit { file: string; find: string; replace: string; count: number; note?: string }
const doc = JSON.parse(readFileSync(join(REPO, PREPARED), 'utf8')) as { finding: string; effective: string; edits: Edit[] }

const occurrences = (s: string, x: string) => s.split(x).length - 1

/** 把全部改动应用到当前文件内容上（按文件聚合） */
function applied(): Map<string, string> {
  const out = new Map<string, string>()
  for (const e of doc.edits) {
    const cur = out.get(e.file) ?? readFileSync(join(REPO, e.file), 'utf8')
    out.set(e.file, cur.split(e.find).join(e.replace))
  }
  return out
}

describe('V0.3 release copy prepared for after the seal (W-14)', () => {
  it('is registered as the W-14 change set that takes effect after the seal', () => {
    expect(doc.finding).toBe('W-14')
    expect(doc.effective).toBe('after-seal')
    const files = new Set(doc.edits.map((e) => e.file))
    for (const f of ['README.md', 'README.zh-CN.md', 'apps/web/src/app/i18n/en.json', 'apps/web/src/app/i18n/zh-CN.json', 'apps/web/src/app/demo/LandingContent.ts']) {
      expect(files.has(f), f).toBe(true)
    }
  })

  it('still applies to the working tree and has not been applied early', () => {
    for (const e of doc.edits) {
      const text = readFileSync(join(REPO, e.file), 'utf8')
      expect(occurrences(text, e.find), `${e.file}: ${e.find}`).toBe(e.count)
      expect(e.replace).not.toBe(e.find)
      if (!e.find.includes(e.replace)) expect(text.includes(e.replace), `${e.file} already carries: ${e.replace}`).toBe(false)
    }
  })

  it('leaves no V0.3 row in development and keeps the copy free of internal terms', () => {
    const out = applied()
    for (const f of ['README.md', 'README.zh-CN.md']) {
      const rows = out.get(f)!.split('\n').filter((l) => /\(V0\.3\)|（V0\.3）|^\| V0\.3 \|/.test(l))
      expect(rows.length, f).toBeGreaterThanOrEqual(5)
      for (const r of rows) expect(r, `${f}: ${r}`).not.toMatch(/In development|in development|开发中/)
    }
    for (const f of ['apps/web/src/app/i18n/en.json', 'apps/web/src/app/i18n/zh-CN.json']) {
      const d = JSON.parse(out.get(f)!) as Record<string, string>
      for (const k of Object.keys(d).filter((x) => x.startsWith('landing.story.roadmap.'))) expect(d[k], k).not.toMatch(/In development|IN DEVELOPMENT|开发中/)
    }
    expect(out.get('apps/web/src/app/demo/LandingContent.ts')).toMatch(/id: 'roadmap'.*points: \['available', 'available', 'research', 'research'\]/)
    for (const e of doc.edits) expect(e.replace, e.file).not.toMatch(/封板|ADR-\d|D3-AC-\d|\bseal/i)
  })

  it('passes copy-lint in strict mode after it is applied', () => {
    const dir = mkdtempSync(join(tmpdir(), 'k09-release-copy-'))
    try {
      const files: string[] = []
      for (const [f, text] of applied()) {
        if (!/\.(md|json)$/.test(f)) continue
        const p = join(dir, basename(f))                                   // 文件名决定语言（README.zh-CN.md、zh-CN.json）
        writeFileSync(p, text)
        files.push(p)
      }
      expect(files).toHaveLength(4)
      const lint = (paths: string[]) => spawnSync('node', [join(REPO, 'tools/lint/copy-lint.mjs'), '--strict', '--no-baseline', ...paths], { cwd: REPO, encoding: 'utf8' })
      const res = lint(files)
      expect(res.status, res.stdout + res.stderr).toBe(0)
      expect(res.stdout + res.stderr).toMatch(/copy-lint: ok/)
      // 对照：显式文件确实被检查（同一调用方式对违规文本报错）
      const ctrl = join(dir, 'control.en.md')
      writeFileSync(ctrl, 'ANet Drone4D is awesome.\n')
      expect(lint([ctrl]).status).toBe(1)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })
})
