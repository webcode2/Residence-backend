# Latency and request architecture

Measured 25 Nov 2026 from inside Google Cloud Run (not from a laptop). Re-run with
`scripts/latency_probe.py` (deployed as the Cloud Run job `residence-latency-probe`).

## Where things run today

| Component | Location |
|---|---|
| API (`residence-api`) | Cloud Run, GCP `us-east5` (Columbus, Ohio) |
| Worker (`residence-worker`) | Cloud Run, GCP `us-east5`, private, scale to zero, 1 instance |
| Wake-up queue | Cloud Tasks `residence-worker`, GCP `us-east4` (not offered in `us-east5`) |
| Postgres | Neon, AWS `us-east-2` (Ohio), via the Neon pooler |
| Redis | Upstash Global, primary AWS `us-east-2`, replicas incl. Virginia and Cape Town |
| Web / mobile entry | Cloudflare Worker `residence.elastrocloud.com` → API |

## Measured latency (API and worker share the same network path)

From Cloud Run `us-east5`, 30 samples each, warm connections unless noted:

| Operation | Median | p90 |
|---|---|---|
| Postgres round trip (`SELECT 1`) — API → DB and worker → DB | 21.9 ms | 23.2 ms |
| App request, 1 indexed read (`BEGIN`/`SELECT`/`ROLLBACK`, pooled) | 66.2 ms | 68.5 ms |
| App request, 3 indexed reads | 110.9 ms | 111.8 ms |
| New Postgres connection (TCP + TLS + auth; once per pool slot) | 200 ms | — |
| Redis: create a token (`SET` with TTL) — API → Redis | 24.6 ms | 24.9 ms |
| Redis: read a token (`GET`) — API → Redis | 24.0 ms | 24.7 ms |
| Redis `PING` | 24.1 ms | 24.5 ms |
| First Redis command on a new connection (TLS) | 224 ms | — |

Rule of thumb today: **every Postgres or Redis round trip costs ~22–25 ms.** A
request's server time is roughly (number of round trips × 22 ms).

### Region comparison (TCP connect = 1 network round trip, median of 15)

| From → To | AWS `us-east-2` (Neon today) | AWS `us-east-1` | Neon pooler | Upstash Global |
|---|---|---|---|---|
| GCP `us-east5` (current) | 20.9 ms | 12.3 ms | 22.4 ms | 20.6 ms |
| GCP `us-east4` (N. Virginia) | 18.7 ms | **2.7 ms** | 19.1 ms | **2.4 ms** |

GCP Columbus and AWS Ohio are not network neighbours (~21 ms). GCP `us-east4` and AWS
`us-east-1` share the Ashburn data-centre cluster (~2.7 ms). Putting everything in
Northern Virginia (API, worker and Cloud Tasks in GCP `us-east4`; Neon and the Upstash
primary in AWS `us-east-1`) would cut each round trip roughly 8× and keep every server
in one region. Moving Neon needs a new Neon project in `us-east-1` and a dump/restore.

## Request architecture

**In the API request (respond with the result):** anything that only talks to Postgres
or Redis, plus calls the user must wait for.
- Login, registration (estate, resident), profile reads/updates, password changes.
- Payments: Bachs checkout creation and session verification run inside the request so
  the subscription is activated before responding. The Bachs webhook is also handled
  in-request.
- Visitor / registration / book-out token creation and verification (Redis fast path,
  Postgres for durable state).

**Queued for the worker (respond immediately, show "queued", finish in the background):**
work that calls slow external services or is long-running.
- Host / Property Owner welcome emails (Resend) — Postgres job table.
- SOS push deliveries (FCM), including retries and missed-alarm reports — Postgres outbox.
- History exports and purges — Postgres job table.
- Gate access logs, visitor-token state changes, chat messages — Redis queues.

How it works: the API commits the work, then calls `wake_worker()`
(`app/services/worker_wake.py`), which creates a Cloud Tasks task. Cloud Tasks POSTs the
private worker's `/internal/drain` with an OIDC token; the worker starts from zero,
drains everything due (`app/worker_app.py`), schedules a wake for the next retry time,
and scales back to zero. A 5-second Redis flag collapses bursts into one wake. Nothing
runs, and nothing is billed, while there is no work.

Notifications follow the same path: gate arrival/departure pushes, RFID and visitor
alert emails, chat FCM fallbacks, estate-settings sync pushes and security-staff welcome
emails are pushed to the Redis list `residence:notify:queue` by
`app/services/notification_queue.py` and sent by the worker (transient FCM failures are
retried up to 3 times; stale device tokens are not). Without a worker configured
(local / docker compose) they are sent before the request returns.

## Redis caching rules (cache-aside)

Every cached read follows the same pattern:

1. Read Redis first.
2. On a miss (never cached, invalidated, or TTL expired) read Postgres, then write the
   result back to Redis with its TTL.
3. Every create, update or delete of the underlying data deletes the Redis entry
   immediately after the Postgres commit, so the next read reloads fresh data.

| Cached data | Key | TTL | Invalidated by |
|---|---|---|---|
| Signed-in user profile (also `GET /users/me/profile`) | `residence:user:{app_id}:{email}` | 30 min | account creation (all 5 paths), profile / password / status / role changes, deletion, resend-welcome |
| A resident's visitor list (`GET /tokens/visitor/history`) | `residence:visitor_history:{app_id}:{user_id}` | 5 min | creating a visitor token, generating a book-out code |
| Visitor token live status (check-in / check-out) | `residence:token_live_status:{token_id}` | 24 h | written by every gate check-in / check-out; overlaid on the cached list in one `MGET` |
| Visitor / registration / book-out codes, RFID lookups, estate validity, subscription state | see `app/core/redis.py` | 20 min – 24 h | the matching create / revoke / update paths |

The profile entry carries a version (`v`); entries in an older shape are treated as misses.
Login, owner login and authentication all build it with `user_cache_payload()`.

## Security personnel (gate officers)

Caretakers register officers on the Admin **Security staff** page
(`POST /api/v1/security-staff`). Only caretakers can create security accounts. A new
officer receives a temporary password (shown once to the caretaker and emailed by the
worker) and is blocked from every endpoint, including gate verification, until they set
their own password. Caretakers can suspend or reinstate an officer
(`PATCH /api/v1/users/{id}/status`, effective on the officer's next request) and reset
their password (`POST /api/v1/security-staff/{id}/reset-password`).
