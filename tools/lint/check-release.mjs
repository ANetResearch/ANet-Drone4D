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
// License and attribution rules (docs/05 §14, ADR-154, ADR-205; D3-AC-24, D3-AC-45, D3-AC-46):
//   REL-05 FAST-LIVO2 (GPL-2.0) runs only as a separate container: no FAST-LIVO2 or rpg_vikit source tree, no C/C++ or build
//          file that includes or defines its code, no binary linked with it, no submodule pointing at it
//   REL-06 gz-sim fork files (FORKS) keep the original copyright header, state the Drone4D modifications under it and are
//          listed in THIRD_PARTY_NOTICES.md; a file with an Open Source Robotics Foundation header or the fork namespace
//          that is not registered is reported (register it, or restore its header)
//   REL-07 research-only components (nvdiffrast, the Inria 3DGS rasterizer and simple-knn, Skyfall-GS, FLUX.1 [dev]) and
//          FAST-LIVO2 stay out of product image recipes (Dockerfiles, requirements and build scripts under tools/deploy/,
//          pyproject.toml, requirements*.in/lock, package.json) and out of the product package python/; research and
//          internal-only images (an images/<dir> named sky, livo, research-* or r<N>-*, or a Dockerfile labelled
//          awr.image.scope=research|internal) are exempt; vendored copies of their source are reported anywhere
//   REL-08 NOTICE and THIRD_PARTY_NOTICES.md carry the attributions: every CC-BY entry of configs/worlds.d3/raw-manifest.json
//          (title, author, source, license), the OSM ODbL statement, the elevation credits, the D3 components of REQUIRED
//          (hill-01, gz-sim fork, x500, FAST-LIVO2 with its version, nvdiffrast, Skyfall-GS, FLUX.1 [dev], recast-navigation
//          at tools/worldglb/RECAST_VERSION, MARS-LVIG) and every package named in requirements-d3.in, the image
//          requirements-*.in files and tools/navsvc/package.json (§14: new components are registered before they merge)
//   REL-09 copy-lint (docs/05 §7.10) over its scope with its baseline register; with --copy-strict a remaining baseline
//          exception fails as well (P0b exit, D3-AC-23)
//   REL-06 (registered files present), REL-08 and REL-09 look at the whole release set and run only when it comes from git;
//   with --files only the per-file rules run.
// Usage: node tools/lint/check-release.mjs [--files <list-file>] [--secret-file <path>] [--copy-strict] [--no-copy]
//   the list file holds one path per line (repository-relative or absolute); --secret-file overrides the deployment secret
//   file for REL-04 (tests use a temporary file with a random value); --no-copy skips REL-09
import { execFileSync } from 'node:child_process'
import { closeSync, existsSync, openSync, readFileSync, readSync, readdirSync, statSync } from 'node:fs'
import { homedir } from 'node:os'
import { isAbsolute, join } from 'node:path'
import { ROOT, Reporter, isBinaryPath, isMain, lineCol, readText, run, scopeFiles } from './_common.mjs'
import { applyRegister, checkText as copyCheck, loadRegister as loadCopyRegister, loadScope as loadCopyScope, BASELINE_FILE as COPY_BASELINE,
  RULES as COPY_RULES } from './copy-lint.mjs'

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

// keys whose values are public identifiers, not credentials: model ids (for example an image model id) are named in
// provenance records and illustration manifests; a value that also serves another key is still compared under that key
export const PUBLIC_KEYS = /(^|_)(MODEL|MODELS|MODEL_ID)$/
/** values of ~/.config/awr/*.env (KEY=VALUE lines, quotes stripped), at least 12 characters, except PUBLIC_KEYS; never printed */
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
      if (v.length >= 12 && !PUBLIC_KEYS.test(m[1])) out.push({ file: n, key: m[1], value: v })
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

// ---- license and attribution (REL-05..09; docs/05 §14, ADR-154, ADR-205; D3-AC-24, D3-AC-45, D3-AC-46) ------------------------
const NATIVE = /\.(c|cc|cpp|cxx|h|hh|hpp|hxx|cu|cuh|inl|ipp)$/i
const BUILD = /(^|\/)(CMakeLists\.txt|package\.xml|[^/]+\.cmake|[^/]+\.launch)$/i
const FORK_SCAN = /\.(c|cc|cpp|cxx|h|hh|hpp|hxx|proto|sdf|config)$/i
const segs = (f) => f.split('/')
const base = (f) => f.slice(f.lastIndexOf('/') + 1)

