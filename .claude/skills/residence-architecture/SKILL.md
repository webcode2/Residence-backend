---
name: residence-architecture
description: Decisions, rules and procedures for the Residence estate app (FastAPI backend, Next.js Admin on Cloudflare, Flutter mobile with separate resident and gate modules, Cloud Run API + scale-to-zero worker, Neon Postgres, Upstash Redis). Read before changing backend request handling, caching, queues, auth/passwords, deployment, regions, or the mobile app structure, so agents do not undo or redo agreed work.
---

# Residence architecture: decisions and rules

These decisions were made with the project owner on 25 Nov 2026. Do not reverse any of
them without asking the owner. When you change something covered here, update this file
in the same change.

Identical copies live in `backend/`, `Admin/` and `mobile/` at
`.claude/skills/residence-architecture/SKILL.md` (and in the top-level `residence/`
folder). When you edit one, update all of them in the same change.

## 1. Repositories and branches

| Folder | GitHub repo | Working branch | Notes |
|---|---|---|---|
| `backend/` | `webcode2/Residence-backend` | `mvp` | FastAPI + SQLAlchemy async + Alembic |
| `Admin/` | `webcode2/Residence-pub_ad` | `ohio-api-host-rename` | Next.js on Cloudflare Workers (OpenNext) |
| `mobile/` | `webcode2/residence-pass-flutter-app` | `host-rename-release-ids` | Flutter, package name `estate` |

The Admin and mobile branches are **not merged into `main` yet**; the owner merges.
The top-level `residence/` folder is not a git repo.

## 2. Infrastructure (Google Cloud project `residence-elastrocloud`)

| Piece | Where | Name / URL |
|---|---|---|
| API | Cloud Run `us-east5` (Columbus) | `residence-api` → `https://residence-api-136222598323.us-east5.run.app` |
| Worker | Cloud Run `us-east5`, private | `residence-worker` → `https://residence-worker-136222598323.us-east5.run.app` |
| Wake-up queue | Cloud Tasks `us-east4` (not offered in us-east5) | `residence-worker` |
| Postgres | Neon, AWS `us-east-2` | secret `DB_URL` |
| Redis | Upstash **Global**, primary Ohio | secrets `REDIS_URL_US`, `UPSTASH_REDIS_REST_TOKEN_US` |
| Admin + web entry | Cloudflare Worker `residence-admin` | `https://residence.elastrocloud.com` (rewrites `/api/v1/*` to the API) |

- **Everything scales to zero; CPU only during requests** (`--cpu-throttling`,
  min-instances 0). The owner rejected always-on CPU and min-instances. Do not change.
- The Africa (`africa-south1`) deployment was deleted on purpose. Do not recreate it.
- Service accounts: `residence-api@` runs API and worker (Secret Manager accessor,
  Cloud Tasks enqueuer); `residence-tasks@` signs the OIDC token Cloud Tasks uses to call
  the private worker (has `run.invoker` on the worker only).
- Secrets live in **Secret Manager** only. Never paste secret values in chat, logs, or
  commits. To add one from `.env`, write it to a temp file, `gcloud secrets versions add
  --data-file`, delete the file. `.env`, `.env.local`, Firebase admin key and
  `.seed*.json` are git-ignored and excluded by `.gcloudignore`.

### Measured latency (from Cloud Run us-east5)
Postgres round trip ~22 ms; typical 1-query request ~66 ms; Redis GET/SET ~24 ms. GCP
us-east4 ↔ AWS us-east-1 measured ~2.7 ms, so **moving everything to Northern Virginia**
(API/worker/Cloud Tasks in us-east4, Neon + Upstash primary in AWS us-east-1) is the
recommended next infra step. **Not approved yet** — ask the owner. Details and re-run
instructions: `backend/docs/LATENCY_AND_ARCHITECTURE.md`, `backend/scripts/latency_probe.py`.

## 3. Request architecture (backend)

**In the request (respond with the result):** anything that only needs Postgres or
Redis: login, registration, profile, tokens, verification, **payments** (Bachs checkout
and verification run in the request so the user is activated before the response).

**Queued for the worker (respond immediately):** anything slow or external — emails,
FCM pushes, exports, purges, gate access logs, visitor-token state, chat persistence.

- Producers enqueue, then call `wake_worker()` (`app/services/worker_wake.py`), which
  creates a Cloud Tasks task → worker `POST /internal/drain` (`app/worker_app.py`).
  A 5 s Redis flag collapses bursts; the worker drains everything due, schedules its
  own next wake for future retries, then sleeps.
- Notifications use `notification_queue.dispatch(kind, ...)`
  (`app/services/notification_queue.py`): kinds `push`, `push_many`, `email_alert`,
  `chat_push`, `settings_sync`, `staff_welcome`. Without a worker configured
  (local/tests) they are sent before the request returns.
- **Never** do slow work inline in a request, and **never** use `asyncio.create_task` /
  `BackgroundTasks` for external calls: with request-only CPU they stall after the
  response. The owner explicitly rejected processing jobs inline ("SERVERLESS_MODE").
- Durable jobs (SOS deliveries, welcome emails, history jobs) live in Postgres tables
  with leases and `SKIP LOCKED`; Redis lists carry access logs, token state, chat and
  notifications. Queue drains must skip or repair rows referencing deleted users so
  one bad item cannot block a queue.
- bcrypt always runs off the event loop (`get_password_hash_async`,
  `verify_password_async`).

