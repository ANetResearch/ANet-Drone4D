# Contributing to ANet-Drone4D

Thanks for your interest! ANet-Drone4D is early (V0.1, the D1 release) and moving fast. It is a real-world grounded 4D
world runtime: the browser shows the world, the server computes it, and the World is the core asset.

## Ground rules

- **Discuss first** for anything that changes a contract: file formats (World Package, ANET_Q16, AWRV, recordings),
  the realtime protocol `awr.rt.v1` and the REST API, reason codes, bus keys and unit suffixes. Open an issue before a
  PR. The contracts in `packages/contracts/` are the single source of truth, and saved worlds and recordings outlive
  any one version.
- Bug fixes, docs, tests, portability fixes and performance reports from real GPUs are always welcome.
- **Design before code.** Behaviour is specified in `docs/` first. A change to specified behaviour updates the
  defining document in the same PR (see [Documentation](#documentation)).
- No dataset files, generated worlds, recordings or secrets in commits (see [Data and secrets](#data-and-secrets)).

## Development

Linux x86-64 (Ubuntu 22.04 or 24.04), Python 3.12, Node 22.12 (`.nvmrc`), make and git. No GPU is needed.

```sh
make setup        # locked install: uv pip sync requirements.lock, npm ci, generated-file checks
make fetch-data   # download UrbanScene3D; read its terms first (THIRD_PARTY_NOTICES.md, section 4)
make worlds       # build the six city worlds locally into worlds/ (never committed)
make dev          # start with Vite on :5173 (profile dev); `make run` serves the production build on :8000
make ci           # the merge gate
```

- **`make ci` must pass before a PR is merged.** It is the full local gate: `make lint` (every rule below), the contract
  checks, `tsc`, a production `vite build`, pytest without the `perf` marker (including `slow` and `needs_data`, so it
  needs the built worlds), and Vitest (unit and browser). Browser tests need Chrome for Testing 151 (`PW_CHROME`).
- **GitHub Actions runs the no-data subset on every push and PR** (`.github/workflows/ci.yml`, gate G1h): `make setup`,
  `make lint`, `make test-contracts`, `make typecheck`, pytest `-m "not perf and not slow and not needs_data"`, Vitest
  unit, `make build`, then `make demo-world`, its deep validation and the tests that need only that world. It needs no
  GPU, browser or UrbanScene3D data; `make ci-nodata` runs the same steps up to the build locally. A test that reads
  `data/raw/` or `worlds/` must carry the `needs_data` marker (or skip itself when the data is missing), otherwise it
  fails there.
- `.venv/bin/pre-commit install` enables the fast subset (ruff and oxlint) on each commit.
- Performance and chaos cases (`make perf CASE=<id>`, `make chaos`) are not in the merge gate. They hold an exclusive
  lock and follow the performance run protocol in `docs/18-性能与测试方案.md`; attach their report when a change
  claims a speed-up.
- Dependencies are pinned to exact versions. Adding, removing or upgrading one goes in its own PR: regenerate
  `requirements.lock` with `make lock` (`make lock-check` must pass) or `package-lock.json` with npm, and give the
  package's license.

## Code conventions

- **Python**: ruff (rule set PY-RUFF-01, line length 120); the `awr` package lives in `python/awr/`. Bus callbacks only
  enqueue work (PY-CB-01); import boundaries are in `tools/lint/py-imports.toml`.
- **TypeScript**: oxlint with type-aware rules. Import boundaries are checked: `@base-ui/react` only inside
  `ui/components/ui/`, `@msgpack/msgpack` only in `net/`, and `engine/**` and `net/**` stay free of React, zustand and
  UI code.
- **Units and frames**: every wire field and identifier that carries a unit ends in its suffix (`_m`, `_mps`, `_ms`,
  `_ns`, `_deg`, ...), from `packages/contracts/rt/units.json` (UNIT-01). World coordinates are ENU metres, Z-up; time
  is int64 `t_sim_ns`; quaternions are `[x, y, z, w]` (`docs/03-设计基线与决策记录.md` §5).
- **Contracts**: edit the schema in `packages/contracts/`, run `make contracts` to regenerate `python/awr/contracts/`
  and `packages/contracts/gen/ts/`, and never edit generated files by hand. `make test-contracts` checks them.
- Match the language of the file you edit: TypeScript comments are in English, many Python docstrings are in Chinese.

**Visual and UI rules**, enforced by `make lint` (full list in `docs/18-性能与测试方案.md` §13, design in
`docs/15-视觉设计规范与色卡.md`):

| Rule | What it rejects |
|---|---|
| EMOJI-01, GLYPH-01 | Emoji, U+FE0F, and symbol glyphs in U+2194 to U+21FF and U+25A0 to U+27BF (check marks, crosses, stars, dots, triangles) in code, docs, UI strings and reports. Write status as words. Arrows U+2190 to U+2193 and mathematical symbols are allowed. |
| VIS-L-01, VIS-L-02 | Hex colour literals outside the token files (`apps/web/src/styles/theme.css` and the generated token files) and CSS colour keywords in `apps/web/src`. Use the ANet Graphite tokens: technology grey, black and white, with red reserved for the one element that needs attention. |
| VIS-L-03 | `backdrop-filter` and backdrop blur. |
| MOT-01 to MOT-04 | Duration and easing literals (use the Transitions.dev motion tokens), more than two resident infinite loops, `transition-all` outside the shadcn sources. |
| ICON-01 to ICON-04 | `lucide-react`, namespace imports of `lucide`, alias icon names and icon keys missing from `ui/icons/registry.ts`. Icons are lucide geometry, morphed with morphicons. |
| RAW-01 | Native `button`, `input`, `select`, `textarea` and `dialog` elements or home-made overlays outside `ui/components/ui/`. UI components come from shadcn/ui (base-mira on Base UI). |
| BRAND-01 to BRAND-03 | Changed or recoloured files in `apps/web/public/brand/` (checked against `brand.lock.json`), brand marks outside their approved places, avatars loaded from `avatars.githubusercontent.com`. |
| LF-TXT-01, LF-CHART-01, LF-PAL-01 to LF-PAL-05 | Arbitrary font sizes, spacing and radii outside the shadcn sources; `Math.random` and full rebuilds in chart code; palette values that drift from `docs/15`. |

## Documentation

- Design documents in `docs/` are written in Chinese, with technical terms in English (`docs/03-设计基线与决策记录.md`
  §10.2). Top-level files such as this one are in English. Issues and PRs may be in English or Chinese.
- Every fact has one defining document, and the others link to it; `docs/README.md` is the map. Conflicts resolve in
  this order: the user requirements R1 to R4, the baseline `docs/03-设计基线与决策记录.md`, then the defining document.
- Protected design intent (the World is the core, the Reality to Agent chain, ANet as a collaboration plane rather
  than a control plane, and the others in `docs/03` §2.5) changes only through a new ADR in `docs/03`.
- `docs/impl/` holds the implementation reports. Describe what is implemented from them, not from the design
  documents.
- Diagrams are mermaid with the Graphite theme snippet from `docs/15`. `docs/01-design.md` is the original design and
  is read-only.

## Path ownership

Every path has exactly one owner module (M00 to M16), listed in `docs/03-设计基线与决策记录.md` §4.3; it works like
CODEOWNERS. For example, M00 owns the `Makefile`, the lock files, `packages/contracts/`, `tools/lint/` and
`tools/ci/`; M11 owns `python/awr/runtime/`, `python/awr/api/` and `apps/web/src/net/`; M15 owns
`apps/web/src/{app,ui,lib,styles}/` and `apps/web/public/brand/`.

- Keep a PR inside one owner's paths where you can, and name the owner module in the PR description. Changes to
  another module's paths need that owner's review.
- Extend through the extension points rather than editing shared files: `mk/<module>.mk` for make targets,
  `@register_stage` for simulation stages, `python/awr/api/rest/<domain>.py` for REST routers,
  `ui/panels/registry.ts` for panels and `viewport/layers/registry.ts` for render layers.
- Tests go in the module's own directory (`tests/<module>/`, `apps/web/tests/<module>/`); cross-module tests go in
  `tests/e2e/`.
- Module branches are named `m<nn>/<topic>`, for example `m05/pointpool`.

## Data and secrets

- Never commit UrbanScene3D files, generated worlds (`worlds/`), recordings and run directories (`runs/`), reference
  checkouts (`refs/`) or `data/raw/`. `.gitignore` covers them; do not force-add them. UrbanScene3D is for
  non-commercial use only and may not be redistributed, in original or altered form.
- Tests that need the dataset read it at run time and carry the `needs_data` marker. Do not copy dataset excerpts
  into fixtures or golden files.
- Never commit secrets. Run secrets live in `runs/<run>/secret` and `runs/<run>/admin.token` (mode 0600);
  `configs/secrets/` and `.env.local` are ignored. Mask tokens before pasting logs (`awr config print` masks them for
  you).

## Licensing of contributions

ANet-Drone4D is released under the ANet Open Source License (ANet-Drone4D), a modified Apache License 2.0 (see
[LICENSE](LICENSE)). By submitting a contribution you agree that it is provided under that License, including its
contributor terms (LICENSE, condition 2): Agent Network Research may make the license stricter or more relaxed, and
may use your contribution commercially, including in its hosted simulation and digital twin services and cloud
operations; and you have the right to submit it on these terms.

If your change brings in third-party code, data or assets, name the source and its license in the PR and add an entry
to [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Do not copy code from projects under GPL, AGPL or non-commercial
licenses: port the idea, write your own code, and say where the idea came from.

## Security issues

Please do **not** open public issues for vulnerabilities; see [SECURITY.md](SECURITY.md).

## 中文速览

- 合并前本地 `make ci` 必须通过；性能用例不进合并门禁，按 18 号文档的性能运行协议执行。
- 文档以中文为主、技术名词保留英文；同一事实只在定义方文档中定义，改行为先改文档。
- 严禁 emoji 与禁用字形；颜色只用 ANet Graphite token，token 文件之外不得出现十六进制色值；图标、动效、组件分别走
  morphicons 与 lucide 注册表、Transitions.dev token、shadcn/ui。
- 每个路径只有一个所有者模块（`docs/03` §4.3），尽量不跨模块修改；不得提交 UrbanScene3D 数据、世界包、运行目录与任何秘密。