/** kind of a compiled artefact from its first bytes (ELF, static archive, Mach-O, WebAssembly), or null */
export function binaryKind(buf) {
  if (!buf || buf.length < 4) return null
  const m = buf.readUInt32BE(0)
  if (m === 0x7f454c46) return 'elf'
  if (buf.length >= 8 && buf.subarray(0, 8).toString('latin1') === '!<arch>\n') return 'ar'
  if ([0xfeedface, 0xfeedfacf, 0xcefaedfe, 0xcffaedfe, 0xcafebabe].includes(m)) return 'macho'
  if (m === 0x0061736d) return 'wasm'
  return null
}

// REL-05 FAST-LIVO2 (GPL-2.0; R-D3-33, ADR-143): only as a separate container, never as source or a linked artefact
const LIVO_DIR = /^(fast[-_]?livo2?|rpg[-_]vikit|vikit[-_](common|ros|py))$/i
const LIVO_FILE = /^LIVMapper\.(cpp|cc|h|hpp)$/
const LIVO_SRC = [/\bLIVMapper\b/, /#\s*include\s*[<"]vikit\//, /\bproject\s*\(\s*fast_livo/i, /<name>\s*fast_livo/i, /\bvikit::/]
const LIVO_BIN = ['LIVMapper', 'fast_livo', '5vikit', 'vikit/'].map((s) => Buffer.from(s, 'latin1'))
const LIVO_URL = /github\.com[/:](hku-mars\/fast[-_]?livo|[\w.-]+\/rpg[-_]vikit)/i

// REL-06 gz-sim fork (§14: fork code keeps the original copyright header; Apache-2.0 Section 4(b): modified files say so)
const GZ = 'gz-sim tag gz-sim8_8.15.0, src/systems/multicopter_control/'
export const GZ_HEADER = [' * Copyright (C) 2019 Open Source Robotics Foundation', ' * Licensed under the Apache License, Version 2.0 (the "License");',
  ' *     http://www.apache.org/licenses/LICENSE-2.0']
export const FORKS = [
  ...['Common.cc', 'Common.hh', 'LeeVelocityController.cc', 'LeeVelocityController.hh', 'Parameters.hh'].map((n) => (
    { path: `native/gz/src/${n}`, upstream: GZ + n, header: GZ_HEADER, modified: true })),
  { path: 'native/gz/src/Drone4DVelocityControl.cc', upstream: GZ + 'MulticopterVelocityControl.cc', header: GZ_HEADER, modified: true },
  { path: 'native/gz/src/Drone4DVelocityControl.hh', upstream: GZ + 'MulticopterVelocityControl.hh', header: GZ_HEADER, modified: true },
  { path: 'native/gz/models/x500_d4d/LICENSE', upstream: 'PX4-gazebo-models x500_base', header: ['BSD 3-Clause License', 'Copyright (c) 2022, Rudis Laboratories'],
    modified: false },
]
const OSRF = /Copyright \(C\) \d{4}[^\n]{0,40}Open Source Robotics Foundation/
const FORK_NS = /\bd4d_multicopter_control\b/
const HEAD_LINES = 40

// REL-07 research-only components (§14: nvdiffrast, Skyfall-GS Inria code, FLUX.1 [dev]; FAST-LIVO2 only in its own container)
export const RESEARCH_TERMS = [
  [/nvdiffrast/i, 'nvdiffrast (NVIDIA Source Code License, research and evaluation only)'],
  [/diff[-_]gaussian[-_]rasteri[sz]ation/i, 'the Inria 3DGS rasterizer (Gaussian-Splatting License)'],
  [/simple[-_]knn/i, 'simple-knn (Gaussian-Splatting License)'],
  [/skyfall[-_]?gs/i, 'Skyfall-GS (Inria-derived code under the Gaussian-Splatting License)'],
  [/flux\.?1[-_ ]?\[?dev\b|flux1[-_]dev/i, 'FLUX.1 [dev] (non-commercial license)'],
  [/fast[-_]?livo|rpg[-_]vikit/i, 'FAST-LIVO2 (GPL-2.0, separate container only)'],
]
const RESEARCH_DIRS = /^(nvdiffrast|diff[-_]gaussian[-_]rasteri[sz]ation(-depth)?|simple[-_]knn|skyfall[-_]gs)$/i
const RESEARCH_IMAGE = /^(sky|livo|research(-.*)?|r\d+(-.*)?)$/i
const RESEARCH_LABEL = /^\s*LABEL\b[^\n]*\bawr\.image\.scope\s*=\s*"?(research|internal)"?/im
// marker lines of the research-only licences; assembled so that no source file of this tool carries them verbatim
const RESEARCH_HEADERS = [[['GRAPHDECO', 'research group'].join(' '), 'Inria 3DGS code (Gaussian-Splatting License)'],
  [['NVIDIA Source Code', 'License'].join(' '), 'code under the NVIDIA Source Code License (nvdiffrast)']]
const RESEARCH_IMPORT = /^\s*(?:import|from)\s+(nvdiffrast|diff_gaussian_rasterization|simple_knn)\b/m

/** a product image recipe: a container or dependency recipe of an image that is not research or internal-only */
export function imageRecipe(f, text = '') {
  const b = base(f)
  const deploy = f.startsWith('tools/deploy/')
  const recipe = (deploy && (/^(Dockerfile|Containerfile)(\..+)?$|\.(dockerfile|containerfile)$/i.test(b) || /^requirements[^/]*\.(in|lock|txt)$/.test(b)
    || (/\/images\//.test(f) && b.endsWith('.sh')))) || (!f.includes('/') && (b === 'pyproject.toml' || /^requirements[^/]*\.(in|lock|txt)$/.test(b)))
    || b === 'package.json'
  if (!recipe) return false
  const m = /(?:^|\/)images\/([^/]+)\//.exec(f)
  if (m && RESEARCH_IMAGE.test(m[1])) return false
  if (/^(Dockerfile|Containerfile)/i.test(b) && RESEARCH_LABEL.test(text)) return false
  return true
}
const stripHash = (text) => text.replace(/(^|\s)#[^\n]*/g, '$1')

/** per-file license rules of one release entry (pure apart from the reporter); head = first bytes of a non-text file */
export function checkLicenseEntry(f, text, R, head = null) {
  const parts = segs(f)
  // REL-05
  const livoDir = parts.slice(0, -1).find((s) => LIVO_DIR.test(s))
  if (livoDir || LIVO_FILE.test(base(f))) R.add(f, 1, 1, 'REL-05', `FAST-LIVO2 or rpg_vikit source (${livoDir ?? base(f)}) must not be in the repository; FAST-LIVO2 runs only as a separate container (GPL-2.0, ADR-143)`)
  if (text !== null && (NATIVE.test(f) || BUILD.test(f))) {
    for (const re of LIVO_SRC) {
      const m = re.exec(text)
      if (m) {
        const [line, col] = lineCol(text, m.index)
        R.add(f, line, col, 'REL-05', `includes or defines FAST-LIVO2 code ("${m[0]}"); exchange data with its container through files only`)
        break
      }
    }
  }
  if (text !== null && base(f) === '.gitmodules') {
    const m = LIVO_URL.exec(text)
    if (m) R.add(f, ...lineCol(text, m.index), 'REL-05', 'a submodule points at FAST-LIVO2 or rpg_vikit; build its container from the upstream at image build time instead')
  }
  if (head) {
    const kind = binaryKind(head)
    const hit = kind && LIVO_BIN.find((b) => head.includes(b))
    if (hit) R.add(f, 1, 1, 'REL-05', `${kind} artefact contains FAST-LIVO2 symbols ("${hit.toString('latin1')}"); linked FAST-LIVO2 binaries must not be published`)
  }
  // REL-06
  const fork = FORKS.find((x) => x.path === f)
  if (text !== null && fork) {
    const lines = text.split('\n')
    const head40 = lines.slice(0, HEAD_LINES).join('\n')
    const where = fork.path.endsWith('LICENSE') ? text : head40
    const miss = fork.header.filter((h) => !where.includes(h))
    if (miss.length) R.add(f, 1, 1, 'REL-06', `fork of ${fork.upstream}: the original copyright header is incomplete (missing "${miss[0].trim()}"); restore it unchanged`)
    if (fork.modified && !/Drone4D modifications/.test(head40)) R.add(f, 1, 1, 'REL-06', `fork of ${fork.upstream}: state the changes in a "Drone4D modifications" paragraph under the header (Apache-2.0 Section 4(b))`)
  } else if (text !== null && FORK_SCAN.test(f)) {
    const m = OSRF.exec(text)
    if (m) R.add(f, ...lineCol(text, m.index), 'REL-06', 'carries an Open Source Robotics Foundation copyright header but is not a registered fork; add it to FORKS in tools/lint/check-release.mjs and to THIRD_PARTY_NOTICES.md section 1')
    else if (FORK_NS.test(text)) R.add(f, 1, 1, 'REL-06', 'uses the gz-sim fork namespace but has no original copyright header; restore the header of the gz-sim file it was copied from')
  }
  // REL-07
  const rdir = parts.slice(0, -1).find((s) => RESEARCH_DIRS.test(s))
  if (rdir) R.add(f, 1, 1, 'REL-07', `vendored research-only component (${rdir}) must not be in the repository; it is used only in internal research images`)
  if (text === null) return
  if (imageRecipe(f, text)) {
    const body = f.endsWith('.json') ? text : stripHash(text)
    for (const [re, what] of RESEARCH_TERMS) {
      const m = re.exec(body)
      if (m) R.add(f, ...lineCol(body, m.index), 'REL-07', `product image recipe references ${what}; use it only in a research or internal-only image`)
    }
  }
  if (NATIVE.test(f) || f.endsWith('.py')) {
    for (const [mark, what] of RESEARCH_HEADERS) {
      const i = text.indexOf(mark)
      if (i >= 0) R.add(f, ...lineCol(text, i), 'REL-07', `${what} must not be in the repository; it is used only in internal research images`)
    }
  }
  if (f.startsWith('python/') && f.endsWith('.py')) {
    const m = RESEARCH_IMPORT.exec(text)
    if (m) R.add(f, ...lineCol(text, m.index + m[0].indexOf(m[1])), 'REL-07', `the product package imports ${m[1]} (research-only); product images must not contain it`)
  }
}

/** the release set must contain every registered fork file, and THIRD_PARTY_NOTICES.md must list it (REL-06) */
export function checkForkSet(files, third, R) {
  const set = new Set(files)
  for (const x of FORKS) {
    if (!set.has(x.path)) R.add(x.path, 1, 1, 'REL-06', `registered fork file (${x.upstream}) is missing from the release set; update FORKS when a fork file moves`)
    if (!third.includes(x.path.replace(/\/LICENSE$/, '/'))) R.add('THIRD_PARTY_NOTICES.md', 1, 1, 'REL-06', `fork file ${x.path} (${x.upstream}) is not listed`)
  }
}

// REL-08 attributions. Each item: what must appear in NOTICE (notice) and in THIRD_PARTY_NOTICES.md (third)
export const OSM_ODBL = ['OpenStreetMap contributors', 'ODbL-1.0', 'https://opendatacommons.org/licenses/odbl/1-0/']
export const REQUIRED = [
  { id: 'CC BY 4.0 license link', notice: ['CC-BY-4.0', 'https://creativecommons.org/licenses/by/4.0/'], third: ['CC-BY-4.0', 'https://creativecommons.org/licenses/by/4.0/'] },
  { id: 'hill-01 source model (ADR-205)', notice: ['Dok11', 'https://sketchfab.com/dok11', 'https://sketchfab.com/3d-models/anciolina-3d-hilltop-village-scan-snowy-64ede430838142f5929edfc7eb371efc'],
    third: ['"Anciolina: 3D Hilltop Village Scan, Snowy"', 'Dok11', 'https://sketchfab.com/3d-models/anciolina-3d-hilltop-village-scan-snowy-64ede430838142f5929edfc7eb371efc'] },
  { id: 'modification statement (CC BY 4.0 Section 3(a)(1)(B))', notice: ['metric scaling'], third: ['Modified: metric scaling, levelling and derived layers'] },
  { id: 'OpenStreetMap ODbL', notice: OSM_ODBL, third: OSM_ODBL },
  { id: 'elevation credits', notice: ['courtesy of the U.S. Geological Survey', 'EU-DEM'], third: ['courtesy of the U.S. Geological Survey', 'EU-DEM', 'Terrain Tiles on AWS'] },
  { id: 'gz-sim fork', notice: ['gz-sim 8.15.0', 'Open Source Robotics Foundation', 'original copyright header'], third: ['gz-sim8_8.15.0', 'Open Source Robotics Foundation'] },
  { id: 'x500 model', notice: ['Rudis Laboratories'], third: ['Rudis Laboratories', 'BSD-3-Clause'] },
  { id: 'FAST-LIVO2 (separate container, version for released data)', notice: ['FAST-LIVO2', 'GPL-2.0', 'separate container', 'main@0d2c034'],
    third: ['FAST-LIVO2', 'GPL-2.0', 'main@0d2c034', 'rpg_vikit', 'generated by FAST-LIVO2'] },
  { id: 'nvdiffrast (research images only)', notice: ['nvdiffrast', 'NVIDIA Source Code License'], third: ['nvdiffrast', 'NVIDIA Source Code License', 'Section 3.3'] },
  { id: 'Skyfall-GS (research images only)', notice: ['Skyfall-GS', 'Gaussian-Splatting License'], third: ['Skyfall-GS', '7319484', 'Gaussian-Splatting License', 'gsplat'] },
  { id: 'FLUX.1 [dev] (internal research only)', notice: ['FLUX.1 [dev]'], third: ['FLUX.1 [dev]', 'Non-Commercial License'] },
  { id: 'recastnavigation (Zlib)', notice: [], third: ['recastnavigation', 'Zlib', 'Mikko Mononen'] },
  { id: 'MARS-LVIG (internal research only)', notice: [], third: ['MARS-LVIG', 'CC BY-NC-SA 4.0'] },
]
const authorName = (a) => a.replace(/\s*\(.*$/, '')
const normName = (s) => s.toLowerCase().replace(/[-_.]+/g, '-')
const escRe = (s) => s.replace(/[.*+?^${}()|[\]\\/]/g, '\\$&')
/** a package name appears as a whole token (case, "-", "_" and "." folded), so "av" does not match inside "java" */
export const namesPackage = (text, name) => new RegExp(`(^|[^a-z0-9-])${escRe(normName(name))}($|[^a-z0-9-])`).test(normName(text))

/** package names of a requirements .in file (comments, options and pins stripped) */
export function requirementNames(text) {
  const out = []
  for (const raw of text.split('\n')) {
    const line = raw.replace(/#.*/, '').trim()
    if (!line || line.startsWith('-')) continue
    const m = /^([A-Za-z0-9][A-Za-z0-9._-]*)/.exec(line)
    if (m) out.push(m[1])
  }
  return out
}

/** REL-08 over the two notice files. manifest: parsed raw-manifest.json; packages: [{ name, from }]; recast: version string */
export function checkNotices({ notice, third, manifest = { files: [] }, packages = [], recast = null }, R) {
  const need = (file, text, what, items) => {
    for (const s of items) if (!text.includes(s)) R.add(file, 1, 1, 'REL-08', `${what}: missing "${s}"`)
  }
  for (const e of manifest.files ?? []) {
    const lic = e.license ?? ''
    if (/^CC-BY/i.test(lic)) {
      need('THIRD_PARTY_NOTICES.md', third, `CC BY source ${e.name}`, [e.title, e.author, e.source_url, lic].filter(Boolean))
      need('NOTICE', notice, `CC BY source ${e.name}`, [e.title, authorName(e.author ?? ''), e.source_url].filter(Boolean))
    } else if (/^ODbL/i.test(lic)) {
      need('THIRD_PARTY_NOTICES.md', third, `ODbL source ${e.name}`, [...OSM_ODBL, base(e.name)])
      need('NOTICE', notice, `ODbL source ${e.name}`, OSM_ODBL)
    } else need('THIRD_PARTY_NOTICES.md', third, `source ${e.name} (${lic || 'no license'})`, [base(e.name)])
  }
  for (const x of REQUIRED) {
    need('NOTICE', notice, x.id, x.notice)
    need('THIRD_PARTY_NOTICES.md', third, x.id, x.third)
  }
  if (recast) need('THIRD_PARTY_NOTICES.md', third, 'recast-navigation version (tools/worldglb/RECAST_VERSION)', [`| ${recast} |`])
  for (const p of packages) if (!namesPackage(third, p.name)) R.add('THIRD_PARTY_NOTICES.md', 1, 1, 'REL-08', `package ${p.name} (${p.from}) is not registered; add it with its license (docs/05 §14)`)
}

/** inputs of checkNotices read from the working tree */
export function noticeInputs(files) {
  const read = (f) => (existsSync(join(ROOT, f)) ? readFileSync(join(ROOT, f), 'utf8') : '')
  const manifestText = read('configs/worlds.d3/raw-manifest.json')
  const packages = []
  const ins = ['requirements-d3.in', ...files.filter((f) => /^tools\/deploy\/.+\/images\/[^/]+\/requirements[^/]*\.in$/.test(f))]
  for (const f of new Set(ins)) for (const name of requirementNames(read(f))) packages.push({ name, from: f })
  for (const f of files.filter((x) => /^tools\/navsvc\/package\.json$/.test(x))) {
    const j = JSON.parse(read(f) || '{}')
    for (const name of Object.keys(j.dependencies ?? {})) packages.push({ name, from: f })
  }
  const recast = read('tools/worldglb/RECAST_VERSION').trim() || null
  return { notice: read('NOTICE'), third: read('THIRD_PARTY_NOTICES.md'), manifest: manifestText ? JSON.parse(manifestText) : { files: [] }, packages, recast }
}

/** REL-09: copy-lint over its scope (the D3-AC-46 release surface plus copy-lint.scope.json) with its register; strict adds the
 *  P0b exit condition (no baseline exception left). scope entries may carry raw text (tests). */
export function checkCopy(R, { strict = false, scope = loadCopyScope(), reg = loadCopyRegister() } = {}) {
  const findings = []
  const scanned = new Set()
  for (const e of scope) {
    const raw = e.raw ?? readText(e.path)
    if (raw === null) continue
    scanned.add(e.path)
    findings.push(...copyCheck(e.path, raw, { lang: e.lang, kind: e.kind, rules: COPY_RULES }))
  }
  const { reported, excused, problems } = applyRegister(findings, reg, { scannedFiles: scanned })
  for (const f of [...reported, ...problems]) R.add(f.file, f.line, f.col, 'REL-09', `copy-lint ${f.rule}: ${f.msg}`)
  if (strict && reg.exceptions.length) R.add(COPY_BASELINE, 1, 1, 'REL-09', `copy-lint: ${reg.exceptions.length} baseline exception(s) remain; the P0b exit requires none (D3-AC-23)`)
  return { files: scanned.size, excused: excused.length, baseline_exceptions: reg.exceptions.length }
}

/** the first bytes of a file (for binaryKind) */
function headBytes(abs, n) {
  const fd = openSync(abs, 'r')
  try {
    const buf = Buffer.alloc(n)
    return buf.subarray(0, readSync(fd, buf, 0, n, 0))
  } finally {
    closeSync(fd)
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
      let head = null
      if (!isBinaryPath(f) && st.size <= 8 * MAX_BYTES) {
        const buf = readFileSync(abs)
        if (!buf.includes(0)) text = buf.toString('utf8')
        else head = buf
      } else {
        head = headBytes(abs, 8)
        if (binaryKind(head) && st.size <= 8 * MAX_BYTES) head = readFileSync(abs)
      }
      checkEntry(f, st.size, text, secrets, R, deploy)
      // license rules match repository-relative paths (FORKS, product recipes); --files may list absolute paths
      const rf = isAbsolute(f) && f.startsWith(ROOT + '/') ? f.slice(ROOT.length + 1) : f
      checkLicenseEntry(rf, text, R, head && binaryKind(head) ? head : null)
    }
    const summary = { files: files.length, bytes, local_secret_values: secrets.length, deploy_secret: Boolean(deploy) }
    if (i < 0) {
      // whole-set rules: the release set came from git
      const inputs = noticeInputs(files)
      checkForkSet(files, inputs.third, R)
      checkNotices(inputs, R)
      summary.notices = { manifest_entries: inputs.manifest.files?.length ?? 0, packages: inputs.packages.length, forks: FORKS.length }
      if (!args.includes('--no-copy')) summary.copy = checkCopy(R, { strict: args.includes('--copy-strict') })
    } else R.note('--files: REL-06 (registered files present), REL-08 and REL-09 need the whole release set and were not run')
    R.finish(summary)
  })
}