## 4. Redis caching (cache-aside)

Read Redis → on miss or TTL expiry read Postgres and re-cache → **delete the entry right
after every create, update or delete commit**.

| Data | Key | TTL | Invalidated by |
|---|---|---|---|
| Signed-in profile (auth + `GET /users/me/profile`) | `residence:user:{app_id}:{email}` | 30 min | every account creation path, profile/password/status/role change, deletion, resend-welcome |
| Resident visitor list (`GET /tokens/visitor/history`) | `residence:visitor_history:{app_id}:{user_id}` | 5 min | token or book-out creation |
| Token live status (check-in/out) | `residence:token_live_status:{token_id}` | 24 h | written on each gate event; overlaid with one MGET |

Build the profile entry only with `user_cache_payload()` in `app/core/redis.py`; bump
`USER_CACHE_VERSION` when its shape changes.

## 5. Accounts, auth and naming

- **Password rule: minimum 8 characters, maximum 72 UTF-8 bytes, everywhere** (backend
  `PASSWORD_MIN_LENGTH` in `app/services/auth.py`, mobile screens, Admin forms).
- Estate sign-in matches email case-insensitively (exact match preferred; never guesses
  between two accounts differing only by case). Lockout counts all capitalisations.
- **Platform owner** is created on container start from `PLATFORM_OWNER_EMAIL` /
  `PLATFORM_OWNER_PASSWORD` (secret) / `PLATFORM_OWNER_NAME` if missing; an existing
  account is never modified (`app/services/owner_bootstrap.py`). It is a separate
  `GLOBAL` account even if an estate login uses the same email.
- **Security officers** are registered only by caretakers on the Admin *Security staff*
  page (`POST /api/v1/security-staff`). They get a temporary password (shown once,
  emailed via the worker) and are blocked from every endpoint until they set their own.
  Suspend/reinstate via `PATCH /users/{id}/status`; reset requires a confirmation dialog.
- User-facing wording is **"Host / Property Owner"**; the stored role value, API paths
  (`/api/v1/landlords`) and DB tables keep `landlord` — do not rename those.

## 6. Mobile app structure (one install, two separate apps)

The owner wants the resident app and the gate (security) app **fully separate inside
one install**, so the gate app can be redesigned without affecting residents.

```
lib/
  main.dart          bootstrap; after sign-in picks ResidentApp or GateApp by role
  shared/            sign-in and app-wide pieces only
    auth/ config/ models/user_model.dart notifications/ sos/ account/ chat/chat_service.dart ui/
  resident/          resident app: screens, state (visitor_store), data, models, widgets
  gate/              gate app: screens, state (gatehouse_store, gate_visitor_store),
                     data, models, its own chat screen (gate_chat_thread_screen)
```

- `gate/` must never import `resident/`; `resident/` never imports `gate/`; `shared/`
  imports neither. Enforced by `mobile/test/module_boundaries_test.dart`.
- Put shared code in `shared/` only when both apps genuinely need it (sign-in, top bar,
  theme, push, SOS). Otherwise copy it into the module that needs it. The gate copies
  (`GateVisitorStore`, `GateChatThreadScreen`, gate `visitor_pass`/`visitor_api`) exist
  for this reason — do not merge them back into shared code.
- **Gate redesign work happens only in `lib/gate/`.**

## 7. How to test and deploy

- **Backend tests** (never against Neon): throwaway containers on a private network,
  `.env` masked:
  `docker run postgres:15-alpine` + `redis:7-alpine`, create `residence_test`, then run
  `backend-backend` image with `-v backend:/app -v empty.env:/app/.env`,
  `DB_URL=postgresql://rp:rp@<pg>:5432/residence`, `REDIS_URL=redis://<redis>:6379/0`,
  `python -m pytest -q`. Known pre-existing failure:
  `test_chat_audit_unit::test_removing_chat_account_revokes_instead_of_deleting_history`
  (stale mock for the owner's `delete_user` rewrite).
- **Mobile**: `flutter analyze` and `flutter test` in `mobile/`.
- **Admin**: `npx tsc --noEmit` in `Admin/`.
- **Deploy backend** (same image for both): `gcloud builds submit backend --region
  us-east5 --tag us-east5-docker.pkg.dev/residence-elastrocloud/cloud-run-source-deploy/residence-api:<tag>`,
  then `gcloud run deploy residence-worker --image ...` and
  `gcloud run deploy residence-api --image ...` (region us-east5). Entrypoint runs
  migrations (API only; worker has `SKIP_MIGRATIONS=1`) and the owner bootstrap.
- **Deploy Admin**: `npm run deploy` in `Admin/` (wrangler; vars in `wrangler.jsonc`).
- **Run the mobile app**: `python tool/dev_run.py --device <serial>` in `mobile/`
  (live API, hot reload on save; `R` + Enter = hot restart).
- Emulator `Medium_Phone_API_37.0` uses `hw.gpu.mode=host` on the NVIDIA GTX 1650.
- Agents may **not** type passwords into the live backend; the owner signs in. A
  `residence-api-staging` service with its own database would allow agent sign-ins.

## 8. Open items (ask before acting)

1. Move all services to Northern Virginia (see section 2).
2. Stale `delete_user` unit test (section 7).
3. Security officer "Peter James" was registered with `@gmaill.com` (typo).
4. Merge Admin and mobile branches into `main` when the owner approves.
5. Staging environment for agent-driven end-to-end testing.
