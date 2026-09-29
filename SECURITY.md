# Security Policy

ANet-Drone4D runs a control plane: it accepts commands that move simulated aircraft today, and it is designed to carry
commands for real ones in later versions. A browser tab on the wrong page, or a stranger on the same network, must not
be able to fly anything. We take every report seriously.

## Reporting a vulnerability

Email **hi@anet0.com** with the subject `[SECURITY] ANet-Drone4D`. Please include:

- the commit you tested (`git rev-parse HEAD`), your platform, and the access mode (loopback or LAN)
- the output of `awr doctor --json` (check it for secrets before sending)
- reproduction steps or a proof of concept
- an impact assessment (what an attacker gains)

Please do **not** open a public issue, and never paste the contents of `runs/<run>/secret`, `runs/<run>/admin.token` or
a token into any report. We aim to acknowledge within 72 hours. Please give us reasonable time to ship a fix before
public disclosure.

ANet-Drone4D is pre-1.0: fixes land on `main`, and there are no maintained release branches yet.

## Security model

What V0.1 enforces. The design is in `docs/17-接口与实时协议规范.md` §3 and `docs/19-部署与运维说明书.md` §12; the
implementation and its tests are described in `docs/impl/M11-api-实现报告.md` and `docs/impl/M11-R-实现报告.md`.

| | Guarantee |
|---|---|
| **Loopback by default** | The API binds `127.0.0.1:8000`, and the internal zenoh bus listens on loopback only, with multicast scouting and shared memory off. Remote viewing uses SSH port forwarding. LAN mode must be chosen explicitly (`AWR_ACCESS_MODE=lan` or `AWR_BIND=0.0.0.0`), and the configuration is rejected if it has no Origin allowlist (`AWR_ORIGINS`). |
| **A token for every request** | Three roles: viewer, operator and admin. Tokens are HMAC-SHA256 signed with keys derived by HKDF from a 32-byte secret generated for each run; they expire after 12 hours and are valid only for their run. They travel in the `Authorization` header or the WebSocket subprotocol, never in a URL and never in a cookie. |
| **Passwords where it matters** | An admin token always needs the admin secret, which is generated for each run, printed only to a terminal and stored in `runs/<run>/admin.token` with mode 0600. In LAN mode an operator token needs it too. Only one operator holds the control seat at a time. |
| **Host and Origin checks** | Every HTTP request and WebSocket upgrade must carry an allowed `Host` (loopback names, or the hosts derived from the LAN allowlist), which blocks DNS rebinding. A browser request whose `Origin` is not on the allowlist is refused (403). |
| **Bounded load** | Commands, clock and environment writes are rate-limited per principal; message sizes, connections and subscriptions are capped; a slow client is disconnected instead of stalling the others. |
| **Commands go through admission** | Every command, from the UI or from an agent, is admitted by the simulation core, which checks the role, the control lease and the vehicle state. Agents collaborate through ANet but cannot issue a safety stop; V0.1 uses an in-process mock of the ANet network, so no agent traffic leaves the host. |
| **Secrets stay out of logs** | Logs, the effective configuration and `awr config print` mask secrets and tokens; the audit log (`runs/<run>/audit.jsonl`) records token ids, never tokens or passwords. Run directories are mode 0700 and secret files 0600. |
| **No real aircraft in V0.1** | V0.1 contains no real-vehicle adapter. If `real_ops.enabled: true`, `AWR_REAL_OPS=1` or the `field` profile appears in the configuration, the supervisor refuses to start (exit code 2). |

The full real-aircraft safety switch is designed for V0.5 (`docs/19-部署与运维说明书.md` §12.5) and is **not
implemented yet**: a deployment profile, a double configuration confirmation, an admin enable with a confirmation
token, and an on-site checklist must all hold before any command reaches a real aircraft, with an emergency stop that
leaves only land, hover and return-to-launch.

### What it does not protect against

- **Other accounts on the same host.** V0.1 assumes a dedicated machine: another local user can connect to the
  loopback bus (`127.0.0.1:7447`). Access control on the bus is planned for V0.5.
- **The network in LAN mode.** LAN mode is plain HTTP: tokens and the admin secret cross the network unencrypted.
  Prefer loopback plus SSH forwarding, and keep LAN mode on a trusted demo network.
- **The public internet.** ANet-Drone4D is not designed to be exposed to the internet before V1.0; that needs TLS
  termination and stronger authentication in front of it.
- **World files are public to whoever can reach the port.** The static `/worlds` files are served read-only without
  a token (the `Host` check still applies).
- **Flight safety.** The simulator is not a certified flight system, and its results are not evidence that a real
  flight is safe.

## Scope

In scope: the code and configuration in this repository (the API and realtime gateway, authentication, the supervisor
and `awr` CLI, the Web sandbox, the World Package tools, the Makefiles and scripts), and design flaws in the security
sections of `docs/`.

Out of scope: vulnerabilities in third-party dependencies (please report them upstream too, and tell us if
ANet-Drone4D is exposed), the UrbanScene3D dataset, and attacks that start from an already compromised account on the
host. Reports about the ANet network itself go to the same address; see
[ANetResearch/ANet](https://github.com/ANetResearch/ANet/blob/main/SECURITY.md).
