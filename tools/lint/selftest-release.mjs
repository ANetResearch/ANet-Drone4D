#!/usr/bin/env node
// Self test of the check-release license and attribution rules (REL-05..09; docs/05 §14, ADR-154, ADR-205; D3-AC-24, D3-AC-45,
// D3-AC-46) on synthetic inputs, plus two consistency checks on the working tree: the registered gz-sim fork files keep their
// headers, and NOTICE and THIRD_PARTY_NOTICES.md satisfy REL-08. Restricted markers (licence header lines, package names) are
// assembled at runtime so this file itself passes check-release.
// Usage: node tools/lint/selftest-release.mjs   (exit 0 when every expectation holds)
import { mkdtempSync, rmSync, writeFileSync, existsSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { ROOT } from './_common.mjs'
import {
  FORKS, GZ_HEADER, OSM_ODBL, PUBLIC_KEYS, REQUIRED, binaryKind, checkCopy, checkForkSet, checkLicenseEntry, checkNotices, imageRecipe,
  localSecrets, namesPackage, noticeInputs, requirementNames,
} from './check-release.mjs'
import { checkText as copyCheck } from './copy-lint.mjs'

class Collect {
  constructor() {
    this.v = []
  }
  add(file, line, col, rule, msg) {
    this.v.push({ file, line, col, rule, msg })
  }
  note() {}
}
let failures = 0
let cases = 0
function expect(name, got, want) {
  cases++
  const ok = JSON.stringify(got) === JSON.stringify(want)
  if (!ok) {
    failures++
    console.error(`FAIL ${name}: got ${JSON.stringify(got)} want ${JSON.stringify(want)}`)
  }
}
const collect = (fn) => {
  const R = new Collect()
  fn(R)
  return R.v
}
const rules = (fn) => collect(fn).map((x) => x.rule)
const lic = (f, text, head = null) => rules((R) => checkLicenseEntry(f, text, R, head))
const j = (...p) => p.join('')

// restricted names, assembled so that no line of this file matches the rules it tests
const LIVO = j('FAST', '-LIVO2')
const LIVMAPPER = j('LIV', 'Mapper')
const NVDR = j('nvdiff', 'rast')
const INRIA_RAST = j('diff-gaussian', '-rasterization')
const KNN = j('simple', '-knn')
const FLUX_DEV = j('FLUX.1', '-dev')
const ELF = Buffer.from([0x7f, 0x45, 0x4c, 0x46, 2, 1, 1, 0])
const GZ_HEAD = ['/*', GZ_HEADER[0], ' *', GZ_HEADER[1], ' * you may not use this file except in compliance with the License.', ' * You may obtain a copy of the License at',
  ' *', GZ_HEADER[2], ' *', ' * limitations under the License.', ' *']

// ---- REL-05 FAST-LIVO2 only as a separate container ------------------------------------------------------------------------------
expect('livo source tree', lic(`third_party/${LIVO}/src/main.cpp`, 'int main() {}'), ['REL-05'])
expect('livo vikit tree', lic(`native/slam/${j('rpg', '_vikit')}/vikit_common/src/x.cpp`, 'int x;'), ['REL-05'])
expect('livo mapper file', lic(`native/slam/${LIVMAPPER}.cpp`, 'int x;'), ['REL-05'])
expect('livo include', lic('native/slam/bridge.cpp', `#include <${j('vik', 'it')}/camera_loader.h>\nint x;\n`), ['REL-05'])
expect('livo cmake project', lic('native/slam/CMakeLists.txt', `cmake_minimum_required(VERSION 3.16)\nproject(${j('fast', '_livo')})\n`), ['REL-05'])
expect('livo package.xml', lic('native/slam/package.xml', `<package><name>${j('fast', '_livo')}</name></package>`), ['REL-05'])
expect('livo mentioned in python and docs is fine', [
  ...lic('python/awr/sensorsvc/slam/bag.py', `"""${LIVO} expects g for /livox/imu; ${LIVMAPPER} reads the bag."""\n`),
  ...lic('docs/research/d3/r1.md', `${LIVO} ${LIVMAPPER} #include <${j('vik', 'it')}/x.h>`),
], [])
expect('livo submodule', lic('.gitmodules', `[submodule "x"]\n\tpath = ext/x\n\turl = https://github.com/${j('hku-mars/', LIVO)}.git\n`), ['REL-05'])
expect('livo linked binary', lic('tools/bin/slam_bridge', null, Buffer.concat([ELF, Buffer.from(`..._ZN${LIVMAPPER}4initEv...`, 'latin1')])), ['REL-05'])
expect('clean elf', lic('tools/bin/other', null, Buffer.concat([ELF, Buffer.from('plain symbols', 'latin1')])), [])
expect('marker in a non-binary blob is not a link product', lic('assets/x.bin', null, Buffer.from(`PNG..${LIVMAPPER}`, 'latin1')), [])
expect('binary kinds', [binaryKind(ELF), binaryKind(Buffer.from('!<arch>\nxxxx', 'latin1')), binaryKind(Buffer.from([0xcf, 0xfa, 0xed, 0xfe])),
  binaryKind(Buffer.from([0, 0x61, 0x73, 0x6d, 1, 0, 0, 0])), binaryKind(Buffer.from('text')), binaryKind(null)], ['elf', 'ar', 'macho', 'wasm', null, null])

// ---- REL-06 gz-sim fork headers ----------------------------------------------------------------------------------------------
{
  const fork = FORKS.find((x) => x.path.endsWith('/Common.cc'))
  const good = [...GZ_HEAD, ' * Drone4D modifications (M24 §6.5.4): namespace renamed.', ' */', '#include "Common.hh"', ''].join('\n')
  expect('fork header kept', lic(fork.path, good), [])
  expect('fork header removed', lic(fork.path, good.replace(GZ_HEADER[0], ' * Copyright 2026 Drone4D')), ['REL-06'])
  expect('fork licence notice removed', lic(fork.path, good.replace(GZ_HEADER[1], ' * Licensed under some terms')), ['REL-06'])
  expect('fork modification notice missing', lic(fork.path, good.replace(' * Drone4D modifications (M24 §6.5.4): namespace renamed.\n', '')), ['REL-06'])
  expect('fork header pushed below the first lines', lic(fork.path, '\n'.repeat(60) + good), ['REL-06', 'REL-06'])
  expect('unregistered osrf header', lic('native/gz/src/NewSystem.cc', good), ['REL-06'])
  expect('unregistered fork namespace without header', lic('native/gz/src/Copy.cc', `namespace ${j('d4d_', 'multicopter_control')} {}\n`), ['REL-06'])
  expect('original work is fine', lic('native/gz/src/Drone4DClock.cc', '/*\n * Copyright 2026 Drone4D. Licensed under the Apache License, Version 2.0.\n */\n'), [])
  const lf = FORKS.find((x) => x.path.endsWith('/LICENSE'))
  expect('x500 licence kept', lic(lf.path, `${lf.header[0]}\n\n${lf.header[1]}\nAll rights reserved.\n`), [])
  expect('x500 licence replaced', lic(lf.path, 'MIT License\n'), ['REL-06'])
  const third = FORKS.map((x) => `\`${x.path.replace(/\/LICENSE$/, '/')}\``).join(', ')
  const set = FORKS.map((x) => x.path)
  expect('fork set complete', rules((R) => checkForkSet(set, third, R)), [])
  expect('fork file missing from the set', rules((R) => checkForkSet(set.slice(1), third, R)), ['REL-06'])
  expect('fork file not listed in notices', rules((R) => checkForkSet(set, third.replace(`\`${set[0]}\``, ''), R)), ['REL-06'])
}

// ---- REL-07 research-only components ----------------------------------------------------------------------------------------
{
  const DF = 'tools/deploy/dmax/images/sensorsvc/Dockerfile'
  expect('product recipe kinds', [imageRecipe(DF), imageRecipe('tools/deploy/dmax/images/gz/build.sh'),
    imageRecipe('tools/deploy/dmax/images/sensorsvc/requirements-gpu.lock'), imageRecipe('requirements-d3.in'), imageRecipe('pyproject.toml'),
    imageRecipe('apps/web/package.json'), imageRecipe('docs/research/d3/Dockerfile'), imageRecipe('tools/deploy/dmax/images/sky/Dockerfile'),
    imageRecipe('tools/deploy/dmax/images/livo/Dockerfile'), imageRecipe('tools/deploy/dmax/images/r3-camera-render/Dockerfile'),
    imageRecipe('tools/deploy/dmax/images/x/Dockerfile', 'FROM a\nLABEL awr.image.scope="research"\n')],
  [true, true, true, true, true, true, false, false, false, false, false])
  expect('product dockerfile installs nvdiffrast', lic(DF, `FROM python:3.12-slim\nRUN pip install ${NVDR}==0.4.0\n`), ['REL-07'])
  expect('product dockerfile comment is fine', lic(DF, `FROM python:3.12-slim\n# ${NVDR} is not used; the camera uses moderngl\nRUN true # no ${KNN}\n`), [])
  expect('research image may use it', lic('tools/deploy/dmax/images/sky/Dockerfile', `RUN pip install ${NVDR} ${INRIA_RAST} ${KNN}\n`), [])
  expect('labelled research image may use it', lic('tools/deploy/dmax/images/x/Dockerfile', `FROM a\nLABEL awr.image.scope="research"\nRUN pip install ${NVDR}\n`), [])
  expect('product lock with the Inria rasterizer', lic('tools/deploy/dmax/images/sensorsvc/requirements-gpu.lock', `${INRIA_RAST}==1.0 \\\n    --hash=sha256:00\n`), ['REL-07'])
  expect('pyproject with simple-knn', lic('pyproject.toml', `[project]\ndependencies = ["${KNN}>=0.1"]\n`), ['REL-07'])
  expect('package.json with FLUX.1 dev', lic('tools/navsvc/package.json', `{"dependencies": {"x": "1"}, "model": "${FLUX_DEV}"}`), ['REL-07'])
  expect('schnell is not dev', lic('tools/navsvc/package.json', '{"model": "FLUX.1-schnell"}'), [])
  expect('product image clones FAST-LIVO2', lic('tools/deploy/dmax/images/gz/Dockerfile', `RUN git clone https://github.com/hku-mars/${LIVO}\n`), ['REL-07'])
  expect('livo image may build it', lic('tools/deploy/dmax/images/livo/Dockerfile', `RUN git clone https://github.com/hku-mars/${LIVO}\n`), [])
  expect('product package imports nvdiffrast', lic('python/awr/sensorsvc/camera/x.py', `import numpy as np\nimport ${NVDR}.torch as dr\n`), ['REL-07'])
  expect('research script import is not product code', lic('tests/d3/research/x.py', `import ${NVDR}.torch as dr\n`), [])
  expect('vendored research source', lic(`third_party/${NVDR}/ops.py`, 'x = 1\n'), ['REL-07'])
  expect('vendored Inria code', lic('python/awr/gs/train.py', `#\n# Copyright (C) 2023, Inria\n# ${j('GRAPHDECO', ' research group')}, https://team.inria.fr/graphdeco\n#\n`), ['REL-07'])
  expect('NVIDIA source code licence header', lic('native/x/raster.cu', `// ${j('NVIDIA Source Code', ' License')} (1-Way Commercial)\n`), ['REL-07'])
}

// ---- REL-08 attributions ----------------------------------------------------------------------------------------------------
{
  const manifest = { files: [
    { name: 'w/a.glb', license: 'CC-BY-4.0', author: 'Ana (https://sketchfab.com/ana)', title: 'A town', source_url: 'https://sketchfab.com/3d-models/a-1' },
    { name: 'osm/a.osm', license: 'ODbL-1.0', author: 'OpenStreetMap contributors', title: 'OSM export', source_url: 'https://api.openstreetmap.org/x' },
    { name: 'dem/N00E000.hgt', license: 'public-domain', author: 'USGS', title: 'tile', source_url: 'https://x' },
  ] }
  const req = (k) => REQUIRED.flatMap((x) => x[k])
  const notice = ['"A town" by Ana (https://sketchfab.com/ana), https://sketchfab.com/3d-models/a-1', ...OSM_ODBL, ...req('notice')].join('\n')
  const third = ['| "A town" | Ana (https://sketchfab.com/ana) | https://sketchfab.com/3d-models/a-1 | CC-BY-4.0 |', ...OSM_ODBL, '`osm/a.osm`',
    '`N00E000.hgt`', ...req('third'), '| @recast-navigation/core | 0.43.1 | MIT |', '| open3d-cpu | MIT |', '| av (PyAV) | BSD-3-Clause |'].join('\n')
  const packages = [{ name: 'open3d_cpu', from: 'requirements-d3.in' }, { name: 'av', from: 'requirements-d3.in' },
    { name: '@recast-navigation/core', from: 'tools/navsvc/package.json' }]
  const run = (o) => collect((R) => checkNotices({ notice, third, manifest, packages, recast: '0.43.1', ...o }, R))
  expect('notices complete', run({}).map((x) => x.rule), [])
  expect('cc-by author missing from notice', run({ notice: notice.replace('by Ana', 'by someone') }).map((x) => `${x.file} ${x.rule}`), ['NOTICE REL-08'])
  expect('cc-by source missing from third', run({ third: third.replace('| https://sketchfab.com/3d-models/a-1 ', '| ') }).map((x) => `${x.file} ${x.rule}`),
    ['THIRD_PARTY_NOTICES.md REL-08'])
  expect('odbl statement missing', run({ notice: notice.split('\n').filter((l) => l !== OSM_ODBL[1]).join('\n') }).length > 0, true)
  expect('dem tile not registered', run({ third: third.replace('`N00E000.hgt`', '') }).length, 1)
  expect('livo version missing', run({ third: third.replaceAll('main@0d2c034', 'main') }).length, 1)
  expect('recast version drift', run({ recast: '0.44.0' }).map((x) => x.rule), ['REL-08'])
  expect('unregistered package', run({ packages: [...packages, { name: 'warp-lang', from: 'tools/deploy/dmax/images/sensorsvc/requirements-gpu.in' }] })
    .map((x) => x.msg.startsWith('package warp-lang')), [true])
  expect('package tokens', [namesPackage('| av (PyAV) |', 'av'), namesPackage('java', 'av'), namesPackage('| open3d-cpu |', 'open3d_cpu'),
    namesPackage('nvidia-nvimgcodec-cu12 (nvImageCodec)', 'nvidia-nvimgcodec-cu12')], [true, false, true, true])
  expect('requirement names', requirementNames('# c\npillow==11.3.0\nnvidia-nvimgcodec-cu12[nvjpeg]==0.9.0.20\n-r other.in\n  rerun-sdk  # viewer\n\n'),
    ['pillow', 'nvidia-nvimgcodec-cu12', 'rerun-sdk'])
}

// ---- REL-09 copy-lint inside REL --------------------------------------------------------------------------------------------
{
  const raw = '# 标题\n\n这个功能超好用！\n'
  const scope = [{ path: 'docs/x.zh.md', lang: 'zh', kind: 'md', raw }]
  const empty = { exceptions: [], allow: [] }
  const found = copyCheck('docs/x.zh.md', raw, { lang: 'zh', kind: 'md' })
  expect('copy fixture has findings', found.length > 0, true)
  const got = collect((R) => checkCopy(R, { scope, reg: empty }))
  expect('copy findings fail REL', [got.length === found.length, got.every((x) => x.rule === 'REL-09' && x.msg.startsWith('copy-lint COPY-'))], [true, true])
  const reg = { exceptions: found.map((f) => ({ file: f.file, rule: f.rule, match: f.match, anchor: f.anchor, reason: '测试', until: 'P0b' })), allow: [] }
  expect('baseline excuses them', rules((R) => checkCopy(R, { scope, reg })), [])
  expect('strict fails on remaining exceptions', rules((R) => checkCopy(R, { scope, reg, strict: true })), ['REL-09'])
  const stale = { exceptions: [...reg.exceptions, { file: 'docs/x.zh.md', rule: 'COPY-01', match: '不存在的短语', reason: '测试', until: 'P0b' }], allow: [] }
  expect('stale register entry fails', rules((R) => checkCopy(R, { scope, reg: stale })), ['REL-09'])
  expect('clean copy passes', rules((R) => checkCopy(R, { scope: [{ path: 'docs/y.md', lang: 'en', kind: 'md', raw: '# Export dataset\n\nThe export writes one file per run.\n' }], reg: empty })), [])
}

// ---- REL-03 public identifiers ------------------------------------------------------------------------------------------------
{
  const dir = mkdtempSync(join(tmpdir(), 'awr-rel03-'))
  try {
    const v = 'v' + Math.random().toString(36).slice(2, 12) + 'Q7'
    writeFileSync(join(dir, 'svc.env'), `SVC_IMAGE_MODEL=public-model-id-001\nSVC_MODEL_ID="public-model-id-002"\nSVC_API_KEY=${v}xxxx\n`)
    expect('model ids are not secrets', localSecrets(dir).map((s) => s.key), ['SVC_API_KEY'])
    expect('public key pattern', ['A_MODEL', 'MODEL', 'A_MODELS', 'A_MODEL_ID', 'A_MODEL_KEY', 'MODELKEY'].map((k) => PUBLIC_KEYS.test(k)), [true, true, true, true, false, false])
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
}

// ---- working tree consistency -----------------------------------------------------------------------------------------------
{
  const got = collect((R) => {
    for (const x of FORKS) {
      const p = join(ROOT, x.path)
      if (!existsSync(p)) R.add(x.path, 1, 1, 'MISSING', 'registered fork file does not exist')
      else checkLicenseEntry(x.path, readFileSync(p, 'utf8'), R)
    }
  })
  expect('registered fork files keep their headers', got.map((x) => `${x.file} ${x.rule}`), [])
  const files = ['tools/deploy/dmax/images/sensorsvc/requirements-gpu.in', 'tools/deploy/dmax/images/sensorsvc/requirements-nvjpeg.in', 'tools/navsvc/package.json']
    .filter((f) => existsSync(join(ROOT, f)))
  const inputs = noticeInputs(files)
  expect('notices of the working tree pass REL-08', collect((R) => checkNotices(inputs, R)).map((x) => `${x.file}: ${x.msg}`), [])
  expect('manifest attribution entries are checked', (inputs.manifest.files ?? []).filter((e) => /^(CC-BY|ODbL)/.test(e.license ?? '')).length >= 4, true)
}

if (failures) {
  console.error(`release selftest: ${failures} of ${cases} failed`)
  process.exit(1)
}
console.log(`release selftest: ok (${cases} cases)`)
