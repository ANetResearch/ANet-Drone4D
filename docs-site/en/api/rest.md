# Open interface

ANet Drone4D exposes two HTTP interfaces and one realtime protocol:

| Interface | Prefix | Served by | For |
|---|---|---|---|
| Sandbox REST v1 | `/api/sandbox/v1` | sandbox-api (8002 locally; routed by path through nginx on the public instance) | Visitors, scripts and external agents creating and operating their own sandboxes |
| Local runtime API | `/api` | api (8000 locally) | Operators and administrators of the local shared show: worlds, fleet, commands, scenarios, runs and system |
| Realtime protocol `awr.rt.v1` | WebSocket | Both api processes | State streams, command calls, velocity setpoints and events |

The interface and the API have the same rights: every action in the browser maps onto the same interface and passes the same admission and
safety checks. The server provides the OpenAPI 3.1 document of the sandbox interface at `/api/sandbox/v1/openapi.json`, and the public
instance renders it with copyable examples at [`/sandbox/api`](https://drone4d.agentnetwork.org.cn/sandbox/api). Python programs should use
the {doc}`sdk`, which handles queueing, proof of work, renewal, resume after power saving and decoding of binary frames.

## Authentication

| Method | Applies to | Notes |
|---|---|---|
| Public | `meta`, `worlds`, `templates`, `capacity`, airframe and sensor catalogs, `challenge` and similar | No credentials, cacheable |
| Session token | `/sessions/{sid}/...` | Issued when the session is created, `Authorization: Bearer v1.<payload>.<sig>`, valid for that sid only; reissued on renewal |
| Queue ticket | `/queue/{ticket_id}...` | Issued on entering the queue, `Authorization: Bearer q1.<...>`, for the queue endpoints only |
| Token in body | `end-beacon`, `park-beacon` | Sent by the browser as a beacon when the page closes, with the token in the request body |
| Admin token | Protected endpoints of the local runtime API | Generated per run in `runs/<run>/admin.token` (0600) and exchanged for an access token through `POST /api/auth/token` |

In public mode, creating a session may require a proof of work: fetch a challenge with `GET /challenge`, solve it and submit it with the
create request; when required, the server returns the challenge with reason code 509.

## Error format

Errors are returned as `application/problem+json`; `code` is the project reason code (`packages/contracts/rt/reasons.json`). Sandbox reason
codes are in the range 500 to 599 and are unrelated to HTTP 5xx. The real response of the local shared show to `GET /api/worlds` without a
token:

```text
{"type":"urn:awr:reason:301","title":"AUTH_REQUIRED","status":401,"code":301,"name":"AUTH_REQUIRED","reason":"AUTH_REQUIRED","message":"缺少 token","detail":null,"remedy":"先签发 token","request_id":"01a135ae-3540-7d01-88af-813820ac3b51"}
```

| Field | Meaning |
|---|---|
| `code`, `name` | Reason code and name, for example 110 `PARAM_OUT_OF_RANGE`, 506 `SANDBOX_GONE`, 525 `NEST_INCOMPATIBLE`, 581 `NO_FEASIBLE_STATION`, 585 `PLAN_INPUT_TOO_LARGE` |
| `message` | Human-readable description (the server answers in Chinese; this example reads "token missing") |
| `detail` | Structured details, for example `{"field": "nest_id|home_enu_m", "why": "EXACTLY_ONE"}` |
| `remedy` | Suggested action (here "issue a token first") |
| `retry_after_ms` | Precise wait in milliseconds when rate limited; the `Retry-After` header carries whole seconds |
| `request_id` | Request ID; include it when reporting a problem |

## Idempotency and rate limits

Create requests (sessions, drones, targets, nests, tasks) accept an `Idempotency-Key` header, and retries with the same key return the same
result; the SDK generates keys automatically. In public mode each request category is rate limited per session with a token bucket:

| Category | Rate | Burst |
|---|---|---|
| General REST | 10 per second | 30 |
| Command calls | 20 per second | 40 |
| Clock | 2 per second | — |
| Entity changes | 5 per second | — |
| Tasks | 1 per second | — |
| Velocity setpoints | 50 Hz | — |

Exceeding a limit returns 429 with reason code 111, and the SDK retries automatically after `retry_after_ms`. Task inputs have their own
quotas; see {doc}`../guide/sandbox`.

## Realtime protocol

The sandbox realtime connection is a WebSocket on `GET /api/sandbox/v1/sessions/{sid}/rt` with the subprotocols `awr.rt.v1` and
`bearer.<token>`. After connecting, the client subscribes to topics (for example the session state `sandbox/session`, fleet state and
per-drone perception), and the server sends data according to each client's interest set and credit: the fleet as 32-byte Lite32 records and
the focused drone as 64-byte Full64 records in batches, with structured messages encoded as msgpack. Commands are issued as a `call` and
return accepted, running and the result in turn; velocity control sends setpoints over the client data channel at up to 50 Hz after the
`call`. The byte layouts are specified in the design document
[docs/17 interface and realtime protocol](https://github.com/ANetResearch/ANet-Drone4D/blob/main/docs/17-接口与实时协议规范.md).

The connection closes with code 4410 when the session ends; power-save parking disconnects with code 4430, and the client resubscribes after
resuming the session; switching the world disconnects with 1012, and the client reconnects to the new world.

## Sandbox REST v1 endpoints

The table below is generated from `packages/contracts/rest/sandbox.openapi.snapshot.json` when the documentation is built. Paths are relative
to the prefix `/api/sandbox/v1`; under "Priority", P1 marks optional capabilities, and endpoints that are not deployed return 404.

```{drone4d-endpoints} packages/contracts/rest/sandbox.openapi.snapshot.json
:kind: sandbox
```

## Local runtime API endpoints

The table below is generated from `packages/contracts/rest/openapi.snapshot.json`. These endpoints serve the local shared show (loopback
access); apart from health, readiness and version information they require a token. The public instance serves the viewing-related subset
read-only.

```{drone4d-endpoints} packages/contracts/rest/openapi.snapshot.json
:kind: runtime
```
