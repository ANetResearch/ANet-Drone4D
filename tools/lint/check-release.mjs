// Release set hygiene (AWR-03 ADR-079; AWR-18 §13.1). The release set is what a commit of the working
// tree would publish: `git ls-files --cached --others --exclude-standard` (GIT_DIR and GIT_WORK_TREE are honoured, so a
// repository whose git directory lives outside the tree is covered). Without git or outside a repository the tool prints a
// note and passes (the hosted CI job always runs inside a clone).
//   REL-01 a file larger than 5,000,000 bytes (README media, fixtures and goldens included; GitHub pages and clones stay light)
//   REL-02 generated or third-party data: anything under worlds/, data/raw/, runs/, refs/, .cache/ or apps/web/dist/, point-cloud
//          files (.ply .las .laz .e57 .pcd) and Potree containers (octree.bin, hierarchy.bin); UrbanScene3D terms forbid
//          redistribution and World Packages are rebuilt locally (ADR-034)
//   REL-03 a credential: private-key blocks and common token shapes, and every value of ~/.config/awr/*.env (12 characters or
//          more) found verbatim in a text file; values are compared in memory and never printed, only the file and the key name
//   REL-04 the deployment admin secret (internal identity passphrase, ADR-123; AWR-04 §15.9.5 item 2): when this machine has a
//          deployment secret file (~/.config/awr/admin.secret, or the file AWR_ADMIN_SECRET_FILE points to), its first line
//          (6 characters or more) must not appear verbatim in any text file of the release set; compared in memory, never
//          printed (only the file, line and column are reported); without git the rule still runs, over the lint scope
// Usage: node tools/lint/check-release.mjs [--files <list-file>] [--secret-file <path>]
//   the list file holds one path per line (repository-relative or absolute); --secret-file overrides the deployment secret
//   file for REL-04 (tests use a temporary file with a random value)
import { execFileSync } from 'node:child_process'
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { homedir } from 'node:os'
import { isAbsolute, join } from 'node:path'
import { ROOT, Reporter, isBinaryPath, isMain, lineCol, run, scopeFiles } from './_common.mjs'

export const MAX_BYTES = 5_000_000
const DATA_DIRS = /^(worlds|data\/raw|runs|refs|\.cache|apps\/web\/dist)\//
const DATA_FILES = /\.(ply|las|laz|e57|pcd)$|(^|\/)(octree|hierarchy)\.bin$/i
// token shapes (AWR-18 §13.1 REL-03); each needs enough entropy that documentation examples do not match
const TOKENS = [
  [/-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----/, 'private key block'],
  [/\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{32,}/, 'API secret key (sk-...)'],
  [/\bAKIA[0-9A-Z]{16}\b/, 'AWS access key id'],
  [/\bgh[pousr]_[A-Za-z0-9]{36,}\b/, 'GitHub token'],
  [/\bgithub_pat_[A-Za-z0-9_]{60,}\b/, 'GitHub fine-grained token'],
  [/\bxox[abprs]-[A-Za-z0-9-]{20,}/, 'Slack token'],
  [/\bAIza[0-9A-Za-z_-]{35}\b/, 'Google API key'],
  [/\bglpat-[A-Za-z0-9_-]{20,}\b/, 'GitLab token'],
]

