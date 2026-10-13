# Deployment

This page outlines the deployment kit of the public demo [drone4d.agentnetwork.org.cn](https://drone4d.agentnetwork.org.cn) and how this
documentation site is published. For individual commands, parameters and troubleshooting, the repository's
[tools/deploy/emax/README.md](https://github.com/ANetResearch/ANet-Drone4D/blob/main/tools/deploy/emax/README.md) is authoritative.

## Topology

One Ubuntu 24.04 server with 4 cores and no GPU, shared with other production services. One systemd unit `anet-drone4d` (system user `awr`)
hosts two process groups:

| Group | Processes | Notes |
|---|---|---|
| Shared show | supervisor, show-api, the sim-core of the shared show | Scenario S7; show-api listens on loopback 18640 only |
| Sandbox | sandbox-pool, sandbox-api, one sim-core per sandbox | sandbox-api listens on loopback 18641 only; inter-process bus on 18647 |

nginx terminates TLS and routes by path: `/api/sandbox/` goes to sandbox-api, the rest of `/api/` to show-api, `/worlds/` is served by nginx
directly from the world package directory, and `/sdk/` serves the Python SDK download. The capacity endpoint has a 1 s micro cache, and every
path is rate limited per address prefix.

## Resource limits

| Item | Value |
|---|---|
| CPU | `CPUQuota=250%`; with cgroup delegation, split into the subgroups `core/` and `sandbox/{ctl,plan,sb}` |
| Memory | `MemoryHigh=1.8G`, `MemoryMax=2G`; the site peaks at about 1.75 GB (including reclaimable file pages) |
| Sandbox concurrency | Computed by the capacity model from measured session cost; the public limit is currently 2, further visitors queue |
| Live viewing | 24 seats, 4 WebSockets per address prefix |
| Disk | At least 5 GB on the partition of `/opt`: seven worlds about 1.07 GB, derived caches about 1.0 GB, 3 releases kept, runs quota 2 GB |

## Deployment procedure

1. **Pack on the development machine.** `make deploy-pack` produces the deployment bundle (code, configuration, scenarios, airframes, demo
   frontend with build information, SDK, license files; about 15 MB) plus world packages and derived cache packages for the seven worlds.
   World packages are named by content version, so an unchanged world reuses the same file. The script verifies that the bundle contains no
   world source data, point cloud files, files over 5 MB, local credentials or deployment passphrases. `make deploy-dry-run` simulates the
   installation locally with deep validation.
2. **Upload.** The bundle and the world package directory are uploaded with rsync; about 1 GB the first time, only changed packages later.
3. **Install.** `install.sh` runs as root: it verifies the bundle, creates the user and directories, builds the release's virtual environment
   from the lock file and warms the numba cache, installs and deep-validates the world packages, installs the systemd unit and quotas,
   switches `current` and waits for show-api to become ready (rolling back to the previous release automatically when it is not ready within
   90 s), then runs a sandbox smoke test (create, ready, end) and keeps the latest 3 releases.
4. **nginx and certificates.** Done manually by operations: `nginx -t` first, then reload; certificates are issued with certbot's webroot
   method. `install.sh` never reloads nginx and never calls certbot.
5. **Verify.** Health endpoint, capacity endpoint, landing page and sandbox smoke test.

Updates follow the same procedure (only changed world packages are uploaded); `install.sh --rollback` returns to the previous release, and
`--status` shows the state.

## Constraints

- **Add this site only.** Other sites on the server, `nginx.conf` and certificates are never touched; the rate limit zones, cache zones,
  upstreams and map variables of this site all carry the `drone4d` prefix.
- **World packages are not committed.** The seven worlds are offered under authorization (see {doc}`worlds`); world packages travel only in
  deployment bundles, never into the git repository, and raw data is never downloaded on the server.
- **Secrets stay out of the repository, bundles and logs.** The internal identity passphrase is written as a file on the server only by
  authorized operations staff (`install.sh --admin-secret-file -` reads it from standard input); per-run signing keys and the sandbox pool keys
  are generated on the server.
- **Permission boundary of anonymous visitors.** Anonymous visitors can only watch the shared show; a sandbox is writable only by the
  visitor who claimed it, within the concurrency limit, world memory admission and prefix quotas. The public instance offers no server-heavy
  features such as GPU reconstruction, recording and replay, or the agent runtime.

## Operations tools

| Tool | Purpose |
|---|---|
| `sbctl.sh` | Sandbox pool operations: status, session list, end a session, maintenance mode, acceptance switch, temporary capacity reduction |
| `observe.py` | Production observation: 72 h service criteria (out of memory, unexpected restarts, real-time factor of the shared show) and seven-day statistics of S7 |
| `nginx-check.sh` | Runs `nginx -t` on the site file in a disposable container, with nginx comparison and end-to-end cases |
| `smoke.py` | Sandbox smoke test after installation |

## Publishing this documentation site

This site uses Sphinx, sphinx-rtd-theme and the Read the Docs directory layout, published under `https://docs.agentnetwork.org.cn/drone4d/`:

| Path | Content |
|---|---|
| `/drone4d/` | Root page: redirects by browser language, Chinese to `zh-cn` and everything else to `en` |
| `/drone4d/en/latest/` | English documentation |
| `/drone4d/zh-cn/latest/` | Chinese documentation |

`.readthedocs.yaml` at the repository root lets Read the Docs build the same sources. Self-hosted publishing takes two steps:

```sh
make docs-site                        # builds into docs-site/_build/html/
tools/deploy/docs/publish.sh          # prepares the publish directory and checksum list, and prints the rsync command
```

`publish.sh` only prepares `.cache/deploy/docs/drone4d/` locally and prints the upload command; it does not upload. The target is
`/var/www/docs.agentnetwork/public/drone4d/` on the documentation server (owner `www-data`); synchronization applies to the `drone4d/`
subdirectory only, never deletes or modifies anything else in that root, and needs no nginx change.
