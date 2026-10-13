# Installation and requirements

This page lists the environment, ports, directories and configuration needed to run ANet Drone4D locally. The step-by-step procedure is
in {doc}`quickstart`.

## Server

| Item | Requirement | Notes |
|---|---|---|
| Operating system | Linux x86-64 (Ubuntu 22.04 or 24.04) | x86-64 only: the shared-memory StateRing relies on the x86 TSO memory order; `make setup` refuses other architectures with exit code 13 |
| Python | 3.12 (with the venv module) | `make setup` creates `.venv` with it and bootstraps uv 0.12.19 to install from `requirements.lock` |
| Node.js | 22.12.x (see `.nvmrc` at the repository root) | Frontend build and contract artifact checks; `make` prefers Node under `~/.local/node/bin` |
| Tools | make, git, curl | — |
| Disk | About 20 GB free | Dependencies about 2 GB; synthetic demo city about 100 MB; the six-city data and world packages another 2 GB or so; `runs/` quota 20 GB by default |
| Shared memory | At least 1 GiB free in `/dev/shm` | The state ring and run directories live in `/dev/shm/awr/<run>` |
| GPU | Not required | Physics, safety, missions, perception and environment are computed on the CPU |
| Network | The first installation needs PyPI and the npm registry | Afterwards it can run offline; `make setup OFFLINE=1` uses the local caches |

There is no hard minimum core count. On machines with 8 or more cores and a profile other than `ci`, the supervisor pins sim-core, api and
other processes to fixed cores by default (`cpu.pin: auto`); on a machine shared with other workloads, turn pinning off, see
{ref}`en-install-env`.

## Browser

Any modern browser with WebGL2 can connect. Desktop browsers get the full interface; tablets get a landscape commander layout; phones get
a five-tab layout (see {doc}`../guide/mobile`). With a hardware GPU the viewer can use higher quality tiers; without one it renders in
software and the quality controller lowers the point budget automatically.

## Ports

By default the server listens on the loopback address 127.0.0.1 only.

| Port | Process | Purpose |
|---|---|---|
| 8000 | api (show-api) | Frontend pages, the REST API `/api/...`, world static files, `awr.rt.v1` realtime connections |
| 8001 | api | Only when the static file split fallback is enabled |
| 8002 | sandbox-api | Sandbox REST v1 `/api/sandbox/v1/...` and sandbox realtime connections (`sandbox` profile) |
| 7447 | zenoh rendezvous | Inter-process bus, loopback only |
| 5173 | vite | Development server (`dev` and `sandbox` profiles) |

To run several instances on one machine, set `AWR_PORT_OFFSET=k` (0 to 31); all ports above shift by `10·k`. For example, with
`AWR_PORT_OFFSET=3` the api listens on 8030 and the rendezvous on 7477. Per-worktree overrides can go into `.env.local` at the repository
root (not committed); `make` reads and exports its variables.

## Directories

| Directory | Content | Committed |
|---|---|---|
| `worlds/` | Built World Packages (`make demo-world`, `make worlds`) | No |
| `data/raw/` | Raw data such as the UrbanScene3D sampled point clouds (`make fetch-data`) | No |
| `runs/` | Per-run logs, recordings, effective configuration and admin token (`runs/<run>/`) | No |
| `var/` | Local runtime state: sandbox pool keys and parked sessions, the lockout table of the internal identity | No |
| `apps/web/dist/` | Frontend production build | No |
| `.venv/` | Python dependencies | No |

The locations of `worlds/`, `data/raw/` and `runs/` can be changed with `AWR_WORLDS_DIR`, `AWR_DATA_DIR` and `AWR_RUNS_DIR`.

(en-install-env)=
## Environment variables

Configuration precedence: command line `--set key=value` > `AWR_*` environment variables > the profile in `configs/runtime.yaml` > the base
values in that file > built-in defaults. Unknown keys and type errors stop startup with exit code 2 and name the key path. Common variables:

| Variable | Effect | Default |
|---|---|---|
| `AWR_PROFILE` | Run profile (see the table below) | `demo` for `make run`, `dev` for `make dev` |
| `AWR_WORLD`, `AWR_SCENARIO` | World and scenario to start | Shenzhen and S1; falls back to synthcity and S0 when Shenzhen is not built and the synthetic demo city is |
| `AWR_PORT_OFFSET` | Port offset k (0 to 31, ports shift by 10·k) | 0 |
| `AWR_BIND`, `AWR_ORIGINS` | Listen address and allowed origins; set both for a LAN demo | `127.0.0.1`, empty |
| `AWR_CPU_PIN` | Core pinning: `auto`, `on`, `off` | `auto` |
| `AWR_SANDBOX_ENABLED`, `AWR_SANDBOX_PORT` | Sandbox switch and sandbox-api port | per profile; 8002 |
| `AWR_RUNS_QUOTA_GB` | Quota of `runs/` | 20 |

## Run profiles

| Profile | Process groups | Purpose |
|---|---|---|
| `demo` | core, ext | Default of `make run`: the shared show plus recording, the agent runtime and the job worker |
| `dev` | core, ext, dev | `make dev`: also starts the vite development server with hot reload |
| `sandbox` | core, ext, dev, sandbox | Local sandbox: also starts sandbox-pool and sandbox-api, quota profile `local` (no prefix quota and no proof of work), batch endpoints open |
| `ci`, `perf` | core, ext | Functional gate and performance cases (port offset 9) |
| `public` | core, sandbox | The public demo (see {doc}`../topics/deployment`) |

Here core is sim-core and api; ext is recorder, replay-worker, agent-runtime and job-worker; sandbox is sandbox-pool and sandbox-api; dev is vite.

## Optional data: the six UrbanScene3D cities

No download is needed to run: `make demo-world` generates the synthetic demo city ANet Synthetic City from a fixed seed. To add Shenzhen,
Shanghai, New York, San Francisco, Suzhou and Chicago locally, download the UrbanScene3D sampled point clouds and accept their terms
(non-commercial use only; redistribution of the data or any modified version is not allowed):

```sh
make fetch-data   # downloads 253 MB, unpacks to about 720 MB, verifies each sha256
make worlds       # builds the six World Packages locally, about 1 to 2.5 min
```

See {doc}`../topics/worlds` for the authorization and citation.

## Access from another machine

The server listens on loopback only. Forward the port on your own computer and open the same address:

```sh
ssh -N -L 8000:127.0.0.1:8000 -o ExitOnForwardFailure=yes -o Compression=no <user>@<server>
```

For a LAN demo, open the port explicitly and register the origin:

```sh
AWR_BIND=0.0.0.0 AWR_ORIGINS=http://<server-ip>:8000 make run
```

## Environment diagnostics

`make doctor` checks the architecture, Python and Node versions, dependency locks, ports, shared memory, disk, raw data and the default world,
and prints a fix command for each failed item; `make doctor DEEP=1` also verifies sha256 sums and runs deep world validation. `make setup`
runs a quick diagnosis at the end; an example is in {doc}`quickstart`.

## Building this documentation

The documentation site uses Sphinx, sphinx-rtd-theme and MyST Markdown. The sources are under `docs-site/` in the repository (`zh_CN/` and
`en/` with identical structure). Dependencies are pinned in `docs-site/requirements.txt` and installed into a separate virtual environment
`.venv-docs`, leaving the project dependencies untouched:

```sh
make docs-site            # builds both languages into docs-site/_build/html/{en,zh-cn}/latest and writes the root redirect page
make docs-site-linkcheck  # link check, results in docs-site/_build/linkcheck/
```