/** values of ~/.config/awr/*.env (KEY=VALUE lines, quotes stripped), at least 12 characters; never printed */
export function localSecrets(dir = join(homedir(), '.config', 'awr')) {
  const out = []
  if (!existsSync(dir)) return out
  for (const n of readdirSync(dir)) {
    if (!n.endsWith('.env')) continue
    let text
    try {
      text = readFileSync(join(dir, n), 'utf8')
    } catch {
      continue
    }
    for (const line of text.split('\n')) {
      const m = /^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/.exec(line)
      if (!m) continue
      const v = m[2].replace(/^(['"])(.*)\1$/, '$2')
      if (v.length >= 12) out.push({ file: n, key: m[1], value: v })
    }
  }
  return out
}

export const DEPLOY_SECRET_MIN = 6

/** the deployment admin secret for REL-04: { source, value } or null. Candidates in order: an explicit path, then
 *  AWR_ADMIN_SECRET_FILE, then ~/.config/awr/admin.secret; the first readable one counts. Never printed. */
export function deploySecret(explicit = null, env = process.env, home = homedir()) {
  const cands = explicit ? [explicit] : [env.AWR_ADMIN_SECRET_FILE, join(home, '.config', 'awr', 'admin.secret')].filter(Boolean)
  for (const p of cands) {
    let text
    try {
      text = readFileSync(p, 'utf8')
    } catch {
      continue
    }
    const value = text.split('\n', 1)[0].replace(/\r$/, '')
    return value.length >= DEPLOY_SECRET_MIN ? { source: p, value } : null
  }
  return null
}

/** the release set, or null when git cannot tell (no git, not a repository) */
export function releaseSet() {
  try {
    const out = execFileSync('git', ['ls-files', '--cached', '--others', '--exclude-standard', '-z'], { cwd: ROOT, encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'], maxBuffer: 64 << 20 })
    return out.split('\0').filter(Boolean)
  } catch {
    return null
  }
}

/** checks of one release entry (pure apart from the reporter): size, data paths, credentials, deployment secret */
export function checkEntry(f, size, text, secrets, R, deploy = null) {
  if (size > MAX_BYTES) R.add(f, 1, 1, 'REL-01', `${size} bytes > ${MAX_BYTES} (5 MB); shrink it or keep it out of the repository`)
  if (DATA_DIRS.test(f)) R.add(f, 1, 1, 'REL-02', 'generated or third-party data directory (worlds, data/raw, runs, refs, .cache, dist) must not be published')
  else if (DATA_FILES.test(f)) R.add(f, 1, 1, 'REL-02', 'point-cloud or Potree container file must not be published (UrbanScene3D terms, ADR-034)')
  if (text === null) return
  for (const [re, what] of TOKENS) {
    const m = re.exec(text)
    if (m) {
      const [line, col] = lineCol(text, m.index)
      R.add(f, line, col, 'REL-03', `looks like a ${what}; remove it and rotate the credential`)
    }
  }
  for (const s of secrets) {
    const i = text.indexOf(s.value)
    if (i >= 0) {
      const [line, col] = lineCol(text, i)
      R.add(f, line, col, 'REL-03', `contains the value of ${s.key} from ~/.config/awr/${s.file}; remove it and rotate the credential`)
    }
  }
  if (deploy && deploy.value.length >= DEPLOY_SECRET_MIN) {
    const i = text.indexOf(deploy.value)
    if (i >= 0) {
      const [line, col] = lineCol(text, i)
      R.add(f, line, col, 'REL-04', 'contains the deployment admin secret (internal identity passphrase); remove it and rotate the secret with install.sh --admin-secret-file')
    }
  }
}

if (isMain(import.meta.url)) {
  await run('check-release', async () => {
    const args = process.argv.slice(2)
    const R = new Reporter('check-release')
    const i = args.indexOf('--files')
    const files = i >= 0 ? readFileSync(args[i + 1], 'utf8').split('\n').map((s) => s.trim()).filter(Boolean) : releaseSet()
    const j = args.indexOf('--secret-file')
    const deploy = deploySecret(j >= 0 ? args[j + 1] : null)
    if (files === null) {
      R.note('git is not available or this is not a repository; release set not checked (set GIT_DIR and GIT_WORK_TREE)')
      if (deploy) {
        // REL-04 does not wait for git: with a deployment secret on this machine, scan the lint scope (sources, docs and
        // release-facing documents) for it; the other rules need the exact release set
        const scope = scopeFiles({ docs: true })
        for (const f of scope) {
          let text = null
          try {
            const buf = readFileSync(join(ROOT, f))
            if (!isBinaryPath(f) && !buf.includes(0)) text = buf.toString('utf8')
          } catch {
            continue
          }
          if (text !== null) checkEntry(f, 0, text, [], R, deploy)
        }
        R.note(`REL-04 only: scanned ${scope.length} files of the lint scope for the deployment secret`)
        R.finish({ files: scope.length, deploy_secret: true, scope: 'lint' })
        return
      }
      R.finish({ files: 0, deploy_secret: false })
      return
    }
    const secrets = localSecrets()
    let bytes = 0
    for (const f of files) {
      const abs = isAbsolute(f) ? f : join(ROOT, f)
      let st
      try {
        st = statSync(abs)
      } catch {
        continue // listed by git but deleted in the working tree
      }
      if (!st.isFile()) continue
      bytes += st.size
      let text = null
      if (!isBinaryPath(f) && st.size <= 8 * MAX_BYTES) {
        const buf = readFileSync(abs)
        if (!buf.includes(0)) text = buf.toString('utf8')
      }
      checkEntry(f, st.size, text, secrets, R, deploy)
    }
    R.finish({ files: files.length, bytes, local_secret_values: secrets.length, deploy_secret: Boolean(deploy) })
  })
}
